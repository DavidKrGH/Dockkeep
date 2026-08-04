"""Dashboard use-case service for the browser GUI."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, TypedDict

from ..models.resolved_config import ResolvedAppConfig
from .backup_stats import BackupStatsService
from .config import ConfigService
from .errors import ConfigServiceError
from .run_history import RunHistoryService
from .runs import historical_run_list_view
from .scheduling import backup_steps, next_run_datetime, scheduled_run_display

if TYPE_CHECKING:
    from .runs import RunService, RunView


class DashboardOverview(TypedDict):
    failures_7d: int
    total_repo_size_bytes: int | None
    snapshots_count: int | None
    jobs_count: int
    backups_count: int


class DashboardWorkflowView(TypedDict):
    name: str
    steps: str
    next_run: datetime | None
    edit_url: str


class DashboardBackupView(TypedDict):
    name: str
    repository: str
    next_run: datetime | None
    total_size_bytes: object
    snapshots_count: object
    last_backup_time: object
    last_backup_data_added_bytes: object
    last_backup_duration_seconds: object
    edit_url: str


class DashboardRcloneView(TypedDict):
    name: str
    source: str
    target: str
    next_run: datetime | None
    edit_url: str


class ScheduledRunView(TypedDict):
    job: str
    name: str
    type: str
    steps: str
    schedule: str
    next_run: datetime
    task_type_label: str | None
    task_type_tone: str | None
    target_primary: str | None
    target_secondary: str | None


class DashboardJobView(TypedDict):
    name: str
    workflows: list[DashboardWorkflowView]
    is_running: bool
    run_status_unknown: bool
    backups: list[DashboardBackupView]
    rclone: list[DashboardRcloneView]
    edit_url: str
    effective_url: str


class DashboardRunsPanelView(TypedDict):
    upcoming_runs: list[ScheduledRunView]
    active_runs: list["RunView"]
    recent_runs: list["RunView"]


class DashboardView(TypedDict):
    overview: DashboardOverview
    jobs: list[DashboardJobView]
    config_error: str | None
    upcoming_runs: list[ScheduledRunView]
    active_runs: list["RunView"]
    recent_runs: list["RunView"]
    chart_data: dict[str, object]
    scheduler_available: bool
    run_status_unknown: bool
    degraded: bool


class DashboardJobStatusView(TypedDict):
    name: str
    is_running: bool
    run_status_unknown: bool


class RunStatusState(TypedDict):
    active_jobs: set[str]
    scheduler_available: bool
    run_status_unknown: bool


logger = logging.getLogger(__name__)


class DashboardService:

    def __init__(
        self,
        config_service: ConfigService,
        backup_stats_service: BackupStatsService | None = None,
        run_service: "RunService | None" = None,
        run_history_service: RunHistoryService | None = None,
    ) -> None:
        self._config_service = config_service
        self._backup_stats_service = backup_stats_service
        self._run_service = run_service
        self._run_history_service = run_history_service

    async def get_dashboard_view(self) -> DashboardView:
        run_status, failures_7d, active_runs = await asyncio.gather(
            self._run_status_state(),
            self._failures_last_week(),
            self._active_runs(),
        )
        recent_runs = await self._recent_runs(exclude_run_ids=_run_ids(active_runs))
        return await asyncio.to_thread(
            self._get_dashboard_view_sync,
            run_status,
            recent_runs,
            failures_7d,
            active_runs,
        )

    async def get_runs_panel_view(self) -> DashboardRunsPanelView:
        active_runs = await self._active_runs()
        recent_runs = await self._recent_runs(exclude_run_ids=_run_ids(active_runs))
        upcoming_runs = await asyncio.to_thread(self._upcoming_runs_sync)
        return {
            "upcoming_runs": upcoming_runs,
            "active_runs": active_runs,
            "recent_runs": recent_runs,
        }

    async def get_job_status_view(self, job_name: str) -> DashboardJobStatusView:
        run_status = await self._run_status_state()
        active_jobs = run_status["active_jobs"]
        run_status_unknown = bool(run_status["run_status_unknown"])
        return {
            "name": job_name,
            "is_running": job_name in active_jobs,
            "run_status_unknown": run_status_unknown,
        }

    def _get_dashboard_view_sync(
        self,
        run_status: RunStatusState,
        recent_runs: list["RunView"],
        failures_7d: int,
        active_runs: list["RunView"],
    ) -> DashboardView:
        try:
            config = self._config_service.load_active_config()
        except ConfigServiceError as exc:
            return {
                "overview": _overview(
                    failures_7d=failures_7d,
                    stats_by_key={},
                    jobs_count=0,
                    backups_count=0,
                ),
                "jobs": [],
                "config_error": exc.message,
                "upcoming_runs": [],
                "active_runs": active_runs,
                "recent_runs": recent_runs,
                "chart_data": _chart_data(_empty_growth_data()),
                "scheduler_available": False,
                "run_status_unknown": True,
                "degraded": True,
            }

        active_jobs = run_status["active_jobs"]
        run_status_unknown = bool(run_status["run_status_unknown"])
        scheduler_available = bool(run_status["scheduler_available"])

        jobs: list[DashboardJobView] = []
        stats_by_key = self._stats_by_key()
        jobs_count = len(config.jobs)
        backups_count = 0

        for job_name, job in config.jobs.items():
            workflows: list[DashboardWorkflowView] = []
            for workflow_name, workflow in job.workflows.items():
                workflow_next = next_run_datetime(workflow.schedule)
                workflows.append(
                    {
                        "name": workflow_name,
                        "steps": " -> ".join(workflow.steps),
                        "next_run": workflow_next,
                        "edit_url": f"/config/jobs/{job_name}/workflows/{workflow_name}",
                    }
                )

            backups: list[DashboardBackupView] = []
            for backup_name, backup in job.backup.items():
                backup_next = next_run_datetime(backup.schedule)
                stats = stats_by_key.get((job_name, backup_name), {})
                last_backup = stats.get("last_backup")
                last_backup_dict = last_backup if isinstance(last_backup, dict) else {}
                backups.append(
                    {
                        "name": backup_name,
                        "repository": backup.repository,
                        "next_run": backup_next,
                        "total_size_bytes": stats.get("total_size_bytes"),
                        "snapshots_count": stats.get("snapshots_count"),
                        "last_backup_time": last_backup_dict.get("time"),
                        "last_backup_data_added_bytes": last_backup_dict.get("data_added_bytes"),
                        "last_backup_duration_seconds": last_backup_dict.get("duration_seconds"),
                        "edit_url": f"/config/jobs/{job_name}/backups/{backup_name}",
                    }
                )
                backups_count += 1

            rclone_list: list[DashboardRcloneView] = []
            for name, task in job.rclone.items():
                rclone_next = next_run_datetime(task.schedule)
                task_dict: DashboardRcloneView = {
                    "name": name,
                    "source": task.source,
                    "target": task.target,
                    "next_run": rclone_next,
                    "edit_url": f"/config/jobs/{job_name}/rclone/{name}",
                }
                rclone_list.append(task_dict)

            jobs.append(
                {
                    "name": job_name,
                    "workflows": workflows,
                    "is_running": job_name in active_jobs,
                    "run_status_unknown": run_status_unknown,
                    "backups": backups,
                    "rclone": rclone_list,
                    "edit_url": f"/config/jobs/{job_name}",
                    "effective_url": f"/config/jobs/{job_name}/effective",
                }
            )

        upcoming_runs = _collect_upcoming_runs(config)
        growth_data = self._growth_data()
        return {
            "overview": _overview(
                failures_7d=failures_7d,
                stats_by_key=stats_by_key,
                jobs_count=jobs_count,
                backups_count=backups_count,
            ),
            "jobs": jobs,
            "config_error": None,
            "upcoming_runs": upcoming_runs,
            "active_runs": active_runs,
            "recent_runs": recent_runs,
            "chart_data": _chart_data(growth_data),
            "scheduler_available": scheduler_available,
            "run_status_unknown": run_status_unknown,
            "degraded": run_status_unknown,
        }

    async def _run_status_state(self) -> RunStatusState:
        if self._run_service is None:
            return {
                "active_jobs": set(),
                "scheduler_available": False,
                "run_status_unknown": False,
            }
        try:
            active_jobs = await self._run_service.active_run_job_names()
            scheduler_available = await self._run_service.run_status_available()
        except Exception as exc:
            logger.info("Dashboard run status unavailable: %s", exc)
            return {
                "active_jobs": set(),
                "scheduler_available": False,
                "run_status_unknown": True,
            }
        return {
            "active_jobs": active_jobs,
            "scheduler_available": scheduler_available,
            "run_status_unknown": not scheduler_available,
        }

    def _stats_by_key(self) -> dict[tuple[str, str], dict[str, object]]:
        if self._backup_stats_service is None:
            return {}
        return {
            (str(stats.get("job", "")), str(stats.get("backup", ""))): stats
            for stats in self._backup_stats_service.get_all_backup_stats()
        }

    def _growth_data(self) -> dict[str, object]:
        if self._backup_stats_service is None:
            return _empty_growth_data()
        return self._backup_stats_service.get_dashboard_growth_data()

    async def _recent_runs(self, *, exclude_run_ids: set[str] | None = None) -> list["RunView"]:
        if self._run_history_service is None:
            return []
        records = await self._run_history_service.list_history(
            limit=8, exclude_run_ids=exclude_run_ids
        )
        return [historical_run_list_view(record) for record in records]

    async def _active_runs(self) -> list["RunView"]:
        if self._run_service is None:
            return []
        try:
            view = await self._run_service.list_active_runs_view()
        except Exception as exc:
            logger.info("Dashboard active runs unavailable: %s", exc)
            return []
        return view["runs"]

    def _upcoming_runs_sync(self) -> list[ScheduledRunView]:
        try:
            config = self._config_service.load_active_config()
        except ConfigServiceError:
            return []
        return _collect_upcoming_runs(config)

    async def _failures_last_week(self) -> int:
        if self._run_history_service is None:
            return 0
        since = datetime.now(timezone.utc) - timedelta(days=7)
        return await self._run_history_service.count_failures_since(since=since)


def _empty_growth_data() -> dict[str, object]:
    return {"all_labels": [], "datasets": []}


def _run_ids(run_views: list["RunView"]) -> set[str]:
    return {str(view["run_id"]) for view in run_views if view.get("run_id")}


def _collect_upcoming_runs(config: ResolvedAppConfig) -> list[ScheduledRunView]:
    upcoming: list[ScheduledRunView] = []
    for job_name, job in config.jobs.items():
        for workflow_name, workflow in job.workflows.items():
            workflow_next = next_run_datetime(workflow.schedule)
            if workflow_next:
                upcoming.append(
                    _scheduled_run_view(
                        job=job_name,
                        name=workflow_name,
                        run_type="workflow",
                        steps=" -> ".join(workflow.steps),
                        schedule=str(workflow.schedule),
                        next_run=workflow_next,
                    )
                )
        for backup_name, backup in job.backup.items():
            backup_next = next_run_datetime(backup.schedule)
            if backup_next:
                upcoming.append(
                    _scheduled_run_view(
                        job=job_name,
                        name=backup_name,
                        run_type="backup",
                        steps=" + ".join(backup_steps(backup)),
                        schedule=str(backup.schedule),
                        next_run=backup_next,
                    )
                )
        for name, task in job.rclone.items():
            rclone_next = next_run_datetime(task.schedule)
            if rclone_next:
                upcoming.append(
                    _scheduled_run_view(
                        job=job_name,
                        name=f"rclone.{name}",
                        run_type="rclone",
                        steps="rclone sync",
                        schedule=str(task.schedule),
                        next_run=rclone_next,
                    )
                )
    upcoming.sort(key=_next_run_sort_key)
    return upcoming


def _overview(
    *,
    failures_7d: int,
    stats_by_key: dict[tuple[str, str], dict[str, object]],
    jobs_count: int,
    backups_count: int,
) -> DashboardOverview:
    return {
        "failures_7d": failures_7d,
        "total_repo_size_bytes": _total_repo_size_bytes(stats_by_key),
        "snapshots_count": _total_snapshots_count(stats_by_key),
        "jobs_count": jobs_count,
        "backups_count": backups_count,
    }


def _chart_data(growth_data: dict[str, object]) -> dict[str, object]:
    datasets = growth_data.get("datasets")
    return {"growth": growth_data if datasets else None}


def _scheduled_run_view(
    *,
    job: str,
    name: str,
    run_type: str,
    steps: str,
    schedule: str,
    next_run: datetime,
) -> ScheduledRunView:
    display = scheduled_run_display(job=job, name=name, run_type=run_type, steps=steps)
    return {
        "job": job,
        "name": name,
        "type": run_type,
        "steps": steps,
        "schedule": schedule,
        "next_run": next_run,
        "task_type_label": display["task_type_label"],
        "task_type_tone": display["task_type_tone"],
        "target_primary": display["target_primary"],
        "target_secondary": display["target_secondary"],
    }


def _next_run_sort_key(run: ScheduledRunView) -> datetime:
    value = run["next_run"]
    return value


def _total_repo_size_bytes(stats_by_key: dict[tuple[str, str], dict[str, object]]) -> int | None:
    return _stats_sum(stats_by_key, "total_size_bytes")


def _total_snapshots_count(stats_by_key: dict[tuple[str, str], dict[str, object]]) -> int | None:
    return _stats_sum(stats_by_key, "snapshots_count")


def _stats_sum(stats_by_key: dict[tuple[str, str], dict[str, object]], field: str) -> int | None:
    total = 0
    seen = False
    seen_repositories: set[str] = set()
    for key, stats in stats_by_key.items():
        repository_identity = _stats_repository_identity(key, stats)
        if repository_identity in seen_repositories:
            continue
        value = stats.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            total += int(value)
            seen = True
            seen_repositories.add(repository_identity)
    return total if seen else None


def _stats_repository_identity(key: tuple[str, str], stats: dict[str, object]) -> str:
    repository_id = stats.get("repository_id")
    if isinstance(repository_id, str) and repository_id:
        return f"repository:{repository_id}"
    return f"backup:{key[0]}.{key[1]}"
