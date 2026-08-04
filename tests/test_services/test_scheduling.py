from __future__ import annotations

from datetime import datetime, timedelta

from src.models.resolved_config import ResolvedBackupConfig, ResolvedExecutionConfig
from src.services.scheduling import backup_steps, next_run_datetime


def test_next_run_datetime_returns_none_for_manual_schedule() -> None:
    assert next_run_datetime(None) is None
    assert next_run_datetime("") is None


def test_next_run_datetime_returns_future_local_timestamp() -> None:
    before = datetime.now().astimezone()

    result = next_run_datetime("* * * * *")

    assert result is not None
    assert result.tzinfo is not None
    assert before < result <= before + timedelta(minutes=2)


def test_next_run_datetime_returns_none_for_unreachable_schedule() -> None:
    assert next_run_datetime("0 0 30 2 *") is None


def test_backup_steps_includes_enabled_post_steps() -> None:
    backup = ResolvedBackupConfig(
        repository="/repo",
        execution=ResolvedExecutionConfig(retention=True, cleanup=True),
    )

    assert backup_steps(backup) == ["backup", "retention", "cleanup"]


def test_backup_steps_omits_disabled_or_inherited_post_steps() -> None:
    inherited = ResolvedBackupConfig(repository="/repo")
    disabled = ResolvedBackupConfig(
        repository="/repo",
        execution=ResolvedExecutionConfig(retention=False, cleanup=False),
    )

    assert backup_steps(inherited) == ["backup"]
    assert backup_steps(disabled) == ["backup"]
