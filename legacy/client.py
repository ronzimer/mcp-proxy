#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import time
import threading
from typing import Any, Dict, Optional


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


def pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def log(msg: str) -> None:
    # Logs go to stderr so stdout stays clean for JSON output
    print(f"[{now_ts()}] {msg}", file=sys.stderr, flush=True)


def compact_send(msg: Dict[str, Any]) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    print(f"→ SEND  id={mid}  method={method}", flush=True)


def compact_recv(msg: Dict[str, Any]) -> None:
    mid = msg.get("id")

    # Standard JSON-RPC response shape: {"jsonrpc":"2.0","id":...,"result":{...}}
    res = msg.get("result")
    if isinstance(res, dict):
        # initialize response
        if "serverInfo" in res:
            si = res.get("serverInfo") or {}
            caps = res.get("capabilities") or {}
            tools_caps = (caps.get("tools") or {}) if isinstance(caps, dict) else {}
            print(
                f"← RECV  id={mid}  initialize: "
                f"server={si.get('name')} v={si.get('version')}  "
                f"tools.listChanged={tools_caps.get('listChanged')}",
                flush=True,
            )
            return

        # tools/list response
        if "tools" in res and isinstance(res.get("tools"), list):
            tools = res.get("tools") or []
            names = [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]
            preview = ", ".join(names[:8])
            suffix = "" if len(names) <= 8 else f", ... (+{len(names) - 8} more)"
            print(f"← RECV  id={mid}  tools/list: {len(names)} tools [{preview}{suffix}]", flush=True)
            return

        # tools/call response (often returns "content" or structured output)
        if "content" in res:
            # Avoid dumping huge content; show a short summary.
            content = res.get("content")
            if isinstance(content, list):
                print(f"← RECV  id={mid}  tools/call: content[{len(content)}]", flush=True)
            else:
                s = str(content)
                s = (s[:140] + "…") if len(s) > 140 else s
                print(f"← RECV  id={mid}  tools/call: {s}", flush=True)
            return

    # Error response shape: {"jsonrpc":"2.0","id":...,"error":{...}}
    err = msg.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        message = err.get("message")
        print(f"← RECV  id={mid}  ERROR code={code} message={message}", flush=True)
        return

    # Notifications or unexpected shapes
    m = msg.get("method")
    if m:
        print(f"← RECV  notification method={m}", flush=True)
    else:
        print(f"← RECV  id={mid}  (unrecognized message)", flush=True)


def send(proc: subprocess.Popen, msg: Dict[str, Any], compact: bool) -> None:
    wire = json.dumps(msg, ensure_ascii=False)
    if compact:
        compact_send(msg)
    else:
        print(f"\n→ SEND:\n{pretty(msg)}\n", flush=True)
    proc.stdin.write(wire + "\n")
    proc.stdin.flush()


def recv_one(proc: subprocess.Popen, compact: bool, timeout_s: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if compact:
            compact_recv(msg)
        else:
            print(f"← RECV:\n{pretty(msg)}\n", flush=True)
        return msg
    raise TimeoutError("Timed out waiting for server response on stdout")


def run_trace(
    cmd_str: str,
    call_tool: Optional[str] = None,
    call_args: Optional[dict] = None,
    compact: bool = False,
) -> None:
    # Run via shell so the user can pass a single command string (including npx -y ...).
    log(f"Starting server (shell): {cmd_str}")
    proc = subprocess.Popen(
        cmd_str,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def pump_stderr() -> None:
        for line in proc.stderr:
            if line:
                print(f"[server-stderr] {line.rstrip()}", file=sys.stderr, flush=True)

    threading.Thread(target=pump_stderr, daemon=True).start()

    try:
        # 1) initialize
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-trace-client", "version": "0.1.0"},
                },
            },
            compact,
        )
        recv_one(proc, compact, timeout_s=10.0)

        # 2) tools/list
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, compact)
        recv_one(proc, compact, timeout_s=10.0)

        # 3) optional tools/call
        if call_tool:
            send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": call_tool, "arguments": call_args or {}},
                },
                compact,
            )
            recv_one(proc, compact, timeout_s=30.0)

        time.sleep(0.2)

    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", required=True, help="Command string to start an MCP server over stdio.")
    ap.add_argument("--call-tool", default=None)
    ap.add_argument("--call-args", default=None, help="JSON dict string for tool arguments.")
    ap.add_argument("--compact", action="store_true", help="Print a compact summary instead of full JSON.")
    args = ap.parse_args()

    call_args = None
    if args.call_args:
        call_args = json.loads(args.call_args)

    run_trace(args.cmd, call_tool=args.call_tool, call_args=call_args, compact=args.compact)


if __name__ == "__main__":
    main()
