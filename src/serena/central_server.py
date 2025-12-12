"""
Centralized Serena Server for multi-client architecture.

Manages multiple client sessions, routes tool calls, and shares resources
across sessions where possible (e.g., language servers for the same project).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sensai.util import logging

from serena.agent import SerenaAgent, SerenaConfig
from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
from serena.constants import DEFAULT_CONTEXT, DEFAULT_MODES
from serena.session import Session, SessionInfo, SessionManager, SessionState
from serena.tools import Tool
from serena.util.logging import MemoryLogHandler

if TYPE_CHECKING:
    from serena.project import Project

log = logging.getLogger(__name__)


@dataclass
class ServerStats:
    """Statistics about the centralized server."""

    started_at: float
    total_sessions_created: int = 0
    total_tool_calls: int = 0
    active_session_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "uptime_seconds": time.time() - self.started_at,
            "total_sessions_created": self.total_sessions_created,
            "total_tool_calls": self.total_tool_calls,
            "active_session_count": self.active_session_count,
        }


@dataclass
class LifecycleEvent:
    """A lifecycle event in the server."""

    timestamp: float
    event_type: str
    session_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "details": self.details,
        }


class CentralizedSerenaServer:
    """
    Centralized server that manages multiple client sessions.

    This server:
    - Maintains a shared SerenaAgent with tool registry (template agent)
    - Manages sessions for each connected client
    - Routes tool calls to session-specific context
    - Shares resources like language servers across sessions using the same project

    Architecture:
    - Template agent: Used for tool definitions, shared config, and as fallback
    - Sessions: Each client connection creates a session with its own state
    - Tool execution: Routes through the appropriate session context
    """

    MAX_LIFECYCLE_EVENTS = 500

    def __init__(
        self,
        context: str = DEFAULT_CONTEXT,
        modes: list[str] | None = None,
        serena_config: SerenaConfig | None = None,
        memory_log_handler: MemoryLogHandler | None = None,
    ) -> None:
        """
        Initialize the centralized server.

        :param context: The context name for the template agent.
        :param modes: List of mode names for the template agent.
        :param serena_config: Configuration for Serena, or None to load from file.
        :param memory_log_handler: Log handler for the dashboard.
        """
        self._started_at = time.time()
        self._context = SerenaAgentContext.load(context)
        self._default_modes = [SerenaAgentMode.load(m) for m in (modes or list(DEFAULT_MODES))]
        self._serena_config = serena_config or SerenaConfig.from_config_file()
        self._memory_log_handler = memory_log_handler

        # Session management
        self._session_manager = SessionManager()
        self._session_manager.start_cleanup_thread()

        # Statistics
        self._stats = ServerStats(started_at=self._started_at)
        self._stats_lock = threading.Lock()

        # Lifecycle events
        self._lifecycle_events: list[LifecycleEvent] = []
        self._lifecycle_lock = threading.Lock()

        # Create a "template" agent for tool registry and shared config
        # This agent provides tool definitions but sessions maintain their own state
        log.info("Initializing centralized Serena server...")
        self._template_agent = SerenaAgent(
            project=None,  # No default project for template
            serena_config=self._serena_config,
            context=self._context,
            modes=self._default_modes,
            memory_log_handler=self._memory_log_handler,
        )

        # Per-session agents: session_id -> SerenaAgent
        # Each session can have its own agent for isolation
        self._session_agents: dict[str, SerenaAgent] = {}
        self._session_agents_lock = threading.Lock()

        self._add_lifecycle_event("server_started", details={"context": context, "modes": modes or list(DEFAULT_MODES)})

        log.info(f"Centralized Serena Server initialized with context '{context}'")

    @property
    def session_manager(self) -> SessionManager:
        """Get the session manager."""
        return self._session_manager

    @property
    def template_agent(self) -> SerenaAgent:
        """Get the template agent (for tool definitions)."""
        return self._template_agent

    @property
    def serena_config(self) -> SerenaConfig:
        """Get the Serena configuration."""
        return self._serena_config

    def _add_lifecycle_event(
        self,
        event_type: str,
        session_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Add a lifecycle event to the log."""
        event = LifecycleEvent(
            timestamp=time.time(),
            event_type=event_type,
            session_id=session_id,
            details=details or {},
        )
        with self._lifecycle_lock:
            self._lifecycle_events.append(event)
            # Trim if too many
            if len(self._lifecycle_events) > self.MAX_LIFECYCLE_EVENTS:
                self._lifecycle_events = self._lifecycle_events[-self.MAX_LIFECYCLE_EVENTS :]

    def get_lifecycle_events(self, limit: int = 100) -> list[LifecycleEvent]:
        """Get recent lifecycle events."""
        with self._lifecycle_lock:
            return list(self._lifecycle_events[-limit:])

    def get_stats(self) -> ServerStats:
        """Get server statistics."""
        with self._stats_lock:
            self._stats.active_session_count = self._session_manager.get_active_session_count()
            return self._stats

    def create_session(self, client_name: str | None = None) -> Session:
        """
        Create a new client session.

        :param client_name: Human-readable name for the client (e.g., "claude-desktop").
        :return: The newly created session.
        """
        session = self._session_manager.create_session(client_name=client_name)

        # Initialize session with default modes
        session.set_active_modes(list(self._default_modes))

        # Create a dedicated agent for this session
        # This provides full isolation between sessions
        with self._session_agents_lock:
            session_agent = SerenaAgent(
                project=None,
                serena_config=self._serena_config,
                context=self._context,
                modes=self._default_modes,
                memory_log_handler=self._memory_log_handler,
            )
            self._session_agents[session.session_id] = session_agent

        with self._stats_lock:
            self._stats.total_sessions_created += 1

        self._add_lifecycle_event(
            "session_created",
            session_id=session.session_id,
            details={"client_name": client_name},
        )

        log.info(f"Created session {session.session_id} for client '{client_name}'")
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        return self._session_manager.get_session(session_id)

    def get_session_agent(self, session_id: str) -> SerenaAgent | None:
        """Get the agent for a specific session."""
        with self._session_agents_lock:
            return self._session_agents.get(session_id)

    def disconnect_session(self, session_id: str) -> bool:
        """
        Disconnect a session.

        :param session_id: The session ID to disconnect.
        :return: True if disconnected, False if not found.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            return False

        # Shutdown the session's agent
        with self._session_agents_lock:
            agent = self._session_agents.pop(session_id, None)
            if agent is not None:
                try:
                    agent.shutdown()
                except Exception as e:
                    log.warning(f"Error shutting down agent for session {session_id}: {e}")

        # Disconnect the session
        self._session_manager.disconnect_session(session_id)

        self._add_lifecycle_event("session_disconnected", session_id=session_id)

        log.info(f"Disconnected session {session_id}")
        return True

    def execute_tool(
        self,
        session_id: str,
        tool_name: str,
        **kwargs: Any,
    ) -> str:
        """
        Execute a tool in the context of a specific session.

        :param session_id: The session ID.
        :param tool_name: Name of the tool to execute.
        :param kwargs: Tool arguments.
        :return: Tool result as string.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            return f"Error: Unknown session {session_id}"

        if session.state == SessionState.DISCONNECTED:
            return f"Error: Session {session_id} is disconnected"

        # Get the session's agent
        agent = self.get_session_agent(session_id)
        if agent is None:
            # Fallback to template agent
            log.warning(f"No agent for session {session_id}, using template agent")
            agent = self._template_agent

        # Update session activity
        session.increment_tool_calls(tool_name)

        with self._stats_lock:
            self._stats.total_tool_calls += 1

        # Get and execute the tool
        try:
            tool = agent.get_tool_by_name(tool_name)
            result = tool.apply_ex(log_call=True, catch_exceptions=True, **kwargs)

            self._add_lifecycle_event(
                "tool_executed",
                session_id=session_id,
                details={"tool_name": tool_name, "success": True},
            )

            return result
        except Exception as e:
            log.exception(f"Error executing tool {tool_name} for session {session_id}")
            self._add_lifecycle_event(
                "tool_executed",
                session_id=session_id,
                details={"tool_name": tool_name, "success": False, "error": str(e)},
            )
            return f"Error: {e}"

    def get_exposed_tools(self) -> list[Tool]:
        """Get the list of exposed tools from the template agent."""
        return list(self._template_agent.get_exposed_tool_instances())

    def get_exposed_tool_names(self) -> list[str]:
        """Get the names of exposed tools."""
        return [tool.get_name() for tool in self.get_exposed_tools()]

    def activate_project_for_session(
        self,
        session_id: str,
        project_path_or_name: str,
    ) -> Project:
        """
        Activate a project for a specific session.

        :param session_id: The session ID.
        :param project_path_or_name: Path or name of the project to activate.
        :return: The activated project.
        :raises ValueError: If session not found.
        :raises ProjectNotFoundError: If project not found.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session {session_id}")

        agent = self.get_session_agent(session_id)
        if agent is None:
            raise ValueError(f"No agent for session {session_id}")

        # Activate project through the session's agent
        project = agent.activate_project_from_path_or_name(project_path_or_name)

        # Update session state
        session.set_active_project(project)

        self._add_lifecycle_event(
            "project_activated",
            session_id=session_id,
            details={"project_name": project.project_name, "project_root": str(project.project_root)},
        )

        log.info(f"Activated project '{project.project_name}' for session {session_id}")
        return project

    def set_modes_for_session(
        self,
        session_id: str,
        modes: list[str],
    ) -> None:
        """
        Set active modes for a specific session.

        :param session_id: The session ID.
        :param modes: List of mode names to activate.
        :raises ValueError: If session not found.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session {session_id}")

        agent = self.get_session_agent(session_id)
        if agent is None:
            raise ValueError(f"No agent for session {session_id}")

        mode_instances = [SerenaAgentMode.load(m) for m in modes]
        agent.set_modes(mode_instances)
        session.set_active_modes(mode_instances)

        self._add_lifecycle_event(
            "modes_changed",
            session_id=session_id,
            details={"modes": modes},
        )

        log.info(f"Set modes {modes} for session {session_id}")

    def get_system_prompt_for_session(self, session_id: str) -> str:
        """
        Get the system prompt for a specific session.

        :param session_id: The session ID.
        :return: The system prompt.
        """
        agent = self.get_session_agent(session_id)
        if agent is None:
            return self._template_agent.create_system_prompt()
        return agent.create_system_prompt()

    def list_sessions(self) -> list[SessionInfo]:
        """Get info for all sessions."""
        return self._session_manager.list_session_infos()

    def get_session_details(self, session_id: str) -> dict[str, Any] | None:
        """
        Get detailed information about a session.

        :param session_id: The session ID.
        :return: Session details dictionary, or None if not found.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            return None

        agent = self.get_session_agent(session_id)

        info = session.get_info().to_dict()
        info["tool_stats"] = session.get_tool_stats()

        if agent is not None:
            info["active_tool_names"] = agent.get_active_tool_names()
            project = agent.get_active_project()
            if project is not None:
                info["project_languages"] = [lang.value for lang in project.project_config.languages]

        return info

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        Shutdown the server and all sessions.

        :param timeout: Timeout for shutdown operations.
        """
        log.info("Shutting down centralized server...")

        self._add_lifecycle_event("server_shutdown")

        # Shutdown all session agents
        with self._session_agents_lock:
            for session_id, agent in list(self._session_agents.items()):
                try:
                    log.debug(f"Shutting down agent for session {session_id}")
                    agent.shutdown(timeout=timeout / 2)
                except Exception as e:
                    log.warning(f"Error shutting down agent for session {session_id}: {e}")
            self._session_agents.clear()

        # Shutdown session manager
        self._session_manager.shutdown()

        # Shutdown template agent
        try:
            self._template_agent.shutdown(timeout=timeout / 2)
        except Exception as e:
            log.warning(f"Error shutting down template agent: {e}")

        log.info("Centralized server shutdown complete")

    def __repr__(self) -> str:
        stats = self.get_stats()
        return f"CentralizedSerenaServer(sessions={stats.active_session_count}, tools={len(self.get_exposed_tools())})"
