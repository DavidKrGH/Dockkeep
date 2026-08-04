"""File Lock Manager für Resource- und Scheduler-Locking."""

import hashlib
import logging
import os
import posixpath
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)

RESOURCE_LOCK_FILE_PATTERN = "dockkeep-resource-{hash}.lock"
SCHEDULER_LOCK_FILE = "dockkeep-scheduler.lock"


def _lock_dir_from_env() -> Path:
    return Path(os.environ.get("DK_LOCK_DIR", "/var/lock"))


def lock_dir() -> Path:
    return _lock_dir_from_env()


class JobAlreadyRunningError(Exception):
    """Wird ausgelöst wenn ein Job bereits läuft und der Lock nicht erworben werden kann."""

    def __init__(self, job_name: str, target: str, lock_path: Path) -> None:
        """Initialisiert den Fehler.

        Args:
            job_name: Name des Jobs der bereits läuft.
            target: Name des Tasks (Backup, Workflow oder "rclone").
            lock_path: Pfad zur Lock-Datei.
        """
        self.job_name = job_name
        self.target = target
        self.lock_path = lock_path
        super().__init__(f"Job '{job_name}' task '{target}' is already running (lock: {lock_path})")


class SchedulerAlreadyRunningError(Exception):
    """Wird ausgelöst wenn bereits eine Scheduler-Instanz läuft."""

    def __init__(self, lock_path: Path) -> None:
        """Initialisiert den Fehler.

        Args:
            lock_path: Pfad zur Scheduler-Lock-Datei.
        """
        self.lock_path = lock_path
        super().__init__(f"Scheduler is already running (lock: {lock_path})")


class SchedulerLock:
    """Globaler File-Lock für genau eine aktive Scheduler-Instanz."""

    def __init__(self, lock_dir: Path | None = None) -> None:
        """Initialisiert den Scheduler-Lock.

        Args:
            lock_dir: Verzeichnis für Lock-Dateien. Standard: /var/lock.
        """
        resolved_lock_dir = lock_dir if lock_dir is not None else _lock_dir_from_env()
        self.lock_path = resolved_lock_dir / SCHEDULER_LOCK_FILE
        self._lock = FileLock(str(self.lock_path), timeout=0)

    @contextmanager
    def acquire(self) -> Generator[None, None, None]:
        """Context Manager der den Scheduler-Lock erwirbt und wieder freigibt.

        Raises:
            SchedulerAlreadyRunningError: Wenn bereits eine Scheduler-Instanz läuft.
        """
        try:
            self._lock.acquire()
        except Timeout:
            raise SchedulerAlreadyRunningError(self.lock_path)

        logger.debug("Scheduler lock acquired (%s)", self.lock_path)
        try:
            yield
        finally:
            self._lock.release()
            logger.debug("Scheduler lock released (%s)", self.lock_path)


@dataclass(frozen=True, order=True)
class ResourceLock:
    """Kanonische Resource, die für operative Läufe exklusiv gesperrt wird."""

    resource_id: str

    @property
    def digest(self) -> str:
        """Stabiler Hash für Lock-Dateinamen ohne Rohwerte."""
        return hashlib.sha256(self.resource_id.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        """Opakes Label für Logs und Fehlertexte."""
        return f"resource:{self.digest[:12]}"


def _normalize_posix_path(value: str) -> str:
    path = value.replace("\\", "/")
    normalized = posixpath.normpath(path or "/")
    if path.startswith("/") and not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized == ".":
        return "/"
    return normalized


def _split_rclone_endpoint(value: str) -> tuple[str, str] | None:
    remote, separator, path = value.partition(":")
    if not separator or not remote or "/" in remote:
        return None
    return remote, path.lstrip("/")


def canonical_resource_id(value: str, *, allow_direct_rclone: bool = False) -> str:
    """Ermittelt eine deterministische Resource-ID für Repository- oder Rclone-Pfade.

    Lokale absolute Pfade werden via ``resolve(strict=False)`` kanonisiert. Restic-
    Rclone-Repositories (``rclone:<remote>:<path>``) und direkte Rclone-Endpunkte
    (``<remote>:<path>``) werden auf dieselbe ``rclone:<remote>:<path>``-ID
    abgebildet, wenn direkte Endpunkte für den Aufrufer erlaubt sind.

    Args:
        value: Repository, lokaler Pfad oder Rclone-Endpunkt.
        allow_direct_rclone: Ob ``<remote>:<path>`` als Rclone-Endpunkt gilt.

    Returns:
        Kanonische Resource-ID. Sie darf nicht für Dateinamen verwendet werden.
    """
    if value.startswith("rclone:"):
        endpoint = _split_rclone_endpoint(value.removeprefix("rclone:"))
        if endpoint is not None:
            remote, path = endpoint
            return f"rclone:{remote}:{_normalize_posix_path(path)}"

    if allow_direct_rclone:
        endpoint = _split_rclone_endpoint(value)
        if endpoint is not None:
            remote, path = endpoint
            return f"rclone:{remote}:{_normalize_posix_path(path)}"

    if value.startswith("/") or value.startswith("~"):
        return f"local:{Path(value).expanduser().resolve(strict=False)}"

    return f"repo:{value}"


def resource_for_repository(repository: str) -> ResourceLock:
    return ResourceLock(canonical_resource_id(repository, allow_direct_rclone=False))


def resource_for_rclone_endpoint(endpoint: str) -> ResourceLock:
    return ResourceLock(canonical_resource_id(endpoint, allow_direct_rclone=True))


class ResourceLockManager:

    def __init__(
        self,
        job_name: str,
        target: str,
        resources: list[ResourceLock] | set[ResourceLock],
        lock_dir: Path | None = None,
    ) -> None:
        """Initialisiert den Manager für operative Resource-Locks.

        Args:
            job_name: Name des Jobs für bestehende Fehlersemantik.
            target: Operativer Task für bestehende Fehlersemantik.
            resources: Zu sperrende Ressourcen. Duplikate werden entfernt.
            lock_dir: Verzeichnis für Lock-Dateien.
        """
        self.job_name = job_name
        self.target = target
        resolved_lock_dir = lock_dir if lock_dir is not None else _lock_dir_from_env()
        self.resources = sorted(set(resources), key=lambda resource: resource.resource_id)
        self.lock_paths = [
            resolved_lock_dir / RESOURCE_LOCK_FILE_PATTERN.format(hash=resource.digest)
            for resource in self.resources
        ]
        resolved_lock_dir = resolved_lock_dir.resolve()
        for lock_path in self.lock_paths:
            if lock_path.resolve().parent != resolved_lock_dir:
                raise ValueError(f"Invalid resource lock path: {lock_path}")
        self._locks = [FileLock(str(lock_path), timeout=0) for lock_path in self.lock_paths]

    @contextmanager
    def acquire(self) -> Generator[None, None, None]:
        """Erwirbt alle Resource-Locks ohne Wartezeit und gibt sie zuverlässig frei."""
        acquired: list[FileLock] = []
        blocked_lock_path: Path | None = None
        try:
            for lock, lock_path in zip(self._locks, self.lock_paths, strict=True):
                lock.acquire()
                acquired.append(lock)
        except Timeout:
            blocked_lock_path = lock_path
            for lock in reversed(acquired):
                lock.release()
            raise JobAlreadyRunningError(self.job_name, self.target, blocked_lock_path)
        except Exception:
            for lock in reversed(acquired):
                lock.release()
            raise

        labels = ", ".join(resource.label for resource in self.resources)
        logger.debug(
            "Resource locks acquired for job '%s' task '%s' (%s)",
            self.job_name,
            self.target,
            labels,
        )
        try:
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            logger.debug(
                "Resource locks released for job '%s' task '%s' (%s)",
                self.job_name,
                self.target,
                labels,
            )
