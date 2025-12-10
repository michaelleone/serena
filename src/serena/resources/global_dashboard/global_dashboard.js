/**
 * Global Dashboard for Serena
 *
 * A thin orchestration layer that embeds actual per-instance dashboards
 * via iframes. This ensures automatic compatibility with upstream improvements
 * to the per-instance dashboard.
 */

class GlobalDashboard {
    constructor() {
        this.currentPage = 'instances';
        this.selectedPid = null;
        this.instances = [];
        this.instancePollInterval = null;

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
                    // Check if selected instance still exists
                    const stillExists = self.instances.some(i => i.pid === self.selectedPid);
                    if (!stillExists && self.instances.length > 0) {
                        self.selectInstance(self.instances[0].pid);
                    } else if (!stillExists) {
                        self.selectedPid = null;
                        self.showNoInstancesContent();
                    } else {
                        // Update the iframe if instance state changed (e.g., became zombie)
                        self.updateInstanceContent();
                    }
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
            this.showNoInstancesContent();
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

        // Update content
        this.updateInstanceContent();
    }

    // ===== Instance Content (iframe or zombie message) =====

    updateInstanceContent() {
        const inst = this.instances.find(i => i.pid === this.selectedPid);

        if (!inst) {
            this.showNoInstancesContent();
            return;
        }

        // If zombie, show zombie message instead of iframe
        if (inst.state === 'zombie') {
            this.showZombieContent(inst);
        } else {
            this.showIframeContent(inst);
        }
    }

    showNoInstancesContent() {
        $('#instance-content').html(`
            <div class="no-instances-message">
                <p>No instances selected</p>
                <p class="hint">Start a Serena instance to see it here</p>
            </div>
        `);
    }

    showIframeContent(inst) {
        const dashboardUrl = `http://127.0.0.1:${inst.port}/dashboard/`;

        // Check if iframe already exists with correct URL
        const $existing = $('#instance-iframe');
        if ($existing.length && $existing.attr('src') === dashboardUrl) {
            return; // Already showing correct dashboard
        }

        const html = `
            <div class="iframe-container">
                <iframe id="instance-iframe"
                        src="${dashboardUrl}"
                        frameborder="0"
                        allow="clipboard-read; clipboard-write">
                </iframe>
            </div>
        `;

        $('#instance-content').html(html);
    }

    showZombieContent(inst) {
        const self = this;
        const zombieTime = inst.zombie_detected_at ?
            new Date(inst.zombie_detected_at * 1000).toLocaleString() : 'Unknown';

        const html = `
            <div class="zombie-container">
                <div class="zombie-banner">
                    <div class="zombie-banner-icon">&#9760;</div>
                    <div class="zombie-banner-text">
                        <div class="zombie-banner-title">Instance Unreachable (Zombie)</div>
                        <div class="zombie-banner-message">
                            This instance is no longer responding to health checks.<br>
                            Detected at: ${zombieTime}<br><br>
                            It will be automatically removed in 5 minutes, or you can force kill it now.
                        </div>
                    </div>
                </div>

                <div class="zombie-info">
                    <h3>Last Known State</h3>
                    <div class="info-grid">
                        <div class="info-label">PID:</div>
                        <div class="info-value">${inst.pid}</div>
                        <div class="info-label">Port:</div>
                        <div class="info-value">${inst.port}</div>
                        <div class="info-label">Project:</div>
                        <div class="info-value">${this.escapeHtml(inst.project_name || 'None')}</div>
                        <div class="info-label">Project Root:</div>
                        <div class="info-value">${this.escapeHtml(inst.project_root || 'N/A')}</div>
                        <div class="info-label">Context:</div>
                        <div class="info-value">${this.escapeHtml(inst.context || 'N/A')}</div>
                        <div class="info-label">Modes:</div>
                        <div class="info-value">${inst.modes.join(', ') || 'N/A'}</div>
                        <div class="info-label">Started:</div>
                        <div class="info-value">${new Date(inst.started_at * 1000).toLocaleString()}</div>
                        <div class="info-label">Last Heartbeat:</div>
                        <div class="info-value">${new Date(inst.last_heartbeat * 1000).toLocaleString()}</div>
                    </div>
                </div>

                <div class="zombie-actions">
                    <button class="btn btn-danger" id="force-kill-btn">Force Kill Process</button>
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
            url: '/global-dashboard/api/lifecycle-events',
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
