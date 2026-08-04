import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.models.config import RawAppConfig
from src.models.resolve import resolve_config
from src.models.resolved_config import ResolvedAppConfig
from src.utils.validation import (
    collect_config_warnings,
    load_config,
    load_raw_config,
    validate_raw_config,
    validate_resolved_config,
)
from tests.config_builders import (
    raw_app,
    raw_app_config,
    raw_backup_task,
    raw_job,
    raw_rclone_task,
    raw_workflow,
    resolved_app_config,
)


@pytest.mark.parametrize(
    "global_config",
    [
        {"lock_retry_delay": 1},
        {"lock_retry_count": 0},
        {"lock_retry_count": 0, "lock_retry_delay": 1},
        {"lock_retry_count": 1, "lock_retry_delay": 1},
    ],
)
def test_lock_retry_resolved_config_valid_combinations(global_config: dict[str, int]) -> None:
    cfg = resolve_config(raw_app_config(raw_app(global_config=global_config)))

    validate_resolved_config(cfg)


def test_lock_retry_count_requires_delay_in_resolved_config() -> None:
    cfg = resolve_config(raw_app_config(raw_app(global_config={"lock_retry_count": 1})))

    with pytest.raises(ValueError, match="lock_retry_count >= 1 requires lock_retry_delay"):
        validate_resolved_config(cfg)


def _raw(data: dict[str, object]) -> RawAppConfig:
    return raw_app_config(data)


def _resolved(data: dict[str, object]) -> ResolvedAppConfig:
    return resolved_app_config(data)


def _minimal_config(*, sources: list[str] | None = None) -> dict[str, object]:
    return raw_app(
        jobs={
            "demo": raw_job(
                backup_tasks={
                    "local": raw_backup_task(
                        include_sources=sources is not None,
                        sources=sources,
                        overrides={"password": "secret"},
                    )
                }
            )
        }
    )


class TestLoadRawConfig:
    def test_load_raw_config_returns_unresolved_raw_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[global.backup]
retention = true
keep_daily = 7

[jobs.docs.backup.local]
repository = "/backups/docs"
sources = ["/data/docs"]
""",
            encoding="utf-8",
        )

        cfg = load_raw_config(config_file)

        assert isinstance(cfg, RawAppConfig)
        assert cfg.global_.backup.retention is True
        assert cfg.jobs["docs"].backup.retention is None
        assert cfg.jobs["docs"].backup.tasks["local"].retention is None
        assert cfg.jobs["docs"].backup.tasks["local"].sources == ["/data/docs"]

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_raw_config(tmp_path / "missing.toml")

    def test_invalid_toml_raises_decode_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("this is not = valid toml [[[", encoding="utf-8")

        with pytest.raises(tomllib.TOMLDecodeError):
            load_raw_config(config_file)

    def test_raw_phase_allows_backup_without_effective_sources(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
""",
            encoding="utf-8",
        )

        cfg = load_raw_config(config_file)

        assert cfg.jobs["demo"].backup.tasks["local"].sources is None


class TestLoadConfig:
    def test_load_config_returns_resolved_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[global.backup]
retention = true
keep_daily = 7
exclude = ["*.tmp"]

[jobs.docs.backup.local]
repository = "/backups/docs"
sources = ["/data/docs"]
""",
            encoding="utf-8",
        )

        cfg = load_config(config_file)

        assert isinstance(cfg, ResolvedAppConfig)
        backup = cfg.jobs["docs"].backup["local"]
        assert backup.execution.retention is True
        assert backup.retention.keep_daily == 7
        assert backup.input.sources == ["/data/docs"]
        assert backup.filters.exclude == ["*.tmp"]

    def test_load_config_allows_missing_inputs_with_warning(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[jobs.demo.backup.local]
repository = "/repo"
password = "secret"
""",
            encoding="utf-8",
        )

        cfg = load_config(config_file)

        assert collect_config_warnings(cfg) == [
            "Job 'demo', backup 'local': no backup inputs configured. "
            "Set 'sources' or 'source_files', otherwise Restic will only report "
            "the error at run time."
        ]


class TestValidateRawConfig:
    def test_relative_repository_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "local": raw_backup_task(
                                repository="relative",
                                include_sources=False,
                            )
                        }
                    )
                }
            )
        )

        with pytest.raises(
            ValueError,
            match=r"\[jobs\.demo\.backup\.local\]: invalid repository format",
        ):
            validate_raw_config(cfg)

    def test_rclone_repository_format_is_valid(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "cloud": raw_backup_task(
                                repository="rclone:gdrive:repo",
                                include_sources=False,
                            )
                        }
                    )
                }
            )
        )

        validate_raw_config(cfg)

    def test_repository_control_character_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "local": raw_backup_task(
                                repository="/repo\nbad",
                                include_sources=False,
                            )
                        }
                    )
                }
            )
        )

        with pytest.raises(ValueError, match="contains invalid characters"):
            validate_raw_config(cfg)

    def test_tags_control_character_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "local": raw_backup_task(
                                include_sources=False,
                                overrides={"tags": ["tag\x01with\x07control\nchars"]},
                            )
                        }
                    )
                }
            )
        )

        with pytest.raises(ValueError, match="contains invalid characters"):
            validate_raw_config(cfg)

    def test_tags_without_control_characters_are_valid(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "local": raw_backup_task(
                                include_sources=False,
                                overrides={"tags": ["nightly", "prod"]},
                            )
                        }
                    )
                }
            )
        )

        validate_raw_config(cfg)

    @pytest.mark.parametrize("source", ["relative/path", "../escape", "", "/path\x00bad"])
    def test_explicit_invalid_source_raises(self, source: str) -> None:
        cfg = _raw(_minimal_config(sources=[source]))

        with pytest.raises(ValueError):
            validate_raw_config(cfg)

    def test_explicit_absolute_sources_are_valid(self) -> None:
        cfg = _raw(_minimal_config(sources=["/data", "/mnt/archive"]))

        validate_raw_config(cfg)

    def test_old_job_level_sources_layout_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
            _raw(
                {
                    "jobs": {
                        "demo": {
                            "sources": ["relative"],
                            "backup": {"local": {"repository": "/repo", "sources": ["/data"]}},
                        }
                    }
                }
            )

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            (
                {"global": {"notifications": {"notify_on_success": True}}},
                "notify_on_success",
            ),
            (
                {
                    "jobs": {
                        "demo": {
                            "backup": {
                                "local": {
                                    "repository": "/repo",
                                    "restic": {"extra_backup_args": ["--verbose"]},
                                }
                            }
                        }
                    }
                },
                "restic",
            ),
            (
                {
                    "jobs": {
                        "demo": {
                            "rclone": {
                                "offsite": {
                                    "source": "/repo",
                                    "remote": "remote:repo",
                                }
                            }
                        }
                    }
                },
                "target|remote",
            ),
            (
                {
                    "jobs": {
                        "demo": {
                            "rclone": {
                                "offsite": {
                                    "source": "/repo",
                                    "target": "remote:repo",
                                    "extra_args": ["--fast-list"],
                                }
                            }
                        }
                    }
                },
                "extra_args",
            ),
        ],
    )
    def test_old_field_names_are_rejected_before_validation(
        self,
        payload: dict[str, object],
        match: str,
    ) -> None:
        with pytest.raises(ValidationError, match=match):
            _raw(payload)

    def test_password_file_must_be_absolute_on_each_raw_level(self, tmp_path: Path) -> None:
        global_password = tmp_path / "global-password"
        global_password.write_text("global", encoding="utf-8")
        job_password = tmp_path / "job-password"
        job_password.write_text("job", encoding="utf-8")
        cfg = _raw(
            {
                "global": {"backup": {"password_file": str(global_password)}},
                "jobs": {
                    "demo": {
                        "backup": {
                            "password_file": str(job_password),
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "password_file": "relative",
                            },
                        },
                    }
                },
            }
        )

        with pytest.raises(ValueError, match="password_file .* must be an absolute path"):
            validate_raw_config(cfg)

    def test_file_reference_fields_must_exist(self, tmp_path: Path) -> None:
        source_file = tmp_path / "sources.txt"
        source_file.write_text("/data\n", encoding="utf-8")
        exclude_file = tmp_path / "excludes.txt"
        exclude_file.write_text("*.tmp\n", encoding="utf-8")
        filter_file = tmp_path / "filter.txt"
        filter_file.write_text("- *.tmp\n", encoding="utf-8")
        cfg = _raw(
            {
                "global": {
                    "backup": {"exclude_files": [str(exclude_file)]},
                    "rclone": {"filter_from": str(filter_file)},
                },
                "jobs": {
                    "demo": {
                        "backup": {
                            "exclude_files": [str(exclude_file)],
                            "local": {
                                "repository": "/repo",
                                "source_files": [str(source_file)],
                                "exclude_files": [str(exclude_file)],
                            },
                        },
                        "rclone": {
                            "filter_from": str(filter_file),
                            "offsite": {
                                "source": "/repo",
                                "target": "remote:repo",
                                "filter_from": str(filter_file),
                            },
                        },
                    }
                },
            }
        )

        validate_raw_config(cfg)

    def test_missing_file_reference_raises(self) -> None:
        cfg = _raw(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "source_files": ["/run/secrets/missing-dk-sources"],
                            }
                        }
                    }
                }
            }
        )

        with pytest.raises(ValueError, match="does not exist or is not a file"):
            validate_raw_config(cfg)

    def test_empty_string_in_file_reference_list_raises(self) -> None:
        cfg = _raw(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "exclude_files": [""],
                            }
                        }
                    }
                }
            }
        )

        with pytest.raises(ValueError, match="must not contain empty strings"):
            validate_raw_config(cfg)

    def test_workflow_reference_to_missing_backup_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        workflows={"daily": {"steps": ["backup.missing"]}},
                    )
                }
            )
        )

        with pytest.raises(ValueError, match="references backup 'missing'"):
            validate_raw_config(cfg)

    def test_workflow_reference_to_missing_rclone_task_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        workflows={"daily": {"steps": ["rclone.missing"]}},
                    )
                }
            )
        )

        with pytest.raises(ValueError, match="references rclone task 'missing'"):
            validate_raw_config(cfg)

    def test_workflow_reference_to_existing_backup_and_rclone_is_valid(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        rclone_tasks={"offsite": raw_rclone_task()},
                        workflows={"daily": {"steps": ["backup.local", "rclone.offsite"]}},
                    )
                }
            )
        )

        validate_raw_config(cfg)

    def test_invalid_rclone_source_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={},
                        rclone_tasks={"offsite": raw_rclone_task(source="relative")},
                    )
                }
            )
        )

        with pytest.raises(ValueError, match=r"\[jobs\.demo\.rclone\.offsite\]: source"):
            validate_raw_config(cfg)

    def test_invalid_rclone_target_raises(self) -> None:
        cfg = _raw(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={},
                        rclone_tasks={"offsite": raw_rclone_task(target="relative/path")},
                    )
                }
            )
        )

        with pytest.raises(ValueError, match=r"\[jobs\.demo\.rclone\.offsite\]: target"):
            validate_raw_config(cfg)


class TestValidateResolvedConfig:
    def test_retention_without_keep_policy_raises(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "retention": True,
                            }
                        }
                    }
                }
            }
        )

        with pytest.raises(ValueError, match="retention=true requires at least one keep_\\*"):
            validate_resolved_config(cfg)

    def test_workflow_retention_step_without_keep_policy_raises(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {"repository": "/repo", "sources": ["/data"]},
                        },
                        "workflow": {"nightly": {"steps": ["backup.local.retention"]}},
                    }
                }
            }
        )

        with pytest.raises(
            ValueError, match=r"step 'backup\.local\.retention'.*no\s+keep_\* policy"
        ):
            validate_resolved_config(cfg)

    def test_workflow_retention_step_with_keep_policy_is_valid(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "keep_last": 3,
                            },
                        },
                        "workflow": {"nightly": {"steps": ["backup.local.retention"]}},
                    }
                }
            }
        )

        validate_resolved_config(cfg)

    def test_workflow_plain_backup_step_does_not_require_keep_policy(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {"repository": "/repo", "sources": ["/data"]},
                        },
                        "workflow": {"nightly": {"steps": ["backup.local.backup"]}},
                    }
                }
            }
        )

        validate_resolved_config(cfg)

    def test_retention_with_inherited_keep_policy_is_valid(self) -> None:
        cfg = _resolved(
            {
                "global": {"backup": {"retention": True, "keep_daily": 7}},
                "jobs": {
                    "demo": {
                        "backup": {"local": {"repository": "/repo", "sources": ["/data"]}},
                    }
                },
            }
        )

        validate_resolved_config(cfg)

    def test_missing_effective_inputs_is_warning_not_error(self) -> None:
        cfg = _resolved({"jobs": {"demo": {"backup": {"local": {"repository": "/repo"}}}}})

        validate_resolved_config(cfg)
        assert collect_config_warnings(cfg)[:1] == [
            "Job 'demo', backup 'local': no backup inputs configured. "
            "Set 'sources' or 'source_files', otherwise Restic will only report "
            "the error at run time."
        ]

    def test_missing_effective_credentials_emits_warning(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                            }
                        }
                    }
                }
            }
        )

        warnings = collect_config_warnings(cfg)
        assert any("no Restic password source configured" in w for w in warnings)

    def test_unresolvable_password_env_emits_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DK_TEST_MISSING_PW", raising=False)
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "password_env": "DK_TEST_MISSING_PW",
                            }
                        }
                    }
                }
            }
        )

        warnings = collect_config_warnings(cfg)
        assert any("DK_TEST_MISSING_PW" in w for w in warnings)

    def test_empty_password_env_emits_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DK_TEST_EMPTY_PW", "")
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "password_env": "DK_TEST_EMPTY_PW",
                            }
                        }
                    }
                }
            }
        )

        warnings = collect_config_warnings(cfg)
        assert any("DK_TEST_EMPTY_PW" in w for w in warnings)

    def test_resolvable_password_env_emits_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DK_TEST_PRESENT_PW", "secret")
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "sources": ["/data"],
                                "password_env": "DK_TEST_PRESENT_PW",
                            }
                        }
                    }
                }
            }
        )

        assert not any("password_env" in w for w in collect_config_warnings(cfg))

    def test_effective_password_file_must_exist(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "password_file": "/run/secrets/missing-dk-test",
                            "local": {"repository": "/repo", "sources": ["/data"]},
                        },
                    }
                }
            }
        )

        with pytest.raises(
            ValueError,
            match=r"\[jobs\.demo\.backup\.local\]: effective password_file .* "
            r"\[jobs\.demo\.backup\].*\[global\.backup\]",
        ):
            validate_resolved_config(cfg)

    def test_existing_effective_password_file_is_valid(self, tmp_path: Path) -> None:
        password_file = tmp_path / "restic-password"
        password_file.write_text("secret", encoding="utf-8")
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "password_file": str(password_file),
                            "local": {"repository": "/repo", "sources": ["/data"]},
                        },
                    }
                }
            }
        )

        validate_resolved_config(cfg)


class TestCollectConfigWarnings:
    def test_no_provider_and_default_notifications_has_no_warning(self) -> None:
        cfg = _resolved(_minimal_config(sources=["/data"]))

        assert collect_config_warnings(cfg) == []

    def test_only_source_files_has_no_missing_input_warning(self) -> None:
        cfg = _resolved(
            {
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "source_files": ["/config/sources.txt"],
                            }
                        }
                    }
                }
            }
        )

        assert collect_config_warnings(cfg) == []

    def test_missing_input_warning_precedes_provider_warning(self) -> None:
        cfg = _resolved(
            {
                "global": {"notify_on_success": True},
                "jobs": {
                    "demo": {"backup": {"local": {"repository": "/repo", "password": "secret"}}}
                },
            }
        )

        assert collect_config_warnings(cfg) == [
            "Job 'demo', backup 'local': no backup inputs configured. "
            "Set 'sources' or 'source_files', otherwise Restic will only report "
            "the error at run time.",
            "Notifications are enabled, but no provider is configured. "
            "Notification events are logged but not sent. "
            "Global triggers are configured under [global]. "
            "Configure [global.notifications.mail] or "
            "[global.notifications.pushover] to send notifications.",
        ]

    def test_any_notification_without_provider_warns(self) -> None:
        cfg = _resolved(
            {
                "global": {"notify_on_error": True},
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "sources": ["/data"],
                            }
                        }
                    }
                },
            }
        )

        assert collect_config_warnings(cfg) == [
            "Notifications are enabled, but no provider is configured. "
            "Notification events are logged but not sent. "
            "Global triggers are configured under [global]. "
            "Configure [global.notifications.mail] or "
            "[global.notifications.pushover] to send notifications."
        ]

    def test_report_schedule_without_provider_warns(self) -> None:
        cfg = _resolved(
            {
                "global": {"notifications": {"report_schedule": "0 8 * * *"}},
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "sources": ["/data"],
                            }
                        }
                    }
                },
            }
        )

        assert collect_config_warnings(cfg) == [
            "Notifications are enabled, but no provider is configured. "
            "Notification events are logged but not sent. "
            "Global triggers are configured under [global]. "
            "Configure [global.notifications.mail] or "
            "[global.notifications.pushover] to send notifications."
        ]

    def test_workflow_only_notification_trigger_without_provider_warns(self) -> None:
        cfg = _resolved(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "local": raw_backup_task(
                                sources=["/data"], overrides={"password": "secret"}
                            )
                        },
                        workflows={
                            "daily": raw_workflow(
                                steps=["backup.local"],
                                overrides={"notify_on_success": True},
                            )
                        },
                    )
                }
            )
        )

        assert collect_config_warnings(cfg) == [
            "Notifications are enabled, but no provider is configured. "
            "Notification events are logged but not sent. "
            "Global triggers are configured under [global]. "
            "Configure [global.notifications.mail] or "
            "[global.notifications.pushover] to send notifications."
        ]

    def test_rclone_only_notification_trigger_without_provider_warns(self) -> None:
        cfg = _resolved(
            raw_app(
                jobs={
                    "demo": raw_job(
                        backup_tasks={
                            "local": raw_backup_task(
                                sources=["/data"], overrides={"password": "secret"}
                            )
                        },
                        rclone_tasks={
                            "offsite": raw_rclone_task(overrides={"notify_on_success": True})
                        },
                    )
                }
            )
        )

        assert collect_config_warnings(cfg) == [
            "Notifications are enabled, but no provider is configured. "
            "Notification events are logged but not sent. "
            "Global triggers are configured under [global]. "
            "Configure [global.notifications.mail] or "
            "[global.notifications.pushover] to send notifications."
        ]

    def test_success_notification_with_provider_has_no_warning(self) -> None:
        cfg = _resolved(
            {
                "global": {
                    "notify_on_success": True,
                    "notifications": {
                        "mail": {
                            "host": "smtp.example.com",
                            "from_addr": "dk@example.com",
                            "to": ["admin@example.com"],
                        },
                    },
                },
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "sources": ["/data"],
                            }
                        }
                    }
                },
            }
        )

        assert collect_config_warnings(cfg) == []

    def test_mail_provider_warns_when_credential_env_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SMTP_USER", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        cfg = _resolved(
            {
                "global": {
                    "notify_on_success": True,
                    "notifications": {
                        "mail": {
                            "host": "smtp.example.com",
                            "username_env": "SMTP_USER",
                            "password_env": "SMTP_PASSWORD",
                            "from_addr": "dk@example.com",
                            "to": ["admin@example.com"],
                        },
                    },
                },
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "sources": ["/data"],
                            }
                        }
                    }
                },
            }
        )

        assert collect_config_warnings(cfg) == [
            "Mail notification provider: username_env 'SMTP_USER' is configured, "
            "but the environment variable was not defined or is empty when the config "
            "was loaded. Notifications will fail at send time until it is set.",
            "Mail notification provider: password_env 'SMTP_PASSWORD' is configured, "
            "but the environment variable was not defined or is empty when the config "
            "was loaded. Notifications will fail at send time until it is set.",
        ]

    def test_pushover_provider_warns_when_credential_env_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
        cfg = _resolved(
            {
                "global": {
                    "notify_on_success": True,
                    "notifications": {
                        "pushover": {
                            "token_env": "PUSHOVER_TOKEN",
                            "user_key_env": "PUSHOVER_USER_KEY",
                        },
                    },
                },
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "sources": ["/data"],
                            }
                        }
                    }
                },
            }
        )

        assert collect_config_warnings(cfg) == [
            "Pushover notification provider: token_env 'PUSHOVER_TOKEN' is configured, "
            "but the environment variable was not defined or is empty when the config "
            "was loaded. Notifications will fail at send time until it is set.",
            "Pushover notification provider: user_key_env 'PUSHOVER_USER_KEY' is configured, "
            "but the environment variable was not defined or is empty when the config "
            "was loaded. Notifications will fail at send time until it is set.",
        ]

    def test_notification_provider_credential_env_warning_accepts_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PUSHOVER_TOKEN", "token")
        monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
        cfg = _resolved(
            {
                "global": {
                    "notify_on_success": True,
                    "notifications": {
                        "pushover": {
                            "token_env": "PUSHOVER_TOKEN",
                            "user_key_env": "PUSHOVER_USER_KEY",
                        },
                    },
                },
                "jobs": {
                    "demo": {
                        "backup": {
                            "local": {
                                "repository": "/repo",
                                "password": "secret",
                                "sources": ["/data"],
                            }
                        }
                    }
                },
            }
        )

        assert collect_config_warnings(cfg) == []
