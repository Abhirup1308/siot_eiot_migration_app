from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    technical_objects_json TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    input_mode TEXT NOT NULL DEFAULT 'MANUAL',
    source_name TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    extraction_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    transform_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    validation_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    load_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    customer_approved INTEGER NOT NULL DEFAULT 0,
    production_approved INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    message TEXT
);


CREATE TABLE IF NOT EXISTS discovery_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    technical_object TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    indicator_count INTEGER,
    position_count INTEGER,
    result_json TEXT,
    error TEXT,
    updated_at TEXT,
    UNIQUE(run_id, technical_object),
    FOREIGN KEY(run_id) REFERENCES migration_runs(id)
);

CREATE TABLE IF NOT EXISTS extraction_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    request_id TEXT,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    zip_path TEXT,
    extract_dir TEXT,
    error TEXT,
    parent_job_id INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    completed_at TEXT,
    UNIQUE(run_id, position_id, date_from, date_to),
    FOREIGN KEY(run_id) REFERENCES migration_runs(id)
);

CREATE TABLE IF NOT EXISTS transform_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    extraction_job_id INTEGER,
    source_csv TEXT NOT NULL,
    source_name TEXT NOT NULL,
    output_parquet TEXT,
    status TEXT NOT NULL DEFAULT 'READY',
    source_rows INTEGER,
    selected_rows INTEGER,
    mapped_measurements INTEGER,
    target_rows INTEGER,
    mapped_combinations INTEGER,
    skipped_combinations INTEGER,
    error TEXT,
    updated_at TEXT,
    UNIQUE(run_id, source_csv),
    FOREIGN KEY(run_id) REFERENCES migration_runs(id),
    FOREIGN KEY(extraction_job_id) REFERENCES extraction_jobs(id)
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_id TEXT,
    status TEXT NOT NULL DEFAULT 'READY',
    description TEXT,
    number_of_records INTEGER,
    uploaded_time TEXT,
    processing_end_time TEXT,
    error TEXT,
    batch_no INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    source_file TEXT,
    last_checked_at TEXT,
    UNIQUE(run_id, phase, file_path),
    FOREIGN KEY(run_id) REFERENCES migration_runs(id)
);

CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    title TEXT NOT NULL,
    detail TEXT,
    FOREIGN KEY(run_id) REFERENCES migration_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_discovery_items_run ON discovery_items(run_id);
CREATE INDEX IF NOT EXISTS idx_discovery_items_status ON discovery_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_run ON extraction_jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_status ON extraction_jobs(run_id, status);
CREATE INDEX IF NOT EXISTS idx_transform_items_run ON transform_items(run_id);
CREATE INDEX IF NOT EXISTS idx_transform_items_status ON transform_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_uploads_run_phase ON uploads(run_id, phase);
CREATE INDEX IF NOT EXISTS idx_activity_events_run ON activity_events(run_id, created_at DESC);
"""


class MigrationDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            # WAL materially reduces contention when worker threads persist progress.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._ensure_column(conn, "migration_runs", "paused", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "migration_runs", "input_mode", "TEXT NOT NULL DEFAULT 'MANUAL'")
            self._ensure_column(conn, "migration_runs", "source_name", "TEXT")
            self._ensure_column(conn, "extraction_jobs", "parent_job_id", "INTEGER")
            self._ensure_column(conn, "extraction_jobs", "attempt", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "extraction_jobs", "updated_at", "TEXT")
            self._ensure_column(conn, "extraction_jobs", "completed_at", "TEXT")
            self._ensure_column(conn, "uploads", "batch_no", "INTEGER")
            self._ensure_column(conn, "uploads", "attempt", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "uploads", "source_file", "TEXT")
            self._ensure_column(conn, "uploads", "last_checked_at", "TEXT")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        technical_objects: list[str],
        date_from: str,
        date_to: str,
        *,
        input_mode: str = "MANUAL",
        source_name: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO migration_runs (
                    id, created_at, technical_objects_json, date_from, date_to,
                    input_mode, source_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now_iso(),
                    json.dumps(technical_objects),
                    date_from,
                    date_to,
                    input_mode,
                    source_name,
                ),
            )

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM migration_runs ORDER BY created_at DESC").fetchall()
        return [self._run_row(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM migration_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["technical_objects"] = json.loads(out.pop("technical_objects_json"))
        out["customer_approved"] = bool(out.get("customer_approved"))
        out["production_approved"] = bool(out.get("production_approved"))
        out["paused"] = bool(out.get("paused"))
        return out

    def update_run(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "discovery_status", "extraction_status", "transform_status",
            "validation_status", "load_status", "customer_approved",
            "production_approved", "paused", "message", "input_mode", "source_name",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {sorted(unknown)}")
        keys = list(values)
        params = [int(v) if isinstance(v, bool) else v for v in values.values()]
        with self.connect() as conn:
            conn.execute(
                f"UPDATE migration_runs SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                [*params, run_id],
            )

    def delete_run(self, run_id: str) -> None:
        with self.connect() as conn:
            # Explicit order keeps this compatible with old databases without CASCADE.
            conn.execute("DELETE FROM activity_events WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM uploads WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM transform_items WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM extraction_jobs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM discovery_items WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM migration_runs WHERE id = ?", (run_id,))

    # ------------------------------------------------------------------
    # Discovery items
    # ------------------------------------------------------------------
    def replace_discovery_items(self, run_id: str, technical_objects: Iterable[str]) -> None:
        rows = [str(v) for v in technical_objects]
        with self.connect() as conn:
            conn.execute("DELETE FROM discovery_items WHERE run_id = ?", (run_id,))
            conn.executemany(
                """
                INSERT INTO discovery_items (run_id, technical_object, status, updated_at)
                VALUES (?, ?, 'PENDING', ?)
                """,
                [(run_id, value, utc_now_iso()) for value in rows],
            )

    def list_discovery_items(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM discovery_items WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_discovery_item(self, item_id: int, **values: Any) -> None:
        if not values:
            return
        allowed = {"status", "indicator_count", "position_count", "result_json", "error"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported discovery fields: {sorted(unknown)}")
        payload = {**values, "updated_at": utc_now_iso()}
        keys = list(payload)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE discovery_items SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                [*payload.values(), item_id],
            )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def replace_extraction_jobs(self, run_id: str, jobs: Iterable[dict[str, Any]]) -> None:
        with self.connect() as conn:
            started = conn.execute(
                "SELECT COUNT(*) FROM extraction_jobs WHERE run_id = ? AND status != 'PLANNED'",
                (run_id,),
            ).fetchone()[0]
            if started:
                raise RuntimeError("Extraction has already started; the plan cannot be replaced.")
            conn.execute("DELETE FROM extraction_jobs WHERE run_id = ?", (run_id,))
            conn.executemany(
                """
                INSERT INTO extraction_jobs (
                    run_id, position_id, date_from, date_to, status, updated_at
                ) VALUES (?, ?, ?, ?, 'PLANNED', ?)
                """,
                [
                    (run_id, j["position_id"], j["date_from"], j["date_to"], utc_now_iso())
                    for j in jobs
                ],
            )

    def insert_extraction_jobs(self, run_id: str, jobs: Iterable[dict[str, Any]]) -> None:
        with self.connect() as conn:
            for job in jobs:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO extraction_jobs (
                        run_id, position_id, date_from, date_to, status,
                        parent_job_id, attempt, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        job["position_id"],
                        job["date_from"],
                        job["date_to"],
                        job.get("status", "PLANNED"),
                        job.get("parent_job_id"),
                        int(job.get("attempt", 1)),
                        utc_now_iso(),
                    ),
                )

    def list_extraction_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM extraction_jobs WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_extraction_job(self, job_id: int, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "request_id", "status", "zip_path", "extract_dir", "error",
            "parent_job_id", "attempt", "completed_at",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported extraction fields: {sorted(unknown)}")
        values = {**values, "updated_at": utc_now_iso()}
        keys = list(values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE extraction_jobs SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                [*values.values(), job_id],
            )

    # ------------------------------------------------------------------
    # Transform items
    # ------------------------------------------------------------------
    def upsert_transform_item(
        self,
        run_id: str,
        source_csv: str,
        source_name: str,
        *,
        extraction_job_id: int | None = None,
        **values: Any,
    ) -> None:
        allowed = {
            "output_parquet", "status", "source_rows", "selected_rows",
            "mapped_measurements", "target_rows", "mapped_combinations",
            "skipped_combinations", "error",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported transform fields: {sorted(unknown)}")
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM transform_items WHERE run_id = ? AND source_csv = ?",
                (run_id, source_csv),
            ).fetchone()
            if row:
                payload = {**values, "updated_at": now}
                if extraction_job_id is not None:
                    payload["extraction_job_id"] = extraction_job_id
                if payload:
                    keys = list(payload)
                    conn.execute(
                        f"UPDATE transform_items SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                        [*payload.values(), row["id"]],
                    )
            else:
                columns = ["run_id", "extraction_job_id", "source_csv", "source_name", "updated_at", *values.keys()]
                conn.execute(
                    f"INSERT INTO transform_items ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [run_id, extraction_job_id, source_csv, source_name, now, *values.values()],
                )

    def upsert_transform_items_bulk(
        self,
        run_id: str,
        items: Iterable[dict[str, Any]],
    ) -> None:
        """Register many extracted CSVs using one SQLite transaction."""
        now = utc_now_iso()
        rows = list(items)
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO transform_items (
                    run_id, extraction_job_id, source_csv, source_name, status, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, source_csv) DO UPDATE SET
                    extraction_job_id = excluded.extraction_job_id,
                    source_name = excluded.source_name,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        run_id,
                        item.get("extraction_job_id"),
                        item["source_csv"],
                        item["source_name"],
                        item.get("status", "READY"),
                        item.get("error"),
                        now,
                    )
                    for item in rows
                ],
            )

    def list_transform_items(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, e.position_id, e.date_from, e.date_to
                FROM transform_items t
                LEFT JOIN extraction_jobs e ON e.id = t.extraction_job_id
                WHERE t.run_id = ?
                ORDER BY t.id
                """,
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_transform_item(self, item_id: int, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "output_parquet", "status", "source_rows", "selected_rows",
            "mapped_measurements", "target_rows", "mapped_combinations",
            "skipped_combinations", "error",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported transform fields: {sorted(unknown)}")
        payload = {**values, "updated_at": utc_now_iso()}
        keys = list(payload)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE transform_items SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                [*payload.values(), item_id],
            )

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------
    def upsert_upload(
        self,
        run_id: str,
        phase: str,
        file_path: str,
        file_name: str,
        **values: Any,
    ) -> None:
        allowed = {
            "file_id", "status", "description", "number_of_records",
            "uploaded_time", "processing_end_time", "error", "batch_no",
            "attempt", "source_file", "last_checked_at",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported upload fields: {sorted(unknown)}")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM uploads WHERE run_id = ? AND phase = ? AND file_path = ?",
                (run_id, phase, file_path),
            ).fetchone()
            if row:
                if values:
                    keys = list(values)
                    conn.execute(
                        f"UPDATE uploads SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
                        [*values.values(), row["id"]],
                    )
            else:
                columns = ["run_id", "phase", "file_path", "file_name", *values.keys()]
                conn.execute(
                    f"INSERT INTO uploads ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [run_id, phase, file_path, file_name, *values.values()],
                )

    def upsert_uploads_bulk(
        self,
        run_id: str,
        phase: str,
        uploads: Iterable[dict[str, Any]],
    ) -> None:
        """Register many prepared upload files in one SQLite transaction."""
        rows = list(uploads)
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO uploads (
                    run_id, phase, file_path, file_name, status,
                    number_of_records, source_file, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, phase, file_path) DO UPDATE SET
                    file_name = excluded.file_name,
                    status = excluded.status,
                    number_of_records = excluded.number_of_records,
                    source_file = excluded.source_file,
                    error = excluded.error
                """,
                [
                    (
                        run_id, phase, item["file_path"], item["file_name"],
                        item.get("status", "READY"), item.get("number_of_records"),
                        item.get("source_file"), item.get("error"),
                    )
                    for item in rows
                ],
            )

    def list_uploads(self, run_id: str, phase: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if phase is None:
                rows = conn.execute("SELECT * FROM uploads WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM uploads WHERE run_id = ? AND phase = ? ORDER BY id",
                    (run_id, phase),
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_unstarted_uploads(self, run_id: str, phase: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM uploads WHERE run_id = ? AND phase = ? AND file_id IS NULL",
                (run_id, phase),
            )

    def update_upload_by_file_id(self, file_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "status", "description", "number_of_records", "processing_end_time",
            "error", "batch_no", "attempt", "last_checked_at",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported upload fields: {sorted(unknown)}")
        keys = list(values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE uploads SET {', '.join(f'{k} = ?' for k in keys)} WHERE file_id = ?",
                [*values.values(), file_id],
            )

    def next_batch_no(self, run_id: str) -> int:
        with self.connect() as conn:
            value = conn.execute(
                "SELECT COALESCE(MAX(batch_no), 0) FROM uploads WHERE run_id = ? AND phase = 'PRODUCTION'",
                (run_id,),
            ).fetchone()[0]
        return int(value or 0) + 1

    # ------------------------------------------------------------------
    # Activity
    # ------------------------------------------------------------------
    def add_event(
        self,
        run_id: str,
        stage: str,
        title: str,
        detail: str | None = None,
        *,
        level: str = "INFO",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO activity_events (run_id, created_at, stage, level, title, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, utc_now_iso(), stage, level, title, detail),
            )

    def list_events(self, run_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM activity_events WHERE run_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (run_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
