import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.gui.app import _compute_asset_version, create_app
from src.gui.filters import format_bytes, format_datetime, format_duration, relative_time
from src.services.errors import ServiceError

GUI_DIR = Path(__file__).resolve().parents[2] / "src" / "gui"


def test_lifespan_starts_and_stops_scheduler_owner_manager(tmp_path: Path) -> None:
    manager = MagicMock()
    with (
        patch("src.gui.app.SchedulerOwnerManager", return_value=manager) as owner_cls,
        patch("src.services.run_manager.RunManager.shutdown", new_callable=AsyncMock) as shutdown,
    ):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app):
            manager.start_background.assert_called_once_with()

    owner_cls.assert_called_once()
    assert owner_cls.call_args.kwargs["appdata_dir"] == tmp_path
    shutdown.assert_awaited_once_with(10.0)
    manager.stop.assert_called_once_with()


def test_create_app_does_not_expose_start_scheduler(tmp_path: Path) -> None:
    app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)

    assert not hasattr(app.state, "start_scheduler")
    assert not hasattr(app.state, "running_jobs")


def test_create_app_registers_shared_byte_formatter(tmp_path: Path) -> None:
    app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)

    assert app.state.templates.env.filters["format_bytes"](1024**3) == "1.0 GB"
    assert app.state.templates.env.filters["format_datetime"](None) == "–"


def test_create_app_registers_content_hashed_asset_version(tmp_path: Path) -> None:
    app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)

    asset_version = app.state.templates.env.globals["asset_version"]

    assert isinstance(asset_version, str)
    assert re.fullmatch(r"[0-9a-f]{12}", asset_version)
    assert asset_version != "24"


def test_asset_version_changes_when_static_content_changes(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    js_dir = static_dir / "js"
    js_dir.mkdir(parents=True)
    asset = js_dir / "app.js"
    asset.write_text("console.log('one');\n", encoding="utf-8")

    first_version = _compute_asset_version(static_dir)
    asset.write_text("console.log('two');\n", encoding="utf-8")
    second_version = _compute_asset_version(static_dir)

    assert re.fullmatch(r"[0-9a-f]{12}", first_version)
    assert re.fullmatch(r"[0-9a-f]{12}", second_version)
    assert second_version != first_version


def test_static_assets_are_served_locally(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            css_response = client.get("/static/css/app.css")
            htmx_response = client.get("/static/js/htmx.min.js")
            editor_response = client.get("/static/js/raw-config-editor.js")
            restore_response = client.get("/static/js/restore.js")

    assert css_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]
    assert htmx_response.status_code == 200
    assert editor_response.status_code == 200
    assert restore_response.status_code == 200


def test_main_pages_render_with_local_assets(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            for path in [
                "/",
                "/runs",
                "/repositories",
                "/config",
                "/config/raw",
                "/config/remotes",
                "/config/remotes/raw",
                "/diagnostics/logs",
                "/diagnostics/database",
                "/help",
            ]:
                response = client.get(path)

                assert response.status_code == 200
                # Offline invariant: pages must not reference external (CDN) assets.
                assert not re.search(
                    r"""(?:src|href)=["']https?://(?!testserver/static/)""",
                    response.text,
                )


def test_base_template_loads_only_global_gui_scripts() -> None:
    base_template = (GUI_DIR / "templates" / "base.html").read_text(encoding="utf-8")

    assert "/js/htmx.min.js" in base_template
    assert "/js/app.js" in base_template
    assert "/js/config-editor-form.js" not in base_template
    assert "/js/restore.js" not in base_template


def test_page_specific_scripts_are_not_loaded_from_global_bundle() -> None:
    app_script = (GUI_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "dkCreateLogStream" in app_script
    assert "dkFormatBytes" in app_script
    assert "dkAddRestorePath" not in app_script
    assert "restore-snapshot-paths" not in app_script
    assert "repository-browser-panel" not in app_script
    assert "data-list-empty" not in app_script


def test_config_editor_forms_load_config_editor_script_locally() -> None:
    form_templates = [
        "config_editor_backup.html",
        "config_editor_backup_defaults.html",
        "config_editor_job.html",
        "config_editor_overview.html",
        "config_editor_rclone.html",
        "config_editor_rclone_defaults.html",
        "config_editor_workflow.html",
    ]

    for template_name in form_templates:
        template = (GUI_DIR / "templates" / template_name).read_text(encoding="utf-8")
        assert "/js/config-editor-form.js" in template


def test_restore_pages_load_restore_script_locally() -> None:
    template = (GUI_DIR / "templates" / "repository_location_snapshot_restore.html").read_text(
        encoding="utf-8"
    )
    assert "/js/restore.js" in template
    assert "/js/config-editor-form.js" not in template


def test_config_navigation_groups_jobs_and_rclone_under_config(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            dashboard = client.get("/")
            config = client.get("/config")
            remotes = client.get("/config/remotes")

    assert dashboard.status_code == 200
    assert 'href="/config/jobs"' in dashboard.text
    assert "Configuration" in dashboard.text
    assert 'href="/rclone"' not in dashboard.text

    assert config.status_code == 200
    assert "Backup jobs" in config.text
    assert "Global settings" in config.text
    assert "Rclone remotes" in config.text
    assert 'href="/config/raw"' in config.text

    assert remotes.status_code == 200
    assert "Jobs" in remotes.text
    assert "Rclone remotes" in remotes.text
    assert 'href="/config/remotes/raw"' in remotes.text


def test_diagnostics_navigation_groups_logs_and_database(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            logs = client.get("/diagnostics/logs", follow_redirects=False)
            database = client.get("/diagnostics/database", follow_redirects=False)
            legacy_logs = client.get("/logs", follow_redirects=False)

    assert logs.status_code == 200
    assert database.status_code == 200
    assert legacy_logs.status_code == 404
    assert "Diagnostics" in logs.text
    assert "Logs" in logs.text
    assert "Database" in logs.text
    assert "Logs" in database.text
    assert "Database" in database.text


def test_service_error_renders_full_page_or_htmx_fragment(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)

        @app.get("/service-error")
        async def service_error_route() -> None:
            raise ServiceError("config_unavailable", "Configuration is unavailable", 503)

        with TestClient(app) as client:
            full_page = client.get("/service-error")
            fragment = client.get("/service-error", headers={"HX-Request": "true"})

    assert full_page.status_code == 503
    assert "text/html" in full_page.headers["content-type"]
    assert "<!DOCTYPE html>" in full_page.text
    assert "503 Service Unavailable" in full_page.text
    assert "config_unavailable" in full_page.text
    assert "Configuration is unavailable" in full_page.text

    assert fragment.status_code == 200
    assert "text/html" in fragment.headers["content-type"]
    assert "<!DOCTYPE html>" not in fragment.text
    assert "config_unavailable" in fragment.text
    assert "Configuration is unavailable" in fragment.text


def test_http_exception_renders_html_error_page(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            response = client.get("/missing")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "<!DOCTYPE html>" in response.text
    assert "404 Not Found" in response.text
    assert not response.text.lstrip().startswith("{")


def test_unexpected_exception_renders_html_error_page(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)

        @app.get("/unexpected")
        async def unexpected_route() -> None:
            raise RuntimeError("boom")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/unexpected")

    assert response.status_code == 500
    assert "text/html" in response.headers["content-type"]
    assert "unexpected_error" in response.text
    assert "Unexpected server error" in response.text


def test_help_page_serves_every_anchor_the_ui_links_to(tmp_path: Path) -> None:
    sources = [path.read_text(encoding="utf-8") for path in (GUI_DIR / "templates").rglob("*.html")]
    sources.append(
        (GUI_DIR.parent / "services" / "config_editor_schema.py").read_text(encoding="utf-8")
    )
    linked_anchors: set[str] = set()
    for pattern in (
        r'href="/help#([\w-]+)"',
        r'help_link\(\s*"([\w-]+)"',
        r'help_anchor="([\w-]+)"',
        r'"help_anchor":\s*"([\w-]+)"',
    ):
        found = {anchor for source in sources for anchor in re.findall(pattern, source)}
        assert found, pattern
        linked_anchors |= found

    manager = MagicMock()
    with (
        patch("src.gui.app.SchedulerOwnerManager", return_value=manager),
        patch("src.services.run_manager.RunManager.shutdown", new_callable=AsyncMock),
    ):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            response = client.get("/help")

    assert response.status_code == 200
    missing = {anchor for anchor in linked_anchors if f'id="{anchor}"' not in response.text}
    assert not missing


def test_removed_legacy_config_routes_return_not_found(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            legacy_get_paths = [
                "/config/editor",
                "/config/save",
                "/config/jobs/global",
                "/config/jobs/raw",
                "/rclone",
                "/rclone/raw",
            ]
            legacy_post_paths = ["/config/save", "/rclone/save"]

            for path in legacy_get_paths:
                response = client.get(path)

                assert response.status_code == 404

            assert client.get("/config/global").status_code == 405

            for path in legacy_post_paths:
                response = client.post(path)

                assert response.status_code == 404


def test_removed_scheduler_page_returns_not_found(tmp_path: Path) -> None:
    manager = MagicMock()
    with patch("src.gui.app.SchedulerOwnerManager", return_value=manager):
        app = create_app(tmp_path / "config.toml", appdata_dir=tmp_path)
        with TestClient(app) as client:
            response = client.get("/scheduler")

    assert response.status_code == 404


def test_templates_do_not_load_external_gui_assets() -> None:
    external_asset_pattern = re.compile(
        r"""(?:src|href|import\s+[^;]*?\s+from)\s*=\s*["']https?://|"""
        r"""import\s+[^;]*?\s+from\s+["']https?://""",
        re.IGNORECASE,
    )

    offenders = [
        path.relative_to(GUI_DIR).as_posix()
        for path in sorted((GUI_DIR / "templates").glob("*.html"))
        if external_asset_pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_static_assets_do_not_import_external_resources() -> None:
    external_resource_pattern = re.compile(
        r"""(?:import\s+[^;]*?\s+from\s+|import\s*\(|src\s*=\s*|href\s*=\s*)["']https?://|"""
        r"""(?:@import\s+|url\(\s*)["']?https?://""",
        re.IGNORECASE,
    )

    offenders = [
        path.relative_to(GUI_DIR).as_posix()
        for path in sorted((GUI_DIR / "static").rglob("*"))
        if path.is_file()
        and path.suffix in {".css", ".js"}
        and external_resource_pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_relative_time_filter_none_returns_placeholder() -> None:
    assert relative_time(None) == "–"


def test_relative_time_filter_invalid_string_returns_truncated_value() -> None:
    assert relative_time("not-a-timestamp") == "not-a-timestamp"


def test_relative_time_filter_just_now() -> None:
    now = datetime.now(timezone.utc)
    assert relative_time(now.isoformat()) == "just now"


def test_relative_time_filter_minutes_ago() -> None:
    ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert relative_time(ts.isoformat()) == "5 minutes ago"


def test_relative_time_filter_hours_ago() -> None:
    ts = datetime.now(timezone.utc) - timedelta(hours=3)
    assert relative_time(ts.isoformat()) == "3 hours ago"


def test_relative_time_filter_yesterday() -> None:
    ts = datetime.now(timezone.utc) - timedelta(hours=30)
    assert relative_time(ts.isoformat()) == "yesterday"


def test_relative_time_filter_days_ago() -> None:
    ts = datetime.now(timezone.utc) - timedelta(days=5)
    assert relative_time(ts.isoformat()) == "5 days ago"


def test_relative_time_filter_future_times_and_datetime_input() -> None:
    now = datetime.now(timezone.utc)
    assert relative_time(now + timedelta(minutes=30, seconds=30)) == "in 30 minutes"
    assert relative_time((now + timedelta(hours=14, minutes=5)).isoformat()) == "in 14 hours"
    assert relative_time(now + timedelta(days=3, hours=1)) == "in 3 days"


def test_relative_time_filter_handles_non_utc_offset_correctly() -> None:
    now_in_plus_two = datetime.now(timezone(timedelta(hours=2)))
    ts = now_in_plus_two - timedelta(minutes=5)
    assert relative_time(ts.isoformat()) == "5 minutes ago"


def test_relative_time_filter_naive_timestamp_compares_against_local_now() -> None:
    ts = datetime.now() - timedelta(minutes=10)
    assert relative_time(ts.isoformat()) == "10 minutes ago"


def test_relative_time_filter_parses_legacy_utc_strings() -> None:
    ts = datetime.now(timezone.utc) - timedelta(hours=3, minutes=5)
    assert relative_time(ts.strftime("%Y-%m-%d %H:%M:%S UTC")) == "3 hours ago"


def test_format_datetime_filter_none_returns_placeholder() -> None:
    assert format_datetime(None) == "–"
    assert format_datetime("") == "–"


def test_format_datetime_filter_formats_local_timestamp_without_seconds() -> None:
    assert format_datetime("2026-07-22T14:05:59") == "2026-07-22 14:05"


def test_format_datetime_filter_converts_aware_timestamp_to_local_timezone() -> None:
    ts = datetime(2026, 7, 22, 12, 5, 59, tzinfo=timezone.utc)
    expected = ts.astimezone().strftime("%Y-%m-%d %H:%M")

    assert format_datetime(ts.isoformat()) == expected
    assert format_datetime(ts.strftime("%Y-%m-%d %H:%M:%S UTC")) == expected


def test_format_datetime_filter_keeps_seconds_but_never_milliseconds() -> None:
    assert format_datetime("2026-07-22T14:05:59.123456", seconds=True) == "2026-07-22 14:05:59"
    assert format_datetime(None, seconds=True) == "–"


def test_format_datetime_filter_invalid_string_returns_truncated_value() -> None:
    assert format_datetime("2026-07-22Tbroken-value") == "2026-07-22 broke"


def test_format_duration_filter_none_returns_placeholder() -> None:
    assert format_duration(None) == "–"


def test_format_duration_filter_zero_seconds() -> None:
    assert format_duration(0) == "0s"


def test_format_duration_filter_seconds_only() -> None:
    assert format_duration(42) == "42s"


def test_format_duration_filter_minutes_and_seconds() -> None:
    assert format_duration(90) == "1m 30s"


def test_format_duration_filter_hours_and_minutes() -> None:
    assert format_duration(3661) == "1h 1m"


def test_format_duration_filter_invalid_value_returns_placeholder() -> None:
    assert format_duration("not-a-number") == "–"


def test_format_bytes_units() -> None:
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(1023) == "1023.0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"
    assert format_bytes(1024**3) == "1.0 GB"
    assert format_bytes(1024**4) == "1.0 TB"
    assert format_bytes(1024**5) == "1.0 PB"
    assert format_bytes(1536) == "1.5 KB"
