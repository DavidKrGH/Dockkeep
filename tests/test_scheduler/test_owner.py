"""Tests for SchedulerOwnerManager runtime ownership behavior."""

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.core.locking import SchedulerLock
from src.models.config import RawAppConfig
from src.models.resolve import resolve_config
from src.models.resolved_config import ResolvedAppConfig
from src.scheduler.cron import SHUTDOWN_TIMEOUT, SchedulerStartOutcome, SchedulerStartState
from src.scheduler.owner import SchedulerOwnerManager
from src.scheduler.status import SchedulerStatusReader, SchedulerStatusWriter


def _wait_for_status(
    reader: SchedulerStatusReader,
    expected_state: str,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = reader.read()
        if status.state == expected_state:
            return
        time.sleep(0.01)
    status = reader.read()
    raise AssertionError(f"expected {expected_state!r}, got {status.state!r}")


def _wait_for_raw_status(
    reader: SchedulerStatusReader,
    expected_state: str,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = reader.read_file()
        if status is not None and status.state == expected_state:
            return
        time.sleep(0.01)
    status = reader.read_file()
    state = status.state if status is not None else None
    raise AssertionError(f"expected raw {expected_state!r}, got {state!r}")


def _valid_config() -> ResolvedAppConfig:
    return resolve_config(
        RawAppConfig.model_validate(
            {
                "global": {"log_level": "info", "backup": {"password": "secret"}},
                "jobs": {
                    "test-job": {
                        "backup": {
                            "local": {
                                "repository": "/backups/test",
                                "sources": ["/data"],
                                "schedule": "0 2 * * *",
                            }
                        },
                    }
                },
            }
        )
    )


def test_owner_passes_appdata_paths_to_scheduler(tmp_path: Path) -> None:
    config = _valid_config()
    appdata_dir = tmp_path / "appdata"
    manager = SchedulerOwnerManager(
        tmp_path / "config.toml",
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        appdata_dir=appdata_dir,
        check_interval=60,
    )

    with (
        patch("src.scheduler.owner.CronScheduler") as scheduler_cls,
        patch("src.scheduler.owner.load_config", return_value=config),
    ):

        def fake_start() -> SchedulerStartOutcome:
            scheduler_cls.call_args.kwargs["start_result_callback"]("running", None)
            return SchedulerStartOutcome(SchedulerStartState.RUNNING)

        scheduler_cls.return_value.start.side_effect = fake_start

        assert manager._start_scheduler(config) is True

    scheduler_cls.assert_called_once()
    assert scheduler_cls.call_args.kwargs["socket_path"] == appdata_dir / "run-control.sock"
    assert scheduler_cls.call_args.kwargs["history_db_path"] == appdata_dir / "appdata.db"
    manager.stop()


def test_owner_writes_config_error_without_starting_scheduler(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"
    manager = SchedulerOwnerManager(
        config_path,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        check_interval=60,
    )

    manager.start_background()
    _wait_for_status(SchedulerStatusReader(tmp_path), "config_error")
    manager.stop()

    status = SchedulerStatusReader(tmp_path).read()
    assert status.state == "config_error"
    assert status.owner == "gui"
    assert status.error is not None
    assert status.error.code == "file_not_found"
    assert manager._scheduler_state() == (None, None)


def test_owner_reports_scheduler_lock_error_without_active_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DK_APPDATA_DIR", str(tmp_path / "appdata"))
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\npassword = "secret"\n[jobs]\n', encoding="utf-8")
    manager = SchedulerOwnerManager(
        config_path,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        check_interval=60,
        start_timeout=1,
    )

    with (
        SchedulerLock(tmp_path).acquire(),
        patch("src.scheduler.owner.load_config", return_value=_valid_config()),
    ):
        manager.start_background()
        _wait_for_status(SchedulerStatusReader(tmp_path), "scheduler_lock_error")
        manager.stop()

    status = SchedulerStatusReader(tmp_path).read()
    assert status.state == "scheduler_lock_error"
    assert status.owner == "gui"
    assert status.error is not None
    assert status.error.code == "scheduler_lock_error"
    assert manager._scheduler_state() == (None, None)


def test_competing_owner_keeps_active_scheduler_status(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    now = datetime.now(UTC)
    writer = SchedulerStatusWriter(tmp_path)
    writer.write(
        state="running",
        owner="gui",
        started_at=now,
        last_tick_at=now,
        config_path=config_path,
        config_mtime=123.0,
    )
    manager = SchedulerOwnerManager(config_path, lock_dir=tmp_path, status_writer=writer)

    with SchedulerLock(tmp_path).acquire():
        manager._write_scheduler_lock_error("held by active scheduler")
        status = SchedulerStatusReader(tmp_path).read()

    assert status.state == "running"
    assert status.error is None


def test_owner_reports_unexpected_error_when_scheduler_setup_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "missing-after-load.toml"
    manager = SchedulerOwnerManager(
        config_path,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        check_interval=60,
        start_timeout=1,
    )

    with patch("src.scheduler.owner.load_config", return_value=_valid_config()):
        manager.start_background()
        _wait_for_raw_status(SchedulerStatusReader(tmp_path), "unknown")
        manager.stop()

    status = SchedulerStatusReader(tmp_path).read_file()
    assert status is not None
    assert status.state == "unknown"
    assert status.error is not None
    assert status.error.code == "unexpected_error"
    assert manager._scheduler_state() == (None, None)


def test_owner_tracks_scheduler_after_scheduler_reports_running(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\npassword = "secret"\n[jobs]\n', encoding="utf-8")
    config = _valid_config()
    manager = SchedulerOwnerManager(
        config_path,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        check_interval=60,
        start_timeout=1,
    )
    stopped = threading.Event()
    scheduler_instance = Mock()
    scheduler_instance.stop.side_effect = stopped.set

    def build_scheduler(*_args: object, **kwargs: object) -> Mock:
        callback = kwargs["start_result_callback"]

        def start() -> SchedulerStartOutcome:
            callback("running", None)
            stopped.wait(timeout=2)
            return SchedulerStartOutcome(SchedulerStartState.RUNNING)

        scheduler_instance.start.side_effect = start
        return scheduler_instance

    with patch("src.scheduler.owner.CronScheduler", side_effect=build_scheduler):
        assert manager._start_scheduler(config) is True
        scheduler, thread = manager._scheduler_state()
        assert scheduler is scheduler_instance
        assert thread is not None
        assert thread.is_alive()
        manager._stop_scheduler_once()
        thread.join(timeout=2)


def test_owner_scheduler_join_timeout_defaults_to_scheduler_shutdown_timeout(
    tmp_path: Path,
) -> None:
    manager = SchedulerOwnerManager(tmp_path / "config.toml", lock_dir=tmp_path)

    assert manager.scheduler_join_timeout == SHUTDOWN_TIMEOUT + 10


def test_owner_stop_uses_injected_scheduler_join_timeout(tmp_path: Path) -> None:
    manager = SchedulerOwnerManager(
        tmp_path / "config.toml",
        lock_dir=tmp_path,
        scheduler_join_timeout=1,
    )
    scheduler = Mock()
    scheduler_thread = Mock()
    scheduler_thread.is_alive.return_value = False
    manager._scheduler = scheduler
    manager._scheduler_thread = scheduler_thread

    manager.stop()

    scheduler.stop.assert_called_once_with()
    scheduler_thread.join.assert_called_once_with(timeout=1)


def test_owner_stop_rechecks_scheduler_started_during_manager_join(tmp_path: Path) -> None:
    manager = SchedulerOwnerManager(
        tmp_path / "config.toml",
        lock_dir=tmp_path,
        scheduler_join_timeout=1,
    )
    scheduler = Mock()
    scheduler_thread = Mock()
    scheduler_thread.is_alive.return_value = False
    manager_thread = Mock()

    def publish_scheduler(*, timeout: int) -> None:
        assert timeout == 90
        manager._scheduler = scheduler
        manager._scheduler_thread = scheduler_thread

    manager_thread.join.side_effect = publish_scheduler
    manager._thread = manager_thread

    manager.stop()

    scheduler.stop.assert_called_once_with()
    scheduler_thread.join.assert_called_once_with(timeout=1)


def test_start_scheduler_stops_scheduler_when_owner_is_already_stopping(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    manager = SchedulerOwnerManager(
        config_path,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        start_timeout=1,
    )
    manager._stop_event.set()
    scheduler_instance = Mock()
    scheduler_instance.start.side_effect = lambda: None

    def build_scheduler(*args: object, **kwargs: object) -> Mock:
        callback = kwargs["start_result_callback"]

        def start() -> None:
            callback("running", None)

        scheduler_instance.start.side_effect = start
        return scheduler_instance

    with patch("src.scheduler.owner.CronScheduler", side_effect=build_scheduler):
        assert manager._start_scheduler(_valid_config()) is False

    scheduler_instance.stop.assert_called_once_with()
    assert manager._scheduler_state() == (None, None)


def test_owner_stop_warns_when_scheduler_thread_stays_alive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = SchedulerOwnerManager(
        tmp_path / "config.toml",
        lock_dir=tmp_path,
        scheduler_join_timeout=1,
    )
    manager._scheduler = Mock()
    scheduler_thread = Mock()
    scheduler_thread.is_alive.return_value = True
    manager._scheduler_thread = scheduler_thread

    with caplog.at_level(logging.WARNING, logger="src.scheduler.owner"):
        manager.stop()

    assert "scheduler lock may still be active" in caplog.text


def test_owner_stop_waits_until_scheduler_thread_exits(tmp_path: Path) -> None:
    done = threading.Event()

    def run() -> None:
        time.sleep(0.05)
        done.set()

    manager = SchedulerOwnerManager(
        tmp_path / "config.toml",
        lock_dir=tmp_path,
        scheduler_join_timeout=1,
    )
    manager._scheduler = Mock()
    scheduler_thread = threading.Thread(target=run)
    manager._scheduler_thread = scheduler_thread
    scheduler_thread.start()

    manager.stop()

    assert done.is_set()
    assert not scheduler_thread.is_alive()


def test_owner_loop_survives_status_writer_failure(tmp_path: Path) -> None:
    class FailingStatusWriter(SchedulerStatusWriter):
        def write(self, **kwargs: object) -> object:
            raise OSError("status path unavailable")

    manager = SchedulerOwnerManager(
        tmp_path / "missing.toml",
        lock_dir=tmp_path,
        check_interval=0.01,
        status_writer=FailingStatusWriter(tmp_path),
    )

    manager.start_background()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if manager._thread is not None and manager._thread.is_alive():
            break
        time.sleep(0.01)

    assert manager._thread is not None
    assert manager._thread.is_alive()
    manager.stop()
