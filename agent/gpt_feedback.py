from __future__ import annotations

import json
from typing import Any


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


def build_gpt_feedback_message(run: dict, events: list[dict]) -> dict:
    codex_result = _latest_event_metadata(events, "codex_exec_finished") or {}
    classification = _latest_event_metadata(events, "changed_file_classification")
    diagnostics = _latest_event_metadata(events, "prompt_repo_impact_diagnostics")
    supervision_decision = _latest_event_metadata(events, "supervision_decision")
    continuation = _latest_event_metadata(events, "continuation_check")

    codex_stdout = str(codex_result.get("stdout") or "")
    codex_stderr = str(codex_result.get("stderr") or "")
    codex_exit_code = codex_result.get("exit_code")
    if not isinstance(codex_exit_code, int):
        codex_exit_code = None
    codex_timed_out = bool(codex_result.get("timed_out", False))
    changed_files = _changed_files_from_classification(classification)

    message_parts = ["Codex finished. Here is the output:\n"]
    if codex_stdout:
        _append_captured_output(message_parts, codex_stdout)
    else:
        message_parts.append("(Codex stdout was missing or empty.)\n")

    if codex_stderr:
        message_parts.append("Codex stderr:\n")
        _append_captured_output(message_parts, codex_stderr)

    message_parts.append(
        "\n".join(
            [
                "Run metadata:",
                f"- run_id: {run['id']}",
                f"- status: {run['status']}",
                f"- exit_code: {codex_exit_code}",
                f"- timed_out: {codex_timed_out}",
                f"- changed_files: {_json_summary(changed_files)}",
                f"- supervision_decision: {_json_summary(supervision_decision)}",
                f"- diagnostics_flags: {_json_summary(_diagnostics_flags(diagnostics))}",
                f"- continuation_allowed: {_continuation_allowed(continuation)}",
            ]
        )
    )

    return {
        "run_id": run["id"],
        "status": run["status"],
        "message": "".join(message_parts),
        "codex_stdout": codex_stdout,
        "codex_stderr": codex_stderr,
        "codex_exit_code": codex_exit_code,
        "codex_timed_out": codex_timed_out,
        "changed_files": changed_files,
        "supervision_decision": supervision_decision,
        "diagnostics": diagnostics,
        "continuation": continuation,
    }
