"""Runtime-only notification context captured when an operational run starts."""

import logging
from dataclasses import dataclass
from pathlib import Path

from ..models.resolved_config import (
    ResolvedGlobalNotificationProvidersConfig,
    ResolvedNotificationsConfig,
)
from .dispatcher import NotificationDispatcher
from .events import NotificationPolicy, NotificationTaskType


@dataclass(frozen=True)
class NotificationContext:
    """Dispatcher, policy and messages tied to one config snapshot."""

    dispatcher: NotificationDispatcher
    policy: NotificationPolicy
    task_type: NotificationTaskType
    task_name: str
    success_message: str
    failure_message: str
    skipped_message: str
    unexpected_error_message: str
    log_path: Path | None


def notification_policy(notifications: ResolvedNotificationsConfig) -> NotificationPolicy:
    return NotificationPolicy(
        notify_on_success=notifications.notify_on_success,
        notify_on_error=notifications.notify_on_error,
        notify_on_skipped=notifications.notify_on_skipped,
    )


def build_notification_context(
    *,
    providers: ResolvedGlobalNotificationProvidersConfig,
    notifications: ResolvedNotificationsConfig,
    logger: logging.Logger,
    task_type: NotificationTaskType,
    task_name: str,
    display_target: str,
    log_path: Path | None,
) -> NotificationContext:
    """Capture dispatcher, policy and stable messages for one run."""
    label = task_type.capitalize()
    return NotificationContext(
        dispatcher=NotificationDispatcher(providers, logger),
        policy=notification_policy(notifications),
        task_type=task_type,
        task_name=task_name,
        success_message=f"{label} {display_target} completed successfully",
        failure_message=f"{label} {display_target} failed",
        skipped_message=f"{label} {display_target} skipped: job already running",
        unexpected_error_message=f"{label} {display_target} encountered an unexpected error",
        log_path=log_path,
    )
