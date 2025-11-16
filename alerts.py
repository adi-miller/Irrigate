from enum import Enum
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, Any


class AlertType(Enum):
    LEAK = "leak"
    MALFUNCTION_NO_FLOW = "malfunction_no_flow"
    IRREGULAR_FLOW = "irregular_flow"
    SYSTEM_EXIT = "system_exit"
    SENSOR_ERROR = "sensor_error"


class AlertSeverity(Enum):
    WARNING = "warning"
    CRITICAL = "critical"


# Map alert types to their severity
ALERT_SEVERITY_MAP = {
    AlertType.LEAK: AlertSeverity.CRITICAL,
    AlertType.MALFUNCTION_NO_FLOW: AlertSeverity.WARNING,
    AlertType.IRREGULAR_FLOW: AlertSeverity.WARNING,
    AlertType.SYSTEM_EXIT: AlertSeverity.CRITICAL,
    AlertType.SENSOR_ERROR: AlertSeverity.WARNING,
}


@dataclass
class Alert:
    """Represents a single alert occurrence"""
    type: AlertType
    valve_name: Optional[str]  # None for system-wide alerts
    timestamp: datetime
    message: str
    data: Dict[str, Any]  # Context data (flow rates, baselines, etc.)
    
    @property
    def severity(self) -> AlertSeverity:
        return ALERT_SEVERITY_MAP[self.type]
    
    def to_dict(self):
        """Convert alert to dictionary for logging/serialization"""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "valve_name": self.valve_name,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "data": self.data
        }


class AlertManager:
    """Manages alert notification and rate limiting"""
    
    def __init__(self, logger, config, irrigate_instance):
        self.logger = logger
        self.config = config
        self.irrigate = irrigate_instance  # Reference to Irrigate instance for schedule evaluation
        
        # Load alert configuration
        alerts_cfg = config.cfg.alerts
        
        # Alert enabled/disabled flags
        self.enabled = {
            AlertType.LEAK: alerts_cfg.enabled.leak,
            AlertType.MALFUNCTION_NO_FLOW: alerts_cfg.enabled.malfunction_no_flow,
            AlertType.IRREGULAR_FLOW: alerts_cfg.enabled.irregular_flow,
            AlertType.SENSOR_ERROR: alerts_cfg.enabled.sensor_error,
            AlertType.SYSTEM_EXIT: alerts_cfg.enabled.system_exit,
        }
        
        # Other alert configuration
        self.leak_repeat_minutes = alerts_cfg.leak_repeat_minutes
        self.leak_detection_exclusions = alerts_cfg.leak_detection_exclusions
        
        # State tracking: key = (alert_type, valve_name), value = last_alerted_time
        self._alert_state: Dict[tuple, datetime] = {}
        
        self.logger.info("AlertManager initialized")
    
    def _should_alert(self, alert_type: AlertType, valve_name: Optional[str] = None) -> bool:
        """Check if we should fire this alert based on repeat logic"""
        key = (alert_type, valve_name)
        
        # LEAK has repeat logic (every N minutes)
        if alert_type == AlertType.LEAK:
            if key in self._alert_state:
                time_since_last = datetime.now() - self._alert_state[key]
                if time_since_last < timedelta(minutes=self.leak_repeat_minutes):
                    return False
            return True
        
        # All other alerts: only fire once until state is cleared
        return key not in self._alert_state
    
    def _record_alert(self, alert_type: AlertType, valve_name: Optional[str] = None):
        """Record that an alert was fired"""
        key = (alert_type, valve_name)
        self._alert_state[key] = datetime.now()
    
    def clear_alert_state(self, alert_type: AlertType, valve_name: Optional[str] = None):
        """Clear alert state (e.g., when condition no longer exists)"""
        key = (alert_type, valve_name)
        if key in self._alert_state:
            del self._alert_state[key]
    
    def _notify(self, alert: Alert):
        """Send notifications for an alert"""
        # For now, just log it with appropriate level
        log_message = f"ALERT [{alert.severity.value.upper()}] {alert.type.value}"
        if alert.valve_name:
            log_message += f" (valve: {alert.valve_name})"
        log_message += f": {alert.message}"
        
        if alert.severity == AlertSeverity.CRITICAL:
            self.logger.critical(log_message)
        else:
            self.logger.warning(log_message)
        
        # Log additional context data if present
        if alert.data:
            self.logger.info(f"  Alert data: {alert.data}")
    
    def is_in_exclusion_window(self, now: datetime) -> bool:
        """Check if current time is within any leak detection exclusion window.
        Reuses existing schedule evaluation logic from Irrigate class."""
        if not self.leak_detection_exclusions:
            return False
        
        for exclusion_sched in self.leak_detection_exclusions:
            # Check if schedule should run today (day/season filters)
            if not self.irrigate.shouldScheduleRun(exclusion_sched, check_date=now):
                continue
            
            # Calculate when this exclusion window starts
            start_time = self.irrigate.calculateScheduleTime(exclusion_sched, now)
            
            # Calculate when it ends (start + duration)
            end_time = start_time + timedelta(minutes=exclusion_sched.duration)
            
            # Check if now is within this window
            if start_time <= now < end_time:
                return True
        
        return False
    
    def alert(self, alert_type: AlertType, message: str, valve_name: Optional[str] = None, 
              data: Optional[Dict[str, Any]] = None):
        """Fire an alert if conditions allow"""
        # Check if this alert type is enabled
        if not self.enabled[alert_type]:
            return
        
        if self._should_alert(alert_type, valve_name):
            alert = Alert(
                type=alert_type,
                valve_name=valve_name,
                timestamp=datetime.now(),
                message=message,
                data=data or {}
            )
            self._notify(alert)
            self._record_alert(alert_type, valve_name)
