import asyncio
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.core.subprocesses import StreamedCommandResult
from src.services.appdata_schema import utc_rfc3339
from src.services.config import ConfigService
from src.services.errors import ConfigServiceError, ServiceError
from src.services.restore import (
    RestoreCommand,
    RestoreMode,
    RestorePreview,
    RestoreRecord,
    RestoreRegistry,
    RestoreRequest,
    RestoreService,
    RestoreStatus,
    _run_restore_command,
)
from src.services.run_history import RunHistoryService
from src.services.run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediate(func: Callable[..., Any], /, *args: object, **kwargs: object) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


def _write_restore_config(path: Path, repository: str = "/repo/secret-value") -> None:
    path.write_text(
        f"""
[global.backup]
backup_timeout = 30

[jobs.demo.backup.local]
sources = ["/data"]
repository = "{repository}"
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )


def _write_multi_backup_config(path: Path) -> None:
    path.write_text(
        """
[jobs.demo.backup.local]
sources = ["/data"]
repository = "/repo/a"
password = "secret-value"

[jobs.demo.backup.other]
sources = ["/data"]
repository = "/repo/b"
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )


def _service(tmp_path: Path, repository: str = "/repo/secret-value") -> RestoreService:
    config_path = tmp_path / "config.toml"
    _write_restore_config(config_path, repository)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    db_path = tmp_path / "appdata.db"
    run_history = RunHistoryService(db_path)
    return RestoreService(
        ConfigService(config_path),
        RestoreRegistry(db_path),
        RunManager(on_started=run_history.create_run, on_terminal=run_history.record),
        restore_base_dir=tmp_path / "restore",
        lock_dir=lock_dir,
        log_base_dir=tmp_path / "logs",
    )


def _persisted_restore_run_ids(db_path: Path) -> list[str]:
    """Return the restore detail rows currently persisted in the AppData DB."""
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT run_id FROM run_restores").fetchall()
    return [str(row[0]) for row in rows]


async def _wait_for_lifecycle_cleanup(manager: RunManager) -> None:
    """Wait until the run manager evicted every record again.

    Eviction only happens once no task, finish task and terminal event
    references the record anymore, so an empty listing is the observable proof
    that the strong task reference was released.
    """
    for _ in range(100):
        if await manager.list() == []:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("runs were not evicted after lifecycle cleanup")


def _mock_result(
    stdout: str = "stdout secret-value",
    stderr: str = "",
    returncode: int = 0,
) -> StreamedCommandResult:
    return StreamedCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _restore_record(
    restore_id: str, status: RestoreStatus = RestoreStatus.SUCCESS
) -> RestoreRecord:
    created_at = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    return RestoreRecord(
        restore_id=restore_id,
        request=RestoreRequest(
            job="demo",
            backup="local",
            snapshot_id="abcdef1234567890",
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/data",),
            include_patterns=(),
            exclude_patterns=(),
            restore_target=Path("/restore/demo/local/abcdef12/20260607T120000Z"),
            overwrite=True,
        ),
        status=status,
        dry_run=True,
        created_at=created_at,
        started_at=created_at,
        finished_at=(
            created_at if status not in {RestoreStatus.QUEUED, RestoreStatus.RUNNING} else None
        ),
        error="boom" if status == RestoreStatus.FAILED else None,
        output="restore output",
        output_truncated=False,
    )


def _preview(service: RestoreService, *args: object, **kwargs: object) -> RestorePreview:
    return asyncio.run(service.preview_restore(*args, **kwargs))  # type: ignore[arg-type]


async def _wait_for_restore(service: RestoreService, restore_id: str) -> RestoreRecord:
    for _ in range(100):
        record = await service.get_restore(restore_id)
        if record.status not in {RestoreStatus.QUEUED, RestoreStatus.RUNNING}:
            return record
        await asyncio.sleep(0.001)
    raise AssertionError("restore did not become terminal")


def test_restore_preview_builds_dry_run_command_without_password_argument(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run_command = AsyncMock(return_value=_mock_result())

    with patch("src.services.restore.stream_command", run_command):
        preview = _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="browser",
            snapshot_paths=["/data/file.txt"],
        )

    assert preview.ok is True
    assert preview.command.argv == [
        "restic",
        "--repo",
        "/repo/secret-value",
        "restore",
        "abcdef1234567890",
        "--target",
        str(preview.request.restore_target),
        "--overwrite",
        "never",
        "--dry-run",
        "--verbose=2",
        "--include",
        "/data/file.txt",
    ]
    assert "--password" not in preview.command.argv
    assert "--password-file" not in preview.command.argv
    assert run_command.call_args.kwargs["env"]["RESTIC_PASSWORD"] == "secret-value"
    assert run_command.call_args.kwargs["timeout"] == 30
    assert preview.output == "stdout secret-value"


def test_restore_pattern_mode_restores_whole_snapshot_without_patterns(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_command = AsyncMock(return_value=_mock_result())

    with patch("src.services.restore.stream_command", run_command):
        preview = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")

    assert preview.request.mode == RestoreMode.PATTERN
    assert preview.request.snapshot_paths == ()
    assert "--include" not in run_command.call_args.args[0]
    assert "--exclude" not in run_command.call_args.args[0]


def test_restore_pattern_mode_normalizes_include_patterns_without_shell_splitting(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run_command = AsyncMock(return_value=_mock_result())

    with patch("src.services.restore.stream_command", run_command):
        preview = _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="pattern",
            include_patterns=["  /data/**  ", "", "/data/**", "*.jpg", "name with spaces"],
        )

    assert preview.request.include_patterns == ("/data/**", "*.jpg", "name with spaces")
    assert run_command.call_args.args[0][-6:] == [
        "--include",
        "/data/**",
        "--include",
        "*.jpg",
        "--include",
        "name with spaces",
    ]


def test_restore_pattern_mode_builds_exclude_arguments(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_command = AsyncMock(return_value=_mock_result())

    with patch("src.services.restore.stream_command", run_command):
        preview = _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="pattern",
            exclude_patterns=[" /cache/** ", "*.tmp"],
        )

    assert preview.request.exclude_patterns == ("/cache/**", "*.tmp")
    assert run_command.call_args.args[0][-4:] == ["--exclude", "/cache/**", "--exclude", "*.tmp"]


def test_restore_streams_full_output_to_job_log_but_bounds_viewmodel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    full_stdout = b"".join(f"line-{i:05d}\n".encode("ascii") for i in range(500))

    async def streaming(
        argv: list[str] | None = None,
        *,
        on_stdout: Callable[[bytes], None] | None = None,
        capture_tail_bytes: int | None = None,
        **_kwargs: object,
    ) -> StreamedCommandResult:
        assert on_stdout is not None
        assert capture_tail_bytes is not None
        on_stdout(full_stdout)
        return StreamedCommandResult(returncode=0, stdout="line-00499\n", stdout_truncated=True)

    with patch("src.services.restore.stream_command", AsyncMock(side_effect=streaming)):
        preview = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")

    # Viewmodel only keeps the bounded tail and reports truncation.
    assert preview.output == "line-00499"
    assert preview.output_truncated is True

    # The full streamed output still reached the restore job log.
    log_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "logs").rglob("*.log")
    )
    assert "line-00000" in log_text
    assert "line-00499" in log_text


def test_restore_timeout_keeps_streamed_tail_and_logs_stderr(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def streaming_timeout(
        argv: list[str] | None = None,
        *,
        on_stdout: Callable[[bytes], None] | None = None,
        on_stderr: Callable[[bytes], None] | None = None,
        **_kwargs: object,
    ) -> StreamedCommandResult:
        assert on_stdout is not None
        assert on_stderr is not None
        on_stdout(b"restore progress\n")
        on_stderr(b"restore warning\n")
        raise asyncio.TimeoutError

    with patch("src.services.restore.stream_command", AsyncMock(side_effect=streaming_timeout)):
        preview = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")

    assert preview.ok is False
    assert preview.output == "restore progress\nrestore warning"
    assert preview.output_truncated is False
    assert preview.error == "restic restore timed out: restore progress\nrestore warning"

    log_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "logs").rglob("*.log")
    )
    assert "restore progress" in log_text
    assert "restore warning" in log_text


@pytest.mark.parametrize(
    ("mode", "snapshot_paths", "include_patterns", "exclude_patterns", "message"),
    [
        ("unknown", None, None, None, "Invalid restore mode"),
        ("pattern", ["/data"], None, None, "Snapshot paths are only allowed in browser mode"),
        ("browser", None, ["*.jpg"], None, "Patterns are only allowed in pattern mode"),
        (
            "pattern",
            None,
            ["*.jpg"],
            ["*.tmp"],
            "Include and exclude patterns cannot be combined",
        ),
        ("pattern", None, ["bad\x00pattern"], None, "Invalid restore pattern"),
        ("pattern", None, ["bad\npattern"], None, "Invalid restore pattern"),
        ("pattern", None, ["bad\rpattern"], None, "Invalid restore pattern"),
    ],
)
def test_restore_rejects_invalid_selection_combinations(
    tmp_path: Path,
    mode: str,
    snapshot_paths: list[str] | None,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
    message: str,
) -> None:
    service = _service(tmp_path)

    with pytest.raises(ServiceError, match=message):
        _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode=mode,
            snapshot_paths=snapshot_paths,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )


def test_restore_default_target_contains_job_backup_snapshot_short_id_and_timestamp(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    with patch("src.services.restore.stream_command", AsyncMock(return_value=_mock_result())):
        preview = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")

    assert preview.request.restore_target.parts[-3:-1] == ("demo", "local")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_abcdef12_[a-f0-9]{8}",
        preview.request.restore_target.parts[-1],
    )
    assert preview.request.restore_target.is_relative_to(tmp_path / "restore")


def test_restore_default_targets_are_collision_resistant_within_same_second(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run_command = AsyncMock(return_value=_mock_result())

    with patch("src.services.restore.stream_command", run_command):
        first = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")
        second = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")

    assert first.request.restore_target != second.request.restore_target
    assert first.request.restore_target.parts[-3:-1] == second.request.restore_target.parts[-3:-1]


def test_restore_rejects_target_outside_restore_base(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ServiceError, match="Restore target must be under restore base"):
        _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="pattern",
            target="/tmp/not-restore",
        )


def test_restore_uses_restore_base_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DK_RESTORE_DIR", "/custom-restore")
    config_path = tmp_path / "config.toml"
    _write_restore_config(config_path)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    service = RestoreService(
        ConfigService(config_path),
        RestoreRegistry(tmp_path / "appdata.db"),
        RunManager(),
        lock_dir=lock_dir,
        log_base_dir=tmp_path / "logs",
    )

    with patch("src.services.restore.stream_command", AsyncMock(return_value=_mock_result())):
        preview = _preview(service, "demo", "local", "abcdef1234567890", mode="pattern")

    assert str(preview.request.restore_target).startswith("/custom-restore/demo/local/")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_abcdef12_[a-f0-9]{8}",
        preview.request.restore_target.name,
    )


def test_restore_rejects_target_outside_env_restore_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DK_RESTORE_DIR", "/custom-restore")
    config_path = tmp_path / "config.toml"
    _write_restore_config(config_path)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    service = RestoreService(
        ConfigService(config_path),
        RestoreRegistry(tmp_path / "appdata.db"),
        RunManager(),
        lock_dir=lock_dir,
        log_base_dir=tmp_path / "logs",
    )

    with pytest.raises(ServiceError, match="Restore target must be under restore base"):
        _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="pattern",
            target="/restore/not-the-env-base",
        )


def test_restore_rejects_relative_env_restore_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write_restore_config(config_path)
    monkeypatch.setenv("DK_RESTORE_DIR", "relative-restore")

    with pytest.raises(ServiceError, match="Restore base must be absolute"):
        RestoreService(
            ConfigService(config_path),
            RestoreRegistry(tmp_path / "appdata.db"),
            RunManager(),
        )


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("relative", "Restore target must be absolute"),
        ("/restore/a/../b", "Invalid restore target"),
        ("/restore/a/./b", "Invalid restore target"),
        ("/restore/a//b", "Invalid restore target"),
        ("/restore/a\\b", "Invalid restore target"),
        ("/restore/a\x00b", "Invalid restore target"),
    ],
)
def test_restore_rejects_unsafe_explicit_targets(tmp_path: Path, target: str, message: str) -> None:
    service = _service(tmp_path)

    with pytest.raises(ServiceError, match=message):
        _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="pattern",
            target=target,
        )


def test_restore_revalidates_target_after_parent_symlink_race(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_restore_config(config_path)
    config = ConfigService(config_path).load_active_config()
    backup = config.jobs["demo"].backup["local"]
    restore_base = tmp_path / "restore"
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (restore_base).mkdir()
    (restore_base / "race").symlink_to(escaped, target_is_directory=True)
    command = RestoreCommand(
        argv=[
            "restic",
            "--repo",
            backup.repository,
            "restore",
            "abcdef1234567890",
            "--target",
            str(restore_base / "race" / "target"),
            "--overwrite",
            "never",
        ],
        restore_target=restore_base / "race" / "target",
        mode=RestoreMode.PATTERN,
        snapshot_paths=(),
        include_patterns=(),
        exclude_patterns=(),
        overwrite=False,
        dry_run=False,
    )
    run_command = AsyncMock(return_value=_mock_result())

    async def scenario() -> None:
        with patch("src.services.restore.stream_command", run_command):
            result = await _run_restore_command(
                command=command,
                backup=backup,
                job_name="demo",
                log_level="INFO",
                log_base_dir=tmp_path / "logs",
                timeout=30,
                restore_base_dir=restore_base.resolve(strict=False),
            )

        assert result.ok is False
        assert "Restore target must not use symlinks" in str(result.error)
        run_command.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("snapshot_paths", "message"),
    [
        (("relative",), "Snapshot path must be absolute"),
        (("/data/../secret",), "Invalid snapshot path"),
        (("/data//secret",), "Invalid snapshot path"),
        (("/data\\secret",), "Invalid snapshot path"),
        (("/data/\x00secret",), "Invalid snapshot path"),
        (("/data/\nsecret",), "Invalid snapshot path"),
        (("/data/\rsecret",), "Invalid snapshot path"),
        (("/data/a*",), "glob characters"),
        (("/data/a?",), "glob characters"),
        (("/data/[abc]",), "glob characters"),
    ],
)
def test_restore_rejects_unsafe_snapshot_paths(
    tmp_path: Path, snapshot_paths: tuple[str, ...], message: str
) -> None:
    service = _service(tmp_path)

    with pytest.raises(ServiceError, match=message):
        _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="browser",
            snapshot_paths=snapshot_paths,
        )


def test_restore_start_tracks_status_transitions(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        with patch(
            "src.services.restore.stream_command", AsyncMock(return_value=_mock_result("restored"))
        ):
            record = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="browser",
                snapshot_paths=["/data"],
                dry_run=True,
            )
            assert record.status == RestoreStatus.QUEUED
            for _ in range(30):
                result = await service.get_restore(record.restore_id)
                if result.status == RestoreStatus.SUCCESS:
                    break
                await asyncio.sleep(0.02)

        assert result.status == RestoreStatus.SUCCESS
        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.output == "restored"

    asyncio.run(scenario())


def test_restore_cancelled_task_reaches_terminal_status(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        command_started = asyncio.Event()
        command_cancelled = asyncio.Event()

        async def slow_run_command(*_args: object, **_kwargs: object) -> StreamedCommandResult:
            command_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                command_cancelled.set()
                raise
            raise AssertionError("stream command was expected to be cancelled")

        with patch("src.services.restore.stream_command", side_effect=slow_run_command):
            record = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="browser",
                snapshot_paths=["/data"],
                dry_run=True,
            )
            await command_started.wait()
            await service.cancel_restore(record.restore_id)
            await service._run_manager.join(record.restore_id)
            result = await service.get_restore(record.restore_id)

        assert result.status == RestoreStatus.CANCELLED
        assert result.finished_at is not None
        assert result.error == "Restore task was cancelled"
        assert command_cancelled.is_set()
        await _wait_for_lifecycle_cleanup(service._run_manager)

    asyncio.run(scenario())


def test_restore_cancel_queued_task_reaches_terminal_status_without_starting_command(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        run_command = AsyncMock(return_value=_mock_result())
        with patch("src.services.restore.stream_command", run_command):
            record = await service.start_restore(
                "demo", "local", "abcdef1234567890", mode="pattern", dry_run=True
            )
            assert record.restore_id == (await service._run_manager.get(record.restore_id)).run_id
            await service.cancel_restore(record.restore_id)
            result = await _wait_for_restore(service, record.restore_id)

        assert result.status == RestoreStatus.CANCELLED
        assert result.finished_at is not None
        assert result.error == "Restore task was cancelled"
        assert record.restore_id not in service._pending_restores
        run_command.assert_not_awaited()

    asyncio.run(scenario())


def test_restore_sync_does_not_persist_active_status_without_pending_state(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        record = _restore_record("restore-without-pending", RestoreStatus.RUNNING)
        await service._registry.add(record)
        run_record = RunRecord(
            run_id=record.restore_id,
            origin=RunOrigin.MANUAL,
            run_kind=RunKind.RESTORE,
            job=record.request.job,
            task_type="restore",
            task_name=record.request.backup,
            status=RunStatus.QUEUED,
            dry_run=record.dry_run,
        )

        await service._sync_run_record(record, run_record)

        stored = await service._registry.get(record.restore_id)
        assert stored is not None
        assert stored.status == RestoreStatus.RUNNING

    asyncio.run(scenario())


def test_cancel_restore_rejects_non_restore_run_without_cancelling_it(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> RunStatus:
        started = asyncio.Event()
        release = asyncio.Event()

        async def running_backup(_: Callable[[], None]) -> bool:
            started.set()
            await release.wait()
            return True

        foreign = await service._run_manager.start(
            RunOrigin.MANUAL,
            "demo",
            "backup",
            "local",
            running_backup,
            run_id="foreign-backup-run",
        )
        await started.wait()

        with pytest.raises(ServiceError) as exc_info:
            await service.cancel_restore(foreign.run_id)

        still_running = await service._run_manager.get(foreign.run_id)
        status_before_release = still_running.status
        release.set()
        await service._run_manager.join(foreign.run_id)

        assert exc_info.value.code == "restore_not_found"
        return status_before_release

    status = asyncio.run(scenario())

    assert status == RunStatus.RUNNING


@pytest.mark.parametrize(
    ("command_result", "expected_status"),
    [
        (_mock_result(stdout="restored", returncode=0), RestoreStatus.SUCCESS),
        (_mock_result(stderr="restore failed", returncode=1), RestoreStatus.FAILED),
    ],
)
def test_restore_cancel_after_operational_phase_keeps_terminal_result(
    tmp_path: Path, command_result: StreamedCommandResult, expected_status: RestoreStatus
) -> None:
    service = _service(tmp_path)

    async def scenario() -> tuple[RestoreRecord, RestoreRecord, RunRecord]:
        final_update_started = asyncio.Event()
        allow_final_update = asyncio.Event()
        original_update = service._registry.update

        async def blocking_final_update(restore_id: str, **changes: object) -> None:
            if "output" in changes or "output_truncated" in changes:
                final_update_started.set()
                await allow_final_update.wait()
            await original_update(restore_id, **changes)

        with (
            patch("src.services.restore.stream_command", AsyncMock(return_value=command_result)),
            patch.object(service._registry, "update", side_effect=blocking_final_update),
        ):
            record = await service.start_restore(
                "demo", "local", "abcdef1234567890", mode="pattern", dry_run=True
            )
            await final_update_started.wait()

            run_during_post_processing = await service._run_manager.get(record.restore_id)
            cancel_result = await service.cancel_restore(record.restore_id)

            allow_final_update.set()
            await service._run_manager.join(record.restore_id)
            final_result = await service.get_restore(record.restore_id)

        return cancel_result, final_result, run_during_post_processing

    cancel_result, final_result, run_during_post_processing = asyncio.run(scenario())

    assert run_during_post_processing.status in {RunStatus.RUNNING, RunStatus(expected_status)}
    assert run_during_post_processing.cancellable is False
    assert cancel_result.status != RestoreStatus.CANCELLED
    assert final_result.status == expected_status
    assert final_result.finished_at is not None
    assert final_result.status != RestoreStatus.CANCELLED


def test_restore_shutdown_cancels_running_restore_and_releases_task_reference(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        command_started = asyncio.Event()
        command_cancelled = asyncio.Event()

        async def slow_run_command(*_args: object, **_kwargs: object) -> StreamedCommandResult:
            command_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                command_cancelled.set()
                raise
            raise AssertionError("stream command was expected to be cancelled")

        with patch("src.services.restore.stream_command", side_effect=slow_run_command):
            record = await service.start_restore(
                "demo", "local", "abcdef1234567890", mode="pattern", dry_run=True
            )
            await command_started.wait()
            await service._run_manager.shutdown(timeout=1)
            result = await service.get_restore(record.restore_id)

        assert command_cancelled.is_set()
        assert result.status == RestoreStatus.CANCELLED
        await _wait_for_lifecycle_cleanup(service._run_manager)
        assert not hasattr(service._registry, "_tasks")

    asyncio.run(scenario())


def test_restore_start_during_shutdown_does_not_persist_detail_record(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        await service._run_manager.shutdown(timeout=1)

        with pytest.raises(ServiceError) as exc_info:
            await service.start_restore(
                "demo", "local", "abcdef1234567890", mode="pattern", dry_run=True
            )

        assert exc_info.value.code == "runtime_stopping"
        assert _persisted_restore_run_ids(tmp_path / "appdata.db") == []

    asyncio.run(scenario())


def test_restore_preview_cancellation_propagates_without_persisting_run(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        command_started = asyncio.Event()
        command_cancelled = asyncio.Event()

        async def slow_run_command(*_args: object, **_kwargs: object) -> StreamedCommandResult:
            command_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                command_cancelled.set()
                raise
            raise AssertionError("stream command was expected to be cancelled")

        with patch("src.services.restore.stream_command", side_effect=slow_run_command):
            task = asyncio.create_task(
                service.preview_restore("demo", "local", "abcdef1234567890", mode="pattern")
            )
            await command_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert command_cancelled.is_set()
        assert await service._run_manager.list() == []
        assert _persisted_restore_run_ids(tmp_path / "appdata.db") == []

    asyncio.run(scenario())


def test_restore_preview_timeout_is_failed_without_persisting_run(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        with patch("src.services.restore.stream_command", side_effect=asyncio.TimeoutError):
            preview = await service.preview_restore(
                "demo", "local", "abcdef1234567890", mode="pattern"
            )

        assert preview.ok is False
        assert "timed out" in str(preview.error)
        assert await service._run_manager.list() == []
        assert _persisted_restore_run_ids(tmp_path / "appdata.db") == []

    asyncio.run(scenario())


def test_restore_timeout_is_failed_and_releases_task_reference(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        with patch("src.services.restore.stream_command", side_effect=asyncio.TimeoutError):
            record = await service.start_restore(
                "demo", "local", "abcdef1234567890", mode="pattern", dry_run=True
            )
            result = await _wait_for_restore(service, record.restore_id)
            await service._run_manager.join(record.restore_id)

        assert result.status == RestoreStatus.FAILED
        assert "timed out" in str(result.error)
        assert await service._run_manager.list() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ConfigServiceError("invalid_config", "broken config"), RestoreStatus.CONFIG_ERROR),
        (RuntimeError("boom"), RestoreStatus.UNEXPECTED_ERROR),
    ],
)
def test_restore_records_structured_operational_errors(
    tmp_path: Path, error: Exception, expected_status: RestoreStatus
) -> None:
    service = _service(tmp_path)

    async def scenario() -> None:
        with patch("src.services.restore._run_locked_restore", side_effect=error):
            record = await service.start_restore(
                "demo", "local", "abcdef1234567890", mode="pattern", dry_run=True
            )
            result = await _wait_for_restore(service, record.restore_id)
            run_record = await service._run_manager.get(record.restore_id)

        assert result.status == expected_status
        assert run_record.status == RunStatus(expected_status.value)
        assert result.finished_at is not None
        assert str(error) in str(result.error)

    asyncio.run(scenario())


def test_restore_default_does_not_overwrite(tmp_path: Path) -> None:
    service = _service(tmp_path)

    run_command = AsyncMock(return_value=_mock_result())
    with patch("src.services.restore.stream_command", run_command):
        _preview(
            service, "demo", "local", "abcdef1234567890", mode="browser", snapshot_paths=["/data"]
        )

    argv = run_command.call_args.args[0]
    assert argv[argv.index("--overwrite") + 1] == "never"


def test_restore_service_builds_template_ready_viewmodels(tmp_path: Path) -> None:
    service = _service(tmp_path)
    record = RestoreRecord(
        restore_id="restore-1",
        request=RestoreRequest(
            job="demo",
            backup="local",
            snapshot_id="abcdef1234567890",
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/data",),
            restore_target=Path("/restore/demo/local/abcdef12/20260601T000000Z"),
        ),
        status=RestoreStatus.QUEUED,
    )

    view = service.restore_view(record)

    assert view["status"] == "queued"
    assert view["status_label"] == "Queued"
    assert view["status_tone"] == "amber"
    assert view["is_active"] is True
    assert view["detail_url"] == f"/runs/{record.restore_id}"
    assert view["status_url"] == f"/restore/{record.restore_id}/status"


def test_restore_preview_view_builds_template_ready_viewmodel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = RestoreRequest(
        job="demo",
        backup="local",
        snapshot_id="abcdef1234567890",
        mode=RestoreMode.BROWSER,
        snapshot_paths=("/data",),
        restore_target=Path("/restore/demo/local/abcdef12/20260601T000000Z"),
        overwrite=True,
    )
    command = RestoreCommand(
        argv=["restic", "restore", "abcdef1234567890", "--dry-run"],
        restore_target=request.restore_target,
        mode=RestoreMode.BROWSER,
        snapshot_paths=request.snapshot_paths,
        include_patterns=(),
        exclude_patterns=(),
        overwrite=True,
        dry_run=True,
    )
    preview = RestorePreview(
        request=request,
        command=command,
        ok=False,
        output="preview output",
        output_truncated=True,
        error="preview failed",
    )

    view = service.preview_view(preview)

    assert view == {
        "ok": False,
        "job": "demo",
        "backup": "local",
        "snapshot_id": "abcdef1234567890",
        "mode": "browser",
        "snapshot_paths": ["/data"],
        "include_patterns": [],
        "exclude_patterns": [],
        "restore_target": "/restore/demo/local/abcdef12/20260601T000000Z",
        "overwrite": True,
        "command": "restic restore abcdef1234567890 --dry-run",
        "argv": ["restic", "restore", "abcdef1234567890", "--dry-run"],
        "dry_run": True,
        "output": "preview output",
        "output_truncated": True,
        "error": "preview failed",
    }


def test_restore_persistence_writes_run_and_restore_detail(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        first_registry = RestoreRegistry(db_path)
        await first_registry.add(_restore_record("restore-run-1", RestoreStatus.FAILED))

        second_registry = RestoreRegistry(db_path)
        record = await second_registry.get("restore-run-1")

        assert record is not None
        assert record.restore_id == "restore-run-1"
        assert record.request.job == "demo"
        assert record.request.backup == "local"
        assert record.request.mode == RestoreMode.BROWSER
        assert record.request.snapshot_paths == ("/data",)
        assert record.request.overwrite is True
        assert record.status == RestoreStatus.FAILED
        assert record.error == "boom"
        assert record.output == "restore output"

        with closing(sqlite3.connect(db_path)) as conn:
            run_row = conn.execute(
                """
                SELECT run_kind, job, task_type, task_name, status, dry_run
                FROM runs
                WHERE run_id = ?
                """,
                ("restore-run-1",),
            ).fetchone()
            restore_count = conn.execute(
                "SELECT COUNT(*) FROM run_restores WHERE run_id = ?",
                ("restore-run-1",),
            ).fetchone()[0]
            step_count = conn.execute(
                "SELECT COUNT(*) FROM run_steps WHERE run_id = ?",
                ("restore-run-1",),
            ).fetchone()[0]

        assert run_row == (
            RunKind.RESTORE.value,
            "demo",
            "restore",
            "local",
            RestoreStatus.FAILED.value,
            1,
        )
        assert restore_count == 1
        assert step_count == 0

    asyncio.run(scenario())


def test_restore_detail_update_persists_terminal_fields(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        await registry.add(_restore_record("restore-old", RestoreStatus.RUNNING))
        await registry.add(_restore_record("restore-new", RestoreStatus.SUCCESS))
        await registry.update(
            "restore-old",
            status=RestoreStatus.SUCCESS,
            finished_at=datetime(2026, 6, 7, 12, 1, tzinfo=timezone.utc),
            output="updated output",
        )

        updated = await registry.get("restore-old")

        assert updated is not None
        assert updated.status == RestoreStatus.SUCCESS
        assert updated.output == "updated output"

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                """
                SELECT runs.status, runs.finished_at, run_restores.output
                FROM runs
                JOIN run_restores ON run_restores.run_id = runs.run_id
                WHERE runs.run_id = ?
                """,
                ("restore-old",),
            ).fetchone()

        assert row == (
            RestoreStatus.SUCCESS.value,
            utc_rfc3339(datetime(2026, 6, 7, 12, 1, tzinfo=timezone.utc)),
            "updated output",
        )

    asyncio.run(scenario())


def test_restore_run_retention_is_disabled_without_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DK_APPDATA_RETENTION_COUNT", raising=False)
    monkeypatch.delenv("DK_APPDATA_RETENTION_DAYS", raising=False)
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        old = _restore_record("restore-old")
        old.finished_at = datetime.now(timezone.utc) - timedelta(days=5000)
        newer = _restore_record("restore-newer")
        newer.finished_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        newest = _restore_record("restore-newest")
        newest.finished_at = datetime.now(timezone.utc) + timedelta(minutes=2)

        await registry.add(old)
        await registry.add(newer)
        await registry.add(newest)

        assert await registry.get("restore-old") is not None
        assert await registry.get("restore-newer") is not None
        assert await registry.get("restore-newest") is not None

    asyncio.run(scenario())


def test_restore_run_retention_prunes_terminal_records_by_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "2")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "3600")
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        old = _restore_record("restore-old")
        old.finished_at = datetime.now(timezone.utc)
        newer = _restore_record("restore-newer")
        newer.finished_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        newest = _restore_record("restore-newest")
        newest.finished_at = datetime.now(timezone.utc) + timedelta(minutes=2)

        await registry.add(old)
        await registry.add(newer)
        await registry.add(newest)

        assert await registry.get("restore-newest") is not None
        assert await registry.get("restore-newer") is not None
        assert await registry.get("restore-old") is None

    asyncio.run(scenario())


def test_restore_run_retention_prunes_terminal_records_by_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "100")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        expired = _restore_record("restore-expired")
        expired.finished_at = datetime.now(timezone.utc) - timedelta(days=2)
        fresh = _restore_record("restore-fresh")
        fresh.finished_at = datetime.now(timezone.utc)

        await registry.add(expired)
        await registry.add(fresh)

        assert await registry.get("restore-fresh") is not None
        assert await registry.get("restore-expired") is None

    asyncio.run(scenario())


def test_restore_run_retention_preserves_active_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "1")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        running = _restore_record("restore-running", RestoreStatus.RUNNING)
        running.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        terminal = _restore_record("restore-terminal")
        terminal.finished_at = datetime.now(timezone.utc)

        await registry.add(running)
        await registry.add(terminal)

        assert await registry.get("restore-running") is not None
        assert await registry.get("restore-terminal") is not None

    asyncio.run(scenario())


def test_pending_restore_is_not_terminalized_during_start_window(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path)
        record = _restore_record("restore-starting", RestoreStatus.RUNNING)
        await service._registry.add(record)
        service._pending_restores[record.restore_id] = record

        result = await service.get_restore(record.restore_id)
        persisted = await service._registry.get(record.restore_id)

        assert result.status == RestoreStatus.RUNNING
        assert persisted is not None
        assert persisted.status == RestoreStatus.RUNNING
        assert persisted.error is None

    asyncio.run(scenario())


def test_restore_run_history_does_not_resurrect_old_active_records_as_live(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        service = _service(tmp_path)
        await service._registry.add(_restore_record("restore-running", RestoreStatus.RUNNING))

        record = await service.get_restore("restore-running")

        assert record is not None
        assert record.status == RestoreStatus.UNEXPECTED_ERROR
        assert record.finished_at is not None
        assert record.error == "Restore runtime is no longer active"
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                """
                SELECT status, finished_at, error
                FROM runs
                WHERE run_id = ?
                """,
                ("restore-running",),
            ).fetchone()
        assert row == (
            RestoreStatus.UNEXPECTED_ERROR.value,
            utc_rfc3339(record.finished_at),
            "Restore runtime is no longer active",
        )

    asyncio.run(scenario())


def test_restore_detail_discard_removes_detail_and_leaves_parent_run(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "appdata.db"

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        await registry.add(_restore_record("restore-1"))
        await registry.discard("restore-1")

        assert await registry.get("restore-1") is None
        with closing(sqlite3.connect(db_path)) as conn:
            run_count = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?",
                ("restore-1",),
            ).fetchone()[0]
            restore_count = conn.execute(
                "SELECT COUNT(*) FROM run_restores WHERE run_id = ?",
                ("restore-1",),
            ).fetchone()[0]
        assert run_count == 1
        assert restore_count == 0

    asyncio.run(scenario())


def test_restore_persistence_uses_to_thread_for_sqlite_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "appdata.db"
    called: list[str] = []

    async def recording_to_thread(
        func: Callable[..., Any], /, *args: object, **kwargs: object
    ) -> Any:
        called.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

    async def scenario() -> None:
        registry = RestoreRegistry(db_path)
        await registry.add(_restore_record("restore-1"))
        await registry.get("restore-1")
        await registry.update("restore-1", status=RestoreStatus.SUCCESS)
        await registry.discard("restore-1")

    asyncio.run(scenario())

    assert called == [
        "_upsert_sync",
        "_get_sync",
        "_get_sync",
        "_upsert_sync",
        "_discard_sync",
    ]


def test_get_restore_view_returns_registered_template_ready_viewmodel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def immediate_to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    service = _service(tmp_path)
    service._run_manager = RunManager()
    record = RestoreRecord(
        restore_id="restore-1",
        request=RestoreRequest(
            job="demo",
            backup="local",
            snapshot_id="abcdef1234567890",
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/data",),
            restore_target=Path("/restore/demo/local/abcdef12/20260601T000000Z"),
        ),
        status=RestoreStatus.RUNNING,
    )

    async def scenario() -> dict[str, object]:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def operation(_mark_not_cancellable: object) -> bool:
            started.set()
            await finish.wait()
            return True

        await service._run_manager.start(
            RunOrigin.MANUAL,
            record.request.job,
            "restore",
            record.request.backup,
            operation,
            run_kind=RunKind.RESTORE,
            run_id=record.restore_id,
        )
        await started.wait()
        await service._registry.add(record)
        view = await service.get_restore_view(record.restore_id)
        finish.set()
        await service._run_manager.join(record.restore_id)
        return view

    view = asyncio.run(scenario())

    assert view["run_id"] == record.restore_id
    assert view["status"] == "running"
    assert view["status_label"] == "Running"
    assert view["status_tone"] == "blue"
    assert view["is_active"] is True
    assert view["is_cancellable"] is True
    assert view["snapshot_paths"] == ["/data"]


def test_get_restore_view_uses_live_run_cancellable_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def immediate_to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    service = _service(tmp_path)
    service._run_manager = RunManager()
    record = RestoreRecord(
        restore_id="restore-1",
        request=RestoreRequest(
            job="demo",
            backup="local",
            snapshot_id="abcdef1234567890",
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/data",),
            restore_target=Path("/restore/demo/local/abcdef12/20260601T000000Z"),
        ),
        status=RestoreStatus.RUNNING,
    )

    async def scenario() -> dict[str, object]:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
            mark_not_cancellable()
            started.set()
            await finish.wait()
            return True

        await service._run_manager.start(
            RunOrigin.MANUAL,
            record.request.job,
            "restore",
            record.request.backup,
            operation,
            run_kind=RunKind.RESTORE,
            run_id=record.restore_id,
        )
        await started.wait()
        await service._registry.add(record)
        view = await service.get_restore_view(record.restore_id)
        finish.set()
        await service._run_manager.join(record.restore_id)
        return view

    view = asyncio.run(scenario())

    assert view["status"] == "running"
    assert view["is_active"] is True
    assert view["is_cancellable"] is False


def test_restore_lock_collision_on_same_repository(tmp_path: Path) -> None:
    service = _service(tmp_path, repository="/repo/shared")

    async def scenario() -> None:
        release = asyncio.Event()
        command_started = asyncio.Event()

        async def slow_run_command(*_args: object, **_kwargs: object) -> StreamedCommandResult:
            command_started.set()
            await release.wait()
            return _mock_result("done")

        with patch("src.services.restore.stream_command", side_effect=slow_run_command):
            first = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="browser",
                snapshot_paths=["/data"],
                dry_run=True,
            )
            for _ in range(30):
                first_seen = await service.get_restore(first.restore_id)
                if first_seen.status == RestoreStatus.RUNNING:
                    break
                await asyncio.sleep(0.01)
            await command_started.wait()
            second = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="browser",
                snapshot_paths=["/data"],
                dry_run=True,
            )
            for _ in range(40):
                second_seen = await service.get_restore(second.restore_id)
                if second_seen.status == RestoreStatus.LOCK_ERROR:
                    break
                await asyncio.sleep(0.01)
            release.set()

        assert second_seen.status == RestoreStatus.LOCK_ERROR
        assert "already running" in str(second_seen.error)
        reloaded = await RestoreRegistry(tmp_path / "appdata.db").get(second.restore_id)
        assert reloaded is not None
        assert reloaded.status == RestoreStatus.LOCK_ERROR
        assert second.restore_id not in service._pending_restores

    asyncio.run(scenario())


def test_restore_lock_collision_on_same_target(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_multi_backup_config(config_path)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    restore_base = tmp_path / "restore"
    shared_target = str(restore_base / "shared-target")
    service = RestoreService(
        ConfigService(config_path),
        RestoreRegistry(tmp_path / "appdata.db"),
        RunManager(),
        restore_base_dir=restore_base,
        lock_dir=lock_dir,
        log_base_dir=tmp_path / "logs",
    )

    async def scenario() -> None:
        release = asyncio.Event()
        command_started = asyncio.Event()

        async def slow_run_command(*_args: object, **_kwargs: object) -> StreamedCommandResult:
            command_started.set()
            await release.wait()
            return _mock_result("done")

        with patch("src.services.restore.stream_command", side_effect=slow_run_command):
            first = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="browser",
                snapshot_paths=["/data"],
                target=shared_target,
                dry_run=True,
            )
            for _ in range(30):
                first_seen = await service.get_restore(first.restore_id)
                if first_seen.status == RestoreStatus.RUNNING:
                    break
                await asyncio.sleep(0.01)
            await command_started.wait()
            second = await service.start_restore(
                "demo",
                "other",
                "abcdef1234567890",
                mode="browser",
                snapshot_paths=["/data"],
                target=shared_target,
                dry_run=True,
            )
            for _ in range(40):
                second_seen = await service.get_restore(second.restore_id)
                if second_seen.status == RestoreStatus.LOCK_ERROR:
                    break
                await asyncio.sleep(0.01)
            release.set()

        assert second_seen.status == RestoreStatus.LOCK_ERROR
        assert "already running" in str(second_seen.error)

    asyncio.run(scenario())


def _override_backup_config(service: RestoreService, *, repository: str) -> Any:
    """Build a donor-style override config: same credentials, different repository path."""
    config = service._config_service.load_active_config()
    donor_backup = config.jobs["demo"].backup["local"]
    return donor_backup.model_copy(update={"repository": repository})


def test_restore_preview_uses_override_repository_instead_of_job_backup_config(
    tmp_path: Path,
) -> None:
    """preview_restore(resolved_backup=...) must build the command against the override path."""
    service = _service(tmp_path, repository="/repo/secret-value")
    override = _override_backup_config(service, repository="/repo/donor-target")
    run_command = AsyncMock(return_value=_mock_result())

    with patch("src.services.restore.stream_command", run_command):
        preview = _preview(
            service,
            "demo",
            "local",
            "abcdef1234567890",
            mode="pattern",
            resolved_backup=override,
        )

    assert preview.ok is True
    assert preview.command.argv[2] == "/repo/donor-target"
    assert "/repo/secret-value" not in preview.command.argv
    # Credentials still come from the override (donor), not from a relookup.
    assert run_command.call_args.kwargs["env"]["RESTIC_PASSWORD"] == "secret-value"


def test_restore_start_executes_against_override_repository_not_config_lookup(
    tmp_path: Path,
) -> None:
    """Regression test for the _execute() re-derivation bug.

    start_restore(resolved_backup=...) must thread the override all the way
    into the actual asynchronous restic invocation, not just the dry-run
    preview path. Before the fix, ``_execute()`` re-derived
    ``config.jobs[request.job].backup[request.backup]`` and silently
    discarded the override, restoring from the donor's own native
    repository instead of the override's repository.
    """
    service = _service(tmp_path, repository="/repo/secret-value")
    override = _override_backup_config(service, repository="/repo/donor-target")
    run_command = AsyncMock(return_value=_mock_result("restored"))

    async def scenario() -> RestoreRecord:
        with patch("src.services.restore.stream_command", run_command):
            record = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="pattern",
                dry_run=True,
                resolved_backup=override,
            )
            return await _wait_for_restore(service, record.restore_id)

    result = asyncio.run(scenario())

    assert result.status == RestoreStatus.SUCCESS
    invoked_argv = run_command.call_args.args[0]
    assert invoked_argv[2] == "/repo/donor-target"
    assert "/repo/secret-value" not in invoked_argv
    assert run_command.call_args.kwargs["env"]["RESTIC_PASSWORD"] == "secret-value"


def test_restore_start_without_override_uses_config_lookup_as_before(tmp_path: Path) -> None:
    """Without resolved_backup (the default), behavior is unchanged: config wins."""
    service = _service(tmp_path, repository="/repo/secret-value")
    run_command = AsyncMock(return_value=_mock_result("restored"))

    async def scenario() -> RestoreRecord:
        with patch("src.services.restore.stream_command", run_command):
            record = await service.start_restore(
                "demo",
                "local",
                "abcdef1234567890",
                mode="pattern",
                dry_run=True,
            )
            return await _wait_for_restore(service, record.restore_id)

    result = asyncio.run(scenario())

    assert result.status == RestoreStatus.SUCCESS
    invoked_argv = run_command.call_args.args[0]
    assert invoked_argv[2] == "/repo/secret-value"
