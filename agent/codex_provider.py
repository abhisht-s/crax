"""Codex native semantics, using CRAX's existing durable subprocess transport."""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from agent import codex_terminal
from agent.codex_invocation import artifact_paths_for, event_metadata
from agent.coding_agent_provider import CodingAgentRequest, ExecutionOutcome, NativeSessionIdentity


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _default_validation_result_builder(
    prompt: str,
    repo_path: str,
    sandbox: str,
    validation_error: str,
) -> dict[str, Any]:
    now = _utc_now()
    codex_path = shutil.which("codex")
    return {
        "mode": "exec",
        "found": codex_path is not None,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, prompt],
        "exit_code": 2,
        "stdout": "",
        "stderr": f"{validation_error}\n",
        "timed_out": False,
        "started_at": now,
        "finished_at": now,
        "validation_error": validation_error,
        "final_message_path": None,
        "final_message": "",
        "final_message_length": 0,
        "final_message_status": "not_requested",
        "final_message_error": validation_error,
    }


def _reason_code(result: dict[str, Any]) -> str:
    if result.get("validation_error"):
        return "codex_validation_error"
    if result.get("found") is False:
        return "codex_not_found"
    if bool(result.get("timed_out")):
        return "codex_timed_out"
    if bool(result.get("progress_persistence_failed")):
        return "codex_progress_persistence_failed"
    exit_code = result.get("exit_code")
    if exit_code is None:
        return "codex_exit_missing"
    if isinstance(exit_code, int) and exit_code != 0:
        return "codex_nonzero_exit"
    final_message_status = result.get("final_message_status")
    if final_message_status != "valid":
        return f"codex_final_message_{final_message_status or 'unavailable'}"
    if not str(result.get("final_message") or "").strip():
        return "codex_final_message_empty"
    if str(result.get("termination_reason") or "") == "uncertain":
        return "codex_invocation_uncertain"
    if result.get("invocation_complete") is False:
        return "codex_invocation_uncertain"
    return "codex_exec_completed"


def _error_message(result: dict[str, Any], reason_code: str) -> str | None:
    if reason_code == "codex_exec_completed":
        return None
    if reason_code == "codex_invocation_uncertain":
        return str(result.get("error_message") or "Codex invocation is uncertain and will not be replayed.")
    if reason_code == "codex_progress_persistence_failed":
        return "Codex progress events could not be persisted."
    validation_error = result.get("validation_error")
    if validation_error:
        return str(validation_error)
    final_message_error = result.get("final_message_error")
    if final_message_error:
        return str(final_message_error)
    stderr = str(result.get("stderr") or "").strip()
    return stderr or None


def _session_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def codex_session_details(run_id: str, events: list[dict]) -> dict[str, Any]:
    """Recover only the latest invocation's ID, including older lossy telemetry."""
    started = next((e for e in reversed(events) if e.get("event_type") == "codex_exec_started"), None)
    if started is None:
        return {"session_id": None, "invocation_id": None}
    metadata = event_metadata(started)
    invocation_id = metadata.get("codex_invocation_id")
    result = {"session_id": _session_id(metadata.get("resume_session_id")), "invocation_id": invocation_id}
    if not isinstance(invocation_id, str) or not invocation_id:
        return result
    for event in reversed(events):
        if event.get("event_type") != "codex_progress_event":
            continue
        progress = event_metadata(event)
        if progress.get("codex_invocation_id") != invocation_id:
            continue
        summary = progress.get("metadata", {}).get("value_summary", {})
        recovered = _session_id(summary.get("codex_session_id"))
        if recovered:
            return {**result, "session_id": recovered}
    if result["session_id"]:
        return result
    paths = artifact_paths_for(run_id, invocation_id, artifact_dir=metadata.get("artifact_dir"))
    # thread.started is near the beginning; never read an entire command log
    # on every dashboard poll, and never fall back to a sibling/older session.
    try:
        with paths.stdout_path.open(encoding="utf-8") as stream:
            prefix = stream.read(65536)
        for line in prefix.splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and raw.get("type") == "thread.started":
                result["session_id"] = _session_id(raw.get("thread_id"))
                break
    except (OSError, UnicodeError):
        pass
    return result


class CodexProvider:
    identity = "codex"

    def __init__(self, runner: Callable | None = None) -> None:
        # Only the legacy service's explicit test seam supplies a runner.
        self._runner = runner

    def normalize_progress(self, line: str) -> dict[str, object]:
        return codex_terminal.normalize_codex_jsonl_event(line)

    def execute(self, request: CodingAgentRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "repo_path": request.repo_path, "timeout_seconds": None,
            "sandbox": request.sandbox, "run_id": request.run_id,
        }
        if request.model != "codex_default":
            kwargs["model"] = request.model
        if request.native_session is not None:
            if request.native_session.coding_agent != self.identity:
                raise ValueError("Native session belongs to another coding agent.")
            kwargs["resume_session_id"] = request.native_session.session_id
        if request.invocation_id is not None:
            kwargs.update(json_stream=True, codex_invocation_id=request.invocation_id,
                          progress_callback=request.progress_callback)
            for name in ("artifact_dir", "extraction_event_id", "extraction_prompt_sha256"):
                value = getattr(request, name)
                if value is not None:
                    kwargs[name] = value
        if self._runner is None:
            kwargs["progress_normalizer"] = self.normalize_progress
        return (self._runner or codex_terminal.run_codex_exec)(request.prompt, **kwargs)

    def observe(self, invocation: Any, *, progress_callback: Callable | None) -> dict[str, Any]:
        result = codex_terminal.observe_existing_codex_invocation(
            invocation, progress_callback=progress_callback,
            progress_normalizer=self.normalize_progress,
        )
        resumed = _session_id(event_metadata(invocation.started_event or {}).get("resume_session_id"))
        if resumed is not None:
            result["resume_session_id"] = resumed
        return result

    session_details = staticmethod(codex_session_details)
    reason_code = staticmethod(_reason_code)
    error_message = staticmethod(_error_message)
    validation_result = staticmethod(_default_validation_result_builder)

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
        native = None
        # Session identity is evidence only; it is never used as final output.
        for line in str(result.get("stdout") or "").splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                session_id = _session_id(event.get("thread_id"))
                if session_id:
                    native = NativeSessionIdentity(self.identity, session_id)
                    break
        if native is None:
            resumed = _session_id(result.get("resume_session_id"))
            if resumed is not None:
                native = NativeSessionIdentity(self.identity, resumed)
        if uncertain:
            provider = "uncertain"
        elif cancelled:
            provider = "cancelled"
        elif interrupted or not_started or (isinstance(exit_code, int) and exit_code != 0):
            provider = "failed"
        elif exit_code == 0 and final == "valid":
            provider = "completed"
        else:
            provider = "unverified"
        return ExecutionOutcome(process, provider, final, cancelled, interrupted, uncertain, native)
