from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

FEEDBACK_PAYLOAD_VERSION = "compact_wrapper_v2_submission_marker"
SUBMISSION_MARKER_BEGIN = "AGENT_SUBMISSION"
SUBMISSION_MARKER_END = "END_AGENT_SUBMISSION"
MAX_CLEAN_FINAL_MESSAGE_CHARS = 12_000
MAX_CHATGPT_FEEDBACK_PAYLOAD_CHARS = 16_000


def _event_metadata(event: dict) -> dict | None:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata

    metadata_json = event.get("metadata_json")
    if not metadata_json:
        return None

    try:
        decoded = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None

    return decoded if isinstance(decoded, dict) else None


def _latest_event_metadata(events: list[dict], event_type: str) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return _event_metadata(event)
    return None


def _changed_files_from_classification(classification: dict | None) -> list[str]:
    if not classification:
        return []

    files = classification.get("files")
    if isinstance(files, list):
        changed_files = []
        for file in files:
            if not isinstance(file, dict):
                continue
            path = str(file.get("path") or "").strip()
            if path:
                changed_files.append(path)
        return changed_files

    changed_files = classification.get("changed_files")
    if isinstance(changed_files, list):
        return [str(path).strip() for path in changed_files if str(path).strip()]

    return []


def _json_summary(value: Any) -> str:
    if value is None:
        return "null"
    return json.dumps(value, sort_keys=True)


def _diagnostics_flags(diagnostics: dict | None) -> list[str] | None:
    if diagnostics is None:
        return None

    flags = diagnostics.get("flags")
    if isinstance(flags, list):
        return [str(flag) for flag in flags]

    return []


def _continuation_allowed(continuation: dict | None) -> bool | None:
    if continuation is None:
        return None

    can_continue = continuation.get("can_continue")
    return can_continue if isinstance(can_continue, bool) else None


def _append_captured_output(parts: list[str], output: str) -> None:
    parts.append(output)
    if not output.endswith("\n"):
        parts.append("\n")


def _build_submission_marker(run_id: str, payload_sha256: str, nonce: str | None = None) -> dict:
    marker_nonce = nonce or str(uuid.uuid4())
    marker_text = "\n".join(
        [
            SUBMISSION_MARKER_BEGIN,
            f"run_id={run_id}",
            f"nonce={marker_nonce}",
            f"payload_sha256={payload_sha256}",
            SUBMISSION_MARKER_END,
        ]
    )
    return {
        "submission_marker_text": marker_text,
        "submission_marker_sha256": hashlib.sha256(marker_text.encode("utf-8")).hexdigest(),
        "submission_marker_nonce": marker_nonce,
        "submission_marker_payload_sha256": payload_sha256,
    }


def _feedback_failure(
    run: dict,
    *,
    reason_code: str,
    error_message: str,
    codex_exit_code: int | None,
    codex_timed_out: bool,
    changed_files: list[str],
    diagnostics: dict[str, Any],
) -> dict:
    return {
        "run_id": run["id"],
        "status": run["status"],
        "ok": False,
        "submittable": False,
        "reason_code": reason_code,
        "error_message": error_message,
        "message": None,
        "payload_without_marker": None,
        "payload_without_marker_sha256": None,
        "feedback_payload_version": FEEDBACK_PAYLOAD_VERSION,
        "feedback_payload_sha256": None,
        "feedback_payload_length": 0,
        "submission_marker_text": None,
        "submission_marker_sha256": None,
        "submission_marker_nonce": None,
        "submission_marker_payload_sha256": None,
        "codex_exit_code": codex_exit_code,
        "codex_timed_out": codex_timed_out,
        "changed_files": changed_files,
        "transport_guard": diagnostics,
    }


def build_gpt_feedback_message(run: dict, events: list[dict], marker_nonce: str | None = None) -> dict:
    codex_result = _latest_event_metadata(events, "codex_exec_finished") or {}
    classification = _latest_event_metadata(events, "changed_file_classification")
    diagnostics = _latest_event_metadata(events, "prompt_repo_impact_diagnostics")
    supervision_decision = _latest_event_metadata(events, "supervision_decision")
    continuation = _latest_event_metadata(events, "continuation_check")

    codex_exit_code = codex_result.get("exit_code")
    if not isinstance(codex_exit_code, int):
        codex_exit_code = None
    codex_timed_out = bool(codex_result.get("timed_out", False))
    changed_files = _changed_files_from_classification(classification)
    final_message_status = str(codex_result.get("final_message_status") or "")
    final_message_error = str(codex_result.get("final_message_error") or "")
    final_message_path = codex_result.get("final_message_path")
    clean_final_message = str(codex_result.get("final_message") or "")
    clean_final_message_length = len(clean_final_message)

    artifact_diagnostics = {
        "final_message_status": final_message_status or None,
        "final_message_error": final_message_error or None,
        "final_message_path": str(final_message_path) if final_message_path is not None else None,
        "final_message_length": clean_final_message_length,
        "max_clean_final_message_chars": MAX_CLEAN_FINAL_MESSAGE_CHARS,
        "max_chatgpt_feedback_payload_chars": MAX_CHATGPT_FEEDBACK_PAYLOAD_CHARS,
    }
    if final_message_status != "valid" or not clean_final_message:
        return _feedback_failure(
            run,
            reason_code="codex_final_message_unavailable",
            error_message=final_message_error or "Codex final assistant message artifact is unavailable.",
            codex_exit_code=codex_exit_code,
            codex_timed_out=codex_timed_out,
            changed_files=changed_files,
            diagnostics=artifact_diagnostics,
        )
    if clean_final_message_length > MAX_CLEAN_FINAL_MESSAGE_CHARS:
        return _feedback_failure(
            run,
            reason_code="codex_final_message_oversize",
            error_message=(
                "Codex final assistant message exceeds the ChatGPT transport limit "
                f"({clean_final_message_length} > {MAX_CLEAN_FINAL_MESSAGE_CHARS})."
            ),
            codex_exit_code=codex_exit_code,
            codex_timed_out=codex_timed_out,
            changed_files=changed_files,
            diagnostics={
                **artifact_diagnostics,
                "final_message_sha256": hashlib.sha256(clean_final_message.encode("utf-8")).hexdigest(),
            },
        )

    message_parts = [
        "Codex completion report\n\n",
        "Run metadata:",
        f"- run_id: {run['id']}",
        f"- run_status: {run['status']}",
        f"- exit_code: {codex_exit_code}",
        f"- timed_out: {str(codex_timed_out).lower()}",
    ]
    if changed_files:
        message_parts.append(f"- changed_files: {_json_summary(changed_files)}")

    decision = supervision_decision.get("decision") if isinstance(supervision_decision, dict) else None
    needs_review = bool(supervision_decision.get("needs_review")) if isinstance(supervision_decision, dict) else False
    approval_required = bool(supervision_decision.get("approval_required")) if isinstance(supervision_decision, dict) else False
    if decision not in {None, "", "continue", "record_only"} or needs_review or approval_required:
        message_parts.append(f"- review_signal: {decision or 'unknown'}")
    if diagnostics is None:
        message_parts.append("- stop_reason: missing_diagnostics")
    elif decision is None:
        message_parts.append("- stop_reason: missing_supervision_decision")
    elif needs_review or approval_required:
        message_parts.append(f"- stop_reason: {decision or 'human_gate_required'}")
    elif codex_exit_code not in (0, None) or codex_timed_out:
        message_parts.append("- stop_reason: codex_execution_not_clean")

    message_parts.extend(["", "Final assistant message:", clean_final_message, ""])
    payload_without_marker = "\n".join(message_parts)
    payload_without_marker_sha256 = hashlib.sha256(payload_without_marker.encode("utf-8")).hexdigest()
    marker = _build_submission_marker(run["id"], payload_without_marker_sha256, nonce=marker_nonce)
    message = f"{marker['submission_marker_text']}\n\n{payload_without_marker}"
    if len(message) > MAX_CHATGPT_FEEDBACK_PAYLOAD_CHARS:
        return _feedback_failure(
            run,
            reason_code="chatgpt_feedback_payload_oversize",
            error_message=(
                "ChatGPT feedback payload exceeds the transport limit "
                f"({len(message)} > {MAX_CHATGPT_FEEDBACK_PAYLOAD_CHARS})."
            ),
            codex_exit_code=codex_exit_code,
            codex_timed_out=codex_timed_out,
            changed_files=changed_files,
            diagnostics={
                **artifact_diagnostics,
                "attempted_payload_length": len(message),
                "attempted_payload_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "final_message_sha256": hashlib.sha256(clean_final_message.encode("utf-8")).hexdigest(),
            },
        )
    message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()

    return {
        "run_id": run["id"],
        "status": run["status"],
        "ok": True,
        "submittable": True,
        "reason_code": None,
        "error_message": None,
        "message": message,
        "payload_without_marker": payload_without_marker,
        "payload_without_marker_sha256": payload_without_marker_sha256,
        "feedback_payload_version": FEEDBACK_PAYLOAD_VERSION,
        "feedback_payload_sha256": message_sha256,
        "feedback_payload_length": len(message),
        **marker,
        "codex_exit_code": codex_exit_code,
        "codex_timed_out": codex_timed_out,
        "changed_files": changed_files,
        "final_message_length": clean_final_message_length,
        "final_message_sha256": hashlib.sha256(clean_final_message.encode("utf-8")).hexdigest(),
        "transport_guard": artifact_diagnostics,
    }
