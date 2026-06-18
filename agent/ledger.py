from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from agent.run_state import RunStatus


DB_PATH = Path("data/agent_ledger.db")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                user_instruction TEXT NOT NULL,
                final_summary TEXT NULL,
                error TEXT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata_json TEXT NULL
            )
            """
        )


def create_run(user_instruction: str) -> str:
    init_db()
    run_id = str(uuid.uuid4())
    now = _utc_now()

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id,
                created_at,
                updated_at,
                status,
                user_instruction,
                final_summary,
                error
            )
            VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """,
            (run_id, now, now, RunStatus.CREATED.value, user_instruction),
        )

    return run_id


def add_event(
    run_id: str,
    event_type: str,
    message: str,
    metadata: dict | None = None,
) -> None:
    init_db()
    metadata_json = json.dumps(metadata, sort_keys=True) if metadata is not None else None

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO events (
                run_id,
                created_at,
                event_type,
                message,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, _utc_now(), event_type, message, metadata_json),
        )


def update_run_status(
    run_id: str,
    status: RunStatus,
    final_summary: str | None = None,
    error: str | None = None,
) -> None:
    init_db()

    with _connect() as connection:
        connection.execute(
            """
            UPDATE runs
            SET updated_at = ?,
                status = ?,
                final_summary = ?,
                error = ?
            WHERE id = ?
            """,
            (_utc_now(), status.value, final_summary, error, run_id),
        )


def get_run(run_id: str) -> dict | None:
    init_db()

    with _connect() as connection:
        row = connection.execute(
            """
            SELECT
                id,
                created_at,
                updated_at,
                status,
                user_instruction,
                final_summary,
                error
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()

    return dict(row) if row is not None else None


def list_events(run_id: str) -> list[dict]:
    init_db()

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                run_id,
                created_at,
                event_type,
                message,
                metadata_json
            FROM events
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()

    return [dict(row) for row in rows]
