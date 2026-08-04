from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.models.resolved_config import ResolvedPushoverNotificationConfig
from src.notifications.events import (
    NotificationEvent,
    NotificationReportEvent,
    NotificationReportRunSummary,
)
from src.notifications.formatting import format_report_push_title
from src.notifications.providers.pushover import _PUSHOVER_API_URL, PushoverProvider


def _pushover_config(
    token_env: str = "PUSHOVER_TOKEN",
    user_key_env: str = "PUSHOVER_USER_KEY",
    priority: int = 0,
    sound: str | None = None,
    device: str | None = None,
) -> ResolvedPushoverNotificationConfig:
    return ResolvedPushoverNotificationConfig(
        token_env=token_env,
        user_key_env=user_key_env,
        priority=priority,
        sound=sound,
        device=device,
    )


def _make_event(
    status: str = "success",
    dry_run: bool = False,
    error: str | None = None,
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
    )


def _make_report_event() -> NotificationReportEvent:
    run = NotificationReportRunSummary(
        origin="scheduler",
        job="homeserver",
        target="homeserver.backup.local",
        status="failed",
        started_at=datetime(2024, 1, 15, 2, 0, 0),
        finished_at=datetime(2024, 1, 15, 2, 0, 45),
        duration_seconds=45,
        dry_run=False,
        error="Repository unavailable",
    )
    return NotificationReportEvent(
        window_start=datetime(2024, 1, 15, 2, 0, 0),
        window_end=datetime(2024, 1, 15, 3, 0, 0),
        generated_at=datetime(2024, 1, 15, 3, 0, 5),
        status_counts={"failed": 1},
        runs=(run,),
    )


def _mock_client(status_code: int = 200) -> MagicMock:
    """Return a context-manager mock httpx.Client with a configurable response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=response,
        )
    else:
        response.raise_for_status.return_value = None

    client_instance = MagicMock()
    client_instance.__enter__ = MagicMock(return_value=client_instance)
    client_instance.__exit__ = MagicMock(return_value=False)
    client_instance.post.return_value = response
    return client_instance


def test_provider_name() -> None:
    provider = PushoverProvider(_pushover_config(), 5)
    assert provider.name == "pushover"


def test_sends_required_payload_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "mytoken")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "myuserkey")

    event = _make_event()
    provider = PushoverProvider(_pushover_config(), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(event)

    mock_client.post.assert_called_once()
    _, kwargs = mock_client.post.call_args
    payload = kwargs["data"]

    assert payload["token"] == "mytoken"
    assert payload["user"] == "myuserkey"
    assert "title" in payload
    assert "message" in payload
    assert payload["html"] == 1
    assert "<b>Status:</b>" in payload["message"]
    assert "priority" in payload


def test_posts_to_correct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(_make_event())

    args, _ = mock_client.post.call_args
    assert args[0] == _PUSHOVER_API_URL


def test_priority_is_included_in_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(priority=-1), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(_make_event())

    _, kwargs = mock_client.post.call_args
    assert kwargs["data"]["priority"] == -1


def test_send_report_uses_report_title_and_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    event = _make_report_event()
    provider = PushoverProvider(_pushover_config(priority=1), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send_report(event)

    _, kwargs = mock_client.post.call_args
    payload = kwargs["data"]
    assert payload["title"] == format_report_push_title(event)
    assert "Needs attention" in payload["title"]
    assert payload["html"] == 1
    assert "<b>Window:</b>" in payload["message"]
    assert "homeserver.backup.local" in payload["message"]
    assert "Repository unavailable" in payload["message"]
    assert payload["priority"] == 1


def test_omits_sound_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(sound=None), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(_make_event())

    _, kwargs = mock_client.post.call_args
    assert "sound" not in kwargs["data"]


def test_omits_device_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(device=None), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(_make_event())

    _, kwargs = mock_client.post.call_args
    assert "device" not in kwargs["data"]


def test_includes_sound_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(sound="magic"), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(_make_event())

    _, kwargs = mock_client.post.call_args
    assert kwargs["data"]["sound"] == "magic"


def test_includes_device_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(device="iphone"), 5)
    mock_client = _mock_client()

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        provider.send(_make_event())

    _, kwargs = mock_client.post.call_args
    assert kwargs["data"]["device"] == "iphone"


def test_timeout_passed_to_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(), 7)
    mock_client = _mock_client()

    with patch(
        "src.notifications.providers.pushover.httpx.Client", return_value=mock_client
    ) as mock_cls:
        provider.send(_make_event())

    mock_cls.assert_called_once_with(timeout=7)


@pytest.mark.parametrize("status_code", [400, 429, 500])
def test_non_2xx_response_raises(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(), 5)
    mock_client = _mock_client(status_code=status_code)

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            provider.send(_make_event())


def test_raises_when_token_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(), 5)

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=_mock_client()):
        with pytest.raises(ValueError, match="PUSHOVER_TOKEN"):
            provider.send(_make_event())


def test_raises_when_user_key_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)

    provider = PushoverProvider(_pushover_config(), 5)

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=_mock_client()):
        with pytest.raises(ValueError, match="PUSHOVER_USER_KEY"):
            provider.send(_make_event())


def test_token_not_in_exception_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure token value is never leaked in error messages."""
    monkeypatch.setenv("PUSHOVER_TOKEN", "supersecrettoken")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(), 5)
    mock_client = _mock_client(status_code=401)

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            provider.send(_make_event())

    assert "supersecrettoken" not in str(exc_info.value)


def test_network_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "ukey")

    provider = PushoverProvider(_pushover_config(), 5)
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")

    with patch("src.notifications.providers.pushover.httpx.Client", return_value=mock_client):
        with pytest.raises(httpx.ConnectError):
            provider.send(_make_event())
