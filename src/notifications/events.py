"""Event types and policy for the notification system."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Mapping

NotificationStatus = Literal[
    "success",
    "failure",
    "skipped",
    "lock_error",
    "config_error",
    "unexpected_error",
]
NotificationTaskType = Literal["backup", "workflow", "rclone"]
NotificationReportStatus = Literal[
    "success",
    "failed",
    "cancelled",
    "skipped",
    "lock_error",
    "config_error",
    "unexpected_error",
]

REPORT_TERMINAL_STATUSES: tuple[NotificationReportStatus, ...] = (
    "success",
    "failed",
    "cancelled",
    "skipped",
    "lock_error",
    "config_error",
    "unexpected_error",
)


@dataclass(frozen=True)
class NotificationPolicy:

    notify_on_success: bool
    notify_on_error: bool
    notify_on_skipped: bool


@dataclass(frozen=True)
class NotificationEvent:
    """Immutable data describing a single notification event."""

    job_name: str
    task_type: NotificationTaskType
    task_name: str
    status: NotificationStatus
    started_at: datetime
    finished_at: datetime | None
    dry_run: bool
    message: str
    log_path: Path | None = None
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed seconds between started_at and finished_at, or None."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class NotificationReportRunSummary:
    """Provider-neutral summary of one terminal run in a report window."""

    origin: str
    job: str
    target: str
    status: NotificationReportStatus
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    dry_run: bool
    error: str | None = None


@dataclass(frozen=True)
class NotificationReportEvent:
    """Immutable data describing a periodic report notification."""

    window_start: datetime
    window_end: datetime
    generated_at: datetime
    status_counts: Mapping[str, int]
    runs: tuple[NotificationReportRunSummary, ...]


@dataclass(frozen=True)
class NotificationDispatchResult:
    """Summary of a dispatcher.notify() call returned for testing."""

    attempted: int
    succeeded: int
    failed: int
