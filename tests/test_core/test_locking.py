"""Tests für Resource- und Scheduler-Locking."""

from pathlib import Path

import pytest

from src.core.locking import (
    RESOURCE_LOCK_FILE_PATTERN,
    SCHEDULER_LOCK_FILE,
    JobAlreadyRunningError,
    ResourceLockManager,
    SchedulerAlreadyRunningError,
    SchedulerLock,
    resource_for_rclone_endpoint,
    resource_for_repository,
)
from src.core.locking import (
    lock_dir as default_lock_dir,
)


@pytest.fixture()
def lock_dir(tmp_path: Path) -> Path:
    """Temporäres Verzeichnis für Lock-Dateien."""
    return tmp_path


def test_scheduler_lock_acquired_and_released(lock_dir: Path) -> None:
    """Scheduler-Lock wird erworben und nach dem Context-Manager freigegeben."""
    scheduler_lock = SchedulerLock(lock_dir=lock_dir)
    scheduler_lock2 = SchedulerLock(lock_dir=lock_dir)

    with scheduler_lock.acquire():
        assert scheduler_lock.lock_path.exists()
        with pytest.raises(SchedulerAlreadyRunningError):
            with scheduler_lock2.acquire():
                pass

    # Nach dem Context-Manager ist der Lock frei: der zweite Erwerb gelingt jetzt.
    with scheduler_lock2.acquire():
        pass


def test_scheduler_lock_uses_env_set_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DK_LOCK_DIR", str(tmp_path))

    scheduler_lock = SchedulerLock()

    assert default_lock_dir() == tmp_path
    assert scheduler_lock.lock_path == tmp_path / SCHEDULER_LOCK_FILE


def test_second_scheduler_lock_raises_already_running(lock_dir: Path) -> None:
    """Zweite Scheduler-Instanz wirft SchedulerAlreadyRunningError."""
    scheduler_lock = SchedulerLock(lock_dir=lock_dir)
    scheduler_lock2 = SchedulerLock(lock_dir=lock_dir)

    with scheduler_lock.acquire():
        with pytest.raises(SchedulerAlreadyRunningError) as exc_info:
            with scheduler_lock2.acquire():
                pass

    assert exc_info.value.lock_path == lock_dir / SCHEDULER_LOCK_FILE


def test_scheduler_lock_file_name_is_global_not_job_dependent(lock_dir: Path) -> None:
    """Scheduler-Lock-Dateiname ist global und nicht job-abhängig."""
    scheduler_lock = SchedulerLock(lock_dir=lock_dir)

    assert scheduler_lock.lock_path == lock_dir / SCHEDULER_LOCK_FILE
    assert "scheduler" in scheduler_lock.lock_path.name
    assert "job" not in scheduler_lock.lock_path.name


def test_resource_lock_file_uses_hash_without_raw_repository_or_secret(lock_dir: Path) -> None:
    """Resource-Lock-Dateinamen enthalten nur Hashes, keine Repository-Rohwerte."""
    repository = "s3:https://user:secret@example.com/bucket"
    manager = ResourceLockManager(
        "job", "backup", {resource_for_repository(repository)}, lock_dir=lock_dir
    )

    lock_name = manager.lock_paths[0].name

    assert lock_name.startswith("dockkeep-resource-")
    assert lock_name.endswith(".lock")
    assert "s3" not in lock_name
    assert "user" not in lock_name
    assert "secret" not in lock_name
    assert "example" not in lock_name


def test_restic_rclone_repository_and_direct_rclone_endpoint_share_lock(
    lock_dir: Path,
) -> None:
    """restic rclone:<remote>:<path> und direkter <remote>:<path> blockieren einander."""
    restic_resource = resource_for_repository("rclone:Nextcloud:/team/../repo")
    endpoint_resource = resource_for_rclone_endpoint("Nextcloud:/repo")
    manager = ResourceLockManager("job-a", "backup", {restic_resource}, lock_dir=lock_dir)
    manager2 = ResourceLockManager("job-b", "rclone", {endpoint_resource}, lock_dir=lock_dir)

    assert restic_resource.resource_id == endpoint_resource.resource_id
    with manager.acquire():
        with pytest.raises(JobAlreadyRunningError):
            with manager2.acquire():
                pass


def test_rclone_endpoint_with_optional_leading_slash_shares_lock() -> None:
    assert (
        resource_for_rclone_endpoint("remote:data").resource_id
        == resource_for_rclone_endpoint("remote:/data").resource_id
    )
    assert (
        resource_for_repository("rclone:remote:data").resource_id
        == resource_for_rclone_endpoint("remote:/data").resource_id
    )


def test_resource_locks_acquired_in_stable_sorted_order(lock_dir: Path) -> None:
    """Mehrere Resource-Locks werden deterministisch sortiert."""
    first = resource_for_repository("/z/repo")
    second = resource_for_repository("/a/repo")
    manager = ResourceLockManager("job", "workflow", {first, second}, lock_dir=lock_dir)

    assert [resource.resource_id for resource in manager.resources] == sorted(
        [first.resource_id, second.resource_id]
    )


def test_different_resource_locks_can_be_held_concurrently(lock_dir: Path) -> None:
    """Unterschiedliche Ressourcen blockieren sich nicht."""
    manager = ResourceLockManager(
        "job-a", "backup-a", {resource_for_repository("/repo/a")}, lock_dir=lock_dir
    )
    manager2 = ResourceLockManager(
        "job-b", "backup-b", {resource_for_repository("/repo/b")}, lock_dir=lock_dir
    )

    with manager.acquire():
        with manager2.acquire():
            pass


def test_resource_locks_released_when_later_acquire_raises_oserror(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bereits erworbene Resource-Locks werden bei Dateisystemfehlern freigegeben."""
    manager = ResourceLockManager(
        "job",
        "workflow",
        {resource_for_repository("/repo/a"), resource_for_repository("/repo/b")},
        lock_dir=lock_dir,
    )
    monkeypatch.setattr(
        manager._locks[1], "acquire", lambda: (_ for _ in ()).throw(OSError("denied"))
    )

    with pytest.raises(OSError, match="denied"):
        with manager.acquire():
            pass

    # Der Probe-Erwerb derselben Ressourcen gelingt nur, wenn der erste Lock
    # trotz des Fehlers beim zweiten Acquire wieder freigegeben wurde.
    probe = ResourceLockManager("job", "workflow", manager.resources, lock_dir=lock_dir)
    with probe.acquire():
        pass


def test_resource_lock_file_pattern_documents_hash_name() -> None:
    """Das Resource-Lock-Dateimuster bleibt explizit hashbasiert."""
    assert RESOURCE_LOCK_FILE_PATTERN == "dockkeep-resource-{hash}.lock"
