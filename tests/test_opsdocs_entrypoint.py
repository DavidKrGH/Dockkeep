"""Smoke tests for Docker entrypoint/runtime wiring."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_is_valid_bash() -> None:
    """The Docker entrypoint remains parseable by bash."""
    subprocess.run(
        ["bash", "-n", str(ROOT / "docker-entrypoint.sh")],
        check=True,
    )


def test_entrypoint_resolves_empty_args_from_dk_mode() -> None:
    """Empty container args are resolved to gui/cli and invalid modes warn."""
    script = (ROOT / "docker-entrypoint.sh").read_text()

    assert 'case "${DK_MODE:-gui}" in' in script
    assert "set -- dk-runtime gui" in script
    assert "set -- dk-runtime scheduler" in script
    assert "invalid DK_MODE" in script
    assert ">&2" in script


def test_entrypoint_dispatches_dk_and_runtime_for_all_exec_paths() -> None:
    """Explicit docker args may target the user CLI or runtime CLI."""
    script = (ROOT / "docker-entrypoint.sh").read_text()

    normalize_index = script.index('if [ "${1:-}" = "dk" ] || [ "${1:-}" = "dk-runtime" ]; then')
    root_exec_index = script.index('exec python -m src.main "$@"')
    non_root_exec_index = script.index('exec gosu "${PUID}:${PGID}" python -m src.main "$@"')
    root_runtime_index = script.index('exec python -m src.runtime "$@"')
    non_root_runtime_index = script.index('exec gosu "${PUID}:${PGID}" python -m src.runtime "$@"')

    assert normalize_index < root_exec_index
    assert normalize_index < non_root_exec_index
    assert normalize_index < root_runtime_index
    assert normalize_index < non_root_runtime_index


def test_entrypoint_prepares_restore_dir() -> None:
    """Restore dir follows the same env/default/mkdir/chown pattern as other dirs."""
    script = (ROOT / "docker-entrypoint.sh").read_text()

    assert 'DK_RESTORE_DIR="${DK_RESTORE_DIR:-/restore}"' in script
    assert '"$DK_RESTORE_DIR"' in script


def test_entrypoint_does_not_rewrite_mounted_ownership_recursively() -> None:
    """PUID/PGID mode warns about host permissions instead of mutating volumes."""
    script = (ROOT / "docker-entrypoint.sh").read_text()

    assert re.search(r"^\s*(if !\s+)?chown\s+-R\b", script, re.MULTILINE) is None
    assert re.search(r"^\s*(if !\s+)?chmod\s+-R\b", script, re.MULTILINE) is None
    assert "Dockkeep does not recursively chown or chmod" in script
    assert "sudo chown -R ${PUID}:${PGID}" in script


def test_entrypoint_rejects_half_set_puid_pgid() -> None:
    """A half-configured non-root mode must not silently fall back to root."""
    script = (ROOT / "docker-entrypoint.sh").read_text()

    assert 'if [ -n "${PUID}" ] || [ -n "${PGID}" ]; then' in script
    assert 'if [ -z "${PUID}" ] || [ -z "${PGID}" ]; then' in script
    assert "PUID and PGID must be set together" in script
    assert "exit 1" in script


def test_dockerfile_has_no_default_command() -> None:
    """The entrypoint, not Docker CMD, owns default mode resolution."""
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert 'CMD ["web"]' not in dockerfile
    assert 'CMD ["gui"]' not in dockerfile
    assert "EXPOSE 8080" in dockerfile


def test_compose_documents_default_gui_runtime() -> None:
    """Compose keeps the single app container on GUI port 8080 by default."""
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "app:" in compose
    assert "DK_MODE: ${DK_MODE:-gui}" in compose
    assert '"8080:8080"' in compose
    assert "dk web" not in compose
    assert "web:" not in compose
    assert "scheduler:" not in compose


def test_makefile_has_gui_target_and_no_web_target() -> None:
    """Local helper targets use gui naming and keep the hard web removal."""
    makefile = (ROOT / "Makefile").read_text()

    assert re.search(r"^gui:", makefile, flags=re.MULTILINE)
    assert not re.search(r"^web:", makefile, flags=re.MULTILINE)
    assert "make gui" in makefile
    assert "make web" not in makefile
    assert " src.runtime gui" in makefile


def test_makefile_test_target_wraps_pytest_with_timeout() -> None:
    """Project test entrypoint bounds pytest hangs with the documented timeout."""
    makefile = (ROOT / "Makefile").read_text()

    assert re.search(
        r"^test:.*\n(?:\t.*\n)*\ttimeout 30 \$\(PYTHON\) -m pytest tests/ -v",
        makefile,
        flags=re.MULTILINE,
    )


def test_ops_docs_describe_gui_default_recovery_and_auth_boundary() -> None:
    """README documents the runtime contract operators need."""
    docs = {
        "README.md": (ROOT / "README.md").read_text(),
    }

    for name, text in docs.items():
        assert "dk-runtime gui" in text, name
        assert "Port 8080" in text or "port 8080" in text, name
        assert "DK_MODE=gui" in text, name
        assert "DK_MODE=cli" in text, name
        assert "recovery mode" in text, name
        assert re.search(r"without\s+built-in\s+authentication", text), name
        assert "Containers without explicit arguments" in text, name
        assert "dk web" not in text, name
        assert "make web" not in text, name
