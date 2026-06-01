#!/usr/bin/env python3
import sys
import json
import time
import socket
import threading
import subprocess
import hashlib
import sqlite3
import argparse
import os
from typing import Dict, Any, Optional, Tuple
from time import perf_counter

from semantic_cache import SemanticCache, build_semantic_text


def log(msg: str) -> None:
    print(f"[proxy-core] {msg}", file=sys.stderr, flush=True)


def start_upstream(cmd):
    log(f"Starting upstream → {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _safe_json_load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class CacheDB:
    """
    SQLite-backed:
      1) requests      - append-only audit/history
      2) cache_entries - actual cache

    Also stores performance metrics in requests:
      - latency_ms
      - upstream_latency_ms
      - cache_status
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init_db()
        self._migrate_requests_table()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    caller_id TEXT,
                    remote_addr TEXT,
                    server_id TEXT NOT NULL,
                    op TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    cache_key TEXT,
                    hit INTEGER NOT NULL,
                    expires_at REAL,
                    response_json TEXT
                );
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_requests_cache_key ON requests(cache_key);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_requests_server_op ON requests(server_id, op);")

            con.execute("""
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    response_json TEXT NOT NULL
                );
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires_at ON cache_entries(expires_at);")

    def _migrate_requests_table(self) -> None:
        def add_col(sql: str) -> None:
            try:
                con.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise

        with self._lock, self._connect() as con:
            add_col("ALTER TABLE requests ADD COLUMN latency_ms REAL;")
            add_col("ALTER TABLE requests ADD COLUMN upstream_latency_ms REAL;")
            add_col("ALTER TABLE requests ADD COLUMN cache_status TEXT;")

    @staticmethod
    def canonical_json(obj: Any) -> str:
        return json.dumps(
            obj,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def make_cache_key(server_id: str, op: str, params: dict) -> str:
        canon = CacheDB.canonical_json(params)
        raw = f"{server_id}|{op}|{canon}".encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _deep_remove_keys(obj: Any, keys_to_remove: set) -> Any:
        if isinstance(obj, dict):
            return {
                k: CacheDB._deep_remove_keys(v, keys_to_remove)
                for k, v in obj.items()
                if k not in keys_to_remove
            }
        if isinstance(obj, list):
            return [CacheDB._deep_remove_keys(x, keys_to_remove) for x in obj]
        return obj

    @staticmethod
    def normalize_tools_call_params(
        params: dict,
        ignore_argument_keys: set,
        default_argument_values: dict,
    ) -> dict:
        tool_name = params.get("name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}

        if default_argument_values:
            for k, v in default_argument_values.items():
                if k not in args:
                    args[k] = v

        args_norm = CacheDB._deep_remove_keys(args, ignore_argument_keys or set())
        return {"name": tool_name, "arguments": args_norm}

    @staticmethod
    def make_cache_key_from_obj(server_id: str, op: str, obj: dict) -> str:
        canon = CacheDB.canonical_json(obj)
        raw = f"{server_id}|{op}|{canon}".encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    def get_cache_entry(self, cache_key: str, now_ts: float) -> Optional[Tuple[dict, float]]:
        with self._lock, self._connect() as con:
            row = con.execute("""
                SELECT response_json, expires_at
                FROM cache_entries
                WHERE cache_key = ?
                LIMIT 1;
            """, (cache_key,)).fetchone()

            if not row:
                return None

            response_json, expires_at = row[0], float(row[1])

            if expires_at <= now_ts:
                con.execute("DELETE FROM cache_entries WHERE cache_key = ?;", (cache_key,))
                return None

        try:
            return json.loads(response_json), expires_at
        except Exception:
            with self._lock, self._connect() as con:
                con.execute("DELETE FROM cache_entries WHERE cache_key = ?;", (cache_key,))
            return None

    def set_cache_entry(self, cache_key: str, expires_at: float, response_obj: dict) -> None:
        response_json = self.canonical_json(response_obj)
        with self._lock, self._connect() as con:
            con.execute("""
                INSERT INTO cache_entries (cache_key, expires_at, response_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    response_json = excluded.response_json;
            """, (cache_key, expires_at, response_json))

    def cleanup_expired_cache(self, now_ts: float) -> int:
        with self._lock, self._connect() as con:
            cur = con.execute("DELETE FROM cache_entries WHERE expires_at <= ?;", (now_ts,))
            return cur.rowcount if cur is not None else 0

    def log_row(
        self,
        ts: float,
        caller_id: str,
        remote_addr: str,
        server_id: str,
        op: str,
        params: dict,
        cache_key: Optional[str],
        hit: int,
        expires_at: Optional[float],
        response_obj: Optional[dict],
        latency_ms: Optional[float],
        upstream_latency_ms: Optional[float],
        cache_status: Optional[str],
    ) -> None:
        params_json = self.canonical_json(params)
        response_json = self.canonical_json(response_obj) if response_obj is not None else None

        with self._lock, self._connect() as con:
            con.execute("""
                INSERT INTO requests (
                    ts, caller_id, remote_addr, server_id, op, params_json,
                    cache_key, hit, expires_at, response_json,
                    latency_ms, upstream_latency_ms, cache_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                ts, caller_id, remote_addr, server_id, op, params_json,
                cache_key, hit, expires_at, response_json,
                latency_ms, upstream_latency_ms, cache_status
            ))


class Upstream:
    def __init__(self, server_id: str, cmd):
        self.server_id = server_id
        self.cmd = list(cmd)

        self._cv = threading.Condition()
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}

        self.proc = None
        self._start_process()

    def _start_process(self) -> None:
        """
        Start the upstream MCP subprocess and attach fresh reader threads.
        """
        self.proc = start_upstream(self.cmd)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _fail_all_pending(self, message: str) -> None:
        """
        Mark all pending requests as completed with an error.
        This prevents waiting threads from hanging if the upstream process dies.
        """
        with self._cv:
            for req_id, slot in list(self._pending.items()):
                slot["msg"] = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32002, "message": message},
                }
                slot["done"] = True
            self._cv.notify_all()

    def _is_dead(self) -> bool:
        return self.proc is None or self.proc.poll() is not None

    def _restart_if_dead(self) -> None:
        """
        Self-healing path:
        if the upstream MCP subprocess exited, start it again before handling
        the next request. This handles dead/crashed local MCP server processes.
        """
        if not self._is_dead():
            return

        exit_code = None
        try:
            exit_code = self.proc.poll() if self.proc is not None else None
        except Exception:
            pass

        log(f"[{self.server_id}] upstream process is not running; restarting (exit_code={exit_code})")

        self._fail_all_pending(f"Upstream process died; restarting server '{self.server_id}'")

        try:
            if self.proc is not None:
                try:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    if self.proc.stdout:
                        self.proc.stdout.close()
                except Exception:
                    pass
                try:
                    if self.proc.stderr:
                        self.proc.stderr.close()
                except Exception:
                    pass
        except Exception:
            pass

        self._start_process()

    def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return

        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                if line:
                    log(f"[{self.server_id}][stderr] {line}")
        except Exception as e:
            log(f"[{self.server_id}] stderr reader stopped: {e!r}")

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return

        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception as e:
                    log(f"[{self.server_id}] Bad JSON from upstream: {e!r} :: {raw}")
                    continue

                msg_id = msg.get("id")
                if msg_id is None:
                    continue

                with self._cv:
                    slot = self._pending.get(msg_id)
                    if slot is not None:
                        slot["msg"] = msg
                        slot["done"] = True
                        self._cv.notify_all()
        except Exception as e:
            log(f"[{self.server_id}] stdout reader stopped: {e!r}")
        finally:
            # If this reader belongs to the current process and the process is dead,
            # release any threads waiting for responses from that process.
            if proc is self.proc and self._is_dead():
                log(f"[{self.server_id}] upstream stdout closed")
                self._fail_all_pending(f"Upstream process for '{self.server_id}' stopped")

    def request(self, method: str, params: Optional[dict], timeout: float) -> Dict[str, Any]:
        with self._cv:
            self._restart_if_dead()

            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = {"done": False, "msg": None}

            req = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                req["params"] = params

            try:
                self.proc.stdin.write(json.dumps(req, ensure_ascii=True) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                self._pending.pop(req_id, None)

                # If the write failed because the process died between the health
                # check and the write, restart it so the next request can recover.
                try:
                    if self._is_dead():
                        self._restart_if_dead()
                except Exception as restart_err:
                    log(f"[{self.server_id}] restart after write failure failed: {restart_err!r}")

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"Upstream write failed: {e!r}"}
                }

            deadline = time.time() + timeout
            while not self._pending[req_id]["done"]:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._pending.pop(req_id, None)
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32001, "message": "Upstream timeout"}
                    }
                self._cv.wait(timeout=remaining)

            msg = self._pending.pop(req_id)["msg"]
            return msg

class ProxyCore:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else 0.0

        self.config = _safe_json_load(config_path)
        listen = self.config.get("listen", {})
        self.host = listen.get("host", "0.0.0.0")
        self.port = int(listen.get("port", 8765))

        self.defaults = self.config.get("defaults", {})
        self.default_ttl = int(self.defaults.get("cache_ttl_s", 0))
        self.default_timeouts = self.defaults.get("timeouts_s", {
            "initialize": 10, "tools/list": 10, "tools/call": 30
        })

        self.cache_key_policy = self.config.get("cache_key_policy", {})
        self.ck_enabled = bool(self.cache_key_policy.get("enabled", False))
        self.ck_tools_call = self.cache_key_policy.get("tools_call", {})
        self.ck_ignore_arg_keys = set(self.ck_tools_call.get("ignore_argument_keys", []))
        self.ck_default_arg_values = dict(self.ck_tools_call.get("default_argument_values", {}))

        self.servers_cfg: Dict[str, dict] = self.config.get("servers", {})
        if not self.servers_cfg:
            raise RuntimeError("Config has no 'servers' entries.")

        self._config_lock = threading.Lock()
        self._client_registry_lock = threading.Lock()

        # caller_id -> {server_id -> last_seen_ts}
        self.client_registry: Dict[str, Dict[str, float]] = {}

        self.db = CacheDB(path=self.config.get("db_path", "proxy_cache.sqlite"))

        self.upstreams: Dict[str, Upstream] = {}
        for sid, scfg in self.servers_cfg.items():
            cmd = scfg.get("cmd")
            if not isinstance(cmd, list) or not cmd:
                raise RuntimeError(f"Server '{sid}' has invalid/missing cmd")
            self.upstreams[sid] = Upstream(sid, cmd)

        sem_cfg = self.config.get("semantic_cache", {})
        self.semantic_cache_enabled = bool(sem_cfg.get("enabled", False))
        self.semantic_cache: Optional[SemanticCache] = None

        if self.semantic_cache_enabled:
            try:
                self.semantic_cache = SemanticCache(
                    qdrant_url=sem_cfg.get("qdrant_url", "http://localhost:6333"),
                    collection_name=sem_cfg.get("collection_name", "semantic_cache"),
                    model_name=sem_cfg.get("model_name", "sentence-transformers/all-MiniLM-L6-v2"),
                    score_threshold=float(sem_cfg.get("score_threshold", 0.85)),
                )
                log("[SEMANTIC CACHE] enabled")
            except Exception as e:
                log(f"[SEMANTIC CACHE] init failed, disabling: {e!r}")
                self.semantic_cache_enabled = False
                self.semantic_cache = None

        self.gc_cfg = self.config.get("cache_gc", {"enabled": True, "on_startup": True, "interval_s": 60})
        self._stop_gc = threading.Event()

        if self.gc_cfg.get("enabled", True):
            if self.gc_cfg.get("on_startup", True):
                try:
                    now = time.time()
                    deleted = self.db.cleanup_expired_cache(now)
                    if deleted:
                        log(f"[CACHE GC] startup deleted_expired={deleted}")

                    if self.semantic_cache_enabled and self.semantic_cache is not None:
                        sem_deleted = self.semantic_cache.cleanup_expired(now)
                        if sem_deleted:
                            log(f"[SEMANTIC CACHE GC] startup deleted_expired={sem_deleted}")
                except Exception as e:
                    log(f"[CACHE GC] startup error: {e!r}")

            threading.Thread(target=self._cache_gc_loop, daemon=True).start()

    def _register_client_server(self, caller_id: str, server_id: Optional[str]) -> None:
        if not caller_id or not server_id:
            return

        if server_id == "proxy":
            return

        with self._client_registry_lock:
            self.client_registry.setdefault(caller_id, {})[server_id] = time.time()

    def _client_seen_servers(self, caller_id: str) -> Dict[str, float]:
        with self._client_registry_lock:
            return dict(self.client_registry.get(caller_id, {}))

    def _maybe_reload_config(self) -> None:
        try:
            current_mtime = os.path.getmtime(self.config_path)
        except OSError:
            return

        if current_mtime <= self.config_mtime:
            return

        with self._config_lock:
            try:
                current_mtime = os.path.getmtime(self.config_path)
                if current_mtime <= self.config_mtime:
                    return

                new_config = _safe_json_load(self.config_path)
                new_servers_cfg = new_config.get("servers", {})
                if not isinstance(new_servers_cfg, dict):
                    log("[CONFIG RELOAD] ignored: 'servers' is not a dictionary")
                    return

                added = []
                for sid, scfg in new_servers_cfg.items():
                    if sid in self.upstreams:
                        continue

                    cmd = scfg.get("cmd")
                    if not isinstance(cmd, list) or not cmd:
                        log(f"[CONFIG RELOAD] ignored server '{sid}': invalid/missing cmd")
                        continue

                    self.upstreams[sid] = Upstream(sid, cmd)
                    added.append(sid)

                self.config = new_config
                self.servers_cfg = new_servers_cfg
                self.config_mtime = current_mtime

                if added:
                    log(f"[CONFIG RELOAD] added servers: {', '.join(sorted(added))}")
                else:
                    log("[CONFIG RELOAD] config refreshed; no new upstreams added")

            except Exception as e:
                log(f"[CONFIG RELOAD] failed: {e!r}")

    def _build_client_config_snippet(
        self,
        missing_servers,
        endpoint_path: str,
        core_host: str,
        core_port: int,
        caller_id: str,
        python_command: str = "python3",
    ) -> Dict[str, Any]:
        snippet = {}

        for sid in sorted(missing_servers):
            snippet[sid] = {
                "command": python_command,
                "args": [
                    endpoint_path,
                    "--server-id", sid,
                    "--core-host", core_host,
                    "--core-port", str(core_port),
                    "--caller-id", caller_id,
                ],
            }

        return snippet

    def _proxy_status(self, caller_id: str, params: Optional[dict] = None) -> Dict[str, Any]:
        self._maybe_reload_config()

        params = params or {}

        endpoint_path = params.get("endpoint_path", "/ABSOLUTE/PATH/TO/proxy_endpoint.py")
        core_host = params.get("core_host", self.host)
        core_port = int(params.get("core_port", self.port))
        python_command = params.get("python_command", "python3")

        proxy_servers = sorted(self.upstreams.keys())

        seen_map = self._client_seen_servers(caller_id)
        client_seen_servers = sorted(s for s in seen_map.keys() if s in self.upstreams)

        missing_servers = sorted(set(proxy_servers) - set(client_seen_servers))

        return {
            "caller_id": caller_id,
            "proxy_servers": proxy_servers,
            "client_seen_servers": client_seen_servers,
            "missing_servers": missing_servers,
            "suggested_mcpServers": self._build_client_config_snippet(
                missing_servers=missing_servers,
                endpoint_path=endpoint_path,
                core_host=core_host,
                core_port=core_port,
                caller_id=caller_id,
                python_command=python_command,
            ),
            "notes": [
                "The proxy did not edit the client configuration file.",
                "client_seen_servers is based on server_ids observed from this caller_id at runtime.",
                "The virtual server_id 'proxy' is only for proxy management tools and is not counted as a real upstream server.",
                "If a server appears under missing_servers, add its suggested snippet to the client's mcpServers config and restart/reload the client."
            ],
        }

    def _proxy_tools_list(self) -> Dict[str, Any]:
        return {
            "tools": [
                {
                    "name": "proxy_discover_servers",
                    "description": "Discover proxy-managed servers and suggest missing client mcpServers entries.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "endpoint_path": {
                                "type": "string",
                                "description": "Absolute path to proxy_endpoint.py on the client machine."
                            },
                            "core_host": {
                                "type": "string",
                                "description": "Proxy-core host/IP as reachable from the client."
                            },
                            "core_port": {
                                "type": "integer",
                                "description": "Proxy-core TCP port."
                            },
                            "python_command": {
                                "type": "string",
                                "description": "Python command to use in the generated client config snippet."
                            }
                        },
                        "additionalProperties": False
                    }
                }
            ]
        }

    def _mcp_text_result(self, obj: Any) -> Dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(obj, ensure_ascii=False, indent=2)
                }
            ],
            "isError": False
        }

    def _cache_gc_loop(self) -> None:
        interval = int(self.gc_cfg.get("interval_s", 60))
        if interval < 1:
            interval = 60

        while not self._stop_gc.is_set():
            try:
                now = time.time()
                deleted = self.db.cleanup_expired_cache(now)
                if deleted:
                    log(f"[CACHE GC] deleted_expired={deleted}")

                if self.semantic_cache_enabled and self.semantic_cache is not None:
                    sem_deleted = self.semantic_cache.cleanup_expired(now)
                    if sem_deleted:
                        log(f"[SEMANTIC CACHE GC] deleted_expired={sem_deleted}")
            except Exception as e:
                log(f"[CACHE GC] error: {e!r}")
            self._stop_gc.wait(interval)

    def _cache_enabled_for(self, server_id: str) -> bool:
        """
        Return whether any caching is enabled for this server.

        If cache_enabled is false in the server config:
        - skip SQLite exact cache lookup
        - skip Qdrant semantic cache lookup
        - skip cache storage
        - always call the upstream server
        """
        scfg = self.servers_cfg.get(server_id, {})
        return bool(scfg.get("cache_enabled", True))

    def _semantic_cache_enabled_for(self, server_id: str) -> bool:
        """
        Return whether semantic caching is enabled for this server.

        This can be disabled per server while still allowing exact caching.
        Useful for deterministic/value-sensitive tools such as calculator,
        unit conversion, or time-related servers.
        """
        scfg = self.servers_cfg.get(server_id, {})
        return bool(scfg.get("semantic_cache_enabled", True))

    def _ttl_for(self, server_id: str) -> int:
        if not self._cache_enabled_for(server_id):
            return 0

        scfg = self.servers_cfg.get(server_id, {})
        return int(scfg.get("cache_ttl_s", self.default_ttl))

    def _timeout_for(self, server_id: str, op: str) -> float:
        scfg = self.servers_cfg.get(server_id, {})
        timeouts = scfg.get("timeouts_s", {})
        if op in timeouts:
            return float(timeouts[op])
        return float(self.default_timeouts.get(op, 10))

    def _make_tools_call_cache_key(self, server_id: str, params: dict) -> str:
        if self.ck_enabled:
            norm = CacheDB.normalize_tools_call_params(
                params,
                ignore_argument_keys=self.ck_ignore_arg_keys,
                default_argument_values=self.ck_default_arg_values,
            )
            return CacheDB.make_cache_key_from_obj(server_id, "tools/call", norm)
        return self.db.make_cache_key(server_id, "tools/call", params)

    def _build_semantic_text(self, server_id: str, params: dict) -> str:
        return build_semantic_text(
            server_id=server_id,
            params=params,
            normalize=self.ck_enabled,
            ignore_argument_keys=self.ck_ignore_arg_keys,
            default_argument_values=self.ck_default_arg_values,
        )

    def _make_semantic_point_id(self, server_id: str, semantic_text: str) -> int:
        raw = f"{server_id}|{semantic_text}".encode("utf-8", errors="replace")
        digest = hashlib.sha256(raw).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=False)

    def handle(self, req: Dict[str, Any], remote_addr: str) -> Dict[str, Any]:
        ts = time.time()
        t0 = perf_counter()

        op = req.get("op")
        server_id = req.get("server_id")
        endpoint_id = req.get("id")
        params = req.get("params") or {}
        caller_id = req.get("caller_id") or "unknown"

        def ok(result: Any) -> Dict[str, Any]:
            return {"id": endpoint_id, "result": result}

        def err(message: str, code: int) -> Dict[str, Any]:
            return {"id": endpoint_id, "error": {"code": code, "message": message}}

        self._maybe_reload_config()

        if server_id == "proxy":
            if op == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {
                        "name": "proxy",
                        "version": "0.1.0",
                        "description": "Proxy management and discovery tools"
                    }
                }

                latency_ms = (perf_counter() - t0) * 1000.0
                self.db.log_row(
                    ts, caller_id, remote_addr, "proxy", op, params,
                    cache_key=None, hit=0, expires_at=None, response_obj=result,
                    latency_ms=latency_ms, upstream_latency_ms=None, cache_status="NA"
                )
                return ok(result)

            if op == "tools/list":
                result = self._proxy_tools_list()

                latency_ms = (perf_counter() - t0) * 1000.0
                self.db.log_row(
                    ts, caller_id, remote_addr, "proxy", op, params,
                    cache_key=None, hit=0, expires_at=None, response_obj=result,
                    latency_ms=latency_ms, upstream_latency_ms=None, cache_status="NA"
                )
                return ok(result)

            if op == "tools/call":
                tool_name = params.get("name")

                if tool_name == "proxy_discover_servers":
                    tool_args = params.get("arguments") or {}
                    if not isinstance(tool_args, dict):
                        tool_args = {}

                    result = self._proxy_status(caller_id=caller_id, params=tool_args)
                    tool_result = self._mcp_text_result(result)

                    latency_ms = (perf_counter() - t0) * 1000.0
                    self.db.log_row(
                        ts, caller_id, remote_addr, "proxy", op, params,
                        cache_key=None, hit=0, expires_at=None, response_obj=result,
                        latency_ms=latency_ms, upstream_latency_ms=None, cache_status="NA"
                    )
                    return ok(tool_result)

                latency_ms = (perf_counter() - t0) * 1000.0
                error_obj = {"error": {"code": -32602, "message": f"Unknown proxy tool: {tool_name}"}}
                self.db.log_row(
                    ts, caller_id, remote_addr, "proxy", op, params,
                    cache_key=None, hit=0, expires_at=None,
                    response_obj=error_obj,
                    latency_ms=latency_ms, upstream_latency_ms=None, cache_status="ERROR"
                )
                return err(f"Unknown proxy tool: {tool_name}", -32602)

            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(
                ts, caller_id, remote_addr, "proxy", op or "unknown", params,
                cache_key=None, hit=0, expires_at=None,
                response_obj={"error": {"code": -32601, "message": f"Unknown proxy op: {op}"}},
                latency_ms=latency_ms, upstream_latency_ms=None, cache_status="ERROR"
            )
            return err(f"Unknown proxy op: {op}", -32601)

        if op == "proxy/status":
            result = self._proxy_status(caller_id=caller_id, params=params)

            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(
                ts, caller_id, remote_addr, "proxy", op, params,
                cache_key=None, hit=0, expires_at=None, response_obj=result,
                latency_ms=latency_ms, upstream_latency_ms=None, cache_status="NA"
            )
            return ok(result)

        if op not in ("initialize", "tools/list", "tools/call"):
            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(
                ts, caller_id, remote_addr, server_id or "unknown", op or "unknown", params,
                cache_key=None, hit=0, expires_at=None,
                response_obj={"error": {"code": -32601, "message": f"Unknown op: {op}"}},
                latency_ms=latency_ms, upstream_latency_ms=None, cache_status="ERROR"
            )
            return err(f"Unknown op: {op}", -32601)

        if server_id not in self.upstreams:
            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(
                ts, caller_id, remote_addr, server_id or "unknown", op, params,
                cache_key=None, hit=0, expires_at=None,
                response_obj={"error": {"code": -32602, "message": f"Unknown server_id: {server_id}"}},
                latency_ms=latency_ms, upstream_latency_ms=None, cache_status="ERROR"
            )
            return err(f"Unknown server_id: {server_id}", -32602)

        self._register_client_server(caller_id, server_id)

        up = self.upstreams[server_id]
        upstream_latency_ms: Optional[float] = None

        if op == "initialize":
            t_up0 = perf_counter()
            resp = up.request("initialize", params, timeout=self._timeout_for(server_id, "initialize"))
            upstream_latency_ms = (perf_counter() - t_up0) * 1000.0
            latency_ms = (perf_counter() - t0) * 1000.0

            if "result" in resp:
                self.db.log_row(
                    ts, caller_id, remote_addr, server_id, op, params,
                    cache_key=None, hit=0, expires_at=None, response_obj=resp["result"],
                    latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="NA"
                )
                return ok(resp["result"])

            err_obj = resp.get("error", {"code": -32000, "message": "initialize failed"})
            self.db.log_row(
                ts, caller_id, remote_addr, server_id, op, params,
                cache_key=None, hit=0, expires_at=None, response_obj={"error": err_obj},
                latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="ERROR"
            )
            return {"id": endpoint_id, "error": err_obj}

        if op == "tools/list":
            t_up0 = perf_counter()
            resp = up.request("tools/list", params, timeout=self._timeout_for(server_id, "tools/list"))
            upstream_latency_ms = (perf_counter() - t_up0) * 1000.0
            latency_ms = (perf_counter() - t0) * 1000.0

            if "result" in resp:
                self.db.log_row(
                    ts, caller_id, remote_addr, server_id, op, params,
                    cache_key=None, hit=0, expires_at=None, response_obj=resp["result"],
                    latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="NA"
                )
                return ok(resp["result"])

            err_obj = resp.get("error", {"code": -32000, "message": "tools/list failed"})
            self.db.log_row(
                ts, caller_id, remote_addr, server_id, op, params,
                cache_key=None, hit=0, expires_at=None, response_obj={"error": err_obj},
                latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="ERROR"
            )
            return {"id": endpoint_id, "error": err_obj}

        ttl = self._ttl_for(server_id)
        cache_key: Optional[str] = None
        semantic_text: Optional[str] = None
        tool_name: Optional[str] = None

        if ttl > 0:
            cache_key = self._make_tools_call_cache_key(server_id, params)

            cached_tuple = self.db.get_cache_entry(cache_key, now_ts=ts)
            if cached_tuple is not None:
                cached_obj, cached_expires_at = cached_tuple
                log(f"[CACHE HIT] server={server_id} caller={caller_id} key={cache_key[:10]}")

                latency_ms = (perf_counter() - t0) * 1000.0
                self.db.log_row(
                    ts, caller_id, remote_addr, server_id, op, params,
                    cache_key=cache_key, hit=1, expires_at=cached_expires_at,
                    response_obj=cached_obj,
                    latency_ms=latency_ms, upstream_latency_ms=None, cache_status="EXACT_HIT"
                )
                return ok(cached_obj)

            log(f"[CACHE MISS] server={server_id} caller={caller_id} key={cache_key[:10]}")
            cache_status = "MISS"

            server_semantic_cache_enabled = self._semantic_cache_enabled_for(server_id)

            if server_semantic_cache_enabled and self.semantic_cache_enabled and self.semantic_cache is not None:
                try:
                    semantic_text = self._build_semantic_text(server_id, params)
                    tool_name = params.get("name", "unknown_tool")

                    semantic_hit = self.semantic_cache.search(
                        server_id=server_id,
                        tool_name=tool_name,
                        text=semantic_text,
                        now_ts=ts,
                    )

                    if semantic_hit is not None:
                        log(f"[SEMANTIC HIT] server={server_id} caller={caller_id} score={semantic_hit['score']:.4f}")

                        semantic_expires_at = float(semantic_hit["expires_at"])
                        self.db.set_cache_entry(cache_key, semantic_expires_at, semantic_hit["response"])

                        latency_ms = (perf_counter() - t0) * 1000.0
                        self.db.log_row(
                            ts, caller_id, remote_addr, server_id, op, params,
                            cache_key=cache_key, hit=1, expires_at=semantic_expires_at,
                            response_obj=semantic_hit["response"],
                            latency_ms=latency_ms, upstream_latency_ms=None, cache_status="SEMANTIC_HIT"
                        )
                        return ok(semantic_hit["response"])

                except Exception as e:
                    log(f"[SEMANTIC CACHE] search error: {e!r}")

        else:
            log(f"[NO-CACHE] server={server_id} caller={caller_id}")
            cache_status = "NO_CACHE"

        t_up0 = perf_counter()
        resp = up.request("tools/call", params, timeout=self._timeout_for(server_id, "tools/call"))
        upstream_latency_ms = (perf_counter() - t_up0) * 1000.0

        if "result" in resp:
            result_obj = resp["result"]
            expires_at = (ts + ttl) if ttl > 0 else None

            if ttl > 0 and cache_key is not None and expires_at is not None:
                self.db.set_cache_entry(cache_key, expires_at, result_obj)

            server_semantic_cache_enabled = self._semantic_cache_enabled_for(server_id)

            if ttl > 0 and server_semantic_cache_enabled and self.semantic_cache_enabled and self.semantic_cache is not None and expires_at is not None:
                try:
                    if semantic_text is None:
                        semantic_text = self._build_semantic_text(server_id, params)
                    if tool_name is None:
                        tool_name = params.get("name", "unknown_tool")

                    point_id = self._make_semantic_point_id(server_id, semantic_text)

                    self.semantic_cache.store(
                        point_id=point_id,
                        server_id=server_id,
                        tool_name=tool_name,
                        text=semantic_text,
                        response=result_obj,
                        created_at=ts,
                        expires_at=expires_at,
                    )
                except Exception as e:
                    log(f"[SEMANTIC CACHE] store error: {e!r}")

            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(
                ts, caller_id, remote_addr, server_id, op, params,
                cache_key=cache_key, hit=0, expires_at=expires_at, response_obj=result_obj,
                latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status=cache_status
            )
            return ok(result_obj)

        err_obj = resp.get("error", {"code": -32000, "message": "tools/call failed"})
        latency_ms = (perf_counter() - t0) * 1000.0
        self.db.log_row(
            ts, caller_id, remote_addr, server_id, op, params,
            cache_key=cache_key, hit=0, expires_at=None, response_obj={"error": err_obj},
            latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="ERROR"
        )
        return {"id": endpoint_id, "error": err_obj}

    def serve(self) -> None:
        log(f"Listening on {self.host}:{self.port}")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(50)

        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()

    def _handle_client(self, conn: socket.socket, addr) -> None:
        log(f"Endpoint connected: {addr}")
        remote_addr = f"{addr[0]}:{addr[1]}"

        f_in = conn.makefile("r", encoding="utf-8")
        f_out = conn.makefile("w", encoding="utf-8")

        try:
            for line in f_in:
                line = line.strip()
                if not line:
                    continue

                try:
                    req = json.loads(line)
                except Exception as e:
                    f_out.write(json.dumps({
                        "id": None,
                        "error": {"code": -32700, "message": f"Bad JSON: {e!r}"}
                    }, ensure_ascii=True) + "\n")
                    f_out.flush()
                    continue

                resp = self.handle(req, remote_addr=remote_addr)
                f_out.write(json.dumps(resp, ensure_ascii=True) + "\n")
                f_out.flush()

        finally:
            try:
                conn.close()
            except Exception:
                pass
            log(f"Endpoint disconnected: {addr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="proxy_core_config.json", help="Path to proxy core config JSON")
    args = ap.parse_args()

    core = ProxyCore(config_path=args.config)
    core.serve()


if __name__ == "__main__":
    main()