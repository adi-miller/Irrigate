from abc import ABC, abstractmethod


class AlertChannel(ABC):
    """Base class for alert notification channels."""

    @abstractmethod
    def send(self, alert) -> bool:
        """Send an alert via this channel.

        Returns True on success, False on failure.
        """
        pass
