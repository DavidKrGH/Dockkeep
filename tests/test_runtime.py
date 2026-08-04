"""Tests for the runtime-only CLI entrypoint."""

from unittest.mock import patch

import pytest

from src.runtime import _build_parser, main


class TestRuntimeParser:
    def test_help_shows_runtime_commands_only(self) -> None:
        help_text = _build_parser().format_help()

        assert "gui" in help_text
        assert "scheduler" in help_text
        assert "jobs" not in help_text
        assert "runs" not in help_text
        assert "logs" not in help_text

    def test_gui_dispatched(self) -> None:
        with (
            patch("src.runtime.cmd_gui", return_value=0) as mock_cmd,
            patch("sys.argv", ["dk-runtime", "gui", "--host", "127.0.0.1", "--port", "8081"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        called_args, config_path = mock_cmd.call_args.args
        assert called_args.host == "127.0.0.1"
        assert called_args.port == 8081
        assert str(config_path).endswith("/config/config.toml")

    def test_scheduler_dispatched(self) -> None:
        with (
            patch("src.runtime.cmd_scheduler", return_value=1) as mock_cmd,
            patch("sys.argv", ["dk-runtime", "scheduler"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        mock_cmd.assert_called_once()
