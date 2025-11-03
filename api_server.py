"""
Simple FastAPI server for Irrigate system
Provides a REST API endpoint for schedule simulation
"""
import uvicorn
import calendar
from fastapi import FastAPI
from datetime import datetime, timedelta
from schedule_simulator import ScheduleSimulator

app = FastAPI(title="Irrigate API", version="1.0.0")

# Global reference to Irrigate instance
irrigate_instance = None


@app.post("/api/simulate")
async def simulate_schedule(
    date: str = None,
    time: str = None, 
    uv: float = None,
    season: str = None,
    rain: bool = None,
    days: int = None
):
    """
    Run irrigation schedule simulation and return results as JSON
    
    Query parameters:
    - date: Date in YYYY-MM-DD or MM-DD format
    - time: Time in HH:MM or HH:MM:SS format
    - uv: UV index override (0-15)
    - season: Season override (Spring, Summer, Fall, Winter)
    - rain: Weather sensor should disable irrigation (true/false)
    - days: Number of days to simulate (default: 1)
    """
    if irrigate_instance is None:
        return {"error": "Irrigate system not initialized"}, 503
    
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
        schedule = simulator.get_todays_schedule()
        sim_datetime = simulator.get_simulation_datetime()
        
        # Build response
        jobs_list = []
        
        for i, job in enumerate(schedule, 1):
            sched = job['schedule']
            
            jobs_list.append({
                "job_number": i,
                "valve_name": job['valve_name'],
                "date": job['sim_date'].strftime('%Y-%m-%d'),
                "day_of_week": calendar.day_abbr[job['sim_date'].weekday()],
                "scheduled_time": job['schedule_time'].strftime('%H:%M:%S'),
                "actual_start_time": job['actual_start'].strftime('%H:%M:%S'),
                "actual_end_time": job['actual_end'].strftime('%H:%M:%S'),
                "queue_delay_minutes": round(job['queue_delay_minutes'], 2),
                "base_duration_minutes": job['base_duration'],
                "actual_duration_minutes": job['duration_minutes'],
                "uv_adjusted": sched.enable_uv_adjustments
            })
        
        # Build overrides dict
        overrides = {}
        if simulator.override_date:
            overrides['date'] = simulator.override_date.strftime('%Y-%m-%d')
        if simulator.override_time:
            overrides['time'] = simulator.override_time.strftime('%H:%M:%S')
        if simulator.override_uv is not None:
            overrides['uv'] = simulator.override_uv
        if simulator.override_season:
            overrides['season'] = simulator.override_season
        if simulator.override_should_disable is not None:
            overrides['weather_disables'] = simulator.override_should_disable
        
        return {
            "success": True,
            "simulation_datetime": sim_datetime.strftime('%Y-%m-%d %H:%M:%S'),
            "timezone": irrigate_instance.cfg.timezone,
            "total_jobs": len(jobs_list),
            "overrides": overrides,
            "jobs": jobs_list
        }
        
    except Exception as ex:
        irrigate_instance.logger.error(f"Error in simulate endpoint: {format(ex)}")
        return {"error": str(ex)}, 500


def run_api_server(irrigate, host="0.0.0.0", port=8000):
    """
    Start the FastAPI server in a thread
    
    Args:
        irrigate: The Irrigate instance to use for API operations
        host: Host to bind to (default: 0.0.0.0 for all interfaces)
        port: Port to listen on (default: 8000)
    """
    global irrigate_instance
    irrigate_instance = irrigate
    
    irrigate.logger.info(f"Starting FastAPI server on {host}:{port}")
    irrigate.logger.info(f"API documentation available at http://{host}:{port}/docs")
    
    # Configure uvicorn to run without reloader (important for threading)
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,  # Disable access logs to avoid clutter
        use_colors=False    # Disable colors in logs for cleaner file output
    )
    server = uvicorn.Server(config)
    server.run()
