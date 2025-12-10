"""Global dashboard for managing all running Serena instances.

Provides a unified view of all instances with tabbed interface,
lifecycle event logging, and zombie management.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from typing import Any

import requests
from flask import Flask, Response, request, send_from_directory
from pydantic import BaseModel

from serena.constants import SERENA_GLOBAL_DASHBOARD_DIR, SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT
from serena.util.instance_registry import (
    ZOMBIE_TIMEOUT_SECONDS,
    InstanceRegistry,
    InstanceState,
)

log = logging.getLogger(__name__)


class InstanceSummary(BaseModel):
    """Summary of an instance for the frontend."""

    pid: int
    port: int
    started_at: float
    last_heartbeat: float
    project_name: str | None
    project_root: str | None
    context: str | None
    modes: list[str]
    state: str
    zombie_detected_at: float | None


class LifecycleEventSummary(BaseModel):
    """Lifecycle event for the frontend."""

    timestamp: float
    event_type: str
    pid: int
    port: int
    project_name: str | None
    message: str | None


class SerenaGlobalDashboardAPI:
    """Flask-based global dashboard for all Serena instances.

    Provides:
    - Tabbed interface showing all instances
    - Proxy routes to individual instance APIs
    - Lifecycle event log
    - Zombie detection and management
    - Force kill capability
    """

    HEARTBEAT_CHECK_INTERVAL = 5  # seconds
    HEARTBEAT_TIMEOUT = 2  # seconds - how long to wait for heartbeat response
    PRUNE_CHECK_INTERVAL = 60  # seconds

    def __init__(self, registry: InstanceRegistry | None = None) -> None:
        """Initialize the global dashboard.

        Args:
            registry: Optional instance registry. If None, creates a new one.

        """
        self._app = Flask(__name__)
        self._registry = registry or InstanceRegistry()
        self._heartbeat_thread: threading.Thread | None = None
        self._prune_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Set up Flask routes for the global dashboard."""
        app = self._app

        # Static files
        @app.route("/global-dashboard/<path:filename>")
        def serve_dashboard(filename: str) -> Response:
            return send_from_directory(SERENA_GLOBAL_DASHBOARD_DIR, filename)

        @app.route("/global-dashboard/")
        def serve_dashboard_index() -> Response:
            return send_from_directory(SERENA_GLOBAL_DASHBOARD_DIR, "index.html")

        # Instance list with health check
        @app.route("/global-dashboard/api/instances", methods=["GET"])
        def list_instances() -> dict[str, Any]:
            summaries = self._build_instance_summaries()
            # Sort by start time (oldest first)
            summaries.sort(key=lambda x: x.started_at)
            return {"instances": [s.model_dump() for s in summaries]}

        # Lifecycle events
        @app.route("/global-dashboard/api/lifecycle-events", methods=["GET"])
        def get_lifecycle_events() -> dict[str, Any]:
            limit = request.args.get("limit", 100, type=int)
            events = self._registry.get_lifecycle_events(limit)
            return {
                "events": [
                    LifecycleEventSummary(
                        timestamp=e.timestamp,
                        event_type=e.event_type,
                        pid=e.pid,
                        port=e.port,
                        project_name=e.project_name,
                        message=e.message,
                    ).model_dump()
                    for e in events
                ]
            }

        # Proxy routes to individual instances
        @app.route("/global-dashboard/api/instance/<int:pid>/logs", methods=["POST"])
        def proxy_logs(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "post", "/get_log_messages", json=request.get_json())

        @app.route("/global-dashboard/api/instance/<int:pid>/tool-names", methods=["GET"])
        def proxy_tool_names(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "get", "/get_tool_names")

        @app.route("/global-dashboard/api/instance/<int:pid>/tool-stats", methods=["GET"])
        def proxy_tool_stats(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "get", "/get_tool_stats")

        @app.route("/global-dashboard/api/instance/<int:pid>/clear-tool-stats", methods=["POST"])
        def proxy_clear_tool_stats(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "post", "/clear_tool_stats")

        @app.route("/global-dashboard/api/instance/<int:pid>/config-overview", methods=["GET"])
        def proxy_config_overview(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "get", "/get_config_overview")

        @app.route("/global-dashboard/api/instance/<int:pid>/queued-executions", methods=["GET"])
        def proxy_queued_executions(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "get", "/queued_task_executions")

        @app.route("/global-dashboard/api/instance/<int:pid>/last-execution", methods=["GET"])
        def proxy_last_execution(pid: int) -> dict[str, Any]:
            return self._proxy_to_instance(pid, "get", "/last_execution")

        @app.route("/global-dashboard/api/instance/<int:pid>/shutdown", methods=["PUT"])
        def proxy_shutdown(pid: int) -> dict[str, Any]:
            result = self._proxy_to_instance(pid, "put", "/shutdown")
            # The instance should unregister itself, but we'll also clean up
            self._registry.unregister(pid)
            return result

        # Force kill for zombies
        @app.route("/global-dashboard/api/instance/<int:pid>/force-kill", methods=["POST"])
        def force_kill(pid: int) -> dict[str, Any]:
            inst = self._registry.get_instance(pid)
            if inst is None:
                return {"ok": False, "error": "Unknown instance"}

            if inst.state != InstanceState.ZOMBIE.value:
                return {"ok": False, "error": "Can only force-kill zombie instances"}

            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)  # Give it a moment

                # Check if still alive, try SIGKILL
                try:
                    os.kill(pid, 0)  # Check if process exists
                    os.kill(pid, signal.SIGKILL)
                    success = True
                except OSError:
                    success = True  # Process is gone

                self._registry.record_force_kill(pid, success)
                return {"ok": success}
            except OSError as e:
                self._registry.record_force_kill(pid, False)
                return {"ok": False, "error": str(e)}

    def _build_instance_summaries(self) -> list[InstanceSummary]:
        """Build summaries for all instances, checking health."""
        instances = self._registry.list_instances()
        summaries: list[InstanceSummary] = []

        for inst in instances:
            # Build summary from stored state
            summaries.append(
                InstanceSummary(
                    pid=inst.pid,
                    port=inst.port,
                    started_at=inst.started_at,
                    last_heartbeat=inst.last_heartbeat,
                    project_name=inst.project_name,
                    project_root=inst.project_root,
                    context=inst.context,
                    modes=inst.modes,
                    state=inst.state,
                    zombie_detected_at=inst.zombie_detected_at,
                )
            )

        return summaries

    def _proxy_to_instance(self, pid: int, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Proxy a request to a specific instance's dashboard."""
        inst = self._registry.get_instance(pid)
        if inst is None:
            return {"error": f"Unknown instance {pid}"}

        if inst.state == InstanceState.ZOMBIE.value:
            return {"error": f"Instance {pid} is a zombie (unreachable)"}

        url = f"http://127.0.0.1:{inst.port}{path}"
        try:
            response = requests.request(method, url, timeout=5.0, **kwargs)
            response.raise_for_status()
            # Update heartbeat on successful communication
            self._registry.update_heartbeat(pid)
            return response.json()
        except requests.RequestException as e:
            # Mark as zombie if unreachable
            self._registry.mark_zombie(pid)
            return {"error": f"Failed to reach instance {pid}: {e}"}

    def _heartbeat_checker(self) -> None:
        """Background thread that checks instance health."""
        while not self._shutdown_event.is_set():
            try:
                instances = self._registry.list_instances()
                for inst in instances:
                    if inst.state == InstanceState.ZOMBIE.value:
                        continue  # Already marked

                    # Try to reach the instance
                    try:
                        response = requests.get(
                            f"http://127.0.0.1:{inst.port}/heartbeat",
                            timeout=self.HEARTBEAT_TIMEOUT,
                        )
                        if response.ok:
                            self._registry.update_heartbeat(inst.pid)
                    except requests.RequestException:
                        self._registry.mark_zombie(inst.pid)
            except Exception as e:
                log.debug(f"Error in heartbeat checker: {e}")

            self._shutdown_event.wait(self.HEARTBEAT_CHECK_INTERVAL)

    def _prune_checker(self) -> None:
        """Background thread that prunes old zombies."""
        while not self._shutdown_event.is_set():
            try:
                pruned = self._registry.prune_zombies(ZOMBIE_TIMEOUT_SECONDS)
                if pruned:
                    log.info(f"Pruned zombie instances: {pruned}")
            except Exception as e:
                log.debug(f"Error in prune checker: {e}")

            self._shutdown_event.wait(self.PRUNE_CHECK_INTERVAL)

    def _find_first_free_port(self, start_port: int) -> int:
        """Find the first free port starting from start_port."""
        port = start_port
        max_attempts = 100
        for _ in range(max_attempts):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", port))
                sock.close()
                return port
            except OSError:
                port += 1
            finally:
                sock.close()
        raise RuntimeError(f"Could not find free port after {max_attempts} attempts starting from {start_port}")

    def _is_serena_global_dashboard(self, port: int) -> bool:
        """Check if the given port is running a Serena global dashboard."""
        try:
            response = requests.get(
                f"http://127.0.0.1:{port}/global-dashboard/api/instances",
                timeout=1.0,
            )
            return response.ok
        except requests.RequestException:
            return False

    def run(self, host: str = "127.0.0.1", port: int = SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT) -> None:
        """Run the global dashboard (blocking).

        Args:
            host: Host to bind to.
            port: Port to bind to.

        """
        # Suppress Flask startup banner
        import flask.cli

        flask.cli.show_server_banner = lambda *args, **kwargs: None  # type: ignore[method-assign]

        # Start background threads
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_checker, daemon=True)
        self._heartbeat_thread.start()

        self._prune_thread = threading.Thread(target=self._prune_checker, daemon=True)
        self._prune_thread.start()

        self._app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    def run_in_thread_if_available(
        self,
        preferred_port: int | None = None,
        owning_pid: int | None = None,
    ) -> tuple[threading.Thread | None, int | None]:
        """Start the global dashboard in a thread if no other global dashboard is running.

        Returns:
            (thread, port) if started, (None, existing_port) if another is running,
            or (None, None) if failed.

        """
        port = preferred_port or SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT

        # Check if a global dashboard is already recorded in registry
        existing_port = self._registry.get_global_dashboard_port()
        if existing_port is not None:
            # Check if it's actually still running
            if self._is_serena_global_dashboard(existing_port):
                log.info(f"Global dashboard already running on port {existing_port}")
                return None, existing_port
            else:
                # Stale record, clear it
                log.info("Clearing stale global dashboard record from registry")
                self._registry.clear_global_dashboard(0)  # Clear regardless of PID

        # Check if preferred port is available
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.close()
        except OSError:
            # Port is in use - check if it's a Serena global dashboard
            sock.close()
            if self._is_serena_global_dashboard(port):
                log.info(f"Global dashboard already running on port {port}")
                return None, port
            else:
                # Something else is using this port, find another
                port = self._find_first_free_port(port + 1)
                log.info(f"Preferred port busy, using port {port} for global dashboard")

        # Start the dashboard
        thread = threading.Thread(target=lambda: self.run(port=port), daemon=True)
        thread.start()

        # Record in registry
        if owning_pid is not None:
            self._registry.set_global_dashboard(owning_pid, port)

        return thread, port

    def shutdown(self) -> None:
        """Signal shutdown to background threads."""
        self._shutdown_event.set()
