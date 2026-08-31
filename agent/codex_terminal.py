from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from agent.ledger import ALLOWED_CODEX_MODEL_SELECTIONS, CODEX_DEFAULT_SELECTION

ALLOWED_CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_JSON_PROGRESS_SOURCE = "codex_cli_jsonl"
CODEX_PROGRESS_TITLE_LIMIT = 240
CODEX_PROGRESS_SUMMARY_LIMIT = 1000
CODEX_PROGRESS_METADATA_TEXT_LIMIT = 500
_ACTIVE_CODEX_PROCESSES: dict[str, subprocess.Popen] = {}
_ACTIVE_CODEX_PROCESSES_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _safe_identifier(value: object, limit: int = 120) -> str | None:
    text = _bounded_text(value, limit)
    if text is None:
        return None
    return "".join(
        char if char.isalnum() or char in {"-", "_", ".", "/", ":"} else "_"
        for char in text
    )[:limit]


def _command_summary(value: object) -> dict[str, object]:
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
        argv0 = Path(parts[0]).name if parts else None
        text = json.dumps(parts, separators=(",", ":"))
        return {
            "argv0": argv0,
            "argc": len(parts),
            "length": len(text),
            "sha256": _sha256_text(text),
        }
    text = str(value or "")
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return {
        "argv0": Path(first).name if first else None,
        "length": len(text),
        "sha256": _sha256_text(text),
    }


def _safe_json_dumps(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), sort_keys=True)


def _json_event_type(event: dict[str, Any]) -> str:
    for key in ("type", "event_type", "event", "kind"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    item = event.get("item")
    if isinstance(item, dict):
        value = item.get("type") or item.get("kind")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _nested_dict_value(event: dict[str, Any], key: str) -> object:
    if key in event:
        return event.get(key)
    item = event.get("item")
    if isinstance(item, dict):
        return item.get(key)
    return None


def _json_event_status(event_type: str, event: dict[str, Any]) -> str:
    lowered = event_type.lower()
    if any(marker in lowered for marker in ("error", "failed", "failure")):
        return "failed"
    if "blocked" in lowered:
        return "blocked"
    if any(marker in lowered for marker in ("started", "begin", "running")):
        return "running"
    if any(marker in lowered for marker in ("finished", "completed", "end", "succeeded")):
        exit_code = _nested_dict_value(event, "exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return "failed"
        return "completed"
    return _bounded_text(event.get("status"), 80) or "observed"


def _safe_json_metadata(event: dict[str, Any], raw_text: str) -> dict[str, object]:
    return {
        "event_type": _json_event_type(event),
        "keys": sorted(str(key) for key in event.keys())[:50],
        "raw_event_length": len(raw_text),
        "raw_event_sha256": _sha256_text(raw_text),
        "value_summary": _json_value_summary(event),
    }


def _json_value_summary(event: dict[str, Any]) -> dict[str, object]:
    summary: dict[str, object] = {}
    event_type = _json_event_type(event)
    summary["event_type"] = event_type
    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str):
            codex_session_id = _safe_identifier(thread_id, 120)
            if codex_session_id:
                summary["codex_session_id"] = codex_session_id
    for key in ("id", "status", "exit_code", "duration_ms", "duration_seconds"):
        value = _nested_dict_value(event, key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                summary[key] = _bounded_text(value, CODEX_PROGRESS_METADATA_TEXT_LIMIT)
    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type") or item.get("kind")
        if isinstance(item_type, str):
            summary["item_type"] = _bounded_text(item_type, 120)
    tool_name = _nested_dict_value(event, "tool_name") or _nested_dict_value(event, "tool")
    if isinstance(tool_name, str):
        summary["tool_name"] = _safe_identifier(tool_name)
    command = _nested_dict_value(event, "command") or _nested_dict_value(event, "cmd")
    if command is not None:
        summary["command"] = _command_summary(command)
    files = _file_change_summary(event)
    if files:
        summary["file_changes"] = files
    error = _nested_dict_value(event, "error") or _nested_dict_value(event, "error_message")
    if error is not None:
        summary["error"] = _bounded_text(error, CODEX_PROGRESS_METADATA_TEXT_LIMIT)
    return summary


def _file_change_summary(event: dict[str, Any]) -> dict[str, object]:
    candidates = (
        _nested_dict_value(event, "files"),
        _nested_dict_value(event, "paths"),
        _nested_dict_value(event, "changed_files"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            paths = [
                _bounded_text(item, 240)
                for item in candidate[:20]
                if _bounded_text(item, 240) is not None
            ]
            return {
                "count": len(candidate),
                "paths": paths,
                "truncated": len(candidate) > len(paths),
            }
    counts = _nested_dict_value(event, "counts")
    if isinstance(counts, dict):
        return {
            str(key): value
            for key, value in counts.items()
            if isinstance(value, (int, float, bool, str))
        }
    return {}


def _assistant_commentary_text(event: dict[str, Any]) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type") or item.get("kind")
    if not isinstance(item_type, str) or item_type.strip().lower() != "agent_message":
        return None
    return _bounded_text(item.get("text"), CODEX_PROGRESS_SUMMARY_LIMIT)


def _progress_event(
    *,
    kind: str,
    status: str,
    title: str,
    summary: str | None = None,
    metadata: dict[str, object] | None = None,
    source: str = CODEX_JSON_PROGRESS_SOURCE,
) -> dict[str, object]:
    return {
        "source": source,
        "kind": kind,
        "status": status,
        "title": _bounded_text(title, CODEX_PROGRESS_TITLE_LIMIT) or kind,
        "summary": _bounded_text(summary, CODEX_PROGRESS_SUMMARY_LIMIT),
        "metadata": metadata or {},
    }


def normalize_codex_jsonl_event(line: str) -> dict[str, object]:
    stripped = line.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        return _progress_event(
            kind="codex_json_event",
            status="malformed",
            title="Malformed Codex JSONL event",
            summary="A Codex JSONL line could not be parsed and was skipped.",
            metadata={
                "line_length": len(line),
                "line_sha256": _sha256_text(line),
                "error_type": type(error).__name__,
                "error_message": _bounded_text(str(error), CODEX_PROGRESS_METADATA_TEXT_LIMIT),
            },
        )

    raw_text = _safe_json_dumps(parsed)
    if not isinstance(parsed, dict):
        return _progress_event(
            kind="codex_json_event",
            status="observed",
            title="Codex JSON event",
            summary="Codex emitted a non-object JSON event.",
            metadata={
                "json_type": type(parsed).__name__,
                "raw_event_length": len(raw_text),
                "raw_event_sha256": _sha256_text(raw_text),
            },
        )

    event_type = _json_event_type(parsed)
    lowered = event_type.lower()
    status = _json_event_status(event_type, parsed)
    metadata = _safe_json_metadata(parsed, raw_text)
    command = _nested_dict_value(parsed, "command") or _nested_dict_value(parsed, "cmd")
    tool_name = _nested_dict_value(parsed, "tool_name") or _nested_dict_value(parsed, "tool")
    exit_code = _nested_dict_value(parsed, "exit_code")
    commentary = _assistant_commentary_text(parsed)

    if commentary is not None:
        return _progress_event(
            kind="assistant_commentary",
            status=status,
            title="Codex update",
            summary=commentary,
            metadata=metadata,
        )

    if "blocked" in lowered:
        return _progress_event(
            kind="blocked",
            status="blocked",
            title="Codex blocked",
            summary=f"Codex emitted {event_type}.",
            metadata=metadata,
        )
    if any(marker in lowered for marker in ("error", "failed", "failure")):
        return _progress_event(
            kind="error",
            status="failed",
            title="Codex error",
            summary=f"Codex emitted {event_type}.",
            metadata=metadata,
        )
    if command is not None or "command" in lowered or "exec" in lowered:
        command_title = _command_summary(command).get("argv0") if command is not None else None
        title_suffix = f": {command_title}" if command_title else ""
        if any(marker in lowered for marker in ("started", "begin", "running")):
            return _progress_event(
                kind="command_started",
                status="running",
                title=f"Command started{title_suffix}",
                summary=f"Codex emitted {event_type}.",
                metadata=metadata,
            )
        if any(marker in lowered for marker in ("finished", "completed", "end")) or exit_code is not None:
            return _progress_event(
                kind="command_finished",
                status=status,
                title=f"Command finished{title_suffix}",
                summary=f"Codex emitted {event_type}.",
                metadata=metadata,
            )
    if tool_name is not None or "tool" in lowered:
        return _progress_event(
            kind="tool_event",
            status=status,
            title="Tool event",
            summary=f"Codex emitted {event_type}.",
            metadata=metadata,
        )
    if ("file" in lowered and "change" in lowered) or "patch" in lowered:
        return _progress_event(
            kind="file_change_summary",
            status=status,
            title="File change summary",
            summary=f"Codex emitted {event_type}.",
            metadata=metadata,
        )

    return _progress_event(
        kind="codex_json_event",
        status=status,
        title="Codex JSON event",
        summary=f"Codex emitted {event_type}.",
        metadata=metadata,
    )


def _emit_progress(
    progress_callback: Callable[[dict[str, object]], None] | None,
    event: dict[str, object],
) -> None:
    if progress_callback is None:
        return
    with suppress(Exception):
        progress_callback(event)


def _safe_run_id(value: str | None) -> str:
    text = str(value or "unscoped-run").strip() or "unscoped-run"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)
    return safe[:80] or "unscoped-run"


def _default_final_message_path(run_id: str | None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"{timestamp}-{uuid.uuid4().hex}.md"
    return (
        Path(tempfile.gettempdir())
        / "agent-gpt-codex-loop"
        / "codex-final-messages"
        / _safe_run_id(run_id)
        / filename
    )


def _final_message_artifact_result(path: Path) -> dict:
    base = {
        "final_message_path": str(path),
        "final_message": "",
        "final_message_length": 0,
        "final_message_status": "missing",
        "final_message_error": None,
    }
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            **base,
            "final_message_error": "Codex final-message artifact was not written.",
        }
    except UnicodeDecodeError as error:
        return {
            **base,
            "final_message_status": "invalid_utf8",
            "final_message_error": f"Codex final-message artifact is not valid UTF-8: {error}",
        }
    except OSError as error:
        return {
            **base,
            "final_message_status": "unreadable",
            "final_message_error": f"Unable to read Codex final-message artifact: {error}",
        }

    clean_text = text.strip()
    if not clean_text:
        return {
            **base,
            "final_message_status": "empty",
            "final_message_error": "Codex final-message artifact was empty.",
        }

    return {
        **base,
        "final_message": clean_text,
        "final_message_length": len(clean_text),
        "final_message_status": "valid",
    }


def run_command(
    command: list[str],
    cwd: str | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    started_at = _utc_now()

    try:
        run_kwargs = {
            "cwd": cwd,
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if timeout_seconds is not None:
            run_kwargs["timeout"] = timeout_seconds
        completed = subprocess.run(
            command,
            **run_kwargs,
        )
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }
    except subprocess.TimeoutExpired as error:
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": None,
            "stdout": _decode_output(error.stdout),
            "stderr": _decode_output(error.stderr),
            "timed_out": True,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }
    except FileNotFoundError as error:
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": 127,
            "stdout": "",
            "stderr": f"{error}\n",
            "timed_out": False,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }


def run_json_streaming_command(
    command: list[str],
    cwd: str | None = None,
    *,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    process_metadata: dict[str, object] | None = None,
) -> dict:
    started_at = _utc_now()
    stdout_parts: list[str] = []
    metadata = process_metadata or {}
    process_key = str(metadata.get("run_id") or "").strip() or None
    process: subprocess.Popen | None = None

    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
            )
            if process_key is not None:
                with _ACTIVE_CODEX_PROCESSES_LOCK:
                    _ACTIVE_CODEX_PROCESSES[process_key] = process
            _emit_progress(
                progress_callback,
                _progress_event(
                    kind="process_started",
                    status="running",
                    title="Codex process started",
                    summary="Codex exec started with JSONL progress enabled.",
                    metadata={
                        **metadata,
                        "pid": getattr(process, "pid", None),
                        "command": _command_summary(command[:-1]),
                        "json_streaming": True,
                    },
                ),
            )
            stdout_stream = process.stdout
            if stdout_stream is not None:
                for raw_line in stdout_stream:
                    line = _decode_output(raw_line)
                    stdout_parts.append(line)
                    if line.strip():
                        _emit_progress(
                            progress_callback,
                            normalize_codex_jsonl_event(line),
                        )
            exit_code = process.wait()
            stderr_file.seek(0)
            stderr_text = stderr_file.read()
        stdout_text = "".join(stdout_parts)
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "timed_out": False,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }
    except FileNotFoundError as error:
        _emit_progress(
            progress_callback,
            _progress_event(
                kind="error",
                status="failed",
                title="Codex process failed to start",
                summary="Codex CLI executable could not be started.",
                metadata={
                    **metadata,
                    "error_type": type(error).__name__,
                    "error_message": _bounded_text(str(error), CODEX_PROGRESS_METADATA_TEXT_LIMIT),
                },
            ),
        )
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": 127,
            "stdout": "".join(stdout_parts),
            "stderr": f"{error}\n",
            "timed_out": False,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }
    finally:
        if process_key is not None and process is not None:
            with _ACTIVE_CODEX_PROCESSES_LOCK:
                if _ACTIVE_CODEX_PROCESSES.get(process_key) is process:
                    _ACTIVE_CODEX_PROCESSES.pop(process_key, None)


def terminate_codex_run(run_id: str, *, timeout_seconds: float = 2.0) -> dict[str, object]:
    with _ACTIVE_CODEX_PROCESSES_LOCK:
        process = _ACTIVE_CODEX_PROCESSES.get(run_id)
    if process is None or process.poll() is not None:
        return {
            "terminated": False,
            "reason_code": "codex_process_not_running",
        }
    try:
        process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout_seconds)
        return {
            "terminated": True,
            "reason_code": "codex_process_terminated",
            "exit_code": process.returncode,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "terminated": False,
            "reason_code": "codex_process_termination_failed",
            "error_message": str(exc),
        }


def run_codex_exec(
    prompt: str,
    repo_path: str | Path | None = None,
    timeout_seconds: float | None = None,
    cwd: str | Path | None = None,
    sandbox: str = "read-only",
    model: str | None = None,
    run_id: str | None = None,
    final_message_path: str | Path | None = None,
    json_stream: bool = False,
    codex_invocation_id: str | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict:
    codex_path = shutil.which("codex")
    if repo_path is None:
        repo_path = cwd if cwd is not None else Path.cwd()

    resolved_repo_path = Path(repo_path).expanduser().resolve(strict=False)
    resolved_repo_path_text = str(resolved_repo_path)
    resolved_final_message_path = (
        Path(final_message_path).expanduser().resolve(strict=False)
        if final_message_path is not None
        else _default_final_message_path(run_id)
    )
    model_text = CODEX_DEFAULT_SELECTION if model is None else str(model).strip()
    command = [
        "codex",
        "exec",
    ]
    if json_stream:
        command.append("--json")
    command.extend(["-C", resolved_repo_path_text])
    if sandbox == "danger-full-access":
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(["-s", sandbox])
    if model_text != CODEX_DEFAULT_SELECTION:
        command.extend(["-m", model_text])
    command.extend(
        [
            "--output-last-message",
            str(resolved_final_message_path),
            prompt,
        ]
    )

    validation_error = None
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        validation_error = (
            "Invalid Codex sandbox. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_SANDBOXES)}."
        )
    elif model_text not in ALLOWED_CODEX_MODEL_SELECTIONS:
        validation_error = (
            "Invalid Codex model. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_MODEL_SELECTIONS)}."
        )
    elif not resolved_repo_path.exists():
        validation_error = f"Repo path does not exist: {resolved_repo_path_text}"
    elif not resolved_repo_path.is_dir():
        validation_error = f"Repo path is not a directory: {resolved_repo_path_text}"

    if validation_error is not None:
        now = _utc_now()
        _emit_progress(
            progress_callback,
            _progress_event(
                kind="error",
                status="failed",
                title="Codex validation failed",
                summary=validation_error,
                metadata={
                    "repo_path": resolved_repo_path_text,
                    "sandbox": sandbox,
                    "model": model_text,
                    "codex_invocation_id": codex_invocation_id,
                    "validation_error": validation_error,
                },
            ),
        )
        return {
            "mode": "exec",
            "found": codex_path is not None,
            "codex_path": codex_path,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "sandbox": sandbox,
            "command": command,
            "exit_code": 2,
            "stdout": "",
            "stderr": f"{validation_error}\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": validation_error,
            "final_message_path": str(resolved_final_message_path),
            "final_message": "",
            "final_message_length": 0,
            "final_message_status": "not_requested",
            "final_message_error": validation_error,
        }

    if codex_path is None:
        now = _utc_now()
        _emit_progress(
            progress_callback,
            _progress_event(
                kind="error",
                status="failed",
                title="Codex CLI not found",
                summary="Codex CLI was not found on PATH.",
                metadata={
                    "repo_path": resolved_repo_path_text,
                    "sandbox": sandbox,
                    "model": model_text,
                    "codex_invocation_id": codex_invocation_id,
                },
            ),
        )
        return {
            "mode": "exec",
            "found": False,
            "codex_path": None,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "sandbox": sandbox,
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": "Codex CLI not found on PATH.\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": None,
            "final_message_path": str(resolved_final_message_path),
            "final_message": "",
            "final_message_length": 0,
            "final_message_status": "not_requested",
            "final_message_error": "Codex CLI not found on PATH.",
        }

    try:
        resolved_final_message_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        now = _utc_now()
        _emit_progress(
            progress_callback,
            _progress_event(
                kind="error",
                status="failed",
                title="Final-message path failed",
                summary="Unable to prepare Codex final-message artifact path.",
                metadata={
                    "repo_path": resolved_repo_path_text,
                    "sandbox": sandbox,
                    "model": model_text,
                    "codex_invocation_id": codex_invocation_id,
                    "error_type": type(error).__name__,
                    "error_message": _bounded_text(str(error), CODEX_PROGRESS_METADATA_TEXT_LIMIT),
                },
            ),
        )
        return {
            "mode": "exec",
            "found": True,
            "codex_path": codex_path,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "sandbox": sandbox,
            "command": command,
            "exit_code": 2,
            "stdout": "",
            "stderr": f"Unable to prepare Codex final-message artifact path: {error}\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": None,
            "final_message_path": str(resolved_final_message_path),
            "final_message": "",
            "final_message_length": 0,
            "final_message_status": "path_prepare_failed",
            "final_message_error": str(error),
        }

    process_metadata = {
        "run_id": run_id,
        "repo_path": resolved_repo_path_text,
        "sandbox": sandbox,
        "model": model_text,
        "codex_invocation_id": codex_invocation_id,
        "final_message_path": str(resolved_final_message_path),
    }
    if json_stream:
        result = run_json_streaming_command(
            command,
            cwd=resolved_repo_path_text,
            progress_callback=progress_callback,
            process_metadata=process_metadata,
        )
    else:
        result = run_command(command, cwd=resolved_repo_path_text, timeout_seconds=None)
    final_message = _final_message_artifact_result(resolved_final_message_path)
    final_status = str(final_message.get("final_message_status") or "")
    final_length = int(final_message.get("final_message_length") or 0)
    _emit_progress(
        progress_callback,
        _progress_event(
            kind="final_message_available",
            status="available" if final_status == "valid" else final_status or "unavailable",
            title="Final message available" if final_status == "valid" else "Final message unavailable",
            summary="Codex final assistant message was read from the final-message artifact.",
            metadata={
                **process_metadata,
                "final_message_status": final_status,
                "final_message_length": final_length,
                "final_message_error": final_message.get("final_message_error"),
            },
        ),
    )
    stdout_text = str(result.get("stdout") or "")
    stderr_text = str(result.get("stderr") or "")
    _emit_progress(
        progress_callback,
        _progress_event(
            kind="process_exited",
            status="completed" if result.get("exit_code") == 0 else "failed",
            title="Codex process exited",
            summary="Codex exec process exited.",
            metadata={
                **process_metadata,
                "exit_code": result.get("exit_code"),
                "timed_out": bool(result.get("timed_out")),
                "stdout_length": len(stdout_text),
                "stdout_sha256": _sha256_text(stdout_text),
                "stderr_length": len(stderr_text),
                "stderr_sha256": _sha256_text(stderr_text),
            },
        ),
    )
    return {
        "mode": "exec",
        "found": True,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": resolved_repo_path_text,
        "sandbox": sandbox,
        "codex_invocation_id": codex_invocation_id,
        "json_stream": bool(json_stream),
        "validation_error": None,
        **result,
        **final_message,
    }


def check_codex_environment(timeout_seconds: float | None = None) -> dict:
    del timeout_seconds
    codex_path = shutil.which("codex")
    result = {
        "codex_path": codex_path,
        "found": codex_path is not None,
        "help": None,
        "doctor": None,
        "timeout_seconds": None,
    }

    if codex_path is None:
        return result

    result["help"] = run_command(
        [codex_path, "--help"],
        timeout_seconds=None,
    )
    result["doctor"] = run_command(
        [codex_path, "doctor"],
        timeout_seconds=None,
    )
    return result
