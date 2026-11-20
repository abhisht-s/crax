"""Reading Claude Code's ``--output-format stream-json`` output.

Step 1 of the Claude Code coding-agent plan. Pure functions with no CRAX
imports, because the durable invocation wrapper imports this module in its own
process.

Claude Code prints one JSON object per line. The ones CRAX relies on:

- ``{"type": "system", "subtype": "init", "session_id": ...}``: first line;
  carries the session id.
- ``{"type": "assistant", "message": {"content": [...]}}``: text, tool_use or
  thinking blocks.
- ``{"type": "user", "message": {"content": [{"type": "tool_result", ...}]}}``.
- ``{"type": "result", "subtype": "success" | "error_max_turns" |
  "error_during_execution", "is_error": bool, "result": str, "session_id": ...}``:
  the last line; ``result`` is the final answer.

Codex writes its final answer to a file (``--output-last-message``). Claude Code
has no such flag, so the wrapper copies the ``result`` text into the same
``final_message.md`` artifact (:func:`write_final_message_from_stream`). Only a
successful result is written; an error result never becomes a "final answer".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

FINAL_MESSAGE_SOURCE_CLAUDE_STREAM = "claude_stream_json_result"

RESULT_EVENT_TYPE = "result"
RESULT_SUBTYPE_SUCCESS = "success"

EXTRACTION_WRITTEN = "written"
EXTRACTION_NO_RESULT_EVENT = "no_result_event"
EXTRACTION_RESULT_NOT_SUCCESS = "result_not_success"
EXTRACTION_EMPTY_RESULT = "empty_result"
EXTRACTION_READ_FAILED = "read_failed"
EXTRACTION_WRITE_FAILED = "write_failed"


def parse_stream_line(line: str) -> dict[str, Any] | None:
    """One stream line as a dict, or None if it is blank, malformed or not an object."""

    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def iter_stream_events(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        event = parse_stream_line(line)
        if event is not None:
            yield event


def last_result_event(lines: Iterable[str]) -> dict[str, Any] | None:
    """The last ``type == "result"`` event, or None."""

    found = None
    for event in iter_stream_events(lines):
        if event.get("type") == RESULT_EVENT_TYPE:
            found = event
    return found


def first_session_id(lines: Iterable[str]) -> str | None:
    """The session id from the first event that carries one (init comes first)."""

    for event in iter_stream_events(lines):
        value = event.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def result_is_success(event: dict[str, Any] | None) -> bool:
    return (
        isinstance(event, dict)
        and event.get("type") == RESULT_EVENT_TYPE
        and event.get("subtype") == RESULT_SUBTYPE_SUCCESS
        and event.get("is_error") is not True
    )


def final_message_from_result(event: dict[str, Any] | None) -> str | None:
    """The final answer text of a successful result event, else None."""

    if not result_is_success(event):
        return None
    text = event.get("result")  # type: ignore[union-attr]
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def write_final_message_from_stream(stdout_path: Path, final_message_path: Path) -> str:
    """Copy the final answer from the stream log to ``final_message_path``.

    Returns an ``EXTRACTION_*`` status. Never raises. When nothing is written,
    the final-message reader later reports the artifact as missing, which the
    provider treats as a failed run.
    """

    try:
        with Path(stdout_path).open(encoding="utf-8", errors="replace") as stream:
            event = last_result_event(stream)
    except OSError:
        return EXTRACTION_READ_FAILED
    if event is None:
        return EXTRACTION_NO_RESULT_EVENT
    if not result_is_success(event):
        return EXTRACTION_RESULT_NOT_SUCCESS
    text = final_message_from_result(event)
    if text is None:
        return EXTRACTION_EMPTY_RESULT
    try:
        _write_text_atomic(Path(final_message_path), text if text.endswith("\n") else f"{text}\n")
    except OSError:
        return EXTRACTION_WRITE_FAILED
    return EXTRACTION_WRITTEN
