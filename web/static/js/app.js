// ==================== GLOBAL STATE ====================

let currentTab = 'valves';
let refreshInterval = null;
let nextRunsInterval = null;
let statusData = null;
let nextRunsData = null;
let lastNextRunsUpdate = 0;

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
    
    // Setup simulate form
    const simForm = document.getElementById('simulate-form');
    if (simForm) {
        simForm.addEventListener('submit', handleSimulate);
    }
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
            renderValves(data.valves, queueData);
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
        
        // Re-render valves if we're on the valves tab to show updated next run info
        if (currentTab === 'valves' && statusData) {
            const queueData = await apiCall('/api/queue');
            renderValves(statusData.valves, queueData);
        }
    } catch (error) {
        console.error('Failed to load next runs:', error);
    }
}

async function loadConfig() {
    try {
        const config = await apiCall('/api/config');
        renderConfig(config);
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
    const indicator = document.querySelector('.status-indicator');
    const statusText = document.querySelector('.status-text');
    const uptime = document.querySelector('.uptime');
    
    if (!indicator || !statusText || !uptime) return;
    
    // Status indicator
    indicator.className = 'status-indicator';
    if (system.status === 'OK') {
        indicator.classList.add('ok');
        statusText.textContent = 'System OK';
    } else if (system.status.includes('Err')) {
        indicator.classList.add('error');
        statusText.textContent = system.status;
    } else {
        indicator.classList.add('warning');
        statusText.textContent = system.status;
    }
    
    // Uptime
    const hours = Math.floor(system.uptime_minutes / 60);
    const minutes = system.uptime_minutes % 60;
    uptime.textContent = `Uptime: ${hours}h ${minutes}m`;
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
                        <span class="valve-info-label">Today:</span>
                        <span class="valve-info-value">
                            ${formatTime(valve.seconds_daily)}
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
                            onclick="startValveManual('${valve.name}')"
                            ${valve.is_open ? 'disabled' : ''}>
                        ▶️ Start
                    </button>
                    
                    <button class="btn btn-secondary btn-small" 
                            onclick="showQueueDialog('${valve.name}')">
                        ⏱️ Queue
                    </button>
                    
                    <button class="btn btn-danger btn-small" 
                            onclick="stopValve('${valve.name}')"
                            ${!valve.is_open ? 'disabled' : ''}>
                        ⏹️ Stop
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
                    
                    <button class="btn btn-small" onclick="showValveDetails('${valve.name}')">
                        ℹ️ Details
                    </button>
                </div>
            </div>
        `;
    }).join('');
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

async function showValveDetails(name) {
    try {
        const details = await apiCall(`/api/valves/${name}`);
        
        const schedules = details.schedules.map((s, i) => `
Schedule ${i + 1}:
  - Days: ${s.days.join(', ')}
  - Seasons: ${s.seasons.join(', ')}
  - Time: ${s.time_based_on} ${s.offset_minutes > 0 ? '+' : ''}${s.offset_minutes} min
  - Duration: ${s.duration} minutes
  - UV Adjustments: ${s.enable_uv_adjustments ? 'Enabled' : 'Disabled'}
        `).join('\n');
        
        alert(`Valve Details: ${name}\n\nType: ${details.type}\nSensor: ${details.sensor_name || 'None'}\n\n${schedules}`);
    } catch (error) {
        console.error('Failed to load valve details:', error);
    }
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

function renderConfig(config) {
    const view = document.getElementById('config-view');
    if (!view) return;
    
    // Remove loading message
    const loading = view.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    view.innerHTML = `
        <div class="config-section">
            <h3>System Configuration</h3>
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
        
        ${config.uv_adjustments && config.uv_adjustments.length > 0 ? `
            <div class="config-section">
                <h3>UV Index Adjustments</h3>
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
        ` : ''}
    `;
}

// ==================== SIMULATION ====================

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
