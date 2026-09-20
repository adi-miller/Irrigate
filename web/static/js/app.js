// ==================== GLOBAL STATE ====================

let currentTab = 'valves';
let refreshInterval = null;
let nextRunsInterval = null;
let statusData = null;
let healthData = null;
let queueSnapshot = null;
let nextRunsData = null;
let lastNextRunsUpdate = 0;
let openSchedulePanels = new Set(); // Track which schedule panels are open
const REQUEST_TIMEOUT_MS = 8000;
const STATUS_MAX_AGE_MS = 15000;
let lastStatusUpdate = null;
let statusFresh = false;
let statusIssue = 'Connecting to the controller.';
let statusVersion = 0;
let statusRequest = null;
let nextRunsVersion = 0;
let nextRunsRequest = null;
let configVersion = 0;
let sensorsVersion = 0;
let unloading = false;
let toastTimeout = null;
const activeRequests = new Map();
const pendingWrites = new Set();
const pendingSettings = new Map();
const scheduleEditors = new Map();
const pendingSchedules = new Set();
const savedSchedules = new Map();
const scheduleLoadVersions = new Map();

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
    
    // Redraw waterflow chart on window resize
    window.addEventListener('resize', () => {
        if (statusData && statusData.waterflow) {
            updateWaterflowChart(statusData.waterflow);
        }
    });
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
            loadSensorsWithConfig();
            break;
        case 'config':
            loadConfig();
            break;
    }
}

// ==================== API CALLS ====================

async function apiCall(endpoint, options = {}, quiet = false, responseType = 'json') {
    if (unloading) throw new Error('Page is closing');
    const controller = new AbortController();
    let timedOut = false;
    const timeout = setTimeout(() => {
        timedOut = true;
        controller.abort();
    }, REQUEST_TIMEOUT_MS);
    activeRequests.set(controller, timeout);
    const cancel = () => controller.abort();
    const aborted = new Promise((resolve, reject) => {
        controller.signal.addEventListener('abort', () => {
            reject(new Error(timedOut ? 'Request timed out' : 'Request cancelled'));
        }, { once: true });
    });
    options.signal?.addEventListener('abort', cancel, { once: true });
    if (options.signal?.aborted) cancel();

    try {
        return await Promise.race([
            (async () => {
                const response = await fetch(endpoint, { ...options, signal: controller.signal });
                if (!response.ok) {
                    const error = await response.json().catch(() => ({ error: response.statusText }));
                    throw new Error(error.detail || error.error || 'Request failed');
                }
                return responseType === 'text' ? response.text() : response.json();
            })(),
            aborted
        ]);
    } catch (error) {
        if (!quiet && !unloading && !options.signal?.aborted) {
            console.error('API Error:', error);
            showToast(error.message, 'error');
        }
        throw error;
    } finally {
        clearTimeout(timeout);
        activeRequests.delete(controller);
        options.signal?.removeEventListener('abort', cancel);
    }
}

async function apiMutation(endpoint, options) {
    const result = await apiCall(endpoint, options);
    if (result?.success !== true) {
        const error = new Error('The controller did not confirm the change.');
        showToast(error.message, 'error');
        throw error;
    }
    return result;
}

function hasFreshStatus() {
    return !unloading && statusFresh && lastStatusUpdate !== null &&
        Date.now() - lastStatusUpdate < STATUS_MAX_AGE_MS;
}

function controllerReady() {
    return hasFreshStatus() && healthData?.ready === true &&
        healthData.controller?.running === true && healthData.controller?.fault === false;
}

function canWriteConfig() {
    return controllerReady() && pendingWrites.size === 0;
}

function valveHealth(name) {
    return healthData?.valves?.find(valve => valve.name === name);
}

function displayValves() {
    return statusData?.valves || healthData?.valves?.map(valve => ({ name: valve.name })) || [];
}

function hasUncertainValveState(health) {
    // A normal acknowledged open also sets possibly_open; it is not physical feedback.
    return health?.state === 'unknown' ||
        (health?.possibly_open === true && health.state !== 'open');
}

function valveNeedsAttention(health) {
    return !!(health?.fault || health?.state === 'fault' || hasUncertainValveState(health));
}

function canStartValve(valve) {
    const health = valve && valveHealth(valve.name);
    return canWriteConfig() && health?.state === 'closed' && !valveNeedsAttention(health) &&
        !health.operation && !valve.is_open && !valve.handled;
}

function canQueueValve(valve) {
    const health = valve && valveHealth(valve.name);
    return canWriteConfig() && !!health && !valveNeedsAttention(health) &&
        ['closed', 'open', 'paused', 'waiting'].includes(health.state);
}

function waterflowFresh() {
    const flow = healthData?.monitoring?.waterflow;
    return hasFreshStatus() && flow?.enabled === true && flow.available === true && flow.fresh === true;
}

function sensorFresh(sensor) {
    const health = healthData?.monitoring?.sensors?.find(item => item.name === sensor.name);
    return hasFreshStatus() && !sensor.error && health?.available === true && health.fresh === true;
}

function refreshStatusUI() {
    updateSystemStatus(statusData?.system);
    updateSensorStatus(statusData?.sensors);
    updateWeatherInfo(statusData?.sensors || []);
    updateWaterflowStatus(statusData?.waterflow);
    const valves = displayValves();
    if (currentTab === 'valves' && (statusData || healthData)) {
        const grid = document.getElementById('valves-grid');
        if (grid && valves.length && grid.querySelectorAll('.valve-card').length === valves.length &&
            valves.every(valve => document.getElementById(`valve-${valve.name}`))) {
            updateValves(valves, queueSnapshot);
        } else {
            renderValves(valves, queueSnapshot);
        }
    } else if (currentTab === 'queue' && queueSnapshot) {
        renderQueue(queueSnapshot);
    }
    updateConfigControls();
}

function invalidateStatus(message) {
    statusVersion++;
    statusRequest?.controller.abort();
    statusRequest = null;
    statusFresh = false;
    statusIssue = message;
    refreshStatusUI();
}

function beginWrite(key) {
    pendingWrites.add(key);
    configVersion++;
    sensorsVersion++;
    invalidateStatus('Updating the controller.');
}

async function finishWrite(key) {
    pendingWrites.delete(key);
    invalidateStatus('Refreshing controller status.');
    if (unloading || pendingWrites.size) return;
    await loadStatus({ force: true });
    if (unloading) return;
    if (currentTab === 'config' && !document.getElementById('config-view')?.children.length) {
        await loadConfig();
    } else if (currentTab === 'sensors' && !document.getElementById('sensors-list')?.children.length) {
        await loadSensorsWithConfig();
    }
}

function loadStatus({ force = false } = {}) {
    if (unloading) return Promise.resolve();
    refreshStatusUI();
    if (pendingWrites.size) return Promise.resolve();
    if (statusRequest && !force) return statusRequest.promise;
    statusRequest?.controller.abort();
    const request = { version: ++statusVersion, controller: new AbortController(), startedAt: Date.now() };
    statusRequest = request;
    request.promise = (async () => {
        try {
            const options = { signal: request.controller.signal };
            const [status, queue, health] = await Promise.allSettled([
                apiCall('/api/status', options, true),
                apiCall('/api/queue', options, true),
                apiCall('/api/health', options, true)
            ]);
            if (unloading || request.version !== statusVersion) return;
            const statusOK = status.status === 'fulfilled' && Array.isArray(status.value?.valves);
            const queueOK = queue.status === 'fulfilled' && Array.isArray(queue.value?.jobs);
            const healthOK = health.status === 'fulfilled' &&
                typeof health.value?.ready === 'boolean' &&
                typeof health.value.controller?.running === 'boolean' &&
                typeof health.value.controller?.fault === 'boolean' && Array.isArray(health.value.valves);

            // Partial successes still supply last-known valves for a best-effort Close.
            if (statusOK) statusData = status.value;
            if (queueOK) queueSnapshot = queue.value;
            if (healthOK) healthData = health.value;
            statusFresh = statusOK && queueOK && healthOK;
            if (statusFresh) {
                lastStatusUpdate = request.startedAt;
                statusIssue = '';
            } else {
                const unavailable = [
                    !statusOK && 'Status', !queueOK && 'Queue', !healthOK && 'Health'
                ].filter(Boolean);
                statusIssue = `${unavailable.join(' / ')} unavailable. Showing last known data.`;
            }
            refreshStatusUI();
        } finally {
            if (statusRequest === request) statusRequest = null;
        }
    })();
    return request.promise;
}

function loadNextRuns({ force = false } = {}) {
    if (unloading) return Promise.resolve();
    if (nextRunsRequest && !force) return nextRunsRequest.promise;
    nextRunsRequest?.controller.abort();
    const request = { version: ++nextRunsVersion, controller: new AbortController() };
    nextRunsRequest = request;
    request.promise = (async () => {
        try {
            const data = await apiCall('/api/next-runs', { signal: request.controller.signal }, true);
            if (unloading || request.version !== nextRunsVersion) return;
            nextRunsData = data.next_runs;
            lastNextRunsUpdate = Date.now();
            // Next-run responses may only change next-run labels, never live state or actions.
            displayValves().forEach(updateNextRun);
        } catch (error) {
            if (!unloading && request.version === nextRunsVersion) {
                console.error('Failed to load next runs:', error);
            }
        } finally {
            if (nextRunsRequest === request) nextRunsRequest = null;
        }
    })();
    return request.promise;
}

async function loadConfig() {
    if (scheduleEditors.size || pendingWrites.size) {
        updateConfigControls();
        return;
    }
    const version = ++configVersion;
    try {
        const [config] = await Promise.all([
            apiCall('/api/config'),
            loadStatus()
        ]);
        if (!unloading && version === configVersion && !scheduleEditors.size && !pendingWrites.size) {
            renderConfig(config, displayValves());
        }
    } catch (error) {
        console.error('Failed to load config:', error);
    }
}

function loadQueue() {
    return loadStatus();
}

// ==================== SYSTEM STATUS ====================

function updateSystemStatus(system) {
    const statusPanel = document.getElementById('system-status');
    const indicator = document.querySelector('.status-indicator');
    const statusText = document.querySelector('.status-text');
    
    if (!statusPanel || !indicator || !statusText) return;
    
    const messages = [];
    let severity = 'ok';
    if (!hasFreshStatus()) {
        severity = 'warning';
        messages.push(statusIssue || 'Status is stale. Showing last known data.');
        messages.push('New actions are paused; Close / Stop is still available (best effort).');
    } else if (!controllerReady()) {
        severity = healthData?.controller?.fault ? 'error' : 'warning';
        messages.push(healthData?.controller?.fault ? 'Controller fault.' : 'Controller not ready.');
        if (healthData?.controller?.last_error) messages.push(healthData.controller.last_error);
        messages.push('New actions are paused; Close / Stop is still available (best effort).');
    } else {
        messages.push('Controller ready.');
        if (healthData.valves.some(valveNeedsAttention)) {
            severity = 'error';
            messages.push('A valve needs attention; use Close / Stop.');
        }
        const flowAlarms = healthData.valves.filter(valve => valve.flow_alarm === true).map(valve => valve.name);
        if (flowAlarms.length) {
            if (severity === 'ok') severity = 'warning';
            messages.push(`No-flow warning for ${flowAlarms.join(', ')}.`);
        }
        const monitoring = healthData.monitoring;
        if (monitoring?.waterflow?.enabled && !waterflowFresh()) {
            if (severity === 'ok') severity = 'warning';
            messages.push('Waterflow is unavailable or stale; totals may be incomplete.');
        }
        if (monitoring?.sensors?.some(sensor => sensor.enabled && (!sensor.available || !sensor.fresh))) {
            if (severity === 'ok') severity = 'warning';
            messages.push('Weather / sensor monitoring is unavailable or stale; watering can continue.');
        }
        if (monitoring?.mqtt?.enabled && !monitoring.mqtt.connected) {
            if (severity === 'ok') severity = 'warning';
            messages.push('MQTT is disconnected.');
        }
        const legacyStatus = typeof system?.status === 'string' ? system.status : '';
        if (legacyStatus && legacyStatus !== 'OK') {
            if (severity === 'ok') severity = 'warning';
            messages.push(legacyStatus);
        }
    }
    messages.push('Valve positions are unverified.');
    statusPanel.style.display = 'flex';
    statusPanel.className = `system-status ${severity}`;
    indicator.className = `status-indicator ${severity}`;
    statusText.textContent = messages.join(' ');
    updateDateTimeInfo(system);
}

function updateDateTimeInfo(system) {
    if (!system?.current_time) return;
    
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

function updateSensorStatus(sensors) {
    const sensorPanel = document.getElementById('sensor-status');
    const sensorText = document.getElementById('sensor-text');
    
    if (!sensorPanel || !sensorText) return;
    
    // Check if any sensors have should_disable = true or factor != 1
    let sensorInfo = [];
    let shouldDisable = false;
    
    if (sensors && sensors.length > 0) {
        sensors.forEach(sensor => {
            if (sensor.enabled && sensorFresh(sensor)) {
                // Check should_disable
                if (sensor.should_disable === true) {
                    shouldDisable = true;
                    sensorInfo.push(`${sensor.name}: Disabled`);
                }
                // Check factor != 1
                else if (sensor.factor !== undefined && sensor.factor !== null && sensor.factor !== 1) {
                    sensorInfo.push(`${sensor.name}: ${sensor.factor.toFixed(2)}x`);
                }
            }
        });
    }
    
    // Show panel if we have sensor info to display
    if (sensorInfo.length > 0) {
        sensorPanel.style.display = 'flex';
        
        // Reset classes
        sensorPanel.className = 'sensor-status';
        
        // Add appropriate class
        if (shouldDisable) {
            sensorPanel.classList.add('disabled');
        } else {
            sensorPanel.classList.add('factor-adjusted');
        }
        
        // Set text (show first sensor with info)
        sensorText.textContent = sensorInfo[0];
    } else {
        sensorPanel.style.display = 'none';
    }
}

function updateWeatherInfo(sensors) {
    const uvElement = document.getElementById('uv-index');
    const uvIconElement = document.getElementById('uv-icon');
    const precipElement = document.getElementById('recent-precip');
    
    if (!uvElement || !precipElement || !uvIconElement) return;
    
    // Find OpenWeatherMap sensor and extract telemetry
    const weatherSensor = sensors.find(s =>
        s.type === 'OpenWeatherMap' && s.enabled && s.telemetry && sensorFresh(s)
    );
    
    if (weatherSensor && weatherSensor.telemetry) {
        // Update UV index with color coding and icon
        if (weatherSensor.telemetry.uv !== undefined) {
            const uvValue = weatherSensor.telemetry.uv;
            uvElement.textContent = Math.round(uvValue);
            
            // Remove all UV color classes
            uvElement.className = 'uv-badge';
            
            // Add color class and icon based on UV value
            if (uvValue < 3) {
                uvElement.classList.add('uv-low');
                uvIconElement.textContent = '🌤️';
            } else if (uvValue < 6) {
                uvElement.classList.add('uv-moderate');
                uvIconElement.textContent = '🌞';
            } else if (uvValue < 8) {
                uvElement.classList.add('uv-high');
                uvIconElement.textContent = '☀️';
            } else if (uvValue < 11) {
                uvElement.classList.add('uv-very-high');
                uvIconElement.textContent = '🔆';
            } else {
                uvElement.classList.add('uv-extreme');
                uvIconElement.textContent = '🔅';
            }
        } else {
            uvElement.textContent = '--';
            uvElement.className = 'uv-badge';
            uvIconElement.textContent = '☀️';
        }
        
        // Update recent precipitation
        if (weatherSensor.telemetry.recentPrecip !== undefined) {
            precipElement.textContent = weatherSensor.telemetry.recentPrecip.toFixed(1);
        } else {
            precipElement.textContent = '--';
        }
    } else {
        uvElement.textContent = '--';
        uvElement.className = 'uv-badge';
        uvIconElement.textContent = '☀️';
        precipElement.textContent = '--';
    }
}

function updateWaterflowStatus(waterflow) {
    const waterflowPanel = document.getElementById('waterflow-status');
    const waterflowText = document.getElementById('waterflow-text');
    
    updateWaterflowChart(waterflow);
    if (!waterflowPanel || !waterflowText) return;
    
    // Only show if waterflow is enabled
    if (!waterflow || !waterflow.enabled) {
        waterflowPanel.style.display = 'none';
        return;
    }
    
    waterflowPanel.style.display = 'flex';
    
    // Reset classes
    waterflowPanel.className = 'waterflow-status';

    if (!waterflowFresh() || !Number.isFinite(waterflow.flow_rate_lpm)) {
        waterflowPanel.classList.add('unavailable');
        waterflowText.textContent = 'Flow unavailable / stale · history only';
        return;
    }
    
    // Check if system is in "Leaking" status
    const isLeaking = statusData && statusData.system && 
                      statusData.system.temp_status && 
                      statusData.system.temp_status.includes('Leaking');
    
    if (isLeaking) {
        // Leak detected!
        waterflowPanel.classList.add('leak');
        waterflowText.textContent = `⚠️ LEAK: ${waterflow.flow_rate_lpm} L/min`;
    } else if (waterflow.is_active && waterflow.flow_rate_lpm > 0) {
        // Active flow
        waterflowPanel.classList.add('active');
        waterflowText.textContent = `${waterflow.flow_rate_lpm} L/min`;
    } else {
        // No flow
        waterflowText.textContent = `${waterflow.flow_rate_lpm} L/min`;
    }
    
}

function updateWaterflowChart(waterflow) {
    const historyBar = document.getElementById('waterflow-history-bar');
    const canvas = document.getElementById('waterflow-chart');
    
    if (!historyBar || !canvas) return;
    
    // Only show if waterflow is enabled
    if (!waterflow || !waterflow.enabled) {
        historyBar.style.display = 'none';
        if (window.updateWaterflowData) window.updateWaterflowData(null);
        document.getElementById('waterflow-tooltip')?.classList.remove('visible');
        return;
    }
    
    // Show the bar even if history is empty or not yet populated
    historyBar.style.display = 'block';
    historyBar.classList.toggle('stale', !waterflowFresh());
    canvas.title = waterflowFresh() ? 'Recorded flow history' : 'Historical flow only; live reading unavailable';
    
    // Store waterflow data for tooltip access
    if (window.updateWaterflowData) {
        window.updateWaterflowData(waterflow);
    }
    
    // Setup canvas
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    
    const width = rect.width;
    const height = rect.height;
    const history = waterflow.history || [];
    const barCount = 120;
    const barWidth = width / barCount;
    
    // Clear canvas
    ctx.clearRect(0, 0, width, height);
    
    // Create gradient for background bars (light gray to white, bottom to top)
    const bgGradient = ctx.createLinearGradient(0, height, 0, 0);
    bgGradient.addColorStop(0, '#d0d0d0'); // Light gray at bottom
    bgGradient.addColorStop(0.3, '#f5f5f5'); // Almost white at top
    
    // Find max value for scaling (or use 15 as a reasonable max)
    // History now contains objects with {timestamp, value}
    const values = history.map(item => Number.isFinite(item.value) ? item.value : 0);
    const maxValue = Math.max(15, ...values);
    
    // First pass: Draw gradient background bars for all positions
    ctx.fillStyle = bgGradient;
    for (let i = 0; i < barCount; i++) {
        const x = i * barWidth;
        ctx.fillRect(x, 0, barWidth - 1, height);
    }
    
    // Second pass: Draw colored value bars on top
    for (let i = 0; i < barCount; i++) {
        const historyItem = history[i];
        const value = historyItem ? historyItem.value : 0;
        const x = i * barWidth;
        
        if (value > 0) {
            // For non-zero values, draw colored bar from bottom
            const barHeight = (value / maxValue) * height;
            const y = height - barHeight;
            
            let color;
            if (value <= 7) {
                color = '#81d4fa'; // Light blue
            } else if (value < 15) {
                color = '#ff9800'; // Orange
            } else {
                color = '#f44336'; // Red
            }
            
            ctx.fillStyle = color;
            ctx.fillRect(x, y, barWidth - 1, barHeight);
        }
    }
}

// Setup tooltip for waterflow chart
function setupWaterflowTooltip() {
    const canvas = document.getElementById('waterflow-chart');
    const tooltip = document.getElementById('waterflow-tooltip');
    
    if (!canvas || !tooltip) return;
    
    let currentWaterflowData = null;
    
    // Store waterflow data for tooltip access
    window.updateWaterflowData = function(waterflow) {
        currentWaterflowData = waterflow;
    };
    
    canvas.addEventListener('mousemove', (e) => {
        if (!currentWaterflowData || !currentWaterflowData.history) {
            tooltip.classList.remove('visible');
            return;
        }
        
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const barCount = 120;
        const barWidth = rect.width / barCount;
        const barIndex = Math.floor(x / barWidth);
        
        if (barIndex >= 0 && barIndex < barCount) {
            const history = currentWaterflowData.history;
            const historyItem = history[barIndex];
            
            if (historyItem && Number.isFinite(historyItem.value)) {
                const value = historyItem.value;
                // Use the actual timestamp from the server
                const timestamp = new Date(historyItem.timestamp);
                const timeString = timestamp.toLocaleTimeString('en-US', { 
                    hour: '2-digit', 
                    minute: '2-digit',
                    hour12: false 
                });
                
                // Position tooltip to the left of cursor (so it's visible on far right)
                // Get tooltip width to offset properly
                tooltip.innerHTML = `<strong>${timeString}</strong><br>${value.toFixed(1)} L/min (recorded)`;
                const tooltipWidth = tooltip.offsetWidth || 100; // fallback width
                tooltip.style.left = `${e.clientX - tooltipWidth - 10}px`; // 10px gap from cursor
                tooltip.style.top = `${rect.top - 35}px`;
                tooltip.classList.add('visible');
            } else {
                tooltip.classList.remove('visible');
            }
        } else {
            tooltip.classList.remove('visible');
        }
    });
    
    canvas.addEventListener('mouseleave', () => {
        tooltip.classList.remove('visible');
    });
}

// Initialize tooltip on load
document.addEventListener('DOMContentLoaded', setupWaterflowTooltip);

// ==================== VALVE RENDERING ====================

function valveProgress(valve) {
    if (!Number.isFinite(valve.seconds_duration) || valve.seconds_duration <= 0 ||
        !Number.isFinite(valve.seconds_remain)) return 0;
    return Math.max(0, Math.min(100, valve.seconds_remain / valve.seconds_duration * 100));
}

function valveHasOperation(valve) {
    return !!(valve.handled || valveHealth(valve.name)?.operation);
}

function renderOperationInfo(valve) {
    if (!valveHasOperation(valve)) return '';
    const operation = valveHealth(valve.name)?.operation;
    const label = { manual: 'Manual', queued: 'Queued', scheduled: 'Scheduled' }[operation] || 'Operation';
    const remaining = Number.isFinite(valve.seconds_remain) ? formatTime(valve.seconds_remain) : '—';
    const duration = Number.isFinite(valve.seconds_duration) ? formatTime(valve.seconds_duration) : '—';
    return `
        <span class="valve-info-label">${label}:</span>
        <span class="valve-info-value">${remaining} left / ${duration}${hasFreshStatus() ? '' : ' (last known)'}</span>
    `;
}

function renderDailyTotal(valve) {
    const time = Number.isFinite(valve.seconds_daily) ? formatTime(valve.seconds_daily) : '—';
    const liters = Number.isFinite(valve.liters_daily) ? `${valve.liters_daily.toFixed(1)}L` : '—';
    const partial = valveHealth(valve.name)?.attribution?.complete === false;
    return `⏱️ ${time} <span class="valve-liters">💧 ${liters}${partial ? ' (partial)' : ''}</span>`;
}

function valveStatusClass(valve) {
    const health = valveHealth(valve.name);
    const state = health?.fault || health?.state === 'fault' ? 'fault' :
        hasUncertainValveState(health) ? 'unknown' :
        health?.state || (valve.is_open ? 'open' : 'closed');
    return `${state}${hasFreshStatus() ? '' : ' stale'}`;
}

function renderValveActions(valve) {
    const health = valveHealth(valve.name);
    const needsClose = !controllerReady() || !health || health.state !== 'closed' ||
        health.operation || valveNeedsAttention(health) || valve.is_open || valve.handled;
    const stopping = pendingWrites.has(`stop:${valve.name}`);
    return `
        ${needsClose ? `
            <button id="valve-stop-${valve.name}" class="btn btn-danger btn-small valve-toggle"
                    title="Best-effort Close / Stop; physical closure is not verified"
                    onclick="stopValve('${valve.name}')" ${stopping ? 'disabled' : ''}>
                ${stopping ? 'Sending Close…' : '🔒 Close / Stop'}
            </button>
        ` : `
            <button id="valve-open-${valve.name}" class="btn btn-success btn-small valve-toggle"
                    title="Manual override, up to 30 minutes. Close before restarting."
                    onclick="startValveManual('${valve.name}')" ${canStartValve(valve) ? '' : 'disabled'}>
                🔓 Open
            </button>
        `}
        <button id="valve-queue-${valve.name}" class="btn btn-secondary btn-small"
                onclick="showQueueDialog('${valve.name}')" ${canQueueValve(valve) ? '' : 'disabled'}>
            ⏱️ Queue
        </button>
        ${valve.enabled ? `
            <button class="btn btn-warning btn-small" onclick="disableValve('${valve.name}')"
                    ${canWriteConfig() ? '' : 'disabled'}>🚫 Disable</button>
        ` : `
            <button class="btn btn-success btn-small" onclick="enableValve('${valve.name}')"
                    ${canWriteConfig() ? '' : 'disabled'}>✅ Enable</button>
        `}
        <button class="btn btn-small" onclick="toggleSchedulePanel('${valve.name}')">ℹ️ Information</button>
    `;
}

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
        const displayStatus = getValveStatus(valve);
        const statusClass = valveStatusClass(valve);
        let queueBadge = '';
        
        // If queued, create a separate queue badge
        if (isQueued) {
            const nextPosition = queuedJobs[0].position;
            const queueText = queuedJobs.length > 1 
                ? `Queued #${nextPosition} (+${queuedJobs.length - 1})`
                : `Queued #${nextPosition}`;
            queueBadge = `<span class="valve-status-badge queued">${queueText}</span>`;
        }
        
        const progress = valveProgress(valve);
        
        // Get next scheduled run for this valve
        const nextRun = nextRunsData && nextRunsData[valve.name];
        const nextRunFormatted = nextRun ? formatNextRun(nextRun.schedule_time_iso) : null;
        
        return `
            <div class="valve-card" id="valve-${valve.name}">
                ${valveHasOperation(valve) ? `
                    <div class="valve-progress-top" role="progressbar" aria-label="Time remaining"
                         aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(progress)}">
                        <div class="progress-fill" style="width: ${progress}%"></div>
                    </div>
                ` : ''}
                <div class="valve-header">
                    <h3 class="valve-name">${valve.name}</h3>
                    <div class="valve-header-right">
                        <span class="valve-status-badge ${statusClass}">${displayStatus}</span>
                        ${queueBadge}
                    </div>
                </div>
                
                <div class="valve-info">
                    <div class="valve-info-row operation-row" ${valveHasOperation(valve) ? '' : 'style="display: none;"'}>
                        ${renderOperationInfo(valve)}
                    </div>
                    <div class="valve-info-row">
                        <span class="valve-info-label">Daily Total:</span>
                        <span class="valve-info-value">
                            ${renderDailyTotal(valve)}
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
                                ${!valve.enabled ? 'Disabled' : 'None in 7 days'}
                            </span>
                        </div>
                    `}
                </div>
                
                <div class="valve-actions ${!valve.enabled ? 'valve-disabled' : ''}">
                    ${renderValveActions(valve)}
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
            statusBadge.className = `valve-status-badge ${valveStatusClass(valve)}`;
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
        
        // Handle progress bar for running valves
        let progressBar = card.querySelector('.valve-progress-top');
        
        if (valveHasOperation(valve)) {
            const progress = valveProgress(valve);
            
            // If progress bar doesn't exist, create it at top of card
            if (!progressBar) {
                progressBar = document.createElement('div');
                progressBar.className = 'valve-progress-top';
                progressBar.innerHTML = `<div class="progress-fill" style="width: ${progress}%"></div>`;
                card.insertBefore(progressBar, card.firstChild);
            } else {
                // Update existing progress bar
                const progressFill = progressBar.querySelector('.progress-fill');
                if (progressFill) {
                    progressFill.style.width = `${progress}%`;
                }
            }
            progressBar.setAttribute('role', 'progressbar');
            progressBar.setAttribute('aria-label', 'Time remaining');
            progressBar.setAttribute('aria-valuemin', '0');
            progressBar.setAttribute('aria-valuemax', '100');
            progressBar.setAttribute('aria-valuenow', Math.round(progress));
        } else {
            // Valve not running - remove progress bar if it exists
            if (progressBar) {
                progressBar.remove();
            }
        }

        const operationRow = card.querySelector('.operation-row');
        if (operationRow) {
            operationRow.style.display = valveHasOperation(valve) ? '' : 'none';
            operationRow.innerHTML = renderOperationInfo(valve);
        }
        
        // Update today's stats
        const todayValueSpan = Array.from(card.querySelectorAll('.valve-info-row')).find(row => 
            row.querySelector('.valve-info-label')?.textContent === 'Daily Total:'
        )?.querySelector('.valve-info-value');
        
        if (todayValueSpan) {
            todayValueSpan.innerHTML = renderDailyTotal(valve);
        }
        
        updateNextRun(valve);
        
        // Update button states
        const valveActions = card.querySelector('.valve-actions');
        if (!valveActions) return;
        
        // Update disabled class on valve-actions
        if (valve.enabled) {
            valveActions.classList.remove('valve-disabled');
        } else {
            valveActions.classList.add('valve-disabled');
        }
        
        valveActions.innerHTML = renderValveActions(valve);
    });
}

function updateNextRun(valve) {
    const row = document.getElementById(`valve-${valve.name}`)?.querySelector('.next-run-row');
    const value = row?.querySelector('.valve-info-value');
    if (!value) return;
    const nextRun = nextRunsData?.[valve.name];
    const formatted = nextRun ? formatNextRun(nextRun.schedule_time_iso) : null;
    value.className = `valve-info-value ${formatted ? 'next-run-time' : 'next-run-none'}`;
    value.textContent = formatted ? `📅 ${formatted}` : !valve.enabled ? 'Disabled' : 'None in 7 days';
}

function getValveStatus(valve) {
    const health = valveHealth(valve.name);
    let label;
    if (health?.fault || health?.state === 'fault') label = 'Fault';
    else if (hasUncertainValveState(health)) label = health?.possibly_open ? 'Possibly open' : 'Unknown';
    else if (health?.state === 'paused') label = 'Paused';
    else if (health?.state === 'waiting') label = 'Waiting';
    else if (health?.state === 'open' || valve.is_open) {
        label = health?.operation === 'manual' ? 'Open · Manual' : 'Open';
        if (valveHasOperation(valve) && Number.isFinite(valve.seconds_remain)) {
            label += ` (${formatTime(valve.seconds_remain)} left)`;
        }
    } else label = valve.enabled === false ? 'Schedule disabled' : 'Closed (commanded)';
    return label + (hasFreshStatus() ? '' : ' · last known');
}

function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return '0:00';
    seconds = Math.floor(seconds);
    
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    
    if (hours > 0) {
        return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    } else {
        return `${minutes}:${secs.toString().padStart(2, '0')}`;
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

async function performValveAction(name, action, endpoint, message, type = 'success') {
    const key = `${action}:${name}`;
    if (unloading || pendingWrites.has(key)) return;
    const valve = displayValves().find(item => item.name === name);
    const allowed = valve && (action === 'stop' || (action === 'manual' ? canStartValve(valve) :
        action === 'queue' ? canQueueValve(valve) : canWriteConfig()));
    if (!allowed) {
        showToast('This action is unavailable. Check controller status; use Close / Stop if needed.', 'error');
        refreshStatusUI();
        return;
    }
    beginWrite(key);
    try {
        await apiMutation(endpoint, { method: 'POST' });
        showToast(message, type);
        if (action === 'enable' || action === 'disable') {
            await loadNextRuns({ force: true });
        }
    } catch (error) {
        console.error(`Failed to ${action} valve:`, error);
    } finally {
        await finishWrite(key);
    }
}

function startValveManual(name, duration) {
    let query = '';
    if (duration !== undefined) {
        const minutes = Number(duration);
        if (!Number.isFinite(minutes) || minutes <= 0) {
            showToast('Enter a finite duration greater than zero.', 'error');
            return;
        }
        query = `?duration_minutes=${Math.min(minutes, 30)}`;
    }
    return performValveAction(name, 'manual', `/api/valves/${encodeURIComponent(name)}/start-manual${query}`,
        `Manual watering accepted for ${name} (up to 30 minutes)`);
}

function queueValve(name, duration) {
    const minutes = Number(duration);
    if (!Number.isFinite(minutes) || minutes <= 0) {
        showToast('Enter a finite duration greater than zero.', 'error');
        return;
    }
    return performValveAction(name, 'queue',
        `/api/valves/${encodeURIComponent(name)}/queue?duration_minutes=${minutes}`,
        `Valve ${name} queued for ${minutes} minutes`);
}

function stopValve(name) {
    return performValveAction(name, 'stop', `/api/valves/${encodeURIComponent(name)}/stop`,
        `Close / Stop accepted for ${name}; physical position is unverified`);
}

function enableValve(name) {
    return performValveAction(name, 'enable', `/api/valves/${encodeURIComponent(name)}/enable`,
        `Valve ${name} enabled`);
}

function disableValve(name) {
    return performValveAction(name, 'disable', `/api/valves/${encodeURIComponent(name)}/disable`,
        `Valve ${name} disabled`, 'warning');
}

function showQueueDialog(name) {
    if (!canQueueValve(displayValves().find(valve => valve.name === name))) {
        refreshStatusUI();
        return;
    }
    const duration = prompt(`Queue ${name} for how many minutes?`, '15');
    if (duration !== null) return queueValve(name, duration);
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
        
        // Get config to fetch irregular_flow_threshold
        const config = await apiCall('/api/config');
        const threshold = config.alerts?.irregular_flow_threshold || 2.0;
        
        // Build metrics section
        let metricsHTML = '';
        if (details.baseline_lpm !== null && details.baseline_lpm !== undefined) {
            const alertRange = (details.baseline_std_dev * threshold).toFixed(2);
            
            const trendText = details.baseline_trend !== null 
                ? `${details.baseline_trend > 0 ? '+' : ''}${details.baseline_trend.toFixed(2)}% per month`
                : 'N/A (need 14+ samples)';
            const trendColor = details.baseline_trend !== null 
                ? (Math.abs(details.baseline_trend) > 5 ? '#ff9800' : '#4CAF50')
                : '#999';
            
            metricsHTML = `
                <div class="info-section">
                    <h4 class="info-section-title">📊 Performance Metrics</h4>
                    <div class="metrics-grid">
                        <div class="metric-item">
                            <span class="metric-label">Baseline Flow Rate:</span>
                            <span class="metric-value">${details.baseline_lpm.toFixed(2)} L/min</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">Std Dev:</span>
                            <span class="metric-value">±${details.baseline_std_dev.toFixed(2)} L/min (alert at ±${alertRange})</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">Data Points:</span>
                            <span class="metric-value">${details.baseline_sample_count} days</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">Flow Trend:</span>
                            <span class="metric-value" style="color: ${trendColor}">${trendText}</span>
                        </div>
                    </div>
                </div>
            `;
        } else if (details.baseline_sample_count > 0) {
            metricsHTML = `
                <div class="info-section">
                    <h4 class="info-section-title">📊 Performance Metrics</h4>
                    <div class="metrics-collecting">
                        <p>⏳ Collecting data (${details.baseline_sample_count}/10 days)</p>
                        <p class="metrics-note">Metrics will be available after 10 days of operation</p>
                    </div>
                </div>
            `;
        } else {
            metricsHTML = `
                <div class="info-section">
                    <h4 class="info-section-title">📊 Performance Metrics</h4>
                    <div class="metrics-collecting">
                        <p>⏳ No data yet</p>
                        <p class="metrics-note">Metrics will appear after the valve operates</p>
                    </div>
                </div>
            `;
        }
        
        // Build schedules section
        let schedulesHTML = '';
        if (!details.schedules || details.schedules.length === 0) {
            schedulesHTML = `
                <div class="schedule-panel-empty">
                    <p>No schedules configured for this valve</p>
                </div>
            `;
        } else {
            schedulesHTML = `
                <div class="info-section">
                    <h4 class="info-section-title">📅 Schedules</h4>
                    <div class="schedules-list">
            `;
            
            schedulesHTML += details.schedules.map((s, i) => {
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
            
            schedulesHTML += `
                    </div>
                </div>
            `;
        }
        
        // Combine schedules and metrics (schedules first)
        panel.innerHTML = schedulesHTML + metricsHTML;
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

async function loadSensorsWithConfig() {
    if (pendingWrites.size) return;
    const version = ++sensorsVersion;
    try {
        const [configData] = await Promise.all([
            apiCall('/api/config'),
            loadStatus()
        ]);
        if (!unloading && version === sensorsVersion && !pendingWrites.size) {
            renderSensors(statusData?.sensors, configData.sensors);
        }
    } catch (error) {
        console.error('Failed to load sensors:', error);
    }
}

function renderSensors(sensors, sensorConfigs = []) {
    const list = document.getElementById('sensors-list');
    if (!list) return;
    
    // Remove loading message
    const loading = list.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    let html = '';
    
    // Add regular sensors
    if (!sensors || sensors.length === 0) {
        list.innerHTML = '<p class="text-center">No sensors configured</p>';
        return;
    } else {
        html += sensors.map(sensor => {
        const telemetry = sensor.telemetry || {};
        const hasError = !sensorFresh(sensor);
        
        // Find matching config
        const sensorConfig = sensorConfigs.find(c => c.name === sensor.name) || {};
        
        return `
            <div class="sensor-card">
                <div class="sensor-header">
                    <h3 class="sensor-name">${sensor.name}</h3>
                    <span class="sensor-type">${sensor.type}</span>
                </div>
                
                ${hasError ? `
                    <div style="color: var(--danger); padding: 1rem; background: #ffebee; border-radius: var(--radius);">
                        ⚠️ Sensor data is unavailable or stale
                    </div>
                ` : `
                    <div class="sensor-telemetry">
                        ${telemetry.uv_index !== undefined ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">UV Index</div>
                                <div class="telemetry-value">${telemetry.uv_index.toFixed(1)}</div>
                            </div>
                        ` : ''}
                        
                        ${telemetry.recentPrecip !== undefined ? `
                            <div class="telemetry-item">
                                <div class="telemetry-label">Recent Precipitation</div>
                                <div class="telemetry-value">${telemetry.recentPrecip.toFixed(1)} mm</div>
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
                
                ${sensorConfig.precipitation ? `
                    <div class="sensor-config" style="margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--border);">
                        <h4 style="margin: 0 0 0.5rem 0; font-size: 0.9rem; color: var(--text-secondary);">⚙️ Settings</h4>
                        <div class="form-group" style="margin-bottom: 0.75rem;">
                            <label for="sensor-${sensor.name}-days" style="font-size: 0.85rem;">Days to Aggregate</label>
                            <input type="number" 
                                   id="sensor-${sensor.name}-days" 
                                   value="${sensorConfig.precipitation.days_to_aggregate}" 
                                   min="1" 
                                   max="7" 
                                   step="1"
                                   data-config-write data-confirmed-value="${sensorConfig.precipitation.days_to_aggregate}"
                                   style="width: 100%; padding: 0.4rem;"
                                   onchange="updateSensorSetting('${sensor.name}', 'precip_days', this.value, this)">
                            <small style="font-size: 0.75rem;">Past days to sum precipitation (1-7)</small>
                        </div>
                        <div class="form-group">
                            <label for="sensor-${sensor.name}-threshold" style="font-size: 0.85rem;">Disable Threshold (mm)</label>
                            <input type="number" 
                                   id="sensor-${sensor.name}-threshold" 
                                   value="${sensorConfig.precipitation.disable_threshold_mm}" 
                                   min="0.1" 
                                   max="50" 
                                   step="0.1"
                                   data-config-write data-confirmed-value="${sensorConfig.precipitation.disable_threshold_mm}"
                                   style="width: 100%; padding: 0.4rem;"
                                   onchange="updateSensorSetting('${sensor.name}', 'precip_threshold', this.value, this)">
                            <small style="font-size: 0.75rem;">Skip irrigation if total exceeds this (0.1-50 mm)</small>
                        </div>
                    </div>
                ` : ''}
            </div>
        `;
        }).join('');
    }
    
    list.innerHTML = html;
    updateConfigControls();
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

function renderWaterflowConfig(config) {
    const waterflow = config.waterflow || {};
    
    if (!waterflow || Object.keys(waterflow).length === 0) {
        return '<p class="config-description">No waterflow sensor configured</p>';
    }
    
    return `
        <div class="config-grid" style="margin-bottom: 1.5rem;">
            <div class="config-item">
                <div class="config-label">TYPE</div>
                <div class="config-value">${waterflow.type || 'Unknown'}</div>
            </div>
        </div>
        
        <div class="alerts-toggle-list">
            <div class="alert-toggle-item">
                <div class="alert-toggle-header">
                    <span class="alert-icon">⚡</span>
                    <div class="alert-info">
                        <div class="alert-label">Sensor Enabled</div>
                        <div class="alert-description">⚠️ Requires system restart to take effect</div>
                    </div>
                    <label class="toggle-switch">
                        <input type="checkbox" 
                               id="waterflow-enabled" data-config-write data-confirmed-value="${!!waterflow.enabled}"
                               ${waterflow.enabled ? 'checked' : ''} 
                               onchange="updateWaterflowSetting('enabled', this.checked, this)">
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            </div>
            
            <div class="alert-toggle-item">
                <div class="alert-toggle-header">
                    <span class="alert-icon">💧</span>
                    <div class="alert-info">
                        <div class="alert-label">Leak Detection</div>
                        <div class="alert-description">Detect leaks when all valves are closed</div>
                    </div>
                    <label class="toggle-switch">
                        <input type="checkbox" 
                               id="waterflow-leak_detection" data-config-write data-confirmed-value="${!!waterflow.leak_detection}"
                               ${waterflow.leak_detection ? 'checked' : ''} 
                               onchange="updateWaterflowSetting('leak_detection', this.checked, this)">
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            </div>
        </div>
    `;
}

function renderAlertsConfig(config) {
    const alerts = config.alerts || {};
    const enabled = alerts.enabled || {};
    
    const alertTypes = [
        { 
            key: 'leak', 
            label: 'Leak Detection', 
            icon: '💧', 
            description: 'Flow detected when all valves are closed',
            setting: {
                id: 'leak-repeat-minutes',
                label: 'Alert Repeat Interval (minutes)',
                value: alerts.leak_repeat_minutes || 15,
                min: 1,
                max: 60,
                step: 1,
                configKey: 'leak_repeat_minutes',
                help: 'How often to repeat leak alerts while leak persists'
            }
        },
        { 
            key: 'malfunction_no_flow', 
            label: 'Malfunction (No Flow)', 
            icon: '🚫', 
            description: 'Valve open but no water flow detected'
        },
        { 
            key: 'irregular_flow', 
            label: 'Irregular Flow', 
            icon: '📊', 
            description: 'Flow rate outside baseline range',
            setting: {
                id: 'irregular-flow-threshold',
                label: 'Threshold (standard deviations)',
                value: alerts.irregular_flow_threshold || 2.0,
                min: 0.5,
                max: 5.0,
                step: 0.1,
                configKey: 'irregular_flow_threshold',
                help: 'Number of standard deviations from baseline to trigger alert'
            }
        },
        { 
            key: 'sensor_error', 
            label: 'Sensor Error', 
            icon: '⚠️', 
            description: 'Sensor failures and communication issues'
        },
        { 
            key: 'system_exit', 
            label: 'System Exit', 
            icon: '🛑', 
            description: 'System shutdown or termination'
        }
    ];
    
    return `
        <div class="alerts-toggle-list">
            ${alertTypes.map(alert => `
                <div class="alert-toggle-item">
                    <div class="alert-toggle-header">
                        <span class="alert-icon">${alert.icon}</span>
                        <div class="alert-info">
                            <div class="alert-label">${alert.label}</div>
                            <div class="alert-description">${alert.description}</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" 
                                   id="alert-${alert.key}" 
                                   data-config-write data-confirmed-value="${!!enabled[alert.key]}"
                                   ${enabled[alert.key] ? 'checked' : ''}
                                   onchange="toggleAlert('${alert.key}', this.checked, this)">
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    ${alert.setting ? `
                        <div class="alert-setting" id="alert-setting-${alert.key}" style="display: ${enabled[alert.key] ? 'block' : 'none'};">
                            <div class="form-group">
                                <label for="${alert.setting.id}">${alert.setting.label}</label>
                                <input type="number" 
                                       id="${alert.setting.id}" 
                                       value="${alert.setting.value}" 
                                       min="${alert.setting.min}" 
                                       max="${alert.setting.max}" 
                                       step="${alert.setting.step}"
                                       data-config-write data-confirmed-value="${alert.setting.value}"
                                       onchange="updateAlertSetting('${alert.setting.configKey}', this.value, this)">
                                <small>${alert.setting.help}</small>
                            </div>
                        </div>
                    ` : ''}
                </div>
            `).join('')}
        </div>
    `;
}

function renderConfig(config, valves) {
    const view = document.getElementById('config-view');
    if (!view) return;
    
    // Remove loading message
    const loading = view.parentElement.querySelector('.loading');
    if (loading) loading.remove();
    
    view.innerHTML = `
        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('schedules')">
                <h3>📅 Schedules</h3>
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
                <h3>🧪 Schedule Simulator</h3>
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

        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('alerts')">
                <h3>🚨 Alert Settings</h3>
                <span class="collapse-icon">▶</span>
            </div>
            <div class="config-section-content" id="alerts" style="display: none;">
                <div class="alerts-config">
                    <p class="config-description">Configure which alerts are enabled and their settings</p>
                    ${renderAlertsConfig(config)}
                </div>
            </div>
        </div>

        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('waterflow-config')">
                <h3>💧 Waterflow Sensor</h3>
                <span class="collapse-icon">▶</span>
            </div>
            <div class="config-section-content" id="waterflow-config" style="display: none;">
                <div class="waterflow-config">
                    <p class="config-description">Configure waterflow sensor settings</p>
                    ${renderWaterflowConfig(config)}
                </div>
            </div>
        </div>

        <div class="config-section collapsible">
            <div class="config-section-header" onclick="toggleConfigSection('system-config')">
                <h3>⚙️ System Configuration</h3>
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
    updateConfigControls();
}

function renderValveSchedules(valves) {
    if (!valves || valves.length === 0) {
        return '<p>No valves configured</p>';
    }
    
    return valves.map(valve => `
        <div class="valve-schedule-section">
            <div class="valve-schedule-header">
                <h4>${valve.name}</h4>
                <button class="btn btn-primary btn-small" data-schedule-add="${valve.name}" onclick="addSchedule('${valve.name}')">
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
    const version = (scheduleLoadVersions.get(valveName) || 0) + 1;
    scheduleLoadVersions.set(valveName, version);
    try {
        const valve = await apiCall(`/api/valves/${encodeURIComponent(valveName)}`);
        if (unloading || scheduleLoadVersions.get(valveName) !== version) return;
        savedSchedules.set(valveName, valve.schedules || []);
        renderSavedSchedules(valveName);
    } catch (error) {
        console.error('Failed to load schedules:', error);
        const container = document.getElementById(`schedules-${valveName}`);
        if (!unloading && container && scheduleLoadVersions.get(valveName) === version &&
            !scheduleEditors.has(valveName) && !savedSchedules.has(valveName)) {
            container.innerHTML = '<p class="error">Failed to load schedules</p>';
        }
    }
}

function renderSavedSchedules(valveName) {
    const container = document.getElementById(`schedules-${valveName}`);
    const schedules = savedSchedules.get(valveName);
    if (!container || !schedules || scheduleEditors.has(valveName)) return;
    container.innerHTML = schedules.length ? schedules.map((sched, idx) => `
        <div class="schedule-item" id="schedule-${valveName}-${idx}">
            <div class="schedule-display" id="schedule-display-${valveName}-${idx}">
                ${renderScheduleDisplay(sched, valveName, idx)}
            </div>
            <div class="schedule-edit" id="schedule-edit-${valveName}-${idx}" style="display: none;">
                ${renderScheduleEditor(sched, valveName, idx)}
            </div>
        </div>
    `).join('') : '<p class="no-schedules">No schedules configured</p>';
    updateConfigControls();
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
            <button class="btn btn-secondary btn-small" data-schedule-edit="${valveName}" onclick="editSchedule('${valveName}', ${idx})">
                ✏️ Edit
            </button>
            <button class="btn btn-danger btn-small" data-config-write data-schedule-delete="${valveName}" onclick="deleteSchedule('${valveName}', ${idx})">
                🗑️ Delete
            </button>
        </div>
    `;
}

function renderScheduleEditor(sched, valveName, idx) {
    const isNew = idx === -1;
    return `
        <form class="schedule-form" onsubmit="saveSchedule(event, '${valveName}', ${idx})">
            ${isNew ? '<p class="schedule-draft-note">New schedule · not saved</p>' : ''}
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
                       value="${sched.fixed_start_time ?? '06:00'}" ${sched.time_based_on === 'fixed' ? 'required' : ''}>
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
                       value="${sched.duration ?? 10}" min="0" step="any" required>
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
                <button type="submit" class="btn btn-success btn-small" data-config-write>
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
    document.getElementById(`fixed_start_time-${valveName}-${idx}`).required = timeBasedOn === 'fixed';
    
    if (timeBasedOn === 'fixed') {
        fixedGroup.style.display = '';
        offsetGroup.style.display = 'none';
    } else {
        fixedGroup.style.display = 'none';
        offsetGroup.style.display = '';
    }
}

function addSchedule(valveName) {
    const container = document.getElementById(`schedules-${valveName}`);
    if (!container || pendingSchedules.has(valveName)) return;
    if (scheduleEditors.has(valveName)) {
        document.getElementById(`schedule-edit-${valveName}-${scheduleEditors.get(valveName)}`)?.querySelector('input')?.focus();
        return;
    }
    const draft = document.createElement('div');
    draft.id = `schedule-${valveName}--1`;
    draft.className = 'schedule-item schedule-draft';
    draft.innerHTML = `
        <div class="schedule-edit" id="schedule-edit-${valveName}--1">
            ${renderScheduleEditor({
                time_based_on: 'fixed', fixed_start_time: '06:00', duration: 10,
                days: [], seasons: [], enable_uv_adjustments: false
            }, valveName, -1)}
        </div>
    `;
    scheduleEditors.set(valveName, -1);
    container.querySelector('.no-schedules')?.remove();
    container.appendChild(draft);
    updateConfigControls();
}

function editSchedule(valveName, idx) {
    if (pendingSchedules.has(valveName) || scheduleEditors.has(valveName)) return;
    const displayEl = document.getElementById(`schedule-display-${valveName}-${idx}`);
    const editEl = document.getElementById(`schedule-edit-${valveName}-${idx}`);
    
    if (displayEl && editEl) {
        scheduleEditors.set(valveName, idx);
        displayEl.style.display = 'none';
        editEl.style.display = 'block';
        updateConfigControls();
    }
}

function cancelEditSchedule(valveName, idx) {
    if (pendingSchedules.has(valveName) || scheduleEditors.get(valveName) !== idx) return;
    scheduleEditors.delete(valveName);
    if (idx === -1) {
        document.getElementById(`schedule-${valveName}--1`)?.remove();
    }
    renderSavedSchedules(valveName);
    updateConfigControls();
}

function setSchedulePending(form, pending) {
    form.dataset.pending = String(pending);
    form.querySelectorAll('input, select, button').forEach(control => {
        control.disabled = pending;
    });
    updateConfigControls();
}

async function saveSchedule(event, valveName, idx) {
    event.preventDefault();
    const scheduleForm = event.currentTarget || event.target;
    if (pendingSchedules.has(valveName) || scheduleForm.dataset.saved === 'true' ||
        scheduleEditors.get(valveName) !== idx) return;
    if (!canWriteConfig()) {
        showToast('Wait for fresh, ready controller status before saving. Your edits are kept.', 'error');
        refreshStatusUI();
        return;
    }
    const timeBasedOn = document.getElementById(`time_based_on-${valveName}-${idx}`).value;
    const duration = Number(document.getElementById(`duration-${valveName}-${idx}`).value);
    const fixedTime = document.getElementById(`fixed_start_time-${valveName}-${idx}`).value.trim();
    if (!Number.isFinite(duration) || duration <= 0) {
        showToast('Enter a finite duration greater than zero.', 'error');
        return;
    }
    if (timeBasedOn === 'fixed' && !fixedTime) {
        showToast('Choose a start time for this fixed-time schedule.', 'error');
        return;
    }
    const scheduleData = {
        time_based_on: timeBasedOn,
        duration,
        enable_uv_adjustments: document.getElementById(`enable_uv_adjustments-${valveName}-${idx}`).checked,
        days: Array.from(scheduleForm.querySelectorAll('.day-checkbox:checked')).map(cb => cb.value),
        seasons: Array.from(scheduleForm.querySelectorAll('.season-checkbox:checked')).map(cb => cb.value)
    };
    if (timeBasedOn === 'fixed') scheduleData.fixed_start_time = fixedTime;
    else scheduleData.offset_minutes = parseInt(document.getElementById(`offset_minutes-${valveName}-${idx}`).value) || 0;

    const key = `schedule:${valveName}`;
    pendingSchedules.add(valveName);
    setSchedulePending(scheduleForm, true);
    scheduleLoadVersions.set(valveName, (scheduleLoadVersions.get(valveName) || 0) + 1);
    beginWrite(key);
    try {
        const endpoint = `/api/valves/${encodeURIComponent(valveName)}/schedules`;
        const result = await apiMutation(idx === -1 ? endpoint : `${endpoint}/${idx}`, {
            method: idx === -1 ? 'POST' : 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(scheduleData)
        });
        scheduleForm.dataset.saved = 'true';
        scheduleEditors.delete(valveName);
        const schedules = [...(savedSchedules.get(valveName) || [])];
        const savedIndex = idx === -1 ? result.schedule_index : idx;
        if (Number.isInteger(savedIndex) && savedIndex >= 0) schedules[savedIndex] = scheduleData;
        savedSchedules.set(valveName, schedules);
        renderSavedSchedules(valveName);
        showToast(`Schedule ${idx === -1 ? 'added to' : 'updated for'} ${valveName}`, 'success');
        refreshOpenSchedulePanels();
        await Promise.all([loadValveSchedules(valveName), loadNextRuns({ force: true })]);
    } catch (error) {
        console.error('Failed to save schedule:', error);
    } finally {
        pendingSchedules.delete(valveName);
        setSchedulePending(scheduleForm, false);
        await finishWrite(key);
        updateConfigControls();
    }
}

async function deleteSchedule(valveName, idx) {
    if (idx < 0 || pendingSchedules.has(valveName) || scheduleEditors.has(valveName)) return;
    if (!canWriteConfig()) {
        showToast('Wait for fresh, ready controller status before deleting.', 'error');
        refreshStatusUI();
        return;
    }
    if (savedSchedules.get(valveName)?.length <= 1) {
        showToast('A valve must keep at least one schedule.', 'error');
        return;
    }
    if (!confirm(`Are you sure you want to delete this schedule for ${valveName}?`)) {
        return;
    }
    
    const key = `schedule:${valveName}`;
    pendingSchedules.add(valveName);
    scheduleLoadVersions.set(valveName, (scheduleLoadVersions.get(valveName) || 0) + 1);
    beginWrite(key);
    try {
        await apiMutation(`/api/valves/${encodeURIComponent(valveName)}/schedules/${idx}`, {
            method: 'DELETE'
        });
        const schedules = [...(savedSchedules.get(valveName) || [])];
        schedules.splice(idx, 1);
        savedSchedules.set(valveName, schedules);
        renderSavedSchedules(valveName);
        showToast(`Schedule deleted from ${valveName}`, 'success');
        refreshOpenSchedulePanels();
        await Promise.all([loadValveSchedules(valveName), loadNextRuns({ force: true })]);
    } catch (error) {
        console.error('Failed to delete schedule:', error);
    } finally {
        pendingSchedules.delete(valveName);
        await finishWrite(key);
        updateConfigControls();
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

function updateConfigControls() {
    document.querySelectorAll('[data-config-write]').forEach(control => {
        const valveName = control.dataset.scheduleDelete;
        const scheduleBusy = valveName && (pendingSchedules.has(valveName) ||
            scheduleEditors.has(valveName) || savedSchedules.get(valveName)?.length <= 1);
        control.disabled = !canWriteConfig() || control.dataset.pending === 'true' ||
            control.closest('.schedule-form')?.dataset.pending === 'true' || !!scheduleBusy;
    });
    document.querySelectorAll('[data-schedule-add], [data-schedule-edit]').forEach(control => {
        const valveName = control.dataset.scheduleAdd || control.dataset.scheduleEdit;
        control.disabled = pendingSchedules.has(valveName) || scheduleEditors.has(valveName);
    });
}

async function persistSetting(control, key, endpoint, payload, message, onSuccess) {
    if (!control) return;
    const checkbox = control.type === 'checkbox';
    const setValue = value => {
        if (checkbox) control.checked = value;
        else control.value = String(value);
    };
    const confirmed = control.dataset.confirmedValue === undefined ?
        (checkbox ? control.defaultChecked : control.defaultValue) :
        (checkbox ? control.dataset.confirmedValue === 'true' : control.dataset.confirmedValue);
    if (pendingSettings.has(key)) {
        setValue(pendingSettings.get(key));
        return;
    }
    if (!canWriteConfig()) {
        setValue(confirmed);
        showToast('Wait for fresh, ready controller status before changing settings.', 'error');
        refreshStatusUI();
        return;
    }
    if (!checkbox && (!String(control.value).trim() || !Number.isFinite(payload.value) ||
        (control.checkValidity && !control.checkValidity()))) {
        setValue(confirmed);
        showToast('Enter a valid number for this setting.', 'error');
        return;
    }
    pendingSettings.set(key, payload.enabled ?? payload.value);
    control.dataset.pending = 'true';
    beginWrite(key);
    try {
        const result = await apiMutation(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const confirmedValue = payload.enabled !== undefined ?
            (result.enabled ?? payload.enabled) : (result.value ?? payload.value);
        control.dataset.confirmedValue = String(confirmedValue);
        setValue(confirmedValue);
        if (onSuccess) onSuccess(confirmedValue);
        showToast(message, 'success');
    } catch (error) {
        setValue(confirmed);
        console.error('Failed to update setting:', error);
    } finally {
        pendingSettings.delete(key);
        control.dataset.pending = 'false';
        await finishWrite(key);
        updateConfigControls();
    }
}

function toggleAlert(alertType, enabled, control = document.getElementById(`alert-${alertType}`)) {
    return persistSetting(control, `alert:${alertType}`, '/api/config/alerts/enabled',
        { alert_type: alertType, enabled },
        `Alert "${alertType}" ${enabled ? 'enabled' : 'disabled'}`, confirmed => {
            const settingDiv = document.getElementById(`alert-setting-${alertType}`);
            if (settingDiv) settingDiv.style.display = confirmed ? 'block' : 'none';
        });
}

function updateAlertSetting(setting, value, control) {
    return persistSetting(control, `alert-setting:${setting}`, '/api/config/alerts/settings',
        { setting, value: Number(value) }, 'Alert setting updated');
}

function updateWaterflowSetting(setting, value, control = document.getElementById(`waterflow-${setting}`)) {
    return persistSetting(control, `waterflow:${setting}`, '/api/config/waterflow', { setting, value },
        setting === 'enabled' ? 'Waterflow sensor updated (restart required)' : 'Waterflow setting updated');
}

function updateSensorSetting(sensorName, setting, value, control) {
    return persistSetting(control, `sensor:${sensorName}:${setting}`,
        `/api/config/sensors/${encodeURIComponent(sensorName)}`,
        { setting, value: Number(value) }, 'Sensor setting updated');
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
        const result = await apiCall(`/api/simulate?${params.toString()}`, {
            method: 'POST'
        }, false, 'text');
        
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
    if (unloading) return;
    const toast = document.getElementById('toast');
    if (!toast) return;
    
    toast.textContent = message;
    toast.className = `toast ${type} show`;
    
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// ==================== CLEANUP ====================

window.addEventListener('beforeunload', () => {
    unloading = true;
    clearInterval(refreshInterval);
    clearInterval(nextRunsInterval);
    clearTimeout(toastTimeout);
    statusVersion++;
    nextRunsVersion++;
    statusRequest?.controller.abort();
    nextRunsRequest?.controller.abort();
    for (const [controller, timeout] of activeRequests) {
        clearTimeout(timeout);
        controller.abort();
    }
});
