import json
import subprocess
from typing import Any, Dict, List, Optional

import yaml

from utils.logger import log_info, log_error, log_debug


class UpstreamServer:
    """
    Represent a single upstream MCP server process (over stdio).
    """

    def __init__(self, server_id: str, description: str, command: str):
        self.server_id = server_id
        self.description = description
        self.command = command
        self.proc: Optional[subprocess.Popen] = None
        self.tools: List[Dict[str, Any]] = []

        # מונה ל-id של JSON-RPC שנשלח לשרת הזה
        self._next_id = 1

    def start(self) -> None:
        """
        Start the upstream server process using the configured command.
        """
        log_info(f"Starting upstream server '{self.server_id}' with: {self.command}")
        # shell=True בשביל פשטות – בהמשך אפשר לשפר לפקודה מפורקת
        self.proc = subprocess.Popen(
            self.command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # אחרי שעלה – מבצעים initialize + tools/list
        self._initialize_and_fetch_tools()

    def _send_request(self, method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """
        Send a JSON-RPC request over stdio and wait for a single-line response.
        מאוד נאיבי – מניח רק בקשה אחת בתור בזמן.
        """
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError(f"Upstream server '{self.server_id}' is not started")

        req_id = self._next_id
        self._next_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        serialized = json.dumps(request, ensure_ascii=False)
        log_debug(f"[{self.server_id}] → {serialized}")
        self.proc.stdin.write(serialized + "\n")
        self.proc.stdin.flush()

        # קוראים שורה אחת חזרה
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Upstream server '{self.server_id}' closed stdout")

        line = line.strip()
        log_debug(f"[{self.server_id}] ← {line}")
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"Upstream error from '{self.server_id}': {resp['error']}")
        return resp

    def _initialize_and_fetch_tools(self) -> None:
        """
        Perform MCP initialize + tools/list with the upstream server.
        """
        # initialize
        init_params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},  # כרגע לא מבקשים שום יכולות מיוחדות
        }
        self._send_request("initialize", init_params)

        # tools/list
        resp = self._send_request("tools/list", {"cursor": None})
        tools = resp.get("result", {}).get("tools") or resp.get("tools")
        if tools is None:
            log_error(f"Upstream '{self.server_id}' did not return tools in tools/list")
            tools = []

        self.tools = tools
        log_info(f"Upstream '{self.server_id}' has {len(self.tools)} tools")

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call a specific tool on this upstream server.
        """
        params = {"name": name, "arguments": arguments}
        resp = self._send_request("tools/call", params)
        # MCP בדרך כלל מחזיר 'result' בשורש, נשתמש בזה
        if "result" in resp:
            return resp["result"]
        return resp


class UpstreamManager:
    """
    Manage multiple upstream MCP servers.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.servers: Dict[str, UpstreamServer] = {}
        # מיפוי שם כלי → id של שרת
        self.tool_to_server: Dict[str, str] = {}

    def load_and_start(self) -> None:
        """
        Load config.yaml, start all upstream servers, fetch their tools.
        """
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        for entry in cfg.get("upstream_servers", []):
            server_id = entry["id"]
            desc = entry.get("description", "")
            cmd = entry["command"]

            server = UpstreamServer(server_id, desc, cmd)
            server.start()
            self.servers[server_id] = server

            # עדכון הטבלה: לכל כלי – לאיזה שרת הוא שייך
            for tool in server.tools:
                name = tool.get("name")
                if not name:
                    continue
                if name in self.tool_to_server:
                    log_error(
                        f"Tool name collision: '{name}' provided by both "
                        f"{self.tool_to_server[name]} and {server_id}"
                    )
                else:
                    self.tool_to_server[name] = server_id

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """
        Return union of all tools from all upstream servers.
        """
        tools: List[Dict[str, Any]] = []
        for s in self.servers.values():
            tools.extend(s.tools)
        return tools

    def route_tool_call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Find which upstream server owns this tool and forward the call.
        """
        server_id = self.tool_to_server.get(name)
        if not server_id:
            raise ValueError(f"No upstream server registered for tool '{name}'")
        server = self.servers[server_id]
        return server.call_tool(name, arguments)

    # פונקציות שעוזרות לכלי proxy.*:

    def get_servers_status(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for s in self.servers.values():
            result.append(
                {
                    "id": s.server_id,
                    "description": s.description,
                    "tools_count": len(s.tools),
                    "status": "connected" if s.proc and s.proc.poll() is None else "down",
                }
            )
        return result

    def get_server_tools(self, server_id: str) -> List[Dict[str, Any]]:
        server = self.servers.get(server_id)
        if not server:
            raise ValueError(f"Unknown upstream server id '{server_id}'")
        return server.tools
