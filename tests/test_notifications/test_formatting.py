from datetime import datetime
from pathlib import Path

import pytest

from src.notifications import formatting
from src.notifications.events import (
    NotificationEvent,
    NotificationReportEvent,
    NotificationReportRunSummary,
)
from src.notifications.formatting import (
    _PUSH_HTML_HARD_LIMIT,
    _truncate,
    format_html_body,
    format_push_html_message,
    format_report_html_body,
    format_report_push_html_message,
    format_report_push_title,
    format_report_subject,
    format_report_text_body,
    format_subject,
    format_text_body,
)


def _event(
    status: str = "success",
    task_type: str = "backup",
    task_name: str = "local",
    dry_run: bool = False,
    message: str = "Backup completed",
    error: str | None = None,
    log_path: Path | None = None,
    finished_at: datetime | None = datetime(2024, 1, 15, 2, 0, 45),
) -> NotificationEvent:
    return NotificationEvent(
        job_name="homeserver",
        task_type=task_type,  # type: ignore[arg-type]
        task_name=task_name,
        status=status,  # type: ignore[arg-type]
        started_at=datetime(2024, 1, 15, 2, 0, 0),
        finished_at=finished_at,
        dry_run=dry_run,
        message=message,
        error=error,
        log_path=log_path,
    )


def _report_run(
    job: str = "homeserver",
    target: str = "homeserver.backup.local",
    status: str = "success",
    error: str | None = None,
    dry_run: bool = False,
) -> NotificationReportRunSummary:
    return NotificationReportRunSummary(
        origin="manual",
        job=job,
        target=target,
        status=status,  # type: ignore[arg-type]
        started_at=datetime(2024, 1, 15, 2, 0, 0),
        finished_at=datetime(2024, 1, 15, 2, 0, 45),
        duration_seconds=45,
        dry_run=dry_run,
        error=error,
    )


def _check_push_html_budget() -> None:
    single_run_fields = (
        formatting._PUSH_HTML_TARGET_LIMIT
        + formatting._PUSH_HTML_MESSAGE_LIMIT
        + formatting._PUSH_HTML_ERROR_LIMIT
        + formatting._PUSH_HTML_LOG_LIMIT
    )
    report_fields = formatting._REPORT_PUSH_HTML_ISSUE_LIMIT * (
        formatting._PUSH_HTML_TARGET_LIMIT + formatting._PUSH_HTML_ERROR_LIMIT
    )
    worst_case = max(single_run_fields, report_fields) + formatting._PUSH_HTML_MARKUP_BUDGET
    if worst_case > formatting._PUSH_HTML_HARD_LIMIT:
        raise AssertionError(
            f"push HTML field budget {worst_case} exceeds provider limit "
            f"{formatting._PUSH_HTML_HARD_LIMIT}"
        )


def _report_event(
    runs: tuple[NotificationReportRunSummary, ...] | None = None,
    status_counts: dict[str, int] | None = None,
) -> NotificationReportEvent:
    report_runs = runs if runs is not None else (_report_run(),)
    return NotificationReportEvent(
        window_start=datetime(2024, 1, 15, 2, 0, 0),
        window_end=datetime(2024, 1, 15, 3, 0, 0),
        generated_at=datetime(2024, 1, 15, 3, 0, 5),
        status_counts=status_counts or {"success": len(report_runs)},
        runs=report_runs,
    )


class TestTruncate:
    def test_returns_value_when_within_limit(self) -> None:
        assert _truncate("abc", 3) == "abc"

    def test_truncates_to_limit_with_marker(self) -> None:
        assert _truncate("abcdef", 5) == "ab..."

    def test_small_limits_never_exceed_limit(self) -> None:
        assert _truncate("abcdef", 0) == ""
        assert _truncate("abcdef", 1) == "."
        assert _truncate("abcdef", 2) == ".."


class TestFormatSubject:
    def test_backup_success(self) -> None:
        assert format_subject(_event(status="success")) == "[DK] Success homeserver.backup.local"

    def test_backup_failure(self) -> None:
        assert format_subject(_event(status="failure")) == "[DK] Failed homeserver.backup.local"

    def test_backup_skipped(self) -> None:
        assert format_subject(_event(status="skipped")) == "[DK] Skipped homeserver.backup.local"

    def test_backup_unexpected_error(self) -> None:
        subject = format_subject(_event(status="unexpected_error"))
        assert subject == "[DK] Unexpected error homeserver.backup.local"

    def test_workflow_success(self) -> None:
        event = _event(status="success", task_type="workflow", task_name="daily")
        assert format_subject(event) == "[DK] Success homeserver.workflow.daily"

    def test_workflow_failure(self) -> None:
        event = _event(status="failure", task_type="workflow", task_name="daily")
        assert format_subject(event) == "[DK] Failed homeserver.workflow.daily"

    def test_dry_run_appended_to_subject(self) -> None:
        subject = format_subject(_event(status="success", dry_run=True))
        assert subject.endswith("[DRY RUN]")
        assert "Success" in subject

    def test_no_dry_run_suffix_when_false(self) -> None:
        subject = format_subject(_event(status="success", dry_run=False))
        assert "[DRY RUN]" not in subject


class TestFormatTextBody:
    def test_contains_status(self) -> None:
        body = format_text_body(_event(status="success"))
        assert "Success" in body

    def test_contains_task_backup(self) -> None:
        body = format_text_body(_event(task_type="backup", task_name="local"))
        assert "homeserver.backup.local" in body

    def test_contains_task_workflow(self) -> None:
        body = format_text_body(_event(task_type="workflow", task_name="daily"))
        assert "homeserver.workflow.daily" in body

    def test_contains_started_at(self) -> None:
        body = format_text_body(_event())
        assert "2024-01-15 02:00:00" in body

    def test_contains_duration_when_available(self) -> None:
        body = format_text_body(_event(finished_at=datetime(2024, 1, 15, 2, 0, 45)))
        assert "Duration:" in body
        assert "45s" in body

    def test_contains_finished_at_when_available(self) -> None:
        body = format_text_body(_event(finished_at=datetime(2024, 1, 15, 2, 0, 45)))
        assert "Finished:" in body
        assert "2024-01-15 02:00:45" in body

    def test_no_duration_when_finished_at_none(self) -> None:
        body = format_text_body(_event(finished_at=None))
        assert "Duration:" in body
        assert "unknown" in body

    def test_dry_run_marker_present(self) -> None:
        body = format_text_body(_event(dry_run=True))
        assert "DRY RUN" in body

    def test_no_dry_run_marker_when_false(self) -> None:
        body = format_text_body(_event(dry_run=False))
        assert "DRY RUN" not in body

    def test_error_included_when_present(self) -> None:
        body = format_text_body(_event(error="Repository not found"))
        assert "Repository not found" in body

    def test_no_error_section_when_absent(self) -> None:
        body = format_text_body(_event(error=None))
        assert "Error\n-----" not in body

    def test_log_path_included_when_present(self) -> None:
        body = format_text_body(_event(log_path=Path("/logs/homeserver/2024-01-15.log")))
        assert "/logs/homeserver/2024-01-15.log" in body

    def test_no_log_path_when_absent(self) -> None:
        body = format_text_body(_event(log_path=None))
        assert "Log:" not in body

    def test_message_included(self) -> None:
        body = format_text_body(_event(message="Backup completed"))
        assert "Backup completed" in body

    def test_long_message_error_and_log_path_are_truncated(self) -> None:
        long_message = "m" * 2_500
        long_error = "e" * 4_500
        long_log_path = Path("/logs") / ("x" * 700)

        body = format_text_body(
            _event(message=long_message, error=long_error, log_path=long_log_path)
        )

        assert long_message not in body
        assert long_error not in body
        assert str(long_log_path) not in body
        assert "m" * 1_997 + "..." in body
        assert "e" * 3_997 + "..." in body
        assert "/logs/" + ("x" * 491) + "..." in body


class TestFormatHtmlBody:
    def test_contains_structured_html_summary(self) -> None:
        body = format_html_body(_event(log_path=Path("/logs/homeserver/2024-01-15.log")))
        assert "<table" in body
        assert "Dockkeep" in body
        assert "homeserver.backup.local" in body
        assert "/logs/homeserver/2024-01-15.log" in body

    def test_escapes_message_and_error(self) -> None:
        body = format_html_body(_event(message="<b>done</b>", error="<script>bad</script>"))
        assert "&lt;b&gt;done&lt;/b&gt;" in body
        assert "&lt;script&gt;bad&lt;/script&gt;" in body
        assert "<script>bad</script>" not in body

    def test_long_message_error_and_log_path_are_truncated_after_escaping(self) -> None:
        long_message = "<" + ("m" * 2_500)
        long_error = "<" + ("e" * 4_500)
        long_log_path = Path("/logs") / ("x" * 700)

        body = format_html_body(
            _event(message=long_message, error=long_error, log_path=long_log_path)
        )

        assert escape_prefix("<" + ("m" * 2_500)) not in body
        assert escape_prefix("<" + ("e" * 4_500)) not in body
        assert str(long_log_path) not in body
        assert "&lt;" + ("m" * 1_996) + "..." in body
        assert "&lt;" + ("e" * 3_996) + "..." in body
        assert "/logs/" + ("x" * 491) + "..." in body


class TestFormatPushHtmlMessage:
    def test_uses_html_labels_and_highlighted_status(self) -> None:
        msg = format_push_html_message(_event(status="success"))
        assert "<b>Status:</b>" in msg
        assert '<font color="#166534">Success</font>' in msg
        assert "<b>Target:</b> homeserver.backup.local" in msg

    def test_workflow_task(self) -> None:
        msg = format_push_html_message(_event(task_type="workflow", task_name="daily"))
        assert "homeserver.workflow.daily" in msg

    def test_duration_included_when_available(self) -> None:
        msg = format_push_html_message(_event(finished_at=datetime(2024, 1, 15, 2, 0, 45)))
        assert "<b>Duration:</b>" in msg
        assert "45s" in msg

    def test_no_duration_when_finished_at_none(self) -> None:
        msg = format_push_html_message(_event(finished_at=None))
        assert "<b>Duration:</b>" in msg
        assert "unknown" in msg

    def test_message_included(self) -> None:
        msg = format_push_html_message(_event(message="Backup completed"))
        assert "Backup completed" in msg

    def test_error_is_escaped_and_highlighted(self) -> None:
        msg = format_push_html_message(_event(error="<bad>"))
        assert "<b>Error:</b>" in msg
        assert "&lt;bad&gt;" in msg
        assert "<bad>" not in msg

    def test_no_error_when_absent(self) -> None:
        msg = format_push_html_message(_event(error=None))
        assert "<b>Error:</b>" not in msg

    def test_dry_run_only_reported_when_set(self) -> None:
        assert "<b>Dry run:</b>" in format_push_html_message(_event(dry_run=True))
        assert "<b>Dry run:</b>" not in format_push_html_message(_event(dry_run=False))

    def test_pathological_values_stay_below_pushover_limit(self) -> None:
        msg = format_push_html_message(
            _event(
                task_name="<" * 500,
                message="<" * 500,
                error="<" * 500,
                log_path=Path("/logs") / ("<" * 500),
            )
        )

        assert len(msg) <= _PUSH_HTML_HARD_LIMIT
        assert "<" * 20 not in msg

    def test_quote_heavy_values_stay_below_pushover_limit(self) -> None:
        """Quotes escape to six characters each — the worst case for the budget."""
        msg = format_push_html_message(
            _event(
                task_name='"' * 500,
                message='"' * 500,
                error='"' * 500,
                log_path=Path("/logs") / ('"' * 500),
                dry_run=True,
                status="unexpected_error",
            )
        )

        assert len(msg) <= _PUSH_HTML_HARD_LIMIT

    def test_field_budget_matches_provider_limit(self) -> None:
        _check_push_html_budget()

    def test_widened_field_limit_fails_the_budget_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raising a field limit past the budget must fail loudly, also under ``-O``."""
        monkeypatch.setattr(formatting, "_PUSH_HTML_ERROR_LIMIT", 500)

        with pytest.raises(AssertionError):
            _check_push_html_budget()


class TestFormatReport:
    def test_subject_marks_successful_window_as_all_clear(self) -> None:
        subject = format_report_subject(_report_event())
        assert subject.startswith("[DK] All clear")
        assert "2024-01-15 02:00:00" in subject
        assert "2024-01-15 03:00:00" in subject

    def test_subject_marks_failure_window_as_needs_attention(self) -> None:
        event = _report_event(
            runs=(_report_run(status="failed"),),
            status_counts={"failed": 1},
        )
        subject = format_report_subject(event)
        assert "Needs attention" in subject
        assert "1 failure" in subject
        assert "1 job" in subject

    def test_skipped_only_window_does_not_trigger_attention(self) -> None:
        """Skipped/cancelled are informational, not real failures."""
        event = _report_event(
            runs=(_report_run(status="skipped"),),
            status_counts={"skipped": 1},
        )
        assert "All clear" in format_report_subject(event)

    def test_text_body_groups_runs_by_job_and_lists_issues(self) -> None:
        event = _report_event(
            runs=(
                _report_run(job="homeserver", target="homeserver.backup.local", status="success"),
                _report_run(
                    job="homeserver",
                    target="homeserver.workflow.daily",
                    status="failed",
                    error="boom",
                ),
                _report_run(job="mediaserver", target="mediaserver.backup.photos"),
            ),
            status_counts={"success": 2, "failed": 1},
        )
        body = format_report_text_body(event)
        assert "2024-01-15 02:00:00" in body
        assert "Jobs" in body
        assert "homeserver" in body
        assert "1 failed, 1 success" in body
        assert "mediaserver" in body
        assert "1 success" in body
        assert "Issues" in body
        assert "homeserver.workflow.daily" in body
        assert "boom" in body
        assert "homeserver.backup.local" not in body

    def test_empty_window_is_a_heartbeat(self) -> None:
        body = format_report_text_body(_report_event(runs=(), status_counts={}))
        assert "No runs finished" in body
        assert "scheduler is alive" in body

    def test_job_list_is_bounded(self) -> None:
        runs = tuple(
            _report_run(job=f"job{index:02d}", target=f"job{index:02d}.backup.local")
            for index in range(45)
        )
        counts = {"success": 45}
        body = format_report_text_body(_report_event(runs=runs, status_counts=counts))
        assert "job39" in body
        assert "job40" not in body
        assert "5 more job(s) omitted" in body

    def test_issue_list_is_bounded_and_lists_failures_first(self) -> None:
        runs = tuple(
            _report_run(
                job=f"job{index:02d}",
                target=f"job{index:02d}.backup.local",
                status="failed",
                error="boom",
            )
            for index in range(25)
        )
        counts = {"failed": 25}
        body = format_report_text_body(_report_event(runs=runs, status_counts=counts))
        assert "job19.backup.local" in body
        assert "job20.backup.local" not in body
        assert "5 more issue(s) omitted" in body

    def test_html_body_escapes_report_run_values(self) -> None:
        event = _report_event(
            runs=(_report_run(job="<job>", target="<target>", status="failed", error="<bad>"),),
            status_counts={"failed": 1},
        )
        body = format_report_html_body(event)
        assert "&lt;job&gt;" in body
        assert "&lt;target&gt;" in body
        assert "&lt;bad&gt;" in body
        assert "<job>" not in body
        assert "<target>" not in body

    def test_push_title_is_short_and_leads_with_verdict(self) -> None:
        event = _report_event(
            runs=(_report_run(status="failed"),),
            status_counts={"failed": 1},
        )
        title = format_report_push_title(event)
        assert title.startswith("Needs attention: 1 failure")
        assert len(title) < 60

    def test_push_html_message_summarizes_window_and_counts(self) -> None:
        msg = format_report_push_html_message(_report_event())
        assert "<b>Window:</b> 2024-01-15 02:00:00 - 2024-01-15 03:00:00" in msg
        assert "<b>Runs:</b> 1 success" in msg
        assert "<b>All clear</b>" in msg

    def test_push_html_message_summarizes_many_runs_without_job_breakdown(self) -> None:
        runs = tuple(
            _report_run(job=f"job{index:02d}", target=f"job{index:02d}.backup.local")
            for index in range(12)
        )
        msg = format_report_push_html_message(
            _report_event(runs=runs, status_counts={"success": 12})
        )
        assert "<b>Runs:</b> 12 success" in msg
        assert "job00" not in msg

    def test_push_html_message_is_heartbeat_for_empty_window(self) -> None:
        msg = format_report_push_html_message(_report_event(runs=(), status_counts={}))
        assert "<b>Heartbeat:</b>" in msg
        assert "<b>Runs:</b> none" in msg

    def test_push_html_message_lists_escaped_issue_detail(self) -> None:
        event = _report_event(
            runs=(_report_run(target="<target>", status="failed", error="<boom>"),),
            status_counts={"failed": 1},
        )
        msg = format_report_push_html_message(event)
        assert "<b>Runs:</b> 1 failed" in msg
        assert "<b>Failed:</b>" in msg
        assert "&lt;target&gt;" in msg
        assert "&lt;boom&gt;" in msg
        assert "<target>" not in msg
        assert "<boom>" not in msg

    def test_push_html_message_stays_below_pushover_limit(self) -> None:
        runs = tuple(
            _report_run(
                target=f"job{index}.backup.{'<' * 500}",
                status="failed",
                error="<" * 500,
            )
            for index in range(6)
        )
        msg = format_report_push_html_message(_report_event(runs=runs, status_counts={"failed": 6}))

        assert len(msg) <= _PUSH_HTML_HARD_LIMIT
        assert "4 more issue(s) omitted" in msg

    def test_push_html_message_stays_below_limit_for_every_status(self) -> None:
        """Quote-heavy issues plus a full status breakdown is the worst case."""
        runs = tuple(
            _report_run(target='"' * 500, status="failed", error='"' * 500) for _ in range(6)
        )
        msg = format_report_push_html_message(
            _report_event(
                runs=runs,
                status_counts={
                    "success": 3,
                    "failed": 6,
                    "cancelled": 1,
                    "skipped": 2,
                    "lock_error": 1,
                    "config_error": 1,
                    "unexpected_error": 1,
                },
            )
        )

        assert len(msg) <= _PUSH_HTML_HARD_LIMIT


def escape_prefix(value: str) -> str:
    return value.replace("<", "&lt;")
