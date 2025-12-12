"""
Session management for multi-client Serena server.

Each session represents a connected MCP client with its own state:
- Active project
- Active modes
- Tool usage statistics
- Connection metadata
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from sensai.util import logging

if TYPE_CHECKING:
    from serena.config.context_mode import SerenaAgentMode
    from serena.project import Project

log = logging.getLogger(__name__)


class SessionState(str, Enum):
    """State of a client session."""

    CONNECTED = "connected"
    """Session is connected but no project is active."""

    ACTIVE = "active"
    """Session has an active project and is ready for tool calls."""

    IDLE = "idle"
    """Session is connected but has had no recent activity."""

    DISCONNECTED = "disconnected"
    """Session has been disconnected (kept for history)."""


@dataclass
class SessionInfo:
    """Serializable information about a client session for API responses."""

    session_id: str
    client_name: str | None
    created_at: float
    last_activity: float
    state: SessionState
    active_project_name: str | None = None
    active_project_root: str | None = None
    active_modes: list[str] = field(default_factory=list)
    tool_call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "session_id": self.session_id,
            "client_name": self.client_name,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "state": self.state.value,
            "active_project_name": self.active_project_name,
            "active_project_root": self.active_project_root,
            "active_modes": self.active_modes,
            "tool_call_count": self.tool_call_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionInfo:
        """Create from dictionary."""
        return cls(
            session_id=data["session_id"],
            client_name=data.get("client_name"),
            created_at=data["created_at"],
            last_activity=data["last_activity"],
            state=SessionState(data["state"]),
            active_project_name=data.get("active_project_name"),
            active_project_root=data.get("active_project_root"),
            active_modes=data.get("active_modes", []),
            tool_call_count=data.get("tool_call_count", 0),
        )


class Session:
    """
    Represents a single client session with its own state.

    Each session maintains:
    - Its own active project (can be different from other sessions)
    - Its own active modes
    - Its own tool usage statistics
    - Connection metadata (client name, timestamps)

    Sessions are thread-safe for concurrent access.
    """

    IDLE_TIMEOUT_SECONDS = 300  # 5 minutes without activity = idle

    def __init__(
        self,
        session_id: str | None = None,
        client_name: str | None = None,
    ) -> None:
        """
        Create a new session.

        :param session_id: Unique session identifier. If None, a UUID will be generated.
        :param client_name: Human-readable name for the client (e.g., "claude-desktop", "cursor").
        """
        self.session_id = session_id or str(uuid.uuid4())
        self.client_name = client_name
        self.created_at = time.time()
        self.last_activity = self.created_at
        self._state = SessionState.CONNECTED

        # Session-specific state
        self._active_project: Project | None = None
        self._active_modes: list[SerenaAgentMode] = []
        self._tool_call_count = 0

        # Per-session tool statistics
        self._tool_stats: dict[str, int] = {}

        # Thread safety
        self._lock = threading.RLock()

    @property
    def state(self) -> SessionState:
        """
        Get the current session state.

        Automatically transitions to IDLE if no recent activity.
        """
        with self._lock:
            # Auto-update to idle if no recent activity
            if self._state in (SessionState.CONNECTED, SessionState.ACTIVE):
                if time.time() - self.last_activity > self.IDLE_TIMEOUT_SECONDS:
                    return SessionState.IDLE
            return self._state

    @state.setter
    def state(self, value: SessionState) -> None:
        """Set the session state."""
        with self._lock:
            self._state = value

    def touch(self) -> None:
        """Update last activity timestamp to current time."""
        with self._lock:
            self.last_activity = time.time()

    def get_active_project(self) -> Project | None:
        """Get the currently active project for this session."""
        with self._lock:
            return self._active_project

    def set_active_project(self, project: Project | None) -> None:
        """
        Set the active project for this session.

        :param project: The project to activate, or None to deactivate.
        """
        with self._lock:
            self._active_project = project
            if project:
                self._state = SessionState.ACTIVE
            else:
                self._state = SessionState.CONNECTED
            self.touch()

    def get_active_modes(self) -> list[SerenaAgentMode]:
        """Get the currently active modes for this session."""
        with self._lock:
            return list(self._active_modes)

    def set_active_modes(self, modes: list[SerenaAgentMode]) -> None:
        """
        Set the active modes for this session.

        :param modes: List of modes to activate.
        """
        with self._lock:
            self._active_modes = list(modes)
            self.touch()

    def increment_tool_calls(self, tool_name: str | None = None) -> None:
        """
        Increment the tool call counter.

        :param tool_name: Optional tool name to track per-tool statistics.
        """
        with self._lock:
            self._tool_call_count += 1
            if tool_name:
                self._tool_stats[tool_name] = self._tool_stats.get(tool_name, 0) + 1
            self.touch()

    def get_tool_stats(self) -> dict[str, int]:
        """Get per-tool call statistics for this session."""
        with self._lock:
            return dict(self._tool_stats)

    def get_tool_call_count(self) -> int:
        """Get total tool call count for this session."""
        with self._lock:
            return self._tool_call_count

    def get_info(self) -> SessionInfo:
        """Get a serializable snapshot of session information."""
        with self._lock:
            return SessionInfo(
                session_id=self.session_id,
                client_name=self.client_name,
                created_at=self.created_at,
                last_activity=self.last_activity,
                state=self.state,
                active_project_name=self._active_project.project_name if self._active_project else None,
                active_project_root=str(self._active_project.project_root) if self._active_project else None,
                active_modes=[m.name for m in self._active_modes],
                tool_call_count=self._tool_call_count,
            )

    def disconnect(self) -> None:
        """Mark the session as disconnected."""
        with self._lock:
            self._state = SessionState.DISCONNECTED
            self.touch()

    def __repr__(self) -> str:
        return f"Session(id={self.session_id[:8]}..., client={self.client_name}, state={self.state.value})"


class SessionManager:
    """
    Manages all active client sessions.

    Thread-safe manager for creating, retrieving, and cleaning up sessions.
    Handles session lifecycle and provides APIs for session enumeration.
    """

    CLEANUP_INTERVAL_SECONDS = 60
    DISCONNECTED_RETENTION_SECONDS = 3600  # Keep disconnected sessions for 1 hour

    def __init__(self) -> None:
        """Create a new session manager."""
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self._cleanup_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()

    def create_session(self, client_name: str | None = None, session_id: str | None = None) -> Session:
        """
        Create a new session and register it.

        :param client_name: Human-readable name for the client.
        :param session_id: Optional specific session ID. If None, a UUID will be generated.
        :return: The newly created session.
        """
        session = Session(session_id=session_id, client_name=client_name)
        with self._lock:
            self._sessions[session.session_id] = session
        log.info(f"Created session {session.session_id} for client '{client_name}'")
        return session

    def get_session(self, session_id: str) -> Session | None:
        """
        Get a session by its ID.

        :param session_id: The session ID to look up.
        :return: The session if found, None otherwise.
        """
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        """Get all registered sessions."""
        with self._lock:
            return list(self._sessions.values())

    def list_session_infos(self) -> list[SessionInfo]:
        """Get serializable info for all sessions."""
        with self._lock:
            return [s.get_info() for s in self._sessions.values()]

    def get_active_sessions(self) -> list[Session]:
        """Get sessions that are connected or active (not disconnected)."""
        with self._lock:
            return [s for s in self._sessions.values() if s.state != SessionState.DISCONNECTED]

    def get_session_count(self) -> int:
        """Get the total number of sessions."""
        with self._lock:
            return len(self._sessions)

    def get_active_session_count(self) -> int:
        """Get the number of non-disconnected sessions."""
        with self._lock:
            return len([s for s in self._sessions.values() if s.state != SessionState.DISCONNECTED])

    def remove_session(self, session_id: str) -> bool:
        """
        Remove a session immediately.

        :param session_id: The session ID to remove.
        :return: True if the session was found and removed, False otherwise.
        """
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.disconnect()
                del self._sessions[session_id]
                log.info(f"Removed session {session_id}")
                return True
            return False

    def disconnect_session(self, session_id: str) -> bool:
        """
        Mark a session as disconnected without removing it.

        The session will be removed later by the cleanup thread.

        :param session_id: The session ID to disconnect.
        :return: True if the session was found and disconnected, False otherwise.
        """
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].disconnect()
                log.info(f"Disconnected session {session_id}")
                return True
            return False

    def _cleanup_old_sessions(self) -> int:
        """
        Remove sessions that have been disconnected for too long.

        :return: Number of sessions cleaned up.
        """
        now = time.time()
        cleaned = 0
        with self._lock:
            to_remove = []
            for session_id, session in self._sessions.items():
                if session.state == SessionState.DISCONNECTED:
                    if now - session.last_activity > self.DISCONNECTED_RETENTION_SECONDS:
                        to_remove.append(session_id)

            for session_id in to_remove:
                del self._sessions[session_id]
                log.debug(f"Cleaned up old disconnected session {session_id}")
                cleaned += 1

        return cleaned

    def start_cleanup_thread(self) -> None:
        """Start the background thread that cleans up old disconnected sessions."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            log.warning("Cleanup thread already running")
            return

        def cleanup_loop() -> None:
            log.debug("Session cleanup thread started")
            while not self._shutdown_event.is_set():
                try:
                    cleaned = self._cleanup_old_sessions()
                    if cleaned > 0:
                        log.info(f"Cleaned up {cleaned} old sessions")
                except Exception as e:
                    log.exception(f"Error in session cleanup: {e}")
                self._shutdown_event.wait(self.CLEANUP_INTERVAL_SECONDS)
            log.debug("Session cleanup thread stopped")

        self._cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True, name="SessionCleanup")
        self._cleanup_thread.start()

    def shutdown(self) -> None:
        """
        Shutdown the session manager.

        Disconnects all sessions and stops the cleanup thread.
        """
        log.info("Shutting down session manager")
        self._shutdown_event.set()

        with self._lock:
            for session in self._sessions.values():
                session.disconnect()

        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)

    def __repr__(self) -> str:
        return f"SessionManager(sessions={self.get_session_count()}, active={self.get_active_session_count()})"
