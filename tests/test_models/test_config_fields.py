from src.models.config_fields import (
    CONFIG_FIELDS,
    ConfigField,
    fields_for_level,
)

_VALID_KINDS = {"scalar", "list", "password_choice", "backend_option", "capability"}
_VALID_INHERITANCE = {"none", "scalar_override", "list_override", "password_choice"}
_VALID_LEVELS = {
    "global",
    "global.backup",
    "global.rclone",
    "global.notifications",
    "job",
    "job.backup",
    "job.rclone",
    "backup",
    "workflow",
    "rclone",
}


def test_schema_is_non_empty() -> None:
    assert CONFIG_FIELDS


def test_all_fields_have_valid_kind_inheritance_and_levels() -> None:
    for field in CONFIG_FIELDS:
        assert field.kind in _VALID_KINDS
        assert field.inheritance in _VALID_INHERITANCE
        assert field.level in _VALID_LEVELS
        if field.parent_level is not None:
            assert field.parent_level in _VALID_LEVELS


def test_inheritance_parent_level_invariants() -> None:
    for field in CONFIG_FIELDS:
        if field.inheritance == "none":
            assert field.parent_level is None
        else:
            assert field.parent_level is not None


def test_list_fields_default_to_empty_tuple() -> None:
    for field in CONFIG_FIELDS:
        if field.kind == "list":
            assert field.default == ()
            assert field.inheritance in {"list_override", "none"}


def test_schema_contains_no_removed_raw_keys_or_levels() -> None:
    removed_keys = {"remote", "rclone_extra_args", "extra_backup_args"}
    assert "global.backup.restic" not in {field.level for field in CONFIG_FIELDS}
    assert not removed_keys & {field.key for field in CONFIG_FIELDS}
    assert not any(
        field.level == "global.notifications" and field.key.startswith("notify_on_")
        for field in CONFIG_FIELDS
    )
    assert not any(
        field.level == "global.notifications" and field.key == "report_schedule"
        for field in CONFIG_FIELDS
    )


def test_fields_for_level_filters_correctly() -> None:
    backup_fields = fields_for_level("backup")
    assert backup_fields
    assert all(field.level == "backup" for field in backup_fields)


def test_config_field_is_frozen() -> None:
    field = CONFIG_FIELDS[0]
    try:
        field.key = "changed"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ConfigField should be immutable")


def test_config_field_instances_are_configfield() -> None:
    assert all(isinstance(field, ConfigField) for field in CONFIG_FIELDS)
