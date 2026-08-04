"""Tests for scheduler status helpers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.core.locking import SchedulerLock
from src.scheduler.status import (
    SchedulerStatusError,
    SchedulerStatusReader,
    SchedulerStatusWriter,
)


def test_writer_and_reader_return_stopped_without_lock(tmp_path: Path) -> None:
    writer = SchedulerStatusWriter(tmp_path)
    now = datetime.now(UTC)
    writer.write(
        state="running",
        owner="gui",
        started_at=now,
        last_tick_at=now,
        config_path=tmp_path / "config.toml",
        config_mtime=123.0,
    )

    status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "stopped"
    assert status.owner == "gui"


def test_reader_reports_running_when_lock_is_held(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    SchedulerStatusWriter(tmp_path).write(
        state="running",
        owner="scheduler-cli",
        started_at=now,
        last_tick_at=now,
        config_path=tmp_path / "config.toml",
        config_mtime=123.0,
    )

    with SchedulerLock(tmp_path).acquire():
        status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "running"
    assert status.owner == "scheduler-cli"


def test_reader_preserves_fresh_config_error_without_lock(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    SchedulerStatusWriter(tmp_path).write(
        state="config_error",
        owner="gui",
        started_at=now,
        last_tick_at=now,
        config_path=tmp_path / "config.toml",
        config_mtime=None,
        error=SchedulerStatusError(code="validation_error", message="bad config"),
    )

    status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "config_error"
    assert status.error is not None
    assert status.error.code == "validation_error"


def test_reader_preserves_fresh_scheduler_lock_error_without_lock(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    SchedulerStatusWriter(tmp_path).write(
        state="scheduler_lock_error",
        owner="gui",
        started_at=now,
        last_tick_at=now,
        config_path=tmp_path / "config.toml",
        config_mtime=None,
        error=SchedulerStatusError(code="scheduler_lock_error", message="held"),
    )

    status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "scheduler_lock_error"
    assert status.error is not None
    assert status.error.code == "scheduler_lock_error"


def test_reader_ignores_stale_running_status_without_lock(tmp_path: Path) -> None:
    stale = datetime.now(UTC) - timedelta(seconds=500)
    SchedulerStatusWriter(tmp_path).write(
        state="running",
        owner="gui",
        started_at=stale,
        last_tick_at=stale,
        config_path=tmp_path / "config.toml",
        config_mtime=None,
    )

    status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "stopped"


def test_reader_returns_unknown_for_invalid_json(tmp_path: Path) -> None:
    status_path = SchedulerStatusWriter(tmp_path).status_path
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text("{not-json", encoding="utf-8")

    status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "unknown"
    assert status.error is not None
    assert status.error.code == "status_error"


def test_reader_strict_raises_for_invalid_json(tmp_path: Path) -> None:
    status_path = SchedulerStatusWriter(tmp_path).status_path
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text("{not-json", encoding="utf-8")

    try:
        SchedulerStatusReader(tmp_path).read(strict=True)
    except ValueError:
        pass
    else:
        raise AssertionError("strict status read should raise")


def test_parallel_writers_use_independent_temporary_files(tmp_path: Path) -> None:
    writer = SchedulerStatusWriter(tmp_path)
    now = datetime.now(UTC)

    def write_status(_: int) -> None:
        writer.write(
            state="running",
            owner="gui",
            started_at=now,
            last_tick_at=now,
            config_path=tmp_path / "config.toml",
            config_mtime=123.0,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(write_status, range(200)))

    status = SchedulerStatusReader(tmp_path).read_file()
    assert status is not None
    assert status.state == "running"
    assert list(tmp_path.glob("*.tmp")) == []
