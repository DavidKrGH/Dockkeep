import asyncio
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.services.config import ConfigService  # noqa: I001
from src.services.config_editor import (
    ConfigEditorService,
    _global_backup_parent,
    _global_cross_cutting_parent,
)
from src.services.factory import ServiceContainer, create_services


class _SyncConfigEditorService:
    """Run async public service calls synchronously for existing behavior tests."""

    _ASYNC_METHODS = {
        "delete_backup",
        "delete_job",
        "delete_rclone",
        "delete_workflow",
        "get_backup_defaults_form",
        "get_backup_form",
        "get_global_form",
        "get_job_form",
        "get_overview",
        "get_rclone_defaults_form",
        "get_rclone_form",
        "get_workflow_form",
        "save_backup_defaults_form",
        "save_backup_form",
        "save_global_form",
        "save_job_form",
        "save_rclone_defaults_form",
        "save_rclone_form",
        "save_workflow_form",
        "test_notification_provider",
        "test_notification_report",
    }

    def __init__(self, service: ConfigEditorService) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._service, name)
        if name not in self._ASYNC_METHODS:
            return attr

        def run(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(attr(*args, **kwargs))

        return run


def _make_service(path: Path) -> _SyncConfigEditorService:
    return _SyncConfigEditorService(ConfigEditorService(ConfigService(path)))


def _write(path: Path, content: str) -> None:
    path.write_text(content.strip(), encoding="utf-8")


def _parsed(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


_MINIMAL = """
[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
"""

_FULL = """
[global.backup]
password = "secret"

[jobs.demo]
hook_timeout = 600

[jobs.demo.backup]
retention = false
exclude = ["*.tmp"]

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
keep_last = 3
keep_hourly = 2
schedule = "0 2 * * *"

[jobs.demo.backup.remote]
repository = "rclone:s3:bucket"
sources = ["/data"]

[jobs.demo.rclone]
transfers = 4

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket/backup"
sync_delete = true

[jobs.demo.workflow.nightly]
schedule = "0 3 * * *"
steps = ["backup.local", "backup.remote"]
"""


def test_get_overview_empty_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, '[jobs.x]\n\n[jobs.x.backup.p]\nrepository = "/r"\npassword = "s"')
    svc = _make_service(config_path)
    result = svc.get_overview()
    assert result["error"] is None
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["name"] == "x"


def test_get_overview_returns_all_jobs(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_overview()

    assert result["error"] is None
    assert result["active_config_error"] is None
    jobs = {j["name"]: j for j in result["jobs"]}
    assert "demo" in jobs
    demo = jobs["demo"]
    assert set(demo["backup_names"]) == {"local", "remote"}
    assert demo["workflow_names"] == ["nightly"]
    assert demo["rclone_names"] == ["offsite"]


def test_get_overview_exposes_notification_warning_without_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global]
notify_on_error = true

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.get_overview()

    assert result["error"] is None
    assert result["warnings"]
    assert "Notifications are enabled" in result["warnings"][0]
    assert "no provider is configured" in result["warnings"][0]


def test_get_overview_exposes_resource_details(tmp_path: Path) -> None:
    """The overview carries per-resource display details (source/target/steps)."""
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    demo = {j["name"]: j for j in svc.get_overview()["jobs"]}["demo"]

    assert demo["backup_details"]["remote"] == {
        "repository": "rclone:s3:bucket",
        "sources": ["/data"],
        "source_files": [],
    }
    assert demo["workflow_details"]["nightly"]["steps"] == ["backup.local", "backup.remote"]
    assert demo["rclone_details"]["offsite"] == {
        "source": "/repo",
        "target": "s3:bucket/backup",
    }


def test_get_overview_ignores_container_default_keys_as_tasks(tmp_path: Path) -> None:
    """Scalar defaults in the container tables are not listed as tasks."""
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    demo = {j["name"]: j for j in svc.get_overview()["jobs"]}["demo"]

    assert "retention" not in demo["backup_names"]
    assert "exclude" not in demo["backup_names"]
    assert "transfers" not in demo["rclone_names"]


def test_get_overview_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    # Missing required 'repository' key makes config Pydantic-invalid but
    # get_overview reads raw TOML so it still returns the structural overview.
    _write(config_path, "[jobs.demo]\n")
    svc = _make_service(config_path)

    result = svc.get_overview()

    assert "demo" in [j["name"] for j in result["jobs"]]
    assert result["error"] is None


def test_get_overview_reports_invalid_active_config(tmp_path: Path) -> None:
    """A parseable but pipeline-invalid config keeps the overview usable
    while reporting that the active config is not runnable."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global]
log_level = "LOUD"

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.get_overview()

    assert "demo" in [j["name"] for j in result["jobs"]]
    assert result["active_config_error"]
    assert "log_level" in result["active_config_error"]


def test_get_overview_marks_invalid_names_for_recovery_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs."bad/name"]

[jobs."bad/name".backup."bad?backup"]
repository = "/repo"
password = "secret"
""",
    )
    svc = _make_service(config_path)

    result = svc.get_overview()

    job = result["jobs"][0]
    assert job["name"] == "bad/name"
    assert job["name_valid"] is False
    assert job["backup_name_valid"]["bad?backup"] is False


def test_save_reports_pre_existing_validation_error_as_unrelated(tmp_path: Path) -> None:
    """A save blocked by an error that existed before the edit points to the
    raw editor instead of blaming the submitted change."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]

[jobs.demo.rclone.mirror]
source = "/data"
target = "/local/path"
""",
    )
    svc = _make_service(config_path)

    result = svc.save_global_form({})

    assert result["saved"] is False
    assert "independently of your change" in result["error"]
    assert "raw editor" in result["error"]
    assert "target" in result["error"]


def test_save_reports_self_caused_validation_error_directly(tmp_path: Path) -> None:
    """A validation failure introduced by the edit itself keeps the plain
    error message without the pre-existing-error hint."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name=None,
        form={"name": "mirror", "source": "/data", "target": "/local/path"},
    )

    assert result["saved"] is False
    assert "target" in result["error"]
    assert "independently of your change" not in result["error"]


def test_get_overview_missing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"
    svc = _make_service(config_path)

    result = svc.get_overview()

    assert result["error"] is not None
    assert result["jobs"] == []


def test_get_job_form_new_job(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.get_job_form(None)

    assert result["error"] is None
    assert result["name"] == ""
    assert result["groups"]


def test_get_job_form_existing_job(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_job_form("demo")

    assert result["error"] is None
    assert result["name"] == "demo"
    assert [group["title"] for group in result["groups"]] == [
        "Hooks",
        "Notifications",
    ]
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}
    assert fields["hook_timeout"]["value"] == "600"


def test_get_job_form_missing_job(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.get_job_form("nonexistent")

    assert result["error"] is not None
    assert "nonexistent" in result["error"]


def test_job_form_shows_inherited_notification_hint_from_global(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global]
notify_on_error = true

[jobs.demo]
""",
    )
    svc = _make_service(config_path)

    result = svc.get_job_form("demo")
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}

    assert fields["notify_on_error"]["hint"] == "true"
    assert fields["notify_on_error"]["inherited"] is True


def test_save_job_creates_new_job_with_cross_cutting(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_job_form(
        job_name=None,
        form={"name": "newjob", "hook_timeout": "300", "pre_hooks": "/scripts/pre.sh"},
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["newjob"]["hook_timeout"] == 300
    assert parsed["jobs"]["newjob"]["pre_hooks"] == ["/scripts/pre.sh"]


def test_save_first_job_creates_missing_config_file(tmp_path: Path) -> None:
    """The very first job must be saveable when no config file exists yet."""
    config_path = tmp_path / "config.toml"
    assert not config_path.exists()
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name=None, form={"name": "firstjob"})

    assert result["saved"] is True
    assert config_path.exists()
    assert "firstjob" in _parsed(config_path)["jobs"]


def test_save_job_only_touches_cross_cutting_keys(tmp_path: Path) -> None:
    """Saving the job form must not touch backup/rclone/workflow subtables."""
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name="demo", form={"name": "demo", "hook_timeout": "900"})

    assert result["saved"] is True
    parsed = _parsed(config_path)
    job = parsed["jobs"]["demo"]
    assert job["hook_timeout"] == 900
    assert job["backup"]["local"]["repository"] == "/repo"
    assert job["backup"]["retention"] is False
    assert job["rclone"]["transfers"] == 4
    assert job["workflow"]["nightly"]["steps"] == ["backup.local", "backup.remote"]


def test_save_job_preserves_backups_on_update(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name="demo", form={"name": "demo", "hook_timeout": "1"})

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert "remote" in parsed["jobs"]["demo"]["backup"]


def test_save_job_rename_preserves_nested_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo]  # keep this comment
hook_timeout = 60

[jobs.demo.backup.local]
repository = "/repo"
password = "s"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name="demo", form={"name": "renamed", "hook_timeout": "60"})

    assert result["saved"] is True
    content = config_path.read_text(encoding="utf-8")
    assert "[jobs.renamed]  # keep this comment" in content
    parsed = _parsed(config_path)
    assert parsed["jobs"]["renamed"]["backup"]["local"]["repository"] == "/repo"
    assert "demo" not in parsed["jobs"]


def test_save_job_invalid_name_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name=None, form={"name": "invalid name!"})

    assert result["saved"] is False
    assert "Invalid" in result["error"]


@pytest.mark.parametrize("job_name", ["_system", "__dockkeep_adhoc_restore__", "new"])
def test_save_job_reserved_name_rejected(tmp_path: Path, job_name: str) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name=None, form={"name": job_name})

    assert result["saved"] is False
    assert "reserved" in result["error"]


def test_save_job_duplicate_name_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_job_form(job_name=None, form={"name": "demo"})

    assert result["saved"] is False
    assert "already exists" in result["error"]


@pytest.mark.parametrize("reserved", ["retention", "cleanup", "password", "keep_last", "new"])
def test_save_backup_form_rejects_reserved_default_key_name(tmp_path: Path, reserved: str) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name=None,
        form={"name": reserved, "repository": "/repo"},
    )

    assert result["saved"] is False
    assert "reserved" in result["error"]


def test_save_backup_form_treats_scalar_default_key_as_not_found(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original = _FULL
    _write(config_path, original)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name="retention",
        form={"name": "renamed", "repository": "/repo"},
    )

    assert result["saved"] is False
    assert result["error"] == "Backup not found: 'retention'"
    assert config_path.read_text(encoding="utf-8") == original.strip()


@pytest.mark.parametrize("reserved", ["transfers", "checkers", "bwlimit", "new"])
def test_save_rclone_form_rejects_reserved_default_key_name(tmp_path: Path, reserved: str) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name=None,
        form={"name": reserved, "source": "/repo", "target": "s3:bucket/x"},
    )

    assert result["saved"] is False
    assert "reserved" in result["error"]


def test_save_rclone_form_treats_scalar_default_key_as_not_found(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original = _FULL
    _write(config_path, original)
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name="transfers",
        form={"name": "renamed", "source": "/repo", "target": "s3:bucket/x"},
    )

    assert result["saved"] is False
    assert result["error"] == "Rclone task not found: 'transfers'"
    assert config_path.read_text(encoding="utf-8") == original.strip()


def test_get_backup_defaults_form_existing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_backup_defaults_form("demo")

    assert result["error"] is None
    assert [group["title"] for group in result["groups"]] == [
        "Repository Options",
        "Backup-Task",
        "Retention-Task",
        "Cleanup-Task",
    ]
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}
    assert fields["retention"]["value"] == "false"
    assert fields["exclude"]["value"] == "*.tmp"
    backup_fields = {
        f["name"]
        for group in result["groups"]
        if group["title"] == "Backup-Task"
        for f in group["fields"]
    }
    assert {"exclude_caches", "one_file_system", "extra_restic_backup_args"}.issubset(backup_fields)
    assert "backup_timeout" in backup_fields
    retention_fields = {
        f["name"]
        for group in result["groups"]
        if group["title"] == "Retention-Task"
        for f in group["fields"]
    }
    assert "extra_restic_forget_args" in retention_fields
    cleanup_fields = {
        f["name"]
        for group in result["groups"]
        if group["title"] == "Cleanup-Task"
        for f in group["fields"]
    }
    assert {"cleanup", "extra_restic_prune_args"}.issubset(cleanup_fields)


def test_exclude_fields_expose_backend_specific_info_panels(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    backup_fields = {
        f["name"]: f
        for group in svc.get_backup_form("demo", "local")["groups"]
        for f in group["fields"]
    }
    rclone_fields = {
        f["name"]: f
        for group in svc.get_rclone_form("demo", "offsite")["groups"]
        for f in group["fields"]
    }

    assert {panel["backend"] for panel in backup_fields["exclude"]["info_panels"]} == {
        "restic",
        "rclone",
    }
    assert [panel["backend"] for panel in rclone_fields["exclude"]["info_panels"]] == ["rclone"]
    assert [panel["backend"] for panel in rclone_fields["filter_from"]["info_panels"]] == ["rclone"]


def test_high_risk_fields_expose_info_panels(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    def fields_by_name(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {f["name"]: f for group in form["groups"] for f in group["fields"]}

    backup_fields = fields_by_name(svc.get_backup_form("demo", "local"))
    backup_default_fields = fields_by_name(svc.get_backup_defaults_form("demo"))
    rclone_fields = fields_by_name(svc.get_rclone_form("demo", "offsite"))
    rclone_default_fields = fields_by_name(svc.get_rclone_defaults_form("demo"))
    job_fields = fields_by_name(svc.get_job_form("demo"))
    workflow_fields = fields_by_name(svc.get_workflow_form("demo", "nightly"))

    assert backup_fields["source_files"]["info_panels"][0]["title"] == "Source file lists"
    assert backup_fields["retention"]["info_panels"][0]["title"] == "Restic retention and cleanup"
    assert backup_default_fields["cleanup"]["info_panels"][0]["title"] == (
        "Restic retention and cleanup"
    )
    assert backup_fields["extra_restic_backup_args"]["info_panels"][0]["title"] == (
        "Additional Restic arguments"
    )
    assert backup_fields["pre_hooks"]["info_panels"][0]["title"] == "Hook execution"
    assert job_fields["on_error_hooks"]["info_panels"][0]["title"] == "Hook execution"
    assert workflow_fields["post_hooks"]["info_panels"][0]["title"] == "Hook execution"
    assert rclone_fields["sync_delete"]["info_panels"][0]["title"] == "Rclone sync deletion"
    assert rclone_default_fields["extra_rclone_args"]["info_panels"][0]["title"] == (
        "Additional Rclone arguments"
    )


def test_notification_env_fields_expose_info_panels() -> None:
    from src.services.config_editor_schema import (
        MAIL_FIELDS,
        PUSHOVER_FIELDS,
        field_views,
    )

    mail_fields = {f["name"]: f for f in field_views({}, MAIL_FIELDS)}
    pushover_fields = {f["name"]: f for f in field_views({}, PUSHOVER_FIELDS)}

    assert mail_fields["username_env"]["info_panels"][0]["title"] == (
        "Notification credentials via environment"
    )
    assert mail_fields["password_env"]["info_panels"][0]["title"] == (
        "Notification credentials via environment"
    )
    assert pushover_fields["token_env"]["info_panels"][0]["title"] == (
        "Notification credentials via environment"
    )
    assert pushover_fields["user_key_env"]["info_panels"][0]["title"] == (
        "Notification credentials via environment"
    )


def test_get_backup_defaults_form_inherits_global_credential_hint(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.backup]
password_env = "RESTIC_PASSWORD"

[jobs.demo]
""",
    )
    svc = _make_service(config_path)

    credential = svc.get_backup_defaults_form("demo")["credential"]

    assert credential["mode"] == "inherit"
    assert credential["parent_hint"] == "Inherited environment variable: RESTIC_PASSWORD"


def test_get_backup_defaults_form_missing_job(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.get_backup_defaults_form("ghost")

    assert "Job not found" in str(result["error"])


def test_save_backup_defaults_changes_only_defaults_not_task_subtables(tmp_path: Path) -> None:
    """Regression: editing [jobs.<job>.backup] defaults must not touch tasks."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.backup]
keep_last = 5

[jobs.demo.backup.local]  # keep comment
repository = "/repo"
password = "s"
sources = ["/data"]
extra_restic_backup_args = ["--verbose"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_backup_defaults_form(
        job_name="demo",
        form={"keep_last": "10", "auto_init": "true", "credential__mode": "inherit"},
    )

    assert result["saved"] is True
    content = config_path.read_text(encoding="utf-8")
    assert "# keep comment" in content
    parsed = _parsed(config_path)
    defaults = parsed["jobs"]["demo"]["backup"]
    assert defaults["keep_last"] == 10
    assert defaults["auto_init"] is True
    local = defaults["local"]
    assert local["repository"] == "/repo"
    assert local["sources"] == ["/data"]
    assert local["extra_restic_backup_args"] == ["--verbose"]


def test_save_backup_defaults_writes_flat_extra_restic_args(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_defaults_form(
        job_name="demo",
        form={
            "extra_restic_backup_args": "--verbose",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    backup = parsed["jobs"]["demo"]["backup"]
    assert backup["extra_restic_backup_args"] == ["--verbose"]
    assert "restic" not in backup


def test_save_backup_defaults_credential(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_defaults_form(
        job_name="demo",
        form={"credential__mode": "password_env", "credential__password_env": "JOB_PW"},
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["backup"]["password_env"] == "JOB_PW"


def test_get_rclone_defaults_form_existing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_rclone_defaults_form("demo")

    assert result["error"] is None
    assert [group["title"] for group in result["groups"]] == [
        "Sync",
        "Rclone Options",
    ]
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}
    assert fields["transfers"]["value"] == "4"


def test_save_rclone_defaults_changes_only_defaults_not_task_subtables(tmp_path: Path) -> None:
    """Regression: editing [jobs.<job>.rclone] defaults must not touch tasks."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.rclone]
transfers = 4

[jobs.demo.rclone.offsite]  # keep comment
source = "/repo"
target = "s3:bucket"
extra_rclone_args = ["--fast-list"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_rclone_defaults_form(
        job_name="demo", form={"transfers": "8", "checkers": "16"}
    )

    assert result["saved"] is True
    content = config_path.read_text(encoding="utf-8")
    assert "# keep comment" in content
    parsed = _parsed(config_path)
    defaults = parsed["jobs"]["demo"]["rclone"]
    assert defaults["transfers"] == 8
    assert defaults["checkers"] == 16
    offsite = defaults["offsite"]
    assert offsite["target"] == "s3:bucket"
    assert offsite["extra_rclone_args"] == ["--fast-list"]


def test_save_rclone_defaults_writes_extra_rclone_args(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_rclone_defaults_form(
        job_name="demo", form={"extra_rclone_args": "--fast-list"}
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["rclone"]["extra_rclone_args"] == ["--fast-list"]


def test_backup_form_shows_inherited_effective_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global]
notify_on_error = true

[global.backup]
password = "secret"
keep_daily = 7

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.get_backup_form("demo", "local")
    assert [group["title"] for group in result["groups"]] == [
        "Basic",
        "Repository Options",
        "Backup-Task",
        "Retention-Task",
        "Cleanup-Task",
        "Hooks",
        "Notifications",
    ]
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}

    assert fields["keep_daily"]["value"] == ""
    assert fields["keep_daily"]["hint"] == "7"
    assert fields["keep_daily"]["inherited"] is True
    assert fields["notify_on_error"]["hint"] == "true"
    assert fields["notify_on_error"]["inherited"] is True


def test_backup_form_inherits_job_backup_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.backup]
keep_daily = 9

[jobs.demo.backup.local]
repository = "/repo"
password = "s"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    fields = {
        f["name"]: f for g in svc.get_backup_form("demo", "local")["groups"] for f in g["fields"]
    }

    assert fields["keep_daily"]["hint"] == "9"
    assert fields["keep_daily"]["inherited"] is True


def test_save_backup_form_clears_field_on_empty_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.backup]
password = "secret"
keep_daily = 7

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
keep_daily = 3
""",
    )
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name="local",
        form={
            "name": "local",
            "repository": "/repo",
            "sources": "/data",
            "keep_daily": "",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert "keep_daily" not in parsed["jobs"]["demo"]["backup"]["local"]


def test_save_backup_form_writes_flat_extra_restic_args(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name="local",
        form={
            "name": "local",
            "repository": "/repo",
            "sources": "/data",
            "extra_restic_backup_args": "--verbose",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    local = _parsed(config_path)["jobs"]["demo"]["backup"]["local"]
    assert local["extra_restic_backup_args"] == ["--verbose"]
    assert "restic" not in local


def test_save_backup_form_preserves_explicit_empty_inheritable_list(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.backup]
password = "secret"
extra_restic_backup_args = ["--one-file-system"]

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
extra_restic_backup_args = []
""",
    )
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name="local",
        form={
            "name": "local",
            "repository": "/repo",
            "sources": "/data",
            "extra_restic_backup_args": "",
            "extra_restic_backup_args__empty": "true",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["backup"]["local"]["extra_restic_backup_args"] == []


def test_save_backup_form_can_switch_empty_inheritable_list_back_to_inherit(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.backup]
password = "secret"
extra_restic_backup_args = ["--one-file-system"]

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
extra_restic_backup_args = []
""",
    )
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name="local",
        form={
            "name": "local",
            "repository": "/repo",
            "sources": "/data",
            "extra_restic_backup_args": "",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert "extra_restic_backup_args" not in parsed["jobs"]["demo"]["backup"]["local"]


def test_save_backup_task_does_not_change_container_defaults(tmp_path: Path) -> None:
    """Regression: editing a backup task must not alter container defaults."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.backup]
keep_last = 5
auto_init = true

[jobs.demo.backup.local]
repository = "/repo"
password = "s"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name="local",
        form={
            "name": "local",
            "repository": "/new-repo",
            "sources": "/data",
            "keep_last": "20",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    defaults = parsed["jobs"]["demo"]["backup"]
    assert defaults["keep_last"] == 5
    assert defaults["auto_init"] is True
    assert defaults["local"]["keep_last"] == 20
    assert defaults["local"]["repository"] == "/new-repo"


def test_save_backup_creates_new_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name=None,
        form={
            "name": "newbackup",
            "repository": "/new-repo",
            "sources": "/data",
            "backend": "restic",
            "credential__mode": "inherit",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["backup"]["newbackup"]["repository"] == "/new-repo"
    assert parsed["jobs"]["demo"]["backup"]["newbackup"]["backend"] == "restic"


def test_save_backup_invalid_name_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name=None,
        form={"name": "bad name", "repository": "/r", "sources": "/data"},
    )

    assert result["saved"] is False
    assert "Invalid" in result["error"]


def test_save_backup_empty_repository_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo", backup_name=None, form={"name": "ok", "repository": ""}
    )

    assert result["saved"] is False
    assert "repository" in result["error"]


def test_save_backup_relative_source_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_backup_form(
        job_name="demo",
        backup_name=None,
        form={"name": "ok", "repository": "/r", "sources": "relative"},
    )

    assert result["saved"] is False
    assert "absolute" in result["error"]


def test_get_backup_form_missing_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_backup_form("demo", "ghost")

    assert "Backup not found" in str(result["error"])


def test_get_backup_form_default_key_is_not_a_task(tmp_path: Path) -> None:
    """A scalar default key like 'retention' must not resolve as a backup task."""
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_backup_form("demo", "retention")

    assert "Backup not found" in str(result["error"])


def test_save_global_form_keeps_masked_password_and_adds_mail_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "credential__mode": "password",
            "credential__password": "global-secret",
            "mail__enabled": "true",
            "mail__host": "smtp.example.com",
            "mail__from_addr": "backup@example.com",
            "mail__to": "ops@example.com",
        }
    )

    assert result["saved"] is True
    form = svc.get_global_form()
    assert form["credential"]["password_configured"] is True
    assert "password" not in form["credential"]
    parsed = _parsed(config_path)
    assert parsed["global"]["backup"]["password"] == "global-secret"
    assert parsed["global"]["notifications"]["mail"]["host"] == "smtp.example.com"
    assert parsed["global"]["notifications"]["mail"]["username_env"] == "SMTP_USER"
    assert parsed["global"]["notifications"]["mail"]["password_env"] == "SMTP_PASSWORD"


def test_save_global_form_round_trips_provider_events(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "credential__mode": "unset",
            "mail__enabled": "true",
            "mail__host": "smtp.example.com",
            "mail__from_addr": "backup@example.com",
            "mail__to": "ops@example.com",
            "mail__events": "",
            "mail__events__error": "true",
            "mail__events__report": "true",
        }
    )

    assert result["saved"] is True
    assert _parsed(config_path)["global"]["notifications"]["mail"]["events"] == ["error", "report"]

    form = svc.get_global_form()
    providers = {provider["name"]: provider for provider in form["providers"]}
    mail_fields = {
        field["name"]: field for group in providers["mail"]["groups"] for field in group["fields"]
    }
    assert mail_fields["mail__events"]["value"] == "error\nreport"
    assert mail_fields["mail__events"]["kind"] == "checklist"
    assert mail_fields["mail__events"]["selected_choices"] == ["error", "report"]


def test_save_global_form_without_events_leaves_channel_unfiltered(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "credential__mode": "unset",
            "mail__enabled": "true",
            "mail__host": "smtp.example.com",
            "mail__from_addr": "backup@example.com",
            "mail__to": "ops@example.com",
            "mail__events": "",
        }
    )

    assert result["saved"] is True
    assert "events" not in _parsed(config_path)["global"]["notifications"]["mail"]


def test_global_form_prefills_default_notification_credential_envs(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.get_global_form()
    providers = {provider["name"]: provider for provider in result["providers"]}
    mail_fields = {
        field["name"]: field for group in providers["mail"]["groups"] for field in group["fields"]
    }
    pushover_fields = {
        field["name"]: field
        for group in providers["pushover"]["groups"]
        for field in group["fields"]
    }

    assert mail_fields["mail__username_env"]["value"] == "SMTP_USER"
    assert mail_fields["mail__password_env"]["value"] == "SMTP_PASSWORD"
    assert pushover_fields["pushover__token_env"]["value"] == "PUSHOVER_TOKEN"
    assert pushover_fields["pushover__user_key_env"]["value"] == "PUSHOVER_USER_KEY"


def test_save_global_form_adds_default_pushover_credential_envs(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "credential__mode": "unset",
            "mail__enabled": "false",
            "pushover__enabled": "true",
        }
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["notifications"]["pushover"]["token_env"] == "PUSHOVER_TOKEN"
    assert parsed["global"]["notifications"]["pushover"]["user_key_env"] == "PUSHOVER_USER_KEY"


def test_test_notification_provider_sends_mail_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.notifications.mail]
host = "smtp.example.com"
from_addr = "backup@example.com"
to = ["ops@example.com"]

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    provider = MagicMock()
    provider_cls = MagicMock(return_value=provider)
    monkeypatch.setattr("src.services.config_editor.MailProvider", provider_cls)
    svc = _make_service(config_path)

    result = svc.test_notification_provider("mail")

    assert result == {
        "ok": True,
        "provider": "mail",
        "message": "Test notification sent successfully.",
    }
    provider_cls.assert_called_once()
    event = provider.send.call_args.args[0]
    assert event.job_name == "dockkeep"
    assert event.task_name == "mail_provider_test"
    assert event.status == "success"


def test_test_notification_provider_uses_unsaved_mail_form_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    provider = MagicMock()
    provider_cls = MagicMock(return_value=provider)
    monkeypatch.setattr("src.services.config_editor.MailProvider", provider_cls)
    svc = _make_service(config_path)

    result = svc.test_notification_provider(
        "mail",
        {
            "mail__enabled": "true",
            "mail__host": "draft.smtp.example.com",
            "mail__port": "2525",
            "mail__connection_security": "none",
            "mail__from_addr": "draft@example.com",
            "mail__to": "ops@example.com\nadmin@example.com",
        },
    )

    assert result["ok"] is True
    config = provider_cls.call_args.args[0]
    assert config.host == "draft.smtp.example.com"
    assert config.port == 2525
    assert config.connection_security == "none"
    assert config.to == ["ops@example.com", "admin@example.com"]


def test_test_notification_provider_reports_missing_pushover_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.test_notification_provider("pushover")

    assert result == {
        "ok": False,
        "provider": "pushover",
        "message": "Pushover provider is not configured.",
    }


def test_test_notification_provider_reports_delivery_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.notifications.pushover]
token_env = "PUSHOVER_TOKEN"
user_key_env = "PUSHOVER_USER_KEY"

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    provider = MagicMock()
    provider.send.side_effect = ValueError("missing token")
    monkeypatch.setattr(
        "src.services.config_editor.PushoverProvider", MagicMock(return_value=provider)
    )
    svc = _make_service(config_path)

    result = svc.test_notification_provider("pushover")

    assert result == {
        "ok": False,
        "provider": "pushover",
        "message": "Test notification failed: missing token",
    }


def test_test_notification_report_sends_mail_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.notifications.mail]
host = "smtp.example.com"
from_addr = "backup@example.com"
to = ["ops@example.com"]

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    provider = MagicMock()
    provider_cls = MagicMock(return_value=provider)
    monkeypatch.setattr("src.services.config_editor.MailProvider", provider_cls)
    svc = _make_service(config_path)

    result = svc.test_notification_report("mail")

    assert result == {
        "ok": True,
        "provider": "mail",
        "message": "Test report sent successfully.",
    }
    provider_cls.assert_called_once()
    event = provider.send_report.call_args.args[0]
    assert len(event.runs) == 2
    assert {run.status for run in event.runs} == {"success", "failed"}


def test_test_notification_report_reports_missing_pushover_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.test_notification_report("pushover")

    assert result == {
        "ok": False,
        "provider": "pushover",
        "message": "Pushover provider is not configured.",
    }


def test_test_notification_report_reports_delivery_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.notifications.pushover]
token_env = "PUSHOVER_TOKEN"
user_key_env = "PUSHOVER_USER_KEY"

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    provider = MagicMock()
    provider.send_report.side_effect = ValueError("missing token")
    monkeypatch.setattr(
        "src.services.config_editor.PushoverProvider", MagicMock(return_value=provider)
    )
    svc = _make_service(config_path)

    result = svc.test_notification_report("pushover")

    assert result == {
        "ok": False,
        "provider": "pushover",
        "message": "Test report failed: missing token",
    }


def test_save_global_form_writes_notify_triggers_under_global(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "global__notify_on_success": "true",
            "credential__mode": "unset",
            "mail__enabled": "false",
            "pushover__enabled": "false",
        }
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["notify_on_success"] is True
    assert "notify_on_success" not in parsed["global"].get("notifications", {})


def test_global_form_edits_report_schedule_under_global_notifications(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.notifications]
report_schedule = "0 7 * * *"

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    form = svc.get_global_form()
    fields = {field["name"]: field for group in form["groups"] for field in group["fields"]}

    assert fields["notifications__report_schedule"]["value"] == "0 7 * * *"

    result = svc.save_global_form(
        {
            "notifications__report_schedule": "0 8 * * *",
            "credential__mode": "unset",
            "mail__enabled": "false",
            "pushover__enabled": "false",
        }
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["notifications"]["report_schedule"] == "0 8 * * *"
    assert "report_schedule" not in parsed["global"]


def test_save_global_form_writes_flat_backup_extra_args(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "backup__extra_restic_backup_args": "--verbose",
            "credential__mode": "unset",
            "mail__enabled": "false",
            "pushover__enabled": "false",
        }
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["backup"]["extra_restic_backup_args"] == ["--verbose"]
    assert "restic" not in parsed["global"]["backup"]


def test_save_global_form_updates_log_level_without_empty_child_tables(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "global__log_level": "debug",
            "credential__mode": "unset",
            "mail__enabled": "false",
            "pushover__enabled": "false",
        }
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["log_level"] == "debug"
    assert "backup" not in parsed["global"]
    assert "rclone" not in parsed["global"]
    assert "notifications" not in parsed["global"]


def test_global_form_offers_all_model_log_levels(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, '[global]\nlog_level = "critical"\n' + _MINIMAL)
    svc = _make_service(config_path)

    result = svc.get_global_form()
    assert [group["title"] for group in result["groups"]] == [
        "General",
        "Repository Options",
        "Backup-Task",
        "Retention-Task",
        "Cleanup-Task",
        "Sync",
        "Rclone Options",
        "Notifications",
    ]
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}

    assert fields["global__log_level"]["value"] == "critical"
    assert "critical" in fields["global__log_level"]["choices"]
    assert fields["global__lock_retry_count"]["kind"] == "number"
    assert fields["global__lock_retry_delay"]["kind"] == "number"


def test_global_form_rerender_preserves_invalid_number_input(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    form = svc.get_global_form(
        submitted={
            "global__log_level": "debug",
            "global__log_retention_days": "abc",
            "global__lock_retry_delay": "oops",
        }
    )
    fields = {f["name"]: f for g in form["groups"] for f in g["fields"]}
    assert fields["global__log_retention_days"]["value"] == "abc"
    assert fields["global__lock_retry_delay"]["value"] == "oops"


def test_global_form_saves_lock_retry_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    svc = _make_service(config_path)

    result = svc.save_global_form(
        {
            "global__log_level": "debug",
            "global__lock_retry_count": "2",
            "global__lock_retry_delay": "5",
            "mail__enabled": "false",
            "pushover__enabled": "false",
        }
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["lock_retry_count"] == 2
    assert parsed["global"]["lock_retry_delay"] == 5


def test_partial_global_form_preserves_existing_credentials_and_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[global.backup]
password_env = "GLOBAL_PW"

[global.notifications.mail]
host = "smtp.example.com"
from_addr = "a@b.c"
to = ["x@y.z"]

[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_global_form({"global__log_level": "debug"})

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["global"]["backup"]["password_env"] == "GLOBAL_PW"
    assert parsed["global"]["notifications"]["mail"]["host"] == "smtp.example.com"


def test_global_parent_uses_global_config_defaults() -> None:
    import tomlkit

    parent = _global_cross_cutting_parent(tomlkit.parse(""))
    assert parent["notify_on_success"] is False
    assert parent["notify_on_error"] is False
    assert parent["notify_on_skipped"] is False

    backup_parent = _global_backup_parent(tomlkit.parse(""))
    assert backup_parent["retention"] is False


def test_get_workflow_form_new_workflow(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_workflow_form("demo", None)

    assert result["error"] is None
    assert set(result["available_steps"]) == {"backup.local", "backup.remote", "rclone.offsite"}
    assert result["available_backups"] == ["local", "remote"]


def test_get_workflow_form_existing_workflow(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_workflow_form("demo", "nightly")

    assert result["error"] is None
    assert result["steps"] == ["backup.local", "backup.remote"]
    assert [group["title"] for group in result["groups"]] == [
        "Basic",
        "Hooks",
        "Notifications",
    ]


def test_get_workflow_form_missing_workflow(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_workflow_form("demo", "ghost")

    assert "Workflow not found" in str(result["error"])


def test_save_workflow_creates_new_workflow_in_singular_section(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_workflow_form(
        job_name="demo",
        workflow_name=None,
        form={"name": "weekly", "schedule": "0 5 * * 0", "steps": "backup.local"},
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert "workflows" not in parsed["jobs"]["demo"]
    wf = parsed["jobs"]["demo"]["workflow"]["weekly"]
    assert wf["steps"] == ["backup.local"]
    assert wf["schedule"] == "0 5 * * 0"


def test_save_workflow_writes_hook_timeout_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_workflow_form(
        job_name="demo",
        workflow_name="nightly",
        form={"name": "nightly", "steps": "backup.local", "hook_timeout": "120"},
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["workflow"]["nightly"]["hook_timeout"] == 120


def test_save_workflow_rename_preserves_comments(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo]

[jobs.demo.backup.local]
repository = "/repo"
password = "s"
sources = ["/data"]

[jobs.demo.workflow.nightly] # keep workflow header comment
steps = ["backup.local"]
""",
    )
    svc = _make_service(config_path)

    result = svc.save_workflow_form(
        job_name="demo",
        workflow_name="nightly",
        form={"name": "renamed", "steps": "backup.local"},
    )

    assert result["saved"] is True
    content = config_path.read_text(encoding="utf-8")
    assert "[jobs.demo.workflow.renamed] # keep workflow header comment" in content


def test_save_workflow_accepts_backup_substeps_and_rclone_steps(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_workflow_form(
        job_name="demo",
        workflow_name=None,
        form={
            "name": "mixed",
            "steps": "backup.local.backup\nbackup.local.retention\nrclone.offsite",
        },
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["workflow"]["mixed"]["steps"] == [
        "backup.local.backup",
        "backup.local.retention",
        "rclone.offsite",
    ]


def test_save_workflow_rejects_unknown_step(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_workflow_form(
        job_name="demo",
        workflow_name=None,
        form={"name": "wf", "steps": "backup.ghost"},
    )

    assert result["saved"] is False
    assert "ghost" in result["error"]


def test_save_workflow_rejects_empty_steps(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_workflow_form(
        job_name="demo", workflow_name=None, form={"name": "wf", "steps": ""}
    )

    assert result["saved"] is False
    assert "step" in result["error"].lower()


def test_delete_workflow_uses_singular_section(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.delete_workflow(job_name="demo", workflow_name="nightly")

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert "workflow" not in parsed["jobs"]["demo"]


def test_get_rclone_form_new_task(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_rclone_form("demo", None)

    assert result["error"] is None
    assert result["target"] == ""
    assert [f["name"] for f in result["primary_fields"]] == ["source", "target"]
    assert [group["title"] for group in result["groups"]] == [
        "Basic",
        "Filter",
        "Rclone Options",
        "Hooks",
        "Notifications",
    ]
    grouped = {f["name"] for g in result["groups"] for f in g["fields"]}
    assert {"source", "target"}.isdisjoint(grouped)


def test_get_rclone_form_existing_rclone(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_rclone_form("demo", "offsite")

    assert result["error"] is None
    assert result["source"] == "/repo"
    assert result["target"] == "s3:bucket/backup"
    primary = {f["name"]: f for f in result["primary_fields"]}
    assert primary["target"]["value"] == "s3:bucket/backup"


def test_get_rclone_form_default_key_is_not_a_task(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.get_rclone_form("demo", "transfers")

    assert "Rclone task not found" in str(result["error"])


def test_rclone_form_uses_job_rclone_extra_args_as_parent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo]

[jobs.demo.rclone]
extra_rclone_args = ["--fast-list"]

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket/backup"
""",
    )
    svc = _make_service(config_path)

    result = svc.get_rclone_form("demo", "offsite")
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}

    assert fields["extra_rclone_args"]["value"] == ""
    assert fields["extra_rclone_args"]["hint"] == "--fast-list"
    assert fields["extra_rclone_args"]["inherited"] is True


def test_rclone_form_preserves_explicit_empty_extra_args_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo]

[jobs.demo.rclone]
extra_rclone_args = ["--fast-list"]

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket/backup"
extra_rclone_args = []
""",
    )
    svc = _make_service(config_path)

    result = svc.get_rclone_form("demo", "offsite")
    fields = {f["name"]: f for g in result["groups"] for f in g["fields"]}

    assert fields["extra_rclone_args"]["empty_checked"] is True
    assert fields["extra_rclone_args"]["inherited"] is False


def test_save_rclone_form_creates_task_with_target(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name=None,
        form={
            "name": "archive",
            "source": "/repo",
            "target": "s3:archive",
            "sync_delete": "true",
        },
    )

    assert result["saved"] is True
    task = _parsed(config_path)["jobs"]["demo"]["rclone"]["archive"]
    assert task["target"] == "s3:archive"
    assert task["sync_delete"] is True
    assert "remote" not in task


def test_save_rclone_task_does_not_change_container_defaults(tmp_path: Path) -> None:
    """Regression: editing an rclone task must not alter container defaults."""
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.rclone]
transfers = 4

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket"
""",
    )
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name="offsite",
        form={"name": "offsite", "source": "/repo", "target": "s3:newbucket", "transfers": "8"},
    )

    assert result["saved"] is True
    parsed = _parsed(config_path)
    rclone = parsed["jobs"]["demo"]["rclone"]
    assert rclone["transfers"] == 4
    assert rclone["offsite"]["target"] == "s3:newbucket"
    assert rclone["offsite"]["transfers"] == 8


def test_save_rclone_form_renames_task_and_rejects_duplicates(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo]

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket"

[jobs.demo.rclone.archive]
source = "/repo"
target = "s3:archive"
""",
    )
    svc = _make_service(config_path)

    duplicate = svc.save_rclone_form(
        job_name="demo",
        rclone_name="offsite",
        form={"name": "archive", "source": "/repo", "target": "s3:bucket"},
    )
    assert duplicate["saved"] is False

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name="offsite",
        form={"name": "mirror", "source": "/repo", "target": "s3:bucket"},
    )
    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["rclone"]["mirror"]["target"] == "s3:bucket"


def test_save_rclone_rejects_empty_source(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name=None,
        form={"name": "x", "source": "", "target": "remote:x"},
    )

    assert result["saved"] is False
    assert "source" in result["error"]


def test_save_rclone_rejects_empty_target(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    result = svc.save_rclone_form(
        job_name="demo",
        rclone_name=None,
        form={"name": "x", "source": "/repo", "target": ""},
    )

    assert result["saved"] is False
    assert "target" in result["error"]


def test_get_rclone_form_missing_job_and_task_return_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _FULL)
    svc = _make_service(config_path)

    assert "Job not found" in str(svc.get_rclone_form("ghost", "offsite")["error"])
    assert "Rclone task not found" in str(svc.get_rclone_form("demo", "ghost")["error"])


def test_delete_rclone_removes_task_and_empty_parent_table(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo]

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket"
""",
    )
    svc = _make_service(config_path)

    result = svc.delete_rclone(job_name="demo", rclone_name="offsite")

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert "rclone" not in parsed["jobs"]["demo"]


def test_delete_rclone_keeps_container_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(
        config_path,
        """
[jobs.demo.rclone]
transfers = 4

[jobs.demo.rclone.offsite]
source = "/repo"
target = "s3:bucket"
""",
    )
    svc = _make_service(config_path)

    result = svc.delete_rclone(job_name="demo", rclone_name="offsite")

    assert result["saved"] is True
    parsed = _parsed(config_path)
    assert parsed["jobs"]["demo"]["rclone"]["transfers"] == 4
    assert "offsite" not in parsed["jobs"]["demo"]["rclone"]


def test_full_editor_round_trip_validates_against_raw_models(tmp_path: Path) -> None:
    from src.models.config import RawAppConfig

    config_path = tmp_path / "config.toml"
    _write(config_path, "[jobs.demo]\n")
    svc = _make_service(config_path)

    assert svc.save_backup_defaults_form(
        job_name="demo",
        form={
            "keep_last": "5",
            "credential__mode": "password_env",
            "credential__password_env": "PW",
        },
    )["saved"]
    assert svc.save_backup_form(
        job_name="demo",
        backup_name=None,
        form={
            "name": "local",
            "repository": "/repo",
            "sources": "/data",
            "backend": "restic",
            "credential__mode": "inherit",
        },
    )["saved"]
    assert svc.save_rclone_defaults_form(job_name="demo", form={"transfers": "4"})["saved"]
    assert svc.save_rclone_form(
        job_name="demo",
        rclone_name=None,
        form={"name": "offsite", "source": "/repo", "target": "s3:bucket"},
    )["saved"]
    assert svc.save_workflow_form(
        job_name="demo",
        workflow_name=None,
        form={"name": "nightly", "steps": "backup.local\nrclone.offsite"},
    )["saved"]

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    cfg = RawAppConfig.model_validate(data)
    assert cfg.jobs["demo"].backup.tasks["local"].repository == "/repo"
    assert cfg.jobs["demo"].rclone.tasks["offsite"].target == "s3:bucket"
    assert "nightly" in cfg.jobs["demo"].workflow


def test_service_container_has_config_editor_service(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    container: ServiceContainer = create_services(config_path, appdata_dir=tmp_path)
    assert isinstance(container.config_editor_service, ConfigEditorService)


def test_config_editor_service_in_container_is_wired_to_config_service(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write(config_path, _MINIMAL)
    container = create_services(config_path, appdata_dir=tmp_path)
    overview = asyncio.run(container.config_editor_service.get_overview())
    assert overview["error"] is None
    assert [j["name"] for j in overview["jobs"]] == ["demo"]
