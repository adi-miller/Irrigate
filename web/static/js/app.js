// ==================== GLOBAL STATE ====================

let currentTab = 'valves';
let refreshInterval = null;
let statusData = null;

// ==================== INITIALIZATION ====================

document.addEventListener('DOMContentLoaded', () => {
    console.log('🌱 Irrigate Control Panel - Starting...');
    
    // Initial load
    loadStatus();
    
    // Auto-refresh every 5 seconds
    refreshInterval = setInterval(loadStatus, 5000);
    
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
            if (statusData) renderValves(statusData.valves);
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
        const data = await apiCall('/api/status');
        statusData = data;
        
        // Update system status
        updateSystemStatus(data.system);
        
        // Update current tab content
        if (currentTab === 'valves') {
            renderValves(data.valves);
        } else if (currentTab === 'sensors') {
            renderSensors(data.sensors);
        }
        
    } catch (error) {
        console.error('Failed to load status:', error);
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

function renderValves(valves) {
    const grid = document.getElementById('valves-grid');
    if (!grid) return;
    
    // Remove loading message
    const loading = grid.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    if (!valves || valves.length === 0) {
        grid.innerHTML = '<p class="text-center">No valves configured</p>';
        return;
    }
    
    grid.innerHTML = valves.map(valve => {
        const status = getValveStatus(valve);
        const timeRemaining = formatTime(valve.seconds_remain);
        const progress = valve.seconds_last > 0 ? 
            ((valve.seconds_last - valve.seconds_remain) / valve.seconds_last * 100) : 0;
        
        return `
            <div class="valve-card" id="valve-${valve.name}">
                <div class="valve-header">
                    <h3 class="valve-name">${valve.name}</h3>
                    <span class="valve-status-badge ${status.toLowerCase()}">${status}</span>
                </div>
                
                <div class="valve-info">
                    ${valve.is_open ? `
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
                    
                    ${valve.seconds_last > 0 ? `
                        <div class="valve-info-row">
                            <span class="valve-info-label">Last Run:</span>
                            <span class="valve-info-value">
                                ${formatTime(valve.seconds_last)}
                                ${valve.liters_last > 0 ? ` / ${valve.liters_last.toFixed(1)}L` : ''}
                            </span>
                        </div>
                    ` : ''}
                </div>
                
                <div class="valve-actions">
                    ${valve.is_open ? `
                        <button class="btn btn-danger btn-small" onclick="stopValve('${valve.name}')">
                            ⏹️ Stop
                        </button>
                    ` : `
                        <button class="btn btn-success btn-small" onclick="showStartDialog('${valve.name}')">
                            ▶️ Start
                        </button>
                        <button class="btn btn-secondary btn-small" onclick="showQueueDialog('${valve.name}')">
                            ⏱️ Queue
                        </button>
                    `}
                    
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

// ==================== VALVE ACTIONS ====================

async function startValve(name, duration) {
    try {
        await apiCall(`/api/valves/${name}/start-manual?duration_minutes=${duration}`, {
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
    } catch (error) {
        console.error('Failed to enable valve:', error);
    }
}

async function disableValve(name) {
    try {
        await apiCall(`/api/valves/${name}/disable`, { method: 'POST' });
        showToast(`Valve ${name} disabled`, 'warning');
        loadStatus();
    } catch (error) {
        console.error('Failed to disable valve:', error);
    }
}

async function suspendValve(name) {
    try {
        await apiCall(`/api/valves/${name}/suspend`, { method: 'POST' });
        showToast(`Valve ${name} suspended`, 'warning');
        loadStatus();
    } catch (error) {
        console.error('Failed to suspend valve:', error);
    }
}

async function resumeValve(name) {
    try {
        await apiCall(`/api/valves/${name}/resume`, { method: 'POST' });
        showToast(`Valve ${name} resumed`, 'success');
        loadStatus();
    } catch (error) {
        console.error('Failed to resume valve:', error);
    }
}

function showStartDialog(name) {
    const duration = prompt(`Start ${name} manually for how many minutes?`, '5');
    if (duration && !isNaN(duration) && duration > 0) {
        startValve(name, parseFloat(duration));
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
