import asyncio
from pathlib import Path

from src.services.factory import create_services


def test_create_services_builds_shared_runtime_services(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo]
[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
""".strip(),
        encoding="utf-8",
    )

    services = create_services(config_path, appdata_dir=tmp_path)
    services.backup_artifact_store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "time": "2024-01-01T12:00:00+00:00"}],
        stats={
            "mode": "raw-data",
            "total_size_bytes": 1024,
            "total_file_count": 5,
            "snapshots_count": 1,
        },
        observed_at="2024-01-01T12:01:00+00:00",
    )

    assert services.run_service is not None
    assert services.run_manager is not None
    assert services.run_history_service is not None
    assert services.restore_service is not None
    assert services.restore_registry is not None
    assert services.restore_service._run_manager is services.run_manager
    assert services.run_service._run_control_client is not None
    assert services.run_service._run_control_client._socket_path == tmp_path / "run-control.sock"
    dashboard_view = asyncio.run(services.dashboard_service.get_dashboard_view())
    assert dashboard_view["config_error"] is None
    assert dashboard_view["overview"]["total_repo_size_bytes"] == 1024
    assert dashboard_view["jobs"][0]["backups"][0]["total_size_bytes"] == 1024
    assert dashboard_view["chart_data"]["growth"] == {
        "all_labels": ["2024-01-01 12:01"],
        "datasets": [{"label": "demo.local", "data": [1024]}],
    }


def test_dashboard_view_uses_template_ready_viewmodels(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo.rclone.offsite]
source = "/repo"
target = "remote:secret-value"
sync_delete = true

[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data/secret-value"]
password = "secret-value"
retention = true
cleanup = true
keep_last = 3
""".strip(),
        encoding="utf-8",
    )

    services = create_services(config_path, appdata_dir=tmp_path)
    view = asyncio.run(services.dashboard_service.get_dashboard_view())
    job = view["jobs"][0]

    assert "config" not in job
    assert "explicit" not in job
    assert job["backups"][0]["name"] == "local"
    assert job["backups"][0]["repository"] == "/repo/secret-value"
    assert job["backups"][0]["edit_url"] == "/config/jobs/demo/backups/local"
    assert job["rclone"][0]["target"] == "remote:secret-value"
    assert job["rclone"][0]["edit_url"] == "/config/jobs/demo/rclone/offsite"
