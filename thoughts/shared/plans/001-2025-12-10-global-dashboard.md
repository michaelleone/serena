# Global Dashboard Implementation Plan

## Overview

Implement a centralized global dashboard that aggregates and manages all running Serena instances across IDE windows, Claude Code conversations, and Claude Desktop app sessions. This addresses the pain point of multiple dashboards popping up and orphaned processes not being cleaned up.

## Current State Analysis

### Existing Architecture
- Each MCP client spawns a new Serena process (stdio transport = 1:1 per client)
- `SerenaDashboardAPI` in [dashboard.py](src/serena/dashboard.py) runs a Flask app per instance
- Dashboard ports start at 0x5EDA (24282) and auto-increment to find free ports
- No cross-instance awareness or coordination exists
- `SerenaConfig` in [serena_config.py](src/serena/config/serena_config.py) manages `web_dashboard` and `web_dashboard_open_on_launch`

### Key Discoveries
- `SerenaAgent.__init__` ([agent.py:154-284](src/serena/agent.py#L154-L284)) starts dashboard at end of initialization
- `_activate_project` ([agent.py:512-527](src/serena/agent.py#L512-L527)) sets `self._active_project` - hook point for registry updates
- `SerenaDashboardAPI._shutdown` ([dashboard.py:450-457](src/serena/dashboard.py#L450-L457)) handles clean shutdown
- `SerenaPaths.serena_user_home_dir` ([serena_config.py:47-89](src/serena/config/serena_config.py#L47-L89)) points to `~/.serena` - storage location for registry
- Existing dashboard has comprehensive UI with sections for config, tools, executions, projects, modes, contexts

## Desired End State

After implementation:
1. A single global dashboard tab can view/manage ALL Serena instances
2. Instances are tracked in a persistent registry with lifecycle events
3. Zombie processes (unreachable instances) are visually marked red and auto-pruned after 5 minutes
4. Global dashboard opens ONCE (first instance to bind port wins)
5. All existing dashboard functionality preserved and configurable independently
6. Tabs colored: Green (live + project), Yellow (live, no project), Red (zombie)
7. Force kill capability for zombie processes
8. Lifecycle event log showing instance starts, stops, zombie detection, etc.

### Verification Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] `uv run poe test` passes
- [ ] Manual test: Start 3 Serena instances, global dashboard shows all 3 tabs
- [ ] Manual test: Close one instance, tab turns red (zombie) after heartbeat timeout
- [ ] Manual test: Zombie auto-pruned from registry after 5 minutes
- [ ] Manual test: Force kill button terminates zombie process
- [ ] Manual test: Lifecycle log shows all events

## What We're NOT Doing

- NOT modifying the existing per-instance dashboard (purely additive)
- NOT implementing multi-session project isolation (future work)
- NOT changing MCP transport mechanisms
- NOT removing or deprecating existing config options

## Implementation Approach

**Strategy**: Additive-only changes to maintain upstream compatibility. New files for registry and global dashboard, minimal hooks into existing code.

**Dependencies**: Add `filelock` for robust cross-process file locking of the instance registry.

---

## Phase 1: Instance Registry

### Overview
Create a persistent registry tracking all running Serena instances with lifecycle events.

### Changes Required

#### 1. Add `filelock` dependency
**File**: `pyproject.toml`
**Changes**: Add filelock to dependencies

```toml
# In [project.dependencies] section, add:
"filelock>=3.0.0",
```

#### 2. Create Instance Registry Module
**File**: `src/serena/util/instance_registry.py` (NEW)
**Changes**: Create new module with `InstanceInfo`, `LifecycleEvent`, and `InstanceRegistry` classes

```python
"""
Instance registry for tracking running Serena instances across processes.
Uses file-based storage with locking for cross-process coordination.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from filelock import FileLock

from serena.config.serena_config import SerenaPaths

REGISTRY_FILENAME = "instances.json"
LOCK_FILENAME = "instances.lock"
ZOMBIE_TIMEOUT_SECONDS = 5 * 60  # 5 minutes


class InstanceState(str, Enum):
    """State of a Serena instance."""
    LIVE_NO_PROJECT = "live_no_project"
    LIVE_WITH_PROJECT = "live_with_project"
    ZOMBIE = "zombie"


class LifecycleEventType(str, Enum):
    """Types of lifecycle events."""
    INSTANCE_STARTED = "instance_started"
    INSTANCE_STOPPED = "instance_stopped"
    PROJECT_ACTIVATED = "project_activated"
    PROJECT_DEACTIVATED = "project_deactivated"
    ZOMBIE_DETECTED = "zombie_detected"
    ZOMBIE_PRUNED = "zombie_pruned"
    ZOMBIE_FORCE_KILLED = "zombie_force_killed"
    HEARTBEAT_RESTORED = "heartbeat_restored"


@dataclass
class LifecycleEvent:
    """A lifecycle event in the registry."""
    timestamp: float
    event_type: str
    pid: int
    port: int
    project_name: Optional[str] = None
    message: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LifecycleEvent":
        return cls(**data)


@dataclass
class InstanceInfo:
    """Information about a running Serena instance."""
    pid: int
    port: int
    started_at: float
    last_heartbeat: float
    context: Optional[str] = None
    modes: list[str] = field(default_factory=list)
    project_name: Optional[str] = None
    project_root: Optional[str] = None
    state: str = InstanceState.LIVE_NO_PROJECT.value
    zombie_detected_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InstanceInfo":
        # Handle modes as list
        if "modes" not in data:
            data["modes"] = []
        return cls(**data)


@dataclass
class RegistryData:
    """Full registry data structure."""
    instances: dict[int, InstanceInfo] = field(default_factory=dict)
    lifecycle_events: list[LifecycleEvent] = field(default_factory=list)
    global_dashboard_pid: Optional[int] = None
    global_dashboard_port: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "instances": {str(k): v.to_dict() for k, v in self.instances.items()},
            "lifecycle_events": [e.to_dict() for e in self.lifecycle_events],
            "global_dashboard_pid": self.global_dashboard_pid,
            "global_dashboard_port": self.global_dashboard_port,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RegistryData":
        instances = {
            int(k): InstanceInfo.from_dict(v)
            for k, v in data.get("instances", {}).items()
        }
        events = [
            LifecycleEvent.from_dict(e)
            for e in data.get("lifecycle_events", [])
        ]
        return cls(
            instances=instances,
            lifecycle_events=events,
            global_dashboard_pid=data.get("global_dashboard_pid"),
            global_dashboard_port=data.get("global_dashboard_port"),
        )


class InstanceRegistry:
    """
    Thread-safe, file-locked registry for Serena instances.

    Stores instance info and lifecycle events in ~/.serena/instances.json
    with cross-process locking via filelock.
    """

    MAX_LIFECYCLE_EVENTS = 1000  # Keep last N events

    def __init__(self, base_dir: Optional[str] = None) -> None:
        if base_dir is None:
            base_dir = SerenaPaths().serena_user_home_dir
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._base_dir / REGISTRY_FILENAME
        self._lock_path = self._base_dir / LOCK_FILENAME
        self._lock = FileLock(str(self._lock_path), timeout=10)

    def _load(self) -> RegistryData:
        """Load registry data from file (must be called within lock)."""
        if not self._registry_path.exists():
            return RegistryData()
        try:
            with self._registry_path.open(encoding="utf-8") as f:
                data = json.load(f)
            return RegistryData.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            # Corrupted file, start fresh
            return RegistryData()

    def _save(self, data: RegistryData) -> None:
        """Save registry data to file atomically (must be called within lock)."""
        # Trim lifecycle events if too many
        if len(data.lifecycle_events) > self.MAX_LIFECYCLE_EVENTS:
            data.lifecycle_events = data.lifecycle_events[-self.MAX_LIFECYCLE_EVENTS:]

        tmp_path = self._registry_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data.to_dict(), f, indent=2)
        tmp_path.replace(self._registry_path)

    def _add_event(
        self,
        data: RegistryData,
        event_type: LifecycleEventType,
        pid: int,
        port: int,
        project_name: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """Add a lifecycle event (must be called within lock)."""
        event = LifecycleEvent(
            timestamp=time.time(),
            event_type=event_type.value,
            pid=pid,
            port=port,
            project_name=project_name,
            message=message,
        )
        data.lifecycle_events.append(event)

    def register(
        self,
        pid: int,
        port: int,
        context: Optional[str] = None,
        modes: Optional[list[str]] = None,
    ) -> InstanceInfo:
        """Register a new instance or update existing."""
        with self._lock:
            data = self._load()
            now = time.time()

            existing = data.instances.get(pid)
            if existing is None:
                # New instance
                info = InstanceInfo(
                    pid=pid,
                    port=port,
                    started_at=now,
                    last_heartbeat=now,
                    context=context,
                    modes=modes or [],
                    state=InstanceState.LIVE_NO_PROJECT.value,
                )
                data.instances[pid] = info
                self._add_event(
                    data, LifecycleEventType.INSTANCE_STARTED,
                    pid, port, message=f"Context: {context}, Modes: {modes}"
                )
            else:
                # Update existing
                existing.port = port
                existing.last_heartbeat = now
                existing.context = context
                existing.modes = modes or []
                # If was zombie, mark as restored
                if existing.state == InstanceState.ZOMBIE.value:
                    existing.state = InstanceState.LIVE_NO_PROJECT.value
                    existing.zombie_detected_at = None
                    self._add_event(
                        data, LifecycleEventType.HEARTBEAT_RESTORED,
                        pid, port
                    )
                info = existing

            self._save(data)
            return info

    def update_project(
        self,
        pid: int,
        project_name: Optional[str],
        project_root: Optional[str] = None,
    ) -> None:
        """Update the active project for an instance."""
        with self._lock:
            data = self._load()
            if pid not in data.instances:
                return

            inst = data.instances[pid]
            old_project = inst.project_name
            inst.project_name = project_name
            inst.project_root = project_root
            inst.last_heartbeat = time.time()

            if project_name:
                inst.state = InstanceState.LIVE_WITH_PROJECT.value
                if old_project != project_name:
                    self._add_event(
                        data, LifecycleEventType.PROJECT_ACTIVATED,
                        pid, inst.port, project_name=project_name
                    )
            else:
                inst.state = InstanceState.LIVE_NO_PROJECT.value
                if old_project:
                    self._add_event(
                        data, LifecycleEventType.PROJECT_DEACTIVATED,
                        pid, inst.port, project_name=old_project
                    )

            self._save(data)

    def update_heartbeat(self, pid: int) -> None:
        """Update the last heartbeat time for an instance."""
        with self._lock:
            data = self._load()
            if pid in data.instances:
                inst = data.instances[pid]
                inst.last_heartbeat = time.time()
                # Restore from zombie if was marked
                if inst.state == InstanceState.ZOMBIE.value:
                    inst.state = (
                        InstanceState.LIVE_WITH_PROJECT.value
                        if inst.project_name
                        else InstanceState.LIVE_NO_PROJECT.value
                    )
                    inst.zombie_detected_at = None
                    self._add_event(
                        data, LifecycleEventType.HEARTBEAT_RESTORED,
                        pid, inst.port, project_name=inst.project_name
                    )
                self._save(data)

    def unregister(self, pid: int) -> None:
        """Unregister an instance (clean shutdown)."""
        with self._lock:
            data = self._load()
            if pid in data.instances:
                inst = data.instances[pid]
                self._add_event(
                    data, LifecycleEventType.INSTANCE_STOPPED,
                    pid, inst.port, project_name=inst.project_name
                )
                del data.instances[pid]
                self._save(data)

    def mark_zombie(self, pid: int) -> None:
        """Mark an instance as zombie (unreachable)."""
        with self._lock:
            data = self._load()
            if pid in data.instances:
                inst = data.instances[pid]
                if inst.state != InstanceState.ZOMBIE.value:
                    inst.state = InstanceState.ZOMBIE.value
                    inst.zombie_detected_at = time.time()
                    self._add_event(
                        data, LifecycleEventType.ZOMBIE_DETECTED,
                        pid, inst.port, project_name=inst.project_name
                    )
                    self._save(data)

    def prune_zombies(self, timeout_seconds: float = ZOMBIE_TIMEOUT_SECONDS) -> list[int]:
        """Remove zombies that have been dead for longer than timeout. Returns pruned PIDs."""
        pruned = []
        with self._lock:
            data = self._load()
            now = time.time()

            to_remove = []
            for pid, inst in data.instances.items():
                if inst.state == InstanceState.ZOMBIE.value:
                    if inst.zombie_detected_at and (now - inst.zombie_detected_at) > timeout_seconds:
                        to_remove.append(pid)

            for pid in to_remove:
                inst = data.instances[pid]
                self._add_event(
                    data, LifecycleEventType.ZOMBIE_PRUNED,
                    pid, inst.port, project_name=inst.project_name,
                    message=f"Auto-pruned after {timeout_seconds}s"
                )
                del data.instances[pid]
                pruned.append(pid)

            if pruned:
                self._save(data)

        return pruned

    def record_force_kill(self, pid: int, success: bool) -> None:
        """Record a force kill attempt."""
        with self._lock:
            data = self._load()
            inst = data.instances.get(pid)
            port = inst.port if inst else 0
            project = inst.project_name if inst else None

            self._add_event(
                data, LifecycleEventType.ZOMBIE_FORCE_KILLED,
                pid, port, project_name=project,
                message=f"Force kill {'succeeded' if success else 'failed'}"
            )

            # Remove from registry if kill succeeded
            if success and pid in data.instances:
                del data.instances[pid]

            self._save(data)

    def list_instances(self) -> list[InstanceInfo]:
        """Get all registered instances."""
        with self._lock:
            data = self._load()
            return list(data.instances.values())

    def get_instance(self, pid: int) -> Optional[InstanceInfo]:
        """Get a specific instance by PID."""
        with self._lock:
            data = self._load()
            return data.instances.get(pid)

    def get_lifecycle_events(self, limit: int = 100) -> list[LifecycleEvent]:
        """Get recent lifecycle events."""
        with self._lock:
            data = self._load()
            return data.lifecycle_events[-limit:]

    def set_global_dashboard(self, pid: int, port: int) -> None:
        """Record which instance is running the global dashboard."""
        with self._lock:
            data = self._load()
            data.global_dashboard_pid = pid
            data.global_dashboard_port = port
            self._save(data)

    def get_global_dashboard_port(self) -> Optional[int]:
        """Get the port of the running global dashboard, if any."""
        with self._lock:
            data = self._load()
            return data.global_dashboard_port

    def clear_global_dashboard(self, pid: int) -> None:
        """Clear global dashboard record if it matches the given PID."""
        with self._lock:
            data = self._load()
            if data.global_dashboard_pid == pid:
                data.global_dashboard_pid = None
                data.global_dashboard_port = None
                self._save(data)
```

### Success Criteria

#### Automated Verification:
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] Unit tests for InstanceRegistry pass (to be added)

---

## Phase 2: Configuration Extensions

### Overview
Add new configuration options for the global dashboard while preserving all existing options.

### Changes Required

#### 1. Add Constants
**File**: `src/serena/constants.py`
**Changes**: Add global dashboard directory constant and default port

```python
# Add after SERENA_DASHBOARD_DIR line:
SERENA_GLOBAL_DASHBOARD_DIR = str(_serena_pkg_path / "resources" / "global_dashboard")
SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT = 25282  # 0x62C2
```

#### 2. Extend SerenaConfig
**File**: `src/serena/config/serena_config.py`
**Changes**: Add three new fields to `SerenaConfig` dataclass and load them in `from_config_file`

In the `SerenaConfig` class definition (around line 367), add after `web_dashboard_open_on_launch`:

```python
    web_dashboard_global: bool = False
    """Whether to start the global dashboard that shows all running Serena instances."""

    web_dashboard_global_open_on_launch: bool = False
    """Whether to open the global dashboard in browser when Serena starts (if enabled)."""

    web_dashboard_global_port: int | None = None
    """Port for the global dashboard. If None, uses default (25282). Auto-increments if busy."""
```

In `from_config_file` method, add after loading `web_dashboard_open_on_launch` (around line 505):

```python
        instance.web_dashboard_global = loaded_commented_yaml.get("web_dashboard_global", False)
        instance.web_dashboard_global_open_on_launch = loaded_commented_yaml.get("web_dashboard_global_open_on_launch", False)
        instance.web_dashboard_global_port = loaded_commented_yaml.get("web_dashboard_global_port", None)
```

#### 3. Update Config Template
**File**: `src/serena/resources/serena_config.template.yml`
**Changes**: Add new config options with documentation

Add after `web_dashboard_open_on_launch` section:

```yaml
web_dashboard_global: False
# whether to start a global dashboard that aggregates all running Serena instances
# into a single tabbed interface. This allows you to view and manage all instances
# from one browser tab instead of having multiple dashboard tabs open.

web_dashboard_global_open_on_launch: False
# whether to open the global dashboard in browser when enabled. Only the first
# Serena instance to start will open the browser; subsequent instances will
# register with the existing global dashboard.

web_dashboard_global_port: 25282
# port for the global dashboard (0x62C2). If this port is already in use by
# another application (not a Serena global dashboard), the next available port
# will be used. If a global dashboard is already running, new instances will
# simply register with it rather than starting another one.
```

### Success Criteria

#### Automated Verification:
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] Config template is valid YAML

---

## Phase 3: Global Dashboard Backend

### Overview
Create the Flask-based global dashboard API that proxies requests to individual instances and manages the registry.

### Changes Required

#### 1. Create Global Dashboard Module
**File**: `src/serena/global_dashboard.py` (NEW)
**Changes**: Create Flask app with proxy routes and instance management

```python
"""
Global dashboard for managing all running Serena instances.

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
from typing import Any, Optional

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from pydantic import BaseModel

from serena.constants import SERENA_GLOBAL_DASHBOARD_DIR, SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT
from serena.util.instance_registry import (
    InstanceRegistry,
    InstanceState,
    ZOMBIE_TIMEOUT_SECONDS,
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
    """
    Flask-based global dashboard for all Serena instances.

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
        self._app = Flask(__name__)
        self._registry = registry or InstanceRegistry()
        self._heartbeat_thread: threading.Thread | None = None
        self._prune_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._setup_routes()

    def _setup_routes(self) -> None:
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

    def _proxy_to_instance(
        self, pid: int, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
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
        """Run the global dashboard (blocking)."""
        # Suppress Flask startup banner
        import flask.cli
        flask.cli.show_server_banner = lambda *args, **kwargs: None

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
        """
        Start the global dashboard in a thread if no other global dashboard is running.

        Returns (thread, port) if started, (None, existing_port) if another is running,
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
```

### Success Criteria

#### Automated Verification:
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes

---

## Phase 4: Global Dashboard Frontend

### Overview
Create the tabbed UI for the global dashboard, reusing existing dashboard styles and patterns.

### Changes Required

#### 1. Create Global Dashboard Directory
**File**: `src/serena/resources/global_dashboard/` (NEW directory)

#### 2. Create index.html
**File**: `src/serena/resources/global_dashboard/index.html` (NEW)

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Serena Global Dashboard</title>
    <link rel="icon" type="image/png" sizes="16x16" href="../dashboard/serena-icon-16.png">
    <link rel="icon" type="image/png" sizes="32x32" href="../dashboard/serena-icon-32.png">
    <link rel="icon" type="image/png" sizes="48x48" href="../dashboard/serena-icon-48.png">
    <link rel="stylesheet" href="../dashboard/dashboard.css">
    <link rel="stylesheet" href="global_dashboard.css">
    <script src="../dashboard/jquery.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2"></script>
    <script src="global_dashboard.js"></script>
</head>
<body>
<div id="frame">
    <header class="header">
        <div class="header-left">
            <div class="logo-container">
                <img id="serena-logo" src="../dashboard/serena-logs.png" alt="Serena">
            </div>
            <span class="global-badge">GLOBAL</span>
        </div>
        <nav class="header-nav">
            <button id="theme-toggle" class="theme-toggle">
                <span id="theme-icon" style="height: 21px">&#127769;</span>
                <span id="theme-text">Dark</span>
            </button>
            <button id="menu-toggle" class="menu-button">
                <span>&#9776;</span>
                <span>Menu</span>
            </button>
            <div id="menu-dropdown" class="menu-dropdown" style="display:none">
                <a href="#" data-page="instances" class="active">Instances</a>
                <a href="#" data-page="lifecycle">Lifecycle Log</a>
            </div>
        </nav>
    </header>

    <div class="main">
        <!-- Instances Page -->
        <div id="page-instances" class="page-view">
            <div class="tab-bar" id="instance-tabs">
                <div class="loading">Loading instances...</div>
            </div>
            <div id="instance-content">
                <div class="no-instances-message" style="text-align: center; padding: 40px; color: var(--text-muted);">
                    Select an instance tab above to view details
                </div>
            </div>
        </div>

        <!-- Lifecycle Log Page -->
        <div id="page-lifecycle" class="page-view" style="display:none">
            <div class="lifecycle-container">
                <div class="lifecycle-header">
                    <h2>Lifecycle Events</h2>
                    <button id="refresh-lifecycle" class="btn">Refresh</button>
                </div>
                <div id="lifecycle-events" class="lifecycle-events">
                    <div class="loading">Loading events...</div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    $(document).ready(function() {
        const dashboard = new GlobalDashboard();
    });
</script>
</body>
</html>
```

#### 3. Create global_dashboard.css
**File**: `src/serena/resources/global_dashboard/global_dashboard.css` (NEW)

```css
/* Global Dashboard Specific Styles */

.global-badge {
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    color: white;
    font-size: 10px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 4px;
    margin-left: 10px;
    letter-spacing: 1px;
}

/* Tab Bar */
.tab-bar {
    display: flex;
    gap: 8px;
    padding: 10px 15px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
    overflow-x: auto;
    flex-wrap: nowrap;
}

.instance-tab {
    padding: 8px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-family: monospace;
    font-size: 13px;
    white-space: nowrap;
    transition: all 0.2s ease;
    border: 2px solid transparent;
    display: flex;
    align-items: center;
    gap: 8px;
}

.instance-tab:hover {
    filter: brightness(1.1);
}

.instance-tab.active {
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.5);
}

/* Tab States */
.tab-live-with-project {
    background: #d1fae5;
    border-color: #059669;
    color: #065f46;
}

.tab-live-no-project {
    background: #fef3c7;
    border-color: #d97706;
    color: #92400e;
}

.tab-zombie {
    background: #fee2e2;
    border-color: #b91c1c;
    color: #7f1d1d;
}

/* Dark mode tab colors */
[data-theme="dark"] .tab-live-with-project {
    background: #064e3b;
    border-color: #10b981;
    color: #a7f3d0;
}

[data-theme="dark"] .tab-live-no-project {
    background: #78350f;
    border-color: #f59e0b;
    color: #fef3c7;
}

[data-theme="dark"] .tab-zombie {
    background: #7f1d1d;
    border-color: #ef4444;
    color: #fecaca;
}

/* Tab status indicator */
.tab-status {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
}

.tab-live-with-project .tab-status {
    background: #059669;
}

.tab-live-no-project .tab-status {
    background: #d97706;
}

.tab-zombie .tab-status {
    background: #b91c1c;
    animation: pulse-red 1.5s infinite;
}

@keyframes pulse-red {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}

/* Instance Content Area */
#instance-content {
    padding: 20px;
    min-height: 400px;
}

.instance-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    padding-bottom: 15px;
    border-bottom: 1px solid var(--border-color);
}

.instance-title {
    font-size: 18px;
    font-weight: 600;
}

.instance-meta {
    color: var(--text-muted);
    font-size: 13px;
}

.instance-actions {
    display: flex;
    gap: 10px;
}

.btn-danger {
    background: #dc2626;
    color: white;
}

.btn-danger:hover {
    background: #b91c1c;
}

.btn-warning {
    background: #f59e0b;
    color: white;
}

.btn-warning:hover {
    background: #d97706;
}

/* Zombie Warning Banner */
.zombie-banner {
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 6px;
    padding: 15px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 15px;
}

[data-theme="dark"] .zombie-banner {
    background: #450a0a;
    border-color: #7f1d1d;
}

.zombie-banner-icon {
    font-size: 24px;
}

.zombie-banner-text {
    flex: 1;
}

.zombie-banner-title {
    font-weight: 600;
    color: #991b1b;
    margin-bottom: 4px;
}

[data-theme="dark"] .zombie-banner-title {
    color: #fca5a5;
}

.zombie-banner-message {
    font-size: 13px;
    color: #7f1d1d;
}

[data-theme="dark"] .zombie-banner-message {
    color: #fecaca;
}

/* Lifecycle Events */
.lifecycle-container {
    padding: 20px;
}

.lifecycle-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
}

.lifecycle-events {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    max-height: 600px;
    overflow-y: auto;
}

.lifecycle-event {
    display: flex;
    align-items: flex-start;
    padding: 12px 15px;
    border-bottom: 1px solid var(--border-color);
    gap: 15px;
}

.lifecycle-event:last-child {
    border-bottom: none;
}

.event-time {
    font-family: monospace;
    font-size: 12px;
    color: var(--text-muted);
    white-space: nowrap;
    min-width: 140px;
}

.event-icon {
    font-size: 16px;
    width: 24px;
    text-align: center;
}

.event-content {
    flex: 1;
}

.event-type {
    font-weight: 500;
    margin-bottom: 2px;
}

.event-details {
    font-size: 13px;
    color: var(--text-muted);
}

/* Event type colors */
.event-started { color: #059669; }
.event-stopped { color: #6b7280; }
.event-project { color: #3b82f6; }
.event-zombie { color: #dc2626; }
.event-pruned { color: #f59e0b; }
.event-killed { color: #7c3aed; }
.event-restored { color: #10b981; }

/* No instances message */
.no-instances-message {
    background: var(--bg-secondary);
    border: 1px dashed var(--border-color);
    border-radius: 8px;
    padding: 40px;
    text-align: center;
}
```

#### 4. Create global_dashboard.js
**File**: `src/serena/resources/global_dashboard/global_dashboard.js` (NEW)

This file will be substantial (~800 lines). Key functionality:
- Fetch and display instances as tabs
- Handle tab selection and content loading
- Proxy API calls through global dashboard backend
- Display lifecycle events
- Handle shutdown and force-kill actions
- Theme management (reuse existing pattern)
- Auto-refresh instances list

```javascript
/**
 * Global Dashboard for Serena
 *
 * Provides unified management of all running Serena instances.
 */

class GlobalDashboard {
    constructor() {
        this.currentPage = 'instances';
        this.selectedPid = null;
        this.instances = [];
        this.instancePollInterval = null;
        this.contentPollInterval = null;

        // Cache per-instance data
        this.instanceCache = {};

        // Initialize
        this.initializeTheme();
        this.setupEventHandlers();
        this.loadInstances();
        this.startInstancePolling();
    }

    // ===== Event Handlers =====

    setupEventHandlers() {
        const self = this;

        // Menu toggle
        $('#menu-toggle').click(() => this.toggleMenu());

        // Theme toggle
        $('#theme-toggle').click(() => this.toggleTheme());

        // Page navigation
        $('[data-page]').click(function(e) {
            e.preventDefault();
            const page = $(this).data('page');
            self.navigateToPage(page);
        });

        // Close menu on outside click
        $(document).click(function(e) {
            if (!$(e.target).closest('.header-nav').length) {
                $('#menu-dropdown').hide();
            }
        });

        // Lifecycle refresh
        $('#refresh-lifecycle').click(() => this.loadLifecycleEvents());
    }

    toggleMenu() {
        $('#menu-dropdown').toggle();
    }

    navigateToPage(page) {
        $('#menu-dropdown').hide();
        $('.page-view').hide();
        $('#page-' + page).show();
        $('[data-page]').removeClass('active');
        $('[data-page="' + page + '"]').addClass('active');
        this.currentPage = page;

        if (page === 'lifecycle') {
            this.loadLifecycleEvents();
        }
    }

    // ===== Instance Management =====

    loadInstances() {
        const self = this;

        $.ajax({
            url: '/global-dashboard/api/instances',
            type: 'GET',
            success: function(response) {
                self.instances = response.instances || [];
                self.renderInstanceTabs();

                // Auto-select first instance if none selected
                if (self.selectedPid === null && self.instances.length > 0) {
                    self.selectInstance(self.instances[0].pid);
                } else if (self.selectedPid !== null) {
                    // Refresh content for selected instance
                    self.loadInstanceContent(self.selectedPid);
                }
            },
            error: function(xhr, status, error) {
                console.error('Error loading instances:', error);
                $('#instance-tabs').html('<div class="error-message">Error loading instances</div>');
            }
        });
    }

    startInstancePolling() {
        // Poll for instance changes every 3 seconds
        this.instancePollInterval = setInterval(() => this.loadInstances(), 3000);
    }

    renderInstanceTabs() {
        const self = this;
        const $tabBar = $('#instance-tabs');

        if (this.instances.length === 0) {
            $tabBar.html('<div class="no-instances-message">No Serena instances running</div>');
            $('#instance-content').html('<div class="no-instances-message">Start a Serena instance to see it here</div>');
            return;
        }

        let html = '';
        this.instances.forEach(function(inst) {
            const tabClass = self.getTabClass(inst.state);
            const activeClass = inst.pid === self.selectedPid ? ' active' : '';
            const projectLabel = inst.project_name || 'NO PROJECT';

            html += `<div class="instance-tab ${tabClass}${activeClass}" data-pid="${inst.pid}">`;
            html += `<span class="tab-status"></span>`;
            html += `<span class="tab-label">${inst.pid} - ${self.escapeHtml(projectLabel)}</span>`;
            html += '</div>';
        });

        $tabBar.html(html);

        // Attach click handlers
        $('.instance-tab').click(function() {
            const pid = parseInt($(this).data('pid'));
            self.selectInstance(pid);
        });
    }

    getTabClass(state) {
        switch (state) {
            case 'live_with_project':
                return 'tab-live-with-project';
            case 'live_no_project':
                return 'tab-live-no-project';
            case 'zombie':
                return 'tab-zombie';
            default:
                return 'tab-live-no-project';
        }
    }

    selectInstance(pid) {
        this.selectedPid = pid;

        // Update tab styling
        $('.instance-tab').removeClass('active');
        $(`.instance-tab[data-pid="${pid}"]`).addClass('active');

        // Load content
        this.loadInstanceContent(pid);
    }

    // ===== Instance Content =====

    loadInstanceContent(pid) {
        const self = this;
        const inst = this.instances.find(i => i.pid === pid);

        if (!inst) {
            $('#instance-content').html('<div class="error-message">Instance not found</div>');
            return;
        }

        // If zombie, show zombie banner
        if (inst.state === 'zombie') {
            this.renderZombieContent(inst);
            return;
        }

        // Load config overview from the instance
        $.ajax({
            url: `/global-dashboard/api/instance/${pid}/config-overview`,
            type: 'GET',
            success: function(response) {
                if (response.error) {
                    self.renderErrorContent(inst, response.error);
                } else {
                    self.renderInstanceContent(inst, response);
                }
            },
            error: function(xhr, status, error) {
                self.renderErrorContent(inst, error);
            }
        });
    }

    renderZombieContent(inst) {
        const self = this;
        const zombieTime = inst.zombie_detected_at ?
            new Date(inst.zombie_detected_at * 1000).toLocaleString() : 'Unknown';

        let html = `
            <div class="instance-header">
                <div>
                    <div class="instance-title">${inst.pid} - ${this.escapeHtml(inst.project_name || 'NO PROJECT')}</div>
                    <div class="instance-meta">Port: ${inst.port} | Context: ${inst.context || 'N/A'}</div>
                </div>
                <div class="instance-actions">
                    <button class="btn btn-danger" id="force-kill-btn">Force Kill</button>
                </div>
            </div>

            <div class="zombie-banner">
                <div class="zombie-banner-icon">&#9760;</div>
                <div class="zombie-banner-text">
                    <div class="zombie-banner-title">Instance Unreachable (Zombie)</div>
                    <div class="zombie-banner-message">
                        This instance is no longer responding to health checks.
                        Detected at: ${zombieTime}.
                        It will be automatically removed in 5 minutes, or you can force kill it now.
                    </div>
                </div>
            </div>

            <div class="config-section">
                <h3>Last Known State</h3>
                <div class="config-grid">
                    <div class="config-label">Project:</div>
                    <div class="config-value">${this.escapeHtml(inst.project_name || 'None')}</div>
                    <div class="config-label">Project Root:</div>
                    <div class="config-value">${this.escapeHtml(inst.project_root || 'N/A')}</div>
                    <div class="config-label">Context:</div>
                    <div class="config-value">${this.escapeHtml(inst.context || 'N/A')}</div>
                    <div class="config-label">Modes:</div>
                    <div class="config-value">${inst.modes.join(', ') || 'N/A'}</div>
                    <div class="config-label">Started:</div>
                    <div class="config-value">${new Date(inst.started_at * 1000).toLocaleString()}</div>
                    <div class="config-label">Last Heartbeat:</div>
                    <div class="config-value">${new Date(inst.last_heartbeat * 1000).toLocaleString()}</div>
                </div>
            </div>
        `;

        $('#instance-content').html(html);

        // Force kill handler
        $('#force-kill-btn').click(function() {
            if (confirm(`Force kill process ${inst.pid}? This will send SIGTERM/SIGKILL to the process.`)) {
                self.forceKillInstance(inst.pid);
            }
        });
    }

    renderErrorContent(inst, error) {
        const html = `
            <div class="instance-header">
                <div>
                    <div class="instance-title">${inst.pid} - ${this.escapeHtml(inst.project_name || 'NO PROJECT')}</div>
                    <div class="instance-meta">Port: ${inst.port}</div>
                </div>
            </div>
            <div class="error-message">
                Error communicating with instance: ${this.escapeHtml(error)}
            </div>
        `;
        $('#instance-content').html(html);
    }

    renderInstanceContent(inst, config) {
        const self = this;

        let html = `
            <div class="instance-header">
                <div>
                    <div class="instance-title">${inst.pid} - ${this.escapeHtml(config.active_project?.name || 'NO PROJECT')}</div>
                    <div class="instance-meta">
                        Port: ${inst.port} |
                        Context: ${config.context?.name || 'N/A'} |
                        Started: ${new Date(inst.started_at * 1000).toLocaleString()}
                    </div>
                </div>
                <div class="instance-actions">
                    <a href="http://127.0.0.1:${inst.port}/dashboard/" target="_blank" class="btn">Open Full Dashboard</a>
                    <button class="btn btn-warning" id="shutdown-btn">Shutdown</button>
                </div>
            </div>
        `;

        // Configuration section
        html += '<section class="config-section"><h2>Current Configuration</h2>';
        html += '<div class="config-grid">';
        html += `<div class="config-label">Active Project:</div>`;
        html += `<div class="config-value">${this.escapeHtml(config.active_project?.name || 'None')}</div>`;

        if (config.active_project?.path) {
            html += `<div class="config-label">Project Path:</div>`;
            html += `<div class="config-value">${this.escapeHtml(config.active_project.path)}</div>`;
        }

        html += `<div class="config-label">Languages:</div>`;
        html += `<div class="config-value">${(config.languages || []).join(', ') || 'N/A'}</div>`;

        html += `<div class="config-label">Active Modes:</div>`;
        html += `<div class="config-value">${(config.modes || []).map(m => m.name).join(', ') || 'None'}</div>`;

        html += `<div class="config-label">File Encoding:</div>`;
        html += `<div class="config-value">${config.encoding || 'N/A'}</div>`;
        html += '</div></section>';

        // Tool Usage section
        html += '<section class="basic-stats-section"><h2>Tool Usage</h2>';
        html += '<div id="tool-stats-display">';

        const stats = config.tool_stats_summary || {};
        if (Object.keys(stats).length === 0) {
            html += '<div class="no-stats-message">No tool usage stats collected yet.</div>';
        } else {
            const sortedTools = Object.keys(stats).sort((a, b) => stats[b].num_calls - stats[a].num_calls);
            const maxCalls = Math.max(...sortedTools.map(t => stats[t].num_calls));

            sortedTools.forEach(function(toolName) {
                const count = stats[toolName].num_calls;
                const pct = maxCalls > 0 ? (count / maxCalls * 100) : 0;

                html += `<div class="stat-bar-container">`;
                html += `<div class="stat-tool-name" title="${toolName}">${toolName}</div>`;
                html += `<div class="bar-wrapper"><div class="bar" style="width: ${pct}%"></div></div>`;
                html += `<div class="stat-count">${count}</div>`;
                html += `</div>`;
            });
        }
        html += '</div></section>';

        // Active Tools section (collapsible)
        html += '<section class="projects-section">';
        html += `<h2 class="collapsible-header" id="tools-header-${inst.pid}">`;
        html += `<span>Active Tools (${(config.active_tools || []).length})</span>`;
        html += '<span class="toggle-icon">&#9660;</span></h2>';
        html += `<div class="collapsible-content tools-grid" id="tools-content-${inst.pid}" style="display:none;">`;
        (config.active_tools || []).forEach(function(tool) {
            html += `<div class="tool-item">${tool}</div>`;
        });
        html += '</div></section>';

        $('#instance-content').html(html);

        // Event handlers
        $('#shutdown-btn').click(function() {
            if (confirm(`Shutdown Serena instance ${inst.pid}?`)) {
                self.shutdownInstance(inst.pid);
            }
        });

        // Collapsible sections
        $(`#tools-header-${inst.pid}`).click(function() {
            $(`#tools-content-${inst.pid}`).slideToggle(300);
            $(this).find('.toggle-icon').toggleClass('expanded');
        });
    }

    // ===== Instance Actions =====

    shutdownInstance(pid) {
        const self = this;

        $.ajax({
            url: `/global-dashboard/api/instance/${pid}/shutdown`,
            type: 'PUT',
            success: function(response) {
                console.log('Shutdown response:', response);
                // Remove from local list and refresh
                self.instances = self.instances.filter(i => i.pid !== pid);
                if (self.selectedPid === pid) {
                    self.selectedPid = self.instances.length > 0 ? self.instances[0].pid : null;
                }
                self.renderInstanceTabs();
                if (self.selectedPid) {
                    self.loadInstanceContent(self.selectedPid);
                } else {
                    $('#instance-content').html('<div class="no-instances-message">No instances selected</div>');
                }
            },
            error: function(xhr, status, error) {
                alert('Error shutting down instance: ' + error);
            }
        });
    }

    forceKillInstance(pid) {
        const self = this;

        $.ajax({
            url: `/global-dashboard/api/instance/${pid}/force-kill`,
            type: 'POST',
            success: function(response) {
                if (response.ok) {
                    alert(`Process ${pid} has been killed.`);
                    self.loadInstances();
                } else {
                    alert(`Failed to kill process: ${response.error}`);
                }
            },
            error: function(xhr, status, error) {
                alert('Error force-killing instance: ' + error);
            }
        });
    }

    // ===== Lifecycle Events =====

    loadLifecycleEvents() {
        const self = this;

        $.ajax({
            url: '/global-dashboard/api/lifecycle-events?limit=200',
            type: 'GET',
            success: function(response) {
                self.renderLifecycleEvents(response.events || []);
            },
            error: function(xhr, status, error) {
                $('#lifecycle-events').html('<div class="error-message">Error loading events</div>');
            }
        });
    }

    renderLifecycleEvents(events) {
        if (events.length === 0) {
            $('#lifecycle-events').html('<div class="no-instances-message">No lifecycle events recorded yet</div>');
            return;
        }

        // Reverse to show newest first
        events = events.slice().reverse();

        let html = '';
        events.forEach(event => {
            const time = new Date(event.timestamp * 1000).toLocaleString();
            const icon = this.getEventIcon(event.event_type);
            const typeClass = this.getEventClass(event.event_type);
            const typeLabel = this.formatEventType(event.event_type);

            html += `<div class="lifecycle-event">`;
            html += `<div class="event-time">${time}</div>`;
            html += `<div class="event-icon">${icon}</div>`;
            html += `<div class="event-content">`;
            html += `<div class="event-type ${typeClass}">${typeLabel}</div>`;
            html += `<div class="event-details">`;
            html += `PID: ${event.pid} | Port: ${event.port}`;
            if (event.project_name) {
                html += ` | Project: ${this.escapeHtml(event.project_name)}`;
            }
            if (event.message) {
                html += `<br>${this.escapeHtml(event.message)}`;
            }
            html += `</div></div></div>`;
        });

        $('#lifecycle-events').html(html);
    }

    getEventIcon(eventType) {
        const icons = {
            'instance_started': '&#9654;',      // Play
            'instance_stopped': '&#9632;',      // Stop
            'project_activated': '&#128193;',   // Folder
            'project_deactivated': '&#128194;', // Empty folder
            'zombie_detected': '&#9760;',       // Skull
            'zombie_pruned': '&#128465;',       // Trash
            'zombie_force_killed': '&#9889;',   // Lightning
            'heartbeat_restored': '&#128154;',  // Green heart
        };
        return icons[eventType] || '&#8226;';
    }

    getEventClass(eventType) {
        const classes = {
            'instance_started': 'event-started',
            'instance_stopped': 'event-stopped',
            'project_activated': 'event-project',
            'project_deactivated': 'event-project',
            'zombie_detected': 'event-zombie',
            'zombie_pruned': 'event-pruned',
            'zombie_force_killed': 'event-killed',
            'heartbeat_restored': 'event-restored',
        };
        return classes[eventType] || '';
    }

    formatEventType(eventType) {
        return eventType.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    }

    // ===== Theme Management =====

    initializeTheme() {
        const savedTheme = localStorage.getItem('serena-theme');
        if (savedTheme) {
            this.setTheme(savedTheme);
        } else {
            const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
            this.setTheme(prefersDark ? 'dark' : 'light');
        }
    }

    toggleTheme() {
        const current = document.documentElement.getAttribute('data-theme') || 'light';
        const newTheme = current === 'light' ? 'dark' : 'light';
        localStorage.setItem('serena-theme', newTheme);
        this.setTheme(newTheme);
    }

    setTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);

        if (theme === 'dark') {
            $('#theme-icon').html('&#9728;');  // Sun
            $('#theme-text').text('Light');
            $('#serena-logo').attr('src', '../dashboard/serena-logo-dark-mode.svg');
        } else {
            $('#theme-icon').html('&#127769;');  // Moon
            $('#theme-text').text('Dark');
            $('#serena-logo').attr('src', '../dashboard/serena-logo.svg');
        }
    }

    // ===== Utilities =====

    escapeHtml(text) {
        if (typeof text !== 'string') return text;
        const patterns = {'<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#x27;'};
        return text.replace(/[<>&"']/g, m => patterns[m]);
    }
}
```

### Success Criteria

#### Automated Verification:
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes

---

## Phase 5: Integration into SerenaAgent

### Overview
Wire the instance registry and global dashboard into SerenaAgent initialization and project activation.

### Changes Required

#### 1. Update SerenaAgent.__init__
**File**: `src/serena/agent.py`
**Changes**: Register instance after dashboard starts, optionally start global dashboard

Add imports at top of file:
```python
from serena.util.instance_registry import InstanceRegistry
from serena.global_dashboard import SerenaGlobalDashboardAPI
from serena.constants import SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT
```

In `__init__`, after the dashboard is started (after line ~278), add:

```python
        # Register this instance in the global registry
        self._instance_registry: InstanceRegistry | None = None
        self._global_dashboard_thread: threading.Thread | None = None

        if self.serena_config.web_dashboard:
            self._instance_registry = InstanceRegistry()
            self._dashboard_port = port  # Save for reference
            self._instance_registry.register(
                pid=os.getpid(),
                port=port,
                context=self._context.name if self._context else None,
                modes=[m.name for m in self._modes] if self._modes else [],
            )

            # Start global dashboard if enabled
            if self.serena_config.web_dashboard_global:
                global_port = self.serena_config.web_dashboard_global_port or SERENA_GLOBAL_DASHBOARD_PORT_DEFAULT
                global_dashboard = SerenaGlobalDashboardAPI(self._instance_registry)
                thread, actual_port = global_dashboard.run_in_thread_if_available(
                    preferred_port=global_port,
                    owning_pid=os.getpid(),
                )

                if thread is not None:
                    self._global_dashboard_thread = thread
                    global_url = f"http://127.0.0.1:{actual_port}/global-dashboard/"
                    log.info("Serena global dashboard started at %s", global_url)

                    if self.serena_config.web_dashboard_global_open_on_launch:
                        process = multiprocessing.Process(target=self._open_dashboard, args=(global_url,))
                        process.start()
                        process.join(timeout=1)
                elif actual_port is not None:
                    log.info("Global dashboard already running on port %s", actual_port)
```

#### 2. Update _activate_project
**File**: `src/serena/agent.py`
**Changes**: Update registry when project is activated

In `_activate_project` method (around line 512), after `self._active_project = project`, add:

```python
        # Update instance registry with project info
        if self._instance_registry is not None:
            try:
                self._instance_registry.update_project(
                    pid=os.getpid(),
                    project_name=project.project_name,
                    project_root=project.project_root,
                )
            except Exception as e:
                log.debug(f"Could not update instance registry: {e}")
```

#### 3. Update shutdown to unregister
**File**: `src/serena/agent.py`
**Changes**: Unregister from registry on shutdown

In the `shutdown` method, add before any cleanup:

```python
        # Unregister from instance registry
        if self._instance_registry is not None:
            try:
                self._instance_registry.unregister(os.getpid())
                self._instance_registry.clear_global_dashboard(os.getpid())
            except Exception as e:
                log.debug(f"Could not unregister from instance registry: {e}")
```

#### 4. Update dashboard heartbeat endpoint
**File**: `src/serena/dashboard.py`
**Changes**: Update registry heartbeat on dashboard API calls

In `SerenaDashboardAPI._get_log_messages` method, add at the start:

```python
        # Update heartbeat in registry (the agent instance holds the registry)
        try:
            if hasattr(self._agent, '_instance_registry') and self._agent._instance_registry is not None:
                self._agent._instance_registry.update_heartbeat(os.getpid())
        except Exception:
            pass  # Ignore heartbeat update failures
```

### Success Criteria

#### Automated Verification:
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] `uv run poe test` passes

---

## Phase 6: Testing and Documentation

### Overview
Add unit tests for the instance registry and integration tests for the global dashboard.

### Changes Required

#### 1. Add Unit Tests for Instance Registry
**File**: `test/serena/util/test_instance_registry.py` (NEW)

```python
"""Tests for the instance registry."""
import os
import tempfile
import time

import pytest

from serena.util.instance_registry import (
    InstanceRegistry,
    InstanceState,
    LifecycleEventType,
)


@pytest.fixture
def temp_registry():
    """Create a registry with a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield InstanceRegistry(base_dir=tmpdir)


class TestInstanceRegistry:
    def test_register_new_instance(self, temp_registry):
        """Test registering a new instance."""
        info = temp_registry.register(
            pid=1234,
            port=24282,
            context="test-context",
            modes=["mode1", "mode2"],
        )

        assert info.pid == 1234
        assert info.port == 24282
        assert info.context == "test-context"
        assert info.modes == ["mode1", "mode2"]
        assert info.state == InstanceState.LIVE_NO_PROJECT.value

    def test_update_project(self, temp_registry):
        """Test updating project for an instance."""
        temp_registry.register(pid=1234, port=24282)

        temp_registry.update_project(
            pid=1234,
            project_name="test-project",
            project_root="/path/to/project",
        )

        inst = temp_registry.get_instance(1234)
        assert inst is not None
        assert inst.project_name == "test-project"
        assert inst.project_root == "/path/to/project"
        assert inst.state == InstanceState.LIVE_WITH_PROJECT.value

    def test_mark_zombie(self, temp_registry):
        """Test marking an instance as zombie."""
        temp_registry.register(pid=1234, port=24282)

        temp_registry.mark_zombie(1234)

        inst = temp_registry.get_instance(1234)
        assert inst is not None
        assert inst.state == InstanceState.ZOMBIE.value
        assert inst.zombie_detected_at is not None

    def test_prune_zombies(self, temp_registry):
        """Test pruning old zombies."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.mark_zombie(1234)

        # Should not prune immediately
        pruned = temp_registry.prune_zombies(timeout_seconds=300)
        assert pruned == []

        # Should prune with 0 timeout
        pruned = temp_registry.prune_zombies(timeout_seconds=0)
        assert pruned == [1234]

        # Should be gone
        assert temp_registry.get_instance(1234) is None

    def test_lifecycle_events(self, temp_registry):
        """Test that lifecycle events are recorded."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.update_project(1234, "test-project")
        temp_registry.mark_zombie(1234)

        events = temp_registry.get_lifecycle_events()

        event_types = [e.event_type for e in events]
        assert LifecycleEventType.INSTANCE_STARTED.value in event_types
        assert LifecycleEventType.PROJECT_ACTIVATED.value in event_types
        assert LifecycleEventType.ZOMBIE_DETECTED.value in event_types

    def test_unregister(self, temp_registry):
        """Test clean unregistration."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.unregister(1234)

        assert temp_registry.get_instance(1234) is None

        events = temp_registry.get_lifecycle_events()
        event_types = [e.event_type for e in events]
        assert LifecycleEventType.INSTANCE_STOPPED.value in event_types
```

### Success Criteria

#### Automated Verification:
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] `uv run poe test` passes including new tests

---

## Testing Strategy

### Unit Tests
- Instance registry CRUD operations
- Lifecycle event recording
- Zombie detection and pruning
- File locking behavior

### Integration Tests
- Global dashboard API endpoints
- Proxy routing to instances
- Force kill functionality

### Manual Testing
1. Start 3 Serena instances with different projects
2. Verify global dashboard shows all 3 tabs with correct colors
3. Activate a project on one instance, verify tab turns green
4. Close one instance normally, verify tab disappears
5. Kill one instance with `kill -9`, verify tab turns red (zombie)
6. Wait 5+ minutes, verify zombie is auto-pruned
7. Test force kill button on a zombie
8. Verify lifecycle log shows all events

## Performance Considerations

- Registry file is small (<10KB for typical usage)
- File locking timeout of 10 seconds prevents deadlocks
- Heartbeat checks every 5 seconds - minimal overhead
- Lifecycle events capped at 1000 entries
- Auto-prune runs every 60 seconds

## Migration Notes

No migration needed - purely additive changes. Existing installations will:
1. Continue working unchanged if global dashboard is not enabled
2. Automatically create `~/.serena/instances.json` when first instance registers
3. Config template update is backward compatible (new options have sensible defaults)

## References

- Original idea document: [thoughts/shared/ideas/global-dashboard.md](thoughts/shared/ideas/global-dashboard.md)
- Existing dashboard: [src/serena/dashboard.py](src/serena/dashboard.py)
- SerenaAgent: [src/serena/agent.py](src/serena/agent.py)
- SerenaConfig: [src/serena/config/serena_config.py](src/serena/config/serena_config.py)
