"""Instance registry for tracking running Serena instances across processes.

Uses file-based storage with locking for cross-process coordination.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
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
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LifecycleEvent":
        """Create from dictionary."""
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
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InstanceInfo":
        """Create from dictionary."""
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
        """Convert to dictionary."""
        return {
            "instances": {str(k): v.to_dict() for k, v in self.instances.items()},
            "lifecycle_events": [e.to_dict() for e in self.lifecycle_events],
            "global_dashboard_pid": self.global_dashboard_pid,
            "global_dashboard_port": self.global_dashboard_port,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RegistryData":
        """Create from dictionary."""
        instances = {int(k): InstanceInfo.from_dict(v) for k, v in data.get("instances", {}).items()}
        events = [LifecycleEvent.from_dict(e) for e in data.get("lifecycle_events", [])]
        return cls(
            instances=instances,
            lifecycle_events=events,
            global_dashboard_pid=data.get("global_dashboard_pid"),
            global_dashboard_port=data.get("global_dashboard_port"),
        )


class InstanceRegistry:
    """Thread-safe, file-locked registry for Serena instances.

    Stores instance info and lifecycle events in ~/.serena/instances.json
    with cross-process locking via filelock.
    """

    MAX_LIFECYCLE_EVENTS = 1000  # Keep last N events

    def __init__(self, base_dir: Optional[str] = None) -> None:
        """Initialize the registry.

        Args:
            base_dir: Optional base directory for registry files.
                     If None, uses ~/.serena from SerenaPaths.

        """
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
            data.lifecycle_events = data.lifecycle_events[-self.MAX_LIFECYCLE_EVENTS :]

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
                    data,
                    LifecycleEventType.INSTANCE_STARTED,
                    pid,
                    port,
                    message=f"Context: {context}, Modes: {modes}",
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
                    self._add_event(data, LifecycleEventType.HEARTBEAT_RESTORED, pid, port)
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
                        data,
                        LifecycleEventType.PROJECT_ACTIVATED,
                        pid,
                        inst.port,
                        project_name=project_name,
                    )
            else:
                inst.state = InstanceState.LIVE_NO_PROJECT.value
                if old_project:
                    self._add_event(
                        data,
                        LifecycleEventType.PROJECT_DEACTIVATED,
                        pid,
                        inst.port,
                        project_name=old_project,
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
                    inst.state = InstanceState.LIVE_WITH_PROJECT.value if inst.project_name else InstanceState.LIVE_NO_PROJECT.value
                    inst.zombie_detected_at = None
                    self._add_event(
                        data,
                        LifecycleEventType.HEARTBEAT_RESTORED,
                        pid,
                        inst.port,
                        project_name=inst.project_name,
                    )
                self._save(data)

    def unregister(self, pid: int) -> None:
        """Unregister an instance (clean shutdown)."""
        with self._lock:
            data = self._load()
            if pid in data.instances:
                inst = data.instances[pid]
                self._add_event(
                    data,
                    LifecycleEventType.INSTANCE_STOPPED,
                    pid,
                    inst.port,
                    project_name=inst.project_name,
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
                        data,
                        LifecycleEventType.ZOMBIE_DETECTED,
                        pid,
                        inst.port,
                        project_name=inst.project_name,
                    )
                    self._save(data)

    def prune_zombies(self, timeout_seconds: float = ZOMBIE_TIMEOUT_SECONDS) -> list[int]:
        """Remove zombies that have been dead for longer than timeout.

        Returns:
            List of pruned PIDs.

        """
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
                    data,
                    LifecycleEventType.ZOMBIE_PRUNED,
                    pid,
                    inst.port,
                    project_name=inst.project_name,
                    message=f"Auto-pruned after {timeout_seconds}s",
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
                data,
                LifecycleEventType.ZOMBIE_FORCE_KILLED,
                pid,
                port,
                project_name=project,
                message=f"Force kill {'succeeded' if success else 'failed'}",
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
