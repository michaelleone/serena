"""
Global Dashboard for Centralized Serena Server.

Provides a unified web interface to view and manage all connected client sessions,
monitor tool usage, view lifecycle events, and manage the server.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, request, send_from_directory
from pydantic import BaseModel
from sensai.util import logging

if TYPE_CHECKING:
    from serena.central_server import CentralizedSerenaServer

log = logging.getLogger(__name__)

# Disable Werkzeug's verbose logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Path to global dashboard static files
GLOBAL_DASHBOARD_DIR = str(Path(__file__).parent / "resources" / "global_dashboard")
DASHBOARD_DIR = str(Path(__file__).parent / "resources" / "dashboard")


class CreateSessionRequest(BaseModel):
    """Request to create a new session."""

    client_name: str | None = None


class ToolCallRequest(BaseModel):
    """Request to call a tool."""

    arguments: dict[str, Any] = {}


class SetModesRequest(BaseModel):
    """Request to set session modes."""

    modes: list[str]


class ActivateProjectRequest(BaseModel):
    """Request to activate a project."""

    project_path_or_name: str


class GlobalDashboardAPI:
    """
    Flask-based API for the global dashboard.

    Provides endpoints for:
    - Session management (list, create, delete, details)
    - Tool execution through sessions
    - Server statistics and lifecycle events
    - Global dashboard UI
    """

    log = logging.getLogger(__qualname__)

    def __init__(
        self,
        server: "CentralizedSerenaServer",
    ) -> None:
        """
        Initialize the global dashboard.

        :param server: The centralized Serena server instance.
        """
        self._server = server
        self._app = Flask(__name__)
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Set up Flask routes."""
        app = self._app

        # ========== Static Files ==========

        @app.route("/global-dashboard/<path:filename>")
        def serve_global_dashboard(filename: str) -> Response:
            """Serve global dashboard static files."""
            return send_from_directory(GLOBAL_DASHBOARD_DIR, filename)

        @app.route("/global-dashboard/")
        def serve_global_dashboard_index() -> Response:
            """Serve global dashboard index."""
            return send_from_directory(GLOBAL_DASHBOARD_DIR, "index.html")

        # Serve shared dashboard assets (css, js, images)
        @app.route("/dashboard/<path:filename>")
        def serve_dashboard_assets(filename: str) -> Response:
            """Serve shared dashboard assets."""
            return send_from_directory(DASHBOARD_DIR, filename)

        # ========== API Routes ==========

        # Health check
        @app.route("/api/health", methods=["GET"])
        def health_check() -> dict[str, Any]:
            return {"status": "ok", "server": "serena-central"}

        # Server stats
        @app.route("/api/stats", methods=["GET"])
        def get_stats() -> dict[str, Any]:
            stats = self._server.get_stats()
            return stats.to_dict()

        # Lifecycle events
        @app.route("/api/lifecycle-events", methods=["GET"])
        def get_lifecycle_events() -> dict[str, Any]:
            limit = request.args.get("limit", 100, type=int)
            events = self._server.get_lifecycle_events(limit)
            return {"events": [e.to_dict() for e in events]}

        # ========== Session Management ==========

        @app.route("/api/sessions", methods=["GET"])
        def list_sessions() -> dict[str, Any]:
            sessions = self._server.list_sessions()
            return {"sessions": [s.to_dict() for s in sessions]}

        @app.route("/api/sessions", methods=["POST"])
        def create_session() -> dict[str, Any]:
            data = request.get_json() or {}
            req = CreateSessionRequest.model_validate(data)
            session = self._server.create_session(client_name=req.client_name)
            return {"session_id": session.session_id, "status": "created"}

        @app.route("/api/sessions/<session_id>", methods=["GET"])
        def get_session(session_id: str) -> dict[str, Any] | tuple[dict[str, Any], int]:
            details = self._server.get_session_details(session_id)
            if details is None:
                return {"error": "Session not found"}, 404
            return details

        @app.route("/api/sessions/<session_id>", methods=["DELETE"])
        def delete_session(session_id: str) -> dict[str, Any] | tuple[dict[str, Any], int]:
            success = self._server.disconnect_session(session_id)
            if not success:
                return {"error": "Session not found"}, 404
            return {"status": "disconnected"}

        @app.route("/api/sessions/<session_id>/heartbeat", methods=["POST"])
        def session_heartbeat(session_id: str) -> dict[str, Any] | tuple[dict[str, Any], int]:
            session = self._server.get_session(session_id)
            if session is None:
                return {"error": "Session not found"}, 404
            session.touch()
            return {"status": "ok"}

        @app.route("/api/sessions/<session_id>/prompt", methods=["GET"])
        def get_session_prompt(session_id: str) -> dict[str, Any] | tuple[dict[str, Any], int]:
            session = self._server.get_session(session_id)
            if session is None:
                return {"error": "Session not found"}, 404
            prompt = self._server.get_system_prompt_for_session(session_id)
            return {"prompt": prompt}

        @app.route("/api/sessions/<session_id>/modes", methods=["PUT"])
        def set_session_modes(session_id: str) -> dict[str, Any] | tuple[dict[str, Any], int]:
            data = request.get_json() or {}
            req = SetModesRequest.model_validate(data)
            try:
                self._server.set_modes_for_session(session_id, req.modes)
                return {"status": "ok", "modes": req.modes}
            except ValueError as e:
                return {"error": str(e)}, 404

        @app.route("/api/sessions/<session_id>/project", methods=["PUT"])
        def activate_session_project(session_id: str) -> dict[str, Any] | tuple[dict[str, Any], int]:
            data = request.get_json() or {}
            req = ActivateProjectRequest.model_validate(data)
            try:
                project = self._server.activate_project_for_session(session_id, req.project_path_or_name)
                return {
                    "status": "ok",
                    "project_name": project.project_name,
                    "project_root": str(project.project_root),
                }
            except ValueError as e:
                return {"error": str(e)}, 404
            except Exception as e:
                return {"error": str(e)}, 400

        # ========== Tool Execution ==========

        @app.route("/api/tools", methods=["GET"])
        def list_tools() -> dict[str, Any]:
            tools = self._server.get_exposed_tools()
            tool_list = []
            for tool in tools:
                tool_list.append(
                    {
                        "name": tool.get_name(),
                        "description": tool.get_apply_docstring() or "",
                        "parameters": tool.get_apply_fn_metadata().arg_model.model_json_schema(),
                        "can_edit": tool.can_edit(),
                    }
                )
            return {"tools": tool_list}

        @app.route("/api/sessions/<session_id>/tools/<tool_name>", methods=["POST"])
        def call_tool(session_id: str, tool_name: str) -> dict[str, Any]:
            data = request.get_json() or {}
            req = ToolCallRequest.model_validate(data)

            result = self._server.execute_tool(
                session_id=session_id,
                tool_name=tool_name,
                **req.arguments,
            )

            is_error = result.startswith("Error:") if isinstance(result, str) else False
            return {"result": result, "is_error": is_error}

        # ========== Configuration ==========

        @app.route("/api/projects", methods=["GET"])
        def list_projects() -> dict[str, Any]:
            projects = []
            for project in self._server.serena_config.projects:
                projects.append(
                    {
                        "name": project.project_name,
                        "root": str(project.project_root),
                    }
                )
            return {"projects": projects}

        @app.route("/api/modes", methods=["GET"])
        def list_modes() -> dict[str, Any]:
            from serena.config.context_mode import SerenaAgentMode

            modes = SerenaAgentMode.list_registered_mode_names()
            return {"modes": modes}

        @app.route("/api/contexts", methods=["GET"])
        def list_contexts() -> dict[str, Any]:
            from serena.config.context_mode import SerenaAgentContext

            contexts = SerenaAgentContext.list_registered_context_names()
            return {"contexts": contexts}

        # ========== Server Management ==========

        @app.route("/api/shutdown", methods=["PUT"])
        def shutdown_server() -> dict[str, Any]:
            log.info("Shutdown requested via API")

            def do_shutdown() -> None:
                import time

                time.sleep(0.5)  # Give time for response
                self._server.shutdown()
                os._exit(0)

            threading.Thread(target=do_shutdown, daemon=True).start()
            return {"status": "shutting down"}

    @staticmethod
    def _find_first_free_port(start_port: int) -> int:
        """Find the first free port starting from start_port."""
        port = start_port
        while port <= 65535:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("0.0.0.0", port))
                    return port
            except OSError:
                port += 1
        raise RuntimeError(f"No free ports found starting from {start_port}")

    def run(self, host: str = "0.0.0.0", port: int = 8080) -> int:
        """
        Run the dashboard server (blocking).

        :param host: Host to bind to.
        :param port: Port to bind to.
        :return: The actual port used.
        """
        # Suppress Flask banner
        from flask import cli

        cli.show_server_banner = lambda *args, **kwargs: None

        self._app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        return port

    def run_in_thread(self, port: int | None = None) -> tuple[threading.Thread, int]:
        """
        Run the dashboard in a background thread.

        :param port: Port to use, or None to auto-select.
        :return: Tuple of (thread, port).
        """
        if port is None:
            port = self._find_first_free_port(8080)

        thread = threading.Thread(target=lambda: self.run(port=port), daemon=True, name="GlobalDashboard")
        thread.start()
        return thread, port


def create_global_dashboard(server: "CentralizedSerenaServer") -> GlobalDashboardAPI:
    """
    Create a global dashboard instance.

    :param server: The centralized Serena server.
    :return: The dashboard API instance.
    """
    return GlobalDashboardAPI(server)
