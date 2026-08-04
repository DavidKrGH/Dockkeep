import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.utils.logging import (
    JOB_LOGGER_NAMESPACE,
    SYSTEM_LOG_NAME,
    _parse_log_level,
    cleanup_all_old_logs,
    cleanup_old_logs,
    job_logger_name,
    log_base_dir,
    log_blank_line,
    log_task_context,
    setup_job_logger,
    setup_root_logger,
    setup_system_logger,
)


@pytest.fixture(autouse=True)
def reset_loggers():
    logger_names = [
        "home-backup",
        "test-job",
        "dockkeep",
        "other-job",
        JOB_LOGGER_NAMESPACE,
        job_logger_name("home-backup"),
        job_logger_name("test-job"),
        job_logger_name("dockkeep"),
        job_logger_name("other-job"),
    ]
    for name in logger_names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logger.propagate = True
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_dk_console", False) or getattr(handler, "_dk_system_dir", None):
            handler.close()
            root.removeHandler(handler)
    yield
    for name in logger_names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logger.propagate = True
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_dk_console", False) or getattr(handler, "_dk_system_dir", None):
            handler.close()
            root.removeHandler(handler)


class TestParseLogLevel:
    def test_valid_levels(self):
        assert _parse_log_level("debug") == logging.DEBUG
        assert _parse_log_level("info") == logging.INFO
        assert _parse_log_level("warning") == logging.WARNING
        assert _parse_log_level("error") == logging.ERROR
        assert _parse_log_level("critical") == logging.CRITICAL

    def test_case_insensitive(self):
        assert _parse_log_level("DEBUG") == logging.DEBUG
        assert _parse_log_level("Info") == logging.INFO

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError, match="Invalid log level"):
            _parse_log_level("verbose")


class TestSetupJobLogger:
    def test_uses_log_dir_env_set_after_import(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DK_LOG_DIR", str(tmp_path))

        setup_job_logger("home-backup")

        assert log_base_dir() == tmp_path
        assert (tmp_path / "home-backup").is_dir()

    def test_returns_logger_with_correct_name(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        assert logger.name == job_logger_name("home-backup")

    def test_job_logger_name_is_namespaced(self) -> None:
        assert job_logger_name("dockkeep") == "dockkeep.jobs.dockkeep"

    def test_creates_log_directory(self, tmp_path):
        setup_job_logger("home-backup", log_base_dir=tmp_path)
        assert (tmp_path / "home-backup").is_dir()

    def test_creates_daily_log_file(self, tmp_path):
        setup_job_logger("home-backup", log_base_dir=tmp_path)
        expected = tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        assert expected.exists()

    def test_sets_correct_log_level(self, tmp_path):
        logger = setup_job_logger("home-backup", log_level="debug", log_base_dir=tmp_path)
        assert logger.level == logging.DEBUG

    def test_has_console_and_file_handler(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        assert len(logger.handlers) == 2

    def test_does_not_propagate(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        assert logger.propagate is False

    def test_idempotent_second_call(self, tmp_path):
        logger1 = setup_job_logger("home-backup", log_base_dir=tmp_path)
        logger2 = setup_job_logger("home-backup", log_base_dir=tmp_path)
        assert logger1 is logger2
        assert len(logger1.handlers) == 2

    def test_second_call_updates_log_level(self, tmp_path):
        logger = setup_job_logger("home-backup", log_level="info", log_base_dir=tmp_path)
        setup_job_logger("home-backup", log_level="debug", log_base_dir=tmp_path)

        assert logger.level == logging.DEBUG
        assert all(handler.level == logging.DEBUG for handler in logger.handlers)

    def test_log_message_written_to_file(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        logger.info("Test message")

        log_file = tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        content = log_file.read_text()
        assert "Test message" in content
        assert "[INFO]" in content
        assert "[home-backup]" in content
        assert f"[{job_logger_name('home-backup')}]" not in content

    def test_log_message_includes_compact_task_context(self, tmp_path):
        setup_job_logger("home-backup", log_base_dir=tmp_path)
        logger = logging.getLogger(f"{job_logger_name('home-backup')}.BackupExecutor")

        with log_task_context("backup.local"):
            logger.info("Starting backup")

        log_file = tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        content = log_file.read_text()
        assert "[BackupExecutor] [backup.local] Starting backup" in content
        assert "[home-backup.BackupExecutor]" not in content
        assert "[dockkeep.jobs.home-backup.BackupExecutor]" not in content

    def test_job_root_logger_keeps_job_name_component_logger_drops_it(self, tmp_path):
        setup_job_logger("home-backup", log_base_dir=tmp_path)
        root_logger = logging.getLogger(job_logger_name("home-backup"))
        component_logger = logging.getLogger(f"{job_logger_name('home-backup')}.BackupExecutor")

        root_logger.info("root line")
        component_logger.info("component line")

        log_file = tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        content = log_file.read_text()
        assert "[home-backup] root line" in content
        assert "[BackupExecutor] component line" in content

    def test_log_timestamp_is_time_only(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        logger.info("Test message")

        log_file = tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        line = log_file.read_text().splitlines()[0]
        assert re.match(r"^\d{2}:\d{2}:\d{2} \[INFO\]", line)
        assert not re.match(r"^\d{4}-\d{2}-\d{2}", line)

    def test_nested_task_context_combines_outer_and_inner_target(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)

        with log_task_context("workflow.daily"):
            logger.info("workflow header")
            with log_task_context("backup.local"):
                logger.info("step line")
            logger.info("workflow footer")

        content = (
            tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        ).read_text()
        assert "[home-backup] [workflow.daily] workflow header" in content
        assert "[home-backup] [workflow.daily › backup.local] step line" in content
        assert "[home-backup] [workflow.daily] workflow footer" in content

    def test_log_blank_line_writes_timestamp_only_separator(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        logger.info("before")
        log_blank_line(logger)
        logger.info("after")

        log_file = tmp_path / "home-backup" / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        lines = log_file.read_text().splitlines()
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", lines[1])
        assert "[INFO]" not in lines[1]
        assert f"[{job_logger_name('home-backup')}]" not in lines[1]

    def test_job_named_like_app_logger_does_not_reuse_app_logger(self, tmp_path):
        app_logger = logging.getLogger("dockkeep")
        job_logger = setup_job_logger("dockkeep", log_base_dir=tmp_path)

        assert job_logger is not app_logger
        assert job_logger.name == job_logger_name("dockkeep")
        assert not app_logger.handlers


class TestSetupRootLogger:
    def test_returns_logger_with_correct_name(self):
        logger = setup_root_logger()
        assert logger.name == "dockkeep"

    def test_has_console_handler(self):
        logger = setup_root_logger()
        assert len(logger.handlers) == 1

    def test_does_not_propagate(self):
        logger = setup_root_logger()
        assert logger.propagate is False

    def test_idempotent_second_call(self):
        logger1 = setup_root_logger()
        logger2 = setup_root_logger()
        assert logger1 is logger2
        assert len(logger1.handlers) == 1

    def test_second_call_updates_log_level(self):
        logger = setup_root_logger("info")
        setup_root_logger("debug")

        assert logger.level == logging.DEBUG
        assert all(handler.level == logging.DEBUG for handler in logger.handlers)


class TestSetupSystemLogger:
    def _today_file(self, base):
        return base / SYSTEM_LOG_NAME / f"{datetime.now().strftime('%Y-%m-%d')}.log"

    def test_named_logger_message_written_to_system_file(self, tmp_path):
        logger = setup_system_logger(log_base_dir=tmp_path)
        logger.info("scheduler started")

        content = self._today_file(tmp_path).read_text()
        assert "scheduler started" in content
        assert "[INFO]" in content

    def test_module_logger_is_captured(self, tmp_path):
        setup_system_logger(log_base_dir=tmp_path)
        logging.getLogger("src.scheduler.cron").warning("config reload failed")

        content = self._today_file(tmp_path).read_text()
        assert "config reload failed" in content
        assert "[WARNING]" in content

    def test_system_logger_lines_carry_no_task_tag(self, tmp_path):
        logger = setup_system_logger(log_base_dir=tmp_path)

        with log_task_context("backup.local"):
            logger.info("system line during run")

        content = self._today_file(tmp_path).read_text()
        assert "[dockkeep] system line during run" in content
        assert "[backup.local]" not in content

    def test_job_logger_does_not_leak_into_system_file(self, tmp_path):
        setup_system_logger(log_base_dir=tmp_path)
        job_logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        job_logger.info("only in job file")

        system_file = self._today_file(tmp_path)
        system_content = system_file.read_text() if system_file.exists() else ""
        assert "only in job file" not in system_content

    def test_idempotent_no_duplicate_root_handlers(self, tmp_path):
        setup_system_logger(log_base_dir=tmp_path)
        setup_system_logger(log_base_dir=tmp_path)

        root = logging.getLogger()
        system_handlers = [h for h in root.handlers if getattr(h, "_dk_system_dir", None)]
        console_handlers = [h for h in root.handlers if getattr(h, "_dk_console", False)]
        assert len(system_handlers) == 1
        assert len(console_handlers) == 1

    def test_similar_log_base_paths_replace_system_handler(self, tmp_path):
        first_base = tmp_path / "logs"
        second_base = tmp_path / "logs-extra"
        setup_system_logger(log_base_dir=first_base)
        setup_system_logger(log_base_dir=second_base)

        root = logging.getLogger()
        system_handlers = [h for h in root.handlers if getattr(h, "_dk_system_dir", None)]
        assert len(system_handlers) == 1
        assert getattr(system_handlers[0], "_dk_system_dir") == str(
            (second_base / SYSTEM_LOG_NAME).resolve()
        )

    def test_graceful_when_dir_not_writable(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")

        logger = setup_system_logger(log_base_dir=blocker)
        logger.info("still works on console")

        root = logging.getLogger()
        assert not any(getattr(h, "_dk_system_dir", None) for h in root.handlers)


class TestCleanupOldLogs:
    def _create_log(self, log_dir: Path, date: datetime) -> Path:
        log_dir.mkdir(parents=True, exist_ok=True)
        f = log_dir / f"{date.strftime('%Y-%m-%d')}.log"
        f.write_text("log content")
        return f

    def test_deletes_old_log(self, tmp_path):
        job_dir = tmp_path / "test-job"
        old_date = datetime.now() - timedelta(days=35)
        self._create_log(job_dir, old_date)

        deleted = cleanup_old_logs("test-job", retention_days=30, log_base_dir=tmp_path)
        assert deleted == 1

    def test_keeps_recent_log(self, tmp_path):
        job_dir = tmp_path / "test-job"
        recent_date = datetime.now() - timedelta(days=5)
        self._create_log(job_dir, recent_date)

        deleted = cleanup_old_logs("test-job", retention_days=30, log_base_dir=tmp_path)
        assert deleted == 0

    def test_returns_zero_for_missing_dir(self, tmp_path):
        deleted = cleanup_old_logs("nonexistent-job", retention_days=30, log_base_dir=tmp_path)
        assert deleted == 0

    def test_ignores_non_date_files(self, tmp_path):
        job_dir = tmp_path / "test-job"
        job_dir.mkdir()
        (job_dir / "scheduler.log").write_text("data")

        deleted = cleanup_old_logs("test-job", retention_days=30, log_base_dir=tmp_path)
        assert deleted == 0
        assert (job_dir / "scheduler.log").exists()

    def test_deletes_multiple_old_logs(self, tmp_path):
        job_dir = tmp_path / "test-job"
        for days_ago in [40, 50, 60]:
            self._create_log(job_dir, datetime.now() - timedelta(days=days_ago))

        deleted = cleanup_old_logs("test-job", retention_days=30, log_base_dir=tmp_path)
        assert deleted == 3


class TestCleanupAllOldLogs:
    def test_cleans_all_job_dirs(self, tmp_path):
        for job in ["job-a", "job-b"]:
            job_dir = tmp_path / job
            job_dir.mkdir()
            log_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            old = tmp_path / job / f"{log_date}.log"
            old.write_text("old log")

        deleted = cleanup_all_old_logs(retention_days=30, log_base_dir=tmp_path)
        assert deleted == 2

    def test_returns_zero_for_empty_base_dir(self, tmp_path):
        deleted = cleanup_all_old_logs(retention_days=30, log_base_dir=tmp_path)
        assert deleted == 0

    def test_returns_zero_for_missing_base_dir(self, tmp_path):
        deleted = cleanup_all_old_logs(retention_days=30, log_base_dir=tmp_path / "nonexistent")
        assert deleted == 0


class TestCleanupRetentionNone:
    def _create_old_log(self, job_dir: Path) -> Path:
        job_dir.mkdir(parents=True, exist_ok=True)
        old_date = datetime.now() - timedelta(days=60)
        f = job_dir / f"{old_date.strftime('%Y-%m-%d')}.log"
        f.write_text("old log content")
        return f

    def test_cleanup_old_logs_none_returns_zero(self, tmp_path):
        job_dir = tmp_path / "test-job"
        log_file = self._create_old_log(job_dir)

        result = cleanup_old_logs("test-job", retention_days=None, log_base_dir=tmp_path)
        assert result == 0
        assert log_file.exists()

    def test_cleanup_all_old_logs_none_returns_zero(self, tmp_path):
        for job in ["job-a", "job-b"]:
            self._create_old_log(tmp_path / job)

        result = cleanup_all_old_logs(retention_days=None, log_base_dir=tmp_path)
        assert result == 0
        assert len(list(tmp_path.rglob("*.log"))) == 2


class TestSetupJobLoggerDateRollover:
    def test_stale_file_handler_replaced_on_date_change(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)

        # FileHandler auf gestrige Datei zeigen lassen (simuliert Mitternachtsüberschreitung)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_file = tmp_path / "home-backup" / f"{yesterday}.log"
        yesterday_file.touch()

        file_handler = next(h for h in logger.handlers if isinstance(h, logging.FileHandler))
        file_handler.close()
        logger.removeHandler(file_handler)
        stale_handler = logging.FileHandler(str(yesterday_file), encoding="utf-8")
        logger.addHandler(stale_handler)

        setup_job_logger("home-backup", log_base_dir=tmp_path)

        file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in file_handlers[0].baseFilename

    def test_current_file_handler_kept_unchanged(self, tmp_path):
        logger = setup_job_logger("home-backup", log_base_dir=tmp_path)
        file_handler_before = next(h for h in logger.handlers if isinstance(h, logging.FileHandler))

        setup_job_logger("home-backup", log_base_dir=tmp_path)

        file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0] is file_handler_before


class TestSetupJobLoggerPathEscape:
    def test_path_escape_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="would escape the log directory"):
            setup_job_logger("../escape", log_base_dir=tmp_path)
