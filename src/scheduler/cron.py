"""Cron scheduler for scheduled backup, workflow, rclone, and report runs."""

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from croniter import CroniterBadDateError, croniter

from ..core.job_runner import BackupRunStatsContext, JobRunner
from ..core.locking import (
    JobAlreadyRunningError,
    SchedulerAlreadyRunningError,
    SchedulerLock,
)
from ..core.locking import (
    lock_dir as default_lock_dir,
)
from ..models.resolved_config import (
    ResolvedAppConfig,
)
from ..notifications.context import NotificationContext, build_notification_context
from ..notifications.dispatcher import NotificationDispatcher
from ..notifications.events import (
    NotificationReportEvent,
    NotificationReportRunSummary,
    NotificationReportStatus,
)
from ..services.run_control import RunControlServer, default_socket_path
from ..services.run_history import RunHistoryService, default_appdata_db_path
from ..services.run_manager import (
    MarkNotCancellable,
    RunManager,
    RunOperation,
    RunOrigin,
    RunRecord,
    RunStatus,
    TerminalHook,
)
from ..services.terminal_hooks import terminal_notification_only_hook, terminal_run_hook
from ..utils.logging import cleanup_all_old_logs, setup_system_logger
from ..utils.logging import log_base_dir as default_log_base_dir
from ..utils.validation import load_config
from .status import SchedulerStatusError, SchedulerStatusWriter

TICK_INTERVAL = 60
WINDOW_SECONDS = 60
SHUTDOWN_TIMEOUT = 300
SCHEDULER_LOCK_START_ATTEMPTS = 3
SCHEDULER_LOCK_RETRY_DELAY_SECONDS = 0.1
# Obergrenze des In-Memory-Puffers terminaler Records für den periodischen
# Run-Bericht im nicht-persistenten CLI-Modus; älteste Einträge fallen zuerst raus.
REPORT_BUFFER_LIMIT = 500
REPORT_STATUSES = (
    RunStatus.SUCCESS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.SKIPPED,
    RunStatus.LOCK_ERROR,
    RunStatus.CONFIG_ERROR,
    RunStatus.UNEXPECTED_ERROR,
)


class SchedulerStartState(StrEnum):
    """Distinguishable outcomes of a :meth:`CronScheduler.start` invocation.

    Mirrors the terminology already established for scheduler status reporting
    (``scheduler_lock_error``, see :mod:`src.scheduler.status`) and for
    catch-all runtime failures elsewhere in the project (``unexpected_error``,
    see :class:`src.services.run_manager.RunStatus`), so callers can map each
    state to the correct exit code/status without re-deriving its meaning from
    a bare boolean.
    """

    RUNNING = "running"
    SCHEDULER_LOCK_ERROR = "scheduler_lock_error"
    UNEXPECTED_ERROR = "unexpected_error"


@dataclass(frozen=True)
class SchedulerStartOutcome:
    """Structured result of :meth:`CronScheduler.start`.

    Replaces the previous bare ``bool`` return value so that callers can
    distinguish a genuine scheduler-singleton-lock conflict from any other
    startup failure (e.g. the local run-control socket could not be started)
    instead of mapping every ``False`` to a lock error.

    Attributes:
        state: The distinguishable outcome of the start attempt.
        message: Optional human-readable detail, set for non-success states.
    """

    state: SchedulerStartState
    message: str | None = None

    @property
    def started(self) -> bool:
        return self.state == SchedulerStartState.RUNNING


class CronScheduler:
    """Führt Workflows und Backups basierend auf Cron-Expressions automatisch aus.

    Der Scheduler läuft auf einem dedizierten asyncio-Event-Loop und prüft jede
    Minute, ob Workflows oder Backups fällig sind. Fällige Tasks werden über einen
    scheduler-eigenen :class:`RunManager` als asyncio-Tasks gestartet, sodass
    unabhängige Ressourcen parallel verarbeitet werden können. Das
    Resource-Locking in JobRunner verhindert kollidierende Läufe; ein
    Lock-Konflikt führt zu Status SKIPPED.

    Die synchrone Methode :meth:`start` bleibt als Fassade erhalten und ruft
    intern ``asyncio.run(...)`` auf. Während der Lauf aktiv ist, exponiert der
    Scheduler seine Runs über einen lokalen Unix-Domain-Socket
    (:class:`RunControlServer`), sodass ein zweiter Prozess sie auflisten und
    abbrechen kann.

    Beim Herunterfahren (:meth:`stop`) signalisiert der Scheduler thread-safe das
    Loop-Ende, bricht alle aktiven Scheduler-Runs ab, wartet bis zu
    SHUTDOWN_TIMEOUT Sekunden auf deren kontrolliertes Cleanup, schließt den
    Control-Socket und gibt schließlich den Scheduler-Lock frei.

    Wenn config_path angegeben ist, wird die Konfiguration bei jedem Tick neu
    geladen, falls sich mtime oder Inhalt der Datei geändert haben.

    Attributes:
        config: Die gesamte Anwendungskonfiguration.
        lock_dir: Verzeichnis für Scheduler- und Resource-Lock-Dateien.
        log_base_dir: Basisverzeichnis für Job-Log-Dateien.
    """

    def __init__(
        self,
        config: ResolvedAppConfig,
        lock_dir: Path | None = None,
        log_base_dir: Path | None = None,
        config_path: Path | None = None,
        owner: Literal["gui", "scheduler-cli"] = "scheduler-cli",
        status_writer: SchedulerStatusWriter | None = None,
        start_result_callback: Callable[[str, str | None], None] | None = None,
        socket_path: Path | None = None,
        history_db_path: Path | None = None,
        on_backup_success: (
            Callable[[str, str, BackupRunStatsContext], Awaitable[object]] | None
        ) = None,
    ) -> None:
        """Initialisiert den CronScheduler.

        Args:
            config: Vollständige Anwendungskonfiguration mit Jobs und Workflows.
            lock_dir: Verzeichnis für Lock-Dateien.
            log_base_dir: Basisverzeichnis für Log-Dateien.
            config_path: Pfad zur TOML-Datei für automatisches Reload bei Änderung.
            owner: Runtime owner shown in scheduler status.
            status_writer: Optional writer for scheduler status updates.
            start_result_callback: Optional callback called once start outcome is known.
            socket_path: Optionaler Pfad für den Run-Control-Socket. Standard ist
                :func:`default_socket_path`; in Tests injizierbar.
            history_db_path: Optionaler Pfad für die SQLite-AppData-DB. Standard ist
                ``$DK_APPDATA_DIR/appdata.db``; in Tests injizierbar.
            on_backup_success: Optional callback invoked after each successful backup run.
                Used by the GUI scheduler to update the stats cache. Remains None in
                headless CLI mode.
        """
        self.config = config
        self._config_lock = threading.RLock()
        self.lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self.log_base_dir = log_base_dir if log_base_dir is not None else default_log_base_dir()
        self._config_path = config_path
        self._config_mtime: float | None = config_path.stat().st_mtime if config_path else 0.0
        self._config_digest: str | None = _config_digest(config_path) if config_path else None
        self.owner = owner
        self._status_writer = status_writer
        self._start_result_callback = start_result_callback
        self._socket_path = socket_path or default_socket_path()
        self._history_db_path = history_db_path or default_appdata_db_path()
        # Persistenz ist eine reine GUI-Modus-Eigenschaft: Der CLI-Scheduler
        # läuft ohne Run-History und darf die AppData-DB nicht anlegen.
        self._run_history_service: RunHistoryService | None = (
            RunHistoryService(db_path=self._history_db_path) if owner == "gui" else None
        )
        self._report_buffer: deque[RunRecord] = deque(maxlen=REPORT_BUFFER_LIMIT)
        self._last_report_window_end: datetime | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._run_manager: RunManager | None = None
        self._control_server: RunControlServer | None = None
        self._start_time: datetime | None = None
        self._last_check_time: datetime | None = None
        self._reload_error: SchedulerStatusError | None = None
        self._on_backup_success = on_backup_success if owner == "gui" else None
        self._last_log_cleanup_day: date | None = None
        self.logger = setup_system_logger(config.global_.log_level, self.log_base_dir)

    def start(self) -> SchedulerStartOutcome:
        """Startet den Scheduler-Daemon (blockierend).

        Läuft einen dedizierten asyncio-Event-Loop bis stop() aufgerufen wird
        oder ein Signal den Prozess beendet. Prüft jede Minute alle Workflows und
        Backups mit Schedule und reicht fällige an den scheduler-eigenen
        RunManager weiter. Nach dem Stop-Signal werden aktive Runs abgebrochen
        und ihr Cleanup boundedly abgewartet.

        Returns:
            Ein :class:`SchedulerStartOutcome`, das den Startausgang strukturiert
            beschreibt: ``RUNNING``, wenn der Scheduler-Lock erworben und die
            Runtime gestartet (und ggf. inzwischen wieder gestoppt) wurde;
            ``SCHEDULER_LOCK_ERROR``, wenn der Scheduler-Singleton-Lock bereits
            von einer anderen Instanz gehalten wird; ``UNEXPECTED_ERROR`` für
            jeden anderen Startfehler (z. B. wenn der lokale Run-Control-Socket
            nicht gestartet werden konnte).

        Example:
            >>> scheduler = CronScheduler(config)
            >>> scheduler.start()  # blockiert
        """
        return asyncio.run(self._run())

    async def _run(self) -> SchedulerStartOutcome:
        """Erwirbt den Singleton-Lock und führt die async Schleife darin aus."""
        last_error: SchedulerAlreadyRunningError | None = None
        for attempt in range(SCHEDULER_LOCK_START_ATTEMPTS):
            try:
                with SchedulerLock(self.lock_dir).acquire():
                    return await self._run_locked()
            except SchedulerAlreadyRunningError as exc:
                last_error = exc
                if attempt < SCHEDULER_LOCK_START_ATTEMPTS - 1:
                    await asyncio.sleep(SCHEDULER_LOCK_RETRY_DELAY_SECONDS)

        if last_error is None:  # Defensive; the loop always sets this before falling through.
            message = "Scheduler lock is already held"
        else:
            message = str(last_error)
            self.logger.warning(
                "Scheduler lock held by another process (%s) — "
                "this instance will not run the scheduler",
                last_error.lock_path,
            )
        self._notify_start_result("scheduler_lock_error", message)
        return SchedulerStartOutcome(SchedulerStartState.SCHEDULER_LOCK_ERROR, message)

    async def _run_locked(self) -> SchedulerStartOutcome:
        self._loop = asyncio.get_running_loop()
        self._async_stop = asyncio.Event()
        if self._stop_event.is_set():
            self._async_stop.set()

        if self._run_history_service is not None:
            self._run_manager = RunManager(
                on_started=self._run_history_service.create_run,
                on_terminal=terminal_run_hook(self._run_history_service),
            )
            await self._run_history_service.mark_active_runs_interrupted(
                origins={RunOrigin.SCHEDULER}
            )
        else:
            self._run_manager = RunManager(on_terminal=self._cli_terminal_hook())
        self._control_server = RunControlServer(self._run_manager, self._socket_path)
        try:
            await self._control_server.start()
        except Exception as exc:
            self.logger.exception(
                "Failed to start run-control socket at %s; scheduler will not start",
                self._socket_path,
            )
            try:
                await self._control_server.close()
            except Exception:
                self.logger.exception("Error while cleaning up failed run-control socket")
            self._control_server = None
            message = f"Run-control socket unavailable: {exc}"
            self._write_status(
                "stopped",
                datetime.now(UTC),
                SchedulerStatusError(code="run_control_start_error", message=message),
            )
            self._notify_start_result("unexpected_error", message)
            return SchedulerStartOutcome(SchedulerStartState.UNEXPECTED_ERROR, message)

        self.logger.info("Run-control socket listening at %s", self._socket_path)
        self.logger.info("Scheduler started")
        self._start_time = datetime.now(UTC)
        self._write_status("running", self._start_time, None)
        self._notify_start_result("running", None)

        try:
            while not self._async_stop.is_set():
                try:
                    self._reload_if_changed()
                    now = _local_now()
                    await self._check_and_run(now)
                    await self._cleanup_logs_if_due(now)
                    self._last_check_time = now
                    self._write_status("running", self._last_check_time, None)
                except Exception as exc:
                    self.logger.exception("Scheduler tick failed; continuing")
                    self._write_status(
                        "running",
                        datetime.now(UTC),
                        SchedulerStatusError(code="scheduler_tick_error", message=str(exc)),
                    )
                try:
                    await asyncio.wait_for(self._async_stop.wait(), timeout=TICK_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._shutdown()
        return SchedulerStartOutcome(SchedulerStartState.RUNNING)

    async def _cleanup_logs_if_due(self, now: datetime) -> None:
        """Apply log retention once per calendar day using the active config.

        Runs ``cleanup_all_old_logs`` in a worker thread so the event loop is not
        blocked. Honors ``global.log_retention_days``; ``None`` disables cleanup.
        """
        config = self._get_config_snapshot()
        retention = config.global_.log_retention_days
        if retention is None:
            return
        today = now.date()
        if self._last_log_cleanup_day == today:
            return
        self._last_log_cleanup_day = today
        try:
            deleted = await asyncio.to_thread(cleanup_all_old_logs, retention, self.log_base_dir)
        except Exception:
            self.logger.exception("Log retention cleanup failed")
            return
        if deleted:
            self.logger.info("Log retention removed %d old log file(s)", deleted)

    async def _shutdown(self) -> None:
        if self._run_manager is not None:
            self.logger.info(
                "Shutdown requested: cancelling active run(s) (timeout: %ds)",
                SHUTDOWN_TIMEOUT,
            )
            try:
                await self._run_manager.shutdown(SHUTDOWN_TIMEOUT)
            except Exception:
                self.logger.exception("Error while shutting down scheduler run manager")
        if self._control_server is not None:
            try:
                await self._control_server.close()
            except Exception:
                self.logger.exception("Error while closing run-control socket")
            self._control_server = None
        self._write_status("stopped", datetime.now(UTC), None)
        self.logger.info("Scheduler stopped")

    def _get_config_snapshot(self) -> ResolvedAppConfig:
        """Gibt die aktuell aktive Config synchronisiert als Snapshot-Referenz zurück."""
        with self._config_lock:
            return self.config

    def _reload_if_changed(self) -> None:
        """Lädt die Config neu, wenn sich Datei-Zeitstempel oder Inhalt geändert haben.

        Vergleicht mtime und SHA-256-Inhaltsdigest der Config-Datei. Bei Änderung
        wird die Datei neu eingelesen. Fehler beim Laden werden geloggt; die bisherige
        Config bleibt dann unverändert aktiv.
        """
        if self._config_path is None:
            return
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError as exc:
            # Keep the reported mtime consistent with the cleared digest so the
            # status file does not advertise a stale config_mtime during a
            # transient stat failure (missing file / permission flap).
            self._config_mtime = None
            self._config_digest = None
            self._record_reload_error(exc)
            return
        digest = _config_digest(self._config_path)
        if mtime == self._config_mtime and digest == self._config_digest:
            return
        try:
            new_config = load_config(self._config_path)
        except Exception as exc:
            self._config_mtime = mtime
            self._config_digest = digest
            self._record_reload_error(exc)
            return
        reload_time = datetime.now(UTC)
        with self._config_lock:
            self._config_mtime = mtime
            self._config_digest = digest
            self.config = new_config
            # NICHT _last_check_time setzen: das würde das Fälligkeitsfenster
            # (`not_before`) dieses Ticks auf ~[now, now] kollabieren lassen und
            # einen im regulären Fenster fälligen Run verschlucken.
            self._reload_error = None
        self.logger.info("Config reloaded from %s", self._config_path)
        self._write_status("running", reload_time, None)

    def _record_reload_error(self, exc: Exception) -> None:
        """Record a config reload error without replacing the active config."""
        self.logger.error("Config reload failed, keeping current config: %s", exc)
        self._reload_error = SchedulerStatusError(code="reload_config_error", message=str(exc))
        self._write_status("running", datetime.now(UTC), self._reload_error)

    def stop(self) -> None:
        """Signalisiert dem Scheduler thread-safe anzuhalten.

        Kann aus einem Signal-Handler oder aus dem SchedulerOwnerManager-Thread
        aufgerufen werden. Setzt das Pre-Loop-Flag und – falls der Loop bereits
        läuft – signalisiert das async Stop-Event thread-safe über
        ``loop.call_soon_threadsafe``.

        Example:
            >>> scheduler = CronScheduler(config)
            >>> threading.Thread(target=scheduler.start).start()
            >>> scheduler.stop()
        """
        self._stop_event.set()
        loop = self._loop
        async_stop = self._async_stop
        if loop is not None and async_stop is not None:
            if loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(async_stop.set)
            except RuntimeError:
                # The scheduler loop may finish between is_closed() and the
                # thread-safe callback registration. stop() remains idempotent.
                return

    async def _check_and_run(self, now: datetime) -> None:
        """Prüft alle fälligen Backups, Workflows und Rclone-Tasks.

        Iteriert über alle Jobs. Backups mit nicht-leerem Schedule werden eigenständig
        ausgeführt. Workflows mit nicht-leerem Schedule werden ebenfalls
        gestartet, wenn sie fällig sind. Rclone-Tasks mit nicht-leerem Schedule
        werden analog behandelt. Die Tasks werden fire-and-forget gestartet und
        nicht abgewartet.

        Args:
            now: Aktueller Zeitpunkt als Referenz für die Cron-Berechnung.
        """
        config = self._get_config_snapshot()
        await self._send_report_if_due(config, now)
        last_check_time = self._last_check_time
        not_before = last_check_time or self._start_time
        include_not_before = last_check_time is None
        for job_name, job_config in config.jobs.items():
            for backup_name, backup in job_config.backup.items():
                if not backup.schedule:
                    continue
                if not _is_due(
                    backup.schedule,
                    now,
                    WINDOW_SECONDS,
                    not_before,
                    include_not_before=include_not_before,
                ):
                    continue
                self.logger.info(
                    "Triggering backup '%s.%s' (schedule: %s)",
                    job_name,
                    backup_name,
                    backup.schedule,
                )
                await self._run_backup(config, job_name, backup_name)

            for wf_name, workflow in job_config.workflows.items():
                if not workflow.schedule:
                    continue
                if not _is_due(
                    workflow.schedule,
                    now,
                    WINDOW_SECONDS,
                    not_before,
                    include_not_before=include_not_before,
                ):
                    continue
                self.logger.info(
                    "Triggering workflow '%s.%s' (schedule: %s)",
                    job_name,
                    wf_name,
                    workflow.schedule,
                )
                await self._run_workflow(config, job_name, wf_name)

            for rclone_name, rclone in job_config.rclone.items():
                if not rclone.schedule:
                    continue
                if _is_due(
                    rclone.schedule,
                    now,
                    WINDOW_SECONDS,
                    not_before,
                    include_not_before=include_not_before,
                ):
                    self.logger.info(
                        "Triggering rclone '%s.rclone.%s' (schedule: %s)",
                        job_name,
                        rclone_name,
                        rclone.schedule,
                    )
                    await self._run_rclone(config, job_name, rclone_name)

    async def _send_report_if_due(self, config: ResolvedAppConfig, now: datetime) -> None:
        """Send the opt-in periodic run report when its global schedule is due."""
        report_schedule = getattr(config.global_.notifications, "report_schedule", None)
        if not report_schedule:
            return
        last_check_time = self._last_check_time
        not_before = last_check_time or self._start_time
        if not _is_due(
            report_schedule,
            now,
            WINDOW_SECONDS,
            not_before,
            include_not_before=last_check_time is None,
        ):
            return

        if self._run_history_service is not None:
            window_start, window_end = _report_window(report_schedule, now)
            try:
                records = await self._run_history_service.list_finished_between(
                    after=window_start,
                    before_or_at=window_end,
                )
            except Exception:  # noqa: BLE001
                self.logger.exception("Periodic run report failed")
                return
        else:
            # Nicht-persistenter CLI-Modus: Der Puffer enthält per Definition
            # alles seit dem letzten Bericht dieses Prozesses; eine
            # Fensterfilterung entfällt. Best-effort wie im GUI-Modus: Puffer
            # und Fenster rücken auch bei fehlgeschlagenem Versand weiter.
            window_end = _report_due_time(report_schedule, now)
            window_start = self._last_report_window_end or self._start_time or window_end
            records = list(self._report_buffer)
            self._report_buffer.clear()
            self._last_report_window_end = window_end

        try:
            event = _report_event_from_records(
                window_start=window_start,
                window_end=window_end,
                generated_at=now,
                records=records,
            )
            dispatcher = NotificationDispatcher(config.global_.notifications, self.logger)
            result = await asyncio.to_thread(dispatcher.notify_report, event)
        except Exception:  # noqa: BLE001
            self.logger.exception("Periodic run report failed")
            return
        self.logger.info(
            "Periodic run report sent for %s to %s via %s: attempted=%d succeeded=%d failed=%d",
            window_start.isoformat(),
            window_end.isoformat(),
            _report_provider_label(config),
            result.attempted,
            result.succeeded,
            result.failed,
        )

    def _cli_terminal_hook(self) -> TerminalHook:
        """Notification-only Terminal-Hook des CLI-Modus, füttert den Berichts-Puffer."""
        notify = terminal_notification_only_hook()

        async def _hook(record: RunRecord) -> None:
            self._report_buffer.append(record)
            await notify(record)

        return _hook

    async def _run_backup(self, config: ResolvedAppConfig, job_name: str, backup_name: str) -> None:
        """Reicht einen fälligen Backup als RunManager-Task ein (fire-and-forget).

        Args:
            config: Aktiver Config-Snapshot.
            job_name: Name des Jobs.
            backup_name: Name des Backups.
        """
        if self._run_manager is None:
            return
        run_id = str(uuid4())
        operation = self._make_backup_operation(config, job_name, backup_name, run_id)
        await self._run_manager.start(
            RunOrigin.SCHEDULER,
            job_name,
            "backup",
            backup_name,
            operation,
            run_id=run_id,
            notify_ctx=self._notification_context(
                config, job_name, "backup", backup_name, f"{job_name}.backup.{backup_name}"
            ),
        )

    def _make_backup_operation(
        self, config: ResolvedAppConfig, job_name: str, backup_name: str, run_id: str
    ) -> RunOperation:
        """Baut die async Operation für einen Backup-Run.

        Die Operation erhält den scheduler-seitigen Notification-Pfad bei
        JobAlreadyRunningError. Unerwartete Fehler werden vom JobRunner selbst
        behandelt; CancelledError wird durchgereicht.
        """
        job_config = config.jobs[job_name]

        async def operation(mark_not_cancellable: MarkNotCancellable) -> bool:
            runner = JobRunner(
                job_name,
                job_config,
                lock_dir=self.lock_dir,
                log_level=config.global_.log_level,
                log_base_dir=self.log_base_dir,
                on_backup_success=self._on_backup_success,
                on_operational_complete=mark_not_cancellable,
                run_history_service=self._run_history_service,
                run_id=run_id if self._run_history_service is not None else None,
                lock_retry_count=config.global_.lock_retry_count,
                lock_retry_delay=config.global_.lock_retry_delay,
            )
            try:
                success = await runner.run_backup(backup_name)
            except asyncio.CancelledError:
                raise
            except JobAlreadyRunningError:
                self.logger.warning(
                    "Skipping backup '%s.backup.%s': job is already running",
                    job_name,
                    backup_name,
                )
                mark_not_cancellable()
                raise
            if not success:
                self.logger.error(
                    "Backup '%s.backup.%s' finished with errors", job_name, backup_name
                )
            return success

        return operation

    async def _run_workflow(
        self, config: ResolvedAppConfig, job_name: str, workflow_name: str
    ) -> None:
        """Reicht einen fälligen Workflow als RunManager-Task ein (fire-and-forget).

        Args:
            config: Aktiver Config-Snapshot.
            job_name: Name des Jobs.
            workflow_name: Name des Workflows.
        """
        if self._run_manager is None:
            return
        run_id = str(uuid4())
        operation = self._make_workflow_operation(config, job_name, workflow_name, run_id)
        await self._run_manager.start(
            RunOrigin.SCHEDULER,
            job_name,
            "workflow",
            workflow_name,
            operation,
            run_id=run_id,
            notify_ctx=self._notification_context(
                config,
                job_name,
                "workflow",
                workflow_name,
                f"{job_name}.workflow.{workflow_name}",
            ),
        )

    def _make_workflow_operation(
        self, config: ResolvedAppConfig, job_name: str, workflow_name: str, run_id: str
    ) -> RunOperation:
        """Baut die async Operation für einen Workflow-Run.

        Die Operation erhält den scheduler-seitigen Notification-Pfad bei
        JobAlreadyRunningError. Unerwartete Fehler werden vom JobRunner selbst
        behandelt; CancelledError wird durchgereicht.
        """
        job_config = config.jobs[job_name]

        async def operation(mark_not_cancellable: MarkNotCancellable) -> bool:
            runner = JobRunner(
                job_name,
                job_config,
                lock_dir=self.lock_dir,
                log_level=config.global_.log_level,
                log_base_dir=self.log_base_dir,
                on_backup_success=self._on_backup_success,
                on_operational_complete=mark_not_cancellable,
                run_history_service=self._run_history_service,
                run_id=run_id if self._run_history_service is not None else None,
                lock_retry_count=config.global_.lock_retry_count,
                lock_retry_delay=config.global_.lock_retry_delay,
            )
            try:
                success = await runner.run_workflow(workflow_name)
            except asyncio.CancelledError:
                raise
            except JobAlreadyRunningError:
                self.logger.warning(
                    "Skipping workflow '%s.%s': job is already running",
                    job_name,
                    workflow_name,
                )
                mark_not_cancellable()
                raise
            if not success:
                self.logger.error("Workflow '%s.%s' finished with errors", job_name, workflow_name)
            return success

        return operation

    async def _run_rclone(self, config: ResolvedAppConfig, job_name: str, rclone_name: str) -> None:
        """Reicht einen fälligen Rclone-Sync als RunManager-Task ein (fire-and-forget).

        Args:
            config: Aktiver Config-Snapshot.
            job_name: Name des Jobs.
            rclone_name: Name des Rclone-Tasks.
        """
        if self._run_manager is None:
            return
        run_id = str(uuid4())
        operation = self._make_rclone_operation(config, job_name, rclone_name, run_id)
        await self._run_manager.start(
            RunOrigin.SCHEDULER,
            job_name,
            "rclone",
            rclone_name,
            operation,
            run_id=run_id,
            notify_ctx=self._notification_context(
                config, job_name, "rclone", rclone_name, f"{job_name}.rclone.{rclone_name}"
            ),
        )

    def _make_rclone_operation(
        self, config: ResolvedAppConfig, job_name: str, rclone_name: str, run_id: str
    ) -> RunOperation:
        """Baut die async Operation für einen Rclone-Run.

        Die Operation erhält den scheduler-seitigen Notification-Pfad bei
        JobAlreadyRunningError. Unerwartete Fehler werden vom JobRunner selbst
        behandelt; CancelledError wird durchgereicht.
        """
        job_config = config.jobs[job_name]

        async def operation(mark_not_cancellable: MarkNotCancellable) -> bool:
            runner = JobRunner(
                job_name,
                job_config,
                lock_dir=self.lock_dir,
                log_level=config.global_.log_level,
                log_base_dir=self.log_base_dir,
                on_backup_success=self._on_backup_success,
                on_operational_complete=mark_not_cancellable,
                run_history_service=self._run_history_service,
                run_id=run_id if self._run_history_service is not None else None,
                lock_retry_count=config.global_.lock_retry_count,
                lock_retry_delay=config.global_.lock_retry_delay,
            )
            try:
                success = await runner.run_step(f"rclone.{rclone_name}")
            except asyncio.CancelledError:
                raise
            except JobAlreadyRunningError:
                self.logger.warning(
                    "Skipping rclone '%s.rclone.%s': job is already running",
                    job_name,
                    rclone_name,
                )
                mark_not_cancellable()
                raise
            if not success:
                self.logger.error(
                    "Rclone '%s.rclone.%s' finished with errors", job_name, rclone_name
                )
            return success

        return operation

    def _write_status(
        self,
        state: Literal["running", "stopped"],
        last_tick_at: datetime | None,
        error: SchedulerStatusError | None,
    ) -> None:
        """Writes scheduler status if a writer is configured."""
        if self._status_writer is None:
            return
        try:
            if state == "running" and error is None:
                error = self._reload_error
            self._status_writer.write(
                state=state,
                owner=self.owner,
                started_at=self._start_time,
                last_tick_at=last_tick_at,
                config_path=self._config_path,
                config_mtime=self._config_mtime if self._config_path is not None else None,
                error=error,
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Could not write scheduler status")

    def _notification_context(
        self,
        config: ResolvedAppConfig,
        job_name: str,
        task_type: Literal["backup", "workflow", "rclone"],
        task_name: str,
        display_target: str,
    ) -> NotificationContext:
        """Capture notification inputs from the config snapshot that triggered a run."""
        job = config.jobs[job_name]
        if task_type == "backup":
            notifications = job.backup[task_name].notifications
        elif task_type == "workflow":
            notifications = job.workflows[task_name].notifications
        else:
            notifications = job.rclone[task_name].notifications
        return build_notification_context(
            providers=config.global_.notifications,
            notifications=notifications,
            logger=logging.getLogger(f"dockkeep.jobs.{job_name}"),
            task_type=task_type,
            task_name=task_name,
            display_target=display_target,
            log_path=self.log_base_dir / job_name / f"{date.today()}.log",
        )

    def _notify_start_result(self, state: str, message: str | None) -> None:
        """Notify the owner manager about the synchronous start result once."""
        if self._start_result_callback is None:
            return
        callback = self._start_result_callback
        self._start_result_callback = None
        callback(state, message)


def _config_digest(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _report_due_time(schedule: str, now: datetime) -> datetime:
    """Return the cron due time that made this tick report-eligible.

    ``now`` is the scheduler poll time. Reports are anchored at the due time,
    not at the later poll timestamp, so adjacent regular reports do not
    double-count runs when polling drifts by a few seconds.
    """
    local_now = now.astimezone()
    due: datetime = _croniter(schedule, local_now + timedelta(microseconds=1)).get_prev(datetime)
    return due


def _report_window(schedule: str, now: datetime) -> tuple[datetime, datetime]:
    current_due = _report_due_time(schedule, now)
    previous_due: datetime = _croniter(schedule, current_due).get_prev(datetime)
    return previous_due, current_due


def _report_event_from_records(
    *,
    window_start: datetime,
    window_end: datetime,
    generated_at: datetime,
    records: list[RunRecord],
) -> NotificationReportEvent:
    status_counts = {status.value: 0 for status in REPORT_STATUSES}
    summaries: list[NotificationReportRunSummary] = []
    for record in records:
        status_counts.setdefault(record.status.value, 0)
        status_counts[record.status.value] += 1
        summaries.append(_report_summary(record))
    return NotificationReportEvent(
        window_start=window_start,
        window_end=window_end,
        generated_at=generated_at,
        status_counts=status_counts,
        runs=tuple(summaries),
    )


def _report_provider_label(config: ResolvedAppConfig) -> str:
    providers = config.global_.notifications
    names: list[str] = []
    if providers.mail is not None:
        names.append("mail")
    if providers.pushover is not None:
        names.append("pushover")
    return ",".join(names) if names else "no providers"


def _report_summary(record: RunRecord) -> NotificationReportRunSummary:
    started_at = record.started_at or record.created_at
    duration_seconds = None
    if record.finished_at is not None:
        duration_seconds = max(0.0, (record.finished_at - started_at).total_seconds())
    return NotificationReportRunSummary(
        origin=record.origin.value,
        job=record.job,
        target=record.display_target,
        status=cast(NotificationReportStatus, record.status.value),
        started_at=started_at,
        finished_at=record.finished_at,
        duration_seconds=duration_seconds,
        dry_run=record.dry_run,
        error=record.error,
    )


def _is_due(
    schedule: str,
    now: datetime,
    window_seconds: int = WINDOW_SECONDS,
    not_before: datetime | None = None,
    *,
    include_not_before: bool = True,
) -> bool:
    """Prüft ob ein Cron-Ausdruck in den letzten window_seconds fällig war.

    Args:
        schedule: Cron-Expression (5 Felder, z.B. "0 2 * * *").
        now: Aktueller Zeitpunkt als Referenz.
        window_seconds: Zeitfenster in Sekunden, das rückwärts geprüft wird.
        not_before: Wenn gesetzt, werden Zeitpunkte vor diesem Wert ignoriert
            (verhindert sofortige Ausführung beim Scheduler-Start).
        include_not_before: Wenn ``False``, wird ein Fälligkeitszeitpunkt exakt
            bei ``not_before`` ignoriert. Das verhindert Doppeltrigger nach
            einem Tick exakt auf der Cron-Grenze.

    Returns:
        True wenn der letzte fällige Zeitpunkt innerhalb des Fensters liegt.

    Example:
        >>> from datetime import datetime
        >>> _is_due("* * * * *", datetime.now())
        True
    """
    local_now = now.astimezone()
    window_start = (
        not_before.astimezone()
        if not_before is not None
        else local_now - timedelta(seconds=window_seconds)
    )
    try:
        cron = _croniter(schedule, local_now + timedelta(microseconds=1))
        last_due: datetime = cron.get_prev(datetime)
    except (CroniterBadDateError, ValueError):
        return False
    if include_not_before:
        return last_due >= window_start
    return last_due > window_start


def _croniter(schedule: str, start_time: datetime) -> croniter:
    return croniter(schedule, start_time)
