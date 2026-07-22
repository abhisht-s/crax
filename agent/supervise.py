from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent.codex_terminal import ALLOWED_CODEX_SANDBOXES
from agent.continuation_policy import can_continue_run
from agent.prompt_extraction import (
    parse_metadata_json,
    select_valid_extracted_codex_prompt_event,
    sha256_text,
)
from agent.prompt_contract import parse_prompt_contract
from agent.workspace_write_policy import (
    classify_workspace_write_prompt,
)


SENTINEL_EXTRACTION_METHOD = "sentinel_block"


class SuperviseAction(StrEnum):
    STOP = "stop"
    ASK_SEND_TO_GPT = "ask_send_to_gpt"
    CAPTURE_GPT_RESPONSE = "capture_gpt_response"
    EXTRACT_NEXT_PROMPT = "extract_next_prompt"
    ASK_RUN_PROMPT = "ask_run_prompt"


@dataclass(frozen=True)
class SupervisePlan:
    action: SuperviseAction
    reason: str
    stop_message: str = ""
    event_ids: dict[str, int] = field(default_factory=dict)
    prompt_preview: str = ""
    prompt_text: str = ""
    prompt_sha: str = ""
    extraction_method: str = ""
    repo_path: str = ""
    sandbox: str = "read-only"
    status: str = ""
    warnings: tuple[str, ...] = ()
    codex_exit_code: int | None = None
    codex_timed_out: bool | None = None
    codex_sandbox: str = ""
    changed_files_count: int | None = None
    supervision_decision: str = ""
    prompt_auto_run_safe: bool = False
    prompt_auto_run_reason: str = ""
    pre_run_policy: dict = field(default_factory=dict)
    expected_scope: dict = field(default_factory=dict)
    prompt_contract: dict = field(default_factory=dict)


def detect_next_supervise_action(
    run: dict | None,
    events: list[dict],
    repo_path: str | None,
    sandbox: str = "read-only",
) -> SupervisePlan:
    repo_path_text = _resolve_repo_path_text(repo_path)
    sandbox_stop = _validate_sandbox(sandbox, repo_path_text)
    if sandbox_stop is not None:
        return sandbox_stop

    repo_stop = _validate_repo_path(repo_path_text, sandbox)
    if repo_stop is not None:
        return repo_stop

    if run is None:
        return _stop("run_missing", "Run not found.", repo_path_text, sandbox)

    status = str(run.get("status") or "")
    status_stop = _status_stop(status, repo_path_text, sandbox)
    if status_stop is not None:
        return status_stop

    latest_codex = _latest_event(events, "codex_exec_finished")
    if latest_codex is None:
        return _stop(
            "no_codex_result",
            "No completed Codex result was found for this run. Use codex-run for the initial step.",
            repo_path_text,
            sandbox,
            status=status,
        )

    codex_metadata = parse_metadata_json(latest_codex)
    codex_stop = _codex_result_stop(latest_codex, codex_metadata, repo_path_text, sandbox, status)
    if codex_stop is not None:
        return codex_stop

    active_run_stop = _active_extracted_prompt_run_stop(events, repo_path_text, sandbox, status)
    if active_run_stop is not None:
        return active_run_stop

    decision_stop = _supervision_decision_stop(
        events,
        latest_codex,
        repo_path_text,
        sandbox,
        status,
    )
    if decision_stop is not None:
        return decision_stop

    continuation = can_continue_run(status)
    if not continuation["can_continue"]:
        return _stop(
            "continuation_denied",
            (
                "Run cannot continue: "
                f"status={continuation['status']} "
                f"reason={continuation['reason']} "
                f"required_action={continuation['required_action'] or ''}"
            ),
            repo_path_text,
            sandbox,
            status=status,
        )

    latest_codex_id = _event_id(latest_codex)
    submitted_event = _latest_successful_submission_after(events, latest_codex_id)
    if submitted_event is None:
        return SupervisePlan(
            action=SuperviseAction.ASK_SEND_TO_GPT,
            reason="codex_result_ready",
            event_ids={"codex_exec_finished": latest_codex_id},
            repo_path=repo_path_text,
            sandbox=sandbox,
            status=status,
            codex_exit_code=_int_or_none(codex_metadata.get("exit_code")),
            codex_timed_out=bool(codex_metadata.get("timed_out")),
            codex_sandbox=str(codex_metadata.get("sandbox") or ""),
            changed_files_count=_latest_changed_files_count(events, latest_codex_id),
            supervision_decision=_latest_supervision_decision(events, latest_codex_id),
            warnings=_latest_diagnostic_warnings(events, latest_codex_id),
        )

    capture_event = _latest_capture_matching_submission(events, _event_id(submitted_event))
    if capture_event is not None and not _capture_integrity_ok(capture_event):
        return _stop(
            "captured_response_integrity_failed",
            "The captured ChatGPT response failed integrity validation.",
            repo_path_text,
            sandbox,
            status=status,
            event_ids={
                "codex_exec_finished": latest_codex_id,
                "gpt_feedback_submission_verified": _event_id(submitted_event),
                "gpt_response_captured": _event_id(capture_event),
            },
        )
    if capture_event is None:
        return SupervisePlan(
            action=SuperviseAction.CAPTURE_GPT_RESPONSE,
            reason="feedback_submitted_capture_needed",
            event_ids={
                "codex_exec_finished": latest_codex_id,
                "gpt_feedback_submission_verified": _event_id(submitted_event),
            },
            repo_path=repo_path_text,
            sandbox=sandbox,
            status=status,
        )

    extraction_event = _latest_extraction_matching_capture(
        events,
        _event_id(capture_event),
        _event_id(submitted_event),
    )
    latest_extraction_event = _latest_event(events, "next_codex_prompt_extracted")
    if extraction_event is None:
        if latest_extraction_event is not None and _event_id(latest_extraction_event) > _event_id(capture_event):
            return _stop(
                "ambiguous_extracted_prompt",
                "A later extracted prompt event does not match the latest captured ChatGPT response.",
                repo_path_text,
                sandbox,
                status=status,
                event_ids={"next_codex_prompt_extracted": _event_id(latest_extraction_event)},
            )
        return SupervisePlan(
            action=SuperviseAction.EXTRACT_NEXT_PROMPT,
            reason="gpt_response_captured_extract_needed",
            event_ids={
                "codex_exec_finished": latest_codex_id,
                "gpt_feedback_submission_verified": _event_id(submitted_event),
                "gpt_response_captured": _event_id(capture_event),
            },
            repo_path=repo_path_text,
            sandbox=sandbox,
            status=status,
        )

    selection = select_valid_extracted_codex_prompt_event(events, extraction_event)
    if not selection.ok:
        return _stop(
            "invalid_extracted_prompt",
            f"Invalid or stale extracted Codex prompt: {selection.error}",
            repo_path_text,
            sandbox,
            status=status,
            event_ids={"next_codex_prompt_extracted": _event_id(extraction_event)},
            warnings=selection.warnings,
        )
    if _event_id(selection.event) != _event_id(extraction_event):
        return _stop(
            "extracted_prompt_event_mismatch",
            "The validated extracted prompt did not match the latest captured ChatGPT response.",
            repo_path_text,
            sandbox,
            status=status,
            event_ids={
                "expected_next_codex_prompt_extracted": _event_id(extraction_event),
                "selected_next_codex_prompt_extracted": _event_id(selection.event),
            },
            warnings=selection.warnings,
        )

    extraction_metadata = selection.metadata
    extraction_method = str(extraction_metadata.get("extraction_method") or "")
    if extraction_method != SENTINEL_EXTRACTION_METHOD:
        return _stop(
            "non_sentinel_prompt",
            (
                "The extracted next prompt did not use the required sentinel format. "
                "Use exactly BEGIN_NEXT_CODEX_PROMPT and END_NEXT_CODEX_PROMPT."
            ),
            repo_path_text,
            sandbox,
            status=status,
            event_ids={"next_codex_prompt_extracted": _event_id(extraction_event)},
            warnings=selection.warnings,
        )

    extraction_event_id = _event_id(extraction_event)
    active_started = _latest_matching_prompt_run_started(
        events,
        extraction_event_id,
        selection.prompt_sha256,
    )
    if active_started is not None:
        active_finished = _latest_matching_prompt_run_finished_after(
            events,
            extraction_event_id,
            selection.prompt_sha256,
            _event_id(active_started),
        )
        if active_finished is None:
            return _stop(
                "extracted_prompt_run_incomplete",
                "An extracted Codex prompt run was started but no matching finish event was found.",
                repo_path_text,
                sandbox,
                status=status,
                event_ids={
                    "next_codex_prompt_extracted": extraction_event_id,
                    "extracted_codex_prompt_run_started": _event_id(active_started),
                },
            )

    latest_finished = _latest_matching_prompt_run_finished_after(
        events,
        extraction_event_id,
        selection.prompt_sha256,
        extraction_event_id,
    )
    if latest_finished is not None:
        return _stop(
            "extracted_prompt_already_run",
            "The latest extracted Codex prompt has already been run.",
            repo_path_text,
            sandbox,
            status=status,
            event_ids={
                "next_codex_prompt_extracted": extraction_event_id,
                "extracted_codex_prompt_run_finished": _event_id(latest_finished),
            },
            warnings=selection.warnings,
        )

    if sandbox == "workspace-write":
        pre_run_policy = classify_workspace_write_prompt(selection.prompt_text, sandbox)
        prompt_auto_run_safe = pre_run_policy.allowed
        prompt_auto_run_reason = pre_run_policy.reason_code
        pre_run_policy_dict = pre_run_policy.to_dict()
        expected_scope = (
            pre_run_policy.expected_scope.to_dict()
            if pre_run_policy.expected_scope is not None
            else {}
        )
        prompt_contract = parse_prompt_contract(selection.prompt_text, sandbox).to_dict()
    else:
        prompt_auto_run_safe, prompt_auto_run_reason = _classify_prompt_auto_run_safety(
            selection.prompt_text,
            sandbox,
        )
        pre_run_policy_dict = {
            "tier": "read_only_routine_auto" if prompt_auto_run_safe else "workspace_write_human_required",
            "allowed": prompt_auto_run_safe,
            "reason_code": prompt_auto_run_reason,
        }
        expected_scope = {}
        prompt_contract = parse_prompt_contract(selection.prompt_text, sandbox).to_dict()
    return SupervisePlan(
        action=SuperviseAction.ASK_RUN_PROMPT,
        reason="fresh_sentinel_prompt_ready",
        event_ids={
            "codex_exec_finished": latest_codex_id,
            "gpt_feedback_submission_verified": _event_id(submitted_event),
            "gpt_response_captured": _event_id(capture_event),
            "next_codex_prompt_extracted": extraction_event_id,
        },
        prompt_preview=_prompt_preview(selection.prompt_text),
        prompt_text=selection.prompt_text,
        prompt_sha=selection.prompt_sha256,
        extraction_method=extraction_method,
        repo_path=repo_path_text,
        sandbox=sandbox,
        status=status,
        warnings=selection.warnings,
        prompt_auto_run_safe=prompt_auto_run_safe,
        prompt_auto_run_reason=prompt_auto_run_reason,
        pre_run_policy=pre_run_policy_dict,
        expected_scope=expected_scope,
        prompt_contract=prompt_contract,
    )


def _resolve_repo_path_text(repo_path: str | None) -> str:
    if repo_path is None or not str(repo_path).strip():
        return ""
    return str(Path(repo_path).expanduser().resolve(strict=False))


def _validate_sandbox(sandbox: str, repo_path: str) -> SupervisePlan | None:
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        return _stop(
            "invalid_sandbox",
            f"Invalid Codex sandbox. Allowed values: {', '.join(ALLOWED_CODEX_SANDBOXES)}.",
            repo_path,
            sandbox,
        )
    return None


def _validate_repo_path(repo_path: str, sandbox: str) -> SupervisePlan | None:
    if not repo_path:
        return _stop("repo_missing", "An explicit --repo path is required.", repo_path, sandbox)
    path = Path(repo_path)
    if not path.exists():
        return _stop("repo_missing", f"Repo path does not exist: {repo_path}", repo_path, sandbox)
    if not path.is_dir():
        return _stop("repo_not_directory", f"Repo path is not a directory: {repo_path}", repo_path, sandbox)
    return None


def _status_stop(status: str, repo_path: str, sandbox: str) -> SupervisePlan | None:
    messages = {
        "created": "This run has no completed Codex result yet. Use codex-run for the initial step.",
        "running": "This run is currently running. Wait for the current step to finish.",
        "failed": "This run is failed. Inspect the failure before continuing.",
        "rejected": "This run was rejected and cannot continue.",
        "needs_review": "This run needs human review before it can continue.",
        "waiting_for_approval": "This run is waiting for explicit human approval before it can continue.",
    }
    if status in messages:
        return _stop(status, messages[status], repo_path, sandbox, status=status)
    return None


def _codex_result_stop(
    event: dict,
    metadata: dict,
    repo_path: str,
    sandbox: str,
    status: str,
) -> SupervisePlan | None:
    event_ids = {"codex_exec_finished": _event_id(event)}
    if metadata.get("validation_error"):
        return _stop(
            "codex_validation_error",
            f"Latest Codex run failed validation: {metadata.get('validation_error')}",
            repo_path,
            sandbox,
            status=status,
            event_ids=event_ids,
        )
    if metadata.get("found") is False:
        return _stop("codex_not_found", "Latest Codex run did not find the Codex CLI.", repo_path, sandbox, status=status, event_ids=event_ids)
    if bool(metadata.get("timed_out")):
        return _stop("codex_timed_out", "Latest Codex run timed out.", repo_path, sandbox, status=status, event_ids=event_ids)
    exit_code = _int_or_none(metadata.get("exit_code"))
    if exit_code is None:
        return _stop("codex_exit_missing", "Latest Codex run has no exit code.", repo_path, sandbox, status=status, event_ids=event_ids)
    if exit_code != 0:
        return _stop("codex_nonzero_exit", f"Latest Codex run exited with code {exit_code}.", repo_path, sandbox, status=status, event_ids=event_ids)
    return None


def _supervision_decision_stop(
    events: list[dict],
    latest_codex: dict,
    repo_path: str,
    sandbox: str,
    status: str,
) -> SupervisePlan | None:
    if status == "approved":
        return None
    diagnostics_event = _latest_event_after(events, "prompt_repo_impact_diagnostics", _event_id(latest_codex))
    if diagnostics_event is None:
        return _stop(
            "missing_required_diagnostics",
            "Latest Codex result is missing required prompt/repo impact diagnostics.",
            repo_path,
            sandbox,
            status=status,
            event_ids={"codex_exec_finished": _event_id(latest_codex)},
        )
    decision_event = _latest_event_after(events, "supervision_decision", _event_id(latest_codex))
    if decision_event is None:
        return _stop(
            "missing_supervision_decision",
            "Latest Codex result is missing a required supervision decision.",
            repo_path,
            sandbox,
            status=status,
            event_ids={"codex_exec_finished": _event_id(latest_codex)},
        )
    metadata = parse_metadata_json(decision_event)
    decision = str(metadata.get("decision") or "")
    if decision not in {"continue", "record_only", "needs_review", "approval_required"}:
        return _stop(
            "unknown_policy_result",
            f"Latest Codex result has an unknown supervision decision: {decision or 'missing'}.",
            repo_path,
            sandbox,
            status=status,
            event_ids={"supervision_decision": _event_id(decision_event)},
        )
    if bool(metadata.get("approval_required")):
        return _stop(
            "approval_required",
            "Latest Codex result requires explicit human approval.",
            repo_path,
            sandbox,
            status=status,
            event_ids={"supervision_decision": _event_id(decision_event)},
        )
    if bool(metadata.get("needs_review")):
        return _stop(
            "needs_review",
            "Latest Codex result needs human review.",
            repo_path,
            sandbox,
            status=status,
            event_ids={"supervision_decision": _event_id(decision_event)},
        )
    return None


def _classify_prompt_auto_run_safety(prompt: str, sandbox: str) -> tuple[bool, str]:
    text = prompt.strip()
    if not text:
        return False, "empty_prompt"
    return True, "safety_classifiers_disabled"


def _active_extracted_prompt_run_stop(
    events: list[dict],
    repo_path: str,
    sandbox: str,
    status: str,
) -> SupervisePlan | None:
    for event in reversed(events):
        if event.get("event_type") != "extracted_codex_prompt_run_started":
            continue
        started_metadata = parse_metadata_json(event)
        extraction_event_id = started_metadata.get("extraction_event_id")
        prompt_sha = started_metadata.get("prompt_sha256")
        if not isinstance(prompt_sha, str) or not prompt_sha.strip():
            continue
        finished = _latest_matching_prompt_run_finished_after(
            events,
            _event_id_value(extraction_event_id),
            prompt_sha,
            _event_id(event),
        )
        if finished is None:
            return _stop(
                "extracted_prompt_run_incomplete",
                "An extracted Codex prompt run was started but no matching finish event was found.",
                repo_path,
                sandbox,
                status=status,
                event_ids={"extracted_codex_prompt_run_started": _event_id(event)},
            )
        return None
    return None


def _latest_successful_submission_after(events: list[dict], event_id: int) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "gpt_feedback_submission_verified":
            continue
        if _event_id(event) <= event_id:
            continue
        metadata = parse_metadata_json(event)
        if metadata.get("reason_code") == "chatgpt_submission_verified":
            return event
    return None


def _latest_capture_matching_submission(events: list[dict], submission_event_id: int) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "gpt_response_captured":
            continue
        metadata = parse_metadata_json(event)
        if not _same_event_id(metadata.get("matched_submission_event_id"), submission_event_id):
            continue
        return event
    return None


def _capture_integrity_ok(event: dict) -> bool:
    metadata = parse_metadata_json(event)
    response_text = metadata.get("response_text")
    response_sha = metadata.get("response_sha256")
    return (
        isinstance(response_text, str)
        and response_text.strip()
        and isinstance(response_sha, str)
        and response_sha.strip()
        and sha256_text(response_text) == response_sha
    )


def _latest_extraction_matching_capture(
    events: list[dict],
    capture_event_id: int,
    submission_event_id: int,
) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "next_codex_prompt_extracted":
            continue
        metadata = parse_metadata_json(event)
        if not _same_event_id(metadata.get("source_event_id"), capture_event_id):
            continue
        if not _same_event_id(metadata.get("matched_submission_event_id"), submission_event_id):
            continue
        return event
    return None


def _latest_matching_prompt_run_started(
    events: list[dict],
    extraction_event_id: int,
    prompt_sha: str,
) -> dict | None:
    return _latest_matching_event_after(
        events,
        "extracted_codex_prompt_run_started",
        extraction_event_id,
        prompt_sha,
        extraction_event_id,
    )


def _latest_matching_prompt_run_finished_after(
    events: list[dict],
    extraction_event_id: int,
    prompt_sha: str,
    after_event_id: int,
) -> dict | None:
    return _latest_matching_event_after(
        events,
        "extracted_codex_prompt_run_finished",
        extraction_event_id,
        prompt_sha,
        after_event_id,
    )


def _latest_matching_event_after(
    events: list[dict],
    event_type: str,
    extraction_event_id: int,
    prompt_sha: str,
    after_event_id: int,
) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != event_type:
            continue
        if _event_id(event) <= after_event_id:
            continue
        metadata = parse_metadata_json(event)
        if not _same_event_id(metadata.get("extraction_event_id"), extraction_event_id):
            continue
        if metadata.get("prompt_sha256") != prompt_sha:
            continue
        return event
    return None


def _latest_event(events: list[dict], event_type: str) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _latest_event_after(events: list[dict], event_type: str, after_event_id: int) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") == event_type and _event_id(event) > after_event_id:
            return event
    return None


def _event_id(event: dict | None) -> int:
    if event is None:
        return -1
    try:
        return int(event.get("id") or -1)
    except (TypeError, ValueError):
        return -1


def _event_id_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _same_event_id(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return left == right


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _latest_changed_files_count(events: list[dict], latest_codex_id: int) -> int | None:
    event = _latest_event_after(events, "changed_file_classification", latest_codex_id)
    if event is None:
        return None
    metadata = parse_metadata_json(event)
    total_files = metadata.get("total_files")
    return total_files if isinstance(total_files, int) else None


def _latest_supervision_decision(events: list[dict], latest_codex_id: int) -> str:
    event = _latest_event_after(events, "supervision_decision", latest_codex_id)
    if event is None:
        return ""
    metadata = parse_metadata_json(event)
    return str(metadata.get("decision") or "")


def _latest_diagnostic_warnings(events: list[dict], latest_codex_id: int) -> tuple[str, ...]:
    event = _latest_event_after(events, "prompt_repo_impact_diagnostics", latest_codex_id)
    if event is None:
        return ()
    metadata = parse_metadata_json(event)
    flags = metadata.get("flags")
    if not isinstance(flags, list):
        return ()
    return tuple(str(flag) for flag in flags if str(flag).strip())


def _prompt_preview(prompt: str, limit: int = 500) -> str:
    if len(prompt) <= limit:
        return prompt
    return f"{prompt[:limit]}\n... (truncated)"


def _stop(
    reason: str,
    message: str,
    repo_path: str,
    sandbox: str,
    status: str = "",
    event_ids: dict[str, int] | None = None,
    warnings: tuple[str, ...] = (),
) -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.STOP,
        reason=reason,
        stop_message=message,
        event_ids=event_ids or {},
        repo_path=repo_path,
        sandbox=sandbox,
        status=status,
        warnings=warnings,
    )
