"""Shared lifecycle manager for asynchronous operational runs."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from ..core.job_runner import UnexpectedRunError
from ..core.locking import JobAlreadyRunningError
from ..notifications.context import NotificationContext
from .errors import ConfigServiceError, NotFoundServiceError, ServiceError

logger = logging.getLogger(__name__)

MarkNotCancellable = Callable[[], None]
RunOperation = Callable[[MarkNotCancellable], Awaitable[bool]]
TerminalHook = Callable[["RunRecord"], Awaitable[None]]
StartedHook = Callable[["RunRecord"], Awaitable[None]]


class RunOrigin(StrEnum):
    """Source that triggered an operational run."""

    MANUAL = "manual"
    SCHEDULER = "scheduler"


class RunStatus(StrEnum):
    """Lifecycle status shared by operational runs."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    LOCK_ERROR = "lock_error"
    CONFIG_ERROR = "config_error"
    UNEXPECTED_ERROR = "unexpected_error"


class RunKind(StrEnum):

    JOB_TASK = "job_task"
    RESTORE = "restore"


_ACTIVE_STATUSES = {RunStatus.QUEUED, RunStatus.RUNNING}


@dataclass
class RunRecord:
    """Process-local record for one asynchronous operational run."""

    run_id: str
    origin: RunOrigin
    run_kind: RunKind
    job: str
    task_type: str
    task_name: str
    status: RunStatus
    dry_run: bool = False
    cancellable: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    notify_ctx: NotificationContext | None = field(default=None, repr=False)

    @property
    def display_target(self) -> str:
        return format_run_target(self.run_kind, self.job, self.task_type, self.task_name)


class RunManager:
    """Track and cancel operational tasks within one asyncio event loop."""

    def __init__(
        self,
        on_terminal: TerminalHook | None = None,
        *,
        on_started: StartedHook | None = None,
    ) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._finish_tasks: dict[str, asyncio.Task[None]] = {}
        self._terminal_events: dict[str, asyncio.Future[None]] = {}
        self._cancel_requested: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._on_started = on_started
        self._on_terminal = on_terminal

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def start(
        self,
        origin: RunOrigin,
        job: str,
        task_type: str,
        task_name: str,
        operation: RunOperation,
        *,
        run_kind: RunKind = RunKind.JOB_TASK,
        dry_run: bool = False,
        run_id: str | None = None,
        notify_ctx: NotificationContext | None = None,
    ) -> RunRecord:
        self._bind_loop()
        if self._stopping:
            raise ServiceError(
                "runtime_stopping",
                "Runtime is stopping and does not accept new runs",
                status_code=503,
            )

        record = RunRecord(
            run_id=run_id or str(uuid4()),
            origin=origin,
            run_kind=run_kind,
            job=job,
            task_type=str(task_type),
            task_name=task_name,
            status=RunStatus.QUEUED,
            dry_run=dry_run,
            notify_ctx=notify_ctx,
        )
        if record.run_id in self._runs:
            raise ServiceError("duplicate_run_id", f"Run already exists: {record.run_id}")

        self._runs[record.run_id] = record
        assert self._loop is not None
        self._terminal_events[record.run_id] = self._loop.create_future()
        task = asyncio.create_task(
            self._execute(record, operation),
            name=f"run-manager:{record.run_id}",
        )
        self._tasks[record.run_id] = task
        task.add_done_callback(lambda done: self._on_task_done(record, done))
        return record

    async def get(self, run_id: str) -> RunRecord:
        self._bind_loop()
        try:
            return self._runs[run_id]
        except KeyError:
            raise NotFoundServiceError(f"Run not found: {run_id}", code="run_not_found") from None

    async def list(self, limit: int | None = None) -> list[RunRecord]:
        self._bind_loop()
        records = sorted(self._runs.values(), key=_run_sort_key, reverse=True)
        return records[:limit] if limit is not None else records

    async def wait_briefly(self, run_id: str, timeout: float) -> RunRecord:
        """Give a just-started run a short bounded window to leave ``queued``/``running``.

        ``start()`` itself stays strictly fire-and-forget (queued-cancel relies on
        that). This is a separate, opt-in wait a caller can use right after
        ``start()`` to let a near-instant outcome (e.g. an immediate resource-lock
        conflict) become visible in its first response, instead of only on the
        next poll. Returns whatever status is current once the run leaves the
        active set or the timeout elapses, whichever comes first; never raises on
        timeout.

        Args:
            run_id: The identifier of the run to observe.
            timeout: Maximum seconds to wait.

        Returns:
            The current ``RunRecord`` for ``run_id``.
        """
        self._bind_loop()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        record = await self.get(run_id)
        while record.status in _ACTIVE_STATUSES and loop.time() < deadline:
            await asyncio.sleep(0.01)
        return record

    async def join(self, run_id: str) -> RunRecord:
        """Await a run's task to completion and return its terminal record.

        Waits for the background task of ``run_id`` to finish, regardless of
        whether it succeeded, failed, or was cancelled. The task's own
        exception is never re-raised here; the terminal outcome is reflected in
        the returned record's status. If this coroutine itself is cancelled,
        ``asyncio.wait`` propagates the ``CancelledError`` to the caller.

        Args:
            run_id: The identifier of the run to await.

        Returns:
            The terminal ``RunRecord`` after the task has completed.

        Raises:
            NotFoundServiceError: If ``run_id`` is unknown.
        """
        self._bind_loop()
        record = await self.get(run_id)
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait({task})
        terminal_event = self._terminal_events.get(run_id)
        if terminal_event is not None:
            await asyncio.shield(terminal_event)
        return record

    async def cancel(self, run_id: str) -> RunRecord:
        """Request cancellation for an active run and return its record."""
        self._bind_loop()
        record = await self.get(run_id)
        if record.status not in _ACTIVE_STATUSES or not record.cancellable:
            return record

        task = self._tasks.get(run_id)
        if task is not None:
            self._cancel_task(run_id, task)
        return record

    async def shutdown(self, timeout: float) -> None:
        """Reject new starts, cancel active tasks, and wait boundedly for cleanup."""
        self._bind_loop()
        if timeout < 0:
            raise ValueError("Shutdown timeout must not be negative")

        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        tasks = list(self._tasks.values())
        for run_id, task in self._tasks.items():
            if not task.done() and self._runs[run_id].cancellable:
                self._cancel_task(run_id, task)
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

        # A task being done does not imply its terminal hook (history persistence)
        # has run yet: queued-cancellation can continue via a separately tracked
        # _finish_tasks entry after _on_task_done has removed the run from _tasks.
        # Wait for all pending terminal events, shielded so a timeout here cannot
        # cancel the underlying _finish() and strand other waiters (mirrors join()).
        pending_terminal = [
            asyncio.shield(event) for event in self._terminal_events.values() if not event.done()
        ]
        if pending_terminal:
            await asyncio.wait(pending_terminal, timeout=max(0.0, deadline - loop.time()))

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("RunManager cannot be shared between event loops")

    async def _execute(self, record: RunRecord, operation: RunOperation) -> None:
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        if self._on_started is not None:
            await self._persist_started_resisting_cancellation(record)
        try:
            success = await operation(lambda: self._mark_not_cancellable(record))
        except asyncio.CancelledError:
            await self._finish(record, RunStatus.CANCELLED)
            raise
        except JobAlreadyRunningError as exc:
            status = (
                RunStatus.SKIPPED if record.origin is RunOrigin.SCHEDULER else RunStatus.LOCK_ERROR
            )
            await self._finish(record, status, error=str(exc))
        except ConfigServiceError as exc:
            await self._finish(record, RunStatus.CONFIG_ERROR, error=str(exc))
        except UnexpectedRunError as exc:
            await self._finish(record, RunStatus.UNEXPECTED_ERROR, error=exc.error or str(exc))
        except Exception as exc:
            logger.exception("Operational run failed unexpectedly: %s", record.run_id)
            await self._finish(record, RunStatus.UNEXPECTED_ERROR, error=str(exc))
        else:
            await self._finish(record, RunStatus.SUCCESS if success else RunStatus.FAILED)

    async def _persist_started_resisting_cancellation(self, record: RunRecord) -> None:
        assert self._on_started is not None
        hook_task = asyncio.ensure_future(self._on_started(record))
        cancellation: asyncio.CancelledError | None = None
        while not hook_task.done():
            try:
                await asyncio.shield(hook_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                break
        try:
            hook_task.result()
        except Exception:
            logger.exception("Run started hook failed for run %s", record.run_id)
        if cancellation is not None:
            raise cancellation

    async def _finish(self, record: RunRecord, status: RunStatus, error: str | None = None) -> None:
        if record.status not in _ACTIVE_STATUSES:
            self._mark_terminal_complete(record.run_id)
            self._drop_completed_record(record.run_id)
            return
        record.status = status
        record.finished_at = datetime.now(timezone.utc)
        record.error = error
        try:
            if self._on_terminal is not None:
                await self._persist_terminal_resisting_cancellation(record)
        finally:
            self._mark_terminal_complete(record.run_id)
            self._drop_completed_record(record.run_id)

    async def _persist_terminal_resisting_cancellation(self, record: RunRecord) -> None:
        """Run the terminal hook so a second cancellation cannot strand persistence.

        Mirrors the restore registry's resisting-cancellation pattern: the hook
        runs to completion even if ``_finish`` is cancelled again mid-await, and
        the cancellation is re-raised afterwards to preserve cancel semantics.
        """
        assert self._on_terminal is not None
        hook_task = asyncio.ensure_future(self._on_terminal(record))
        cancellation: asyncio.CancelledError | None = None
        while not hook_task.done():
            try:
                await asyncio.shield(hook_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                break
        try:
            hook_task.result()
        except Exception:
            logger.exception("Run terminal hook failed for run %s", record.run_id)
        if cancellation is not None:
            raise cancellation

    def _drop_completed_record(self, run_id: str) -> None:
        """Drop terminal records once no process-local lifecycle state needs them."""
        record = self._runs.get(run_id)
        if (
            record is None
            or record.status in _ACTIVE_STATUSES
            or run_id in self._tasks
            or run_id in self._finish_tasks
            or run_id in self._terminal_events
        ):
            return
        self._runs.pop(run_id, None)

    def _mark_terminal_complete(self, run_id: str) -> None:
        terminal_event = self._terminal_events.get(run_id)
        if terminal_event is not None and not terminal_event.done():
            terminal_event.set_result(None)
            self._terminal_events.pop(run_id, None)

    def _mark_not_cancellable(self, record: RunRecord) -> None:
        if record.status in _ACTIVE_STATUSES:
            record.cancellable = False

    def _cancel_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        if run_id not in self._cancel_requested:
            self._cancel_requested.add(run_id)
            task.cancel()

    def _on_task_done(self, record: RunRecord, task: asyncio.Task[None]) -> None:
        assert self._loop is not None
        if task.cancelled():
            finish_task = self._loop.create_task(
                self._finish(record, RunStatus.CANCELLED),
                name="run-manager:finish",
            )
            self._track_finish_task(record.run_id, finish_task)
        else:
            try:
                task.result()
            except Exception:
                logger.exception("Run manager task failed unexpectedly: %s", record.run_id)
                finish_task = self._loop.create_task(
                    self._finish(
                        record, RunStatus.UNEXPECTED_ERROR, error="Run manager task failed"
                    ),
                    name="run-manager:finish",
                )
                self._track_finish_task(record.run_id, finish_task)
        self._tasks.pop(record.run_id, None)
        self._cancel_requested.discard(record.run_id)
        self._drop_completed_record(record.run_id)

    def _track_finish_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._finish_tasks[run_id] = task
        task.add_done_callback(lambda _: self._on_finish_task_done(run_id))

    def _on_finish_task_done(self, run_id: str) -> None:
        self._finish_tasks.pop(run_id, None)
        self._drop_completed_record(run_id)


def _run_sort_key(record: RunRecord) -> datetime:
    return record.finished_at or record.started_at or record.created_at


def format_run_target(run_kind: RunKind, job: str, task_type: str, task_name: str) -> str:
    if run_kind == RunKind.RESTORE:
        return f"{job}.restore.{task_name}"
    return f"{job}.{task_type}.{task_name}"
