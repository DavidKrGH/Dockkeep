import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.workflow import WorkflowEngine
from src.models.resolved_config import (
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedExecutionConfig,
    ResolvedHooksConfig,
    ResolvedInputConfig,
    ResolvedJobConfig,
    ResolvedRcloneSyncTaskConfig,
    ResolvedRetentionConfig,
    ResolvedTimeoutsConfig,
    ResolvedWorkflowConfig,
)


@pytest.fixture()
def backup_config() -> ResolvedBackupConfig:
    return ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        input=ResolvedInputConfig(sources=["/data"]),
        retention=ResolvedRetentionConfig(keep_daily=7),
    )


@pytest.fixture()
def job_config(backup_config: ResolvedBackupConfig) -> ResolvedJobConfig:
    return ResolvedJobConfig(
        backup={"local": backup_config},
        rclone={
            "offsite": ResolvedRcloneSyncTaskConfig(source="/backups/test", target="remote:bucket")
        },
    )


@pytest.fixture()
def engine(job_config: ResolvedJobConfig) -> WorkflowEngine:
    return WorkflowEngine("test-job", job_config)


def test_execute_step_backup_calls_run_backup(engine: WorkflowEngine) -> None:
    with patch.object(engine, "_run_backup", return_value=True) as mock:
        result = asyncio.run(engine.execute_step("backup.local"))
    mock.assert_called_once_with("local")
    assert result is True


def test_execute_step_backup_backup_calls_run_backup_backup(engine: WorkflowEngine) -> None:
    with patch.object(engine, "_run_backup_backup", return_value=True) as mock:
        result = asyncio.run(engine.execute_step("backup.local.backup"))
    mock.assert_called_once_with("local")
    assert result is True


def test_execute_step_backup_retention_calls_run_backup_forget(engine: WorkflowEngine) -> None:
    with patch.object(engine, "_run_backup_forget", return_value=True) as mock:
        result = asyncio.run(engine.execute_step("backup.local.retention"))
    mock.assert_called_once_with("local")
    assert result is True


def test_execute_step_backup_cleanup_calls_run_backup_prune(engine: WorkflowEngine) -> None:
    with patch.object(engine, "_run_backup_prune", return_value=True) as mock:
        result = asyncio.run(engine.execute_step("backup.local.cleanup"))
    mock.assert_called_once_with("local")
    assert result is True


def test_execute_step_rclone_calls_run_rclone(engine: WorkflowEngine) -> None:
    with patch.object(engine, "_run_rclone", return_value=True) as mock:
        result = asyncio.run(engine.execute_step("rclone.offsite"))
    mock.assert_called_once_with("offsite")
    assert result is True


def test_execute_step_unknown_returns_false(engine: WorkflowEngine) -> None:
    """execute_step mit unbekanntem Format gibt False zurück."""
    assert asyncio.run(engine.execute_step("unknown")) is False


def test_execute_step_invalid_backup_substep_returns_false(engine: WorkflowEngine) -> None:
    """execute_step mit ungültigem Sub-Step gibt False zurück."""
    assert asyncio.run(engine.execute_step("backup.local.invalid")) is False


def test_run_backup_backup_only_when_flags_false(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup führt nur backup aus wenn retention=False und cleanup=False."""
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.ForgetExecutor") as forget_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup("local"))

    assert result is True
    backup_mock.return_value.execute.assert_called_once()
    forget_mock.return_value.execute.assert_not_called()
    prune_mock.return_value.execute.assert_not_called()


def test_run_backup_runs_forget_when_flag_true(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup führt backup und forget aus wenn retention=True."""
    backup_config.execution = ResolvedExecutionConfig(retention=True, cleanup=False)

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.ForgetExecutor") as forget_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        forget_mock.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup("local"))

    assert result is True
    backup_mock.return_value.execute.assert_called_once()
    forget_mock.return_value.execute.assert_called_once()
    prune_mock.return_value.execute.assert_not_called()


def test_run_backup_runs_prune_without_forget(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup führt backup dann prune aus wenn cleanup=True und retention=False."""
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=True)

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.ForgetExecutor") as forget_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        prune_mock.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup("local"))

    assert result is True
    backup_mock.return_value.execute.assert_called_once()
    forget_mock.return_value.execute.assert_not_called()
    prune_mock.return_value.execute.assert_called_once()


def test_run_backup_all_steps_in_order(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup führt backup, forget, prune in Reihenfolge aus wenn beide Flags True."""
    call_order: list[str] = []
    backup_config.execution = ResolvedExecutionConfig(retention=True, cleanup=True)

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.ForgetExecutor") as forget_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(
            side_effect=lambda _: call_order.append("backup") or True
        )
        forget_mock.return_value.execute = AsyncMock(
            side_effect=lambda _: call_order.append("forget") or True
        )
        prune_mock.return_value.execute = AsyncMock(
            side_effect=lambda _: call_order.append("prune") or True
        )
        result = asyncio.run(engine._run_backup("local"))

    assert result is True
    assert call_order == ["backup", "forget", "prune"]


def test_run_backup_aborts_on_backup_failure(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup gibt sofort False zurück wenn backup fehlschlägt."""
    backup_config.execution = ResolvedExecutionConfig(retention=True, cleanup=True)

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.ForgetExecutor") as forget_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=False)
        result = asyncio.run(engine._run_backup("local"))

    assert result is False
    forget_mock.return_value.execute.assert_not_called()
    prune_mock.return_value.execute.assert_not_called()


def test_run_backup_aborts_on_forget_failure(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup gibt False zurück wenn forget fehlschlägt; prune läuft nicht.

    Der bereits erfolgreiche Backup-Step bleibt fuer die Nacharbeit erhalten,
    auch wenn ein spaeterer Workflow-Step den Gesamtlauf scheitern laesst.
    """
    backup_config.execution = ResolvedExecutionConfig(retention=True, cleanup=True)
    summary = {"message_type": "summary", "snapshot_id": "snap-1"}

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.ForgetExecutor") as forget_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        backup_mock.return_value.summary = summary
        forget_mock.return_value.execute = AsyncMock(return_value=False)
        result = asyncio.run(engine._run_backup("local"))

    assert result is False
    prune_mock.return_value.execute.assert_not_called()
    candidate = engine.consume_backup_artifact_candidate("local")
    assert candidate is not None
    assert candidate.backup_summary == summary


def test_run_backup_aborts_on_prune_failure(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup gibt False zurück wenn prune fehlschlägt.

    Der bereits erfolgreiche Backup-Step bleibt fuer die Nacharbeit erhalten,
    obwohl der nachfolgende Prune-Step fehlschlaegt.
    """
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=True)
    summary = {"message_type": "summary", "snapshot_id": "snap-1"}

    with (
        patch("src.core.workflow.BackupExecutor") as backup_mock,
        patch("src.core.workflow.PruneExecutor") as prune_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        backup_mock.return_value.summary = summary
        prune_mock.return_value.execute = AsyncMock(return_value=False)
        result = asyncio.run(engine._run_backup("local"))

    assert result is False
    candidate = engine.consume_backup_artifact_candidate("local")
    assert candidate is not None
    assert candidate.backup_summary == summary


def test_run_backup_nonexistent_returns_false(engine: WorkflowEngine) -> None:
    """_run_backup gibt False zurück wenn Backup nicht existiert."""
    assert asyncio.run(engine._run_backup("nonexistent")) is False


def test_run_backup_uses_resolved_backup_sources(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """_run_backup nutzt die bereits aufgelösten backup.input.sources."""
    backup_config.input = ResolvedInputConfig(sources=["/backup-data"])
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)

    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        asyncio.run(engine._run_backup("local"))

    mock_cls.assert_called_once_with("test-job", ["/backup-data"], dry_run=False, timeout=None)


def test_run_backup_records_artifact_candidate_from_executor_summary(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """Successful backup execution stores the parsed summary for later post-processing."""
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)
    summary = {"message_type": "summary", "snapshot_id": "snap-1", "files_new": 2}

    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        mock_cls.return_value.summary = summary
        assert asyncio.run(engine._run_backup("local")) is True

    candidate = engine.consume_backup_artifact_candidate("local")
    assert candidate is not None
    assert candidate.backup_summary == summary


def test_run_backup_dry_run_does_not_record_artifact_candidate(
    job_config: ResolvedJobConfig, backup_config: ResolvedBackupConfig
) -> None:
    """Dry-run backups do not feed stats/artifact post-processing candidates."""
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)
    engine = WorkflowEngine("test-job", job_config, dry_run=True)

    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        mock_cls.return_value.summary = {"snapshot_id": "dry-run-snap"}
        assert asyncio.run(engine._run_backup("local")) is True

    assert engine.consume_backup_artifact_candidate("local") is None
    assert engine.completed_backup_artifacts == {}


def test_run_backup_backup_records_artifact_candidates_in_execution_order(
    engine: WorkflowEngine,
) -> None:
    """Repeated backup substeps keep one candidate per execution, in FIFO order."""
    first = MagicMock()
    first.execute = AsyncMock(return_value=True)
    first.summary = {"snapshot_id": "snap-1"}
    second = MagicMock()
    second.execute = AsyncMock(return_value=True)
    second.summary = {"snapshot_id": "snap-2"}

    with patch("src.core.workflow.BackupExecutor", side_effect=[first, second]):
        assert asyncio.run(engine._run_backup_backup("local")) is True
        assert asyncio.run(engine._run_backup_backup("local")) is True

    first_candidate = engine.consume_backup_artifact_candidate("local")
    second_candidate = engine.consume_backup_artifact_candidate("local")
    assert first_candidate is not None
    assert second_candidate is not None
    assert first_candidate.backup_summary == {"snapshot_id": "snap-1"}
    assert second_candidate.backup_summary == {"snapshot_id": "snap-2"}
    assert engine.consume_backup_artifact_candidate("local") is None


def test_run_backup_backup_calls_executor(engine: WorkflowEngine) -> None:
    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup_backup("local"))
    mock_cls.assert_called_once_with("test-job", ["/data"], dry_run=False, timeout=None)
    assert result is True


def test_run_backup_forget_calls_executor(engine: WorkflowEngine) -> None:
    with patch("src.core.workflow.ForgetExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup_forget("local"))
    mock_cls.assert_called_once_with("test-job", dry_run=False, timeout=None)
    assert result is True


def test_run_backup_prune_calls_executor(engine: WorkflowEngine) -> None:
    with patch("src.core.workflow.PruneExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup_prune("local"))
    mock_cls.assert_called_once_with("test-job", dry_run=False, timeout=None)
    assert result is True


def test_run_backup_backup_nonexistent_returns_false(engine: WorkflowEngine) -> None:
    assert asyncio.run(engine._run_backup_backup("nonexistent")) is False


def test_run_backup_forget_nonexistent_returns_false(engine: WorkflowEngine) -> None:
    assert asyncio.run(engine._run_backup_forget("nonexistent")) is False


def test_run_backup_prune_nonexistent_returns_false(engine: WorkflowEngine) -> None:
    assert asyncio.run(engine._run_backup_prune("nonexistent")) is False


def test_run_rclone_calls_executor(engine: WorkflowEngine) -> None:
    with patch("src.core.workflow.RcloneExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_rclone("offsite"))
    mock_cls.assert_called_once_with("test-job", dry_run=False, timeout=None)
    assert result is True


def test_run_rclone_uses_task_timeout(job_config: ResolvedJobConfig) -> None:
    """_run_rclone nutzt den effektiven Timeout des Rclone-Tasks."""
    job_config.rclone["offsite"].timeouts = ResolvedTimeoutsConfig(rclone_timeout=37)
    engine = WorkflowEngine("test-job", job_config)
    with patch("src.core.workflow.RcloneExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_rclone("offsite"))
    mock_cls.assert_called_once_with("test-job", dry_run=False, timeout=37)
    assert result is True


def test_run_rclone_runs_task_hooks_in_order(job_config: ResolvedJobConfig) -> None:
    job_config.rclone["offsite"].hooks = ResolvedHooksConfig(
        pre_hooks=["rclone-pre"], post_hooks=["rclone-post"], on_error_hooks=["rclone-error"]
    )
    engine = WorkflowEngine("test-job", job_config)
    order: list[str] = []

    async def run_hooks(hooks: list[str], context: str, *, timeout: int | None) -> bool:
        del hooks, timeout
        order.append(context)
        return True

    async def execute_rclone(_: ResolvedRcloneSyncTaskConfig) -> bool:
        order.append("rclone")
        return True

    with (
        patch.object(engine, "_run_hooks", side_effect=run_hooks),
        patch("src.core.workflow.RcloneExecutor") as rclone_cls,
    ):
        rclone_cls.return_value.execute = AsyncMock(side_effect=execute_rclone)
        result = asyncio.run(engine._run_rclone("offsite"))

    assert result is True
    assert order == ["rclone 'offsite' pre", "rclone", "rclone 'offsite' post"]


def test_run_rclone_failure_runs_on_error_hooks(job_config: ResolvedJobConfig) -> None:
    job_config.rclone["offsite"].hooks = ResolvedHooksConfig(
        pre_hooks=["rclone-pre"], on_error_hooks=["rclone-error"]
    )
    engine = WorkflowEngine("test-job", job_config)
    order: list[str] = []

    async def run_hooks(hooks: list[str], context: str, *, timeout: int | None) -> bool:
        del hooks, timeout
        order.append(context)
        return True

    async def execute_rclone(_: ResolvedRcloneSyncTaskConfig) -> bool:
        order.append("rclone")
        return False

    with (
        patch.object(engine, "_run_hooks", side_effect=run_hooks),
        patch("src.core.workflow.RcloneExecutor") as rclone_cls,
    ):
        rclone_cls.return_value.execute = AsyncMock(side_effect=execute_rclone)
        result = asyncio.run(engine._run_rclone("offsite"))

    assert result is False
    assert order == ["rclone 'offsite' pre", "rclone", "rclone 'offsite' on_error"]


def test_run_rclone_no_config_returns_false() -> None:
    """_run_rclone gibt False zurück wenn der Rclone-Task nicht konfiguriert ist."""
    job_config = ResolvedJobConfig(
        backup={"local": ResolvedBackupConfig(repository="/backups/test")}
    )
    engine = WorkflowEngine("job", job_config)
    assert asyncio.run(engine._run_rclone("offsite")) is False


def test_execute_backup_delegates_to_run_backup(engine: WorkflowEngine) -> None:
    with patch.object(engine, "_run_backup", return_value=True) as mock:
        result = asyncio.run(engine.execute_backup("local"))
    mock.assert_called_once_with("local")
    assert result is True


def test_execute_backup_nonexistent_returns_false(engine: WorkflowEngine) -> None:
    """execute_backup gibt False zurück wenn Backup nicht existiert."""
    assert asyncio.run(engine.execute_backup("nonexistent")) is False


def test_execute_workflow_runs_all_steps_in_order(engine: WorkflowEngine) -> None:
    """execute_workflow führt alle Steps in der angegebenen Reihenfolge aus."""
    call_order: list[str] = []

    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local", "rclone.offsite"],
    )

    with (
        patch.object(
            engine, "_run_backup", side_effect=lambda _: call_order.append("backup.local") or True
        ),
        patch.object(
            engine, "_run_rclone", side_effect=lambda _: call_order.append("rclone") or True
        ),
    ):
        result = asyncio.run(engine.execute_workflow("full", workflow))

    assert result is True
    assert call_order == ["backup.local", "rclone"]


def test_execute_workflow_aborts_on_first_failed_step(engine: WorkflowEngine) -> None:
    """Workflow bricht nach dem ersten fehlschlagenden Step ab."""
    call_order: list[str] = []

    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local.backup", "backup.local.retention"],
    )

    with (
        patch.object(
            engine, "_run_backup_backup", side_effect=lambda _: call_order.append("backup") or False
        ),
        patch.object(
            engine, "_run_backup_forget", side_effect=lambda _: call_order.append("forget") or True
        ),
    ):
        result = asyncio.run(engine.execute_workflow("partial", workflow))

    assert result is False
    assert call_order == ["backup"]


def test_execute_workflow_single_step(engine: WorkflowEngine) -> None:
    """Ein Workflow mit einem Step führt genau diesen Step aus."""
    workflow = ResolvedWorkflowConfig(schedule="0 1 * * *", steps=["backup.local.backup"])

    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine.execute_workflow("only-backup", workflow))

    assert result is True
    mock_cls.return_value.execute.assert_called_once()


def test_execute_workflow_returns_false_when_backup_missing(engine: WorkflowEngine) -> None:
    """Workflow gibt False zurück wenn ein Step auf ein nicht-existentes Backup verweist."""
    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local.backup", "backup.nonexistent.backup"],
    )

    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine.execute_workflow("missing-backup", workflow))

    assert result is False


def test_dry_run_propagated_to_backup_executor(job_config: ResolvedJobConfig) -> None:
    engine = WorkflowEngine("test-job", job_config, dry_run=True)
    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        asyncio.run(engine._run_backup_backup("local"))
    mock_cls.assert_called_once_with("test-job", ["/data"], dry_run=True, timeout=None)


def test_dry_run_propagated_to_forget_executor(job_config: ResolvedJobConfig) -> None:
    engine = WorkflowEngine("test-job", job_config, dry_run=True)
    with patch("src.core.workflow.ForgetExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        asyncio.run(engine._run_backup_forget("local"))
    mock_cls.assert_called_once_with("test-job", dry_run=True, timeout=None)


def test_dry_run_propagated_to_prune_executor(job_config: ResolvedJobConfig) -> None:
    engine = WorkflowEngine("test-job", job_config, dry_run=True)
    with patch("src.core.workflow.PruneExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        asyncio.run(engine._run_backup_prune("local"))
    mock_cls.assert_called_once_with("test-job", dry_run=True, timeout=None)


def test_dry_run_propagated_to_rclone_executor(job_config: ResolvedJobConfig) -> None:
    engine = WorkflowEngine("test-job", job_config, dry_run=True)
    with patch("src.core.workflow.RcloneExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        asyncio.run(engine._run_rclone("offsite"))
    mock_cls.assert_called_once_with("test-job", dry_run=True, timeout=None)


def test_dry_run_propagated_through_run_backup(
    job_config: ResolvedJobConfig, backup_config: ResolvedBackupConfig
) -> None:
    """WorkflowEngine(dry_run=True) reicht dry_run durch _run_backup an BackupExecutor weiter."""
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)
    engine = WorkflowEngine("test-job", job_config, dry_run=True)
    with patch("src.core.workflow.BackupExecutor") as mock_cls:
        mock_cls.return_value.execute = AsyncMock(return_value=True)
        asyncio.run(engine._run_backup("local"))
    mock_cls.assert_called_once_with("test-job", ["/data"], dry_run=True, timeout=None)


def test_dry_run_backup_skips_configured_hooks(
    job_config: ResolvedJobConfig, backup_config: ResolvedBackupConfig
) -> None:
    """Backup-Hooks werden im Dry-Run nicht ausgeführt und sichtbar ausgelassen."""
    backup_config.hooks = ResolvedHooksConfig(
        pre_hooks=["./pre.sh"], post_hooks=["./post.sh"], on_error_hooks=["./error.sh"]
    )
    engine = WorkflowEngine("test-job", job_config, dry_run=True)

    with (
        patch("src.core.workflow.HookExecutor") as hook_cls,
        patch.object(engine.logger, "info") as log_info,
        patch("src.core.workflow.BackupExecutor") as backup_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup("local"))

    assert result is True
    hook_cls.assert_not_called()
    log_info.assert_any_call(
        "Skipping %s hooks during dry-run (%d configured)", "backup 'local' pre", 1
    )
    log_info.assert_any_call(
        "Skipping %s hooks during dry-run (%d configured)", "backup 'local' post", 1
    )


def test_dry_run_backup_skips_on_error_hooks_after_executor_failure(
    job_config: ResolvedJobConfig, backup_config: ResolvedBackupConfig
) -> None:
    """Auch on_error-Hooks bleiben bei einem fehlgeschlagenen Dry-Run aus."""
    backup_config.hooks = ResolvedHooksConfig(on_error_hooks=["./error.sh"])
    engine = WorkflowEngine("test-job", job_config, dry_run=True)

    with (
        patch("src.core.workflow.HookExecutor") as hook_cls,
        patch.object(engine.logger, "info") as log_info,
        patch("src.core.workflow.BackupExecutor") as backup_mock,
    ):
        backup_mock.return_value.execute = AsyncMock(return_value=False)
        result = asyncio.run(engine._run_backup("local"))

    assert result is False
    hook_cls.assert_not_called()
    log_info.assert_called_once_with(
        "Skipping %s hooks during dry-run (%d configured)", "backup 'local' on_error", 1
    )


def test_dry_run_workflow_skips_configured_hooks(
    job_config: ResolvedJobConfig,
) -> None:
    """Workflow-Hooks werden im Dry-Run nicht ausgeführt und sichtbar ausgelassen."""
    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local.backup"],
        hooks=ResolvedHooksConfig(
            pre_hooks=["./pre.sh"], post_hooks=["./post.sh"], on_error_hooks=["./error.sh"]
        ),
    )
    engine = WorkflowEngine("test-job", job_config, dry_run=True)

    with (
        patch.object(engine.logger, "info") as log_info,
        patch.object(engine, "execute_step", return_value=True),
    ):
        result = asyncio.run(engine.execute_workflow("daily", workflow))

    assert result is True
    log_info.assert_any_call(
        "Skipping %s hooks during dry-run (%d configured)", "workflow 'daily' pre", 1
    )
    log_info.assert_any_call(
        "Skipping %s hooks during dry-run (%d configured)", "workflow 'daily' post", 1
    )


def test_dry_run_workflow_skips_on_error_hooks_after_step_failure(
    job_config: ResolvedJobConfig,
) -> None:
    """Workflow-on_error-Hooks bleiben bei einem fehlgeschlagenen Dry-Run aus."""
    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local.backup"],
        hooks=ResolvedHooksConfig(on_error_hooks=["./error.sh"]),
    )
    engine = WorkflowEngine("test-job", job_config, dry_run=True)

    with (
        patch.object(engine.logger, "info") as log_info,
        patch.object(engine, "execute_step", return_value=False),
    ):
        result = asyncio.run(engine.execute_workflow("daily", workflow))

    assert result is False
    log_info.assert_any_call(
        "Skipping %s hooks during dry-run (%d configured)", "workflow 'daily' on_error", 1
    )


def test_backup_pre_hook_failure_calls_on_error_and_returns_false(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """pre_hooks schlägt fehl: kein Backup, on_error_hooks ausgeführt, False."""
    backup_config.hooks = ResolvedHooksConfig(pre_hooks=["./pre.sh"], on_error_hooks=["./err.sh"])
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)

    hook_calls: list[list[str]] = []

    async def hook_run_side_effect(hooks: list[str]) -> bool:
        hook_calls.append(hooks)
        if hooks == ["./pre.sh"]:
            return False
        return True

    with (
        patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        patch("src.core.workflow.BackupExecutor") as backup_mock,
    ):
        mock_hook_cls.return_value.run = AsyncMock(side_effect=hook_run_side_effect)
        result = asyncio.run(engine._run_backup("local"))

    assert result is False
    backup_mock.return_value.execute.assert_not_called()
    assert ["./err.sh"] in hook_calls


def test_backup_backup_failure_calls_on_error_and_returns_false(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """Backup schlägt fehl → on_error_hooks ausgeführt, False zurückgegeben."""
    backup_config.hooks = ResolvedHooksConfig(on_error_hooks=["./err.sh"])
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)

    hook_calls: list[list[str]] = []

    async def hook_run_side_effect(hooks: list[str]) -> bool:
        hook_calls.append(hooks)
        return True

    with (
        patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        patch("src.core.workflow.BackupExecutor") as backup_mock,
    ):
        mock_hook_cls.return_value.run = AsyncMock(side_effect=hook_run_side_effect)
        backup_mock.return_value.execute = AsyncMock(return_value=False)
        result = asyncio.run(engine._run_backup("local"))

    assert result is False
    assert ["./err.sh"] in hook_calls


def test_backup_post_hook_failure_returns_true(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """post_hooks schlägt fehl → Backup war erfolgreich → True (Warning geloggt)."""
    backup_config.hooks = ResolvedHooksConfig(post_hooks=["./post.sh"])
    backup_config.execution = ResolvedExecutionConfig(retention=False, cleanup=False)

    async def hook_run_side_effect(hooks: list[str]) -> bool:
        if hooks == ["./post.sh"]:
            return False
        return True

    with (
        patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        patch("src.core.workflow.BackupExecutor") as backup_mock,
    ):
        mock_hook_cls.return_value.run = AsyncMock(side_effect=hook_run_side_effect)
        backup_mock.return_value.execute = AsyncMock(return_value=True)
        result = asyncio.run(engine._run_backup("local"))

    assert result is True


def test_workflow_pre_hook_failure_aborts(engine: WorkflowEngine) -> None:
    """Workflow-pre_hooks schlägt fehl → Steps nicht ausgeführt, on_error_hooks aufgerufen."""
    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local"],
        hooks=ResolvedHooksConfig(pre_hooks=["./pre.sh"], on_error_hooks=["./err.sh"]),
    )

    hook_calls: list[list[str]] = []

    async def hook_run_side_effect(hooks: list[str]) -> bool:
        hook_calls.append(hooks)
        if hooks == ["./pre.sh"]:
            return False
        return True

    with (
        patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        patch.object(engine, "_run_backup", return_value=True) as backup_mock,
    ):
        mock_hook_cls.return_value.run = AsyncMock(side_effect=hook_run_side_effect)
        result = asyncio.run(engine.execute_workflow("full", workflow))

    assert result is False
    backup_mock.assert_not_called()
    assert ["./err.sh"] in hook_calls


def test_workflow_post_hook_failure_returns_true(engine: WorkflowEngine) -> None:
    """Alle Steps erfolgreich, post_hooks schlägt fehl → True (Warning geloggt)."""
    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local"],
        hooks=ResolvedHooksConfig(post_hooks=["./post.sh"]),
    )

    async def hook_run_side_effect(hooks: list[str]) -> bool:
        if hooks == ["./post.sh"]:
            return False
        return True

    with (
        patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        patch.object(engine, "_run_backup", return_value=True),
    ):
        mock_hook_cls.return_value.run = AsyncMock(side_effect=hook_run_side_effect)
        result = asyncio.run(engine.execute_workflow("full", workflow))

    assert result is True


def test_workflow_cancelled_step_skips_following_step_and_on_error_hook(
    engine: WorkflowEngine,
) -> None:
    """Cancellation eines laufenden Steps propagiert ohne Fehlerbehandlung."""
    workflow = ResolvedWorkflowConfig(
        schedule="0 2 * * *",
        steps=["backup.local.backup", "backup.local.retention"],
        hooks=ResolvedHooksConfig(on_error_hooks=["./err.sh"]),
    )
    started = asyncio.Event()
    following_step = AsyncMock(return_value=True)

    async def cancelled_step(_: str) -> bool:
        started.set()
        await asyncio.Future()
        return True

    async def scenario() -> None:
        with (
            patch.object(engine, "_run_backup_backup", side_effect=cancelled_step),
            patch.object(engine, "_run_backup_forget", following_step),
            patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        ):
            hook_run = AsyncMock(return_value=True)
            mock_hook_cls.return_value.run = hook_run
            task = asyncio.create_task(engine.execute_workflow("full", workflow))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        following_step.assert_not_called()
        hook_run.assert_awaited_once_with([])

    asyncio.run(scenario())


def test_cancelled_backup_executor_skips_on_error_hook(
    engine: WorkflowEngine, backup_config: ResolvedBackupConfig
) -> None:
    """Cancellation eines Backup-Executors propagiert ohne on_error_hooks."""
    backup_config.hooks = ResolvedHooksConfig(on_error_hooks=["./err.sh"])
    started = asyncio.Event()

    async def cancelled_execute(_: ResolvedBackupConfig) -> bool:
        started.set()
        await asyncio.Future()
        return True

    async def scenario() -> None:
        with (
            patch("src.core.workflow.BackupExecutor") as backup_mock,
            patch("src.core.workflow.HookExecutor") as mock_hook_cls,
        ):
            hook_run = AsyncMock(return_value=True)
            mock_hook_cls.return_value.run = hook_run
            backup_mock.return_value.execute = AsyncMock(side_effect=cancelled_execute)
            task = asyncio.create_task(engine.execute_backup("local"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        hook_run.assert_awaited_once_with([])

    asyncio.run(scenario())
