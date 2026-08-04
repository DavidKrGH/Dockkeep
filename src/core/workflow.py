"""Workflow execution for backup, rclone, and workflow targets."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..executors.backup import BackupExecutor
from ..executors.forget import ForgetExecutor
from ..executors.hooks import HookExecutor
from ..executors.prune import PruneExecutor
from ..executors.rclone import RcloneExecutor
from ..models.resolved_config import ResolvedBackupConfig, ResolvedJobConfig, ResolvedWorkflowConfig
from ..utils.logging import job_logger_name, log_blank_line, log_task_context

StepStartedCallback = Callable[[str, str, str, str, object], Awaitable[str | None]]
StepFinishedCallback = Callable[[str | None, str, bool, str | None, bool], Awaitable[None]]


@dataclass(frozen=True)
class BackupArtifactCandidate:
    """Restic summary captured before repository post-processing runs."""

    backup_summary: dict[str, object] | None


class WorkflowEngine:
    """Dispatches configured job targets without owning resource locks."""

    def __init__(
        self,
        job_name: str,
        job_config: ResolvedJobConfig,
        dry_run: bool = False,
        on_step_started: StepStartedCallback | None = None,
        on_step_finished: StepFinishedCallback | None = None,
    ) -> None:
        self.job_name = job_name
        self.job_config = job_config
        self.dry_run = dry_run
        self.logger = logging.getLogger(job_logger_name(job_name))
        self._scripts_dir = os.environ.get("DK_SCRIPTS_DIR", "/scripts")
        self.completed_backup_artifacts: dict[str, list[BackupArtifactCandidate]] = {}
        self._on_step_started = on_step_started
        self._on_step_finished = on_step_finished

    async def execute_workflow(self, workflow_name: str, workflow: ResolvedWorkflowConfig) -> bool:
        """Führt alle Steps eines Workflows sequenziell aus, umgeben von Hooks.

        Reihenfolge: pre_hooks → Steps → post_hooks.
        Bei pre_hook- oder Step-Fehler werden on_error_hooks ausgeführt.
        post_hook-Fehler werden nur als Warning geloggt.

        Args:
            workflow_name: Name des Workflows (nur für Logging).
            workflow: Workflow-Konfiguration mit Steps-Liste.

        Returns:
            True wenn alle Steps erfolgreich waren, False beim ersten Fehler.
        """
        dry_run_suffix = " [DRY RUN]" if self.dry_run else ""
        total = len(workflow.steps)
        steps_preview = " → ".join(workflow.steps)
        hook_t = workflow.timeouts.hook_timeout

        self.logger.info("  Steps: %s%s", steps_preview, dry_run_suffix)

        if not await self._run_hooks(
            workflow.hooks.pre_hooks, f"workflow '{workflow_name}' pre", timeout=hook_t
        ):
            await self._run_hooks(
                workflow.hooks.on_error_hooks,
                f"workflow '{workflow_name}' on_error",
                timeout=hook_t,
            )
            return False

        for i, step in enumerate(workflow.steps, 1):
            with log_task_context(step):
                log_blank_line(self.logger)
                self.logger.info("  [%d/%d] ── %s", i, total, step.upper())
                success = await self.execute_step(step)

                if not success:
                    self.logger.error("  [%d/%d] FAILED: %s — workflow aborted", i, total, step)
                    await self._run_hooks(
                        workflow.hooks.on_error_hooks,
                        f"workflow '{workflow_name}' on_error",
                        timeout=hook_t,
                    )
                    return False

                self.logger.info("  [%d/%d] ✓ %s done", i, total, step)

        if not await self._run_hooks(
            workflow.hooks.post_hooks, f"workflow '{workflow_name}' post", timeout=hook_t
        ):
            self.logger.warning(
                "post_hooks failed for workflow '%s' — workflow still succeeded", workflow_name
            )

        return True

    async def execute_step(self, step: str) -> bool:
        """Führt einen einzelnen Step aus.

        Unterstützte Formate:
          - ``"backup.<name>"``         – vollständiges Backup
            (backup + ggf. retention + ggf. cleanup)
          - ``"backup.<name>.backup"``  – nur Backup-Schritt
          - ``"backup.<name>.retention"`` – nur Retention (ignoriert execution.retention)
          - ``"backup.<name>.cleanup"`` – nur Cleanup (ignoriert execution.cleanup)
          - ``"rclone.<name>"``         – Rclone-Sync-Step

        Args:
            step: Step-String.

        Returns:
            True bei Erfolg, False bei Fehler oder unbekanntem Format.
        """
        parts = step.split(".")
        if parts[0] == "rclone" and len(parts) == 2:
            return await self._run_rclone(parts[1])

        if parts[0] == "backup" and len(parts) >= 2:
            backup_name = parts[1]
            if len(parts) == 2:
                return await self._run_backup(backup_name)
            if len(parts) == 3:
                substep = parts[2]
                if substep == "backup":
                    return await self._run_backup_backup(backup_name)
                if substep == "retention":
                    return await self._run_backup_forget(backup_name)
                if substep == "cleanup":
                    return await self._run_backup_prune(backup_name)

        self.logger.error("Unknown step format: '%s'", step)
        return False

    async def execute_backup(self, backup_name: str) -> bool:
        return await self._run_backup(backup_name)

    def consume_backup_artifact_candidate(self, backup_name: str) -> BackupArtifactCandidate | None:
        candidates = self.completed_backup_artifacts.get(backup_name)
        if not candidates:
            return None
        candidate = candidates.pop(0)
        if not candidates:
            self.completed_backup_artifacts.pop(backup_name, None)
        return candidate

    async def _run_hooks(self, hooks: list[str], context: str, timeout: int | None) -> bool:
        if self.dry_run:
            if hooks:
                self.logger.info(
                    "Skipping %s hooks during dry-run (%d configured)",
                    context,
                    len(hooks),
                )
            return True
        executor = HookExecutor(self.job_name, self._scripts_dir, hook_timeout=timeout)
        return await executor.run(hooks)

    async def run_job_hooks(self, phase: str) -> bool:
        hooks_config = self.job_config.hooks
        timeout = self.job_config.timeouts.hook_timeout
        if phase == "pre":
            return await self._run_hooks(hooks_config.pre_hooks, "job pre", timeout=timeout)
        if phase == "post":
            return await self._run_hooks(hooks_config.post_hooks, "job post", timeout=timeout)
        if phase == "on_error":
            return await self._run_hooks(
                hooks_config.on_error_hooks,
                "job on_error",
                timeout=timeout,
            )
        raise ValueError(f"Unknown job hook phase: {phase!r}")

    async def _run_backup(self, backup_name: str) -> bool:
        """Führt ein vollständiges Backup aus: pre_hooks → Backup+Forget+Prune → post_hooks.

        Bei pre_hook- oder Backup-Fehler werden on_error_hooks ausgeführt und False
        zurückgegeben. post_hook-Fehler werden nur als Warning geloggt; das Ergebnis
        bleibt True.

        Returns:
            True bei Erfolg, False bei Fehler oder nicht-existentem Backup.
        """
        backup: ResolvedBackupConfig | None = self.job_config.backup.get(backup_name)
        if backup is None:
            self.logger.error("Backup '%s' not found in job '%s'", backup_name, self.job_name)
            return False

        hook_t = backup.timeouts.hook_timeout
        if not await self._run_hooks(
            backup.hooks.pre_hooks, f"backup '{backup_name}' pre", timeout=hook_t
        ):
            await self._run_hooks(
                backup.hooks.on_error_hooks, f"backup '{backup_name}' on_error", timeout=hook_t
            )
            return False

        success = await self._run_backup_core(backup_name, backup)

        if not success:
            await self._run_hooks(
                backup.hooks.on_error_hooks, f"backup '{backup_name}' on_error", timeout=hook_t
            )
            return False

        if not await self._run_hooks(
            backup.hooks.post_hooks, f"backup '{backup_name}' post", timeout=hook_t
        ):
            self.logger.warning(
                "post_hooks failed for backup '%s' — backup still succeeded", backup_name
            )

        return True

    async def _run_backup_core(self, backup_name: str, backup: ResolvedBackupConfig) -> bool:
        if not await self._run_backup_backup(backup_name):
            return False

        if backup.execution.retention:
            log_blank_line(self.logger)
            if not await self._run_backup_forget(backup_name):
                return False

        if backup.execution.cleanup:
            log_blank_line(self.logger)
            if not await self._run_backup_prune(backup_name):
                return False

        return True

    async def _run_backup_backup(self, backup_name: str) -> bool:
        backup: ResolvedBackupConfig | None = self.job_config.backup.get(backup_name)
        if backup is None:
            self.logger.error("Backup '%s' not found in job '%s'", backup_name, self.job_name)
            return False

        restic_t = backup.timeouts.backup_timeout
        sources = backup.input.sources
        executor = BackupExecutor(self.job_name, sources, dry_run=self.dry_run, timeout=restic_t)
        success = await self._run_tracked_step(
            step=f"backup.{backup_name}.backup",
            backend="restic",
            task_type="backup",
            task_name=backup_name,
            effective_task_config=backup,
            operation=lambda: executor.execute(backup),
        )
        if success and not self.dry_run:
            self._record_backup_artifact_candidate(backup_name, executor)
        return success

    def _record_backup_artifact_candidate(self, backup_name: str, executor: BackupExecutor) -> None:
        summary = getattr(executor, "summary", None)
        raw_summary: dict[str, object] | None
        if isinstance(summary, dict):
            raw_summary = summary
        elif summary is not None:
            raw = getattr(summary, "raw", None)
            raw_summary = raw if isinstance(raw, dict) else None
        else:
            raw_summary = None
        self.completed_backup_artifacts.setdefault(backup_name, []).append(
            BackupArtifactCandidate(backup_summary=raw_summary)
        )

    async def _run_backup_forget(self, backup_name: str) -> bool:
        """Run retention even when ``execution.retention`` is false."""
        backup: ResolvedBackupConfig | None = self.job_config.backup.get(backup_name)
        if backup is None:
            self.logger.error("Backup '%s' not found in job '%s'", backup_name, self.job_name)
            return False

        restic_t = backup.timeouts.backup_timeout
        return await self._run_tracked_step(
            step=f"backup.{backup_name}.retention",
            backend="restic",
            task_type="backup",
            task_name=backup_name,
            effective_task_config=backup,
            operation=lambda: ForgetExecutor(
                self.job_name, dry_run=self.dry_run, timeout=restic_t
            ).execute(backup),
        )

    async def _run_backup_prune(self, backup_name: str) -> bool:
        """Run cleanup even when ``execution.cleanup`` is false."""
        backup: ResolvedBackupConfig | None = self.job_config.backup.get(backup_name)
        if backup is None:
            self.logger.error("Backup '%s' not found in job '%s'", backup_name, self.job_name)
            return False

        restic_t = backup.timeouts.backup_timeout
        return await self._run_tracked_step(
            step=f"backup.{backup_name}.cleanup",
            backend="restic",
            task_type="backup",
            task_name=backup_name,
            effective_task_config=backup,
            operation=lambda: PruneExecutor(
                self.job_name, dry_run=self.dry_run, timeout=restic_t
            ).execute(backup),
        )

    async def _run_rclone(self, rclone_name: str) -> bool:
        if rclone_name not in self.job_config.rclone:
            self.logger.error("Rclone task '%s' not found in job '%s'", rclone_name, self.job_name)
            return False

        task = self.job_config.rclone[rclone_name]
        hook_t = task.timeouts.hook_timeout
        if not await self._run_hooks(
            task.hooks.pre_hooks, f"rclone '{rclone_name}' pre", timeout=hook_t
        ):
            await self._run_hooks(
                task.hooks.on_error_hooks, f"rclone '{rclone_name}' on_error", timeout=hook_t
            )
            return False

        rclone_t = task.timeouts.rclone_timeout
        executor = RcloneExecutor(
            self.job_name,
            dry_run=self.dry_run,
            timeout=rclone_t,
        )
        success = await self._run_tracked_step(
            step=f"rclone.{rclone_name}.rclone",
            backend="rclone",
            task_type="rclone",
            task_name=rclone_name,
            effective_task_config=task,
            operation=lambda: executor.execute(task),
        )
        if not success:
            await self._run_hooks(
                task.hooks.on_error_hooks, f"rclone '{rclone_name}' on_error", timeout=hook_t
            )
            return False

        if not await self._run_hooks(
            task.hooks.post_hooks, f"rclone '{rclone_name}' post", timeout=hook_t
        ):
            self.logger.warning(
                "post_hooks failed for rclone '%s' — rclone still succeeded", rclone_name
            )

        return True

    async def _run_tracked_step(
        self,
        *,
        step: str,
        backend: str,
        task_type: str,
        task_name: str,
        effective_task_config: object,
        operation: Callable[[], Awaitable[bool]],
    ) -> bool:
        run_step_id: str | None = None
        if self._on_step_started is not None:
            run_step_id = await self._on_step_started(
                step, backend, task_type, task_name, effective_task_config
            )
        try:
            success = await operation()
        except asyncio.CancelledError:
            if self._on_step_finished is not None:
                await self._on_step_finished(run_step_id, step, False, "Step cancelled", True)
            raise
        except Exception as exc:
            if self._on_step_finished is not None:
                await self._on_step_finished(run_step_id, step, False, str(exc), False)
            raise
        if self._on_step_finished is not None:
            await self._on_step_finished(
                run_step_id, step, success, None if success else "Step failed", False
            )
        return success
