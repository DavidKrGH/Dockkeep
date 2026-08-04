"""Asynchronous manual run service."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlencode
from uuid import uuid4

from ..core.job_runner import JobRunner
from ..core.locking import lock_dir as default_lock_dir
from ..models.resolved_config import ResolvedAppConfig
from ..notifications.context import build_notification_context
from ..utils.logging import log_base_dir as default_log_base_dir
from ..utils.targets import ParsedTaskSelector
from ..utils.targets import parse_task_selector as parse_run_task_selector
from .appdata_schema import connect_appdata_db, utc_rfc3339
from .backup_stats_collector import BackupStatsCollector
from .config import ConfigService, get_job_or_raise
from .errors import NotFoundServiceError, ServiceError
from .run_control import RunControlClient
from .run_history import RunHistoryService
from .run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus, format_run_target

logger = logging.getLogger(__name__)

# Bounded window for start_run_status_view() to let a near-instant outcome (e.g.
# a resource-lock conflict) become visible in the very first response instead of
# only on the next HTMX poll. Short enough to be imperceptible for the common
# case where the run keeps running past this window.
_INITIAL_STATUS_WINDOW_SECONDS = 0.2
DEFAULT_RUNS_PAGE_SIZE = 50


class RunStepView(TypedDict):
    run_step_id: str
    position: int
    step: str
    backend: str
    task_type: str
    task_name: str
    status: str
    status_label: str
    status_tone: str
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    error: object
    effective_task_config_pretty: str


class RunRestoreDetailView(TypedDict):
    run_restore_id: str
    run_id: str
    job: str
    backup: str
    backend: str
    snapshot_id: str
    mode: str
    restore_target: str
    snapshot_paths: list[object]
    include_patterns: list[object]
    exclude_patterns: list[object]
    overwrite: bool
    error: object
    output: object
    output_truncated: bool
    task_label: str


class RunDetailsView(TypedDict):
    steps: list[RunStepView]
    restore: RunRestoreDetailView | None
    has_steps: bool
    has_restore: bool


class RunView(TypedDict, total=False):
    run_id: str
    origin: str
    origin_label: str
    run_kind: str
    target: str
    task_label: str
    task_kind: str | None
    task_type_label: str | None
    task_type_tone: str | None
    task_name: str | None
    task_substep_label: str | None
    target_primary: str
    target_secondary: str | None
    job: str
    step: str
    dry_run: bool
    status: str
    status_label: str
    status_tone: str
    is_active: bool
    is_cancellable: bool
    error: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    detail_url: str
    status_url: str
    cancel_url: str
    logs_url: str | None
    log_stream_url: str | None
    steps: list[RunStepView]
    restore: RunRestoreDetailView | None
    has_steps: bool
    has_restore: bool
    action_job: str
    action_step: str
    action_target: str | None


class RunsPageView(TypedDict):
    runs: list[RunView]
    active_runs: list[RunView]
    history_runs: list[RunView]
    active_count: int
    history_count: int
    total_count: int
    scheduler_available: bool
    page: int
    page_size: int
    has_next_page: bool
    has_previous_page: bool
    next_page_url: str | None
    previous_page_url: str | None
    fragment_url: str
    filters: "RunFiltersView"
    filter_options: "RunFilterOptionsView"
    filter_clear_url: str


class RunFiltersView(TypedDict):
    job: str
    task: str
    status: str
    origin: str
    is_active: bool


class RunFilterOptionView(TypedDict):
    value: str
    label: str


class RunFilterOptionsView(TypedDict):
    jobs: list[RunFilterOptionView]
    tasks: list[RunFilterOptionView]
    statuses: list[RunFilterOptionView]
    origins: list[RunFilterOptionView]


class ActiveRunsView(TypedDict):
    runs: list[RunView]
    scheduler_available: bool


class RunService:

    def __init__(
        self,
        config_service: ConfigService,
        run_manager: RunManager,
        lock_dir: Path | None = None,
        log_base_dir: Path | None = None,
        stats_collector: BackupStatsCollector | None = None,
        run_control_client: RunControlClient | None = None,
        run_history_service: RunHistoryService | None = None,
    ) -> None:
        self._config_service = config_service
        self._run_manager = run_manager
        self._lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self._log_base_dir = log_base_dir if log_base_dir is not None else default_log_base_dir()
        self._stats_collector = stats_collector
        self._run_control_client = run_control_client
        self._run_history_service = run_history_service

    async def start_run(self, task_selector: str, dry_run: bool = False) -> RunRecord:
        parsed = parse_task_selector(task_selector)
        config = self._config_service.load_active_config()
        job = get_job_or_raise(config, parsed.job)
        if parsed.kind in {"backup", "backup_step"} and parsed.name not in job.backup:
            raise ServiceError("task_not_found", f"Backup not found: {task_selector}", 404)
        if parsed.kind == "workflow" and parsed.name not in job.workflows:
            raise ServiceError("task_not_found", f"Workflow not found: {task_selector}", 404)
        if parsed.kind == "rclone" and parsed.name not in job.rclone:
            raise ServiceError(
                "task_not_found", f"Rclone task not configured: {task_selector}", 404
            )

        task_type = _task_type_from_parsed(parsed)
        task_name = _task_name_from_parsed(parsed)
        existing = await self._active_manual_run(task_type, task_name, parsed.job, dry_run)
        if existing is not None:
            return existing

        run_id = str(uuid4())
        return await self._run_manager.start(
            RunOrigin.MANUAL,
            parsed.job,
            task_type,
            task_name,
            lambda mark_not_cancellable: self._execute(
                parsed, config, dry_run, mark_not_cancellable, run_id
            ),
            dry_run=dry_run,
            run_id=run_id,
            notify_ctx=build_notification_context(
                providers=config.global_.notifications,
                notifications=(
                    job.workflows[parsed.name].notifications
                    if parsed.kind == "workflow"
                    else (
                        job.rclone[parsed.name].notifications
                        if parsed.kind == "rclone"
                        else job.backup[parsed.name].notifications
                    )
                ),
                logger=logging.getLogger(f"dockkeep.jobs.{parsed.job}"),
                task_type=task_type,  # type: ignore[arg-type]
                task_name=parsed.name,
                display_target=task_selector,
                log_path=self._log_base_dir / parsed.job / f"{datetime.now().date()}.log",
            ),
        )

    async def _active_manual_run(
        self,
        task_type: str,
        task_name: str,
        job: str,
        dry_run: bool,
    ) -> RunRecord | None:
        """Return the active manual run for the same target, if any.

        Best-effort dedupe: the check and the subsequent start are not atomic,
        so two concurrent requests can still both start. That race is benign —
        the loser ends in ``lock_error`` exactly like before the dedupe.
        """
        for record in await self._run_manager.list():
            if (
                record.origin == RunOrigin.MANUAL
                and record.run_kind == RunKind.JOB_TASK
                and record.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                and record.job == job
                and record.task_type == task_type
                and record.task_name == task_name
                and record.dry_run == dry_run
            ):
                return record
        return None

    async def start_run_status_view(
        self,
        target: str,
        *,
        action_job: str,
        action_step: str,
        dry_run: bool = False,
    ) -> RunView:
        try:
            record = await self.start_run(target, dry_run=dry_run)
            record = await self._run_manager.wait_briefly(
                record.run_id, timeout=_INITIAL_STATUS_WINDOW_SECONDS
            )
            view = _run_record_view(record)
            return _apply_run_action(
                view,
                action_job=action_job,
                action_step=action_step,
                dry_run=dry_run,
                record_dry_run=record.dry_run,
                with_actions=True,
            )
        except ServiceError as exc:
            return _run_status_error_view(
                action_job,
                action_step,
                dry_run=dry_run,
                status=exc.code,
                error=exc.message,
            )
        except Exception as exc:
            logger.exception("Unexpected error while starting manual run %s", target)
            return _run_status_error_view(
                action_job,
                action_step,
                dry_run=dry_run,
                status="unexpected_error",
                error=str(exc),
            )

    async def cancel_run(self, run_id: str) -> RunRecord | dict[str, object]:
        """Cancel one active run, routing by where the run lives.

        Local manual runs are cancelled through the local run manager. If the
        run is unknown locally and a control client is configured, the
        cancellation is delegated to the scheduler runtime over the control
        socket.

        Args:
            run_id: The identifier of the run to cancel.

        Returns:
            The local ``RunRecord`` for manual runs, or the scheduler's
            serialized run dictionary for delegated cancellations.

        Raises:
            NotFoundServiceError: If neither the local manager nor the
                scheduler runtime knows the run.
            ServiceError: If the scheduler runtime is unreachable.
        """
        try:
            await self._run_manager.get(run_id)
        except NotFoundServiceError:
            if self._run_control_client is not None:
                return await self._run_control_client.cancel_run(run_id)
            raise
        return await self._run_manager.cancel(run_id)

    async def list_runs_view(
        self,
        *,
        page: int = 1,
        page_size: int | None = None,
        job: str | None = None,
        task: str | None = None,
        status: str | None = None,
        origin: str | None = None,
    ) -> RunsPageView:
        """Return the merged manual and scheduler runs page viewmodel.

        Manual runs always come from the local run manager. When a control
        client is configured, scheduler runs are fetched over the control
        socket and merged into one list sorted newest-first. If the scheduler
        runtime is unreachable, only manual runs are returned and
        ``scheduler_available`` is set to ``False``.

        Args:
            page: One-based page number for historical runs. Page 1 also
                includes all live runs above the history page.
            page_size: Number of historical runs per page. Defaults to 50.

        Returns:
            The runs page viewmodel including the merged ``runs`` list and a
            ``scheduler_available`` flag.
        """
        page = max(page, 1)
        effective_page_size = page_size or DEFAULT_RUNS_PAGE_SIZE
        effective_page_size = max(effective_page_size, 1)
        filters = _run_filters(job=job, task=task, status=status, origin=origin)
        live_views, live_ids, scheduler_available = await self._live_run_views()
        active_views = [view for view in live_views if view["is_active"]]
        active_views.sort(key=_run_view_sort_key, reverse=True)
        views = list(live_views) if page == 1 else []

        history_offset = (page - 1) * effective_page_size
        history_limit = effective_page_size + 1

        history_views: list[RunView] = []
        has_next_page = False
        history_filter_values: dict[str, list[str]] = {
            "jobs": [],
            "tasks": [],
            "statuses": [],
            "origins": [],
        }
        if self._run_history_service is not None:
            history_filter_values = await self._run_history_service.list_filter_values()
            history = await self._run_history_service.list_history(
                limit=history_limit,
                offset=history_offset,
                exclude_run_ids=live_ids,
                job=filters["job"] or None,
                task=filters["task"] or None,
                status=filters["status"] or None,
                origin=filters["origin"] or None,
            )
            for record in history:
                history_views.append(historical_run_list_view(record))
            if len(history_views) > effective_page_size:
                has_next_page = True
                history_views = history_views[:effective_page_size]

        views.extend(history_views)

        views.sort(key=_run_view_sort_key, reverse=True)
        has_previous_page = page > 1
        filter_query = _run_filter_query(filters)
        return {
            "runs": views,
            "active_runs": active_views,
            "history_runs": history_views,
            "active_count": len(active_views),
            "history_count": len(history_views),
            "total_count": len(views),
            "scheduler_available": scheduler_available,
            "page": page,
            "page_size": effective_page_size,
            "has_next_page": has_next_page,
            "has_previous_page": has_previous_page,
            "next_page_url": _runs_page_url(page + 1, filter_query) if has_next_page else None,
            "previous_page_url": (
                _runs_page_url(page - 1, filter_query) if has_previous_page else None
            ),
            "fragment_url": "/runs/fragments/list",
            "filters": filters,
            "filter_options": _run_filter_options(
                config=self._load_config_for_filter_options(),
                history_filter_values=history_filter_values,
                live_views=live_views,
                selected=filters,
            ),
            "filter_clear_url": "/runs",
        }

    async def list_active_runs_view(self) -> ActiveRunsView:
        """Return currently active runs merged from run manager and scheduler control.

        Used by the dashboard runs panel; entries carry the shared status
        fragment fields (``status_url``, ``cancel_url``), so cancellation works
        directly from the panel. If the scheduler runtime is unreachable, only
        local manual runs are returned and ``scheduler_available`` is False.
        """
        views, _live_ids, scheduler_available = await self._live_run_views()
        active = [view for view in views if view["is_active"]]
        active.sort(key=_run_view_sort_key, reverse=True)
        return {"runs": active, "scheduler_available": scheduler_available}

    async def _live_run_views(self) -> tuple[list[RunView], set[str], bool]:
        records = await self._run_manager.list()
        views = [_run_record_view(record) for record in records]
        live_ids = {record.run_id for record in records}
        scheduler_available = True
        if self._run_control_client is not None:
            try:
                scheduler_runs = await self._run_control_client.list_runs()
            except ServiceError as exc:
                logger.info("Scheduler runs unavailable: %s (%s)", exc.code, exc.message)
                scheduler_available = False
            else:
                views.extend(_scheduler_run_view(data) for data in scheduler_runs)
                live_ids.update(
                    str(data["run_id"])
                    for data in scheduler_runs
                    if isinstance(data.get("run_id"), str)
                )
        else:
            scheduler_available = False
        return views, live_ids, scheduler_available

    async def active_run_job_names(self) -> set[str]:
        """Return the set of job names that currently have an active run.

        Combines live runs from the local run manager with scheduler runs from
        the control socket; a job counts as active while any of its runs is
        ``queued`` or ``running``. Used by the dashboard live-status indicator.
        If the scheduler control socket is unreachable, local run manager jobs
        are still returned; callers use ``run_status_available()`` separately
        to detect the degraded scheduler state.
        """
        jobs: set[str] = set()
        for record in await self._run_manager.list():
            if str(record.status) in _ACTIVE_STATUS_CODES:
                jobs.add(record.job)
        if self._run_control_client is not None:
            try:
                scheduler_runs = await self._run_control_client.list_runs()
            except ServiceError as exc:
                logger.info("Scheduler runs unavailable: %s (%s)", exc.code, exc.message)
            else:
                for data in scheduler_runs:
                    if str(data.get("status", "")) in _ACTIVE_STATUS_CODES:
                        job = str(data.get("job", ""))
                        if job:
                            jobs.add(job)
        return jobs

    async def run_status_available(self) -> bool:
        if self._run_control_client is None:
            return False
        try:
            await self._run_control_client.list_runs()
        except ServiceError as exc:
            logger.info("Scheduler run status unavailable: %s (%s)", exc.code, exc.message)
            return False
        return True

    async def get_run_view(self, run_id: str) -> RunView:
        """Return one run detail viewmodel.

        Steps and restore details are loaded from the shared AppData DB and
        merged in regardless of whether the run is still live or already
        historical: ``run_steps``/``run_restores`` rows are written as soon as
        each step starts, so an active run's detail page reflects progress
        already made instead of staying empty until the run terminates.
        """
        try:
            record = await self._run_manager.get(run_id)
        except NotFoundServiceError:
            scheduler_run = await self._get_scheduler_run(run_id)
            if scheduler_run is not None:
                return await self._run_view_with_details(_scheduler_run_view(scheduler_run), run_id)
            if self._run_history_service is not None:
                historical = await self._run_history_service.get(run_id)
                if historical is not None:
                    return await self._historical_run_view(historical)
            raise
        return await self._run_view_with_details(_run_record_view(record), run_id)

    async def _historical_run_view(self, record: RunRecord) -> RunView:
        view = await self._run_view_with_details(_run_record_view(record), record.run_id)
        view["is_active"] = False
        view["is_cancellable"] = False
        view["log_stream_url"] = None
        _mark_historical_active_run_stale(view, record)
        return view

    async def _run_view_with_details(self, view: RunView, run_id: str) -> RunView:
        if self._run_history_service is None:
            return view
        details = await _load_run_details(self._run_history_service.db_path, run_id)
        view["steps"] = details["steps"]
        view["restore"] = details["restore"]
        view["has_steps"] = details["has_steps"]
        view["has_restore"] = details["has_restore"]
        restore = details["restore"]
        if restore is not None:
            view["task_label"] = restore["task_label"]
        return view

    async def get_run_status_view(
        self,
        run_id: str,
        *,
        action_job: str | None = None,
        action_step: str | None = None,
        dry_run: bool | None = None,
        with_actions: bool = True,
    ) -> RunView:
        """Return a compact status-fragment viewmodel for HTMX polling.

        Local manual runs are served from the run manager. If the run is
        unknown locally and a control client is configured, the status is
        fetched from the scheduler runtime over the control socket. When
        ``with_actions`` is ``False`` the run-trigger button is suppressed (used
        by the runs overview list, which is a view, not a control surface).
        """
        try:
            record = await self._run_manager.get(run_id)
        except NotFoundServiceError:
            scheduler_run = await self._get_scheduler_run(run_id)
            if scheduler_run is not None:
                return self.scheduler_status_view(scheduler_run)
            if self._run_history_service is not None:
                historical = await self._run_history_service.get(run_id)
                if historical is not None:
                    view = _run_record_view(historical)
                    view["is_active"] = False
                    view["is_cancellable"] = False
                    view["log_stream_url"] = None
                    _mark_historical_active_run_stale(view, historical)
                    allow_actions = with_actions and historical.origin is not RunOrigin.SCHEDULER
                    return _apply_run_action(
                        view,
                        action_job=action_job,
                        action_step=action_step,
                        dry_run=dry_run,
                        record_dry_run=historical.dry_run,
                        with_actions=allow_actions,
                    )
            raise
        view = _run_record_view(record)
        return _apply_run_action(
            view,
            action_job=action_job,
            action_step=action_step,
            dry_run=dry_run,
            record_dry_run=record.dry_run,
            with_actions=with_actions,
        )

    def scheduler_status_view(self, data: dict[str, object]) -> RunView:
        """Return a status-fragment viewmodel for a scheduler run dictionary.

        Scheduler runs are not tracked by the local run manager, so their
        status fragment is built directly from the control-socket payload. No
        run-action buttons are offered for scheduler runs.

        Args:
            data: A serialized scheduler run record from the control client.

        Returns:
            A status-fragment viewmodel compatible with ``run_status.html``.
        """
        view = _scheduler_run_view(data)
        view["action_job"] = ""
        view["action_step"] = ""
        view["action_target"] = None
        return view

    def _load_config_for_filter_options(self) -> ResolvedAppConfig | None:
        try:
            return self._config_service.load_active_config()
        except ServiceError:
            return None

    async def _get_scheduler_run(self, run_id: str) -> dict[str, object] | None:
        if self._run_control_client is None:
            return None
        try:
            scheduler_runs = await self._run_control_client.list_runs()
        except ServiceError as exc:
            logger.info("Scheduler run lookup unavailable: %s (%s)", exc.code, exc.message)
            return None
        for data in scheduler_runs:
            if data.get("run_id") == run_id:
                return data
        return None

    async def _execute(
        self,
        parsed: ParsedTaskSelector,
        config: ResolvedAppConfig,
        dry_run: bool,
        mark_not_cancellable: Callable[[], None],
        run_id: str,
    ) -> bool:
        job = config.jobs[parsed.job]
        on_success = (
            self._stats_collector.collect_and_store_async
            if self._stats_collector is not None
            else None
        )
        runner = JobRunner(
            parsed.job,
            job,
            lock_dir=self._lock_dir,
            log_level=config.global_.log_level,
            log_base_dir=self._log_base_dir,
            dry_run=dry_run,
            on_backup_success=on_success,
            on_operational_complete=mark_not_cancellable,
            run_history_service=self._run_history_service,
            run_id=run_id,
            lock_retry_count=config.global_.lock_retry_count,
            lock_retry_delay=config.global_.lock_retry_delay,
        )
        return await _run_parsed_task_selector(runner, parsed)


def parse_task_selector(task_selector: str) -> ParsedTaskSelector:
    """Parse a backup, rclone or workflow task selector."""
    try:
        return parse_run_task_selector(task_selector)
    except ValueError as exc:
        raise ServiceError("invalid_task_selector", str(exc), 400) from exc


async def _run_parsed_task_selector(runner: JobRunner, parsed: ParsedTaskSelector) -> bool:
    if parsed.kind == "backup":
        return await runner.run_backup(parsed.name)
    if parsed.kind == "workflow":
        return await runner.run_workflow(parsed.name)
    if parsed.kind == "backup_step":
        return await runner.run_step(f"backup.{parsed.name}.{parsed.substep}")
    return await runner.run_step(f"rclone.{parsed.name}")


_ACTIVE_STATUS_CODES = {"queued", "running"}

_STATUS_VIEW_BY_CODE: dict[str, tuple[str, str]] = {
    "queued": ("Queued", "amber"),
    "running": ("Running", "blue"),
    "success": ("Done", "green"),
    "failed": ("Failed", "red"),
    "skipped": ("Skipped", "slate"),
    "lock_error": ("Already active", "amber"),
    "config_error": ("Config error", "red"),
    "cancelled": ("Cancelled", "slate"),
    "runtime_stopping": ("Shutting down", "amber"),
    "unexpected_error": ("Unexpected error", "red"),
}

_START_ERROR_VIEW_BY_CODE: dict[str, tuple[str, str]] = {
    "config_error": ("Config error", "red"),
    "invalid_parameter": ("Invalid request", "red"),
    "invalid_task_selector": ("Invalid target", "red"),
    "not_found": ("Not found", "red"),
    "run_not_found": ("Run not found", "red"),
    "task_not_found": ("Task not found", "red"),
}

_ORIGIN_LABELS = {
    RunOrigin.MANUAL.value: "Manual",
    RunOrigin.SCHEDULER.value: "Scheduler",
}


def _run_filters(
    *,
    job: str | None = None,
    task: str | None = None,
    status: str | None = None,
    origin: str | None = None,
) -> RunFiltersView:
    filters = {
        "job": _clean_filter_value(job),
        "task": _clean_filter_value(task),
        "status": _clean_filter_value(status),
        "origin": _clean_filter_value(origin),
    }
    return {
        "job": filters["job"],
        "task": filters["task"],
        "status": filters["status"],
        "origin": filters["origin"],
        "is_active": any(filters.values()),
    }


def _clean_filter_value(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _run_filter_query(filters: RunFiltersView) -> dict[str, str]:
    query: dict[str, str] = {}
    if filters["job"]:
        query["job"] = filters["job"]
    if filters["task"]:
        query["task"] = filters["task"]
    if filters["status"]:
        query["status"] = filters["status"]
    if filters["origin"]:
        query["origin"] = filters["origin"]
    return query


def _runs_page_url(page: int, filter_query: dict[str, str]) -> str:
    query = {"page": str(page), **filter_query}
    return f"/runs?{urlencode(query)}"


def _run_filter_options(
    *,
    config: ResolvedAppConfig | None,
    history_filter_values: dict[str, list[str]],
    live_views: list[RunView],
    selected: RunFiltersView,
) -> RunFilterOptionsView:
    job_values = set(history_filter_values.get("jobs", []))
    if config is not None:
        job_values.update(config.jobs.keys())
    job_values.update(view["job"] for view in live_views if view.get("job"))
    if selected["job"]:
        job_values.add(selected["job"])

    task_values = set(history_filter_values.get("tasks", []))
    if config is not None:
        task_values.update(_config_task_filter_values(config))
    task_values.update(_task_filter_value(view) for view in live_views if _task_filter_value(view))
    if selected["task"]:
        task_values.add(selected["task"])

    status_values = set(history_filter_values.get("statuses", []))
    status_values.update(view["status"] for view in live_views if view.get("status"))
    if selected["status"]:
        status_values.add(selected["status"])

    origin_values = set(history_filter_values.get("origins", []))
    origin_values.update(view["origin"] for view in live_views if view.get("origin"))
    if selected["origin"]:
        origin_values.add(selected["origin"])

    return {
        "jobs": [{"value": value, "label": value} for value in sorted(job_values)],
        "tasks": [
            {"value": value, "label": _task_filter_label(value)} for value in sorted(task_values)
        ],
        "statuses": [
            {"value": value, "label": _STATUS_VIEW_BY_CODE.get(value, (value, ""))[0]}
            for value in _ordered_values(status_values, list(_STATUS_VIEW_BY_CODE))
        ],
        "origins": [
            {"value": value, "label": _ORIGIN_LABELS.get(value, value)}
            for value in _ordered_values(origin_values, list(_ORIGIN_LABELS))
        ],
    }


def _ordered_values(values: set[str], preferred_order: list[str]) -> list[str]:
    preferred = [value for value in preferred_order if value in values]
    remaining = sorted(values.difference(preferred_order))
    return preferred + remaining


def _config_task_filter_values(config: ResolvedAppConfig) -> set[str]:
    values: set[str] = set()
    for job in config.jobs.values():
        values.update(f"backup.{name}" for name in job.backup)
        values.update(f"workflow.{name}" for name in job.workflows)
        values.update(f"rclone.{name}" for name in job.rclone)
    return values


def _task_filter_value(view: RunView) -> str:
    step = view.get("step")
    if isinstance(step, str) and step:
        return step
    task_kind = view.get("task_kind")
    task_name = view.get("task_name")
    if task_kind == "restore" and isinstance(task_name, str):
        return f"restore.{task_name.split('.', 1)[-1]}"
    return ""


def _task_filter_label(value: str) -> str:
    task_type, _separator, task_name = value.partition(".")
    label, _tone, _kind = _task_type_label_parts(task_type)
    return f"{label} {task_name}" if task_name else value


def _task_type_label_parts(task_type: str) -> tuple[str, str, str]:
    if task_type == "rclone":
        return "Rclone", "success", "rclone"
    if task_type == "backup":
        return "Backup", "info", "backup"
    if task_type == "workflow":
        return "Workflow", "neutral", "workflow"
    if task_type == "restore":
        return "Restore", "warning", "restore"
    return "Task", "neutral", "task"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return utc_rfc3339(value)


def _duration_between(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    if started_at is None:
        return None
    end = finished_at or datetime.now(timezone.utc)
    return round((end - started_at).total_seconds(), 1)


def _status_view_by_code(status_code: str) -> tuple[str, str, str]:
    label, tone = _STATUS_VIEW_BY_CODE.get(status_code, ("Unknown status", "slate"))
    return status_code, label, tone


def _start_error_status_view_by_code(status_code: str) -> tuple[str, str, str]:
    label, tone = _START_ERROR_VIEW_BY_CODE.get(
        status_code,
        _STATUS_VIEW_BY_CODE.get(status_code, ("Start failed", "red")),
    )
    return status_code, label, tone


def _task_type_from_parsed(parsed: ParsedTaskSelector) -> str:
    if parsed.kind == "backup_step":
        return "backup"
    return parsed.kind


def _task_name_from_parsed(parsed: ParsedTaskSelector) -> str:
    if parsed.kind == "backup_step" and parsed.substep:
        return f"{parsed.name}.{parsed.substep}"
    return parsed.name


class StructuredTaskDisplay(TypedDict):
    task_kind: str
    task_type_label: str
    task_type_tone: str
    task_name: str
    task_substep_label: str | None
    task_label: str


def _structured_task_display(
    run_kind: str,
    job: str,
    task_type: str,
    task_name: str,
) -> StructuredTaskDisplay:
    if run_kind == RunKind.RESTORE.value or task_type == "restore":
        return {
            "task_kind": "restore",
            "task_type_label": "Restore",
            "task_type_tone": "warning",
            "task_name": f"{job}.{task_name}",
            "task_substep_label": None,
            "task_label": f"Restore {job}.{task_name}",
        }
    substep = None
    display_name = task_name
    if task_type == "backup" and "." in task_name:
        maybe_name, maybe_substep = task_name.rsplit(".", 1)
        if maybe_substep in {"backup", "retention", "cleanup"} and maybe_name:
            display_name = maybe_name
            substep = maybe_substep

    if task_type == "rclone":
        label, tone, kind = "Rclone", "success", "rclone"
    elif task_type == "backup":
        label, tone, kind = "Backup", "info", "backup"
    elif task_type == "workflow":
        label, tone, kind = "Workflow", "neutral", "workflow"
    else:
        label, tone, kind = "Task", "neutral", "task"
    type_label = f"{label} ({substep})" if substep else label
    return {
        "task_kind": kind,
        "task_type_label": type_label,
        "task_type_tone": tone,
        "task_name": display_name,
        "task_substep_label": substep,
        "task_label": f"{type_label} {display_name}" if kind != "task" else display_name,
    }


def _target_primary(job: str, task_name: object) -> str:
    task = str(task_name or "").strip()
    if job and task:
        return f"{job}: {task}"
    return job or task or "Task"


def _target_secondary(details: object = None) -> str | None:
    if not details:
        return None
    return str(details)


def build_action_target(job: object, step: object, dry_run: bool = False) -> str | None:
    if not job or not step:
        return None
    return f"/jobs/{job}/{step}/{'dry-run' if dry_run else 'run'}"


def _run_status_error_view(
    job: str,
    step: str,
    *,
    dry_run: bool,
    status: str,
    error: str,
) -> RunView:
    _status, status_label, status_tone = _start_error_status_view_by_code(status)
    return {
        "status": status,
        "status_label": status_label,
        "status_tone": status_tone,
        "is_active": False,
        "is_cancellable": False,
        "error": error,
        "job": job,
        "step": step,
        "action_job": job,
        "action_step": step,
        "dry_run": dry_run,
        "action_target": build_action_target(job, step, dry_run),
    }


def _build_run_view(
    *,
    run_id: str,
    origin: str,
    run_kind: str,
    job: str,
    task_type: str,
    task_name: str,
    status_code: str,
    dry_run: bool,
    cancellable: bool,
    error: str | None,
    created_at: datetime | None,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> RunView:
    status_code, status_label, status_tone = _status_view_by_code(status_code)
    display = _structured_task_display(run_kind, job, task_type, task_name)
    is_active = status_code in _ACTIVE_STATUS_CODES
    try:
        target = format_run_target(RunKind(run_kind), job, task_type, task_name)
    except ValueError:
        target = f"{job}.{task_type}.{task_name}" if job else task_name

    if run_kind == RunKind.RESTORE.value:
        restore_job = job
        step = ""
        task_label = str(display["task_label"])
        detail_url: str = f"/runs/{run_id}"
        logs_url: str | None = None
        log_stream_url: str | None = (
            f"/diagnostics/logs/{restore_job}/stream" if is_active else None
        )
    else:
        step = f"{task_type}.{task_name}"
        task_label = target
        detail_url = f"/runs/{run_id}"
        logs_url = f"/diagnostics/logs?job={job}" if job else "/diagnostics/logs"
        log_stream_url = f"/diagnostics/logs/{job}/stream" if job and is_active else None
    display_task_name = task_name if run_kind == RunKind.RESTORE.value else display["task_name"]

    return {
        "run_id": run_id,
        "origin": origin,
        "origin_label": _ORIGIN_LABELS.get(origin, origin or "—"),
        "run_kind": run_kind,
        "target": target,
        "task_label": display["task_label"] or task_label,
        "task_kind": display["task_kind"],
        "task_type_label": display["task_type_label"],
        "task_type_tone": display["task_type_tone"],
        "task_name": display["task_name"],
        "task_substep_label": display["task_substep_label"],
        "target_primary": _target_primary(job, display_task_name),
        "target_secondary": _target_secondary(),
        "job": job,
        "step": step,
        "dry_run": dry_run,
        "status": status_code,
        "status_label": status_label,
        "status_tone": status_tone,
        "is_active": is_active,
        "is_cancellable": is_active and cancellable,
        "error": error,
        "created_at": _format_timestamp(created_at),
        "started_at": _format_timestamp(started_at),
        "finished_at": _format_timestamp(finished_at),
        "duration_seconds": _duration_between(started_at, finished_at),
        "detail_url": detail_url,
        "status_url": f"/runs/{run_id}/status",
        "cancel_url": f"/runs/{run_id}/cancel",
        "logs_url": logs_url,
        "log_stream_url": log_stream_url,
        "steps": [],
        "restore": None,
        "has_steps": False,
        "has_restore": False,
    }


async def _load_run_details(db_path: Path, run_id: str) -> RunDetailsView:
    return await asyncio.to_thread(_load_run_details_sync, db_path, run_id)


def _load_run_details_sync(db_path: Path, run_id: str) -> RunDetailsView:
    with closing(connect_appdata_db(db_path)) as conn:
        step_rows = conn.execute(
            """
            SELECT run_step_id, position, step, backend, task_type, task_name,
                   started_at, finished_at, status, error, effective_task_config_json
            FROM run_steps
            WHERE run_id = ?
            ORDER BY position ASC
            """,
            (run_id,),
        ).fetchall()
        restore_row = conn.execute(
            """
            SELECT run_restore_id, run_id, job, backup, backend, snapshot_id, mode,
                   restore_target, snapshot_paths_json, include_patterns_json,
                   exclude_patterns_json, overwrite, error, output, output_truncated
            FROM run_restores
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    steps = [_run_step_view(row) for row in step_rows]
    restore = _run_restore_detail_view(restore_row) if restore_row is not None else None
    return {
        "steps": steps,
        "restore": restore,
        "has_steps": bool(steps),
        "has_restore": restore is not None,
    }


def _run_step_view(row: sqlite3.Row) -> RunStepView:
    status, status_label, status_tone = _status_view_by_code(str(row["status"]))
    return {
        "run_step_id": str(row["run_step_id"]),
        "position": int(row["position"]),
        "step": str(row["step"]),
        "backend": str(row["backend"]),
        "task_type": str(row["task_type"]),
        "task_name": str(row["task_name"]),
        "status": status,
        "status_label": status_label,
        "status_tone": status_tone,
        "started_at": _format_timestamp(_parse_timestamp(row["started_at"])),
        "finished_at": _format_timestamp(_parse_timestamp(row["finished_at"])),
        "duration_seconds": _duration_between(
            _parse_timestamp(row["started_at"]),
            _parse_timestamp(row["finished_at"]),
        ),
        "error": row["error"],
        "effective_task_config_pretty": _pretty_task_config(row["effective_task_config_json"]),
    }


def _run_restore_detail_view(row: sqlite3.Row) -> RunRestoreDetailView:
    snapshot_id = str(row["snapshot_id"])
    short_snapshot = snapshot_id[:8]
    job = str(row["job"])
    backup = str(row["backup"])
    run_id = str(row["run_id"])
    return {
        "run_restore_id": str(row["run_restore_id"]),
        "run_id": run_id,
        "job": job,
        "backup": backup,
        "backend": str(row["backend"]),
        "snapshot_id": snapshot_id,
        "mode": str(row["mode"]),
        "restore_target": str(row["restore_target"]),
        "snapshot_paths": _loads_json_list(row["snapshot_paths_json"]),
        "include_patterns": _loads_json_list(row["include_patterns_json"]),
        "exclude_patterns": _loads_json_list(row["exclude_patterns_json"]),
        "overwrite": bool(row["overwrite"]),
        "error": row["error"],
        "output": row["output"],
        "output_truncated": bool(row["output_truncated"]),
        "task_label": f"Restore {job}.{backup} @ {short_snapshot}",
    }


def _loads_json_list(value: object) -> list[object]:
    if not isinstance(value, str) or not value:
        return []
    decoded = json.loads(value)
    return decoded if isinstance(decoded, list) else []


def _pretty_task_config(value: object) -> str:
    """Formatiert den persistierten Config-Snapshot eines Steps fuer die Anzeige.

    Der Schreibpfad legt zu jedem Step ein JSON-Objekt ab, ein Step ohne
    Snapshot existiert also nicht. Unlesbarer Inhalt wird roh durchgereicht,
    damit eine einzelne kaputte Zeile nicht die ganze Detailseite kippt.
    """
    text = value if isinstance(value, str) else str(value)
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True)


def _apply_run_action(
    view: RunView,
    *,
    action_job: str | None,
    action_step: str | None,
    dry_run: bool | None,
    record_dry_run: bool,
    with_actions: bool,
) -> RunView:
    """Attach (or suppress) the run-trigger action fields on a status view.

    When ``with_actions`` is ``False`` the action target is blanked so the
    status fragment renders no run-trigger button (used by the runs overview).
    """
    view["dry_run"] = record_dry_run if dry_run is None else dry_run
    if not with_actions:
        view["action_job"] = ""
        view["action_step"] = ""
        view["action_target"] = None
        return view
    view["action_job"] = action_job or view["job"]
    view["action_step"] = action_step or view["step"]
    view["action_target"] = build_action_target(
        view["action_job"], view["action_step"], bool(view["dry_run"])
    )
    return view


def _run_record_view(record: RunRecord) -> RunView:
    return _build_run_view(
        run_id=record.run_id,
        origin=record.origin.value,
        run_kind=record.run_kind.value,
        job=record.job,
        task_type=record.task_type,
        task_name=record.task_name,
        status_code=str(record.status),
        dry_run=record.dry_run,
        cancellable=record.cancellable,
        error=record.error,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def historical_run_list_view(record: RunRecord) -> RunView:
    """Baut die geteilte Listen-Viewmodel-Zeile fuer einen historisierten Run.

    Wird von der Runs-Seite und vom Dashboard-Runs-Panel verwendet, damit
    History-Zeilen ueberall identisch dargestellt werden.
    """
    view = _run_record_view(record)
    view["is_active"] = False
    view["is_cancellable"] = False
    view["log_stream_url"] = None
    _mark_historical_active_run_stale(view, record)
    return view


def _mark_historical_active_run_stale(view: RunView, record: RunRecord) -> None:
    if record.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
        return
    view["status_label"] = "Interrupted"
    view["status_tone"] = "amber"
    view["error"] = record.error or "Run was left active by a previous process."


def _scheduler_run_view(data: dict[str, object]) -> RunView:
    """Convert a scheduler run dictionary into the shared run viewmodel.

    Args:
        data: A serialized scheduler run record from the control client.

    Returns:
        A run viewmodel matching the manual-run viewmodel shape.
    """
    error = data.get("error")
    run_kind = str(data.get("run_kind", RunKind.JOB_TASK.value))
    job = str(data.get("job", ""))
    task_type = str(data.get("task_type", "task"))
    task_name = str(data.get("task_name", data.get("target", "")))
    return _build_run_view(
        run_id=str(data.get("run_id", "")),
        origin=str(data.get("origin", "")),
        run_kind=run_kind,
        job=job,
        task_type=task_type,
        task_name=task_name,
        status_code=str(data.get("status", "")),
        dry_run=bool(data.get("dry_run", False)),
        cancellable=bool(data.get("cancellable", False)),
        error=error if isinstance(error, str) else None,
        created_at=_parse_timestamp(data.get("created_at")),
        started_at=_parse_timestamp(data.get("started_at")),
        finished_at=_parse_timestamp(data.get("finished_at")),
    )


def _run_view_sort_key(view: RunView) -> str:
    for key in ("finished_at", "started_at", "created_at"):
        value = view.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
