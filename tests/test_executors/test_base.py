"""Tests für BaseExecutor."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar
from unittest.mock import AsyncMock, patch

import pytest

from src.core.subprocesses import StreamedCommandResult
from src.executors.base import BaseExecutor
from tests.stream_stubs import stream_command_stub

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run one async executor operation."""
    return asyncio.run(coro)


class ConcreteExecutor(BaseExecutor):
    """Minimale Konkret-Implementierung für Tests."""

    async def execute(self, config: Any) -> bool:
        return True


class FailingExecutor(BaseExecutor):
    """Executor der immer False zurückgibt."""

    async def execute(self, config: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def _isolate_job_loggers() -> None:
    """Keep these logger assertions independent from prior job logger setup."""
    for logger_name in ("job", "job.ConcreteExecutor", "job.FailingExecutor"):
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True


def _patch_stream(stub: Callable[..., Any]) -> Any:
    return patch("src.executors.base.stream_command", new=AsyncMock(side_effect=stub))


class TestAbstractMethod:
    def test_cannot_instantiate_base_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseExecutor("job")  # type: ignore[abstract]

    def test_concrete_subclass_can_be_instantiated_and_awaited(self) -> None:
        executor = ConcreteExecutor("job")
        assert run_async(executor.execute(None)) is True


class TestLogCommand:
    def test_logs_repo_url_credentials(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = ConcreteExecutor("job")
        with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.job.ConcreteExecutor"):
            executor.log_command(
                ["restic", "--repo", "https://user:pass@example.test/repo", "backup"]
            )
        assert "https://user:pass@example.test/repo" in caplog.text


class TestRunSubprocessSuccess:
    def test_returns_true_on_exit_code_zero(self) -> None:
        executor = ConcreteExecutor("job")
        with _patch_stream(stream_command_stub(returncode=0, stdout=b"ok\n")):
            result = run_async(executor.run_subprocess(["echo", "ok"]))
        assert result is True

    def test_passes_command_env_and_timeout_to_runner(self) -> None:
        executor = ConcreteExecutor("job", timeout=30)
        custom_env = {"RESTIC_PASSWORD": "secret", "PATH": "/usr/bin"}
        mock_run = AsyncMock(side_effect=stream_command_stub(returncode=0))
        with patch("src.executors.base.stream_command", new=mock_run):
            run_async(executor.run_subprocess(["restic", "check"], env=custom_env))
        mock_run.assert_awaited_once()
        assert mock_run.await_args is not None
        assert mock_run.await_args.args[0] == ["restic", "check"]
        assert mock_run.await_args.kwargs["env"] == custom_env
        assert mock_run.await_args.kwargs["timeout"] == 30

    def test_no_env_or_timeout_passes_none(self) -> None:
        executor = ConcreteExecutor("job")
        mock_run = AsyncMock(side_effect=stream_command_stub(returncode=0))
        with patch("src.executors.base.stream_command", new=mock_run):
            run_async(executor.run_subprocess(["true"]))
        assert mock_run.await_args is not None
        assert mock_run.await_args.args[0] == ["true"]
        assert mock_run.await_args.kwargs["env"] is None
        assert mock_run.await_args.kwargs["timeout"] is None

    def test_logs_secret_values_from_output_and_command(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = ConcreteExecutor("job")
        with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.job.ConcreteExecutor"):
            with _patch_stream(
                stream_command_stub(
                    returncode=0,
                    stdout=b"using supersecret\n",
                    stderr=b"warning supersecret\n",
                )
            ):
                run_async(executor.run_subprocess(["cmd", "supersecret"]))
        assert "cmd supersecret" in caplog.text
        assert "using supersecret" in caplog.text
        assert "warning supersecret" in caplog.text

    def test_stdout_split_across_chunks_is_logged_as_complete_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = ConcreteExecutor("job")

        async def split_stream(
            argv: list[str] | None = None,
            *,
            on_stdout: Callable[[bytes], None] | None = None,
            **_kwargs: object,
        ) -> StreamedCommandResult:
            assert on_stdout is not None
            on_stdout(b"first half ")
            on_stdout(b"second half\n")
            return StreamedCommandResult(returncode=0)

        mock = AsyncMock(side_effect=split_stream)
        with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.job.ConcreteExecutor"):
            with patch("src.executors.base.stream_command", new=mock):
                run_async(executor.run_subprocess(["cmd"], stdout_level=logging.DEBUG))

        debug_records = [
            record
            for record in caplog.records
            if record.name == "dockkeep.jobs.job.ConcreteExecutor"
        ]
        assert [record.levelno for record in debug_records] == [logging.DEBUG, logging.DEBUG]
        assert debug_records[-1].getMessage().endswith("first half second half")

    def test_stdout_interceptor_receives_lines_instead_of_logger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = ConcreteExecutor("job")
        intercepted: list[str] = []

        with caplog.at_level(logging.DEBUG, logger="dockkeep.jobs.job.ConcreteExecutor"):
            with _patch_stream(stream_command_stub(returncode=0, stdout=b"json line\n")):
                result = run_async(
                    executor.run_subprocess(
                        ["cmd"],
                        stdout_interceptor=intercepted.append,
                    )
                )

        assert result is True
        assert intercepted == ["json line"]
        assert all("json line" not in record.getMessage() for record in caplog.records)


class TestRunSubprocessFailure:
    def test_returns_false_on_nonzero_exit(self) -> None:
        executor = ConcreteExecutor("job")
        with _patch_stream(stream_command_stub(returncode=2, stderr=b"fatal error\n")):
            result = run_async(executor.run_subprocess(["restic", "backup"]))
        assert result is False

    def test_nonzero_exit_logs_bounded_stderr_and_exit_code(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = ConcreteExecutor("job")
        with caplog.at_level(logging.ERROR, logger="dockkeep.jobs.job.ConcreteExecutor"):
            with _patch_stream(stream_command_stub(returncode=2, stderr=b"boom failure\n")):
                result = run_async(executor.run_subprocess(["restic", "backup"]))
        assert result is False
        error_records = [
            record
            for record in caplog.records
            if record.name == "dockkeep.jobs.job.ConcreteExecutor"
        ]
        assert [record.levelno for record in error_records] == [logging.ERROR, logging.ERROR]

    def test_nonzero_exit_flushes_stdout_interceptor(self) -> None:
        executor = ConcreteExecutor("job")
        intercepted: list[str] = []

        with _patch_stream(stream_command_stub(returncode=2, stdout=b"partial json")):
            result = run_async(
                executor.run_subprocess(["cmd"], stdout_interceptor=intercepted.append)
            )

        assert result is False
        assert intercepted == ["partial json"]

    def test_returns_false_when_command_not_found(self) -> None:
        executor = ConcreteExecutor("job")
        with patch(
            "src.executors.base.stream_command",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError,
        ):
            result = run_async(executor.run_subprocess(["nonexistent-binary"]))
        assert result is False

    def test_returns_false_on_os_error(self) -> None:
        executor = ConcreteExecutor("job")
        with patch(
            "src.executors.base.stream_command",
            new_callable=AsyncMock,
            side_effect=OSError("Permission denied"),
        ):
            result = run_async(executor.run_subprocess(["restricted-cmd"]))
        assert result is False


class TestRunSubprocessTimeout:
    def test_timeout_returns_false(self) -> None:
        executor = ConcreteExecutor("job", timeout=5)
        with patch(
            "src.executors.base.stream_command",
            new_callable=AsyncMock,
            side_effect=TimeoutError,
        ):
            result = run_async(executor.run_subprocess(["sleep", "60"]))
        assert result is False

    def test_timeout_flushes_stdout_interceptor(self) -> None:
        executor = ConcreteExecutor("job", timeout=5)
        intercepted: list[str] = []

        async def timeout_stream(
            argv: list[str] | None = None,
            *,
            on_stdout: Callable[[bytes], None] | None = None,
            **_kwargs: object,
        ) -> StreamedCommandResult:
            assert on_stdout is not None
            on_stdout(b"partial json")
            raise TimeoutError

        with patch("src.executors.base.stream_command", new=AsyncMock(side_effect=timeout_stream)):
            result = run_async(
                executor.run_subprocess(["sleep", "60"], stdout_interceptor=intercepted.append)
            )

        assert result is False
        assert intercepted == ["partial json"]

    def test_cancelled_error_is_not_swallowed(self) -> None:
        executor = ConcreteExecutor("job")
        with patch(
            "src.executors.base.stream_command",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ):
            with pytest.raises(asyncio.CancelledError):
                run_async(executor.run_subprocess(["sleep", "60"]))

    def test_cancelled_error_flushes_stdout_interceptor(self) -> None:
        executor = ConcreteExecutor("job")
        intercepted: list[str] = []

        async def cancelled_stream(
            argv: list[str] | None = None,
            *,
            on_stdout: Callable[[bytes], None] | None = None,
            **_kwargs: object,
        ) -> StreamedCommandResult:
            assert on_stdout is not None
            on_stdout(b"partial json")
            raise asyncio.CancelledError

        with patch(
            "src.executors.base.stream_command",
            new=AsyncMock(side_effect=cancelled_stream),
        ):
            with pytest.raises(asyncio.CancelledError):
                run_async(
                    executor.run_subprocess(["sleep", "60"], stdout_interceptor=intercepted.append)
                )

        assert intercepted == ["partial json"]


class TestNormalizeExtraArgs:
    def test_none_returns_empty_list(self) -> None:
        assert ConcreteExecutor("job")._normalize_extra_args(None) == []

    def test_flag_value_strings_are_split(self) -> None:
        assert ConcreteExecutor("job")._normalize_extra_args(
            ["--keep-tag manual", "--tag Server"]
        ) == ["--keep-tag", "manual", "--tag", "Server"]

    def test_quoted_values_are_preserved(self) -> None:
        assert ConcreteExecutor("job")._normalize_extra_args(['--tag "Server Backup"']) == [
            "--tag",
            "Server Backup",
        ]

    def test_invalid_shell_quote_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            ConcreteExecutor("job")._normalize_extra_args(['--tag "unterminated'])
