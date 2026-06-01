#!/usr/bin/env python3
import sys
import json
import socket
import argparse
import platform
from typing import Dict, Any


def log(msg: str) -> None:
    """
    Log diagnostic messages to stderr only.
    """
    print(f"[proxy-endpoint] {msg}", file=sys.stderr, flush=True)


def default_caller_id() -> str:
    """
    Use a stable, human-readable identifier for logging on the proxy-core side.
    """
    try:
        return platform.node() or "unknown"
    except Exception:
        return "unknown"


def core_request(host: str, port: int, payload: Dict[str, Any], timeout_s: float = 6.0) -> Dict[str, Any]:
    """
    Send one JSON request line to proxy-core and read one JSON response line.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)

    try:
        s.connect((host, port))
        f_in = s.makefile("r", encoding="utf-8")
        f_out = s.makefile("w", encoding="utf-8")

        f_out.write(json.dumps(payload) + "\n")
        f_out.flush()

        line = f_in.readline()
        if not line:
            return {
                "id": payload.get("id"),
                "error": {"code": -32010, "message": "No response from proxy-core"},
            }

        return json.loads(line)

    except Exception as e:
        return {
            "id": payload.get("id"),
            "error": {"code": -32011, "message": f"proxy-core connection failed: {e!r}"},
        }

    finally:
        try:
            s.close()
        except Exception:
            pass


def main() -> None:
    """
    Claude-facing MCP server process.

    This endpoint presents itself as a normal MCP server process, but delegates
    all real work to proxy-core over TCP.

    Important:
    - The endpoint does not inject proxy management tools into real servers.
    - If server_id == "proxy", proxy-core handles it as a virtual management server.
    - If server_id is calculator/wiki/open-meteo/etc., proxy-core routes it to the real upstream.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-id", required=True, help="Upstream server identifier or virtual server id 'proxy'")
    ap.add_argument("--core-host", default="127.0.0.1")
    ap.add_argument("--core-port", type=int, default=8765)
    ap.add_argument("--caller-id", default=None, help="Optional caller id for proxy-core logging/caching")
    args = ap.parse_args()

    server_id = args.server_id
    host = args.core_host
    port = args.core_port
    caller_id = args.caller_id or default_caller_id()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue

        try:
            msg = json.loads(raw)
        except Exception as e:
            log(f"Bad JSON from client: {e!r} :: {raw}")
            continue

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            resp = core_request(host, port, {
                "op": "initialize",
                "server_id": server_id,
                "id": msg_id,
                "params": params,
                "caller_id": caller_id,
            }, timeout_s=6.0)

            if "result" in resp:
                out = {"jsonrpc": "2.0", "id": msg_id, "result": resp["result"]}
            else:
                # Fallback for UI friendliness if proxy-core is slow/unavailable.
                out = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {
                            "name": server_id,
                            "version": "0.1.0",
                            "description": "Managed via proxy-core",
                        },
                    },
                }

            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
            continue

        if method == "tools/list":
            resp = core_request(host, port, {
                "op": "tools/list",
                "server_id": server_id,
                "id": msg_id,
                "params": params,
                "caller_id": caller_id,
            }, timeout_s=6.0)

            if "result" in resp:
                out = {"jsonrpc": "2.0", "id": msg_id, "result": resp["result"]}
            else:
                out = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": resp.get("error", {"code": -32000, "message": "tools/list failed"}),
                }

            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
            continue

        if method == "tools/call":
            resp = core_request(host, port, {
                "op": "tools/call",
                "server_id": server_id,
                "id": msg_id,
                "params": params,
                "caller_id": caller_id,
            }, timeout_s=30.0)

            if "result" in resp:
                out = {"jsonrpc": "2.0", "id": msg_id, "result": resp["result"]}
            else:
                out = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": resp.get("error", {"code": -32000, "message": "tools/call failed"}),
                }

            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
            continue

        log(f"Ignoring unsupported method: {method!r}")


if __name__ == "__main__":
    main()