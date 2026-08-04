"""Local Unix-domain-socket control plane for scheduler runs.

The scheduler runtime exposes its operational runs over a local Unix-domain
socket so a second process (CLI or GUI) can list and cancel them. The protocol
is intentionally minimal JSON Lines: one request object and one response object
per connection.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from .appdata_schema import utc_rfc3339
from .errors import NotFoundServiceError, ServiceError
from .run_manager import RunManager, RunRecord

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 2.0
_IO_TIMEOUT = 5.0
_PROBE_TIMEOUT = 0.5
_MAX_LINE_BYTES = 1 << 20


def default_socket_path() -> Path:
    """Return the default control-plane socket path.

    Returns:
        The socket path under ``$DK_APPDATA_DIR`` (default ``/appdata``).
    """
    return Path(os.environ.get("DK_APPDATA_DIR", "/appdata")) / "run-control.sock"


def record_to_dict(record: RunRecord) -> dict[str, object]:
    """Serialize a run record into a JSON-compatible dictionary.

    Args:
        record: The run record to serialize.

    Returns:
        A dictionary with all run record fields. StrEnum values are serialized
        as their string value and datetimes as ISO 8601 strings or ``None``.
    """
    return {
        "run_id": record.run_id,
        "origin": str(record.origin),
        "run_kind": str(record.run_kind),
        "job": record.job,
        "task_type": record.task_type,
        "task_name": record.task_name,
        "target": record.display_target,
        "status": str(record.status),
        "dry_run": record.dry_run,
        "cancellable": record.cancellable,
        "created_at": utc_rfc3339(record.created_at) if record.created_at else None,
        "started_at": utc_rfc3339(record.started_at) if record.started_at else None,
        "finished_at": utc_rfc3339(record.finished_at) if record.finished_at else None,
        "error": record.error,
    }


class RunControlServer:
    """Serve scheduler run state over a local Unix-domain socket."""

    def __init__(self, manager: RunManager, socket_path: Path) -> None:
        """Initialize the server.

        Args:
            manager: The run manager whose runs are exposed.
            socket_path: The Unix-domain-socket path to listen on.
        """
        self._manager = manager
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._owns_socket_path = False
        self._socket_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        """Start listening on the socket after robustly cleaning stale files.

        The parent directory is created if needed. An existing socket file is
        only removed if a short probe connection confirms that no live server
        answers on it. The file mode is restricted to ``0o600``.

        Raises:
            ServiceError: If a live server already owns the socket path. The
                caller already holds the scheduler singleton lock, so this is
                an anomaly and must not clobber the active socket.
        """
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        await self._clean_stale_socket()
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(self._socket_path),
            limit=_MAX_LINE_BYTES,
        )
        self._owns_socket_path = True
        os.chmod(self._socket_path, 0o600)
        stat_result = self._socket_path.stat()
        self._socket_identity = (stat_result.st_dev, stat_result.st_ino)

    async def close(self) -> None:
        """Stop serving and remove this server's socket file if it still exists."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._owns_socket_path:
            try:
                if self._socket_identity is None or self._socket_path_matches_identity():
                    self._socket_path.unlink()
            except FileNotFoundError:
                pass
            finally:
                self._owns_socket_path = False
                self._socket_identity = None

    async def _clean_stale_socket(self) -> None:
        if not self._socket_path.exists():
            return
        if await self._socket_is_live():
            raise ServiceError(
                "run_control_socket_in_use",
                f"Active control socket already exists: {self._socket_path}",
                status_code=409,
            )
        self._socket_path.unlink()

    def _socket_path_matches_identity(self) -> bool:
        if self._socket_identity is None:
            return False
        stat_result = self._socket_path.stat()
        return (stat_result.st_dev, stat_result.st_ino) == self._socket_identity

    async def _socket_is_live(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    path=str(self._socket_path),
                    limit=_MAX_LINE_BYTES,
                ),
                timeout=_PROBE_TIMEOUT,
            )
        except (FileNotFoundError, ConnectionRefusedError, OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        del reader
        return True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await asyncio.wait_for(self._read_and_dispatch(reader), timeout=_IO_TIMEOUT)
        except asyncio.TimeoutError:
            response = _error_response("timeout", "Request timed out")
        except Exception:
            logger.exception("Unexpected error while handling control request")
            response = _error_response("internal_error", "Internal server error")
        try:
            await asyncio.wait_for(self._write_response(writer, response), timeout=_IO_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            logger.warning("Failed to write control response")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _read_and_dispatch(self, reader: asyncio.StreamReader) -> dict[str, object]:
        try:
            line = await reader.readline()
        except (OSError, ValueError):
            return _error_response("bad_request", "Could not read request")
        if not line:
            return _error_response("bad_request", "Empty request")
        if len(line) > _MAX_LINE_BYTES:
            return _error_response("bad_request", "Request too large")
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error_response("bad_request", "Malformed JSON request")
        if not isinstance(request, dict):
            return _error_response("bad_request", "Request must be a JSON object")
        return await self._dispatch(request)

    async def _dispatch(self, request: dict[str, object]) -> dict[str, object]:
        action = request.get("action")
        if action == "list_runs":
            records = await self._manager.list()
            return {"ok": True, "runs": [record_to_dict(r) for r in records]}
        if action == "cancel_run":
            run_id = request.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                return _error_response("bad_request", "Missing or invalid run_id")
            try:
                record = await self._manager.cancel(run_id)
            except NotFoundServiceError as exc:
                return _error_response(exc.code, exc.message)
            return {"ok": True, "run": record_to_dict(record)}
        return _error_response("bad_request", f"Unknown action: {action!r}")

    async def _write_response(
        self, writer: asyncio.StreamWriter, response: dict[str, object]
    ) -> None:
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await writer.drain()


class RunControlClient:
    """Client for the local scheduler run-control socket."""

    def __init__(self, socket_path: Path) -> None:
        """Initialize the client.

        Args:
            socket_path: The Unix-domain-socket path to connect to.
        """
        self._socket_path = socket_path

    async def list_runs(self) -> list[dict[str, object]]:
        """Return scheduler run records as dictionaries.

        Returns:
            A list of serialized run records.

        Raises:
            ServiceError: If the scheduler runtime is unreachable.
        """
        response = await self._request({"action": "list_runs"})
        runs = response.get("runs")
        return list(runs) if isinstance(runs, list) else []

    async def cancel_run(self, run_id: str) -> dict[str, object]:
        """Request cancellation of a scheduler run.

        Args:
            run_id: The identifier of the run to cancel.

        Returns:
            The serialized run record.

        Raises:
            NotFoundServiceError: If the run does not exist.
            ServiceError: If the scheduler runtime is unreachable or the
                request fails.
        """
        response = await self._request({"action": "cancel_run", "run_id": run_id})
        run = response.get("run")
        return dict(run) if isinstance(run, dict) else {}

    async def _request(self, request: dict[str, object]) -> dict[str, object]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    path=str(self._socket_path),
                    limit=_MAX_LINE_BYTES,
                ),
                timeout=_CONNECT_TIMEOUT,
            )
        except (FileNotFoundError, ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                "scheduler_unreachable",
                "Scheduler runtime is not reachable",
                status_code=503,
            ) from exc

        try:
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT)
            line = await asyncio.wait_for(reader.readline(), timeout=_IO_TIMEOUT)
        except ValueError as exc:
            raise ServiceError(
                "scheduler_unreachable",
                "Scheduler runtime returned a response that exceeds the protocol limit",
                status_code=503,
            ) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                "scheduler_unreachable",
                "Scheduler runtime did not respond",
                status_code=503,
            ) from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

        return self._parse_response(line)

    def _parse_response(self, line: bytes) -> dict[str, object]:
        if not line:
            raise ServiceError(
                "scheduler_unreachable",
                "Scheduler runtime returned an empty response",
                status_code=503,
            )
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(
                "scheduler_unreachable",
                "Scheduler runtime returned a malformed response",
                status_code=503,
            ) from exc
        if not isinstance(response, dict):
            raise ServiceError(
                "scheduler_unreachable",
                "Scheduler runtime returned an unexpected response",
                status_code=503,
            )
        if not response.get("ok"):
            self._raise_for_error(response)
        return response

    def _raise_for_error(self, response: dict[str, object]) -> None:
        error = response.get("error")
        if not isinstance(error, dict):
            raise ServiceError("scheduler_error", "Scheduler returned an error")
        code = error.get("code", "scheduler_error")
        message = error.get("message", "Scheduler returned an error")
        if code == "run_not_found":
            raise NotFoundServiceError(message, code="run_not_found")
        raise ServiceError(code, message)


def _error_response(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}
