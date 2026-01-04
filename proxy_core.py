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
from typing import Dict, Any, Optional, Tuple
from time import perf_counter


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
      1) requests      - append-only audit/history (big table)
      2) cache_entries - actual cache (small, fast lookup by primary key)

    Also stores performance metrics in requests:
      - latency_ms (end-to-end in proxy-core)
      - upstream_latency_ms (only when calling upstream)
      - cache_status (HIT / MISS / NO_CACHE / NA / ERROR)
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
        """
        Add new columns to requests if they don't exist (no DB reset needed).
        """
        def add_col(sql: str) -> None:
            try:
                con.execute(sql)
            except sqlite3.OperationalError as e:
                # "duplicate column name" => already exists; ignore
                if "duplicate column name" not in str(e).lower():
                    raise

        with self._lock, self._connect() as con:
            add_col("ALTER TABLE requests ADD COLUMN latency_ms REAL;")
            add_col("ALTER TABLE requests ADD COLUMN upstream_latency_ms REAL;")
            add_col("ALTER TABLE requests ADD COLUMN cache_status TEXT;")

    @staticmethod
    def canonical_json(obj: Any) -> str:
        """
        IMPORTANT:
        - ensure_ascii=True forces ASCII-only JSON (uses \\uXXXX escapes),
          which avoids UnicodeEncodeError caused by Windows/Claude "surrogate" chars.
        - sort_keys + separators => stable hashing for cache_key.
        """
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

    # ---------------------------
    # Cache key normalization (optional)
    # ---------------------------

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
        """
        Normalize tools/call params to stabilize cache keys across clients.
        Expected params shape (common in MCP):
          { "name": "<tool_name>", "arguments": { ... } }
        We keep only tool name + normalized arguments.
        """
        tool_name = params.get("name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}

        # Apply defaults if missing
        if default_argument_values:
            for k, v in default_argument_values.items():
                if k not in args:
                    args[k] = v

        # Remove noisy keys (deeply)
        args_norm = CacheDB._deep_remove_keys(args, ignore_argument_keys or set())

        return {"name": tool_name, "arguments": args_norm}

    @staticmethod
    def make_cache_key_from_obj(server_id: str, op: str, obj: dict) -> str:
        canon = CacheDB.canonical_json(obj)
        raw = f"{server_id}|{op}|{canon}".encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    # ---------------------------
    # Cache table (cache_entries)
    # ---------------------------

    def get_cache_entry(self, cache_key: str, now_ts: float) -> Optional[Tuple[dict, float]]:
        """
        Return (response_obj, expires_at) if present and not expired.
        If expired -> delete it and return None. (lazy eviction)
        """
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

    # ---------------------------
    # Audit table (requests)
    # ---------------------------

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
        self.proc = start_upstream(cmd)

        self._cv = threading.Condition()
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            if line:
                log(f"[{self.server_id}][stderr] {line}")

    def _read_stdout(self) -> None:
        for raw in self.proc.stdout:
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

    def request(self, method: str, params: Optional[dict], timeout: float) -> Dict[str, Any]:
        with self._cv:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = {"done": False, "msg": None}

            req = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                req["params"] = params

            try:
                # ensure_ascii=True to avoid emitting surrogates on write
                self.proc.stdin.write(json.dumps(req, ensure_ascii=True) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                self._pending.pop(req_id, None)
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": f"Upstream write failed: {e!r}"}}

            deadline = time.time() + timeout
            while not self._pending[req_id]["done"]:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._pending.pop(req_id, None)
                    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "Upstream timeout"}}
                self._cv.wait(timeout=remaining)

            msg = self._pending.pop(req_id)["msg"]
            return msg


class ProxyCore:
    def __init__(self, config_path: str):
        self.config = _safe_json_load(config_path)
        listen = self.config.get("listen", {})
        self.host = listen.get("host", "0.0.0.0")
        self.port = int(listen.get("port", 8765))

        self.defaults = self.config.get("defaults", {})
        self.default_ttl = int(self.defaults.get("cache_ttl_s", 0))
        self.default_timeouts = self.defaults.get("timeouts_s", {
            "initialize": 10, "tools/list": 10, "tools/call": 30
        })

        # Cache key normalization policy (optional)
        self.cache_key_policy = self.config.get("cache_key_policy", {})
        self.ck_enabled = bool(self.cache_key_policy.get("enabled", False))
        self.ck_tools_call = self.cache_key_policy.get("tools_call", {})
        self.ck_ignore_arg_keys = set(self.ck_tools_call.get("ignore_argument_keys", []))
        self.ck_default_arg_values = dict(self.ck_tools_call.get("default_argument_values", {}))

        self.servers_cfg: Dict[str, dict] = self.config.get("servers", {})
        if not self.servers_cfg:
            raise RuntimeError("Config has no 'servers' entries.")

        self.db = CacheDB(path=self.config.get("db_path", "proxy_cache.sqlite"))

        # Build upstream processes dynamically from config
        self.upstreams: Dict[str, Upstream] = {}
        for sid, scfg in self.servers_cfg.items():
            cmd = scfg.get("cmd")
            if not isinstance(cmd, list) or not cmd:
                raise RuntimeError(f"Server '{sid}' has invalid/missing cmd")
            self.upstreams[sid] = Upstream(sid, cmd)

        # Cache GC settings
        self.gc_cfg = self.config.get("cache_gc", {"enabled": True, "on_startup": True, "interval_s": 60})
        self._stop_gc = threading.Event()

        if self.gc_cfg.get("enabled", True):
            if self.gc_cfg.get("on_startup", True):
                try:
                    deleted = self.db.cleanup_expired_cache(time.time())
                    if deleted:
                        log(f"[CACHE GC] startup deleted_expired={deleted}")
                except Exception as e:
                    log(f"[CACHE GC] startup error: {e!r}")

            threading.Thread(target=self._cache_gc_loop, daemon=True).start()

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
            except Exception as e:
                log(f"[CACHE GC] error: {e!r}")
            self._stop_gc.wait(interval)

    def _ttl_for(self, server_id: str) -> int:
        scfg = self.servers_cfg.get(server_id, {})
        return int(scfg.get("cache_ttl_s", self.default_ttl))

    def _timeout_for(self, server_id: str, op: str) -> float:
        scfg = self.servers_cfg.get(server_id, {})
        timeouts = scfg.get("timeouts_s", {})
        if op in timeouts:
            return float(timeouts[op])
        return float(self.default_timeouts.get(op, 10))

    def _make_tools_call_cache_key(self, server_id: str, params: dict) -> str:
        """
        Build cache_key for tools/call. If cache_key_policy enabled, normalize params first.
        """
        if self.ck_enabled:
            norm = CacheDB.normalize_tools_call_params(
                params,
                ignore_argument_keys=self.ck_ignore_arg_keys,
                default_argument_values=self.ck_default_arg_values,
            )
            return CacheDB.make_cache_key_from_obj(server_id, "tools/call", norm)
        return self.db.make_cache_key(server_id, "tools/call", params)

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

        if op not in ("initialize", "tools/list", "tools/call"):
            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(ts, caller_id, remote_addr, server_id or "unknown", op or "unknown", params,
                            cache_key=None, hit=0, expires_at=None,
                            response_obj={"error": {"code": -32601, "message": f"Unknown op: {op}"}},
                            latency_ms=latency_ms, upstream_latency_ms=None, cache_status="ERROR")
            return err(f"Unknown op: {op}", -32601)

        if server_id not in self.upstreams:
            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(ts, caller_id, remote_addr, server_id or "unknown", op, params,
                            cache_key=None, hit=0, expires_at=None,
                            response_obj={"error": {"code": -32602, "message": f"Unknown server_id: {server_id}"}},
                            latency_ms=latency_ms, upstream_latency_ms=None, cache_status="ERROR")
            return err(f"Unknown server_id: {server_id}", -32602)

        up = self.upstreams[server_id]
        upstream_latency_ms: Optional[float] = None

        # initialize
        if op == "initialize":
            t_up0 = perf_counter()
            resp = up.request("initialize", params, timeout=self._timeout_for(server_id, "initialize"))
            upstream_latency_ms = (perf_counter() - t_up0) * 1000.0
            latency_ms = (perf_counter() - t0) * 1000.0

            if "result" in resp:
                self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                                cache_key=None, hit=0, expires_at=None, response_obj=resp["result"],
                                latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="NA")
                return ok(resp["result"])

            err_obj = resp.get("error", {"code": -32000, "message": "initialize failed"})
            self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                            cache_key=None, hit=0, expires_at=None, response_obj={"error": err_obj},
                            latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="ERROR")
            return {"id": endpoint_id, "error": err_obj}

        # tools/list
        if op == "tools/list":
            t_up0 = perf_counter()
            resp = up.request("tools/list", params, timeout=self._timeout_for(server_id, "tools/list"))
            upstream_latency_ms = (perf_counter() - t_up0) * 1000.0
            latency_ms = (perf_counter() - t0) * 1000.0

            if "result" in resp:
                self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                                cache_key=None, hit=0, expires_at=None, response_obj=resp["result"],
                                latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="NA")
                return ok(resp["result"])

            err_obj = resp.get("error", {"code": -32000, "message": "tools/list failed"})
            self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                            cache_key=None, hit=0, expires_at=None, response_obj={"error": err_obj},
                            latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="ERROR")
            return {"id": endpoint_id, "error": err_obj}

        # tools/call
        ttl = self._ttl_for(server_id)
        cache_key: Optional[str] = None

        if ttl > 0:
            cache_key = self._make_tools_call_cache_key(server_id, params)
            cached_tuple = self.db.get_cache_entry(cache_key, now_ts=ts)
            if cached_tuple is not None:
                cached_obj, cached_expires_at = cached_tuple
                log(f"[CACHE HIT] server={server_id} caller={caller_id} key={cache_key[:10]}")

                latency_ms = (perf_counter() - t0) * 1000.0
                self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                                cache_key=cache_key, hit=1, expires_at=cached_expires_at,
                                response_obj=cached_obj,
                                latency_ms=latency_ms, upstream_latency_ms=None, cache_status="HIT")
                return ok(cached_obj)

            log(f"[CACHE MISS] server={server_id} caller={caller_id} key={cache_key[:10]}")
            cache_status = "MISS"
        else:
            log(f"[NO-CACHE] server={server_id} caller={caller_id}")
            cache_status = "NO_CACHE"

        # call upstream
        t_up0 = perf_counter()
        resp = up.request("tools/call", params, timeout=self._timeout_for(server_id, "tools/call"))
        upstream_latency_ms = (perf_counter() - t_up0) * 1000.0

        if "result" in resp:
            result_obj = resp["result"]
            expires_at = (ts + ttl) if ttl > 0 else None

            if ttl > 0 and cache_key is not None and expires_at is not None:
                self.db.set_cache_entry(cache_key, expires_at, result_obj)

            latency_ms = (perf_counter() - t0) * 1000.0
            self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                            cache_key=cache_key, hit=0, expires_at=expires_at, response_obj=result_obj,
                            latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status=cache_status)
            return ok(result_obj)

        err_obj = resp.get("error", {"code": -32000, "message": "tools/call failed"})
        latency_ms = (perf_counter() - t0) * 1000.0
        self.db.log_row(ts, caller_id, remote_addr, server_id, op, params,
                        cache_key=cache_key, hit=0, expires_at=None, response_obj={"error": err_obj},
                        latency_ms=latency_ms, upstream_latency_ms=upstream_latency_ms, cache_status="ERROR")
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
