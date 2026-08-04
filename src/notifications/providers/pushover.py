"""Pushover notification provider."""

import os

import httpx

from src.models.config import NotificationEventKind
from src.models.resolved_config import ResolvedPushoverNotificationConfig

from ..events import NotificationEvent, NotificationReportEvent
from ..formatting import (
    format_push_html_message,
    format_report_push_html_message,
    format_report_push_title,
    format_subject,
)

_PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


class PushoverProvider:
    """Sends push notifications via the Pushover Messages API.

    Token and user key are resolved from environment variables at send time so
    they are never stored in the object or included in logs.
    """

    name = "pushover"

    def __init__(self, config: ResolvedPushoverNotificationConfig, timeout: int) -> None:
        self._config = config
        self._timeout = timeout
        self.events: list[NotificationEventKind] | None = config.events

    def send(self, event: NotificationEvent) -> None:
        """Build and deliver a Pushover notification for the given event.

        Args:
            event: The notification event to deliver.

        Raises:
            ValueError: When a required credential env var is missing or empty.
            httpx.HTTPStatusError: When the Pushover API returns a non-2xx response.
            httpx.RequestError: On network or connection failures.
        """
        cfg = self._config
        token, user_key = self._resolve_credentials()

        payload: dict[str, object] = {
            "token": token,
            "user": user_key,
            "title": format_subject(event),
            "message": format_push_html_message(event),
            "html": 1,
            "priority": cfg.priority,
        }

        if cfg.sound is not None:
            payload["sound"] = cfg.sound
        if cfg.device is not None:
            payload["device"] = cfg.device

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(_PUSHOVER_API_URL, data=payload)
            response.raise_for_status()

    def send_report(self, event: NotificationReportEvent) -> None:
        """Build and deliver a Pushover notification for the given report event."""
        cfg = self._config
        token, user_key = self._resolve_credentials()

        payload: dict[str, object] = {
            "token": token,
            "user": user_key,
            "title": format_report_push_title(event),
            "message": format_report_push_html_message(event),
            "html": 1,
            "priority": cfg.priority,
        }

        if cfg.sound is not None:
            payload["sound"] = cfg.sound
        if cfg.device is not None:
            payload["device"] = cfg.device

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(_PUSHOVER_API_URL, data=payload)
            response.raise_for_status()

    def _resolve_credentials(self) -> tuple[str, str]:
        """Read Pushover credentials from environment variables at send time.

        Returns:
            Tuple of (token, user_key).

        Raises:
            ValueError: When a required env var is missing or empty.
        """
        cfg = self._config
        token = os.environ.get(cfg.token_env)
        if not token:
            raise ValueError(
                f"Environment variable '{cfg.token_env}' is not set or empty "
                "(required for Pushover token)"
            )
        user_key = os.environ.get(cfg.user_key_env)
        if not user_key:
            raise ValueError(
                f"Environment variable '{cfg.user_key_env}' is not set or empty "
                "(required for Pushover user key)"
            )
        return token, user_key
