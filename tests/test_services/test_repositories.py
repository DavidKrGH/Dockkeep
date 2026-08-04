from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.subprocesses import CommandResult
from src.services.config import ConfigService
from src.services.errors import NotFoundServiceError, ServiceError
from src.services.repositories import RepositoryService


def _write_repo_config(path: Path, repository: str = "/repo") -> None:
    path.write_text(
        f"""
[jobs.demo.backup.local]
repository = "{repository}"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )


def test_configured_repository_location_keys_covers_all_jobs_and_backups(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.job1.backup.a]
repository = "/repo-a"
sources = ["/data"]
password = "secret"

[jobs.job1.backup.b]
repository = "/repo-b"
sources = ["/data"]
password = "secret"

[jobs.job2.backup.a]
repository = "/repo-a"
sources = ["/data"]
password = "secret"
""".strip(),
        encoding="utf-8",
    )
    service = RepositoryService(ConfigService(config_path))

    keys = service.configured_repository_location_keys()

    assert keys == {"local:/repo-a", "local:/repo-b"}


def test_repository_service_reads_backend_repository_id_with_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path, "/repo/secret-value")
    service = RepositoryService(ConfigService(config_path))
    result = CommandResult(0, stdout='{"id": "repo-id", "version": 2}', stderr="")

    with patch("src.services.repositories.run_command", AsyncMock(return_value=result)) as run:
        repository_id = service.backend_repository_id("demo", "local")

    assert repository_id == "repo-id"
    assert run.await_args.args[0] == [
        "restic",
        "--repo",
        "/repo/secret-value",
        "cat",
        "config",
        "--json",
    ]


@pytest.mark.parametrize(
    "command_result",
    [
        CommandResult(2, stdout="", stderr="repo unavailable"),
        CommandResult(0, stdout="{", stderr=""),
        CommandResult(0, stdout="{}", stderr=""),
        CommandResult(0, stdout='{"id": ""}', stderr=""),
        CommandResult(0, stdout="[]", stderr=""),
    ],
)
def test_repository_service_backend_repository_id_returns_none_for_nonfatal_failures(
    tmp_path: Path, command_result: CommandResult
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))

    with patch("src.services.repositories.run_command", AsyncMock(return_value=command_result)):
        assert service.backend_repository_id("demo", "local") is None


def test_repository_service_backend_repository_id_returns_none_for_subprocess_error(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))

    with patch("src.services.repositories.run_command", AsyncMock(side_effect=OSError("missing"))):
        assert service.backend_repository_id("demo", "local") is None


def test_repository_service_delete_location_delegates_to_artifact_store(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    artifact_store = MagicMock()
    artifact_store.delete_location.return_value = True
    service = RepositoryService(ConfigService(config_path), artifact_store)

    service.delete_location("repo-id", "loc-id")

    artifact_store.delete_location.assert_called_once_with("repo-id", "loc-id")


def test_repository_service_delete_location_maps_missing_location(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    artifact_store = MagicMock()
    artifact_store.delete_location.return_value = False
    service = RepositoryService(ConfigService(config_path), artifact_store)

    with pytest.raises(NotFoundServiceError):
        service.delete_location("repo-id", "loc-id")


def test_repository_service_merge_location_delegates_to_artifact_store(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    artifact_store = MagicMock()
    artifact_store.merge_location.return_value = True
    service = RepositoryService(ConfigService(config_path), artifact_store)

    service.merge_location("repo-id", "source-loc-id", "target-loc-id")

    artifact_store.merge_location.assert_called_once_with(
        "repo-id", "source-loc-id", "target-loc-id"
    )


def test_repository_service_merge_location_maps_missing_location(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    artifact_store = MagicMock()
    artifact_store.merge_location.return_value = False
    service = RepositoryService(ConfigService(config_path), artifact_store)

    with pytest.raises(NotFoundServiceError):
        service.merge_location("repo-id", "source-loc-id", "target-loc-id")


def test_repository_service_merge_location_rejects_same_source_and_target(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    artifact_store = MagicMock()
    service = RepositoryService(ConfigService(config_path), artifact_store)

    with pytest.raises(ServiceError):
        service.merge_location("repo-id", "loc-id", "loc-id")

    artifact_store.merge_location.assert_not_called()
