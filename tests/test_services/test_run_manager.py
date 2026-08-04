import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from src.core.locking import JobAlreadyRunningError
from src.services.errors import ConfigServiceError, NotFoundServiceError, ServiceError
from src.services.run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus


async def _wait_for_terminal(manager: RunManager, run_id: str) -> None:
    for _ in range(100):
        try:
            record = await manager.get(run_id)
        except NotFoundServiceError:
            return
        if record.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("run did not become terminal")


async def _wait_for_lifecycle_cleanup(manager: RunManager) -> None:
    """Wait until every record is evicted again.

    ``_drop_completed_record()`` only pops a terminal record once no task,
    finish task and terminal event reference it anymore, so an empty listing is
    the observable proof that the strong task references were released.
    """
    for _ in range(100):
        if await manager.list() == []:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("runs were not evicted after lifecycle cleanup")


async def _start_job_task(
    manager: RunManager,
    origin: RunOrigin,
    task_name: str,
    operation: Callable[[Callable[[], None]], Awaitable[bool]],
    **kwargs: Any,
) -> RunRecord:
    return await manager.start(origin, "demo", "backup", task_name, operation, **kwargs)


def test_run_manager_drops_terminal_records_after_lifecycle_cleanup() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        joined = await manager.join(record.run_id)

        assert joined is record
        assert record.status == RunStatus.SUCCESS
        await _wait_for_lifecycle_cleanup(manager)
        with pytest.raises(NotFoundServiceError):
            await manager.get(record.run_id)

    asyncio.run(scenario())


def test_run_manager_keeps_active_records_until_terminal_cleanup() -> None:
    async def scenario() -> None:
        manager = RunManager()
        release = asyncio.Event()

        async def blocking(_: Callable[[], None]) -> bool:
            await release.wait()
            return True

        active = await _start_job_task(manager, RunOrigin.MANUAL, "live", blocking)
        await asyncio.sleep(0)

        assert (await manager.get(active.run_id)).status == RunStatus.RUNNING
        assert await manager.list() == [active]

        release.set()
        await manager.join(active.run_id)

        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [(True, RunStatus.SUCCESS), (False, RunStatus.FAILED)],
)
def test_run_manager_records_bool_result_and_releases_task_reference(
    result: bool, expected_status: RunStatus
) -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            return result

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        assert record.status == RunStatus.QUEUED
        assert len(await manager.list()) == 1

        await _wait_for_terminal(manager, record.run_id)

        assert record.status == expected_status
        assert record.run_kind == RunKind.JOB_TASK
        assert record.job == "demo"
        assert record.task_type == "backup"
        assert record.task_name == "local"
        assert record.display_target == "demo.backup.local"
        assert record.started_at is not None
        assert record.finished_at is not None
        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())


def test_run_manager_started_hook_exception_does_not_block_operation() -> None:
    async def started_hook(_: RunRecord) -> None:
        raise RuntimeError("started hook failed")

    async def scenario() -> None:
        manager = RunManager(on_started=started_hook)
        operation_called = False

        async def operation(_: Callable[[], None]) -> bool:
            nonlocal operation_called
            operation_called = True
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await _wait_for_terminal(manager, record.run_id)

        assert operation_called is True
        assert record.status == RunStatus.SUCCESS

    asyncio.run(scenario())


def test_run_manager_terminal_hook_exception_does_not_change_terminal_status() -> None:
    async def terminal_hook(_: RunRecord) -> None:
        raise RuntimeError("terminal hook failed")

    async def scenario() -> None:
        manager = RunManager(on_terminal=terminal_hook)

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await _wait_for_terminal(manager, record.run_id)

        assert record.status == RunStatus.SUCCESS
        assert record.finished_at is not None

    asyncio.run(scenario())


def test_run_manager_rejects_legacy_string_target_start_signature() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            return True

        legacy_start: Any = manager.start
        with pytest.raises(TypeError):
            await legacy_start(RunOrigin.MANUAL, "demo.backup.local", operation)

        assert await manager.list() == []

    asyncio.run(scenario())


def test_run_manager_calls_terminal_hook_with_final_record() -> None:
    async def scenario() -> None:
        seen = []

        async def on_terminal(record: RunRecord) -> None:
            seen.append(record)

        manager = RunManager(on_terminal=on_terminal)

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await _wait_for_terminal(manager, record.run_id)

        assert seen == [record]
        assert seen[0].status == RunStatus.SUCCESS
        assert seen[0].finished_at is not None

    asyncio.run(scenario())


def test_run_manager_terminal_hook_failure_does_not_change_terminal_status() -> None:
    async def scenario() -> None:
        manager = RunManager(on_terminal=_raising_terminal_hook)

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await _wait_for_terminal(manager, record.run_id)

        assert record.status == RunStatus.SUCCESS
        assert record.finished_at is not None

    asyncio.run(scenario())


async def _raising_terminal_hook(record: RunRecord) -> None:
    raise RuntimeError(f"history broken for {record.run_id}")


def test_run_manager_records_structured_failures() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def config_error(_: Callable[[], None]) -> bool:
            raise ConfigServiceError("invalid_config", "broken config")

        async def unexpected_error(_: Callable[[], None]) -> bool:
            raise RuntimeError("boom")

        config_record = await _start_job_task(manager, RunOrigin.MANUAL, "local", config_error)
        unexpected_record = await manager.start(
            RunOrigin.MANUAL, "demo", "backup", "other", unexpected_error
        )
        await _wait_for_terminal(manager, config_record.run_id)
        await _wait_for_terminal(manager, unexpected_record.run_id)

        assert config_record.status == RunStatus.CONFIG_ERROR
        assert config_record.error == "broken config"
        assert unexpected_record.status == RunStatus.UNEXPECTED_ERROR
        assert unexpected_record.error == "boom"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("origin", "expected_status"),
    [
        (RunOrigin.MANUAL, RunStatus.LOCK_ERROR),
        (RunOrigin.MANUAL, RunStatus.LOCK_ERROR),
        (RunOrigin.SCHEDULER, RunStatus.SKIPPED),
        (RunOrigin.SCHEDULER, RunStatus.SKIPPED),
    ],
)
def test_run_manager_maps_lock_conflicts_by_origin(
    origin: RunOrigin, expected_status: RunStatus
) -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            raise JobAlreadyRunningError("demo", "local", Path("/tmp/demo.lock"))

        record = await _start_job_task(manager, origin, "local", operation)
        await _wait_for_terminal(manager, record.run_id)

        assert record.status == expected_status
        assert "already running" in str(record.error)

    asyncio.run(scenario())


def test_run_manager_cancel_is_idempotent_for_running_and_terminal_run() -> None:
    async def scenario() -> None:
        manager = RunManager()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await asyncio.sleep(0.01)
                cleaned_up.set()
                raise

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await started.wait()

        assert await manager.cancel(record.run_id) is record
        await cleanup_started.wait()
        assert await manager.cancel(record.run_id) is record
        await _wait_for_terminal(manager, record.run_id)

        assert cleaned_up.is_set()
        assert record.status == RunStatus.CANCELLED
        assert record.finished_at is not None
        await _wait_for_lifecycle_cleanup(manager)
        with pytest.raises(NotFoundServiceError):
            await manager.cancel(record.run_id)

    asyncio.run(scenario())


def test_run_manager_cancel_queued_task_marks_it_cancelled_without_starting_operation() -> None:
    async def scenario() -> None:
        manager = RunManager()
        operation_started = False

        async def operation(_: Callable[[], None]) -> bool:
            nonlocal operation_started
            operation_started = True
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await manager.cancel(record.run_id)
        await _wait_for_terminal(manager, record.run_id)

        assert operation_started is False
        assert record.status == RunStatus.CANCELLED
        assert record.started_at is None
        assert record.finished_at is not None
        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())


def test_run_manager_cancel_unknown_run_raises_structured_not_found() -> None:
    async def scenario() -> None:
        manager = RunManager()

        with pytest.raises(NotFoundServiceError) as exc_info:
            await manager.cancel("missing")

        assert exc_info.value.code == "run_not_found"
        assert exc_info.value.status_code == 404
        assert exc_info.value.message == "Run not found: missing"

    asyncio.run(scenario())


def test_run_manager_join_returns_terminal_record_for_normal_run() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        joined = await manager.join(record.run_id)

        assert joined is record
        assert joined.status == RunStatus.SUCCESS
        assert joined.finished_at is not None
        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())


def test_run_manager_releases_terminal_event_after_terminal_completion() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await manager.join(record.run_id)

        assert manager._terminal_events == {}

    asyncio.run(scenario())


def test_run_manager_wait_briefly_returns_immediately_on_fast_lock_failure() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            raise JobAlreadyRunningError("demo", "local", Path("/tmp/demo.lock"))

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await manager.wait_briefly(record.run_id, timeout=0.2)
        elapsed = loop.time() - started

        assert result is record
        assert result.status == RunStatus.LOCK_ERROR
        # Resolved well before the full timeout budget was spent.
        assert elapsed < 0.15

    asyncio.run(scenario())


def test_run_manager_wait_briefly_is_bounded_for_a_still_running_run() -> None:
    async def scenario() -> None:
        manager = RunManager()
        release = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            await release.wait()
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await manager.wait_briefly(record.run_id, timeout=0.05)
        elapsed = loop.time() - started

        assert result.status == RunStatus.RUNNING
        assert elapsed >= 0.03

        release.set()
        await _wait_for_terminal(manager, record.run_id)

    asyncio.run(scenario())


def test_run_manager_join_returns_cancelled_record_without_raising() -> None:
    async def scenario() -> None:
        manager = RunManager()
        started = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            started.set()
            await asyncio.Event().wait()
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await started.wait()
        await manager.cancel(record.run_id)

        joined = await manager.join(record.run_id)

        assert joined is record
        assert joined.status == RunStatus.CANCELLED
        assert joined.finished_at is not None

    asyncio.run(scenario())


def test_run_manager_join_waits_for_queued_cancel_terminal_hook() -> None:
    async def scenario() -> None:
        hook_started = asyncio.Event()
        release_hook = asyncio.Event()
        hook_finished = False

        async def on_terminal(record: RunRecord) -> None:
            nonlocal hook_finished
            assert record.status == RunStatus.CANCELLED
            hook_started.set()
            await release_hook.wait()
            hook_finished = True

        manager = RunManager(on_terminal=on_terminal)
        operation_started = False

        async def operation(_: Callable[[], None]) -> bool:
            nonlocal operation_started
            operation_started = True
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await manager.cancel(record.run_id)

        join_task = asyncio.create_task(manager.join(record.run_id))
        await hook_started.wait()
        await asyncio.sleep(0)

        assert join_task.done() is False
        assert hook_finished is False
        assert operation_started is False

        release_hook.set()
        joined = await join_task

        assert joined is record
        assert record.status == RunStatus.CANCELLED
        assert record.started_at is None
        assert record.finished_at is not None
        assert hook_finished is True
        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())


def test_run_manager_join_unknown_run_raises_structured_not_found() -> None:
    async def scenario() -> None:
        manager = RunManager()

        with pytest.raises(NotFoundServiceError) as exc_info:
            await manager.join("missing")

        assert exc_info.value.code == "run_not_found"

    asyncio.run(scenario())


def test_run_manager_does_not_cancel_post_processing_after_operational_completion() -> None:
    async def scenario() -> None:
        manager = RunManager()
        post_processing_started = asyncio.Event()
        release_post_processing = asyncio.Event()

        async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
            mark_not_cancellable()
            post_processing_started.set()
            await release_post_processing.wait()
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await post_processing_started.wait()

        assert record.status == RunStatus.RUNNING
        assert record.cancellable is False
        assert await manager.cancel(record.run_id) is record
        assert record.status == RunStatus.RUNNING

        release_post_processing.set()
        await _wait_for_terminal(manager, record.run_id)
        assert record.status == RunStatus.SUCCESS

    asyncio.run(scenario())


def test_run_manager_shutdown_waits_for_cleanup_and_rejects_new_starts() -> None:
    async def scenario() -> None:
        manager = RunManager()
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)
                cleaned_up.set()
                raise

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await started.wait()
        await manager.shutdown(timeout=1)
        await manager.shutdown(timeout=1)

        assert manager.stopping is True
        assert cleaned_up.is_set()
        assert record.status == RunStatus.CANCELLED
        await _wait_for_lifecycle_cleanup(manager)

        with pytest.raises(ServiceError) as exc_info:
            await _start_job_task(manager, RunOrigin.MANUAL, "other", operation)

        assert exc_info.value.code == "runtime_stopping"
        assert exc_info.value.status_code == 503

    asyncio.run(scenario())


def test_run_manager_shutdown_waits_without_cancelling_post_processing() -> None:
    async def scenario() -> None:
        manager = RunManager()
        post_processing_started = asyncio.Event()
        release_post_processing = asyncio.Event()

        async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
            mark_not_cancellable()
            post_processing_started.set()
            await release_post_processing.wait()
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await post_processing_started.wait()
        await manager.shutdown(timeout=0.01)

        assert record.status == RunStatus.RUNNING
        assert record.cancellable is False

        release_post_processing.set()
        await _wait_for_terminal(manager, record.run_id)
        assert record.status == RunStatus.SUCCESS

    asyncio.run(scenario())


def test_run_manager_shutdown_waits_for_queued_cancel_terminal_hook() -> None:
    async def scenario() -> None:
        hook_started = asyncio.Event()
        release_hook = asyncio.Event()
        hook_finished = False

        async def on_terminal(record: RunRecord) -> None:
            nonlocal hook_finished
            assert record.status == RunStatus.CANCELLED
            hook_started.set()
            await release_hook.wait()
            hook_finished = True

        manager = RunManager(on_terminal=on_terminal)
        operation_started = False

        async def operation(_: Callable[[], None]) -> bool:
            nonlocal operation_started
            operation_started = True
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await manager.cancel(record.run_id)

        shutdown_task = asyncio.create_task(manager.shutdown(timeout=1))
        await hook_started.wait()
        await asyncio.sleep(0)

        assert shutdown_task.done() is False
        assert hook_finished is False
        assert operation_started is False

        release_hook.set()
        await shutdown_task

        assert record.status == RunStatus.CANCELLED
        assert record.started_at is None
        assert record.finished_at is not None
        assert hook_finished is True
        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())


def test_run_manager_shutdown_waits_for_already_untracked_finish_task() -> None:
    async def scenario() -> None:
        hook_started = asyncio.Event()
        release_hook = asyncio.Event()
        hook_finished = False

        async def on_terminal(record: RunRecord) -> None:
            nonlocal hook_finished
            assert record.status == RunStatus.CANCELLED
            hook_started.set()
            await release_hook.wait()
            hook_finished = True

        manager = RunManager(on_terminal=on_terminal)
        operation_started = False

        async def operation(_: Callable[[], None]) -> bool:
            nonlocal operation_started
            operation_started = True
            return True

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await manager.cancel(record.run_id)
        # Der Terminal-Hook läuft erst im separaten Finish-Task, den
        # `_on_task_done()` anlegt, nachdem es den operativen Task bereits
        # verworfen hat. Ein gestarteter Hook beweist damit den Zustand, den
        # dieser Test braucht: operativer Task weg, Finish-Task noch offen.
        await hook_started.wait()

        shutdown_task = asyncio.create_task(manager.shutdown(timeout=1))
        await asyncio.sleep(0)

        assert shutdown_task.done() is False
        assert hook_finished is False
        assert operation_started is False

        release_hook.set()
        await shutdown_task

        assert record.status == RunStatus.CANCELLED
        assert record.started_at is None
        assert record.finished_at is not None
        assert hook_finished is True
        assert manager._terminal_events == {}

    asyncio.run(scenario())


def test_run_manager_shutdown_timeout_is_bounded() -> None:
    async def scenario() -> None:
        manager = RunManager()
        started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_cleanup.wait()
                raise

        record = await _start_job_task(manager, RunOrigin.MANUAL, "local", operation)
        await started.wait()
        await asyncio.wait_for(manager.shutdown(timeout=0.01), timeout=0.1)

        assert record.status == RunStatus.RUNNING
        assert len(await manager.list()) == 1

        release_cleanup.set()
        await _wait_for_terminal(manager, record.run_id)
        assert record.status == RunStatus.CANCELLED
        await _wait_for_lifecycle_cleanup(manager)

    asyncio.run(scenario())
