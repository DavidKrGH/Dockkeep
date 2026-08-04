"""Service container factory for the GUI adapters."""

import os
from dataclasses import dataclass
from pathlib import Path

from .backup_stats import BackupStatsService
from .backup_stats_collector import BackupStatsCollector
from .config import ConfigService
from .config_editor import ConfigEditorService
from .dashboard import DashboardService
from .database_inspection import DatabaseInspectionService
from .logs import LogService
from .rclone import RcloneService
from .repositories import RepositoryService
from .repository_artifact_store import RepositoryArtifactStore
from .repository_browser import RepositoryBrowserService
from .restore import RestoreRegistry, RestoreService
from .run_control import RunControlClient
from .run_history import RunHistoryService
from .run_manager import RunManager, RunOrigin
from .runs import RunService
from .terminal_hooks import terminal_run_hook


@dataclass(frozen=True)
class ServiceContainer:
    """Application services created once per FastAPI app process."""

    config_service: ConfigService
    run_manager: RunManager
    run_service: RunService
    log_service: LogService
    rclone_service: RcloneService
    repository_service: RepositoryService
    repository_browser_service: RepositoryBrowserService
    restore_registry: RestoreRegistry
    restore_service: RestoreService
    dashboard_service: DashboardService
    config_editor_service: ConfigEditorService
    backup_stats_service: BackupStatsService
    backup_artifact_store: RepositoryArtifactStore
    backup_stats_collector: BackupStatsCollector
    run_history_service: RunHistoryService
    database_inspection_service: DatabaseInspectionService


def create_services(config_path: Path, appdata_dir: Path | None = None) -> ServiceContainer:
    config_service = ConfigService(config_path)
    resolved_appdata_dir = appdata_dir or Path(os.environ.get("DK_APPDATA_DIR", "/appdata"))
    appdata_db_path = resolved_appdata_dir / "appdata.db"
    run_history_service = RunHistoryService(db_path=appdata_db_path)
    run_history_service.mark_active_runs_interrupted_sync(origins={RunOrigin.MANUAL})
    backup_artifact_store = RepositoryArtifactStore(db_path=appdata_db_path)
    database_inspection_service = DatabaseInspectionService(db_path=appdata_db_path)
    run_manager = RunManager(
        on_started=run_history_service.create_run,
        on_terminal=terminal_run_hook(run_history_service),
    )
    log_service = LogService()

    rclone_service = RcloneService(config_path.parent)
    repository_service = RepositoryService(config_service, backup_artifact_store)
    backup_stats_collector = BackupStatsCollector(repository_service, backup_artifact_store)
    backup_stats_service = BackupStatsService(
        backup_artifact_store, backup_stats_collector, config_service=config_service
    )
    repository_browser_service = RepositoryBrowserService(
        config_service,
        backup_artifact_store=backup_artifact_store,
        backup_stats_service=backup_stats_service,
    )
    restore_registry = RestoreRegistry(db_path=appdata_db_path)
    restore_service = RestoreService(config_service, restore_registry, run_manager)
    config_editor_service = ConfigEditorService(config_service=config_service)

    run_control_client = RunControlClient(resolved_appdata_dir / "run-control.sock")
    run_service = RunService(
        config_service,
        run_manager,
        stats_collector=backup_stats_collector,
        run_control_client=run_control_client,
        run_history_service=run_history_service,
    )
    dashboard_service = DashboardService(
        config_service,
        backup_stats_service=backup_stats_service,
        run_service=run_service,
        run_history_service=run_history_service,
    )

    return ServiceContainer(
        config_service=config_service,
        run_manager=run_manager,
        run_service=run_service,
        log_service=log_service,
        rclone_service=rclone_service,
        repository_service=repository_service,
        repository_browser_service=repository_browser_service,
        restore_registry=restore_registry,
        restore_service=restore_service,
        dashboard_service=dashboard_service,
        config_editor_service=config_editor_service,
        backup_stats_service=backup_stats_service,
        backup_artifact_store=backup_artifact_store,
        backup_stats_collector=backup_stats_collector,
        run_history_service=run_history_service,
        database_inspection_service=database_inspection_service,
    )
