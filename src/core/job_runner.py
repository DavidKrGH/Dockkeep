"""Job Runner mit Locking-Mechanismus."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..models.resolved_config import (
    ResolvedJobConfig,
    ResolvedWorkflowConfig,
)
from ..utils.logging import log_base_dir as default_log_base_dir
from ..utils.logging import log_blank_line, log_task_context, setup_job_logger
from .locking import (
    JobAlreadyRunningError,
    ResourceLock,
    ResourceLockManager,
    resource_for_rclone_endpoint,
    resource_for_repository,
)
from .locking import (
    lock_dir as default_lock_dir,
)
from .workflow import WorkflowEngine

if TYPE_CHECKING:
    from ..services.run_history import RunHistoryService


class UnexpectedRunError(Exception):
    """Signals that a JobRunner operation hit a truly unexpected exception.

    It is re-raised so a surrounding ``RunManager`` can record the terminal run
    status as ``unexpected_error`` instead of a plain ``failed``.
    """

    def __init__(self, message: str, *, error: str | None = None) -> None:
        super().__init__(message)
        self.error = error


@dataclass(frozen=True)
class BackupRunStatsContext:
    """Successful restic substep context for artifact/stat post-processing."""

    run_id: str | None = None
    run_step_id: str | None = None
    backup_summary: dict[str, object] | None = None
    trigger_kind: str = "backup"


_BACKUP_SUBSTEPS = ("backup", "retention", "cleanup")


def _step_to_lock_task(step: str) -> str:
    """Leitet den Lock-Task-Namen aus einem Step-String ab.

    Args:
        step: Step-String, z.B. "rclone.offsite", "backup.local", "backup.local.backup".

    Returns:
        Task-Name für den Lock: der Backup- oder Rclone-Task-Name (nur Name-Teil).

    Raises:
        ValueError: Wenn das Step-Format ungültig ist.
    """
    parts = step.split(".")
    if parts[0] == "rclone" and len(parts) == 2 and parts[1]:
        return parts[1]
    if parts[0] == "backup" and len(parts) in {2, 3} and parts[1]:
        if len(parts) == 3 and parts[2] not in _BACKUP_SUBSTEPS:
            raise ValueError(f"Invalid step format: '{step}' (expected 'backup.NAME[.substep]')")
        return parts[1]
    raise ValueError(
        f"Invalid step format: '{step}' (expected 'backup.NAME[.substep]' or 'rclone.NAME')"
    )


class JobRunner:
    """Führt Backups, Steps und Workflows eines Jobs mit Resource-Locking aus.

    Erwirbt vor jeder Ausführung exklusive Locks für die betroffenen
    Repositories und Rclone-Endpunkte. Workflows halten die Vereinigungsmenge
    ihrer Step-Ressourcen über die gesamte Laufzeit.

    Die eigentliche Executor-Dispatch-Logik liegt in WorkflowEngine; dieser
    Runner ist ausschließlich für Lock-Management und Routing zuständig.

    Attributes:
        job_name: Name des Jobs (Dict-Key aus ResolvedAppConfig.jobs).
        job_config: Vollständige Job-Konfiguration.
    """

    def __init__(
        self,
        job_name: str,
        job_config: ResolvedJobConfig,
        lock_dir: Path | None = None,
        log_level: str = "debug",
        log_base_dir: Path | None = None,
        dry_run: bool = False,
        on_backup_success: (
            Callable[[str, str, BackupRunStatsContext], Awaitable[object]] | None
        ) = None,
        on_operational_complete: Callable[[], object] | None = None,
        run_history_service: "RunHistoryService | None" = None,
        run_id: str | None = None,
        lock_retry_count: int | None = None,
        lock_retry_delay: int | None = None,
    ) -> None:
        """Initialisiert den JobRunner.

        Richtet den Job-spezifischen Logger mit tagesaktuellem File-Handler ein,
        bevor das erste Backup, Step oder Workflow ausgeführt wird.

        Args:
            job_name: Name des Jobs (Dict-Key aus ResolvedAppConfig.jobs).
            job_config: Vollständige Job-Konfiguration.
            lock_dir: Verzeichnis für Lock-Dateien (Standard: /var/lock).
                Kann für Tests auf ein temporäres Verzeichnis gesetzt werden.
            log_level: Log-Level für diesen Job (Standard: "debug").
            log_base_dir: Basis-Verzeichnis für Log-Dateien (Standard: /logs).
                Kann für Tests auf ein temporäres Verzeichnis gesetzt werden.
            dry_run: Wenn True, wird --dry-run durch die gesamte Ausführungskette
                bis zu den Executors weitergereicht.
            on_backup_success: Optional async callback invoked after successful restic
                backup/retention/cleanup substeps. Not called on dry_run. Exceptions are swallowed.
            on_operational_complete: Optional callback invoked after operative locks are
                complete and before awaited post-processing starts, while resource locks are
                still held.
            run_id: Optional RunManager ID to attach to stats post-processing.
        """
        self.job_name = job_name
        self.job_config = job_config
        self._dry_run = dry_run
        self._on_backup_success = on_backup_success
        self._on_operational_complete = on_operational_complete
        self._run_history_service = run_history_service
        self._run_id = run_id
        self._lock_retry_count = lock_retry_count or 0
        self._lock_retry_delay = lock_retry_delay
        self._last_successful_step_ids: dict[tuple[str, str], list[str]] = {}
        self._log_base_dir = log_base_dir if log_base_dir is not None else default_log_base_dir()
        self._lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self.logger = setup_job_logger(
            job_name, log_level=log_level, log_base_dir=self._log_base_dir
        )
        self._engine = WorkflowEngine(
            job_name,
            job_config,
            dry_run=dry_run,
            on_step_started=self._create_run_step,
            on_step_finished=self._finish_run_step,
        )

    async def run_backup(self, backup_name: str) -> bool:
        with log_task_context(f"backup.{backup_name}"):
            log_blank_line(self.logger)
            if backup_name not in self.job_config.backup:
                self.logger.error("Backup '%s' not found in job '%s'", backup_name, self.job_name)
                return False

            self.logger.info(
                "── BACKUP: %s ──────────────────────────────────────────────", backup_name.upper()
            )
            resources = self._resources_for_backup(backup_name)
            return await self._run_locked(
                kind="backup",
                target=backup_name,
                resources=resources,
                lock_task_name=backup_name,
                operation=lambda: self._engine.execute_backup(backup_name),
                collect=lambda success: self._collect_backup_run_step_successes(
                    backup_name, success
                ),
            )

    async def run_step(self, step: str) -> bool:
        with log_task_context(step):
            log_blank_line(self.logger)
            self.logger.info(
                "── STEP: %s ──────────────────────────────────────────────", step.upper()
            )
            # Step-format and existence checks run before the operational try block:
            # an invalid step format or unknown task is a validation failure (False ->
            # FAILED), not an unexpected runtime exception, and must not be re-raised
            # as UnexpectedRunError.
            try:
                task_name = _step_to_lock_task(step)
            except ValueError as exc:
                self.logger.error("%s", exc)
                return False
            parts = step.split(".")
            if parts[0] == "backup" and task_name not in self.job_config.backup:
                self.logger.error("Backup '%s' not found in job '%s'", task_name, self.job_name)
                return False
            if parts[0] == "rclone" and task_name not in self.job_config.rclone:
                self.logger.error(
                    "Rclone task '%s' not found in job '%s'", task_name, self.job_name
                )
                return False

            resources = self._resources_for_step(step)
            return await self._run_locked(
                kind="step",
                target=step,
                resources=resources,
                lock_task_name=task_name,
                operation=lambda: self._engine.execute_step(step),
                collect=lambda success: self._collect_step_successes(step, task_name, success),
            )

    async def run_workflow(self, workflow_name: str) -> bool:
        with log_task_context(f"workflow.{workflow_name}"):
            log_blank_line(self.logger)
            self.logger.info(
                "── WORKFLOW: %s ──────────────────────────────────────────────",
                workflow_name.upper(),
            )
            workflow = self.job_config.workflows.get(workflow_name)
            if workflow is None:
                self.logger.error(
                    "Workflow '%s' not found in job '%s'", workflow_name, self.job_name
                )
                return False

            resources = self._resources_for_workflow(workflow)
            if resources is None:
                return False
            return await self._run_locked(
                kind="workflow",
                target=workflow_name,
                resources=resources,
                lock_task_name=workflow_name,
                operation=lambda: self._engine.execute_workflow(workflow_name, workflow),
                collect=lambda success: self._collect_workflow_step_successes(workflow, success),
            )

    async def _run_locked(
        self,
        *,
        kind: str,
        target: str,
        resources: set[ResourceLock],
        lock_task_name: str,
        operation: Callable[[], Awaitable[bool]],
        collect: Callable[[bool], Awaitable[None]],
    ) -> bool:
        success = False
        operational_started = False
        try:
            lock_manager = ResourceLockManager(
                self.job_name, lock_task_name, resources, lock_dir=self._lock_dir
            )
            async with self._acquire_with_retry(lock_manager):
                operational_started = True
                success = await self._run_with_job_hooks(operation)
                self._mark_operational_complete()
                await collect(success)
        except JobAlreadyRunningError as exc:
            self.logger.warning(
                "── %s '%s' NOT STARTED: resource lock is already held (%s)",
                kind.upper(),
                target,
                exc.lock_path,
            )
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if operational_started:
                self._mark_operational_complete()
            self.logger.exception("Unexpected error during %s '%s': %s", kind, target, exc)
            self.logger.error("── %s '%s' UNEXPECTED ERROR", kind.upper(), target)
            raise UnexpectedRunError(
                f"Unexpected error during {kind} '{target}'", error=str(exc)
            ) from exc

        if success:
            self.logger.info("── %s '%s' COMPLETED ✓", kind.upper(), target)
        else:
            self.logger.error("── %s '%s' FAILED", kind.upper(), target)
        return success

    @asynccontextmanager
    async def _acquire_with_retry(self, lock_manager: ResourceLockManager) -> AsyncIterator[None]:
        attempts = self._lock_retry_count + 1
        context: AbstractContextManager[None] | None = None
        for attempt in range(1, attempts + 1):
            try:
                context = lock_manager.acquire()
                context.__enter__()
                break
            except JobAlreadyRunningError as exc:
                if attempt >= attempts:
                    raise
                delay = self._lock_retry_delay
                if delay is None:
                    raise
                self.logger.warning(
                    "Resource lock blocked at %s (attempt %s/%s); retrying in %s seconds",
                    exc.lock_path,
                    attempt,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        if context is None:  # pragma: no cover - defensive
            raise AssertionError("unreachable lock retry state")
        try:
            yield
        finally:
            context.__exit__(None, None, None)

    def _mark_operational_complete(self) -> None:
        if self._on_operational_complete is not None:
            try:
                self._on_operational_complete()
            except Exception:
                self.logger.warning("Operational completion callback failed", exc_info=True)

    async def _create_run_step(
        self,
        step: str,
        backend: str,
        task_type: str,
        task_name: str,
        effective_task_config: object,
    ) -> str | None:
        if self._run_history_service is None or self._run_id is None:
            return None
        return await self._run_history_service.create_step(
            run_id=self._run_id,
            step=step,
            backend=backend,
            task_type=task_type,
            task_name=task_name,
            effective_task_config=effective_task_config,
        )

    async def _finish_run_step(
        self,
        run_step_id: str | None,
        step: str,
        success: bool,
        error: str | None,
        cancelled: bool = False,
    ) -> None:
        if self._run_history_service is None or run_step_id is None:
            return
        status = "success" if success else "failed"
        if cancelled:
            status = "cancelled"
        await self._run_history_service.finish_step(run_step_id, status=status, error=error)
        if success:
            parts = step.split(".")
            if len(parts) == 3 and parts[0] == "backup" and parts[2] in _BACKUP_SUBSTEPS:
                self._last_successful_step_ids.setdefault((parts[1], parts[2]), []).append(
                    run_step_id
                )

    async def _run_with_job_hooks(self, operation: Callable[[], Awaitable[bool]]) -> bool:
        if not await self._engine.run_job_hooks("pre"):
            await self._engine.run_job_hooks("on_error")
            return False

        success = await operation()
        if success:
            if not await self._engine.run_job_hooks("post"):
                self.logger.warning(
                    "post_hooks failed for job '%s' — run result remains successful",
                    self.job_name,
                )
            return True

        await self._engine.run_job_hooks("on_error")
        return False

    async def _collect_backup_success(
        self,
        backup_name: str,
        context: BackupRunStatsContext,
        label: str,
        target: str,
    ) -> None:
        if self._dry_run or self._on_backup_success is None:
            return
        try:
            await self._on_backup_success(self.job_name, backup_name, context)
        except Exception:
            self.logger.warning(
                "Stats collection failed silently for %s '%s'",
                label,
                target,
                exc_info=True,
            )

    async def _collect_backup_run_step_successes(
        self, backup_name: str, full_run_success: bool
    ) -> None:
        await self._collect_successful_backup_substep(
            backup_name,
            "backup",
            "backup",
            backup_name,
            fallback_success=full_run_success,
        )
        backup = self.job_config.backup[backup_name]
        if backup.execution.retention:
            await self._collect_successful_backup_substep(
                backup_name,
                "retention",
                "backup",
                backup_name,
                fallback_success=full_run_success,
            )
        if backup.execution.cleanup:
            await self._collect_successful_backup_substep(
                backup_name,
                "cleanup",
                "backup",
                backup_name,
                fallback_success=full_run_success,
            )

    async def _collect_step_successes(self, step: str, task_name: str, success: bool) -> None:
        parts = step.split(".")
        if parts[0] == "backup" and len(parts) == 2:
            await self._collect_backup_run_step_successes(task_name, success)
        elif parts[0] == "backup" and len(parts) == 3:
            await self._collect_successful_backup_substep(
                task_name, parts[2], "step", step, fallback_success=success
            )

    async def _collect_workflow_step_successes(
        self, workflow: ResolvedWorkflowConfig, workflow_success: bool
    ) -> None:
        for step in workflow.steps:
            parts = step.split(".")
            if parts[0] != "backup" or len(parts) not in {2, 3} or not parts[1]:
                continue
            if parts[1] not in self.job_config.backup:
                continue
            if len(parts) == 2:
                await self._collect_backup_run_step_successes(parts[1], workflow_success)
                continue
            await self._collect_successful_backup_substep(
                parts[1],
                parts[2],
                "workflow step",
                step,
                fallback_success=workflow_success,
            )

    async def _collect_successful_backup_substep(
        self,
        backup_name: str,
        substep: str,
        label: str,
        target: str,
        *,
        fallback_success: bool,
    ) -> None:
        if substep not in _BACKUP_SUBSTEPS:
            return
        pending_ids = self._last_successful_step_ids.get((backup_name, substep))
        run_step_id = pending_ids.pop(0) if pending_ids else None
        backup_summary = self._artifact_context_fields(backup_name) if substep == "backup" else None
        if (
            run_step_id is None
            and not fallback_success
            and (substep != "backup" or backup_summary is None)
        ):
            return
        await self._collect_backup_success(
            backup_name,
            BackupRunStatsContext(
                run_id=self._run_id,
                run_step_id=run_step_id,
                backup_summary=backup_summary,
                trigger_kind="backup" if substep == "backup" else substep,
            ),
            label,
            target,
        )

    def _artifact_context_fields(self, backup_name: str) -> dict[str, object] | None:
        candidate = self._engine.consume_backup_artifact_candidate(backup_name)
        if candidate is None:
            return None
        return candidate.backup_summary

    def _resources_for_backup(self, backup_name: str) -> set[ResourceLock]:
        backup = self.job_config.backup[backup_name]
        return {resource_for_repository(backup.repository)}

    def _resources_for_step(self, step: str) -> set[ResourceLock]:
        task_name = _step_to_lock_task(step)
        parts = step.split(".")
        if parts[0] == "rclone":
            rclone_name = task_name
            rclone = self.job_config.rclone[rclone_name]
            return {
                resource_for_rclone_endpoint(rclone.source),
                resource_for_rclone_endpoint(rclone.target),
            }
        return self._resources_for_backup(task_name)

    def _resources_for_workflow(self, workflow: ResolvedWorkflowConfig) -> set[ResourceLock] | None:
        resources: set[ResourceLock] = set()
        for step in workflow.steps:
            try:
                task_name = _step_to_lock_task(step)
            except ValueError as exc:
                self.logger.error("%s", exc)
                return None
            parts = step.split(".")
            if parts[0] == "backup" and task_name not in self.job_config.backup:
                self.logger.error("Backup '%s' not found in job '%s'", task_name, self.job_name)
                return None
            if parts[0] == "rclone" and task_name not in self.job_config.rclone:
                self.logger.error(
                    "Rclone task '%s' not found in job '%s'", task_name, self.job_name
                )
                return None
            resources.update(self._resources_for_step(step))
        return resources
