import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from src.executors.rclone import RcloneExecutor
from src.models.resolved_config import ResolvedRcloneOptionsConfig, ResolvedRcloneSyncTaskConfig


def make_executor() -> RcloneExecutor:
    return RcloneExecutor("test-job")


def execute(executor: RcloneExecutor, config: ResolvedRcloneSyncTaskConfig) -> bool:
    return asyncio.run(executor.execute(config))


@pytest.fixture()
def copy_config() -> ResolvedRcloneSyncTaskConfig:
    return ResolvedRcloneSyncTaskConfig(
        source="/backups/local",
        target="gdrive:backups/home",
    )


@pytest.fixture()
def sync_config() -> ResolvedRcloneSyncTaskConfig:
    return ResolvedRcloneSyncTaskConfig(
        source="/backups/local",
        target="gdrive:backups/home",
        sync_delete=True,
    )


def test_execute_copy_success(
    copy_config: ResolvedRcloneSyncTaskConfig, mocker: MockerFixture
) -> None:
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    mocker.patch.object(executor, "run_subprocess", new_callable=AsyncMock, return_value=True)

    result = execute(executor, copy_config)

    assert result is True


def test_execute_sync_success(
    sync_config: ResolvedRcloneSyncTaskConfig, tmp_path: Path, mocker: MockerFixture
) -> None:
    executor = make_executor()
    source = tmp_path / "repo"
    source.mkdir()
    (source / "config").write_text("restic repo\n", encoding="utf-8")
    sync_config = sync_config.model_copy(update={"source": str(source)})
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    mocker.patch.object(executor, "run_subprocess", new_callable=AsyncMock, return_value=True)

    result = execute(executor, sync_config)

    assert result is True


@pytest.mark.parametrize("source_state", ["missing", "file", "empty_dir"])
def test_execute_sync_delete_rejects_unsafe_local_source(
    tmp_path: Path, source_state: str, mocker: MockerFixture
) -> None:
    if source_state == "missing":
        source = tmp_path / "missing"
    elif source_state == "file":
        source = tmp_path / "source-file"
        source.write_text("not a directory\n", encoding="utf-8")
    else:
        source = tmp_path / "empty"
        source.mkdir()
    config = ResolvedRcloneSyncTaskConfig(
        source=str(source),
        target="gdrive:backups/home",
        sync_delete=True,
    )
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    run_mock = mocker.patch.object(executor, "run_subprocess", new_callable=AsyncMock)

    result = execute(executor, config)

    assert result is False
    run_mock.assert_not_awaited()


def test_execute_copy_allows_empty_local_source(tmp_path: Path, mocker: MockerFixture) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    config = ResolvedRcloneSyncTaskConfig(source=str(source), target="gdrive:backups/home")
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    run_mock = mocker.patch.object(
        executor, "run_subprocess", new_callable=AsyncMock, return_value=True
    )

    result = execute(executor, config)

    assert result is True
    run_mock.assert_awaited_once()


def test_execute_subprocess_failure_returns_false(
    copy_config: ResolvedRcloneSyncTaskConfig, mocker: MockerFixture
) -> None:
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    mocker.patch.object(executor, "run_subprocess", new_callable=AsyncMock, return_value=False)

    assert execute(executor, copy_config) is False


def test_execute_calls_run_subprocess(
    copy_config: ResolvedRcloneSyncTaskConfig, mocker: MockerFixture
) -> None:
    """execute() übergibt das korrekte Kommando an run_subprocess."""
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    run_mock = mocker.patch.object(
        executor, "run_subprocess", new_callable=AsyncMock, return_value=True
    )

    execute(executor, copy_config)

    run_mock.assert_awaited_once()
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rclone"
    assert cmd[1] == "copy"
    assert "/backups/local" in cmd
    assert "gdrive:backups/home" in cmd


def test_execute_remote_not_configured_returns_false(
    copy_config: ResolvedRcloneSyncTaskConfig, mocker: MockerFixture
) -> None:
    """Nicht konfiguriertes Remote → False, kein subprocess-Aufruf."""
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=False
    )
    run_mock = mocker.patch.object(executor, "run_subprocess", new_callable=AsyncMock)

    result = execute(executor, copy_config)

    assert result is False
    run_mock.assert_not_awaited()


def test_execute_remote_check_uses_executor_timeout(
    copy_config: ResolvedRcloneSyncTaskConfig, mocker: MockerFixture
) -> None:
    executor = RcloneExecutor("test-job", timeout=37)
    check_mock = mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    mocker.patch.object(executor, "run_subprocess", new_callable=AsyncMock, return_value=True)

    assert execute(executor, copy_config) is True
    check_mock.assert_awaited_once_with(copy_config.target, timeout=37)


def test_execute_propagates_cancellation(
    copy_config: ResolvedRcloneSyncTaskConfig, mocker: MockerFixture
) -> None:
    executor = make_executor()
    mocker.patch(
        "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
    )
    mocker.patch.object(
        executor,
        "run_subprocess",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError,
    )

    with pytest.raises(asyncio.CancelledError):
        execute(executor, copy_config)


def test_build_command_copy_when_sync_delete_false() -> None:
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst")
    cmd = make_executor()._build_command(config)

    assert cmd[:4] == ["rclone", "copy", "/src", "remote:dst"]


def test_build_command_sync_when_sync_delete_true() -> None:
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst", sync_delete=True)
    cmd = make_executor()._build_command(config)

    assert cmd[:4] == ["rclone", "sync", "/src", "remote:dst"]


def test_build_command_transfers() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src", target="remote:dst", options=ResolvedRcloneOptionsConfig(transfers=8)
    )
    cmd = make_executor()._build_command(config)

    assert "--transfers" in cmd
    assert "8" in cmd


def test_build_command_checkers() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src", target="remote:dst", options=ResolvedRcloneOptionsConfig(checkers=16)
    )
    cmd = make_executor()._build_command(config)

    assert "--checkers" in cmd
    assert "16" in cmd


def test_build_command_bwlimit() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src", target="remote:dst", options=ResolvedRcloneOptionsConfig(bwlimit="10M")
    )
    cmd = make_executor()._build_command(config)

    assert "--bwlimit" in cmd
    assert "10M" in cmd


def test_build_command_exclude_single() -> None:
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst", exclude=["*.tmp"])
    cmd = make_executor()._build_command(config)

    assert "--exclude" in cmd
    assert "*.tmp" in cmd


def test_build_command_exclude_multiple() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src", target="remote:dst", exclude=["*.tmp", "*.log"]
    )
    cmd = make_executor()._build_command(config)

    assert cmd.count("--exclude") == 2
    assert "*.tmp" in cmd
    assert "*.log" in cmd


def test_build_command_filter_from() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src", target="remote:dst", filter_from="/config/filters.txt"
    )
    cmd = make_executor()._build_command(config)

    assert "--filter-from" in cmd
    assert "/config/filters.txt" in cmd


def test_build_command_no_dry_run_by_default() -> None:
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst")
    cmd = make_executor()._build_command(config)

    assert "--dry-run" not in cmd


def test_build_command_dry_run_runtime_flag() -> None:
    """dry_run=True am Executor erzeugt --dry-run."""
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst")
    executor = RcloneExecutor("test-job", dry_run=True)
    cmd = executor._build_command(config)

    assert "--dry-run" in cmd


def test_build_command_no_optional_flags_by_default() -> None:
    """Ohne optionale Parameter erscheinen keine zusätzlichen Flags."""
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst")
    cmd = make_executor()._build_command(config)

    assert "--transfers" not in cmd
    assert "--checkers" not in cmd
    assert "--bwlimit" not in cmd
    assert "--exclude" not in cmd
    assert "--filter-from" not in cmd
    assert "--dry-run" not in cmd


def test_build_command_all_options_combined() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/backups/local",
        target="s3:mybucket/backups",
        sync_delete=True,
        exclude=["*.tmp"],
        filter_from="/filters.txt",
        options=ResolvedRcloneOptionsConfig(transfers=4, checkers=8, bwlimit="5M"),
    )
    executor = RcloneExecutor("test-job", dry_run=True)
    cmd = executor._build_command(config)

    assert cmd[:4] == ["rclone", "sync", "/backups/local", "s3:mybucket/backups"]
    assert "--transfers" in cmd
    assert "--checkers" in cmd
    assert "--bwlimit" in cmd
    assert "--exclude" in cmd
    assert "--filter-from" in cmd
    assert "--dry-run" in cmd


def test_build_command_extra_args() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src",
        target="remote:dst",
        options=ResolvedRcloneOptionsConfig(extra_args=["--verbose", "--no-traverse"]),
    )
    cmd = make_executor()._build_command(config)

    assert "--verbose" in cmd
    assert "--no-traverse" in cmd
    assert cmd.index("--verbose") > cmd.index("copy")


def test_build_command_extra_args_none_safe() -> None:
    config = ResolvedRcloneSyncTaskConfig(source="/src", target="remote:dst")
    cmd = make_executor()._build_command(config)

    assert "--verbose" not in cmd


def test_build_command_extra_args_split_flag_value() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src",
        target="remote:dst",
        options=ResolvedRcloneOptionsConfig(extra_args=["--drive-chunk-size 64M", "--transfers 8"]),
    )
    cmd = make_executor()._build_command(config)

    assert "--drive-chunk-size" in cmd
    assert "64M" in cmd
    assert "--transfers" in cmd
    assert "8" in cmd
    assert "--drive-chunk-size 64M" not in cmd


def test_build_command_extra_args_preserve_quoted_value() -> None:
    config = ResolvedRcloneSyncTaskConfig(
        source="/src",
        target="remote:dst",
        options=ResolvedRcloneOptionsConfig(
            extra_args=['--metadata-set "backup name=Server Backup"']
        ),
    )
    cmd = make_executor()._build_command(config)

    assert "--metadata-set" in cmd
    assert "backup name=Server Backup" in cmd
    assert '"backup name=Server Backup"' not in cmd
