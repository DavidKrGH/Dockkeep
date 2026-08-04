import asyncio
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from src.notifications.context import NotificationContext
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import NotificationPolicy
from src.services.errors import NotFoundServiceError
from src.services.run_control import record_to_dict
from src.services.run_history import RunHistoryService
from src.services.run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus
from src.services.terminal_hooks import (
    terminal_notification_only_hook,
    terminal_run_hook,
)


def _record(run_id: str = "run-1") -> RunRecord:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    return RunRecord(
        run_id=run_id,
        origin=RunOrigin.MANUAL,
        run_kind=RunKind.JOB_TASK,
        job="job",
        task_type="backup",
        task_name="main",
        status=RunStatus.SUCCESS,
        dry_run=False,
        cancellable=False,
        created_at=now,
        started_at=now,
        finished_at=now,
        error=None,
    )


def test_terminal_run_hook_persists_parent_run_without_notification_context(
    monkeypatch: Any,
) -> None:
    events: list[str] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            events.append(f"history:{record.run_id}")

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    hook = terminal_run_hook(cast(RunHistoryService, HistoryService()))

    asyncio.run(cast(Coroutine[Any, Any, None], hook(_record())))

    assert events == ["history:run-1"]


def test_notification_only_hook_notifies_without_history(monkeypatch: Any) -> None:
    """Der CLI-Terminal-Hook versendet Notifications ohne jede Persistenz."""
    events: list[str] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    def fake_notification(record: RunRecord) -> None:
        events.append(f"notify:{record.run_id}")

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr("src.services.terminal_hooks.terminal_notification_hook", fake_notification)
    hook = terminal_notification_only_hook()

    asyncio.run(cast(Coroutine[Any, Any, None], hook(_record())))

    assert events == ["notify:run-1"]


def test_notification_only_hook_isolates_notification_failure(monkeypatch: Any) -> None:
    """Ein Provider-Fehler entkommt dem notification-only Hook nicht."""

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    def failing_notification(record: RunRecord) -> None:
        raise RuntimeError("smtp down")

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(
        "src.services.terminal_hooks.terminal_notification_hook", failing_notification
    )
    hook = terminal_notification_only_hook()

    asyncio.run(cast(Coroutine[Any, Any, None], hook(_record())))


def test_terminal_hook_tolerates_missing_notify_ctx(monkeypatch: Any) -> None:
    """Runs started without a notify_ctx (e.g. restores) persist but never notify."""
    events: list[str] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            events.append(f"history:{record.run_id}")

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    record = _record()
    assert record.notify_ctx is None

    asyncio.run(
        cast(
            Coroutine[Any, Any, None],
            terminal_run_hook(cast(RunHistoryService, HistoryService()))(record),
        )
    )

    assert events == ["history:run-1"]


def test_terminal_hook_persists_before_notification_and_isolates_history_failure(
    monkeypatch: Any,
) -> None:
    events: list[str] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            events.append("history")
            raise RuntimeError("database unavailable")

    class Dispatcher:
        def notify(self, event: Any, policy: Any) -> None:
            events.append(f"notify:{event.status}:{event.message}")

    record = _record()
    record.notify_ctx = NotificationContext(
        dispatcher=cast(NotificationDispatcher, Dispatcher()),
        policy=NotificationPolicy(True, True, True),
        task_type="backup",
        task_name="main",
        success_message="success message",
        failure_message="failure message",
        skipped_message="skipped message",
        unexpected_error_message="unexpected message",
        log_path=None,
    )
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)

    asyncio.run(
        cast(
            Coroutine[Any, Any, None],
            terminal_run_hook(cast(RunHistoryService, HistoryService()))(record),
        )
    )

    assert events == ["history", "notify:success:success message"]


def test_terminal_hook_suppresses_non_notifiable_statuses(monkeypatch: Any) -> None:
    notified: list[object] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            pass

    class Dispatcher:
        def notify(self, event: Any, policy: Any) -> None:
            notified.append(event)

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    for status in (RunStatus.CANCELLED,):
        record = _record(status.value)
        record.status = status
        record.notify_ctx = NotificationContext(
            dispatcher=cast(NotificationDispatcher, Dispatcher()),
            policy=NotificationPolicy(True, True, True),
            task_type="backup",
            task_name="main",
            success_message="success",
            failure_message="failure",
            skipped_message="skipped",
            unexpected_error_message="unexpected",
            log_path=None,
        )
        asyncio.run(
            cast(
                Coroutine[Any, Any, None],
                terminal_run_hook(cast(RunHistoryService, HistoryService()))(record),
            )
        )

    assert notified == []


@pytest.mark.parametrize(
    ("run_status", "event_status", "message"),
    [
        (RunStatus.SUCCESS, "success", "success"),
        (RunStatus.FAILED, "failure", "failure"),
        (RunStatus.SKIPPED, "skipped", "skipped"),
        (RunStatus.LOCK_ERROR, "lock_error", "failure"),
        (RunStatus.CONFIG_ERROR, "config_error", "failure"),
        (RunStatus.UNEXPECTED_ERROR, "unexpected_error", "unexpected"),
    ],
)
def test_terminal_hook_maps_status_and_preserves_runtime_event_data(
    monkeypatch: Any,
    run_status: RunStatus,
    event_status: str,
    message: str,
) -> None:
    events: list[Any] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            pass

    class Dispatcher:
        def notify(self, event: Any, policy: Any) -> None:
            events.append(event)

    record = _record()
    record.status = run_status
    record.dry_run = True
    record.error = "boom"
    record.notify_ctx = NotificationContext(
        dispatcher=cast(NotificationDispatcher, Dispatcher()),
        policy=NotificationPolicy(True, True, True),
        task_type="backup",
        task_name="main",
        success_message="success",
        failure_message="failure",
        skipped_message="skipped",
        unexpected_error_message="unexpected",
        log_path=None,
    )
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)

    asyncio.run(
        cast(
            Coroutine[Any, Any, None],
            terminal_run_hook(cast(RunHistoryService, HistoryService()))(record),
        )
    )

    assert len(events) == 1
    assert events[0].status == event_status
    assert events[0].message == message
    assert events[0].dry_run is True
    assert events[0].started_at == record.started_at
    assert events[0].finished_at == record.finished_at
    assert events[0].error == (
        "boom"
        if run_status
        in {
            RunStatus.FAILED,
            RunStatus.LOCK_ERROR,
            RunStatus.CONFIG_ERROR,
            RunStatus.UNEXPECTED_ERROR,
        }
        else None
    )


def test_notification_failure_does_not_escape_or_serialize_context(monkeypatch: Any) -> None:
    persisted: list[str] = []

    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            persisted.append(record.run_id)

    class Dispatcher:
        def notify(self, event: Any, policy: Any) -> None:
            raise RuntimeError("provider setup failed")

    record = _record()
    record.notify_ctx = NotificationContext(
        dispatcher=cast(NotificationDispatcher, Dispatcher()),
        policy=NotificationPolicy(True, True, True),
        task_type="backup",
        task_name="main",
        success_message="success",
        failure_message="failure",
        skipped_message="skipped",
        unexpected_error_message="unexpected",
        log_path=None,
    )
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)

    asyncio.run(
        cast(
            Coroutine[Any, Any, None],
            terminal_run_hook(cast(RunHistoryService, HistoryService()))(record),
        )
    )

    assert persisted == ["run-1"]
    assert "notify_ctx" not in record_to_dict(record)


def test_notification_failure_does_not_block_join_or_eviction(monkeypatch: Any) -> None:
    async def immediate_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    class HistoryService:
        async def record(self, record: RunRecord) -> None:
            pass

    class Dispatcher:
        def notify(self, event: Any, policy: Any) -> None:
            raise RuntimeError("notification failed")

    context = NotificationContext(
        dispatcher=cast(NotificationDispatcher, Dispatcher()),
        policy=NotificationPolicy(True, True, True),
        task_type="backup",
        task_name="main",
        success_message="success",
        failure_message="failure",
        skipped_message="skipped",
        unexpected_error_message="unexpected",
        log_path=None,
    )
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)

    async def run() -> None:
        manager = RunManager(
            on_terminal=terminal_run_hook(cast(RunHistoryService, HistoryService()))
        )

        async def operation(mark_not_cancellable: Any) -> bool:
            return True

        record = await manager.start(
            RunOrigin.MANUAL,
            "job",
            "backup",
            "main",
            operation,
            notify_ctx=context,
        )
        final = await manager.join(record.run_id)

        assert final.status is RunStatus.SUCCESS
        with pytest.raises(NotFoundServiceError):
            await manager.get(record.run_id)

    asyncio.run(run())
