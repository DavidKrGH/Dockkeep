"""Shared helpers for restic snapshot statistics."""

from datetime import datetime


def compute_duration(summary: dict[str, object]) -> float | None:
    start = summary.get("backup_start")
    end = summary.get("backup_end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        dt_start = datetime.fromisoformat(start)
        dt_end = datetime.fromisoformat(end)
        seconds = (dt_end - dt_start).total_seconds()
        if seconds < 0:
            return None
        return round(seconds)
    except (ValueError, TypeError):
        return None
