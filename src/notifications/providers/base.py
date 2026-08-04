"""Base protocol for notification providers."""

from typing import Protocol

from src.models.config import NotificationEventKind

from ..events import NotificationEvent, NotificationReportEvent


class NotificationProvider(Protocol):
    """Protocol that every notification provider must satisfy."""

    name: str

    events: list[NotificationEventKind] | None
    """Ereignisarten, die dieser Kanal transportiert; ``None`` = alle.

    Reines Routing: die Liste kann nur einschränken, was die Task-Policy
    ohnehin auslöst.
    """

    def send(self, event: NotificationEvent) -> None:
        """Deliver the notification event.

        Args:
            event: The fully formed notification event to deliver.

        Raises:
            Exception: Any provider-specific delivery error. The dispatcher
                catches all exceptions, so providers should let errors propagate.
        """
        ...

    def send_report(self, event: NotificationReportEvent) -> None:
        """Deliver a periodic report notification.

        Args:
            event: The fully formed report event to deliver.

        Raises:
            Exception: Any provider-specific delivery error. The dispatcher
                catches all exceptions, so providers should let errors propagate.
        """
        ...
