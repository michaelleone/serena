/**
 * Global Dashboard for Serena Central Server
 *
 * Provides unified management of all connected client sessions.
 */

class GlobalDashboard {
    constructor() {
        this.currentPage = 'sessions';
        this.selectedSessionId = null;
        this.sessions = [];
        this.pollInterval = null;
        this.statsPollInterval = null;

        // Initialize
        this.initializeTheme();
        this.setupEventHandlers();
        this.loadStats();
        this.loadSessions();
        this.startPolling();
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

        // Refresh buttons
        $('#refresh-sessions').click(() => this.loadSessions());
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

        // Load page-specific data
        switch (page) {
            case 'sessions':
                this.loadSessions();
                break;
            case 'lifecycle':
                this.loadLifecycleEvents();
                break;
            case 'tools':
                this.loadTools();
                break;
            case 'config':
                this.loadConfig();
                break;
        }
    }

    // ===== Stats =====

    loadStats() {
        const self = this;

        $.ajax({
            url: '/api/stats',
            type: 'GET',
            success: function(response) {
                self.renderStats(response);
            },
            error: function(xhr, status, error) {
                console.error('Error loading stats:', error);
            }
        });
    }

    renderStats(stats) {
        // Format uptime
        const uptime = stats.uptime_seconds || 0;
        const hours = Math.floor(uptime / 3600);
        const minutes = Math.floor((uptime % 3600) / 60);
        const seconds = Math.floor(uptime % 60);

        let uptimeStr = '';
        if (hours > 0) uptimeStr += hours + 'h ';
        if (minutes > 0 || hours > 0) uptimeStr += minutes + 'm ';
        uptimeStr += seconds + 's';

        $('#stat-uptime').text(uptimeStr);
        $('#stat-sessions').text(stats.active_session_count || 0);
        $('#stat-tools').text(stats.total_tool_calls || 0);
    }

    startPolling() {
        // Poll stats every 5 seconds
        this.statsPollInterval = setInterval(() => this.loadStats(), 5000);

        // Poll sessions every 3 seconds when on sessions page
        this.pollInterval = setInterval(() => {
            if (this.currentPage === 'sessions') {
                this.loadSessions();
            }
        }, 3000);
    }

    // ===== Sessions =====

    loadSessions() {
        const self = this;

        $.ajax({
            url: '/api/sessions',
            type: 'GET',
            success: function(response) {
                self.sessions = response.sessions || [];
                self.renderSessionTabs();

                // Load selected session details
                if (self.selectedSessionId) {
                    self.loadSessionDetails(self.selectedSessionId);
                } else if (self.sessions.length > 0) {
                    self.selectSession(self.sessions[0].session_id);
                }
            },
            error: function(xhr, status, error) {
                console.error('Error loading sessions:', error);
                $('#session-tabs').html('<div class="error-message">Error loading sessions</div>');
            }
        });
    }

    renderSessionTabs() {
        const self = this;
        const $tabBar = $('#session-tabs');

        if (this.sessions.length === 0) {
            $tabBar.html('<div class="no-sessions-message">No sessions connected</div>');
            $('#session-content').html('<div class="no-sessions-message">Start a client to see sessions here</div>');
            return;
        }

        let html = '';
        this.sessions.forEach(function(session) {
            const tabClass = self.getTabClass(session.state);
            const activeClass = session.session_id === self.selectedSessionId ? ' active' : '';
            const label = session.active_project_name || session.client_name || 'Unknown';
            const shortId = session.session_id.substring(0, 8);

            html += `<div class="session-tab ${tabClass}${activeClass}" data-session-id="${session.session_id}">`;
            html += `<span class="tab-status"></span>`;
            html += `<span class="tab-label">${self.escapeHtml(label)} (${shortId})</span>`;
            html += '</div>';
        });

        $tabBar.html(html);

        // Attach click handlers
        $('.session-tab').click(function() {
            const sessionId = $(this).data('session-id');
            self.selectSession(sessionId);
        });
    }

    getTabClass(state) {
        switch (state) {
            case 'active':
                return 'tab-active';
            case 'connected':
                return 'tab-connected';
            case 'idle':
                return 'tab-idle';
            case 'disconnected':
                return 'tab-disconnected';
            default:
                return 'tab-connected';
        }
    }

    selectSession(sessionId) {
        this.selectedSessionId = sessionId;

        // Update tab styling
        $('.session-tab').removeClass('active');
        $(`.session-tab[data-session-id="${sessionId}"]`).addClass('active');

        // Load session details
        this.loadSessionDetails(sessionId);
    }

    loadSessionDetails(sessionId) {
        const self = this;

        $.ajax({
            url: `/api/sessions/${sessionId}`,
            type: 'GET',
            success: function(response) {
                if (response.error) {
                    self.renderSessionError(response.error);
                } else {
                    self.renderSessionDetails(response);
                }
            },
            error: function(xhr, status, error) {
                self.renderSessionError(error);
            }
        });
    }

    renderSessionDetails(session) {
        const self = this;

        let html = `
            <div class="session-header">
                <div>
                    <div class="session-title">${this.escapeHtml(session.active_project_name || 'No Project')}</div>
                    <div class="session-meta">
                        Session: ${session.session_id.substring(0, 8)}...
                        | Client: ${this.escapeHtml(session.client_name || 'Unknown')}
                        | State: ${session.state}
                    </div>
                </div>
                <div class="session-actions">
                    <button class="btn btn-danger" id="disconnect-btn">Disconnect</button>
                </div>
            </div>

            <div class="session-details">
                <div class="detail-card">
                    <h4>Session Info</h4>
                    <div class="detail-grid">
                        <div class="detail-label">Session ID:</div>
                        <div class="detail-value">${session.session_id}</div>
                        <div class="detail-label">Client:</div>
                        <div class="detail-value">${this.escapeHtml(session.client_name || 'N/A')}</div>
                        <div class="detail-label">State:</div>
                        <div class="detail-value">${session.state}</div>
                        <div class="detail-label">Created:</div>
                        <div class="detail-value">${new Date(session.created_at * 1000).toLocaleString()}</div>
                        <div class="detail-label">Last Activity:</div>
                        <div class="detail-value">${new Date(session.last_activity * 1000).toLocaleString()}</div>
                    </div>
                </div>

                <div class="detail-card">
                    <h4>Project Info</h4>
                    <div class="detail-grid">
                        <div class="detail-label">Project:</div>
                        <div class="detail-value">${this.escapeHtml(session.active_project_name || 'None')}</div>
                        <div class="detail-label">Root:</div>
                        <div class="detail-value">${this.escapeHtml(session.active_project_root || 'N/A')}</div>
                        <div class="detail-label">Languages:</div>
                        <div class="detail-value">${(session.project_languages || []).join(', ') || 'N/A'}</div>
                        <div class="detail-label">Modes:</div>
                        <div class="detail-value">${(session.active_modes || []).join(', ') || 'N/A'}</div>
                    </div>
                </div>

                <div class="detail-card">
                    <h4>Tool Usage (${session.tool_call_count || 0} calls)</h4>
                    <div class="tool-stats-list">
        `;

        const toolStats = session.tool_stats || {};
        const sortedTools = Object.keys(toolStats).sort((a, b) => toolStats[b] - toolStats[a]);

        if (sortedTools.length === 0) {
            html += '<div style="color: var(--text-muted);">No tool calls yet</div>';
        } else {
            sortedTools.forEach(function(tool) {
                html += `
                    <div class="tool-stat-item">
                        <span>${tool}</span>
                        <span>${toolStats[tool]}</span>
                    </div>
                `;
            });
        }

        html += `
                    </div>
                </div>
            </div>
        `;

        $('#session-content').html(html);

        // Disconnect handler
        $('#disconnect-btn').click(function() {
            if (confirm(`Disconnect session ${session.session_id.substring(0, 8)}...?`)) {
                self.disconnectSession(session.session_id);
            }
        });
    }

    renderSessionError(error) {
        $('#session-content').html(`<div class="error-message">Error: ${this.escapeHtml(error)}</div>`);
    }

    disconnectSession(sessionId) {
        const self = this;

        $.ajax({
            url: `/api/sessions/${sessionId}`,
            type: 'DELETE',
            success: function(response) {
                self.selectedSessionId = null;
                self.loadSessions();
            },
            error: function(xhr, status, error) {
                alert('Error disconnecting session: ' + error);
            }
        });
    }

    // ===== Lifecycle Events =====

    loadLifecycleEvents() {
        const self = this;

        $.ajax({
            url: '/api/lifecycle-events?limit=200',
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
            $('#lifecycle-events').html('<div class="no-sessions-message">No lifecycle events yet</div>');
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

            if (event.session_id) {
                html += `Session: ${event.session_id.substring(0, 8)}...`;
            }

            const details = event.details || {};
            if (details.client_name) {
                html += ` | Client: ${this.escapeHtml(details.client_name)}`;
            }
            if (details.project_name) {
                html += ` | Project: ${this.escapeHtml(details.project_name)}`;
            }
            if (details.tool_name) {
                html += ` | Tool: ${details.tool_name}`;
            }
            if (details.modes) {
                html += ` | Modes: ${details.modes.join(', ')}`;
            }

            html += `</div></div></div>`;
        });

        $('#lifecycle-events').html(html);
    }

    getEventIcon(eventType) {
        const icons = {
            'server_started': '&#9654;',
            'server_shutdown': '&#9632;',
            'session_created': '&#128100;',
            'session_disconnected': '&#128683;',
            'project_activated': '&#128193;',
            'tool_executed': '&#9881;',
            'modes_changed': '&#9881;',
        };
        return icons[eventType] || '&#8226;';
    }

    getEventClass(eventType) {
        if (eventType.startsWith('server')) return 'event-server';
        if (eventType.startsWith('session')) return 'event-session';
        if (eventType.startsWith('project')) return 'event-project';
        if (eventType.startsWith('tool')) return 'event-tool';
        if (eventType.startsWith('mode')) return 'event-mode';
        return '';
    }

    formatEventType(eventType) {
        return eventType.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    }

    // ===== Tools =====

    loadTools() {
        const self = this;

        $.ajax({
            url: '/api/tools',
            type: 'GET',
            success: function(response) {
                self.renderTools(response.tools || []);
            },
            error: function(xhr, status, error) {
                $('#tools-list').html('<div class="error-message">Error loading tools</div>');
            }
        });
    }

    renderTools(tools) {
        if (tools.length === 0) {
            $('#tools-list').html('<div class="no-sessions-message">No tools available</div>');
            return;
        }

        let html = '';
        tools.forEach(tool => {
            const badge = tool.can_edit
                ? '<span class="tool-badge edit">EDIT</span>'
                : '<span class="tool-badge read">READ</span>';

            html += `
                <div class="tool-card">
                    <div class="tool-name">${tool.name}${badge}</div>
                    <div class="tool-description">${this.escapeHtml(this.truncate(tool.description, 150))}</div>
                </div>
            `;
        });

        $('#tools-list').html(html);
    }

    // ===== Configuration =====

    loadConfig() {
        this.loadProjects();
        this.loadModes();
        this.loadContexts();
    }

    loadProjects() {
        $.ajax({
            url: '/api/projects',
            type: 'GET',
            success: function(response) {
                const projects = response.projects || [];
                let html = '';

                if (projects.length === 0) {
                    html = '<div style="color: var(--text-muted);">No projects registered</div>';
                } else {
                    projects.forEach(p => {
                        html += `
                            <div class="config-item project">
                                <span>${p.name}</span>
                                <span class="project-root">${p.root}</span>
                            </div>
                        `;
                    });
                }

                $('#projects-list').html(html);
            }
        });
    }

    loadModes() {
        $.ajax({
            url: '/api/modes',
            type: 'GET',
            success: function(response) {
                const modes = response.modes || [];
                let html = modes.map(m => `<div class="config-item">${m}</div>`).join('');
                $('#modes-list').html(html || '<div style="color: var(--text-muted);">No modes</div>');
            }
        });
    }

    loadContexts() {
        $.ajax({
            url: '/api/contexts',
            type: 'GET',
            success: function(response) {
                const contexts = response.contexts || [];
                let html = contexts.map(c => `<div class="config-item">${c}</div>`).join('');
                $('#contexts-list').html(html || '<div style="color: var(--text-muted);">No contexts</div>');
            }
        });
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

    truncate(text, maxLen) {
        if (!text) return '';
        if (text.length <= maxLen) return text;
        return text.substring(0, maxLen) + '...';
    }
}
