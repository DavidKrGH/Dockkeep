import copy
from typing import Any

import pytest
from pydantic import ValidationError

from src.models.config import RawAppConfig
from src.models.resolve import resolve_config
from src.models.resolved_config import (
    ResolvedAppConfig,
    ResolvedBackupConfig,
    ResolvedJobConfig,
    ResolvedRcloneSyncTaskConfig,
    ResolvedWorkflowConfig,
)
from tests.config_builders import (
    deep_merge,
    example_raw_app_config,
    raw_app,
    raw_app_config,
    raw_backup_task,
    raw_rclone_task,
    raw_workflow,
)


def _build_raw(overrides: dict[str, Any] | None = None) -> RawAppConfig:
    data = raw_app(
        global_config={
            "hook_timeout": 30,
            "notify_on_success": False,
            "notify_on_error": True,
            "backup": {
                "backup_timeout": 600,
                "retention": True,
                "cleanup": False,
                "keep_last": 5,
                "exclude": ["*.tmp"],
                "exclude_files": ["/etc/global-excludes.txt"],
                "extra_restic_backup_args": ["--global-extra"],
            },
            "rclone": {
                "extra_rclone_args": ["--global-rclone"],
            },
        },
        jobs={
            "job1": {
                "pre_hooks": ["job-pre"],
                "post_hooks": ["job-post"],
                "on_error_hooks": ["job-error"],
                "backup": {
                    "keep_last": 7,
                    "b1": raw_backup_task(repository="/repo1", sources=["/data/job1"]),
                    "b2": raw_backup_task(
                        repository="/repo2",
                        sources=[],
                        overrides={
                            "source_files": ["/lists/job1-b2.txt"],
                            "exclude": [],
                            "exclude_files": ["/excludes/job1-b2.txt"],
                            "retention": False,
                            "backup_timeout": 120,
                            "extra_restic_backup_args": [],
                            "pre_hooks": ["backup-pre"],
                        },
                    ),
                },
                "rclone": {
                    "transfers": 4,
                    "extra_rclone_args": ["--job-rclone"],
                    "r1": raw_rclone_task(source="/repo1", target="myremote:bucket/repo1"),
                },
                "workflow": {
                    "w1": raw_workflow(steps=["backup.b1", "rclone.r1"]),
                    "w2": raw_workflow(
                        steps=["backup.b2"],
                        overrides={"hook_timeout": 120},
                    ),
                },
            },
        },
    )
    if overrides:
        data = deep_merge(data, overrides)
    return raw_app_config(data)


def test_resolve_returns_resolved_app_config() -> None:
    resolved = resolve_config(_build_raw())
    assert isinstance(resolved, ResolvedAppConfig)
    assert isinstance(resolved.jobs["job1"], ResolvedJobConfig)
    assert isinstance(resolved.jobs["job1"].backup["b1"], ResolvedBackupConfig)
    assert isinstance(resolved.jobs["job1"].rclone["r1"], ResolvedRcloneSyncTaskConfig)
    assert isinstance(resolved.jobs["job1"].workflows["w1"], ResolvedWorkflowConfig)


def test_example_config_full_job_local_resolves_expected_overrides() -> None:
    resolved = resolve_config(example_raw_app_config())
    full_job = resolved.jobs["full_job"]
    local = full_job.backup["local"]

    assert full_job.hooks.pre_hooks == ["/scripts/pre-job.sh"]
    assert local.repository == "/backups/full_job/local"
    assert local.credentials.password_env == "BACKUP_PASSWORD_LOCAL"
    assert local.input.sources == ["/data/projects", "/data/documents"]
    assert local.input.source_files == ["/config/sources/local-extra.txt"]
    assert local.filters.exclude == ["*.tmp", "node_modules", ".venv"]
    assert local.execution.retention is True
    assert local.execution.cleanup is True
    assert local.timeouts.backup_timeout == 3600
    assert local.timeouts.hook_timeout == 300
    assert local.retention.keep_last == 14
    assert local.backend_options.restic.extra_backup_args == [
        "--tag local",
        '--tag "Server Backup"',
    ]
    assert local.hooks.pre_hooks == ["/scripts/pre-local.sh"]
    assert local.notifications.notify_on_success is False


def test_example_config_workflow_maintenance_inherits_job_hook_timeout() -> None:
    full_job = resolve_config(example_raw_app_config()).jobs["full_job"]

    assert full_job.workflows["daily"].timeouts.hook_timeout == 120
    assert full_job.workflows["maintenance"].timeouts.hook_timeout == 600


def test_resolved_backup_config_only_allows_restic_backend() -> None:
    with pytest.raises(ValidationError):
        ResolvedBackupConfig(repository="/repo", backend="borg")  # type: ignore[arg-type]


def test_global_fields_carry_through_unchanged() -> None:
    resolved = resolve_config(
        _build_raw(
            {
                "global": {
                    "lock_retry_count": 2,
                    "lock_retry_delay": 5,
                    "notifications": {
                        "report_schedule": "0 8 * * *",
                        "mail": {
                            "host": "smtp.example.test",
                            "from_addr": "dk@example.test",
                            "to": ["admin@example.test"],
                            "events": ["error", "report"],
                        },
                        "pushover": {
                            "token_env": "PUSHOVER_TOKEN",
                            "user_key_env": "PUSHOVER_USER_KEY",
                            "events": ["error"],
                        },
                    },
                }
            }
        )
    )
    assert resolved.global_.log_level == "debug"
    assert resolved.global_.log_retention_days is None
    assert resolved.global_.lock_retry_count == 2
    assert resolved.global_.lock_retry_delay == 5
    assert resolved.global_.notifications.report_schedule == "0 8 * * *"
    assert resolved.global_.notifications.mail is not None
    assert resolved.global_.notifications.mail.events == ["error", "report"]
    assert resolved.global_.notifications.pushover is not None
    assert resolved.global_.notifications.pushover.events == ["error"]


def test_backup_inherits_global_backup_to_job_backup_to_task() -> None:
    resolved = resolve_config(_build_raw())
    b1 = resolved.jobs["job1"].backup["b1"]
    b2 = resolved.jobs["job1"].backup["b2"]
    assert b1.retention.keep_last == 7
    assert b2.retention.keep_last == 7
    assert b1.timeouts.backup_timeout == 600
    assert b2.timeouts.backup_timeout == 120


def test_keep_within_granular_fields_inherit_and_resolve() -> None:
    raw = RawAppConfig.model_validate(
        raw_app(
            global_config={
                "backup": {
                    "keep_within_hourly": "7d",
                    "keep_within_daily": "1m",
                }
            },
            jobs={
                "job1": {
                    "backup": {
                        "keep_within_weekly": "1y",
                        "local": raw_backup_task(
                            repository="/repo",
                            sources=["/data"],
                            overrides={
                                "keep_within_monthly": "5y",
                                "keep_within_yearly": "75y",
                            },
                        ),
                    }
                }
            },
        )
    )

    retention = resolve_config(raw).jobs["job1"].backup["local"].retention

    assert retention.keep_within_hourly == "7d"
    assert retention.keep_within_daily == "1m"
    assert retention.keep_within_weekly == "1y"
    assert retention.keep_within_monthly == "5y"
    assert retention.keep_within_yearly == "75y"


def test_list_override_none_inherits_parent_list() -> None:
    b1 = resolve_config(_build_raw()).jobs["job1"].backup["b1"]
    assert b1.filters.exclude == ["*.tmp"]
    assert b1.filters.exclude_files == ["/etc/global-excludes.txt"]
    assert b1.input.sources == ["/data/job1"]
    assert b1.input.source_files == []


def test_list_override_empty_list_stops_inheritance() -> None:
    b2 = resolve_config(_build_raw()).jobs["job1"].backup["b2"]
    assert b2.input.sources == []
    assert b2.filters.exclude == []
    assert b2.input.source_files == ["/lists/job1-b2.txt"]
    assert b2.filters.exclude_files == ["/excludes/job1-b2.txt"]


def test_inherited_lists_are_independent_copies() -> None:
    raw = _build_raw()
    raw_before = copy.deepcopy(raw)

    b1 = resolve_config(raw).jobs["job1"].backup["b1"]
    b1.backend_options.restic.extra_backup_args.append("mutated-only-here")

    b1_again = resolve_config(raw).jobs["job1"].backup["b1"]
    assert b1_again.backend_options.restic.extra_backup_args == ["--global-extra"]
    assert raw.model_dump() == raw_before.model_dump()


def test_password_choice_inherits_from_global_backup() -> None:
    raw = _build_raw({"global": {"backup": {"password": "global-secret"}}})
    b1 = resolve_config(raw).jobs["job1"].backup["b1"]
    assert b1.credentials.password == "global-secret"
    assert b1.credentials.password_env is None
    assert b1.credentials.password_file is None


def test_password_choice_inherits_from_job_backup_defaults() -> None:
    raw = _build_raw(
        {
            "global": {"backup": {"password": "global-secret"}},
            "jobs": {"job1": {"backup": {"password_env": "JOB_BACKUP_SECRET"}}},
        }
    )
    b1 = resolve_config(raw).jobs["job1"].backup["b1"]
    assert b1.credentials.password is None
    assert b1.credentials.password_env == "JOB_BACKUP_SECRET"
    assert b1.credentials.password_file is None


def test_empty_password_env_resolves_as_missing_and_keeps_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_SECRET", "")
    raw = _build_raw(
        {
            "global": {"backup": {"password": "global-secret"}},
            "jobs": {"job1": {"backup": {"b1": {"password_env": "LOCAL_SECRET"}}}},
        }
    )

    b1 = resolve_config(raw).jobs["job1"].backup["b1"]

    assert b1.credentials.password is None
    assert b1.credentials.password_env == "LOCAL_SECRET"


def test_notifications_inherit_global_to_backup_workflow_and_rclone() -> None:
    resolved = resolve_config(_build_raw())
    job = resolved.jobs["job1"]
    assert job.backup["b1"].notifications.notify_on_error is True
    assert job.workflows["w1"].notifications.notify_on_error is True
    assert job.rclone["r1"].notifications.notify_on_error is True


def test_rclone_options_inherit_global_to_task() -> None:
    raw = _build_raw({"global": {"rclone": {"extra_rclone_args": ["--fast-list"]}}})
    rclone = resolve_config(raw).jobs["job1"].rclone["r1"]
    assert rclone.options.transfers == 4
    assert rclone.options.extra_args == ["--job-rclone"]


def test_rclone_options_can_inherit_directly_from_global_rclone_defaults() -> None:
    raw = _build_raw(
        {
            "global": {
                "rclone": {
                    "rclone_timeout": 999,
                    "sync_delete": True,
                    "transfers": 9,
                    "checkers": 11,
                    "bwlimit": "99M",
                    "exclude": ["global-exclude"],
                    "filter_from": "/filters/global-rclone.txt",
                    "extra_rclone_args": ["--global-only"],
                }
            },
            "jobs": {
                "job1": {
                    "rclone": {
                        "rclone_timeout": None,
                        "sync_delete": None,
                        "transfers": None,
                        "checkers": None,
                        "bwlimit": None,
                        "exclude": None,
                        "filter_from": None,
                        "extra_rclone_args": None,
                    }
                }
            },
        }
    )
    rclone = resolve_config(raw).jobs["job1"].rclone["r1"]
    assert rclone.timeouts.rclone_timeout == 999
    assert rclone.sync_delete is True
    assert rclone.options.transfers == 9
    assert rclone.options.checkers == 11
    assert rclone.options.bwlimit == "99M"
    assert rclone.exclude == ["global-exclude"]
    assert rclone.filter_from == "/filters/global-rclone.txt"
    assert rclone.options.extra_args == ["--global-only"]


def test_rclone_extra_rclone_args_inherit_from_job_rclone_defaults() -> None:
    raw = _build_raw({"jobs": {"job1": {"rclone": {"extra_rclone_args": ["--job-rclone"]}}}})
    rclone = resolve_config(raw).jobs["job1"].rclone["r1"]
    assert rclone.options.extra_args == ["--job-rclone"]


def test_rclone_extra_args_empty_task_list_stops_job_inheritance() -> None:
    raw = _build_raw(
        {
            "jobs": {
                "job1": {
                    "rclone": {
                        "extra_rclone_args": ["--job-rclone"],
                        "r1": {"extra_rclone_args": []},
                    },
                }
            }
        }
    )
    rclone = resolve_config(raw).jobs["job1"].rclone["r1"]
    assert rclone.options.extra_args == []


def test_rclone_target_resolves_from_target() -> None:
    rclone = resolve_config(_build_raw()).jobs["job1"].rclone["r1"]
    assert rclone.target == "myremote:bucket/repo1"


def test_rclone_sync_delete_defaults_to_false_when_unset() -> None:
    rclone = resolve_config(_build_raw()).jobs["job1"].rclone["r1"]
    assert rclone.sync_delete is False


def test_hooks_do_not_inherit_from_job_to_tasks_or_workflows() -> None:
    job = resolve_config(_build_raw()).jobs["job1"]
    assert job.hooks.pre_hooks == ["job-pre"]
    assert job.backup["b1"].hooks.pre_hooks == []
    assert job.backup["b2"].hooks.pre_hooks == ["backup-pre"]
    assert job.rclone["r1"].hooks.pre_hooks == []
    assert job.workflows["w1"].hooks.pre_hooks == []


def test_workflow_hook_timeout_can_override_job_timeout() -> None:
    raw = _build_raw({"jobs": {"job1": {"hook_timeout": 45}}})
    job = resolve_config(raw).jobs["job1"]
    assert job.workflows["w1"].timeouts.hook_timeout == 45
    assert job.workflows["w2"].timeouts.hook_timeout == 120


def test_resolved_default_lists_are_independent() -> None:
    first = ResolvedBackupConfig(repository="/first")
    second = ResolvedBackupConfig(repository="/second")
    first.input.sources.append("/mutated")
    first.backend_options.restic.extra_backup_args.append("--mutated")
    assert second.input.sources == []
    assert second.backend_options.restic.extra_backup_args == []
