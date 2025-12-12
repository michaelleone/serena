"""
MCP Proxy for connecting stdio clients to a centralized Serena server.

This proxy allows local MCP clients (like Claude Desktop, Cursor, Cline) that
only support stdio transport to connect to a remote centralized Serena server.

Architecture:
    Claude Desktop (stdio) -> MCP Proxy (stdio<->HTTP) -> Central Server (HTTP/SSE)
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from typing import Any

import requests
from sensai.util import logging

log = logging.getLogger(__name__)


class MCPProxyError(Exception):
    """Error in MCP proxy operation."""


class SerenaMCPProxy:
    """
    Lightweight MCP proxy for stdio-to-HTTP bridging.

    Reads MCP messages from stdin, forwards to central server,
    and writes responses to stdout.

    This allows stdio-only clients to connect to the centralized server.
    """

    DEFAULT_TIMEOUT = 300.0  # 5 minutes for tool execution
    HEARTBEAT_INTERVAL = 30.0  # seconds

    def __init__(
        self,
        server_url: str,
        session_id: str | None = None,
        client_name: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        Initialize the proxy.

        :param server_url: URL of the centralized Serena server (e.g., http://localhost:8000).
        :param session_id: Existing session ID to reconnect to, or None to create new.
        :param client_name: Human-readable name for this client.
        :param timeout: Request timeout in seconds.
        """
        self._server_url = server_url.rstrip("/")
        self._session_id = session_id
        self._client_name = client_name or f"mcp-proxy-{id(self)}"
        self._timeout = timeout
        self._shutdown_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        # Tool cache (populated from server)
        self._tools: dict[str, dict[str, Any]] = {}

    def _api_url(self, path: str) -> str:
        """Build API URL."""
        return f"{self._server_url}/api{path}"

    def _make_request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        Make an HTTP request to the server.

        :param method: HTTP method (GET, POST, PUT, DELETE).
        :param path: API path (without /api prefix).
        :param json_data: JSON data to send.
        :param timeout: Request timeout.
        :return: Response JSON.
        :raises MCPProxyError: If request fails.
        """
        url = self._api_url(path)
        timeout = timeout or self._timeout

        try:
            response = requests.request(
                method=method,
                url=url,
                json=json_data,
                timeout=timeout,
                headers={"X-Session-ID": self._session_id} if self._session_id else None,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError as e:
            raise MCPProxyError(f"Cannot connect to server at {self._server_url}: {e}")
        except requests.exceptions.Timeout:
            raise MCPProxyError(f"Request timed out after {timeout}s")
        except requests.exceptions.HTTPError as e:
            raise MCPProxyError(f"HTTP error: {e}")
        except json.JSONDecodeError:
            raise MCPProxyError("Invalid JSON response from server")

    def connect(self) -> None:
        """
        Connect to the server and create/restore session.

        :raises MCPProxyError: If connection fails.
        """
        log.info(f"Connecting to {self._server_url}...")

        if self._session_id:
            # Try to reconnect to existing session
            try:
                response = self._make_request("GET", f"/sessions/{self._session_id}", timeout=10)
                if response.get("session_id"):
                    log.info(f"Reconnected to session {self._session_id}")
                    return
            except MCPProxyError:
                log.warning(f"Session {self._session_id} not found, creating new")
                self._session_id = None

        # Create new session
        response = self._make_request(
            "POST",
            "/sessions",
            json_data={"client_name": self._client_name},
            timeout=10,
        )
        self._session_id = response["session_id"]
        log.info(f"Created session {self._session_id}")

    def disconnect(self) -> None:
        """Disconnect from the server."""
        self._shutdown_event.set()

        if self._session_id:
            try:
                self._make_request(
                    "DELETE",
                    f"/sessions/{self._session_id}",
                    timeout=5,
                )
                log.info(f"Disconnected session {self._session_id}")
            except MCPProxyError as e:
                log.warning(f"Error disconnecting: {e}")

    def _load_tools(self) -> None:
        """Load tool definitions from the server."""
        response = self._make_request("GET", "/tools", timeout=30)
        self._tools = {tool["name"]: tool for tool in response.get("tools", [])}
        log.info(f"Loaded {len(self._tools)} tools from server")

    def _handle_initialize(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP initialize request."""
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "serena-proxy",
                    "version": "0.1.0",
                },
            },
        }

    def _handle_list_tools(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP tools/list request."""
        if not self._tools:
            self._load_tools()

        tools_list = []
        for name, tool in self._tools.items():
            tools_list.append(
                {
                    "name": name,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("parameters", {"type": "object", "properties": {}}),
                }
            )

        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"tools": tools_list},
        }

    def _handle_call_tool(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP tools/call request."""
        params = request.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return self._make_error_response(request.get("id"), -32602, "Missing tool name")

        try:
            response = self._make_request(
                "POST",
                f"/sessions/{self._session_id}/tools/{tool_name}",
                json_data={"arguments": arguments},
                timeout=self._timeout,
            )

            result_text = response.get("result", "")
            is_error = response.get("is_error", False)

            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": is_error,
                },
            }
        except MCPProxyError as e:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "content": [{"type": "text", "text": f"Proxy error: {e}"}],
                    "isError": True,
                },
            }

    def _handle_get_prompt(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP prompts/get request."""
        try:
            response = self._make_request(
                "GET",
                f"/sessions/{self._session_id}/prompt",
                timeout=30,
            )
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "description": "System prompt for Serena",
                    "messages": [{"role": "assistant", "content": {"type": "text", "text": response.get("prompt", "")}}],
                },
            }
        except MCPProxyError as e:
            return self._make_error_response(request.get("id"), -32603, str(e))

    def _make_error_response(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        """Create a JSON-RPC error response."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _process_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """
        Process a single MCP request.

        :param request: The MCP request.
        :return: The response, or None for notifications.
        """
        method = request.get("method", "")

        # Notifications don't need responses
        if "id" not in request:
            if method == "notifications/initialized":
                log.debug("Received initialized notification")
            return None

        # Handle different methods
        if method == "initialize":
            return self._handle_initialize(request)
        elif method == "tools/list":
            return self._handle_list_tools(request)
        elif method == "tools/call":
            return self._handle_call_tool(request)
        elif method == "prompts/get":
            return self._handle_get_prompt(request)
        elif method == "prompts/list":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"prompts": []},
            }
        elif method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"resources": []},
            }
        else:
            return self._make_error_response(request.get("id"), -32601, f"Method not found: {method}")

    def _read_messages(self) -> Iterator[dict[str, Any]]:
        """Read JSON-RPC messages from stdin."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
                yield message
            except json.JSONDecodeError as e:
                log.error(f"Invalid JSON: {e}")

    def _write_message(self, message: dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdout."""
        print(json.dumps(message), flush=True)

    def _heartbeat_loop(self) -> None:
        """Background thread to send heartbeats."""
        while not self._shutdown_event.is_set():
            try:
                self._make_request(
                    "POST",
                    f"/sessions/{self._session_id}/heartbeat",
                    timeout=10,
                )
            except MCPProxyError as e:
                log.warning(f"Heartbeat failed: {e}")
            self._shutdown_event.wait(self.HEARTBEAT_INTERVAL)

    def run(self) -> None:
        """
        Run the proxy main loop.

        Reads from stdin, processes requests, writes to stdout.
        Blocks until stdin is closed or shutdown is called.
        """
        log.info(f"MCP Proxy started, connecting to {self._server_url}")

        try:
            # Connect to server
            self.connect()

            # Start heartbeat thread
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="ProxyHeartbeat")
            self._heartbeat_thread.start()

            # Main message loop
            for message in self._read_messages():
                if self._shutdown_event.is_set():
                    break

                response = self._process_request(message)
                if response is not None:
                    self._write_message(response)

        except KeyboardInterrupt:
            log.info("Interrupted")
        except Exception as e:
            log.exception(f"Proxy error: {e}")
        finally:
            self.disconnect()


def run_proxy(
    server_url: str,
    session_id: str | None = None,
    client_name: str | None = None,
) -> None:
    """
    Run the MCP proxy as a standalone process.

    :param server_url: URL of the centralized Serena server.
    :param session_id: Existing session ID to reconnect to.
    :param client_name: Human-readable name for this client.
    """
    proxy = SerenaMCPProxy(
        server_url=server_url,
        session_id=session_id,
        client_name=client_name,
    )
    proxy.run()
