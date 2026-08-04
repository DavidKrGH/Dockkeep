"""Integration smoke tests for GUI runtime wiring."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.gui.app import create_app


def _write_minimal_config(path: Path) -> None:
    path.write_text(
        """
[global]
[global.backup]
password = "secret"

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
schedule = "0 2 * * *"

[jobs.demo.workflow.daily]
steps = ["backup.local"]
""".strip(),
        encoding="utf-8",
    )


def test_gui_app_exposes_html_without_router_scheduler_hook(
    tmp_path: Path,
) -> None:
    """The GUI process is one FastAPI app with HTML routes and owner-managed scheduling."""
    config_path = tmp_path / "config.toml"
    _write_minimal_config(config_path)
    manager = MagicMock()
    manager.lock_dir = tmp_path

    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(config_path, appdata_dir=tmp_path)
        assert not hasattr(app.state, "start_scheduler")

        with TestClient(app) as client:
            dashboard = client.get("/")

    assert dashboard.status_code == 200
    manager.start_background.assert_called_once_with()
    manager.stop.assert_called_once_with()
