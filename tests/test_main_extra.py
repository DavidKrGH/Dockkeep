from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.main import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    cmd_show_logs,
    cmd_tail_logs,
    main,
)
from src.scheduler.cron import SchedulerStartOutcome, SchedulerStartState


@pytest.fixture(autouse=True)
def _default_cli_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI-Handler-Tests laufen im CLI-Modus."""
    monkeypatch.setenv("DK_MODE", "cli")


class TestCmdShowLogs:
    def _args(self, job_name: str, days: int = 1, tail: int | None = None) -> MagicMock:
        args = MagicMock()
        args.job_name = job_name
        args.days = days
        args.tail = tail
        return args

    def test_missing_log_directory_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("nonexistent-job"))
        assert result == EXIT_ERROR
        assert "No logs found" in capsys.readouterr().out

    def test_shows_todays_log(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.log"
        log_file.write_text("2024-01-01 INFO Backup started\n")

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob"))

        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "Backup started" in out

    def test_no_logs_in_date_range_returns_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob", days=1))

        assert result == EXIT_SUCCESS
        assert "No log files found" in capsys.readouterr().out

    def test_multiple_days_shows_all(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today()
        for i in range(3):
            log_date = today - timedelta(days=i)
            (log_dir / f"{log_date.strftime('%Y-%m-%d')}.log").write_text(f"Log for day -{i}\n")

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob", days=3))

        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "Log for day -0" in out
        assert "Log for day -1" in out
        assert "Log for day -2" in out

    def test_tail_returns_last_n_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.log"
        log_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob", tail=3))

        assert result == EXIT_SUCCESS
        out = capsys.readouterr().out
        assert "line3" in out
        assert "line4" in out
        assert "line5" in out
        assert "line1" not in out

    def test_tail_across_days_is_bounded_and_chronological(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today()
        yesterday = today - timedelta(days=1)
        (log_dir / f"{yesterday.strftime('%Y-%m-%d')}.log").write_text(
            "old1\nold2\n",
            encoding="utf-8",
        )
        (log_dir / f"{today.strftime('%Y-%m-%d')}.log").write_text(
            "new1\nnew2\n",
            encoding="utf-8",
        )

        def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
            raise AssertionError(f"read_text must not be used for tailed logs: {self}")

        monkeypatch.setattr(Path, "read_text", fail_read_text)

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob", days=2, tail=3))

        assert result == EXIT_SUCCESS
        assert capsys.readouterr().out.splitlines() == ["old2", "new1", "new2"]

    def test_tail_zero_reads_no_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today().strftime("%Y-%m-%d")
        (log_dir / f"{today}.log").write_text("line1\nline2\n", encoding="utf-8")

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob", tail=0))

        assert result == EXIT_SUCCESS
        assert capsys.readouterr().out == ""

    def test_tail_no_logs_found_returns_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_show_logs(self._args("myjob", tail=10))

        assert result == EXIT_SUCCESS

    def test_oserror_reading_log_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.log"
        log_file.write_text("content")

        with (
            patch("src.main.LOG_BASE_DIR", tmp_path),
            patch("pathlib.Path.read_text", side_effect=OSError("permission denied")),
        ):
            result = cmd_show_logs(self._args("myjob"))

        assert result == EXIT_ERROR

    @pytest.mark.parametrize("bad_name", ["../bad", "../../etc", "foo/bar", "bad name", "a.b"])
    def test_traversal_or_invalid_job_name_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_name: str
    ) -> None:
        base = tmp_path / "logs"
        base.mkdir()
        secret = tmp_path / "secret.log"
        secret.write_text("SECRET OUTSIDE BASE\n")

        read_calls: list[Path] = []
        original_read_text = Path.read_text

        def spy_read_text(self: Path, *a: object, **k: object) -> str:
            read_calls.append(self)
            return original_read_text(self, *a, **k)  # type: ignore[arg-type]

        with (
            patch("src.main.LOG_BASE_DIR", base),
            patch("pathlib.Path.read_text", spy_read_text),
        ):
            result = cmd_show_logs(self._args(bad_name))

        assert result == EXIT_ERROR
        assert "Invalid job name" in capsys.readouterr().out
        assert secret not in read_calls

    def test_main_dispatches_show_logs(self) -> None:
        with (
            patch("src.main.cmd_show_logs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dk", "logs", "show", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()


class TestCmdTailLogs:
    def _args(self, job_name: str) -> MagicMock:
        args = MagicMock()
        args.job_name = job_name
        return args

    def test_missing_log_directory_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_tail_logs(self._args("nonexistent-job"))
        assert result == EXIT_ERROR
        assert "No logs found" in capsys.readouterr().out

    def test_no_log_files_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()

        with patch("src.main.LOG_BASE_DIR", tmp_path):
            result = cmd_tail_logs(self._args("myjob"))

        assert result == EXIT_ERROR
        assert "No log files" in capsys.readouterr().out

    def test_follows_existing_log_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        today = date.today().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.log"
        log_file.write_text("initial content\n")

        # open() wirft KeyboardInterrupt → cmd_tail_logs fängt es und gibt EXIT_SUCCESS zurück
        with (
            patch("src.main.LOG_BASE_DIR", tmp_path),
            patch("builtins.open", side_effect=KeyboardInterrupt),
        ):
            result = cmd_tail_logs(self._args("myjob"))
        assert result == EXIT_SUCCESS

    def test_falls_back_to_newest_log_when_today_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log_dir = tmp_path / "myjob"
        log_dir.mkdir()
        old_log = log_dir / "2024-01-01.log"
        old_log.write_text("old content\n")

        with (
            patch("src.main.LOG_BASE_DIR", tmp_path),
            patch("builtins.open", side_effect=KeyboardInterrupt),
        ):
            result = cmd_tail_logs(self._args("myjob"))

        assert result == EXIT_SUCCESS

    @pytest.mark.parametrize("bad_name", ["../bad", "../../etc", "foo/bar", "bad name", "a.b"])
    def test_traversal_or_invalid_job_name_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_name: str
    ) -> None:
        base = tmp_path / "logs"
        base.mkdir()
        opened: list[object] = []
        original_open = open

        def spy_open(file: object, *a: object, **k: object) -> object:
            opened.append(file)
            return original_open(file, *a, **k)  # type: ignore[arg-type]

        with (
            patch("src.main.LOG_BASE_DIR", base),
            patch("builtins.open", spy_open),
        ):
            result = cmd_tail_logs(self._args(bad_name))

        assert result == EXIT_ERROR
        assert "Invalid job name" in capsys.readouterr().out
        assert opened == []

    def test_main_dispatches_tail_logs(self) -> None:
        with (
            patch("src.main.cmd_tail_logs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dk", "logs", "tail", "my-job"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0
        mock_cmd.assert_called_once()


class TestMainDispatcher:
    def test_show_logs_days_option(self) -> None:
        with (
            patch("src.main.cmd_show_logs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dk", "logs", "show", "my-job", "--days", "7"]),
            pytest.raises(SystemExit),
        ):
            main()
        called_args = mock_cmd.call_args.args[0]
        assert called_args.days == 7

    def test_show_logs_tail_option(self) -> None:
        with (
            patch("src.main.cmd_show_logs", return_value=0) as mock_cmd,
            patch("sys.argv", ["dk", "logs", "show", "my-job", "--tail", "50"]),
            pytest.raises(SystemExit),
        ):
            main()
        called_args = mock_cmd.call_args.args[0]
        assert called_args.tail == 50


class TestCmdSchedulerSignal:
    def test_signal_handler_stops_scheduler(self, monkeypatch: pytest.MonkeyPatch) -> None:

        from src.main import cmd_scheduler

        monkeypatch.setenv("DK_MODE", "cli")
        mock_scheduler = MagicMock()
        mock_scheduler.start.side_effect = lambda: SchedulerStartOutcome(
            SchedulerStartState.RUNNING
        )

        with (
            patch("src.main.load_config"),
            patch("src.main.CronScheduler", return_value=mock_scheduler),
        ):
            # Trigger SIGINT after registering the handler
            result = cmd_scheduler(Path("/dummy/config.toml"))

        assert result == EXIT_SUCCESS
