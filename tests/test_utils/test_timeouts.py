import logging

import pytest

from src.utils.timeouts import env_timeout


def test_env_timeout_uses_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DK_TEST_TIMEOUT", "17")
    assert env_timeout("DK_TEST_TIMEOUT", 10, logging.getLogger("test")) == 17


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_env_timeout_invalid_value_logs_fallback(
    value: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DK_TEST_TIMEOUT", value)
    with caplog.at_level(logging.WARNING):
        timeout = env_timeout("DK_TEST_TIMEOUT", 10, logging.getLogger("test"))
    assert timeout == 10
    assert "DK_TEST_TIMEOUT" in caplog.text
