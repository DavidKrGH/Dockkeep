"""Shared parsing helpers for run task selectors."""

from __future__ import annotations

from dataclasses import dataclass

BACKUP_SUBSTEPS = frozenset({"backup", "retention", "cleanup"})


@dataclass(frozen=True)
class ParsedTaskSelector:
    """Parsed manual-run task selector."""

    job: str
    kind: str
    name: str
    substep: str | None = None


def split_task_selector(task_selector: str) -> tuple[str, str]:
    """Split a task selector into job name and task suffix."""
    dot_pos = task_selector.find(".")
    if dot_pos <= 0 or dot_pos == len(task_selector) - 1:
        raise ValueError(
            f"Invalid task selector format '{task_selector}'. Expected: "
            "<job-name>.backup.<name> | <job-name>.rclone.<name> | "
            "<job-name>.workflow.<name>"
        )
    return task_selector[:dot_pos], task_selector[dot_pos + 1 :]


def parse_task_selector(task_selector: str) -> ParsedTaskSelector:
    """Parse a backup, rclone or workflow task selector."""
    job, step_name = split_task_selector(task_selector)
    parts = step_name.split(".")
    if not parts[0]:
        raise ValueError("Task selector must be <job>.<task>[.<substep>]")

    if parts[0] == "backup":
        if len(parts) == 2 and parts[1]:
            return ParsedTaskSelector(job, "backup", parts[1])
        if len(parts) == 3 and parts[1] and parts[2] in BACKUP_SUBSTEPS:
            return ParsedTaskSelector(job, "backup_step", parts[1], parts[2])
        raise ValueError("Backup task must be <job>.backup.<name>[.<backup|retention|cleanup>]")

    if parts[0] == "rclone":
        if len(parts) == 2 and parts[1]:
            return ParsedTaskSelector(job, "rclone", parts[1])
        raise ValueError("Rclone task must be <job>.rclone.<name>")

    if parts[0] == "workflow":
        if len(parts) == 2 and parts[1]:
            return ParsedTaskSelector(job, "workflow", parts[1])
        raise ValueError("Workflow task must be <job>.workflow.<name>")

    raise ValueError(
        "Task selector must be <job>.backup.<name>, <job>.rclone.<name>, "
        "or <job>.workflow.<name>"
    )
