"""Tests for the central asynchronous subprocess runner."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import src.core.subprocesses as subprocesses
from src.core.subprocesses import (
    CommandResult,
    StreamedCommandResult,
    run_command,
    sigterm_grace_period,
    stream_command,
)

PYTHON = Path(sys.executable)
FAST_GRACE_PERIOD_SECONDS = 0.15
PROCESS_START_TIMEOUT_SECONDS = 2.0


@pytest.fixture(autouse=True)
def clear_sigterm_grace_period_cache() -> Iterator[None]:
    sigterm_grace_period.cache_clear()
    yield
    sigterm_grace_period.cache_clear()


def _python_command(script: str, *args: str) -> list[str]:
    return [str(PYTHON), "-c", script, *args]


async def _wait_for_pid(path: Path) -> int:
    deadline = asyncio.get_running_loop().time() + PROCESS_START_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            contents = path.read_text(encoding="ascii")
            if contents:
                return int(contents)
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for PID file: {path}")


def _is_running(pid: int) -> bool:
    """Return whether a process is still executing, excluding unreaped zombies."""
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="ascii").split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return len(fields) >= 3 and fields[2] != "Z"


async def _assert_processes_stopped(*pids: int) -> None:
    deadline = asyncio.get_running_loop().time() + PROCESS_START_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if not any(_is_running(pid) for pid in pids):
            return
        await asyncio.sleep(0.01)
    running = [pid for pid in pids if _is_running(pid)]
    raise AssertionError(f"Processes still running after cleanup: {running}")


def _process_tree_command(pid_dir: Path, *, ignore_sigterm: bool) -> list[str]:
    signal_setup = "signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_sigterm else ""
    child_script = f"""
import os
import signal
import sys
import time
from pathlib import Path

{signal_setup}
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(0.05)
"""
    parent_script = f"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

{signal_setup}
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
subprocess.Popen([sys.executable, "-c", {child_script!r}, sys.argv[2]])
while True:
    time.sleep(0.05)
"""
    return _python_command(
        parent_script,
        str(pid_dir / "parent.pid"),
        str(pid_dir / "child.pid"),
    )


def _detached_pipe_holder_command(pid_dir: Path) -> list[str]:
    grandchild_script = """
import os
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(0.05)
"""
    parent_script = f"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
subprocess.Popen(
    [sys.executable, "-c", {grandchild_script!r}, sys.argv[2]],
    start_new_session=True,
)
while True:
    time.sleep(0.05)
"""
    return _python_command(
        parent_script,
        str(pid_dir / "parent.pid"),
        str(pid_dir / "grandchild.pid"),
    )


def _exiting_parent_with_detached_pipe_holder_command(pid_dir: Path) -> list[str]:
    parent_script = """
import os
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
sys.stdout.write("parent-output\\n")
sys.stdout.flush()
child_pid = os.fork()
if child_pid == 0:
    os.setsid()
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="ascii")
    while True:
        time.sleep(0.05)
os._exit(0)
"""
    return _python_command(
        parent_script,
        str(pid_dir / "parent.pid"),
        str(pid_dir / "grandchild.pid"),
    )


async def _stop_process(pid: int) -> None:
    if not _is_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = asyncio.get_running_loop().time() + PROCESS_START_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if not _is_running(pid):
            return
        await asyncio.sleep(0.01)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await _assert_processes_stopped(pid)


def test_grace_period_is_named_and_defaults_to_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DK_SIGTERM_GRACE_PERIOD", raising=False)
    sigterm_grace_period.cache_clear()

    assert sigterm_grace_period() == 10


def test_grace_period_reads_env_set_after_import(monkeypatch: pytest.MonkeyPatch) -> None:
    sigterm_grace_period.cache_clear()
    monkeypatch.setenv("DK_SIGTERM_GRACE_PERIOD", "3")

    assert sigterm_grace_period() == 3

    sigterm_grace_period.cache_clear()


def test_invalid_grace_period_warning_is_cached(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DK_SIGTERM_GRACE_PERIOD", "invalid")

    with caplog.at_level("WARNING", logger="src.core.subprocesses"):
        assert sigterm_grace_period() == 10
        assert sigterm_grace_period() == 10

    assert caplog.text.count("DK_SIGTERM_GRACE_PERIOD") == 1


def test_run_command_returns_stdout_stderr_and_zero_returncode(tmp_path: Path) -> None:
    async def scenario() -> None:
        result = await run_command(
            _python_command(
                "import os, sys; "
                "print(os.environ['RUNNER_TEST']); "
                "print(os.getcwd()); "
                "print('warning', file=sys.stderr)"
            ),
            env={**os.environ, "RUNNER_TEST": "success"},
            cwd=tmp_path,
        )

        assert result == CommandResult(
            returncode=0,
            stdout=f"success\n{tmp_path}\n",
            stderr="warning\n",
        )

    asyncio.run(scenario())


def test_run_command_returns_nonzero_returncode() -> None:
    async def scenario() -> None:
        result = await run_command(
            _python_command("import sys; print('failed', file=sys.stderr); sys.exit(23)")
        )

        assert result == CommandResult(returncode=23, stdout="", stderr="failed\n")

    asyncio.run(scenario())


def test_run_command_decodes_invalid_utf8_without_failing() -> None:
    async def scenario() -> None:
        result = await run_command(
            _python_command("import os; os.write(1, b'out-\\xff'); os.write(2, b'err-\\xfe')")
        )

        assert result.returncode == 0
        assert result.stdout == "out-\ufffd"
        assert result.stderr == "err-\ufffd"

    asyncio.run(scenario())


def test_run_command_propagates_missing_binary(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(FileNotFoundError):
            await run_command([str(tmp_path / "does-not-exist")])

    asyncio.run(scenario())


def test_run_command_propagates_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    async def raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_oserror)

    async def scenario() -> None:
        with pytest.raises(OSError, match="permission denied"):
            await run_command(["restricted-command"])

    asyncio.run(scenario())


def test_timeout_stops_parent_and_child_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        command = _process_tree_command(tmp_path, ignore_sigterm=True)
        task = asyncio.create_task(run_command(command, timeout=0.5))
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        with pytest.raises(TimeoutError):
            await task

        await _assert_processes_stopped(parent_pid, child_pid)

    asyncio.run(scenario())


def test_run_command_timeout_stops_waiting_when_detached_child_holds_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            run_command(_detached_pipe_holder_command(tmp_path), timeout=0.2)
        )
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        grandchild_pid = await _wait_for_pid(tmp_path / "grandchild.pid")

        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=1.0)
            await _assert_processes_stopped(parent_pid)
        finally:
            await _stop_process(grandchild_pid)

    asyncio.run(scenario())


def test_run_command_returns_when_finished_parent_leaves_detached_pipe_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            run_command(_exiting_parent_with_detached_pipe_holder_command(tmp_path))
        )
        grandchild_pid = await _wait_for_pid(tmp_path / "grandchild.pid")

        try:
            result = await asyncio.wait_for(task, timeout=1.0)

            assert result.returncode == 0
            assert result.stdout == "parent-output\n"
            assert result.stderr == ""
        finally:
            await _stop_process(grandchild_pid)

    asyncio.run(scenario())


def test_run_command_unexpected_pump_exception_stops_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )
    pid_path = tmp_path / "child.pid"

    async def fail_after_process_started(
        stream: asyncio.StreamReader,
        callback: subprocesses.StreamCallback | None,
        read_size: int,
        tail: object | None = None,
    ) -> None:
        del stream, callback, read_size, tail
        await _wait_for_pid(pid_path)
        raise OSError("stream reader failed")

    monkeypatch.setattr(subprocesses, "_pump_stream", fail_after_process_started)

    async def scenario() -> None:
        script = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')\n"
            "time.sleep(30)\n"
        )

        with pytest.raises(OSError, match="stream reader failed"):
            await run_command(_python_command(script, str(pid_path)))

        child_pid = int(pid_path.read_text(encoding="ascii"))
        await _assert_processes_stopped(child_pid)

    asyncio.run(scenario())


def test_cancellation_stops_parent_and_child_processes(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = asyncio.create_task(
            run_command(_process_tree_command(tmp_path, ignore_sigterm=False))
        )
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_processes_stopped(parent_pid, child_pid)

    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_bounded_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            run_command(_process_tree_command(tmp_path, ignore_sigterm=True))
        )
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        started = time.monotonic()
        task.cancel()
        await asyncio.sleep(FAST_GRACE_PERIOD_SECONDS / 3)
        task.cancel()
        await asyncio.sleep(FAST_GRACE_PERIOD_SECONDS / 3)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert time.monotonic() - started >= FAST_GRACE_PERIOD_SECONDS * 0.8
        await _assert_processes_stopped(parent_pid, child_pid)

    asyncio.run(scenario())


def test_stream_command_feeds_callbacks_incrementally_and_returns_returncode(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        result = await stream_command(
            _python_command(
                "import os, sys; "
                "os.write(1, b'out-1\\n'); "
                "os.write(2, b'err-1\\n'); "
                "os.write(1, b'out-2\\n'); "
                "sys.exit(7)"
            ),
            on_stdout=stdout_chunks.append,
            on_stderr=stderr_chunks.append,
        )

        assert result == StreamedCommandResult(returncode=7)
        assert b"".join(stdout_chunks) == b"out-1\nout-2\n"
        assert b"".join(stderr_chunks) == b"err-1\n"

    asyncio.run(scenario())


def test_stream_command_works_without_callbacks(tmp_path: Path) -> None:
    async def scenario() -> None:
        result = await stream_command(_python_command("import sys; sys.exit(0)"))

        assert result == StreamedCommandResult(returncode=0)

    asyncio.run(scenario())


def test_stream_command_propagates_missing_binary(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(FileNotFoundError):
            await stream_command([str(tmp_path / "does-not-exist")])

    asyncio.run(scenario())


def test_stream_command_propagates_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    async def raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_oserror)

    async def scenario() -> None:
        with pytest.raises(OSError, match="permission denied"):
            await stream_command(["restricted-command"])

    asyncio.run(scenario())


def test_stream_command_timeout_stops_parent_and_child_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        command = _process_tree_command(tmp_path, ignore_sigterm=True)
        task = asyncio.create_task(stream_command(command, timeout=0.5))
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        with pytest.raises(TimeoutError):
            await task

        await _assert_processes_stopped(parent_pid, child_pid)

    asyncio.run(scenario())


def test_stream_command_timeout_stops_waiting_when_detached_child_holds_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            stream_command(_detached_pipe_holder_command(tmp_path), timeout=0.2)
        )
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        grandchild_pid = await _wait_for_pid(tmp_path / "grandchild.pid")

        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=1.0)
            await _assert_processes_stopped(parent_pid)
        finally:
            await _stop_process(grandchild_pid)

    asyncio.run(scenario())


def test_stream_command_returns_when_finished_parent_leaves_detached_pipe_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        stdout_chunks: list[bytes] = []
        task = asyncio.create_task(
            stream_command(
                _exiting_parent_with_detached_pipe_holder_command(tmp_path),
                on_stdout=stdout_chunks.append,
            )
        )
        grandchild_pid = await _wait_for_pid(tmp_path / "grandchild.pid")

        try:
            result = await asyncio.wait_for(task, timeout=1.0)

            assert result == StreamedCommandResult(returncode=0)
            assert b"".join(stdout_chunks) == b"parent-output\n"
        finally:
            await _stop_process(grandchild_pid)

    asyncio.run(scenario())


def test_stream_command_timeout_covers_process_after_streams_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        script = (
            "import os, signal, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')\n"
            "os.write(1, b'before-close-out\\n')\n"
            "os.write(2, b'before-close-err\\n')\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "time.sleep(30)\n"
        )

        task = asyncio.create_task(
            stream_command(
                _python_command(script, str(tmp_path / "child.pid")),
                on_stdout=stdout_chunks.append,
                on_stderr=stderr_chunks.append,
                timeout=0.3,
                capture_tail_bytes=64,
            )
        )
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        with pytest.raises(TimeoutError):
            await task

        assert b"".join(stdout_chunks) == b"before-close-out\n"
        assert b"".join(stderr_chunks) == b"before-close-err\n"
        await _assert_processes_stopped(child_pid)

    asyncio.run(scenario())


def test_stream_command_propagates_callback_exception_before_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        script = (
            "import os, signal, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')\n"
            "sys.stdout.write('trigger-callback\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )

        def fail_callback(_chunk: bytes) -> None:
            raise RuntimeError("callback exploded")

        task = asyncio.create_task(
            stream_command(
                _python_command(script, str(tmp_path / "child.pid")),
                on_stdout=fail_callback,
                timeout=5,
            )
        )
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        with pytest.raises(RuntimeError, match="callback exploded"):
            await task

        await _assert_processes_stopped(child_pid)

    asyncio.run(scenario())


def test_stream_command_cancellation_stops_parent_and_child_process_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        chunks: list[bytes] = []
        task = asyncio.create_task(
            stream_command(
                _process_tree_command(tmp_path, ignore_sigterm=False),
                on_stdout=chunks.append,
            )
        )
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _assert_processes_stopped(parent_pid, child_pid)

    asyncio.run(scenario())


def test_stream_command_repeated_cancellation_waits_for_bounded_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            stream_command(_process_tree_command(tmp_path, ignore_sigterm=True))
        )
        parent_pid = await _wait_for_pid(tmp_path / "parent.pid")
        child_pid = await _wait_for_pid(tmp_path / "child.pid")

        started = time.monotonic()
        task.cancel()
        await asyncio.sleep(FAST_GRACE_PERIOD_SECONDS / 3)
        task.cancel()
        await asyncio.sleep(FAST_GRACE_PERIOD_SECONDS / 3)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert time.monotonic() - started >= FAST_GRACE_PERIOD_SECONDS * 0.8
        await _assert_processes_stopped(parent_pid, child_pid)

    asyncio.run(scenario())


def test_stream_command_capture_tail_returns_bounded_tail_with_full_callbacks() -> None:
    async def scenario() -> None:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        result = await stream_command(
            _python_command(
                "import sys\n"
                "for i in range(1000):\n"
                "    sys.stdout.write(f'line-{i:05d}\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stderr.write('E' * 5000)\n"
            ),
            on_stdout=stdout_chunks.append,
            on_stderr=stderr_chunks.append,
            capture_tail_bytes=64,
        )

        # Callbacks still observe the complete output despite the bounded tail.
        assert b"".join(stdout_chunks).count(b"\n") == 1000
        assert len(b"".join(stderr_chunks)) == 5000

        # The returned result only holds the last bytes per stream.
        assert result.stdout_truncated is True
        assert result.stderr_truncated is True
        assert len(result.stdout.encode("utf-8")) <= 64
        assert len(result.stderr.encode("utf-8")) <= 64
        assert result.stdout.endswith("line-00999\n")
        assert set(result.stderr) == {"E"}

    asyncio.run(scenario())


def test_stream_command_capture_tail_keeps_short_output_untruncated() -> None:
    async def scenario() -> None:
        result = await stream_command(
            _python_command(
                "import sys; sys.stdout.write('hello\\n'); sys.stderr.write('warn\\n')"
            ),
            capture_tail_bytes=65536,
        )

        assert result.stdout == "hello\n"
        assert result.stderr == "warn\n"
        assert result.stdout_truncated is False
        assert result.stderr_truncated is False

    asyncio.run(scenario())


def test_stream_command_timeout_drains_emitted_output_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        chunks: list[bytes] = []
        script = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('MARKER-LINE\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        task = asyncio.create_task(
            stream_command(_python_command(script), on_stdout=chunks.append, timeout=0.3)
        )

        with pytest.raises(TimeoutError):
            await task

        assert b"MARKER-LINE\n" in b"".join(chunks)

    asyncio.run(scenario())


def test_stream_command_cancellation_drains_emitted_output_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocesses,
        "sigterm_grace_period",
        lambda: FAST_GRACE_PERIOD_SECONDS,
    )

    async def scenario() -> None:
        chunks: list[bytes] = []
        ready = tmp_path / "ready.pid"
        script = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('MARKER-LINE\\n')\n"
            "sys.stdout.flush()\n"
            f"open({str(ready)!r}, 'w').write('1')\n"
            "time.sleep(30)\n"
        )
        task = asyncio.create_task(stream_command(_python_command(script), on_stdout=chunks.append))
        await _wait_for_pid(ready)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert b"MARKER-LINE\n" in b"".join(chunks)

    asyncio.run(scenario())
