"""Asynchronous subprocess execution for cancellable operational commands."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..utils.timeouts import env_timeout
from .stream_logging import ByteTailBuffer

_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def sigterm_grace_period() -> int:
    return env_timeout("DK_SIGTERM_GRACE_PERIOD", 10, _logger)


@dataclass(frozen=True)
class CommandResult:
    """Result of an asynchronously executed command."""

    returncode: int
    stdout: str
    stderr: str


async def run_command(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Run a command and terminate its process group on timeout or cancellation.

    Args:
        argv: Command and arguments without shell processing.
        env: Optional environment for the child process.
        cwd: Optional working directory for the child process.
        timeout: Optional command timeout in seconds.

    Returns:
        The command return code and decoded output streams.

    Raises:
        asyncio.TimeoutError: If the command exceeds ``timeout``.
        asyncio.CancelledError: If the calling task is cancelled.
        OSError: If the child process cannot be started.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    assert proc.stdout is not None
    assert proc.stderr is not None
    pumps = (
        asyncio.create_task(_pump_stream(proc.stdout, stdout_chunks.append, 65536)),
        asyncio.create_task(_pump_stream(proc.stderr, stderr_chunks.append, 65536)),
    )

    try:
        await _await_process_then_bounded_drain(proc, pumps, timeout)
    except BaseException:
        await _terminate_group_draining_pumps(proc, pumps)
        raise

    assert proc.returncode is not None
    return CommandResult(
        returncode=proc.returncode,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


@dataclass(frozen=True)
class StreamedCommandResult:
    """Result of an asynchronously executed, incrementally streamed command.

    When ``capture_tail_bytes`` is passed to :func:`stream_command`, ``stdout``
    and ``stderr`` hold only the last captured bytes per stream (decoded with
    replacement) and the ``*_truncated`` flags record whether earlier bytes were
    dropped. Without ``capture_tail_bytes`` they stay empty and only the
    callbacks observe output.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


StreamCallback = Callable[[bytes], None]


async def stream_command(
    argv: list[str],
    *,
    on_stdout: StreamCallback | None = None,
    on_stderr: StreamCallback | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
    read_size: int = 65536,
    capture_tail_bytes: int | None = None,
) -> StreamedCommandResult:
    """Run a command, feeding its output to callbacks as it arrives.

    Unlike :func:`run_command`, this does not buffer the full stdout/stderr in
    memory; instead, ``on_stdout``/``on_stderr`` are invoked synchronously with
    each decoded chunk as soon as it is read, which keeps memory bounded for
    commands with large or unbounded output (e.g. ``restic ls --json`` listing
    huge directories). The process is started and torn down with the same
    process-group semantics as :func:`run_command`.

    On timeout or cancellation the pump tasks are not cancelled; instead the
    process group is signalled and the pumps are allowed to drain to EOF so that
    every byte the process emitted before dying still reaches the callbacks (and
    the tail buffers). Only after both pumps finish is the original
    ``TimeoutError``/``CancelledError`` re-raised.

    Args:
        argv: Command and arguments without shell processing.
        on_stdout: Optional callback invoked with each non-empty stdout chunk.
        on_stderr: Optional callback invoked with each non-empty stderr chunk.
        env: Optional environment for the child process.
        cwd: Optional working directory for the child process.
        timeout: Optional command timeout in seconds.
        read_size: Maximum number of bytes to read per chunk.
        capture_tail_bytes: If set, retain at most this many trailing bytes per
            stream for the returned ``stdout``/``stderr`` tail.

    Returns:
        The command return code, plus a bounded tail per stream when
        ``capture_tail_bytes`` is set.

    Raises:
        asyncio.TimeoutError: If the command exceeds ``timeout``.
        asyncio.CancelledError: If the calling task is cancelled.
        OSError: If the child process cannot be started.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    stdout_tail = ByteTailBuffer(capture_tail_bytes) if capture_tail_bytes is not None else None
    stderr_tail = ByteTailBuffer(capture_tail_bytes) if capture_tail_bytes is not None else None
    assert proc.stdout is not None
    assert proc.stderr is not None
    pumps = (
        asyncio.create_task(_pump_stream(proc.stdout, on_stdout, read_size, stdout_tail)),
        asyncio.create_task(_pump_stream(proc.stderr, on_stderr, read_size, stderr_tail)),
    )

    try:
        await _await_process_then_bounded_drain(proc, pumps, timeout)
    except BaseException:
        await _terminate_group_draining_pumps(proc, pumps)
        raise

    assert proc.returncode is not None
    return StreamedCommandResult(
        returncode=proc.returncode,
        stdout=stdout_tail.decode() if stdout_tail is not None else "",
        stderr=stderr_tail.decode() if stderr_tail is not None else "",
        stdout_truncated=stdout_tail.truncated if stdout_tail is not None else False,
        stderr_truncated=stderr_tail.truncated if stderr_tail is not None else False,
    )


async def _await_process_then_bounded_drain(
    proc: asyncio.subprocess.Process,
    pumps: tuple[asyncio.Task[None], asyncio.Task[None]],
    timeout: float | None,
) -> None:
    await _await_process_exit_or_pump_failure(proc, pumps, timeout)
    await _drain_finished_process_pumps(proc, pumps)


async def _await_process_exit_or_pump_failure(
    proc: asyncio.subprocess.Process,
    pumps: tuple[asyncio.Task[None], asyncio.Task[None]],
    timeout: float | None,
) -> None:
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout

    while proc.returncode is None:
        active_pumps = [task for task in pumps if not task.done()]
        if not active_pumps:
            await _sleep_until_process_exit_or_timeout(proc, deadline)
            continue

        if deadline is None:
            wait_timeout = 0.05
        else:
            wait_timeout = max(0.0, deadline - asyncio.get_running_loop().time())
            wait_timeout = min(wait_timeout, 0.05)

        done, pending = await asyncio.wait(
            active_pumps, timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        del pending
        if not done:
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError
            continue
        for task in done:
            task.result()

    assert proc.returncode is not None


async def _sleep_until_process_exit_or_timeout(
    proc: asyncio.subprocess.Process,
    deadline: float | None,
) -> None:
    if deadline is None:
        await asyncio.sleep(0.05)
        return
    wait_timeout = deadline - asyncio.get_running_loop().time()
    if wait_timeout <= 0:
        raise asyncio.TimeoutError
    await asyncio.sleep(min(wait_timeout, 0.05))
    if proc.returncode is None and asyncio.get_running_loop().time() >= deadline:
        raise asyncio.TimeoutError


async def _drain_finished_process_pumps(
    proc: asyncio.subprocess.Process,
    pumps: tuple[asyncio.Task[None], asyncio.Task[None]],
) -> None:
    _done, pending = await asyncio.wait(pumps, timeout=sigterm_grace_period())
    for task in pumps:
        if task.done():
            task.result()
    if pending:
        _close_pipe_transports(proc)
        _cancel_pending_tasks(pending)


async def _pump_stream(
    stream: asyncio.StreamReader,
    callback: StreamCallback | None,
    read_size: int,
    tail: ByteTailBuffer | None = None,
) -> None:
    while True:
        chunk = await stream.read(read_size)
        if not chunk:
            return
        if callback is not None:
            callback(chunk)
        if tail is not None:
            tail.feed(chunk)


async def _terminate_group_draining_pumps(
    proc: asyncio.subprocess.Process,
    pumps: tuple[asyncio.Task[None], asyncio.Task[None]],
) -> None:
    cleanup_task = asyncio.create_task(_drain_pumps_to_eof(proc, pumps))
    cancellation: asyncio.CancelledError | None = None

    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            cancellation = exc

    cleanup_task.result()
    if cancellation is not None:
        raise cancellation


async def _drain_pumps_to_eof(
    proc: asyncio.subprocess.Process,
    pumps: tuple[asyncio.Task[None], asyncio.Task[None]],
) -> None:
    _signal_process_group(proc, signal.SIGTERM)

    process_wait = asyncio.create_task(proc.wait())
    watched_tasks = (*pumps, process_wait)
    _done, pending = await asyncio.wait(watched_tasks, timeout=sigterm_grace_period())
    if pending:
        _signal_process_group(proc, signal.SIGKILL)

        await _await_process_after_sigkill(proc, process_wait)
        await _cancel_tasks_after_bounded_wait(pumps)
        return

    await asyncio.gather(*pumps, return_exceptions=True)
    await process_wait


async def _await_process_after_sigkill(
    proc: asyncio.subprocess.Process,
    process_wait: asyncio.Task[int],
) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(process_wait), timeout=sigterm_grace_period())
    except asyncio.TimeoutError:
        _close_pipe_transports(proc)
        await process_wait


def _close_pipe_transports(proc: asyncio.subprocess.Process) -> None:
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    get_pipe_transport = getattr(transport, "get_pipe_transport", None)
    if get_pipe_transport is None:
        return
    for fd in (1, 2):
        pipe_transport = get_pipe_transport(fd)
        if pipe_transport is not None:
            pipe_transport.close()


async def _cancel_tasks_after_bounded_wait(
    tasks: tuple[asyncio.Task[Any], ...],
) -> None:
    done, pending = await asyncio.wait(tasks, timeout=sigterm_grace_period())
    for task in done:
        task.result()
    if pending:
        _cancel_pending_tasks(pending)


def _cancel_pending_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
        task.add_done_callback(_consume_task_exception)


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except BaseException:
        pass


def _signal_process_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
