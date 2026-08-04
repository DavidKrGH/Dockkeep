"""Tests für src/main.py – CLI Entry Point."""

import argparse
import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import (
    EXIT_CANCELLED,
    EXIT_CONFIG_ERROR,
    EXIT_ERROR,
    EXIT_LOCK_ERROR,
    EXIT_SUCCESS,
    _build_parser,
    _foreground_run_exit_code,
    _resolve_dk_mode,
    cmd_cancel_run,
    cmd_gui,
    cmd_list_jobs,
    cmd_list_runs,
    cmd_list_tasks,
    cmd_list_workflows,
    cmd_next_runs,
    cmd_run_job,
    cmd_scheduler,
    cmd_scheduler_status,
    cmd_shell,
    cmd_validate_config,
    main,
)
from src.scheduler.cron import SchedulerStartOutcome, SchedulerStartState
from src.services.errors import NotFoundServiceError, ServiceError
from src.utils.targets import parse_task_selector


@pytest.fixture(autouse=True)
def _default_cli_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-Handler-Tests laufen im CLI-Modus; Modus-Guard-Tests setzen DK_MODE selbst um."""
    monkeypatch.setenv("DK_MODE", "cli")


class TestParseTaskSelector:
    def test_backup_task_selector(self) -> None:
        parsed = parse_task_selector("home.backup.documents")
        assert (parsed.job, parsed.kind, parsed.name) == ("home", "backup", "documents")

    def test_workflow_with_hyphen(self) -> None:
        parsed = parse_task_selector("my-job.workflow.full-backup")
        assert (parsed.job, parsed.kind, parsed.name) == ("my-job", "workflow", "full-backup")

    def test_rclone_task_selector(self) -> None:
        parsed = parse_task_selector("job.rclone.offsite")
        assert (parsed.job, parsed.kind, parsed.name) == ("job", "rclone", "offsite")

    def test_backup_substep_format(self) -> None:
        parsed = parse_task_selector("job.backup.local.retention")
        assert (parsed.job, parsed.kind, parsed.name, parsed.substep) == (
            "job",
            "backup_step",
            "local",
            "retention",
        )

    def test_short_workflow_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Task selector must"):
            parse_task_selector("job.full-backup")

    def test_missing_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task selector format"):
            parse_task_selector("home-backup")

    def test_leading_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task selector format"):
            parse_task_selector(".backup")

    def test_trailing_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task selector format"):
            parse_task_selector("home-backup.")

    def test_only_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task selector format"):
            parse_task_selector(".")


class TestCmdRunJobConfigErrors:
    def _make_args(self, target: str | None) -> MagicMock:
        args = MagicMock()
        args.task_selector = target
        args.dry_run = False
        return args

    def test_config_file_not_found(self, tmp_path: Path) -> None:
        args = self._make_args("job.backup")
        result = cmd_run_job(args, tmp_path / "nonexistent.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_invalid_toml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad.toml"
        cfg.write_text("not = valid toml [[[")
        args = self._make_args("job.backup")
        result = cmd_run_job(args, cfg)
        assert result == EXIT_CONFIG_ERROR

    def test_invalid_target_format(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[global]\n"
            "[jobs.myjob.backup.local]\n"
            "repository = '/repo'\n"
            "sources = ['/data']\n"
        )
        args = self._make_args("no-dot-here")
        with patch("src.main.load_config") as mock_load:
            mock_load.return_value = MagicMock(
                global_=MagicMock(log_level="info"),
                jobs={},
            )
            result = cmd_run_job(args, cfg)
        assert result == EXIT_CONFIG_ERROR

    def test_missing_target_lists_available_run_targets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job_mock = MagicMock(
            backup={"local": MagicMock()},
            rclone={"offsite": MagicMock()},
            workflows={"full": MagicMock()},
        )
        config = MagicMock(global_=MagicMock(log_level="info"), jobs={"my-job": job_mock})
        with (
            patch("src.main.load_config", return_value=config),
            patch("src.main.JobRunner") as runner_mock,
        ):
            result = cmd_run_job(self._make_args(None), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        runner_mock.assert_not_called()
        out = capsys.readouterr().out
        assert "Available run targets:" in out
        assert "my-job:" in out
        assert "dk run my-job.backup.local" in out
        assert "dk run my-job.backup.local.retention" in out
        assert "dk run my-job.rclone.offsite" in out
        assert "dk run my-job.workflow.full" in out

    def test_job_not_found(self, tmp_path: Path) -> None:
        args = self._make_args("missing-job.backup.local")
        with patch("src.main.load_config") as mock_load:
            mock_load.return_value = MagicMock(
                global_=MagicMock(log_level="info"),
                jobs={},
            )
            result = cmd_run_job(args, tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_unknown_step(self, tmp_path: Path) -> None:
        args = self._make_args("my-job.workflow.unknown-step")
        job_mock = MagicMock(workflows={}, backup={})
        with patch("src.main.load_config") as mock_load:
            mock_load.return_value = MagicMock(
                global_=MagicMock(log_level="info"),
                jobs={"my-job": job_mock},
            )
            result = cmd_run_job(args, tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_validation_error_on_load(self, tmp_path: Path) -> None:
        from pydantic import ValidationError

        args = self._make_args("job.backup")
        with patch(
            "src.main.load_config", side_effect=ValidationError.from_exception_data("m", [])
        ):
            result = cmd_run_job(args, tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_value_error_on_load(self, tmp_path: Path) -> None:
        args = self._make_args("job.backup")
        with patch("src.main.load_config", side_effect=ValueError("bad config")):
            result = cmd_run_job(args, tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR


class TestCmdRunJobSuccess:
    def _args(self, target: str, dry_run: bool = False) -> MagicMock:
        args = MagicMock()
        args.task_selector = target
        args.dry_run = dry_run
        return args

    def _config_mock(self, job_name: str, job_mock: MagicMock) -> MagicMock:
        return MagicMock(
            global_=MagicMock(log_level="info"),
            jobs={job_name: job_mock},
        )

    def test_run_backup_success(self, tmp_path: Path) -> None:
        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
            patch("src.main.build_notification_context"),
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        runner_instance.run_backup.assert_awaited_once_with("local")

    def test_run_backup_substep_success(self, tmp_path: Path) -> None:
        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_step = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local.backup"), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        runner_instance.run_step.assert_awaited_once_with("backup.local.backup")

    def test_run_rclone_step(self, tmp_path: Path) -> None:
        job_mock = MagicMock(workflows={}, backup={}, rclone={"offsite": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_step = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.rclone.offsite"), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        runner_instance.run_step.assert_awaited_once_with("rclone.offsite")

    def test_run_workflow_success(self, tmp_path: Path) -> None:
        workflow_mock = MagicMock()
        job_mock = MagicMock(workflows={"full-backup": workflow_mock}, backup={})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_workflow = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(
                self._args("my-job.workflow.full-backup"), tmp_path / "config.toml"
            )

        assert result == EXIT_SUCCESS
        runner_instance.run_workflow.assert_awaited_once_with("full-backup")

    def test_unknown_task_returns_exit_config_error(self, tmp_path: Path) -> None:
        job_mock = MagicMock(workflows={}, backup={})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            mock_runner.return_value = runner_instance

            result = cmd_run_job(
                self._args("my-job.workflow.unknown-task"), tmp_path / "config.toml"
            )

        assert result == EXIT_CONFIG_ERROR

    def test_dry_run_flag_passed_to_job_runner(self, tmp_path: Path) -> None:
        """--dry-run übergibt dry_run=True an JobRunner."""
        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(
                self._args("my-job.backup.local", dry_run=True), tmp_path / "config.toml"
            )

        assert result == EXIT_SUCCESS
        assert mock_runner.call_args.kwargs.get("dry_run") is True

    def test_backup_failure_returns_exit_error(self, tmp_path: Path) -> None:
        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(return_value=False)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_ERROR

    def test_lock_error_returns_exit_lock_error(self, tmp_path: Path) -> None:
        from src.core.locking import JobAlreadyRunningError

        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(
                side_effect=JobAlreadyRunningError("my-job", "local", tmp_path / "my-job.lock")
            )
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_LOCK_ERROR

    def test_notification_context_uses_config_snapshot_and_exit_code_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Der Laufkontext wird aus dem aktiven Config-Snapshot gebaut."""
        from src.models.resolved_config import (
            ResolvedGlobalNotificationProvidersConfig,
            ResolvedMailNotificationConfig,
        )

        notifications = ResolvedGlobalNotificationProvidersConfig(
            mail=ResolvedMailNotificationConfig(
                host="smtp.example.com",
                from_addr="dk@example.com",
                to=["admin@example.com"],
            ),
        )
        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = MagicMock(
            global_=MagicMock(log_level="info", notifications=notifications),
            jobs={"my-job": job_mock},
        )

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
            patch("src.main.build_notification_context") as build_context,
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        assert build_context.call_args.kwargs["providers"] is notifications
        assert "notifications_config" not in mock_runner.call_args.kwargs

    def test_exit_error_unchanged_with_notifications_config(self, tmp_path: Path) -> None:
        """Exit-Code bleibt EXIT_ERROR bei Fehler, auch wenn notifications_config gesetzt ist."""
        from src.models.resolved_config import (
            ResolvedGlobalNotificationProvidersConfig,
            ResolvedMailNotificationConfig,
        )

        notifications = ResolvedGlobalNotificationProvidersConfig(
            mail=ResolvedMailNotificationConfig(
                host="smtp.example.com",
                from_addr="dk@example.com",
                to=["admin@example.com"],
            ),
        )
        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = MagicMock(
            global_=MagicMock(log_level="info", notifications=notifications),
            jobs={"my-job": job_mock},
        )

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(return_value=False)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_ERROR

    def test_sigint_cancellation_returns_exit_cancelled(self, tmp_path: Path) -> None:
        """Ctrl+C während eines laufenden Runs ergibt Exit-Code 130 (CANCELLED)."""
        import signal as signal_module

        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        async def blocking_run(_: str) -> bool:
            # Trigger SIGINT, then yield so the installed handler can cancel us.
            signal_module.raise_signal(signal_module.SIGINT)
            await asyncio.Event().wait()
            return True

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(side_effect=blocking_run)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_CANCELLED

    def test_late_sigint_cancel_not_found_is_ignored(self, tmp_path: Path) -> None:
        """A SIGINT cancel task may race with terminal eviction and must stay quiet."""
        import signal as signal_module

        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = self._config_mock("my-job", job_mock)

        async def fast_success(_: str) -> bool:
            signal_module.raise_signal(signal_module.SIGINT)
            await asyncio.sleep(0)
            return True

        async def missing_cancel(self: object, run_id: str) -> object:
            del self, run_id
            raise NotFoundServiceError("Run not found", code="run_not_found")

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
            patch("src.main.RunManager.cancel", new=missing_cancel),
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(side_effect=fast_success)
            mock_runner.return_value = runner_instance

            result = cmd_run_job(self._args("my-job.backup.local"), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS


class TestCmdRunJobAppDataFreedom:
    """CLI-Modus-Vertrag: dk run erzeugt keinerlei AppData-Daten."""

    def test_run_creates_no_appdata_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        appdata_dir = tmp_path / "appdata"
        appdata_dir.mkdir()
        monkeypatch.setenv("DK_APPDATA_DIR", str(appdata_dir))

        job_mock = MagicMock(workflows={}, backup={"local": MagicMock()})
        config_mock = MagicMock(
            global_=MagicMock(log_level="info"),
            jobs={"my-job": job_mock},
        )

        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.JobRunner") as mock_runner,
            patch("src.main.build_notification_context"),
        ):
            runner_instance = MagicMock()
            runner_instance.run_backup = AsyncMock(return_value=True)
            mock_runner.return_value = runner_instance

            args = MagicMock()
            args.task_selector = "my-job.backup.local"
            args.dry_run = False
            result = cmd_run_job(args, tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        assert list(appdata_dir.iterdir()) == []


class TestForegroundRunExitCode:
    """Status→Exit-Code-Mapping für dk-run-Vordergrundläufe."""

    def test_mapping(self) -> None:
        from src.services.run_manager import RunStatus

        assert _foreground_run_exit_code(RunStatus.SUCCESS) == EXIT_SUCCESS
        assert _foreground_run_exit_code(RunStatus.FAILED) == EXIT_ERROR
        assert _foreground_run_exit_code(RunStatus.UNEXPECTED_ERROR) == EXIT_ERROR
        assert _foreground_run_exit_code(RunStatus.CONFIG_ERROR) == EXIT_CONFIG_ERROR
        assert _foreground_run_exit_code(RunStatus.LOCK_ERROR) == EXIT_LOCK_ERROR
        assert _foreground_run_exit_code(RunStatus.CANCELLED) == EXIT_CANCELLED

    def test_non_terminal_statuses_map_defensively_to_error(self) -> None:
        from src.services.run_manager import RunStatus

        assert _foreground_run_exit_code(RunStatus.QUEUED) == EXIT_ERROR
        assert _foreground_run_exit_code(RunStatus.RUNNING) == EXIT_ERROR
        assert _foreground_run_exit_code(RunStatus.SKIPPED) == EXIT_ERROR


class TestCmdScheduler:
    def test_blocked_without_cli_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DK_MODE", raising=False)
        with patch("src.main.load_config") as mock_load:
            result = cmd_scheduler(tmp_path / "config.toml")
        assert result == EXIT_ERROR
        mock_load.assert_not_called()

    def test_config_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DK_MODE", "cli")
        result = cmd_scheduler(tmp_path / "nonexistent.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_invalid_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DK_MODE", "cli")
        cfg = tmp_path / "bad.toml"
        cfg.write_text("not = valid toml [[[")
        result = cmd_scheduler(cfg)
        assert result == EXIT_CONFIG_ERROR

    def test_validation_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        monkeypatch.setenv("DK_MODE", "cli")
        with patch(
            "src.main.load_config", side_effect=ValidationError.from_exception_data("m", [])
        ):
            result = cmd_scheduler(tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_value_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DK_MODE", "cli")
        with patch("src.main.load_config", side_effect=ValueError("bad config")):
            result = cmd_scheduler(tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_success_starts_scheduler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DK_MODE", "cli")
        with patch("src.main.load_config"), patch("src.main.CronScheduler") as mock_cls:
            mock_cls.return_value.start.return_value = SchedulerStartOutcome(
                SchedulerStartState.RUNNING
            )
            result = cmd_scheduler(tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        mock_cls.return_value.start.assert_called_once()

    def test_scheduler_lock_error_yields_exit_lock_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ein echter Scheduler-Lock-Konflikt bleibt Exit-Code 3."""
        monkeypatch.setenv("DK_MODE", "cli")
        with patch("src.main.load_config"), patch("src.main.CronScheduler") as mock_cls:
            mock_cls.return_value.start.return_value = SchedulerStartOutcome(
                SchedulerStartState.SCHEDULER_LOCK_ERROR,
                message="Scheduler lock is already held by another process",
            )
            result = cmd_scheduler(tmp_path / "config.toml")

        assert result == EXIT_LOCK_ERROR
        mock_cls.return_value.start.assert_called_once()

    def test_run_control_startup_failure_yields_exit_error_not_lock_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ein Run-Control-Startfehler ergibt Exit 1 mit treffender Meldung, nicht Exit 3
        und wird nicht pauschal als Lock-Fehler gemeldet."""
        monkeypatch.setenv("DK_MODE", "cli")
        with patch("src.main.load_config"), patch("src.main.CronScheduler") as mock_cls:
            mock_cls.return_value.start.return_value = SchedulerStartOutcome(
                SchedulerStartState.UNEXPECTED_ERROR,
                message="Run-control socket unavailable: [Errno 98] Address already in use",
            )
            result = cmd_scheduler(tmp_path / "config.toml")

        assert result == EXIT_ERROR
        assert result != EXIT_LOCK_ERROR
        mock_cls.return_value.start.assert_called_once()

    def test_unexpected_scheduler_start_exception_yields_exit_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DK_MODE", "cli")
        with patch("src.main.load_config"), patch("src.main.CronScheduler") as mock_cls:
            mock_cls.return_value.start.side_effect = RuntimeError("scheduler exploded")
            result = cmd_scheduler(tmp_path / "config.toml")

        assert result == EXIT_ERROR
        mock_cls.return_value.start.assert_called_once()

    def test_invalid_mode_logs_and_blocks_before_config_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DK_MODE", "bad")
        with (
            patch("src.main._log_invalid_dk_mode") as mock_log,
            patch("src.main.load_config") as mock_load,
            patch("src.main.CronScheduler") as mock_scheduler,
        ):
            result = cmd_scheduler(tmp_path / "config.toml")

        assert result == EXIT_ERROR
        mock_log.assert_called_once_with("bad")
        mock_load.assert_not_called()
        mock_scheduler.assert_not_called()


class TestRuntimeMode:
    def test_resolve_defaults_to_gui(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DK_MODE", raising=False)
        assert _resolve_dk_mode() == ("gui", None)

    def test_resolve_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DK_MODE", "cli")
        assert _resolve_dk_mode() == ("cli", None)

    def test_invalid_mode_falls_back_to_gui(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DK_MODE", "bad")
        assert _resolve_dk_mode() == ("gui", "bad")


class TestCmdGui:
    def test_blocked_in_cli_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        args = MagicMock(host="127.0.0.1", port=8080)
        monkeypatch.setenv("DK_MODE", "cli")
        with patch("src.gui.app.create_app") as mock_create:
            result = cmd_gui(args, tmp_path / "config.toml")
        assert result == EXIT_ERROR
        mock_create.assert_not_called()

    def test_invalid_mode_logs_and_starts_gui(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args = MagicMock(host="127.0.0.1", port=8080)
        monkeypatch.setenv("DK_MODE", "bad")
        with (
            patch("src.main._log_invalid_dk_mode") as mock_log,
            patch("src.gui.app.create_app") as mock_create,
            patch("uvicorn.run") as mock_run,
        ):
            result = cmd_gui(args, tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        mock_log.assert_called_once_with("bad")
        mock_create.assert_called_once_with(tmp_path / "config.toml")
        mock_run.assert_called_once()

    def test_non_owner_command_ignores_invalid_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = MagicMock()
        config.jobs = {}
        monkeypatch.setenv("DK_MODE", "bad")
        with (
            patch("src.main._log_invalid_dk_mode") as mock_log,
            patch("src.main.load_config", return_value=config) as mock_load,
        ):
            result = cmd_validate_config(tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        mock_load.assert_called_once_with(tmp_path / "config.toml")
        mock_log.assert_not_called()


class TestCmdSchedulerStatus:
    def test_running_returns_success(self) -> None:
        status = MagicMock()
        status.state = "running"
        status.to_dict.return_value = {"state": "running"}
        with patch("src.main.SchedulerStatusReader") as mock_reader:
            mock_reader.return_value.read.return_value = status
            result = cmd_scheduler_status()
        assert result == EXIT_SUCCESS

    def test_stopped_returns_error(self) -> None:
        status = MagicMock()
        status.state = "stopped"
        status.to_dict.return_value = {"state": "stopped"}
        with patch("src.main.SchedulerStatusReader") as mock_reader:
            mock_reader.return_value.read.return_value = status
            result = cmd_scheduler_status()
        assert result == EXIT_ERROR

    def test_read_failure_returns_status_error(self) -> None:
        with patch("src.main.SchedulerStatusReader") as mock_reader:
            mock_reader.return_value.read.side_effect = ValueError("bad json")
            result = cmd_scheduler_status()
        assert result == EXIT_ERROR


class TestMain:
    def test_public_help_shows_current_grouped_commands_only(self) -> None:
        help_text = _build_parser("cli").format_help()

        assert "run " in help_text
        assert "config" in help_text
        assert "jobs" in help_text
        assert "schedule" in help_text
        assert "scheduler" in help_text
        assert "runs" in help_text
        assert "logs" in help_text
        assert "shell" in help_text
        assert "run-job" not in help_text
        assert "validate-config" not in help_text
        assert "check-repo" not in help_text
        assert "restic-unlock" not in help_text
        assert "dk jobs tasks JOB" in help_text
        assert "dk shell JOB.backup.NAME" in help_text

    def test_gui_mode_help_hides_blocked_commands(self) -> None:
        help_text = _build_parser("gui").format_help()

        assert "\n    config" in help_text
        assert "\n    shell" in help_text
        for blocked in ("run", "jobs", "schedule", "scheduler", "runs", "logs"):
            assert f"\n    {blocked}" not in help_text
        assert "dk config validate" in help_text

    @pytest.mark.parametrize(
        "argv",
        [
            ["dockkeep", "run-job", "my-job.backup.local"],
            ["dockkeep", "validate-config"],
            ["dockkeep", "list-jobs"],
            ["dockkeep", "list-tasks", "my-job"],
            ["dockkeep", "list-workflows", "my-job"],
            ["dockkeep", "next-runs"],
            ["dockkeep", "scheduler-status"],
            ["dockkeep", "list-runs"],
            ["dockkeep", "cancel-run", "run-1"],
            ["dockkeep", "show-logs", "my-job"],
            ["dockkeep", "tail-logs", "my-job"],
            ["dockkeep", "check-repo", "my-job", "--backup", "local"],
            ["dockkeep", "restic-unlock", "my-job", "--backup", "local"],
            ["dockkeep", "gui"],
        ],
    )
    def test_removed_legacy_commands_are_unknown(self, argv: list[str]) -> None:
        with patch("sys.argv", argv), pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0

    def test_run_dispatched(self) -> None:
        with (
            patch("src.main.cmd_run_job", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "run", "my-job.backup.local"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()

    def test_run_without_target_reaches_handler_for_contextual_help(self) -> None:
        with (
            patch("src.main.cmd_run_job", return_value=2) as mock_cmd,
            patch("sys.argv", ["dockkeep", "run"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 2
        called_args = mock_cmd.call_args.args[0]
        assert called_args.task_selector is None

    def test_shell_without_target_reaches_handler_for_overview(self) -> None:
        with (
            patch("src.main.cmd_shell", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "shell"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.task_selector is None

    def test_config_validate_dispatched(self) -> None:
        with (
            patch("src.main.cmd_validate_config", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "config", "validate"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()

    def test_jobs_list_dispatched(self) -> None:
        with (
            patch("src.main.cmd_list_jobs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "list"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()

    def test_jobs_tasks_dispatched(self) -> None:
        with (
            patch("src.main.cmd_list_tasks", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "tasks", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name == "my-job"

    def test_jobs_tasks_without_job_reaches_handler_for_contextual_help(self) -> None:
        with (
            patch("src.main.cmd_list_tasks", return_value=2) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "tasks"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 2
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name is None

    def test_jobs_workflows_dispatched(self) -> None:
        with (
            patch("src.main.cmd_list_workflows", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "workflows", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name == "my-job"

    def test_jobs_workflows_without_job_reaches_handler_for_contextual_help(self) -> None:
        with (
            patch("src.main.cmd_list_workflows", return_value=2) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "workflows"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 2
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name is None

    def test_schedule_next_dispatched(self) -> None:
        with (
            patch("src.main.cmd_next_runs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "schedule", "next", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name == "my-job"

    def test_scheduler_status_group_dispatched(self) -> None:
        with (
            patch("src.main.cmd_scheduler_status", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "scheduler", "status"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()

    def test_runs_list_dispatched(self) -> None:
        with (
            patch("src.main.cmd_list_runs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "runs", "list"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()

    def test_runs_cancel_dispatched(self) -> None:
        with (
            patch("src.main.cmd_cancel_run", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "runs", "cancel", "run-1"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.run_id == "run-1"

    def test_logs_show_dispatched(self) -> None:
        with (
            patch("src.main.cmd_show_logs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "logs", "show", "my-job", "--tail", "20"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name == "my-job"
        assert called_args.tail == 20

    def test_logs_tail_dispatched(self) -> None:
        with (
            patch("src.main.cmd_tail_logs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "logs", "tail", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args = mock_cmd.call_args.args[0]
        assert called_args.job_name == "my-job"

    def test_bare_scheduler_requires_subcommand(self) -> None:
        with (
            patch("sys.argv", ["dockkeep", "scheduler"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0

    def test_web_command_is_unknown(self) -> None:
        with (
            patch("sys.argv", ["dockkeep", "web"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0

    def test_custom_config_path_passed(self) -> None:
        with (
            patch("src.main.cmd_run_job", return_value=0) as mock_cmd,
            patch(
                "sys.argv",
                [
                    "dockkeep",
                    "--config",
                    "/custom/path.toml",
                    "run",
                    "my-job.backup.local",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            main()

        _, called_path = mock_cmd.call_args.args
        assert called_path == Path("/custom/path.toml")

    def test_missing_command_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("sys.argv", ["dockkeep"]), pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == EXIT_SUCCESS
        assert "usage: dk" in capsys.readouterr().out


_CommandFn = Callable[[argparse.Namespace, Path], int]


def _args_with(**attrs: object) -> MagicMock:
    args = MagicMock()
    for key, value in attrs.items():
        setattr(args, key, value)
    return args


_CONFIG_LOADING_COMMANDS = [
    pytest.param(cmd_list_jobs, _args_with(), id="list_jobs"),
    pytest.param(cmd_list_workflows, _args_with(job_name="my-job"), id="list_workflows"),
    pytest.param(cmd_list_tasks, _args_with(job_name="my-job"), id="list_tasks"),
    pytest.param(cmd_next_runs, _args_with(job_name=None), id="next_runs"),
]

_JOB_LOOKUP_COMMANDS = [
    pytest.param(cmd_list_workflows, _args_with(job_name="missing"), id="list_workflows"),
    pytest.param(cmd_list_tasks, _args_with(job_name="missing"), id="list_tasks"),
    pytest.param(cmd_next_runs, _args_with(job_name="missing"), id="next_runs"),
]


class TestGuiModeCommandGuard:
    """Modusgrenze: Im GUI-Modus sind nur shell und config validate erlaubt."""

    @pytest.mark.parametrize("dk_mode", [None, "", "bogus"])
    @pytest.mark.parametrize(
        "argv",
        [
            ["dockkeep", "run", "my-job.backup.local"],
            ["dockkeep", "jobs", "list"],
            ["dockkeep", "jobs", "tasks", "my-job"],
            ["dockkeep", "jobs", "workflows", "my-job"],
            ["dockkeep", "schedule", "next"],
            ["dockkeep", "scheduler", "status"],
            ["dockkeep", "runs", "list"],
            ["dockkeep", "runs", "cancel", "run-1"],
            ["dockkeep", "logs", "show", "my-job"],
            ["dockkeep", "logs", "tail", "my-job"],
        ],
    )
    def test_blocked_before_config_load_in_gui_mode(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str], dk_mode: str | None
    ) -> None:
        if dk_mode is None:
            monkeypatch.delenv("DK_MODE", raising=False)
        else:
            monkeypatch.setenv("DK_MODE", dk_mode)

        with (
            patch("src.main.load_config") as mock_load,
            patch("sys.argv", argv),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == EXIT_ERROR
        mock_load.assert_not_called()

    @pytest.mark.parametrize(
        "argv",
        [
            ["dockkeep", "run"],
            ["dockkeep", "run", "--help"],
            ["dockkeep", "jobs"],
            ["dockkeep", "schedule"],
            ["dockkeep", "scheduler"],
            ["dockkeep", "runs"],
            ["dockkeep", "logs"],
            ["dockkeep", "--config", "/tmp/config.toml", "run", "--help"],
        ],
    )
    def test_blocked_groups_do_not_escape_through_argparse(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("DK_MODE", "gui")

        with patch("sys.argv", argv), pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == EXIT_ERROR
        output = capsys.readouterr()
        assert "Dockkeep runs in GUI mode" in output.out
        assert "usage:" not in output.err

    @pytest.mark.parametrize("dk_mode", [None, "gui", "cli"])
    def test_bare_command_prints_mode_help(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dk_mode: str | None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        if dk_mode is None:
            monkeypatch.delenv("DK_MODE", raising=False)
        else:
            monkeypatch.setenv("DK_MODE", dk_mode)

        with patch("sys.argv", ["dockkeep"]), pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == EXIT_SUCCESS
        output = capsys.readouterr()
        assert "usage: dk" in output.out

    @pytest.mark.parametrize("dk_mode", [None, "gui", "cli"])
    def test_shell_allowed_in_both_modes(
        self, monkeypatch: pytest.MonkeyPatch, dk_mode: str | None
    ) -> None:
        if dk_mode is None:
            monkeypatch.delenv("DK_MODE", raising=False)
        else:
            monkeypatch.setenv("DK_MODE", dk_mode)

        with (
            patch("src.main.cmd_shell", return_value=EXIT_SUCCESS) as mock_cmd,
            patch("sys.argv", ["dockkeep", "shell"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == EXIT_SUCCESS
        mock_cmd.assert_called_once()

    @pytest.mark.parametrize("dk_mode", [None, "gui", "cli"])
    def test_config_validate_allowed_in_both_modes(
        self, monkeypatch: pytest.MonkeyPatch, dk_mode: str | None
    ) -> None:
        if dk_mode is None:
            monkeypatch.delenv("DK_MODE", raising=False)
        else:
            monkeypatch.setenv("DK_MODE", dk_mode)

        with (
            patch("src.main.cmd_validate_config", return_value=EXIT_SUCCESS) as mock_cmd,
            patch("sys.argv", ["dockkeep", "config", "validate"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == EXIT_SUCCESS
        mock_cmd.assert_called_once()


class TestSharedConfigErrorPaths:
    """Exit-Code-Contract der wiederkehrenden Fehlerpfade, einmal parametrisiert.

    Ersetzt die pro Command-Klasse kopierten test_config_not_found/test_config_error/
    test_job_not_found/test_backup_not_found ohne Verlust an Exit-Code-Abdeckung.
    """

    @pytest.mark.parametrize(("command", "args"), _CONFIG_LOADING_COMMANDS)
    def test_config_not_found(self, command: _CommandFn, args: MagicMock, tmp_path: Path) -> None:
        result = command(args, tmp_path / "nonexistent.toml")
        assert result == EXIT_CONFIG_ERROR

    @pytest.mark.parametrize(("command", "args"), _CONFIG_LOADING_COMMANDS)
    def test_config_load_error(self, command: _CommandFn, args: MagicMock, tmp_path: Path) -> None:
        with patch("src.main.load_config", side_effect=ValueError("bad")):
            result = command(args, tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR

    @pytest.mark.parametrize(("command", "args"), _JOB_LOOKUP_COMMANDS)
    def test_job_not_found(self, command: _CommandFn, args: MagicMock, tmp_path: Path) -> None:
        with patch("src.main.load_config", return_value=MagicMock(jobs={})):
            result = command(args, tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR


class TestCmdValidateConfig:
    def test_config_not_found(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = cmd_validate_config(tmp_path / "nonexistent.toml")
        assert result == EXIT_CONFIG_ERROR
        assert "✗" in capsys.readouterr().out

    def test_invalid_toml(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = tmp_path / "bad.toml"
        cfg.write_text("not = valid [[[")
        result = cmd_validate_config(cfg)
        assert result == EXIT_CONFIG_ERROR
        assert "✗" in capsys.readouterr().out

    def test_validation_error_groups_by_job(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pydantic import ValidationError

        with patch(
            "src.main.load_config",
            side_effect=ValidationError.from_exception_data("m", []),
        ):
            result = cmd_validate_config(tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR
        assert "✗" in capsys.readouterr().out

    def test_value_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("src.main.load_config", side_effect=ValueError("bad config")):
            result = cmd_validate_config(tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR
        out = capsys.readouterr().out
        assert "✗" in out
        assert "bad config" in out

    def test_success_prints_job_and_workflow_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf_mock = MagicMock()
        backup_a = MagicMock()
        backup_b = MagicMock()
        job_mock = MagicMock(
            workflows={"full-backup": wf_mock},
            backup={"local": backup_a, "target": backup_b},
        )
        config_mock = MagicMock(
            jobs={"job-a": job_mock, "job-b": MagicMock(workflows={}, backup={})}
        )
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_validate_config(tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "✓" in out
        assert "2 jobs" in out
        assert "1 workflow" in out
        assert "2 backups" in out

    def test_success_shows_cron_message_when_workflows_exist(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job_mock = MagicMock(workflows={"wf": MagicMock()}, backup={})
        config_mock = MagicMock(jobs={"job-a": job_mock})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_validate_config(tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "cron" in capsys.readouterr().out.lower()

    def test_success_no_cron_message_without_workflows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_mock = MagicMock(jobs={"job-a": MagicMock(workflows={}, backup={})})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_validate_config(tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "cron" not in capsys.readouterr().out.lower()


class TestCmdShell:
    def _args(self, task_selector: str | None) -> MagicMock:
        args = MagicMock()
        args.task_selector = task_selector
        return args

    def test_missing_target_lists_all_shell_targets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backup = MagicMock(
            repository="/backups/local",
            credentials=MagicMock(
                password_env="RESTIC_PASSWORD",
                password_file=None,
                password=None,
            ),
        )
        rclone = MagicMock(source="/backups/local", target="remote:bucket")
        config = MagicMock(
            jobs={
                "alpha": MagicMock(backup={"local": backup}, rclone={"offsite": rclone}),
                "empty": MagicMock(backup={}, rclone={}),
            }
        )

        with (
            patch("src.main.load_config", return_value=config),
            patch("src.main._spawn_shell") as spawn_shell,
        ):
            result = cmd_shell(self._args(None), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        spawn_shell.assert_not_called()
        out = capsys.readouterr().out
        assert "Available tasks for 'alpha':" in out
        assert "dk shell alpha.backup.local" in out
        assert "/backups/local" in out
        assert "password_env: RESTIC_PASSWORD" in out
        assert "dk shell alpha.rclone.offsite" in out
        assert "/backups/local → remote:bucket" in out
        assert "empty" not in out

    def test_missing_target_without_any_targets_prints_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = MagicMock(jobs={"empty": MagicMock(backup={}, rclone={})})
        with patch("src.main.load_config", return_value=config):
            result = cmd_shell(self._args(None), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        assert "No backups or rclone tasks configured." in capsys.readouterr().out


class TestCmdListJobs:
    def _args(self) -> MagicMock:
        return MagicMock()

    def _config(self, jobs: dict) -> MagicMock:
        return MagicMock(jobs=jobs)

    def test_no_jobs_prints_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("src.main.load_config", return_value=self._config({})):
            result = cmd_list_jobs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "No jobs" in capsys.readouterr().out

    def test_lists_all_jobs(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        job_a = MagicMock(backup={"local": MagicMock()}, rclone={}, workflows={})
        job_b = MagicMock(backup={}, rclone={}, workflows={})
        with patch("src.main.load_config", return_value=self._config({"a": job_a, "b": job_b})):
            result = cmd_list_jobs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "a" in out
        assert "b" in out

    def test_omits_raw_job_sources(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job = MagicMock(backup={}, rclone={}, workflows={})
        with patch("src.main.load_config", return_value=self._config({"myjob": job})):
            result = cmd_list_jobs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "Sources:" not in capsys.readouterr().out

    def test_shows_workflows_with_schedule(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf = MagicMock(schedule="0 2 * * *")
        job = MagicMock(backup={}, rclone={}, workflows={"full": wf})
        with patch("src.main.load_config", return_value=self._config({"myjob": job})):
            result = cmd_list_jobs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "full (0 2 * * *)" in out

    def test_shows_none_when_no_workflows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job = MagicMock(backup={}, rclone={}, workflows={})
        with patch("src.main.load_config", return_value=self._config({"myjob": job})):
            result = cmd_list_jobs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "(none)" in capsys.readouterr().out

    def test_main_dispatches_list_jobs(self) -> None:
        with (
            patch("src.main.cmd_list_jobs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "list"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()


class TestCmdListWorkflows:
    def _args(self, job_name: str | None) -> MagicMock:
        args = MagicMock()
        args.job_name = job_name
        return args

    def test_missing_job_lists_available_jobs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "src.main.load_config",
            return_value=MagicMock(jobs={"alpha": MagicMock(), "beta": MagicMock()}),
        ):
            result = cmd_list_workflows(self._args(None), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "Missing JOB" in out
        assert "dk jobs workflows JOB" in out
        assert "Available jobs:" in out
        assert "alpha" in out
        assert "beta" in out

    def test_no_workflows_prints_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job_mock = MagicMock(workflows={})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_workflows(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "No workflows" in capsys.readouterr().out

    def test_lists_workflow_details(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf = MagicMock(
            schedule="0 2 * * *",
            steps=["backup.local", "backup.local.retention", "rclone.offsite"],
        )
        job_mock = MagicMock(workflows={"full-backup": wf})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_workflows(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "full-backup" in out
        assert "0 2 * * *" in out
        assert "scheduled" in out
        assert "backup.local → backup.local.retention → rclone.offsite" in out
        assert "Next run:" in out

    def test_manual_workflow_shows_manual(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf = MagicMock(schedule=None, steps=["backup.local"])
        job_mock = MagicMock(workflows={"nightly": wf})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_workflows(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "manual" in out
        assert "disabled" not in out

    def test_empty_schedule_shows_manual_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf = MagicMock(schedule="", steps=["backup.local"])
        job_mock = MagicMock(workflows={"manual": wf})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_workflows(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "manual only" in capsys.readouterr().out

    def test_main_dispatches_list_workflows(self) -> None:
        with (
            patch("src.main.cmd_list_workflows", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "workflows", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()


class TestCmdListTasks:
    def _args(self, job_name: str | None) -> MagicMock:
        args = MagicMock()
        args.job_name = job_name
        return args

    def test_missing_job_lists_available_jobs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "src.main.load_config",
            return_value=MagicMock(jobs={"alpha": MagicMock(), "beta": MagicMock()}),
        ):
            result = cmd_list_tasks(self._args(None), tmp_path / "config.toml")

        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "Missing JOB" in out
        assert "dk jobs tasks JOB" in out
        assert "Available jobs:" in out
        assert "alpha" in out
        assert "beta" in out

    def test_job_not_found_echoes_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("src.main.load_config", return_value=MagicMock(jobs={})):
            result = cmd_list_tasks(self._args("missing"), tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR
        assert "missing" in capsys.readouterr().out

    def test_no_tasks_prints_header(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        job_mock = MagicMock(backup={}, rclone={})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_tasks(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "Tasks for job" in capsys.readouterr().out

    def test_lists_backup_details_with_schedule(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backup_mock = MagicMock(repository="/backups/local", schedule="0 2 * * *")
        job_mock = MagicMock(backup={"local": backup_mock}, rclone={})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_tasks(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "local" in out
        assert "/backups/local" in out
        assert "0 2 * * *" in out
        assert "Next run:" in out

    def test_backup_without_schedule_shows_manual_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backup_mock = MagicMock(repository="/backups/local", schedule=None)
        job_mock = MagicMock(backup={"local": backup_mock}, rclone={})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_tasks(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "manual only" in capsys.readouterr().out

    def test_lists_named_rclone_tasks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rclone_mock = MagicMock(
            source="/data/docs",
            target="remote:docs",
            schedule="0 3 * * *",
        )
        job_mock = MagicMock(backup={}, rclone={"offsite": rclone_mock})
        with patch("src.main.load_config", return_value=MagicMock(jobs={"my-job": job_mock})):
            result = cmd_list_tasks(self._args("my-job"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "Rclone tasks:" in out
        assert "offsite" in out
        assert "/data/docs" in out
        assert "remote:docs" in out
        assert "0 3 * * *" in out
        assert "Next run:" in out

    def test_main_dispatches_list_tasks(self) -> None:
        with (
            patch("src.main.cmd_list_tasks", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "jobs", "tasks", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()


class TestCmdNextRuns:
    def _args(self, job_name: str | None = None) -> MagicMock:
        args = MagicMock()
        args.job_name = job_name
        return args

    def test_no_scheduled_runs(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        backup_mock = MagicMock(schedule=None)
        job_mock = MagicMock(backup={"local": backup_mock}, workflows={}, rclone={})
        config_mock = MagicMock(jobs={"my-job": job_mock})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        assert "No scheduled runs" in capsys.readouterr().out

    def test_shows_backups_with_schedule(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backup_mock = MagicMock(schedule="0 2 * * *")
        job_mock = MagicMock(backup={"local": backup_mock}, workflows={}, rclone={})
        config_mock = MagicMock(jobs={"my-job": job_mock})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "my-job.backup.local" in out
        assert "(backup)" in out

    def test_shows_workflows_with_schedule(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf_mock = MagicMock(schedule="0 3 * * *")
        job_mock = MagicMock(backup={}, workflows={"daily": wf_mock}, rclone={})
        config_mock = MagicMock(jobs={"my-job": job_mock})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "my-job.workflow.daily" in out
        assert "(workflow)" in out

    def test_filters_by_job_name(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        backup_a = MagicMock(schedule="0 2 * * *")
        backup_b = MagicMock(schedule="0 3 * * *")
        job_a = MagicMock(backup={"local": backup_a}, workflows={}, rclone={})
        job_b = MagicMock(backup={"target": backup_b}, workflows={}, rclone={})
        config_mock = MagicMock(jobs={"job-a": job_a, "job-b": job_b})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(job_name="job-a"), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "job-a.backup.local" in out
        assert "job-b" not in out

    def test_job_not_found_with_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_mock = MagicMock(jobs={})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(job_name="nonexistent"), tmp_path / "config.toml")
        assert result == EXIT_CONFIG_ERROR
        assert "nonexistent" in capsys.readouterr().out

    def test_sorted_by_next_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # backup at 3am comes after backup at 2am
        backup_early = MagicMock(schedule="0 2 * * *")
        backup_late = MagicMock(schedule="0 3 * * *")
        job_mock = MagicMock(
            backup={"late": backup_late, "early": backup_early}, workflows={}, rclone={}
        )
        config_mock = MagicMock(jobs={"my-job": job_mock})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        early_pos = out.find("early")
        late_pos = out.find("late")
        assert early_pos < late_pos

    def test_workflow_without_schedule_is_not_shown(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wf_mock = MagicMock(schedule=None)
        job_mock = MagicMock(backup={}, workflows={"nightly": wf_mock}, rclone={})
        config_mock = MagicMock(jobs={"my-job": job_mock})
        with patch("src.main.load_config", return_value=config_mock):
            result = cmd_next_runs(self._args(), tmp_path / "config.toml")
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "No scheduled runs" in out
        assert "[disabled]" not in out

    def test_invalid_runtime_schedule_returns_config_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backup_mock = MagicMock(schedule="0 0 30 2 *")
        job_mock = MagicMock(backup={"local": backup_mock}, workflows={}, rclone={})
        config_mock = MagicMock(jobs={"my-job": job_mock})
        with (
            patch("src.main.load_config", return_value=config_mock),
            patch("src.main.next_run_datetime", return_value=None),
        ):
            result = cmd_next_runs(self._args(), tmp_path / "config.toml")

        assert result == EXIT_CONFIG_ERROR
        assert "Invalid schedule for my-job.backup.local" in capsys.readouterr().out

    def test_main_dispatches_next_runs(self) -> None:
        with (
            patch("src.main.cmd_next_runs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "schedule", "next"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()


class TestRunJobDryRunFlag:
    def test_dry_run_flag_parsed_from_cli(self) -> None:
        """run --dry-run setzt args.dry_run=True."""
        with (
            patch("src.main.cmd_run_job", return_value=0) as mock_cmd,
            patch(
                "sys.argv",
                ["dockkeep", "run", "my-job.backup.local", "--dry-run"],
            ),
            pytest.raises(SystemExit),
        ):
            main()
        called_args = mock_cmd.call_args.args[0]
        assert called_args.dry_run is True

    def test_dry_run_flag_absent_by_default(self) -> None:
        """run ohne --dry-run setzt args.dry_run=False."""
        with (
            patch("src.main.cmd_run_job", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "run", "my-job.backup.local"]),
            pytest.raises(SystemExit),
        ):
            main()
        called_args = mock_cmd.call_args.args[0]
        assert called_args.dry_run is False


def _patch_run_control_client(
    *,
    list_runs_return: object = None,
    list_runs_side_effect: BaseException | None = None,
    cancel_run_return: object = None,
    cancel_run_side_effect: BaseException | None = None,
) -> object:
    """Build a patch context for RunControlClient with async stub methods."""
    client = MagicMock()
    client.list_runs = AsyncMock(return_value=list_runs_return, side_effect=list_runs_side_effect)
    client.cancel_run = AsyncMock(
        return_value=cancel_run_return, side_effect=cancel_run_side_effect
    )
    return patch("src.main.RunControlClient", return_value=client)


class TestCmdListRuns:
    def test_prints_structured_run_targets_and_returns_success(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs = [
            {
                "run_id": "run-1",
                "origin": "scheduler",
                "run_kind": "job_task",
                "job": "myjob",
                "task_type": "backup",
                "task_name": "daily",
                "status": "running",
                "started_at": "2026-06-02T02:00:00+00:00",
                "finished_at": None,
            },
            {
                "run_id": "run-2",
                "origin": "scheduler",
                "run_kind": "restore",
                "job": "myjob",
                "task_type": "restore",
                "task_name": "local",
                "status": "success",
                "started_at": "2026-06-01T02:00:00+00:00",
                "finished_at": "2026-06-01T02:05:00+00:00",
            },
        ]
        with _patch_run_control_client(list_runs_return=runs):
            result = cmd_list_runs(MagicMock())
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "TASK" in out
        assert "TARGET" not in out
        assert "run-1" in out
        assert "run-2" in out
        assert "scheduler" in out
        assert "scheduler" in out
        assert "myjob.backup.daily" in out
        assert "myjob.restore.local" in out
        assert "running" in out
        assert "success" in out

    def test_missing_structured_target_fields_prints_placeholder(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs = [
            {
                "run_id": "run-1",
                "origin": "scheduler",
                "target": "legacy.target",
                "status": "running",
            }
        ]
        with _patch_run_control_client(list_runs_return=runs):
            result = cmd_list_runs(MagicMock())
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "legacy.target" not in out
        assert "run-1  scheduler  -  running" in out

    def test_empty_list_prints_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _patch_run_control_client(list_runs_return=[]):
            result = cmd_list_runs(MagicMock())
        assert result == EXIT_SUCCESS
        assert "No scheduler runs" in capsys.readouterr().out

    def test_scheduler_unreachable_returns_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = ServiceError("scheduler_unreachable", "Scheduler runtime is not reachable")
        with _patch_run_control_client(list_runs_side_effect=err):
            result = cmd_list_runs(MagicMock())
        assert result == EXIT_ERROR
        assert "No scheduler runtime reachable" in capsys.readouterr().err


class TestCmdCancelRun:
    def _args(self, run_id: str) -> MagicMock:
        args = MagicMock()
        args.run_id = run_id
        return args

    def test_active_and_cancellable_prints_cancellation_requested(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Aktiver, abbrechbarer Run: Ausgabe meldet ehrlich nur die angeforderte Cancellation."""
        record = {"run_id": "run-1", "status": "running", "cancellable": True}
        with _patch_run_control_client(cancel_run_return=record):
            result = cmd_cancel_run(self._args("run-1"))
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "run-1" in out
        assert "Cancellation requested" in out
        assert "status: running" in out

    def test_active_but_not_cancellable_prints_post_processing_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Aktiver, nicht mehr abbrechbarer Run (Nacharbeiten): keine falsche Cancel-Aussage."""
        record = {"run_id": "run-1", "status": "running", "cancellable": False}
        with _patch_run_control_client(cancel_run_return=record):
            result = cmd_cancel_run(self._args("run-1"))
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "run-1" in out
        assert "no longer cancellable" in out
        assert "post-processing" in out
        assert "status: running" in out

    def test_terminal_run_prints_already_finished_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Terminaler Run: idempotenter cancel() liefert unveraendertes Record statt 'cancelled'."""
        record = {"run_id": "run-1", "status": "success", "cancellable": False}
        with _patch_run_control_client(cancel_run_return=record):
            result = cmd_cancel_run(self._args("run-1"))
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "run-1" in out
        assert "already finished" in out
        assert "nothing to cancel" in out
        assert "status: success" in out

    def test_unknown_run_returns_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = NotFoundServiceError("no such run", code="run_not_found")
        with _patch_run_control_client(cancel_run_side_effect=err):
            result = cmd_cancel_run(self._args("missing"))
        assert result == EXIT_ERROR
        assert "Run not found: missing" in capsys.readouterr().err

    def test_scheduler_unreachable_returns_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = ServiceError("scheduler_unreachable", "Scheduler runtime is not reachable")
        with _patch_run_control_client(cancel_run_side_effect=err):
            result = cmd_cancel_run(self._args("run-1"))
        assert result == EXIT_ERROR
        assert "No scheduler runtime reachable" in capsys.readouterr().err


class TestMainRunControlDispatch:
    def test_list_runs_dispatched(self) -> None:
        with (
            patch("src.main.cmd_list_runs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "runs", "list"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()

    def test_cancel_run_dispatched(self) -> None:
        with (
            patch("src.main.cmd_cancel_run", return_value=0) as mock_cmd,
            patch("sys.argv", ["dockkeep", "runs", "cancel", "run-1"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()
        called_args = mock_cmd.call_args.args[0]
        assert called_args.run_id == "run-1"

    def test_cancel_run_requires_run_id(self) -> None:
        with (
            patch("sys.argv", ["dockkeep", "runs", "cancel"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code != 0
