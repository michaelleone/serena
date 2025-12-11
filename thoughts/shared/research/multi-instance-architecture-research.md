# Serena Multi-Instance Architecture Research Report

**Date**: 2025-12-11
**Purpose**: Provide a foundational analysis for overhauling Serena's MCP architecture to support multiple sessions in a single instance with a unified dashboard UI.

---

## Executive Summary

Serena currently operates on a **per-conversation, per-instance model** where each MCP client (Claude Desktop session, VS Code conversation, etc.) spawns an independent Serena process. This architecture creates three critical problems:

1. **Resource Proliferation**: Each conversation creates a new process with its own language servers, memory, and dashboard
2. **Zombie Accumulation**: Abruptly terminated conversations leave orphaned processes
3. **Management Fragmentation**: Users cannot view or manage all instances from a single location

An existing plan (`thoughts/shared/plans/001-2025-12-10-global-dashboard.md`) proposes a **global dashboard overlay** using a file-based instance registry. While this addresses visibility and zombie management, it does **not** consolidate the underlying multi-process architecture.

This research report provides a comprehensive analysis of the current architecture, critically examines the proposed solution, explores alternative architectures including true multi-session consolidation, and deeply analyzes UI/UX requirements for multi-instance management.

---

## Part 1: Current Architecture Analysis

### 1.1 Instance Creation Model

**Source files**: `src/serena/cli.py:104-190`, `src/serena/mcp.py:316-352`

The instance lifecycle follows this pattern:

```
MCP Client (Claude Desktop, VS Code, etc.)
    │
    ▼ [spawns process via stdio]
serena-mcp-server CLI entry point
    │
    ▼ [creates factory]
SerenaMCPFactorySingleProcess
    │
    ▼ [creates single agent per process]
SerenaAgent instance
    │
    ├─→ Project (one active at a time)
    │     └─→ LanguageServerManager
    │           └─→ SolidLanguageServer instances (per language)
    │
    ├─→ TaskExecutor (serializes all tool calls)
    │
    ├─→ SerenaDashboardAPI (Flask app)
    │     └─→ Port: 0x5EDA (24282) + auto-increment
    │
    └─→ All Tool instances (created once)
```

**Key observations**:
- **1:1 process mapping**: Each MCP client connection spawns a dedicated OS process
- **No process sharing**: Even identical project activations create separate instances
- **Independent state**: Each process has completely isolated memory, no cross-process communication
- **Dashboard sprawl**: Each instance runs its own web dashboard on a unique port

### 1.2 Transport Mechanisms

**Source file**: `src/serena/cli.py:139-172`

Serena supports three transports:
- **stdio** (default): One process per client, stdin/stdout communication
- **sse**: Server-Sent Events, theoretically shareable but creates single instance
- **streamable-http**: HTTP-based, also creates single instance per server

The **stdio transport** is the root cause of per-conversation processes, but it's dictated by MCP protocol requirements for Claude Desktop and similar clients.

### 1.3 Session Identity Model

**Critical finding**: Serena has **no explicit session concept**.

- Sessions are implicitly identified by **process ID (PID)**
- No session tokens, no client identifiers, no multiplexing capability
- `SerenaAgent.__init__` logs PID but doesn't track it beyond logging:

```python
# agent.py:226
log.info(f"Starting Serena server (version={serena_version()}, process id={os.getpid()}, parent process id={os.getppid()})")
```

This lack of session abstraction makes multi-tenancy impossible without architectural changes.

### 1.4 State Management

**State categories identified**:

| State Type | Scope | Location | Persistence |
|------------|-------|----------|-------------|
| SerenaAgent | Per-process | Memory | None |
| Active Project | Per-process | Memory + disk | `.serena/project.yml` |
| Language Servers | Per-project-per-process | Subprocesses | Cache in `.serena/cache/` |
| Tool Instances | Per-process | Memory | None |
| Tool Usage Stats | Per-process | Memory | None (cleared on restart) |
| Memories | Per-project | Disk | `.serena/memories/*.md` |
| Logs | Per-process | Memory + rotating file | `~/.serena/logs/` |

**Critical conflict**: If two conversations work on the same project, they each have their own:
- Language server instances (resource waste)
- Tool usage statistics (fragmented analytics)
- Dashboard (confusing UX)

### 1.5 Dashboard Architecture

**Source file**: `src/serena/dashboard.py`

The current dashboard is a Flask application with:
- **Technology**: Flask + jQuery + Chart.js (vanilla JS, no framework)
- **Communication**: HTTP polling (1-second intervals for most data)
- **Heartbeat**: 250ms `/heartbeat` endpoint
- **Instance scope**: Completely isolated, no awareness of other instances

**Dashboard data model** (`ResponseConfigOverview`):
- Active project name, path, languages
- Context and modes
- Active tools and usage statistics
- Registered projects, available tools/modes/contexts
- Project memories

**Key limitation**: The dashboard directly accesses `self._agent`, making it impossible to serve multiple agents from one dashboard without refactoring.

### 1.6 Tool Execution Model

**Source file**: `src/serena/task_executor.py`

Tool execution is **serialized** through a `TaskExecutor`:

```python
class TaskExecutor:
    def __init__(self, name: str):
        self._task_executor_lock = threading.Lock()
        self._task_executor_queue: list[TaskExecutor.Task] = []
        self._task_executor_thread = Thread(target=self._process_task_queue, daemon=True)
```

This serialization prevents race conditions but creates a bottleneck. In a multi-session architecture, we would need per-session queues or a shared queue with session affinity.

---

## Part 2: Problem Deep-Dive

### 2.1 Zombie Instance Problem

**Causes of zombie processes**:

1. **Abrupt client disconnection**: User closes IDE/app without graceful shutdown
2. **Client crash**: Claude Desktop or VS Code crashes
3. **Network timeout**: Connection drops, parent doesn't signal child
4. **Kill signals not propagated**: Container/docker termination

**Current cleanup mechanisms** (`src/serena/agent.py:680-693`):

```python
def shutdown(self, timeout: float = 2.0) -> None:
    if self._active_project is not None:
        self._active_project.shutdown(timeout=timeout)
        self._active_project = None
    if self._gui_log_viewer:
        self._gui_log_viewer.stop()
```

**Problem**: This method is only called during graceful shutdown. Abrupt termination bypasses it entirely.

**Language server cleanup** (`src/solidlsp/ls_handler.py:228-298`):
- Multi-stage shutdown: LSP shutdown → SIGTERM → wait → SIGKILL
- Uses `psutil` for process tree cleanup
- **Weakness**: Parent process death may not trigger cleanup

**Zombie accumulation symptoms**:
- Multiple `python` processes for same project
- Multiple language server processes (pyright, gopls, etc.)
- Port exhaustion (dashboards claiming ports 24282+)
- Memory bloat from unused caches

### 2.2 Resource Duplication Problem

For N concurrent conversations on the same project:

| Resource | Per-Instance | Total (N=3) |
|----------|--------------|-------------|
| SerenaAgent | 1 | 3 |
| Language Server Manager | 1 | 3 |
| Python LSP (pyright) | 1 | 3 |
| Other LSPs | varies | varies × 3 |
| TaskExecutor threads | 1 | 3 |
| Dashboard Flask threads | 1 | 3 |
| Memory log buffer | 10MB | 30MB |

Each language server can consume 200-500MB RAM. Three conversations on a Python/TypeScript project could easily consume 2GB+ of RAM.

### 2.3 User Experience Problems

1. **Dashboard Discovery**: Users don't know which port their dashboard is on
2. **Multiple Tabs**: Each conversation opens a new dashboard tab
3. **State Confusion**: Which instance is my current conversation connected to?
4. **No Cross-Instance Visibility**: Cannot see what other instances are doing
5. **Zombie Visibility**: No way to see or kill orphaned instances

### 2.4 Project Isolation Failure

**Scenario**: User has two Claude conversations about the same project.

- Each has its own `_active_project` instance
- Language server caches are duplicated
- Memory files are shared (disk) but edits might conflict
- Tool usage statistics are split across instances

This is neither proper isolation (wasted resources) nor proper sharing (no coordination).

---

## Part 3: Analysis of Existing Proposed Solution

### 3.1 Summary of Proposed Approach

The existing plan (`001-2025-12-10-global-dashboard.md`) proposes:

1. **Instance Registry**: File-based JSON registry (`~/.serena/instances.json`) with file locking
2. **Global Dashboard**: Separate Flask app that aggregates all instances
3. **Heartbeat Monitoring**: Global dashboard pings each instance's `/heartbeat`
4. **Zombie Management**: Mark unreachable instances as zombies, auto-prune after 5 minutes
5. **Force Kill**: Send SIGTERM/SIGKILL to zombie processes
6. **Lifecycle Events**: Log all instance state changes

### 3.2 Strengths of Proposed Approach

1. **Additive-only**: No changes to existing per-instance architecture
2. **Backward compatible**: Old configurations continue to work
3. **Visibility**: Users can finally see all running instances
4. **Zombie cleanup**: Automatic detection and removal
5. **Force kill capability**: Users can terminate stuck instances
6. **Simple implementation**: File-based, no database, no network coordination

### 3.3 Critical Analysis: Limitations

**Limitation 1: Does Not Address Resource Duplication**

The global dashboard is purely an **observation layer**. Three conversations still spawn three processes with three sets of language servers. No resources are shared.

**Limitation 2: Polling-Based Architecture**

```python
HEARTBEAT_CHECK_INTERVAL = 5  # seconds
HEARTBEAT_TIMEOUT = 2  # seconds
```

This means zombie detection takes 5+ seconds, and the global dashboard must maintain O(N) HTTP connections for N instances. At scale (10+ instances), this creates unnecessary overhead.

**Limitation 3: File Locking Contention**

```python
self._lock = FileLock(str(self._lock_path), timeout=10)
```

Every instance registration, heartbeat update, and project activation acquires a file lock. With many instances, this creates contention. File locking is also notoriously unreliable on network filesystems.

**Limitation 4: No Session Isolation**

The proposal doesn't introduce session IDs. All instances remain process-isolated but conceptually unlinked. A user can't say "show me the instance for my current conversation."

**Limitation 5: Race Condition in Port Selection**

```python
def _find_first_free_port(self, start_port: int) -> int:
    port = start_port
    for _ in range(max_attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            port += 1
```

Between `sock.close()` and actually starting Flask, another process could claim the port. This is a classic TOCTOU race.

**Limitation 6: Global Dashboard Becomes Single Point of Failure**

If the global dashboard process dies, no instance can check for zombies or update the registry (though instances continue working). The first-instance-wins model for global dashboard startup could also lead to issues if that instance dies.

### 3.4 Assessment

The proposed global dashboard is a **good first step** for visibility and zombie management, but it's a **band-aid** on the underlying architectural problem. It should be implemented as Phase 1, but a more fundamental refactor should follow.

---

## Part 4: Alternative Architectural Approaches

### 4.1 Approach A: True Multi-Session Server (Recommended for Phase 2)

**Concept**: Single long-running Serena server process that multiplexes multiple client sessions.

```
                          ┌──────────────────────────────────┐
                          │     Serena Multi-Session Server  │
                          │                                  │
┌─────────────┐           │  ┌─────────────────────────────┐ │
│ Claude      │ ◀─stdio─▶ │  │ Session Manager             │ │
│ Desktop #1  │           │  │  - session_1 → agent_1      │ │
└─────────────┘           │  │  - session_2 → agent_2      │ │
                          │  │  - session_3 → agent_3      │ │
┌─────────────┐           │  └─────────────────────────────┘ │
│ Claude      │ ◀─stdio─▶ │                                  │
│ Desktop #2  │           │  ┌─────────────────────────────┐ │
└─────────────┘           │  │ Shared Resources            │ │
                          │  │  - LanguageServerPool       │ │
┌─────────────┐           │  │  - ProjectCache             │ │
│ VS Code     │ ◀─stdio─▶ │  │  - DashboardAPI (unified)   │ │
│ Extension   │           │  └─────────────────────────────┘ │
└─────────────┘           └──────────────────────────────────┘
```

**Key components**:

1. **Session Manager**: Tracks active sessions with unique session IDs
2. **Agent Pool**: Lightweight per-session agents sharing resources
3. **Language Server Pool**: Shared language servers with reference counting
4. **Unified Dashboard**: Single dashboard serving all sessions

**Challenges**:
- MCP protocol assumes process-per-client for stdio transport
- Requires a launcher/proxy layer to multiplex stdio connections
- More complex error isolation (one session's crash could affect others)

**Implementation sketch**:

```python
class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.ls_pool = LanguageServerPool()

    def create_session(self, session_id: str, project: str) -> Session:
        session = Session(
            id=session_id,
            agent=LightweightAgent(project, self.ls_pool),
        )
        self.sessions[session_id] = session
        return session

    def get_or_create_ls(self, project: str, language: Language) -> SolidLanguageServer:
        """Returns a shared language server, creating if needed."""
        return self.ls_pool.get_or_create(project, language)
```

### 4.2 Approach B: Proxy-Based Consolidation

**Concept**: A coordinator process sits between MCP clients and Serena instances.

```
┌─────────────┐         ┌───────────────┐         ┌─────────────────┐
│ Claude      │ ◀─────▶ │               │ ◀─────▶ │ Serena Instance │
│ Desktop #1  │         │               │         │ (project A)     │
└─────────────┘         │               │         └─────────────────┘
                        │    Serena     │
┌─────────────┐         │   Coordinator │         ┌─────────────────┐
│ Claude      │ ◀─────▶ │               │ ◀─────▶ │ Serena Instance │
│ Desktop #2  │         │               │         │ (project A)     │
└─────────────┘         │               │         │   [SHARED]      │
                        └───────────────┘         └─────────────────┘
```

**Benefits**:
- Coordinator can route multiple clients to same instance for same project
- Instances remain isolated (crash isolation)
- Coordinator handles lifecycle management

**Challenges**:
- Adds latency (message forwarding)
- Coordinator is single point of failure
- Complex message routing logic

### 4.3 Approach C: Hybrid Approach (Recommended)

Combine the global dashboard (observation layer) with gradual resource consolidation:

**Phase 1**: Implement global dashboard (current proposal)
**Phase 2**: Add language server pooling (shared across instances)
**Phase 3**: Add session abstraction layer
**Phase 4**: Migrate to multi-session server

**Language Server Pooling** (Phase 2):

```python
class GlobalLanguageServerPool:
    """Shared pool of language servers accessible by all Serena instances."""

    SOCKET_PATH = "/tmp/serena-lsp-pool.sock"  # Unix socket for IPC

    def __init__(self):
        self._servers: dict[str, dict[str, SolidLanguageServer]] = {}  # project -> lang -> server
        self._refcounts: dict[tuple[str, str], int] = {}

    def acquire(self, project_root: str, language: str) -> LSPClient:
        """Get a client connection to the pooled language server."""
        key = (project_root, language)
        if key not in self._servers:
            self._servers[key] = self._start_server(project_root, language)
        self._refcounts[key] = self._refcounts.get(key, 0) + 1
        return LSPClient(self._servers[key])

    def release(self, project_root: str, language: str):
        """Release reference to language server, stopping if no refs."""
        key = (project_root, language)
        self._refcounts[key] -= 1
        if self._refcounts[key] <= 0:
            self._servers[key].shutdown()
            del self._servers[key]
```

---

## Part 5: Dashboard UI/UX Deep Analysis

### 5.1 Current Dashboard UI Model

The existing dashboard (`src/serena/resources/dashboard/`) is designed for **single-instance viewing**:

```
┌─────────────────────────────────────────────────────────────────┐
│ [Logo] Serena                          [Theme] [Menu ▼]         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────┐  ┌───────────────────────┐ │
│  │ Current Configuration           │  │ Registered Projects   │ │
│  │ • Project: my-app               │  │ • my-app [active]     │ │
│  │ • Languages: Python, TypeScript │  │ • other-project       │ │
│  │ • Context: desktop-app          │  │                       │ │
│  │ • Modes: interactive            │  └───────────────────────┘ │
│  └─────────────────────────────────┘                            │
│                                                                 │
│  ┌─────────────────────────────────┐  ┌───────────────────────┐ │
│  │ Tool Usage                      │  │ Available Tools       │ │
│  │ [find_symbol ████████ 15]       │  │ (collapsed)           │ │
│  │ [read_file   ██████ 12]         │  └───────────────────────┘ │
│  └─────────────────────────────────┘                            │
│                                                                 │
│  ┌─────────────────────────────────┐  ┌───────────────────────┐ │
│  │ Execution Queue                 │  │ Available Modes       │ │
│  │ • Running: FindSymbolTool       │  │ (collapsed)           │ │
│  │ • Queued: (none)                │  └───────────────────────┘ │
│  └─────────────────────────────────┘                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 Multi-Instance Dashboard Requirements

A unified dashboard must answer these user questions:

1. **"How many Serena instances are running?"** → Instance count + list
2. **"Which instance is my current conversation?"** → Session-to-instance mapping
3. **"What is each instance doing?"** → Per-instance status
4. **"Are any instances stuck or dead?"** → Health status + zombie detection
5. **"How do I kill a stuck instance?"** → Force termination capability
6. **"What happened over time?"** → Lifecycle event log

### 5.3 Proposed Multi-Instance UI Design

#### Primary Navigation Model: Tab-Based Instance Switching

```
┌─────────────────────────────────────────────────────────────────┐
│ [Logo] Serena Global Dashboard                [Theme] [Menu ▼]  │
├─────────────────────────────────────────────────────────────────┤
│ INSTANCES:                                                      │
│ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ │
│ │ ● 1234      │ │ ● 5678      │ │ ◉ 9012      │ │ ● 3456      │ │
│ │ my-app      │ │ serena      │ │ NO PROJECT  │ │ ZOMBIE      │ │
│ │ [ACTIVE]    │ │             │ │             │ │ [KILL]      │ │
│ └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ │
│                                                                 │
│ ═══════════════════════════════════════════════════════════════ │
│                                                                 │
│                    [Instance Detail View]                       │
│                                                                 │
│  Same layout as current single-instance dashboard               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Tab states** (color-coded):
- **Green dot + solid border**: Live instance with active project
- **Yellow dot + dashed border**: Live instance, no project activated
- **Red dot + pulsing border**: Zombie (unreachable)
- **Blue highlight**: Currently selected tab

#### Instance Overview Mode

For users who want to see all instances at a glance:

```
┌─────────────────────────────────────────────────────────────────┐
│ [Logo] Serena Global Dashboard            [Overview] [Menu ▼]   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────┐  ┌─────────────────────┐               │
│  │ PID: 1234           │  │ PID: 5678           │               │
│  │ Project: my-app     │  │ Project: serena     │               │
│  │ Status: ● ACTIVE    │  │ Status: ● IDLE      │               │
│  │ Port: 24282         │  │ Port: 24283         │               │
│  │ Tools called: 47    │  │ Tools called: 12    │               │
│  │ Started: 10:30 AM   │  │ Started: 11:45 AM   │               │
│  │ [Open Dashboard]    │  │ [Open Dashboard]    │               │
│  │ [Shutdown]          │  │ [Shutdown]          │               │
│  └─────────────────────┘  └─────────────────────┘               │
│                                                                 │
│  ┌─────────────────────┐  ┌─────────────────────┐               │
│  │ PID: 9012           │  │ PID: 3456           │               │
│  │ Project: (none)     │  │ Project: old-proj   │               │
│  │ Status: ◉ NO PROJECT│  │ Status: ☠ ZOMBIE    │               │
│  │ Port: 24284         │  │ Last seen: 10 min   │               │
│  │ Tools called: 0     │  │ [Force Kill]        │               │
│  │ Started: 12:00 PM   │  │                     │               │
│  │ [Open Dashboard]    │  │                     │               │
│  │ [Shutdown]          │  │                     │               │
│  └─────────────────────┘  └─────────────────────┘               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Lifecycle Event Timeline

Critical for debugging and understanding instance history:

```
┌─────────────────────────────────────────────────────────────────┐
│ LIFECYCLE EVENTS                                    [Refresh]   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  12:05:32  ▶ Instance 1234 started (context: desktop-app)       │
│  12:05:35  📁 Instance 1234 activated project: my-app           │
│  12:06:01  ▶ Instance 5678 started (context: agent)             │
│  12:06:15  📁 Instance 5678 activated project: serena           │
│  12:10:45  ☠ Instance 3456 marked as ZOMBIE (no heartbeat)      │
│  12:15:45  🗑 Instance 3456 auto-pruned after 5 minutes          │
│  12:20:00  ⚡ Instance 7890 force-killed by user                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 5.4 Interaction Patterns

#### Quick Actions

Each instance should have quick action buttons:
- **Open Full Dashboard**: Opens the per-instance dashboard in new tab
- **Shutdown**: Graceful shutdown via `/shutdown` API
- **Force Kill**: For zombies, sends SIGTERM/SIGKILL

#### Filtering and Sorting

With many instances, users need:
- **Filter by state**: Show only active / only zombies
- **Filter by project**: Show instances for specific project
- **Sort by**: Started time / Last activity / Project name

#### Real-Time Updates

The dashboard should feel alive:
- WebSocket-based updates (better than polling)
- Animated transitions for state changes
- Toast notifications for significant events

### 5.5 Information Hierarchy

**Level 1 - Global Status** (always visible):
- Total instances count
- Zombies count (with warning badge)
- Aggregate resource usage

**Level 2 - Instance Summary** (in tab bar):
- PID
- Project name (or "NO PROJECT")
- State indicator (dot color)

**Level 3 - Instance Detail** (in main content):
- Full configuration
- Tool usage statistics
- Execution queue
- Logs (filtered to this instance)

### 5.6 Responsive Design Considerations

The global dashboard must work on:
- **Desktop**: Full multi-column layout
- **Tablet**: Collapsed sidebar, tabs scroll horizontally
- **Mobile**: Stack instances vertically, detail view slides in

### 5.7 Accessibility

- Keyboard navigation between tabs (Arrow keys)
- Screen reader announcements for state changes
- Color-blind friendly state indicators (add icons, not just colors)
- High contrast mode support

---

## Part 6: Critical Trade-offs

### 6.1 Complexity vs. Functionality

| Approach | Complexity | Zombie Mgmt | Resource Sharing | Session Isolation |
|----------|------------|-------------|------------------|-------------------|
| Current (nothing) | Low | None | None | Full (process) |
| Global Dashboard | Medium | Yes | None | Full (process) |
| Language Server Pool | Medium-High | Yes | Partial (LSPs) | Partial |
| Multi-Session Server | High | Yes | Full | Logical only |

**Recommendation**: Start with Global Dashboard, plan for gradual resource consolidation.

### 6.2 Stability vs. Efficiency

**Process isolation benefits**:
- One session crash doesn't affect others
- Memory leaks are contained
- Simple debugging (one process = one session)

**Process sharing benefits**:
- Dramatically lower memory usage
- Faster project activation (reuse warm caches)
- Single dashboard, single source of truth

**Trade-off**: The current architecture prioritizes stability. A multi-session server would prioritize efficiency at the cost of more complex error handling.

### 6.3 MCP Protocol Constraints

The MCP protocol using stdio transport **requires** a process per client. Alternatives:
- Use HTTP/SSE transport (not universally supported)
- Implement a launcher that forks after shared initialization
- Accept per-process limitation, share resources via IPC

### 6.4 File Locking Reliability

File locking (`filelock` library) works well on local filesystems but is problematic:
- On network filesystems (NFS, CIFS)
- On some container runtimes
- When processes die without releasing locks

**Mitigation**: Use timeout-based lock expiry and stale lock detection.

---

## Part 7: Recommendations

### Immediate (Phase 1): Implement Global Dashboard

Follow the existing plan with these enhancements:

1. **Use WebSocket instead of polling** for real-time updates
2. **Add explicit session IDs** even if not enforced (future-proofing)
3. **Improve port selection** with actual binding before advertising
4. **Add per-instance "Open in New Tab"** buttons for full dashboard access

### Short-term (Phase 2): Language Server Pooling

1. **Implement a global LSP coordinator** as a separate daemon
2. **Use Unix sockets** for low-latency IPC
3. **Reference-count** language servers per project
4. **Gracefully handle coordinator restarts**

### Medium-term (Phase 3): Session Abstraction

1. **Introduce explicit SessionID** in SerenaAgent
2. **Propagate SessionID** in MCP tool calls (if protocol allows)
3. **Map client connections to sessions** for debugging
4. **Allow session introspection** from dashboard

### Long-term (Phase 4): Multi-Session Architecture

1. **Design launcher/proxy layer** for stdio multiplexing
2. **Implement lightweight session agents** sharing resources
3. **Single unified dashboard** as the only UI
4. **Full resource consolidation** (one language server per project, period)

---

## Part 8: Appendix

### A. Key File References

| Component | File | Key Lines |
|-----------|------|-----------|
| CLI Entry | `src/serena/cli.py` | 104-190 |
| MCP Factory | `src/serena/mcp.py` | 316-352 |
| SerenaAgent | `src/serena/agent.py` | 154-286 |
| Dashboard API | `src/serena/dashboard.py` | 119-580 |
| Dashboard UI | `src/serena/resources/dashboard/` | - |
| Task Executor | `src/serena/task_executor.py` | 18-195 |
| LS Handler | `src/solidlsp/ls_handler.py` | 228-298 |
| Existing Plan | `thoughts/shared/plans/001-2025-12-10-global-dashboard.md` | - |

### B. Glossary

- **MCP**: Model Context Protocol - the communication protocol between AI clients and tool servers
- **LSP**: Language Server Protocol - the protocol for language intelligence services
- **stdio transport**: Process communication via stdin/stdout (default for Claude Desktop)
- **Zombie instance**: A Serena process that is no longer reachable but still consuming resources
- **Session**: A logical unit of conversation (currently 1:1 with process)

### C. Open Questions for Future Investigation

1. Can MCP protocol be extended to support session multiplexing?
2. What's the maximum practical number of concurrent instances?
3. How do other MCP servers handle multi-instance scenarios?
4. Should language server caching be centralized (single cache dir) or per-project?
5. What metrics should be collected for instance health monitoring?

---

## Conclusion

Serena's current per-instance architecture was appropriate for initial development but creates scaling and UX problems as usage grows. The proposed global dashboard is a valuable first step that provides visibility and zombie management without disrupting existing functionality.

However, for true resource efficiency, a phased migration toward shared resources (starting with language server pooling) and eventually a multi-session architecture is recommended. The key is to maintain the stability benefits of process isolation while gradually enabling resource sharing for commonly-used components like language servers.

The dashboard UI must evolve from single-instance focus to multi-instance orchestration, with clear visual hierarchy, quick actions, and real-time updates to give users confidence in managing their Serena ecosystem.
