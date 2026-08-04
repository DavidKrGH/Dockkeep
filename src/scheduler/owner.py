"""Scheduler owner manager for GUI runtimes."""

from __future__ import annotations

import logging
import queue
import threading
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ..core.job_runner import BackupRunStatsContext
from ..core.locking import lock_dir as default_lock_dir
from ..models.resolved_config import ResolvedAppConfig
from ..utils.logging import log_base_dir as default_log_base_dir
from ..utils.validation import load_config
from .cron import SHUTDOWN_TIMEOUT, CronScheduler
from .status import SchedulerStatusError, SchedulerStatusReader, SchedulerStatusWriter

logger = logging.getLogger(__name__)

MANAGER_CHECK_INTERVAL = 60
SCHEDULER_START_TIMEOUT = 10


@dataclass(frozen=True)
class SchedulerStartResult:
    """Synchronous result reported by CronScheduler.start()."""

    state: str
    message: str | None = None


class SchedulerOwnerManager:
    """Owns the CronScheduler inside the GUI process.

    The manager handles initial config recovery. Once a scheduler has started
    successfully, hot reload remains owned by ``CronScheduler``.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        lock_dir: Path | None = None,
        log_base_dir: Path | None = None,
        check_interval: int = MANAGER_CHECK_INTERVAL,
        start_timeout: int = SCHEDULER_START_TIMEOUT,
        scheduler_join_timeout: int | None = None,
        status_writer: SchedulerStatusWriter | None = None,
        appdata_dir: Path | None = None,
        on_backup_success: (
            Callable[[str, str, BackupRunStatsContext], Awaitable[object]] | None
        ) = None,
    ) -> None:
        """Initialize the manager."""
        self.config_path = config_path
        self.lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self.log_base_dir = log_base_dir if log_base_dir is not None else default_log_base_dir()
        self.check_interval = check_interval
        self.start_timeout = start_timeout
        self.scheduler_join_timeout = (
            scheduler_join_timeout if scheduler_join_timeout is not None else SHUTDOWN_TIMEOUT + 10
        )
        self.status_writer = status_writer or SchedulerStatusWriter(lock_dir)
        self.appdata_dir = appdata_dir
        self._on_backup_success = on_backup_success
        self.started_at = datetime.now(UTC)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._scheduler: CronScheduler | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_config_mtime: float | None = None

    def start_background(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run, daemon=True, name="scheduler-owner")
        self._thread.start()

    def run(self) -> None:
        _lock_error_active = False
        while not self._stop_event.is_set():
            try:
                scheduler, scheduler_thread = self._scheduler_state()
                if scheduler is not None:
                    thread_alive = scheduler_thread is not None and scheduler_thread.is_alive()
                    if thread_alive:
                        self._stop_event.wait(timeout=self.check_interval)
                        continue
                    logger.warning("Scheduler thread has stopped unexpectedly — attempting restart")
                    self._clear_scheduler_state()
                    _lock_error_active = False

                result = self._load_config_for_start()
                if isinstance(result, ResolvedAppConfig):
                    if self._start_scheduler(result):
                        _lock_error_active = False
                        continue
                    # _start_scheduler returns False for both lock_error and unexpected_error;
                    # status file already updated. Suppress repeated log spam for lock conflicts.
                    if not _lock_error_active:
                        _lock_error_active = True
                    else:
                        logger.debug(
                            "Scheduler lock still held by another process — "
                            "waiting %ds before retry",
                            self.check_interval,
                        )
                else:
                    _lock_error_active = False
                    self._write_config_error(result)
            except Exception:  # noqa: BLE001
                _lock_error_active = False
                logger.exception("Scheduler owner loop failed; will retry")

            self._stop_event.wait(timeout=self.check_interval)

    def stop(self) -> None:
        """Stop manager and owned scheduler cleanly."""
        self._stop_event.set()
        stopped_scheduler = self._stop_scheduler_once()
        if self._thread is not None:
            self._thread.join(timeout=90)
        stopped_scheduler = self._stop_scheduler_once(except_scheduler=stopped_scheduler)
        _, scheduler_thread = self._scheduler_state()
        if scheduler_thread is not None:
            scheduler_thread.join(timeout=self.scheduler_join_timeout)
            if scheduler_thread.is_alive():
                logger.warning(
                    "Scheduler thread did not stop within %s seconds; "
                    "scheduler lock may still be active",
                    self.scheduler_join_timeout,
                )

    def _load_config_for_start(self) -> ResolvedAppConfig | SchedulerStatusError:
        try:
            config = load_config(self.config_path)
        except FileNotFoundError as exc:
            return SchedulerStatusError(code="file_not_found", message=str(exc))
        except tomllib.TOMLDecodeError as exc:
            return SchedulerStatusError(code="toml_error", message=str(exc))
        except ValidationError as exc:
            return SchedulerStatusError(code="validation_error", message=str(exc))
        except ValueError as exc:
            return SchedulerStatusError(code="value_error", message=str(exc))
        except OSError as exc:
            return SchedulerStatusError(code="read_error", message=str(exc))

        try:
            self._last_config_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._last_config_mtime = None
        return config

    def _start_scheduler(self, config: ResolvedAppConfig) -> bool:
        result_queue: queue.Queue[SchedulerStartResult] = queue.Queue(maxsize=1)

        def _on_start_result(state: str, message: str | None) -> None:
            result_queue.put(SchedulerStartResult(state=state, message=message))

        try:
            scheduler = CronScheduler(
                config,
                lock_dir=self.lock_dir,
                log_base_dir=self.log_base_dir,
                config_path=self.config_path,
                owner="gui",
                status_writer=self.status_writer,
                start_result_callback=_on_start_result,
                socket_path=(
                    self.appdata_dir / "run-control.sock" if self.appdata_dir is not None else None
                ),
                history_db_path=(
                    self.appdata_dir / "appdata.db" if self.appdata_dir is not None else None
                ),
                on_backup_success=self._on_backup_success,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduler setup failed during startup")
            self._write_unexpected_error(str(exc))
            return False

        def _run_scheduler() -> None:
            try:
                outcome = scheduler.start()
                if not outcome.started and result_queue.empty():
                    result_queue.put(
                        SchedulerStartResult(state=outcome.state, message=outcome.message)
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Scheduler thread crashed during startup")
                if result_queue.empty():
                    result_queue.put(
                        SchedulerStartResult(state="unexpected_error", message=str(exc))
                    )

        thread = threading.Thread(target=_run_scheduler, daemon=True, name="cron-scheduler")
        thread.start()

        try:
            result = result_queue.get(timeout=self.start_timeout)
        except queue.Empty:
            result = SchedulerStartResult(
                state="unexpected_error",
                message="Timed out waiting for scheduler start result",
            )

        if result.state == "running":
            with self._state_lock:
                if self._stop_event.is_set():
                    scheduler.stop()
                    should_publish = False
                else:
                    self._scheduler = scheduler
                    self._scheduler_thread = thread
                    should_publish = True
            if not should_publish:
                thread.join(timeout=5)
                return False
            return True

        if result.state == "scheduler_lock_error":
            self._write_scheduler_lock_error(result.message or "Scheduler lock is already held")
        else:
            self._write_unexpected_error(result.message or "Scheduler start failed")
        scheduler.stop()
        thread.join(timeout=5)
        return False

    def _write_config_error(self, error: SchedulerStatusError) -> None:
        now = datetime.now(UTC)
        try:
            self.status_writer.write(
                state="config_error",
                owner="gui",
                started_at=self.started_at,
                last_tick_at=now,
                config_path=self.config_path,
                config_mtime=self._safe_config_mtime(),
                error=error,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not write scheduler owner config-error status")

    def _write_scheduler_lock_error(self, message: str) -> None:
        try:
            status = SchedulerStatusReader(self.lock_dir).read()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read scheduler status before writing lock error")
            status = None
        if status is not None and status.state == "running":
            logger.warning(
                "Scheduler lock held by an active scheduler; keeping its status unchanged"
            )
            return
        now = datetime.now(UTC)
        try:
            self.status_writer.write(
                state="scheduler_lock_error",
                owner="gui",
                started_at=self.started_at,
                last_tick_at=now,
                config_path=self.config_path,
                config_mtime=self._safe_config_mtime(),
                error=SchedulerStatusError(code="scheduler_lock_error", message=message),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not write scheduler owner lock-error status")

    def _write_unexpected_error(self, message: str) -> None:
        now = datetime.now(UTC)
        try:
            self.status_writer.write(
                state="unknown",
                owner="gui",
                started_at=self.started_at,
                last_tick_at=now,
                config_path=self.config_path,
                config_mtime=self._safe_config_mtime(),
                error=SchedulerStatusError(code="unexpected_error", message=message),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not write scheduler owner unexpected-error status")

    def _safe_config_mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return self._last_config_mtime

    def _scheduler_state(self) -> tuple[CronScheduler | None, threading.Thread | None]:
        with self._state_lock:
            return self._scheduler, self._scheduler_thread

    def _clear_scheduler_state(self) -> None:
        with self._state_lock:
            self._scheduler = None
            self._scheduler_thread = None

    def _stop_scheduler_once(self, except_scheduler: object | None = None) -> object | None:
        with self._state_lock:
            scheduler = self._scheduler
        if scheduler is not None and scheduler is not except_scheduler:
            scheduler.stop()
            return scheduler
        return except_scheduler
