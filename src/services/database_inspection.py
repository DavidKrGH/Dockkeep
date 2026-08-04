"""Read-only inspection helpers for the AppData SQLite database."""

import asyncio
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlencode

from .run_history import default_appdata_db_path

PAGE_SIZE = 50
LONG_VALUE_PREVIEW_LENGTH = 96


@dataclass(frozen=True)
class InspectionTable:
    """Known AppData table exposed by the read-only diagnostics UI."""

    name: str
    label: str
    group: str
    order_by: str


@dataclass(frozen=True)
class RelationTarget:
    """Clickable relation target for a table cell."""

    section: str
    table: str
    column: str


TABLES: tuple[InspectionTable, ...] = (
    InspectionTable(
        "appdata_meta",
        "AppData-Meta",
        "system",
        "key ASC",
    ),
    InspectionTable(
        "runs",
        "Run-History",
        "runs",
        "COALESCE(finished_at, started_at) DESC, run_id DESC",
    ),
    InspectionTable(
        "run_steps",
        "Run-Steps",
        "runs",
        "run_id DESC, position ASC",
    ),
    InspectionTable(
        "run_restores",
        "Restore-Details",
        "restores",
        "run_id DESC, run_restore_id DESC",
    ),
    InspectionTable(
        "repositories",
        "Repositories",
        "repositories",
        "backend ASC, backend_repository_id ASC",
    ),
    InspectionTable(
        "repository_locations",
        "Repository-Locations",
        "repositories",
        "repository_location_key ASC, location_id DESC",
    ),
    InspectionTable(
        "artifacts",
        "Artifacts",
        "artifacts",
        "created_at DESC, artifact_id DESC",
    ),
    InspectionTable(
        "artifact_locations",
        "Artifact-Locations",
        "artifacts",
        "present DESC, artifact_id DESC",
    ),
    InspectionTable(
        "repository_stats_points",
        "Stats-Points",
        "stats",
        "collected_at DESC, stats_point_id DESC",
    ),
)

GROUPS: tuple[dict[str, str], ...] = (
    {"key": "system", "label": "System"},
    {"key": "runs", "label": "Runs"},
    {"key": "restores", "label": "Restores"},
    {"key": "repositories", "label": "Repositories"},
    {"key": "artifacts", "label": "Artifacts"},
    {"key": "stats", "label": "Stats"},
)

RELATION_TARGETS: dict[str, RelationTarget] = {
    "artifact_id": RelationTarget("artifacts", "artifacts", "artifact_id"),
    "repository_id": RelationTarget("repositories", "repositories", "repository_id"),
    "location_id": RelationTarget("repositories", "repository_locations", "location_id"),
    "run_id": RelationTarget("runs", "runs", "run_id"),
    "run_step_id": RelationTarget("runs", "run_steps", "run_step_id"),
    "created_run_step_id": RelationTarget("runs", "run_steps", "run_step_id"),
    "removed_by_run_step_id": RelationTarget("runs", "run_steps", "run_step_id"),
    "run_restore_id": RelationTarget("restores", "run_restores", "run_restore_id"),
    "stats_point_id": RelationTarget("stats", "repository_stats_points", "stats_point_id"),
    "backend_artifact_id": RelationTarget("artifacts", "artifacts", "backend_artifact_id"),
    "snapshot_id": RelationTarget("artifacts", "artifacts", "backend_artifact_id"),
}

# Columns whose cell link filters the table currently shown instead of jumping
# to the relation target: for these the id keeps its name across tables, so
# staying put groups the sibling rows (e.g. all steps of one run). Jumping to
# the owning row is still one click away, because the sidebar carries the
# active filter over to any table that has the same column.
SAME_TABLE_FILTER_COLUMNS = frozenset(
    {
        "artifact_id",
        "backend_artifact_id",
        "location_id",
        "repository_id",
        "run_id",
        "run_restore_id",
        "run_step_id",
        "stats_point_id",
    }
)

DATA_MODEL_CARDS: tuple[dict[str, object], ...] = (
    {
        "title": "Repository",
        "tables": ["repositories", "repository_locations"],
        "note": "Physical Restic repository and observed access locations.",
    },
    {
        "title": "Runs",
        "tables": ["runs", "run_steps", "run_restores"],
        "note": "Operational runs, job-task steps, and restore details.",
    },
    {
        "title": "Artifact",
        "tables": ["artifacts", "artifact_locations"],
        "note": "A backup artifact plus location observations.",
    },
    {
        "title": "Stats",
        "tables": ["repository_stats_points"],
        "note": "Repository growth observations per location.",
    },
)


class DatabaseInfoView(TypedDict):
    path: str
    exists: bool
    size_bytes: int | None
    wal_exists: bool
    wal_size_bytes: int | None
    shm_exists: bool
    appdata_dir: str


class TableGroupItemView(TypedDict):
    name: str
    label: str
    count: int | None
    exists: bool
    url: str


class TableGroupView(TypedDict):
    key: str
    label: str
    tables: list[TableGroupItemView]


class CellView(TypedDict):
    column: str
    value: Any
    display: str
    preview: str
    is_null: bool
    is_long: bool
    link: str | None


class ActiveFilterView(TypedDict):
    column: str
    value: str


class TableView(TypedDict, total=False):
    name: str
    label: str
    exists: bool
    columns: list[str]
    rows: list[dict[str, Any]]
    row_cells: list[list[CellView]]
    total: int
    page: int
    pages: int
    previous_page: int | None
    next_page: int | None
    previous_page_url: str | None
    next_page_url: str | None
    clear_filter_url: str
    filter: ActiveFilterView | None


class DatabaseView(TypedDict):
    db: DatabaseInfoView
    groups: list[TableGroupView]
    active_section: str
    active_table: str | None
    show_system_overview: bool
    integrity: str
    table: TableView | None
    page_size: int
    data_model: tuple[dict[str, object], ...]


class DatabaseInspectionService:

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_appdata_db_path()

    async def get_database_view(
        self,
        *,
        section: str = "system",
        table: str | None = None,
        page: int = 1,
        filter_column: str | None = None,
        filter_value: str | None = None,
    ) -> DatabaseView:
        return await asyncio.to_thread(
            self._get_database_view_sync,
            section=section,
            table=table,
            page=page,
            filter_column=filter_column,
            filter_value=filter_value,
        )

    def _get_database_view_sync(
        self,
        *,
        section: str = "system",
        table: str | None = None,
        page: int = 1,
        filter_column: str | None = None,
        filter_value: str | None = None,
    ) -> DatabaseView:
        page = max(1, page)
        active_section = section if section in {group["key"] for group in GROUPS} else "system"
        known_tables = list(TABLES)
        tables_by_name = {item.name: item for item in known_tables}
        table_for_section = _first_table_for_section(active_section, known_tables)
        active_table_name = table or table_for_section
        active_table = tables_by_name.get(active_table_name or "")
        if active_table is None or active_table.group != active_section:
            active_table = tables_by_name.get(table_for_section or "")
        show_system_overview = active_section == "system" and table is None

        db_info = self._db_info()
        table_view: TableView | None = None
        counts: dict[str, int | None] = {item.name: None for item in known_tables}
        # An active filter is carried over to every table that has the same
        # column, so both the nav link and its count must reflect it.
        nav_filters: dict[str, ActiveFilterView | None] = {item.name: None for item in known_tables}
        integrity = "missing"
        existing_tables: set[str] = set()

        if self.db_path.exists():
            with closing(self._connect()) as conn:
                existing_tables = _existing_tables(conn)
                for item in known_tables:
                    if item.name not in existing_tables:
                        continue
                    nav_filters[item.name] = _active_filter(
                        _columns(conn, item.name), filter_column, filter_value
                    )
                    counts[item.name] = _count_table(
                        conn, item.name, active_filter=nav_filters[item.name]
                    )
                integrity = _integrity_check(conn)
                if active_table is not None:
                    table_view = _read_table(
                        conn,
                        active_table,
                        page=page,
                        filter_column=filter_column,
                        filter_value=filter_value,
                    )

        grouped_tables: list[TableGroupView] = [
            {
                "key": group["key"],
                "label": group["label"],
                "tables": [
                    {
                        "name": item.name,
                        "label": item.label,
                        "count": counts[item.name],
                        "exists": item.name in existing_tables,
                        "url": _table_url(item, page=1, active_filter=nav_filters[item.name]),
                    }
                    for item in known_tables
                    if item.group == group["key"]
                ],
            }
            for group in GROUPS
        ]

        return {
            "db": db_info,
            "groups": grouped_tables,
            "active_section": active_section,
            "active_table": active_table.name if active_table else None,
            "show_system_overview": show_system_overview,
            "integrity": integrity,
            "table": table_view,
            "page_size": PAGE_SIZE,
            "data_model": DATA_MODEL_CARDS,
        }

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.db_path.resolve(strict=False).as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 1000")
        return conn

    def _db_info(self) -> DatabaseInfoView:
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        shm_path = self.db_path.with_name(f"{self.db_path.name}-shm")
        return {
            "path": str(self.db_path),
            "exists": self.db_path.exists(),
            "size_bytes": _file_size(self.db_path),
            "wal_exists": wal_path.exists(),
            "wal_size_bytes": _file_size(wal_path),
            "shm_exists": shm_path.exists(),
            "appdata_dir": str(Path(os.environ.get("DK_APPDATA_DIR", "/appdata"))),
        }


def _first_table_for_section(section: str, tables: list[InspectionTable]) -> str | None:
    for item in tables:
        if item.group == section:
            return item.name
    return None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _count_table(
    conn: sqlite3.Connection, table: str, *, active_filter: ActiveFilterView | None = None
) -> int:
    if active_filter is None:
        row = conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()
    else:
        row = conn.execute(
            f'SELECT COUNT(*) AS count FROM "{table}" ' f'WHERE "{active_filter["column"]}" = ?',
            (active_filter["value"],),
        ).fetchone()
    return int(row["count"])


def _integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row is not None else "unknown"


def _read_table(
    conn: sqlite3.Connection,
    table: InspectionTable,
    *,
    page: int,
    filter_column: str | None,
    filter_value: str | None,
) -> TableView:
    if table.name not in _existing_tables(conn):
        return {
            "name": table.name,
            "label": table.label,
            "exists": False,
            "columns": [],
            "rows": [],
            "total": 0,
            "page": page,
            "pages": 0,
            "filter": None,
        }
    columns = _columns(conn, table.name)
    active_filter = _active_filter(columns, filter_column, filter_value)
    total = _count_table(conn, table.name, active_filter=active_filter)
    offset = (page - 1) * PAGE_SIZE
    where_clause = ""
    params: list[object] = []
    if active_filter is not None:
        where_clause = f' WHERE "{active_filter["column"]}" = ?'
        params.append(active_filter["value"])
    params.extend([PAGE_SIZE, offset])
    rows = conn.execute(
        f'SELECT * FROM "{table.name}"{where_clause} '
        f"ORDER BY {table.order_by} LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 1
    row_dicts = [_row_dict(row) for row in rows]
    return {
        "name": table.name,
        "label": table.label,
        "exists": True,
        "columns": columns,
        "rows": row_dicts,
        "row_cells": [_row_cells(table, row, columns) for row in row_dicts],
        "total": total,
        "page": page,
        "pages": pages,
        "previous_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < pages else None,
        "previous_page_url": (
            _table_url(table, page=page - 1, active_filter=active_filter) if page > 1 else None
        ),
        "next_page_url": (
            _table_url(table, page=page + 1, active_filter=active_filter) if page < pages else None
        ),
        "clear_filter_url": _table_url(table, page=1, active_filter=None),
        "filter": active_filter,
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [str(row["name"]) for row in rows]


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _active_filter(
    columns: list[str], filter_column: str | None, filter_value: str | None
) -> ActiveFilterView | None:
    if not filter_column or filter_value is None or filter_column not in columns:
        return None
    return {"column": filter_column, "value": filter_value}


def _row_cells(table: InspectionTable, row: dict[str, Any], columns: list[str]) -> list[CellView]:
    return [_cell_view(table, column, row[column]) for column in columns]


def _cell_view(table: InspectionTable, column: str, value: Any) -> CellView:
    if value is None:
        return {
            "column": column,
            "value": None,
            "display": "NULL",
            "preview": "NULL",
            "is_null": True,
            "is_long": False,
            "link": None,
        }
    display = str(value)
    is_long = _is_long_value(column, display)
    return {
        "column": column,
        "value": value,
        "display": display,
        "preview": _preview(display) if is_long else display,
        "is_null": False,
        "is_long": is_long,
        "link": _relation_link(table, column, display),
    }


def _is_long_value(column: str, display: str) -> bool:
    column_name = column.lower()
    return (
        len(display) > LONG_VALUE_PREVIEW_LENGTH
        or "\n" in display
        or "json" in column_name
        or column_name
        in {"error", "output", "snapshot_paths", "include_patterns", "exclude_patterns"}
    )


def _preview(display: str) -> str:
    single_line = " ".join(display.splitlines())
    if len(single_line) <= LONG_VALUE_PREVIEW_LENGTH:
        return single_line
    return f"{single_line[:LONG_VALUE_PREVIEW_LENGTH].rstrip()}..."


def _relation_link(table: InspectionTable, column: str, value: str) -> str | None:
    column_name = column.lower()
    target = RELATION_TARGETS.get(column_name)
    if target is None or not value:
        return None
    if column_name in SAME_TABLE_FILTER_COLUMNS:
        target = RelationTarget(table.group, table.name, column)
    query = urlencode(
        {
            "section": target.section,
            "table": target.table,
            "filter_column": target.column,
            "filter_value": value,
        }
    )
    return f"/diagnostics/database?{query}"


def _table_url(table: InspectionTable, *, page: int, active_filter: ActiveFilterView | None) -> str:
    params = {
        "section": table.group,
        "table": table.name,
        "page": str(page),
    }
    if active_filter is not None:
        params["filter_column"] = active_filter["column"]
        params["filter_value"] = active_filter["value"]
    return f"/diagnostics/database?{urlencode(params)}"
