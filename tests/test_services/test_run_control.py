import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from src.services.appdata_schema import utc_rfc3339
from src.services.errors import NotFoundServiceError, ServiceError
from src.services.run_control import (
    RunControlClient,
    RunControlServer,
    default_socket_path,
    record_to_dict,
)
from src.services.run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus


async def _wait_for_status(record_status: Callable[[], RunStatus], status: RunStatus) -> None:
    for _ in range(100):
        if record_status() == status:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"run did not reach status {status}")


def test_default_socket_path_uses_appdata_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DK_APPDATA_DIR", "/custom/appdata")
    assert default_socket_path() == Path("/custom/appdata/run-control.sock")
    monkeypatch.delenv("DK_APPDATA_DIR", raising=False)
    assert default_socket_path() == Path("/appdata/run-control.sock")


def test_record_to_dict_serializes_all_fields() -> None:
    async def scenario() -> None:
        manager = RunManager()

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await manager.start(
            RunOrigin.SCHEDULER, "demo", "backup", "local", operation, dry_run=True
        )
        await manager.join(record.run_id)

        data = record_to_dict(record)
        assert data["run_id"] == record.run_id
        assert data["origin"] == "scheduler"
        assert isinstance(data["origin"], str)
        assert data["run_kind"] == "job_task"
        assert data["job"] == "demo"
        assert data["task_type"] == "backup"
        assert data["task_name"] == "local"
        assert data["target"] == "demo.backup.local"
        assert "task_selector" not in data
        assert data["status"] == "success"
        assert data["dry_run"] is True
        assert data["created_at"] == utc_rfc3339(record.created_at)
        assert data["finished_at"] == utc_rfc3339(record.finished_at)
        assert data["error"] is None

    asyncio.run(scenario())


def test_round_trip_list_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"
        release = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            await release.wait()
            return True

        record = await manager.start(RunOrigin.SCHEDULER, "demo", "backup", "local", operation)
        await _wait_for_status(lambda: record.status, RunStatus.RUNNING)

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            client = RunControlClient(socket_path)
            runs = await client.list_runs()
        finally:
            await server.close()

        assert len(runs) == 1
        assert runs[0] == {
            "run_id": record.run_id,
            "origin": "scheduler",
            "run_kind": "job_task",
            "job": "demo",
            "task_type": "backup",
            "task_name": "local",
            "target": "demo.backup.local",
            "status": "running",
            "dry_run": False,
            "cancellable": record.cancellable,
            "created_at": utc_rfc3339(record.created_at),
            "started_at": utc_rfc3339(record.started_at),
            "finished_at": None,
            "error": None,
        }
        release.set()
        await manager.join(record.run_id)

    asyncio.run(scenario())


def test_list_runs_allows_response_above_asyncio_default_line_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        records = [
            RunRecord(
                run_id=f"run-{idx}",
                origin=RunOrigin.SCHEDULER,
                run_kind=RunKind.JOB_TASK,
                job="demo",
                task_type="backup",
                task_name=f"local-{idx}",
                status=RunStatus.RUNNING,
                error="x" * 1000,
            )
            for idx in range(90)
        ]
        payload_size = len(
            json.dumps({"ok": True, "runs": [record_to_dict(record) for record in records]})
        )
        assert payload_size > 64 * 1024

        class LargeRunManager:
            async def list(self) -> list[RunRecord]:
                return records

        socket_path = tmp_path / "run-control.sock"
        server = RunControlServer(cast(RunManager, LargeRunManager()), socket_path)
        await server.start()
        try:
            client = RunControlClient(socket_path)
            runs = await client.list_runs()
        finally:
            await server.close()

        assert len(runs) == len(records)
        assert runs[-1]["error"] == "x" * 1000

    asyncio.run(scenario())


def test_list_runs_omits_terminal_scheduler_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        async def operation(_: Callable[[], None]) -> bool:
            return True

        record = await manager.start(RunOrigin.SCHEDULER, "demo", "backup", "local", operation)
        await manager.join(record.run_id)

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            client = RunControlClient(socket_path)
            runs = await client.list_runs()
        finally:
            await server.close()

        assert runs == []

    asyncio.run(scenario())


def test_cancel_run_active_returns_run_dict(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"
        started = asyncio.Event()

        async def operation(_: Callable[[], None]) -> bool:
            started.set()
            await asyncio.Event().wait()
            return True

        record = await manager.start(RunOrigin.SCHEDULER, "demo", "backup", "local", operation)
        await started.wait()

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            client = RunControlClient(socket_path)
            run = await client.cancel_run(record.run_id)
        finally:
            await server.close()

        assert run["run_id"] == record.run_id
        await _wait_for_status(lambda: record.status, RunStatus.CANCELLED)

    asyncio.run(scenario())


def test_cancel_unknown_run_raises_not_found(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            client = RunControlClient(socket_path)
            with pytest.raises(NotFoundServiceError) as exc_info:
                await client.cancel_run("missing")
        finally:
            await server.close()

        assert exc_info.value.code == "run_not_found"
        assert exc_info.value.status_code == 404

    asyncio.run(scenario())


def test_unknown_action_returns_bad_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.write(b'{"action":"nope"}\n')
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

        response = json.loads(line.decode("utf-8"))
        assert response["ok"] is False
        assert response["error"]["code"] == "bad_request"

    asyncio.run(scenario())


def test_malformed_request_returns_bad_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.write(b"not json at all\n")
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

        response = json.loads(line.decode("utf-8"))
        assert response["ok"] is False
        assert response["error"]["code"] == "bad_request"

    asyncio.run(scenario())


def test_stale_socket_file_is_cleaned_on_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"
        socket_path.write_text("leftover")
        assert socket_path.exists()

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            client = RunControlClient(socket_path)
            assert await client.list_runs() == []
        finally:
            await server.close()

    asyncio.run(scenario())


def test_live_socket_is_not_clobbered(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        first = RunControlServer(manager, socket_path)
        await first.start()
        try:
            second = RunControlServer(manager, socket_path)
            with pytest.raises(ServiceError) as exc_info:
                await second.start()
            assert exc_info.value.code == "run_control_socket_in_use"
            await second.close()

            # The first, live server must still answer.
            client = RunControlClient(socket_path)
            assert await client.list_runs() == []
            assert socket_path.exists()
        finally:
            await first.close()

    asyncio.run(scenario())


def test_failed_start_close_does_not_unlink_foreign_live_socket(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        owner = RunControlServer(manager, socket_path)
        await owner.start()
        try:
            contender = RunControlServer(manager, socket_path)
            with pytest.raises(ServiceError):
                await contender.start()

            await contender.close()

            client = RunControlClient(socket_path)
            assert await client.list_runs() == []
            assert socket_path.exists()
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_unreachable_server_raises_promptly(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "missing.sock"
        client = RunControlClient(socket_path)

        with pytest.raises(ServiceError) as exc_info:
            await asyncio.wait_for(client.list_runs(), timeout=3.0)
        assert exc_info.value.code == "scheduler_unreachable"

        with pytest.raises(ServiceError) as cancel_exc:
            await asyncio.wait_for(client.cancel_run("x"), timeout=3.0)
        assert cancel_exc.value.code == "scheduler_unreachable"

    asyncio.run(scenario())


def test_non_listening_file_is_unreachable(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "bogus.sock"
        socket_path.write_text("not a socket")
        client = RunControlClient(socket_path)

        with pytest.raises(ServiceError) as exc_info:
            await asyncio.wait_for(client.list_runs(), timeout=3.0)
        assert exc_info.value.code == "scheduler_unreachable"

    asyncio.run(scenario())


def test_socket_mode_is_restricted(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        server = RunControlServer(manager, socket_path)
        await server.start()
        try:
            mode = socket_path.stat().st_mode & 0o777
            assert mode == 0o600
        finally:
            await server.close()

    asyncio.run(scenario())


def test_close_removes_socket_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = RunManager()
        socket_path = tmp_path / "run-control.sock"

        server = RunControlServer(manager, socket_path)
        await server.start()
        assert socket_path.exists()
        await server.close()
        assert not socket_path.exists()

        # close() is idempotent on a missing file.
        await server.close()

    asyncio.run(scenario())


def test_client_maps_oversized_response_to_service_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "run-control.sock"

        async def handle(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b'{"ok":true,"runs":[' + b'"x",' * 70000 + b"]}\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=str(socket_path))
        try:
            client = RunControlClient(socket_path)
            with pytest.raises(ServiceError) as exc_info:
                await client.list_runs()
        finally:
            server.close()
            await server.wait_closed()

        assert exc_info.value.code == "scheduler_unreachable"

    asyncio.run(scenario())
