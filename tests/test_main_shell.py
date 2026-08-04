from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.main import (
    EXIT_CONFIG_ERROR,
    EXIT_SUCCESS,
    _cmd_shell_backup,
    _cmd_shell_overview,
    _cmd_shell_rclone,
    _print_shell_banner,
    _shell_password_type,
    cmd_shell,
)
from src.models.resolved_config import (
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedInputConfig,
    ResolvedRcloneSyncTaskConfig,
)


class TestShellPasswordType:
    def test_password_env(self) -> None:
        backup = ResolvedBackupConfig(
            repository="/r",
            credentials=ResolvedCredentials(password="resolved", password_env="MY_VAR"),
        )
        assert _shell_password_type(backup) == "password_env: MY_VAR"

    def test_password_file(self) -> None:
        backup = ResolvedBackupConfig(
            repository="/r",
            credentials=ResolvedCredentials(password_file="/run/secrets/pw"),
        )
        assert _shell_password_type(backup) == "password_file"

    def test_direct_password(self) -> None:
        backup = ResolvedBackupConfig(
            repository="/r",
            credentials=ResolvedCredentials(password="secret"),
        )
        assert _shell_password_type(backup) == "password"

    def test_no_password(self) -> None:
        backup = ResolvedBackupConfig(repository="/r")
        assert _shell_password_type(backup) == "no password configured"


class TestPrintShellBanner:
    def test_outputs_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_shell_banner(" myjob / mybackup ", ["line one", "line two"])
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "myjob / mybackup" in captured.err
        assert "line one" in captured.err
        assert "Type 'exit' to leave" in captured.err

    def test_banner_structure(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_shell_banner(" job / backup ", ["content"])
        captured = capsys.readouterr()
        lines = captured.err.splitlines()
        assert lines[0].startswith("┌─")
        assert lines[1].startswith("│  content")
        assert lines[-1].startswith("└─")


class TestCmdShellOverview:
    def _make_config(self, job_name: str, job_config: MagicMock) -> MagicMock:
        config = MagicMock()
        config.jobs = {job_name: job_config}
        return config

    def test_unknown_job(self, capsys: pytest.CaptureFixture[str]) -> None:
        config = MagicMock()
        config.jobs = {}
        result = _cmd_shell_overview("ghost", config)
        assert result == EXIT_CONFIG_ERROR
        assert "ghost" in capsys.readouterr().out

    def test_job_with_no_backups_no_rclone(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = MagicMock(backup={}, rclone={})
        config = self._make_config("myjob", job)
        result = _cmd_shell_overview("myjob", config)
        assert result == EXIT_SUCCESS
        assert "No backups" in capsys.readouterr().out

    def test_lists_backups(self, capsys: pytest.CaptureFixture[str]) -> None:
        backup = ResolvedBackupConfig(
            repository="/backups/docs",
            credentials=ResolvedCredentials(password="s"),
        )
        job = MagicMock(
            backup={"docs": backup},
            rclone={},
        )
        config = self._make_config("home", job)
        result = _cmd_shell_overview("home", config)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "dk shell home.backup.docs" in out
        assert "/backups/docs" in out
        assert "(password)" in out

    def test_lists_rclone(self, capsys: pytest.CaptureFixture[str]) -> None:
        rclone_task = MagicMock(source="/data/docs", target="gdrive:backup")
        job = MagicMock(backup={}, rclone={"main": rclone_task})
        config = self._make_config("home", job)
        result = _cmd_shell_overview("home", config)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "dk shell home.rclone.main" in out
        assert "/data/docs" in out
        assert "gdrive:backup" in out

    def test_lists_backups_and_rclone(self, capsys: pytest.CaptureFixture[str]) -> None:
        backup = ResolvedBackupConfig(
            repository="/backups/docs",
            credentials=ResolvedCredentials(password="s"),
        )
        rclone_task = MagicMock(source="/data", target="remote:dest")
        job = MagicMock(backup={"docs": backup}, rclone={"main": rclone_task})
        config = self._make_config("home", job)
        result = _cmd_shell_overview("home", config)
        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "home.backup.docs" in out
        assert "home.rclone.main" in out


class TestCmdShellBackup:
    def _make_backup(self, **kwargs: object) -> ResolvedBackupConfig:
        repository = str(kwargs.pop("repository", "/backups/test"))
        password = kwargs.pop("password", "secret")
        password_file = kwargs.pop("password_file", None)
        sources = kwargs.pop("sources", [])
        return ResolvedBackupConfig(
            repository=repository,
            credentials=ResolvedCredentials(
                password=password if isinstance(password, str) else None,
                password_file=password_file if isinstance(password_file, str) else None,
            ),
            input=ResolvedInputConfig(sources=list(sources) if isinstance(sources, list) else []),
        )

    def test_sets_restic_repository_in_env(self) -> None:
        backup = self._make_backup(repository="/backups/myrepo")
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_backup("myjob", "mybackup", backup)
        env = mock_run.call_args[1]["env"]
        assert env["RESTIC_REPOSITORY"] == "/backups/myrepo"

    def test_sets_restic_password_in_env(self) -> None:
        backup = self._make_backup(password="mypassword")
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_backup("myjob", "mybackup", backup)
        env = mock_run.call_args[1]["env"]
        assert env["RESTIC_PASSWORD"] == "mypassword"

    def test_sets_restic_password_file_in_env(self, tmp_path: Path) -> None:
        pw_file = tmp_path / "pw"
        pw_file.write_text("secret")
        backup = ResolvedBackupConfig(
            repository="/backups/test",
            credentials=ResolvedCredentials(password_file=str(pw_file)),
        )
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_backup("myjob", "mybackup", backup)
        env = mock_run.call_args[1]["env"]
        assert env["RESTIC_PASSWORD_FILE"] == str(pw_file)
        assert "RESTIC_PASSWORD" not in env

    def test_returns_shell_exit_code(self) -> None:
        backup = self._make_backup()
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=42)
            result = _cmd_shell_backup("myjob", "mybackup", backup)
        assert result == 42

    def test_banner_printed_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        backup = self._make_backup(sources=["/data/docs", "/data/photos"])
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_backup("home", "docs", backup)
        err = capsys.readouterr().err
        assert "home / docs" in err
        assert "RESTIC_REPOSITORY" in err
        assert "restic snapshots" in err
        assert "/data/docs" in err
        assert "/data/photos" in err

    def test_banner_warns_when_password_env_configured_but_unresolved(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unresolved password_env must stay visible in the generated shell command."""
        backup = ResolvedBackupConfig(
            repository="/backups/test",
            credentials=ResolvedCredentials(password_env="RESTIC_PW_FROM_ENV"),
        )
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_backup("myjob", "mybackup", backup)
        env = mock_run.call_args[1]["env"]
        err = capsys.readouterr().err

        assert "RESTIC_PASSWORD" not in env
        assert "RESTIC_PASSWORD_FILE" not in env
        assert "WARNING" in err
        assert "RESTIC_PW_FROM_ENV" in err


class TestCmdShellRclone:
    def test_spawns_shell(self) -> None:
        rclone = ResolvedRcloneSyncTaskConfig(source="/data", target="remote:dest")
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _cmd_shell_rclone("myjob", "main", rclone)
        assert result == 0
        mock_run.assert_called_once()

    def test_banner_shows_source_and_remote(self, capsys: pytest.CaptureFixture[str]) -> None:
        rclone = ResolvedRcloneSyncTaskConfig(source="/data/docs", target="gdrive:backup")
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_rclone("home", "main", rclone)
        err = capsys.readouterr().err
        assert "/data/docs" in err
        assert "gdrive:backup" in err
        assert "rclone sync" in err

    def test_does_not_set_restic_vars(self) -> None:
        rclone = ResolvedRcloneSyncTaskConfig(source="/data", target="remote:dest")
        with patch("src.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _cmd_shell_rclone("myjob", "main", rclone)
        env = mock_run.call_args[1]["env"]
        assert "RESTIC_REPOSITORY" not in env
        assert "RESTIC_PASSWORD" not in env


class TestCmdShell:
    def _make_args(self, target: str) -> MagicMock:
        args = MagicMock()
        args.task_selector = target
        return args

    def test_config_file_not_found(self, tmp_path: Path) -> None:
        args = self._make_args("myjob")
        result = cmd_shell(args, tmp_path / "nonexistent.toml")
        assert result == EXIT_CONFIG_ERROR

    def test_overview_mode_dispatched(self) -> None:
        args = self._make_args("myjob")
        with (
            patch("src.main.load_config") as mock_load,
            patch("src.main._cmd_shell_overview") as mock_overview,
        ):
            mock_load.return_value = MagicMock(jobs={})
            mock_overview.return_value = EXIT_SUCCESS
            result = cmd_shell(args, Path("config.toml"))
        mock_overview.assert_called_once_with("myjob", mock_load.return_value)
        assert result == EXIT_SUCCESS

    def test_shell_mode_unknown_job(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = self._make_args("ghost.backup.local")
        with patch("src.main.load_config") as mock_load:
            mock_load.return_value = MagicMock(jobs={})
            result = cmd_shell(args, Path("config.toml"))
        assert result == EXIT_CONFIG_ERROR
        assert "ghost" in capsys.readouterr().out

    def test_shell_mode_unknown_backup(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = self._make_args("myjob.backup.missing")
        job = MagicMock(backup={}, rclone={})
        with patch("src.main.load_config") as mock_load:
            mock_load.return_value = MagicMock(jobs={"myjob": job})
            result = cmd_shell(args, Path("config.toml"))
        assert result == EXIT_CONFIG_ERROR
        assert "missing" in capsys.readouterr().out

    def test_shell_mode_rclone_dispatched(self) -> None:
        args = self._make_args("myjob.rclone.main")
        rclone = ResolvedRcloneSyncTaskConfig(source="/data", target="remote:dest")
        job = MagicMock(rclone={"main": rclone}, backup={})
        with (
            patch("src.main.load_config") as mock_load,
            patch("src.main._cmd_shell_rclone") as mock_rclone,
        ):
            mock_load.return_value = MagicMock(jobs={"myjob": job})
            mock_rclone.return_value = EXIT_SUCCESS
            result = cmd_shell(args, Path("config.toml"))
        mock_rclone.assert_called_once_with("myjob", "main", rclone)
        assert result == EXIT_SUCCESS

    def test_shell_mode_backup_dispatched(self) -> None:
        args = self._make_args("myjob.backup.mybackup")
        backup = ResolvedBackupConfig(
            repository="/backups",
            credentials=ResolvedCredentials(password="s"),
        )
        job = MagicMock(backup={"mybackup": backup}, rclone={})
        with (
            patch("src.main.load_config") as mock_load,
            patch("src.main._cmd_shell_backup") as mock_backup,
        ):
            mock_load.return_value = MagicMock(jobs={"myjob": job})
            mock_backup.return_value = EXIT_SUCCESS
            result = cmd_shell(args, Path("config.toml"))
        mock_backup.assert_called_once_with("myjob", "mybackup", backup)
        assert result == EXIT_SUCCESS
