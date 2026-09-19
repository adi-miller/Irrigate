from abc import ABC, abstractmethod


class AlertChannel(ABC):
    """Local-only construction; one bounded delivery attempt per send()."""

    @abstractmethod
    def send(self, alert) -> bool:
        """Send an alert via this channel.

        Returns True only on confirmed success, False on failure. Do not retry
        or sleep here: AlertManager owns backoff and invokes this off control
        threads. Implementations must bound their I/O with a timeout.
        """
        pass
