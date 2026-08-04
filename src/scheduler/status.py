"""Scheduler status file helpers."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock, Timeout

from ..core.locking import SCHEDULER_LOCK_FILE
from ..core.locking import lock_dir as default_lock_dir

SCHEDULER_STATUS_FILE = "dockkeep-scheduler.status.json"
STATUS_VERSION = 1
DEFAULT_STALE_AFTER_SECONDS = 130

SchedulerState = Literal["running", "stopped", "unknown", "config_error", "scheduler_lock_error"]
SchedulerOwner = Literal["gui", "scheduler-cli"]


@dataclass(frozen=True)
class SchedulerStatusError:
    """Machine-readable scheduler status error."""

    code: str
    message: str


@dataclass(frozen=True)
class SchedulerStatus:
    """Public scheduler status payload."""

    state: SchedulerState
    pid: int | None
    started_at: str | None
    last_tick_at: str | None
    config_path: str | None
    config_mtime: float | None
    owner: SchedulerOwner | None
    error: SchedulerStatusError | None
    version: int = STATUS_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SchedulerStatus":
        raw_error = data.get("error")
        error = None
        if isinstance(raw_error, dict):
            error = SchedulerStatusError(
                code=str(raw_error.get("code", "unknown")),
                message=str(raw_error.get("message", "")),
            )
        raw_pid = data.get("pid")
        raw_started_at = data.get("started_at")
        raw_last_tick_at = data.get("last_tick_at")
        raw_config_path = data.get("config_path")
        raw_config_mtime = data.get("config_mtime")
        raw_version = data.get("version")
        return cls(
            state=_coerce_state(data.get("state")),
            pid=raw_pid if isinstance(raw_pid, int) else None,
            started_at=raw_started_at if isinstance(raw_started_at, str) else None,
            last_tick_at=raw_last_tick_at if isinstance(raw_last_tick_at, str) else None,
            config_path=raw_config_path if isinstance(raw_config_path, str) else None,
            config_mtime=(
                float(raw_config_mtime) if isinstance(raw_config_mtime, int | float) else None
            ),
            owner=_coerce_owner(data.get("owner")),
            error=error,
            version=raw_version if isinstance(raw_version, int) else STATUS_VERSION,
        )


class SchedulerStatusWriter:
    """Writes the scheduler status JSON file atomically."""

    def __init__(self, lock_dir: Path | None = None) -> None:
        """Initialize the writer.

        Args:
            lock_dir: Directory containing scheduler lock and status files.
        """
        resolved_lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self.status_path = resolved_lock_dir / SCHEDULER_STATUS_FILE

    def write(
        self,
        *,
        state: SchedulerState,
        owner: SchedulerOwner | None,
        started_at: datetime | None,
        last_tick_at: datetime | None,
        config_path: Path | None,
        config_mtime: float | None,
        error: SchedulerStatusError | None = None,
    ) -> SchedulerStatus:
        """Write and return the current status."""
        status = SchedulerStatus(
            state=state,
            pid=os.getpid(),
            started_at=_format_dt(started_at),
            last_tick_at=_format_dt(last_tick_at),
            config_path=str(config_path) if config_path is not None else None,
            config_mtime=config_mtime,
            owner=owner,
            error=error,
        )
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.status_path.with_name(f".{self.status_path.name}.{uuid4().hex}.tmp")
        try:
            tmp_path.write_text(
                json.dumps(status.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self.status_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return status


class SchedulerStatusReader:

    def __init__(
        self,
        lock_dir: Path | None = None,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        """Initialize the reader."""
        self.lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self.status_path = self.lock_dir / SCHEDULER_STATUS_FILE
        self.lock_path = self.lock_dir / SCHEDULER_LOCK_FILE
        self.stale_after_seconds = stale_after_seconds

    def read_file(self) -> SchedulerStatus | None:
        """Read the raw status file.

        Raises:
            OSError: If the file cannot be read.
            json.JSONDecodeError: If the file is not valid JSON.
            ValueError: If the file does not contain a JSON object.
        """
        if not self.status_path.exists():
            return None
        raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Scheduler status file does not contain an object")
        return SchedulerStatus.from_dict(raw)

    def read(self, *, strict: bool = False) -> SchedulerStatus:
        """Return evaluated scheduler status.

        Args:
            strict: When True, invalid status files are raised to the caller.
                When False, invalid files evaluate to ``unknown``.
        """
        try:
            raw_status = self.read_file()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            if strict:
                raise
            return _unknown_status(str(exc))

        lock_held = self._is_scheduler_lock_held()
        fresh = raw_status is not None and self._is_fresh(raw_status)

        if not lock_held:
            if (
                fresh
                and raw_status is not None
                and raw_status.owner == "gui"
                and raw_status.state in ("config_error", "scheduler_lock_error")
            ):
                return raw_status
            return _status_from_raw(raw_status, "stopped")

        if fresh and raw_status is not None:
            if raw_status.state == "scheduler_lock_error":
                return raw_status
            return _status_from_raw(raw_status, "running")

        return _unknown_status("Scheduler lock is held but status is missing or stale")

    def _is_scheduler_lock_held(self) -> bool:
        lock = FileLock(str(self.lock_path), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            return True
        else:
            lock.release()
            return False

    def _is_fresh(self, status: SchedulerStatus) -> bool:
        timestamp = status.last_tick_at or status.started_at
        if timestamp is None:
            return False
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return (datetime.now(UTC) - parsed).total_seconds() <= self.stale_after_seconds


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _coerce_state(value: object) -> SchedulerState:
    if value in {"running", "stopped", "config_error", "scheduler_lock_error"}:
        return value  # type: ignore[return-value]
    return "unknown"


def _coerce_owner(value: object) -> SchedulerOwner | None:
    if value in {"gui", "scheduler-cli"}:
        return value  # type: ignore[return-value]
    return None


def _status_from_raw(raw_status: SchedulerStatus | None, state: SchedulerState) -> SchedulerStatus:
    if raw_status is None:
        return SchedulerStatus(
            state=state,
            pid=None,
            started_at=None,
            last_tick_at=None,
            config_path=None,
            config_mtime=None,
            owner=None,
            error=None,
        )
    return SchedulerStatus(
        state=state,
        pid=raw_status.pid,
        started_at=raw_status.started_at,
        last_tick_at=raw_status.last_tick_at,
        config_path=raw_status.config_path,
        config_mtime=raw_status.config_mtime,
        owner=raw_status.owner,
        error=raw_status.error,
    )


def _unknown_status(message: str) -> SchedulerStatus:
    return SchedulerStatus(
        state="unknown",
        pid=None,
        started_at=None,
        last_tick_at=None,
        config_path=None,
        config_mtime=None,
        owner=None,
        error=SchedulerStatusError(code="status_error", message=message),
    )
