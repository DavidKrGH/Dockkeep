"""Centralized message formatting for all notification providers."""

from datetime import datetime
from html import escape

from .events import (
    REPORT_TERMINAL_STATUSES,
    NotificationEvent,
    NotificationReportEvent,
    NotificationReportRunSummary,
)

_STATUS_LABELS = {
    "success": "Success",
    "failure": "Failed",
    "skipped": "Skipped",
    "lock_error": "Lock error",
    "config_error": "Config error",
    "unexpected_error": "Unexpected error",
}

# Statuses that represent a genuine problem vs. an expected/informational
# outcome for a single run. Mirrors the report's danger/warn/ok grouping so a
# single-run email and a report email read as the same visual language.
_EVENT_DANGER_STATUSES = frozenset({"failure", "lock_error", "config_error", "unexpected_error"})
_EVENT_WARN_STATUSES = frozenset({"skipped"})

_SEVERITY_HEADLINE_COLORS = {"ok": "#166534", "warn": "#8f6108", "danger": "#991b1b"}

_SUMMARY_VALUE_LIMIT = 500
_MAIL_MESSAGE_LIMIT = 2_000
_MAIL_ERROR_LIMIT = 4_000
_SUBJECT_TASK_LIMIT = 180

# Push messages are HTML (Pushover ``html=1``) and therefore cannot be capped by
# truncating the finished message: that would cut a tag or an escaped entity in
# half. Instead every variable field carries its own limit, small enough that
# field limits plus the fixed markup stay below the hard provider limit.
_PUSH_HTML_HARD_LIMIT = 1_024  # Pushover: 1024 UTF-8 characters per message
_PUSH_HTML_MARKUP_BUDGET = 400  # tags, labels, newlines, status/duration values
_PUSH_HTML_TARGET_LIMIT = 140
_PUSH_HTML_MESSAGE_LIMIT = 160
_PUSH_HTML_ERROR_LIMIT = 140
_PUSH_HTML_LOG_LIMIT = 160

_REPORT_STATUS_LABELS = {
    "success": "Success",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "skipped": "Skipped",
    "lock_error": "Lock error",
    "config_error": "Config error",
    "unexpected_error": "Unexpected error",
}

# Statuses that represent a genuine problem vs. an expected/informational
# outcome. Used to color job badges and issue entries, and to decide the
# overall report verdict ("Needs attention" vs. "All clear").
_REPORT_DANGER_STATUSES = frozenset({"failed", "lock_error", "config_error", "unexpected_error"})
_REPORT_WARN_STATUSES = frozenset({"skipped", "cancelled"})

_SEVERITY_STYLES = {
    "danger": {"bg": "#fbe9e7", "fg": "#9a3a35"},
    "warn": {"bg": "#fdf3df", "fg": "#8f6108"},
    "ok": {"bg": "#e7eff2", "fg": "#2b6079"},
}

_REPORT_ERROR_LIMIT = 180
_REPORT_RUN_LIMIT = 20  # individually listed issues before truncating (mail)
_REPORT_JOB_LIMIT = 40  # individually listed jobs before truncating (mail)
_REPORT_PUSH_HTML_ISSUE_LIMIT = 2


def _task_label(event: NotificationEvent) -> str:
    if event.task_type == "backup":
        return f"{event.job_name}.backup.{event.task_name}"
    if event.task_type == "rclone":
        return f"{event.job_name}.rclone.{event.task_name}"
    if event.task_type == "workflow":
        return f"{event.job_name}.workflow.{event.task_name}"
    raise ValueError(f"Unknown notification task type: {event.task_type!r}")


def _task_type_label(event: NotificationEvent) -> str:
    if event.task_type == "backup":
        return "Backup"
    if event.task_type == "rclone":
        return "Rclone sync"
    if event.task_type == "workflow":
        return "Workflow"
    raise ValueError(f"Unknown notification task type: {event.task_type!r}")


def _status_label(event: NotificationEvent) -> str:
    return _STATUS_LABELS.get(event.status, event.status.replace("_", " ").title())


def _event_severity(event: NotificationEvent) -> str:
    if event.status in _EVENT_DANGER_STATUSES:
        return "danger"
    if event.status in _EVENT_WARN_STATUSES:
        return "warn"
    return "ok"


def _push_severity_color(severity: str) -> str:
    if severity == "danger":
        return "#991b1b"
    if severity == "warn":
        return "#8f6108"
    return "#166534"


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _duration_label(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"

    rounded_seconds = max(0, round(seconds))
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _report_status_label(status: str) -> str:
    return _REPORT_STATUS_LABELS.get(status, status.replace("_", " ").title())


def _pluralize(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _relative_label(value: datetime, *, now: datetime) -> str:
    seconds = max(0.0, (now - value).total_seconds())
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{round(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{round(hours)}h ago"
    days = hours / 24
    return f"{round(days)}d ago"


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return "." * limit
    return value[: limit - 3].rstrip() + "..."


def _escape_truncated(value: str, limit: int) -> str:
    if limit <= 0:
        return ""

    escaped = escape(value)
    if len(escaped) <= limit:
        return escaped
    if limit <= 3:
        return "." * limit

    parts: list[str] = []
    used = 0
    body_limit = limit - 3
    for char in value:
        escaped_char = escape(char)
        if used + len(escaped_char) > body_limit:
            break
        parts.append(escaped_char)
        used += len(escaped_char)
    return "".join(parts).rstrip() + "..."


def _summary_rows(
    event: NotificationEvent, *, truncate_values: bool = False
) -> list[tuple[str, str]]:
    rows = [
        ("Status", _status_label(event)),
        ("Job", event.job_name),
        ("Type", _task_type_label(event)),
        ("Task", _task_label(event)),
        ("Started", _timestamp(event.started_at)),
    ]
    if event.finished_at is not None:
        rows.append(("Finished", _timestamp(event.finished_at)))
    rows.append(("Duration", _duration_label(event.duration_seconds)))
    rows.append(("Dry run", "yes" if event.dry_run else "no"))
    if event.log_path is not None:
        rows.append(("Log", str(event.log_path)))
    if truncate_values:
        return [(key, _truncate(value, _SUMMARY_VALUE_LIMIT)) for key, value in rows]
    return rows


def format_subject(event: NotificationEvent) -> str:
    """Build a short email subject line.

    Examples:
        [DK] Success homeserver.backup.local
        [DK] Failed homeserver.workflow.daily
        [DK] Skipped homeserver.backup.local [DRY RUN]
    """
    label = _truncate(_task_label(event), _SUBJECT_TASK_LIMIT)
    subject = f"[DK] {_status_label(event)} {label}"
    if event.dry_run:
        subject += " [DRY RUN]"
    return subject


def format_text_body(event: NotificationEvent) -> str:
    status = _status_label(event)
    lines: list[str] = [
        f"Dockkeep - {status}",
        _task_label(event),
        "",
        "Summary",
        "-------",
    ]

    for key, value in _summary_rows(event, truncate_values=True):
        lines.append(f"{key + ':':<10} {value}")

    if event.message:
        lines.append("")
        lines.append("Message")
        lines.append("-------")
        lines.append(_truncate(event.message, _MAIL_MESSAGE_LIMIT))

    if event.error:
        lines.append("")
        lines.append("Error")
        lines.append("-----")
        lines.append(_truncate(event.error, _MAIL_ERROR_LIMIT))

    if event.dry_run:
        lines.append("")
        lines.append("DRY RUN: no changes were made.")

    return "\n".join(lines)


def format_html_body(event: NotificationEvent) -> str:
    status = _status_label(event)
    verdict_color = _SEVERITY_HEADLINE_COLORS[_event_severity(event)]
    key_style = "text-align:left;padding:6px 14px 6px 0;color:#475569;"
    value_style = "padding:6px 0;color:#0f172a;"
    detail_rows = [
        (key, value)
        for key, value in _summary_rows(event, truncate_values=True)
        if key not in ("Status", "Type", "Task")
    ]
    row_html = "\n".join(
        (
            "<tr>"
            f'<th style="{key_style}">{escape(key)}</th>'
            f'<td style="{value_style}">{escape(value)}</td>'
            "</tr>"
        )
        for key, value in detail_rows
    )
    details_html = (
        _section_label_html("Details") + '<div style="padding:0 20px 4px;">'
        '<table role="presentation" style="border-collapse:collapse;width:100%;font-size:13.5px;">'
        f"{row_html}</table></div>"
    )

    message_html = ""
    if event.message:
        message = _truncate(event.message, _MAIL_MESSAGE_LIMIT)
        message_html = (
            _section_label_html("Message")
            + '<div style="padding:0 20px 4px;font-size:13px;color:#334155;line-height:1.5;">'
            f"{escape(message)}</div>"
        )

    error_html = ""
    if event.error:
        error = _truncate(event.error, _MAIL_ERROR_LIMIT)
        error_colors = _SEVERITY_STYLES["danger"]
        error_html = (
            _section_label_html("Error") + '<div style="padding:0 20px 4px;">'
            f'<div style="padding:12px 14px;border-radius:9px;background:{error_colors["bg"]};'
            f'color:{error_colors["fg"]};font-family:ui-monospace,SFMono-Regular,Consolas,'
            f'monospace;font-size:12.5px;line-height:1.45;white-space:pre-wrap;">'
            f"{escape(error)}</div></div>"
        )

    dry_run_html = ""
    if event.dry_run:
        dry_run_html = (
            '<div style="margin:4px 20px 16px;padding:12px 14px;border-radius:9px;'
            'background:#eff6ff;color:#1e40af;font-size:13px;">'
            "Dry run: no changes were made.</div>"
        )

    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f8fafc;font-family:Arial,sans-serif;">
    <main style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;
border-radius:12px;overflow:hidden;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;
padding:14px 20px;border-bottom:1px solid #e2e8f0;">
        <span style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;
color:#3f6b4f;font-weight:700;">Dockkeep</span>
        <span style="font-size:11px;letter-spacing:.04em;color:#64748b;">
{escape(_task_type_label(event))}</span>
      </div>
      <div style="padding:18px 20px 16px;border-left:4px solid {verdict_color};">
        <div style="font-size:18px;font-weight:700;color:#0f172a;">{escape(status)}</div>
        <div style="font-size:12.5px;color:#64748b;margin-top:3px;">
{escape(_task_label(event))}</div>
      </div>
      {details_html}
      {message_html}
      {error_html}
      {dry_run_html}
      <div style="margin-top:6px;padding:14px 20px 18px;border-top:1px solid #e2e8f0;
font-size:11.5px;color:#64748b;">
        Sent by Dockkeep &middot; configured via
        <code>notify_on_*</code> for this {escape(_task_type_label(event).lower())}
      </div>
    </main>
  </body>
</html>
"""


def format_push_html_message(event: NotificationEvent) -> str:
    """Build a compact Pushover-compatible HTML push message.

    Line structure relies on newline characters, which the provider renders as
    line breaks in HTML mode; ``<br>`` is not part of the documented tag set.
    """
    status = escape(_status_label(event))
    status_color = _push_severity_color(_event_severity(event))
    parts: list[str] = [
        f'<b>Status:</b> <font color="{status_color}">{status}</font>',
        f"<b>Target:</b> {_escape_truncated(_task_label(event), _PUSH_HTML_TARGET_LIMIT)}",
        f"<b>Duration:</b> {escape(_duration_label(event.duration_seconds))}",
    ]

    if event.dry_run:
        parts.append('<b>Dry run:</b> <font color="#1e40af">yes</font>')

    if event.message:
        parts.append("")
        parts.append(_escape_truncated(event.message, _PUSH_HTML_MESSAGE_LIMIT))

    if event.error:
        error = _escape_truncated(event.error, _PUSH_HTML_ERROR_LIMIT)
        parts.extend(
            [
                "",
                "<b>Error:</b>",
                f'<font color="{_push_severity_color("danger")}">{error}</font>',
            ]
        )

    if event.log_path is not None:
        parts.extend(
            ["", f"<b>Log:</b> {_escape_truncated(str(event.log_path), _PUSH_HTML_LOG_LIMIT)}"]
        )

    return "\n".join(parts)


def _report_has_failures(event: NotificationReportEvent) -> bool:
    return any(event.status_counts.get(status, 0) > 0 for status in _REPORT_DANGER_STATUSES)


def _report_danger_run_count(event: NotificationReportEvent) -> int:
    return sum(event.status_counts.get(status, 0) for status in _REPORT_DANGER_STATUSES)


def _job_severity(runs: list[NotificationReportRunSummary]) -> str:
    statuses = {run.status for run in runs}
    if statuses & _REPORT_DANGER_STATUSES:
        return "danger"
    if statuses & _REPORT_WARN_STATUSES:
        return "warn"
    return "ok"


def _grouped_jobs(
    event: NotificationReportEvent,
) -> list[tuple[str, list[NotificationReportRunSummary]]]:
    grouped: dict[str, list[NotificationReportRunSummary]] = {}
    for run in event.runs:
        grouped.setdefault(run.job, []).append(run)
    severity_rank = {"danger": 0, "warn": 1, "ok": 2}
    return sorted(
        grouped.items(),
        key=lambda item: (severity_rank[_job_severity(item[1])], item[0]),
    )


def _report_danger_job_count(event: NotificationReportEvent) -> int:
    return sum(1 for _, runs in _grouped_jobs(event) if _job_severity(runs) == "danger")


def _job_counts_label(runs: list[NotificationReportRunSummary]) -> str:
    counts: dict[str, int] = {}
    for run in runs:
        counts[run.status] = counts.get(run.status, 0) + 1
    order = [status for status in REPORT_TERMINAL_STATUSES if status != "success"] + ["success"]
    parts = [
        f"{counts[status]} {_report_status_label(status).lower()}"
        for status in order
        if counts.get(status)
    ]
    return ", ".join(parts)


def _report_issues(event: NotificationReportEvent) -> list[NotificationReportRunSummary]:
    issues = [run for run in event.runs if run.status != "success"]
    return sorted(issues, key=lambda run: 0 if run.status in _REPORT_DANGER_STATUSES else 1)


def _report_subtitle(event: NotificationReportEvent) -> str:
    window = f"{_timestamp(event.window_start)} - {_timestamp(event.window_end)}"
    if not event.runs:
        return f"no runs finished ({window})"
    if _report_has_failures(event):
        failures = _pluralize(_report_danger_run_count(event), "failure")
        jobs = _pluralize(_report_danger_job_count(event), "job")
        return f"{failures} across {jobs} ({window})"
    total_jobs = len({run.job for run in event.runs})
    return f"{_pluralize(len(event.runs), 'run')} across {_pluralize(total_jobs, 'job')} ({window})"


def _report_verdict(event: NotificationReportEvent) -> str:
    return "Needs attention" if _report_has_failures(event) else "All clear"


def format_report_subject(event: NotificationReportEvent) -> str:
    return f"[DK] {_report_verdict(event)}: {_report_subtitle(event)}"


def format_report_push_title(event: NotificationReportEvent) -> str:
    hours = round((event.window_end - event.window_start).total_seconds() / 3600)
    span = f"{hours}h" if hours > 0 else "report"
    if not _report_has_failures(event):
        return f"All clear ({span} report)"
    failures = _pluralize(_report_danger_run_count(event), "failure")
    return f"Needs attention: {failures} ({span} report)"


def _issue_text_line(run: NotificationReportRunSummary, now: datetime) -> str:
    header = (
        f"- {_report_status_label(run.status):<10}| {run.target} | "
        f"{_duration_label(run.duration_seconds)} | {_timestamp(run.started_at)} "
        f"({_relative_label(run.started_at, now=now)})"
    )
    detail = run.error or ("dry run" if run.dry_run else "")
    if detail:
        return header + "\n" + " " * 14 + _truncate(detail, _REPORT_ERROR_LIMIT)
    return header


def format_report_text_body(event: NotificationReportEvent) -> str:
    lines: list[str] = [
        f"Dockkeep - Run report: {_report_verdict(event)}",
        _report_subtitle(event),
    ]

    jobs = _grouped_jobs(event)
    if jobs:
        visible_jobs = jobs[:_REPORT_JOB_LIMIT]
        lines.extend(["", "Jobs", "----"])
        for job, runs in visible_jobs:
            tag = "[ok]" if _job_severity(runs) == "ok" else "[!]"
            lines.append(f"{tag:<4} {job:<14} {_job_counts_label(runs)}")
        hidden_jobs = len(jobs) - len(visible_jobs)
        if hidden_jobs > 0:
            lines.append(f"... {hidden_jobs} more job(s) omitted.")

    issues = _report_issues(event)
    if issues:
        visible_issues = issues[:_REPORT_RUN_LIMIT]
        lines.extend(["", "Issues", "------"])
        lines.extend(_issue_text_line(run, event.generated_at) for run in visible_issues)
        hidden_issues = len(issues) - len(visible_issues)
        if hidden_issues > 0:
            lines.append(f"... {hidden_issues} more issue(s) omitted.")
    elif not event.runs:
        lines.extend(
            [
                "",
                "Heartbeat",
                "---------",
                "The scheduler is alive. No runs finished in this window.",
            ]
        )

    return "\n".join(lines)


def _section_label_html(label: str) -> str:
    return (
        '<div style="padding:16px 20px 8px;font-size:11px;letter-spacing:.08em;'
        f'text-transform:uppercase;color:#64748b;font-weight:700;">{escape(label)}</div>'
    )


def _job_row_html(job: str, runs: list[NotificationReportRunSummary]) -> str:
    colors = _SEVERITY_STYLES[_job_severity(runs)]
    return (
        '<div style="display:flex;align-items:center;gap:10px;font-size:13px;">'
        '<span style="flex:0 0 auto;padding:3px 9px;border-radius:6px;font-weight:600;'
        "font-size:12px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;"
        f'background:{colors["bg"]};color:{colors["fg"]};">{escape(job)}</span>'
        f'<span style="margin-left:auto;color:#64748b;">{escape(_job_counts_label(runs))}</span>'
        "</div>"
    )


def _report_jobs_html(event: NotificationReportEvent) -> str:
    jobs = _grouped_jobs(event)
    if not jobs:
        return ""
    visible_jobs = jobs[:_REPORT_JOB_LIMIT]
    rows = "\n".join(_job_row_html(job, runs) for job, runs in visible_jobs)
    hidden_jobs = len(jobs) - len(visible_jobs)
    if hidden_jobs > 0:
        rows += (
            '\n<div style="font-size:12px;color:#64748b;">'
            f"... {hidden_jobs} more job(s) omitted.</div>"
        )
    return (
        _section_label_html("Jobs")
        + f'<div style="padding:0 20px;display:flex;flex-direction:column;gap:6px;">{rows}</div>'
    )


def _issue_box_html(run: NotificationReportRunSummary, now: datetime) -> str:
    severity = "danger" if run.status in _REPORT_DANGER_STATUSES else "warn"
    colors = _SEVERITY_STYLES[severity]
    detail = run.error or ("dry run" if run.dry_run else "")
    detail_html = (
        f'<div style="font-size:12.5px;color:#0f172a;opacity:.85;margin-top:3px;">'
        f"{escape(_truncate(detail, _REPORT_ERROR_LIMIT))}</div>"
        if detail
        else ""
    )
    meta = f"{run.origin} &middot; {_relative_label(run.started_at, now=now)}"
    return (
        f'<div style="padding:12px 14px;border-radius:9px;background:{colors["bg"]};">'
        '<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;font-size:13px;">'
        f'<span style="font-weight:700;color:{colors["fg"]};">'
        f"{escape(_report_status_label(run.status))}</span>"
        '<span style="font-family:ui-monospace,SFMono-Regular,Consolas,monospace;'
        f'font-size:12.5px;color:#0f172a;">{escape(run.target)}</span>'
        f'<span style="margin-left:auto;font-size:12px;color:#64748b;">'
        f"{escape(_duration_label(run.duration_seconds))}</span>"
        "</div>"
        f"{detail_html}"
        f'<div style="font-size:11.5px;color:#64748b;opacity:.85;margin-top:3px;">{meta}</div>'
        "</div>"
    )


def _report_issues_html(event: NotificationReportEvent) -> str:
    issues = _report_issues(event)
    if not issues:
        return ""
    visible_issues = issues[:_REPORT_RUN_LIMIT]
    items = "\n".join(_issue_box_html(run, event.generated_at) for run in visible_issues)
    hidden_issues = len(issues) - len(visible_issues)
    if hidden_issues > 0:
        items += (
            '\n<div style="font-size:12px;color:#64748b;">'
            f"... {hidden_issues} more issue(s) omitted.</div>"
        )
    return (
        _section_label_html("Issues")
        + '<div style="padding:0 20px 4px;display:flex;flex-direction:column;gap:10px;">'
        f"{items}</div>"
    )


def format_report_html_body(event: NotificationReportEvent) -> str:
    verdict = _report_verdict(event)
    verdict_color = _SEVERITY_HEADLINE_COLORS["danger" if _report_has_failures(event) else "ok"]
    body_html = _report_jobs_html(event) + _report_issues_html(event)
    if not body_html:
        body_html = (
            '<div style="padding:16px 20px 20px;font-size:13px;color:#334155;">'
            "No runs finished in this reporting window. The scheduler is alive."
            "</div>"
        )

    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f8fafc;font-family:Arial,sans-serif;">
    <main style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;
border-radius:12px;overflow:hidden;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;
padding:14px 20px;border-bottom:1px solid #e2e8f0;">
        <span style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;
color:#3f6b4f;font-weight:700;">Dockkeep</span>
        <span style="font-size:11px;letter-spacing:.04em;color:#64748b;">Run report</span>
      </div>
      <div style="padding:18px 20px 16px;border-left:4px solid {verdict_color};">
        <div style="font-size:18px;font-weight:700;color:#0f172a;">{escape(verdict)}</div>
        <div style="font-size:12.5px;color:#64748b;margin-top:3px;">
{escape(_report_subtitle(event))}</div>
      </div>
      {body_html}
      <div style="margin-top:6px;padding:14px 20px 18px;border-top:1px solid #e2e8f0;
font-size:11.5px;color:#64748b;">
        Sent by the Dockkeep scheduler &middot; configured under
        <code>global.notifications.report_schedule</code>
      </div>
    </main>
  </body>
</html>
"""


def _issue_push_html_lines(run: NotificationReportRunSummary) -> str:
    detail = run.error or ("dry run" if run.dry_run else "")
    lines = [_escape_truncated(run.target, _PUSH_HTML_TARGET_LIMIT)]
    if detail:
        detail_color = _push_severity_color("danger")
        lines.append(
            f'<font color="{detail_color}">'
            f"{_escape_truncated(detail, _PUSH_HTML_ERROR_LIMIT)}</font>"
        )
    return "\n".join(lines)


def _status_counts_push_label(event: NotificationReportEvent) -> str:
    order = [status for status in REPORT_TERMINAL_STATUSES if status != "success"] + ["success"]
    parts = [
        f"{event.status_counts[status]} {_report_status_label(status).lower()}"
        for status in order
        if event.status_counts.get(status)
    ]
    return ", ".join(parts) if parts else "none"


def format_report_push_html_message(event: NotificationReportEvent) -> str:
    """Build a compact Pushover-compatible HTML message for a periodic report.

    Line structure relies on newline characters, which the provider renders as
    line breaks in HTML mode; ``<br>`` is not part of the documented tag set.
    """
    window = f"{escape(_timestamp(event.window_start))} - {escape(_timestamp(event.window_end))}"
    parts = [
        f"<b>Window:</b> {window}",
        f"<b>Runs:</b> {escape(_status_counts_push_label(event))}",
    ]

    issues = _report_issues(event)
    if issues:
        visible_issues = issues[:_REPORT_PUSH_HTML_ISSUE_LIMIT]
        parts.append("")
        for index, run in enumerate(visible_issues):
            severity = "danger" if run.status in _REPORT_DANGER_STATUSES else "warn"
            status_color = _push_severity_color(severity)
            if index > 0:
                parts.append("")
            status_label = escape(_report_status_label(run.status))
            parts.append(f'<font color="{status_color}"><b>{status_label}:</b></font>')
            parts.append(_issue_push_html_lines(run))
        hidden_issues = len(issues) - len(visible_issues)
        if hidden_issues > 0:
            parts.append("")
            parts.append(f"<i>... {hidden_issues} more issue(s) omitted.</i>")
    elif not event.runs:
        heartbeat_color = _push_severity_color("ok")
        parts.extend(
            ["", f'<font color="{heartbeat_color}"><b>Heartbeat:</b></font> scheduler is alive.']
        )
    else:
        # Reached only when every run in the window was a success: any other
        # terminal status would have produced an issue entry above.
        parts.insert(0, f'<font color="{_push_severity_color("ok")}"><b>All clear</b></font>')

    return "\n".join(parts)
