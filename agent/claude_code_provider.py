"""Claude Code as a CRAX coding agent (headless ``claude -p``).

Step 1 of the Claude Code coding-agent plan. Implements the
:class:`agent.coding_agent_provider.CodingAgentProvider` contract on top of the
same durable transport Codex uses: the per-invocation wrapper, artifact
directory, process-group cancel and crash recovery in ``agent.codex_invocation``
and ``agent.codex_terminal``. Persisted ``codex_*`` event types, metadata keys
and artifact paths are kept; ``coding_agent: "claude_code"`` tells them apart.

``agent.coding_agents.resolve_provider`` returns this provider for
``claude_code`` (Step 2). Runs cannot pick it from the dashboard or CLI yet
(Step 3).

How Codex maps onto Claude Code:

- ``codex exec --json -C <repo>``: ``claude -p --output-format stream-json
  --verbose``, run with the repo as working directory.
- ``--output-last-message <file>``: none. The wrapper copies the final
  ``result`` event's text into the same ``final_message.md``
  (``agent.claude_code_stream``).
- session id parsed from ``thread.started``: CRAX picks it up front with
  ``--session-id <uuid>``; continuation is ``--resume <uuid>`` (same id).
- sandbox levels: see :data:`PERMISSION_PROFILES`. ``danger-full-access`` (the
  autonomous level) is Claude Code's auto mode, not
  ``--dangerously-skip-permissions``. The levels are intended to match
  Codex's levels; they have only been checked against a fake CLI, not live.

Every run also passes ``--permission-prompts none`` (anything that would ask
is refused instead of hanging), ``--setting-sources project,local`` (the
user's personal settings, hooks and plugins do not load) and
``--strict-mcp-config`` (no MCP servers).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from agent import codex_terminal
from agent.claude_code_stream import (
    FINAL_MESSAGE_SOURCE_CLAUDE_STREAM,
    RESULT_EVENT_TYPE,
    first_session_id,
    last_result_event,
    result_is_success,
)
from agent.codex_invocation import artifact_paths_for, event_metadata, read_json_file
from agent.coding_agent_provider import CodingAgentRequest, ExecutionOutcome, NativeSessionIdentity
from agent.claude_code_models import CLAUDE_EFFORT_LEVELS, claude_model_supports_effort
from agent.ledger import ALLOWED_CLAUDE_CODE_MODEL_SELECTIONS, CODEX_DEFAULT_SELECTION

CLAUDE_CODE_IDENTITY = "claude_code"
CLAUDE_EXECUTABLE = "claude"
CLAUDE_AGENT_LABEL = "Claude Code"
CLAUDE_PROGRESS_SOURCE = "claude_code_stream_json"

# From the ledger, which validates run profiles: CODEX_DEFAULT_SELECTION means
# "no --model flag" (Claude Code's own default); the rest are CLI model aliases.
ALLOWED_CLAUDE_MODEL_SELECTIONS = ALLOWED_CLAUDE_CODE_MODEL_SELECTIONS

READ_ONLY_ALLOWED_TOOLS = ("Read", "Grep", "Glob")
READ_ONLY_DENIED_TOOLS = ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch")
FILE_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")

# sandbox -> (permission flags, extra settings). Intended equivalents of Codex's
# sandbox levels; not yet verified against the live CLI.
PERMISSION_PROFILES: dict[str, tuple[tuple[str, ...], dict[str, Any] | None]] = {
    # Only read/search tools; everything else is refused without asking.
    "read-only": (
        ("--permission-mode", "dontAsk"),
        {"permissions": {"allow": list(READ_ONLY_ALLOWED_TOOLS), "deny": list(READ_ONLY_DENIED_TOOLS)}},
    ),
    # File edits allowed; shell commands run inside Claude Code's OS sandbox,
    # which limits writes to the working directory, with no unsandboxed escape.
    "workspace-write": (
        ("--permission-mode", "acceptEdits"),
        {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False}},
    ),
    # The autonomous level (Codex: Full Access). Claude Code runs in auto mode:
    # its safety classifier approves or blocks each action instead of asking;
    # with --permission-prompts none anything it would ask about is refused.
    # No OS sandbox, like Codex Full Access. Auto-mode rules are Claude Code's
    # defaults: --setting-sources skips the user's own autoMode settings.
    "danger-full-access": (("--permission-mode", "auto"), None),
}
ALLOWED_CLAUDE_SANDBOXES = tuple(PERMISSION_PROFILES)

COMMON_FLAGS = (
    "-p",
    "--output-format", "stream-json",
    "--verbose",
    "--permission-prompts", "none",
    "--setting-sources", "project,local",
    "--strict-mcp-config",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _is_default_model(model: str | None) -> bool:
    return model is None or model == CODEX_DEFAULT_SELECTION


# --- command --------------------------------------------------------------------------


_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,99}$")


def claude_settings_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base).expanduser() / "settings.json"


def user_claude_defaults(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The user's own default ``model`` and ``effortLevel`` from their Claude settings.

    Runs pass ``--setting-sources project,local``, which skips the user's
    settings file (hooks, plugins, permissions). That also dropped the model
    and effort the user chose, so a "default" run silently used Claude Code's
    built-in default model (found live 2026-11-20: Fable 5.2 instead of the
    user's ``opus``). Only these two values are read back and passed as flags.
    Anything missing, unreadable or malformed is ignored.
    """

    try:
        data = json.loads(claude_settings_path(environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, str] = {}
    model = data.get("model")
    if isinstance(model, str) and _SAFE_MODEL.match(model.strip()):
        found["model"] = model.strip()
    effort = data.get("effortLevel")
    if isinstance(effort, str) and effort.strip() in CLAUDE_EFFORT_LEVELS:
        found["effort"] = effort.strip()
    return found


def build_claude_command(
    prompt: str,
    *,
    sandbox: str,
    model: str | None = None,
    effort: str | None = None,
    session_id: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """The ``claude`` argv. The prompt is last, after ``--``, so it is never read as a flag."""

    if (session_id is None) == (resume_session_id is None):
        raise ValueError("Exactly one of session_id and resume_session_id is required.")
    permission_flags, settings = PERMISSION_PROFILES[sandbox]
    command = [CLAUDE_EXECUTABLE, *COMMON_FLAGS, *permission_flags]
    if settings is not None:
        command.extend(["--settings", json.dumps(settings, sort_keys=True, separators=(",", ":"))])
    if not _is_default_model(model):
        command.extend(["--model", str(model)])
    if effort is not None:
        if effort not in CLAUDE_EFFORT_LEVELS:
            raise ValueError(f"Invalid Claude Code effort {effort!r}.")
        command.extend(["--effort", effort])
    if resume_session_id is not None:
        command.extend(["--resume", resume_session_id])
    else:
        command.extend(["--session-id", str(session_id)])
    command.extend(["--", prompt])
    return command


def validate_claude_request(
    *,
    repo_path: Path,
    sandbox: str,
    model: str | None,
    session_id: str | None,
    resume_session_id: str | None,
) -> str | None:
    if sandbox not in ALLOWED_CLAUDE_SANDBOXES:
        return f"Invalid Claude Code sandbox. Allowed values: {', '.join(ALLOWED_CLAUDE_SANDBOXES)}."
    if model is not None and model not in ALLOWED_CLAUDE_MODEL_SELECTIONS:
        return f"Invalid Claude Code model. Allowed values: {', '.join(ALLOWED_CLAUDE_MODEL_SELECTIONS)}."
    if resume_session_id is not None and _uuid_text(resume_session_id) != resume_session_id:
        return "Invalid Claude Code session ID."
    if session_id is not None and _uuid_text(session_id) != session_id:
        return "Invalid Claude Code session ID."
    if not repo_path.exists():
        return f"Repo path does not exist: {repo_path}"
    if not repo_path.is_dir():
        return f"Repo path is not a directory: {repo_path}"
    return None


# --- progress -------------------------------------------------------------------------


def _bounded(value: object, limit: int) -> str | None:
    return codex_terminal._bounded_text(value, limit)


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _stream_event_type(event: dict[str, Any]) -> str:
    kind = str(event.get("type") or "unknown")
    if kind == "system":
        return f"system.{event.get('subtype') or 'unknown'}"
    if kind == RESULT_EVENT_TYPE:
        return f"result.{event.get('subtype') or 'unknown'}"
    if kind in ("assistant", "user"):
        types = [str(block.get("type") or "unknown") for block in _content_blocks(event)]
        return f"{kind}.{types[0] if types else 'empty'}"
    return kind


def _value_summary(event: dict[str, Any], event_type: str) -> dict[str, object]:
    summary: dict[str, object] = {"event_type": event_type}
    session_id = _uuid_text(event.get("session_id"))
    if session_id is not None and event.get("type") in ("system", RESULT_EVENT_TYPE):
        summary["claude_session_id"] = session_id
    for key in ("subtype", "model", "permissionMode", "num_turns", "duration_ms", "is_error"):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)):
            summary[key] = _bounded(value, codex_terminal.CODEX_PROGRESS_METADATA_TEXT_LIMIT)
    tool_uses = [block for block in _content_blocks(event) if block.get("type") == "tool_use"]
    if tool_uses:
        first = tool_uses[0]
        summary["tool_name"] = codex_terminal._safe_identifier(first.get("name"))
        if len(tool_uses) > 1:
            summary["tool_use_count"] = len(tool_uses)
        tool_input = first.get("input") if isinstance(first.get("input"), dict) else {}
        if first.get("name") == "Bash" and tool_input.get("command") is not None:
            # Like Codex progress: argv0, length and hash only, never the raw command.
            summary["command"] = codex_terminal._command_summary(tool_input.get("command"))
        if first.get("name") in FILE_EDIT_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            paths = [_bounded(path, 240)] if _bounded(path, 240) else []
            summary["file_changes"] = {"count": len(paths), "paths": paths, "truncated": False}
    errors = event.get("errors")
    if event.get("type") == RESULT_EVENT_TYPE and not result_is_success(event):
        detail = errors if isinstance(errors, list) and errors else event.get("result")
        if detail:
            summary["error"] = _bounded(detail if isinstance(detail, str) else json.dumps(detail, default=str),
                                        codex_terminal.CODEX_PROGRESS_METADATA_TEXT_LIMIT)
    return summary


def _progress(kind: str, status: str, title: str, summary: str | None, metadata: dict[str, object]) -> dict[str, object]:
    return codex_terminal._progress_event(
        kind=kind, status=status, title=title, summary=summary, metadata=metadata, source=CLAUDE_PROGRESS_SOURCE,
    )


def normalize_claude_stream_event(line: str) -> dict[str, object]:
    """One stream-json line as a CRAX progress event (same kinds the dashboard knows)."""

    raw = line.strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        return _progress("codex_json_event", "malformed", "Malformed Claude Code stream event",
                         "A Claude Code stream line could not be parsed and was skipped.", {
                             "line_length": len(line),
                             "line_sha256": codex_terminal._sha256_text(line),
                             "error_type": type(error).__name__,
                         })
    raw_text = codex_terminal._safe_json_dumps(parsed)
    if not isinstance(parsed, dict):
        return _progress("codex_json_event", "observed", "Claude Code stream event",
                         "Claude Code emitted a non-object JSON event.", {
                             "json_type": type(parsed).__name__,
                             "raw_event_length": len(raw_text),
                             "raw_event_sha256": codex_terminal._sha256_text(raw_text),
                         })
    event_type = _stream_event_type(parsed)
    metadata: dict[str, object] = {
        "event_type": event_type,
        "keys": sorted(str(key) for key in parsed.keys())[:50],
        "raw_event_length": len(raw_text),
        "raw_event_sha256": codex_terminal._sha256_text(raw_text),
        "value_summary": _value_summary(parsed, event_type),
    }
    kind = parsed.get("type")
    blocks = _content_blocks(parsed)

    if kind == "system" and parsed.get("subtype") == "init":
        return _progress("codex_json_event", "running", "Claude Code session started",
                         "Claude Code started a session.", metadata)
    if kind == RESULT_EVENT_TYPE:
        if result_is_success(parsed):
            turns = parsed.get("num_turns")
            detail = f" after {turns} turns" if isinstance(turns, int) else ""
            return _progress("codex_json_event", "completed", "Claude Code finished",
                             f"Claude Code finished{detail}.", metadata)
        error = metadata["value_summary"].get("error")  # type: ignore[union-attr]
        return _progress("error", "failed", "Claude Code error",
                         str(error or f"Claude Code result: {parsed.get('subtype') or 'error'}."), metadata)
    if kind == "assistant":
        tool_uses = [block for block in blocks if block.get("type") == "tool_use"]
        if tool_uses:
            name = str(tool_uses[0].get("name") or "tool")
            if name == "Bash":
                argv0 = metadata["value_summary"].get("command", {}).get("argv0")  # type: ignore[union-attr]
                return _progress("command_started", "running", f"Command started: {argv0}" if argv0 else "Command started",
                                 "Claude Code ran a shell command.", metadata)
            if name in FILE_EDIT_TOOLS:
                return _progress("file_change_summary", "running", "File change summary",
                                 f"Claude Code used {name}.", metadata)
            return _progress("tool_event", "running", "Tool event", f"Claude Code used {name}.", metadata)
        texts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
        text = "\n".join(t for t in texts if t.strip())
        if text.strip():
            return _progress("assistant_commentary", "observed", "Claude Code update", text, metadata)
    if kind == "user":
        results = [block for block in blocks if block.get("type") == "tool_result"]
        if results:
            failed = any(block.get("is_error") is True for block in results)
            return _progress("tool_event", "failed" if failed else "completed",
                             "Tool failed" if failed else "Tool finished",
                             f"Claude Code received {len(results)} tool result(s).", metadata)
    return _progress("codex_json_event", "observed", "Claude Code stream event",
                     f"Claude Code emitted {event_type}.", metadata)


# --- execution ------------------------------------------------------------------------


def _base_result(prompt: str, repo_path: str, sandbox: str, command: list[str]) -> dict[str, Any]:
    now = _utc_now()
    return {
        "mode": "exec",
        "found": True,
        "executable_path": shutil.which(CLAUDE_EXECUTABLE),
        "coding_agent": CLAUDE_CODE_IDENTITY,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "command": command,
        "exit_code": 2,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "started_at": now,
        "finished_at": now,
        "validation_error": None,
        "final_message_path": None,
        "final_message": "",
        "final_message_length": 0,
        "final_message_status": "not_requested",
        "final_message_error": None,
    }


def claude_validation_result(prompt: str, repo_path: str, sandbox: str, validation_error: str) -> dict[str, Any]:
    result = _base_result(prompt, repo_path, sandbox, [CLAUDE_EXECUTABLE, "-p", "--", prompt])
    result.update({
        "found": result["executable_path"] is not None,
        "stderr": f"{validation_error}\n",
        "validation_error": validation_error,
        "final_message_error": validation_error,
    })
    return result


def run_claude_code_exec(
    prompt: str,
    *,
    repo_path: str | Path,
    sandbox: str,
    model: str | None = None,
    run_id: str | None = None,
    invocation_id: str | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    artifact_dir: str | Path | None = None,
    extraction_event_id: int | None = None,
    extraction_prompt_sha256: str | None = None,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Run ``claude -p`` through the durable wrapper; mirrors ``codex_terminal.run_codex_exec``."""

    if resume_session_id is None and session_id is None:
        session_id = str(uuid.uuid4())
    if resume_session_id is not None:
        session_id = None
    resolved_repo = Path(repo_path).expanduser().resolve(strict=False)
    repo_text = str(resolved_repo)
    invocation_id = invocation_id or f"codex-invocation-{uuid.uuid4().hex}"
    paths = artifact_paths_for(run_id or "unscoped-run", invocation_id, artifact_dir=artifact_dir)
    progress_metadata = {
        "coding_agent": CLAUDE_CODE_IDENTITY, "repo_path": repo_text, "sandbox": sandbox,
        "model": model, "codex_invocation_id": invocation_id,
    }

    explicit_effort = None if reasoning_effort in (None, CODEX_DEFAULT_SELECTION) else reasoning_effort
    validation_error = validate_claude_request(
        repo_path=resolved_repo, sandbox=sandbox, model=model,
        session_id=session_id, resume_session_id=resume_session_id,
    )
    if validation_error is None and explicit_effort is not None:
        if explicit_effort not in CLAUDE_EFFORT_LEVELS:
            validation_error = f"Invalid Claude Code effort. Allowed values: {', '.join(CLAUDE_EFFORT_LEVELS)}."
        elif not _is_default_model(model) and not claude_model_supports_effort(str(model), explicit_effort):
            validation_error = f"Claude model {model} does not support effort {explicit_effort}."
    if validation_error is not None:
        codex_terminal._emit_progress(progress_callback, _progress(
            "error", "failed", "Claude Code validation failed", validation_error,
            {**progress_metadata, "validation_error": validation_error}))
        return claude_validation_result(prompt, repo_text, sandbox, validation_error)

    # "Default" means what the user gets running `claude` themselves: their own
    # settings' model/effort, else Claude Code's built-in default.
    user_defaults = user_claude_defaults()
    effective_model = model
    model_source = "explicit"
    if _is_default_model(model):
        effective_model = user_defaults.get("model")
        model_source = "user_settings" if effective_model else "claude_code_default"
    # Effort: the run's explicit choice, else the user's own effortLevel, but
    # only if the model in use accepts it (e.g. Haiku 4.5 takes no effort).
    effort = explicit_effort
    effort_source = "explicit"
    if effort is None:
        effort = user_defaults.get("effort")
        effort_source = "user_settings" if effort else "claude_code_default"
        if effort and effective_model and not claude_model_supports_effort(effective_model, effort):
            effort, effort_source = None, "model_has_no_such_effort"
    command = build_claude_command(prompt, sandbox=sandbox, model=effective_model, effort=effort,
                                   session_id=session_id, resume_session_id=resume_session_id)
    executable_path = shutil.which(CLAUDE_EXECUTABLE)
    if executable_path is None:
        codex_terminal._emit_progress(progress_callback, _progress(
            "error", "failed", "Claude Code CLI not found", "Claude Code CLI was not found on PATH.", progress_metadata))
        result = _base_result(prompt, repo_text, sandbox, command)
        result.update({
            "found": False, "exit_code": None,
            "stderr": "Claude Code CLI not found on PATH.\n",
            "final_message_path": str(paths.final_message_path),
            "final_message_error": "Claude Code CLI not found on PATH.",
        })
        return result

    process_metadata: dict[str, Any] = {
        **progress_metadata,
        "run_id": run_id,
        "final_message_path": str(paths.final_message_path),
        "artifact_dir": str(paths.artifact_dir),
        "final_message_source": FINAL_MESSAGE_SOURCE_CLAUDE_STREAM,
        "agent_label": CLAUDE_AGENT_LABEL,
        "claude_session_id": resume_session_id or session_id,
        "claude_model": effective_model,
        "claude_model_source": model_source,
        "claude_effort": effort,
        "claude_effort_source": effort_source,
    }
    if extraction_event_id is not None:
        process_metadata["extraction_event_id"] = extraction_event_id
    if extraction_prompt_sha256 is not None:
        process_metadata["extraction_prompt_sha256"] = extraction_prompt_sha256
    result = codex_terminal.run_json_streaming_command(
        command, cwd=repo_text, progress_callback=progress_callback,
        process_metadata=process_metadata, progress_normalizer=normalize_claude_stream_event,
    )
    final_message = codex_terminal._final_message_artifact_result(paths.final_message_path, CLAUDE_AGENT_LABEL)
    _emit_finish_progress(progress_callback, process_metadata, result, final_message)
    payload = {
        **_base_result(prompt, repo_text, sandbox, command),
        "executable_path": executable_path,
        "codex_invocation_id": invocation_id,
        "json_stream": True,
        "claude_session_id": resume_session_id or session_id,
        "claude_model": effective_model,
        "claude_model_source": model_source,
        "claude_effort": effort,
        "claude_effort_source": effort_source,
        **result,
        **final_message,
    }
    if resume_session_id is not None:
        payload["resume_session_id"] = resume_session_id
    return payload


def _emit_finish_progress(
    progress_callback: Callable[[dict[str, object]], None] | None,
    metadata: dict[str, Any],
    result: dict[str, Any],
    final_message: dict[str, Any],
) -> None:
    status = str(final_message.get("final_message_status") or "")
    base = {key: value for key, value in metadata.items() if key != "agent_label"}
    codex_terminal._emit_progress(progress_callback, _progress(
        "final_message_available", "available" if status == "valid" else status or "unavailable",
        "Final message available" if status == "valid" else "Final message unavailable",
        "Claude Code final answer was read from the final-message artifact.",
        {**base, "final_message_status": status,
         "final_message_length": int(final_message.get("final_message_length") or 0),
         "final_message_error": final_message.get("final_message_error")}))
    stdout_text = str(result.get("stdout") or "")
    stderr_text = str(result.get("stderr") or "")
    codex_terminal._emit_progress(progress_callback, _progress(
        "process_exited", "completed" if result.get("exit_code") == 0 else "failed",
        "Claude Code process exited", "Claude Code process exited.",
        {**base, "exit_code": result.get("exit_code"), "timed_out": bool(result.get("timed_out")),
         "stdout_length": len(stdout_text), "stdout_sha256": codex_terminal._sha256_text(stdout_text),
         "stderr_length": len(stderr_text), "stderr_sha256": codex_terminal._sha256_text(stderr_text)}))


# --- evidence -------------------------------------------------------------------------


def _session_from_stdout(stdout: str) -> str | None:
    lines = stdout.splitlines()
    result = last_result_event(lines)
    from_result = _uuid_text(result.get("session_id")) if result else None
    return from_result or _uuid_text(first_session_id(lines))


def _command_session_id(command: object) -> str | None:
    if not isinstance(command, list):
        return None
    for flag in ("--resume", "--session-id"):
        if flag in command:
            index = command.index(flag)
            if index + 1 < len(command):
                return _uuid_text(command[index + 1])
    return None


def claude_session_details(run_id: str, events: list[dict]) -> dict[str, Any]:
    """The latest invocation's session id: stream evidence, else the id CRAX passed."""

    started = next((e for e in reversed(events) if e.get("event_type") == "codex_exec_started"), None)
    if started is None:
        return {"session_id": None, "invocation_id": None}
    metadata = event_metadata(started)
    invocation_id = metadata.get("codex_invocation_id")
    result = {"session_id": _uuid_text(metadata.get("resume_session_id")), "invocation_id": invocation_id}
    if not isinstance(invocation_id, str) or not invocation_id:
        return result
    for event in reversed(events):
        if event.get("event_type") != "codex_progress_event":
            continue
        progress = event_metadata(event)
        if progress.get("codex_invocation_id") != invocation_id:
            continue
        recovered = _uuid_text(progress.get("metadata", {}).get("value_summary", {}).get("claude_session_id"))
        if recovered:
            return {**result, "session_id": recovered}
    if result["session_id"]:
        return result
    paths = artifact_paths_for(run_id, invocation_id, artifact_dir=metadata.get("artifact_dir"))
    try:
        with paths.stdout_path.open(encoding="utf-8") as stream:
            prefix = stream.read(65536)
        recovered = _uuid_text(first_session_id(prefix.splitlines()))
    except (OSError, UnicodeError):
        recovered = None
    if recovered is None:
        intent = read_json_file(paths.intent_path) or {}
        recovered = _command_session_id(intent.get("command"))
    return {**result, "session_id": recovered}


def _result_event(result: dict[str, Any]) -> dict[str, Any] | None:
    return last_result_event(str(result.get("stdout") or "").splitlines())


def _reason_code(result: dict[str, Any]) -> str:
    if result.get("validation_error"):
        return "claude_code_validation_error"
    if result.get("found") is False:
        return "claude_code_not_found"
    if bool(result.get("timed_out")):
        return "claude_code_timed_out"
    if bool(result.get("progress_persistence_failed")):
        return "claude_code_progress_persistence_failed"
    exit_code = result.get("exit_code")
    if exit_code is None:
        return "claude_code_exit_missing"
    event = _result_event(result)
    if event is not None and not result_is_success(event):
        subtype = codex_terminal._safe_identifier(event.get("subtype"), 60) or "unknown"
        return f"claude_code_result_{subtype}" if subtype != "success" else "claude_code_result_error"
    if isinstance(exit_code, int) and exit_code != 0:
        return "claude_code_nonzero_exit"
    final_message_status = result.get("final_message_status")
    if final_message_status != "valid":
        return f"claude_code_final_message_{final_message_status or 'unavailable'}"
    if not str(result.get("final_message") or "").strip():
        return "claude_code_final_message_empty"
    # The controller branches on this exact code for any agent.
    if str(result.get("termination_reason") or "") == "uncertain" or result.get("invocation_complete") is False:
        return "codex_invocation_uncertain"
    return "claude_code_exec_completed"


def _error_message(result: dict[str, Any], reason_code: str) -> str | None:
    if reason_code == "claude_code_exec_completed":
        return None
    if reason_code == "codex_invocation_uncertain":
        return str(result.get("error_message") or "Claude Code invocation is uncertain and will not be replayed.")
    if reason_code == "claude_code_progress_persistence_failed":
        return "Claude Code progress events could not be persisted."
    if result.get("validation_error"):
        return str(result["validation_error"])
    if reason_code.startswith("claude_code_result_"):
        event = _result_event(result) or {}
        errors = event.get("errors")
        detail = errors if isinstance(errors, list) and errors else event.get("result")
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str) if detail else ""
        return f"Claude Code ended with {event.get('subtype') or 'an error'}" + (f": {text[:1000]}" if text else ".")
    if result.get("final_message_error"):
        return str(result["final_message_error"])
    stderr = str(result.get("stderr") or "").strip()
    return stderr or None


class ClaudeCodeProvider:
    identity = CLAUDE_CODE_IDENTITY

    def __init__(self, runner: Callable[..., dict[str, Any]] | None = None) -> None:
        self._runner = runner or run_claude_code_exec

    def normalize_progress(self, line: str) -> dict[str, object]:
        return normalize_claude_stream_event(line)

    def execute(self, request: CodingAgentRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "repo_path": request.repo_path, "sandbox": request.sandbox, "model": request.model,
            "run_id": request.run_id, "invocation_id": request.invocation_id,
            "progress_callback": request.progress_callback, "artifact_dir": request.artifact_dir,
            "extraction_event_id": request.extraction_event_id,
            "extraction_prompt_sha256": request.extraction_prompt_sha256,
            "reasoning_effort": getattr(request, "reasoning_effort", None),
        }
        if request.native_session is not None:
            if request.native_session.coding_agent != self.identity:
                raise ValueError("Native session belongs to another coding agent.")
            kwargs["resume_session_id"] = request.native_session.session_id
        return self._runner(request.prompt, **kwargs)

    def observe(self, invocation: Any, *, progress_callback: Callable | None) -> dict[str, Any]:
        result = codex_terminal.observe_existing_codex_invocation(
            invocation, progress_callback=progress_callback,
            progress_normalizer=self.normalize_progress, agent_label=CLAUDE_AGENT_LABEL,
        )
        result["coding_agent"] = self.identity
        result["executable_path"] = shutil.which(CLAUDE_EXECUTABLE)
        resumed = _uuid_text(event_metadata(invocation.started_event or {}).get("resume_session_id"))
        if resumed is not None:
            result["resume_session_id"] = resumed
        result["claude_session_id"] = resumed or _command_session_id(result.get("command"))
        return result

    session_details = staticmethod(claude_session_details)
    reason_code = staticmethod(_reason_code)
    error_message = staticmethod(_error_message)
    validation_result = staticmethod(claude_validation_result)

    def cancel(self, run_id: str, **kwargs: Any) -> dict[str, object]:
        return codex_terminal.terminate_coding_agent_processes(run_id, **kwargs)

    def classify(self, result: dict[str, Any]) -> ExecutionOutcome:
        cancelled = bool(result.get("cancel_requested")) or result.get("termination_reason") == "operator_cancelled"
        exit_code = result.get("exit_code")
        not_started = bool(result.get("validation_error")) or result.get("found") is False
        uncertain = not not_started and (
            result.get("invocation_complete") is False
            or result.get("termination_reason") == "uncertain"
            or exit_code is None
        )
        interrupted = cancelled or (isinstance(exit_code, int) and exit_code < 0) or bool(result.get("timed_out"))
        if not_started:
            process = "not_started"
        elif uncertain:
            process = "uncertain"
        elif cancelled:
            process = "cancelled"
        elif interrupted:
            process = "interrupted"
        else:
            process = "exited"
        final = str(result.get("final_message_status") or "unavailable")
        if final == "valid" and not str(result.get("final_message") or "").strip():
            final = "empty"
        event = _result_event(result)
        result_failed = event is not None and not result_is_success(event)
        session = _session_from_stdout(str(result.get("stdout") or "")) or _uuid_text(
            result.get("resume_session_id")) or _uuid_text(result.get("claude_session_id"))
        native = NativeSessionIdentity(self.identity, session) if session and not not_started else None
        if uncertain:
            provider = "uncertain"
        elif cancelled:
            provider = "cancelled"
        elif interrupted or not_started or result_failed or (isinstance(exit_code, int) and exit_code != 0):
            provider = "failed"
        elif exit_code == 0 and final == "valid" and result_is_success(event):
            provider = "completed"
        else:
            provider = "unverified"
        return ExecutionOutcome(process, provider, final, cancelled, interrupted, uncertain, native)
