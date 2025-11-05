// ==================== GLOBAL STATE ====================

let currentTab = 'valves';
let refreshInterval = null;
let nextRunsInterval = null;
let statusData = null;
let nextRunsData = null;
let lastNextRunsUpdate = 0;
let openSchedulePanels = new Set(); // Track which schedule panels are open

// ==================== INITIALIZATION ====================

document.addEventListener('DOMContentLoaded', () => {
    console.log('🌱 Irrigate Control Panel - Starting...');
    
    // Initial load
    loadStatus();
    loadNextRuns();  // Initial load of next scheduled runs
    
    // Auto-refresh every 5 seconds
    refreshInterval = setInterval(loadStatus, 5000);
    
    // Refresh next runs every 2 minutes
    nextRunsInterval = setInterval(loadNextRuns, 120000);
    
    // Setup simulate form handler (now in config tab)
    setupSimulateForm();
});

// ==================== TAB SWITCHING ====================

function switchTab(tabName) {
    currentTab = tabName;
    
    // Update tab buttons
    document.querySelectorAll('.tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });
    
    // Update tab content
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `tab-${tabName}`);
    });
    
    // Load content based on tab
    switch(tabName) {
        case 'valves':
            // Reload to get fresh queue data
            loadStatus();
            break;
        case 'queue':
            loadQueue();
            break;
        case 'sensors':
            if (statusData) renderSensors(statusData.sensors);
            break;
        case 'config':
            loadConfig();
            break;
    }
}

// ==================== API CALLS ====================

async function apiCall(endpoint, options = {}) {
    try {
        const response = await fetch(endpoint, options);
        
        if (!response.ok) {
            const error = await response.json().catch(() => ({ error: response.statusText }));
            throw new Error(error.detail || error.error || 'Request failed');
        }
        
        return await response.json();
    } catch (error) {
        console.error('API Error:', error);
        showToast(error.message, 'error');
        throw error;
    }
}

async function loadStatus() {
    try {
        const [data, queueData] = await Promise.all([
            apiCall('/api/status'),
            apiCall('/api/queue')
        ]);
        statusData = data;
        
        // Update system status
        updateSystemStatus(data.system);
        
        // Update current tab content
        if (currentTab === 'valves') {
            // Check if valves grid exists and has content
            const grid = document.getElementById('valves-grid');
            if (grid && grid.children.length > 0) {
                // Update existing valve cards without re-rendering
                updateValves(data.valves, queueData);
            } else {
                // Initial render
                renderValves(data.valves, queueData);
            }
        } else if (currentTab === 'sensors') {
            renderSensors(data.sensors);
        } else if (currentTab === 'queue') {
            renderQueue(queueData);
        }
        
    } catch (error) {
        console.error('Failed to load status:', error);
    }
}

async function loadNextRuns() {
    try {
        const data = await apiCall('/api/next-runs');
        nextRunsData = data.next_runs;
        lastNextRunsUpdate = Date.now();
        
        // Update valves if we're on the valves tab to show updated next run info
        if (currentTab === 'valves' && statusData) {
            const queueData = await apiCall('/api/queue');
            const grid = document.getElementById('valves-grid');
            if (grid && grid.children.length > 0) {
                updateValves(statusData.valves, queueData);
            } else {
                renderValves(statusData.valves, queueData);
            }
        }
    } catch (error) {
        console.error('Failed to load next runs:', error);
    }
}

async function loadConfig() {
    try {
        const [config, status] = await Promise.all([
            apiCall('/api/config'),
            apiCall('/api/status')
        ]);
        renderConfig(config, status.valves);
    } catch (error) {
        console.error('Failed to load config:', error);
    }
}

async function loadQueue() {
    try {
        const queueData = await apiCall('/api/queue');
        renderQueue(queueData);
    } catch (error) {
        console.error('Failed to load queue:', error);
    }
}

// ==================== SYSTEM STATUS ====================

function updateSystemStatus(system) {
    const statusPanel = document.getElementById('system-status');
    const indicator = document.querySelector('.status-indicator');
    const statusText = document.querySelector('.status-text');
    
    if (!statusPanel || !indicator || !statusText) return;
    
    // Hide panel if status is OK, show otherwise
    if (system.status === 'OK') {
        statusPanel.style.display = 'none';
    } else {
        statusPanel.style.display = 'flex';
        
        // Status indicator
        indicator.className = 'status-indicator';
        if (system.status.includes('Err')) {
            indicator.classList.add('error');
            statusText.textContent = system.status;
        } else {
            indicator.classList.add('warning');
            statusText.textContent = system.status;
        }
    }
    
    // Update datetime information
    updateDateTimeInfo(system);
}

function updateDateTimeInfo(system) {
    if (!system.current_time) return;
    
    try {
        // Parse the ISO datetime
        const currentTime = new Date(system.current_time);
        
        // Update date - shorter format
        const dateEl = document.getElementById('current-date');
        if (dateEl) {
            const options = { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' };
            dateEl.textContent = currentTime.toLocaleDateString('en-US', options);
        }
        
        // Update time
        const timeEl = document.getElementById('current-time');
        if (timeEl) {
            const timeStr = currentTime.toLocaleTimeString('en-US', { 
                hour: 'numeric', 
                minute: '2-digit',
                hour12: true 
            });
            timeEl.textContent = timeStr;
        }
        
        // Update season
        const seasonEl = document.getElementById('current-season');
        if (seasonEl && system.season) {
            const seasonEmoji = {
                'Spring': '🌸',
                'Summer': '☀️',
                'Fall': '🍂',
                'Winter': '❄️'
            };
            seasonEl.textContent = `${seasonEmoji[system.season] || ''} ${system.season}`;
        }
        
        // Update sunrise - shorter format
        const sunriseEl = document.getElementById('sunrise-time');
        if (sunriseEl && system.sunrise) {
            const sunrise = new Date(system.sunrise);
            sunriseEl.textContent = sunrise.toLocaleTimeString('en-US', { 
                hour: 'numeric', 
                minute: '2-digit',
                hour12: true 
            });
        }
        
        // Update sunset - shorter format
        const sunsetEl = document.getElementById('sunset-time');
        if (sunsetEl && system.sunset) {
            const sunset = new Date(system.sunset);
            sunsetEl.textContent = sunset.toLocaleTimeString('en-US', { 
                hour: 'numeric', 
                minute: '2-digit',
                hour12: true 
            });
        }
    } catch (error) {
        console.error('Error updating datetime info:', error);
    }
}

// ==================== VALVE RENDERING ====================

function renderValves(valves, queueData = null) {
    const grid = document.getElementById('valves-grid');
    if (!grid) return;
    
    // Remove loading message
    const loading = grid.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    if (!valves || valves.length === 0) {
        grid.innerHTML = '<p class="text-center">No valves configured</p>';
        return;
    }
    
    // Build a map of queued valves with their position
    const queuedValves = new Map();
    if (queueData && queueData.jobs) {
        queueData.jobs.forEach((job, index) => {
            const position = index + 1; // 1-based position
            if (!queuedValves.has(job.valve_name)) {
                queuedValves.set(job.valve_name, []);
            }
            queuedValves.get(job.valve_name).push({ ...job, position });
        });
    }
    
    grid.innerHTML = valves.map(valve => {
        const queuedJobs = queuedValves.get(valve.name) || [];
        const isQueued = queuedJobs.length > 0;
        
        // Determine display status
        let displayStatus = getValveStatus(valve);
        let statusClass = displayStatus.toLowerCase();
        let queueBadge = '';
        
        // If queued, create a separate queue badge
        if (isQueued) {
            const nextPosition = queuedJobs[0].position;
            const queueText = queuedJobs.length > 1 
                ? `Queued #${nextPosition} (+${queuedJobs.length - 1})`
                : `Queued #${nextPosition}`;
            queueBadge = `<span class="valve-status-badge queued">${queueText}</span>`;
        }
        
        const timeRemaining = formatTime(valve.seconds_remain);
        // Progress = remaining time / original job duration
        // This correctly accounts for suspension since seconds_remain doesn't decrease when suspended
        const progress = valve.seconds_duration > 0 ? 
            (valve.seconds_remain / valve.seconds_duration * 100) : 0;
        
        // Get next scheduled run for this valve
        const nextRun = nextRunsData && nextRunsData[valve.name];
        const nextRunFormatted = nextRun ? formatNextRun(nextRun.schedule_time_iso) : null;
        
        return `
            <div class="valve-card" id="valve-${valve.name}">
                <div class="valve-header">
                    <h3 class="valve-name">${valve.name}</h3>
                    <div class="valve-header-right">
                        <span class="valve-status-badge ${statusClass}">${displayStatus}</span>
                        ${queueBadge}
                    </div>
                </div>
                
                <div class="valve-info">
                    ${valve.handled ? `
                        <div class="valve-info-row">
                            <span class="valve-info-label">Time Remaining:</span>
                            <span class="valve-info-value">${timeRemaining}</span>
                        </div>
                        <div class="valve-progress">
                            <div class="progress-bar">
                                <div class="progress-fill" style="width: ${progress}%"></div>
                            </div>
                        </div>
                    ` : ''}
                    
                    <div class="valve-info-row">
                        <span class="valve-info-label">Daily Total:</span>
                        <span class="valve-info-value">
                            💧${formatTime(valve.seconds_daily)}
                            ${valve.liters_daily > 0 ? ` / ${valve.liters_daily.toFixed(1)}L` : ''}
                        </span>
                    </div>
                    
                    ${nextRunFormatted ? `
                        <div class="valve-info-row next-run-row">
                            <span class="valve-info-label">Next Run:</span>
                            <span class="valve-info-value next-run-time">📅 ${nextRunFormatted}</span>
                        </div>
                    ` : `
                        <div class="valve-info-row next-run-row">
                            <span class="valve-info-label">Next Run:</span>
                            <span class="valve-info-value next-run-none">
                                ${!valve.enabled ? 'Disabled' : (valve.suspended ? 'Suspended' : 'None in 7 days')}
                            </span>
                        </div>
                    `}
                </div>
                
                <div class="valve-actions">
                    <button class="btn btn-success btn-small" 
                            onclick="startValveManual('${valve.name}')">
                        🔓 Open
                    </button>
                    
                    <button class="btn btn-secondary btn-small" 
                            onclick="showQueueDialog('${valve.name}')">
                        ⏱️ Queue
                    </button>
                    
                    <button class="btn btn-danger btn-small" 
                            onclick="stopValve('${valve.name}')">
                        🔒 Close
                    </button>
                    
                    ${valve.enabled ? `
                        <button class="btn btn-warning btn-small" onclick="disableValve('${valve.name}')">
                            🚫 Disable
                        </button>
                    ` : `
                        <button class="btn btn-success btn-small" onclick="enableValve('${valve.name}')">
                            ✅ Enable
                        </button>
                    `}
                    
                    ${valve.suspended ? `
                        <button class="btn btn-primary btn-small" onclick="resumeValve('${valve.name}')">
                            ▶️ Resume
                        </button>
                    ` : `
                        <button class="btn btn-warning btn-small" onclick="suspendValve('${valve.name}')">
                            ⏸️ Suspend
                        </button>
                    `}
                    
                    <button class="btn btn-small" onclick="toggleSchedulePanel('${valve.name}')">
                        📅 Schedules
                    </button>
                </div>
                
                <div id="schedule-panel-${valve.name}" class="schedule-panel" style="display: none;">
                    <div class="schedule-panel-loading">Loading schedules...</div>
                </div>
            </div>
        `;
    }).join('');
}

function updateValves(valves, queueData = null) {
    if (!valves || valves.length === 0) return;
    
    // Build a map of queued valves with their position
    const queuedValves = new Map();
    if (queueData && queueData.jobs) {
        queueData.jobs.forEach((job, index) => {
            const position = index + 1;
            if (!queuedValves.has(job.valve_name)) {
                queuedValves.set(job.valve_name, []);
            }
            queuedValves.get(job.valve_name).push({ ...job, position });
        });
    }
    
    valves.forEach(valve => {
        const card = document.getElementById(`valve-${valve.name}`);
        if (!card) return; // Card doesn't exist, might need full render
        
        const queuedJobs = queuedValves.get(valve.name) || [];
        const isQueued = queuedJobs.length > 0;
        
        // Update status badge
        const displayStatus = getValveStatus(valve);
        const statusBadge = card.querySelector('.valve-status-badge:not(.queued)');
        if (statusBadge) {
            statusBadge.className = `valve-status-badge ${displayStatus.toLowerCase()}`;
            statusBadge.textContent = displayStatus;
        }
        
        // Update queue badge
        const headerRight = card.querySelector('.valve-header-right');
        let queueBadge = card.querySelector('.valve-status-badge.queued');
        
        if (isQueued) {
            const nextPosition = queuedJobs[0].position;
            const queueText = queuedJobs.length > 1 
                ? `Queued #${nextPosition} (+${queuedJobs.length - 1})`
                : `Queued #${nextPosition}`;
            
            if (queueBadge) {
                queueBadge.textContent = queueText;
            } else {
                queueBadge = document.createElement('span');
                queueBadge.className = 'valve-status-badge queued';
                queueBadge.textContent = queueText;
                headerRight.appendChild(queueBadge);
            }
        } else if (queueBadge) {
            queueBadge.remove();
        }
        
        // Get the valve-info container
        const valveInfo = card.querySelector('.valve-info');
        if (!valveInfo) return;
        
        // Handle time remaining and progress bar for running valves
        let timeRemainingRow = Array.from(valveInfo.querySelectorAll('.valve-info-row')).find(row => 
            row.querySelector('.valve-info-label')?.textContent === 'Time Remaining:'
        );
        let progressBar = valveInfo.querySelector('.valve-progress');
        
        if (valve.handled) {
            const timeRemaining = formatTime(valve.seconds_remain);
            const progress = valve.seconds_duration > 0 ? 
                (valve.seconds_remain / valve.seconds_duration * 100) : 0;
            
            // If time remaining row doesn't exist, create it
            if (!timeRemainingRow) {
                const todayRow = Array.from(valveInfo.querySelectorAll('.valve-info-row')).find(row => 
                    row.querySelector('.valve-info-label')?.textContent === 'Today:'
                );
                
                timeRemainingRow = document.createElement('div');
                timeRemainingRow.className = 'valve-info-row';
                timeRemainingRow.innerHTML = `
                    <span class="valve-info-label">Time Remaining:</span>
                    <span class="valve-info-value">${timeRemaining}</span>
                `;
                
                if (todayRow) {
                    valveInfo.insertBefore(timeRemainingRow, todayRow);
                } else {
                    valveInfo.insertBefore(timeRemainingRow, valveInfo.firstChild);
                }
            } else {
                // Update existing time remaining
                const timeValueSpan = timeRemainingRow.querySelector('.valve-info-value');
                if (timeValueSpan) {
                    timeValueSpan.textContent = timeRemaining;
                }
            }
            
            // If progress bar doesn't exist, create it
            if (!progressBar) {
                progressBar = document.createElement('div');
                progressBar.className = 'valve-progress';
                progressBar.innerHTML = `
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: ${progress}%"></div>
                    </div>
                `;
                
                if (timeRemainingRow && timeRemainingRow.nextSibling) {
                    valveInfo.insertBefore(progressBar, timeRemainingRow.nextSibling);
                } else {
                    const todayRow = Array.from(valveInfo.querySelectorAll('.valve-info-row')).find(row => 
                        row.querySelector('.valve-info-label')?.textContent === 'Today:'
                    );
                    if (todayRow) {
                        valveInfo.insertBefore(progressBar, todayRow);
                    }
                }
            } else {
                // Update existing progress bar
                const progressFill = progressBar.querySelector('.progress-fill');
                if (progressFill) {
                    progressFill.style.width = `${progress}%`;
                }
            }
        } else {
            // Valve not running - remove time remaining and progress bar if they exist
            if (timeRemainingRow) {
                timeRemainingRow.remove();
            }
            if (progressBar) {
                progressBar.remove();
            }
        }
        
        // Update today's stats
        const todayValueSpan = Array.from(card.querySelectorAll('.valve-info-row')).find(row => 
            row.querySelector('.valve-info-label')?.textContent === 'Today:'
        )?.querySelector('.valve-info-value');
        
        if (todayValueSpan) {
            todayValueSpan.textContent = `${formatTime(valve.seconds_daily)}${valve.liters_daily > 0 ? ` / ${valve.liters_daily.toFixed(1)}L` : ''}`;
        }
        
        // Update next run if available
        const nextRun = nextRunsData && nextRunsData[valve.name];
        const nextRunFormatted = nextRun ? formatNextRun(nextRun.schedule_time_iso) : null;
        const nextRunRow = Array.from(card.querySelectorAll('.valve-info-row')).find(row => 
            row.querySelector('.valve-info-label')?.textContent === 'Next Run:'
        );
        
        if (nextRunRow) {
            const nextRunValue = nextRunRow.querySelector('.valve-info-value');
            if (nextRunValue) {
                if (nextRunFormatted) {
                    nextRunValue.className = 'valve-info-value next-run-time';
                    nextRunValue.textContent = `📅 ${nextRunFormatted}`;
                } else {
                    nextRunValue.className = 'valve-info-value next-run-none';
                    nextRunValue.textContent = !valve.enabled ? 'Disabled' : (valve.suspended ? 'Suspended' : 'None in 7 days');
                }
            }
        }
        
        // Update button states
        const startBtn = card.querySelector('button[onclick*="startValveManual"]');
        const stopBtn = card.querySelector('button[onclick*="stopValve"]');
        const enableBtn = card.querySelector('button[onclick*="enableValve"]');
        const disableBtn = card.querySelector('button[onclick*="disableValve"]');
        const suspendBtn = card.querySelector('button[onclick*="suspendValve"]');
        const resumeBtn = card.querySelector('button[onclick*="resumeValve"]');
        
        // Open and Close buttons are always enabled since valve state may not be accurate
        
        // Handle enable/disable button toggle
        if (valve.enabled && enableBtn) {
            enableBtn.outerHTML = `<button class="btn btn-warning btn-small" onclick="disableValve('${valve.name}')">🚫 Disable</button>`;
        } else if (!valve.enabled && disableBtn) {
            disableBtn.outerHTML = `<button class="btn btn-success btn-small" onclick="enableValve('${valve.name}')">✅ Enable</button>`;
        }
        
        // Handle suspend/resume button toggle
        if (valve.suspended && suspendBtn) {
            suspendBtn.outerHTML = `<button class="btn btn-primary btn-small" onclick="resumeValve('${valve.name}')">▶️ Resume</button>`;
        } else if (!valve.suspended && resumeBtn) {
            resumeBtn.outerHTML = `<button class="btn btn-warning btn-small" onclick="suspendValve('${valve.name}')">⏸️ Suspend</button>`;
        }
    });
}

function getValveStatus(valve) {
    if (!valve.enabled) return 'Disabled';
    if (valve.suspended) return 'Suspended';
    if (valve.is_open) return 'Open';
    if (valve.seconds_last > 60 && valve.liters_last === 0) return 'Malfunction';
    return 'Closed';
}

function formatTime(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    
    if (hours > 0) {
        return `${hours}h ${minutes}m`;
    } else if (minutes > 0) {
        return `${minutes}m ${secs}s`;
    } else {
        return `${secs}s`;
    }
}

function formatNextRun(isoString) {
    if (!isoString) return null;
    
    const scheduleTime = new Date(isoString);
    const now = new Date();
    
    // Reset times to start of day for accurate day comparison
    const scheduleDate = new Date(scheduleTime.getFullYear(), scheduleTime.getMonth(), scheduleTime.getDate());
    const todayDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    
    // Calculate difference in days
    const diffMs = scheduleDate - todayDate;
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
    
    // Format time
    const timeStr = scheduleTime.toLocaleTimeString('en-US', { 
        hour: 'numeric', 
        minute: '2-digit',
        hour12: true 
    });
    
    let dayStr;
    
    // If today
    if (diffDays === 0) {
        dayStr = 'Today';
    }
    // If tomorrow
    else if (diffDays === 1) {
        dayStr = 'Tomorrow';
    }
    // If within a week (2-6 days), show day name
    else if (diffDays >= 2 && diffDays < 7) {
        dayStr = scheduleTime.toLocaleDateString('en-US', { weekday: 'short' });
    }
    // Otherwise show date
    else {
        dayStr = scheduleTime.toLocaleDateString('en-US', { 
            month: 'short', 
            day: 'numeric' 
        });
    }
    
    // Use middle dot as separator
    return `${dayStr} • ${timeStr}`;
}

// ==================== VALVE ACTIONS ====================

async function startValveManual(name) {
    try {
        await apiCall(`/api/valves/${name}/start-manual`, {
            method: 'POST'
        });
        showToast(`Valve ${name} started manually`, 'success');
        loadStatus();
    } catch (error) {
        console.error('Failed to start valve:', error);
    }
}

async function queueValve(name, duration) {
    try {
        await apiCall(`/api/valves/${name}/queue?duration_minutes=${duration}`, {
            method: 'POST'
        });
        showToast(`Valve ${name} queued for ${duration} minutes`, 'success');
        loadStatus();
    } catch (error) {
        console.error('Failed to queue valve:', error);
    }
}

async function stopValve(name) {
    try {
        await apiCall(`/api/valves/${name}/stop`, { method: 'POST' });
        showToast(`Valve ${name} stopped`, 'success');
        loadStatus();
    } catch (error) {
        console.error('Failed to stop valve:', error);
    }
}

async function enableValve(name) {
    try {
        await apiCall(`/api/valves/${name}/enable`, { method: 'POST' });
        showToast(`Valve ${name} enabled`, 'success');
        loadStatus();
        loadNextRuns();  // Refresh next runs since schedule availability changed
    } catch (error) {
        console.error('Failed to enable valve:', error);
    }
}

async function disableValve(name) {
    try {
        await apiCall(`/api/valves/${name}/disable`, { method: 'POST' });
        showToast(`Valve ${name} disabled`, 'warning');
        loadStatus();
        loadNextRuns();  // Refresh next runs since schedule availability changed
    } catch (error) {
        console.error('Failed to disable valve:', error);
    }
}

async function suspendValve(name) {
    try {
        await apiCall(`/api/valves/${name}/suspend`, { method: 'POST' });
        showToast(`Valve ${name} suspended`, 'warning');
        loadStatus();
        loadNextRuns();  // Refresh next runs since schedule availability changed
    } catch (error) {
        console.error('Failed to suspend valve:', error);
    }
}

async function resumeValve(name) {
    try {
        await apiCall(`/api/valves/${name}/resume`, { method: 'POST' });
        showToast(`Valve ${name} resumed`, 'success');
        loadStatus();
        loadNextRuns();  // Refresh next runs since schedule availability changed
    } catch (error) {
        console.error('Failed to resume valve:', error);
    }
}

function showQueueDialog(name) {
    const duration = prompt(`Queue ${name} for how many minutes?`, '15');
    if (duration && !isNaN(duration) && duration > 0) {
        queueValve(name, parseFloat(duration));
    }
}

async function toggleSchedulePanel(name) {
    const panel = document.getElementById(`schedule-panel-${name}`);
    if (!panel) return;
    
    // If panel is already visible, hide it
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        openSchedulePanels.delete(name);
        return;
    }
    
    // Show panel and load schedule data
    panel.style.display = 'block';
    openSchedulePanels.add(name);
    
    await loadSchedulePanelData(name);
}

async function loadSchedulePanelData(name) {
    const panel = document.getElementById(`schedule-panel-${name}`);
    if (!panel) return;
    
    try {
        const details = await apiCall(`/api/valves/${name}`);
        
        if (!details.schedules || details.schedules.length === 0) {
            panel.innerHTML = `
                <div class="schedule-panel-empty">
                    <p>No schedules configured for this valve</p>
                </div>
            `;
            return;
        }
        
        const schedulesHTML = details.schedules.map((s, i) => {
            // Format days - show "Everyday" if empty or all 7 days
            const allDays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
            let daysText = 'Everyday';
            if (s.days && s.days.length > 0 && s.days.length < 7) {
                // Check if it's all days
                const hasAllDays = allDays.every(day => s.days.includes(day));
                if (!hasAllDays) {
                    daysText = s.days.join(', ');
                }
            }
            
            // Format seasons - show "All Year" if empty or all 4 seasons
            const allSeasons = ['Spring', 'Summer', 'Fall', 'Winter'];
            let seasonsText = 'All Year';
            if (s.seasons && s.seasons.length > 0 && s.seasons.length < 4) {
                // Check if it's all seasons
                const hasAllSeasons = allSeasons.every(season => s.seasons.includes(season));
                if (!hasAllSeasons) {
                    seasonsText = s.seasons.join(', ');
                }
            }
            
            // Format time with offset and proper capitalization
            let timeBasedOn = s.time_based_on.charAt(0).toUpperCase() + s.time_based_on.slice(1);
            let timeText = timeBasedOn;
            
            if (s.time_based_on === 'fixed' && s.fixed_start_time) {
                // Convert 24-hour time to 12-hour format to match "Next Run" display
                const [hours, minutes] = s.fixed_start_time.split(':');
                const hour = parseInt(hours);
                const ampm = hour >= 12 ? 'PM' : 'AM';
                const hour12 = hour % 12 || 12;
                const formattedTime = `${hour12}:${minutes} ${ampm}`;
                timeText = `${formattedTime} (Fixed)`;
            } else if (s.offset_minutes !== 0) {
                timeText += ` ${s.offset_minutes > 0 ? '+' : ''}${s.offset_minutes} min`;
            }
            
            return `
                <div class="schedule-item">
                    <div class="schedule-item-header">
                        <span class="schedule-number">Schedule ${i + 1}</span>
                        ${s.enable_uv_adjustments ? '<span class="schedule-uv-pill">UV Adjusted</span>' : ''}
                    </div>
                    <div class="schedule-item-details">
                        <div class="schedule-when">
                            <span class="schedule-days">${daysText}</span>
                            <span class="schedule-seasons">${seasonsText}</span>
                        </div>
                        <div class="schedule-detail-row">
                            <span class="schedule-detail-label">Time:</span>
                            <span class="schedule-detail-value">${timeText}</span>
                        </div>
                        <div class="schedule-detail-row">
                            <span class="schedule-detail-label">Duration:</span>
                            <span class="schedule-detail-value">${s.duration} minutes</span>
                        </div>
                    </div>
                </div>
            `;
        }).join('');
        
        panel.innerHTML = schedulesHTML;
    } catch (error) {
        panel.innerHTML = `
            <div class="schedule-panel-error">
                <p>Failed to load schedules</p>
            </div>
        `;
        console.error('Failed to load valve schedules:', error);
    }
}

function refreshOpenSchedulePanels() {
    // Reload data for any open schedule panels
    openSchedulePanels.forEach(name => {
        loadSchedulePanelData(name);
    });
}

// ==================== SENSOR RENDERING ====================

function renderSensors(sensors) {
    const list = document.getElementById('sensors-list');
    if (!list) return;
    
    // Remove loading message
    const loading = list.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    if (!sensors || sensors.length === 0) {
        list.innerHTML = '<p class="text-center">No sensors configured</p>';
        return;
    }
    
    list.innerHTML = sensors.map(sensor => {
        const telemetry = sensor.telemetry || {};
        const hasError = sensor.error || false;
        
        return `
            <div class="sensor-card">
                <div class="sensor-header">
                    <h3 class="sensor-name">${sensor.name}</h3>
                    <span class="sensor-type">${sensor.type}</span>
                </div>
                
                ${hasError ? `
                    <div style="color: var(--danger); padding: 1rem; background: #ffebee; border-radius: var(--radius);">
                        ⚠️ Sensor Error - Unable to retrieve data
                    </div>
                ` : `
                    <div class="sensor-telemetry">
                        ${telemetry.uv_index !== undefined ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">UV Index</div>
                                <div class="telemetry-value">${telemetry.uv_index.toFixed(1)}</div>
                            </div>
                        ` : ''}
                        
                        ${telemetry.temperature !== undefined ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">Temperature</div>
                                <div class="telemetry-value">${telemetry.temperature.toFixed(1)}°C</div>
                            </div>
                        ` : ''}
                        
                        ${telemetry.rain !== undefined ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">Rain</div>
                                <div class="telemetry-value">${telemetry.rain ? '🌧️ Yes' : '☀️ No'}</div>
                            </div>
                        ` : ''}
                        
                        ${sensor.factor !== undefined && sensor.factor !== null ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">Factor</div>
                                <div class="telemetry-value">${sensor.factor.toFixed(2)}x</div>
                            </div>
                        ` : ''}
                        
                        ${sensor.should_disable !== undefined && sensor.should_disable !== null ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">Should Disable</div>
                                <div class="telemetry-value">${sensor.should_disable ? '🚫 Yes' : '✅ No'}</div>
                            </div>
                        ` : ''}
                    </div>
                `}
            </div>
        `;
    }).join('');
}

// ==================== QUEUE RENDERING ====================

function renderQueue(queueData) {
    const view = document.getElementById('queue-view');
    if (!view) return;
    
    // Remove loading message
    const loading = view.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    if (!queueData.jobs || queueData.jobs.length === 0) {
        view.innerHTML = `
            <div class="queue-empty">
                <div class="empty-icon">📭</div>
                <h3>Queue is Empty</h3>
                <p>No valves are currently queued for irrigation</p>
            </div>
        `;
        return;
    }
    
    view.innerHTML = `
        <div class="queue-header">
            <h2>Job Queue</h2>
            <span class="queue-count">${queueData.queue_size} job${queueData.queue_size !== 1 ? 's' : ''} in queue</span>
        </div>
        <div class="queue-list">
            ${queueData.jobs.map((job, index) => `
                <div class="queue-item">
                    <div class="queue-item-number">${index + 1}</div>
                    <div class="queue-item-details">
                        <div class="queue-item-valve">${job.valve_name}</div>
                        <div class="queue-item-info">
                            <span class="queue-duration">⏱️ ${job.duration_minutes} min</span>
                            <span class="queue-type ${job.is_scheduled ? 'scheduled' : 'manual'}">
                                ${job.is_scheduled ? '📅 Scheduled' : '👤 Manual'}
                            </span>
                        </div>
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

// ==================== CONFIG RENDERING ====================

function renderConfig(config, valves) {
    const view = document.getElementById('config-view');
    if (!view) return;
    
    // Remove loading message
    const loading = view.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    view.innerHTML = `
        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('schedules')">
                <h3>Schedules</h3>
                <span class="collapse-icon">▶</span>
            </div>
            <div class="config-section-content" id="schedules" style="display: none;">
                <div id="schedules-container">
                    ${valves ? renderValveSchedules(valves) : '<p>Loading schedules...</p>'}
                </div>
            </div>
        </div>

        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('simulate')">
                <h3>Schedule Simulator</h3>
                <span class="collapse-icon">▶</span>
            </div>
            <div class="config-section-content" id="simulate" style="display: none;">
                <div class="simulate-form">
                    <p>Test irrigation schedules with different conditions</p>
                    
                    <form id="simulate-form">
                        <div class="form-row">
                            <div class="form-group">
                                <label for="sim-date">Date (YYYY-MM-DD or MM-DD)</label>
                                <input type="text" id="sim-date" placeholder="11-05">
                            </div>
                            <div class="form-group">
                                <label for="sim-time">Time (HH:MM)</label>
                                <input type="text" id="sim-time" placeholder="06:00">
                            </div>
                        </div>

                        <div class="form-row">
                            <div class="form-group">
                                <label for="sim-uv">UV Index (0-15)</label>
                                <input type="number" id="sim-uv" min="0" max="15" step="0.1" placeholder="5.0">
                            </div>
                            <div class="form-group">
                                <label for="sim-season">Season</label>
                                <select id="sim-season">
                                    <option value="">Current Season</option>
                                    <option value="Spring">Spring</option>
                                    <option value="Summer">Summer</option>
                                    <option value="Fall">Fall</option>
                                    <option value="Winter">Winter</option>
                                </select>
                            </div>
                        </div>

                        <div class="form-row">
                            <div class="form-group">
                                <label for="sim-rain">Should Disable</label>
                                <select id="sim-rain">
                                    <option value="">Normal</option>
                                    <option value="true">Yes (disable)</option>
                                    <option value="false">No</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label for="sim-days">Days to Simulate</label>
                                <input type="number" id="sim-days" min="1" max="30" value="1">
                            </div>
                        </div>

                        <button type="submit" class="btn btn-primary">Run Simulation</button>
                    </form>

                    <div id="simulate-output" class="simulate-output" style="display: none;">
                        <h3>Simulation Results</h3>
                        <pre id="simulate-results"></pre>
                    </div>
                </div>
            </div>
        </div>

        ${config.uv_adjustments && config.uv_adjustments.length > 0 ? `
            <div class="config-section collapsible">
                <div class="config-section-header" onclick="toggleConfigSection('uv-adjustments')">
                    <h3>UV Index Adjustments</h3>
                    <span class="collapse-icon">▶</span>
                </div>
                <div class="config-section-content" id="uv-adjustments" style="display: none;">
                    <table class="config-table">
                        <thead>
                            <tr>
                                <th>Max UV Index</th>
                                <th>Multiplier</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${config.uv_adjustments.map(adj => `
                                <tr>
                                    <td>≤ ${adj.max_uv_index}</td>
                                    <td>${adj.multiplier}x</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            </div>
        ` : ''}
        
        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('system-config')">
                <h3>System Configuration</h3>
                <span class="collapse-icon">▶</span>
            </div>
            <div class="config-section-content" id="system-config" style="display: none;">
                <div class="config-grid">
                    <div class="config-item">
                        <div class="config-label">Timezone</div>
                        <div class="config-value">${config.timezone}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-label">Location</div>
                        <div class="config-value">${config.location.latitude.toFixed(4)}, ${config.location.longitude.toFixed(4)}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-label">Max Concurrent Valves</div>
                        <div class="config-value">${config.max_concurrent_valves}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-label">Telemetry</div>
                        <div class="config-value">${config.telemetry_enabled ? '✅ Enabled' : '🚫 Disabled'}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-label">MQTT</div>
                        <div class="config-value">${config.mqtt_enabled ? '✅ Enabled' : '🚫 Disabled'}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-label">Valves</div>
                        <div class="config-value">${config.valve_count}</div>
                    </div>
                    <div class="config-item">
                        <div class="config-label">Sensors</div>
                        <div class="config-value">${config.sensor_count}</div>
                    </div>
                </div>
            </div>
        </div>
    `;
    
    // Load schedules for all valves after rendering
    if (valves) {
        valves.forEach(valve => {
            loadValveSchedules(valve.name);
        });
    }
    
    // Setup simulate form handler
    setupSimulateForm();
}

function renderValveSchedules(valves) {
    if (!valves || valves.length === 0) {
        return '<p>No valves configured</p>';
    }
    
    return valves.map(valve => `
        <div class="valve-schedule-section">
            <div class="valve-schedule-header">
                <h4>${valve.name}</h4>
                <button class="btn btn-primary btn-small" onclick="addSchedule('${valve.name}')">
                    ➕ Add Schedule
                </button>
            </div>
            <div class="schedules-list" id="schedules-${valve.name}">
                ${renderSchedulesList(valve.name)}
            </div>
        </div>
    `).join('');
}

function renderSchedulesList(valveName) {
    return `<div class="schedule-loading">Loading...</div>`;
}

async function loadValveSchedules(valveName) {
    try {
        const valve = await apiCall(`/api/valves/${valveName}`);
        const container = document.getElementById(`schedules-${valveName}`);
        if (!container) return;
        
        if (!valve.schedules || valve.schedules.length === 0) {
            container.innerHTML = '<p class="no-schedules">No schedules configured</p>';
            return;
        }
        
        container.innerHTML = valve.schedules.map((sched, idx) => `
            <div class="schedule-item" id="schedule-${valveName}-${idx}">
                <div class="schedule-display" id="schedule-display-${valveName}-${idx}">
                    ${renderScheduleDisplay(sched, valveName, idx)}
                </div>
                <div class="schedule-edit" id="schedule-edit-${valveName}-${idx}" style="display: none;">
                    ${renderScheduleEditor(sched, valveName, idx)}
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load schedules:', error);
        const container = document.getElementById(`schedules-${valveName}`);
        if (container) {
            container.innerHTML = '<p class="error">Failed to load schedules</p>';
        }
    }
}

function renderScheduleDisplay(sched, valveName, idx) {
    const days = sched.days && sched.days.length > 0 ? sched.days.join(', ') : 'Every day';
    const seasons = sched.seasons && sched.seasons.length > 0 ? sched.seasons.join(', ') : 'All seasons';
    
    let timeStr = '';
    if (sched.time_based_on === 'fixed') {
        timeStr = `at ${sched.fixed_start_time}`;
    } else if (sched.time_based_on === 'sunrise') {
        const offset = sched.offset_minutes || 0;
        timeStr = offset === 0 ? 'at sunrise' : 
                  offset > 0 ? `${offset}min after sunrise` : 
                  `${Math.abs(offset)}min before sunrise`;
    } else if (sched.time_based_on === 'sunset') {
        const offset = sched.offset_minutes || 0;
        timeStr = offset === 0 ? 'at sunset' : 
                  offset > 0 ? `${offset}min after sunset` : 
                  `${Math.abs(offset)}min before sunset`;
    }
    
    return `
        <div class="schedule-info">
            <div class="schedule-row">
                <span class="schedule-label">Time:</span>
                <span class="schedule-value">${timeStr}</span>
            </div>
            <div class="schedule-row">
                <span class="schedule-label">Duration:</span>
                <span class="schedule-value">${sched.duration} minutes</span>
            </div>
            <div class="schedule-row">
                <span class="schedule-label">Days:</span>
                <span class="schedule-value">${days}</span>
            </div>
            <div class="schedule-row">
                <span class="schedule-label">Seasons:</span>
                <span class="schedule-value">${seasons}</span>
            </div>
            <div class="schedule-row">
                <span class="schedule-label">UV Adjustments:</span>
                <span class="schedule-value">${sched.enable_uv_adjustments ? '✅ Enabled' : '🚫 Disabled'}</span>
            </div>
        </div>
        <div class="schedule-actions">
            <button class="btn btn-secondary btn-small" onclick="editSchedule('${valveName}', ${idx})">
                ✏️ Edit
            </button>
            <button class="btn btn-danger btn-small" onclick="deleteSchedule('${valveName}', ${idx})">
                🗑️ Delete
            </button>
        </div>
    `;
}

function renderScheduleEditor(sched, valveName, idx) {
    const isNew = idx === -1;
    return `
        <form class="schedule-form" onsubmit="saveSchedule(event, '${valveName}', ${idx})">
            <div class="form-group">
                <label>Time Based On:</label>
                <select id="time_based_on-${valveName}-${idx}" class="form-control" onchange="updateTimeFields('${valveName}', ${idx})">
                    <option value="fixed" ${sched.time_based_on === 'fixed' ? 'selected' : ''}>Fixed Time</option>
                    <option value="sunrise" ${sched.time_based_on === 'sunrise' ? 'selected' : ''}>Sunrise</option>
                    <option value="sunset" ${sched.time_based_on === 'sunset' ? 'selected' : ''}>Sunset</option>
                </select>
            </div>
            
            <div class="form-group" id="fixed_time_group-${valveName}-${idx}" style="${sched.time_based_on === 'fixed' ? '' : 'display: none;'}">
                <label>Start Time:</label>
                <input type="time" id="fixed_start_time-${valveName}-${idx}" class="form-control" 
                       value="${sched.fixed_start_time || '06:00'}">
            </div>
            
            <div class="form-group" id="offset_group-${valveName}-${idx}" style="${sched.time_based_on !== 'fixed' ? '' : 'display: none;'}">
                <label>Offset (minutes):</label>
                <input type="number" id="offset_minutes-${valveName}-${idx}" class="form-control" 
                       value="${sched.offset_minutes || 0}" step="1">
                <small>Positive = after, Negative = before</small>
            </div>
            
            <div class="form-group">
                <label>Duration (minutes):</label>
                <input type="number" id="duration-${valveName}-${idx}" class="form-control" 
                       value="${sched.duration || 10}" min="1" required>
            </div>
            
            <div class="form-group">
                <label>Days of Week:</label>
                <div class="checkbox-group">
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Sun" ${sched.days && sched.days.includes('Sun') ? 'checked' : ''}>
                        Sunday
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Mon" ${sched.days && sched.days.includes('Mon') ? 'checked' : ''}>
                        Monday
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Tue" ${sched.days && sched.days.includes('Tue') ? 'checked' : ''}>
                        Tuesday
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Wed" ${sched.days && sched.days.includes('Wed') ? 'checked' : ''}>
                        Wednesday
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Thu" ${sched.days && sched.days.includes('Thu') ? 'checked' : ''}>
                        Thursday
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Fri" ${sched.days && sched.days.includes('Fri') ? 'checked' : ''}>
                        Friday
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="day-checkbox" value="Sat" ${sched.days && sched.days.includes('Sat') ? 'checked' : ''}>
                        Saturday
                    </label>
                </div>
                <small>Leave all unchecked for every day</small>
            </div>
            
            <div class="form-group">
                <label>Seasons:</label>
                <div class="checkbox-group">
                    <label class="checkbox-label">
                        <input type="checkbox" class="season-checkbox" value="Spring" ${sched.seasons && sched.seasons.includes('Spring') ? 'checked' : ''}>
                        Spring
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="season-checkbox" value="Summer" ${sched.seasons && sched.seasons.includes('Summer') ? 'checked' : ''}>
                        Summer
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="season-checkbox" value="Fall" ${sched.seasons && sched.seasons.includes('Fall') ? 'checked' : ''}>
                        Fall
                    </label>
                    <label class="checkbox-label">
                        <input type="checkbox" class="season-checkbox" value="Winter" ${sched.seasons && sched.seasons.includes('Winter') ? 'checked' : ''}>
                        Winter
                    </label>
                </div>
                <small>Leave all unchecked for all seasons</small>
            </div>
            
            <div class="form-group">
                <label class="checkbox-label">
                    <input type="checkbox" id="enable_uv_adjustments-${valveName}-${idx}" 
                           ${sched.enable_uv_adjustments ? 'checked' : ''}>
                    Enable UV Adjustments
                </label>
            </div>
            
            <div class="schedule-actions">
                <button type="submit" class="btn btn-success btn-small">
                    💾 Save
                </button>
                <button type="button" class="btn btn-secondary btn-small" onclick="cancelEditSchedule('${valveName}', ${idx})">
                    ❌ Cancel
                </button>
            </div>
        </form>
    `;
}

// ==================== SCHEDULE MANAGEMENT ====================

function updateTimeFields(valveName, idx) {
    const timeBasedOn = document.getElementById(`time_based_on-${valveName}-${idx}`).value;
    const fixedGroup = document.getElementById(`fixed_time_group-${valveName}-${idx}`);
    const offsetGroup = document.getElementById(`offset_group-${valveName}-${idx}`);
    
    if (timeBasedOn === 'fixed') {
        fixedGroup.style.display = '';
        offsetGroup.style.display = 'none';
    } else {
        fixedGroup.style.display = 'none';
        offsetGroup.style.display = '';
    }
}

async function addSchedule(valveName) {
    try {
        // Create a new schedule with default values
        const defaultSchedule = {
            time_based_on: 'fixed',
            fixed_start_time: '06:00',
            duration: 10,
            days: [],
            seasons: [],
            enable_uv_adjustments: false
        };
        
        const result = await apiCall(`/api/valves/${valveName}/schedules`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(defaultSchedule)
        });
        
        showToast(`Schedule added to ${valveName}`, 'success');
        
        // Reload the schedules for this valve
        await loadValveSchedules(valveName);
        
        // Automatically enter edit mode for the new schedule
        const newIdx = result.schedule_index;
        editSchedule(valveName, newIdx);
        
        // Refresh any open schedule panels in the valves view
        refreshOpenSchedulePanels();
        
        // Reload next runs to update the display
        await loadNextRuns();
        
    } catch (error) {
        console.error('Failed to add schedule:', error);
    }
}

function editSchedule(valveName, idx) {
    const displayEl = document.getElementById(`schedule-display-${valveName}-${idx}`);
    const editEl = document.getElementById(`schedule-edit-${valveName}-${idx}`);
    
    if (displayEl && editEl) {
        displayEl.style.display = 'none';
        editEl.style.display = 'block';
    }
}

function cancelEditSchedule(valveName, idx) {
    const displayEl = document.getElementById(`schedule-display-${valveName}-${idx}`);
    const editEl = document.getElementById(`schedule-edit-${valveName}-${idx}`);
    
    if (displayEl && editEl) {
        displayEl.style.display = 'block';
        editEl.style.display = 'none';
    }
}

async function saveSchedule(event, valveName, idx) {
    event.preventDefault();
    
    try {
        const timeBasedOn = document.getElementById(`time_based_on-${valveName}-${idx}`).value;
        const duration = parseInt(document.getElementById(`duration-${valveName}-${idx}`).value);
        const enableUv = document.getElementById(`enable_uv_adjustments-${valveName}-${idx}`).checked;
        
        // Collect selected days from checkboxes
        const scheduleForm = event.target;
        const dayCheckboxes = scheduleForm.querySelectorAll('.day-checkbox:checked');
        const selectedDays = Array.from(dayCheckboxes).map(cb => cb.value);
        
        // Collect selected seasons from checkboxes
        const seasonCheckboxes = scheduleForm.querySelectorAll('.season-checkbox:checked');
        const selectedSeasons = Array.from(seasonCheckboxes).map(cb => cb.value);
        
        const scheduleData = {
            time_based_on: timeBasedOn,
            duration: duration,
            enable_uv_adjustments: enableUv,
            days: selectedDays,
            seasons: selectedSeasons
        };
        
        // Add time-specific fields
        if (timeBasedOn === 'fixed') {
            scheduleData.fixed_start_time = document.getElementById(`fixed_start_time-${valveName}-${idx}`).value;
        } else {
            scheduleData.offset_minutes = parseInt(document.getElementById(`offset_minutes-${valveName}-${idx}`).value) || 0;
        }
        
        await apiCall(`/api/valves/${valveName}/schedules/${idx}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(scheduleData)
        });
        
        showToast(`Schedule updated for ${valveName}`, 'success');
        
        // Reload the schedules for the config page
        await loadValveSchedules(valveName);
        
        // Refresh any open schedule panels in the valves view
        refreshOpenSchedulePanels();
        
        // Reload next runs to update the display
        await loadNextRuns();
        
    } catch (error) {
        console.error('Failed to save schedule:', error);
    }
}

async function deleteSchedule(valveName, idx) {
    if (!confirm(`Are you sure you want to delete this schedule for ${valveName}?`)) {
        return;
    }
    
    try {
        await apiCall(`/api/valves/${valveName}/schedules/${idx}`, {
            method: 'DELETE'
        });
        
        showToast(`Schedule deleted from ${valveName}`, 'success');
        
        // Reload the schedules for the config page
        await loadValveSchedules(valveName);
        
        // Refresh any open schedule panels in the valves view
        refreshOpenSchedulePanels();
        
        // Reload next runs to update the display
        await loadNextRuns();
        
    } catch (error) {
        console.error('Failed to delete schedule:', error);
    }
}

// ==================== SIMULATION ====================

function setupSimulateForm() {
    const simForm = document.getElementById('simulate-form');
    if (simForm) {
        // Remove existing listener if any
        simForm.removeEventListener('submit', handleSimulate);
        // Add new listener
        simForm.addEventListener('submit', handleSimulate);
    }
}

function toggleConfigSection(sectionId) {
    const content = document.getElementById(sectionId);
    const header = content.previousElementSibling;
    const icon = header.querySelector('.collapse-icon');
    
    if (content.style.display === 'none') {
        content.style.display = 'block';
        icon.textContent = '▼';
    } else {
        content.style.display = 'none';
        icon.textContent = '▶';
    }
}

async function handleSimulate(e) {
    e.preventDefault();
    
    const date = document.getElementById('sim-date').value;
    const time = document.getElementById('sim-time').value;
    const uv = document.getElementById('sim-uv').value;
    const season = document.getElementById('sim-season').value;
    const rain = document.getElementById('sim-rain').value;
    const days = document.getElementById('sim-days').value;
    
    const params = new URLSearchParams();
    if (date) params.append('date', date);
    if (time) params.append('time', time);
    if (uv) params.append('uv', uv);
    if (season) params.append('season', season);
    if (rain) params.append('rain', rain);
    if (days && days > 1) params.append('days', days);
    
    try {
        const response = await fetch(`/api/simulate?${params.toString()}`, {
            method: 'POST'
        });
        
        if (!response.ok) {
            throw new Error('Simulation failed');
        }
        
        const result = await response.text();
        
        document.getElementById('simulate-output').style.display = 'block';
        document.getElementById('simulate-results').textContent = result;
        
        showToast('Simulation completed', 'success');
    } catch (error) {
        console.error('Simulation error:', error);
        showToast('Simulation failed: ' + error.message, 'error');
    }
}

// ==================== TOAST NOTIFICATIONS ====================

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    
    toast.textContent = message;
    toast.className = `toast ${type} show`;
    
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// ==================== CLEANUP ====================

window.addEventListener('beforeunload', () => {
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }
});
