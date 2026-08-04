"""Shared scheduling helpers for services and public config mappers."""

from datetime import datetime

from croniter import CroniterBadDateError, croniter

from ..models.resolved_config import ResolvedBackupConfig


def next_run_datetime(schedule: str | None) -> datetime | None:
    if not schedule:
        return None
    try:
        next_time: datetime = croniter(schedule, datetime.now().astimezone()).get_next(datetime)
    except (CroniterBadDateError, ValueError):
        return None
    return next_time.astimezone()


def backup_steps(backup: ResolvedBackupConfig) -> list[str]:
    steps = ["backup"]
    if backup.execution.retention:
        steps.append("retention")
    if backup.execution.cleanup:
        steps.append("cleanup")
    return steps


def scheduled_run_display(
    *,
    job: str,
    name: str,
    run_type: str,
    steps: str | None = None,
) -> dict[str, str | None]:
    if run_type == "backup":
        type_label, tone = "Backup", "info"
    elif run_type == "rclone":
        type_label, tone = "Rclone", "success"
    elif run_type == "workflow":
        type_label, tone = "Workflow", "neutral"
    else:
        type_label, tone = "Task", "neutral"
    secondary = None
    if steps and steps != run_type:
        secondary = steps
    return {
        "task_type_label": type_label,
        "task_type_tone": tone,
        "target_primary": f"{job}: {name}" if job and name else job or name or "Task",
        "target_secondary": secondary,
    }
