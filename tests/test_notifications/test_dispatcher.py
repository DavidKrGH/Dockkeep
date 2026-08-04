import logging
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.models.resolved_config import (
    ResolvedGlobalNotificationProvidersConfig,
    ResolvedMailNotificationConfig,
    ResolvedPushoverNotificationConfig,
)
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.events import (
    NotificationEvent,
    NotificationPolicy,
    NotificationReportEvent,
    NotificationReportRunSummary,
)


def _make_event(
    status: str = "success",
    task_type: str = "backup",
    task_name: str = "local",
    dry_run: bool = False,
) -> NotificationEvent:
    return NotificationEvent(
        job_name="homeserver",
        task_type=task_type,  # type: ignore[arg-type]
        task_name=task_name,
        status=status,  # type: ignore[arg-type]
        started_at=datetime(2024, 1, 15, 2, 0, 0),
        finished_at=datetime(2024, 1, 15, 2, 0, 45),
        dry_run=dry_run,
        message="Backup completed",
    )


def _make_report_event() -> NotificationReportEvent:
    run = NotificationReportRunSummary(
        origin="scheduler",
        job="homeserver",
        target="homeserver.backup.local",
        status="success",
        started_at=datetime(2024, 1, 15, 2, 0, 0),
        finished_at=datetime(2024, 1, 15, 2, 0, 45),
        duration_seconds=45,
        dry_run=False,
    )
    return NotificationReportEvent(
        window_start=datetime(2024, 1, 15, 2, 0, 0),
        window_end=datetime(2024, 1, 15, 3, 0, 0),
        generated_at=datetime(2024, 1, 15, 3, 0, 5),
        status_counts={"success": 1},
        runs=(run,),
    )


def _all_false_policy() -> NotificationPolicy:
    return NotificationPolicy(
        notify_on_success=False,
        notify_on_error=False,
        notify_on_skipped=False,
    )


def _policy_with(status: str) -> NotificationPolicy:
    return NotificationPolicy(
        notify_on_success=(status == "success"),
        notify_on_error=(status in {"failure", "lock_error", "config_error", "unexpected_error"}),
        notify_on_skipped=(status == "skipped"),
    )


def _fake_provider(name: str = "fake", events: list[str] | None = None) -> MagicMock:
    provider = MagicMock()
    provider.name = name
    provider.events = events
    return provider


def _minimal_mail_config() -> ResolvedMailNotificationConfig:
    return ResolvedMailNotificationConfig(
        host="localhost",
        port=1025,
        connection_security="none",
        from_addr="dk@example.test",
        to=["admin@example.test"],
    )


def _dispatcher_with_providers(
    *providers: MagicMock,
    logger: logging.Logger | None = None,
) -> NotificationDispatcher:
    """Build a dispatcher with injected fake providers (bypassing _build_providers)."""
    config = ResolvedGlobalNotificationProvidersConfig()
    log = logger or logging.getLogger("test")
    d = NotificationDispatcher(config, log)
    d._providers = list(providers)
    return d


@pytest.mark.parametrize(
    "status",
    ["success", "failure", "skipped", "lock_error", "config_error", "unexpected_error"],
)
def test_no_send_when_policy_all_false(status: str) -> None:
    provider = _fake_provider()
    d = _dispatcher_with_providers(provider)
    result = d.notify(_make_event(status=status), _all_false_policy())
    provider.send.assert_not_called()
    assert result.attempted == 0


@pytest.mark.parametrize(
    "status",
    ["success", "failure", "skipped", "lock_error", "config_error", "unexpected_error"],
)
def test_sends_when_policy_matches(status: str) -> None:
    provider = _fake_provider()
    d = _dispatcher_with_providers(provider)
    result = d.notify(_make_event(status=status), _policy_with(status))
    provider.send.assert_called_once()
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0


def test_does_not_send_for_mismatched_status() -> None:
    provider = _fake_provider()
    d = _dispatcher_with_providers(provider)
    policy = _policy_with("failure")
    result = d.notify(_make_event(status="success"), policy)
    provider.send.assert_not_called()
    assert result.attempted == 0


def test_sends_to_all_active_providers() -> None:
    p1 = _fake_provider("mail")
    p2 = _fake_provider("pushover")
    event = _make_event(status="success")
    policy = _policy_with("success")
    d = _dispatcher_with_providers(p1, p2)
    result = d.notify(event, policy)
    p1.send.assert_called_once_with(event)
    p2.send.assert_called_once_with(event)
    assert result.attempted == 2
    assert result.succeeded == 2
    assert result.failed == 0


@pytest.mark.parametrize("status", ["failure", "lock_error", "config_error", "unexpected_error"])
def test_provider_events_route_error_statuses_per_channel(status: str) -> None:
    mail_provider = _fake_provider("mail", events=["success", "report"])
    pushover_provider = _fake_provider("pushover", events=["error"])
    event = _make_event(status=status)
    policy = _policy_with(status)
    d = _dispatcher_with_providers(mail_provider, pushover_provider)

    result = d.notify(event, policy)

    mail_provider.send.assert_not_called()
    pushover_provider.send.assert_called_once_with(event)
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0


def test_unset_provider_events_carry_every_event() -> None:
    mail_provider = _fake_provider("mail")
    event = _make_event(status="failure")
    d = _dispatcher_with_providers(mail_provider)

    result = d.notify(event, _policy_with("failure"))

    mail_provider.send.assert_called_once_with(event)
    assert result.attempted == 1


def test_provider_events_cannot_raise_events_the_task_policy_suppresses() -> None:
    mail_provider = _fake_provider("mail", events=["success"])
    event = _make_event(status="success")
    d = _dispatcher_with_providers(mail_provider)

    result = d.notify(event, _policy_with("failure"))

    mail_provider.send.assert_not_called()
    assert result.attempted == 0


def test_event_routed_to_no_channel_logs_hint(caplog: pytest.LogCaptureFixture) -> None:
    mail_provider = _fake_provider("mail", events=["report"])
    log = logging.getLogger("test_routing_drop")
    d = _dispatcher_with_providers(mail_provider, logger=log)

    with caplog.at_level(logging.INFO):
        result = d.notify(_make_event(status="failure"), _policy_with("failure"))

    mail_provider.send.assert_not_called()
    assert result.attempted == 0
    assert any("no configured provider routes" in record.message for record in caplog.records)


def test_continues_after_provider_failure() -> None:
    p1 = _fake_provider("mail")
    p1.send.side_effect = ConnectionError("SMTP refused")
    p2 = _fake_provider("pushover")
    event = _make_event(status="failure")
    policy = _policy_with("failure")
    d = _dispatcher_with_providers(p1, p2)
    result = d.notify(event, policy)
    p2.send.assert_called_once_with(event)
    assert result.attempted == 2
    assert result.succeeded == 1
    assert result.failed == 1


def test_all_providers_fail_returns_result_without_raising() -> None:
    p1 = _fake_provider("mail")
    p1.send.side_effect = RuntimeError("boom")
    p2 = _fake_provider("pushover")
    p2.send.side_effect = RuntimeError("bang")
    d = _dispatcher_with_providers(p1, p2)
    result = d.notify(_make_event(status="success"), _policy_with("success"))
    assert result.attempted == 2
    assert result.succeeded == 0
    assert result.failed == 2


def test_provider_timeout_log_mentions_editable_env(caplog: pytest.LogCaptureFixture) -> None:
    p1 = _fake_provider("mail")
    p1.send.side_effect = TimeoutError("timed out")
    d = _dispatcher_with_providers(p1)
    with caplog.at_level(logging.ERROR):
        d.notify(_make_event(status="failure"), _policy_with("failure"))
    assert any("DK_NOTIFICATION_TIMEOUT" in record.message for record in caplog.records)


def test_no_providers_logs_warning_when_notification_requested(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = ResolvedGlobalNotificationProvidersConfig()
    log = logging.getLogger("test_no_providers")
    d = NotificationDispatcher(config, log)
    with caplog.at_level(logging.WARNING):
        result = d.notify(_make_event(status="success"), _policy_with("success"))
    assert result.attempted == 0
    assert any("no providers" in r.message for r in caplog.records)


def test_no_providers_no_log_when_policy_disables() -> None:
    config = ResolvedGlobalNotificationProvidersConfig()
    log = MagicMock()
    d = NotificationDispatcher(config, log)
    d.notify(_make_event(status="success"), _all_false_policy())
    log.warning.assert_not_called()


def test_no_providers_built_from_empty_config() -> None:
    config = ResolvedGlobalNotificationProvidersConfig()
    d = NotificationDispatcher(config, logging.getLogger("test"))
    assert d._providers == []


def test_built_providers_carry_configured_events() -> None:
    """Die Routing-Liste der Config landet am gebauten Provider."""
    config = ResolvedGlobalNotificationProvidersConfig(
        mail=_minimal_mail_config(),
        pushover=ResolvedPushoverNotificationConfig(
            token_env="PUSHOVER_TOKEN",
            user_key_env="PUSHOVER_USER_KEY",
            events=["error"],
        ),
    )
    d = NotificationDispatcher(config, logging.getLogger("test"))
    events = {provider.name: provider.events for provider in d._providers}
    assert events == {"mail": None, "pushover": ["error"]}


def test_notification_timeout_env_overrides_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_NOTIFICATION_TIMEOUT", "27")
    config = ResolvedGlobalNotificationProvidersConfig(mail=_minimal_mail_config())
    d = NotificationDispatcher(config, logging.getLogger("test"))
    provider = d._providers[0]
    assert provider._timeout == 27  # type: ignore[attr-defined]


def test_mail_provider_default_timeout_is_ten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ohne DK_NOTIFICATION_TIMEOUT bekommt MailProvider timeout=10."""
    monkeypatch.delenv("DK_NOTIFICATION_TIMEOUT", raising=False)
    config = ResolvedGlobalNotificationProvidersConfig(mail=_minimal_mail_config())
    d = NotificationDispatcher(config, logging.getLogger("test"))
    provider = d._providers[0]
    assert provider._timeout == 10  # type: ignore[attr-defined]


def test_notification_timeout_env_applies_to_pushover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DK_NOTIFICATION_TIMEOUT=30 wird an PushoverProvider übergeben."""
    monkeypatch.setenv("DK_NOTIFICATION_TIMEOUT", "30")
    pushover_cfg = ResolvedPushoverNotificationConfig(
        token_env="PUSHOVER_TOKEN", user_key_env="PUSHOVER_USER_KEY"
    )
    config = ResolvedGlobalNotificationProvidersConfig(pushover=pushover_cfg)
    d = NotificationDispatcher(config, logging.getLogger("test"))
    provider = d._providers[0]
    assert provider._timeout == 30  # type: ignore[attr-defined]


def test_event_passed_to_provider_unchanged() -> None:
    provider = _fake_provider()
    d = _dispatcher_with_providers(provider)
    event = _make_event(status="skipped", dry_run=True)
    policy = _policy_with("skipped")
    d.notify(event, policy)
    provider.send.assert_called_once_with(event)


def test_report_sends_to_all_active_providers_without_policy() -> None:
    p1 = _fake_provider("mail")
    p2 = _fake_provider("pushover")
    event = _make_report_event()
    d = _dispatcher_with_providers(p1, p2)

    result = d.notify_report(event)

    p1.send_report.assert_called_once_with(event)
    p2.send_report.assert_called_once_with(event)
    p1.send.assert_not_called()
    p2.send.assert_not_called()
    assert result.attempted == 2
    assert result.succeeded == 2
    assert result.failed == 0


def test_report_events_route_to_channels_carrying_report() -> None:
    mail_provider = _fake_provider("mail", events=["error", "report"])
    pushover_provider = _fake_provider("pushover", events=["error"])
    event = _make_report_event()
    d = _dispatcher_with_providers(mail_provider, pushover_provider)

    result = d.notify_report(event)

    mail_provider.send_report.assert_called_once_with(event)
    pushover_provider.send_report.assert_not_called()
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0


def test_report_routed_to_no_channel_logs_hint(caplog: pytest.LogCaptureFixture) -> None:
    mail_provider = _fake_provider("mail", events=["error"])
    log = logging.getLogger("test_report_routing_drop")
    d = _dispatcher_with_providers(mail_provider, logger=log)

    with caplog.at_level(logging.INFO):
        result = d.notify_report(_make_report_event())

    mail_provider.send_report.assert_not_called()
    assert result.attempted == 0
    assert any("no configured provider routes" in record.message for record in caplog.records)


def test_report_continues_after_provider_failure() -> None:
    p1 = _fake_provider("mail")
    p1.send_report.side_effect = RuntimeError("smtp failed")
    p2 = _fake_provider("pushover")
    event = _make_report_event()
    d = _dispatcher_with_providers(p1, p2)

    result = d.notify_report(event)

    p2.send_report.assert_called_once_with(event)
    assert result.attempted == 2
    assert result.succeeded == 1
    assert result.failed == 1


def test_report_timeout_log_mentions_editable_env(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _fake_provider("mail")
    provider.send_report.side_effect = TimeoutError("timed out")
    d = _dispatcher_with_providers(provider)
    with caplog.at_level(logging.ERROR):
        d.notify_report(_make_report_event())
    assert any("DK_NOTIFICATION_TIMEOUT" in record.message for record in caplog.records)


def test_report_no_providers_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    config = ResolvedGlobalNotificationProvidersConfig()
    log = logging.getLogger("test_report_no_providers")
    d = NotificationDispatcher(config, log)
    with caplog.at_level(logging.WARNING):
        result = d.notify_report(_make_report_event())
    assert result.attempted == 0
    assert any("no providers" in record.message for record in caplog.records)
