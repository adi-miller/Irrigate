import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from schedule_simulator import ScheduleSimulator

app = FastAPI(title="Irrigate API", version="1.0.0")

# Global reference to Irrigate instance
irrigate_instance = None


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


def run_api_server(irrigate, host="0.0.0.0", port=8000):
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
        access_log=True,   # Disable access logs to avoid clutter
        use_colors=True     # Enable colors in logs for better readability
    )
    server = uvicorn.Server(config)
    server.run()
