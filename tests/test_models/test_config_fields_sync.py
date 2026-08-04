from collections.abc import Iterable

import pytest

from src.models.config import (
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
    StrictConfigModel,
)
from src.models.config_fields import CONFIG_FIELDS
from src.services.config_editor_schema import (
    BACKUP_FIELDS,
    GLOBAL_BACKUP_FIELDS,
    GLOBAL_FIELDS,
    GLOBAL_NOTIFICATION_FIELDS,
    GLOBAL_RCLONE_FIELDS,
    JOB_BACKUP_DEFAULTS_FIELDS,
    JOB_FIELDS,
    JOB_RCLONE_DEFAULTS_FIELDS,
    MAIL_FIELDS,
    PUSHOVER_FIELDS,
    RCLONE_FIELDS,
    WORKFLOW_FIELDS,
    EditorField,
)

type RawModel = type[StrictConfigModel]

_MODEL_BY_LEVEL: dict[str, RawModel] = {
    "global": RawGlobalConfig,
    "global.backup": RawGlobalBackupConfig,
    "global.rclone": RawGlobalRcloneConfig,
    "global.notifications": RawGlobalNotificationsConfig,
    "job": RawJobConfig,
    "job.backup": RawJobBackupSectionConfig,
    "job.rclone": RawJobRcloneSectionConfig,
    "backup": RawBackupConfig,
    "workflow": RawWorkflowConfig,
    "rclone": RawRcloneSyncTaskConfig,
}


def _model_field_names(model: RawModel) -> set[str]:
    return set(model.model_fields)


def _editor_keys(fields: Iterable[EditorField]) -> set[str]:
    return {field.key for field in fields}


def test_config_field_keys_exist_on_raw_models() -> None:
    violations: list[str] = []

    for field in CONFIG_FIELDS:
        model = _MODEL_BY_LEVEL.get(field.level)
        if model is not None and field.key not in _model_field_names(model):
            violations.append(f"{field.level}.{field.key} missing on {model.__name__}")

        parent_model = _MODEL_BY_LEVEL.get(field.parent_level or "")
        parent_key = field.parent_key or field.key
        if (
            field.inheritance != "none"
            and parent_model is not None
            and parent_key not in _model_field_names(parent_model)
        ):
            violations.append(
                f"{field.parent_level}.{parent_key} missing on {parent_model.__name__}"
            )

    assert not violations, "CONFIG_FIELDS references unknown Raw model fields:\n" + "\n".join(
        violations
    )


@pytest.mark.parametrize(
    ("name", "fields", "model"),
    [
        ("global", GLOBAL_FIELDS, RawGlobalConfig),
        ("global.notifications", GLOBAL_NOTIFICATION_FIELDS, RawGlobalNotificationsConfig),
        ("global.backup", GLOBAL_BACKUP_FIELDS, RawGlobalBackupConfig),
        ("global.rclone", GLOBAL_RCLONE_FIELDS, RawGlobalRcloneConfig),
        ("global.notifications.mail", MAIL_FIELDS, RawMailNotificationConfig),
        ("global.notifications.pushover", PUSHOVER_FIELDS, RawPushoverNotificationConfig),
        ("job", JOB_FIELDS, RawJobConfig),
        ("job.backup", JOB_BACKUP_DEFAULTS_FIELDS, RawJobBackupSectionConfig),
        ("job.rclone", JOB_RCLONE_DEFAULTS_FIELDS, RawJobRcloneSectionConfig),
        ("backup", BACKUP_FIELDS, RawBackupConfig),
        ("workflow", WORKFLOW_FIELDS, RawWorkflowConfig),
        ("rclone", RCLONE_FIELDS, RawRcloneSyncTaskConfig),
    ],
)
def test_editor_schema_keys_exist_on_raw_models(
    name: str,
    fields: tuple[EditorField, ...],
    model: RawModel,
) -> None:
    unknown_keys = _editor_keys(fields) - _model_field_names(model)

    assert not unknown_keys, f"{name} editor fields missing on {model.__name__}: {unknown_keys}"
