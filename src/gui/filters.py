from datetime import datetime


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        return None


def format_datetime(value: object, *, seconds: bool = False) -> str:
    cutoff = 19 if seconds else 16
    dt = _parse_datetime(value)
    if dt is None:
        if value is None or str(value).strip() == "":
            return "–"
        return str(value).replace("T", " ")[:cutoff]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def relative_time(value: object) -> str:
    if value is None:
        return "–"
    dt = _parse_datetime(value)
    if dt is None:
        return str(value).replace("T", " ")[:16]
    # Restic timestamps are timezone-aware (usually UTC). Treat naive values as
    # local time and keep now() in the same tzinfo so the difference isn't
    # skewed by the container's timezone.
    now = datetime.now(dt.tzinfo)
    seconds = (now - dt).total_seconds()
    if seconds < -60:
        ahead = -seconds
        if ahead < 3600:
            return f"in {int(ahead / 60)} minutes"
        if ahead < 86400:
            return f"in {int(ahead / 3600)} hours"
        return f"in {int(ahead / 86400)} days"
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)} hours ago"
    if seconds < 172800:
        return "yesterday"
    return f"{int(seconds / 86400)} days ago"


def format_duration(value: object) -> str:
    if not isinstance(value, (int, float, str)):
        return "–"
    try:
        total = int(float(value))
    except (ValueError, TypeError):
        return "–"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}m {s}s"
    h, rem = divmod(total, 3600)
    return f"{h}h {rem // 60}m"


def format_bytes(value: object) -> str:
    if not isinstance(value, int | float | str):
        return "-"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "-"

    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
