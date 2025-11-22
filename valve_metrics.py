import csv
import os
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

METRICS_FILE = os.path.join("data", "valve_metrics.csv")

def append_daily_summary(valve_name, date, total_seconds, total_liters):
    """
    Append a daily summary for a valve to the CSV file.
    Only call this if the valve actually operated (total_seconds > 0).
    
    Args:
        valve_name: Name of the valve
        date: Date string in YYYY-MM-DD format
        total_seconds: Total seconds valve was open
        total_liters: Total liters used
    """
    if total_seconds <= 0:
        return  # Don't record days when valve didn't operate
    
    # Calculate average liters per minute
    avg_liters_per_minute = (total_liters / total_seconds) * 60 if total_seconds > 0 else 0
    
    # Create data directory and file with header if it doesn't exist
    os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
    file_exists = os.path.isfile(METRICS_FILE)
    
    with open(METRICS_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['valve_name', 'date', 'total_seconds', 'total_liters', 'avg_liters_per_minute'])
        writer.writerow([valve_name, date, total_seconds, round(total_liters, 2), round(avg_liters_per_minute, 2)])


def load_baselines(valves_dict, logger):
    """
    Load the last 30 days of data for each valve and calculate baselines.
    Updates valve objects with baseline metrics.
    
    Args:
        valves_dict: Dictionary of valve objects keyed by name
        logger: Logger instance
    """
    if not os.path.isfile(METRICS_FILE):
        logger.info("No valve metrics file found. Baselines will be calculated after data is collected.")
        return
    
    # Read all data from CSV
    valve_data = defaultdict(list)
    
    with open(METRICS_FILE, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            valve_data[row['valve_name']].append({
                'date': datetime.strptime(row['date'], '%Y-%m-%d').date(),
                'total_seconds': float(row['total_seconds']),
                'total_liters': float(row['total_liters']),
                'avg_liters_per_minute': float(row['avg_liters_per_minute'])
            })
    
    # Calculate baselines for each valve
    cutoff_date = datetime.now().date() - timedelta(days=30)
    
    for valve_name, valve in valves_dict.items():
        if valve_name not in valve_data:
            logger.info(f"No historical data for valve '{valve_name}'")
            valve.baseline_lpm = None
            valve.baseline_trend = None
            valve.baseline_std_dev = None
            valve.baseline_sample_count = 0
            continue
        
        # Filter to last 30 days
        recent_data = [d for d in valve_data[valve_name] if d['date'] > cutoff_date]
        
        if len(recent_data) < 10:
            logger.info(f"Insufficient data for valve '{valve_name}' (only {len(recent_data)} days, need 10+)")
            valve.baseline_lpm = None
            valve.baseline_trend = None
            valve.baseline_std_dev = None
            valve.baseline_sample_count = len(recent_data)
            continue
        
        # Sort by date
        recent_data.sort(key=lambda x: x['date'])
        
        # Extract avg_liters_per_minute values
        lpm_values = [d['avg_liters_per_minute'] for d in recent_data]
        
        # Calculate weighted average (more weight to recent data)
        n = len(lpm_values)
        weighted_sum = 0
        weight_total = 0
        
        for i, lpm in enumerate(lpm_values):
            # Weight: older = 0.5, middle = 1.0, recent = 1.5
            if i < n / 3:
                weight = 0.5
            elif i < 2 * n / 3:
                weight = 1.0
            else:
                weight = 1.5
            
            weighted_sum += lpm * weight
            weight_total += weight
        
        baseline_lpm = weighted_sum / weight_total
        
        # Calculate standard deviation
        std_dev = statistics.stdev(lpm_values) if len(lpm_values) > 1 else 0
        
        # Calculate trend (linear regression slope) - only if enough samples
        baseline_trend_pct = None
        if len(lpm_values) >= 14:
            # y = mx + b, where x is day index (0 to n-1), y is lpm
            x_values = list(range(len(lpm_values)))
            x_mean = statistics.mean(x_values)
            y_mean = statistics.mean(lpm_values)
            
            numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, lpm_values))
            denominator = sum((x - x_mean) ** 2 for x in x_values)
            
            # Slope represents change in lpm per day
            trend = numerator / denominator if denominator != 0 else 0
            
            # Convert to percentage change per 30 days
            baseline_trend_pct = (trend * 30 / baseline_lpm * 100) if baseline_lpm > 0 else 0
        
        # Update valve object
        valve.baseline_lpm = round(baseline_lpm, 2)
        valve.baseline_trend = round(baseline_trend_pct, 2) if baseline_trend_pct is not None else None
        valve.baseline_std_dev = round(std_dev, 2)
        valve.baseline_sample_count = len(recent_data)
        
        trend_text = f"{valve.baseline_trend:+.2f}% per month" if valve.baseline_trend is not None else "N/A (need 14+ samples)"
        logger.info(f"Valve '{valve_name}' baseline: {valve.baseline_lpm} L/min, "
                   f"trend: {trend_text}, "
                   f"std dev: {valve.baseline_std_dev}, "
                   f"samples: {valve.baseline_sample_count}")


def write_daily_summaries(valves_dict, date_str, logger):
    """
    Write daily summaries for all valves that operated today.
    
    Args:
        valves_dict: Dictionary of valve objects keyed by name
        date_str: Date string in YYYY-MM-DD format
        logger: Logger instance
    """
    for valve_name, valve in valves_dict.items():
        if valve.secondsDaily > 0:
            append_daily_summary(valve_name, date_str, valve.secondsDaily, valve.litersDaily)
            logger.info(f"Wrote daily summary for valve '{valve_name}': "
                       f"{valve.secondsDaily}s, {valve.litersDaily:.2f}L")
