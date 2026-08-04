import asyncio
import logging

import pytest
from pytest_mock import MockerFixture

from src.core.subprocesses import CommandResult
from src.utils.restic import (
    async_check_repository,
    async_ensure_repository,
    async_init_repository,
    resolve_env,
)


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def test_resolve_env_direct_password() -> None:
    env = resolve_env(password="mysecret")
    assert env["RESTIC_PASSWORD"] == "mysecret"


def test_resolve_env_no_args_returns_copy_of_os_environ() -> None:
    env = resolve_env()
    assert isinstance(env, dict)
    # Must be a copy, not the same object
    import os

    assert env is not os.environ


def test_resolve_env_includes_existing_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOME_VAR", "some_value")
    env = resolve_env(password="pw")
    assert env["SOME_VAR"] == "some_value"


def test_resolve_env_password_file() -> None:
    """resolve_env mit password_file setzt RESTIC_PASSWORD_FILE."""
    env = resolve_env(password_file="/run/secrets/restic-password")
    assert env["RESTIC_PASSWORD_FILE"] == "/run/secrets/restic-password"


def test_resolve_env_password_file_not_password() -> None:
    """Bei password_file wird RESTIC_PASSWORD NICHT gesetzt."""
    env = resolve_env(password_file="/run/secrets/restic-password")
    assert "RESTIC_PASSWORD" not in env


def test_resolve_env_password_sets_password_not_file() -> None:
    """Bei password wird RESTIC_PASSWORD_FILE NICHT gesetzt."""
    env = resolve_env(password="secret")
    assert "RESTIC_PASSWORD_FILE" not in env


def test_resolve_env_no_args_removes_inherited_restic_secret_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geerbte Restic-Secret-Variablen werden ohne konfigurierte Quelle bereinigt."""
    monkeypatch.setenv("RESTIC_PASSWORD", "inherited-password")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/run/secrets/inherited")
    monkeypatch.setenv("RESTIC_PASSWORD_COMMAND", "printf inherited")

    env = resolve_env()

    assert "RESTIC_PASSWORD" not in env
    assert "RESTIC_PASSWORD_FILE" not in env
    assert "RESTIC_PASSWORD_COMMAND" not in env


def test_resolve_env_password_overrides_inherited_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direktes Passwort setzt exakt RESTIC_PASSWORD und entfernt RESTIC_PASSWORD_FILE."""
    monkeypatch.setenv("RESTIC_PASSWORD", "inherited-password")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/run/secrets/inherited")

    env = resolve_env(password="configured-password")

    assert env["RESTIC_PASSWORD"] == "configured-password"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_resolve_env_password_file_overrides_inherited_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password-Datei setzt exakt RESTIC_PASSWORD_FILE und entfernt RESTIC_PASSWORD."""
    monkeypatch.setenv("RESTIC_PASSWORD", "inherited-password")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/run/secrets/inherited")

    env = resolve_env(password_file="/run/secrets/configured")

    assert "RESTIC_PASSWORD" not in env
    assert env["RESTIC_PASSWORD_FILE"] == "/run/secrets/configured"


@pytest.mark.anyio
async def test_async_check_repository_success(mocker: MockerFixture) -> None:
    run_mock = mocker.patch(
        "src.utils.restic.run_command",
        return_value=CommandResult(returncode=0, stdout="", stderr=""),
    )

    assert await async_check_repository("/backups/test", {"RESTIC_PASSWORD": "x"}) is True
    run_mock.assert_awaited_once_with(
        ["restic", "--repo", "/backups/test", "cat", "config"],
        env={"RESTIC_PASSWORD": "x"},
        timeout=None,
    )


@pytest.mark.anyio
async def test_async_check_repository_failure(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.restic.run_command",
        return_value=CommandResult(returncode=1, stdout="", stderr=""),
    )

    assert await async_check_repository("/backups/test", {}) is False


@pytest.mark.anyio
async def test_async_check_repository_timeout_returns_false(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=TimeoutError)

    assert await async_check_repository("/backups/test", {}, timeout=30) is False


@pytest.mark.anyio
async def test_async_check_repository_cancellation_propagates(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await async_check_repository("/backups/test", {})


@pytest.mark.anyio
async def test_async_init_repository_success(mocker: MockerFixture) -> None:
    run_mock = mocker.patch(
        "src.utils.restic.run_command",
        return_value=CommandResult(returncode=0, stdout="created\n", stderr=""),
    )

    assert await async_init_repository("/backups/test", {"RESTIC_PASSWORD": "x"}) is True
    run_mock.assert_awaited_once_with(
        ["restic", "--repo", "/backups/test", "init"],
        env={"RESTIC_PASSWORD": "x"},
        timeout=None,
    )


@pytest.mark.anyio
async def test_async_init_repository_failure(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.restic.run_command",
        return_value=CommandResult(returncode=1, stdout="", stderr="failed\n"),
    )

    assert await async_init_repository("/backups/test", {}) is False


@pytest.mark.anyio
async def test_async_init_repository_routes_stderr_to_given_logger(
    mocker: MockerFixture,
) -> None:
    """The restic init stderr must surface on the caller-supplied logger.

    This is the contract that lets the job log show the real reason an
    auto-init failed (e.g. a missing repository password) instead of only the
    generic executor error.
    """
    mocker.patch(
        "src.utils.restic.run_command",
        return_value=CommandResult(
            returncode=1, stdout="", stderr="Fatal: an empty password is not allowed\n"
        ),
    )
    job_logger = logging.getLogger("Test.BackupExecutor")
    error_mock = mocker.patch.object(job_logger, "error")

    assert await async_init_repository("/backups/test", {}, log=job_logger) is False
    logged = " ".join(str(call.args) for call in error_mock.call_args_list)
    assert "empty password" in logged


@pytest.mark.anyio
async def test_async_init_repository_timeout_returns_false(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=TimeoutError)

    assert await async_init_repository("/backups/test", {}, timeout=30) is False


@pytest.mark.anyio
async def test_async_init_repository_cancellation_propagates(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await async_init_repository("/backups/test", {})


@pytest.mark.anyio
async def test_async_ensure_repository_already_initialised(mocker: MockerFixture) -> None:
    check_mock = mocker.patch("src.utils.restic.async_check_repository", return_value=True)
    init_mock = mocker.patch("src.utils.restic.async_init_repository")

    assert await async_ensure_repository("/backups/test", {}, timeout=45) is True
    check_mock.assert_awaited_once_with("/backups/test", {}, timeout=45, log=mocker.ANY)
    init_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_async_ensure_repository_initialises_missing_repo(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.async_check_repository", return_value=False)
    init_mock = mocker.patch("src.utils.restic.async_init_repository", return_value=True)

    assert await async_ensure_repository("/backups/test", {}, timeout=45) is True
    init_mock.assert_awaited_once_with("/backups/test", {}, timeout=45, log=mocker.ANY)


@pytest.mark.anyio
async def test_async_ensure_repository_init_fails(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.async_check_repository", return_value=False)
    mocker.patch("src.utils.restic.async_init_repository", return_value=False)

    assert await async_ensure_repository("/backups/test", {}) is False


@pytest.mark.anyio
async def test_async_check_repository_file_not_found(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=FileNotFoundError)

    assert await async_check_repository("/backups/test", {}) is False


@pytest.mark.anyio
async def test_async_check_repository_os_error(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=OSError("permission denied"))

    assert await async_check_repository("/backups/test", {}) is False


@pytest.mark.anyio
async def test_async_init_repository_file_not_found(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=FileNotFoundError)

    assert await async_init_repository("/backups/test", {}) is False


@pytest.mark.anyio
async def test_async_init_repository_os_error(mocker: MockerFixture) -> None:
    mocker.patch("src.utils.restic.run_command", side_effect=OSError("no such file"))

    assert await async_init_repository("/backups/test", {}) is False
