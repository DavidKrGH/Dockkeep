"""Shared stand-ins for ``stream_command`` used by executor/restore tests.

After DK-BUG-AUDIT-008 the operational executors, hooks and restore stream
output via :func:`src.core.subprocesses.stream_command`. These helpers mock it
while still feeding the stdout/stderr callbacks (so live logging and tail
buffers behave as in production) and returning a ``StreamedCommandResult``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.core.subprocesses import StreamedCommandResult

StreamCommandStub = Callable[..., Awaitable[StreamedCommandResult]]


def stream_command_stub(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> StreamCommandStub:
    """Return an async ``stream_command`` stand-in with a fixed result.

    Args:
        returncode: Exit code to report.
        stdout: Bytes fed to the ``on_stdout`` callback.
        stderr: Bytes fed to the ``on_stderr`` callback.

    Returns:
        An async callable matching the ``stream_command`` signature.
    """

    async def _run(
        argv: list[str] | None = None,
        *,
        on_stdout: Callable[[bytes], None] | None = None,
        on_stderr: Callable[[bytes], None] | None = None,
        capture_tail_bytes: int | None = None,
        **_kwargs: object,
    ) -> StreamedCommandResult:
        if on_stdout is not None and stdout:
            on_stdout(stdout)
        if on_stderr is not None and stderr:
            on_stderr(stderr)
        decode = capture_tail_bytes is not None
        return StreamedCommandResult(
            returncode=returncode,
            stdout=stdout.decode("utf-8", "replace") if decode else "",
            stderr=stderr.decode("utf-8", "replace") if decode else "",
        )

    return _run
