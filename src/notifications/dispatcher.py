"""Notification dispatcher: routes events to active providers."""

import logging

import httpx

from src.models.config import NotificationEventKind
from src.models.resolved_config import ResolvedGlobalNotificationProvidersConfig
from src.utils.timeouts import env_timeout

from .events import (
    NotificationDispatchResult,
    NotificationEvent,
    NotificationPolicy,
    NotificationReportEvent,
)
from .providers.base import NotificationProvider

_STATUS_EVENT_KINDS: dict[str, NotificationEventKind] = {
    "success": "success",
    "failure": "error",
    "lock_error": "error",
    "config_error": "error",
    "unexpected_error": "error",
    "skipped": "skipped",
}
"""Terminale Run-Status auf die Ereignisart, die sie auslösen.

``cancelled`` fehlt bewusst: dafür wird nie benachrichtigt.
"""


class NotificationDispatcher:
    """Builds active providers and dispatches notification events.

    Provider failures are caught and logged; they never propagate to callers.
    """

    TIMEOUT_ENV = "DK_NOTIFICATION_TIMEOUT"
    DEFAULT_TIMEOUT = 10

    def __init__(
        self, config: ResolvedGlobalNotificationProvidersConfig, logger: logging.Logger
    ) -> None:
        self._logger = logger
        self._providers: list[NotificationProvider] = self._build_providers(config, logger)

    @staticmethod
    def _build_providers(
        config: ResolvedGlobalNotificationProvidersConfig, logger: logging.Logger
    ) -> list[NotificationProvider]:
        providers: list[NotificationProvider] = []
        if config.mail is not None:
            from .providers.mail import MailProvider  # noqa: PLC0415

            timeout = env_timeout(
                NotificationDispatcher.TIMEOUT_ENV, NotificationDispatcher.DEFAULT_TIMEOUT, logger
            )
            providers.append(MailProvider(config.mail, timeout))
        if config.pushover is not None:
            from .providers.pushover import PushoverProvider  # noqa: PLC0415

            timeout = env_timeout(
                NotificationDispatcher.TIMEOUT_ENV, NotificationDispatcher.DEFAULT_TIMEOUT, logger
            )
            providers.append(PushoverProvider(config.pushover, timeout))
        return providers

    def notify(
        self,
        event: NotificationEvent,
        policy: NotificationPolicy,
    ) -> NotificationDispatchResult:
        """Send event to all active providers if the policy allows it.

        Args:
            event: Fully formed event describing the execution outcome.
            policy: Resolved notify_on_* flags for the backup, workflow or rclone task.

        Returns:
            A result summary for test assertions. Never raises.
        """
        if not self._should_notify(event.status, policy):
            return NotificationDispatchResult(attempted=0, succeeded=0, failed=0)

        if not self._providers:
            self._logger.warning(
                "Notification requested for %s (%s) but no providers are configured",
                _event_label(event),
                event.status,
            )
            return NotificationDispatchResult(attempted=0, succeeded=0, failed=0)

        kind = _STATUS_EVENT_KINDS[event.status]
        providers = self._carrying_providers(self._providers, kind)
        if not providers:
            self._logger.info(
                "Notification for %s (%s) was not sent: no configured provider routes %r events. "
                "Adjust the 'events' list of the notification providers.",
                _event_label(event),
                event.status,
                kind,
            )
            return NotificationDispatchResult(attempted=0, succeeded=0, failed=0)

        attempted = 0
        succeeded = 0
        failed = 0
        for provider in providers:
            attempted += 1
            try:
                provider.send(event)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                    self._logger.error(
                        "Notification provider %r timed out for %s (%s): %s. "
                        "Adjust %s to change the notification timeout.",
                        provider.name,
                        _event_label(event),
                        event.status,
                        exc,
                        self.TIMEOUT_ENV,
                    )
                else:
                    self._logger.error(
                        "Notification provider %r failed for %s (%s): %s",
                        provider.name,
                        _event_label(event),
                        event.status,
                        exc,
                    )

        return NotificationDispatchResult(attempted=attempted, succeeded=succeeded, failed=failed)

    def notify_report(self, event: NotificationReportEvent) -> NotificationDispatchResult:
        """Send a periodic report to all active providers.

        Reports are opt-in at the scheduler/config layer, so no per-task
        NotificationPolicy is applied here.
        """
        if not self._providers:
            self._logger.warning(
                "Notification report requested for %s to %s but no providers are configured",
                event.window_start,
                event.window_end,
            )
            return NotificationDispatchResult(attempted=0, succeeded=0, failed=0)

        providers = self._carrying_providers(self._providers, "report")
        if not providers:
            self._logger.info(
                "Report for %s to %s was not sent: no configured provider routes 'report' events. "
                "Adjust the 'events' list of the notification providers.",
                event.window_start,
                event.window_end,
            )
            return NotificationDispatchResult(attempted=0, succeeded=0, failed=0)

        attempted = 0
        succeeded = 0
        failed = 0
        for provider in providers:
            attempted += 1
            try:
                provider.send_report(event)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                    self._logger.error(
                        "Notification provider %r timed out for report %s to %s: %s. "
                        "Adjust %s to change the notification timeout.",
                        provider.name,
                        event.window_start,
                        event.window_end,
                        exc,
                        self.TIMEOUT_ENV,
                    )
                else:
                    self._logger.error(
                        "Notification provider %r failed for report %s to %s: %s",
                        provider.name,
                        event.window_start,
                        event.window_end,
                        exc,
                    )

        return NotificationDispatchResult(attempted=attempted, succeeded=succeeded, failed=failed)

    @staticmethod
    def _carrying_providers(
        providers: list[NotificationProvider], kind: NotificationEventKind
    ) -> list[NotificationProvider]:
        """Return the providers whose channel routing carries an event kind.

        Ein Provider ohne gesetztes ``events`` hat keinen Routing-Filter und
        trägt alles.
        """
        return [p for p in providers if p.events is None or kind in p.events]

    @staticmethod
    def _should_notify(status: str, policy: NotificationPolicy) -> bool:
        kind = _STATUS_EVENT_KINDS.get(status)
        if kind == "success":
            return policy.notify_on_success
        if kind == "error":
            return policy.notify_on_error
        if kind == "skipped":
            return policy.notify_on_skipped
        return False


def _event_label(event: NotificationEvent) -> str:
    if event.task_type == "backup":
        return f"{event.job_name}.backup.{event.task_name}"
    if event.task_type == "rclone":
        return f"{event.job_name}.rclone.{event.task_name}"
    if event.task_type == "workflow":
        return f"{event.job_name}.workflow.{event.task_name}"
    raise ValueError(f"Unknown notification task type: {event.task_type!r}")
