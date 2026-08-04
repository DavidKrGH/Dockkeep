from pathlib import Path

import pytest
from pydantic import ValidationError

from src.models.config import (
    RawAppConfig,
    RawBackupConfig,
    RawGlobalBackupConfig,
    RawGlobalConfig,
    RawGlobalNotificationsConfig,
    RawGlobalRcloneConfig,
    RawJobBackupSectionConfig,
    RawJobConfig,
    RawJobRcloneSectionConfig,
    RawMailNotificationConfig,
    RawPushoverNotificationConfig,
    RawRcloneSyncTaskConfig,
    RawWorkflowConfig,
)
from src.models.resolve import resolve_config
from src.utils.validation import validate_raw_config, validate_resolved_config
from tests.config_builders import (
    example_config_data_with_existing_files,
    example_raw_app_config,
    raw_app,
    raw_backup_task,
    raw_job,
    raw_rclone_task,
)


def test_global_config_defaults_use_target_backup_section() -> None:
    cfg = RawGlobalConfig.model_validate({})

    assert cfg.log_level == "debug"
    assert cfg.log_retention_days is None
    assert cfg.lock_retry_count is None
    assert cfg.lock_retry_delay is None
    assert cfg.backup.backup_timeout is None
    assert cfg.backup.retention is False
    assert cfg.backup.cleanup is False
    assert cfg.backup.auto_init is False
    assert cfg.backup.extra_restic_backup_args is None
    assert cfg.rclone.rclone_timeout is None
    assert cfg.notify_on_error is False
    assert cfg.notify_on_skipped is False


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"lock_retry_count": -1}, "greater than or equal to 0"),
        ({"lock_retry_delay": 0}, "greater than or equal to 1"),
    ],
)
def test_global_lock_retry_field_bounds(payload: dict[str, int], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RawGlobalConfig.model_validate(payload)


def test_global_backup_password_fields_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        RawGlobalBackupConfig.model_validate(
            {"password": "secret", "password_env": "RESTIC_PASSWORD"}
        )


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (RawGlobalBackupConfig, {}),
        (RawJobBackupSectionConfig, {}),
        (RawBackupConfig, {"repository": "/backups/test"}),
    ],
)
def test_restic_password_env_none_preserves_inheritance(
    model_cls: type[RawGlobalBackupConfig | RawJobBackupSectionConfig | RawBackupConfig],
    payload: dict[str, object],
) -> None:
    cfg = model_cls.model_validate(payload)

    assert cfg.password_env is None


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (RawGlobalBackupConfig, {"password_env": "RESTIC_PASSWORD"}),
        (RawJobBackupSectionConfig, {"password_env": "RESTIC_PASSWORD"}),
        (RawBackupConfig, {"repository": "/backups/test", "password_env": "RESTIC_PASSWORD"}),
    ],
)
def test_restic_password_env_accepts_non_empty_names(
    model_cls: type[RawGlobalBackupConfig | RawJobBackupSectionConfig | RawBackupConfig],
    payload: dict[str, object],
) -> None:
    cfg = model_cls.model_validate(payload)

    assert cfg.password_env == "RESTIC_PASSWORD"


@pytest.mark.parametrize("password_env", ["", "   "])
@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (RawGlobalBackupConfig, {}),
        (RawJobBackupSectionConfig, {}),
        (RawBackupConfig, {"repository": "/backups/test"}),
    ],
)
def test_restic_password_env_rejects_empty_or_blank_names(
    model_cls: type[RawGlobalBackupConfig | RawJobBackupSectionConfig | RawBackupConfig],
    payload: dict[str, object],
    password_env: str,
) -> None:
    with pytest.raises(ValidationError, match="environment variable name must not be empty"):
        model_cls.model_validate({**payload, "password_env": password_env})


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        (
            "username_env",
            {
                "host": "smtp.example.test",
                "username_env": "SMTP_USER",
                "password_env": "SMTP_PASS",
                "from_addr": "dk@example.test",
                "to": ["admin@example.test"],
            },
        ),
        (
            "password_env",
            {
                "host": "smtp.example.test",
                "username_env": "SMTP_USER",
                "password_env": "SMTP_PASS",
                "from_addr": "dk@example.test",
                "to": ["admin@example.test"],
            },
        ),
    ],
)
@pytest.mark.parametrize("env_name", ["", "   "])
def test_mail_notification_env_fields_reject_empty_or_blank_names(
    field: str, payload: dict[str, object], env_name: str
) -> None:
    with pytest.raises(ValidationError, match="environment variable name must not be empty"):
        RawMailNotificationConfig.model_validate({**payload, field: env_name})


@pytest.mark.parametrize("field", ["token_env", "user_key_env"])
@pytest.mark.parametrize("env_name", ["", "   "])
def test_pushover_notification_env_fields_reject_empty_or_blank_names(
    field: str, env_name: str
) -> None:
    with pytest.raises(ValidationError, match="environment variable name must not be empty"):
        RawPushoverNotificationConfig.model_validate(
            {"token_env": "PUSHOVER_TOKEN", "user_key_env": "PUSHOVER_USER_KEY", field: env_name}
        )


def test_global_backup_rejects_backend_defaults() -> None:
    with pytest.raises(ValidationError):
        RawGlobalBackupConfig.model_validate({"backend": "borg"})


def test_global_notifications_reject_notify_triggers() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawAppConfig.model_validate(
            {
                "global": {
                    "notifications": {"notify_on_success": True},
                }
            }
        )


def test_provider_events_route_channels_independently() -> None:
    cfg = RawGlobalNotificationsConfig.model_validate(
        {
            "mail": {
                "host": "smtp.example.test",
                "from_addr": "dk@example.test",
                "to": ["admin@example.test"],
                "events": ["success", "error", "skipped", "report"],
            },
            "pushover": {
                "token_env": "PUSHOVER_TOKEN",
                "user_key_env": "PUSHOVER_USER_KEY",
                "events": ["error"],
            },
        }
    )

    assert cfg.mail is not None
    assert cfg.mail.events == ["success", "error", "skipped", "report"]
    assert cfg.pushover is not None
    assert cfg.pushover.events == ["error"]


def test_provider_events_default_to_unset() -> None:
    cfg = RawGlobalNotificationsConfig.model_validate(
        {
            "mail": {
                "host": "smtp.example.test",
                "from_addr": "dk@example.test",
                "to": ["admin@example.test"],
            }
        }
    )

    assert cfg.mail is not None
    assert cfg.mail.events is None


@pytest.mark.parametrize(
    ("events", "match"),
    [
        (["nope"], "Input should be"),
        ([], "at least 1 item"),
    ],
)
def test_provider_events_reject_invalid_values(events: list[str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RawGlobalNotificationsConfig.model_validate(
            {
                "mail": {
                    "host": "smtp.example.test",
                    "from_addr": "dk@example.test",
                    "to": ["admin@example.test"],
                    "events": events,
                }
            }
        )


@pytest.mark.parametrize("value", ["", "   "])
def test_global_notification_report_schedule_empty_string_coerces_to_none(
    value: str,
) -> None:
    cfg = RawGlobalNotificationsConfig.model_validate({"report_schedule": value})

    assert cfg.report_schedule is None


def test_global_notification_report_schedule_accepts_valid_cron() -> None:
    cfg = RawGlobalNotificationsConfig.model_validate({"report_schedule": "0 8 * * *"})

    assert cfg.report_schedule == "0 8 * * *"


@pytest.mark.parametrize("value", ["0 8 * *", "not a cron expression"])
def test_global_notification_report_schedule_rejects_invalid_cron(value: str) -> None:
    with pytest.raises(ValidationError, match="Invalid cron expression"):
        RawGlobalNotificationsConfig.model_validate({"report_schedule": value})


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (RawGlobalNotificationsConfig, {"report_schedule": "0 0 30 2 *"}),
        (RawBackupConfig, {"repository": "/backups/test", "schedule": "0 0 30 2 *"}),
        (RawWorkflowConfig, {"steps": ["backup.local"], "schedule": "0 0 30 2 *"}),
        (
            RawRcloneSyncTaskConfig,
            {"source": "/backups/test", "target": "remote:test", "schedule": "0 0 30 2 *"},
        ),
    ],
)
def test_cron_schedules_reject_unreachable_dates(
    model_cls: type[
        RawGlobalNotificationsConfig | RawBackupConfig | RawWorkflowConfig | RawRcloneSyncTaskConfig
    ],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="no reachable next run"):
        model_cls.model_validate(payload)


def test_keep_within_empty_string_coerces_to_none() -> None:
    cfg = RawGlobalBackupConfig.model_validate({"keep_within": ""})
    assert cfg.keep_within is None


def test_keep_within_granular_empty_string_coerces_to_none() -> None:
    cfg = RawGlobalBackupConfig.model_validate({"keep_within_daily": ""})
    assert cfg.keep_within_daily is None


def test_keep_within_invalid_format_still_raises() -> None:
    with pytest.raises(ValidationError, match="Invalid keep_within format"):
        RawGlobalBackupConfig.model_validate({"keep_within": "not-a-duration"})


@pytest.mark.parametrize(
    "value",
    [
        "5s",
        "2w3d",
        "1M",
    ],
)
@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (RawGlobalBackupConfig, {}),
        (RawJobBackupSectionConfig, {}),
        (RawBackupConfig, {"repository": "/backups/test"}),
    ],
)
@pytest.mark.parametrize(
    "field_name",
    [
        "keep_within",
        "keep_within_hourly",
        "keep_within_daily",
        "keep_within_weekly",
        "keep_within_monthly",
        "keep_within_yearly",
    ],
)
def test_keep_within_rejects_restic_invalid_units(
    model_cls: type[RawGlobalBackupConfig | RawJobBackupSectionConfig | RawBackupConfig],
    payload: dict[str, object],
    value: str,
    field_name: str,
) -> None:
    with pytest.raises(ValidationError, match="Invalid keep_within format"):
        model_cls.model_validate({**payload, field_name: value})


@pytest.mark.parametrize(
    "value",
    [
        "7d",
        "2m3d",
        "1y2m3d4h",
    ],
)
def test_keep_within_accepts_restic_valid_units(value: str) -> None:
    cfg = RawGlobalBackupConfig.model_validate({"keep_within": value})

    assert cfg.keep_within == value


def test_keep_within_granular_accepts_restic_valid_units() -> None:
    cfg = RawBackupConfig.model_validate(
        {
            "repository": "/backups/test",
            "keep_within_hourly": "7d",
            "keep_within_daily": "1m",
            "keep_within_weekly": "1y",
            "keep_within_monthly": "5y",
            "keep_within_yearly": "75y",
        }
    )

    assert cfg.keep_within_hourly == "7d"
    assert cfg.keep_within_daily == "1m"
    assert cfg.keep_within_weekly == "1y"
    assert cfg.keep_within_monthly == "5y"
    assert cfg.keep_within_yearly == "75y"


@pytest.mark.parametrize(
    "value",
    [
        "1.5M",
        "0.625M",
        "off",
        "10M:off",
        "08:00,512k 12:00,off",
    ],
)
@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (RawGlobalRcloneConfig, {}),
        (RawJobRcloneSectionConfig, {}),
        (RawRcloneSyncTaskConfig, {"source": "/src", "target": "remote:dst"}),
    ],
)
def test_bwlimit_accepts_rclone_valid_values(
    model_cls: type[RawGlobalRcloneConfig | RawJobRcloneSectionConfig | RawRcloneSyncTaskConfig],
    payload: dict[str, object],
    value: str,
) -> None:
    cfg = model_cls.model_validate({**payload, "bwlimit": value})

    assert cfg.bwlimit == value


@pytest.mark.parametrize("value", ["fast", "10Q", "24:00,1M", "08:00,"])
def test_bwlimit_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValidationError, match="Invalid bwlimit format"):
        RawGlobalRcloneConfig.model_validate({"bwlimit": value})


def test_restic_extra_args_validate_entries() -> None:
    cfg = RawGlobalBackupConfig.model_validate({"extra_restic_backup_args": ["--tag daily"]})
    assert cfg.extra_restic_backup_args == ["--tag daily"]

    with pytest.raises(ValidationError, match="Extra args entry must not be empty"):
        RawGlobalBackupConfig.model_validate({"extra_restic_backup_args": ["   "]})


def test_backup_config_rejects_restic_submodel() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawBackupConfig.model_validate(
            {"repository": "/backups/test", "restic": {"extra_backup_args": ["--verbose"]}}
        )


def test_backup_config_rejects_rclone_timeout() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawBackupConfig.model_validate({"repository": "/backups/test", "rclone_timeout": 30})


@pytest.mark.parametrize(
    "field",
    ["password", "password_env", "password_file", "restic", "backup_timeout", "rclone_timeout"],
)
def test_removed_global_top_level_fields_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawGlobalConfig.model_validate({field: "legacy"})


def test_job_config_has_container_sections_and_job_hooks() -> None:
    cfg = RawJobConfig.model_validate(
        raw_job(
            backup_defaults={"backup_timeout": 600, "retention": True},
            rclone_defaults={"rclone_timeout": 300},
            rclone_tasks={"offsite": raw_rclone_task()},
            workflows={"daily": {"steps": ["backup.local", "rclone.offsite"]}},
            overrides={"pre_hooks": ["/scripts/pre-job.sh"]},
        )
    )

    assert cfg.backup.backup_timeout == 600
    assert cfg.backup.retention is True
    assert cfg.backup.tasks["local"].sources == ["/data"]
    assert cfg.rclone.rclone_timeout == 300
    assert cfg.rclone.tasks["offsite"].target == "remote:repo"
    assert cfg.workflow["daily"].steps == ["backup.local", "rclone.offsite"]
    assert cfg.pre_hooks == ["/scripts/pre-job.sh"]


@pytest.mark.parametrize(
    "field",
    [
        "backend",
        "sources",
        "source_files",
        "exclude",
        "exclude_files",
        "retention",
        "cleanup",
        "restic",
        "transfers",
        "rclone_extra_args",
        "workflows",
    ],
)
def test_removed_job_level_fields_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawJobConfig.model_validate({field: "legacy"})


def test_job_backup_section_parses_dynamic_tasks() -> None:
    cfg = RawJobBackupSectionConfig.model_validate(
        {"exclude": ["*.tmp"], "local": raw_backup_task()}
    )

    assert cfg.exclude == ["*.tmp"]
    assert cfg.tasks["local"].repository == "/repo"


def test_job_rclone_section_parses_dynamic_tasks() -> None:
    cfg = RawJobRcloneSectionConfig.model_validate({"transfers": 4, "offsite": raw_rclone_task()})

    assert cfg.transfers == 4
    assert cfg.tasks["offsite"].target == "remote:repo"


@pytest.mark.parametrize(
    ("section_cls", "payload"),
    [
        (
            RawJobBackupSectionConfig,
            {"tasks": {"local": raw_backup_task()}},
        ),
        (
            RawJobRcloneSectionConfig,
            {"tasks": {"offsite": raw_rclone_task()}},
        ),
    ],
)
def test_job_task_sections_reject_explicit_tasks_input(
    section_cls: type[RawJobBackupSectionConfig | RawJobRcloneSectionConfig],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="tasks is internal"):
        section_cls.model_validate(payload)


def test_container_rejects_unknown_scalar_defaults() -> None:
    with pytest.raises(ValidationError, match="Unknown field"):
        RawJobBackupSectionConfig.model_validate({"sources": ["/data"]})


@pytest.mark.parametrize("field", ["backend", "source_files"])
def test_job_backup_section_rejects_task_only_or_task_backend_defaults(field: str) -> None:
    with pytest.raises(ValidationError, match="Unknown field"):
        RawJobBackupSectionConfig.model_validate({field: "legacy"})


@pytest.mark.parametrize("field", ["extra_args", "rclone_extra_args"])
def test_job_rclone_section_rejects_old_extra_arg_defaults(field: str) -> None:
    with pytest.raises(ValidationError, match="Unknown field"):
        RawJobRcloneSectionConfig.model_validate({field: ["--fast-list"]})


def test_global_rclone_rejects_old_extra_args() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawGlobalConfig.model_validate({"rclone": {"extra_args": ["--fast-list"]}})


def test_workflow_steps_use_target_tokens() -> None:
    cfg = RawWorkflowConfig.model_validate(
        {
            "steps": [
                "backup.local",
                "backup.local.retention",
                "backup.local.cleanup",
                "rclone.offsite",
            ]
        }
    )
    assert cfg.steps == [
        "backup.local",
        "backup.local.retention",
        "backup.local.cleanup",
        "rclone.offsite",
    ]


@pytest.mark.parametrize(
    "step",
    ["backup.local." + "for" + "get", "backup.local." + "pr" + "une"],
)
def test_workflow_steps_reject_removed_tokens(step: str) -> None:
    with pytest.raises(ValidationError, match="Invalid step format"):
        RawWorkflowConfig.model_validate({"steps": [step]})


def test_app_config_accepts_target_toml_shape() -> None:
    cfg = RawAppConfig.model_validate(
        raw_app(
            global_config={
                "backup": {
                    "backup_timeout": 600,
                    "retention": True,
                    "cleanup": False,
                    "extra_restic_backup_args": ["--verbose"],
                },
            },
            jobs={"docs": raw_job()},
        )
    )

    assert cfg.global_.backup.retention is True
    assert cfg.global_.backup.extra_restic_backup_args == ["--verbose"]
    assert cfg.jobs["docs"].backup.tasks["local"].repository == "/repo"


@pytest.mark.parametrize("job_name", ["_system", "__dockkeep_adhoc_restore__"])
def test_app_config_rejects_reserved_job_names(job_name: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        RawAppConfig.model_validate(raw_app(jobs={job_name: raw_job()}))


def test_example_config_file_is_raw_model_contract() -> None:
    cfg = example_raw_app_config()

    assert "full_job" in cfg.jobs
    assert cfg.jobs["full_job"].backup.tasks["local"].repository == "/backups/full_job/local"
    assert cfg.jobs["full_job"].rclone.tasks["offsite"].target == "backup_remote:full_job/local"


def test_example_config_file_parse_validate_and_resolve_contract(tmp_path: Path) -> None:
    cfg = RawAppConfig.model_validate(example_config_data_with_existing_files(tmp_path))

    validate_raw_config(cfg)
    resolved = resolve_config(cfg)
    validate_resolved_config(resolved)

    full_job = resolved.jobs["full_job"]
    local = full_job.backup["local"]
    remote = full_job.backup["remote"]
    offsite = full_job.rclone["offsite"]
    daily = full_job.workflows["daily"]
    maintenance = full_job.workflows["maintenance"]

    assert local.repository == "/backups/full_job/local"
    assert local.execution.retention is True
    assert local.execution.cleanup is True
    assert local.retention.keep_last == 14
    assert remote.input.sources == []
    assert remote.execution.retention is True
    assert remote.execution.cleanup is False
    assert offsite.target == "backup_remote:full_job/local"
    assert daily.timeouts.hook_timeout == 120
    assert maintenance.timeouts.hook_timeout == 600
    assert full_job.hooks.pre_hooks == ["/scripts/pre-job.sh"]
    assert local.hooks.pre_hooks == ["/scripts/pre-local.sh"]
    assert offsite.hooks.pre_hooks == ["/scripts/pre-rclone-offsite.sh"]


def test_rclone_sync_config_still_uses_rclone_target_fields() -> None:
    cfg = RawRcloneSyncTaskConfig.model_validate(
        {
            "source": "/repo",
            "target": "remote:path",
            "extra_rclone_args": ["--fast-list"],
            "rclone_timeout": 30,
        }
    )
    assert cfg.extra_rclone_args == ["--fast-list"]
    assert cfg.rclone_timeout == 30


def test_rclone_sync_config_rejects_remote_and_old_extra_args() -> None:
    with pytest.raises(ValidationError, match="target"):
        RawRcloneSyncTaskConfig.model_validate(
            {"source": "/repo", "remote": "remote:path", "extra_args": ["--fast-list"]}
        )


def test_rclone_sync_config_rejects_remote_even_with_target() -> None:
    with pytest.raises(ValidationError, match="remote"):
        RawRcloneSyncTaskConfig.model_validate(
            {"source": "/repo", "target": "remote:path", "remote": "legacy:path"}
        )


def test_rclone_sync_config_rejects_old_extra_args_with_valid_target() -> None:
    with pytest.raises(ValidationError, match="extra_args"):
        RawRcloneSyncTaskConfig.model_validate(
            {"source": "/repo", "target": "remote:path", "extra_args": ["--fast-list"]}
        )
