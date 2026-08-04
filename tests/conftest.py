"""Pytest-Fixtures für alle Tests."""

import logging

import pytest

from src.models.config import RawBackupConfig, RawJobConfig
from tests.config_builders import raw_backup_task, raw_job


def _clear_logger_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_dk_console", False) or getattr(handler, "_dk_system_dir", None):
            handler.close()
            logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _reset_dk_root_logging():
    """Entfernt nach jedem Test die vom System-Logger gesetzten Root-Handler.

    ``setup_system_logger`` konfiguriert den globalen Root-Logger; ohne diese
    Bereinigung würden markierte Konsolen-/File-Handler (teils auf gelöschte
    tmp-Verzeichnisse zeigend) zwischen Tests lecken.
    """
    root = logging.getLogger()
    named = logging.getLogger("dockkeep")
    for logger in (root, named):
        _clear_logger_handlers(logger)
    named.propagate = True
    yield
    for logger in (root, named):
        _clear_logger_handlers(logger)
    named.propagate = True


@pytest.fixture()
def minimal_backup_config() -> RawBackupConfig:
    """Minimale valide Backup-Config für Executor- und Core-Tests."""
    return RawBackupConfig.model_validate(
        raw_backup_task(
            repository="/backups/test",
            overrides={"password": "secret", "keep_daily": 7},
        )
    )


@pytest.fixture()
def job_with_backups() -> RawJobConfig:
    """Job mit lokalem und rclone-basiertem Backup."""
    return RawJobConfig.model_validate(
        raw_job(
            backup_tasks={
                "local": raw_backup_task(
                    repository="/backups/test",
                    overrides={"password": "secret", "keep_daily": 7},
                ),
                "remote": raw_backup_task(
                    repository="rclone:gdrive:backups/test",
                    overrides={"password": "secret", "keep_daily": 7},
                ),
            }
        )
    )
