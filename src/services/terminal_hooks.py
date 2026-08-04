"""Shared RunManager terminal hooks for persistence and notifications."""

import asyncio
import logging

from ..notifications.events import NotificationEvent
from ..utils.logging import log_task_context
from .run_history import RunHistoryService
from .run_manager import RunRecord, TerminalHook

_NOTIFICATION_STATUS = {
    "success": ("success", "success_message"),
    "failed": ("failure", "failure_message"),
    "skipped": ("skipped", "skipped_message"),
    "lock_error": ("lock_error", "failure_message"),
    "config_error": ("config_error", "failure_message"),
    "unexpected_error": ("unexpected_error", "unexpected_error_message"),
}


def terminal_notification_hook(record: RunRecord) -> None:
    """Dispatch one terminal notification when the captured context permits it."""
    context = record.notify_ctx
    mapping = _NOTIFICATION_STATUS.get(record.status.value)
    if context is None or mapping is None or record.started_at is None:
        return
    status, message_field = mapping
    event = NotificationEvent(
        job_name=record.job,
        task_type=context.task_type,
        task_name=context.task_name,
        status=status,  # type: ignore[arg-type]
        started_at=record.started_at,
        finished_at=record.finished_at,
        dry_run=record.dry_run,
        message=getattr(context, message_field),
        log_path=context.log_path,
        error=record.error if status not in {"success", "skipped"} else None,
    )
    with log_task_context(f"{record.task_type}.{record.task_name}"):
        context.dispatcher.notify(event, context.policy)


def terminal_notification_only_hook() -> TerminalHook:
    """Terminal-Hook für den CLI-Modus: nur Notification, keine Persistenz."""

    async def _hook(record: RunRecord) -> None:
        try:
            await asyncio.to_thread(terminal_notification_hook, record)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "Run notification failed for run %s", record.run_id
            )

    return _hook


def terminal_run_hook(run_history_service: RunHistoryService) -> TerminalHook:

    async def _hook(record: RunRecord) -> None:
        try:
            await run_history_service.record(record)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "Run history persistence failed for run %s", record.run_id
            )
        try:
            await asyncio.to_thread(terminal_notification_hook, record)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "Run notification failed for run %s", record.run_id
            )

    return _hook
