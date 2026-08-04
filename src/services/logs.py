"""Log listing, reading and streaming helpers."""

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from ..utils.logging import log_base_dir as default_log_base_dir
from .errors import ServiceError

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LogJobView(TypedDict):
    name: str
    dates: list[str]


class LogJobsView(TypedDict):
    jobs: list[LogJobView]


class LogJobDatesView(TypedDict):
    job: str
    dates: list[str]


class LogsView(TypedDict):
    job_names: list[str]
    selected_job: str | None
    selected_date: str
    available_dates: list[str]
    log_content: str | None
    log_tail: int
    log_truncated: bool
    log_error: str | None


class RawLogView(TypedDict):
    job: str
    date: str
    content: str
    truncated: bool


def tail_file_lines(path: Path, tail: int) -> tuple[list[str], bool]:
    if tail <= 0:
        return [], False
    lines: deque[str] = deque(maxlen=tail)
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line_count += 1
            lines.append(line)
    return list(lines), line_count > tail


def is_valid_job_name(job: str) -> bool:
    return _NAME_RE.fullmatch(job) is not None


def resolve_job_log_dir(job: str, base_dir: Path | None = None) -> Path | None:
    """Resolve the log directory for *job* under *base_dir*.

    Applies the same name and path-traversal guard as :class:`LogService`:
    the job name must match the allowed character set and the resolved
    directory must be a direct child of the resolved base directory.

    Args:
        job: Job name from untrusted input (e.g. CLI argument).
        base_dir: Base log directory.

    Returns:
        The resolved log directory, or ``None`` if the name is invalid or
        would escape *base_dir*.
    """
    if not is_valid_job_name(job):
        return None
    resolved_base_dir = base_dir if base_dir is not None else default_log_base_dir()
    base = resolved_base_dir.resolve()
    job_dir = (base / job).resolve()
    if job_dir.parent != base:
        return None
    return job_dir


class LogService:
    """Access job logs with path traversal protection."""

    def __init__(self, log_base_dir: Path | None = None) -> None:
        self._base = log_base_dir if log_base_dir is not None else default_log_base_dir()

    def list_jobs(self) -> LogJobsView:
        """List jobs with available log dates."""
        jobs: list[LogJobView] = []
        if self._base.exists():
            for child in sorted(self._base.iterdir()):
                if child.is_dir() and _NAME_RE.fullmatch(child.name):
                    jobs.append({"name": child.name, "dates": self._dates_for(child.name)})
        return {"jobs": jobs}

    def list_job(self, job: str) -> LogJobDatesView:
        """List log dates for one job."""
        self._validate_job(job)
        return {"job": job, "dates": self._dates_for(job)}

    _STATIC_TAIL = 10_000

    async def get_logs_view(self, job: str | None = None, date: str | None = None) -> LogsView:
        return await asyncio.to_thread(self._get_logs_view_sync, job, date)

    def _get_logs_view_sync(self, job: str | None = None, date: str | None = None) -> LogsView:
        jobs_result = self.list_jobs()
        jobs = jobs_result["jobs"]
        job_names = [str(item["name"]) for item in jobs]
        selected_job = job or (job_names[0] if job_names else None)
        selected_date = date
        available_dates: list[str] = []
        log_content: str | None = None
        log_truncated: bool = False
        log_error: str | None = None

        if selected_job and selected_job not in job_names:
            log_error = f"Unknown log job: {selected_job}"
        elif selected_job:
            selected_meta = next(item for item in jobs if item["name"] == selected_job)
            available_dates = selected_meta["dates"]
            selected_date = selected_date or (available_dates[0] if available_dates else None)
            if selected_date and selected_date not in available_dates:
                log_error = f"Unknown log date for {selected_job}: {selected_date}"
            elif selected_date:
                try:
                    raw = self._read_raw_sync(selected_job, selected_date, tail=self._STATIC_TAIL)
                    log_content = str(raw["content"])
                    log_truncated = bool(raw.get("truncated", False))
                except ServiceError as exc:
                    log_error = exc.message

        return {
            "job_names": job_names,
            "selected_job": selected_job,
            "selected_date": selected_date or datetime.now().strftime("%Y-%m-%d"),
            "available_dates": available_dates,
            "log_content": log_content,
            "log_tail": self._STATIC_TAIL,
            "log_truncated": log_truncated,
            "log_error": log_error,
        }

    async def read_raw(self, job: str, date: str, tail: int = 0) -> RawLogView:
        return await asyncio.to_thread(self._read_raw_sync, job, date, tail)

    def _read_raw_sync(self, job: str, date: str, tail: int = 0) -> RawLogView:
        path = self._log_path(job, date)
        try:
            if tail > 0:
                lines, truncated = tail_file_lines(path, tail)
                content = "".join(lines)
            else:
                content = path.read_text(encoding="utf-8")
                truncated = False
        except FileNotFoundError as exc:
            raise ServiceError("log_not_found", "Log file not found", 404) from exc
        except OSError as exc:
            raise ServiceError("read_error", str(exc), 500) from exc
        return {"job": job, "date": date, "content": content, "truncated": truncated}

    _DisconnectCheck = Callable[[], Coroutine[Any, Any, bool]]

    def open_stream(
        self,
        job: str,
        tail: int = 100,
        is_disconnected: "_DisconnectCheck | None" = None,
    ) -> AsyncIterator[str]:
        """Validate and prepare a log stream before the HTTP response starts."""
        self._validate_job(job)
        job_dir = self._base / job
        dates = self._dates_for(job)
        path = job_dir / f"{dates[0]}.log" if dates else None
        return self._stream_prepared(job_dir, path, tail, is_disconnected)

    async def stream(
        self,
        job: str,
        tail: int = 100,
        is_disconnected: "_DisconnectCheck | None" = None,
    ) -> AsyncIterator[str]:
        """Yield SSE lines containing JSON payloads."""
        async for line in self.open_stream(job, tail, is_disconnected):
            yield line

    async def _stream_prepared(
        self,
        job_dir: Path,
        path: Path | None,
        tail: int,
        is_disconnected: "_DisconnectCheck | None" = None,
    ) -> AsyncIterator[str]:
        """Yield SSE lines from a prevalidated stream source."""
        while path is None:
            if await _client_disconnected(is_disconnected):
                return
            dates = self._dates_in_dir(job_dir)
            if dates:
                path = job_dir / f"{dates[0]}.log"
                break
            await asyncio.sleep(1)
        position = 0
        if path.exists():
            lines, _truncated = await asyncio.to_thread(tail_file_lines, path, tail)
            for line in lines:
                yield _sse(line.rstrip("\n"))
            position = await asyncio.to_thread(_file_size, path)
        while True:
            await asyncio.sleep(1)
            if is_disconnected is not None and await is_disconnected():
                return
            if not path.exists():
                path = None
                while path is None:
                    if await _client_disconnected(is_disconnected):
                        return
                    dates = self._dates_in_dir(job_dir)
                    if dates:
                        path = job_dir / f"{dates[0]}.log"
                        position = 0
                        break
                    await asyncio.sleep(1)
                continue
            lines, position = await asyncio.to_thread(_read_from_position, path, position)
            for line in lines:
                yield _sse(line.rstrip("\n"))
            dates = self._dates_in_dir(job_dir)
            newest_path = job_dir / f"{dates[0]}.log" if dates else None
            if newest_path is not None and newest_path != path:
                path = newest_path
                position = 0

    def _dates_for(self, job: str) -> list[str]:
        self._validate_job(job)
        job_dir = self._base / job
        if not job_dir.exists():
            return []
        return self._dates_in_dir(job_dir)

    @staticmethod
    def _dates_in_dir(job_dir: Path) -> list[str]:
        return sorted(
            (path.stem for path in job_dir.glob("*.log") if _DATE_RE.fullmatch(path.stem)),
            reverse=True,
        )

    def _log_path(self, job: str, date: str) -> Path:
        self._validate_job(job)
        if not _DATE_RE.fullmatch(date):
            raise ServiceError("invalid_date", "Date must be YYYY-MM-DD", 400)
        path = (self._base / job / f"{date}.log").resolve()
        base = self._base.resolve()
        if path.parent != (base / job).resolve():
            raise ServiceError("invalid_path", "Invalid log path", 400)
        return path

    @staticmethod
    def _validate_job(job: str) -> None:
        if not _NAME_RE.fullmatch(job):
            raise ServiceError("invalid_job", "Invalid job name", 400)


def _sse(line: str) -> str:
    return f"data: {json.dumps({'line': line})}\n\n"


def _file_size(path: Path) -> int:
    return path.stat().st_size


def _read_from_position(path: Path, position: int) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(position)
        lines = list(handle)
        return lines, handle.tell()


async def _client_disconnected(is_disconnected: LogService._DisconnectCheck | None) -> bool:
    if is_disconnected is None:
        return False
    return await is_disconnected()
