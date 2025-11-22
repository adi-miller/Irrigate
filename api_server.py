import uvicorn
import model
import pytz
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from schedule_simulator import ScheduleSimulator
from datetime import datetime, timedelta
from suntime import Sun
import time

app = FastAPI(title="Irrigate API", version="1.0.0")

# Global reference to Irrigate instance
irrigate_instance = None

# Cache for next scheduled runs
# Structure: {"data": {...}, "timestamp": float, "ttl": int}
next_runs_cache = {"data": None, "timestamp": 0, "ttl": 300}  # 5 minute TTL

def invalidate_next_runs_cache():
    """Invalidate the next scheduled runs cache"""
    global next_runs_cache
    next_runs_cache["timestamp"] = 0
    next_runs_cache["data"] = None


def is_cache_valid():
    """Check if the next runs cache is still valid"""
    if next_runs_cache["data"] is None:
        return False
    age = time.time() - next_runs_cache["timestamp"]
    return age < next_runs_cache["ttl"]


def get_next_scheduled_runs():
    global next_runs_cache
    
    if is_cache_valid():
        return next_runs_cache["data"]
    
    result = {}
    
    try:
        tz = pytz.timezone(irrigate_instance.cfg.timezone)
        now = tz.localize(datetime.now())
        tomorrow = now + timedelta(days=1)
        tomorrow_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        
        all_jobs = []
        
        # Simulation 1: Today from now onwards
        simulator_today = ScheduleSimulator(irrigate_instance)
        simulator_today.override_time = now.time()
        jobs_today = simulator_today.get_scheduled_jobs_for_simulation()
        all_jobs.extend(jobs_today)
        
        # Simulation 2: Next 6 full days (tomorrow through day 6)
        simulator_future = ScheduleSimulator(irrigate_instance)
        simulator_future.simulate_days = 6
        simulator_future.override_date = tomorrow_midnight.date()
        simulator_future.override_time = tomorrow_midnight.time()
        jobs_future = simulator_future.get_scheduled_jobs_for_simulation()
        all_jobs.extend(jobs_future)
        
        # Find the earliest job for each valve
        for job in all_jobs:
            valve_name = job['valve_name']
            schedule_time = job['schedule_time']
            
            # Find the schedule index in the valve's schedules
            valve = job['valve']
            schedule_obj = job['schedule']
            schedule_index = 0
            for idx, sched in enumerate(valve.schedules):
                if sched is schedule_obj:
                    schedule_index = idx
                    break
            
            # Only keep the earliest run for each valve
            if valve_name not in result or schedule_time < result[valve_name]['schedule_time']:
                result[valve_name] = {
                    'schedule_time': schedule_time,
                    'schedule_time_iso': schedule_time.isoformat(),
                    'duration_minutes': job['duration_minutes'],
                    'schedule_index': schedule_index
                }
        
        # Update cache
        next_runs_cache["data"] = result
        next_runs_cache["timestamp"] = time.time()
        
        return result
        
    except Exception as ex:
        irrigate_instance.logger.error(f"Error calculating next scheduled runs: {ex}")
        import traceback
        irrigate_instance.logger.error(traceback.format_exc())
        return {}


@app.get("/api/status")
async def get_full_status():
    """Get complete system status"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    # Return raw properties - let the UI determine status strings
    valves = []
    for name, v in irrigate_instance.valves.items():
        valves.append({
            "name": name,
            "enabled": v.enabled,
            "is_open": v.is_open,
            "handled": v.handled,
            "seconds_daily": v.secondsDaily,
            "liters_daily": v.litersDaily,
            "seconds_remain": v.secondsRemain,
            "seconds_duration": getattr(v, 'secondsDuration', 0),  # Total job duration
            "seconds_last": v.secondsLast if hasattr(v, 'secondsLast') else 0,
            "liters_last": v.litersLast if hasattr(v, 'litersLast') else 0,
        })
    
    sensors = []
    for name, s in irrigate_instance.sensors.items():
        sensor_data = {
            "name": name,
            "type": s.type if hasattr(s, 'type') else "unknown",
            "enabled": s.enabled if hasattr(s, 'enabled') else False,
        }
        
        # Try to get sensor methods (may fail if sensor has errors)
        try:
            sensor_data["should_disable"] = s.shouldDisable() if hasattr(s, 'shouldDisable') else False
            sensor_data["factor"] = s.getFactor() if hasattr(s, 'getFactor') else 1.0
            sensor_data["telemetry"] = s.getTelemetry(True) if hasattr(s, 'getTelemetry') else {}
        except Exception:
            sensor_data["should_disable"] = None
            sensor_data["factor"] = None
            sensor_data["telemetry"] = {}
            sensor_data["error"] = True
        
        sensors.append(sensor_data)
    
    # Calculate current time info, season, and sunrise/sunset
    tz = pytz.timezone(irrigate_instance.cfg.timezone)
    now = datetime.now(tz)
    lat, lon = irrigate_instance.cfg.getLatLon()
    season = irrigate_instance.getSeason(lat, now)
    
    # Calculate sunrise and sunset
    sun = Sun(lat, lon)
    now_naive = now.replace(tzinfo=None)
    sunrise = sun.get_sunrise_time(at_date=now_naive, time_zone=tz)
    sunrise = sunrise.replace(year=now.year, month=now.month, day=now.day)
    sunset = sun.get_sunset_time(at_date=now_naive, time_zone=tz)
    sunset = sunset.replace(year=now.year, month=now.month, day=now.day)
    
    # Get waterflow data
    waterflow_data = {
        "enabled": False,
        "type": None,
        "flow_rate_lpm": 0,
        "is_active": False,
        "leak_detection_enabled": False,
        "last_update": None,
        "history": []
    }
    
    if irrigate_instance.waterflow:
        waterflow_data["enabled"] = irrigate_instance.waterflow.enabled
        waterflow_data["type"] = irrigate_instance.waterflow.type
        waterflow_data["leak_detection_enabled"] = irrigate_instance.waterflow.leakdetection
        
        if irrigate_instance.waterflow.started:
            flow_rate = irrigate_instance.waterflow.lastLiter_1m()
            waterflow_data["flow_rate_lpm"] = round(flow_rate, 2)
            waterflow_data["is_active"] = flow_rate > 0
            
            # Get history (last 60 minutes)
            if hasattr(irrigate_instance.waterflow, 'getHistory'):
                waterflow_data["history"] = irrigate_instance.waterflow.getHistory()
            
            # Get last update time if available
            if hasattr(irrigate_instance.waterflow, '_lastupdate'):
                waterflow_data["last_update"] = irrigate_instance.waterflow._lastupdate.isoformat()
    
    return {
        "system": {
            "status": irrigate_instance._status,
            "temp_status": list(irrigate_instance._tempStatus.keys()),
            "uptime_minutes": int((datetime.now() - irrigate_instance.startTime).total_seconds() / 60),
            "started_at": irrigate_instance.startTime.isoformat(),
            "current_time": now.isoformat(),
            "season": season,
            "sunrise": sunrise.isoformat(),
            "sunset": sunset.isoformat(),
            "timezone": irrigate_instance.cfg.timezone
        },
        "valves": valves,
        "sensors": sensors,
        "waterflow": waterflow_data
    }


@app.get("/api/valves")
async def get_valves():
    """Get all valves summary"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    valves = []
    for name, v in irrigate_instance.valves.items():
        valves.append({
            "name": name,
            "enabled": v.enabled,
            "is_open": v.is_open,
            "seconds_remain": v.secondsRemain,
        })
    
    return {"valves": valves}


@app.get("/api/valves/{valve_name}")
async def get_valve_details(valve_name: str):
    """Get detailed valve information"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    v = irrigate_instance.valves[valve_name]
    
    # Build schedules array
    schedules = []
    for i, s in enumerate(v.schedules):
        schedules.append({
            "index": i,
            "seasons": s.seasons if hasattr(s, 'seasons') else [],
            "days": s.days if hasattr(s, 'days') else [],
            "time_based_on": s.time_based_on,
            "fixed_start_time": s.fixed_start_time if hasattr(s, 'fixed_start_time') else None,
            "offset_minutes": s.offset_minutes if hasattr(s, 'offset_minutes') else 0,
            "duration": s.duration,
            "enable_uv_adjustments": s.enable_uv_adjustments
        })
    
    return {
        "name": v.name,
        "type": v.config.type,
        "enabled": v.enabled,
        "is_open": v.is_open,
        "handled": v.handled,
        "sensor_name": v.sensor.config.name if hasattr(v, 'sensor') else None,
        "seconds_daily": v.secondsDaily,
        "liters_daily": v.litersDaily,
        "seconds_remain": v.secondsRemain,
        "seconds_last": v.secondsLast if hasattr(v, 'secondsLast') else 0,
        "liters_last": v.litersLast if hasattr(v, 'litersLast') else 0,
        "schedules": schedules,
        "has_waterflow": v.waterflow is not None
    }


@app.get("/api/next-runs")
async def get_next_runs():
    """Get next scheduled run for each valve (cached for efficiency)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    next_runs = get_next_scheduled_runs()
    
    return {
        "next_runs": next_runs,
        "cache_age_seconds": int(time.time() - next_runs_cache["timestamp"]) if next_runs_cache["data"] else 0,
        "cache_ttl_seconds": next_runs_cache["ttl"]
    }


@app.get("/api/sensors")
async def get_sensors():
    """Get all sensor data"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    sensors = []
    for name, s in irrigate_instance.sensors.items():
        sensor_data = {
            "name": name,
            "type": s.type if hasattr(s, 'type') else "unknown",
            "enabled": s.enabled if hasattr(s, 'enabled') else False,
        }
        
        try:
            sensor_data["should_disable"] = s.shouldDisable() if hasattr(s, 'shouldDisable') else False
            sensor_data["factor"] = s.getFactor() if hasattr(s, 'getFactor') else 1.0
            sensor_data["telemetry"] = s.getTelemetry() if hasattr(s, 'getTelemetry') else {}
        except Exception:
            sensor_data["error"] = True
        
        sensors.append(sensor_data)
    
    return {"sensors": sensors}


@app.get("/api/queue")
async def get_queue():
    """Get current job queue"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    # Get all items from queue without removing them
    queue_items = []
    temp_items = []
    
    # Extract items from queue
    while not irrigate_instance.q.empty():
        try:
            job = irrigate_instance.q.get_nowait()
            temp_items.append(job)
            queue_items.append({
                "valve_name": job.valve.name,
                "duration_minutes": job.duration,
                "is_scheduled": job.sched is not None,
                "schedule_index": getattr(job.sched, 'index', None) if job.sched else None
            })
        except:
            break
    
    # Put items back in queue
    for job in temp_items:
        irrigate_instance.q.put(job)
    
    return {
        "queue_size": len(queue_items),
        "jobs": queue_items
    }


@app.get("/api/config")
async def get_config():
    """Get system configuration (read-only)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    cfg = irrigate_instance.cfg
    
    # Get alerts configuration
    alerts_config = {}
    if hasattr(cfg.cfg, 'alerts'):
        alerts_cfg = cfg.cfg.alerts
        alerts_config = {
            "enabled": {
                "leak": alerts_cfg.enabled.leak if hasattr(alerts_cfg, 'enabled') else True,
                "malfunction_no_flow": alerts_cfg.enabled.malfunction_no_flow if hasattr(alerts_cfg, 'enabled') else True,
                "irregular_flow": alerts_cfg.enabled.irregular_flow if hasattr(alerts_cfg, 'enabled') else True,
                "sensor_error": alerts_cfg.enabled.sensor_error if hasattr(alerts_cfg, 'enabled') else True,
                "system_exit": alerts_cfg.enabled.system_exit if hasattr(alerts_cfg, 'enabled') else True,
            },
            "leak_repeat_minutes": alerts_cfg.leak_repeat_minutes if hasattr(alerts_cfg, 'leak_repeat_minutes') else 15,
            "irregular_flow_threshold": alerts_cfg.irregular_flow_threshold if hasattr(alerts_cfg, 'irregular_flow_threshold') else 2.0,
        }
    
    # Get waterflow configuration
    waterflow_config = {}
    if irrigate_instance.waterflow:
        waterflow_config = {
            "enabled": irrigate_instance.waterflow.enabled,
            "type": irrigate_instance.waterflow.type,
            "leak_detection": irrigate_instance.waterflow.leakdetection
        }
    
    # Get sensors configuration
    sensors_config = []
    for name, sensor in irrigate_instance.sensors.items():
        sensor_cfg = {
            "name": name,
            "type": sensor.type if hasattr(sensor, 'type') else 'unknown',
            "enabled": sensor.enabled if hasattr(sensor, 'enabled') else False
        }
        
        # Add OpenWeatherMap specific config
        if sensor.type == 'OpenWeatherMap' and hasattr(sensor, 'precip_days'):
            sensor_cfg["precipitation"] = {
                "days_to_aggregate": sensor.precip_days,
                "disable_threshold_mm": sensor.precip_threshold
            }
        
        sensors_config.append(sensor_cfg)
    
    return {
        "timezone": cfg.timezone,
        "location": {
            "latitude": cfg.latitude,
            "longitude": cfg.longitude
        },
        "max_concurrent_valves": cfg.valvesConcurrency,
        "telemetry_enabled": cfg.telemetry,
        "mqtt_enabled": cfg.mqttEnabled,
        "valve_count": len(irrigate_instance.valves),
        "sensor_count": len(irrigate_instance.sensors),
        "alerts": alerts_config,
        "waterflow": waterflow_config,
        "sensors": sensors_config
    }


@app.post("/api/config/alerts/enabled")
async def update_alert_enabled(request: dict):
    """Update alert enabled/disabled state"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    alert_type = request.get("alert_type")
    enabled = request.get("enabled")
    
    if not alert_type or enabled is None:
        raise HTTPException(status_code=400, detail="Missing alert_type or enabled")
    
    # Update the alert manager
    from alerts import AlertType
    alert_type_enum = AlertType(alert_type)
    irrigate_instance.alerts.enabled[alert_type_enum] = enabled
    
    # Update config file
    irrigate_instance.cfg.cfg.alerts.enabled.__dict__[alert_type] = enabled
    irrigate_instance.cfg.save_runtime_config()
    
    irrigate_instance.logger.info(f"Alert '{alert_type}' {'enabled' if enabled else 'disabled'}")
    
    return {"success": True, "alert_type": alert_type, "enabled": enabled}


@app.post("/api/config/alerts/settings")
async def update_alert_setting(request: dict):
    """Update alert settings (leak_repeat_minutes, irregular_flow_threshold)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    setting = request.get("setting")
    value = request.get("value")
    
    if not setting or value is None:
        raise HTTPException(status_code=400, detail="Missing setting or value")
    
    # Update the alert manager
    if setting == "leak_repeat_minutes":
        irrigate_instance.alerts.leak_repeat_minutes = int(value)
        irrigate_instance.cfg.cfg.alerts.leak_repeat_minutes = int(value)
    elif setting == "irregular_flow_threshold":
        irrigate_instance.alerts.irregular_flow_threshold = float(value)
        irrigate_instance.cfg.cfg.alerts.irregular_flow_threshold = float(value)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {setting}")
    
    # Save config file
    irrigate_instance.cfg.save_runtime_config()
    
    irrigate_instance.logger.info(f"Alert setting '{setting}' updated to {value}")
    
    return {"success": True, "setting": setting, "value": value}


@app.post("/api/config/waterflow")
async def update_waterflow_config(request: dict):
    """Update waterflow configuration (enabled, leak_detection)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if not irrigate_instance.waterflow:
        raise HTTPException(status_code=400, detail="Waterflow not configured in system")
    
    setting = request.get("setting")
    value = request.get("value")
    
    if not setting or value is None:
        raise HTTPException(status_code=400, detail="Missing setting or value")
    
    # Update waterflow settings
    if setting == "enabled":
        irrigate_instance.waterflow.enabled = bool(value)
        irrigate_instance.cfg.cfg.waterflow.enabled = bool(value)
        irrigate_instance.logger.info(f"Waterflow {'enabled' if value else 'disabled'} (requires restart to take effect)")
    elif setting == "leak_detection":
        irrigate_instance.waterflow.leakdetection = bool(value)
        irrigate_instance.cfg.cfg.waterflow.leakdetection = bool(value)
        irrigate_instance.logger.info(f"Waterflow leak detection {'enabled' if value else 'disabled'}")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {setting}")
    
    # Save config file
    irrigate_instance.cfg.save_runtime_config()
    
    return {"success": True, "setting": setting, "value": value}


@app.post("/api/config/sensors/{sensor_name}")
async def update_sensor_config(sensor_name: str, request: dict):
    """Update sensor configuration settings"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if sensor_name not in irrigate_instance.sensors:
        raise HTTPException(status_code=404, detail=f"Sensor '{sensor_name}' not found")
    
    sensor = irrigate_instance.sensors[sensor_name]
    setting = request.get("setting")
    value = request.get("value")
    
    if not setting or value is None:
        raise HTTPException(status_code=400, detail="Missing setting or value")
    
    # Find sensor config in cfg
    sensor_cfg = None
    for s in irrigate_instance.cfg.cfg.sensors:
        if s.name == sensor_name:
            sensor_cfg = s
            break
    
    if not sensor_cfg:
        raise HTTPException(status_code=404, detail=f"Sensor config for '{sensor_name}' not found")
    
    # Update sensor-specific settings
    if sensor.type == 'OpenWeatherMap':
        if setting == "precip_days":
            sensor.precip_days = int(value)
            if not hasattr(sensor_cfg, 'precipitation'):
                from types import SimpleNamespace
                sensor_cfg.precipitation = SimpleNamespace()
            sensor_cfg.precipitation.days_to_aggregate = int(value)
            irrigate_instance.logger.info(f"Sensor '{sensor_name}' precipitation days updated to {value}")
        elif setting == "precip_threshold":
            sensor.precip_threshold = float(value)
            if not hasattr(sensor_cfg, 'precipitation'):
                from types import SimpleNamespace
                sensor_cfg.precipitation = SimpleNamespace()
            sensor_cfg.precipitation.disable_threshold_mm = float(value)
            irrigate_instance.logger.info(f"Sensor '{sensor_name}' precipitation threshold updated to {value}mm")
        else:
            raise HTTPException(status_code=400, detail=f"Unknown setting: {setting}")
    else:
        raise HTTPException(status_code=400, detail=f"Sensor type '{sensor.type}' settings not supported")
    
    # Save config file
    irrigate_instance.cfg.save_runtime_config()
    
    return {"success": True, "sensor": sensor_name, "setting": setting, "value": value}


@app.post("/api/valves/{valve_name}/start-manual")
async def start_valve_manual(valve_name: str, duration_minutes: float = 5):
    """Immediately open valve (bypass queue, no concurrency check)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.is_open = True  # Track state
    valve.open()
    irrigate_instance.logger.info(f"Manual start: Valve '{valve_name}' opened manually")
    
    return {
        "success": True,
        "valve": valve_name,
        "action": "opened_manual"
    }


@app.post("/api/valves/{valve_name}/queue")
async def queue_valve(valve_name: str, duration_minutes: float):
    """Queue a job for this valve (respects concurrency)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    job = model.Job(valve=valve, duration=duration_minutes, sched=None)
    irrigate_instance.queueJob(job)
    
    return {
        "success": True,
        "valve": valve_name,
        "duration_minutes": duration_minutes,
        "action": "queued",
        "queued_at": datetime.now().isoformat()
    }


@app.post("/api/valves/{valve_name}/stop")
async def stop_valve(valve_name: str):
    """Immediately close valve"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.is_open = False  # Track state (job will detect and terminate)
    valve.close()
    irrigate_instance.logger.info(f"Manual stop: Valve '{valve_name}' closed")
    
    return {
        "success": True,
        "valve": valve_name,
        "action": "closed"
    }


@app.post("/api/valves/{valve_name}/enable")
async def enable_valve(valve_name: str):
    """Enable valve"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.enabled = True
    
    # Persist changes to config file
    irrigate_instance.cfg.save_runtime_config()
    
    irrigate_instance.logger.info(f"Valve '{valve_name}' enabled")
    
    invalidate_next_runs_cache()
    
    return {"success": True, "valve": valve_name, "action": "enabled"}


@app.post("/api/valves/{valve_name}/disable")
async def disable_valve(valve_name: str):
    """Disable valve"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.enabled = False
    
    # Persist changes to config file
    irrigate_instance.cfg.save_runtime_config()
    
    irrigate_instance.logger.info(f"Valve '{valve_name}' disabled")
    
    invalidate_next_runs_cache()
    
    return {"success": True, "valve": valve_name, "action": "disabled"}


@app.put("/api/valves/{valve_name}/schedules/{schedule_index}")
async def update_valve_schedule(valve_name: str, schedule_index: int, schedule_data: dict):
    """Update a specific schedule for a valve
    
    Request body should contain schedule fields:
    {
        "seasons": ["Spring", "Summer"],  // optional
        "days": ["Mon", "Tue", "Wed"],    // optional
        "time_based_on": "fixed|sunrise|sunset",
        "fixed_start_time": "06:00",      // required if time_based_on is "fixed"
        "offset_minutes": -30,            // optional, for sunrise/sunset
        "duration": 20,                   // minutes
        "enable_uv_adjustments": true     // optional
    }
    """
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    
    if schedule_index < 0 or schedule_index >= len(valve.schedules):
        raise HTTPException(status_code=404, detail=f"Schedule index {schedule_index} not found for valve '{valve_name}'")
    
    try:
        # Update the schedule object in memory
        sched = valve.schedules[schedule_index]
        
        # Update fields that are provided
        if "seasons" in schedule_data:
            sched.seasons = schedule_data["seasons"]
        if "days" in schedule_data:
            sched.days = schedule_data["days"]
        if "time_based_on" in schedule_data:
            sched.time_based_on = schedule_data["time_based_on"]
        if "fixed_start_time" in schedule_data:
            sched.fixed_start_time = schedule_data["fixed_start_time"]
        if "offset_minutes" in schedule_data:
            sched.offset_minutes = schedule_data["offset_minutes"]
        if "duration" in schedule_data:
            sched.duration = schedule_data["duration"]
        if "enable_uv_adjustments" in schedule_data:
            sched.enable_uv_adjustments = schedule_data["enable_uv_adjustments"]
        
        # Validate the schedule
        if sched.time_based_on == "fixed" and not hasattr(sched, 'fixed_start_time'):
            raise HTTPException(status_code=400, detail="fixed_start_time is required when time_based_on is 'fixed'")
        
        # Persist changes to config file
        irrigate_instance.cfg.save_runtime_config()
        
        irrigate_instance.logger.info(f"Updated schedule {schedule_index} for valve '{valve_name}'")
        
        invalidate_next_runs_cache()
        
        return {
            "success": True,
            "valve": valve_name,
            "schedule_index": schedule_index,
            "action": "schedule_updated"
        }
        
    except Exception as ex:
        irrigate_instance.logger.error(f"Error updating schedule: {ex}")
        raise HTTPException(status_code=400, detail=str(ex))


@app.post("/api/valves/{valve_name}/schedules")
async def create_valve_schedule(valve_name: str, schedule_data: dict):
    """Create a new schedule for a valve
    
    Request body should contain schedule fields:
    {
        "seasons": ["Spring", "Summer"],  // optional
        "days": ["Mon", "Tue", "Wed"],    // optional
        "time_based_on": "fixed|sunrise|sunset",
        "fixed_start_time": "06:00",      // required if time_based_on is "fixed"
        "offset_minutes": -30,            // optional, for sunrise/sunset
        "duration": 20,                   // minutes
        "enable_uv_adjustments": true     // optional
    }
    """
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    
    try:
        from types import SimpleNamespace
        
        # Create a new schedule object
        new_schedule = SimpleNamespace()
        
        # Set required and optional fields
        new_schedule.seasons = schedule_data.get("seasons", [])
        new_schedule.days = schedule_data.get("days", [])
        new_schedule.time_based_on = schedule_data.get("time_based_on", "fixed")
        new_schedule.duration = schedule_data.get("duration", 10)
        new_schedule.enable_uv_adjustments = schedule_data.get("enable_uv_adjustments", False)
        
        # Handle time-based fields
        if new_schedule.time_based_on == "fixed":
            if "fixed_start_time" not in schedule_data:
                raise HTTPException(status_code=400, detail="fixed_start_time is required when time_based_on is 'fixed'")
            new_schedule.fixed_start_time = schedule_data["fixed_start_time"]
        else:
            new_schedule.offset_minutes = schedule_data.get("offset_minutes", 0)
        
        # Add the new schedule to the valve
        valve.schedules.append(new_schedule)
        
        # Persist changes to config file
        irrigate_instance.cfg.save_runtime_config()
        
        schedule_index = len(valve.schedules) - 1
        irrigate_instance.logger.info(f"Created new schedule {schedule_index} for valve '{valve_name}'")
        
        invalidate_next_runs_cache()
        
        return {
            "success": True,
            "valve": valve_name,
            "schedule_index": schedule_index,
            "action": "schedule_created"
        }
        
    except HTTPException:
        raise
    except Exception as ex:
        irrigate_instance.logger.error(f"Error creating schedule: {ex}")
        raise HTTPException(status_code=400, detail=str(ex))


@app.delete("/api/valves/{valve_name}/schedules/{schedule_index}")
async def delete_valve_schedule(valve_name: str, schedule_index: int):
    """Delete a specific schedule from a valve"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    
    if schedule_index < 0 or schedule_index >= len(valve.schedules):
        raise HTTPException(status_code=404, detail=f"Schedule index {schedule_index} not found for valve '{valve_name}'")
    
    if len(valve.schedules) == 1:
        raise HTTPException(status_code=400, detail=f"Cannot delete the last schedule for valve '{valve_name}'. A valve must have at least one schedule.")
    
    try:
        # Remove the schedule
        deleted_schedule = valve.schedules.pop(schedule_index)
        
        # Persist changes to config file
        irrigate_instance.cfg.save_runtime_config()
        
        irrigate_instance.logger.info(f"Deleted schedule {schedule_index} from valve '{valve_name}'")
        
        invalidate_next_runs_cache()
        
        return {
            "success": True,
            "valve": valve_name,
            "schedule_index": schedule_index,
            "action": "schedule_deleted",
            "remaining_schedules": len(valve.schedules)
        }
        
    except Exception as ex:
        irrigate_instance.logger.error(f"Error deleting schedule: {ex}")
        raise HTTPException(status_code=400, detail=str(ex))


@app.put("/api/valves/{valve_name}/enabled")
async def update_valve_enabled(valve_name: str, enabled: bool):
    """Update valve enabled status and persist to config
    
    Request body: {"enabled": true/false}
    """
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.enabled = enabled
    
    # Persist changes to config file
    irrigate_instance.cfg.save_runtime_config()
    
    irrigate_instance.logger.info(f"Valve '{valve_name}' enabled status set to {enabled}")
    
    invalidate_next_runs_cache()
    
    return {
        "success": True,
        "valve": valve_name,
        "enabled": enabled,
        "action": "enabled_updated"
    }


@app.post("/api/simulate", response_class=PlainTextResponse)
async def simulate_schedule(
    date: str = None,
    time: str = None, 
    uv: float = None,
    season: str = None,
    rain: bool = None,
    days: int = None
):
    """
    Run irrigation schedule simulation and return formatted text output
    
    Query parameters:
    - date: Date in YYYY-MM-DD or MM-DD format
    - time: Time in HH:MM or HH:MM:SS format
    - uv: UV index override (0-15)
    - season: Season override (Spring, Summer, Fall, Winter)
    - rain: Weather sensor should disable irrigation (true/false)
    - days: Number of days to simulate (default: 1)
    """
    if irrigate_instance is None:
        return "ERROR: Irrigate system not initialized", 503
    
    try:
        # Create simulator instance
        simulator = ScheduleSimulator(irrigate_instance)
        
        # Build options string from parameters
        options = []
        if date:
            options.append(f"date:{date}")
        if time:
            options.append(f"time:{time}")
        if uv is not None:
            options.append(f"uv:{uv}")
        if season:
            options.append(f"season:{season}")
        if rain is not None:
            options.append(f"rain:{'yes' if rain else 'no'}")
        if days and days > 1:
            options.append(f"days:{days}")
        
        # Parse options and run simulation
        simulator.parse_schedule_options(','.join(options))
        
        # Return the formatted schedule text (same as --simulate output)
        return simulator.format_schedule()
        
    except Exception as ex:
        irrigate_instance.logger.error(f"Error in simulate endpoint: {format(ex)}")
        return f"ERROR: {str(ex)}", 500


# Serve static files
app.mount("/static", StaticFiles(directory="web/static"), name="static")

@app.get("/")
async def serve_frontend():
    """Serve the main web UI"""
    return FileResponse('web/index.html')


def run_api_server(irrigate, host="0.0.0.0", port=8000):
    global irrigate_instance
    irrigate_instance = irrigate
    
    irrigate.logger.info(f"Starting FastAPI server on {host}:{port}")
    irrigate.logger.info(f"API documentation available at http://{host}:{port}/docs")
    irrigate.logger.info(f"Web UI available at http://{host}:{port}/")
    
    # Configure uvicorn to run without reloader (important for threading)
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
        use_colors=True
    )
    server = uvicorn.Server(config)
    server.run()
