import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Never
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.core.job_runner import BackupRunStatsContext, JobRunner, UnexpectedRunError
from src.core.locking import (
    JobAlreadyRunningError,
    ResourceLockManager,
    resource_for_rclone_endpoint,
    resource_for_repository,
)
from src.core.workflow import BackupArtifactCandidate
from src.models.resolved_config import (
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedExecutionConfig,
    ResolvedHooksConfig,
    ResolvedInputConfig,
    ResolvedJobConfig,
    ResolvedNotificationsConfig,
    ResolvedRcloneSyncTaskConfig,
    ResolvedRetentionConfig,
    ResolvedWorkflowConfig,
)
from src.services.run_manager import RunManager, RunOrigin, RunStatus


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run post-processing callbacks inline to keep unit tests deterministic."""

    async def immediate(func: Callable[..., object], /, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


@pytest.fixture()
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def backup_config() -> ResolvedBackupConfig:
    return ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        input=ResolvedInputConfig(sources=["/data"]),
        retention=ResolvedRetentionConfig(keep_daily=7),
    )


@pytest.fixture()
def full_job_config(backup_config: ResolvedBackupConfig) -> ResolvedJobConfig:
    return ResolvedJobConfig(
        backup={"local": backup_config},
        rclone={
            "offsite": ResolvedRcloneSyncTaskConfig(source="/backups/test", target="remote:bucket")
        },
        workflows={
            "full": ResolvedWorkflowConfig(
                schedule="0 2 * * *",
                steps=["backup.local", "rclone.offsite"],
            ),
            "manual": ResolvedWorkflowConfig(
                schedule=None,
                steps=["backup.local"],
            ),
        },
    )


@pytest.fixture()
def runner(full_job_config: ResolvedJobConfig, lock_dir: Path) -> JobRunner:
    return JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)


def _repository_blocker(
    job_name: str, target: str, repository: str, lock_dir: Path
) -> ResourceLockManager:
    return ResourceLockManager(
        job_name, target, {resource_for_repository(repository)}, lock_dir=lock_dir
    )


def test_job_runner_creates_log_file(lock_dir: Path, full_job_config: ResolvedJobConfig) -> None:
    import logging
    from datetime import datetime

    from src.utils.logging import job_logger_name

    job_name = "log-init-test"
    logging.getLogger(job_logger_name(job_name)).handlers.clear()

    JobRunner(job_name, full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

    expected_log = lock_dir / job_name / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    assert expected_log.exists(), f"Log-Datei wurde nicht angelegt: {expected_log}"


def test_run_backup_success(runner: JobRunner) -> None:
    with patch.object(runner._engine, "execute_backup", return_value=True) as mock:
        result = asyncio.run(runner.run_backup("local"))
    mock.assert_called_once_with("local")
    assert result is True


def test_run_backup_failure(runner: JobRunner) -> None:
    """run_backup gibt False zurück wenn execute_backup False liefert."""
    with patch.object(runner._engine, "execute_backup", return_value=False):
        result = asyncio.run(runner.run_backup("local"))
    assert result is False


def test_run_backup_not_found_returns_false_without_lock(runner: JobRunner, lock_dir: Path) -> None:
    """run_backup gibt False zurück ohne Lock zu erwerben wenn Backup nicht existiert."""
    with patch("src.core.job_runner.ResourceLockManager") as mock_lock_cls:
        result = asyncio.run(runner.run_backup("nonexistent"))
    assert result is False
    mock_lock_cls.assert_not_called()


def test_run_backup_raises_when_job_already_running(
    runner: JobRunner, lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """run_backup wirft JobAlreadyRunningError bei belegtem Resource-Lock."""
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_backup("local"))


@pytest.mark.parametrize(
    ("lock_retry_count", "lock_retry_delay"),
    [
        (None, None),
        (0, 1),
    ],
)
def test_run_backup_does_not_retry_when_unset_or_zero(
    lock_dir: Path,
    full_job_config: ResolvedJobConfig,
    lock_retry_count: int | None,
    lock_retry_delay: int | None,
) -> None:
    """Unset/zero retry count preserves immediate lock failure behavior."""
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        lock_retry_count=lock_retry_count,
        lock_retry_delay=lock_retry_delay,
    )
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire(), patch("src.core.job_runner.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_backup("local"))

    sleep.assert_not_called()


def test_run_backup_succeeds_after_lock_retry(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """A failed immediate acquire can retry later without holding a lock meanwhile."""
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        lock_retry_count=1,
        lock_retry_delay=1,
    )
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    blocker_context = blocker.acquire()
    blocker_context.__enter__()

    async def release_blocker(delay: int) -> None:
        assert delay == 1
        blocker_context.__exit__(None, None, None)

    with (
        patch("src.core.job_runner.asyncio.sleep", new=AsyncMock(side_effect=release_blocker)),
        patch.object(runner._engine, "execute_backup", new=AsyncMock(return_value=True)) as run,
    ):
        result = asyncio.run(runner.run_backup("local"))

    assert result is True
    run.assert_awaited_once_with("local")


def test_run_backup_exhausted_retries_raise_original_lock_error(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        lock_retry_count=1,
        lock_retry_delay=1,
    )
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire(), patch("src.core.job_runner.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(JobAlreadyRunningError) as exc_info:
            asyncio.run(runner.run_backup("local"))

    assert exc_info.value.lock_path == blocker.lock_paths[0]
    sleep.assert_awaited_once_with(1)


def test_job_pre_hooks_do_not_run_for_failed_lock_attempts(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(pre_hooks=["job-pre"])
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        lock_retry_count=1,
        lock_retry_delay=1,
    )
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with (
        blocker.acquire(),
        patch("src.core.job_runner.asyncio.sleep", new=AsyncMock()),
        patch.object(runner._engine, "run_job_hooks", new=AsyncMock()) as hooks,
    ):
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_backup("local"))

    hooks.assert_not_called()


def test_cancellation_during_lock_retry_sleep_propagates(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        lock_retry_count=1,
        lock_retry_delay=1,
    )
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)

    async def cancelled_sleep(_: int) -> None:
        raise asyncio.CancelledError

    with (
        blocker.acquire(),
        patch("src.core.job_runner.asyncio.sleep", new=AsyncMock(side_effect=cancelled_sleep)),
        patch.object(runner._engine, "run_job_hooks", new=AsyncMock()) as hooks,
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner.run_backup("local"))

    hooks.assert_not_called()


def test_run_backup_releases_lock_after_execution(runner: JobRunner, lock_dir: Path) -> None:
    """Lock wird nach run_backup freigegeben."""
    with patch.object(runner._engine, "execute_backup", return_value=True):
        asyncio.run(runner.run_backup("local"))
    probe = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    # Acquiring the same resource again only succeeds if the run released its lock.
    with probe.acquire():
        pass


def test_run_backup_releases_lock_on_exception(runner: JobRunner, lock_dir: Path) -> None:
    """Lock wird auch bei unerwarteter Exception freigegeben."""
    with patch.object(runner._engine, "execute_backup", side_effect=RuntimeError("crash")):
        with pytest.raises(UnexpectedRunError):
            asyncio.run(runner.run_backup("local"))
    probe = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    # Acquiring the same resource again only succeeds if the run released its lock.
    with probe.acquire():
        pass


def test_run_step_backup_backup_success(runner: JobRunner) -> None:
    """run_step delegiert an engine.execute_step mit dem Step-String."""
    with patch.object(runner._engine, "execute_step", return_value=True) as mock:
        result = asyncio.run(runner.run_step("backup.local.backup"))
    mock.assert_called_once_with("backup.local.backup")
    assert result is True


def test_run_step_rclone_success(runner: JobRunner) -> None:
    """run_step('rclone.offsite') delegiert korrekt an execute_step."""
    with patch.object(runner._engine, "execute_step", return_value=True) as mock:
        result = asyncio.run(runner.run_step("rclone.offsite"))
    mock.assert_called_once_with("rclone.offsite")
    assert result is True


def test_run_step_backup_full(runner: JobRunner) -> None:
    """run_step('backup.local') delegiert korrekt an execute_step."""
    with patch.object(runner._engine, "execute_step", return_value=True) as mock:
        result = asyncio.run(runner.run_step("backup.local"))
    mock.assert_called_once_with("backup.local")
    assert result is True


def test_run_step_failure_returns_false(runner: JobRunner) -> None:
    """run_step gibt False zurück wenn execute_step False liefert."""
    with patch.object(runner._engine, "execute_step", return_value=False):
        result = asyncio.run(runner.run_step("backup.local.retention"))
    assert result is False


def test_run_step_raises_when_job_already_running(
    runner: JobRunner, lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """run_step wirft JobAlreadyRunningError bei belegtem Resource-Lock."""
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_step("backup.local.backup"))


def test_run_step_releases_lock_after_execution(runner: JobRunner, lock_dir: Path) -> None:
    """Lock wird nach run_step freigegeben."""
    with patch.object(runner._engine, "execute_step", return_value=True):
        asyncio.run(runner.run_step("backup.local"))
    probe = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    # Acquiring the same resource again only succeeds if the run released its lock.
    with probe.acquire():
        pass


def test_run_step_releases_lock_on_exception(runner: JobRunner, lock_dir: Path) -> None:
    """Lock wird auch bei unerwarteter Exception in execute_step freigegeben."""
    with patch.object(runner._engine, "execute_step", side_effect=RuntimeError("crash")):
        with pytest.raises(UnexpectedRunError):
            asyncio.run(runner.run_step("backup.local"))
    probe = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    # Acquiring the same resource again only succeeds if the run released its lock.
    with probe.acquire():
        pass


def test_run_workflow_executes_steps(runner: JobRunner) -> None:
    """run_workflow delegiert an engine.execute_workflow."""
    with patch.object(runner._engine, "execute_workflow", return_value=True) as mock:
        result = asyncio.run(runner.run_workflow("full"))
    mock.assert_called_once_with("full", runner.job_config.workflows["full"])
    assert result is True


def test_run_workflow_not_found_returns_false(runner: JobRunner) -> None:
    """run_workflow mit unbekanntem Workflow-Namen gibt False zurück."""
    result = asyncio.run(runner.run_workflow("nonexistent"))
    assert result is False


def test_run_workflow_manual_executes_normally(runner: JobRunner) -> None:
    """run_workflow führt manuelle Workflows trotzdem direkt aus."""
    with patch.object(runner._engine, "execute_workflow", return_value=True) as mock_exec:
        result = asyncio.run(runner.run_workflow("manual"))
    assert result is True
    mock_exec.assert_called_once_with("manual", runner.job_config.workflows["manual"])


def test_run_workflow_raises_when_job_already_running(
    runner: JobRunner, lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """run_workflow wirft JobAlreadyRunningError wenn der Lock bereits gehalten wird."""
    blocker = ResourceLockManager(
        "test-job",
        "full",
        {
            resource_for_repository("/backups/test"),
            resource_for_rclone_endpoint("remote:bucket"),
        },
        lock_dir=lock_dir,
    )
    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_workflow("full"))


def test_run_workflow_releases_lock_after_execution(runner: JobRunner, lock_dir: Path) -> None:
    """Lock wird nach run_workflow freigegeben."""
    with patch.object(runner._engine, "execute_workflow", return_value=True):
        asyncio.run(runner.run_workflow("full"))
    probe = ResourceLockManager(
        "test-job",
        "full",
        {
            resource_for_repository("/backups/test"),
            resource_for_rclone_endpoint("remote:bucket"),
        },
        lock_dir=lock_dir,
    )
    # Acquiring the same resource again only succeeds if the run released its lock.
    with probe.acquire():
        pass


def test_single_step_workflow(lock_dir: Path) -> None:
    """Ein Workflow mit nur einem Step führt genau diesen Step aus."""
    job_config = ResolvedJobConfig(
        backup={
            "local": ResolvedBackupConfig(
                repository="/backups/test",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
            )
        },
        workflows={
            "only-backup": ResolvedWorkflowConfig(
                schedule="0 1 * * *", steps=["backup.local.backup"]
            ),
        },
    )
    runner = JobRunner("job-runner-test", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(runner.run_workflow("only-backup"))

    assert result is True
    mock_cls.return_value.execute.assert_called_once()


def test_job_runner_passes_dry_run_to_engine(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """JobRunner(dry_run=True) übergibt dry_run=True an die WorkflowEngine."""
    with patch("src.core.job_runner.WorkflowEngine") as mock_engine_cls:
        mock_engine_cls.return_value = MagicMock()
        JobRunner(
            "test-job",
            full_job_config,
            lock_dir=lock_dir,
            log_base_dir=lock_dir,
            dry_run=True,
        )
    mock_engine_cls.assert_called_once()
    _, kwargs = mock_engine_cls.call_args
    assert kwargs["dry_run"] is True
    assert callable(kwargs["on_step_started"])
    assert callable(kwargs["on_step_finished"])


def test_run_backup_dry_run_propagated(lock_dir: Path, full_job_config: ResolvedJobConfig) -> None:
    """JobRunner(dry_run=True) reicht dry_run korrekt durch run_backup an execute_backup weiter."""
    runner = JobRunner(
        "test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir, dry_run=True
    )
    with patch.object(runner._engine, "execute_backup", return_value=True) as mock:
        asyncio.run(runner.run_backup("local"))
    mock.assert_called_once_with("local")


def test_run_step_dry_run_propagated(lock_dir: Path, full_job_config: ResolvedJobConfig) -> None:
    """JobRunner(dry_run=True) reicht dry_run korrekt durch run_step an execute_step weiter."""
    runner = JobRunner(
        "test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir, dry_run=True
    )
    with patch.object(runner._engine, "execute_step", return_value=True) as mock:
        asyncio.run(runner.run_step("backup.local.backup"))
    mock.assert_called_once_with("backup.local.backup")


def test_job_hooks_wrap_run_in_order(lock_dir: Path, full_job_config: ResolvedJobConfig) -> None:
    full_job_config.hooks = ResolvedHooksConfig(
        pre_hooks=["job-pre"], post_hooks=["job-post"], on_error_hooks=["job-error"]
    )
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    order: list[str] = []

    async def run_hooks(hooks: list[str], context: str, *, timeout: int | None) -> bool:
        del hooks, timeout
        order.append(context)
        return True

    async def execute_backup(_: str) -> bool:
        order.append("operation")
        return True

    with (
        patch.object(runner._engine, "_run_hooks", side_effect=run_hooks),
        patch.object(runner._engine, "execute_backup", side_effect=execute_backup),
    ):
        result = asyncio.run(runner.run_backup("local"))

    assert result is True
    assert order == ["job pre", "operation", "job post"]


def test_job_pre_hook_abort_prevents_operation(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(pre_hooks=["job-pre"])
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

    async def run_job_hooks(phase: str) -> bool:
        return phase != "pre"

    with (
        patch.object(runner._engine, "run_job_hooks", side_effect=run_job_hooks) as hook_mock,
        patch.object(runner._engine, "execute_backup", return_value=True) as operation,
    ):
        result = asyncio.run(runner.run_backup("local"))

    assert result is False
    operation.assert_not_called()
    assert hook_mock.await_args_list == [call("pre"), call("on_error")]


def test_job_pre_hook_failure_runs_job_on_error_hooks(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(pre_hooks=["job-pre"], on_error_hooks=["job-error"])
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    phases: list[str] = []

    async def run_job_hooks(phase: str) -> bool:
        phases.append(phase)
        return phase != "pre"

    with (
        patch.object(runner._engine, "run_job_hooks", side_effect=run_job_hooks),
        patch.object(runner._engine, "execute_backup", return_value=True) as operation,
    ):
        result = asyncio.run(runner.run_backup("local"))

    assert result is False
    operation.assert_not_called()
    assert phases == ["pre", "on_error"]


def test_step_error_text_matching_cancelled_is_historized_as_failed(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    run_history = AsyncMock()
    runner._run_history_service = run_history

    asyncio.run(
        runner._finish_run_step("step-1", "backup.local.backup", False, "Step cancelled", False)
    )

    run_history.finish_step.assert_awaited_once_with(
        "step-1", status="failed", error="Step cancelled"
    )


def test_workflow_on_error_runs_before_job_on_error(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(on_error_hooks=["job-error"])
    full_job_config.workflows["manual"].hooks = ResolvedHooksConfig(on_error_hooks=["wf-error"])
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    order: list[str] = []

    async def run_hooks(hooks: list[str], context: str, *, timeout: int | None) -> bool:
        del hooks, timeout
        order.append(context)
        return True

    with (
        patch.object(runner._engine, "_run_hooks", side_effect=run_hooks),
        patch.object(runner._engine, "execute_step", return_value=False),
    ):
        result = asyncio.run(runner.run_workflow("manual"))

    assert result is False
    assert order.index("workflow 'manual' on_error") < order.index("job on_error")


def test_task_on_error_runs_before_job_on_error(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(on_error_hooks=["job-error"])
    full_job_config.backup["local"].hooks = ResolvedHooksConfig(on_error_hooks=["backup-error"])
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    order: list[str] = []

    async def run_hooks(hooks: list[str], context: str, *, timeout: int | None) -> bool:
        del hooks, timeout
        order.append(context)
        return True

    with (
        patch.object(runner._engine, "_run_hooks", side_effect=run_hooks),
        patch("src.core.workflow.BackupExecutor") as backup_cls,
    ):
        backup_cls.return_value.execute = AsyncMock(return_value=False)
        result = asyncio.run(runner.run_backup("local"))

    assert result is False
    assert order.index("backup 'local' on_error") < order.index("job on_error")


def test_job_hooks_run_inside_resource_locks(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(pre_hooks=["job-pre"], post_hooks=["job-post"])
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    checked: list[str] = []

    def assert_repository_locked(context: str) -> None:
        probe = _repository_blocker("test-job", "probe", "/backups/test", lock_dir)
        with pytest.raises(JobAlreadyRunningError):
            with probe.acquire():
                pass
        checked.append(context)

    async def run_hooks(hooks: list[str], context: str, *, timeout: int | None) -> bool:
        del hooks, timeout
        assert_repository_locked(context)
        return True

    async def execute_backup(_: str) -> bool:
        assert_repository_locked("operation")
        return True

    with (
        patch.object(runner._engine, "_run_hooks", side_effect=run_hooks),
        patch.object(runner._engine, "execute_backup", side_effect=execute_backup),
    ):
        result = asyncio.run(runner.run_backup("local"))

    assert result is True
    assert checked == ["job pre", "operation", "job post"]


def test_dry_run_skips_job_hooks(lock_dir: Path, full_job_config: ResolvedJobConfig) -> None:
    full_job_config.hooks = ResolvedHooksConfig(pre_hooks=["job-pre"], post_hooks=["job-post"])
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        dry_run=True,
    )

    with (
        patch("src.core.workflow.HookExecutor") as hook_cls,
        patch("src.core.workflow.BackupExecutor") as backup_cls,
    ):
        backup_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(runner.run_backup("local"))

    assert result is True
    hook_cls.assert_not_called()


def test_cancellation_skips_job_on_error(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    full_job_config.hooks = ResolvedHooksConfig(on_error_hooks=["job-error"])
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    started = asyncio.Event()
    phases: list[str] = []

    async def run_job_hooks(phase: str) -> bool:
        phases.append(phase)
        return True

    async def cancelled_operation(_: str) -> bool:
        started.set()
        await asyncio.Future()
        return True

    async def scenario() -> None:
        with (
            patch.object(runner._engine, "run_job_hooks", side_effect=run_job_hooks),
            patch.object(runner._engine, "execute_backup", side_effect=cancelled_operation),
        ):
            task = asyncio.create_task(runner.run_backup("local"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())

    assert phases == ["pre"]


@pytest.fixture()
def notifying_backup_config() -> ResolvedBackupConfig:
    return ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        input=ResolvedInputConfig(sources=["/data"]),
        retention=ResolvedRetentionConfig(keep_daily=7),
        notifications=ResolvedNotificationsConfig(
            notify_on_success=True,
            notify_on_error=True,
            notify_on_skipped=True,
        ),
    )


@pytest.fixture()
def notifying_job_config(notifying_backup_config: ResolvedBackupConfig) -> ResolvedJobConfig:
    return ResolvedJobConfig(
        backup={"local": notifying_backup_config},
        workflows={
            "full": ResolvedWorkflowConfig(
                steps=["backup.local"],
                notifications=ResolvedNotificationsConfig(
                    notify_on_success=True,
                    notify_on_error=True,
                    notify_on_skipped=False,
                ),
            ),
        },
    )


@pytest.fixture()
def notifying_rclone_job_config() -> ResolvedJobConfig:
    return ResolvedJobConfig(
        backup={
            "local": ResolvedBackupConfig(
                repository="/backups/test",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
                retention=ResolvedRetentionConfig(keep_daily=7),
                notifications=ResolvedNotificationsConfig(
                    notify_on_success=True,
                    notify_on_error=True,
                    notify_on_skipped=True,
                ),
            )
        },
        rclone={
            "offsite": ResolvedRcloneSyncTaskConfig(
                source="/backups/test",
                target="remote:bucket",
                notifications=ResolvedNotificationsConfig(
                    notify_on_success=True,
                    notify_on_error=True,
                    notify_on_skipped=True,
                ),
            )
        },
    )


def test_run_backup_unexpected_error_raises_unexpected_run_error(
    lock_dir: Path,
    notifying_job_config: ResolvedJobConfig,
) -> None:
    """Unexpected executor exceptions must reach RunManager as ``unexpected_error``."""
    runner = JobRunner(
        "test-job",
        notifying_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
    )
    with patch.object(runner._engine, "execute_backup", side_effect=RuntimeError("boom")):
        with pytest.raises(UnexpectedRunError) as exc_info:
            asyncio.run(runner.run_backup("local"))

    assert exc_info.value.error == "boom"


def test_run_workflow_unexpected_error_raises_unexpected_run_error(
    lock_dir: Path,
    notifying_job_config: ResolvedJobConfig,
) -> None:
    """run_workflow re-raises UnexpectedRunError wenn execute_workflow unerwartet wirft."""
    runner = JobRunner(
        "test-job",
        notifying_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
    )
    with patch.object(runner._engine, "execute_workflow", side_effect=RuntimeError("boom")):
        with pytest.raises(UnexpectedRunError):
            asyncio.run(runner.run_workflow("full"))


def test_run_step_unexpected_error_raises_unexpected_run_error(runner: JobRunner) -> None:
    """run_step re-raises UnexpectedRunError wenn execute_step unerwartet wirft."""
    with patch.object(runner._engine, "execute_step", side_effect=RuntimeError("boom")):
        with pytest.raises(UnexpectedRunError) as exc_info:
            asyncio.run(runner.run_step("backup.local.backup"))

    assert exc_info.value.error == "boom"


def test_run_backup_lock_manager_construction_error_raises_unexpected_run_error(
    lock_dir: Path,
    notifying_job_config: ResolvedJobConfig,
) -> None:
    """ResourceLockManager construction errors use the unexpected-error path."""
    runner = JobRunner(
        "test-job",
        notifying_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
    )
    with patch("src.core.job_runner.ResourceLockManager", side_effect=ValueError("bad resource")):
        with pytest.raises(UnexpectedRunError):
            asyncio.run(runner.run_backup("local"))


def test_run_workflow_lock_manager_construction_error_raises_unexpected_run_error(
    lock_dir: Path,
    notifying_job_config: ResolvedJobConfig,
) -> None:
    """ResourceLockManager construction errors use the unexpected-error path."""
    runner = JobRunner(
        "test-job",
        notifying_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
    )
    with patch("src.core.job_runner.ResourceLockManager", side_effect=ValueError("bad resource")):
        with pytest.raises(UnexpectedRunError):
            asyncio.run(runner.run_workflow("full"))


def test_run_step_lock_manager_construction_error_raises_unexpected_run_error(
    lock_dir: Path,
    notifying_job_config: ResolvedJobConfig,
) -> None:
    """ResourceLockManager construction errors use the unexpected-error path."""
    runner = JobRunner(
        "test-job",
        notifying_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
    )
    with patch("src.core.job_runner.ResourceLockManager", side_effect=ValueError("bad resource")):
        with pytest.raises(UnexpectedRunError):
            asyncio.run(runner.run_step("backup.local.backup"))


def test_run_backup_job_already_running_propagates(
    runner: JobRunner, lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """JobAlreadyRunningError propagiert aus run_backup heraus."""
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_backup("local"))


def test_run_workflow_job_already_running_propagates(
    runner: JobRunner, lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """JobAlreadyRunningError propagiert aus run_workflow heraus."""
    blocker = _repository_blocker("test-job", "full", "/backups/test", lock_dir)
    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_workflow("full"))


def test_run_step_job_already_running_propagates(
    runner: JobRunner, lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """JobAlreadyRunningError propagiert aus run_step heraus."""
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_step("backup.local.backup"))


def test_run_step_invalid_format_returns_false(runner: JobRunner) -> None:
    result = asyncio.run(runner.run_step("invalidstep"))
    assert result is False


def test_run_step_invalid_prefix_returns_false_without_lock_collision(
    runner: JobRunner, lock_dir: Path
) -> None:
    """Ungültige Prefixes werden vor dem Lock-Erwerb verworfen."""
    blocker = _repository_blocker("test-job", "local", "/backups/test", lock_dir)
    with blocker.acquire():
        result = asyncio.run(runner.run_step("foo.local"))

    assert result is False


def test_run_workflow_blocks_parallel_direct_backup_with_same_repository(
    lock_dir: Path,
) -> None:
    """Ein Workflow-Step hält denselben Repository-Lock wie ein direkter Backup-Lauf."""
    job_config = ResolvedJobConfig(
        backup={
            "local": ResolvedBackupConfig(
                repository="/backups/shared",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
            )
        },
        workflows={"full": ResolvedWorkflowConfig(steps=["backup.local"])},
    )
    workflow_runner = JobRunner("job", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    direct_runner = JobRunner("job", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

    async def assert_direct_backup_is_blocked(*_: object) -> bool:
        with patch.object(direct_runner._engine, "execute_backup", return_value=True):
            with pytest.raises(JobAlreadyRunningError):
                await direct_runner.run_backup("local")
        return True

    with patch.object(
        workflow_runner._engine, "execute_workflow", side_effect=assert_direct_backup_is_blocked
    ):
        assert asyncio.run(workflow_runner.run_workflow("full")) is True


def test_backups_with_same_repository_block_across_different_jobs(lock_dir: Path) -> None:
    """Gleiche Repositories blockieren auch über verschiedene Jobs hinweg."""
    config_a = ResolvedJobConfig(
        backup={
            "local": ResolvedBackupConfig(
                repository="/backups/shared",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data-a"]),
            )
        },
    )
    config_b = ResolvedJobConfig(
        backup={
            "remote": ResolvedBackupConfig(
                repository="/backups/shared",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data-b"]),
            )
        },
    )
    runner_a = JobRunner("job-a", config_a, lock_dir=lock_dir, log_base_dir=lock_dir)
    runner_b = JobRunner("job-b", config_b, lock_dir=lock_dir, log_base_dir=lock_dir)

    async def assert_other_job_is_blocked(_: str) -> bool:
        with patch.object(runner_b._engine, "execute_backup", return_value=True):
            with pytest.raises(JobAlreadyRunningError):
                await runner_b.run_backup("remote")
        return True

    with patch.object(runner_a._engine, "execute_backup", side_effect=assert_other_job_is_blocked):
        assert asyncio.run(runner_a.run_backup("local")) is True


def test_backups_with_different_repositories_do_not_block(lock_dir: Path) -> None:
    """Unterschiedliche Repositories können parallel gelockt werden."""
    config_a = ResolvedJobConfig(
        backup={
            "local": ResolvedBackupConfig(
                repository="/backups/a",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data-a"]),
            )
        },
    )
    config_b = ResolvedJobConfig(
        backup={
            "remote": ResolvedBackupConfig(
                repository="/backups/b",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data-b"]),
            )
        },
    )
    runner_a = JobRunner("job-a", config_a, lock_dir=lock_dir, log_base_dir=lock_dir)
    runner_b = JobRunner("job-b", config_b, lock_dir=lock_dir, log_base_dir=lock_dir)

    async def run_other_job(_: str) -> bool:
        with patch.object(runner_b._engine, "execute_backup", return_value=True):
            return await runner_b.run_backup("remote")

    with patch.object(runner_a._engine, "execute_backup", side_effect=run_other_job):
        assert asyncio.run(runner_a.run_backup("local")) is True


def test_rclone_endpoint_blocks_restic_rclone_repository(lock_dir: Path) -> None:
    """rclone:Nextcloud:/path und Nextcloud:/path sperren dieselbe Resource."""
    job_config = ResolvedJobConfig(
        backup={
            "cloud": ResolvedBackupConfig(
                repository="rclone:Nextcloud:/path",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
            )
        },
        rclone={
            "offsite": ResolvedRcloneSyncTaskConfig(source="/cache/repo", target="Nextcloud:/path")
        },
    )
    runner = JobRunner("job", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    blocker = _repository_blocker("other-job", "cloud", "rclone:Nextcloud:/path", lock_dir)

    with blocker.acquire():
        with pytest.raises(JobAlreadyRunningError):
            asyncio.run(runner.run_step("rclone.offsite"))


def test_workflow_holds_union_of_resource_locks_for_entire_runtime(lock_dir: Path) -> None:
    """Ein Workflow hält alle Step-Ressourcen bereits vor dem ersten Step bis zum Ende."""
    job_config = ResolvedJobConfig(
        backup={
            "a": ResolvedBackupConfig(
                repository="/backups/a",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
            ),
            "b": ResolvedBackupConfig(
                repository="/backups/b",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
            ),
        },
        workflows={"both": ResolvedWorkflowConfig(steps=["backup.a", "backup.b"])},
    )
    runner = JobRunner("job", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    contender = JobRunner("contender", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

    async def assert_later_step_resource_is_already_blocked(*_: object) -> bool:
        with patch.object(contender._engine, "execute_backup", return_value=True):
            with pytest.raises(JobAlreadyRunningError):
                await contender.run_backup("b")
        return True

    with patch.object(
        runner._engine,
        "execute_workflow",
        side_effect=assert_later_step_resource_is_already_blocked,
    ):
        assert asyncio.run(runner.run_workflow("both")) is True


def test_run_step_rclone_without_config_returns_false_without_resource_lock(
    lock_dir: Path,
) -> None:
    """run_step('rclone.offsite') ohne passende Rclone-Config erwirbt keinen Resource-Lock."""
    job_config = ResolvedJobConfig(
        backup={
            "local": ResolvedBackupConfig(
                repository="/backups/test",
                credentials=ResolvedCredentials(password="secret"),
                input=ResolvedInputConfig(sources=["/data"]),
            )
        },
    )
    runner = JobRunner("job", job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

    with patch("src.core.job_runner.ResourceLockManager") as mock_lock_cls:
        assert asyncio.run(runner.run_step("rclone.offsite")) is False

    mock_lock_cls.assert_not_called()


def test_run_backup_callback_called_on_success(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird nach erfolgreichem Backup-Run mit Kontext aufgerufen."""
    callback = AsyncMock()
    summary = {"snapshot_id": "snap-run-1", "files_new": 3}
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_id="run-123",
    )
    runner._engine.completed_backup_artifacts.setdefault("local", []).append(
        BackupArtifactCandidate(backup_summary=summary)
    )
    with patch.object(runner._engine, "execute_backup", return_value=True):
        asyncio.run(runner.run_backup("local"))

    callback.assert_called_once()
    job, backup, context = callback.call_args.args
    assert (job, backup) == ("test-job", "local")
    assert context == BackupRunStatsContext(
        run_id="run-123",
        backup_summary=summary,
        trigger_kind="backup",
    )


def test_run_backup_callback_not_called_on_failure(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird bei fehlgeschlagenem Backup nicht aufgerufen."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_backup", return_value=False):
        asyncio.run(runner.run_backup("local"))

    callback.assert_not_called()


def test_run_backup_callback_not_called_on_dry_run(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird bei dry_run=True nicht aufgerufen."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        dry_run=True,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_backup", return_value=True):
        asyncio.run(runner.run_backup("local"))

    callback.assert_not_called()


def test_run_backup_callback_exception_does_not_break_run(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """Eine Exception im Callback bricht den Backup-Run nicht ab."""
    callback = AsyncMock(side_effect=RuntimeError("stats boom"))
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_backup", return_value=True):
        result = asyncio.run(runner.run_backup("local"))

    assert result is True
    callback.assert_called_once()


def test_run_step_backup_substep_callback_called_on_success(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird nach erfolgreichem backup.X.backup-Step aufgerufen."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_id="run-step-123",
    )
    with patch.object(runner._engine, "execute_step", return_value=True):
        asyncio.run(runner.run_step("backup.local.backup"))

    callback.assert_called_once_with(
        "test-job",
        "local",
        BackupRunStatsContext(run_id="run-step-123"),
    )


@pytest.mark.parametrize(
    ("step", "trigger_kind"),
    [("backup.local.retention", "retention"), ("backup.local.cleanup", "cleanup")],
)
def test_run_step_retention_cleanup_callback_called_with_maintenance_context(
    lock_dir: Path, full_job_config: ResolvedJobConfig, step: str, trigger_kind: str
) -> None:
    """Pure retention/cleanup steps trigger stats post-processing for reconciliation."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_step", return_value=True):
        asyncio.run(runner.run_step(step))

    callback.assert_called_once_with(
        "test-job",
        "local",
        BackupRunStatsContext(trigger_kind=trigger_kind),
    )


def test_run_step_full_backup_callback_includes_enabled_maintenance_contexts(
    lock_dir: Path, full_job_config: ResolvedJobConfig, backup_config: ResolvedBackupConfig
) -> None:
    """A successful backup.NAME step runs backup, retention and cleanup post-processing."""
    callback = AsyncMock()
    summary = {"message_type": "summary", "snapshot_id": "snap-full"}
    config = full_job_config.model_copy(
        update={
            "backup": {
                "local": backup_config.model_copy(
                    update={"execution": ResolvedExecutionConfig(retention=True, cleanup=True)}
                )
            }
        }
    )
    runner = JobRunner(
        "test-job",
        config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_id="run-step-full",
    )
    runner._engine.completed_backup_artifacts.setdefault("local", []).append(
        BackupArtifactCandidate(backup_summary=summary)
    )
    with patch.object(runner._engine, "execute_step", return_value=True):
        asyncio.run(runner.run_step("backup.local"))

    assert [call.args for call in callback.call_args_list] == [
        (
            "test-job",
            "local",
            BackupRunStatsContext(
                run_id="run-step-full",
                backup_summary=summary,
                trigger_kind="backup",
            ),
        ),
        (
            "test-job",
            "local",
            BackupRunStatsContext(run_id="run-step-full", trigger_kind="retention"),
        ),
        (
            "test-job",
            "local",
            BackupRunStatsContext(run_id="run-step-full", trigger_kind="cleanup"),
        ),
    ]


def test_run_workflow_callback_called_for_backup_steps_on_success(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird nach erfolgreichem Workflow für backup.X Steps aufgerufen."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    # "manual" workflow has steps=["backup.local"]
    with patch.object(runner._engine, "execute_workflow", return_value=True):
        asyncio.run(runner.run_workflow("manual"))

    callback.assert_called_once()
    job, backup, context = callback.call_args.args
    assert (job, backup) == ("test-job", "local")
    assert isinstance(context, BackupRunStatsContext)


def test_run_workflow_callback_called_for_backup_substeps_on_success(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird im Workflow fuer backup.X.backup-Steps aufgerufen."""
    callback = AsyncMock()
    config = full_job_config.model_copy(
        update={
            "workflows": {"only-backup-step": ResolvedWorkflowConfig(steps=["backup.local.backup"])}
        }
    )
    runner = JobRunner(
        "test-job",
        config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_id="run-workflow-123",
    )
    with patch.object(runner._engine, "execute_workflow", return_value=True):
        asyncio.run(runner.run_workflow("only-backup-step"))

    callback.assert_called_once_with(
        "test-job",
        "local",
        BackupRunStatsContext(run_id="run-workflow-123"),
    )


def test_run_workflow_callback_consumes_backup_artifact_candidates_in_step_order(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """Repeated backup steps consume the next summary candidate for that backup."""
    callback_calls: list[tuple[str, str, BackupRunStatsContext]] = []

    async def callback(job: str, backup: str, context: BackupRunStatsContext) -> None:
        callback_calls.append((job, backup, context))

    config = full_job_config.model_copy(
        update={
            "workflows": {
                "two-backups": ResolvedWorkflowConfig(
                    steps=["backup.local.backup", "backup.local.backup"]
                )
            }
        }
    )
    runner = JobRunner(
        "test-job",
        config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_id="run-workflow-123",
    )
    first_summary = {"snapshot_id": "snap-1"}
    second_summary = {"snapshot_id": "snap-2"}
    runner._engine.completed_backup_artifacts.setdefault("local", []).extend(
        [
            BackupArtifactCandidate(first_summary),
            BackupArtifactCandidate(second_summary),
        ]
    )

    with patch.object(runner._engine, "execute_workflow", return_value=True):
        asyncio.run(runner.run_workflow("two-backups"))

    assert callback_calls == [
        (
            "test-job",
            "local",
            BackupRunStatsContext(
                run_id="run-workflow-123",
                backup_summary=first_summary,
            ),
        ),
        (
            "test-job",
            "local",
            BackupRunStatsContext(
                run_id="run-workflow-123",
                backup_summary=second_summary,
            ),
        ),
    ]


def test_run_workflow_pairs_each_repeated_backup_substep_with_its_own_run_step_id(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """Repeated real backup substeps must not share one run_step_id.

    Runs the real ``execute_workflow``/``_run_tracked_step`` chain (no mocked
    ``execute_workflow``) with a fake ``run_history_service`` that hands out a
    distinct ``run_step_id`` per ``create_step()`` call, exactly as
    ``RunHistoryService`` would. Before the fix, ``_last_successful_step_ids``
    Each callback must observe the run_step_id from its own tracked step.
    """
    from src.core.subprocesses import CommandResult

    callback_calls: list[tuple[str, str, BackupRunStatsContext]] = []

    async def callback(job: str, backup: str, context: BackupRunStatsContext) -> None:
        callback_calls.append((job, backup, context))

    config = full_job_config.model_copy(
        update={
            "workflows": {
                "two-backups": ResolvedWorkflowConfig(
                    steps=["backup.local.backup", "backup.local.backup"]
                )
            }
        }
    )
    run_history_service = MagicMock()
    run_history_service.create_step = AsyncMock(side_effect=["step-A", "step-B"])
    run_history_service.finish_step = AsyncMock(return_value=None)

    runner = JobRunner(
        "test-job",
        config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_history_service=run_history_service,
        run_id="run-workflow-123",
    )

    completed = CommandResult(returncode=0, stdout="", stderr="")
    with (
        patch("src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed),
        patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
    ):
        assert asyncio.run(runner.run_workflow("two-backups")) is True

    assert [context.run_step_id for _, _, context in callback_calls] == ["step-A", "step-B"]


@pytest.mark.parametrize(
    ("step", "trigger_kind"),
    [("backup.local.retention", "retention"), ("backup.local.cleanup", "cleanup")],
)
def test_run_workflow_callback_called_for_retention_cleanup_only_steps(
    lock_dir: Path, full_job_config: ResolvedJobConfig, step: str, trigger_kind: str
) -> None:
    """Workflow maintenance steps produce stats contexts when the workflow succeeds."""
    callback = AsyncMock()
    config = full_job_config.model_copy(
        update={"workflows": {"maintenance": ResolvedWorkflowConfig(steps=[step])}}
    )
    runner = JobRunner(
        "test-job",
        config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_workflow", return_value=True):
        asyncio.run(runner.run_workflow("maintenance"))

    callback.assert_called_once_with(
        "test-job",
        "local",
        BackupRunStatsContext(trigger_kind=trigger_kind),
    )


def test_run_workflow_callback_not_called_for_rclone_steps(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird nicht für rclone-Steps aufgerufen."""
    callback = AsyncMock()
    rclone_only_config = full_job_config.model_copy(
        update={"workflows": {"rclone-only": ResolvedWorkflowConfig(steps=["rclone.offsite"])}}
    )
    rclone_runner = JobRunner(
        "test-job",
        rclone_only_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    with patch.object(rclone_runner._engine, "execute_workflow", return_value=True):
        asyncio.run(rclone_runner.run_workflow("rclone-only"))

    callback.assert_not_called()


def test_run_workflow_callback_not_called_on_failure(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird bei fehlgeschlagenem Workflow nicht aufgerufen."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_workflow", return_value=False):
        asyncio.run(runner.run_workflow("manual"))

    callback.assert_not_called()


def test_run_workflow_collects_successful_backup_step_before_later_failure(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """A successful backup step keeps its post-processing even if a later step fails."""
    callback = AsyncMock()
    summary = {"message_type": "summary", "snapshot_id": "summary-only"}
    config = full_job_config.model_copy(
        update={
            "workflows": {
                "backup-then-rclone": ResolvedWorkflowConfig(
                    steps=["backup.local.backup", "rclone.offsite"]
                )
            }
        }
    )
    runner = JobRunner(
        "test-job",
        config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        run_id="run-workflow-123",
    )

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.RcloneExecutor") as rclone_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        backup_mock.return_value.summary = summary
        rclone_mock.return_value.execute = AsyncMock(return_value=False)
        assert asyncio.run(runner.run_workflow("backup-then-rclone")) is False

    callback.assert_called_once_with(
        "test-job",
        "local",
        BackupRunStatsContext(
            run_id="run-workflow-123",
            backup_summary=summary,
        ),
    )


def test_run_workflow_callback_not_called_on_dry_run(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """on_backup_success wird bei dry_run=True im Workflow nicht aufgerufen."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        dry_run=True,
        on_backup_success=callback,
    )
    with patch.object(runner._engine, "execute_workflow", return_value=True):
        asyncio.run(runner.run_workflow("manual"))

    callback.assert_not_called()


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("run_backup", ("local",)),
        ("run_step", ("backup.local",)),
        ("run_workflow", ("manual",)),
    ],
)
def test_cancellation_propagates_without_stats(
    lock_dir: Path,
    full_job_config: ResolvedJobConfig,
    method_name: str,
    args: tuple[str, ...],
) -> None:
    """Cancellation propagiert ohne Stats-Nacharbeit."""
    callback = AsyncMock()
    runner = JobRunner(
        "test-job",
        full_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
    )

    async def cancelled(*_: object) -> bool:
        raise asyncio.CancelledError

    method = getattr(runner, method_name)
    engine_method = {
        "run_backup": "execute_backup",
        "run_step": "execute_step",
        "run_workflow": "execute_workflow",
    }[method_name]
    with patch.object(runner._engine, engine_method, new=AsyncMock(side_effect=cancelled)):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(method(*args))

    callback.assert_not_called()


def test_backup_lock_is_held_during_cancellation_cleanup_and_released_afterward(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """Der synchrone Lock umschließt auch awaitbares Cancellation-Cleanup."""
    runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)
    probe = _repository_blocker("probe", "local", "/backups/test", lock_dir)

    async def scenario() -> None:
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def execute_backup(_: str) -> Never:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await finish_cleanup.wait()
                raise

        with patch.object(
            runner._engine, "execute_backup", new=AsyncMock(side_effect=execute_backup)
        ):
            task = asyncio.create_task(runner.run_backup("local"))
            await asyncio.sleep(0)
            task.cancel()
            await cleanup_started.wait()

            with pytest.raises(JobAlreadyRunningError):
                with probe.acquire():
                    pass

            finish_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Acquiring the same resource again only succeeds if the run released its lock.
        with probe.acquire():
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method_name", "target", "engine_method"),
    [
        ("run_backup", "local", "execute_backup"),
        ("run_step", "backup.local.backup", "execute_step"),
        ("run_workflow", "full", "execute_workflow"),
    ],
)
def test_post_processing_runs_after_operational_complete_while_lock_held(
    lock_dir: Path,
    notifying_job_config: ResolvedJobConfig,
    method_name: str,
    target: str,
    engine_method: str,
) -> None:
    """Nacharbeit aller JobRunner-Pfade laeuft unter gehaltenem Resource-Lock.

    ``on_operational_complete`` (macht den Run in RunManager nicht mehr
    ``cancellable``) laeuft bereits **vor** der Stats-Nacharbeit, ebenfalls noch
    unter gehaltenem Lock. Ein paralleler Lock-Versuch
    auf dieselbe Ressource ist daher waehrend der Nacharbeit blockiert. Die Probe
    darf das Ergebnis nicht ueber eine Exception
    transportieren: ein JobAlreadyRunningError aus dem Callback wuerde von
    ``JobRunner._collect_backup_success`` verschluckt und den Test sonst
    stillschweigend gruen erscheinen lassen, ohne etwas zu pruefen.
    """
    probe = _repository_blocker("probe", "local", "/backups/test", lock_dir)

    def lock_is_held() -> bool:
        try:
            with probe.acquire():
                return False
        except JobAlreadyRunningError:
            return True

    observed: dict[str, bool] = {}
    call_order: list[str] = []

    def observe_stats_lock(*_: object) -> None:
        observed["stats"] = lock_is_held()
        call_order.append("stats")

    def observe_operational_complete_lock() -> None:
        observed["operational_complete"] = lock_is_held()
        call_order.append("operational_complete")

    callback = AsyncMock(side_effect=observe_stats_lock)
    operational_complete = MagicMock(side_effect=observe_operational_complete_lock)
    runner = JobRunner(
        "test-job",
        notifying_job_config,
        lock_dir=lock_dir,
        log_base_dir=lock_dir,
        on_backup_success=callback,
        on_operational_complete=operational_complete,
    )
    method = getattr(runner, method_name)
    with patch.object(runner._engine, engine_method, new=AsyncMock(return_value=True)):
        assert asyncio.run(method(target)) is True

    callback.assert_called_once()
    job, backup, context = callback.call_args.args
    assert (job, backup) == ("test-job", "local")
    assert context == BackupRunStatsContext(run_id=None)
    operational_complete.assert_called_once_with()
    assert observed == {"stats": True, "operational_complete": True}
    assert call_order == ["operational_complete", "stats"]


@pytest.mark.parametrize(
    "origin",
    [RunOrigin.MANUAL, RunOrigin.MANUAL, RunOrigin.SCHEDULER],
)
def test_run_backup_internal_unexpected_exception_records_unexpected_error_via_run_manager(
    lock_dir: Path, full_job_config: ResolvedJobConfig, origin: RunOrigin
) -> None:
    """Reale JobRunner-interne Exception endet über RunManager als unexpected_error.

    Es wird KEIN run_* gemockt; stattdessen wirft der echte Executor-Pfad in der
    WorkflowEngine. Der JobRunner fängt/loggt/benachrichtigt intern und re-raised
    UnexpectedRunError, sodass der RunManager terminal UNEXPECTED_ERROR setzt
    (statt FAILED). Gilt konsistent über manuelle und Scheduler-Origins.
    """

    async def scenario() -> None:
        manager = RunManager()
        runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

        async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
            del mark_not_cancellable
            return await runner.run_backup("local")

        with patch("src.core.workflow.BackupExecutor") as backup_cls:
            backup_cls.return_value.execute = AsyncMock(side_effect=RuntimeError("boom"))
            record = await manager.start(origin, "test-job", "backup", "local", operation)
            await manager.join(record.run_id)

        assert record.status == RunStatus.UNEXPECTED_ERROR
        assert record.error is not None

    asyncio.run(scenario())


def test_run_backup_internal_false_records_failed_via_run_manager(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """Normaler Restic-False (Operation fehlgeschlagen) endet weiterhin als failed.

    Grenzfall: ein Executor, der False zurückgibt (z.B.
    restic exit != 0 oder Timeout-Expired), ist KEIN unexpected_error, sondern
    ein normaler failed.
    """

    async def scenario() -> None:
        manager = RunManager()
        runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

        async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
            del mark_not_cancellable
            return await runner.run_backup("local")

        with patch("src.core.workflow.BackupExecutor") as backup_cls:
            backup_cls.return_value.execute = AsyncMock(return_value=False)
            record = await manager.start(RunOrigin.MANUAL, "test-job", "backup", "local", operation)
            await manager.join(record.run_id)

        assert record.status == RunStatus.FAILED

    asyncio.run(scenario())


def test_run_workflow_internal_unexpected_exception_records_unexpected_error_via_run_manager(
    lock_dir: Path, full_job_config: ResolvedJobConfig
) -> None:
    """Reale interne Workflow-Exception endet über RunManager als unexpected_error."""

    async def scenario() -> None:
        manager = RunManager()
        runner = JobRunner("test-job", full_job_config, lock_dir=lock_dir, log_base_dir=lock_dir)

        async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
            del mark_not_cancellable
            return await runner.run_workflow("manual")

        with patch("src.core.workflow.BackupExecutor") as backup_cls:
            backup_cls.return_value.execute = AsyncMock(side_effect=RuntimeError("boom"))
            record = await manager.start(
                RunOrigin.SCHEDULER, "test-job", "workflow", "manual", operation
            )
            await manager.join(record.run_id)

        assert record.status == RunStatus.UNEXPECTED_ERROR

    asyncio.run(scenario())
