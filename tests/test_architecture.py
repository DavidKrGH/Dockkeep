"""Architecture boundary tests for the CLI/GUI/service layers."""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import fields
from pathlib import Path

from src.services.factory import ServiceContainer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
GUI_ROUTER_ROOT = SRC_ROOT / "gui" / "routers"
GUI_APP = SRC_ROOT / "gui" / "app.py"
GUI_TEMPLATE_ROOT = SRC_ROOT / "gui" / "templates"
GUI_CSS = SRC_ROOT / "gui" / "static" / "css" / "app.css"

CLASS_ATTR_RE = re.compile(r'class\s*=\s*(["\'])(.*?)\1', re.DOTALL)
JINJA_BLOCK_RE = re.compile(r"({[{%#].*?[}%]})", re.DOTALL)
QUOTED_STRING_RE = re.compile(r"([\"'])(.*?)\1", re.DOTALL)
CLASS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:/.\[\],%-]+$")

NON_CSS_CLASS_HOOKS = {
    "group",
    "only-when-selected",
    "field-info-panels",
}


def _python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.py") if path.name != "__init__.py")


def _template_files() -> list[Path]:
    return sorted(GUI_TEMPLATE_ROOT.rglob("*.html"))


def _module_name(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    return ".".join(relative.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _location(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(PROJECT_ROOT)}:{getattr(node, 'lineno', '?')}"


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _class_chunks(value: str) -> list[str]:
    """Return literal class chunks from an HTML class attribute."""
    chunks: list[str] = []
    position = 0
    for match in JINJA_BLOCK_RE.finditer(value):
        chunks.append(value[position : match.start()])
        jinja_block = match.group(1)
        chunks.extend(
            quoted.group(2)
            for quoted in QUOTED_STRING_RE.finditer(jinja_block)
            if _looks_like_class_literal(quoted.group(2))
        )
        position = match.end()
    chunks.append(value[position:])
    return chunks


def _looks_like_class_literal(value: str) -> bool:
    """Ignore plain Jinja comparison values such as 'green' or 'list'."""
    return any(marker in value for marker in ("-", ":", "[", "]"))


def _iter_template_classes() -> list[tuple[Path, int, str]]:
    classes: list[tuple[Path, int, str]] = []
    for path in _template_files():
        text = path.read_text(encoding="utf-8")
        for match in CLASS_ATTR_RE.finditer(text):
            line = _line_for_offset(text, match.start())
            for chunk in _class_chunks(match.group(2)):
                for token in re.split(r"\s+", chunk.strip()):
                    if token and CLASS_TOKEN_RE.fullmatch(token):
                        classes.append((path, line, token))
    return classes


def _defined_css_classes() -> set[str]:
    css = GUI_CSS.read_text(encoding="utf-8")
    classes: set[str] = set()
    for match in re.finditer(r"\.((?:\\.|[A-Za-z0-9_/-])+)", css):
        classes.add(re.sub(r"\\(.)", r"\1", match.group(1)))
    return classes


def _resolve_import_from(current_module: str, node: ast.ImportFrom) -> str:
    module = node.module or ""
    if node.level == 0:
        return module

    current_parts = current_module.split(".")
    base_parts = current_parts[: -node.level]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(base_parts)


def _iter_imports(path: Path) -> list[tuple[ast.AST, str, str | None]]:
    """Return imported module paths plus optional imported symbol names."""
    tree = _parse(path)
    current_module = _module_name(path)
    imports: list[tuple[ast.AST, str, str | None]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node, alias.name, None))
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(current_module, node)
            imports.append((node, module, None))
            for alias in node.names:
                if alias.name == "*":
                    imports.append((node, f"{module}.*", alias.name))
                else:
                    imports.append((node, f"{module}.{alias.name}", alias.name))
    return imports


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def test_domain_layers_do_not_import_gui_layer() -> None:
    """Domain and service layers must not depend on HTML adapters."""
    violations: list[str] = []
    for root in (SRC_ROOT / "services", SRC_ROOT / "core", SRC_ROOT / "models"):
        for path in _python_files(root):
            for node, imported, _symbol in _iter_imports(path):
                if imported in {"src.gui", "gui"} or imported.startswith(("src.gui.", "gui.")):
                    violations.append(f"{_location(path, node)} imports {imported}")

    assert not violations, "Forbidden gui-layer imports:\n" + "\n".join(violations)


def test_gui_routers_do_not_import_operational_domain_logic() -> None:
    """HTML routers may adapt service data, but must not own operational logic."""
    forbidden_modules = {
        "subprocess",
        "src.utils.restic",
        "src.utils.rclone",
    }
    forbidden_symbols = {
        "CronScheduler",
        "JobRunner",
        "LOG_BASE_DIR",
        "load_config",
    }
    violations: list[str] = []

    for path in _python_files(GUI_ROUTER_ROOT):
        for node, imported, symbol in _iter_imports(path):
            if imported in forbidden_modules or any(
                imported.startswith(f"{module}.") for module in forbidden_modules
            ):
                violations.append(f"{_location(path, node)} imports {imported}")
            if symbol in forbidden_symbols:
                violations.append(f"{_location(path, node)} imports {symbol}")
            if imported == "src.scheduler.cron" or imported.startswith("src.scheduler.cron."):
                violations.append(f"{_location(path, node)} imports {imported}")

    assert not violations, "Forbidden gui-router domain imports:\n" + "\n".join(violations)


def test_gui_routers_use_services_container_only() -> None:
    """HTML routers must not bypass the shared service container."""
    forbidden_imports = {
        "src.models.config",
        "src.utils.validation.load_config",
    }
    forbidden_state_fields = {field.name for field in fields(ServiceContainer)}
    violations: list[str] = []

    for path in _python_files(GUI_ROUTER_ROOT):
        for node, imported, _symbol in _iter_imports(path):
            if imported in forbidden_imports or any(
                imported.startswith(f"{module}.") for module in forbidden_imports
            ):
                violations.append(f"{_location(path, node)} imports {imported}")

        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == "load_active_config":
                violations.append(f"{_location(path, node)} calls load_active_config")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_build_"):
                if "config" in node.name or "view" in node.name:
                    violations.append(f"{_location(path, node)} defines {node.name}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden_state_fields:
                parent = _call_name(node.value)
                if parent and parent.endswith("app.state"):
                    violations.append(f"{_location(path, node)} accesses app.state.{node.attr}")

    assert not violations, "Forbidden gui-router service bypasses:\n" + "\n".join(violations)


def test_gui_routers_render_only_through_helpers() -> None:
    """Routers use shared helpers for templates and do not build HTML strings inline."""
    violations: list[str] = []
    allowed_helpers = GUI_ROUTER_ROOT / "_helpers.py"

    for path in _python_files(GUI_ROUTER_ROOT):
        if path == allowed_helpers:
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                parent = _call_name(node.value)
                if node.attr == "templates" and parent and parent.endswith("app.state"):
                    violations.append(f"{_location(path, node)} accesses app.state.templates")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.lstrip()
                if value.startswith(("<span", "<div", "<pre", "<button", "<table")):
                    violations.append(f"{_location(path, node)} contains inline HTML")

    assert not violations, "Forbidden gui-router rendering bypasses:\n" + "\n".join(violations)


def test_project_does_not_expose_json_api_package() -> None:
    """The project intentionally exposes CLI and GUI, not a JSON API."""
    assert not (SRC_ROOT / "api").exists()


def test_project_does_not_expose_legacy_web_package() -> None:
    """The GUI adapter package is src.gui; the former src.web path stays removed."""
    assert not (SRC_ROOT / "web").exists()


def test_public_repo_does_not_include_agent_specifications() -> None:
    """Agent-specific working notes stay out of the public repository surface."""
    tracked_files = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert "AGENTS.md" not in tracked_files
    assert "CLAUDE.md" not in tracked_files
    assert not any(path.startswith(".claude/") for path in tracked_files)


def test_gui_app_does_not_expose_legacy_runtime_state() -> None:
    """Manual run state lives in RunManager, not loose app.state fields."""
    tree = _parse(GUI_APP)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "running_jobs":
            parent = _call_name(node.value)
            if parent and parent.endswith("app.state"):
                violations.append(f"{_location(GUI_APP, node)} accesses app.state.running_jobs")

    assert not violations, "Forbidden legacy runtime state:\n" + "\n".join(violations)


def test_gui_template_classes_are_defined_in_app_css() -> None:
    """Tailwind-like template classes must be part of the local CSS layer."""
    defined_classes = _defined_css_classes()
    violations: list[str] = []

    for path, line, class_name in _iter_template_classes():
        if class_name in defined_classes or class_name in NON_CSS_CLASS_HOOKS:
            continue
        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line} uses .{class_name}")

    assert not violations, "Undefined GUI template classes:\n" + "\n".join(violations)


def test_gui_templates_do_not_define_inline_styles() -> None:
    """GUI templates keep shared styling in the local app.css layer."""
    violations: list[str] = []

    for path in _template_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<style\b", text, re.IGNORECASE):
            line = _line_for_offset(text, match.start())
            violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line} defines <style>")
        for match in re.finditer(r"\sstyle\s*=", text, re.IGNORECASE):
            line = _line_for_offset(text, match.start())
            violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line} defines style attribute")

    assert not violations, "Inline GUI template styles:\n" + "\n".join(violations)


def test_gui_routers_do_not_use_scheduler_start_hooks() -> None:
    """Starting or restarting the scheduler is runtime-manager responsibility."""
    violations: list[str] = []

    for path in _python_files(GUI_ROUTER_ROOT):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "start_scheduler":
                violations.append(f"{_location(path, node)} accesses start_scheduler")
            if isinstance(node, ast.Call) and _call_name(node.func) == "getattr":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    if node.args[1].value == "start_scheduler":
                        violations.append(f"{_location(path, node)} looks up start_scheduler")

    assert not violations, "Forbidden scheduler start hooks:\n" + "\n".join(violations)


def test_gui_routers_do_not_perform_direct_config_log_or_rclone_file_io() -> None:
    """Config, log and rclone file access belongs in services, not HTML routers."""
    forbidden_calls = {
        "open",
        "Path.read_text",
        "Path.write_text",
        "os.replace",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "NamedTemporaryFile",
        "TemporaryDirectory",
    }
    forbidden_attribute_calls = {"read_text", "write_text"}
    violations: list[str] = []

    for path in _python_files(GUI_ROUTER_ROOT):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node.func)
            if call_name in forbidden_calls:
                violations.append(f"{_location(path, node)} calls {call_name}")
            elif (
                isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_attribute_calls
            ):
                violations.append(f"{_location(path, node)} calls .{node.func.attr}()")

    assert not violations, "Forbidden direct gui-router file operations:\n" + "\n".join(violations)
