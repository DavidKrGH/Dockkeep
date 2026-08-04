"""SMTP email notification provider."""

import os
import smtplib
from email.message import EmailMessage

from src.models.config import NotificationEventKind
from src.models.resolved_config import ResolvedMailNotificationConfig

from ..events import NotificationEvent, NotificationReportEvent
from ..formatting import (
    format_html_body,
    format_report_html_body,
    format_report_subject,
    format_report_text_body,
    format_subject,
    format_text_body,
)


class MailProvider:
    """Sends plain-text email notifications via SMTP.

    Credentials are resolved from environment variables at send time so they
    are never stored in the object or logged accidentally.
    """

    name = "mail"

    def __init__(self, config: ResolvedMailNotificationConfig, timeout: int) -> None:
        self._config = config
        self._timeout = timeout
        self.events: list[NotificationEventKind] | None = config.events

    def send(self, event: NotificationEvent) -> None:
        """Build and deliver an email for the given event.

        Args:
            event: The notification event to deliver.

        Raises:
            ValueError: When a required credential env var is missing.
            smtplib.SMTPException: On SMTP protocol or delivery errors.
            OSError: On connection failures.
        """
        cfg = self._config
        msg = EmailMessage()
        msg["Subject"] = format_subject(event)
        msg["From"] = cfg.from_addr
        msg["To"] = ", ".join(cfg.to)
        msg.set_content(format_text_body(event))
        msg.add_alternative(format_html_body(event), subtype="html")

        self._send_message(msg)

    def send_report(self, event: NotificationReportEvent) -> None:
        """Build and deliver an email for the given report event."""
        cfg = self._config
        msg = EmailMessage()
        msg["Subject"] = format_report_subject(event)
        msg["From"] = cfg.from_addr
        msg["To"] = ", ".join(cfg.to)
        msg.set_content(format_report_text_body(event))
        msg.add_alternative(format_report_html_body(event), subtype="html")

        self._send_message(msg)

    def _send_message(self, msg: EmailMessage) -> None:
        cfg = self._config
        username, password = self._resolve_credentials()

        if cfg.connection_security == "ssl":
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=self._timeout) as smtp:
                if username is not None and password is not None:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=self._timeout) as smtp:
                if cfg.connection_security == "starttls":
                    smtp.starttls()
                if username is not None and password is not None:
                    smtp.login(username, password)
                smtp.send_message(msg)

    def _resolve_credentials(self) -> tuple[str | None, str | None]:
        """Read SMTP credentials from environment variables at send time.

        Returns:
            Tuple of (username, password), both None when unconfigured.

        Raises:
            ValueError: When only one of username_env/password_env is set but the
                corresponding env var is missing or empty.
        """
        cfg = self._config
        if cfg.username_env is None and cfg.password_env is None:
            return None, None

        username: str | None = None
        password: str | None = None

        if cfg.username_env is not None:
            username = os.environ.get(cfg.username_env) or None
            if username is None:
                raise ValueError(
                    f"SMTP credential env var '{cfg.username_env}' is not set or empty"
                )

        if cfg.password_env is not None:
            password = os.environ.get(cfg.password_env) or None
            if password is None:
                raise ValueError(
                    f"SMTP credential env var '{cfg.password_env}' is not set or empty"
                )

        return username, password
