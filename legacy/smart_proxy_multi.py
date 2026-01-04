#!/usr/bin/env python3
import sys
import subprocess
import threading
import json


def log(msg: str):
    """
    Log diagnostic messages to stderr (NOT stdout).
    stdout must remain "clean" for MCP JSON-RPC messages.
    Sending logs to stderr ensures Claude will display them
    in the MCP debug console.
    """
    print(f"[proxy] {msg}", file=sys.stderr, flush=True)


def start_upstream(cmd):
    """
    Start a single upstream MCP server process.
    Each upstream process runs independently (filesystem, memory, etc.).
    We capture stdin/stdout/stderr so we can talk MCP with it.
    """
    log(f"Starting upstream → {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,   # for sending MCP requests
        stdout=subprocess.PIPE,  # for receiving MCP responses
        stderr=subprocess.PIPE,  # for upstream logs
        text=True,
        bufsize=1,               # line-buffered (important for interactive MCP)
    )


def main():
    """
    Main Smart Proxy logic:
    - Launch multiple upstream MCP servers
    - Merge their tools into a single "virtual" MCP server
    - Route each tools/call to the correct upstream based on tool name
    """

    # ----------------------------------------------------------------------
    # 1. Define which upstream MCP servers we want to run
    # ----------------------------------------------------------------------
    upstream_cmds = {
        "fs": [
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            "/Users/ronzimerman/Desktop",
        ],
        "memory": [
            "npx",
            "-y",
            "@modelcontextprotocol/server-memory",
        ],
    }

    # Launch each upstream server process
    upstreams = {key: start_upstream(cmd) for key, cmd in upstream_cmds.items()}

    # Dictionary:
    # tool_name → upstream_key ("fs" or "memory")
    # This mapping is built dynamically when we merge tools/list responses.
    tool_to_upstream = {}

    # State needed for merging tools/list results
    current_list_request_id = None    # tracks which request we are currently collecting
    pending_list_upstreams = set()    # which upstreams still owe us a tools/list response?
    list_results = {}                 # temporary storage: upstream_key → list of tools

    # Lock to protect shared state (because threads read/write)
    lock = threading.Lock()

    # ----------------------------------------------------------------------
    # 2. Thread function: handle stdout coming FROM each upstream MCP server
    # ----------------------------------------------------------------------
    def handle_upstream_stdout(up_key, proc):
        """
        Each upstream MCP server has its own stdout listener thread.
        This thread receives JSON-RPC messages from that upstream,
        parses them, and either:
        - merges tools/list results, or
        - forwards all other messages directly to Claude.
        """
        nonlocal current_list_request_id, pending_list_upstreams, list_results, tool_to_upstream

        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue

            # Try parsing the upstream JSON RPC message
            try:
                msg = json.loads(raw)
            except Exception as e:
                log(f"[{up_key}] Failed to parse JSON: {e!r} :: {raw}")
                continue

            msg_id = msg.get("id")
            result = msg.get("result")

            with lock:
                # ==================================================================
                # CASE A: This is a tools/list response from upstream
                # ==================================================================
                if (
                    msg_id is not None
                    and current_list_request_id is not None
                    and msg_id == current_list_request_id
                    and isinstance(result, dict)
                    and "tools" in result
                ):
                    tools = result.get("tools") or []
                    log(f"[{up_key}] tools/list returned {len(tools)} tools")

                    # Store the result for merging later
                    list_results[up_key] = tools

                    # Mark this upstream as "done"
                    if up_key in pending_list_upstreams:
                        pending_list_upstreams.remove(up_key)

                    # If all upstreams responded → merge and send to Claude
                    if not pending_list_upstreams:
                        merged_tools = []
                        tool_to_upstream.clear()

                        # Build unified tool list + routing table
                        for u_key, tools_list in list_results.items():
                            for tool in tools_list:
                                name = tool.get("name")
                                if not name:
                                    continue
                                tool_to_upstream[name] = u_key
                                merged_tools.append(tool)

                        log(f"Merged total tools: {len(merged_tools)}")

                        # Construct a single tools/list response back to Claude
                        response = {
                            "jsonrpc": "2.0",
                            "id": current_list_request_id,
                            "result": {"tools": merged_tools},
                        }
                        sys.stdout.write(json.dumps(response) + "\n")
                        sys.stdout.flush()

                        # Reset merge cycle state
                        current_list_request_id = None
                        list_results = {}

                # ==================================================================
                # CASE B: Any other upstream message → forward directly to Claude
                # e.g. initialize results, tools/call results, notifications, etc.
                # ==================================================================
                else:
                    sys.stdout.write(json.dumps(msg) + "\n")
                    sys.stdout.flush()

    # Start a stdout-handling thread for each upstream server
    for key, proc in upstreams.items():
        t = threading.Thread(
            target=handle_upstream_stdout,
            args=(key, proc),
            daemon=True,
        )
        t.start()

    # ----------------------------------------------------------------------
    # 3. Main loop: messages FROM Claude → decide what to do
    # ----------------------------------------------------------------------
    for raw in sys.stdin:
        if not raw:
            break
        raw_stripped = raw.strip()
        if not raw_stripped:
            continue

        # Parse incoming JSON-RPC message from Claude
        try:
            msg = json.loads(raw_stripped)
        except Exception as e:
            log(f"Failed to parse JSON from client: {e!r} :: {raw_stripped}")
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # ======================================================================
        # CASE 3.1 — initialize (Claude is starting)
        # ======================================================================
        if method == "initialize":
            log("Got initialize → forwarding to upstreams and replying to Claude")

            # Forward initialize to every upstream MCP server
            for key, proc in upstreams.items():
                try:
                    proc.stdin.write(raw_stripped + "\n")
                    proc.stdin.flush()
                except Exception as e:
                    log(f"[{key}] Failed to send initialize upstream: {e!r}")

            # Respond immediately to Claude.
            # This is CRITICAL: Claude expects a quick initialize response.
            init_response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": msg.get("params", {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {
                        "name": "smart-proxy-multi",
                        "version": "0.1.0",
                    },
                },
            }
            sys.stdout.write(json.dumps(init_response) + "\n")
            sys.stdout.flush()
            continue

        # ======================================================================
        # CASE 3.2 — tools/list (Claude requests the full unified toolset)
        # ======================================================================
        if method == "tools/list":
            log("Got tools/list → requesting lists from all upstream MCP servers")

            with lock:
                current_list_request_id = msg_id
                pending_list_upstreams = set(upstreams.keys())
                list_results = {}

            # Forward the request to each upstream server
            for key, proc in upstreams.items():
                try:
                    proc.stdin.write(raw_stripped + "\n")
                    proc.stdin.flush()
                except Exception as e:
                    log(f"[{key}] Failed to send tools/list upstream: {e!r}")
            continue

        # ======================================================================
        # CASE 3.3 — tools/call (route this call to the correct upstream)
        # ======================================================================
        if method == "tools/call":
            params = msg.get("params") or {}
            tool_name = params.get("name")

            if not tool_name:
                log("tools/call received without tool name → ignoring")
                continue

            # Lookup which upstream this tool belongs to
            with lock:
                up_key = tool_to_upstream.get(tool_name)

            if not up_key:
                log(f"Unknown tool '{tool_name}' — maybe tools/list wasn't called yet?")
                continue

            log(f"Routing tools/call for '{tool_name}' → upstream '{up_key}'")

            proc = upstreams.get(up_key)
            if not proc:
                log(f"Upstream process '{up_key}' not found")
                continue

            try:
                proc.stdin.write(raw_stripped + "\n")
                proc.stdin.flush()
            except Exception as e:
                log(f"[{up_key}] Failed to send tools/call upstream: {e!r}")
            continue

        # ======================================================================
        # CASE 3.4 — Any other method → broadcast to all upstreams
        # ======================================================================
        log(f"Got other method '{method}' → broadcasting to upstreams")
        for key, proc in upstreams.items():
            try:
                proc.stdin.write(raw_stripped + "\n")
                proc.stdin.flush()
            except Exception as e:
                log(f"[{key}] Failed to broadcast message upstream: {e!r}")


if __name__ == "__main__":
    main()
