from typing import Any

import pytest

from src.models.resolved_config import ResolvedAppConfig
from src.utils.validation import collect_config_warnings, validate_resolved_config
from tests.config_builders import (
    raw_app,
    raw_backup_task,
    raw_job,
    raw_rclone_task,
    resolved_app_config,
)


def _resolved(raw: dict[str, object]) -> ResolvedAppConfig:
    return resolved_app_config(raw)


def _base_app(**job_overrides: object) -> dict[str, object]:
    return raw_app(
        jobs={
            "myjob": raw_job(
                backup_tasks={"local": raw_backup_task(overrides={"password": "secret"})},
                overrides=dict(job_overrides),
            )
        }
    )


def test_global_notify_defaults_are_effective_on_backup() -> None:
    config = _resolved(_base_app())
    backup = config.jobs["myjob"].backup["local"]

    assert backup.notifications.notify_on_success is False
    assert backup.notifications.notify_on_error is False
    assert backup.notifications.notify_on_skipped is False


def test_job_notify_overrides_global_for_backup() -> None:
    config = _resolved(
        raw_app(
            global_config={"notify_on_error": True},
            jobs={"myjob": raw_job(overrides={"notify_on_error": False})},
        )
    )

    assert config.jobs["myjob"].backup["local"].notifications.notify_on_error is False


def test_backup_notify_overrides_job() -> None:
    config = _resolved(
        {
            "global": {"notify_on_success": False},
            "jobs": {
                "myjob": {
                    "notify_on_success": False,
                    "backup": {
                        "local": raw_backup_task(
                            overrides={"notify_on_success": True},
                        )
                    },
                }
            },
        }
    )

    assert config.jobs["myjob"].backup["local"].notifications.notify_on_success is True


def test_workflow_inherits_job_notifications() -> None:
    config = _resolved(
        raw_app(
            global_config={"notify_on_error": True},
            jobs={
                "myjob": raw_job(
                    workflows={"daily": {"steps": ["backup.local"]}},
                    overrides={"notify_on_error": False},
                )
            },
        )
    )

    assert config.jobs["myjob"].workflows["daily"].notifications.notify_on_error is False


def test_workflow_notify_overrides_job() -> None:
    config = _resolved(
        raw_app(
            global_config={"notify_on_error": True},
            jobs={
                "myjob": raw_job(
                    workflows={"daily": {"steps": ["backup.local"], "notify_on_error": False}},
                    overrides={"notify_on_error": True},
                )
            },
        )
    )

    assert config.jobs["myjob"].workflows["daily"].notifications.notify_on_error is False


def test_rclone_inherits_job_notifications() -> None:
    config = _resolved(
        raw_app(
            global_config={"notify_on_success": True},
            jobs={
                "myjob": raw_job(
                    rclone_tasks={"offsite": raw_rclone_task()},
                    overrides={"notify_on_error": True},
                )
            },
        )
    )
    rclone = config.jobs["myjob"].rclone["offsite"]

    assert rclone.notifications.notify_on_success is True
    assert rclone.notifications.notify_on_error is True


def test_rclone_notify_overrides_job() -> None:
    config = _resolved(
        raw_app(
            global_config={"notify_on_error": True},
            jobs={
                "myjob": raw_job(
                    rclone_tasks={
                        "offsite": raw_rclone_task(
                            overrides={"notify_on_error": False},
                        )
                    },
                    overrides={"notify_on_error": True},
                )
            },
        )
    )

    assert config.jobs["myjob"].rclone["offsite"].notifications.notify_on_error is False


def test_provider_presence_check_defaults_without_provider_ok() -> None:
    config = _resolved(_base_app())

    validate_resolved_config(config)
    assert collect_config_warnings(config) == []


def test_provider_presence_check_any_trigger_without_provider_warns() -> None:
    config = _resolved(raw_app(global_config={"notify_on_error": True}, jobs={"myjob": raw_job()}))

    validate_resolved_config(config)
    assert collect_config_warnings(config)


def _mail(events: list[str] | None = None) -> dict[str, Any]:
    """Build a minimal mail provider table, optionally with channel routing."""
    mail: dict[str, Any] = {
        "host": "smtp.example.test",
        "from_addr": "dk@example.test",
        "to": ["admin@example.test"],
    }
    if events is not None:
        mail["events"] = events
    return mail


@pytest.mark.parametrize(
    ("trigger", "carried", "uncarried_kind"),
    [
        ("notify_on_error", ["success"], "error"),
        ("notify_on_success", ["error"], "success"),
        ("notify_on_skipped", ["error"], "skipped"),
    ],
)
def test_trigger_without_carrying_channel_warns(
    trigger: str, carried: list[str], uncarried_kind: str
) -> None:
    config = _resolved(
        raw_app(
            global_config={trigger: True, "notifications": {"mail": _mail(carried)}},
            jobs={"myjob": raw_job()},
        )
    )

    warnings = collect_config_warnings(config)

    assert any(f"'{uncarried_kind}' events" in warning for warning in warnings)


def test_report_schedule_without_carrying_channel_warns() -> None:
    config = _resolved(
        raw_app(
            global_config={
                "notifications": {"report_schedule": "0 8 * * *", "mail": _mail(["error"])}
            },
            jobs={"myjob": raw_job()},
        )
    )

    warnings = collect_config_warnings(config)

    assert any("'report' events" in warning for warning in warnings)


@pytest.mark.parametrize("events", [None, ["error", "report"]])
def test_carried_events_produce_no_routing_warning(events: list[str] | None) -> None:
    config = _resolved(
        raw_app(
            global_config={
                "notify_on_error": True,
                "notifications": {"report_schedule": "0 8 * * *", "mail": _mail(events)},
            },
            jobs={
                "myjob": raw_job(
                    backup_tasks={"local": raw_backup_task(overrides={"password": "secret"})}
                )
            },
        )
    )

    assert collect_config_warnings(config) == []


def test_second_channel_carrying_the_event_is_enough() -> None:
    config = _resolved(
        raw_app(
            global_config={
                "notify_on_error": True,
                "notifications": {
                    "mail": _mail(["success"]),
                    "pushover": {
                        "token_env": "PUSHOVER_TOKEN",
                        "user_key_env": "PUSHOVER_USER_KEY",
                        "events": ["error"],
                    },
                },
            },
            jobs={
                "myjob": raw_job(
                    backup_tasks={"local": raw_backup_task(overrides={"password": "secret"})}
                )
            },
        )
    )

    warnings = collect_config_warnings(config)

    assert not [w for w in warnings if w.startswith("Notification routing")]


def test_provider_presence_check_all_flags_false_without_provider_ok() -> None:
    config = _resolved(
        {
            "global": {
                "notify_on_success": False,
                "notify_on_error": False,
                "notify_on_skipped": False,
            },
            "jobs": {
                "myjob": raw_job(
                    backup_tasks={"local": raw_backup_task(overrides={"password": "secret"})}
                )
            },
        }
    )

    validate_resolved_config(config)
    assert collect_config_warnings(config) == []
