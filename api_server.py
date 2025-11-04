import uvicorn
import model
import pytz
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from schedule_simulator import ScheduleSimulator
from datetime import datetime, timedelta
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
            "suspended": v.suspended,
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
            sensor_data["telemetry"] = s.getTelemetry() if hasattr(s, 'getTelemetry') else {}
        except Exception:
            sensor_data["should_disable"] = None
            sensor_data["factor"] = None
            sensor_data["telemetry"] = {}
            sensor_data["error"] = True
        
        sensors.append(sensor_data)
    
    return {
        "system": {
            "status": irrigate_instance._status,
            "temp_status": list(irrigate_instance._tempStatus.keys()),
            "uptime_minutes": int((datetime.now() - irrigate_instance.startTime).total_seconds() / 60),
            "started_at": irrigate_instance.startTime.isoformat()
        },
        "valves": valves,
        "sensors": sensors,
        "waterflow": {
            "enabled": irrigate_instance.waterflow.enabled if irrigate_instance.waterflow else False,
        }
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
            "suspended": v.suspended,
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
        "suspended": v.suspended,
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
    return {
        "timezone": cfg.timezone,
        "location": {
            "latitude": cfg.latitude,
            "longitude": cfg.longitude
        },
        "max_concurrent_valves": cfg.valvesConcurrency,
        "telemetry_enabled": cfg.telemetry,
        "mqtt_enabled": cfg.mqttEnabled,
        "uv_adjustments": [
            {"max_uv_index": adj.max_uv_index, "multiplier": adj.multiplier}
            for adj in cfg.cfg.uv_adjustments
        ],
        "valve_count": len(irrigate_instance.valves),
        "sensor_count": len(irrigate_instance.sensors)
    }


@app.post("/api/valves/{valve_name}/start-manual")
async def start_valve_manual(valve_name: str, duration_minutes: float = 5):
    """Immediately open valve (bypass queue, no concurrency check)"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
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
    irrigate_instance.logger.info(f"Valve '{valve_name}' disabled")
    
    invalidate_next_runs_cache()
    
    return {"success": True, "valve": valve_name, "action": "disabled"}


@app.post("/api/valves/{valve_name}/suspend")
async def suspend_valve(valve_name: str):
    """Suspend valve"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.suspended = True
    irrigate_instance.logger.info(f"Valve '{valve_name}' suspended")
    
    invalidate_next_runs_cache()
    
    return {"success": True, "valve": valve_name, "action": "suspended"}


@app.post("/api/valves/{valve_name}/resume")
async def resume_valve(valve_name: str):
    """Resume valve"""
    if irrigate_instance is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if valve_name not in irrigate_instance.valves:
        raise HTTPException(status_code=404, detail=f"Valve '{valve_name}' not found")
    
    valve = irrigate_instance.valves[valve_name]
    valve.suspended = False
    irrigate_instance.logger.info(f"Valve '{valve_name}' resumed")
    
    invalidate_next_runs_cache()
    
    return {"success": True, "valve": valve_name, "action": "resumed"}


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
