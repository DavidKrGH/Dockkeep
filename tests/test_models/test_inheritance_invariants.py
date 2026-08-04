from collections.abc import Mapping
from typing import Any

import pytest

from src.models.config import RawAppConfig
from src.models.resolve import resolve_config


def _base_backup(**extra: Any) -> dict[str, Any]:
    backup = {"repository": "/repo", "sources": ["/data"], "password": "x"}
    backup.update(extra)
    return backup


def _resolve(config: Mapping[str, Any]) -> Any:
    raw = RawAppConfig.model_validate(config)
    return resolve_config(raw)


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current[part]
        else:
            current = getattr(current, part)
    return current


@pytest.mark.parametrize(
    ("name", "config", "path", "expected"),
    [
        pytest.param(
            "hook_timeout erbt global->backup",
            {
                "global": {"hook_timeout": 100},
                "jobs": {"j": {"backup": {"b": _base_backup()}}},
            },
            "jobs.j.backup.b.timeouts.hook_timeout",
            100,
            id="hook-timeout-inherits-global-to-backup",
        ),
        pytest.param(
            "hook_timeout job override",
            {
                "global": {"hook_timeout": 100},
                "jobs": {"j": {"hook_timeout": 50, "backup": {"b": _base_backup()}}},
            },
            "jobs.j.backup.b.timeouts.hook_timeout",
            50,
            id="hook-timeout-job-override",
        ),
        pytest.param(
            "backup_timeout erbt global.backup->backup",
            {
                "global": {"backup": {"backup_timeout": 900}},
                "jobs": {"j": {"backup": {"b": _base_backup()}}},
            },
            "jobs.j.backup.b.timeouts.backup_timeout",
            900,
            id="backup-timeout-inherits-global-backup-to-backup",
        ),
        pytest.param(
            "backup_timeout job.backup override",
            {
                "global": {"backup": {"backup_timeout": 900}},
                "jobs": {"j": {"backup": {"backup_timeout": 300, "b": _base_backup()}}},
            },
            "jobs.j.backup.b.timeouts.backup_timeout",
            300,
            id="backup-timeout-job-backup-override",
        ),
        pytest.param(
            "extra_backup_args erben",
            {
                "global": {"backup": {"extra_restic_backup_args": ["--foo bar"]}},
                "jobs": {"j": {"backup": {"b": _base_backup()}}},
            },
            "jobs.j.backup.b.backend_options.restic.extra_backup_args",
            ["--foo bar"],
            id="extra-backup-args-inherit",
        ),
        pytest.param(
            "extra_backup_args [] stoppt",
            {
                "global": {"backup": {"extra_restic_backup_args": ["--foo bar"]}},
                "jobs": {"j": {"backup": {"b": _base_backup(extra_restic_backup_args=[])}}},
            },
            "jobs.j.backup.b.backend_options.restic.extra_backup_args",
            [],
            id="extra-backup-args-empty-list-stops-inheritance",
        ),
        pytest.param(
            "notify_on_error erbt global->backup",
            {
                "global": {"notify_on_error": True},
                "jobs": {"j": {"backup": {"b": _base_backup()}}},
            },
            "jobs.j.backup.b.notifications.notify_on_error",
            True,
            id="notify-on-failure-inherits-global-to-backup",
        ),
        pytest.param(
            "notify_on_error false stoppt",
            {
                "global": {"notify_on_error": True},
                "jobs": {
                    "j": {
                        "notify_on_error": False,
                        "backup": {"b": _base_backup()},
                    }
                },
            },
            "jobs.j.backup.b.notifications.notify_on_error",
            False,
            id="notify-on-failure-false-stops-inheritance",
        ),
        pytest.param(
            "rclone transfers erben",
            {
                "global": {
                    "rclone": {
                        "transfers": 8,
                        "extra_rclone_args": ["--fast-list"],
                    }
                },
                "jobs": {"j": {"rclone": {"t": {"source": "/src", "target": "remote:dst"}}}},
            },
            "jobs.j.rclone.t.options.transfers",
            8,
            id="rclone-transfers-inherit",
        ),
        pytest.param(
            "rclone extra_args erben",
            {
                "global": {
                    "rclone": {
                        "transfers": 8,
                        "extra_rclone_args": ["--fast-list"],
                    }
                },
                "jobs": {"j": {"rclone": {"t": {"source": "/src", "target": "remote:dst"}}}},
            },
            "jobs.j.rclone.t.options.extra_args",
            ["--fast-list"],
            id="rclone-extra-args-inherit",
        ),
        pytest.param(
            "rclone extra_args [] stoppt",
            {
                "global": {"rclone": {"extra_rclone_args": ["--fast-list"]}},
                "jobs": {
                    "j": {
                        "rclone": {
                            "t": {
                                "source": "/src",
                                "target": "remote:dst",
                                "extra_rclone_args": [],
                            }
                        }
                    }
                },
            },
            "jobs.j.rclone.t.options.extra_args",
            [],
            id="rclone-extra-args-empty-list-stops-inheritance",
        ),
        pytest.param(
            "rclone schedule keine Vererbung",
            {"jobs": {"j": {"rclone": {"t": {"source": "/src", "target": "remote:dst"}}}}},
            "jobs.j.rclone.t.schedule",
            None,
            id="rclone-schedule-does-not-inherit",
        ),
        pytest.param(
            "sources=[] gueltig",
            {
                "jobs": {
                    "j": {
                        "backup": {
                            "b": {
                                "repository": "/repo",
                                "sources": [],
                                "password": "x",
                            }
                        }
                    }
                }
            },
            "jobs.j.backup.b.input.sources",
            [],
            id="empty-sources-are-valid",
        ),
        pytest.param(
            "rclone_timeout erbt global.rclone->task",
            {
                "global": {"rclone": {"rclone_timeout": 1200}},
                "jobs": {"j": {"rclone": {"t": {"source": "/src", "target": "remote:dst"}}}},
            },
            "jobs.j.rclone.t.timeouts.rclone_timeout",
            1200,
            id="rclone-timeout-inherits-global-rclone-to-task",
        ),
        pytest.param(
            "sync_delete erbt global.rclone->task",
            {
                "global": {"rclone": {"sync_delete": True}},
                "jobs": {"j": {"rclone": {"t": {"source": "/src", "target": "remote:dst"}}}},
            },
            "jobs.j.rclone.t.sync_delete",
            True,
            id="sync-delete-inherits-global-rclone-to-task",
        ),
    ],
)
def test_documented_inheritance_invariants(
    name: str,
    config: Mapping[str, Any],
    path: str,
    expected: Any,
) -> None:
    resolved = _resolve(config)

    assert _get_path(resolved, path) == expected, name


def test_password_env_resolves_to_password_and_keeps_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_TEST_PW", "secret123")

    resolved = _resolve(
        {
            "jobs": {
                "j": {
                    "backup": {
                        "b": {
                            "repository": "/repo",
                            "sources": ["/data"],
                            "password_env": "DK_TEST_PW",
                        }
                    }
                }
            }
        }
    )

    credentials = resolved.jobs["j"].backup["b"].credentials
    assert credentials.password == "secret123"
    assert credentials.password_env == "DK_TEST_PW"


def test_child_password_env_replaces_parent_password_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_TEST_PW", "secret123")

    resolved = _resolve(
        {
            "global": {"backup": {"password": "globalpw"}},
            "jobs": {
                "j": {
                    "backup": {
                        "b": {
                            "repository": "/repo",
                            "sources": ["/data"],
                            "password_env": "DK_TEST_PW",
                        }
                    }
                }
            },
        }
    )

    credentials = resolved.jobs["j"].backup["b"].credentials
    assert credentials.password == "secret123"
    assert credentials.password_env == "DK_TEST_PW"
    assert credentials.password_file is None


def test_resolve_config_does_not_mutate_raw_config() -> None:
    raw = RawAppConfig.model_validate(
        {
            "global": {"backup": {"extra_restic_backup_args": ["--foo"]}},
            "jobs": {"j": {"backup": {"b": _base_backup()}}},
        }
    )
    before = raw.model_dump()

    resolve_config(raw)

    assert raw.model_dump() == before
