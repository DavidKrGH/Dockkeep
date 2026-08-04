"""Tests für den CronScheduler (asyncio-Task-Modell)."""

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.core.locking import JobAlreadyRunningError, SchedulerAlreadyRunningError, SchedulerLock
from src.models.config import RawAppConfig
from src.models.resolve import resolve_config
from src.models.resolved_config import ResolvedAppConfig
from src.notifications.events import NotificationDispatchResult
from src.scheduler.cron import (
    CronScheduler,
    SchedulerStartOutcome,
    SchedulerStartState,
    _is_due,
    _report_event_from_records,
    _report_window,
)
from src.scheduler.status import SchedulerStatusReader, SchedulerStatusWriter
from src.services.run_manager import (
    MarkNotCancellable,
    RunKind,
    RunManager,
    RunOrigin,
    RunRecord,
    RunStatus,
)


def _resolved(raw: dict) -> ResolvedAppConfig:
    """Baut eine ResolvedAppConfig aus einem Raw-Config-Dict (TOML-Struktur)."""
    return resolve_config(RawAppConfig.model_validate(raw))


@pytest.fixture(autouse=True)
def _test_appdata_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep scheduler appdata writes inside the test temp directory."""
    monkeypatch.setenv("DK_APPDATA_DIR", str(tmp_path / "appdata"))


@pytest.fixture()
def app_config() -> ResolvedAppConfig:
    """ResolvedAppConfig mit einem Job, einem Backup und zwei Workflows."""
    return _resolved(
        {
            "global": {"log_level": "info"},
            "jobs": {
                "test-job": {
                    "backup": {
                        "local": {"repository": "/backups/test", "schedule": "0 2 * * *"},
                    },
                    "workflow": {
                        "daily": {"schedule": "0 2 * * *", "steps": ["backup.local"]},
                        "manual-wf": {"steps": ["backup.local"]},
                    },
                }
            },
        }
    )


@pytest.fixture()
def scheduler(app_config: ResolvedAppConfig, tmp_path: Path) -> CronScheduler:
    return CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "run-control.sock",
        history_db_path=tmp_path / "appdata.db",
    )


def test_is_due_returns_true_when_last_due_within_window() -> None:
    """_is_due gibt True zurück wenn das letzte Fälligkeitsdatum im Fenster liegt."""
    assert _is_due("* * * * *", datetime.now().astimezone(), window_seconds=60) is True


def test_is_due_returns_true_at_exact_window_boundary() -> None:
    """_is_due gibt True zurück wenn last_due == window_start (inklusive)."""
    base = datetime(2025, 1, 30, 2, 1, 0, tzinfo=UTC)
    assert _is_due("* * * * *", base, window_seconds=60) is True


def test_is_due_returns_false_when_last_due_outside_window() -> None:
    """_is_due gibt False zurück wenn das Fälligkeitsdatum außerhalb des Fensters liegt."""
    base = datetime(2025, 1, 30, 2, 0, 30, tzinfo=UTC)
    assert _is_due("0 * * * *", base, window_seconds=10) is False


def test_is_due_returns_false_for_unreachable_schedule() -> None:
    base = datetime(2025, 1, 30, 2, 0, 30, tzinfo=UTC)

    assert _is_due("0 0 30 2 *", base, window_seconds=60) is False


def test_is_due_returns_true_within_custom_window() -> None:
    """_is_due gibt True zurück wenn last_due innerhalb des angegebenen Fensters liegt."""
    base = datetime(2025, 1, 30, 2, 0, 5, tzinfo=UTC)
    assert _is_due("0 * * * *", base, window_seconds=60) is True


def test_is_due_returns_true_on_exact_cron_boundary() -> None:
    """_is_due berücksichtigt einen Lauf exakt zum Prüfzeitpunkt."""
    base = datetime(2025, 1, 30, 2, 1, 0, tzinfo=UTC)
    assert _is_due("* * * * *", base, window_seconds=0) is True


def test_is_due_uses_not_before_for_poll_drift() -> None:
    """_is_due verpasst Läufe bei Poll-Drift nicht, wenn last_check gesetzt ist."""
    now = datetime(2025, 1, 30, 2, 1, 2, tzinfo=UTC)
    last_check = datetime(2025, 1, 30, 2, 0, 30, tzinfo=UTC)

    assert _is_due("* * * * *", now, window_seconds=1) is False
    assert _is_due("* * * * *", now, window_seconds=1, not_before=last_check) is True


def test_is_due_excludes_previous_tick_at_exact_cron_boundary() -> None:
    """_is_due triggert einen exakt vorherigen Cron-Zeitpunkt nicht doppelt."""
    now = datetime(2025, 1, 30, 2, 1, 0, tzinfo=UTC)
    last_check = datetime(2025, 1, 30, 2, 0, 0, tzinfo=UTC)

    assert (
        _is_due(
            "0 * * * *",
            now,
            window_seconds=60,
            not_before=last_check,
            include_not_before=False,
        )
        is False
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires time.tzset()")
def test_is_due_interprets_schedule_in_local_timezone() -> None:
    """_is_due wertet Cron-Felder in lokaler Containerzeit aus."""
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        now = datetime(2026, 6, 1, 0, 0, 5, tzinfo=UTC)

        assert _is_due("0 2 * * *", now, window_seconds=60) is True
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires time.tzset()")
def test_is_due_uses_local_timezone_after_dst_transition() -> None:
    """_is_due berücksichtigt den lokalen Offset direkt nach dem DST-Wechsel."""
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        now = datetime(2026, 3, 29, 1, 0, 5, tzinfo=UTC)

        assert _is_due("0 3 * * *", now, window_seconds=60) is True
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def test_report_window_returns_previous_complete_cron_interval() -> None:
    now = datetime(2026, 1, 1, 2, 0, 5, tzinfo=UTC)

    start, end = _report_window("0 * * * *", now)

    assert start == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    assert end == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)


def test_report_event_counts_all_terminal_statuses() -> None:
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    event = _report_event_from_records(
        window_start=base - timedelta(hours=1),
        window_end=base,
        generated_at=base + timedelta(seconds=5),
        records=[
            RunRecord(
                run_id="success",
                origin=RunOrigin.SCHEDULER,
                run_kind=RunKind.JOB_TASK,
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.SUCCESS,
                started_at=base - timedelta(minutes=2),
                finished_at=base - timedelta(minutes=1),
            ),
            RunRecord(
                run_id="failed",
                origin=RunOrigin.MANUAL,
                run_kind=RunKind.JOB_TASK,
                job="demo",
                task_type="workflow",
                task_name="daily",
                status=RunStatus.FAILED,
                started_at=base - timedelta(minutes=4),
                finished_at=base - timedelta(minutes=3),
            ),
        ],
    )

    assert event.status_counts["success"] == 1
    assert event.status_counts["failed"] == 1
    assert event.status_counts["cancelled"] == 0
    assert [run.target for run in event.runs] == ["demo.backup.local", "demo.workflow.daily"]


async def _drain_manager(manager: RunManager, *, timeout: float = 5.0) -> None:
    """Wait until the manager has no strongly referenced tasks left."""
    deadline = asyncio.get_running_loop().time() + timeout
    while await manager.list():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("RunManager tasks did not finish in time")
        await asyncio.sleep(0.01)


def _recording_run_manager() -> tuple[RunManager, list[RunRecord]]:
    records: list[RunRecord] = []

    async def on_terminal(record: RunRecord) -> None:
        records.append(record)

    return RunManager(on_terminal=on_terminal), records


def test_check_and_run_triggers_due_backup(scheduler: CronScheduler) -> None:
    """_check_and_run reicht fällige Backups an _run_backup weiter."""
    scheduler.config.jobs["test-job"].backup["local"].schedule = "* * * * *"

    async def run() -> None:
        scheduler._run_manager, terminal_records = _recording_run_manager()
        with patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_awaited_once_with(scheduler.config, "test-job", "local")

    asyncio.run(run())


def test_check_and_run_does_not_retrigger_previous_tick_boundary(
    scheduler: CronScheduler,
) -> None:
    """_check_and_run triggert den Cron-Zeitpunkt des letzten Ticks nicht erneut."""
    now = datetime(2025, 1, 30, 2, 1, 0, tzinfo=UTC)
    scheduler.config.jobs["test-job"].backup["local"].schedule = "0 * * * *"
    scheduler._last_check_time = datetime(2025, 1, 30, 2, 0, 0, tzinfo=UTC)

    async def run() -> None:
        with patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(now)
        mock_run.assert_not_awaited()

    asyncio.run(run())


def test_check_and_run_skips_backup_with_empty_schedule(scheduler: CronScheduler) -> None:
    """_check_and_run überspringt Backups ohne Schedule."""
    scheduler.config.jobs["test-job"].backup["local"].schedule = ""

    async def run() -> None:
        with patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_not_awaited()

    asyncio.run(run())


def test_check_and_run_skips_backup_with_none_schedule(scheduler: CronScheduler) -> None:
    """_check_and_run überspringt Backups mit schedule=None."""
    scheduler.config.jobs["test-job"].backup["local"].schedule = None

    async def run() -> None:
        with patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_not_awaited()

    asyncio.run(run())


def test_check_and_run_triggers_due_workflow(scheduler: CronScheduler) -> None:
    """_check_and_run reicht fällige Workflows an _run_workflow weiter."""
    scheduler.config.jobs["test-job"].workflows["daily"].schedule = "* * * * *"

    async def run() -> None:
        with patch.object(scheduler, "_run_workflow", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_awaited_once_with(scheduler.config, "test-job", "daily")

    asyncio.run(run())


def test_check_and_run_skips_not_due_workflow(scheduler: CronScheduler) -> None:
    """_check_and_run überspringt Workflows die noch nicht fällig sind."""
    base = datetime(2025, 1, 30, 2, 31, 0, tzinfo=UTC)
    scheduler.config.jobs["test-job"].workflows["daily"].schedule = "0 * * * *"

    async def run() -> None:
        with patch.object(scheduler, "_run_workflow", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(base)
        mock_run.assert_not_awaited()

    asyncio.run(run())


def test_check_and_run_skips_workflow_with_empty_schedule(scheduler: CronScheduler) -> None:
    """_check_and_run überspringt Workflows mit leerem Schedule."""
    scheduler.config.jobs["test-job"].workflows["daily"].schedule = ""

    async def run() -> None:
        with patch.object(scheduler, "_run_workflow", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_not_awaited()

    asyncio.run(run())


def test_check_and_run_skips_workflow_with_none_schedule(scheduler: CronScheduler) -> None:
    """_check_and_run überspringt Workflows mit schedule=None."""
    scheduler.config.jobs["test-job"].workflows["manual-wf"].schedule = None

    async def run() -> None:
        with patch.object(scheduler, "_run_workflow", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_not_awaited()

    asyncio.run(run())


def test_check_and_run_multiple_jobs(tmp_path: Path) -> None:
    """_check_and_run löst Workflows für mehrere Jobs aus."""
    config = _resolved(
        {
            "global": {},
            "jobs": {
                "job-a": {
                    "backup": {"local": {"repository": "/backups/a"}},
                    "workflow": {"w": {"schedule": "* * * * *", "steps": ["backup.local"]}},
                },
                "job-b": {
                    "backup": {"local": {"repository": "/backups/b"}},
                    "workflow": {"w": {"schedule": "* * * * *", "steps": ["backup.local"]}},
                },
            },
        }
    )
    scheduler = CronScheduler(
        config, lock_dir=tmp_path, log_base_dir=tmp_path, socket_path=tmp_path / "ctl.sock"
    )

    async def run() -> None:
        with patch.object(scheduler, "_run_workflow", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        assert mock_run.await_count == 2
        calls = {(c.args[1], c.args[2]) for c in mock_run.await_args_list}
        assert calls == {("job-a", "w"), ("job-b", "w")}

    asyncio.run(run())


def test_check_and_run_passes_current_config_snapshot_to_backup(scheduler: CronScheduler) -> None:
    """_check_and_run übergibt den aktiven Config-Snapshot an _run_backup."""
    scheduler.config.jobs["test-job"].backup["local"].schedule = "* * * * *"
    snapshot = scheduler.config

    async def run() -> None:
        with (
            patch.object(scheduler, "_get_config_snapshot", return_value=snapshot),
            patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run,
        ):
            await scheduler._check_and_run(datetime.now().astimezone())
        assert mock_run.await_args.args[0] is snapshot

    asyncio.run(run())


def test_check_and_run_passes_current_config_snapshot_to_workflow(scheduler: CronScheduler) -> None:
    """_check_and_run übergibt den aktiven Config-Snapshot an _run_workflow."""
    scheduler.config.jobs["test-job"].workflows["daily"].schedule = "* * * * *"
    snapshot = scheduler.config

    async def run() -> None:
        with (
            patch.object(scheduler, "_get_config_snapshot", return_value=snapshot),
            patch.object(scheduler, "_run_workflow", new=AsyncMock()) as mock_run,
        ):
            await scheduler._check_and_run(datetime.now().astimezone())
        assert mock_run.await_args.args[0] is snapshot

    asyncio.run(run())


def test_check_and_run_sends_due_periodic_report(tmp_path: Path) -> None:
    config = _resolved(
        {
            "global": {"notifications": {"report_schedule": "0 * * * *"}},
            "jobs": {},
        }
    )
    scheduler = CronScheduler(
        config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )
    scheduler._last_check_time = datetime(2026, 1, 1, 1, 59, 30, tzinfo=UTC)
    now = datetime(2026, 1, 1, 2, 0, 5, tzinfo=UTC)
    history = AsyncMock()
    history.list_finished_between.return_value = []
    scheduler._run_history_service = history

    async def run() -> None:
        with patch("src.scheduler.cron.NotificationDispatcher") as dispatcher_cls:
            dispatcher_cls.return_value.notify_report.return_value = NotificationDispatchResult(
                attempted=0,
                succeeded=0,
                failed=0,
            )

            await scheduler._check_and_run(now)

        history.list_finished_between.assert_awaited_once_with(
            after=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
            before_or_at=datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
        )
        dispatcher_cls.return_value.notify_report.assert_called_once()
        event = dispatcher_cls.return_value.notify_report.call_args.args[0]
        assert event.window_start == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
        assert event.window_end == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        assert event.generated_at == now

    asyncio.run(run())


def test_periodic_report_log_names_active_providers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _resolved(
        {
            "global": {
                "notifications": {
                    "report_schedule": "0 * * * *",
                    "mail": {
                        "host": "smtp.example.test",
                        "from_addr": "dockkeep@example.test",
                        "to": ["admin@example.test"],
                    },
                    "pushover": {
                        "token_env": "PUSHOVER_TOKEN",
                        "user_key_env": "PUSHOVER_USER_KEY",
                    },
                }
            },
            "jobs": {},
        }
    )
    scheduler = CronScheduler(
        config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )
    scheduler._last_check_time = datetime(2026, 1, 1, 1, 59, 30, tzinfo=UTC)
    scheduler._run_history_service = AsyncMock()
    scheduler._run_history_service.list_finished_between.return_value = []

    async def run() -> None:
        with patch("src.scheduler.cron.NotificationDispatcher") as dispatcher_cls:
            dispatcher_cls.return_value.notify_report.return_value = NotificationDispatchResult(
                attempted=2,
                succeeded=1,
                failed=1,
            )
            with caplog.at_level(logging.INFO):
                await scheduler._check_and_run(datetime(2026, 1, 1, 2, 0, 5, tzinfo=UTC))

    asyncio.run(run())

    assert any("via mail,pushover" in record.message for record in caplog.records)


def test_check_and_run_skips_periodic_report_without_schedule(scheduler: CronScheduler) -> None:
    async def run() -> None:
        with patch("src.scheduler.cron.NotificationDispatcher") as dispatcher_cls:
            await scheduler._check_and_run(datetime(2026, 1, 1, 2, 0, 5, tzinfo=UTC))
        dispatcher_cls.assert_not_called()

    asyncio.run(run())


def test_periodic_report_failure_does_not_block_due_backup(scheduler: CronScheduler) -> None:
    scheduler.config.global_.notifications.report_schedule = "0 * * * *"
    scheduler.config.jobs["test-job"].backup["local"].schedule = "0 * * * *"
    scheduler._last_check_time = datetime(2026, 1, 1, 1, 59, 30, tzinfo=UTC)
    now = datetime(2026, 1, 1, 2, 0, 5, tzinfo=UTC)
    history = AsyncMock()
    history.list_finished_between.side_effect = RuntimeError("db busy")
    scheduler._run_history_service = history

    async def run() -> None:
        with patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(now)
        mock_run.assert_awaited_once_with(scheduler.config, "test-job", "local")

    asyncio.run(run())


def test_cli_scheduler_start_creates_no_appdata_db(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """CLI-Scheduler-Start (Konstruktion + Startup-Pfad) legt keine AppData-DB an."""
    db_path = tmp_path / "appdata" / "appdata.db"
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        owner="scheduler-cli",
        socket_path=tmp_path / "ctl.sock",
        history_db_path=db_path,
    )

    async def fake_check(now: datetime) -> None:
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    assert not db_path.exists()


def test_gui_scheduler_start_sweeps_scheduler_runs_into_appdata_db(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """GUI-Scheduler-Start behält Run-History samt Startup-Sweep für SCHEDULER-Runs."""
    db_path = tmp_path / "appdata" / "appdata.db"
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        owner="gui",
        socket_path=tmp_path / "ctl.sock",
        history_db_path=db_path,
    )
    assert scheduler._run_history_service is not None
    asyncio.run(
        scheduler._run_history_service.create_run(
            RunRecord(
                run_id="stale-run",
                origin=RunOrigin.SCHEDULER,
                run_kind=RunKind.JOB_TASK,
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
    )

    async def fake_check(now: datetime) -> None:
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    assert db_path.exists()
    swept = asyncio.run(scheduler._run_history_service.get("stale-run"))
    assert swept is not None
    assert swept.status == RunStatus.UNEXPECTED_ERROR


def test_cli_terminal_hook_buffers_terminal_records(scheduler: CronScheduler) -> None:
    """Der CLI-Terminal-Hook legt terminale Records im Berichts-Puffer ab."""

    async def run() -> None:
        manager = RunManager(on_terminal=scheduler._cli_terminal_hook())

        async def operation(mark_not_cancellable: MarkNotCancellable) -> bool:
            return True

        record = await manager.start(RunOrigin.SCHEDULER, "demo", "backup", "local", operation)
        await _drain_manager(manager)
        assert [r.run_id for r in scheduler._report_buffer] == [record.run_id]

    asyncio.run(run())


def test_cli_scheduler_report_sends_buffer_and_advances_window(tmp_path: Path) -> None:
    """CLI-Report speist den Puffer in die bestehende Pipeline und leert ihn danach."""
    config = _resolved(
        {
            "global": {"notifications": {"report_schedule": "0 * * * *"}},
            "jobs": {},
        }
    )
    scheduler = CronScheduler(
        config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        owner="scheduler-cli",
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )
    assert scheduler._run_history_service is None
    scheduler._start_time = datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    scheduler._last_check_time = datetime(2026, 1, 1, 1, 59, 30, tzinfo=UTC)
    base = datetime(2026, 1, 1, 1, 45, tzinfo=UTC)
    scheduler._report_buffer.append(
        RunRecord(
            run_id="r1",
            origin=RunOrigin.SCHEDULER,
            run_kind=RunKind.JOB_TASK,
            job="demo",
            task_type="backup",
            task_name="local",
            status=RunStatus.SUCCESS,
            started_at=base,
            finished_at=base + timedelta(minutes=1),
        )
    )

    async def run() -> None:
        with patch("src.scheduler.cron.NotificationDispatcher") as dispatcher_cls:
            dispatcher_cls.return_value.notify_report.return_value = NotificationDispatchResult(
                attempted=1,
                succeeded=1,
                failed=0,
            )
            await scheduler._check_and_run(datetime(2026, 1, 1, 2, 0, 5, tzinfo=UTC))

            first = dispatcher_cls.return_value.notify_report.call_args.args[0]
            assert [r.target for r in first.runs] == ["demo.backup.local"]
            assert first.window_start == datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
            assert first.window_end == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
            assert len(scheduler._report_buffer) == 0

            # Folgebericht: leerer Puffer wird als Heartbeat gesendet, das Fenster
            # beginnt am Ende des vorherigen Berichts.
            scheduler._last_check_time = datetime(2026, 1, 1, 2, 59, 30, tzinfo=UTC)
            await scheduler._check_and_run(datetime(2026, 1, 1, 3, 0, 5, tzinfo=UTC))

            second = dispatcher_cls.return_value.notify_report.call_args.args[0]
            assert second.runs == ()
            assert second.window_start == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
            assert second.window_end == datetime(2026, 1, 1, 3, 0, tzinfo=UTC)

    asyncio.run(run())


def test_run_backup_starts_run_manager_task_and_calls_job_runner(scheduler: CronScheduler) -> None:
    """_run_backup startet einen RunManager-Task, der JobRunner.run_backup aufruft."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(return_value=True)
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        mock_cls.return_value.run_backup.assert_awaited_once_with("local")
        assert records[0].origin == RunOrigin.SCHEDULER
        assert records[0].display_target == "test-job.backup.local"
        assert records[0].status == RunStatus.SUCCESS

    asyncio.run(run())


def test_run_backup_passes_job_config_to_runner(tmp_path: Path) -> None:
    """Scheduled Backups übergeben job_config an JobRunner."""
    config = _resolved(
        {
            "global": {
                "backup": {"backup_timeout": 11},
                "rclone": {"rclone_timeout": 22},
                "hook_timeout": 33,
            },
            "jobs": {"test-job": {"backup": {"local": {"repository": "/backups/test"}}}},
        }
    )
    scheduler = CronScheduler(
        config, lock_dir=tmp_path, log_base_dir=tmp_path, socket_path=tmp_path / "ctl.sock"
    )

    async def run() -> None:
        scheduler._run_manager, terminal_records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(return_value=True)
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        assert mock_cls.call_args.args[1] is config.jobs["test-job"]

    asyncio.run(run())


def test_run_backup_uses_passed_snapshot_not_scheduler_config(tmp_path: Path) -> None:
    """_run_backup nutzt den übergebenen Snapshot statt scheduler.config."""
    snapshot = _resolved(
        {
            "global": {"log_level": "debug", "backup": {"backup_timeout": 11}},
            "jobs": {"test-job": {"backup": {"local": {"repository": "/backups/snapshot"}}}},
        }
    )
    current_config = _resolved(
        {
            "global": {"log_level": "error", "backup": {"backup_timeout": 99}},
            "jobs": {"test-job": {"backup": {"local": {"repository": "/backups/current"}}}},
        }
    )
    scheduler = CronScheduler(
        snapshot, lock_dir=tmp_path, log_base_dir=tmp_path, socket_path=tmp_path / "ctl.sock"
    )
    scheduler.config = current_config

    async def run() -> None:
        scheduler._run_manager, terminal_records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(return_value=True)
            await scheduler._run_backup(snapshot, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        assert mock_cls.call_args.args[1] is snapshot.jobs["test-job"]
        assert mock_cls.call_args.kwargs["log_level"] == "debug"

    asyncio.run(run())


def test_run_backup_handles_already_running_as_skipped(scheduler: CronScheduler) -> None:
    """Ein Lock-Konflikt im Backup-Run endet als SKIPPED, ohne dass eine Exception entkommt."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(
                side_effect=JobAlreadyRunningError("test-job", "local", Path("/var/lock/test.lock"))
            )
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        assert records[0].status == RunStatus.SKIPPED

    asyncio.run(run())


def test_run_backup_unexpected_exception_marks_unexpected_error(scheduler: CronScheduler) -> None:
    """Eine unerwartete Exception propagiert zu RunManager → UNEXPECTED_ERROR."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(side_effect=RuntimeError("boom"))
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        assert records[0].status == RunStatus.UNEXPECTED_ERROR

    asyncio.run(run())


def test_run_backup_failure_marks_failed(scheduler: CronScheduler) -> None:
    """Ein run_backup, das False zurückgibt, endet als FAILED."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(return_value=False)
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        assert records[0].status == RunStatus.FAILED

    asyncio.run(run())


def test_run_workflow_starts_run_manager_task(scheduler: CronScheduler) -> None:
    """_run_workflow startet einen RunManager-Task, der run_workflow aufruft."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_workflow = AsyncMock(return_value=True)
            await scheduler._run_workflow(scheduler.config, "test-job", "daily")
            await _drain_manager(scheduler._run_manager)
        mock_cls.return_value.run_workflow.assert_awaited_once_with("daily")
        assert records[0].display_target == "test-job.workflow.daily"
        assert records[0].status == RunStatus.SUCCESS

    asyncio.run(run())


def test_run_workflow_passes_job_config_to_runner(tmp_path: Path) -> None:
    """Scheduled Workflows übergeben job_config an JobRunner."""
    config = _resolved(
        {
            "global": {
                "backup": {"backup_timeout": 11},
                "rclone": {"rclone_timeout": 22},
                "hook_timeout": 33,
            },
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/test"}},
                    "workflow": {"daily": {"steps": ["backup.local"]}},
                }
            },
        }
    )
    scheduler = CronScheduler(
        config, lock_dir=tmp_path, log_base_dir=tmp_path, socket_path=tmp_path / "ctl.sock"
    )

    async def run() -> None:
        scheduler._run_manager = RunManager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_workflow = AsyncMock(return_value=True)
            await scheduler._run_workflow(scheduler.config, "test-job", "daily")
            await _drain_manager(scheduler._run_manager)
        assert mock_cls.call_args.args[1] is config.jobs["test-job"]

    asyncio.run(run())


def test_run_workflow_uses_passed_snapshot_not_scheduler_config(tmp_path: Path) -> None:
    """_run_workflow nutzt den übergebenen Snapshot statt scheduler.config."""
    snapshot = _resolved(
        {
            "global": {"log_level": "debug", "hook_timeout": 11},
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/snapshot"}},
                    "workflow": {"daily": {"steps": ["backup.local"]}},
                }
            },
        }
    )
    current_config = _resolved(
        {
            "global": {"log_level": "error", "hook_timeout": 99},
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/current"}},
                    "workflow": {"daily": {"steps": ["backup.local"]}},
                }
            },
        }
    )
    scheduler = CronScheduler(
        snapshot, lock_dir=tmp_path, log_base_dir=tmp_path, socket_path=tmp_path / "ctl.sock"
    )
    scheduler.config = current_config

    async def run() -> None:
        scheduler._run_manager = RunManager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_workflow = AsyncMock(return_value=True)
            await scheduler._run_workflow(snapshot, "test-job", "daily")
            await _drain_manager(scheduler._run_manager)
        assert mock_cls.call_args.args[1] is snapshot.jobs["test-job"]
        assert mock_cls.call_args.kwargs["log_level"] == "debug"

    asyncio.run(run())


def test_run_workflow_handles_already_running_as_skipped(scheduler: CronScheduler) -> None:
    """Ein Lock-Konflikt im Workflow-Run endet als SKIPPED."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_workflow = AsyncMock(
                side_effect=JobAlreadyRunningError("test-job", "daily", Path("/var/lock/test.lock"))
            )
            await scheduler._run_workflow(scheduler.config, "test-job", "daily")
            await _drain_manager(scheduler._run_manager)
        assert records[0].status == RunStatus.SKIPPED

    asyncio.run(run())


def test_run_workflow_unexpected_exception_marks_unexpected_error(scheduler: CronScheduler) -> None:
    """Eine unerwartete Workflow-Exception endet als UNEXPECTED_ERROR."""

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_workflow = AsyncMock(side_effect=RuntimeError("oom"))
            await scheduler._run_workflow(scheduler.config, "test-job", "daily")
            await _drain_manager(scheduler._run_manager)
        assert records[0].status == RunStatus.UNEXPECTED_ERROR

    asyncio.run(run())


def test_double_trigger_skips_second_run_for_same_resource(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Zweiter fälliger Lauf derselben Ressource wird via Lock-Konflikt SKIPPED.

    Das erste ``run_backup`` blockiert (hält den Lock), das zweite simuliert den
    Lock-Konflikt durch ``JobAlreadyRunningError``. Es darf keine Exception
    entkommen und nur ein operativer Lauf darf tatsächlich starten.
    """
    from unittest.mock import MagicMock

    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "ctl.sock",
    )
    release = asyncio.Event()
    started = asyncio.Event()
    operational_runs = 0

    async def run() -> None:
        nonlocal operational_runs
        scheduler._run_manager, records = _recording_run_manager()

        async def blocking_run(backup_name: str) -> bool:
            nonlocal operational_runs
            operational_runs += 1
            started.set()
            await release.wait()
            return True

        async def conflicting_run(backup_name: str) -> bool:
            raise JobAlreadyRunningError("test-job", "local", Path("/var/lock/test.lock"))

        run_backup_mocks = [
            AsyncMock(side_effect=blocking_run),
            AsyncMock(side_effect=conflicting_run),
        ]

        def make_instance(*args: object, **kwargs: object) -> object:
            instance = MagicMock()
            instance.run_backup = run_backup_mocks.pop(0)
            return instance

        with patch("src.scheduler.cron.JobRunner", side_effect=make_instance):
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await asyncio.wait_for(started.wait(), timeout=2)
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            # Let the second (skipping) run resolve, then release the first.
            await asyncio.sleep(0.05)
            release.set()
            await _drain_manager(scheduler._run_manager)

        statuses = sorted(str(r.status) for r in records)
        assert str(RunStatus.SKIPPED) in statuses
        assert str(RunStatus.SUCCESS) in statuses
        assert operational_runs == 1

    asyncio.run(run())


def test_get_config_snapshot_returns_active_config(scheduler: CronScheduler) -> None:
    """_get_config_snapshot gibt die aktuell aktive Config zurück."""
    assert scheduler._get_config_snapshot() is scheduler.config


def test_reload_if_changed_swaps_config_visible_via_snapshot(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Erfolgreicher Reload ersetzt die aktive Config synchronisiert."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
    )
    scheduler._config_mtime = 0
    new_config = _resolved(
        {
            "global": {"log_level": "debug"},
            "jobs": {"new-job": {"backup": {"local": {"repository": "/backups/new"}}}},
        }
    )

    with patch("src.scheduler.cron.load_config", return_value=new_config):
        scheduler._reload_if_changed()

    assert scheduler._get_config_snapshot() is new_config
    # Ein Reload darf das Fälligkeits-`not_before` (_last_check_time) NICHT
    # überschreiben, sonst kollabiert das Fälligkeitsfenster des laufenden Ticks.
    assert scheduler._last_check_time is None


def test_reload_within_tick_still_triggers_due_run(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Reload im selben Tick darf einen fälligen Run nicht verschlucken.

    Mirrors the documented repro: ein im regulären Fenster
    (``[vorheriger_Tick, now]``) fälliger Backup muss auch dann getriggert
    werden, wenn im selben Tick erst ein Config-Reload erfolgt. Der Reload darf
    `_last_check_time` (das Fälligkeits-`not_before`) nicht auf ~now anheben.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
    )
    scheduler._config_mtime = 0
    # Vorheriger echter Tick liegt eine Minute zurück (reguläres Fenster).
    now = datetime.now().astimezone()
    scheduler._last_check_time = now - timedelta(seconds=60)
    new_config = _resolved(
        {
            "global": {"log_level": "info"},
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/x", "schedule": "* * * * *"}}
                }
            },
        }
    )

    async def run() -> None:
        scheduler._run_manager = RunManager()
        with patch("src.scheduler.cron.load_config", return_value=new_config):
            scheduler._reload_if_changed()
        # Reload darf das Fälligkeitsfenster nicht kollabieren lassen.
        assert scheduler._last_check_time == now - timedelta(seconds=60)
        with patch.object(scheduler, "_run_backup", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(now)
        mock_run.assert_awaited_once_with(scheduler.config, "test-job", "local")

    asyncio.run(run())


def test_reload_if_changed_keeps_old_config_on_failure(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Fehlgeschlagener Reload lässt die zuletzt gültige Config aktiv."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
    )
    scheduler._config_mtime = 0

    with patch("src.scheduler.cron.load_config", side_effect=ValueError("bad config")):
        scheduler._reload_if_changed()

    assert scheduler._get_config_snapshot() is app_config


def test_reload_stat_failure_neutralizes_config_mtime(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Ein transienter stat-Fehler setzt den gemeldeten config_mtime auf None."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
    )
    assert scheduler._config_mtime is not None

    config_path.unlink()
    scheduler._reload_if_changed()

    assert scheduler._config_mtime is None
    assert scheduler._config_digest is None
    assert scheduler._reload_error is not None


def test_cleanup_logs_if_due_applies_retention_once_per_day(tmp_path: Path) -> None:
    """Log-Retention läuft beim Tick und höchstens einmal pro Kalendertag."""
    config = _resolved(
        {
            "global": {"log_retention_days": 1},
            "jobs": {"demo": {"backup": {"local": {"repository": "/repo"}}}},
        }
    )
    scheduler = CronScheduler(config, lock_dir=tmp_path, log_base_dir=tmp_path)
    job_logs = tmp_path / "demo"
    job_logs.mkdir()
    old_log = job_logs / "2000-01-01.log"
    old_log.write_text("old", encoding="utf-8")
    today_log = job_logs / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    today_log.write_text("today", encoding="utf-8")
    now = datetime.now().astimezone()

    asyncio.run(scheduler._cleanup_logs_if_due(now))

    assert not old_log.exists()
    assert today_log.exists()
    assert scheduler._last_log_cleanup_day == now.date()

    # A second tick on the same day must not run cleanup again.
    old_log.write_text("recreated", encoding="utf-8")
    asyncio.run(scheduler._cleanup_logs_if_due(now))
    assert old_log.exists()


def test_cleanup_logs_if_due_noop_without_retention(tmp_path: Path) -> None:
    """Ohne log_retention_days werden keine Logs entfernt."""
    config = _resolved({"jobs": {"demo": {"backup": {"local": {"repository": "/repo"}}}}})
    scheduler = CronScheduler(config, lock_dir=tmp_path, log_base_dir=tmp_path)
    job_logs = tmp_path / "demo"
    job_logs.mkdir()
    old_log = job_logs / "2000-01-01.log"
    old_log.write_text("old", encoding="utf-8")

    asyncio.run(scheduler._cleanup_logs_if_due(datetime.now().astimezone()))

    assert old_log.exists()


def test_reload_if_changed_detects_same_mtime_content_change(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Reload erkennt Inhaltsänderungen auch bei unverändertem mtime."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
    )
    original_mtime = scheduler._config_mtime
    config_path.write_text('[global]\nlog_level = "debug"\n[jobs]\n', encoding="utf-8")
    os.utime(config_path, (original_mtime, original_mtime))
    new_config = _resolved(
        {
            "global": {"log_level": "debug"},
            "jobs": {"new-job": {"backup": {"local": {"repository": "/backups/new"}}}},
        }
    )

    with patch("src.scheduler.cron.load_config", return_value=new_config) as mock_load:
        scheduler._reload_if_changed()

    mock_load.assert_called_once_with(config_path)
    assert scheduler._get_config_snapshot() is new_config


def test_reload_if_changed_records_failed_signature(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Dieselbe ungültige Datei löst nicht bei jedem Tick denselben Reload aus."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
    )
    scheduler._config_mtime = 0
    scheduler._config_digest = None

    with patch("src.scheduler.cron.load_config", side_effect=ValueError("bad config")) as mock_load:
        scheduler._reload_if_changed()
        scheduler._reload_if_changed()

    assert mock_load.call_count == 1
    assert scheduler._get_config_snapshot() is app_config


def test_reload_if_changed_writes_running_status_with_reload_error(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Fehlgeschlagener Reload hält den Scheduler-Status auf running."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    writer = SchedulerStatusWriter(tmp_path)
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
        status_writer=writer,
        owner="gui",
    )
    scheduler._config_mtime = 0

    with patch("src.scheduler.cron.load_config", side_effect=ValueError("bad config")):
        scheduler._reload_if_changed()

    status = SchedulerStatusReader(tmp_path).read_file()
    assert status is not None
    assert status.state == "running"
    assert status.error is not None
    assert status.error.code == "reload_config_error"


def test_reload_if_changed_records_deleted_config_as_reload_error(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Eine entfernte Config-Datei bleibt als Reload-Fehler sichtbar."""
    config_path = tmp_path / "config.toml"
    content = '[global]\nlog_level = "info"\n[jobs]\n'
    config_path.write_text(content, encoding="utf-8")
    writer = SchedulerStatusWriter(tmp_path)
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
        status_writer=writer,
        owner="gui",
    )
    original_mtime = scheduler._config_mtime
    config_path.unlink()

    scheduler._reload_if_changed()

    assert scheduler._get_config_snapshot() is app_config
    status = SchedulerStatusReader(tmp_path).read_file()
    assert status is not None
    assert status.state == "running"
    assert status.error is not None
    assert status.error.code == "reload_config_error"
    assert str(config_path) in status.error.message

    config_path.write_text(content, encoding="utf-8")
    os.utime(config_path, (original_mtime, original_mtime))
    with patch("src.scheduler.cron.load_config", return_value=app_config) as mock_load:
        scheduler._reload_if_changed()

    mock_load.assert_called_once_with(config_path)
    recovered_status = SchedulerStatusReader(tmp_path).read_file()
    assert recovered_status is not None
    assert recovered_status.error is None


def test_regular_tick_status_keeps_reload_error_until_successful_reload(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Reguläre Tick-Statuswrites löschen einen aktiven Reload-Fehler nicht."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[global]\nlog_level = "info"\n[jobs]\n', encoding="utf-8")
    writer = SchedulerStatusWriter(tmp_path)
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        config_path=config_path,
        status_writer=writer,
        owner="gui",
    )
    scheduler._config_mtime = 0

    with patch("src.scheduler.cron.load_config", side_effect=ValueError("bad config")):
        scheduler._reload_if_changed()

    scheduler._write_status("running", datetime.now(UTC), None)
    failed_status = SchedulerStatusReader(tmp_path).read_file()
    assert failed_status is not None
    assert failed_status.error is not None
    assert failed_status.error.code == "reload_config_error"

    config_path.write_text('[global]\nlog_level = "debug"\n[jobs]\n', encoding="utf-8")
    with patch("src.scheduler.cron.load_config", return_value=app_config):
        scheduler._reload_if_changed()

    scheduler._write_status("running", datetime.now(UTC), None)
    recovered_status = SchedulerStatusReader(tmp_path).read_file()
    assert recovered_status is not None
    assert recovered_status.error is None


def test_stop_signals_scheduler_to_exit(scheduler: CronScheduler) -> None:
    """stop() aus einem anderen Thread beendet eine laufende start()-Schleife."""

    async def fake_check(now: datetime) -> None:
        return None

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        thread = threading.Thread(target=scheduler.start)
        thread.start()
        # Wait until the loop is actually running before signalling.
        deadline = time.monotonic() + 5
        while scheduler._loop is None and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduler.stop()
        thread.join(timeout=10)

    assert not thread.is_alive(), "Scheduler-Thread läuft noch nach stop()"


def test_stop_before_loop_starts_is_honored(scheduler: CronScheduler) -> None:
    """Ein stop() vor dem Loop-Start beendet den Loop sofort beim Eintritt."""
    checks = 0

    async def fake_check(now: datetime) -> None:
        nonlocal checks
        checks += 1

    scheduler.stop()  # set the pre-loop flag before start()
    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    assert checks == 0


def test_start_returns_running_outcome_when_scheduler_lock_is_free(
    scheduler: CronScheduler,
) -> None:
    """start() gibt ein RUNNING-Outcome zurück, wenn der Scheduler-Lock frei ist."""
    running_outcome = SchedulerStartOutcome(SchedulerStartState.RUNNING)
    with patch.object(
        scheduler, "_run_locked", new=AsyncMock(return_value=running_outcome)
    ) as mock_locked:
        result = scheduler.start()

    assert result == running_outcome
    assert result.started is True
    mock_locked.assert_awaited_once()


def test_start_returns_lock_error_outcome_when_scheduler_lock_is_held(
    scheduler: CronScheduler,
) -> None:
    """start() liefert ein SCHEDULER_LOCK_ERROR-Outcome, wenn der globale Lock belegt ist."""
    with (
        SchedulerLock(scheduler.lock_dir).acquire(),
        patch.object(scheduler, "_check_and_run", new=AsyncMock()) as mock_check,
    ):
        result = scheduler.start()

    assert result.state == SchedulerStartState.SCHEDULER_LOCK_ERROR
    assert result.started is False
    assert result.message is not None
    mock_check.assert_not_awaited()


def test_scheduler_writes_running_tick_and_stopped_status(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """CronScheduler writes status on start, tick, and stop."""
    writer = SchedulerStatusWriter(tmp_path)
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        status_writer=writer,
        owner="gui",
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )

    async def fake_check(now: datetime) -> None:
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    status = writer.status_path.read_text(encoding="utf-8")
    assert '"state": "stopped"' in status
    assert '"owner": "gui"' in status


def test_scheduler_tick_exception_is_reported_and_next_tick_continues(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    writer = SchedulerStatusWriter(tmp_path)
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        status_writer=writer,
        owner="gui",
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )
    attempts = 0

    async def fake_check(now: datetime) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("tick exploded")
        scheduler.stop()

    with (
        patch("src.scheduler.cron.TICK_INTERVAL", 0.01),
        patch.object(scheduler, "_check_and_run", side_effect=fake_check),
        patch.object(scheduler, "_write_status", wraps=scheduler._write_status) as mock_status,
    ):
        assert scheduler.start().started is True

    assert attempts == 2
    assert any(
        call.args[2] is not None and call.args[2].code == "scheduler_tick_error"
        for call in mock_status.call_args_list
    )


def test_scheduler_releases_lock_after_stop(app_config: ResolvedAppConfig, tmp_path: Path) -> None:
    """Nach stop() ist der Scheduler-Lock wieder frei erwerbbar."""
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )

    async def fake_check(now: datetime) -> None:
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    # The lock must be releasable/acquirable again afterwards.
    with SchedulerLock(tmp_path).acquire():
        pass


def test_scheduler_start_result_callback_reports_running(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    results: list[tuple[str, str | None]] = []
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        start_result_callback=lambda state, message: results.append((state, message)),
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
    )

    async def fake_check(now: datetime) -> None:
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    assert results == [("running", None)]


def test_stop_after_scheduler_loop_finished_is_idempotent(scheduler: CronScheduler) -> None:
    """stop() bleibt nach beendetem Event-Loop ohne RuntimeError erneut aufrufbar."""

    async def fake_check(now: datetime) -> None:
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    scheduler.stop()


def test_scheduler_start_result_callback_reports_lock_error(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    results: list[tuple[str, str | None]] = []
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        start_result_callback=lambda state, message: results.append((state, message)),
        socket_path=tmp_path / "ctl.sock",
    )

    with SchedulerLock(tmp_path).acquire():
        outcome = scheduler.start()

    assert outcome.started is False
    assert outcome.state == SchedulerStartState.SCHEDULER_LOCK_ERROR
    assert outcome.message is not None
    assert results
    assert results[0][0] == "scheduler_lock_error"


def test_scheduler_start_retries_transient_scheduler_lock_conflict(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    attempts = 0

    class FlakySchedulerLock:
        def __init__(self, lock_dir: Path) -> None:
            self.lock_path = lock_dir / "dockkeep-scheduler.lock"

        @contextmanager
        def acquire(self) -> object:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise SchedulerAlreadyRunningError(self.lock_path)
            yield

    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "ctl.sock",
    )

    with (
        patch("src.scheduler.cron.SchedulerLock", FlakySchedulerLock),
        patch("src.scheduler.cron.asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
        patch.object(
            scheduler,
            "_run_locked",
            new_callable=AsyncMock,
            return_value=SchedulerStartOutcome(SchedulerStartState.RUNNING),
        ) as run_locked,
    ):
        outcome = asyncio.run(scheduler._run())

    assert outcome.started is True
    assert attempts == 3
    assert sleep_mock.await_count == 2
    run_locked.assert_awaited_once()


def test_control_socket_created_on_start_and_removed_on_shutdown(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Der Control-Socket existiert während des Laufs und wird beim Stop entfernt."""
    socket_path = tmp_path / "run-control.sock"
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=socket_path,
        history_db_path=tmp_path / "appdata.db",
    )
    socket_seen = threading.Event()

    async def fake_check(now: datetime) -> None:
        if socket_path.exists():
            socket_seen.set()
        scheduler.stop()

    with patch.object(scheduler, "_check_and_run", side_effect=fake_check):
        assert scheduler.start().started is True

    assert socket_seen.is_set(), "Control-Socket wurde während des Laufs nicht angelegt"
    assert not socket_path.exists(), "Control-Socket wurde beim Shutdown nicht entfernt"


def test_start_aborts_when_control_socket_in_use(
    app_config: ResolvedAppConfig, tmp_path: Path
) -> None:
    """Ein bereits aktiver Control-Socket verhindert den Start sauber."""
    socket_path = tmp_path / "run-control.sock"
    results: list[tuple[str, str | None]] = []
    writer = SchedulerStatusWriter(tmp_path)
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=socket_path,
        start_result_callback=lambda state, message: results.append((state, message)),
        status_writer=writer,
        history_db_path=tmp_path / "appdata.db",
    )

    async def boom(self: object) -> None:
        raise RuntimeError("socket in use")

    checked = False

    async def fake_check(now: datetime) -> None:
        nonlocal checked
        checked = True

    with (
        patch("src.scheduler.cron.RunControlServer.start", new=boom),
        patch("src.scheduler.cron.RunControlServer.close", new=AsyncMock()) as mock_close,
        patch.object(scheduler, "_check_and_run", side_effect=fake_check),
    ):
        outcome = scheduler.start()

    # Run-control startup failures must be distinguishable from genuine
    # scheduler-lock conflicts: they map to UNEXPECTED_ERROR, not
    # SCHEDULER_LOCK_ERROR, so callers don't report a misleading lock error.
    assert outcome.started is False
    assert outcome.state == SchedulerStartState.UNEXPECTED_ERROR
    assert outcome.message is not None
    assert "Run-control socket unavailable" in outcome.message

    assert checked is False
    mock_close.assert_awaited_once()
    assert results
    assert results[0][0] == "unexpected_error"
    assert results[0][1] is not None
    assert "Run-control socket unavailable" in results[0][1]
    status = SchedulerStatusReader(tmp_path).read_file()
    assert status is not None
    assert status.state == "stopped"
    assert status.error is not None
    assert status.error.code == "run_control_start_error"


def test_shutdown_cancels_active_run(app_config: ResolvedAppConfig, tmp_path: Path) -> None:
    """Ein aktiver Lauf wird beim Scheduler-Shutdown abgebrochen."""
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        socket_path=tmp_path / "ctl.sock",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        scheduler._control_server = None

        async def long_run(backup_name: str) -> bool:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return True

        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(side_effect=long_run)
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await asyncio.wait_for(started.wait(), timeout=2)
            await scheduler._shutdown()

        assert cancelled.is_set()
        assert records[0].status == RunStatus.CANCELLED

    asyncio.run(run())


def test_rclone_schedule_is_triggered_when_due(scheduler: CronScheduler) -> None:
    """_check_and_run reicht einen fälligen Rclone-Task an _run_rclone weiter."""
    scheduler.config = _resolved(
        {
            "global": {"log_level": "info"},
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/test"}},
                    "rclone": {
                        "offsite": {
                            "source": "/backups/test",
                            "target": "myremote:bucket",
                            "schedule": "* * * * *",
                        },
                    },
                }
            },
        }
    )

    async def run() -> None:
        scheduler._run_manager = RunManager()
        with patch.object(scheduler, "_run_rclone", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_awaited_once_with(scheduler.config, "test-job", "offsite")

    asyncio.run(run())


def test_rclone_schedule_not_triggered_when_no_schedule(scheduler: CronScheduler) -> None:
    """_check_and_run überspringt Rclone-Tasks ohne Schedule."""
    scheduler.config = _resolved(
        {
            "global": {"log_level": "info"},
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/test"}},
                    "rclone": {
                        "offsite": {
                            "source": "/backups/test",
                            "target": "myremote:bucket",
                        },
                    },
                }
            },
        }
    )

    async def run() -> None:
        with patch.object(scheduler, "_run_rclone", new=AsyncMock()) as mock_run:
            await scheduler._check_and_run(datetime.now().astimezone())
        mock_run.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("owner", ["gui", "scheduler-cli"])
def test_run_backup_run_id_wiring_depends_on_owner(
    app_config: ResolvedAppConfig, tmp_path: Path, owner: str
) -> None:
    """GUI-Owner reicht run_id und Run-History an JobRunner durch, CLI-Owner nicht."""
    on_backup_success = AsyncMock()
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        owner=owner,  # type: ignore[arg-type]
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
        on_backup_success=on_backup_success,
    )

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_backup = AsyncMock(return_value=True)
            await scheduler._run_backup(scheduler.config, "test-job", "local")
            await _drain_manager(scheduler._run_manager)
        if owner == "gui":
            assert mock_cls.call_args.kwargs["run_id"] == records[0].run_id
            assert mock_cls.call_args.kwargs["run_history_service"] is not None
            assert mock_cls.call_args.kwargs["on_backup_success"] is on_backup_success
        else:
            assert mock_cls.call_args.kwargs["run_id"] is None
            assert mock_cls.call_args.kwargs["run_history_service"] is None
            assert mock_cls.call_args.kwargs["on_backup_success"] is None

    asyncio.run(run())


@pytest.mark.parametrize("owner", ["gui", "scheduler-cli"])
def test_run_workflow_run_id_wiring_depends_on_owner(
    app_config: ResolvedAppConfig, tmp_path: Path, owner: str
) -> None:
    """Workflow-Dispatch folgt derselben owner-abhängigen run_id-Verdrahtung."""
    on_backup_success = AsyncMock()
    scheduler = CronScheduler(
        app_config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        owner=owner,  # type: ignore[arg-type]
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
        on_backup_success=on_backup_success,
    )

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_workflow = AsyncMock(return_value=True)
            await scheduler._run_workflow(scheduler.config, "test-job", "daily")
            await _drain_manager(scheduler._run_manager)
        if owner == "gui":
            assert mock_cls.call_args.kwargs["run_id"] == records[0].run_id
            assert mock_cls.call_args.kwargs["on_backup_success"] is on_backup_success
        else:
            assert mock_cls.call_args.kwargs["run_id"] is None
            assert mock_cls.call_args.kwargs["on_backup_success"] is None

    asyncio.run(run())


@pytest.mark.parametrize("owner", ["gui", "scheduler-cli"])
def test_run_rclone_run_id_wiring_depends_on_owner(tmp_path: Path, owner: str) -> None:
    """Rclone-Dispatch folgt derselben owner-abhängigen run_id-Verdrahtung."""
    config = _resolved(
        {
            "global": {"log_level": "info"},
            "jobs": {
                "test-job": {
                    "backup": {"local": {"repository": "/backups/test"}},
                    "rclone": {
                        "offsite": {
                            "source": "/backups/test",
                            "target": "myremote:bucket",
                            "schedule": "* * * * *",
                        },
                    },
                }
            },
        }
    )
    on_backup_success = AsyncMock()
    scheduler = CronScheduler(
        config,
        lock_dir=tmp_path,
        log_base_dir=tmp_path,
        owner=owner,  # type: ignore[arg-type]
        socket_path=tmp_path / "ctl.sock",
        history_db_path=tmp_path / "appdata.db",
        on_backup_success=on_backup_success,
    )

    async def run() -> None:
        scheduler._run_manager, records = _recording_run_manager()
        with patch("src.scheduler.cron.JobRunner") as mock_cls:
            mock_cls.return_value.run_step = AsyncMock(return_value=True)
            await scheduler._run_rclone(scheduler.config, "test-job", "offsite")
            await _drain_manager(scheduler._run_manager)
        if owner == "gui":
            assert mock_cls.call_args.kwargs["run_id"] == records[0].run_id
            assert mock_cls.call_args.kwargs["on_backup_success"] is on_backup_success
        else:
            assert mock_cls.call_args.kwargs["run_id"] is None
            assert mock_cls.call_args.kwargs["on_backup_success"] is None

    asyncio.run(run())
