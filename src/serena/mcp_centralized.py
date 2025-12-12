"""
MCP Server for Centralized Serena Architecture.

Provides session-aware MCP interface for the centralized server.
Supports multiple concurrent client connections through sessions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings
from mcp.server.fastmcp.tools.base import Tool as MCPTool
from mcp.types import ToolAnnotations
from pydantic_settings import SettingsConfigDict
from sensai.util import logging

from serena.agent import SerenaConfig
from serena.central_server import CentralizedSerenaServer
from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
from serena.constants import DEFAULT_CONTEXT, DEFAULT_MODES
from serena.mcp import SerenaMCPFactory
from serena.session import Session
from serena.tools import Tool
from serena.util.exception import show_fatal_exception_safe
from serena.util.logging import MemoryLogHandler

log = logging.getLogger(__name__)


class SerenaMCPCentralized(SerenaMCPFactory):
    """
    MCP server factory for centralized multi-session architecture.

    This factory creates an MCP server that routes all tool calls
    through a CentralizedSerenaServer, supporting multiple concurrent
    client sessions.

    Key differences from SerenaMCPFactorySingleProcess:
    - Uses CentralizedSerenaServer instead of direct SerenaAgent
    - Creates sessions for each client connection
    - Supports session-aware tool execution
    - Can share resources across sessions
    """

    def __init__(
        self,
        context: str = DEFAULT_CONTEXT,
        modes: list[str] | None = None,
        memory_log_handler: MemoryLogHandler | None = None,
    ) -> None:
        """
        Initialize the centralized MCP factory.

        :param context: The context name for the server.
        :param modes: List of mode names for the default modes.
        :param memory_log_handler: Log handler for the dashboard.
        """
        # Initialize context attribute as parent class expects
        self.context = SerenaAgentContext.load(context)
        self.context_name = context
        self.project: str | None = None  # Parent class expects this
        self._modes = modes or list(DEFAULT_MODES)
        self._memory_log_handler = memory_log_handler
        self._server: CentralizedSerenaServer | None = None

        # Current session (set per-connection)
        # For SSE/HTTP transport, this would be per-request via headers
        self._current_session: Session | None = None

    @property
    def server(self) -> CentralizedSerenaServer | None:
        """Get the centralized server instance."""
        return self._server

    def _instantiate_agent(self, serena_config: SerenaConfig, modes: list[SerenaAgentMode]) -> None:
        """
        Initialize the centralized server (called by create_mcp_server).

        :param serena_config: The Serena configuration.
        :param modes: List of default modes.
        """
        self._server = CentralizedSerenaServer(
            context=self.context_name,
            modes=[m.name for m in modes],
            serena_config=serena_config,
            memory_log_handler=self._memory_log_handler,
        )

        # Create a default session for this MCP connection
        self._current_session = self._server.create_session(client_name="mcp-client")
        log.info(f"Created default session {self._current_session.session_id}")

    def _iter_tools(self) -> Iterator[Tool]:
        """Iterate over tools from the centralized server."""
        if self._server is None:
            return
        yield from self._server.get_exposed_tools()

    def _get_initial_instructions(self) -> str:
        """Get initial instructions from the server."""
        if self._server is None or self._current_session is None:
            return ""
        return self._server.get_system_prompt_for_session(self._current_session.session_id)

    def make_session_aware_mcp_tool(self, tool: Tool, openai_tool_compatible: bool = True) -> MCPTool:
        """
        Create an MCP tool that routes through the centralized server with session awareness.

        :param tool: The Serena Tool instance to convert.
        :param openai_tool_compatible: Whether to make schema OpenAI-compatible.
        :return: MCP tool that routes through the session.
        """
        import docstring_parser

        func_name = tool.get_name()
        func_doc = tool.get_apply_docstring() or ""
        func_arg_metadata = tool.get_apply_fn_metadata()
        is_async = False
        parameters = func_arg_metadata.arg_model.model_json_schema()

        if openai_tool_compatible:
            parameters = SerenaMCPFactory._sanitize_for_openai_tools(parameters)

        docstring = docstring_parser.parse(func_doc)

        # Build description
        overridden_description = None
        if self._server is not None:
            overridden_description = self._server.template_agent.get_tool_description_override(func_name)

        if overridden_description is not None:
            func_doc = overridden_description
        elif docstring.description:
            func_doc = docstring.description
        else:
            func_doc = ""
        func_doc = func_doc.strip().strip(".")
        if func_doc:
            func_doc += "."
        if docstring.returns and (docstring_returns_descr := docstring.returns.description):
            prefix = " " if func_doc else ""
            func_doc = f"{func_doc}{prefix}Returns {docstring_returns_descr.strip().strip('.')}."

        # Parse parameter descriptions
        docstring_params = {param.arg_name: param for param in docstring.params}
        parameters_properties: dict[str, dict[str, Any]] = parameters["properties"]
        for parameter, properties in parameters_properties.items():
            if (param_doc := docstring_params.get(parameter)) and param_doc.description:
                param_desc = f"{param_doc.description.strip().strip('.') + '.'}"
                properties["description"] = param_desc[0].upper() + param_desc[1:]

        # Create session-aware execution function
        def execute_fn(**kwargs: Any) -> str:
            if self._server is None:
                return "Error: Server not initialized"
            if self._current_session is None:
                return "Error: No session"
            return self._server.execute_tool(
                session_id=self._current_session.session_id,
                tool_name=func_name,
                **kwargs,
            )

        annotations = ToolAnnotations(readOnlyHint=not tool.can_edit())

        return MCPTool(
            fn=execute_fn,
            name=func_name,
            description=func_doc,
            parameters=parameters,
            fn_metadata=func_arg_metadata,
            is_async=is_async,
            context_kwarg=None,
            annotations=annotations,
            title=None,
        )

    def _set_mcp_tools_session_aware(self, mcp: FastMCP, openai_tool_compatible: bool = False) -> None:
        """
        Update the tools in the MCP server with session-aware versions.

        :param mcp: The FastMCP server instance.
        :param openai_tool_compatible: Whether to make schemas OpenAI-compatible.
        """
        if mcp is not None:
            # noinspection PyProtectedMember
            mcp._tool_manager._tools = {}
            for tool in self._iter_tools():
                mcp_tool = self.make_session_aware_mcp_tool(tool, openai_tool_compatible=openai_tool_compatible)
                # noinspection PyProtectedMember
                mcp._tool_manager._tools[tool.get_name()] = mcp_tool
            # noinspection PyProtectedMember
            log.info(f"Started centralized MCP server with {len(mcp._tool_manager._tools)} session-aware tools")

    @asynccontextmanager
    async def server_lifespan(self, mcp_server: FastMCP) -> AsyncIterator[None]:
        """Manage server lifecycle with session awareness."""
        context = SerenaAgentContext.load(self.context_name)
        openai_tool_compatible = context.name in ["chatgpt", "codex", "oaicompat-agent"]
        self._set_mcp_tools_session_aware(mcp_server, openai_tool_compatible=openai_tool_compatible)
        log.info("Centralized MCP server lifetime setup complete")
        yield

        # Cleanup on shutdown
        if self._server is not None:
            self._server.shutdown()

    def create_mcp_server(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        modes: Sequence[str] = DEFAULT_MODES,
        enable_web_dashboard: bool | None = None,
        enable_gui_log_window: bool | None = None,
        log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
        trace_lsp_communication: bool | None = None,
        tool_timeout: float | None = None,
    ) -> FastMCP:
        """
        Create an MCP server with centralized session management.

        :param host: The host to bind to.
        :param port: The port to bind to.
        :param modes: List of mode names.
        :param enable_web_dashboard: Whether to enable the web dashboard.
        :param enable_gui_log_window: Whether to enable the GUI log window.
        :param log_level: Log level.
        :param trace_lsp_communication: Whether to trace LSP communication.
        :param tool_timeout: Timeout in seconds for tool execution.
        :return: The FastMCP server instance.
        """
        try:
            config = SerenaConfig.from_config_file()

            # Update configuration with provided parameters
            if enable_web_dashboard is not None:
                config.web_dashboard = enable_web_dashboard
            if enable_gui_log_window is not None:
                config.gui_log_window_enabled = enable_gui_log_window
            if log_level is not None:
                log_level = cast(Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], log_level.upper())
                config.log_level = logging.getLevelNamesMapping()[log_level]
            if trace_lsp_communication is not None:
                config.trace_lsp_communication = trace_lsp_communication
            if tool_timeout is not None:
                config.tool_timeout = tool_timeout

            modes_instances = [SerenaAgentMode.load(mode) for mode in modes]
            self._instantiate_agent(config, modes_instances)

        except Exception as e:
            show_fatal_exception_safe(e)
            raise

        # Override model_config to disable .env files
        Settings.model_config = SettingsConfigDict(env_prefix="FASTMCP_")
        instructions = self._get_initial_instructions()
        mcp = FastMCP(lifespan=self.server_lifespan, host=host, port=port, instructions=instructions)
        return mcp

    # Session management methods for external API access

    def get_current_session(self) -> Session | None:
        """Get the current session for this MCP connection."""
        return self._current_session

    def create_new_session(self, client_name: str | None = None) -> Session | None:
        """
        Create a new session (for multi-session scenarios).

        :param client_name: Human-readable name for the client.
        :return: The new session, or None if server not initialized.
        """
        if self._server is None:
            return None
        return self._server.create_session(client_name=client_name)

    def switch_session(self, session_id: str) -> bool:
        """
        Switch to a different session.

        :param session_id: The session ID to switch to.
        :return: True if switched, False if session not found.
        """
        if self._server is None:
            return False
        session = self._server.get_session(session_id)
        if session is None:
            return False
        self._current_session = session
        return True
