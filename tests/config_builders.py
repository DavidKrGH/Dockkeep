"""Reusable builders for the target Raw config layout used in tests."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any, cast

from src.models.config import RawAppConfig
from src.models.resolve import resolve_config
from src.models.resolved_config import ResolvedAppConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "docs" / "example_config_file.toml"


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-merged copy of ``base`` with ``overrides`` applied."""
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def raw_backup_task(
    *,
    repository: str = "/repo",
    sources: list[str] | None = None,
    include_sources: bool = True,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a concrete ``[jobs.<job>.backup.<name>]`` task dict."""
    task: dict[str, Any] = {"repository": repository}
    if include_sources:
        task["sources"] = ["/data"] if sources is None else sources
    if overrides:
        task = deep_merge(task, overrides)
    return task


def raw_rclone_task(
    *,
    source: str = "/repo",
    target: str = "remote:repo",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a concrete ``[jobs.<job>.rclone.<name>]`` task dict."""
    task: dict[str, Any] = {"source": source, "target": target}
    if overrides:
        task = deep_merge(task, overrides)
    return task


def raw_workflow(
    *,
    steps: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a concrete ``[jobs.<job>.workflow.<name>]`` dict."""
    workflow: dict[str, Any] = {"steps": ["backup.local"] if steps is None else steps}
    if overrides:
        workflow = deep_merge(workflow, overrides)
    return workflow


def raw_job(
    *,
    backup_tasks: dict[str, dict[str, Any]] | None = None,
    rclone_tasks: dict[str, dict[str, Any]] | None = None,
    workflows: dict[str, dict[str, Any]] | None = None,
    backup_defaults: dict[str, Any] | None = None,
    rclone_defaults: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``[jobs.<job>]`` dict using the new container sections."""
    job: dict[str, Any] = {}
    backup_section = copy.deepcopy(backup_defaults or {})
    if backup_tasks is None:
        backup_section.update({"local": raw_backup_task()})
    else:
        backup_section.update(copy.deepcopy(backup_tasks))
    job["backup"] = backup_section

    if rclone_defaults is not None or rclone_tasks is not None:
        rclone_section = copy.deepcopy(rclone_defaults or {})
        rclone_section.update(copy.deepcopy(rclone_tasks or {}))
        job["rclone"] = rclone_section

    if workflows is not None:
        job["workflow"] = copy.deepcopy(workflows)

    if overrides:
        job = deep_merge(job, overrides)
    return job


def raw_app(
    *,
    jobs: dict[str, dict[str, Any]] | None = None,
    global_config: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full Raw-app config dict in the target TOML shape."""
    app: dict[str, Any] = {"jobs": copy.deepcopy(jobs or {"demo": raw_job()})}
    if global_config is not None:
        app["global"] = copy.deepcopy(global_config)
    if overrides:
        app = deep_merge(app, overrides)
    return app


def raw_app_config(data: dict[str, Any] | None = None) -> RawAppConfig:
    """Validate a Raw-app config dict."""
    return RawAppConfig.model_validate(raw_app() if data is None else data)


def resolved_app_config(data: dict[str, Any] | None = None) -> ResolvedAppConfig:
    """Validate and resolve a Raw-app config dict."""
    return resolve_config(raw_app_config(data))


def example_config_data() -> dict[str, Any]:
    """Load the documented example config as the Raw contract fixture."""
    return tomllib.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))


def example_config_data_with_existing_files(tmp_path: Path) -> dict[str, Any]:
    """Load the documented example config and materialize file-reference fields."""
    data = example_config_data()
    replacements: dict[str, str] = {}

    def materialize(path_value: str) -> str:
        if not path_value.startswith("/"):
            return path_value
        if path_value not in replacements:
            fixture_path = tmp_path / path_value.lstrip("/").replace("/", "__")
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            fixture_path.write_text("# test fixture\n", encoding="utf-8")
            replacements[path_value] = str(fixture_path)
        return replacements[path_value]

    def rewrite(value: Any, key: str | None = None) -> Any:
        file_keys = {"source_files", "exclude_files", "filter_from", "password_file"}
        if isinstance(value, dict):
            return {
                child_key: rewrite(child_value, child_key)
                for child_key, child_value in value.items()
            }
        if key in {"source_files", "exclude_files"} and isinstance(value, list):
            return [materialize(item) if isinstance(item, str) else item for item in value]
        if key in file_keys and isinstance(value, str):
            return materialize(value)
        return value

    return cast(dict[str, Any], rewrite(data))


def example_raw_app_config() -> RawAppConfig:
    """Validate ``docs/example_config_file.toml`` as the Raw contract fixture."""
    return RawAppConfig.model_validate(example_config_data())
