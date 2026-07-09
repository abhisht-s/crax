from __future__ import annotations

import json
import os
import secrets
import hashlib
import sqlite3
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent.run_state import RunStatus


DB_PATH = Path("data/agent_ledger.db")
RUN_DESTINATION_BOUND_EVENT_TYPE = "run_destination_bound"
RUN_DESTINATION_BOUND_MESSAGE = "Run destination bound."
RUN_DESTINATION_BOUND_SCHEMA_VERSION = 1
RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE = "run_execution_profile_selected"
RUN_EXECUTION_PROFILE_SELECTED_MESSAGE = "Run execution profile selected."
RUN_EXECUTION_PROFILE_SCHEMA_VERSION = 1
CODEX_DEFAULT_SELECTION = "codex_default"
ALLOWED_EXECUTION_PROFILE_SANDBOXES = ("read-only", "workspace-write")
ALLOWED_CODEX_MODEL_SELECTIONS = (
    CODEX_DEFAULT_SELECTION,
    "gpt-5",
    "gpt-5-codex",
)
ALLOWED_REASONING_EFFORT_SELECTIONS = (CODEX_DEFAULT_SELECTION,)
ALLOWED_APPROVAL_POLICY_SELECTIONS = (CODEX_DEFAULT_SELECTION,)
ALLOWED_EXECUTION_PROFILE_SOURCES = (
    "explicit_user_selection",
    "system_default",
    "legacy_compatibility",
)
CODEX_EXEC_STARTED_EVENT_TYPE = "codex_exec_started"
CODEX_PROGRESS_EVENT_TYPE = "codex_progress_event"
CODEX_PROGRESS_EVENT_MESSAGE = "Codex progress event."
CODEX_PROGRESS_SCHEMA_VERSION = 1
CODEX_PROGRESS_ALLOWED_KINDS = (
    "process_started",
    "codex_json_event",
    "command_started",
    "command_finished",
    "tool_event",
    "file_change_summary",
    "final_message_available",
    "process_exited",
    "error",
    "blocked",
)
CODEX_PROGRESS_DEFAULT_LIMIT = 100
CODEX_PROGRESS_MAX_LIMIT = 200
CODEX_PROGRESS_TEXT_LIMIT = 1000
CODEX_PROGRESS_TITLE_LIMIT = 240
CODEX_PROGRESS_METADATA_TEXT_LIMIT = 500
CODEX_PROGRESS_METADATA_JSON_LIMIT = 8 * 1024
CODEX_PROGRESS_SOURCE_DEFAULT = "codex_cli"
CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE = "chatgpt_ui_lease_acquired"
CHATGPT_UI_LEASE_ACQUIRED_MESSAGE = "ChatGPT Desktop UI lease acquired."
CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE = "chatgpt_ui_lease_released"
CHATGPT_UI_LEASE_RELEASED_MESSAGE = "ChatGPT Desktop UI lease released."
CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE = "chatgpt_ui_lease_acquire_denied"
CHATGPT_UI_LEASE_ACQUIRE_DENIED_MESSAGE = (
    "ChatGPT Desktop UI lease acquisition denied because another lease is active."
)
CHATGPT_UI_LEASE_SCHEMA_VERSION = 1
CHATGPT_UI_LEASE_EVENT_TYPES = (
    CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
    CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
)
CHATGPT_UI_LEASE_REDACTION_EVENT_TYPE = "chatgpt_ui_lease_token_redaction"
CHATGPT_UI_LEASE_REDACTION_MESSAGE = "ChatGPT Desktop UI lease token redaction completed."
CHATGPT_UI_LEASE_REDACTION_SCHEMA_VERSION = 1
CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION = 1
CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE = "chatgpt_handoff_enqueued"
CHATGPT_HANDOFF_ENQUEUED_MESSAGE = "Run enqueued for ChatGPT Desktop handoff."
CHATGPT_HANDOFF_CLAIMED_EVENT_TYPE = "chatgpt_handoff_claimed"
CHATGPT_HANDOFF_CLAIMED_MESSAGE = "ChatGPT Desktop handoff queue item claimed."
CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE = "chatgpt_handoff_completed"
CHATGPT_HANDOFF_COMPLETED_MESSAGE = "ChatGPT Desktop handoff queue item completed."
CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE = "chatgpt_handoff_blocked"
CHATGPT_HANDOFF_BLOCKED_MESSAGE = "ChatGPT Desktop handoff queue item blocked."
CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_EVENT_TYPE = "chatgpt_handoff_claim_unavailable"
CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_MESSAGE = "ChatGPT Desktop handoff queue claim unavailable."
CHATGPT_HANDOFF_CLAIM_DENIED_EVENT_TYPE = "chatgpt_handoff_claim_denied"
CHATGPT_HANDOFF_CLAIM_DENIED_MESSAGE = "ChatGPT Desktop handoff queue claim update denied."
CHATGPT_HANDOFF_QUEUE_STATE_EVENT_TYPES = (
    CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE,
    CHATGPT_HANDOFF_CLAIMED_EVENT_TYPE,
    CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE,
    CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE,
)
CHATGPT_HANDOFF_QUEUE_AUDIT_EVENT_TYPES = (
    CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_EVENT_TYPE,
    CHATGPT_HANDOFF_CLAIM_DENIED_EVENT_TYPE,
)
CHATGPT_HANDOFF_QUEUE_EVENT_TYPES = (
    *CHATGPT_HANDOFF_QUEUE_STATE_EVENT_TYPES,
    *CHATGPT_HANDOFF_QUEUE_AUDIT_EVENT_TYPES,
)


class AtomicDestinationBindingStatus(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    MISSING = "missing"
    BOUND = "bound"
    IDEMPOTENT = "idempotent"
    DIFFERENT_DESTINATION = "different_destination"
    INVALID = "invalid"
    OPERATIONAL_FAILURE = "operational_failure"


class AtomicExecutionProfileStatus(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    MISSING = "missing"
    SELECTED = "selected"
    IDEMPOTENT = "idempotent"
    DIFFERENT_PROFILE = "different_profile"
    EXECUTION_STARTED = "execution_started"
    INVALID = "invalid"
    OPERATIONAL_FAILURE = "operational_failure"


class AtomicChatGPTUILeaseStatus(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    MISSING = "missing"
    ACQUIRED = "acquired"
    ALREADY_HELD = "already_held"
    RELEASED = "released"
    IDEMPOTENT_RELEASE = "idempotent_release"
    TOKEN_MISMATCH = "token_mismatch"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ACTIVE_LEASE_MISMATCH = "active_lease_mismatch"
    INVALID = "invalid"
    OPERATIONAL_FAILURE = "operational_failure"


class AtomicChatGPTHandoffQueueStatus(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    MISSING = "missing"
    ENQUEUED = "enqueued"
    IDEMPOTENT = "idempotent"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    EMPTY = "empty"
    WAITING_FOR_ACTIVE_CLAIM = "waiting_for_active_claim"
    OWNER_MISMATCH = "owner_mismatch"
    NOT_CLAIMED = "not_claimed"
    INVALID = "invalid"
    OPERATIONAL_FAILURE = "operational_failure"


@dataclass(frozen=True)
class AtomicDestinationBindingResult:
    status: AtomicDestinationBindingStatus
    run_id: str
    project_title: str | None = None
    chat_title: str | None = None
    event_id: int | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AtomicExecutionProfileResult:
    status: AtomicExecutionProfileStatus
    run_id: str
    sandbox: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    approval_policy: str | None = None
    profile_source: str | None = None
    event_id: int | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AtomicChatGPTUILeaseResult:
    status: AtomicChatGPTUILeaseStatus
    run_id: str | None = None
    lease_token: str | None = None
    owner_pid: int | None = None
    owning_run_id: str | None = None
    acquired_at: str | None = None
    released_at: str | None = None
    active_event_id: int | None = None
    run_status: str | None = None
    event_id: int | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChatGPTUILeaseTokenRedactionResult:
    ok: bool
    events_redacted: int
    already_fingerprint_only: int
    skipped_invalid: int
    schema_version: int = CHATGPT_UI_LEASE_REDACTION_SCHEMA_VERSION
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict | None = None
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AtomicChatGPTHandoffQueueResult:
    status: AtomicChatGPTHandoffQueueStatus
    run_id: str | None = None
    queue_entry_id: str | None = None
    queue_sequence: int | None = None
    enqueue_source: str | None = None
    claim_owner_identifier: str | None = None
    claimed_at: str | None = None
    terminal_outcome: str | None = None
    terminal_reason_code: str | None = None
    event_id: int | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class _ChatGPTUILeaseState:
    status: AtomicChatGPTUILeaseStatus
    lease_token: str | None = None
    owner_pid: int | None = None
    owning_run_id: str | None = None
    acquired_at: str | None = None
    released_at: str | None = None
    active_event_id: int | None = None
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()
    released_tokens: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _HandoffQueueEntry:
    run_id: str
    queue_entry_id: str
    queue_sequence: int
    enqueue_source: str
    enqueue_event_id: int
    status: str
    claim_owner_identifier: str | None = None
    claimed_at: str | None = None
    terminal_outcome: str | None = None
    terminal_reason_code: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class _HandoffQueueState:
    status: AtomicChatGPTHandoffQueueStatus
    entries: tuple[_HandoffQueueEntry, ...] = ()
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _init_schema(connection: sqlite3.Connection) -> None:
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


def init_db() -> None:
    with closing(_connect()) as connection:
        with connection:
            _init_schema(connection)


def create_run(user_instruction: str) -> str:
    init_db()
    run_id = str(uuid.uuid4())
    now = _utc_now()

    with closing(_connect()) as connection:
        with connection:
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

    with closing(_connect()) as connection:
        with connection:
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


def add_codex_progress_event(
    run_id: str,
    codex_invocation_id: str,
    progress_event: dict[str, Any],
) -> dict[str, Any]:
    init_db()
    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        sequence = _next_event_id(connection)
        created_at = _utc_now()
        normalized = _normalize_codex_progress_event(
            run_id,
            codex_invocation_id,
            progress_event,
            sequence=sequence,
            created_at=created_at,
        )
        cursor = connection.execute(
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
            (
                run_id,
                created_at,
                CODEX_PROGRESS_EVENT_TYPE,
                normalized["title"],
                json.dumps(normalized, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        if isinstance(event_id, int) and event_id != sequence:
            normalized = {
                **normalized,
                "sequence": event_id,
                "event_id": event_id,
            }
            connection.execute(
                "UPDATE events SET metadata_json = ? WHERE id = ?",
                (json.dumps(normalized, sort_keys=True), event_id),
            )
        connection.commit()
        transaction_started = False
        return normalized
    except sqlite3.Error:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


def list_codex_progress_events(
    run_id: str,
    *,
    after_sequence: int = 0,
    limit: int = CODEX_PROGRESS_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    init_db()
    safe_after = max(0, int(after_sequence))
    safe_limit = max(1, min(int(limit), CODEX_PROGRESS_MAX_LIMIT))
    with closing(_connect()) as connection:
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
              AND event_type = ?
              AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (run_id, CODEX_PROGRESS_EVENT_TYPE, safe_after, safe_limit),
        ).fetchall()

    return [_codex_progress_event_from_row(row) for row in rows]


def update_run_status(
    run_id: str,
    status: RunStatus,
    final_summary: str | None = None,
    error: str | None = None,
) -> None:
    init_db()

    with closing(_connect()) as connection:
        with connection:
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

    with closing(_connect()) as connection:
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

    with closing(_connect()) as connection:
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


def list_chatgpt_ui_lease_events() -> list[dict]:
    init_db()

    with closing(_connect()) as connection:
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
            WHERE event_type IN (?, ?)
            ORDER BY id ASC
            """,
            CHATGPT_UI_LEASE_EVENT_TYPES,
        ).fetchall()

    return [dict(row) for row in rows]


def chatgpt_ui_lease_token_fingerprint(lease_token: str) -> str:
    return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()


def redact_chatgpt_ui_lease_tokens() -> ChatGPTUILeaseTokenRedactionResult:
    """Redact historical raw ChatGPT UI lease tokens from local SQLite events."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    redacted = 0
    already = 0
    skipped = 0
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        rows = connection.execute(
            """
            SELECT id, run_id, metadata_json
            FROM events
            WHERE event_type IN (?, ?)
            ORDER BY id ASC
            """,
            CHATGPT_UI_LEASE_EVENT_TYPES,
        ).fetchall()
        for row in rows:
            metadata = _metadata_dict_from_json(row["metadata_json"])
            if metadata is None:
                skipped += 1
                continue
            raw_token = metadata.get("lease_token")
            fingerprint = metadata.get("lease_token_sha256")
            if isinstance(raw_token, str) and raw_token:
                metadata["lease_token_sha256"] = chatgpt_ui_lease_token_fingerprint(raw_token)
                metadata.pop("lease_token", None)
                connection.execute(
                    "UPDATE events SET metadata_json = ? WHERE id = ?",
                    (json.dumps(metadata, sort_keys=True), row["id"]),
                )
                redacted += 1
                continue
            if isinstance(fingerprint, str) and _valid_sha256(fingerprint):
                already += 1
                continue
            skipped += 1

        metadata = {
            "schema_version": CHATGPT_UI_LEASE_REDACTION_SCHEMA_VERSION,
            "events_redacted": redacted,
            "already_fingerprint_only": already,
            "skipped_invalid": skipped,
        }
        cursor = connection.execute(
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
            (
                "local-maintenance",
                _utc_now(),
                CHATGPT_UI_LEASE_REDACTION_EVENT_TYPE,
                CHATGPT_UI_LEASE_REDACTION_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return ChatGPTUILeaseTokenRedactionResult(
            ok=True,
            events_redacted=redacted,
            already_fingerprint_only=already,
            skipped_invalid=skipped,
            event_type=CHATGPT_UI_LEASE_REDACTION_EVENT_TYPE,
            event_id=event_id if isinstance(event_id, int) else None,
            message=CHATGPT_UI_LEASE_REDACTION_MESSAGE,
            metadata=metadata,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return ChatGPTUILeaseTokenRedactionResult(
            ok=False,
            events_redacted=redacted,
            already_fingerprint_only=already,
            skipped_invalid=skipped,
            reason_code="chatgpt_ui_lease_redaction_failed",
            error_message=f"Failed to redact ChatGPT UI lease tokens: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def list_chatgpt_handoff_queue_events() -> list[dict]:
    init_db()

    placeholders = ", ".join("?" for _ in CHATGPT_HANDOFF_QUEUE_EVENT_TYPES)
    with closing(_connect()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                id,
                run_id,
                created_at,
                event_type,
                message,
                metadata_json
            FROM events
            WHERE event_type IN ({placeholders})
            ORDER BY id ASC
            """,
            CHATGPT_HANDOFF_QUEUE_EVENT_TYPES,
        ).fetchall()

    return [dict(row) for row in rows]


def enqueue_chatgpt_handoff(
    run_id: str,
    *,
    enqueue_source: str,
) -> AtomicChatGPTHandoffQueueResult:
    """Atomically enqueue a run for the serialized ChatGPT Desktop lane."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        run = connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.RUN_NOT_FOUND,
                run_id=run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
            )

        state = _reconstruct_handoff_queue_state(_select_handoff_queue_state_rows(connection))
        if state.status == AtomicChatGPTHandoffQueueStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return _handoff_queue_result_from_state(state, run_id=run_id)

        active = _active_handoff_entry_for_run(state, run_id)
        if active is not None:
            connection.rollback()
            transaction_started = False
            if active.status == "pending":
                return AtomicChatGPTHandoffQueueResult(
                    status=AtomicChatGPTHandoffQueueStatus.IDEMPOTENT,
                    run_id=active.run_id,
                    queue_entry_id=active.queue_entry_id,
                    queue_sequence=active.queue_sequence,
                    enqueue_source=active.enqueue_source,
                    event_written=False,
                    event_ids=active.event_ids,
                )
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.CLAIMED,
                run_id=active.run_id,
                queue_entry_id=active.queue_entry_id,
                queue_sequence=active.queue_sequence,
                enqueue_source=active.enqueue_source,
                claim_owner_identifier=active.claim_owner_identifier,
                claimed_at=active.claimed_at,
                event_written=False,
                reason_code="chatgpt_handoff_already_claimed",
                error_message="Run already has an active claimed ChatGPT handoff queue item.",
                event_ids=active.event_ids,
            )

        queue_sequence = _next_event_id(connection)
        queue_entry_id = f"chatgpt-handoff-{queue_sequence}"
        now = _utc_now()
        metadata = {
            "schema_version": CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION,
            "run_id": run_id,
            "queue_sequence": queue_sequence,
            "queue_entry_id": queue_entry_id,
            "enqueue_source": enqueue_source,
        }
        cursor = connection.execute(
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
            (
                run_id,
                now,
                CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE,
                CHATGPT_HANDOFF_ENQUEUED_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicChatGPTHandoffQueueResult(
            status=AtomicChatGPTHandoffQueueStatus.ENQUEUED,
            run_id=run_id,
            queue_entry_id=queue_entry_id,
            queue_sequence=queue_sequence,
            enqueue_source=enqueue_source,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicChatGPTHandoffQueueResult(
            status=AtomicChatGPTHandoffQueueStatus.OPERATIONAL_FAILURE,
            run_id=run_id,
            reason_code="chatgpt_handoff_enqueue_transaction_failed",
            error_message=f"Failed to enqueue ChatGPT handoff: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def claim_next_chatgpt_handoff(
    *,
    claim_owner_identifier: str,
) -> AtomicChatGPTHandoffQueueResult:
    """Atomically claim the oldest eligible pending ChatGPT handoff item."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        state = _reconstruct_handoff_queue_state(_select_handoff_queue_state_rows(connection))
        if state.status == AtomicChatGPTHandoffQueueStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return _handoff_queue_result_from_state(state)

        head = _oldest_active_handoff_entry(state)
        if head is None:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.EMPTY,
                reason_code="chatgpt_handoff_queue_empty",
                error_message="No pending ChatGPT handoff queue item is available.",
                event_ids=state.event_ids,
            )

        if head.status == "claimed":
            metadata = {
                "schema_version": CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION,
                "run_id": head.run_id,
                "queue_sequence": head.queue_sequence,
                "queue_entry_id": head.queue_entry_id,
                "claim_owner_identifier": claim_owner_identifier,
                "active_claim_owner_identifier": head.claim_owner_identifier,
                "reason_code": "chatgpt_handoff_queue_head_already_claimed",
            }
            cursor = connection.execute(
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
                (
                    head.run_id,
                    _utc_now(),
                    CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_EVENT_TYPE,
                    CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_MESSAGE,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            event_id = cursor.lastrowid
            connection.commit()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.WAITING_FOR_ACTIVE_CLAIM,
                run_id=head.run_id,
                queue_entry_id=head.queue_entry_id,
                queue_sequence=head.queue_sequence,
                enqueue_source=head.enqueue_source,
                claim_owner_identifier=head.claim_owner_identifier,
                claimed_at=head.claimed_at,
                event_id=event_id if isinstance(event_id, int) else None,
                event_written=True,
                reason_code="chatgpt_handoff_queue_head_already_claimed",
                error_message="The oldest active ChatGPT handoff queue item is already claimed.",
                event_ids=head.event_ids,
            )

        now = _utc_now()
        metadata = {
            "schema_version": CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION,
            "run_id": head.run_id,
            "queue_sequence": head.queue_sequence,
            "queue_entry_id": head.queue_entry_id,
            "claim_owner_identifier": claim_owner_identifier,
            "claimed_at": now,
        }
        cursor = connection.execute(
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
            (
                head.run_id,
                now,
                CHATGPT_HANDOFF_CLAIMED_EVENT_TYPE,
                CHATGPT_HANDOFF_CLAIMED_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicChatGPTHandoffQueueResult(
            status=AtomicChatGPTHandoffQueueStatus.CLAIMED,
            run_id=head.run_id,
            queue_entry_id=head.queue_entry_id,
            queue_sequence=head.queue_sequence,
            enqueue_source=head.enqueue_source,
            claim_owner_identifier=claim_owner_identifier,
            claimed_at=now,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
            event_ids=head.event_ids,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicChatGPTHandoffQueueResult(
            status=AtomicChatGPTHandoffQueueStatus.OPERATIONAL_FAILURE,
            reason_code="chatgpt_handoff_claim_transaction_failed",
            error_message=f"Failed to claim ChatGPT handoff: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def complete_chatgpt_handoff(
    queue_sequence: int,
    *,
    claim_owner_identifier: str,
    reason_code: str,
    lease_correlation: dict[str, object] | None = None,
) -> AtomicChatGPTHandoffQueueResult:
    return _finish_chatgpt_handoff(
        queue_sequence,
        claim_owner_identifier=claim_owner_identifier,
        event_type=CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE,
        message=CHATGPT_HANDOFF_COMPLETED_MESSAGE,
        terminal_outcome="completed",
        reason_code=reason_code,
        lease_correlation=lease_correlation,
    )


def block_chatgpt_handoff(
    queue_sequence: int,
    *,
    claim_owner_identifier: str,
    reason_code: str,
    lease_correlation: dict[str, object] | None = None,
) -> AtomicChatGPTHandoffQueueResult:
    return _finish_chatgpt_handoff(
        queue_sequence,
        claim_owner_identifier=claim_owner_identifier,
        event_type=CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE,
        message=CHATGPT_HANDOFF_BLOCKED_MESSAGE,
        terminal_outcome="blocked",
        reason_code=reason_code,
        lease_correlation=lease_correlation,
    )


def acquire_chatgpt_ui_lease(
    run_id: str,
    *,
    reason: str | None = None,
    source: str | None = None,
) -> AtomicChatGPTUILeaseResult:
    """Atomically acquire the process-global ChatGPT Desktop UI lease."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        run = connection.execute(
            "SELECT id FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.RUN_NOT_FOUND,
                run_id=run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
            )

        rows = _select_chatgpt_ui_lease_rows(connection)
        existing = _reconstruct_chatgpt_ui_lease_state(rows)
        if existing.status == AtomicChatGPTUILeaseStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return _lease_result_from_state(existing, run_id=run_id)

        now = _utc_now()
        if existing.status == AtomicChatGPTUILeaseStatus.ACQUIRED:
            metadata = _compact_optional_metadata(
                {
                    "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
                    "requested_owning_run_id": run_id,
                    "request_owner_pid": os.getpid(),
                    "denied_at": now,
                    "active_owning_run_id": existing.owning_run_id,
                    "active_owner_pid": existing.owner_pid,
                    "active_acquired_at": existing.acquired_at,
                    "reason": reason,
                    "source": source,
                }
            )
            cursor = connection.execute(
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
                (
                    run_id,
                    now,
                    CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE,
                    CHATGPT_UI_LEASE_ACQUIRE_DENIED_MESSAGE,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            event_id = cursor.lastrowid
            connection.commit()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.ALREADY_HELD,
                run_id=run_id,
                owner_pid=existing.owner_pid,
                owning_run_id=existing.owning_run_id,
                acquired_at=existing.acquired_at,
                active_event_id=existing.active_event_id,
                event_id=event_id if isinstance(event_id, int) else None,
                event_written=True,
                reason_code="chatgpt_ui_lease_already_held",
                error_message="ChatGPT Desktop UI lease is already active.",
                event_ids=existing.event_ids,
            )

        lease_token = secrets.token_urlsafe(32)
        metadata = _compact_optional_metadata(
            {
                "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
                "lease_token_sha256": chatgpt_ui_lease_token_fingerprint(lease_token),
                "owner_pid": os.getpid(),
                "owning_run_id": run_id,
                "acquired_at": now,
                "reason": reason,
                "source": source,
            }
        )
        cursor = connection.execute(
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
            (
                run_id,
                now,
                CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
                CHATGPT_UI_LEASE_ACQUIRED_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.ACQUIRED,
            run_id=run_id,
            lease_token=lease_token,
            owner_pid=os.getpid(),
            owning_run_id=run_id,
            acquired_at=now,
            active_event_id=event_id if isinstance(event_id, int) else None,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.OPERATIONAL_FAILURE,
            run_id=run_id,
            reason_code="chatgpt_ui_lease_acquire_transaction_failed",
            error_message=f"Failed to acquire ChatGPT Desktop UI lease: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def release_chatgpt_ui_lease(
    lease_token: str,
    *,
    reason: str | None = None,
    source: str | None = None,
) -> AtomicChatGPTUILeaseResult:
    """Release the active ChatGPT Desktop UI lease only with its opaque token."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        lease_token_sha256 = chatgpt_ui_lease_token_fingerprint(lease_token)
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        rows = _select_chatgpt_ui_lease_rows(connection)
        existing = _reconstruct_chatgpt_ui_lease_state(rows)
        if existing.status == AtomicChatGPTUILeaseStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return _lease_result_from_state(existing)

        if existing.status == AtomicChatGPTUILeaseStatus.MISSING:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.IDEMPOTENT_RELEASE
                if lease_token_sha256 in existing.released_tokens
                else AtomicChatGPTUILeaseStatus.MISSING,
                lease_token=lease_token
                if lease_token_sha256 in existing.released_tokens
                else None,
                reason_code=(
                    None
                    if lease_token_sha256 in existing.released_tokens
                    else "chatgpt_ui_lease_not_active"
                ),
                error_message=(
                    None
                    if lease_token_sha256 in existing.released_tokens
                    else "No ChatGPT Desktop UI lease is active."
                ),
                event_ids=existing.event_ids,
            )

        if existing.lease_token != lease_token_sha256:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.TOKEN_MISMATCH,
                owner_pid=existing.owner_pid,
                owning_run_id=existing.owning_run_id,
                acquired_at=existing.acquired_at,
                active_event_id=existing.active_event_id,
                reason_code="chatgpt_ui_lease_token_mismatch",
                error_message="ChatGPT Desktop UI lease token does not match the active lease.",
                event_ids=existing.event_ids,
            )

        now = _utc_now()
        metadata = _compact_optional_metadata(
            {
                "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
                "lease_token_sha256": chatgpt_ui_lease_token_fingerprint(lease_token),
                "owner_pid": existing.owner_pid,
                "owning_run_id": existing.owning_run_id,
                "acquired_at": existing.acquired_at,
                "released_at": now,
                "reason": reason,
                "source": source,
            }
        )
        cursor = connection.execute(
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
            (
                existing.owning_run_id,
                now,
                CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
                CHATGPT_UI_LEASE_RELEASED_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.RELEASED,
            run_id=existing.owning_run_id,
            lease_token=lease_token,
            owner_pid=existing.owner_pid,
            owning_run_id=existing.owning_run_id,
            acquired_at=existing.acquired_at,
            released_at=now,
            active_event_id=existing.active_event_id,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
            event_ids=existing.event_ids,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.OPERATIONAL_FAILURE,
            reason_code="chatgpt_ui_lease_release_transaction_failed",
            error_message=f"Failed to release ChatGPT Desktop UI lease: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def manual_release_stale_chatgpt_ui_lease(
    *,
    owning_run_id: str,
    owner_pid: int,
    acquired_at: str,
    active_event_id: int,
    reason: str,
    source: str = "manual_stale_release",
    confirm_stale: bool = False,
    expected_run_status: str | None = None,
    expected_lease_token_sha256: str | None = None,
) -> AtomicChatGPTUILeaseResult:
    """Append a manual release for an operator-confirmed stale UI lease.

    This intentionally does not steal a lease or infer staleness. The caller must
    provide exact metadata for the currently active lease and set confirm_stale.
    """

    normalized_run_id = owning_run_id.strip() if isinstance(owning_run_id, str) else ""
    normalized_acquired_at = acquired_at.strip() if isinstance(acquired_at, str) else ""
    normalized_reason = reason.strip() if isinstance(reason, str) else ""
    normalized_source = source.strip() if isinstance(source, str) else ""
    normalized_expected_status = (
        expected_run_status.strip()
        if isinstance(expected_run_status, str) and expected_run_status.strip()
        else None
    )
    normalized_expected_token = (
        expected_lease_token_sha256.strip()
        if isinstance(expected_lease_token_sha256, str)
        and expected_lease_token_sha256.strip()
        else None
    )

    if not confirm_stale:
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.CONFIRMATION_REQUIRED,
            run_id=normalized_run_id or None,
            reason_code="manual_stale_lease_confirmation_required",
            error_message="Manual stale ChatGPT UI lease release requires confirm_stale=True.",
        )
    if (
        not normalized_run_id
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or not normalized_acquired_at
        or not isinstance(active_event_id, int)
        or active_event_id <= 0
        or not normalized_reason
        or not normalized_source
    ):
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.INVALID,
            run_id=normalized_run_id or None,
            reason_code="invalid_manual_stale_lease_release_request",
            error_message=(
                "Manual stale ChatGPT UI lease release requires owning_run_id, "
                "owner_pid, acquired_at, active_event_id, reason, and source."
            ),
        )
    if normalized_expected_token is not None and not _valid_sha256(normalized_expected_token):
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.INVALID,
            run_id=normalized_run_id,
            reason_code="invalid_expected_lease_token_sha256",
            error_message="expected_lease_token_sha256 must be a lowercase SHA-256 hex digest.",
        )

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        run = connection.execute(
            "SELECT id, status FROM runs WHERE id = ?",
            (normalized_run_id,),
        ).fetchone()
        if run is None:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.RUN_NOT_FOUND,
                run_id=normalized_run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {normalized_run_id}",
            )

        run_status = str(run["status"])
        if normalized_expected_status is not None and run_status != normalized_expected_status:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.ACTIVE_LEASE_MISMATCH,
                run_id=normalized_run_id,
                owner_pid=owner_pid,
                owning_run_id=normalized_run_id,
                acquired_at=normalized_acquired_at,
                active_event_id=active_event_id,
                run_status=run_status,
                reason_code="manual_stale_lease_run_status_mismatch",
                error_message="Run status no longer matches the expected stale lease owner state.",
            )

        rows = _select_chatgpt_ui_lease_rows(connection)
        existing = _reconstruct_chatgpt_ui_lease_state(rows)
        if existing.status == AtomicChatGPTUILeaseStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return _lease_result_from_state(existing, run_id=normalized_run_id)

        if existing.status == AtomicChatGPTUILeaseStatus.MISSING:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.MISSING,
                run_id=normalized_run_id,
                reason_code="chatgpt_ui_lease_not_active",
                error_message="No ChatGPT Desktop UI lease is active.",
                event_ids=existing.event_ids,
            )

        mismatch = (
            existing.owning_run_id != normalized_run_id
            or existing.owner_pid != owner_pid
            or existing.acquired_at != normalized_acquired_at
            or existing.active_event_id != active_event_id
            or (
                normalized_expected_token is not None
                and existing.lease_token != normalized_expected_token
            )
        )
        if mismatch:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTUILeaseResult(
                status=AtomicChatGPTUILeaseStatus.ACTIVE_LEASE_MISMATCH,
                run_id=normalized_run_id,
                owner_pid=existing.owner_pid,
                owning_run_id=existing.owning_run_id,
                acquired_at=existing.acquired_at,
                active_event_id=existing.active_event_id,
                run_status=run_status,
                reason_code="active_chatgpt_ui_lease_mismatch",
                error_message="Active ChatGPT Desktop UI lease does not match the expected stale lease.",
                event_ids=existing.event_ids,
            )

        now = _utc_now()
        metadata = _compact_optional_metadata(
            {
                "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
                "lease_token_sha256": existing.lease_token,
                "owner_pid": existing.owner_pid,
                "owning_run_id": existing.owning_run_id,
                "acquired_at": existing.acquired_at,
                "released_at": now,
                "reason": normalized_reason,
                "source": normalized_source,
                "manual_release": True,
                "stale_confirmed": True,
                "active_acquire_event_id": existing.active_event_id,
                "verified_run_status": run_status,
            }
        )
        cursor = connection.execute(
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
            (
                existing.owning_run_id,
                now,
                CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
                CHATGPT_UI_LEASE_RELEASED_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.RELEASED,
            run_id=existing.owning_run_id,
            owner_pid=existing.owner_pid,
            owning_run_id=existing.owning_run_id,
            acquired_at=existing.acquired_at,
            released_at=now,
            active_event_id=existing.active_event_id,
            run_status=run_status,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
            event_ids=existing.event_ids,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicChatGPTUILeaseResult(
            status=AtomicChatGPTUILeaseStatus.OPERATIONAL_FAILURE,
            run_id=normalized_run_id or None,
            reason_code="manual_stale_lease_release_transaction_failed",
            error_message=f"Failed to manually release stale ChatGPT Desktop UI lease: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def bind_run_destination(
    run_id: str,
    project_title: str,
    chat_title: str,
) -> AtomicDestinationBindingResult:
    """Atomically bind an existing run to one destination ledger event."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        run = connection.execute(
            "SELECT id FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            connection.rollback()
            transaction_started = False
            return AtomicDestinationBindingResult(
                status=AtomicDestinationBindingStatus.RUN_NOT_FOUND,
                run_id=run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
            )

        rows = connection.execute(
            """
            SELECT id, metadata_json
            FROM events
            WHERE run_id = ?
              AND event_type = ?
            ORDER BY id ASC
            """,
            (run_id, RUN_DESTINATION_BOUND_EVENT_TYPE),
        ).fetchall()
        existing = _reconstruct_destination_binding(run_id, rows)
        if existing.status == AtomicDestinationBindingStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return existing

        if existing.status == AtomicDestinationBindingStatus.IDEMPOTENT:
            connection.rollback()
            transaction_started = False
            if (
                existing.project_title == project_title
                and existing.chat_title == chat_title
            ):
                return AtomicDestinationBindingResult(
                    status=AtomicDestinationBindingStatus.IDEMPOTENT,
                    run_id=run_id,
                    project_title=existing.project_title,
                    chat_title=existing.chat_title,
                    event_ids=existing.event_ids,
                )

            return AtomicDestinationBindingResult(
                status=AtomicDestinationBindingStatus.DIFFERENT_DESTINATION,
                run_id=run_id,
                project_title=existing.project_title,
                chat_title=existing.chat_title,
                reason_code="destination_already_bound_to_different_destination",
                error_message="Run is already bound to a different destination.",
                event_ids=existing.event_ids,
            )

        metadata = {
            "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
            "project_title": project_title,
            "chat_title": chat_title,
        }
        cursor = connection.execute(
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
            (
                run_id,
                _utc_now(),
                RUN_DESTINATION_BOUND_EVENT_TYPE,
                RUN_DESTINATION_BOUND_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicDestinationBindingResult(
            status=AtomicDestinationBindingStatus.BOUND,
            run_id=run_id,
            project_title=project_title,
            chat_title=chat_title,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicDestinationBindingResult(
            status=AtomicDestinationBindingStatus.OPERATIONAL_FAILURE,
            run_id=run_id,
            reason_code="destination_binding_transaction_failed",
            error_message=f"Failed to bind run destination: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def bind_run_execution_profile(
    run_id: str,
    sandbox: str,
    model: str,
    reasoning_effort: str,
    approval_policy: str,
    profile_source: str,
) -> AtomicExecutionProfileResult:
    """Atomically select one immutable execution profile for an existing run."""

    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        run = connection.execute(
            "SELECT id FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            connection.rollback()
            transaction_started = False
            return AtomicExecutionProfileResult(
                status=AtomicExecutionProfileStatus.RUN_NOT_FOUND,
                run_id=run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
            )

        rows = connection.execute(
            """
            SELECT id, metadata_json
            FROM events
            WHERE run_id = ?
              AND event_type = ?
            ORDER BY id ASC
            """,
            (run_id, RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE),
        ).fetchall()
        existing = _reconstruct_execution_profile(run_id, rows)
        if existing.status == AtomicExecutionProfileStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return existing

        execution_started = connection.execute(
            """
            SELECT 1
            FROM events
            WHERE run_id = ?
              AND event_type = ?
            LIMIT 1
            """,
            (run_id, CODEX_EXEC_STARTED_EVENT_TYPE),
        ).fetchone()
        if execution_started is not None:
            connection.rollback()
            transaction_started = False
            return AtomicExecutionProfileResult(
                status=AtomicExecutionProfileStatus.EXECUTION_STARTED,
                run_id=run_id,
                sandbox=existing.sandbox,
                model=existing.model,
                reasoning_effort=existing.reasoning_effort,
                approval_policy=existing.approval_policy,
                profile_source=existing.profile_source,
                reason_code="execution_profile_immutable_after_codex_exec_started",
                error_message=(
                    "Run execution profile cannot be selected after Codex execution "
                    "has started."
                ),
                event_ids=existing.event_ids,
            )

        if existing.status == AtomicExecutionProfileStatus.IDEMPOTENT:
            connection.rollback()
            transaction_started = False
            if (
                existing.sandbox == sandbox
                and existing.model == model
                and existing.reasoning_effort == reasoning_effort
                and existing.approval_policy == approval_policy
                and existing.profile_source == profile_source
            ):
                return AtomicExecutionProfileResult(
                    status=AtomicExecutionProfileStatus.IDEMPOTENT,
                    run_id=run_id,
                    sandbox=existing.sandbox,
                    model=existing.model,
                    reasoning_effort=existing.reasoning_effort,
                    approval_policy=existing.approval_policy,
                    profile_source=existing.profile_source,
                    event_ids=existing.event_ids,
                )

            return AtomicExecutionProfileResult(
                status=AtomicExecutionProfileStatus.DIFFERENT_PROFILE,
                run_id=run_id,
                sandbox=existing.sandbox,
                model=existing.model,
                reasoning_effort=existing.reasoning_effort,
                approval_policy=existing.approval_policy,
                profile_source=existing.profile_source,
                reason_code="execution_profile_already_selected_different_profile",
                error_message="Run already has a different execution profile.",
                event_ids=existing.event_ids,
            )

        metadata = {
            "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
            "sandbox": sandbox,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "approval_policy": approval_policy,
            "profile_source": profile_source,
        }
        cursor = connection.execute(
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
            (
                run_id,
                _utc_now(),
                RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
                RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicExecutionProfileResult(
            status=AtomicExecutionProfileStatus.SELECTED,
            run_id=run_id,
            sandbox=sandbox,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            profile_source=profile_source,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicExecutionProfileResult(
            status=AtomicExecutionProfileStatus.OPERATIONAL_FAILURE,
            run_id=run_id,
            reason_code="execution_profile_transaction_failed",
            error_message=f"Failed to select run execution profile: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def _reconstruct_destination_binding(
    run_id: str,
    rows: list[sqlite3.Row],
) -> AtomicDestinationBindingResult:
    event_ids = tuple(
        event_id
        for row in rows
        if isinstance(event_id := row["id"], int)
    )
    if not rows:
        return AtomicDestinationBindingResult(
            status=AtomicDestinationBindingStatus.MISSING,
            run_id=run_id,
        )

    bindings: list[tuple[str, str]] = []
    for row in rows:
        binding = _destination_binding_from_metadata_json(row["metadata_json"])
        if binding is None:
            return AtomicDestinationBindingResult(
                status=AtomicDestinationBindingStatus.INVALID,
                run_id=run_id,
                reason_code="malformed_destination_binding_event",
                error_message="Run destination binding event metadata is malformed.",
                event_ids=event_ids,
            )
        bindings.append(binding)

    unique_bindings = frozenset(bindings)
    if len(unique_bindings) != 1:
        return AtomicDestinationBindingResult(
            status=AtomicDestinationBindingStatus.INVALID,
            run_id=run_id,
            reason_code="contradictory_destination_binding_events",
            error_message="Run destination binding events contain conflicting destinations.",
            event_ids=event_ids,
        )

    project, chat = bindings[0]
    return AtomicDestinationBindingResult(
        status=AtomicDestinationBindingStatus.IDEMPOTENT,
        run_id=run_id,
        project_title=project,
        chat_title=chat,
        event_ids=event_ids,
    )


def _reconstruct_execution_profile(
    run_id: str,
    rows: list[sqlite3.Row],
) -> AtomicExecutionProfileResult:
    event_ids = tuple(
        event_id
        for row in rows
        if isinstance(event_id := row["id"], int)
    )
    if not rows:
        return AtomicExecutionProfileResult(
            status=AtomicExecutionProfileStatus.MISSING,
            run_id=run_id,
        )

    profiles: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        profile = _execution_profile_from_metadata_json(row["metadata_json"])
        if profile is None:
            return AtomicExecutionProfileResult(
                status=AtomicExecutionProfileStatus.INVALID,
                run_id=run_id,
                reason_code="malformed_execution_profile_event",
                error_message="Run execution profile event metadata is malformed.",
                event_ids=event_ids,
            )
        profiles.append(profile)

    unique_profiles = frozenset(profiles)
    if len(unique_profiles) != 1:
        return AtomicExecutionProfileResult(
            status=AtomicExecutionProfileStatus.INVALID,
            run_id=run_id,
            reason_code="contradictory_execution_profile_events",
            error_message="Run execution profile events contain conflicting profiles.",
            event_ids=event_ids,
        )

    sandbox, model, reasoning_effort, approval_policy, profile_source = profiles[0]
    return AtomicExecutionProfileResult(
        status=AtomicExecutionProfileStatus.IDEMPOTENT,
        run_id=run_id,
        sandbox=sandbox,
        model=model,
        reasoning_effort=reasoning_effort,
        approval_policy=approval_policy,
        profile_source=profile_source,
        event_ids=event_ids,
    )


def _select_chatgpt_ui_lease_rows(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, run_id, event_type, metadata_json
        FROM events
        WHERE event_type IN (?, ?)
        ORDER BY id ASC
        """,
        CHATGPT_UI_LEASE_EVENT_TYPES,
    ).fetchall()


def _select_handoff_queue_state_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in CHATGPT_HANDOFF_QUEUE_STATE_EVENT_TYPES)
    return connection.execute(
        f"""
        SELECT id, run_id, event_type, metadata_json
        FROM events
        WHERE event_type IN ({placeholders})
        ORDER BY id ASC
        """,
        CHATGPT_HANDOFF_QUEUE_STATE_EVENT_TYPES,
    ).fetchall()


def _next_event_id(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?",
        ("events",),
    ).fetchone()
    if row is not None and isinstance(row["seq"], int):
        return int(row["seq"]) + 1
    row = connection.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM events").fetchone()
    max_id = int(row["max_id"]) if row is not None else 0
    return max_id + 1


def _reconstruct_handoff_queue_state(
    rows: list[sqlite3.Row],
) -> _HandoffQueueState:
    event_ids = tuple(
        event_id
        for row in rows
        if isinstance(event_id := row["id"], int)
    )
    entries: dict[int, _HandoffQueueEntry] = {}
    active_run_ids: set[str] = set()

    for row in rows:
        event_id = row["id"]
        event_type = row["event_type"]
        metadata = _handoff_queue_metadata(row)
        if metadata is None:
            return _invalid_handoff_queue_state(
                "malformed_chatgpt_handoff_queue_event",
                "ChatGPT handoff queue event metadata is malformed.",
                event_ids,
            )
        if metadata["run_id"] != row["run_id"]:
            return _invalid_handoff_queue_state(
                "chatgpt_handoff_queue_run_mismatch",
                "ChatGPT handoff queue event run_id does not match metadata.",
                event_ids,
            )
        queue_sequence = metadata["queue_sequence"]
        entry = entries.get(queue_sequence)

        if event_type == CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE:
            if queue_sequence != event_id:
                return _invalid_handoff_queue_state(
                    "chatgpt_handoff_queue_sequence_mismatch",
                    "ChatGPT handoff queue enqueue sequence does not match event id.",
                    event_ids,
                )
            if entry is not None:
                return _invalid_handoff_queue_state(
                    "duplicate_chatgpt_handoff_enqueue_sequence",
                    "ChatGPT handoff queue contains duplicate enqueue sequence.",
                    event_ids,
                )
            run_id = str(metadata["run_id"])
            if run_id in active_run_ids:
                return _invalid_handoff_queue_state(
                    "duplicate_active_chatgpt_handoff_for_run",
                    "Run has multiple active ChatGPT handoff queue entries.",
                    event_ids,
                )
            active_run_ids.add(run_id)
            entries[queue_sequence] = _HandoffQueueEntry(
                run_id=run_id,
                queue_entry_id=str(metadata["queue_entry_id"]),
                queue_sequence=queue_sequence,
                enqueue_source=str(metadata["enqueue_source"]),
                enqueue_event_id=event_id,
                status="pending",
                event_ids=(event_id,),
            )
            continue

        if entry is None:
            return _invalid_handoff_queue_state(
                "chatgpt_handoff_queue_event_without_enqueue",
                "ChatGPT handoff queue event has no matching enqueue event.",
                event_ids,
            )
        if metadata["queue_entry_id"] != entry.queue_entry_id:
            return _invalid_handoff_queue_state(
                "chatgpt_handoff_queue_entry_id_mismatch",
                "ChatGPT handoff queue event does not match its enqueue entry id.",
                event_ids,
            )

        if event_type == CHATGPT_HANDOFF_CLAIMED_EVENT_TYPE:
            claim_owner = metadata.get("claim_owner_identifier")
            claimed_at = metadata.get("claimed_at")
            if entry.status != "pending":
                return _invalid_handoff_queue_state(
                    "contradictory_chatgpt_handoff_claim_event",
                    "ChatGPT handoff queue claim event does not match a pending entry.",
                    event_ids,
                )
            if not isinstance(claim_owner, str) or claim_owner.strip() == "":
                return _invalid_handoff_queue_state(
                    "malformed_chatgpt_handoff_claim_event",
                    "ChatGPT handoff queue claim owner is malformed.",
                    event_ids,
                )
            if not isinstance(claimed_at, str) or claimed_at.strip() == "":
                return _invalid_handoff_queue_state(
                    "malformed_chatgpt_handoff_claim_event",
                    "ChatGPT handoff queue claimed_at is malformed.",
                    event_ids,
                )
            entries[queue_sequence] = _replace_handoff_entry(
                entry,
                status="claimed",
                claim_owner_identifier=claim_owner,
                claimed_at=claimed_at,
                event_ids=(*entry.event_ids, event_id),
            )
            continue

        if event_type in {CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE, CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE}:
            terminal_outcome = "completed" if event_type == CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE else "blocked"
            claim_owner = metadata.get("claim_owner_identifier")
            reason_code = metadata.get("reason_code")
            if entry.status != "claimed":
                return _invalid_handoff_queue_state(
                    "chatgpt_handoff_terminal_without_active_claim",
                    "ChatGPT handoff queue terminal event does not match an active claim.",
                    event_ids,
                )
            if claim_owner != entry.claim_owner_identifier:
                return _invalid_handoff_queue_state(
                    "chatgpt_handoff_terminal_owner_mismatch",
                    "ChatGPT handoff queue terminal event owner does not match claim owner.",
                    event_ids,
                )
            if metadata.get("terminal_outcome") != terminal_outcome:
                return _invalid_handoff_queue_state(
                    "chatgpt_handoff_terminal_outcome_mismatch",
                    "ChatGPT handoff queue terminal event outcome is contradictory.",
                    event_ids,
                )
            if not isinstance(reason_code, str) or reason_code.strip() == "":
                return _invalid_handoff_queue_state(
                    "malformed_chatgpt_handoff_terminal_event",
                    "ChatGPT handoff queue terminal reason_code is malformed.",
                    event_ids,
                )
            entries[queue_sequence] = _replace_handoff_entry(
                entry,
                status=terminal_outcome,
                terminal_outcome=terminal_outcome,
                terminal_reason_code=reason_code,
                event_ids=(*entry.event_ids, event_id),
            )
            active_run_ids.discard(entry.run_id)
            continue

        return _invalid_handoff_queue_state(
            "unknown_chatgpt_handoff_queue_event",
            "ChatGPT handoff queue history contains an unknown event type.",
            event_ids,
        )

    return _HandoffQueueState(
        status=AtomicChatGPTHandoffQueueStatus.MISSING if not entries else AtomicChatGPTHandoffQueueStatus.ENQUEUED,
        entries=tuple(sorted(entries.values(), key=lambda entry: entry.queue_sequence)),
        event_ids=event_ids,
    )


def _replace_handoff_entry(entry: _HandoffQueueEntry, **changes: object) -> _HandoffQueueEntry:
    values = {
        "run_id": entry.run_id,
        "queue_entry_id": entry.queue_entry_id,
        "queue_sequence": entry.queue_sequence,
        "enqueue_source": entry.enqueue_source,
        "enqueue_event_id": entry.enqueue_event_id,
        "status": entry.status,
        "claim_owner_identifier": entry.claim_owner_identifier,
        "claimed_at": entry.claimed_at,
        "terminal_outcome": entry.terminal_outcome,
        "terminal_reason_code": entry.terminal_reason_code,
        "event_ids": entry.event_ids,
    }
    values.update(changes)
    return _HandoffQueueEntry(**values)  # type: ignore[arg-type]


def _active_handoff_entry_for_run(
    state: _HandoffQueueState,
    run_id: str,
) -> _HandoffQueueEntry | None:
    for entry in state.entries:
        if entry.run_id == run_id and entry.status in {"pending", "claimed"}:
            return entry
    return None


def _oldest_active_handoff_entry(state: _HandoffQueueState) -> _HandoffQueueEntry | None:
    for entry in state.entries:
        if entry.status in {"pending", "claimed"}:
            return entry
    return None


def _find_handoff_entry(
    state: _HandoffQueueState,
    queue_sequence: int,
) -> _HandoffQueueEntry | None:
    for entry in state.entries:
        if entry.queue_sequence == queue_sequence:
            return entry
    return None


def _invalid_handoff_queue_state(
    reason_code: str,
    error_message: str,
    event_ids: tuple[int, ...],
) -> _HandoffQueueState:
    return _HandoffQueueState(
        status=AtomicChatGPTHandoffQueueStatus.INVALID,
        reason_code=reason_code,
        error_message=error_message,
        event_ids=event_ids,
    )


def _handoff_queue_result_from_state(
    state: _HandoffQueueState,
    *,
    run_id: str | None = None,
) -> AtomicChatGPTHandoffQueueResult:
    return AtomicChatGPTHandoffQueueResult(
        status=state.status,
        run_id=run_id,
        reason_code=state.reason_code,
        error_message=state.error_message,
        event_ids=state.event_ids,
    )


def _handoff_queue_metadata(row: sqlite3.Row) -> dict[str, object] | None:
    metadata = _metadata_dict_from_json(row["metadata_json"])
    if metadata is None:
        return None
    if metadata.get("schema_version") != CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION:
        return None
    run_id = metadata.get("run_id")
    queue_sequence = metadata.get("queue_sequence")
    queue_entry_id = metadata.get("queue_entry_id")
    if not isinstance(run_id, str) or run_id.strip() == "":
        return None
    if not isinstance(queue_sequence, int) or queue_sequence <= 0:
        return None
    if not isinstance(queue_entry_id, str) or queue_entry_id.strip() == "":
        return None
    event_type = row["event_type"]
    if event_type == CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE:
        enqueue_source = metadata.get("enqueue_source")
        if not isinstance(enqueue_source, str) or enqueue_source.strip() == "":
            return None
    return metadata


def _finish_chatgpt_handoff(
    queue_sequence: int,
    *,
    claim_owner_identifier: str,
    event_type: str,
    message: str,
    terminal_outcome: str,
    reason_code: str,
    lease_correlation: dict[str, object] | None,
) -> AtomicChatGPTHandoffQueueResult:
    connection: sqlite3.Connection | None = None
    transaction_started = False
    try:
        connection = _connect()
        _init_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True

        state = _reconstruct_handoff_queue_state(_select_handoff_queue_state_rows(connection))
        if state.status == AtomicChatGPTHandoffQueueStatus.INVALID:
            connection.rollback()
            transaction_started = False
            return _handoff_queue_result_from_state(state)

        entry = _find_handoff_entry(state, queue_sequence)
        if entry is None:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.MISSING,
                queue_sequence=queue_sequence,
                reason_code="chatgpt_handoff_queue_entry_missing",
                error_message="ChatGPT handoff queue entry was not found.",
                event_ids=state.event_ids,
            )
        if entry.status in {"completed", "blocked"}:
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.COMPLETED
                if entry.status == "completed"
                else AtomicChatGPTHandoffQueueStatus.BLOCKED,
                run_id=entry.run_id,
                queue_entry_id=entry.queue_entry_id,
                queue_sequence=entry.queue_sequence,
                enqueue_source=entry.enqueue_source,
                claim_owner_identifier=entry.claim_owner_identifier,
                terminal_outcome=entry.terminal_outcome,
                terminal_reason_code=entry.terminal_reason_code,
                event_written=False,
                event_ids=entry.event_ids,
            )
        if entry.status != "claimed":
            connection.rollback()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.NOT_CLAIMED,
                run_id=entry.run_id,
                queue_entry_id=entry.queue_entry_id,
                queue_sequence=entry.queue_sequence,
                enqueue_source=entry.enqueue_source,
                reason_code="chatgpt_handoff_queue_entry_not_claimed",
                error_message="ChatGPT handoff queue entry is not claimed.",
                event_ids=entry.event_ids,
            )
        if entry.claim_owner_identifier != claim_owner_identifier:
            metadata = {
                "schema_version": CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION,
                "run_id": entry.run_id,
                "queue_sequence": entry.queue_sequence,
                "queue_entry_id": entry.queue_entry_id,
                "claim_owner_identifier": claim_owner_identifier,
                "active_claim_owner_identifier": entry.claim_owner_identifier,
                "reason_code": "chatgpt_handoff_claim_owner_mismatch",
            }
            cursor = connection.execute(
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
                (
                    entry.run_id,
                    _utc_now(),
                    CHATGPT_HANDOFF_CLAIM_DENIED_EVENT_TYPE,
                    CHATGPT_HANDOFF_CLAIM_DENIED_MESSAGE,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            event_id = cursor.lastrowid
            connection.commit()
            transaction_started = False
            return AtomicChatGPTHandoffQueueResult(
                status=AtomicChatGPTHandoffQueueStatus.OWNER_MISMATCH,
                run_id=entry.run_id,
                queue_entry_id=entry.queue_entry_id,
                queue_sequence=entry.queue_sequence,
                enqueue_source=entry.enqueue_source,
                claim_owner_identifier=entry.claim_owner_identifier,
                claimed_at=entry.claimed_at,
                event_id=event_id if isinstance(event_id, int) else None,
                event_written=True,
                reason_code="chatgpt_handoff_claim_owner_mismatch",
                error_message="ChatGPT handoff queue claim owner does not match.",
                event_ids=entry.event_ids,
            )

        metadata = _compact_optional_metadata(
            {
                "schema_version": CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION,
                "run_id": entry.run_id,
                "queue_sequence": entry.queue_sequence,
                "queue_entry_id": entry.queue_entry_id,
                "claim_owner_identifier": claim_owner_identifier,
                "terminal_outcome": terminal_outcome,
                "reason_code": reason_code,
                "lease_correlation": _sanitize_lease_correlation(lease_correlation),
            }
        )
        cursor = connection.execute(
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
            (
                entry.run_id,
                _utc_now(),
                event_type,
                message,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        event_id = cursor.lastrowid
        connection.commit()
        transaction_started = False
        return AtomicChatGPTHandoffQueueResult(
            status=AtomicChatGPTHandoffQueueStatus.COMPLETED
            if terminal_outcome == "completed"
            else AtomicChatGPTHandoffQueueStatus.BLOCKED,
            run_id=entry.run_id,
            queue_entry_id=entry.queue_entry_id,
            queue_sequence=entry.queue_sequence,
            enqueue_source=entry.enqueue_source,
            claim_owner_identifier=claim_owner_identifier,
            claimed_at=entry.claimed_at,
            terminal_outcome=terminal_outcome,
            terminal_reason_code=reason_code,
            event_id=event_id if isinstance(event_id, int) else None,
            event_written=True,
            event_ids=entry.event_ids,
        )
    except sqlite3.Error as exc:
        if connection is not None and transaction_started:
            with suppress(sqlite3.Error):
                connection.rollback()
        return AtomicChatGPTHandoffQueueResult(
            status=AtomicChatGPTHandoffQueueStatus.OPERATIONAL_FAILURE,
            queue_sequence=queue_sequence,
            reason_code="chatgpt_handoff_terminal_transaction_failed",
            error_message=f"Failed to mark ChatGPT handoff terminal: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


def _sanitize_lease_correlation(
    lease_correlation: dict[str, object] | None,
) -> dict[str, object] | None:
    if lease_correlation is None:
        return None
    denied_keys = {"lease_token", "raw_lease_token", "token"}
    clean = {
        key: value
        for key, value in lease_correlation.items()
        if key not in denied_keys
        and isinstance(key, str)
        and isinstance(value, (str, int, bool))
    }
    return clean or None


def _reconstruct_chatgpt_ui_lease_state(
    rows: list[sqlite3.Row],
) -> _ChatGPTUILeaseState:
    event_ids = tuple(
        event_id
        for row in rows
        if isinstance(event_id := row["id"], int)
    )
    if not rows:
        return _ChatGPTUILeaseState(
            status=AtomicChatGPTUILeaseStatus.MISSING,
        )

    active: dict[str, object] | None = None
    active_event_id: int | None = None
    acquired_by_token: dict[str, dict[str, object]] = {}
    released_tokens: set[str] = set()
    for row in rows:
        event_id = row["id"] if isinstance(row["id"], int) else None
        event_type = row["event_type"]
        if event_type == CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE:
            acquire = _chatgpt_ui_lease_acquire_from_metadata_json(
                row["metadata_json"],
                row["run_id"],
            )
            if acquire is None:
                return _invalid_chatgpt_ui_lease_state(
                    "malformed_chatgpt_ui_lease_acquire_event",
                    "ChatGPT Desktop UI lease acquire event metadata is malformed.",
                    event_ids,
                )
            lease_token_sha256 = str(acquire["lease_token_sha256"])
            if lease_token_sha256 in acquired_by_token:
                return _invalid_chatgpt_ui_lease_state(
                    "duplicate_chatgpt_ui_lease_acquire_token",
                    "ChatGPT Desktop UI lease history contains a duplicate lease token.",
                    event_ids,
                )
            if active is not None:
                return _invalid_chatgpt_ui_lease_state(
                    "contradictory_active_chatgpt_ui_lease_events",
                    "ChatGPT Desktop UI lease history contains multiple active leases.",
                    event_ids,
                )
            acquired_by_token[lease_token_sha256] = acquire
            active = acquire
            active_event_id = event_id
            continue

        if event_type == CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE:
            release = _chatgpt_ui_lease_release_from_metadata_json(
                row["metadata_json"],
                row["run_id"],
            )
            if release is None:
                return _invalid_chatgpt_ui_lease_state(
                    "malformed_chatgpt_ui_lease_release_event",
                    "ChatGPT Desktop UI lease release event metadata is malformed.",
                    event_ids,
                )
            lease_token_sha256 = str(release["lease_token_sha256"])
            acquire = acquired_by_token.get(lease_token_sha256)
            if acquire is None:
                return _invalid_chatgpt_ui_lease_state(
                    "chatgpt_ui_lease_release_without_acquire",
                    "ChatGPT Desktop UI lease release event has no matching acquire event.",
                    event_ids,
                )
            if (
                release["owning_run_id"] != acquire["owning_run_id"]
                or release["owner_pid"] != acquire["owner_pid"]
                or release["acquired_at"] != acquire["acquired_at"]
            ):
                return _invalid_chatgpt_ui_lease_state(
                    "contradictory_chatgpt_ui_lease_release_event",
                    "ChatGPT Desktop UI lease release event does not match its acquire event.",
                    event_ids,
                )
            if active is not None and active["lease_token_sha256"] == lease_token_sha256:
                active = None
                active_event_id = None
            released_tokens.add(lease_token_sha256)
            continue

        return _invalid_chatgpt_ui_lease_state(
            "unknown_chatgpt_ui_lease_event",
            "ChatGPT Desktop UI lease history contains an unknown event type.",
            event_ids,
        )

    if active is None:
        return _ChatGPTUILeaseState(
            status=AtomicChatGPTUILeaseStatus.MISSING,
            event_ids=event_ids,
            released_tokens=frozenset(released_tokens),
        )

    return _ChatGPTUILeaseState(
        status=AtomicChatGPTUILeaseStatus.ACQUIRED,
        lease_token=str(active["lease_token_sha256"]),
        owner_pid=active["owner_pid"] if isinstance(active["owner_pid"], int) else None,
        owning_run_id=str(active["owning_run_id"]),
        acquired_at=str(active["acquired_at"]),
        active_event_id=active_event_id,
        event_ids=event_ids,
        released_tokens=frozenset(released_tokens),
    )


def _invalid_chatgpt_ui_lease_state(
    reason_code: str,
    error_message: str,
    event_ids: tuple[int, ...],
) -> _ChatGPTUILeaseState:
    return _ChatGPTUILeaseState(
        status=AtomicChatGPTUILeaseStatus.INVALID,
        reason_code=reason_code,
        error_message=error_message,
        event_ids=event_ids,
    )


def _lease_result_from_state(
    state: _ChatGPTUILeaseState,
    *,
    run_id: str | None = None,
) -> AtomicChatGPTUILeaseResult:
    return AtomicChatGPTUILeaseResult(
        status=state.status,
        run_id=run_id,
        lease_token=state.lease_token,
        owner_pid=state.owner_pid,
        owning_run_id=state.owning_run_id,
        acquired_at=state.acquired_at,
        released_at=state.released_at,
        active_event_id=state.active_event_id,
        reason_code=state.reason_code,
        error_message=state.error_message,
        event_ids=state.event_ids,
    )


def _chatgpt_ui_lease_acquire_from_metadata_json(
    metadata_json: object,
    event_run_id: object,
) -> dict[str, object] | None:
    metadata = _metadata_dict_from_json(metadata_json)
    if metadata is None:
        return None
    if metadata.get("schema_version") != CHATGPT_UI_LEASE_SCHEMA_VERSION:
        return None

    lease_token_sha256 = _lease_token_sha256_from_metadata(metadata)
    owner_pid = metadata.get("owner_pid")
    owning_run_id = metadata.get("owning_run_id")
    acquired_at = metadata.get("acquired_at")
    if not isinstance(lease_token_sha256, str):
        return None
    if not isinstance(owner_pid, int):
        return None
    if not isinstance(owning_run_id, str) or owning_run_id == "":
        return None
    if owning_run_id != event_run_id:
        return None
    if not isinstance(acquired_at, str) or acquired_at == "":
        return None
    return {
        "lease_token_sha256": lease_token_sha256,
        "owner_pid": owner_pid,
        "owning_run_id": owning_run_id,
        "acquired_at": acquired_at,
    }


def _chatgpt_ui_lease_release_from_metadata_json(
    metadata_json: object,
    event_run_id: object,
) -> dict[str, object] | None:
    metadata = _metadata_dict_from_json(metadata_json)
    if metadata is None:
        return None
    if metadata.get("schema_version") != CHATGPT_UI_LEASE_SCHEMA_VERSION:
        return None

    lease_token_sha256 = _lease_token_sha256_from_metadata(metadata)
    owner_pid = metadata.get("owner_pid")
    owning_run_id = metadata.get("owning_run_id")
    acquired_at = metadata.get("acquired_at")
    released_at = metadata.get("released_at")
    if not isinstance(lease_token_sha256, str):
        return None
    if not isinstance(owner_pid, int):
        return None
    if not isinstance(owning_run_id, str) or owning_run_id == "":
        return None
    if owning_run_id != event_run_id:
        return None
    if not isinstance(acquired_at, str) or acquired_at == "":
        return None
    if not isinstance(released_at, str) or released_at == "":
        return None
    return {
        "lease_token_sha256": lease_token_sha256,
        "owner_pid": owner_pid,
        "owning_run_id": owning_run_id,
        "acquired_at": acquired_at,
        "released_at": released_at,
    }


def _lease_token_sha256_from_metadata(metadata: dict) -> str | None:
    fingerprint = metadata.get("lease_token_sha256")
    if isinstance(fingerprint, str) and _valid_sha256(fingerprint):
        return fingerprint
    historical_raw = metadata.get("lease_token")
    if isinstance(historical_raw, str) and historical_raw:
        return chatgpt_ui_lease_token_fingerprint(historical_raw)
    return None


def _valid_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _normalize_codex_progress_event(
    run_id: str,
    codex_invocation_id: str,
    progress_event: dict[str, Any],
    *,
    sequence: int,
    created_at: str,
) -> dict[str, Any]:
    source = _bounded_progress_text(
        progress_event.get("source") or CODEX_PROGRESS_SOURCE_DEFAULT,
        CODEX_PROGRESS_TITLE_LIMIT,
    )
    kind = _bounded_progress_text(progress_event.get("kind"), 80) or "codex_json_event"
    if kind not in CODEX_PROGRESS_ALLOWED_KINDS:
        kind = "codex_json_event"
    status = _bounded_progress_text(progress_event.get("status"), 80) or "observed"
    title = _bounded_progress_text(progress_event.get("title"), CODEX_PROGRESS_TITLE_LIMIT)
    if title is None:
        title = kind.replace("_", " ").title()
    summary = _bounded_progress_text(progress_event.get("summary"), CODEX_PROGRESS_TEXT_LIMIT)
    metadata = progress_event.get("metadata")
    metadata = _sanitize_codex_progress_metadata(metadata if isinstance(metadata, dict) else {})
    return {
        "schema_version": CODEX_PROGRESS_SCHEMA_VERSION,
        "event_id": sequence,
        "run_id": str(run_id),
        "codex_invocation_id": _bounded_progress_text(codex_invocation_id, CODEX_PROGRESS_TITLE_LIMIT)
        or "unknown-invocation",
        "sequence": sequence,
        "created_at": created_at,
        "source": source,
        "kind": kind,
        "status": status,
        "title": title,
        "summary": summary,
        "metadata": metadata,
    }


def _codex_progress_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _metadata_dict_from_json(row["metadata_json"])
    sequence = int(row["id"])
    if metadata is None or metadata.get("schema_version") != CODEX_PROGRESS_SCHEMA_VERSION:
        return {
            "schema_version": CODEX_PROGRESS_SCHEMA_VERSION,
            "event_id": sequence,
            "run_id": str(row["run_id"]),
            "codex_invocation_id": "unknown-invocation",
            "sequence": sequence,
            "created_at": str(row["created_at"] or ""),
            "source": CODEX_PROGRESS_SOURCE_DEFAULT,
            "kind": "codex_json_event",
            "status": "malformed",
            "title": "Codex progress event",
            "summary": "Progress event metadata was malformed.",
            "metadata": {
                "stored_event_type": str(row["event_type"] or ""),
                "metadata_json_length": len(str(row["metadata_json"] or "")),
            },
        }
    event = _normalize_codex_progress_event(
        str(row["run_id"]),
        str(metadata.get("codex_invocation_id") or "unknown-invocation"),
        metadata,
        sequence=sequence,
        created_at=str(row["created_at"] or metadata.get("created_at") or ""),
    )
    return {
        **event,
        "event_id": sequence,
        "sequence": sequence,
    }


def _bounded_progress_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _sanitize_codex_progress_metadata(value: Any) -> dict[str, Any]:
    sanitized = _sanitize_codex_progress_value(value, depth=0)
    if not isinstance(sanitized, dict):
        return {}
    encoded = json.dumps(sanitized, sort_keys=True, default=str)
    if len(encoded) <= CODEX_PROGRESS_METADATA_JSON_LIMIT:
        return sanitized
    compact = {
        key: sanitized[key]
        for key in sanitized
        if key
        in {
            "event_type",
            "keys",
            "raw_event_length",
            "raw_event_sha256",
            "line_length",
            "line_sha256",
            "exit_code",
            "timed_out",
            "duration_ms",
            "command",
            "tool_name",
            "file_counts",
            "final_message_status",
            "final_message_length",
            "stdout_length",
            "stdout_sha256",
            "stderr_length",
            "stderr_sha256",
        }
    }
    compact["metadata_truncated"] = True
    compact["metadata_json_length"] = len(encoded)
    return compact


def _sanitize_codex_progress_value(value: Any, *, depth: int) -> Any:
    if depth > 4:
        return {"truncated": True, "reason": "max_depth"}
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)
            if key_text.lower() == "command" and not isinstance(item, dict):
                result[key_text] = _command_metadata_summary(item)
                continue
            if _codex_progress_key_is_sensitive(key_text):
                if isinstance(item, str):
                    result[f"{key_text}_length"] = len(item)
                    result[f"{key_text}_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
                continue
            result[key_text] = _sanitize_codex_progress_value(item, depth=depth + 1)
        if len(value) > 50:
            result["truncated_keys"] = len(value) - 50
        return result
    if isinstance(value, (list, tuple)):
        items = [_sanitize_codex_progress_value(item, depth=depth + 1) for item in value[:25]]
        if len(value) > 25:
            items.append({"truncated_items": len(value) - 25})
        return items
    if isinstance(value, str):
        if len(value) <= CODEX_PROGRESS_METADATA_TEXT_LIMIT:
            return value
        return {
            "preview": value[:CODEX_PROGRESS_METADATA_TEXT_LIMIT],
            "length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "truncated": True,
        }
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_progress_text(value, CODEX_PROGRESS_METADATA_TEXT_LIMIT)


def _codex_progress_key_is_sensitive(key: str) -> bool:
    key_text = key.lower()
    if key_text in {
        "stdout",
        "stderr",
        "output",
        "content",
        "delta",
        "text",
        "message",
        "prompt",
        "prompt_text",
        "response_text",
        "feedback_message",
        "reasoning",
        "analysis",
        "token",
        "lease_token",
        "raw_lease_token",
    }:
        return True
    return any(
        fragment in key_text
        for fragment in (
            "secret",
            "password",
            "api_key",
            "apikey",
            "authorization",
            "credential",
            "bearer",
        )
    )


def _command_metadata_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, (list, tuple)):
        argv0 = str(value[0]) if value else ""
        text = json.dumps([str(item) for item in value], separators=(",", ":"))
        return {
            "argv0": Path(argv0).name if argv0 else None,
            "argc": len(value),
            "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    text = str(value or "")
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return {
        "argv0": Path(first).name if first else None,
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _metadata_dict_from_json(metadata_json: object) -> dict | None:
    if not isinstance(metadata_json, str):
        return None
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata


def _compact_optional_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in metadata.items()
        if value is not None and value != ""
    }


def _destination_binding_from_metadata_json(
    metadata_json: object,
) -> tuple[str, str] | None:
    if not isinstance(metadata_json, str):
        return None
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("schema_version") != RUN_DESTINATION_BOUND_SCHEMA_VERSION:
        return None

    project_title = metadata.get("project_title")
    chat_title = metadata.get("chat_title")
    if not isinstance(project_title, str) or not isinstance(chat_title, str):
        return None

    project_title = project_title.strip()
    chat_title = chat_title.strip()
    if project_title == "" or chat_title == "":
        return None
    return project_title, chat_title


def _execution_profile_from_metadata_json(
    metadata_json: object,
) -> tuple[str, str, str, str, str] | None:
    if not isinstance(metadata_json, str):
        return None
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None

    expected_keys = {
        "schema_version",
        "sandbox",
        "model",
        "reasoning_effort",
        "approval_policy",
        "profile_source",
    }
    if set(metadata) != expected_keys:
        return None
    if metadata.get("schema_version") != RUN_EXECUTION_PROFILE_SCHEMA_VERSION:
        return None

    sandbox = metadata.get("sandbox")
    model = metadata.get("model")
    reasoning_effort = metadata.get("reasoning_effort")
    approval_policy = metadata.get("approval_policy")
    profile_source = metadata.get("profile_source")
    if not all(
        isinstance(value, str)
        for value in (
            sandbox,
            model,
            reasoning_effort,
            approval_policy,
            profile_source,
        )
    ):
        return None

    if sandbox not in ALLOWED_EXECUTION_PROFILE_SANDBOXES:
        return None
    if model not in ALLOWED_CODEX_MODEL_SELECTIONS:
        return None
    if reasoning_effort not in ALLOWED_REASONING_EFFORT_SELECTIONS:
        return None
    if approval_policy not in ALLOWED_APPROVAL_POLICY_SELECTIONS:
        return None
    if profile_source not in ALLOWED_EXECUTION_PROFILE_SOURCES:
        return None

    return sandbox, model, reasoning_effort, approval_policy, profile_source
