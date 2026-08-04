import asyncio
from pathlib import Path

import pytest

from src.services.config import ConfigService
from src.services.errors import NotFoundServiceError


def test_config_service_editor_returns_raw_content(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo]
[global]
notify_on_success = true

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )

    result = asyncio.run(ConfigService(config_path).read_raw())

    assert result["valid"] is True
    assert "secret-value" in str(result["content"])
    assert result["warnings"]


def test_config_service_save_raw_persists_valid_content(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    content = """
[jobs.demo]
[global]
notify_on_success = true

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
password = "secret-value"
""".strip()

    result = asyncio.run(ConfigService(config_path).save_raw(content))

    assert result["valid"] is True
    assert result["error"] is None
    assert result["warnings"]
    assert config_path.read_text(encoding="utf-8") == content


def test_config_service_save_raw_does_not_persist_invalid_content(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original = """
[jobs.demo]
[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
password = "secret-value"
""".strip()
    config_path.write_text(original, encoding="utf-8")

    result = asyncio.run(ConfigService(config_path).save_raw("[jobs.demo"))

    assert result["valid"] is False
    assert result["error"] is not None
    assert result["warnings"] == []
    assert config_path.read_text(encoding="utf-8") == original


def test_validate_content_with_nonexistent_password_file_returns_invalid(tmp_path: Path) -> None:
    missing_password_file = tmp_path / "missing-password"
    content = f"""
[global.backup]
password_file = "{missing_password_file}"

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
""".strip()

    result = ConfigService(tmp_path / "config.toml").validate_content(content)

    assert result["valid"] is False
    assert result["error"] is not None
    assert "password_file" in str(result["error"])
    assert "does not exist or is not a file" in str(result["error"])


def test_validate_content_runs_resolved_validation(tmp_path: Path) -> None:
    content = """
[jobs.demo]
[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
retention = true
""".strip()

    result = ConfigService(tmp_path / "config.toml").validate_content(content)

    assert result["valid"] is False
    assert result["error"] is not None
    assert result["error"]["code"] == "value_error"
    assert "retention=true requires at least one keep_* field" in str(result["error"])


def test_validate_content_accepts_keep_within_granular_policy(tmp_path: Path) -> None:
    content = """
[jobs.demo]
[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
retention = true
keep_within_daily = "7d"
""".strip()

    result = ConfigService(tmp_path / "config.toml").validate_content(content)

    assert result["valid"] is True


def _effective_field(task: dict, group_label: str, field_label: str) -> dict:
    group = next(g for g in task["groups"] if g["label"] == group_label)
    return next(f for f in group["fields"] if f["label"] == field_label)


def _effective_field_value(task: dict, group_label: str, field_label: str) -> str:
    return _effective_field(task, group_label, field_label)["value"]


_INHERITANCE_CONFIG = """
[global]
hook_timeout = 100

[global.backup]
password = "s3cret-value"
backup_timeout = 60
keep_last = 5
extra_restic_backup_args = ["--verbose"]

[jobs.demo]
hook_timeout = 200

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
keep_last = 3
extra_restic_backup_args = []
""".strip()


def test_get_effective_job_view_applies_three_level_inheritance(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_INHERITANCE_CONFIG, encoding="utf-8")

    view = asyncio.run(ConfigService(config_path).get_effective_job_view("demo"))

    backup = next(item for item in view["backups"] if item["name"] == "local")
    assert _effective_field_value(backup, "Retention", "Keep last N") == "3"
    assert _effective_field_value(backup, "Timeouts", "Backup timeout (seconds)") == "60"
    assert _effective_field_value(backup, "Timeouts", "Hook timeout (seconds)") == "200"
    assert (
        _effective_field_value(backup, "Backend options (restic)", "Additional backup arguments")
        == "empty"
    )
    assert _effective_field_value(backup, "Credentials", "Password source") == "set in config"
    assert "s3cret-value" not in repr(view)


def test_get_effective_job_view_reports_field_sources(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_INHERITANCE_CONFIG, encoding="utf-8")

    view = asyncio.run(ConfigService(config_path).get_effective_job_view("demo"))

    backup = next(item for item in view["backups"] if item["name"] == "local")
    assert _effective_field(backup, "Retention", "Keep last N")["source"] == "task"
    assert _effective_field(backup, "Timeouts", "Backup timeout (seconds)")["source"] == "global"
    assert _effective_field(backup, "Timeouts", "Hook timeout (seconds)")["source"] == "job"
    assert (
        _effective_field(backup, "Backend options (restic)", "Additional backup arguments")[
            "source"
        ]
        == "task"
    )
    assert _effective_field(backup, "Credentials", "Password source")["source"] == "global"
    assert _effective_field(backup, "Execution", "Run retention automatically")["source"] == (
        "default"
    )
    retention = next(g for g in backup["groups"] if g["label"] == "Retention")
    assert all(f["label"] != "Daily snapshots" for f in retention["fields"])


def test_get_effective_job_view_summary_warnings_and_steps(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
schedule = "0 3 * * *"
password = "pw"

[jobs.demo.rclone.mirror]
source = "/repo"
target = "remote:bucket/repo"

[jobs.demo.workflow.nightly]
steps = ["backup.local", "rclone.mirror"]
""".strip(),
        encoding="utf-8",
    )

    view = asyncio.run(ConfigService(config_path).get_effective_job_view("demo"))

    backup = next(item for item in view["backups"] if item["name"] == "local")
    assert any("No backup inputs" in warning for warning in backup["warnings"])
    assert backup["main_group"] is None
    schedule_field = next(f for f in backup["summary_fields"] if f["label"] == "Cron schedule")
    assert schedule_field["value"] == "0 3 * * *"
    assert schedule_field["note"].startswith("next run ")

    workflow = next(item for item in view["workflows"] if item["name"] == "nightly")
    assert workflow["steps"] == [
        {"label": "backup.local", "kind": "backup", "target": "local"},
        {"label": "rclone.mirror", "kind": "rclone", "target": "mirror"},
    ]
    schedule_field = next(f for f in workflow["summary_fields"] if f["label"] == "Cron schedule")
    assert schedule_field["value"] == "manual only"

    rclone = next(item for item in view["rclone_tasks"] if item["name"] == "mirror")
    assert rclone["warnings"] == []
    assert all(g["label"] != "Filters" for g in rclone["groups"])


def test_get_effective_job_view_unknown_job_raises_not_found(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo]
[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
password = "secret"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(NotFoundServiceError):
        asyncio.run(ConfigService(config_path).get_effective_job_view("missing"))
