import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_mock import MockerFixture

from src.executors.forget import ForgetExecutor
from src.models.resolved_config import (
    ResolvedBackendOptions,
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedResticBackendOptions,
    ResolvedRetentionConfig,
)
from tests.stream_stubs import stream_command_stub


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def make_executor() -> ForgetExecutor:
    return ForgetExecutor("test-job")


@pytest.fixture()
def backup_config() -> ResolvedBackupConfig:
    return ResolvedBackupConfig(
        repository="/backups/local",
        retention=ResolvedRetentionConfig(keep_daily=7),
        credentials=ResolvedCredentials(password="secret"),
    )


@pytest.mark.anyio
async def test_execute_success(backup_config: ResolvedBackupConfig, mocker: MockerFixture) -> None:
    executor = make_executor()
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    result = await executor.execute(backup_config)

    assert result is True


@pytest.mark.anyio
async def test_execute_subprocess_failure_returns_false(
    backup_config: ResolvedBackupConfig, mocker: MockerFixture
) -> None:
    executor = make_executor()
    mocker.patch.object(executor, "run_subprocess", return_value=False)

    assert await executor.execute(backup_config) is False


@pytest.mark.anyio
async def test_execute_password_env_missing_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NONEXISTENT_PW", raising=False)
    config = ResolvedBackupConfig(
        repository="/backups/local",
        credentials=ResolvedCredentials(password_env="NONEXISTENT_PW"),
    )
    executor = make_executor()

    result = await executor.execute(config)

    assert result is False


def test_build_command_basic_structure() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_daily=7)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert cmd[:4] == ["restic", "--repo", "/backups/local", "forget"]


def test_build_command_keep_last() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_last=5)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-last" in cmd
    assert "5" in cmd


def test_build_command_keep_hourly() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_hourly=24)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-hourly" in cmd
    assert "24" in cmd


def test_build_command_keep_daily() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_daily=7)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-daily" in cmd
    assert "7" in cmd


def test_build_command_keep_weekly() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_weekly=4)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-weekly" in cmd
    assert "4" in cmd


def test_build_command_keep_monthly() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_monthly=6)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-monthly" in cmd
    assert "6" in cmd


def test_build_command_keep_yearly() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_yearly=1)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-yearly" in cmd
    assert "1" in cmd


def test_build_command_keep_within() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_within="2m3d")
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-within" in cmd
    assert "2m3d" in cmd


def test_build_command_keep_within_granular_policies() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        retention=ResolvedRetentionConfig(
            keep_within_hourly="7d",
            keep_within_daily="1m",
            keep_within_weekly="1y",
            keep_within_monthly="5y",
            keep_within_yearly="75y",
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-within-hourly" in cmd
    assert "7d" in cmd
    assert "--keep-within-daily" in cmd
    assert "1m" in cmd
    assert "--keep-within-weekly" in cmd
    assert "1y" in cmd
    assert "--keep-within-monthly" in cmd
    assert "5y" in cmd
    assert "--keep-within-yearly" in cmd
    assert "75y" in cmd


def test_build_command_no_keep_flags_when_none() -> None:
    """Ohne keep-Parameter erscheinen keine --keep-* Flags."""
    config = ResolvedBackupConfig(repository="/backups/local")
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-last" not in cmd
    assert "--keep-hourly" not in cmd
    assert "--keep-daily" not in cmd
    assert "--keep-weekly" not in cmd
    assert "--keep-monthly" not in cmd
    assert "--keep-yearly" not in cmd
    assert "--keep-within" not in cmd
    assert "--keep-within-hourly" not in cmd
    assert "--keep-within-daily" not in cmd
    assert "--keep-within-weekly" not in cmd
    assert "--keep-within-monthly" not in cmd
    assert "--keep-within-yearly" not in cmd


@pytest.mark.anyio
async def test_build_command_correct_repo_path_used(mocker: MockerFixture) -> None:
    """execute() übergibt config.repository an run_subprocess."""
    config = ResolvedBackupConfig(
        repository="/backups/myrepo",
        retention=ResolvedRetentionConfig(keep_daily=7),
        credentials=ResolvedCredentials(password="secret"),
    )
    executor = make_executor()
    run_mock = mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor.execute(config)

    called_cmd = run_mock.call_args[0][0]
    assert "/backups/myrepo" in called_cmd


def test_build_command_extra_args() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        retention=ResolvedRetentionConfig(keep_daily=7),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_forget_args=["--verbose", "--no-cache"])
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--verbose" in cmd
    assert "--no-cache" in cmd
    assert cmd.index("--verbose") > cmd.index("forget")


def test_build_command_extra_args_none_safe() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_daily=7)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--verbose" not in cmd


def test_build_command_extra_args_split_flag_value() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        retention=ResolvedRetentionConfig(keep_daily=7),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_forget_args=["--keep-tag manual"])
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-tag" in cmd
    assert "manual" in cmd
    assert "--keep-tag manual" not in cmd


def test_build_command_extra_args_preserve_quoted_value() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        retention=ResolvedRetentionConfig(keep_daily=7),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_forget_args=['--keep-tag "Server Backup"'])
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--keep-tag" in cmd
    assert "Server Backup" in cmd
    assert '"Server Backup"' not in cmd


def test_build_command_dry_run_flag() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_daily=7)
    )
    executor = ForgetExecutor("test-job", dry_run=True)
    cmd = executor._build_command("/backups/local", config)

    assert "--dry-run" in cmd


def test_build_command_no_dry_run_flag_by_default() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local", retention=ResolvedRetentionConfig(keep_daily=7)
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--dry-run" not in cmd


@pytest.mark.anyio
async def test_forget_logs_repository_credentials(caplog: pytest.LogCaptureFixture) -> None:
    config = ResolvedBackupConfig(
        repository="https://user:pass@example.test/repo",
        retention=ResolvedRetentionConfig(keep_daily=7),
        credentials=ResolvedCredentials(password="secret"),
    )
    executor = ForgetExecutor("forget-log-job")

    with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.forget-log-job.ForgetExecutor"):
        with patch(
            "src.executors.base.stream_command",
            new=AsyncMock(side_effect=stream_command_stub(returncode=1)),
        ):
            await executor.execute(config)

    assert "https://user:pass@example.test/repo" in caplog.text


@pytest.mark.anyio
async def test_execute_passes_password_file_to_resolve_env(
    mocker: MockerFixture,
) -> None:
    """Bei backup.password_file wird resolve_env mit dem korrekten Argument aufgerufen."""
    config = ResolvedBackupConfig(
        repository="/backups/local",
        retention=ResolvedRetentionConfig(keep_daily=7),
        credentials=ResolvedCredentials(password_file="/run/secrets/restic-pw"),
    )
    executor = make_executor()
    mock_resolve = mocker.patch(
        "src.executors.forget.resolve_env",
        return_value={"RESTIC_PASSWORD_FILE": "/run/secrets/restic-pw"},
    )
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor.execute(config)

    mock_resolve.assert_called_once_with(
        config.credentials.password, config.credentials.password_file
    )
