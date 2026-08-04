from __future__ import annotations

from src.services.backup_stats_helpers import compute_duration


def test_compute_duration_ignores_total_duration_and_uses_timestamps() -> None:
    summary = {
        "total_duration": 12,
        "backup_start": "2026-06-09T10:00:00+00:00",
        "backup_end": "2026-06-09T10:05:00+00:00",
    }

    assert compute_duration(summary) == 300


def test_compute_duration_falls_back_to_start_end_timestamps() -> None:
    summary = {
        "backup_start": "2026-06-09T10:00:00+00:00",
        "backup_end": "2026-06-09T10:01:30+00:00",
    }

    assert compute_duration(summary) == 90


def test_compute_duration_rounds_to_whole_seconds() -> None:
    summary = {
        "backup_start": "2026-06-09T10:00:00+00:00",
        "backup_end": "2026-06-09T10:00:01.6+00:00",
    }

    assert compute_duration(summary) == 2


def test_compute_duration_returns_none_for_missing_or_invalid_data() -> None:
    assert compute_duration({}) is None
    assert compute_duration({"backup_start": "invalid", "backup_end": "also-invalid"}) is None
    assert (
        compute_duration(
            {
                "backup_start": "2026-06-09T10:00:00+00:00",
                "backup_end": "2026-06-09T09:59:59+00:00",
            }
        )
        is None
    )
