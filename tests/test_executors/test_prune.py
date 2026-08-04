import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_mock import MockerFixture

from src.executors.prune import PruneExecutor
from src.models.resolved_config import (
    ResolvedBackendOptions,
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedResticBackendOptions,
)
from tests.stream_stubs import stream_command_stub


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def make_executor() -> PruneExecutor:
    return PruneExecutor("test-job")


@pytest.fixture()
def backup_config() -> ResolvedBackupConfig:
    return ResolvedBackupConfig(
        repository="/backups/local", credentials=ResolvedCredentials(password="secret")
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
    config = ResolvedBackupConfig(repository="/backups/local")
    cmd = make_executor()._build_command("/backups/local", config)

    assert cmd == ["restic", "--repo", "/backups/local", "prune"]


@pytest.mark.anyio
async def test_build_command_correct_repo_path_used(mocker: MockerFixture) -> None:
    """execute() übergibt config.repository an run_subprocess."""
    config = ResolvedBackupConfig(
        repository="/backups/myrepo", credentials=ResolvedCredentials(password="secret")
    )
    executor = make_executor()
    run_mock = mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor.execute(config)

    called_cmd = run_mock.call_args[0][0]
    assert "/backups/myrepo" in called_cmd


def test_build_command_extra_args() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_prune_args=["--verbose", "--no-cache"])
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--verbose" in cmd
    assert "--no-cache" in cmd
    assert cmd.index("--verbose") > cmd.index("prune")


def test_build_command_extra_args_none_safe() -> None:
    config = ResolvedBackupConfig(repository="/backups/local")
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--verbose" not in cmd


def test_build_command_extra_args_split_flag_value() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_prune_args=["--max-unused 5%"])
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--max-unused" in cmd
    assert "5%" in cmd
    assert "--max-unused 5%" not in cmd


def test_build_command_extra_args_preserve_quoted_value() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/local",
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_prune_args=['--option "value with spaces"'])
        ),
    )
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--option" in cmd
    assert "value with spaces" in cmd
    assert '"value with spaces"' not in cmd


def test_build_command_dry_run_flag() -> None:
    config = ResolvedBackupConfig(repository="/backups/local")
    executor = PruneExecutor("test-job", dry_run=True)
    cmd = executor._build_command("/backups/local", config)

    assert "--dry-run" in cmd


def test_build_command_no_dry_run_flag_by_default() -> None:
    config = ResolvedBackupConfig(repository="/backups/local")
    cmd = make_executor()._build_command("/backups/local", config)

    assert "--dry-run" not in cmd


@pytest.mark.anyio
async def test_prune_logs_repository_credentials(caplog: pytest.LogCaptureFixture) -> None:
    config = ResolvedBackupConfig(
        repository="https://user:pass@example.test/repo",
        credentials=ResolvedCredentials(password="secret"),
    )
    executor = PruneExecutor("prune-log-job")

    with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.prune-log-job.PruneExecutor"):
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
        credentials=ResolvedCredentials(password_file="/run/secrets/restic-pw"),
    )
    executor = make_executor()
    mock_resolve = mocker.patch(
        "src.executors.prune.resolve_env",
        return_value={"RESTIC_PASSWORD_FILE": "/run/secrets/restic-pw"},
    )
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor.execute(config)

    mock_resolve.assert_called_once_with(
        config.credentials.password, config.credentials.password_file
    )
