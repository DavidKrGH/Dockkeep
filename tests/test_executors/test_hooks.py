import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.core.subprocesses import StreamedCommandResult
from src.executors.hooks import HookExecutor

PROCESS_STOP_TIMEOUT_SECONDS = 2.0
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run async hook tests with asyncio because subprocess cleanup uses asyncio."""
    return "asyncio"


def make_executor(scripts_dir: str = "/scripts") -> HookExecutor:
    return HookExecutor("myjob", scripts_dir)


def _ok_result() -> StreamedCommandResult:
    return StreamedCommandResult(returncode=0)


def _fail_result() -> StreamedCommandResult:
    return StreamedCommandResult(returncode=1, stderr="hook error")


def _assert_hook_invocation(
    mock_run: AsyncMock, argv: list[str], cwd: Path, timeout: int | None = None
) -> None:
    """Assert the streamed hook invocation argv/cwd/timeout, ignoring callbacks."""
    assert mock_run.call_count == 1
    call = mock_run.call_args
    assert call.args[0] == argv
    assert call.kwargs["cwd"] == cwd
    assert call.kwargs["timeout"] == timeout


async def _wait_for_pid(path: Path) -> int:
    deadline = asyncio.get_running_loop().time() + PROCESS_STOP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            contents = path.read_text(encoding="ascii")
            if contents:
                return int(contents)
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for PID file: {path}")


def _is_running(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="ascii").split()
    except FileNotFoundError:
        return False
    return len(fields) >= 3 and fields[2] != "Z"


async def _assert_processes_stopped(*pids: int) -> None:
    deadline = asyncio.get_running_loop().time() + PROCESS_STOP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if not any(_is_running(pid) for pid in pids):
            return
        await asyncio.sleep(0.01)
    running = [pid for pid in pids if _is_running(pid)]
    raise AssertionError(f"Processes still running after cleanup: {running}")


def _process_tree_hook(path: Path, parent_pid_path: Path, child_pid_path: Path) -> Path:
    child_script = f"""
import os
import time
from pathlib import Path

Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(0.05)
"""
    parent_script = f"""#!{sys.executable}
import os
import subprocess
import sys
import time
from pathlib import Path

Path({str(parent_pid_path)!r}).write_text(str(os.getpid()), encoding="ascii")
subprocess.Popen([sys.executable, "-c", {child_script!r}])
while True:
    time.sleep(0.05)
"""
    path.write_text(parent_script, encoding="ascii")
    path.chmod(0o755)
    return path


async def test_run_empty_hooks() -> None:
    executor = make_executor()

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run([])

    assert result is True
    mock_run.assert_not_called()


def _script(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(mode)
    return path


async def test_run_script_path_absolute(tmp_path: Path) -> None:
    """Absolute script paths inside scripts_dir are allowed and run directly."""
    script = _script(tmp_path / "stop.sh")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run([str(script)])

    assert result is True
    _assert_hook_invocation(mock_run, [str(script)], tmp_path)


async def test_run_relative_script_path_is_not_treated_as_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Script hooks must be absolute paths under scripts_dir."""
    monkeypatch.delenv("DK_ALLOW_INLINE_HOOKS", raising=False)
    _script(tmp_path / "stop-db.sh")
    executor = make_executor(str(tmp_path))

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run(["./stop-db.sh"])

    assert result is False
    mock_run.assert_not_called()


async def test_run_script_path_with_whitespace(tmp_path: Path) -> None:
    """Absolute script paths may contain whitespace when the resolved file exists."""
    script = _script(tmp_path / "my documents" / "hook.sh")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run([str(script)])

    assert result is True
    _assert_hook_invocation(mock_run, [str(script)], tmp_path)


async def test_run_script_path_with_argument(tmp_path: Path) -> None:
    """Absolute script hooks may include positional arguments without using a shell."""
    script = _script(tmp_path / "StartStopContainer.sh")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run([f"{script} start"])

    assert result is True
    _assert_hook_invocation(mock_run, [str(script), "start"], tmp_path)


async def test_run_script_path_with_argument_logs_full_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Successful script hook logs include positional arguments."""
    script = _script(tmp_path / "StartStopContainer.sh")
    executor = make_executor(str(tmp_path))

    with caplog.at_level("INFO", logger="dockkeep.jobs.myjob.HookExecutor"):
        with patch(
            "src.executors.hooks.stream_command",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            result = await executor.run([f"{script} start"])

    assert result is True
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "dockkeep.jobs.myjob.HookExecutor"
    ]
    assert f"Running hook: script {script} start" in messages
    assert f"Hook completed successfully: script {script} start" in messages


async def test_run_quoted_script_path_with_whitespace_and_argument(tmp_path: Path) -> None:
    """Script paths containing whitespace must be quoted when arguments follow."""
    script = _script(tmp_path / "my documents" / "hook.sh")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run([f'"{script}" start'])

    assert result is True
    _assert_hook_invocation(mock_run, [str(script), "start"], tmp_path)


async def test_run_missing_script_path_with_argument_rejected(tmp_path: Path) -> None:
    """Missing script hooks with arguments are rejected before subprocess startup."""
    script = tmp_path / "missing.sh"
    executor = make_executor(str(tmp_path))

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run([f"{script} start"])

    assert result is False
    mock_run.assert_not_called()


async def test_run_script_relative_parent_path_rejected(tmp_path: Path) -> None:
    """Relative parent paths cannot escape scripts_dir."""
    executor = make_executor(str(tmp_path / "scripts"))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run(["../other/hook.sh"])

    assert result is False
    mock_run.assert_not_called()


async def test_run_plain_script_name_under_scripts_dir_is_not_treated_as_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plain script names are inline commands, not script paths."""
    monkeypatch.delenv("DK_ALLOW_INLINE_HOOKS", raising=False)
    _script(tmp_path / "pre.sh")
    executor = make_executor(str(tmp_path))

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run(["pre.sh"])

    assert result is False
    mock_run.assert_not_called()


async def test_run_absolute_path_outside_scripts_dir_rejected(tmp_path: Path) -> None:
    """Absolute paths outside scripts_dir are rejected."""
    outside = _script(tmp_path / "outside.sh")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    executor = make_executor(str(scripts_dir))

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run([str(outside)])

    assert result is False
    mock_run.assert_not_called()


async def test_run_missing_absolute_script_inside_scripts_dir_rejected(tmp_path: Path) -> None:
    """Missing absolute script paths under scripts_dir are rejected before subprocess startup."""
    missing = tmp_path / "missing.sh"
    executor = make_executor(str(tmp_path))

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run([str(missing)])

    assert result is False
    mock_run.assert_not_called()


async def test_run_non_executable_script_logs_chmod_hint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Scripts must be executable and produce a clear chmod hint when they are not."""
    script = _script(tmp_path / "pre.sh", mode=0o644)
    executor = make_executor(str(tmp_path))

    with caplog.at_level("ERROR", logger="dockkeep.jobs.myjob.HookExecutor"):
        with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
            result = await executor.run([str(script)])

    assert result is False
    mock_run.assert_not_called()
    assert "chmod +x" in caplog.text


async def test_run_inline_disallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ohne DK_ALLOW_INLINE_HOOKS=true wird ein Inline-Befehl abgelehnt."""
    monkeypatch.delenv("DK_ALLOW_INLINE_HOOKS", raising=False)
    executor = make_executor()

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run(["echo hello"])

    assert result is False
    mock_run.assert_not_called()


async def test_run_inline_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mit DK_ALLOW_INLINE_HOOKS=true wird der Inline-Befehl per Shell ausgeführt."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run(["echo hello"])

    assert result is True
    _assert_hook_invocation(mock_run, ["/bin/sh", "-c", "echo hello"], tmp_path)


async def test_run_inline_success_is_logged_at_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Successful hooks are visible in normal job logs."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    with caplog.at_level("INFO", logger="dockkeep.jobs.myjob.HookExecutor"):
        with patch(
            "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
        ) as mock_run:
            result = await executor.run(["echo hello"])

    assert result is True
    _assert_hook_invocation(mock_run, ["/bin/sh", "-c", "echo hello"], tmp_path)
    info_records = [
        record for record in caplog.records if record.name == "dockkeep.jobs.myjob.HookExecutor"
    ]
    assert [record.levelname for record in info_records] == ["INFO", "INFO"]


async def test_run_inline_allowed_supports_shell_syntax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inline-Hooks support shell operators and redirection."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    result = await executor.run(["echo hello > hook-output.txt && echo done >> hook-output.txt"])

    assert result is True
    assert (tmp_path / "hook-output.txt").read_text(encoding="utf-8") == "hello\ndone\n"


async def test_run_inline_with_absolute_redirect_target_is_not_treated_as_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inline-Hooks may contain absolute paths in shell redirections."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))
    cmd = "echo \"$(date '+%Y-%m-%d %H:%M:%S') - Hook inline ausgeführt\" >> /scripts/hook-test.log"

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run([cmd])

    assert result is True
    _assert_hook_invocation(mock_run, ["/bin/sh", "-c", cmd], tmp_path)


async def test_run_inline_whitespace_only_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whitespace-only inline hooks are rejected before subprocess startup."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
        result = await executor.run(["   "])

    assert result is False
    mock_run.assert_not_called()


async def test_run_missing_scripts_dir_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing hook cwd is reported clearly before subprocess startup."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path / "missing-scripts"))

    with caplog.at_level("ERROR", logger="dockkeep.jobs.myjob.HookExecutor"):
        with patch("src.executors.hooks.stream_command", new_callable=AsyncMock) as mock_run:
            result = await executor.run(["echo hello"])

    assert result is False
    mock_run.assert_not_called()
    error_records = [
        record for record in caplog.records if record.name == "dockkeep.jobs.myjob.HookExecutor"
    ]
    assert [record.levelname for record in error_records] == ["ERROR"]


async def test_run_script_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """FileNotFoundError beim Subprocess-Start → False."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command",
        new_callable=AsyncMock,
        side_effect=FileNotFoundError("no such file"),
    ):
        result = await executor.run(["missing-command"])

    assert result is False


async def test_run_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TimeoutError vom zentralen Runner → False."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = HookExecutor("myjob", tmp_path, hook_timeout=3)

    with patch(
        "src.executors.hooks.stream_command",
        new_callable=AsyncMock,
        side_effect=TimeoutError,
    ):
        result = await executor.run(["slow-command"])

    assert result is False


async def test_run_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """OSError beim Subprocess-Start → False."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command",
        new_callable=AsyncMock,
        side_effect=OSError("permission denied"),
    ):
        result = await executor.run(["restricted-command"])

    assert result is False


async def test_run_script_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """returncode != 0 → False."""
    monkeypatch.setenv("DK_ALLOW_INLINE_HOOKS", "true")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command",
        new_callable=AsyncMock,
        return_value=_fail_result(),
    ):
        result = await executor.run(["failing-command"])

    assert result is False


async def test_run_stops_on_first_failure(tmp_path: Path) -> None:
    """Beim ersten fehlschlagenden Hook werden nachfolgende Hooks nicht ausgeführt."""
    first = _script(tmp_path / "first.sh")
    _script(tmp_path / "second.sh")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command",
        new_callable=AsyncMock,
        return_value=_fail_result(),
    ) as mock_run:
        result = await executor.run([str(first), str(tmp_path / "second.sh")])

    assert result is False
    assert mock_run.call_count == 1
    assert mock_run.call_args[0][0] == [str(first)]


async def test_run_multiple_success(tmp_path: Path) -> None:
    """Alle Hooks erfolgreich → True, beide wurden aufgerufen."""
    first = _script(tmp_path / "first.sh")
    second = _script(tmp_path / "second.sh")
    executor = make_executor(str(tmp_path))

    with patch(
        "src.executors.hooks.stream_command", new_callable=AsyncMock, return_value=_ok_result()
    ) as mock_run:
        result = await executor.run([str(first), str(second)])

    assert result is True
    assert mock_run.call_count == 2
    invoked_argv = [call.args[0] for call in mock_run.call_args_list]
    assert invoked_argv == [[str(first)], [str(second)]]
    for call in mock_run.call_args_list:
        assert call.kwargs["cwd"] == tmp_path
        assert call.kwargs["timeout"] is None


async def test_cancellation_stops_hook_parent_and_child_processes(tmp_path: Path) -> None:
    """Cancellation propagates and the central runner terminates the hook process group."""
    parent_pid_path = tmp_path / "parent.pid"
    child_pid_path = tmp_path / "child.pid"
    script = _process_tree_hook(tmp_path / "process-tree-hook.py", parent_pid_path, child_pid_path)
    executor = make_executor(str(tmp_path))

    task = asyncio.create_task(executor.run([str(script)]))
    parent_pid = await _wait_for_pid(parent_pid_path)
    child_pid = await _wait_for_pid(child_pid_path)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_processes_stopped(parent_pid, child_pid)
