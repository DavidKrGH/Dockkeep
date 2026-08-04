import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.models.resolved_config import ResolvedMailNotificationConfig
from src.notifications.events import (
    NotificationEvent,
    NotificationReportEvent,
    NotificationReportRunSummary,
)
from src.notifications.formatting import format_report_subject, format_subject
from src.notifications.providers.mail import MailProvider


def _mail_config(
    connection_security: str = "starttls",
    username_env: str | None = None,
    password_env: str | None = None,
    to: list[str] | None = None,
) -> ResolvedMailNotificationConfig:
    return ResolvedMailNotificationConfig(
        host="smtp.example.test",
        port=1025,
        connection_security=connection_security,  # type: ignore[arg-type]
        username_env=username_env,
        password_env=password_env,
        from_addr="dk@example.test",
        to=to or ["admin@example.test"],
    )


def _make_event(
    status: str = "success",
    dry_run: bool = False,
    error: str | None = None,
    log_path: Path | None = None,
) -> NotificationEvent:
    return NotificationEvent(
        job_name="homeserver",
        task_type="backup",
        task_name="local",
        status=status,  # type: ignore[arg-type]
        started_at=datetime(2024, 1, 15, 2, 0, 0),
        finished_at=datetime(2024, 1, 15, 2, 0, 45),
        dry_run=dry_run,
        message="Backup completed",
        error=error,
        log_path=log_path,
    )


def _make_report_event() -> NotificationReportEvent:
    run = NotificationReportRunSummary(
        origin="manual",
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


def test_provider_name() -> None:
    provider = MailProvider(_mail_config(), 10)
    assert provider.name == "mail"


def test_send_plain_smtp_no_auth() -> None:
    event = _make_event()
    cfg = _mail_config(connection_security="none")
    provider = MailProvider(cfg, 5)

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    mock_smtp_cls.assert_called_once_with("smtp.example.test", 1025, timeout=5)
    mock_smtp_instance.starttls.assert_not_called()
    mock_smtp_instance.login.assert_not_called()
    mock_smtp_instance.send_message.assert_called_once()


def test_send_calls_starttls_when_configured() -> None:
    event = _make_event()
    cfg = _mail_config(connection_security="starttls")
    provider = MailProvider(cfg, 5)

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    mock_smtp_instance.starttls.assert_called_once()
    mock_smtp_instance.send_message.assert_called_once()


def test_send_uses_smtp_ssl_when_configured() -> None:
    event = _make_event()
    cfg = _mail_config(connection_security="ssl")
    provider = MailProvider(cfg, 5)

    mock_smtp_ssl_instance = MagicMock()
    mock_smtp_ssl_cls = MagicMock(return_value=mock_smtp_ssl_instance)
    mock_smtp_ssl_instance.__enter__ = MagicMock(return_value=mock_smtp_ssl_instance)
    mock_smtp_ssl_instance.__exit__ = MagicMock(return_value=False)

    with (
        patch("src.notifications.providers.mail.smtplib.SMTP_SSL", mock_smtp_ssl_cls),
        patch("src.notifications.providers.mail.smtplib.SMTP") as mock_plain,
    ):
        provider.send(event)

    mock_smtp_ssl_cls.assert_called_once_with("smtp.example.test", 1025, timeout=5)
    mock_plain.assert_not_called()
    mock_smtp_ssl_instance.send_message.assert_called_once()
    mock_smtp_ssl_instance.starttls.assert_not_called()


def test_send_authenticates_when_credentials_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_USER", "testuser")
    monkeypatch.setenv("SMTP_PASS", "secret")

    event = _make_event()
    cfg = _mail_config(
        connection_security="none", username_env="SMTP_USER", password_env="SMTP_PASS"
    )
    provider = MailProvider(cfg, 5)

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    mock_smtp_instance.starttls.assert_not_called()
    mock_smtp_instance.login.assert_called_once_with("testuser", "secret")


def test_send_authenticates_via_ssl_when_credentials_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMTP_USER", "ssluser")
    monkeypatch.setenv("SMTP_PASS", "sslsecret")

    event = _make_event()
    cfg = _mail_config(
        connection_security="ssl", username_env="SMTP_USER", password_env="SMTP_PASS"
    )
    provider = MailProvider(cfg, 5)

    mock_smtp_ssl_instance = MagicMock()
    mock_smtp_ssl_cls = MagicMock(return_value=mock_smtp_ssl_instance)
    mock_smtp_ssl_instance.__enter__ = MagicMock(return_value=mock_smtp_ssl_instance)
    mock_smtp_ssl_instance.__exit__ = MagicMock(return_value=False)

    with patch("src.notifications.providers.mail.smtplib.SMTP_SSL", mock_smtp_ssl_cls):
        provider.send(event)

    mock_smtp_ssl_instance.login.assert_called_once_with("ssluser", "sslsecret")


def test_send_raises_when_credential_env_var_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)

    event = _make_event()
    cfg = _mail_config(username_env="SMTP_USER", password_env="SMTP_PASS")
    provider = MailProvider(cfg, 5)

    with pytest.raises(ValueError, match="SMTP_USER"):
        provider.send(event)


def test_sent_message_has_correct_subject_and_recipients() -> None:
    event = _make_event(status="failure")
    cfg = _mail_config(to=["alice@example.test", "bob@example.test"])
    provider = MailProvider(cfg, 5)

    sent_messages: list[EmailMessage] = []

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = lambda msg: sent_messages.append(msg)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg["Subject"] == format_subject(event)
    assert msg["From"] == "dk@example.test"
    assert "alice@example.test" in msg["To"]
    assert "bob@example.test" in msg["To"]


def test_sent_message_body_matches_format_text_body() -> None:
    event = _make_event(status="success", error=None)
    cfg = _mail_config()
    provider = MailProvider(cfg, 5)

    sent_messages: list[EmailMessage] = []

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = lambda msg: sent_messages.append(msg)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    body = sent_messages[0].get_body()
    assert body is not None
    payload = body.get_payload(decode=True)
    assert payload is not None
    body_text = payload.decode()
    assert "Success" in body_text
    assert "homeserver" in body_text


def test_sent_message_includes_html_alternative() -> None:
    event = _make_event(status="failure", error="Repository unavailable")
    cfg = _mail_config()
    provider = MailProvider(cfg, 5)

    sent_messages: list[EmailMessage] = []

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = lambda msg: sent_messages.append(msg)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    html_body = sent_messages[0].get_body(preferencelist=("html",))
    assert html_body is not None
    payload = html_body.get_payload(decode=True)
    assert payload is not None
    html_text = payload.decode()
    assert "<table" in html_text
    assert "Repository unavailable" in html_text


def test_sent_message_includes_dry_run_marker() -> None:
    event = _make_event(status="success", dry_run=True)
    cfg = _mail_config()
    provider = MailProvider(cfg, 5)

    sent_messages: list[EmailMessage] = []

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = lambda msg: sent_messages.append(msg)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send(event)

    assert "[DRY RUN]" in sent_messages[0]["Subject"]


def test_send_report_uses_report_subject_and_bodies() -> None:
    event = _make_report_event()
    cfg = _mail_config()
    provider = MailProvider(cfg, 5)

    sent_messages: list[EmailMessage] = []

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = lambda msg: sent_messages.append(msg)

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        provider.send_report(event)

    assert sent_messages[0]["Subject"] == format_report_subject(event)
    text_body = sent_messages[0].get_body(preferencelist=("plain",))
    html_body = sent_messages[0].get_body(preferencelist=("html",))
    assert text_body is not None
    assert html_body is not None
    text_payload = text_body.get_payload(decode=True)
    html_payload = html_body.get_payload(decode=True)
    assert text_payload is not None
    assert html_payload is not None
    assert "Run report" in text_payload.decode()
    assert "homeserver" in html_payload.decode()


def test_smtp_exception_propagates() -> None:
    event = _make_event()
    cfg = _mail_config()
    provider = MailProvider(cfg, 5)

    mock_smtp_instance = MagicMock()
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
    mock_smtp_instance.__exit__ = MagicMock(return_value=False)
    mock_smtp_instance.send_message.side_effect = smtplib.SMTPException("Delivery failed")

    with patch("src.notifications.providers.mail.smtplib.SMTP", mock_smtp_cls):
        with pytest.raises(smtplib.SMTPException, match="Delivery failed"):
            provider.send(event)


def test_connection_error_propagates() -> None:
    event = _make_event()
    cfg = _mail_config()
    provider = MailProvider(cfg, 5)

    with patch(
        "src.notifications.providers.mail.smtplib.SMTP",
        side_effect=ConnectionRefusedError("Connection refused"),
    ):
        with pytest.raises(ConnectionRefusedError):
            provider.send(event)


def test_resolve_credentials_raises_when_username_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError wenn username_env konfiguriert aber Env-Var fehlt."""
    monkeypatch.delenv("SMTP_USER_MISSING", raising=False)
    monkeypatch.setenv("SMTP_PASS_OK", "password")
    cfg = _mail_config(username_env="SMTP_USER_MISSING", password_env="SMTP_PASS_OK")
    provider = MailProvider(cfg, 5)
    with pytest.raises(ValueError, match="SMTP_USER_MISSING"):
        provider._resolve_credentials()


def test_resolve_credentials_raises_when_username_env_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError wenn username_env auf leere Env-Var zeigt."""
    monkeypatch.setenv("SMTP_USER_EMPTY", "")
    monkeypatch.setenv("SMTP_PASS_OK", "password")
    cfg = _mail_config(username_env="SMTP_USER_EMPTY", password_env="SMTP_PASS_OK")
    provider = MailProvider(cfg, 5)
    with pytest.raises(ValueError, match="SMTP_USER_EMPTY"):
        provider._resolve_credentials()


def test_resolve_credentials_raises_when_password_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError wenn password_env konfiguriert aber Env-Var fehlt."""
    monkeypatch.setenv("SMTP_USER_OK", "user@example.com")
    monkeypatch.delenv("SMTP_PASS_MISSING", raising=False)
    cfg = _mail_config(username_env="SMTP_USER_OK", password_env="SMTP_PASS_MISSING")
    provider = MailProvider(cfg, 5)
    with pytest.raises(ValueError, match="SMTP_PASS_MISSING"):
        provider._resolve_credentials()


def test_resolve_credentials_succeeds_with_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beide Env-Vars gesetzt → Tuple wird korrekt zurückgegeben."""
    monkeypatch.setenv("SMTP_USER_OK", "myuser")
    monkeypatch.setenv("SMTP_PASS_OK", "mypass")
    cfg = _mail_config(username_env="SMTP_USER_OK", password_env="SMTP_PASS_OK")
    provider = MailProvider(cfg, 5)
    username, password = provider._resolve_credentials()
    assert username == "myuser"
    assert password == "mypass"
