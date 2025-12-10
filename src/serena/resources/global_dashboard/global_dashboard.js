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
