import copy
import logging
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from suntime import Sun

import model
from alerts import AlertType
from controller import ControlError
from schedule_simulator import ScheduleSimulator
from scheduling import uv_factor
from sensors.base_sensor import SensorUnavailable


app = FastAPI(title="Irrigate API", version="1.0.0")
irrigate_instance = None
next_runs_cache = {"data": None, "timestamp": 0, "ttl": 300}
_cache_lock = threading.Lock()
_cache_generation = 0
_cache_instance = None
_LEGACY_ALERT_FLAGS = (
    "leak", "malfunction_no_flow", "irregular_flow", "sensor_error", "system_exit",
)
_WEB_DIRECTORY = Path(__file__).resolve().parent / "web"


def _instance():
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    return irrigate_instance


def _valve(instance, name):
    if name not in instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{name}' not found")
    return instance.valves[name]


def _candidate_valve(candidate, name):
    for valve in candidate["valves"]:
        if valve["name"] == name:
            return valve
    raise HTTPException(status_code=404, detail=f"Valve '{name}' not found")


def _local_now(instance):
    return instance.clock.now().astimezone(pytz.timezone(instance.cfg.timezone))


@app.exception_handler(RequestValidationError)
async def log_request_validation_error(request: Request, exc: RequestValidationError):
    logger = irrigate_instance.logger if irrigate_instance is not None else logging.getLogger("Irrigate.API")
    route = request.scope.get("route")
    # Route templates and error counts are safe; raw input, bodies and URLs are not.
    logger.warning(
        "API request validation rejected: route=%s errors=%s",
        getattr(route, "path", "<unmatched>"), len(exc.errors()),
    )
    return await request_validation_exception_handler(request, exc)


def _control_error(instance, error):
    instance.logger.warning("API control request rejected (status %s)", error.status_code)
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


async def _run_control(instance, action, *args):
    try:
        return await run_in_threadpool(action, *args)
    except ControlError as error:
        _control_error(instance, error)
    except Exception as error:
        instance.logger.error("API control action failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Control action failed; see system health") from error


async def _update_config(instance, mutator, *, enabled_updates=None):
    try:
        if enabled_updates is None:
            result = await run_in_threadpool(instance.update_config, mutator)
        else:
            result = await run_in_threadpool(
                instance.update_config, mutator, enabled_updates=enabled_updates,
            )
    except HTTPException:
        raise
    except ControlError as error:
        _control_error(instance, error)
    except ValueError as error:
        instance.logger.warning("API configuration candidate rejected (%s)", type(error).__name__)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        instance.logger.error("API configuration update failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Configuration update failed; see system health") from error
    invalidate_next_runs_cache()
    return result


def _boolean(value, name):
    if type(value) is not bool:
        raise HTTPException(status_code=400, detail=f"{name} must be a boolean")
    return value


def _number(value, conversion):
    try:
        if isinstance(value, bool):
            raise ValueError("Boolean is not a numeric setting")
        number = conversion(value)
        if not math.isfinite(number):
            raise ValueError("Numeric settings must be finite")
        return number
    except (ValueError, TypeError, OverflowError) as error:
        raise HTTPException(status_code=400, detail="Invalid numeric setting value") from error


def invalidate_next_runs_cache():
    global _cache_generation, _cache_instance
    with _cache_lock:
        next_runs_cache["timestamp"] = 0
        next_runs_cache["data"] = None
        _cache_generation += 1
        _cache_instance = None


def _cache_valid(instance):
    age = instance.clock.monotonic() - next_runs_cache["timestamp"]
    return (
        _cache_instance is instance and next_runs_cache["data"] is not None
        and 0 <= age < next_runs_cache["ttl"]
    )


def is_cache_valid():
    if irrigate_instance is None:
        return False
    with _cache_lock:
        return _cache_valid(irrigate_instance)


def get_next_scheduled_runs():
    global _cache_instance
    instance = _instance()
    with _cache_lock:
        if _cache_valid(instance):
            return copy.deepcopy(next_runs_cache["data"])
        generation = _cache_generation
    try:
        now = _local_now(instance)
        today = ScheduleSimulator(instance)
        today.override_date = now.date()
        today.override_time = now.time()
        future = ScheduleSimulator(instance)
        future.simulate_days = 6
        future.override_date = (now + timedelta(days=1)).date()
        future.override_time = now.replace(hour=0, minute=0, second=0, microsecond=0).time()
        jobs = today.get_scheduled_jobs_for_simulation() + future.get_scheduled_jobs_for_simulation()
        result = {}
        for job in jobs:
            name, scheduled = job["valve_name"], job["schedule_time"]
            index = job.get("schedule_index")
            if index is None:
                index = next(
                    (i for i, schedule in enumerate(job["valve"].schedules) if schedule is job["schedule"]), 0,
                )
            if name not in result or scheduled < result[name]["schedule_time"]:
                result[name] = {
                    "schedule_time": scheduled,
                    "schedule_time_iso": scheduled.isoformat(),
                    "duration_minutes": job["duration_minutes"],
                    "schedule_index": index,
                }
        with _cache_lock:
            if generation == _cache_generation and irrigate_instance is instance:
                next_runs_cache["data"] = copy.deepcopy(result)
                next_runs_cache["timestamp"] = instance.clock.monotonic()
                _cache_instance = instance
        return result
    except Exception as error:
        instance.logger.error("Next-run prediction failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Unable to calculate next scheduled runs") from error


def _weather_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SensorUnavailable("Weather reading is not numeric")
    if not math.isfinite(value) or value < 0:
        raise SensorUnavailable("Weather reading is invalid")
    return value


def _sensor_projection(name, sensor, full_status=False):
    result = {
        "name": name, "type": getattr(sensor, "type", "unknown"),
        "enabled": getattr(sensor, "enabled", False),
    }
    try:
        if callable(getattr(sensor, "snapshot", None)):
            observation = sensor.snapshot()
            uv = _weather_number(observation["uv"])
            precipitation = _weather_number(observation["recentPrecip"])
            values = {
                "should_disable": precipitation > sensor.precip_threshold,
                "factor": uv_factor(uv, getattr(sensor, "uv_adjustments", [])),
                "telemetry": {"uv": uv, "recentPrecip": precipitation},
            }
        else:
            if not all(callable(getattr(sensor, method, None)) for method in (
                "shouldDisable", "getFactor", "getTelemetry",
            )):
                raise SensorUnavailable("Sensor does not expose a complete reading")
            values = {
                "should_disable": sensor.shouldDisable(),
                "factor": sensor.getFactor(),
                "telemetry": sensor.getTelemetry(True) if full_status else sensor.getTelemetry(),
            }
            for key in ("uv", "recentPrecip"):
                if key in values["telemetry"]:
                    _weather_number(values["telemetry"][key])
        if not math.isfinite(values["factor"]):
            raise SensorUnavailable("Weather factor is invalid")
        result.update(values)
    except Exception:
        if full_status:
            result.update(should_disable=None, factor=None, telemetry={})
        result["error"] = True
    return result


def _waterflow_projection(flow):
    result = {
        "enabled": False, "type": None, "flow_rate_lpm": 0, "is_active": False,
        "leak_detection_enabled": False, "last_update": None, "history": [],
    }
    if flow is None:
        return result
    result.update(
        enabled=flow.enabled, type=flow.type, leak_detection_enabled=flow.leakdetection,
    )
    if flow.started:
        observation = flow.snapshot()
        value = observation["value"]
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        result["flow_rate_lpm"] = round(value, 2) if valid else 0
        result["is_active"] = valid and value > 0
        timestamp = observation["timestamp"]
        result["last_update"] = timestamp.isoformat() if timestamp is not None else None
        result["history"] = flow.getHistory()
    return result


@app.get("/api/status")
async def get_full_status():
    """Return the legacy status projection without consuming observations."""
    instance = _instance()
    valves = instance.controller.snapshot()
    sensors = [_sensor_projection(name, sensor, True) for name, sensor in instance.sensors.items()]
    now = _local_now(instance)
    tz = pytz.timezone(instance.cfg.timezone)
    lat, lon = instance.cfg.getLatLon()
    sun = Sun(lat, lon)
    now_naive = now.replace(tzinfo=None)
    sunrise = sun.get_sunrise_time(at_date=now_naive, time_zone=tz).replace(
        year=now.year, month=now.month, day=now.day,
    )
    sunset = sun.get_sunset_time(at_date=now_naive, time_zone=tz).replace(
        year=now.year, month=now.month, day=now.day,
    )
    with instance._state_lock:
        status, temporary = instance._status, list(instance._tempStatus)
    return {
        "system": {
            "status": status,
            "temp_status": temporary,
            "uptime_minutes": int(max(0, instance.clock.monotonic() - instance._start_mono) / 60),
            "started_at": instance.startTime.replace(tzinfo=None).isoformat(),
            "current_time": now.isoformat(),
            "season": instance.getSeason(lat, now),
            "sunrise": sunrise.isoformat(),
            "sunset": sunset.isoformat(),
            "timezone": instance.cfg.timezone,
        },
        "valves": valves,
        "sensors": sensors,
        "waterflow": _waterflow_projection(instance.waterflow),
    }


@app.get("/api/health")
async def get_health():
    return _instance().get_health()


@app.get("/api/valves")
async def get_valves():
    instance = _instance()
    return {"valves": [
        {key: valve[key] for key in ("name", "enabled", "is_open", "seconds_remain")}
        for valve in instance.controller.snapshot()
    ]}


@app.get("/api/valves/{valve_name}")
async def get_valve_details(valve_name: str):
    instance = _instance()
    valve = _valve(instance, valve_name)
    with instance.controller.lock:
        values = instance.controller.valve_snapshot(valve_name)
        sensor = getattr(valve, "sensor", None)
        return {
            **{key: values[key] for key in (
                "name", "enabled", "is_open", "handled", "seconds_daily", "liters_daily",
                "seconds_remain", "seconds_last", "liters_last",
            )},
            "type": valve.config.type,
            "sensor_name": sensor.config.name if sensor is not None else None,
            "schedules": [
                {
                    "index": index, "seasons": list(schedule.seasons), "days": list(schedule.days),
                    "time_based_on": schedule.time_based_on,
                    "fixed_start_time": getattr(schedule, "fixed_start_time", None),
                    "offset_minutes": getattr(schedule, "offset_minutes", 0),
                    "duration": schedule.duration,
                    "enable_uv_adjustments": schedule.enable_uv_adjustments,
                }
                for index, schedule in enumerate(valve.schedules)
            ],
            "has_waterflow": valve.waterflow is not None,
            "baseline_lpm": valve.baseline_lpm,
            "baseline_trend": valve.baseline_trend,
            "baseline_std_dev": valve.baseline_std_dev,
            "baseline_sample_count": valve.baseline_sample_count,
        }


@app.get("/api/next-runs")
async def get_next_runs():
    instance = _instance()
    next_runs = await run_in_threadpool(get_next_scheduled_runs)
    with _cache_lock:
        age = (
            int(max(0, instance.clock.monotonic() - next_runs_cache["timestamp"]))
            if _cache_instance is instance and next_runs_cache["data"] else 0
        )
    return {
        "next_runs": next_runs, "cache_age_seconds": age,
        "cache_ttl_seconds": next_runs_cache["ttl"],
    }


@app.get("/api/sensors")
async def get_sensors():
    instance = _instance()
    return {"sensors": [_sensor_projection(name, sensor) for name, sensor in instance.sensors.items()]}


@app.get("/api/queue")
async def get_queue():
    jobs = _instance().controller.queue_snapshot()
    items = [{
        "valve_name": job.valve.name,
        "duration_minutes": job.duration,
        "is_scheduled": job.sched is not None,
        "schedule_index": getattr(job.sched, "index", None) if job.sched is not None else None,
    } for job in jobs]
    return {"queue_size": len(items), "jobs": items}


@app.get("/api/config")
async def get_config():
    instance = _instance()
    with instance.controller.lock:
        cfg = instance.cfg
        data = cfg.get_data()
        alerts = data["alerts"]
        flow = instance.waterflow
        sensors = []
        for name, sensor in instance.sensors.items():
            item = {
                "name": name, "type": getattr(sensor, "type", "unknown"),
                "enabled": getattr(sensor, "enabled", False),
            }
            if item["type"] == "OpenWeatherMap" and hasattr(sensor, "precip_days"):
                item["precipitation"] = {
                    "days_to_aggregate": sensor.precip_days,
                    "disable_threshold_mm": sensor.precip_threshold,
                }
            sensors.append(item)
        return {
            "timezone": cfg.timezone,
            "location": {"latitude": cfg.latitude, "longitude": cfg.longitude},
            "max_concurrent_valves": cfg.valvesConcurrency,
            "telemetry_enabled": cfg.telemetry,
            "mqtt_enabled": cfg.mqttEnabled,
            "valve_count": len(instance.valves),
            "sensor_count": len(instance.sensors),
            "alerts": {
                "enabled": {flag: alerts["enabled"][flag] for flag in _LEGACY_ALERT_FLAGS},
                "leak_repeat_minutes": alerts["leak_repeat_minutes"],
                "irregular_flow_threshold": alerts["irregular_flow_threshold"],
            },
            "waterflow": {
                "enabled": flow.enabled, "type": flow.type, "leak_detection": flow.leakdetection,
            } if flow is not None else {},
            "sensors": sensors,
        }


@app.post("/api/config/alerts/enabled")
async def update_alert_enabled(request: dict):
    instance = _instance()
    alert_type, enabled = request.get("alert_type"), request.get("enabled")
    if not alert_type or enabled is None:
        raise HTTPException(status_code=400, detail="Missing alert_type or enabled")
    if not isinstance(alert_type, str) or alert_type not in {item.value for item in AlertType}:
        raise HTTPException(status_code=400, detail="Unknown alert_type")
    enabled = _boolean(enabled, "enabled")
    await _update_config(instance, lambda data: data["alerts"]["enabled"].update({alert_type: enabled}))
    return {"success": True, "alert_type": alert_type, "enabled": enabled}


@app.post("/api/config/alerts/settings")
async def update_alert_setting(request: dict):
    instance = _instance()
    setting, value = request.get("setting"), request.get("value")
    if not setting or value is None:
        raise HTTPException(status_code=400, detail="Missing setting or value")
    if setting not in ("leak_repeat_minutes", "irregular_flow_threshold"):
        raise HTTPException(status_code=400, detail="Unknown setting")
    number = _number(value, int if setting == "leak_repeat_minutes" else float)
    await _update_config(instance, lambda data: data["alerts"].update({setting: number}))
    return {"success": True, "setting": setting, "value": value}


@app.post("/api/config/waterflow")
async def update_waterflow_config(request: dict):
    instance = _instance()
    if instance.waterflow is None:
        raise HTTPException(status_code=400, detail="Waterflow not configured in system")
    setting, value = request.get("setting"), request.get("value")
    if not setting or value is None:
        raise HTTPException(status_code=400, detail="Missing setting or value")
    if setting not in ("enabled", "leak_detection"):
        raise HTTPException(status_code=400, detail="Unknown setting")
    enabled = _boolean(value, "value")
    key = "leakdetection" if setting == "leak_detection" else setting
    await _update_config(instance, lambda data: data["waterflow"].update({key: enabled}))
    return {"success": True, "setting": setting, "value": value}


@app.post("/api/config/sensors/{sensor_name}")
async def update_sensor_config(sensor_name: str, request: dict):
    instance = _instance()
    if sensor_name not in instance.sensors:
        raise HTTPException(status_code=404, detail=f"Sensor '{sensor_name}' not found")
    sensor = instance.sensors[sensor_name]
    setting, value = request.get("setting"), request.get("value")
    if not setting or value is None:
        raise HTTPException(status_code=400, detail="Missing setting or value")
    if getattr(sensor, "type", None) != "OpenWeatherMap":
        raise HTTPException(status_code=400, detail="Sensor type settings not supported")
    if setting not in ("precip_days", "precip_threshold"):
        raise HTTPException(status_code=400, detail="Unknown setting")
    number = _number(value, int if setting == "precip_days" else float)
    key = "days_to_aggregate" if setting == "precip_days" else "disable_threshold_mm"

    def mutate(data):
        for item in data["sensors"]:
            if item["name"] == sensor_name:
                item["precipitation"][key] = number
                return
        raise HTTPException(status_code=404, detail=f"Sensor config for '{sensor_name}' not found")

    await _update_config(instance, mutate)
    return {"success": True, "sensor": sensor_name, "setting": setting, "value": value}


@app.post("/api/valves/{valve_name}/start-manual")
async def start_valve_manual(valve_name: str, duration_minutes: float = 30):
    """Start a bounded manual operation; duration_minutes is an optional query parameter."""
    instance = _instance()
    _valve(instance, valve_name)
    await _run_control(instance, instance.controller.start_manual, valve_name, duration_minutes)
    return {"success": True, "valve": valve_name, "action": "opened_manual"}


@app.post("/api/valves/{valve_name}/queue")
async def queue_valve(valve_name: str, duration_minutes: float):
    instance = _instance()
    valve = _valve(instance, valve_name)
    job = model.Job(valve=valve, duration=duration_minutes, sched=None)
    await _run_control(instance, instance.queueJob, job)
    return {
        "success": True, "valve": valve_name, "duration_minutes": duration_minutes,
        "action": "queued", "queued_at": _local_now(instance).replace(tzinfo=None).isoformat(),
    }


@app.post("/api/valves/{valve_name}/stop")
async def stop_valve(valve_name: str):
    instance = _instance()
    _valve(instance, valve_name)
    await _run_control(instance, instance.controller.stop, valve_name)
    return {"success": True, "valve": valve_name, "action": "closed"}


async def _set_valve_enabled(instance, name, enabled):
    _valve(instance, name)
    await _update_config(
        instance, lambda data: _candidate_valve(data, name).update(enabled=enabled),
        enabled_updates={name: enabled},
    )
    if not enabled and instance.controller.faults.get(name):
        _control_error(instance, ControlError(
            "Valve disabled, but close could not be confirmed; see system health and retry Close", 503,
        ))


@app.post("/api/valves/{valve_name}/enable")
async def enable_valve(valve_name: str):
    await _set_valve_enabled(_instance(), valve_name, True)
    return {"success": True, "valve": valve_name, "action": "enabled"}


@app.post("/api/valves/{valve_name}/disable")
async def disable_valve(valve_name: str):
    await _set_valve_enabled(_instance(), valve_name, False)
    return {"success": True, "valve": valve_name, "action": "disabled"}


def _schedule_at(valve, index):
    if index < 0 or index >= len(valve["schedules"]):
        raise HTTPException(
            status_code=404, detail=f"Schedule index {index} not found for valve '{valve['name']}'",
        )
    return valve["schedules"][index]


@app.put("/api/valves/{valve_name}/schedules/{schedule_index}")
async def update_valve_schedule(valve_name: str, schedule_index: int, schedule_data: dict):
    """Update only supplied schedule fields in a validated configuration candidate."""
    instance = _instance()
    _valve(instance, valve_name)

    def mutate(data):
        _schedule_at(_candidate_valve(data, valve_name), schedule_index).update(schedule_data)

    await _update_config(instance, mutate)
    return {
        "success": True, "valve": valve_name, "schedule_index": schedule_index,
        "action": "schedule_updated",
    }


@app.post("/api/valves/{valve_name}/schedules")
async def create_valve_schedule(valve_name: str, schedule_data: dict):
    instance = _instance()
    _valve(instance, valve_name)
    schedule = copy.deepcopy(schedule_data)
    for key, value in (
        ("seasons", []), ("days", []), ("time_based_on", "fixed"),
        ("duration", 10), ("enable_uv_adjustments", False),
    ):
        schedule.setdefault(key, value)
    if schedule["time_based_on"] == "fixed":
        if "fixed_start_time" not in schedule:
            raise HTTPException(status_code=400, detail="fixed_start_time is required when time_based_on is 'fixed'")
    else:
        schedule.setdefault("offset_minutes", 0)
    accepted = await _update_config(
        instance, lambda data: _candidate_valve(data, valve_name)["schedules"].append(schedule),
    )
    index = len(_candidate_valve(accepted, valve_name)["schedules"]) - 1
    return {"success": True, "valve": valve_name, "schedule_index": index, "action": "schedule_created"}


@app.delete("/api/valves/{valve_name}/schedules/{schedule_index}")
async def delete_valve_schedule(valve_name: str, schedule_index: int):
    instance = _instance()
    _valve(instance, valve_name)

    def mutate(data):
        valve = _candidate_valve(data, valve_name)
        _schedule_at(valve, schedule_index)
        if len(valve["schedules"]) == 1:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete the last schedule for valve '{valve_name}'. A valve must have at least one schedule.",
            )
        valve["schedules"].pop(schedule_index)

    accepted = await _update_config(instance, mutate)
    return {
        "success": True, "valve": valve_name, "schedule_index": schedule_index,
        "action": "schedule_deleted",
        "remaining_schedules": len(_candidate_valve(accepted, valve_name)["schedules"]),
    }


@app.put("/api/valves/{valve_name}/enabled")
async def update_valve_enabled(valve_name: str, enabled: bool):
    """Persist enabled status; enabled is a required boolean QUERY parameter."""
    await _set_valve_enabled(_instance(), valve_name, enabled)
    return {"success": True, "valve": valve_name, "enabled": enabled, "action": "enabled_updated"}


@app.post("/api/simulate", response_class=PlainTextResponse)
async def simulate_schedule(
    date: str = None, time: str = None, uv: float = None,
    season: str = None, rain: bool = None, days: int = None,
):
    """Return the legacy formatted schedule using a read-only simulator."""
    instance = _instance()
    options = []
    for key, value in (("date", date), ("time", time), ("uv", uv), ("season", season), ("days", days)):
        if value is not None:
            options.append(f"{key}:{value}")
    if rain is not None:
        options.append(f"rain:{'yes' if rain else 'no'}")

    def simulate():
        simulator = ScheduleSimulator(instance)
        simulator.parse_schedule_options(",".join(options))
        return simulator.format_schedule()

    try:
        return await run_in_threadpool(simulate)
    except (ValueError, OverflowError) as error:
        instance.logger.warning("API simulation input rejected (%s)", type(error).__name__)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        instance.logger.error("API simulation failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Unable to simulate schedule") from error


app.mount("/static", StaticFiles(directory=str(_WEB_DIRECTORY / "static")), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse(str(_WEB_DIRECTORY / "index.html"))


def run_api_server(irrigate, host="0.0.0.0", port=8000):
    global irrigate_instance
    irrigate_instance = irrigate
    invalidate_next_runs_cache()
    irrigate.logger.info("Starting FastAPI server on %s:%s", host, port)
    config = uvicorn.Config(
        app=app, host=host, port=port, log_level="info", access_log=False, use_colors=True,
    )
    server = uvicorn.Server(config)
    irrigate.api_server = server
    server.run()
