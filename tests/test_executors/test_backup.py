import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_mock import MockerFixture

from src.executors.backup import BackupExecutor
from src.models.resolved_config import (
    ResolvedBackendOptions,
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedExecutionConfig,
    ResolvedFiltersConfig,
    ResolvedInputConfig,
    ResolvedResticBackendOptions,
)
from tests.stream_stubs import stream_command_stub


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def make_executor(sources: list[str] | None = None) -> BackupExecutor:
    return BackupExecutor("test-job", sources or ["/data"])


@pytest.fixture()
def backup_config() -> ResolvedBackupConfig:
    return ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
    )


@pytest.mark.anyio
async def test_execute_success(backup_config: ResolvedBackupConfig, mocker: MockerFixture) -> None:
    executor = make_executor()
    mock = mocker.patch.object(executor, "_backup_to_repo", return_value=True)

    result = await executor.execute(backup_config)

    assert result is True
    mock.assert_called_once_with("/backups/test", backup_config)


@pytest.mark.anyio
async def test_execute_fails_returns_false(
    backup_config: ResolvedBackupConfig, mocker: MockerFixture
) -> None:
    executor = make_executor()
    mocker.patch.object(executor, "_backup_to_repo", return_value=False)

    assert await executor.execute(backup_config) is False


def test_build_command_basic_structure(backup_config: ResolvedBackupConfig) -> None:
    executor = make_executor(["/data/docs", "/data/photos"])
    cmd = executor._build_command("/backups/test", backup_config)

    assert cmd[:5] == ["restic", "--repo", "/backups/test", "backup", "--json"]
    assert "--" in cmd
    assert "/data/docs" in cmd
    assert "/data/photos" in cmd


def test_build_command_sources_preceded_by_double_dash(backup_config: ResolvedBackupConfig) -> None:
    """Sources werden nach '--' angehängt (verhindert Option-Injection)."""
    executor = make_executor(["/data", "/mnt/backup"])
    cmd = executor._build_command("/backups/test", backup_config)

    separator_idx = cmd.index("--")
    assert "/data" in cmd[separator_idx:]
    assert "/mnt/backup" in cmd[separator_idx:]


def test_build_command_flags_come_before_double_dash() -> None:
    """Restic options must stay before ``--`` so they are not treated as sources."""
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        filters=ResolvedFiltersConfig(exclude=["*.tmp"]),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(exclude_caches=True)
        ),
        tags=["daily"],
    )
    executor = make_executor(["/data"])
    cmd = executor._build_command("/backups/test", config)

    separator_idx = cmd.index("--")
    flags_section = cmd[:separator_idx]
    sources_section = cmd[separator_idx:]

    assert "--exclude=*.tmp" in flags_section, "--exclude muss VOR '--' stehen"
    assert "--exclude-caches" in flags_section, "--exclude-caches muss VOR '--' stehen"
    assert "--tag" in flags_section, "--tag muss VOR '--' stehen"
    assert "/data" in sources_section, "Source muss nach '--' stehen"


def test_build_command_source_files_no_double_dash(backup_config: ResolvedBackupConfig) -> None:
    """source_files und sources dürfen gemeinsam an restic übergeben werden."""
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        input=ResolvedInputConfig(source_files=["/config/sources.txt"]),
    )
    executor = make_executor(["/data"])
    cmd = executor._build_command("/backups/test", config)

    assert "--files-from" in cmd
    assert "--" in cmd
    assert "/data" in cmd


def test_build_command_exclude_patterns() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        filters=ResolvedFiltersConfig(exclude=["*.tmp", "*.log"]),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--exclude=*.tmp" in cmd
    assert "--exclude=*.log" in cmd


def test_build_command_with_tags() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        tags=["daily", "automated"],
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--tag" in cmd
    assert "daily" in cmd
    assert "automated" in cmd


def test_build_command_exclude_caches() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(exclude_caches=True)
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--exclude-caches" in cmd


def test_build_command_no_exclude_caches_when_false() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(exclude_caches=False)
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--exclude-caches" not in cmd


def test_build_command_no_exclude_caches_when_none() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(exclude_caches=None)
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--exclude-caches" not in cmd


def test_build_command_one_file_system() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(one_file_system=True)
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--one-file-system" in cmd


def test_build_command_source_files_combines_with_sources() -> None:
    """source_files und sources werden gemeinsam ans Restic-Kommando übergeben."""
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        input=ResolvedInputConfig(source_files=["/config/sources.txt"]),
    )
    executor = make_executor(["/data"])
    cmd = executor._build_command("/backups/test", config)

    assert "--files-from" in cmd
    assert "/config/sources.txt" in cmd
    assert "/data" in cmd


def test_build_command_exclude_file() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        filters=ResolvedFiltersConfig(exclude_files=["/config/excludes.txt"]),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--exclude-file" in cmd
    assert "/config/excludes.txt" in cmd


def test_build_command_extra_args() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_backup_args=["--verbose", "--no-cache"])
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--verbose" in cmd
    assert "--no-cache" in cmd
    assert cmd.index("--verbose") > cmd.index("backup")


def test_build_command_extra_args_none_safe() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--verbose" not in cmd


def test_build_command_extra_args_split_flag_value() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_backup_args=["--host homeserver"])
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--host" in cmd
    assert "homeserver" in cmd
    assert "--host homeserver" not in cmd


def test_build_command_extra_args_preserve_quoted_value() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        backend_options=ResolvedBackendOptions(
            restic=ResolvedResticBackendOptions(extra_backup_args=['--tag "Server Backup"'])
        ),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--tag" in cmd
    assert "Server Backup" in cmd
    assert '"Server Backup"' not in cmd


def test_build_command_dry_run_flag() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
    )
    executor = BackupExecutor("test-job", ["/data"], dry_run=True)
    cmd = executor._build_command("/backups/test", config)

    assert "--dry-run" in cmd


def test_build_command_no_dry_run_flag_by_default() -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
    )
    cmd = make_executor()._build_command("/backups/test", config)

    assert "--dry-run" not in cmd


def test_build_command_enables_json_output(backup_config: ResolvedBackupConfig) -> None:
    cmd = make_executor()._build_command("/backups/test", backup_config)

    assert "--json" in cmd
    assert cmd.index("--json") > cmd.index("backup")


@pytest.mark.anyio
async def test_backup_to_repo_password_error_returns_false(mocker: MockerFixture) -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password_env="NONEXISTENT_PW_VAR"),
    )
    mocker.patch.dict("os.environ", {}, clear=False)
    import os

    os.environ.pop("NONEXISTENT_PW_VAR", None)

    result = await make_executor()._backup_to_repo("/backups/test", config)

    # password_env ist nicht aufgelöst (kein password gesetzt) -> resolve_env
    # erhält None/None und setzt keine RESTIC_PASSWORD*-Variablen; das
    # Subprocess-Mock liefert hier keinen Erfolg, da run_subprocess real
    # ausgeführt wird und restic fehlschlägt.
    assert result is False


@pytest.mark.anyio
async def test_backup_to_repo_auto_init_false_skips_check(
    backup_config: ResolvedBackupConfig, mocker: MockerFixture
) -> None:
    executor = make_executor()
    ensure_mock = mocker.patch("src.executors.backup.async_ensure_repository")
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor._backup_to_repo("/backups/test", backup_config)

    ensure_mock.assert_not_called()


@pytest.mark.anyio
async def test_backup_to_repo_auto_init_true_checks_repo(mocker: MockerFixture) -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        execution=ResolvedExecutionConfig(auto_init=True),
    )
    executor = make_executor()
    ensure_mock = mocker.patch("src.executors.backup.async_ensure_repository", return_value=True)
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor._backup_to_repo("/backups/test", config)

    ensure_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_backup_to_repo_auto_init_passes_executor_timeout(mocker: MockerFixture) -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        execution=ResolvedExecutionConfig(auto_init=True),
    )
    executor = BackupExecutor("test-job", ["/data"], timeout=42)
    ensure_mock = mocker.patch("src.executors.backup.async_ensure_repository", return_value=True)
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor._backup_to_repo("/backups/test", config)

    assert ensure_mock.call_args.kwargs["timeout"] == 42


@pytest.mark.anyio
async def test_backup_to_repo_auto_init_fails_returns_false(mocker: MockerFixture) -> None:
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        execution=ResolvedExecutionConfig(auto_init=True),
    )
    executor = make_executor()
    mocker.patch("src.executors.backup.async_ensure_repository", return_value=False)
    run_mock = mocker.patch.object(executor, "run_subprocess")

    result = await executor._backup_to_repo("/backups/test", config)

    assert result is False
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_backup_to_repo_subprocess_failure_returns_false(mocker: MockerFixture) -> None:
    """run_subprocess schlägt fehl → False, Fehler geloggt."""
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
    )
    executor = make_executor()
    mocker.patch.object(executor, "run_subprocess", return_value=False)

    result = await executor._backup_to_repo("/backups/test", config)

    assert result is False


@pytest.mark.anyio
async def test_backup_to_repo_captures_json_summary_without_raw_stdout_logs(
    backup_config: ResolvedBackupConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = make_executor()
    stdout = (
        b'{"message_type":"status","percent_done":0.5}\n'
        b"not json\n"
        b'{"message_type":"summary","snapshot_id":"abcdef1234567890",'
        b'"files_new":2,"files_changed":1,"files_unmodified":3,'
        b'"dirs_new":4,"dirs_changed":5,"dirs_unmodified":6,'
        b'"data_added":789,"total_files_processed":6,'
        b'"total_bytes_processed":12345,"total_duration":1.25}'
    )

    with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.test-job.BackupExecutor"):
        with patch(
            "src.executors.base.stream_command",
            new=AsyncMock(side_effect=stream_command_stub(returncode=0, stdout=stdout)),
        ):
            result = await executor._backup_to_repo("/backups/test", backup_config)

    assert result is True
    assert executor.summary is not None
    assert executor.summary.snapshot_id == "abcdef1234567890"
    assert executor.summary.raw["snapshot_id"] == "abcdef1234567890"
    assert executor.summary.files_new == 2
    assert executor.summary.files_changed == 1
    assert executor.summary.files_unmodified == 3
    assert executor.summary.dirs_new == 4
    assert executor.summary.dirs_changed == 5
    assert executor.summary.dirs_unmodified == 6
    assert executor.summary.data_added == 789
    assert executor.summary.total_files_processed == 6
    assert executor.summary.total_bytes_processed == 12345
    assert executor.summary.total_duration == 1.25
    assert '"message_type":"summary"' not in caplog.text
    assert "[stdout]" not in caplog.text


@pytest.mark.anyio
async def test_backup_to_repo_keeps_success_when_summary_is_missing(
    backup_config: ResolvedBackupConfig,
) -> None:
    executor = make_executor()

    with patch(
        "src.executors.base.stream_command",
        new=AsyncMock(
            side_effect=stream_command_stub(
                returncode=0,
                stdout=b'{"message_type":"summary","files_new":2}\n',
            )
        ),
    ):
        result = await executor._backup_to_repo("/backups/test", backup_config)

    assert result is True
    assert executor.summary is None


@pytest.mark.anyio
async def test_backup_to_repo_dry_run_auto_init_checks_repo(mocker: MockerFixture) -> None:
    """dry_run=True: auto_init bereitet das Repository vor."""
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password="secret"),
        execution=ResolvedExecutionConfig(auto_init=True),
    )
    executor = BackupExecutor("test-job", ["/data"], dry_run=True)
    ensure_mock = mocker.patch("src.executors.backup.async_ensure_repository", return_value=True)
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor._backup_to_repo("/backups/test", config)

    ensure_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_backup_logs_repository_credentials(caplog: pytest.LogCaptureFixture) -> None:
    repository = "https://user:pass@example.test/repo"
    config = ResolvedBackupConfig(
        repository=repository,
        credentials=ResolvedCredentials(password="restic-secret"),
    )
    executor = BackupExecutor("backup-log-job", ["/data"])

    with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.backup-log-job.BackupExecutor"):
        with patch(
            "src.executors.base.stream_command",
            new=AsyncMock(side_effect=stream_command_stub(returncode=1)),
        ):
            await executor._backup_to_repo(repository, config)

    assert "https://user:pass@example.test/repo" in caplog.text


@pytest.mark.anyio
async def test_backup_to_repo_passes_password_file_to_resolve_env(
    mocker: MockerFixture,
) -> None:
    """Bei backup.password_file wird resolve_env mit dem korrekten Argument aufgerufen."""
    config = ResolvedBackupConfig(
        repository="/backups/test",
        credentials=ResolvedCredentials(password_file="/run/secrets/restic-pw"),
    )
    executor = make_executor()
    mock_resolve = mocker.patch(
        "src.executors.backup.resolve_env",
        return_value={"RESTIC_PASSWORD_FILE": "/run/secrets/restic-pw"},
    )
    mocker.patch.object(executor, "run_subprocess", return_value=True)

    await executor._backup_to_repo("/backups/test", config)

    mock_resolve.assert_called_once_with(
        config.credentials.password, config.credentials.password_file
    )
