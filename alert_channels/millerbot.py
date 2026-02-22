import time
import requests
from alert_channels.base import AlertChannel


class MillerBotChannel(AlertChannel):
    """Alert channel that sends notifications via the MillerBot proactive endpoint."""

    def __init__(self, logger, cfg):
        self.logger = logger
        self.url = cfg.url
        self.user_id = cfg.user_id
        self.api_key = cfg.api_key
        self.role = cfg.role

    def _format_message(self, alert) -> str:
        lines = [
            f"[{alert.severity.value.upper()}] {alert.type.value}",
            f"Time: {alert.timestamp.isoformat()}",
        ]
        if alert.valve_name:
            lines.append(f"Valve: {alert.valve_name}")
        lines.append(f"Message: {alert.message}")
        if alert.data:
            lines.append("Data:")
            for k, v in alert.data.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def send(self, alert) -> bool:
        message = self._format_message(alert)
        payload = {
            "user_id": self.user_id,
            "query": message,
            "role": self.role,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }

        for attempt in range(5):
            try:
                response = requests.post(self.url, headers=headers, json=payload, timeout=10)
                response.raise_for_status()
                self.logger.info(f"MillerBot alert sent successfully: {alert.type.value}")
                return True
            except requests.exceptions.RequestException as e:
                delay = 2 * (2 ** attempt)
                if attempt < 4:
                    self.logger.warning(
                        f"MillerBot send failed (attempt {attempt + 1}/5), retrying in {delay}s: {e}"
                    )
                    time.sleep(delay)
                else:
                    self.logger.error(f"MillerBot send failed after 5 attempts: {e}")
                    return False
