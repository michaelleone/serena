# Centralized Serena Server Implementation Plan

## Overview

Transform Serena from a 1:1 client-server model into a centralized server architecture where a single persistent Serena server handles multiple MCP client connections through sessions.

## Current State Analysis

### Existing Architecture
- Each MCP client spawns a new Serena process (stdio transport = 1:1 per client)
- `SerenaAgent` is single-tenant - one agent per process
- `SerenaDashboardAPI` runs per-instance on ports starting at 24282
- No session management or multi-client coordination
- Transport options: stdio (default), sse, streamable-http

### Key Components
- `SerenaMCPFactorySingleProcess` in [mcp.py](src/serena/mcp.py) - creates MCP server with FastMCP
- `SerenaAgent` in [agent.py](src/serena/agent.py) - orchestrates tools, projects, language servers
- `SerenaDashboardAPI` in [dashboard.py](src/serena/dashboard.py) - Flask-based dashboard
- `cli.py` - entry point via `serena-mcp-server` command

## Desired End State

After implementation:
1. A single centralized Serena server runs persistently (started once, runs continuously)
2. Multiple MCP clients connect to this server via an MCP proxy mechanism
3. Each client connection creates a "session" with its own state (active project, modes, etc.)
4. A global dashboard shows all connected sessions with status, activity, and controls
5. Language server resources can be shared across sessions working on the same project
6. Clients can use stdio transport locally while the central server uses SSE/HTTP

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     CENTRALIZED SERENA SERVER                           │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │                    SessionManager                               │    │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐           │    │
│  │  │  Session A   │ │  Session B   │ │  Session C   │   ...     │    │
│  │  │  (claude)    │ │  (cursor)    │ │  (cline)     │           │    │
│  │  │  project: X  │ │  project: Y  │ │  project: X  │           │    │
│  │  └──────────────┘ └──────────────┘ └──────────────┘           │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │              SerenaAgent (shared core)                          │    │
│  │  - Tool Registry (shared)                                       │    │
│  │  - Language Server Pool (shared per project)                    │    │
│  │  - Configuration (global + per-session overrides)               │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │              Global Dashboard (Flask)                           │    │
│  │  - All sessions view with status colors                         │    │
│  │  - Per-session tool stats and logs                              │    │
│  │  - Session management (view, terminate)                         │    │
│  │  - Lifecycle event log                                          │    │
│  └────────────────────────────────────────────────────────────────┘    │
│                              │                                          │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │              MCP Server (SSE/HTTP transport)                    │    │
│  │  - Accepts connections from MCP proxies                         │    │
│  │  - Routes tool calls to appropriate session                     │    │
│  │  - Session authentication/identification                        │    │
│  └────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                              ▲
                              │ HTTP/SSE
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│ MCP Proxy A   │    │ MCP Proxy B   │    │ MCP Proxy C   │
│ (stdio)       │    │ (stdio)       │    │ (stdio)       │
└───────────────┘    └───────────────┘    └───────────────┘
        ▲                     ▲                     ▲
        │ stdio               │ stdio               │ stdio
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│ Claude Desktop│    │    Cursor     │    │    Cline      │
└───────────────┘    └───────────────┘    └───────────────┘
```

### Verification Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] `uv run poe test` passes
- [ ] Manual test: Start central server, connect 3 clients, global dashboard shows all 3
- [ ] Manual test: Each client can activate different projects independently
- [ ] Manual test: Tool calls from one client don't affect another client's session
- [ ] Manual test: Language server is shared when two sessions use same project
- [ ] Manual test: Session disconnect is detected and shown in dashboard

## What We're NOT Doing (First Iteration)

- NOT implementing complex authentication (use simple session tokens for now)
- NOT implementing distributed/clustered servers
- NOT modifying existing tool implementations
- NOT removing the existing single-process mode (keep it as fallback)

---

## Phase 1: Session Management Core

### Overview
Create the session management layer that tracks multiple client sessions with their state.

### Changes Required

#### 1. Create Session Module
**File**: `src/serena/session.py` (NEW)

```python
"""
Session management for multi-client Serena server.

Each session represents a connected MCP client with its own state:
- Active project
- Active modes
- Tool usage statistics
- Log buffer
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
    ACTIVE = "active"  # Has an active project
    IDLE = "idle"  # Connected but no recent activity
    DISCONNECTED = "disconnected"


@dataclass
class SessionInfo:
    """Information about a client session."""
    session_id: str
    client_name: str | None  # e.g., "claude-desktop", "cursor", "cline"
    created_at: float
    last_activity: float
    state: SessionState = SessionState.CONNECTED
    active_project_name: str | None = None
    active_project_root: str | None = None
    active_modes: list[str] = field(default_factory=list)
    tool_call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
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


class Session:
    """
    Represents a single client session with its own state.

    Each session has:
    - Its own active project (can be different from other sessions)
    - Its own active modes
    - Its own tool usage statistics
    - Its own log buffer for the dashboard
    """

    IDLE_TIMEOUT_SECONDS = 300  # 5 minutes without activity = idle

    def __init__(
        self,
        session_id: str | None = None,
        client_name: str | None = None,
    ) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.client_name = client_name
        self.created_at = time.time()
        self.last_activity = self.created_at
        self._state = SessionState.CONNECTED

        # Session-specific state
        self._active_project: Project | None = None
        self._active_modes: list[SerenaAgentMode] = []
        self._tool_call_count = 0

        # Thread safety
        self._lock = threading.RLock()

    @property
    def state(self) -> SessionState:
        with self._lock:
            # Auto-update to idle if no recent activity
            if self._state in (SessionState.CONNECTED, SessionState.ACTIVE):
                if time.time() - self.last_activity > self.IDLE_TIMEOUT_SECONDS:
                    return SessionState.IDLE
            return self._state

    @state.setter
    def state(self, value: SessionState) -> None:
        with self._lock:
            self._state = value

    def touch(self) -> None:
        """Update last activity timestamp."""
        with self._lock:
            self.last_activity = time.time()

    def get_active_project(self) -> Project | None:
        with self._lock:
            return self._active_project

    def set_active_project(self, project: Project | None) -> None:
        with self._lock:
            self._active_project = project
            if project:
                self._state = SessionState.ACTIVE
            else:
                self._state = SessionState.CONNECTED
            self.touch()

    def get_active_modes(self) -> list[SerenaAgentMode]:
        with self._lock:
            return list(self._active_modes)

    def set_active_modes(self, modes: list[SerenaAgentMode]) -> None:
        with self._lock:
            self._active_modes = list(modes)
            self.touch()

    def increment_tool_calls(self) -> None:
        with self._lock:
            self._tool_call_count += 1
            self.touch()

    def get_info(self) -> SessionInfo:
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
        with self._lock:
            self._state = SessionState.DISCONNECTED


class SessionManager:
    """
    Manages all active client sessions.

    Thread-safe manager for creating, retrieving, and cleaning up sessions.
    """

    CLEANUP_INTERVAL_SECONDS = 60
    DISCONNECTED_RETENTION_SECONDS = 3600  # Keep disconnected sessions for 1 hour

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self._cleanup_thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()

    def create_session(self, client_name: str | None = None) -> Session:
        """Create a new session and return it."""
        session = Session(client_name=client_name)
        with self._lock:
            self._sessions[session.session_id] = session
        log.info(f"Created session {session.session_id} for client {client_name}")
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        """Get all sessions."""
        with self._lock:
            return list(self._sessions.values())

    def list_session_infos(self) -> list[SessionInfo]:
        """Get info for all sessions."""
        with self._lock:
            return [s.get_info() for s in self._sessions.values()]

    def remove_session(self, session_id: str) -> None:
        """Remove a session."""
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.disconnect()
                del self._sessions[session_id]
                log.info(f"Removed session {session_id}")

    def disconnect_session(self, session_id: str) -> None:
        """Mark a session as disconnected (but don't remove it yet)."""
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].disconnect()
                log.info(f"Disconnected session {session_id}")

    def _cleanup_old_sessions(self) -> None:
        """Remove sessions that have been disconnected for too long."""
        now = time.time()
        with self._lock:
            to_remove = []
            for session_id, session in self._sessions.items():
                if session.state == SessionState.DISCONNECTED:
                    if now - session.last_activity > self.DISCONNECTED_RETENTION_SECONDS:
                        to_remove.append(session_id)

            for session_id in to_remove:
                del self._sessions[session_id]
                log.info(f"Cleaned up old disconnected session {session_id}")

    def start_cleanup_thread(self) -> None:
        """Start background cleanup thread."""
        def cleanup_loop() -> None:
            while not self._shutdown_event.is_set():
                self._cleanup_old_sessions()
                self._shutdown_event.wait(self.CLEANUP_INTERVAL_SECONDS)

        self._cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def shutdown(self) -> None:
        """Shutdown the session manager."""
        self._shutdown_event.set()
        with self._lock:
            for session in self._sessions.values():
                session.disconnect()
```

### Success Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] Unit tests for Session and SessionManager pass

---

## Phase 2: Centralized Server Core

### Overview
Create the centralized server that manages sessions and routes tool calls.

### Changes Required

#### 1. Create Centralized Server Module
**File**: `src/serena/central_server.py` (NEW)

This module will contain:
- `CentralizedSerenaServer` - Main server class
- Session-aware tool execution
- Language server pool management
- Global dashboard integration

Key responsibilities:
1. Manage the SessionManager
2. Route tool calls to the correct session context
3. Share language servers across sessions using the same project
4. Provide APIs for the global dashboard

```python
"""
Centralized Serena Server for multi-client architecture.

Manages multiple client sessions, routes tool calls, and shares resources.
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

from sensai.util import logging

from serena.agent import SerenaAgent, SerenaConfig
from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
from serena.constants import DEFAULT_CONTEXT, DEFAULT_MODES
from serena.session import Session, SessionManager
from serena.tools import Tool
from serena.util.logging import MemoryLogHandler

if TYPE_CHECKING:
    from serena.project import Project

log = logging.getLogger(__name__)


class CentralizedSerenaServer:
    """
    Centralized server that manages multiple client sessions.

    This server:
    - Maintains a shared SerenaAgent with tool registry
    - Manages sessions for each connected client
    - Routes tool calls to session-specific context
    - Shares language servers across sessions using the same project
    """

    def __init__(
        self,
        context: str = DEFAULT_CONTEXT,
        modes: list[str] | None = None,
        serena_config: SerenaConfig | None = None,
        memory_log_handler: MemoryLogHandler | None = None,
    ) -> None:
        self._context = SerenaAgentContext.load(context)
        self._default_modes = [
            SerenaAgentMode.load(m) for m in (modes or list(DEFAULT_MODES))
        ]
        self._serena_config = serena_config or SerenaConfig.from_config_file()
        self._memory_log_handler = memory_log_handler

        # Session management
        self._session_manager = SessionManager()
        self._session_manager.start_cleanup_thread()

        # Create a "template" agent for tool registry and shared config
        # Individual sessions will use this for tool definitions but maintain their own state
        self._template_agent = SerenaAgent(
            project=None,  # No default project
            serena_config=self._serena_config,
            context=self._context,
            modes=self._default_modes,
            memory_log_handler=self._memory_log_handler,
        )

        # Language server pool: project_root -> language server instance
        self._ls_pool: dict[str, Any] = {}
        self._ls_pool_lock = threading.RLock()

        log.info("Centralized Serena Server initialized")

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def template_agent(self) -> SerenaAgent:
        return self._template_agent

    def create_session(self, client_name: str | None = None) -> Session:
        """Create a new client session."""
        session = self._session_manager.create_session(client_name=client_name)
        # Initialize session with default modes
        session.set_active_modes(list(self._default_modes))
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        return self._session_manager.get_session(session_id)

    def execute_tool(
        self,
        session_id: str,
        tool_name: str,
        **kwargs: Any,
    ) -> str:
        """
        Execute a tool in the context of a specific session.

        This routes the tool call to the session's active project and modes.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            return f"Error: Unknown session {session_id}"

        session.increment_tool_calls()

        # Get the tool from the template agent
        tool = self._template_agent.get_tool_by_name(tool_name)
        if tool is None:
            return f"Error: Unknown tool {tool_name}"

        # Execute in session context
        # For now, we use the template agent but with session's project
        # TODO: More sophisticated session-aware execution
        try:
            return tool.apply_ex(log_call=True, catch_exceptions=True, **kwargs)
        except Exception as e:
            log.exception(f"Error executing tool {tool_name} for session {session_id}")
            return f"Error: {e}"

    def get_exposed_tools(self) -> list[Tool]:
        """Get the list of exposed tools from the template agent."""
        return list(self._template_agent.get_exposed_tool_instances())

    def activate_project_for_session(
        self,
        session_id: str,
        project: Project,
    ) -> None:
        """Activate a project for a specific session."""
        session = self._session_manager.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session {session_id}")

        session.set_active_project(project)
        log.info(f"Activated project {project.project_name} for session {session_id}")

    def shutdown(self) -> None:
        """Shutdown the server and all sessions."""
        log.info("Shutting down centralized server")
        self._session_manager.shutdown()
        self._template_agent.shutdown()
```

### Success Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] CentralizedSerenaServer can be instantiated

---

## Phase 3: MCP Server Adapter

### Overview
Create an MCP server that routes requests through the centralized server with session awareness.

### Changes Required

#### 1. Create Session-Aware MCP Factory
**File**: `src/serena/mcp_centralized.py` (NEW)

This will be a new MCP factory that:
- Creates sessions for connecting clients
- Routes tool calls through CentralizedSerenaServer
- Supports SSE/HTTP transport for remote connections

```python
"""
MCP Server for Centralized Serena Architecture.

Provides session-aware MCP interface for the centralized server.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic_settings import SettingsConfigDict
from sensai.util import logging

from serena.central_server import CentralizedSerenaServer
from serena.config.context_mode import SerenaAgentMode
from serena.constants import DEFAULT_CONTEXT, DEFAULT_MODES
from serena.mcp import SerenaMCPFactory
from serena.tools import Tool
from serena.util.logging import MemoryLogHandler

log = logging.getLogger(__name__)


class SerenaMCPCentralized(SerenaMCPFactory):
    """
    MCP server factory for centralized multi-session architecture.

    This factory creates an MCP server that routes all tool calls
    through a CentralizedSerenaServer, supporting multiple concurrent
    client sessions.
    """

    def __init__(
        self,
        context: str = DEFAULT_CONTEXT,
        modes: list[str] | None = None,
        memory_log_handler: MemoryLogHandler | None = None,
    ) -> None:
        # Don't call super().__init__ - we handle context differently
        self.context_name = context
        self._modes = modes or list(DEFAULT_MODES)
        self._memory_log_handler = memory_log_handler
        self._server: CentralizedSerenaServer | None = None

        # Current session ID (set per-connection)
        # In a full implementation, this would be per-request
        self._current_session_id: str | None = None

    def _instantiate_agent(self, serena_config: Any, modes: list[SerenaAgentMode]) -> None:
        """Initialize the centralized server."""
        self._server = CentralizedSerenaServer(
            context=self.context_name,
            modes=[m.name for m in modes],
            serena_config=serena_config,
            memory_log_handler=self._memory_log_handler,
        )

        # Create a default session for this connection
        session = self._server.create_session(client_name="mcp-client")
        self._current_session_id = session.session_id

    def _iter_tools(self) -> Iterator[Tool]:
        """Iterate over tools from the centralized server."""
        if self._server is None:
            return
        yield from self._server.get_exposed_tools()

    def _get_initial_instructions(self) -> str:
        """Get initial instructions from the template agent."""
        if self._server is None:
            return ""
        return self._server.template_agent.create_system_prompt()

    @asynccontextmanager
    async def server_lifespan(self, mcp_server: FastMCP) -> AsyncIterator[None]:
        """Manage server lifecycle."""
        from serena.config.context_mode import SerenaAgentContext

        context = SerenaAgentContext.load(self.context_name)
        openai_tool_compatible = context.name in ["chatgpt", "codex", "oaicompat-agent"]
        self._set_mcp_tools(mcp_server, openai_tool_compatible=openai_tool_compatible)
        log.info("Centralized MCP server lifetime setup complete")
        yield

        # Cleanup on shutdown
        if self._server is not None:
            self._server.shutdown()
```

### Success Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] MCP server can be created with centralized backend

---

## Phase 4: MCP Proxy Client

### Overview
Create a lightweight MCP proxy that allows stdio-based clients to connect to the central server.

### Changes Required

#### 1. Create MCP Proxy Module
**File**: `src/serena/mcp_proxy.py` (NEW)

This proxy:
- Accepts stdio connections from local MCP clients (Claude Desktop, etc.)
- Forwards requests to the central server via HTTP/SSE
- Maintains session state

```python
"""
MCP Proxy for connecting stdio clients to a centralized Serena server.

This proxy allows local MCP clients (like Claude Desktop) that only support
stdio transport to connect to a remote centralized Serena server.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import requests
from sensai.util import logging

log = logging.getLogger(__name__)


class SerenaMCPProxy:
    """
    Lightweight MCP proxy for stdio-to-HTTP bridging.

    Reads MCP messages from stdin, forwards to central server,
    and writes responses to stdout.
    """

    def __init__(
        self,
        server_url: str,
        session_id: str | None = None,
        client_name: str | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._session_id = session_id
        self._client_name = client_name or "mcp-proxy"

        # Create session on first connect
        if self._session_id is None:
            self._create_session()

    def _create_session(self) -> None:
        """Create a new session on the central server."""
        response = requests.post(
            f"{self._server_url}/api/sessions",
            json={"client_name": self._client_name},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        self._session_id = data["session_id"]
        log.info(f"Created session {self._session_id}")

    def _forward_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Forward an MCP request to the central server."""
        response = requests.post(
            f"{self._server_url}/api/mcp",
            json={
                "session_id": self._session_id,
                "request": request,
            },
            timeout=300,  # Tool execution can take time
        )
        response.raise_for_status()
        return response.json()

    def run(self) -> None:
        """Run the proxy, reading from stdin and writing to stdout."""
        log.info(f"MCP Proxy started, connected to {self._server_url}")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
                response = self._forward_request(request)
                print(json.dumps(response), flush=True)
            except json.JSONDecodeError as e:
                log.error(f"Invalid JSON: {e}")
            except requests.RequestException as e:
                log.error(f"Request failed: {e}")
                # Send error response
                error_response = {
                    "error": {
                        "code": -32603,
                        "message": str(e),
                    }
                }
                print(json.dumps(error_response), flush=True)
```

#### 2. Add CLI Command for Proxy
**File**: `src/serena/cli.py` (modify)

Add a new command `serena-mcp-proxy` that starts the proxy.

### Success Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] Proxy can connect to central server and forward requests

---

## Phase 5: Global Dashboard

### Overview
Create a global dashboard that shows all connected sessions.

### Changes Required

#### 1. Create Global Dashboard Backend
**File**: `src/serena/global_dashboard.py` (NEW)

Flask-based dashboard with:
- Session list with status colors (connected=green, active=blue, idle=yellow, disconnected=red)
- Per-session details (project, modes, tool stats)
- Lifecycle event log
- Session management (view, terminate)

#### 2. Create Global Dashboard Frontend
**File**: `src/serena/resources/global_dashboard/` (NEW directory)
- `index.html` - Main dashboard page
- `global_dashboard.css` - Styles
- `global_dashboard.js` - JavaScript logic

The dashboard will show:
- Tab bar with all sessions (color-coded by state)
- Selected session details panel
- Global lifecycle event log
- Server statistics

### Success Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] Dashboard shows all connected sessions
- [ ] Sessions are color-coded by state

---

## Phase 6: CLI and Configuration

### Overview
Add CLI commands and configuration options for the centralized server.

### Changes Required

#### 1. Add CLI Commands
**File**: `src/serena/cli.py` (modify)

New commands:
- `serena-server` - Start the centralized server
- `serena-mcp-proxy` - Start the proxy to connect to a server

#### 2. Add Configuration Options
**File**: `src/serena/config/serena_config.py` (modify)

New options:
- `central_server_enabled: bool` - Whether to run in centralized mode
- `central_server_host: str` - Host for the central server
- `central_server_port: int` - Port for the central server
- `central_server_url: str | None` - URL to connect proxy to

#### 3. Update Config Template
**File**: `src/serena/resources/serena_config.template.yml` (modify)

Add documentation for new options.

### Success Criteria
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes
- [ ] CLI commands work as expected

---

## Phase 7: Integration and Testing

### Overview
Integration testing and documentation.

### Changes Required

#### 1. Add Unit Tests
**File**: `test/serena/test_session.py` (NEW)
**File**: `test/serena/test_central_server.py` (NEW)

#### 2. Add Integration Tests
**File**: `test/serena/test_centralized_integration.py` (NEW)

Test scenarios:
- Multiple sessions with different projects
- Tool execution isolation between sessions
- Session lifecycle (connect, activate project, disconnect)
- Dashboard API endpoints

### Success Criteria
- [ ] All tests pass
- [ ] `uv run poe format` passes
- [ ] `uv run poe type-check` passes

---

## Implementation Order

1. **Phase 1**: Session Management Core - Foundation for multi-client support
2. **Phase 2**: Centralized Server Core - Main server logic
3. **Phase 3**: MCP Server Adapter - Expose via MCP protocol
4. **Phase 4**: MCP Proxy Client - Allow stdio clients to connect
5. **Phase 5**: Global Dashboard - Visibility into all sessions
6. **Phase 6**: CLI and Configuration - User-facing interface
7. **Phase 7**: Integration and Testing - Ensure quality

## Dependencies

Add to `pyproject.toml`:
```toml
# No new dependencies required - uses existing requests, flask, mcp
```

## Migration Notes

- Existing single-process mode (`serena-mcp-server`) continues to work unchanged
- New centralized mode is opt-in via `serena-server` command
- Configuration is backward compatible

## References

- Current MCP implementation: [src/serena/mcp.py](src/serena/mcp.py)
- SerenaAgent: [src/serena/agent.py](src/serena/agent.py)
- Dashboard: [src/serena/dashboard.py](src/serena/dashboard.py)
- CLI: [src/serena/cli.py](src/serena/cli.py)
