from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.codex_terminal import ALLOWED_CODEX_SANDBOXES
from agent.continuation_policy import can_continue_run
from agent.prompt_contract import parse_prompt_contract
from agent.prompt_extraction import (
    select_latest_valid_extracted_codex_prompt,
    select_valid_extracted_codex_prompt_event,
    sha256_text,
)
from agent.workspace_write_policy import (
    POLICY_VERSION as WORKSPACE_WRITE_POLICY_VERSION,
)


CodexFlowCoordinator = Callable[[str, dict, str, str, str, float | None, bool], dict[str, Any]]


@dataclass(frozen=True)
class ExtractedCodexPromptExecutionResult:
    ok: bool
    run_id: str
    reason_code: str | None
    error_message: str | None
    selected_event_id: int | None
    selected_prompt_text: str | None
    selected_prompt_sha256: str | None
    selected_method: str | None
    source_capture_event_id: int | None
    source_response_sha256: str | None
    artifact_path: str | None
    artifact_status: str | None
    continuation_result: dict[str, Any] | None
    workspace_write_pre_run_result: dict[str, Any] | None
    sandbox: str
    started_event_id: int | None
    finished_event_id: int | None
    codex_flow_result: dict[str, Any] | None
    exit_code: int | None
    timed_out: bool | None
    status: str | None
    events_written: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    persisted: bool = False
    selection: Any = None


def _default_codex_flow_coordinator(
    run_id: str,
    run: dict,
    prompt: str,
    repo_path_text: str,
    sandbox: str,
    timeout: float | None,
    confirm_full_access: bool,
) -> dict[str, Any]:
    from agent.cli import _run_codex_exec_flow

    return _run_codex_exec_flow(
        run_id,
        run,
        prompt,
        repo_path_text,
        sandbox,
        timeout,
        confirm_full_access=confirm_full_access,
    )


def _event_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _event_id_or_negative(value: object) -> int:
    event_id = _event_id(value)
    return event_id if event_id is not None else -1


def _record_wrapper_event(
    event_ledger: Any,
    events_written: list[dict[str, Any]],
    run_id: str,
    event_type: str,
    message: str,
    metadata: dict[str, Any],
) -> int | None:
    event_id = _event_id(event_ledger.add_event(run_id, event_type, message, metadata))
    events_written.append(
        {
            "event_type": event_type,
            "event_id": event_id,
            "message": message,
            "metadata": metadata,
        }
    )
    return event_id


def _failure(
    run_id: str,
    sandbox: str,
    reason_code: str,
    error_message: str,
    exit_code: int,
    *,
    continuation_result: dict[str, Any] | None = None,
    selected_event_id: int | None = None,
    selected_prompt_text: str | None = None,
    selected_prompt_sha256: str | None = None,
    selected_method: str | None = None,
    source_capture_event_id: int | None = None,
    source_response_sha256: str | None = None,
    artifact_path: str | None = None,
    artifact_status: str | None = None,
    workspace_write_pre_run_result: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExtractedCodexPromptExecutionResult:
    return ExtractedCodexPromptExecutionResult(
        ok=False,
        run_id=run_id,
        reason_code=reason_code,
        error_message=error_message,
        selected_event_id=selected_event_id,
        selected_prompt_text=selected_prompt_text,
        selected_prompt_sha256=selected_prompt_sha256,
        selected_method=selected_method,
        source_capture_event_id=source_capture_event_id,
        source_response_sha256=source_response_sha256,
        artifact_path=artifact_path,
        artifact_status=artifact_status,
        continuation_result=continuation_result,
        workspace_write_pre_run_result=workspace_write_pre_run_result,
        sandbox=sandbox,
        started_event_id=None,
        finished_event_id=None,
        codex_flow_result=None,
        exit_code=exit_code,
        timed_out=None,
        status=None,
        events_written=[],
        metadata=metadata or {},
        persisted=False,
        selection=None,
    )


def _latest_extraction_event_id(events: list[dict]) -> int:
    latest_extraction_event_id = -1
    for event in events:
        if event.get("event_type") != "next_codex_prompt_extracted":
            continue
        latest_extraction_event_id = max(
            latest_extraction_event_id,
            _event_id_or_negative(event),
        )
    return latest_extraction_event_id


def _event_by_id(events: list[dict], expected_event_id: int) -> dict | None:
    for event in events:
        if _event_id_or_negative(event) == expected_event_id:
            return event
    return None


def _changed_after_approval_failure(
    run_id: str,
    sandbox: str,
    workspace_write_pre_run_policy: dict[str, Any] | None,
) -> ExtractedCodexPromptExecutionResult:
    return _failure(
        run_id,
        sandbox,
        "extracted_prompt_changed_after_approval",
        "Stopped: the next prompt changed after it was shown for approval.",
        1,
        workspace_write_pre_run_result=workspace_write_pre_run_policy,
    )


def _selection_artifact_status(selection: Any) -> str | None:
    prompt_path = getattr(selection, "prompt_path", None)
    if not prompt_path:
        return None
    warnings = tuple(getattr(selection, "warnings", ()))
    if "extracted prompt artifact is missing; using validated prompt_text metadata." in warnings:
        return "missing_allowed"
    return "validated"


def _invalid_selection_artifact_status(selection: Any) -> str | None:
    prompt_path = getattr(selection, "prompt_path", None)
    error = str(getattr(selection, "error", "") or "")
    if "artifact SHA" in error:
        return "sha_mismatch"
    if "artifact" in error and "missing" in error:
        return "missing"
    if "failed to read extracted prompt artifact" in error:
        return "read_failed"
    if "path exists but is not a file" in error:
        return "not_file"
    if prompt_path:
        return "invalid"
    return None


def _exit_code_from_flow(flow: dict[str, Any]) -> int:
    result = flow["result"]
    if result["validation_error"]:
        return 2
    if not result["found"]:
        return 1
    if result["timed_out"]:
        return 124
    if result["exit_code"] != 0:
        return result["exit_code"] or 1
    governance_observation = flow.get("governance_observation") or {}
    if governance_observation.get("objective_failures"):
        return 1
    return 0


def execute_extracted_codex_prompt_service(
    run_id: str,
    run: dict | None,
    repo_path_text: str,
    sandbox: str,
    timeout: float | None,
    *,
    confirm_full_access: bool,
    allow_full_access: bool,
    approval_mode: str,
    expected_extraction_event_id: int | None = None,
    expected_prompt_sha256: str | None = None,
    expected_prompt_text: str | None = None,
    expected_extraction_method: str | None = None,
    workspace_write_pre_run_policy: dict[str, Any] | None = None,
    expected_scope: dict[str, Any] | None = None,
    ledger: Any = default_ledger,
    latest_extraction_selector: Callable[..., Any] = select_latest_valid_extracted_codex_prompt,
    extraction_event_selector: Callable[..., Any] = select_valid_extracted_codex_prompt_event,
    continuation_evaluator: Callable[[str | None], dict[str, Any]] = can_continue_run,
    prompt_contract_parser: Callable[..., Any] = parse_prompt_contract,
    codex_flow_coordinator: CodexFlowCoordinator | None = None,
    hash_function: Callable[[str], str] = sha256_text,
    prompt_preview_callback: Callable[[str, Any, str, str], None] | None = None,
) -> ExtractedCodexPromptExecutionResult:
    del timeout
    current_run = ledger.get_run(run_id)
    if current_run is None:
        return _failure(
            run_id,
            sandbox,
            "run_not_found",
            f"Run not found: {run_id}",
            1,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )
    run = current_run

    continuation = continuation_evaluator(run["status"])
    if not continuation["can_continue"]:
        message = (
            "Run cannot continue: "
            f"status={continuation['status']} "
            f"reason={continuation['reason']} "
            f"required_action={continuation['required_action'] or ''}"
        )
        return _failure(
            run_id,
            sandbox,
            "continuation_denied",
            message,
            2,
            continuation_result=continuation,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )

    repo_path = Path(repo_path_text).expanduser().resolve(strict=False)
    repo_path_text = str(repo_path)
    if not repo_path.exists():
        return _failure(
            run_id,
            sandbox,
            "repo_missing",
            f"Repo path does not exist: {repo_path_text}",
            2,
            continuation_result=continuation,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )
    if not repo_path.is_dir():
        return _failure(
            run_id,
            sandbox,
            "repo_not_directory",
            f"Repo path is not a directory: {repo_path_text}",
            2,
            continuation_result=continuation,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        return _failure(
            run_id,
            sandbox,
            "invalid_sandbox",
            "Invalid Codex sandbox. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_SANDBOXES)}.",
            2,
            continuation_result=continuation,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )
    if sandbox == "danger-full-access" and not allow_full_access:
        return _failure(
            run_id,
            sandbox,
            "danger_full_access_blocked",
            "The supervise command does not support danger-full-access in v0.1.",
            2,
            continuation_result=continuation,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )
    if sandbox == "danger-full-access" and not confirm_full_access:
        return _failure(
            run_id,
            sandbox,
            "full_access_confirmation_required",
            "Codex sandbox danger-full-access requires --confirm-full-access.",
            2,
            continuation_result=continuation,
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )

    events = ledger.list_events(run_id)
    if expected_extraction_event_id is None:
        selection = latest_extraction_selector(
            events,
            expect_prompt_sha256=expected_prompt_sha256,
        )
    else:
        if _latest_extraction_event_id(events) > expected_extraction_event_id:
            return _changed_after_approval_failure(
                run_id,
                sandbox,
                workspace_write_pre_run_policy,
            )

        extraction_event = _event_by_id(events, expected_extraction_event_id)
        if extraction_event is None:
            return _changed_after_approval_failure(
                run_id,
                sandbox,
                workspace_write_pre_run_policy,
            )
        selection = extraction_event_selector(
            events,
            extraction_event,
            expect_prompt_sha256=expected_prompt_sha256,
        )

    if not selection.ok:
        event_id = selection.event.get("id") if selection.event else None
        return _failure(
            run_id,
            sandbox,
            "invalid_extracted_prompt",
            str(selection.error or "Invalid extracted Codex prompt."),
            1,
            continuation_result=continuation,
            selected_event_id=_event_id(event_id),
            selected_prompt_text=selection.prompt_text or None,
            selected_prompt_sha256=selection.prompt_sha256 or None,
            selected_method=selection.metadata.get("extraction_method"),
            source_capture_event_id=_event_id(selection.source_event_id),
            source_response_sha256=selection.metadata.get("source_response_sha256"),
            artifact_path=selection.prompt_path,
            artifact_status=_invalid_selection_artifact_status(selection),
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
            metadata={"selection_warnings": tuple(selection.warnings)},
        )

    selected_event_id = selection.event.get("id") if selection.event else None
    selected_event_id_int = _event_id_or_negative(selected_event_id)
    if (
        expected_extraction_event_id is not None
        and selected_event_id_int != expected_extraction_event_id
    ):
        return _changed_after_approval_failure(
            run_id,
            sandbox,
            workspace_write_pre_run_policy,
        )
    if expected_prompt_sha256 is not None and selection.prompt_sha256 != expected_prompt_sha256:
        return _changed_after_approval_failure(
            run_id,
            sandbox,
            workspace_write_pre_run_policy,
        )
    if expected_prompt_text is not None:
        if (
            selection.prompt_text != expected_prompt_text
            or hash_function(selection.prompt_text) != selection.prompt_sha256
        ):
            return _changed_after_approval_failure(
                run_id,
                sandbox,
                workspace_write_pre_run_policy,
            )
    if hash_function(selection.prompt_text) != selection.prompt_sha256:
        return _failure(
            run_id,
            sandbox,
            "selected_prompt_sha_validation_failed",
            "Stopped: the selected prompt failed SHA validation.",
            1,
            continuation_result=continuation,
            selected_event_id=_event_id(selected_event_id),
            selected_prompt_text=selection.prompt_text,
            selected_prompt_sha256=selection.prompt_sha256,
            selected_method=selection.metadata.get("extraction_method"),
            source_capture_event_id=_event_id(selection.source_event_id),
            source_response_sha256=selection.metadata.get("source_response_sha256"),
            artifact_path=selection.prompt_path,
            artifact_status=_selection_artifact_status(selection),
            workspace_write_pre_run_result=workspace_write_pre_run_policy,
        )
    if expected_extraction_method is not None:
        selected_method = selection.metadata.get("extraction_method")
        if selected_method != expected_extraction_method:
            return _changed_after_approval_failure(
                run_id,
                sandbox,
                workspace_write_pre_run_policy,
            )

    if prompt_preview_callback is not None:
        prompt_preview_callback(run_id, selection, repo_path_text, sandbox)

    extraction_event_id = selection.event.get("id") if selection.event else None
    prompt_contract = prompt_contract_parser(selection.prompt_text, sandbox).to_dict()
    selection_metadata = {
        "extraction_event_id": extraction_event_id,
        "prompt_sha256": selection.prompt_sha256,
        "prompt_length": selection.prompt_length,
        "repo_path": repo_path_text,
        "sandbox": sandbox,
        "prompt_contract": prompt_contract,
        "source_event_id": selection.source_event_id,
        "matched_submission_event_id": selection.matched_submission_event_id,
        "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
        "pre_run_policy": workspace_write_pre_run_policy or {},
        "expected_scope": expected_scope or {},
        "auto_run_allowed": approval_mode == "auto",
        "reason_code": (workspace_write_pre_run_policy or {}).get("reason_code"),
    }

    events_written: list[dict[str, Any]] = []
    selected_event_wrapper_id = _record_wrapper_event(
        ledger,
        events_written,
        run_id,
        "extracted_codex_prompt_selected",
        (
            "Selected extracted Codex prompt for automatic execution."
            if approval_mode == "auto"
            else "Selected extracted Codex prompt for human-confirmed execution."
        ),
        {
            **selection_metadata,
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
        },
    )
    del selected_event_wrapper_id
    started_event_id = _record_wrapper_event(
        ledger,
        events_written,
        run_id,
        "extracted_codex_prompt_run_started",
        (
            "Running extracted Codex prompt automatically after routine-safe classification."
            if approval_mode == "auto"
            else "Running extracted Codex prompt after explicit human confirmation."
        ),
        {
            **selection_metadata,
            "timeout": None,
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
        },
    )

    coordinator = codex_flow_coordinator or _default_codex_flow_coordinator
    flow = coordinator(
        run_id,
        run,
        selection.prompt_text,
        repo_path_text,
        sandbox,
        None,
        confirm_full_access,
    )

    raw_result = flow["result"]
    supervision_decision = flow["supervision_decision"] or {}
    transition = flow["transition"] or {}
    finished_event_id = _record_wrapper_event(
        ledger,
        events_written,
        run_id,
        "extracted_codex_prompt_run_finished",
        "Finished extracted Codex prompt execution.",
        {
            **selection_metadata,
            "exit_code": raw_result["exit_code"],
            "timed_out": raw_result["timed_out"],
            "status": transition.get("next_status"),
            "supervision_decision": supervision_decision.get("decision"),
            "validation_error": raw_result["validation_error"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
        },
    )

    exit_code = _exit_code_from_flow(flow)
    return ExtractedCodexPromptExecutionResult(
        ok=exit_code == 0,
        run_id=run_id,
        reason_code="extracted_codex_prompt_run_completed" if exit_code == 0 else "extracted_codex_prompt_run_failed",
        error_message=None,
        selected_event_id=_event_id(selected_event_id),
        selected_prompt_text=selection.prompt_text,
        selected_prompt_sha256=selection.prompt_sha256,
        selected_method=selection.metadata.get("extraction_method"),
        source_capture_event_id=_event_id(selection.source_event_id),
        source_response_sha256=selection.metadata.get("source_response_sha256"),
        artifact_path=selection.prompt_path,
        artifact_status=_selection_artifact_status(selection),
        continuation_result=continuation,
        workspace_write_pre_run_result=workspace_write_pre_run_policy,
        sandbox=sandbox,
        started_event_id=started_event_id,
        finished_event_id=finished_event_id,
        codex_flow_result=flow,
        exit_code=exit_code,
        timed_out=bool(raw_result["timed_out"]),
        status=transition.get("next_status"),
        events_written=events_written,
        metadata={
            "selection_metadata": selection_metadata,
            "selection_warnings": tuple(selection.warnings),
            "codex_exit_code": raw_result["exit_code"],
            "codex_found": raw_result["found"],
            "codex_validation_error": raw_result["validation_error"],
        },
        persisted=True,
        selection=selection,
    )
