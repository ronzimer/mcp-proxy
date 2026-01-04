#!/usr/bin/env python3
import sys
import subprocess
import threading


def log(msg: str):
    """
    Write log messages to stderr so they appear inside Claude's MCP logs.
    This helps debugging because stdout is reserved for MCP JSON messages.
    """
    print(f"[proxy] {msg}", file=sys.stderr, flush=True)


def pipe_stdin_to_upstream(upstream: subprocess.Popen):
    """
    Forward every line coming **from Claude** (stdin) INTO the upstream MCP server.
    Essentially: Claude → proxy → upstream.
    """
    try:
        for line in sys.stdin:
            if not line:
                break  # EOF → stop forwarding

            # Log the incoming message for debugging
            log(f"stdin -> upstream: {line.strip()}")

            # Write the MCP message to the upstream server's stdin
            upstream.stdin.write(line)
            upstream.stdin.flush()

    except Exception as e:
        # If something breaks while forwarding traffic, log it
        log(f"error forwarding stdin -> upstream: {e!r}")

    finally:
        # Cleanly close the upstream stdin when finished
        try:
            upstream.stdin.close()
        except Exception:
            pass


def pipe_upstream_to_stdout(upstream: subprocess.Popen):
    """
    Forward every line that comes FROM the upstream MCP server back TO Claude.
    Essentially: upstream → proxy → Claude.
    """
    try:
        for line in upstream.stdout:
            if not line:
                break  # EOF from upstream

            log(f"upstream -> stdout: {line.strip()}")

            # Forward the upstream JSON RPC line directly to Claude
            sys.stdout.write(line)
            sys.stdout.flush()

    except Exception as e:
        # If upstream closes unexpectedly or sends malformed output
        log(f"error forwarding upstream -> stdout: {e!r}")


def pipe_upstream_stderr(upstream: subprocess.Popen):
    """
    Continuously read stderr output from the upstream server and log it.
    This is essential because many MCP servers print errors or warnings to stderr.
    """
    for line in upstream.stderr:
        log(f"[upstream stderr] {line.rstrip()}")


def main():
    """
    Main entry point:
    - Start a single upstream MCP server
    - Create two threads:
        1. Forward Claude → upstream
        2. Forward upstream → Claude
    - Wait for both threads to finish
    """
    # ---- Define which upstream MCP server we want to launch ----
    # This example uses the official "memory" MCP server
    upstream_cmd = [
        "npx",
        "-y",
        "@modelcontextprotocol/server-memory",
    ]

    log(f"starting upstream: {' '.join(upstream_cmd)}")

    try:
        # Launch the upstream MCP server as a subprocess
        upstream = subprocess.Popen(
            upstream_cmd,
            stdin=subprocess.PIPE,   # We send MCP JSON messages here
            stdout=subprocess.PIPE,  # We receive MCP JSON messages here
            stderr=subprocess.PIPE,  # Upstream logs/warnings/errors
            text=True,               # Treat streams as text lines
            bufsize=1,               # Line-buffered mode
        )
    except Exception as e:
        log(f"FAILED to start upstream: {e!r}")
        sys.exit(1)

    # Start a background thread that listens to upstream stderr
    # (useful for debugging)
    threading.Thread(
        target=pipe_upstream_stderr,
        args=(upstream,),
        daemon=True,
    ).start()

    # Create the two main forwarding threads:
    # 1) Claude → upstream
    t1 = threading.Thread(
        target=pipe_stdin_to_upstream,
        args=(upstream,),
        daemon=True,
    )

    # 2) upstream → Claude
    t2 = threading.Thread(
        target=pipe_upstream_to_stdout,
        args=(upstream,),
        daemon=True,
    )

    # Start both data-forwarding threads
    t1.start()
    t2.start()

    # Wait until both threads finish (usually when Claude disconnects)
    t1.join()
    t2.join()

    log("terminating upstream")

    # Attempt graceful shutdown of the upstream server
    try:
        upstream.terminate()
    except Exception:
        pass


if __name__ == "__main__":
    main()
