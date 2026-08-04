import asyncio
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from src.core.subprocesses import CommandResult
from src.utils.rclone import (
    async_check_remote,
    async_list_remotes,
    parse_remote_name,
)


def test_parse_remote_name_simple() -> None:
    assert parse_remote_name("gdrive:backups/home") == "gdrive"


def test_parse_remote_name_no_path() -> None:
    assert parse_remote_name("s3:") == "s3"


def test_parse_remote_name_only_name_and_bucket() -> None:
    assert parse_remote_name("s3:mybucket") == "s3"


def test_parse_remote_name_deep_path() -> None:
    assert parse_remote_name("b2:bucket/path/to/dir") == "b2"


def test_parse_remote_name_no_colon_raises() -> None:
    with pytest.raises(ValueError, match="Invalid rclone remote path"):
        parse_remote_name("nodrive")


def test_async_list_remotes_returns_names_without_colon(mocker: MockerFixture) -> None:
    run_mock = mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=0, stdout="gdrive:\ns3:\n", stderr=""),
    )

    assert asyncio.run(async_list_remotes(timeout=30)) == ["gdrive", "s3"]
    run_mock.assert_awaited_once_with(["rclone", "listremotes"], timeout=30)


def test_async_list_remotes_nonzero_exit_returns_empty_list(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=1, stdout="", stderr="config missing"),
    )

    assert asyncio.run(async_list_remotes()) == []


def test_async_list_remotes_timeout_returns_empty_list(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        side_effect=TimeoutError,
    )

    assert asyncio.run(async_list_remotes(timeout=30)) == []


def test_async_list_remotes_propagates_cancellation(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(async_list_remotes())


def test_async_list_remotes_empty_config(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        return_value=CommandResult(returncode=0, stdout="", stderr=""),
    )

    assert asyncio.run(async_list_remotes()) == []


def test_async_list_remotes_file_not_found(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        side_effect=FileNotFoundError,
    )

    assert asyncio.run(async_list_remotes()) == []


def test_async_list_remotes_os_error(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.run_command",
        new_callable=AsyncMock,
        side_effect=OSError("permission denied"),
    )

    assert asyncio.run(async_list_remotes()) == []


def test_async_check_remote_configured(mocker: MockerFixture) -> None:
    list_mock = mocker.patch(
        "src.utils.rclone.async_list_remotes",
        new_callable=AsyncMock,
        return_value=["gdrive", "s3"],
    )

    assert asyncio.run(async_check_remote("gdrive:backups/home", timeout=25)) is True
    list_mock.assert_awaited_once_with(timeout=25)


def test_async_check_remote_not_configured(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.async_list_remotes",
        new_callable=AsyncMock,
        return_value=["s3"],
    )

    assert asyncio.run(async_check_remote("gdrive:backups/home")) is False


def test_async_check_remote_invalid_path_skips_listing(mocker: MockerFixture) -> None:
    list_mock = mocker.patch("src.utils.rclone.async_list_remotes", new_callable=AsyncMock)

    assert asyncio.run(async_check_remote("nodrive")) is False
    list_mock.assert_not_awaited()


def test_async_check_remote_empty_remotes(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.async_list_remotes",
        new_callable=AsyncMock,
        return_value=[],
    )

    assert asyncio.run(async_check_remote("gdrive:backups")) is False


def test_async_check_remote_does_not_match_partial_name(mocker: MockerFixture) -> None:
    mocker.patch(
        "src.utils.rclone.async_list_remotes",
        new_callable=AsyncMock,
        return_value=["gdrive2"],
    )

    assert asyncio.run(async_check_remote("gdrive:backups")) is False
