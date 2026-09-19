const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'static', 'js', 'app.js'), 'utf8');
const index = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const healthyOpen = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'ui', 'healthy-open.json'), 'utf8'));
const copy = value => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function response(data, status = 200) {
    const snapshot = copy(data);
    return {
        ok: status >= 200 && status < 300,
        status,
        statusText: status === 200 ? 'OK' : 'Unavailable',
        json: async () => copy(snapshot),
        text: async () => String(snapshot)
    };
}

function matches(element, selector) {
    const parts = selector.trim().split(/\s+/);
    const compound = parts.pop();
    let restExcluded = false;
    let rest = compound.replace(/:not\(([^)]+)\)/g, (_, excluded) => {
        if (matches(element, excluded)) restExcluded = true;
        return '';
    });
    // Only the selectors used by the real application are needed by this offline DOM.
    if (restExcluded) return false;
    const attributes = [...rest.matchAll(/\[([\w-]+)(?:([*]?=)["']?([^\]"']*)["']?)?\]/g)];
    rest = rest.replace(/\[[^\]]+\]/g, '');
    for (const [, key, operator, value] of attributes) {
        const actual = element.getAttribute(key);
        if (actual === null || (operator === '=' && actual !== value) ||
            (operator === '*=' && !actual.includes(value))) return false;
    }
    if (rest.includes(':checked') && !element.checked) return false;
    if (rest.includes(':disabled') && !element.disabled) return false;
    rest = rest.replace(/:checked|:disabled/g, '');
    const tag = rest.match(/^[\w-]+/);
    if (tag && element.tagName.toLowerCase() !== tag[0].toLowerCase()) return false;
    const id = rest.match(/#([\w-]+)/);
    if (id && element.id !== id[1]) return false;
    for (const [, name] of rest.matchAll(/\.([\w-]+)/g)) {
        if (!element.classList.contains(name)) return false;
    }
    if (!parts.length) return true;
    let parent = element.parentElement;
    while (parent) {
        if (matches(parent, parts.join(' '))) return true;
        parent = parent.parentElement;
    }
    return false;
}

class Element {
    constructor(tagName) {
        this.tagName = tagName.toUpperCase();
        this.attributes = new Map();
        this.childNodes = [];
        this.parentElement = null;
        this.dataset = {};
        this.style = {};
        this.listeners = new Map();
        this.checked = false;
        this.defaultChecked = false;
        this.defaultValue = '';
        this.disabled = false;
        this.required = false;
    }
    get children() { return this.childNodes.filter(child => child instanceof Element); }
    get firstChild() { return this.childNodes[0] || null; }
    get previousElementSibling() {
        const children = this.parentElement?.children || [];
        return children[children.indexOf(this) - 1] || null;
    }
    get id() { return this.attributes.get('id') || ''; }
    set id(value) { this.attributes.set('id', value); }
    get className() { return this.attributes.get('class') || ''; }
    set className(value) { this.attributes.set('class', value); }
    get type() { return this.attributes.get('type') || ''; }
    get value() {
        if (this.tagName === 'SELECT' && this._value === undefined) {
            return (this.children.find(child => child.selected) || this.children[0])?.value || '';
        }
        return this._value ?? this.attributes.get('value') ?? '';
    }
    set value(value) { this._value = String(value); }
    get textContent() {
        return this.childNodes.map(child => child instanceof Element ? child.textContent : child).join('');
    }
    set textContent(value) {
        this.children.forEach(child => { child.parentElement = null; });
        this.childNodes = [String(value)];
    }
    get innerHTML() {
        return this.childNodes.map(child => child instanceof Element ? child.outerHTML : child).join('');
    }
    set innerHTML(html) {
        this.children.forEach(child => { child.parentElement = null; });
        this.childNodes = [];
        parseHTML(String(html), this);
    }
    get outerHTML() {
        return `<${this.tagName.toLowerCase()}${[...this.attributes].map(([key, value]) =>
            ` ${key}="${value}"`).join('')}>${this.innerHTML}</${this.tagName.toLowerCase()}>`;
    }
    get classList() {
        const values = () => new Set(this.className.split(/\s+/).filter(Boolean));
        return {
            contains: name => values().has(name),
            add: (...names) => { this.className = [...new Set([...values(), ...names])].join(' '); },
            remove: (...names) => { this.className = [...values()].filter(name => !names.includes(name)).join(' '); },
            toggle: (name, force) => {
                const set = values();
                const add = force === undefined ? !set.has(name) : force;
                if (add) set.add(name); else set.delete(name);
                this.className = [...set].join(' ');
                return add;
            }
        };
    }
    setAttribute(key, value = '') {
        value = String(value);
        this.attributes.set(key, value);
        if (key.startsWith('data-')) this.dataset[dataKey(key)] = value;
        if (key === 'value') { this._value = value; this.defaultValue = value; }
        if (key === 'checked') { this.checked = true; this.defaultChecked = true; }
        if (key === 'selected') this.selected = true;
        if (key === 'disabled') this.disabled = true;
        if (key === 'required') this.required = true;
        if (key === 'style') {
            for (const rule of value.split(';')) {
                const [name, content] = rule.split(':');
                if (name?.trim() && content !== undefined) {
                    this.style[name.trim().replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = content.trim();
                }
            }
        }
    }
    getAttribute(key) {
        if (key.startsWith('data-')) return this.dataset[dataKey(key)] ?? null;
        return this.attributes.get(key) ?? null;
    }
    appendChild(child) {
        child.remove();
        child.parentElement = this;
        this.childNodes.push(child);
        return child;
    }
    insertBefore(child, before) {
        child.remove();
        child.parentElement = this;
        const index = this.childNodes.indexOf(before);
        this.childNodes.splice(index < 0 ? this.childNodes.length : index, 0, child);
    }
    remove() {
        if (this.parentElement) {
            const parent = this.parentElement;
            parent.childNodes.splice(parent.childNodes.indexOf(this), 1);
            this.parentElement = null;
        }
    }
    querySelectorAll(selector) {
        const selectors = selector.split(',').map(value => value.trim());
        const result = [];
        const visit = element => {
            for (const child of element.children) {
                if (selectors.some(value => matches(child, value))) result.push(child);
                visit(child);
            }
        };
        visit(this);
        return result;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) {
        for (let element = this; element; element = element.parentElement) {
            if (matches(element, selector)) return element;
        }
        return null;
    }
    addEventListener(name, listener) {
        if (!this.listeners.has(name)) this.listeners.set(name, new Set());
        this.listeners.get(name).add(listener);
    }
    removeEventListener(name, listener) { this.listeners.get(name)?.delete(listener); }
    dispatch(name) { for (const listener of this.listeners.get(name) || []) listener(); }
    focus() { this.focused = true; }
    checkValidity() {
        if (this.required && !this.value) return false;
        if (this.type !== 'number' || !this.value) return true;
        const value = Number(this.value);
        return Number.isFinite(value) &&
            (!this.attributes.has('min') || value >= Number(this.attributes.get('min'))) &&
            (!this.attributes.has('max') || value <= Number(this.attributes.get('max')));
    }
    getBoundingClientRect() { return { top: 0, width: 480, height: 40 }; }
    getContext() {
        return {
            scale() {}, clearRect() {}, fillRect() {},
            createLinearGradient() { return { addColorStop() {} }; }
        };
    }
}

function dataKey(attribute) {
    return attribute.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function parseHTML(html, root) {
    const stack = [root];
    const voidTags = new Set(['INPUT', 'META', 'LINK', 'BR', 'HR', 'IMG']);
    for (const token of html.match(/<!--[\s\S]*?-->|<![^>]*>|<\/?[\w-]+[^>]*>|[^<]+/g) || []) {
        if (token.startsWith('<!')) continue;
        if (token.startsWith('</')) {
            const tag = token.slice(2, -1).trim().toUpperCase();
            const index = stack.findLastIndex(element => element.tagName === tag);
            if (index > 0) stack.length = index;
        } else if (token.startsWith('<')) {
            const [, tag, attributes] = token.match(/^<([\w-]+)([\s\S]*?)\/?>$/);
            const element = new Element(tag);
            for (const [, key, double, single, bare] of attributes.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
                element.setAttribute(key, double ?? single ?? bare ?? '');
            }
            stack.at(-1).appendChild(element);
            if (!voidTags.has(element.tagName) && !token.endsWith('/>')) stack.push(element);
        } else {
            stack.at(-1).childNodes.push(token);
        }
    }
}

function fakeClock() {
    let now = Date.parse('2026-09-18T12:00:00Z'), nextId = 0;
    const timers = new Map();
    const schedule = (callback, delay, repeat) => {
        const id = ++nextId;
        timers.set(id, { callback, at: now + delay, repeat });
        return id;
    };
    class ClockDate extends Date {
        constructor(...args) { super(...(args.length ? args : [now])); }
        static now() { return now; }
    }
    return {
        Date: ClockDate,
        timers,
        setTimeout: (callback, delay) => schedule(callback, delay, 0),
        clearTimeout: id => timers.delete(id),
        setInterval: (callback, delay) => schedule(callback, delay, delay),
        clearInterval: id => timers.delete(id),
        async advance(milliseconds) {
            const end = now + milliseconds;
            while (true) {
                const next = [...timers].filter(([, timer]) => timer.at <= end)
                    .sort((a, b) => a[1].at - b[1].at)[0];
                if (!next) break;
                const [id, timer] = next;
                now = timer.at;
                if (timer.repeat) timer.at += timer.repeat; else timers.delete(id);
                timer.callback();
                await flush();
            }
            now = end;
            await flush();
        }
    };
}

function fixtures() {
    const schedule = {
        time_based_on: 'fixed', fixed_start_time: '06:00', duration: 10,
        days: [], seasons: [], enable_uv_adjustments: false
    };
    return {
        status: {
            system: { status: 'OK', current_time: '2026-09-18T12:00:00Z', season: 'Fall' },
            valves: [{
                name: 'North', enabled: true, is_open: false, handled: false,
                seconds_remain: 0, seconds_duration: 0, seconds_daily: 0, liters_daily: 0,
                seconds_last: 120, liters_last: 0
            }],
            waterflow: { enabled: false, flow_rate_lpm: 0, is_active: false, history: [] },
            sensors: [{
                name: 'Weather', type: 'OpenWeatherMap', enabled: true, error: false,
                telemetry: { uv: 4, recentPrecip: 0 }, should_disable: false, factor: 1
            }]
        },
        health: {
            ready: true, controller: { running: true, fault: false, last_error: null },
            valves: [{
                name: 'North', state: 'closed', operation: null, fault: null,
                possibly_open: false, physical_state: 'unverified',
                attribution: { complete: true, reason: null }
            }],
            monitoring: {
                mqtt: { enabled: false, connected: false },
                waterflow: { enabled: false, available: false, fresh: false, age_seconds: null, reason: 'disabled' },
                sensors: [{ name: 'Weather', enabled: true, available: true, fresh: true, age_seconds: 0, reason: null }]
            },
            alerts: {},
            accounting: { unattributed_liters: 0, unavailable_seconds: 0, persistence_error: null }
        },
        queue: { jobs: [], queue_size: 0 },
        nextRuns: {},
        schedules: [copy(schedule), { ...copy(schedule), fixed_start_time: '18:00' }],
        config: {
            waterflow: { type: 'meter', enabled: true, leak_detection: true },
            alerts: {
                enabled: { leak: true, malfunction_no_flow: false, irregular_flow: true, sensor_error: true, system_exit: true },
                leak_repeat_minutes: 15, irregular_flow_threshold: 2
            },
            sensors: [{ name: 'Weather', precipitation: { days_to_aggregate: 3, disable_threshold_mm: 5 } }],
            timezone: 'UTC', location: { latitude: 0, longitude: 0 }, max_concurrent_valves: 1,
            telemetry_enabled: false, mqtt_enabled: false, valve_count: 1, sensor_count: 1
        }
    };
}

function harness() {
    const state = fixtures();
    const document = new Element('document');
    document.innerHTML = index;
    document.createElement = name => new Element(name);
    document.getElementById = id => document.querySelectorAll('[id]').find(element => element.id === id) || null;
    const window = new Element('window');
    window.devicePixelRatio = 1;
    const clock = fakeClock();
    const requests = [], routes = new Map(), errors = [];
    const defaults = request => {
        const { url, method, options } = request;
        if (method === 'GET') {
            if (url === '/api/status') return response(state.status);
            if (url === '/api/queue') return response(state.queue);
            if (url === '/api/health') return response(state.health);
            if (url === '/api/next-runs') return response({ next_runs: state.nextRuns });
            if (url === '/api/config') return response(state.config);
            if (url === '/api/valves/North') return response({ ...state.status.valves[0], schedules: state.schedules });
        }
        const body = options.body ? JSON.parse(options.body) : undefined;
        if (url === '/api/valves/North/schedules' && method === 'POST') {
            state.schedules.push(body);
            return response({ success: true, valve: 'North', schedule_index: state.schedules.length - 1 });
        }
        const schedule = url.match(/^\/api\/valves\/North\/schedules\/(\d+)$/);
        if (schedule && method === 'PUT') {
            state.schedules[Number(schedule[1])] = body;
            return response({ success: true, valve: 'North', schedule_index: Number(schedule[1]) });
        }
        if (schedule && method === 'DELETE') {
            if (state.schedules.length <= 1) return response({ detail: 'Cannot delete the last schedule' }, 400);
            state.schedules.splice(Number(schedule[1]), 1);
            return response({ success: true, valve: 'North', schedule_index: Number(schedule[1]) });
        }
        if (method === 'POST' && url.startsWith('/api/config/')) return response({ success: true, ...body });
        if (method === 'POST' && url.startsWith('/api/valves/North/start-manual')) {
            const minutes = Math.min(Number(new URL(url, 'http://offline.invalid').searchParams.get('duration_minutes') || 30), 30);
            Object.assign(state.status.valves[0], { is_open: true, handled: true, seconds_duration: minutes * 60, seconds_remain: minutes * 60 });
            Object.assign(state.health.valves[0], copy(healthyOpen.health.valves[0]));
            return response({ success: true, valve: 'North', action: 'opened_manual' });
        }
        if (method === 'POST' && url.startsWith('/api/valves/North/queue?')) {
            const minutes = Number(new URL(url, 'http://offline.invalid').searchParams.get('duration_minutes'));
            state.queue.jobs.push({ valve_name: 'North', duration_minutes: minutes, is_scheduled: false });
            state.queue.queue_size++;
            return response({ success: true, valve: 'North', action: 'queued' });
        }
        if (method === 'POST' && url === '/api/valves/North/stop') {
            Object.assign(state.status.valves[0], { is_open: false, handled: false, seconds_remain: 0 });
            Object.assign(state.health.valves[0], { state: 'closed', operation: null, fault: null, possibly_open: false });
            return response({ success: true, valve: 'North', action: 'stopped' });
        }
        if (method === 'POST' && /\/(enable|disable)$/.test(url)) {
            state.status.valves[0].enabled = url.endsWith('/enable');
            return response({ success: true, valve: 'North', action: state.status.valves[0].enabled ? 'enabled' : 'disabled' });
        }
        throw new Error(`Unexpected offline request: ${method} ${url}`);
    };
    const context = vm.createContext({
        document, window, Date: clock.Date, AbortController, URLSearchParams,
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
        setInterval: clock.setInterval, clearInterval: clock.clearInterval,
        console: { log() {}, error: (...args) => errors.push(args) },
        confirm: () => true, prompt: () => null,
        fetch: (url, options = {}) => {
            assert.ok(String(url).startsWith('/api/'), 'Only offline relative API calls are permitted');
            const request = { url, options, method: options.method || 'GET' };
            requests.push(request);
            return Promise.resolve((routes.get(`${request.method} ${url}`) || defaults)(request));
        }
    });
    vm.runInContext(source, context, { filename: 'app.js' });
    return {
        context, state, document, window, clock, requests, routes, defaults, errors,
        run: code => vm.runInContext(code, context),
        element: id => document.getElementById(id),
        writes: () => requests.filter(request => request.method !== 'GET'),
        clearRequests: () => { requests.length = 0; },
        ready: () => context.loadStatus(),
        async schedules() {
            await context.loadStatus();
            vm.runInContext("currentTab = 'config'", context);
            context.renderConfig(copy(state.config), copy(state.status.valves));
            await context.loadValveSchedules('North');
            requests.length = 0;
        },
        form(idx) { return document.getElementById(`schedule-edit-North-${idx}`).querySelector('form'); },
        save(idx, form) {
            form ||= this.form(idx);
            return context.saveSchedule({ target: form, currentTarget: form, preventDefault() {} }, 'North', idx);
        }
    };
}

test('health banner remains outside hidden flow history; startup null status is safe', async () => {
    const h = harness();
    const banner = h.element('system-status');
    assert.equal(banner.closest('#waterflow-history-bar'), null);
    assert.equal(banner.getAttribute('role'), 'status');
    for (const system of [null, undefined, { status: null }]) {
        assert.doesNotThrow(() => h.context.updateSystemStatus(system));
    }
    h.state.status.system = { status: null };
    h.state.health.ready = false;
    await h.ready();
    assert.match(banner.textContent, /not ready/i);
    assert.equal(banner.style.display, 'flex');
    assert.equal(h.element('waterflow-history-bar').style.display, 'none');
    assert.equal(h.element('valve-stop-North').disabled, false);
    assert.equal(h.element('valve-queue-North').disabled, true);
    await h.context.startValveManual('North');
    assert.equal(h.writes().length, 0);
});

test('Add, double Add, and Cancel are client-only and leave no persisted schedule', async () => {
    const h = harness();
    await h.schedules();
    h.context.addSchedule('North');
    const draft = h.element('schedule-North--1');
    h.element('duration-North--1').value = '22';
    h.context.addSchedule('North');
    assert.equal(h.document.querySelectorAll('.schedule-draft').length, 1);
    assert.equal(h.element('schedule-North--1'), draft);
    assert.equal(h.element('duration-North--1').value, '22');
    assert.match(draft.textContent, /not saved/i);
    h.context.cancelEditSchedule('North', -1);
    assert.equal(h.element('schedule-North--1'), null);
    assert.equal(h.state.schedules.length, 2);
    assert.equal(h.requests.length, 0);
});

test('draft Save posts once, locks duplicate submissions, and existing edits still PUT', async () => {
    const h = harness();
    await h.schedules();
    h.context.addSchedule('North');
    h.element('fixed_start_time-North--1').value = '07:45';
    h.element('duration-North--1').value = '12.5';
    h.element('enable_uv_adjustments-North--1').checked = true;
    const form = h.form(-1);
    form.querySelector('.day-checkbox').checked = true;
    form.querySelector('.season-checkbox').checked = true;
    const held = deferred();
    let original;
    h.routes.set('POST /api/valves/North/schedules', request => { original = request; return held.promise; });
    const save = h.save(-1, form);
    await h.save(-1, form);
    h.context.cancelEditSchedule('North', -1);
    h.context.addSchedule('North');
    assert.equal(h.writes().length, 1);
    assert.equal(form.querySelector('button[type="submit"]').disabled, true);
    assert.equal(form.querySelector('button[type="button"]').disabled, true);
    assert.notEqual(h.element('schedule-North--1'), null);
    assert.deepEqual(JSON.parse(original.options.body), {
        time_based_on: 'fixed', duration: 12.5, enable_uv_adjustments: true,
        days: ['Sun'], seasons: ['Spring'], fixed_start_time: '07:45'
    });
    held.resolve(h.defaults(original));
    await save;
    await h.save(-1, form);
    assert.equal(h.writes().length, 1);
    assert.equal(h.element('schedule-North--1'), null);
    assert.equal(h.state.schedules.length, 3);

    h.context.editSchedule('North', 0);
    h.element('time_based_on-North-0').value = 'sunset';
    h.context.updateTimeFields('North', 0);
    h.element('offset_minutes-North-0').value = '-15';
    h.element('duration-North-0').value = '20';
    await h.save(0);
    assert.equal(h.writes()[1].method, 'PUT');
    assert.equal(h.writes()[1].url, '/api/valves/North/schedules/0');
    assert.deepEqual(JSON.parse(h.writes()[1].options.body), {
        time_based_on: 'sunset', duration: 20, enable_uv_adjustments: false,
        days: [], seasons: [], offset_minutes: -15
    });
});

test('failed schedule saves keep the same draft and values; retry is explicit', async () => {
    const h = harness();
    await h.schedules();
    h.context.addSchedule('North');
    const form = h.form(-1);
    h.element('fixed_start_time-North--1').value = '09:12';
    h.element('duration-North--1').value = '0.5';
    form.querySelector('.season-checkbox').checked = true;
    h.routes.set('POST /api/valves/North/schedules', () => response({ detail: 'Cannot save now' }, 500));
    await h.save(-1, form);
    assert.equal(h.form(-1), form);
    assert.equal(h.element('fixed_start_time-North--1').value, '09:12');
    assert.equal(h.element('duration-North--1').value, '0.5');
    assert.equal(form.querySelector('.season-checkbox').checked, true);
    assert.equal(form.querySelector('button[type="submit"]').disabled, false);
    assert.equal(h.state.schedules.length, 2);
    assert.equal(h.writes().length, 1);
    h.routes.delete('POST /api/valves/North/schedules');
    await h.save(-1, form);
    assert.equal(h.writes().length, 2);
    assert.equal(h.state.schedules.at(-1).duration, 0.5);
});

test('failed existing edits retain their form; Cancel restores confirmed values without writes', async () => {
    const h = harness();
    await h.schedules();
    h.context.editSchedule('North', 0);
    const form = h.form(0);
    h.element('duration-North-0').value = '27';
    h.routes.set('PUT /api/valves/North/schedules/0', () => response({ detail: 'Rejected' }, 400));
    await h.save(0, form);
    assert.equal(h.form(0), form);
    assert.equal(h.element('duration-North-0').value, '27');
    assert.equal(h.element('schedule-edit-North-0').style.display, 'block');
    h.clearRequests();
    h.context.cancelEditSchedule('North', 0);
    assert.equal(h.element('duration-North-0').value, '10');
    assert.equal(h.requests.length, 0);
});

test('empty fixed time and non-finite or non-positive durations never reach persistence', async () => {
    const h = harness();
    await h.schedules();
    h.context.addSchedule('North');
    h.element('fixed_start_time-North--1').value = '';
    await h.save(-1);
    assert.match(h.element('toast').textContent, /start time/i);
    h.element('fixed_start_time-North--1').value = '06:00';
    for (const value of ['', '0', '-2', 'Infinity', 'NaN', '5x']) {
        h.element('duration-North--1').value = value;
        await h.save(-1);
    }
    assert.equal(h.requests.length, 0);
    assert.notEqual(h.element('schedule-North--1'), null);
});

test('a late schedule/config load cannot discard a draft; Cancel uses confirmed local data', async () => {
    const h = harness();
    await h.schedules();
    const held = deferred();
    h.routes.set('GET /api/valves/North', () => held.promise);
    const load = h.context.loadValveSchedules('North');
    h.context.addSchedule('North');
    h.element('duration-North--1').value = '24';
    const draft = h.form(-1);
    await h.context.loadConfig();
    held.resolve(response({ schedules: h.state.schedules }));
    await load;
    assert.equal(h.form(-1), draft);
    assert.equal(h.element('duration-North--1').value, '24');
    h.clearRequests();
    h.context.cancelEditSchedule('North', -1);
    assert.equal(h.requests.length, 0);
    assert.notEqual(h.element('schedule-North-0'), null);
});

test('saved schedule deletion uses DELETE and preserves the last-schedule rule', async () => {
    const h = harness();
    await h.schedules();
    await h.context.deleteSchedule('North', 1);
    assert.equal(h.writes().length, 1);
    assert.equal(h.writes()[0].method, 'DELETE');
    assert.equal(h.writes()[0].url, '/api/valves/North/schedules/1');
    assert.equal(h.writes()[0].options.body, undefined);
    assert.equal(h.state.schedules.length, 1);
    await h.context.deleteSchedule('North', 0);
    assert.equal(h.writes().length, 1);
    assert.equal(h.document.querySelector('[data-schedule-delete]').disabled, true);
});

test('polls coalesce; forced newer successes cannot be overwritten by old responses or errors', async () => {
    const h = harness();
    const old = new Map();
    for (const url of ['/api/status', '/api/queue', '/api/health']) {
        const held = deferred();
        old.set(url, held);
        h.routes.set(`GET ${url}`, () => held.promise);
    }
    const first = h.context.loadStatus();
    const repeated = h.context.loadStatus();
    h.context.loadQueue();
    assert.equal(h.requests.length, 3);
    assert.equal(first, repeated);
    h.routes.clear();
    Object.assign(h.state.status.valves[0], { is_open: true, handled: true, seconds_duration: 1800, seconds_remain: 1700 });
    Object.assign(h.state.health.valves[0], { state: 'open', operation: 'manual', possibly_open: true });
    await h.context.loadStatus({ force: true });
    old.get('/api/status').resolve(response(fixtures().status));
    old.get('/api/health').resolve(response(fixtures().health));
    old.get('/api/queue').reject(new Error('late offline response'));
    await first;
    assert.equal(h.run('statusData.valves[0].seconds_remain'), 1700);
    assert.equal(h.run('healthData.valves[0].operation'), 'manual');
    assert.equal(h.run('hasFreshStatus()'), true);
    assert.notEqual(h.element('valve-stop-North'), null);
    assert.equal(h.element('valve-open-North'), null);
});

test('an action invalidates in-flight status and never allows a stale response to renew Open', async () => {
    const h = harness();
    await h.ready();
    const held = deferred();
    h.routes.set('GET /api/status', () => held.promise);
    const poll = h.context.loadStatus();
    h.routes.delete('GET /api/status');
    await h.context.startValveManual('North');
    held.resolve(response(fixtures().status));
    await poll;
    assert.equal(h.run('healthData.valves[0].operation'), 'manual');
    assert.equal(h.run('statusData.valves[0].handled'), true);
    await h.context.startValveManual('North');
    assert.equal(h.writes().length, 1);
    assert.equal(h.writes()[0].url, '/api/valves/North/start-manual');
    assert.equal(h.writes()[0].options.body, undefined);
});

test('failed health polls disable new actions and saves but retain best-effort Close, even on startup', async () => {
    const h = harness();
    await h.schedules();
    h.context.addSchedule('North');
    h.run("currentTab = 'valves'");
    h.routes.set('GET /api/health', () => response({ detail: 'Unavailable' }, 503));
    await h.context.loadStatus();
    assert.equal(h.run('hasFreshStatus()'), false);
    assert.match(h.element('system-status').textContent, /Health unavailable/);
    assert.equal(h.element('valve-queue-North').disabled, true);
    assert.equal(h.element('valve-stop-North').disabled, false);
    assert.equal(h.form(-1).querySelector('button[type="submit"]').disabled, true);
    h.clearRequests();
    await h.context.startValveManual('North');
    await h.context.queueValve('North', 45);
    await h.save(-1);
    assert.equal(h.writes().length, 0);
    await h.context.stopValve('North');
    assert.deepEqual(h.writes().map(request => request.url), ['/api/valves/North/stop']);
    assert.match(h.element('toast').textContent, /accepted.*unverified/);

    const startup = harness();
    startup.routes.set('GET /api/health', () => Promise.reject(new Error('offline')));
    await startup.ready();
    assert.equal(startup.element('valve-stop-North').disabled, false);
    assert.equal(startup.element('valve-queue-North').disabled, true);
});

test('status or queue failures also degrade freshness despite a successful ready health response', async () => {
    for (const endpoint of ['/api/status', '/api/queue']) {
        const h = harness();
        await h.ready();
        h.routes.set(`GET ${endpoint}`, () => Promise.reject(new Error('Offline')));
        await h.ready();
        assert.equal(h.run('hasFreshStatus()'), false);
        assert.equal(h.element('valve-queue-North').disabled, true);
        assert.equal(h.element('valve-stop-North').disabled, false);
        await h.context.startValveManual('North');
        await h.context.queueValve('North', 10);
        assert.equal(h.writes().length, 0);
    }
});

test('failed Close never produces a success toast or an optimistic closed state', async () => {
    for (const reply of [response({ detail: 'Close failed' }, 503), response({ success: false })]) {
        const h = harness();
        Object.assign(h.state.status.valves[0], { is_open: false, handled: true, seconds_duration: 600, seconds_remain: 500 });
        Object.assign(h.state.health.valves[0], { state: 'paused', operation: 'queued' });
        await h.ready();
        h.routes.set('POST /api/valves/North/stop', () => reply);
        h.routes.set('GET /api/health', () => Promise.reject(new Error('offline')));
        await h.context.stopValve('North');
        assert.equal(h.element('toast').classList.contains('success'), false);
        assert.equal(h.element('toast').classList.contains('error'), true);
        assert.match(h.element('valve-North').textContent, /Paused/);
        assert.equal(h.element('valve-stop-North').disabled, false);
        assert.equal(h.run('healthData.valves[0].operation'), 'queued');
    }
});

test('paused, waiting, owned, fault, and possibly-open valves expose Close and cannot be reopened', async () => {
    const states = [
        { state: 'paused', operation: 'scheduled' },
        { state: 'waiting', operation: 'queued' },
        { state: 'closed', operation: 'manual' },
        { state: 'fault', operation: null, fault: 'Close failed', possibly_open: true },
        { state: 'unknown', operation: null, possibly_open: true }
    ];
    for (const state of states) {
        const h = harness();
        Object.assign(h.state.health.valves[0], state);
        h.state.status.valves[0].is_open = false;
        await h.ready();
        assert.equal(h.element('valve-open-North'), null);
        assert.equal(h.element('valve-stop-North').disabled, false);
        await h.context.startValveManual('North');
        assert.equal(h.writes().length, 0);
        await h.context.stopValve('North');
        assert.equal(h.writes()[0].url, '/api/valves/North/stop');
    }
});

test('controller faults and stopped controllers block new actions, not Close', async () => {
    for (const controller of [
        { running: true, fault: true, last_error: 'Close command failed' },
        { running: false, fault: false, last_error: null }
    ]) {
        const h = harness();
        h.state.health.controller = controller;
        await h.ready();
        assert.equal(h.element('valve-open-North'), null);
        assert.equal(h.element('valve-queue-North').disabled, true);
        assert.equal(h.element('valve-stop-North').disabled, false);
        await h.context.stopValve('North');
        assert.equal(h.writes().length, 1);
        assert.equal(h.writes()[0].url, '/api/valves/North/stop');
    }
});

test('duplicate Open is guarded in flight and a tab first opened during a write loads afterward', async () => {
    for (const tab of ['config', 'sensors']) {
        const h = harness();
        await h.ready();
        const held = deferred();
        let request;
        h.routes.set('POST /api/valves/North/start-manual', value => { request = value; return held.promise; });
        const opening = h.context.startValveManual('North');
        await h.context.startValveManual('North');
        assert.equal(h.writes().length, 1);
        assert.equal(h.element('valve-stop-North').disabled, false);
        h.context.switchTab(tab);
        held.resolve(h.defaults(request));
        await opening;
        await flush();
        assert.notEqual(h.document.querySelector(tab === 'config' ? '.config-section' : '.sensor-card'), null);
        assert.equal(h.writes().length, 1);
    }
});

test('manual override ignores schedule enablement and weather outage; durations retain API query policy', async () => {
    const h = harness();
    h.state.status.valves[0].enabled = false;
    Object.assign(h.state.health.monitoring.sensors[0], { available: false, fresh: false, reason: 'weather outage' });
    h.state.status.sensors[0].error = true;
    await h.ready();
    assert.equal(h.element('valve-open-North').disabled, false);
    assert.equal(h.element('valve-queue-North').disabled, false);
    assert.match(h.element('system-status').textContent, /watering can continue/);
    await h.context.startValveManual('North', 90);
    assert.equal(h.writes()[0].url, '/api/valves/North/start-manual?duration_minutes=30');
    assert.equal(h.writes()[0].options.body, undefined);
    assert.equal(h.element('valve-open-North'), null);
    await h.context.queueValve('North', 45.5);
    assert.equal(h.writes()[1].url, '/api/valves/North/queue?duration_minutes=45.5');
    const queuedJobs = copy(h.state.queue.jobs);
    await h.context.stopValve('North');
    assert.deepEqual(h.state.queue.jobs, queuedJobs, 'Stop must not clear unrelated pending queue jobs');
    assert.equal(h.writes()[2].url, '/api/valves/North/stop');
});

test('fresh weather decisions remain visible without blocking manual override', async () => {
    const h = harness();
    h.state.status.sensors[0].should_disable = true;
    await h.ready();
    assert.equal(h.element('sensor-status').style.display, 'flex');
    assert.match(h.element('sensor-text').textContent, /Weather: Disabled/);
    assert.equal(h.element('uv-index').textContent, '4');
    assert.equal(h.element('valve-open-North').disabled, false);
});

test('empty valve snapshots preserve the existing no-valves display', async () => {
    const h = harness();
    h.state.status.valves = [];
    h.state.health.valves = [];
    await h.ready();
    assert.match(h.element('valves-grid').textContent, /No valves configured/);
    assert.equal(h.writes().length, 0);
});

test('expired snapshots block new writes and show last-known server countdowns, not inferred closure', async () => {
    const h = harness();
    Object.assign(h.state.status.valves[0], {
        enabled: false, is_open: true, handled: true, seconds_duration: 1800, seconds_remain: 1720
    });
    Object.assign(h.state.health.valves[0], { state: 'open', operation: 'manual', possibly_open: true });
    await h.ready();
    assert.match(h.element('valve-North').textContent, /Manual/);
    assert.match(h.element('valve-North').textContent, /28:40 left \/ 30:00/);
    assert.equal(h.element('valve-North').querySelector('[role="progressbar"]').getAttribute('aria-valuenow'), '96');
    await h.clock.advance(16000);
    h.context.refreshStatusUI();
    assert.equal(h.run('hasFreshStatus()'), false);
    assert.match(h.element('valve-North').textContent, /28:40 left \/ 30:00 \(last known\)/);
    assert.equal(h.element('valve-queue-North').disabled, true);
    assert.equal(h.element('valve-stop-North').disabled, false);
    assert.equal(h.context.valveProgress({ seconds_duration: 60, seconds_remain: 90 }), 100);
    assert.equal(h.context.valveProgress({ seconds_duration: 60, seconds_remain: -1 }), 0);
    assert.equal(h.context.formatTime(61.5), '1:01');
});

test('schedule-disabled valves still display Open and a server countdown on render and refresh', async () => {
    for (const operation of ['manual', 'queued', 'scheduled', null]) {
        const h = harness();
        Object.assign(h.state.status.valves[0], {
            enabled: false, is_open: true, handled: true, seconds_duration: 600, seconds_remain: 245
        });
        Object.assign(h.state.health.valves[0], { state: 'open', operation, possibly_open: true });
        await h.ready();
        const card = h.element('valve-North');
        const badge = card.querySelector('.valve-status-badge');
        assert.match(badge.textContent, /^Open.*\(4:05 left\)$/);
        assert.doesNotMatch(badge.textContent, /disabled/i);
        if (operation === 'manual') assert.match(badge.textContent, /Manual/);
        assert.equal(badge.classList.contains('open'), true);
        assert.match(card.querySelector('.operation-row').textContent, /4:05 left \/ 10:00/);
        assert.equal(h.element('valve-stop-North').disabled, false);
        h.state.status.valves[0].seconds_remain = 180;
        await h.ready();
        assert.match(badge.textContent, /^Open.*\(3:00 left\)$/);
        assert.match(card.querySelector('.operation-row').textContent, /3:00 left \/ 10:00/);
        assert.equal(h.writes().length, 0);
    }
});

test('controller-verified healthy open is not an actuator fault on first render or refresh', async () => {
    for (const alreadyRendered of [false, true]) {
        const h = harness();
        if (alreadyRendered) await h.ready();
        Object.assign(h.state, copy(healthyOpen));
        await h.ready();
        const card = h.element('valve-North');
        const badge = card.querySelector('.valve-status-badge');
        assert.equal(h.run('healthData.valves[0].possibly_open'), true);
        assert.equal(badge.classList.contains('open'), true);
        assert.equal(badge.classList.contains('fault'), false);
        assert.match(badge.textContent, /^Open · Manual \(5:00 left\)$/);
        assert.match(card.querySelector('.operation-row').textContent, /5:00 left \/ 5:00/);
        assert.equal(card.querySelector('[role="progressbar"]').getAttribute('aria-valuenow'), '100');
        assert.equal(h.element('system-status').classList.contains('error'), false);
        assert.doesNotMatch(h.element('system-status').textContent, /fault|needs attention|no-flow/i);
        assert.match(h.element('system-status').textContent, /Valve positions are unverified/);
        assert.equal(h.element('valve-stop-North').disabled, false);
        assert.equal(h.element('valve-queue-North').disabled, false);
        assert.equal(h.element('valve-open-North'), null);
        assert.equal(h.writes().length, 0);
    }
});

test('controller-verified healthy open allows queue follow-up, not renewed Open; stale data still guards it', async () => {
    const h = harness();
    Object.assign(h.state, copy(healthyOpen));
    await h.ready();
    await h.context.startValveManual('North');
    assert.equal(h.writes().length, 0);
    await h.context.queueValve('North', 45);
    assert.equal(h.writes().length, 1);
    assert.equal(h.writes()[0].url, '/api/valves/North/queue?duration_minutes=45');
    assert.equal(h.writes()[0].options.body, undefined);
    assert.equal(h.run('healthData.valves[0].operation'), 'manual');
    assert.equal(h.element('valve-queue-North').disabled, false);
    await h.context.startValveManual('North');
    assert.equal(h.writes().length, 1);
    h.routes.set('GET /api/health', () => response({}, 503));
    await h.ready();
    assert.equal(h.element('valve-queue-North').disabled, true);
    assert.equal(h.element('valve-stop-North').disabled, false);
    await h.context.queueValve('North', 5);
    assert.equal(h.writes().length, 1);
    await h.context.stopValve('North');
    assert.equal(h.writes().length, 2);
    assert.equal(h.writes()[1].url, '/api/valves/North/stop');
    assert.equal(h.state.queue.jobs.length, 1);
});

test('possibly-open unknown, contradictory closed, and fault states remain guarded', async () => {
    for (const health of [
        { state: 'unknown', operation: null, fault: null },
        { state: 'closed', operation: null, fault: null },
        { state: 'fault', fault: 'Close failed' },
        { state: 'open', fault: 'Actuation failed' }
    ]) {
        const h = harness();
        Object.assign(h.state, copy(healthyOpen));
        Object.assign(h.state.health.valves[0], health);
        if (health.operation === null) {
            Object.assign(h.state.status.valves[0], { is_open: false, handled: false });
        }
        await h.ready();
        const badge = h.element('valve-North').querySelector('.valve-status-badge');
        assert.equal(badge.classList.contains('open'), false);
        assert.equal(h.element('valve-open-North'), null);
        assert.equal(h.element('valve-stop-North').disabled, false);
        assert.equal(h.element('valve-queue-North').disabled, true);
        await h.context.startValveManual('North');
        await h.context.queueValve('North', 5);
        assert.equal(h.writes().length, 0);
        assert.equal(h.element('system-status').classList.contains('error'), true);
    }
});

test('qualified flow alarms warn without actuator-fault styling or blocking idle manual override', async () => {
    const h = harness();
    h.state.status.valves[0].enabled = false;
    h.state.health.valves[0].flow_alarm = true;
    h.state.health.valves[0].attribution.complete = false;
    h.state.status.waterflow.enabled = true;
    h.state.health.monitoring.waterflow.enabled = true;
    await h.ready();
    const banner = h.element('system-status');
    const badge = h.element('valve-North').querySelector('.valve-status-badge');
    assert.match(banner.textContent, /No-flow warning for North/);
    assert.equal(banner.classList.contains('warning'), true);
    assert.equal(banner.classList.contains('error'), false);
    assert.equal(badge.classList.contains('fault'), false);
    assert.doesNotMatch(badge.textContent, /fault|malfunction/i);
    assert.equal(h.run('controllerReady()'), true);
    assert.equal(h.run('canWriteConfig()'), true);
    assert.equal(h.element('valve-open-North').disabled, false);
    assert.equal(h.element('valve-queue-North').disabled, false);
    await h.context.startValveManual('North');
    assert.equal(h.writes().length, 1);
    assert.equal(h.writes()[0].url, '/api/valves/North/start-manual');
    assert.equal(h.element('valve-open-North'), null, 'An owned operation still requires Close first');
    h.state.health.valves[0].flow_alarm = false;
    await h.ready();
    assert.doesNotMatch(banner.textContent, /No-flow warning/);
    assert.equal(banner.classList.contains('error'), false);
});

test('missing, disabled, stale, or incomplete monitoring never fabricates a flow alarm', async () => {
    const cases = [
        null,
        { enabled: false, available: false, fresh: false },
        { enabled: true, available: false, fresh: false },
        { enabled: true, available: true, fresh: false },
        { enabled: true, available: true, fresh: true }
    ];
    for (const flow of cases) {
        for (const alarm of [undefined, false]) {
            const h = harness();
            h.state.health.valves[0].attribution = { complete: false, reason: 'Incomplete monitoring' };
            if (alarm !== undefined) h.state.health.valves[0].flow_alarm = alarm;
            if (flow) h.state.health.monitoring.waterflow = flow;
            else delete h.state.health.monitoring;
            h.state.status.waterflow.enabled = true;
            await h.ready();
            assert.doesNotMatch(h.element('system-status').textContent, /no-flow|malfunction|controller fault/i);
            assert.doesNotMatch(h.context.getValveStatus(h.state.status.valves[0]), /fault|malfunction/i);
            assert.equal(h.run('controllerReady()'), true);
            assert.equal(h.element('valve-open-North').disabled, false);
            assert.equal(h.element('valve-queue-North').disabled, false);
            assert.equal(h.writes().length, 0);
        }
    }
});

test('unavailable waterflow is not fresh zero and historical zero liters do not imply malfunction', async () => {
    const h = harness();
    h.state.status.waterflow.enabled = true;
    h.state.health.monitoring.waterflow.enabled = true;
    await h.ready();
    assert.match(h.element('waterflow-text').textContent, /unavailable.*stale/);
    assert.doesNotMatch(h.element('waterflow-text').textContent, /0 L\/min/);
    assert.equal(h.element('waterflow-history-bar').classList.contains('stale'), true);
    assert.doesNotMatch(h.context.getValveStatus(h.state.status.valves[0]), /malfunction/i);
    assert.equal(h.element('valve-open-North').disabled, false);
    Object.assign(h.state.health.monitoring.waterflow, { available: true, fresh: true });
    await h.ready();
    assert.equal(h.element('waterflow-text').textContent, '0 L/min');
});

test('next-runs has independent versioning and cannot resurrect status or action availability', async () => {
    const h = harness();
    await h.ready();
    const old = deferred(), latest = deferred();
    h.routes.set('GET /api/next-runs', () => old.promise);
    const first = h.context.loadNextRuns();
    h.routes.set('GET /api/next-runs', () => latest.promise);
    const second = h.context.loadNextRuns({ force: true });
    h.routes.set('GET /api/health', () => response({}, 503));
    await h.ready();
    const queueReads = h.requests.filter(request => request.url === '/api/queue').length;
    latest.resolve(response({ next_runs: { North: { schedule_time_iso: '2026-09-20T06:00:00Z' } } }));
    await second;
    old.resolve(response({ next_runs: { North: { schedule_time_iso: '2026-09-19T06:00:00Z' } } }));
    await first;
    assert.equal(h.run("nextRunsData.North.schedule_time_iso"), '2026-09-20T06:00:00Z');
    assert.equal(h.requests.filter(request => request.url === '/api/queue').length, queueReads);
    assert.equal(h.run('hasFreshStatus()'), false);
    assert.equal(h.element('valve-queue-North').disabled, true);
    assert.equal(h.element('valve-stop-North').disabled, false);
});

test('failed waterflow toggles restore the confirmed original control, not global event.target', async () => {
    const h = harness();
    await h.ready();
    h.element('config-view').innerHTML = h.context.renderWaterflowConfig(h.state.config);
    const enabled = h.element('waterflow-enabled'), other = h.element('waterflow-leak_detection');
    assert.match(enabled.getAttribute('onchange'), /this.checked, this/);
    const held = deferred();
    h.routes.set('POST /api/config/waterflow', () => held.promise);
    enabled.checked = false;
    const update = h.context.updateWaterflowSetting('enabled', false, enabled);
    assert.equal(enabled.disabled, true);
    h.context.event = { target: other };
    other.checked = false;
    enabled.checked = true;
    await h.context.updateWaterflowSetting('enabled', true, enabled);
    assert.equal(h.writes().length, 1);
    assert.equal(enabled.checked, false, 'Duplicate changes retain the in-flight value');
    held.resolve(response({ detail: 'Failed' }, 500));
    await update;
    assert.equal(enabled.checked, true);
    assert.equal(enabled.disabled, false);
    assert.equal(other.checked, false);
    assert.deepEqual(JSON.parse(h.writes()[0].options.body), { setting: 'enabled', value: false });
});

test('alert and sensor controls commit confirmed values and roll back failed toggles and numbers', async () => {
    const h = harness();
    await h.ready();
    h.element('config-view').innerHTML = h.context.renderAlertsConfig(h.state.config);
    h.context.renderSensors(h.state.status.sensors, h.state.config.sensors);
    const alert = h.element('alert-leak'), number = h.element('leak-repeat-minutes');
    const sensor = h.element('sensor-Weather-days');
    alert.checked = false;
    await h.context.toggleAlert('leak', false, alert);
    assert.equal(alert.dataset.confirmedValue, 'false');
    assert.equal(h.element('alert-setting-leak').style.display, 'none');
    h.routes.set('POST /api/config/alerts/enabled', () => response({ success: false }));
    alert.checked = true;
    await h.context.toggleAlert('leak', true, alert);
    assert.equal(alert.checked, false);
    assert.equal(h.element('alert-setting-leak').style.display, 'none');
    assert.deepEqual(JSON.parse(h.writes()[0].options.body), { alert_type: 'leak', enabled: false });

    number.value = '20';
    await h.context.updateAlertSetting('leak_repeat_minutes', number.value, number);
    assert.equal(number.dataset.confirmedValue, '20');
    h.routes.set('POST /api/config/alerts/settings', () => response({}, 500));
    number.value = '22';
    await h.context.updateAlertSetting('leak_repeat_minutes', number.value, number);
    assert.equal(number.value, '20');
    h.routes.set('POST /api/config/sensors/Weather', () => Promise.reject(new Error('Offline')));
    sensor.value = '6';
    await h.context.updateSensorSetting('Weather', 'precip_days', sensor.value, sensor);
    assert.equal(sensor.value, '3');
    assert.deepEqual(JSON.parse(h.writes().at(-1).options.body), { setting: 'precip_days', value: 6 });
    assert.equal(sensor.disabled, false);
});

test('offline settings changes roll back without issuing a write', async () => {
    const h = harness();
    await h.ready();
    h.element('config-view').innerHTML = h.context.renderWaterflowConfig(h.state.config);
    const control = h.element('waterflow-enabled');
    await h.clock.advance(16000);
    control.checked = false;
    await h.context.updateWaterflowSetting('enabled', false, control);
    assert.equal(control.checked, true);
    assert.equal(control.disabled, true);
    assert.equal(h.writes().length, 0);
});

test('reads are bounded even if a fake transport ignores abort', async () => {
    const h = harness();
    for (const url of ['/api/status', '/api/queue', '/api/health']) {
        h.routes.set(`GET ${url}`, () => new Promise(() => {}));
    }
    const poll = h.context.loadStatus();
    await h.clock.advance(8000);
    await poll;
    assert.equal(h.run('hasFreshStatus()'), false);
    assert.match(h.element('system-status').textContent, /unavailable/);
    assert.ok(h.requests.every(request => request.options.signal.aborted));
    assert.equal(h.run('activeRequests.size'), 0);
});

test('beforeunload clears both intervals, timeouts, and aborts reads without starting new work', async () => {
    const h = harness();
    for (const url of ['/api/status', '/api/queue', '/api/health', '/api/next-runs']) {
        h.routes.set(`GET ${url}`, () => new Promise(() => {}));
    }
    h.document.dispatch('DOMContentLoaded');
    assert.equal([...h.clock.timers.values()].filter(timer => timer.repeat).length, 2);
    assert.equal(h.requests.length, 4);
    h.context.showToast('Pending');
    h.window.dispatch('beforeunload');
    await flush();
    assert.equal(h.clock.timers.size, 0);
    assert.ok(h.requests.every(request => request.options.signal.aborted));
    assert.equal(h.run('activeRequests.size'), 0);
    await h.clock.advance(120000);
    await h.context.loadStatus();
    await h.context.loadNextRuns();
    assert.equal(h.requests.length, 4);
    assert.equal(h.writes().length, 0);
});
