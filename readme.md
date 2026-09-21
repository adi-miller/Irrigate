# Irrigate

Irrigate controls three-wire latching irrigation valves, schedules watering,
receives a shared MQTT flow reading, and exposes a FastAPI control panel.
Python 3.9 or later is supported.

## Safety model

One serialized controller owns opening, closing, cancellation, deadlines and
accounting. HTTP, MQTT and scheduled operations use that controller; no command
has an unmanaged GPIO path.

**On production startup/restart, Irrigate sends Close to every configured valve
before allowing new watering. Any failed close blocks all new watering.**
Interrupted operations are not restored from disk. SIGINT, SIGTERM and critical
thread failure use the same close-before-exit sequence. Close failures are
retried boundedly and leave an explicit fault, rather than a successful-looking
closed state.

The software tracks *command acknowledgements*, not physical position.
`is_open` is conservative when a valve might still be open. A successful pulse,
zero observed flow or a successful HTTP response does **not** prove mechanical
closure. GPIO/kernel hangs, failed wiring, power loss and SIGKILL cannot be made
safe by a Python timer alone. Independent hardware protection or operator
verification is required for protection against those failures.

## Installation and offline development

The normal dependency file does not install GPIO. Test doubles are injected
explicitly; there is no top-level `RPi` package masking the real driver.

On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q -o log_cli=false
node --test tests\ui.test.cjs
```

The tests use fake clocks, GPIO histories, MQTT/HTTP transports and temporary
configuration/accounting data. Network/GPIO guards fail unexpected real access.
No broker, weather API, MillerBot, Grafana or Raspberry Pi is needed.

Use the checked-in fixture **only with an explicit offline flag**:

```powershell
python irrigate.py --test --config=test_config.json
python irrigate.py --config=test_config.json --simulate=date:2025-06-15,uv:0,rain:no
```

`--test` is an offline initialization check and exits; it does not pulse real
hardware, run an API server or contact external services. `--simulate` also
selects offline composition before loading adapters. Offline accounting uses a
temporary directory, not production history. The old hardware-cycling test mode
and obsolete YAML fixture are not supported.

For an authorized Raspberry Pi installation, use the existing working real GPIO
adapter, or install the board-appropriate adapter separately. The classic
RPi.GPIO dependency is listed in `requirements-pi.txt`. Verify board/OS support
before installing it; do not replace a working adapter blindly. Production
reports a startup fault when the real adapter is unavailable and never falls
back to a fake.

## Configuration

Runtime configuration is JSON:

```text
python irrigate.py --config=config.json
```

The default filename is `config.json`. Validate against the bundled
`config.schema.json`, which is loaded independently of the configuration file's
directory. Do not put actual API keys or deployment configuration into Git.

The configuration retains the existing MQTT, valve, sensor, alert and schedule
key names. Optional day/season lists default to unrestricted; omitted UV
adjustments default to false. Existing alert flags default to true when omitted.
Leak repeat defaults to 15 minutes and the irregular-flow threshold to two
standard deviations. Weather precipitation defaults remain three days and
1.0 mm. Exclusion schedules require an explicit duration.

`telemetry.idle_interval` and `telemetry.active_interval` are **minutes**, matching
the original runtime behavior, not the former schema's incorrect seconds label.

Supported production backends are `3wire` valves, `mqtt` waterflow and
`openweathermap` weather. `2wire`, GPIO waterflow and `watering_mode: volume`
were not implemented and now fail validation explicitly. Three-wire pulse
duration defaults to 0.02 seconds and is capped at 0.2 seconds. Both coils are
de-energized before and after a pulse; a failed de-energization prevents an
opposite pulse.

Runtime edits are validated as complete candidates before publication. A
same-directory temporary file, fsync, atomic replacement and `<config>.last-good`
protect the accepted configuration. Failed validation/persistence does not leave
partially applied live edits. Invalid numeric values, empty fixed times,
unsupported settings and broken references are rejected. The previous accepted
configuration can be restored explicitly with `Config.recover_last_good(...)`;
recovery is not an excuse to silently accept a corrupt primary file.

## Open, Queue, Close and Disable

| Action | Behavior |
| --- | --- |
| Immediate **Open** | Bypasses queue, enabled/sensor admission and queued concurrency, but never operation ownership, fault/readiness checks or a server deadline. Defaults to **30 minutes**; finite positive durations above 30 become 30. |
| Repeated Open | Returns an explicit conflict while any operation owns the valve, including a sensor-paused operation. No extra pulse, renewal, preemption or duration change. Close first. |
| **Queue** | Uses a required positive, finite, representable duration in minutes and configured queued concurrency. No 30-minute cap. Ad-hoc queue requests do not use schedule-bound sensors. |
| Scheduled job | Its fixed monotonic lifetime starts at **dispatch/dequeue**, not when first physically opened. Initial sensor waiting and later pauses consume that same lifetime; even a never-opened job expires naturally. |
| **Close / Stop** | Cancels the current operation, including sensor-paused work, and attempts Close. Sensor recovery cannot reopen that cancelled job. Unrelated pending jobs remain and can run subsequently. |
| **Disable** | Prevents scheduled/queued opening and terminates owned queued/scheduled work. Explicit manual Open retains its enabled override. It is not an emergency replacement for Close. |

HTTP routes and successful response shapes remain compatible:

```text
POST /api/valves/{valve_name}/start-manual
POST /api/valves/{valve_name}/start-manual?duration_minutes=12.5
POST /api/valves/{valve_name}/queue?duration_minutes=45
POST /api/valves/{valve_name}/stop
POST /api/valves/{valve_name}/enable
POST /api/valves/{valve_name}/disable
PUT  /api/valves/{valve_name}/enabled?enabled=false
```

Duration is the `duration_minutes` **query parameter**, with an underscore.
An invalid duration is rejected and logged without actuation. A busy valve
returns HTTP 409; a failed close or blocked readiness is not reported as a
successful close/open.
Positive durations must remain nonzero at the supported time resolution,
including after UV adjustment; an expected rejected job does not stop unrelated
watering. Sensor resumes also check global actuator readiness before every Open
pulse, without extending their original deadline.

### MQTT commands

Use the configured client-name prefix and the original topics:

```text
<client_name>/queue/<valve>/command
<client_name>/enabled/<valve>/command
<client_name>/forceopen/<valve>/command
<client_name>/forceclose/<valve>/command
```

Command valve names map underscores to spaces as before. `queue` takes numeric
minutes; `enabled` takes `0` or `1` and changes runtime enablement.

**Intentional change:** `forceopen` now accepts **bare numeric minutes**, not
JSON, and no longer ignores its payload. An empty (zero-length) payload means
30 minutes. Positive finite numbers through 30 are honored; larger finite
numbers become 30. Thus legacy payload `1` now means **one minute**. Whitespace
alone, nonnumeric/JSON input, zero, negative values, NaN, infinity and overflow
are rejected and logged without a pulse. `forceclose` uses the same cancellation
and close path as HTTP Stop and retains its ignored trigger payload.
Close supersedes older MQTT Open commands still awaiting dispatch, including
when Close is requested through HTTP. A genuinely later Open is preserved;
unrelated queued irrigation jobs are not removed. Runtime enabled commands
publish their status even when periodic telemetry is disabled.

## Monitoring, telemetry and alerts

`GET /api/health` is the separate, additive source for readiness, actuator
uncertainty, monitoring freshness, accounting quality and notification delivery
health. Existing status/config/queue response fields and MQTT topic names,
numeric units, capitalization and status vocabulary remain unchanged. Manual
operations now participate in countdowns, seconds/liters totals and active
publication cadence. Completed totals are published as well.

Weather refresh normally runs every two hours. Only a **complete valid
snapshot** is published; a failed refresh does not replace it with zeros or a
partial rain total. Data becomes stale after **six hours**. Never-valid data is
unavailable immediately. Unavailable weather permits bounded watering with an
explicit health/error/alert indication; unavailable UV adjustments use the
configured base duration. Active deadlines are never extended. Fresh valid
readings still apply ordinary rain/UV behavior. Reading status or simulating
does not consume pending MQTT weather telemetry.
Changing the precipitation aggregation window makes an older-window snapshot
unavailable until a matching complete refresh; the same diagnosed fallback
applies in the meantime.

Flow freshness is separate from its numeric value. The existing 60-second
freshness limit is retained; a stale/disconnected sensor is **unavailable**, not
a zero-flow observation, and cannot resolve a leak. The last observation and its
original timestamp remain available for display.
Before any observation, the legacy numeric status field remains `0` as an
unavailable placeholder, with `last_update: null` and empty history; it is not a
measured zero. Consumers must use health/freshness rather than that number alone.

**Source liveness is not measurement freshness.** The MQTT meter normally reports
zero flow every **600 seconds while idle**, but reports much faster while watering.
When every valve is acknowledged closed (including sensor-paused or queued
operations), no actuator state is uncertain, and the latest valid reading is
zero, monitoring allows **600 seconds plus 60 seconds of heartbeat jitter grace**.
After that 660-second bound source health becomes unavailable. Positive flow
with closed valves and unknown/faulted/possibly-open states retain the
60-second source-health bound. Disconnection
or invalid readings make source health unavailable immediately; neither renews
the heartbeat nor clears an invalid-reading error without a valid observation.

Before the first reading, the same bounded idle wait starts only after successful
startup Close reconciliation, once per process. Opening from healthy idle allows
at most **60 seconds from the Open command** for the first active report; later
reports must remain fresh within the existing 60-second measurement limit.
An opening grace is consumed once per valid observation (or initial startup
wait), not renewed by polling, reconnects, repeated Close/Open, or sensor
pause/resume. Closing without an intervening observation does not erase that
pending active-report deadline. No grace extends a watering deadline.

**Stale/no-reading notifications have a separate, uniform window.** An enabled
meter's `monitoring_unavailable` warning for missing data is eligible only when
**more than 660 seconds** have elapsed since its last genuine valid observation:
the nominal 10-minute heartbeat interval plus the existing 1-minute delivery
margin. At exactly 60, 600 or 660 seconds there is no stale-data notification;
the first monitoring tick after 660 seconds may enqueue one. This applies to
zero and positive last readings, including the positive tail after physical
watering stops, and to idle, active, paused or uncertain actuator states. Before
any valid observation, the same notification window uses the runtime's single
monotonic initialization time, even if startup valve reconciliation fails.
Polling, reconnects and valve commands cannot renew it. The source-health
opening grace neither extends this notification window nor makes old data fresh.

There is **one non-repeating missing-reading incident per outage**, using the
existing waterflow alert subject. A new valid observation rearms it, even if that
observation arrives and expires between monitoring ticks. Invalid payloads,
reason changes, reconnects, pauses, Close/Open and schedule completion do not
recover a missing-reading incident. Disabled/unconfigured meters create no new
warnings; disabling/re-enabling a meter is not sample recovery and does not
erase an already queued incident or its bounded delivery retries. Disconnection
and invalid-reading warnings remain immediate, and deferring a stale warning
does not clear an existing failure or mark monitoring healthy. The notification
uses `stale reading` or `no valid reading`; state-specific source-health reasons
remain available independently through health.

For an enabled meter, `/api/health` adds
`monitoring.waterflow.source.available` and `.reason` for this liveness policy.
The existing `available`, `fresh`, `age_seconds` and `reason` still describe the
strict measurement snapshot. The UI uses source liveness for its monitoring
warning, but still marks an old reading/history stale rather than displaying a
fresh zero. Reading status/health never renews timers or creates observations;
the heartbeat tolerance is not used for accounting, leak or no-flow decisions.

Flow integration uses the reported aggregate L/min rate over observed intervals.
Per-valve liters are **estimates**, not dedicated per-valve meter readings. Only
fresh intervals with exactly one eligible commanded-open valve are attributed;
overlap/uncertainty remains unattributed, never duplicated or arbitrarily divided.
Other unobserved consumers on the shared meter cannot be excluded. Health
distinguishes complete coverage from partial totals; incomplete new data is not
used to infer normal/irregular flow baselines.

No-flow detection uses rolling positive-flow age with the opening/resume grace,
so it detects flow stopping after an earlier positive reading. Unavailable
monitoring is not no-flow. Closed-valve leak detection retains its settling
period, configured repeats and day/season/time exclusions.

Existing alert IDs and severities remain. New optional flags/IDs are:

| ID | Severity | Meaning |
| --- | --- | --- |
| `safety_intervention` | critical | A protective intervention, critical-thread failure or materially late deadline handling |
| `actuation_failure` | critical | A GPIO command failed; closure may be unverified |
| `monitoring_unavailable` | warning | Required monitoring or accounting/logging diagnostics are unavailable |

Ordinary on-time completion is not an alarm. Notifications and log writing run
off the valve deadline thread with bounded queues and visible failure/overflow
state. MillerBot retains the original request body (`user_id`, `query`, `role`),
headers and message envelope. Attempts/delivery failures are tracked per channel;
failed sends are not recorded as delivered. Retries are bounded, and recovery
cancels obsolete pending retries. These delivery retries are not periodic outage
reminders. Incident deduplication and the outbox are memory-only: process/power
failure can lose undelivered messages, and a later process may warn again for an
unresolved outage. HTTP acceptance by MillerBot does not independently confirm
downstream Telegram delivery.

## Accounting and simulation

Local accounting splits operations at configured-local midnight and finalizes
each valve/date once. Runtime checkpoints preserve recoverable counters and
quality; they do not restore watering operations or invent water use during
downtime. `data/valve_metrics.csv` retains its existing columns/units and historical
rows. Partial totals and export/checkpoint state are kept separately. Failed
persistence is explicit; historical Influx data is not rewritten.
Midnight export follows serialized integration through the date boundary, with
file I/O outside the controller lock. A later-day restart finalizes recovered
past dates before refreshing baselines; it never resumes their watering.

The metrics directory also contains checksummed `runtime_state.json`, its `.bak`
mirror, and temporary export/replacement files. Corrupt evidence is retained as
`.corrupt-<id>` rather than silently replaced by healthy-looking empty data.
Checkpointing normally bounds unflushed accounting to 30 seconds plus the
previous/current flush durations; storage failures remove that bound. Only one
process may write a metrics directory. Preserve these files together with the
CSV during backup and any separately authorized deployment.

Scheduling, next-run previews and simulation share pure calculations. Date/time,
season, UV (including zero) and rain overrides affect the simulation rather than
changing live sensors or sending alerts. A continuously rain-inhibited simulated
job still consumes its dispatched lifetime/slot, but has no simulated open time.

The UI creates a client-only schedule draft on Add; only Save persists it.
Cancel creates no active schedule. Polling rejects stale/out-of-order responses,
shows offline/fault status outside the flow panel, preserves best-effort emergency
Close and uses server countdowns. A lost/failed response is never proof of closure.

## Service template and deployment boundary

`irrigate.service` is a **template**, not evidence of the installed unit name.
The existing deployment uses case-sensitive **`Irrigate.service`**, running from
`/home/pi/Irrigate`. The template now requests SIGTERM, and both SIGTERM/SIGINT
use the same cleanup path. A bounded systemd stop cannot guarantee closure of
latching hardware after power loss, SIGKILL or a stalled/failed GPIO interface.

Deployment requires separate explicit approval. Before an authorized deployment,
preserve the Pi's `config.json`, `data`, `log.txt`, `xRPi` and other deployment-specific
state; verify the existing unit and real GPIO adapter; prepare a rollback that
does not restore the masking stub; and perform separately authorized real-device
checks. Do not copy sensitive live configuration into this repository, restore
the old `RPi` fake, blindly overwrite state or automatically rename/restart the
installed service.

CI defines hermetic Windows/Linux checks. A Windows pass or mocked GPIO test is
not evidence that Linux execution, real valve timing or deployment was verified.
