"""Integration-Tests: Zusammenspiel von Config-Loading, Validation und Executors.

Diese Tests verwenden echte TOML-Dokumente (keine Mock-Config-Objekte) und prüfen,
ob die gesamte Kette – Laden, Validieren, Dispatchen, Ausführen – korrekt zusammenspielt.
Subprocess-Aufrufe (restic, rclone) werden gemockt, damit keine echten Tools nötig sind.
"""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.job_runner import JobRunner
from src.core.subprocesses import CommandResult
from src.main import (
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    cmd_list_jobs,
    cmd_list_workflows,
    cmd_run_job,
    cmd_validate_config,
)
from src.utils.validation import load_config, load_raw_config


@pytest.fixture(autouse=True)
def set_restic_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setzt RESTIC_PASSWORD für Configs, die password_env referenzieren."""
    monkeypatch.setenv("RESTIC_PASSWORD", "test-secret")
    monkeypatch.setenv("DB_BACKUP_PASSWORD", "db-test-secret")


# ---------------------------------------------------------------------------
# Fixtures: minimale TOML-Konfigurationen
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_toml(tmp_path: Path) -> Path:
    """Minimale gültige TOML-Config mit einem lokalen Job."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[global]
log_level = "info"

[global.backup]
password_env = "RESTIC_PASSWORD"

[jobs.myjob.backup.local]
repository = "/backups/myjob"
sources = ["/data"]
schedule = "0 2 * * *"
auto_init = true
retention = true
cleanup = true
keep_daily = 7
keep_weekly = 4
extra_restic_prune_args = ["--max-unused 5%"]

[jobs.myjob.workflow.daily]
schedule = "0 2 * * *"
steps = ["backup.local"]

[jobs.myjob.workflow.snapshot]
schedule = ""
steps = ["backup.local.backup"]
""",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture()
def hybrid_toml(tmp_path: Path) -> Path:
    """TOML-Config mit lokalem + Cloud-Backup und rclone-Step."""
    cfg = tmp_path / "hybrid.toml"
    cfg.write_text(
        """
[global]
log_level = "debug"

[global.backup]
password_env = "RESTIC_PASSWORD"
keep_daily = 7
keep_weekly = 4
keep_monthly = 6
exclude_caches = true
one_file_system = false

[global.rclone]
transfers = 4
checkers = 8

[jobs.photos.backup]
auto_init = true

[jobs.photos.backup.local]
repository = "/backups/photos"
sources = ["/data/photos", "/data/videos"]
retention = true
cleanup = true
keep_daily = 14
keep_weekly = 8
tags = ["photos"]
exclude = ["*.tmp"]
extra_restic_prune_args = ["--max-unused 10%"]

[jobs.photos.backup.remote]
repository = "rclone:gdrive:backups/photos"
sources = ["/data/photos", "/data/videos"]
retention = true
keep_daily = 14
keep_weekly = 8

[jobs.photos.rclone.offsite]
source = "/backups/photos"
target = "gdrive:backups/photos"
sync_delete = false

[jobs.photos.workflow.nightly]
schedule = "0 3 * * *"
steps = ["backup.local", "backup.remote", "rclone.offsite"]

[jobs.disabled-job.backup.local]
repository = "/backups/archive"
sources = ["/data/archive"]
keep_yearly = 5
""",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture()
def multi_job_toml(tmp_path: Path) -> Path:
    """Config mit mehreren Jobs, verschiedenen Targets und Passwort-Varianten."""
    cfg = tmp_path / "multi.toml"
    cfg.write_text(
        """
[global]
log_level = "info"

[global.backup]
password_env = "RESTIC_PASSWORD"
keep_daily = 7
keep_weekly = 4
keep_monthly = 6
keep_yearly = 1
exclude_caches = true
one_file_system = false

[jobs.docs.backup]
retention = true

[jobs.docs.backup.local]
repository = "/backups/docs"
sources = ["/data/docs"]

[jobs.docs.workflow.daily]
schedule = "0 1 * * *"
steps = ["backup.local"]

[jobs.db.backup.remote]
repository = "rclone:s3:bucket/db"
sources = ["/data/db"]
password_env = "DB_BACKUP_PASSWORD"
retention = true
keep_hourly = 24
keep_daily = 7

[jobs.db.workflow.hourly]
schedule = "0 * * * *"
steps = ["backup.remote"]
""",
        encoding="utf-8",
    )
    return cfg


# ---------------------------------------------------------------------------
# Config-Loading Integration
# ---------------------------------------------------------------------------


class TestConfigLoadingIntegration:
    def test_minimal_config_loads_successfully(self, minimal_toml: Path) -> None:
        config = load_raw_config(minimal_toml)
        assert "myjob" in config.jobs
        job = config.jobs["myjob"]
        assert job.backup.tasks["local"].sources == ["/data"]
        assert job.backup.tasks["local"].repository == "/backups/myjob"

    def test_global_defaults_merged_into_backup(self, minimal_toml: Path) -> None:
        config = load_config(minimal_toml)
        backup = config.jobs["myjob"].backup["local"]
        assert backup.backend_options.restic.exclude_caches is None
        assert backup.backend_options.restic.one_file_system is None
        assert backup.retention.keep_daily == 7
        assert backup.retention.keep_weekly == 4
        assert backup.retention.keep_monthly is None
        assert backup.retention.keep_yearly is None

    def test_hybrid_config_loads_all_steps(self, hybrid_toml: Path) -> None:
        config = load_raw_config(hybrid_toml)
        job = config.jobs["photos"]
        assert sorted(job.backup.tasks) == ["local", "remote"]
        assert bool(job.rclone.tasks)

    def test_hybrid_config_has_two_repositories(self, hybrid_toml: Path) -> None:
        config = load_raw_config(hybrid_toml)
        backups = config.jobs["photos"].backup.tasks
        assert backups["local"].repository == "/backups/photos"
        assert backups["remote"].repository == "rclone:gdrive:backups/photos"

    def test_multi_job_config_loads(self, multi_job_toml: Path) -> None:
        config = load_raw_config(multi_job_toml)
        assert "docs" in config.jobs
        assert "db" in config.jobs

    def test_password_env_passed_through(self, multi_job_toml: Path) -> None:
        config = load_config(multi_job_toml)
        db = config.jobs["db"]
        assert db.backup["remote"].credentials.password == "db-test-secret"

    def test_all_jobs_in_config(self, hybrid_toml: Path) -> None:
        config = load_raw_config(hybrid_toml)
        assert "disabled-job" in config.jobs

    def test_workflow_schedules_preserved(self, minimal_toml: Path) -> None:
        config = load_raw_config(minimal_toml)
        workflows = config.jobs["myjob"].workflow
        assert "daily" in workflows
        assert "snapshot" in workflows
        assert workflows["daily"].schedule == "0 2 * * *"
        assert workflows["daily"].steps == ["backup.local"]
        assert workflows["snapshot"].schedule is None

    def test_rclone_defaults_merged(self, hybrid_toml: Path) -> None:
        config = load_config(hybrid_toml)
        rclone = config.jobs["photos"].rclone["offsite"]
        assert rclone.options.transfers == 4  # aus global.rclone
        assert rclone.options.checkers == 8  # aus global.rclone

    def test_extra_args_inheritance_from_toml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "extra-args.toml"
        cfg_path.write_text(
            """
[global]

[global.backup]
password_env = "RESTIC_PASSWORD"
extra_restic_backup_args = ["--verbose"]
extra_restic_forget_args = ["--keep-tag global"]
extra_restic_prune_args = ["--max-unused 5%"]

[global.rclone]
extra_rclone_args = ["--fast-list"]

[jobs.docs.backup]
extra_restic_backup_args = ["--host docs"]

[jobs.docs.rclone]
extra_rclone_args = ["--drive-chunk-size 64M"]

[jobs.docs.backup.inherited]
repository = "/backups/docs-inherited"
sources = ["/data/docs"]

[jobs.docs.backup.empty]
repository = "/backups/docs-empty"
sources = ["/data/docs"]
extra_restic_backup_args = []
extra_restic_forget_args = []
extra_restic_prune_args = []

[jobs.docs.rclone.offsite]
source = "/backups/docs"
target = "gdrive:backups/docs"
""",
            encoding="utf-8",
        )

        config = load_config(cfg_path)
        job = config.jobs["docs"]
        inherited = job.backup["inherited"]
        empty = job.backup["empty"]

        assert inherited.backend_options.restic.extra_backup_args == ["--host docs"]
        assert inherited.backend_options.restic.extra_forget_args == ["--keep-tag global"]
        assert inherited.backend_options.restic.extra_prune_args == ["--max-unused 5%"]
        assert empty.backend_options.restic.extra_backup_args == []
        assert empty.backend_options.restic.extra_forget_args == []
        assert empty.backend_options.restic.extra_prune_args == []
        assert bool(job.rclone)
        assert job.rclone["offsite"].options.extra_args == ["--drive-chunk-size 64M"]


# ---------------------------------------------------------------------------
# Validate-Config CLI Integration
# ---------------------------------------------------------------------------


class TestValidateConfigCLIIntegration:
    def test_minimal_config_validates_ok(
        self, minimal_toml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = cmd_validate_config(minimal_toml)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "✓" in out
        assert "1 job" in out

    def test_hybrid_config_validates_ok(
        self, hybrid_toml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = cmd_validate_config(hybrid_toml)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "✓" in out

    def test_multi_job_config_validates_ok(
        self, multi_job_toml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = cmd_validate_config(multi_job_toml)
        assert result == EXIT_SUCCESS

    def test_invalid_config_reports_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad_cfg = tmp_path / "bad.toml"
        bad_cfg.write_text(
            """
[global]

[jobs.broken.backup.local]
repository = "relative/repo"
sources = ["/data"]
""",
            encoding="utf-8",
        )
        result = cmd_validate_config(bad_cfg)
        assert result == EXIT_CONFIG_ERROR
        assert "✗" in capsys.readouterr().out

    def test_backup_without_keep_fields_fails_validation(self, tmp_path: Path) -> None:
        cfg = tmp_path / "no-keep.toml"
        cfg.write_text(
            """
[global]

[global.backup]
password_env = "RESTIC_PASSWORD"

[jobs.myjob.backup.local]
repository = "/backups/myjob"
sources = ["/data"]
retention = true
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="retention=true requires"):
            load_config(cfg)

    def test_example_full_toml_validates(self, capsys: pytest.CaptureFixture[str]) -> None:
        example_path = Path(__file__).parent.parent / "fixtures" / "example-full.toml"
        original_is_file = Path.is_file

        def example_file_exists(path: Path) -> bool:
            return str(path).startswith("/config/") or original_is_file(path)

        with patch("pathlib.Path.is_file", example_file_exists):
            result = cmd_validate_config(example_path)

        assert result == EXIT_SUCCESS


# ---------------------------------------------------------------------------
# List-Jobs CLI Integration
# ---------------------------------------------------------------------------


class TestListJobsCLIIntegration:
    def _args(self) -> MagicMock:
        return MagicMock()

    def test_lists_all_jobs(self, multi_job_toml: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = cmd_list_jobs(self._args(), multi_job_toml)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "docs" in out
        assert "db" in out

    def test_shows_workflow_schedules(
        self, minimal_toml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = cmd_list_jobs(self._args(), minimal_toml)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "daily" in out
        assert "0 2 * * *" in out


# ---------------------------------------------------------------------------
# List-Workflows CLI Integration
# ---------------------------------------------------------------------------


class TestListWorkflowsCLIIntegration:
    def _args(self, job_name: str) -> MagicMock:
        args = MagicMock()
        args.job_name = job_name
        return args

    def test_lists_workflows_with_next_run(
        self, minimal_toml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = cmd_list_workflows(self._args("myjob"), minimal_toml)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "daily" in out
        assert "snapshot" in out
        assert "Next run:" in out

    def test_manual_workflow_shown_as_manual(
        self, minimal_toml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = cmd_list_workflows(self._args("myjob"), minimal_toml)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "snapshot (manual)" in out
        assert "manual only" in out
        assert "disabled" not in out


# ---------------------------------------------------------------------------
# Dry-Run Integration
# ---------------------------------------------------------------------------


class TestDryRunIntegration:
    """Tests für dry_run=True durch die vollständige Ausführungskette.

    Subprocess-Aufrufe werden gemockt; es wird geprüft, dass --dry-run
    an restic/rclone übergeben wird.
    """

    def _make_runner(self, job_name: str, toml_path: Path, tmp_path: Path) -> JobRunner:
        config = load_config(toml_path)
        job_config = config.jobs[job_name]
        return JobRunner(
            job_name,
            job_config,
            lock_dir=tmp_path / "locks",
            log_level="debug",
            log_base_dir=tmp_path / "logs",
            dry_run=True,
        )

    def test_dry_run_backup_passes_flag_to_restic(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)
        completed = CommandResult(returncode=0, stdout="", stderr="")
        mock_run = AsyncMock(return_value=completed)
        with (
            patch("src.executors.base.stream_command", new=mock_run),
            patch("src.utils.restic.run_command", new=mock_run),
        ):
            result = asyncio.run(runner.run_backup("local"))
        assert result is True
        cmd_strs = [str(c) for c in mock_run.call_args_list]
        assert any("restic" in c for c in cmd_strs)
        assert any("--dry-run" in c for c in cmd_strs)

    def test_dry_run_workflow_all_steps_get_flag(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)
        completed = CommandResult(returncode=0, stdout="", stderr="")
        mock_run = AsyncMock(return_value=completed)
        with (
            patch("src.executors.base.stream_command", new=mock_run),
            patch("src.utils.restic.run_command", new=mock_run),
        ):
            result = asyncio.run(runner.run_workflow("daily"))
        assert result is True
        assert mock_run.call_count >= 3  # backup + forget + prune
        commands = [c.args[0] for c in mock_run.call_args_list if c.args]
        operational = [
            cmd
            for cmd in commands
            if isinstance(cmd, list) and any(step in cmd for step in ("backup", "forget", "prune"))
        ]
        assert operational
        assert all("--dry-run" in cmd for cmd in operational)

    def test_dry_run_backup_runs_auto_init_check(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)
        completed = CommandResult(returncode=0, stdout="", stderr="")
        mock_run = AsyncMock(return_value=completed)
        with (
            patch("src.executors.base.stream_command", new=mock_run),
            patch("src.utils.restic.run_command", new=mock_run),
        ):
            result = asyncio.run(runner.run_backup("local"))
        assert result is True
        # auto_init darf im Dry-Run das Repository vorbereiten.
        cat_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and isinstance(c.args[0], list) and "cat" in c.args[0]
        ]
        assert len(cat_calls) == 1

    def test_dry_run_rclone_passes_flag(self, hybrid_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("photos", hybrid_toml, tmp_path)
        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as mock_run,
            patch(
                "src.executors.rclone.async_check_remote", new_callable=AsyncMock, return_value=True
            ),
        ):
            result = asyncio.run(runner.run_step("rclone.offsite"))
        assert result is True
        cmd_strs = [str(c) for c in mock_run.call_args_list]
        assert any("rclone" in c for c in cmd_strs)
        assert any("--dry-run" in c for c in cmd_strs)

    def test_dry_run_unknown_job_returns_config_error(self, minimal_toml: Path) -> None:
        args = MagicMock()
        args.task_selector = "nonexistent.backup.local"
        args.dry_run = True
        result = cmd_run_job(args, minimal_toml)
        assert result == EXIT_CONFIG_ERROR


# ---------------------------------------------------------------------------
# JobRunner + Executor Integration
# ---------------------------------------------------------------------------


class TestJobRunnerIntegration:
    """Tests, die den JobRunner zusammen mit echten Executors testen.

    Subprocess-Aufrufe werden gemockt, um restic/rclone nicht wirklich zu starten.
    """

    def _make_runner(self, job_name: str, toml_path: Path, tmp_path: Path) -> JobRunner:
        config = load_config(toml_path)
        job_config = config.jobs[job_name]
        return JobRunner(
            job_name,
            job_config,
            lock_dir=tmp_path / "locks",
            log_level="debug",
            log_base_dir=tmp_path / "logs",
        )

    def test_backup_task_calls_restic(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as mock_run,
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            result = asyncio.run(runner.run_backup("local"))

        assert result is True
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("restic" in c for c in calls)

    def test_backup_task_failure_returns_false(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        completed = CommandResult(returncode=0, stdout="", stderr="")
        failed = CommandResult(returncode=1, stdout="", stderr="error")
        with (
            patch("src.executors.base.stream_command", new_callable=AsyncMock, return_value=failed),
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            result = asyncio.run(runner.run_backup("local"))

        assert result is False

    def test_workflow_executes_all_steps(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as mock_run,
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            result = asyncio.run(runner.run_workflow("daily"))

        assert result is True
        # backup, forget, prune → mindestens 3 restic-Aufrufe
        assert mock_run.call_count >= 3

    def test_workflow_aborts_on_step_failure(self, minimal_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        call_count = 0

        async def side_effect(*args: Any, **kwargs: Any) -> CommandResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return CommandResult(returncode=0, stdout="ok", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="fail")

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, side_effect=side_effect
            ),
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            result = asyncio.run(runner.run_workflow("daily"))

        assert result is False
        # Mehr als 1 Aufruf (backup OK), aber nicht alle 3 (Abbruch nach forget-Fehler)
        assert call_count < 5

    def test_manual_workflow_executes_when_called_directly(
        self, minimal_toml: Path, tmp_path: Path
    ) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as mock_run,
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            result = asyncio.run(runner.run_workflow("snapshot"))

        assert result is True
        mock_run.assert_called()

    def test_two_backups_each_call_restic_once(self, hybrid_toml: Path, tmp_path: Path) -> None:
        runner = self._make_runner("photos", hybrid_toml, tmp_path)

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as mock_run,
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            local_result = asyncio.run(runner.run_backup("local"))
            remote_result = asyncio.run(runner.run_backup("remote"))

        assert local_result is True
        assert remote_result is True
        backup_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and isinstance(c.args[0], list) and "backup" in c.args[0]
        ]
        assert len(backup_calls) == 2

    def test_auto_init_calls_restic_init_when_repo_missing(
        self, minimal_toml: Path, tmp_path: Path
    ) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        async def side_effect(*args: Any, **kwargs: Any) -> CommandResult:
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "cat" in cmd:
                return CommandResult(returncode=1, stdout="", stderr="")
            return CommandResult(returncode=0, stdout="ok", stderr="")

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as executor_run,
            patch(
                "src.utils.restic.run_command", new_callable=AsyncMock, side_effect=side_effect
            ) as restic_run,
        ):
            result = asyncio.run(runner.run_backup("local"))

        assert result is True
        cmd_strs = [str(c) for c in [*restic_run.call_args_list, *executor_run.call_args_list]]
        assert any("init" in c for c in cmd_strs)

    def test_backup_executes_backup_forget_prune_pipeline(
        self, minimal_toml: Path, tmp_path: Path
    ) -> None:
        runner = self._make_runner("myjob", minimal_toml, tmp_path)

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with (
            patch(
                "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
            ) as mock_run,
            patch("src.utils.restic.run_command", new_callable=AsyncMock, return_value=completed),
        ):
            result = asyncio.run(runner.run_backup("local"))

        assert result is True
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("backup" in c for c in calls)
        assert any("forget" in c for c in calls)
        assert any("prune" in c for c in calls)

    def test_backup_step_retention_runs_independently(
        self, minimal_toml: Path, tmp_path: Path
    ) -> None:
        config = load_config(minimal_toml)
        runner = JobRunner(
            "myjob",
            config.jobs["myjob"],
            lock_dir=tmp_path / "locks",
            log_base_dir=tmp_path / "logs",
        )

        completed = CommandResult(returncode=0, stdout="", stderr="")
        with patch(
            "src.executors.base.stream_command", new_callable=AsyncMock, return_value=completed
        ) as mock_run:
            result = asyncio.run(runner.run_step("backup.local.retention"))

        assert result is True
        assert any("forget" in str(c) for c in mock_run.call_args_list)
