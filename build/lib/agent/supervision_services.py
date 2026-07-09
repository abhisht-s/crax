from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.chatgpt_ax_destination_snapshot import ChatGPTAXDestinationSnapshotAdapter
from agent.chatgpt_destination_gate import (
    DESTINATION_VERIFIED_EXACT,
    DestinationEvidenceSummary,
    DestinationGateResult,
    DestinationLeaseContext,
    destination_gate_failure,
)
from agent.chatgpt_ax_capture import DEFAULT_STABLE_SECONDS
from agent.chatgpt_services import (
    capture_chatgpt_response_service,
    extract_next_codex_prompt_service,
    submit_feedback_to_chatgpt_service,
)
from agent.extracted_prompt_services import execute_extracted_codex_prompt_service
from agent.run_services import (
    acquire_chatgpt_ui_lease,
    release_chatgpt_ui_lease,
    verify_chatgpt_destination_for_run,
)
from agent.run_state import RunStatus
from agent.supervise import SuperviseAction, SupervisePlan, detect_next_supervise_action


ApprovalDecision = str | None
CHATGPT_UI_LEASE_HANDOFF_REASON = "automatic_handoff"
CHATGPT_UI_LEASE_HANDOFF_SOURCE = "supervision_handoff"
CHATGPT_UI_LEASE_RELEASE_FAILURE_EVENT_TYPE = "chatgpt_ui_lease_release_failed"
CHATGPT_UI_LEASE_RELEASE_FAILURE_MESSAGE = (
    "ChatGPT Desktop UI lease release failed after handoff."
)
CHATGPT_DESTINATION_GATE_BLOCKED_EVENT_TYPE = "chatgpt_destination_gate_blocked_handoff"
CHATGPT_DESTINATION_GATE_BLOCKED_MESSAGE = (
    "Automatic ChatGPT handoff blocked by exact destination verification gate."
)
CHATGPT_DESTINATION_GATE_BLOCKED_STATUS_ERROR = (
    "Automatic ChatGPT handoff blocked by exact destination verification gate; "
    "manual review is required before retry."
)


@dataclass(frozen=True)
class SupervisionStepResult:
    ok: bool
    run_id: str
    planner_action: str | None = None
    planner_reason_code: str | None = None
    planner_metadata: dict[str, Any] = field(default_factory=dict)
    action_executed: bool = False
    action_result: Any = None
    next_state_hint: str | None = None
    requires_human_approval: bool = False
    approval_kind: str | None = None
    human_review_required: bool = False
    terminal: bool = False
    completed: bool = False
    blocked: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    events_written: list[dict[str, Any]] = field(default_factory=list)
    run_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _event_id_from_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _latest_event_id(events: list[dict], event_type: str) -> int:
    latest_id = -1
    for event in events:
        if event.get("event_type") != event_type:
            continue
        latest_id = max(latest_id, _event_id_from_value(event.get("id")))
    return latest_id


def _event_metadata(event: dict) -> dict:
    import json

    metadata_json = event.get("metadata_json")
    if not metadata_json:
        return {}
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _latest_matching_workspace_write_post_run_policy(events: list[dict], codex_event_id: int) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "workspace_write_post_run_policy":
            continue
        metadata = _event_metadata(event)
        if _event_id_from_value(metadata.get("codex_exec_finished_event_id")) == codex_event_id:
            return metadata
    return None


def send_plan_auto_safe(plan: object, events: list[dict]) -> tuple[bool, str]:
    sandbox = getattr(plan, "sandbox", "")
    if sandbox == "workspace-write":
        codex_event_id = _event_id_from_value(getattr(plan, "event_ids", {}).get("codex_exec_finished"))
        post_run_policy = _latest_matching_workspace_write_post_run_policy(events, codex_event_id)
        policy = post_run_policy.get("post_run_policy") if isinstance(post_run_policy, dict) else None
        if isinstance(policy, dict) and policy.get("allowed") is True:
            return True, "workspace_write_post_run_verified"
        return False, "workspace_write_post_run_verification_missing_or_failed"
    if sandbox != "read-only":
        return False, "auto_submit_requires_read_only_sandbox"
    changed_files_count = getattr(plan, "changed_files_count", None)
    if changed_files_count is None:
        return False, "changed_files_unknown"
    if changed_files_count > 0:
        return False, "codex_result_changed_files"
    return True, "routine_clean_read_only_result"


def record_supervise_auto_stop(
    run_id: str,
    plan: object,
    reason: str | None = None,
    *,
    ledger: Any = default_ledger,
) -> dict[str, Any]:
    metadata = {
        "run_id": run_id,
        "approval_mode": "auto",
        "automatic_stop_reason": reason or getattr(plan, "reason", ""),
        "plan_reason": getattr(plan, "reason", ""),
        "stop_message": getattr(plan, "stop_message", ""),
        "action": str(getattr(plan, "action", "")),
        "event_ids": getattr(plan, "event_ids", {}),
        "prompt_sha256": getattr(plan, "prompt_sha", ""),
        "prompt_auto_run_safe": bool(getattr(plan, "prompt_auto_run_safe", False)),
        "prompt_auto_run_reason": getattr(plan, "prompt_auto_run_reason", ""),
    }
    event_id = ledger.add_event(
        run_id,
        "supervise_auto_stopped",
        "Automatic supervise stopped at a mandatory human gate.",
        metadata,
    )
    return {
        "event_type": "supervise_auto_stopped",
        "event_id": event_id if isinstance(event_id, int) else None,
        "message": "Automatic supervise stopped at a mandatory human gate.",
        "metadata": metadata,
    }


def _default_submit_service(
    run_id: str,
    run: dict,
    app_name: str,
    *,
    approval_mode: str,
    ledger: Any,
) -> Any:
    return submit_feedback_to_chatgpt_service(
        run_id,
        run,
        app_name=app_name,
        approval_mode=approval_mode,
        ledger=ledger,
    )


def _default_capture_service(
    run_id: str,
    run: dict,
    app_name: str,
    timeout_seconds: float | None,
    stable_seconds: float,
    *,
    require_sentinel_response: bool,
    ledger: Any,
) -> Any:
    del run, timeout_seconds
    return capture_chatgpt_response_service(
        run_id,
        app_name=app_name,
        timeout_seconds=None,
        stable_seconds=stable_seconds,
        require_sentinel_response=require_sentinel_response,
        ledger=ledger,
    )


def _default_extraction_service(
    run_id: str,
    *,
    require_sentinel: bool,
    confirm_extract: bool,
    ledger: Any,
) -> Any:
    return extract_next_codex_prompt_service(
        run_id,
        require_sentinel=require_sentinel,
        confirm_extract=confirm_extract,
        ledger=ledger,
    )


def _default_run_prompt_service(
    run_id: str,
    run: dict,
    repo_path_text: str,
    sandbox: str,
    timeout: float | None,
    *,
    expected_extraction_event_id: int | None,
    expected_prompt_sha256: str | None,
    expected_prompt_text: str | None,
    expected_extraction_method: str | None,
    approval_mode: str,
    pre_run_policy: dict | None,
    expected_scope: dict | None,
    ledger: Any,
) -> Any:
    return execute_extracted_codex_prompt_service(
        run_id,
        run,
        repo_path_text,
        sandbox,
        timeout,
        confirm_full_access=False,
        allow_full_access=False,
        approval_mode=approval_mode,
        expected_extraction_event_id=expected_extraction_event_id,
        expected_prompt_sha256=expected_prompt_sha256,
        expected_prompt_text=expected_prompt_text,
        expected_extraction_method=expected_extraction_method,
        workspace_write_pre_run_policy=pre_run_policy,
        expected_scope=expected_scope,
        ledger=ledger,
    )


def _default_destination_adapter_factory() -> Any:
    return ChatGPTAXDestinationSnapshotAdapter()


def _default_destination_gate_service(
    run_id: str,
    lease_context: DestinationLeaseContext | None,
    *,
    adapter_factory: Callable[[], Any],
    ledger: Any,
) -> DestinationGateResult:
    return verify_chatgpt_destination_for_run(
        run_id,
        lease_context,
        adapter_factory=adapter_factory,
        ledger=ledger,
    )


def _normalize_approval_mode(approval_mode: str) -> str:
    if approval_mode in {"human", "interactive"}:
        return "human"
    return "auto"


def _validate_approval_decision(approval_decision: ApprovalDecision) -> str | None:
    if approval_decision is None:
        return None
    if approval_decision in {"approved", "rejected"}:
        return approval_decision
    return ""


def _plan_metadata(plan: SupervisePlan) -> dict[str, Any]:
    return asdict(plan)


def _base_metadata(plan: SupervisePlan, run: dict | None, events: list[dict]) -> dict[str, Any]:
    return {
        "plan": plan,
        "run": run,
        "event_count": len(events),
        "event_ids": getattr(plan, "event_ids", {}),
    }


def _result_ok(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result == 0
    ok = getattr(result, "ok", None)
    if ok is not None:
        return bool(ok)
    exit_code = getattr(result, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code == 0
    return bool(result)


def _result_reason_code(result: Any, default: str | None = None) -> str | None:
    reason = getattr(result, "reason_code", None)
    if reason:
        return str(reason)
    if isinstance(result, bool):
        return None if result else default
    if isinstance(result, int):
        return None if result == 0 else default
    return default


def _result_error_message(result: Any) -> str | None:
    error = getattr(result, "error_message", None)
    if error:
        return str(error)
    return None


def _result_events_written(result: Any) -> list[dict[str, Any]]:
    events = getattr(result, "events_written", None)
    if isinstance(events, list):
        return events
    event_type = getattr(result, "event_type", None)
    if event_type:
        return [
            {
                "event_type": event_type,
                "event_id": getattr(result, "event_id", None),
                "metadata": getattr(result, "metadata", None),
            }
        ]
    return []


def _event_from_service_result(result: Any) -> dict[str, Any] | None:
    event_type = getattr(result, "event_type", None)
    if not event_type:
        return None
    return {
        "event_type": event_type,
        "event_id": getattr(result, "event_id", None),
        "metadata": getattr(result, "metadata", None),
    }


def _release_failure_event(
    run_id: str,
    release_result: Any,
    *,
    ledger: Any,
) -> dict[str, Any]:
    metadata = {
        "run_id": run_id,
        "reason_code": _result_reason_code(
            release_result,
            "chatgpt_ui_lease_release_failed",
        ),
        "error": _result_error_message(release_result),
        "lease_event_type": getattr(release_result, "event_type", None),
        "lease_event_written": bool(getattr(release_result, "event_written", False)),
        "lease_event_ids": list(getattr(release_result, "event_ids", ()) or ()),
        "exclusive_ui_owner_only": True,
        "destination_verified": False,
    }
    event_id = ledger.add_event(
        run_id,
        CHATGPT_UI_LEASE_RELEASE_FAILURE_EVENT_TYPE,
        CHATGPT_UI_LEASE_RELEASE_FAILURE_MESSAGE,
        metadata,
    )
    return {
        "event_type": CHATGPT_UI_LEASE_RELEASE_FAILURE_EVENT_TYPE,
        "event_id": event_id if isinstance(event_id, int) else None,
        "metadata": metadata,
    }


def _release_exception_event(
    run_id: str,
    exc: Exception,
    *,
    ledger: Any,
) -> dict[str, Any]:
    metadata = {
        "run_id": run_id,
        "reason_code": "chatgpt_ui_lease_release_failed",
        "error": str(exc),
        "exclusive_ui_owner_only": True,
        "destination_verified": False,
    }
    event_id = ledger.add_event(
        run_id,
        CHATGPT_UI_LEASE_RELEASE_FAILURE_EVENT_TYPE,
        CHATGPT_UI_LEASE_RELEASE_FAILURE_MESSAGE,
        metadata,
    )
    return {
        "event_type": CHATGPT_UI_LEASE_RELEASE_FAILURE_EVENT_TYPE,
        "event_id": event_id if isinstance(event_id, int) else None,
        "metadata": metadata,
    }


def _append_release_metadata(
    result: SupervisionStepResult,
    release_result: Any,
    release_event: dict[str, Any] | None,
) -> SupervisionStepResult:
    lease_metadata = {
        "release_ok": _result_ok(release_result),
        "release_reason_code": _result_reason_code(release_result),
        "release_error_message": _result_error_message(release_result),
        "release_event_type": getattr(release_result, "event_type", None),
        "release_event_id": getattr(release_result, "event_id", None),
        "exclusive_ui_owner_only": True,
        "destination_verified": False,
    }
    events_written = list(result.events_written)
    if release_event is not None:
        events_written.append(release_event)
    return replace(
        result,
        events_written=events_written,
        metadata={
            **result.metadata,
            "chatgpt_ui_lease": {
                **result.metadata.get("chatgpt_ui_lease", {}),
                **lease_metadata,
            },
        },
    )


def _lease_context_from_acquire_result(
    run_id: str,
    acquire_result: Any,
) -> DestinationLeaseContext | None:
    lease_token = getattr(acquire_result, "lease_token", None)
    owner = getattr(acquire_result, "owner", None)
    if not isinstance(lease_token, str) or lease_token.strip() == "" or owner is None:
        return None
    try:
        return DestinationLeaseContext(
            owning_run_id=getattr(owner, "owning_run_id"),
            lease_token=lease_token,
            owner_pid=getattr(owner, "owner_pid"),
            acquired_at=getattr(owner, "acquired_at"),
        )
    except (TypeError, ValueError):
        return None


def _evidence_summary_metadata(summary: Any) -> dict[str, Any]:
    if isinstance(summary, DestinationEvidenceSummary):
        return asdict(summary)
    if hasattr(summary, "__dataclass_fields__"):
        try:
            return asdict(summary)
        except (TypeError, ValueError):
            return {}
    return {}


def _destination_gate_metadata(
    gate_result: DestinationGateResult,
    acquire_result: Any,
) -> dict[str, Any]:
    return {
        "run_id": gate_result.run_id,
        "binding_project_title": gate_result.binding_project_title,
        "binding_chat_title": gate_result.binding_chat_title,
        "gate_state": gate_result.state,
        "gate_reason_code": gate_result.reason_code,
        "evidence_summary": _evidence_summary_metadata(gate_result.evidence_summary),
        "feedback_ui_handoff_skipped": True,
        "lease_correlation": {
            "acquire_event_id": getattr(acquire_result, "event_id", None),
            "lease_event_type": getattr(acquire_result, "event_type", None),
            "lease_owning_run_id": gate_result.lease_owning_run_id,
            "lease_owner_pid": gate_result.lease_owner_pid,
            "lease_token_present": gate_result.lease_token_present,
            "lease_context_matches_run": gate_result.lease_context_matches_run,
        },
    }


def _record_destination_gate_blocked_event(
    gate_result: DestinationGateResult,
    acquire_result: Any,
    *,
    ledger: Any,
) -> dict[str, Any]:
    metadata = _destination_gate_metadata(gate_result, acquire_result)
    event_id = ledger.add_event(
        gate_result.run_id,
        CHATGPT_DESTINATION_GATE_BLOCKED_EVENT_TYPE,
        CHATGPT_DESTINATION_GATE_BLOCKED_MESSAGE,
        metadata,
    )
    return {
        "event_type": CHATGPT_DESTINATION_GATE_BLOCKED_EVENT_TYPE,
        "event_id": event_id if isinstance(event_id, int) else None,
        "metadata": metadata,
    }


def _mark_destination_gate_handoff_blocked(
    run_id: str,
    run: dict | None,
    *,
    ledger: Any,
) -> str | None:
    if run is None or not hasattr(ledger, "update_run_status"):
        return None
    try:
        ledger.update_run_status(
            run_id,
            RunStatus.NEEDS_REVIEW,
            final_summary=run.get("final_summary"),
            error=CHATGPT_DESTINATION_GATE_BLOCKED_STATUS_ERROR,
        )
    except Exception:
        return None
    return RunStatus.NEEDS_REVIEW.value


def _destination_gate_blocked_result(
    *,
    gate_result: DestinationGateResult,
    acquire_result: Any,
    run_id: str,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    events_written: list[dict[str, Any]],
) -> SupervisionStepResult:
    gate_metadata = _destination_gate_metadata(gate_result, acquire_result)
    return SupervisionStepResult(
        ok=False,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=False,
        next_state_hint="blocked",
        blocked=True,
        reason_code=gate_result.reason_code or "destination_verification_unavailable",
        error_message=gate_result.reason_code or "destination_verification_unavailable",
        events_written=events_written,
        run_status=status,
        metadata={
            **metadata,
            "chatgpt_destination_gate": {
                **gate_metadata,
                "feedback_ui_handoff_skipped": True,
            },
            "chatgpt_ui_lease": {
                "acquire_ok": True,
                "event_type": getattr(acquire_result, "event_type", None),
                "event_id": getattr(acquire_result, "event_id", None),
                "exclusive_ui_owner_only": True,
                "destination_verified": False,
            },
        },
    )


def _chatgpt_lease_denied_result(
    *,
    run_id: str,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    acquire_result: Any,
) -> SupervisionStepResult:
    reason_code = _result_reason_code(
        acquire_result,
        "chatgpt_ui_lease_already_held",
    )
    return SupervisionStepResult(
        ok=False,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=False,
        next_state_hint="blocked",
        blocked=True,
        reason_code=reason_code,
        error_message=_result_error_message(acquire_result) or reason_code,
        events_written=_result_events_written(acquire_result),
        run_status=status,
        metadata={
            **metadata,
            "chatgpt_ui_lease": {
                "acquire_ok": False,
                "reason_code": reason_code,
                "event_type": getattr(acquire_result, "event_type", None),
                "event_id": getattr(acquire_result, "event_id", None),
                "exclusive_ui_owner_only": True,
                "destination_verified": False,
            },
        },
    )


def _stop_completed(reason: str) -> bool:
    return reason in {"extracted_prompt_already_run"}


def _human_review_required(reason: str, status: str | None) -> bool:
    return reason in {
        "approval_required",
        "needs_review",
        "waiting_for_approval",
    } or status in {"needs_review", "waiting_for_approval"}


def _approval_mismatch(
    plan: SupervisePlan,
    *,
    expected_planner_action: str | None,
    expected_event_ids: dict[str, int] | None,
    expected_prompt_sha256: str | None,
) -> str | None:
    if expected_planner_action is not None and str(plan.action) != expected_planner_action:
        return "planner_action_changed_after_approval"
    if expected_event_ids:
        for key, value in expected_event_ids.items():
            if _event_id_from_value(plan.event_ids.get(key)) != _event_id_from_value(value):
                return "planner_event_ids_changed_after_approval"
    if expected_prompt_sha256 is not None and getattr(plan, "prompt_sha", "") != expected_prompt_sha256:
        return "planner_prompt_changed_after_approval"
    return None


def _chatgpt_step_result(
    *,
    ok: bool,
    run_id: str,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    result: Any,
    next_state_hint: str,
    default_reason_code: str,
    events_written: list[dict[str, Any]],
) -> SupervisionStepResult:
    return SupervisionStepResult(
        ok=ok,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=True,
        action_result=result,
        next_state_hint=next_state_hint if ok else "blocked",
        blocked=not ok,
        reason_code=_result_reason_code(result, default_reason_code),
        error_message=_result_error_message(result),
        events_written=events_written,
        run_status=status,
        metadata=metadata,
    )


def _run_chatgpt_handoff_transaction(
    *,
    run_id: str,
    run: dict | None,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    app_name: str,
    mode: str,
    capture_timeout_seconds: float | None,
    capture_stable_seconds: float,
    ledger: Any,
    submit_service: Callable[..., Any],
    capture_service: Callable[..., Any],
    extraction_service: Callable[..., Any],
    destination_gate_service: Callable[..., Any],
    destination_adapter_factory: Callable[[], Any],
    before_action_callback: Callable[[SupervisePlan, dict | None, list[dict]], None] | None,
    events: list[dict],
) -> SupervisionStepResult:
    acquire_result = acquire_chatgpt_ui_lease(
        run_id,
        reason=CHATGPT_UI_LEASE_HANDOFF_REASON,
        source=CHATGPT_UI_LEASE_HANDOFF_SOURCE,
        ledger=ledger,
    )
    if not _result_ok(acquire_result):
        return _chatgpt_lease_denied_result(
            run_id=run_id,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            acquire_result=acquire_result,
        )

    lease_token = getattr(acquire_result, "lease_token", None)
    if not isinstance(lease_token, str) or lease_token.strip() == "":
        return _chatgpt_lease_denied_result(
            run_id=run_id,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            acquire_result=acquire_result,
        )

    final_result: SupervisionStepResult | None = None
    release_event: dict[str, Any] | None = None
    release_result: Any = None
    original_exc: BaseException | None = None
    try:
        events_written = _result_events_written(acquire_result)
        lease_context = _lease_context_from_acquire_result(run_id, acquire_result)
        try:
            gate_result = destination_gate_service(
                run_id,
                lease_context,
                adapter_factory=destination_adapter_factory,
                ledger=ledger,
            )
        except Exception:
            gate_result = destination_gate_failure(
                run_id=run_id,
                reason_code="destination_verification_unavailable",
                binding=None,
                lease_context=lease_context,
            )

        if not _result_ok(gate_result) or getattr(gate_result, "state", None) != DESTINATION_VERIFIED_EXACT:
            gate_event = _record_destination_gate_blocked_event(
                gate_result,
                acquire_result,
                ledger=ledger,
            )
            events_written.append(gate_event)
            blocked_status = _mark_destination_gate_handoff_blocked(
                run_id,
                run,
                ledger=ledger,
            )
            final_result = _destination_gate_blocked_result(
                gate_result=gate_result,
                acquire_result=acquire_result,
                run_id=run_id,
                plan=plan,
                action_value=action_value,
                plan_metadata=plan_metadata,
                status=blocked_status or status,
                metadata=metadata,
                events_written=events_written,
            )
        elif before_action_callback is not None:
            before_action_callback(plan, run, events)

        current_result: Any = None
        current_stage = str(plan.action)

        if final_result is None and plan.action == SuperviseAction.ASK_SEND_TO_GPT:
            current_result = submit_service(
                run_id,
                run,
                app_name,
                approval_mode=mode,
                ledger=ledger,
            )
            event = _event_from_service_result(current_result)
            if event is not None:
                events_written.append(event)
            if not _result_ok(current_result):
                final_result = _chatgpt_step_result(
                    ok=False,
                    run_id=run_id,
                    plan=plan,
                    action_value=action_value,
                    plan_metadata=plan_metadata,
                    status=status,
                    metadata=metadata,
                    result=current_result,
                    next_state_hint="capture_gpt_response",
                    default_reason_code="submit_feedback_failed",
                    events_written=events_written,
                )
            else:
                current_stage = str(SuperviseAction.CAPTURE_GPT_RESPONSE)

        if final_result is None and current_stage == str(SuperviseAction.CAPTURE_GPT_RESPONSE):
            current_result = capture_service(
                run_id,
                run,
                app_name,
                capture_timeout_seconds,
                capture_stable_seconds,
                require_sentinel_response=True,
                ledger=ledger,
            )
            event = _event_from_service_result(current_result)
            if event is not None:
                events_written.append(event)
            if not _result_ok(current_result):
                final_result = _chatgpt_step_result(
                    ok=False,
                    run_id=run_id,
                    plan=plan,
                    action_value=action_value,
                    plan_metadata=plan_metadata,
                    status=status,
                    metadata=metadata,
                    result=current_result,
                    next_state_hint="extract_next_prompt",
                    default_reason_code="capture_gpt_response_failed",
                    events_written=events_written,
                )
            else:
                current_stage = str(SuperviseAction.EXTRACT_NEXT_PROMPT)

        if final_result is None and current_stage == str(SuperviseAction.EXTRACT_NEXT_PROMPT):
            current_result = extraction_service(
                run_id,
                require_sentinel=True,
                confirm_extract=True,
                ledger=ledger,
            )
            event = _event_from_service_result(current_result)
            if event is not None:
                events_written.append(event)
            ok = _result_ok(current_result)
            final_result = _chatgpt_step_result(
                ok=ok,
                run_id=run_id,
                plan=plan,
                action_value=action_value,
                plan_metadata=plan_metadata,
                status=status,
                metadata=metadata,
                result=current_result,
                next_state_hint="ask_run_prompt",
                default_reason_code="extract_next_prompt_failed",
                events_written=events_written,
            )

        if final_result is None:
            final_result = SupervisionStepResult(
                ok=False,
                run_id=run_id,
                planner_action=action_value,
                planner_reason_code=plan.reason,
                planner_metadata=plan_metadata,
                action_executed=False,
                terminal=True,
                blocked=True,
                reason_code="unknown_chatgpt_handoff_action",
                error_message=f"Unknown ChatGPT handoff action: {plan.action}",
                run_status=status,
                metadata=metadata,
            )
    except BaseException as exc:
        original_exc = exc
        raise
    finally:
        try:
            release_result = release_chatgpt_ui_lease(
                lease_token,
                reason=CHATGPT_UI_LEASE_HANDOFF_REASON,
                source=CHATGPT_UI_LEASE_HANDOFF_SOURCE,
                ledger=ledger,
            )
            if not _result_ok(release_result):
                try:
                    release_event = _release_failure_event(
                        run_id,
                        release_result,
                        ledger=ledger,
                    )
                except Exception:
                    release_event = None
        except Exception as release_exc:
            if original_exc is not None:
                try:
                    _release_exception_event(run_id, release_exc, ledger=ledger)
                except Exception:
                    pass
            else:
                try:
                    release_result = type(
                        "_ReleaseExceptionResult",
                        (),
                        {
                            "ok": False,
                            "reason_code": "chatgpt_ui_lease_release_failed",
                            "error_message": str(release_exc),
                            "event_type": None,
                            "event_id": None,
                        },
                    )()
                    release_event = _release_exception_event(
                        run_id,
                        release_exc,
                        ledger=ledger,
                    )
                except Exception:
                    release_event = None

    if final_result is None:
        raise RuntimeError("ChatGPT handoff transaction produced no result")
    if release_result is not None:
        final_result = _append_release_metadata(
            final_result,
            release_result,
            release_event,
        )
    return final_result


def run_supervision_step(
    run_id: str,
    repo_path_text: str,
    sandbox: str,
    approval_mode: str = "auto",
    *,
    approval_decision: ApprovalDecision = None,
    expected_planner_action: str | None = None,
    expected_event_ids: dict[str, int] | None = None,
    expected_prompt_sha256: str | None = None,
    app_name: str = "ChatGPT",
    timeout: float | None = None,
    capture_timeout_seconds: float | None = None,
    capture_stable_seconds: float = DEFAULT_STABLE_SECONDS,
    ledger: Any = default_ledger,
    planner: Callable[..., SupervisePlan] = detect_next_supervise_action,
    submit_service: Callable[..., Any] = _default_submit_service,
    capture_service: Callable[..., Any] = _default_capture_service,
    extraction_service: Callable[..., Any] = _default_extraction_service,
    extracted_prompt_execution_service: Callable[..., Any] = _default_run_prompt_service,
    destination_gate_service: Callable[..., Any] = _default_destination_gate_service,
    destination_adapter_factory: Callable[[], Any] = _default_destination_adapter_factory,
    send_auto_safety_evaluator: Callable[[object, list[dict]], tuple[bool, str]] = send_plan_auto_safe,
    auto_stop_recorder: Callable[..., dict[str, Any]] = record_supervise_auto_stop,
    before_action_callback: Callable[[SupervisePlan, dict | None, list[dict]], None] | None = None,
) -> SupervisionStepResult:
    normalized_decision = _validate_approval_decision(approval_decision)
    if normalized_decision == "":
        return SupervisionStepResult(
            ok=False,
            run_id=run_id,
            action_executed=False,
            reason_code="invalid_approval_decision",
            error_message="Invalid approval decision. Expected approved, rejected, or None.",
            blocked=True,
        )

    run = ledger.get_run(run_id)
    events = ledger.list_events(run_id) if run is not None else []
    plan = planner(run, events, repo_path_text, sandbox=sandbox)
    plan_metadata = _plan_metadata(plan)
    metadata = _base_metadata(plan, run, events)
    mode = _normalize_approval_mode(approval_mode)
    action = plan.action
    action_value = str(action)
    status = getattr(plan, "status", None) or (str(run.get("status")) if isinstance(run, dict) else None)

    mismatch = _approval_mismatch(
        plan,
        expected_planner_action=expected_planner_action,
        expected_event_ids=expected_event_ids,
        expected_prompt_sha256=expected_prompt_sha256,
    )
    if mismatch is not None:
        return SupervisionStepResult(
            ok=False,
            run_id=run_id,
            planner_action=action_value,
            planner_reason_code=plan.reason,
            planner_metadata=plan_metadata,
            action_executed=False,
            reason_code=mismatch,
            error_message="Planner state changed after approval; no action was executed.",
            blocked=True,
            run_status=status,
            metadata=metadata,
        )

    if action == SuperviseAction.STOP:
        events_written: list[dict[str, Any]] = []
        if mode == "auto":
            events_written.append(auto_stop_recorder(run_id, plan, ledger=ledger))
        completed = _stop_completed(plan.reason)
        return SupervisionStepResult(
            ok=completed,
            run_id=run_id,
            planner_action=action_value,
            planner_reason_code=plan.reason,
            planner_metadata=plan_metadata,
            action_executed=False,
            next_state_hint="terminal",
            human_review_required=_human_review_required(plan.reason, status),
            terminal=True,
            completed=completed,
            blocked=not completed,
            reason_code=plan.reason,
            error_message=plan.stop_message or plan.reason,
            events_written=events_written,
            run_status=status,
            metadata=metadata,
        )

    if action == SuperviseAction.ASK_SEND_TO_GPT:
        if mode == "human":
            if normalized_decision is None:
                if before_action_callback is not None:
                    before_action_callback(plan, run, events)
                return SupervisionStepResult(
                    ok=True,
                    run_id=run_id,
                    planner_action=action_value,
                    planner_reason_code=plan.reason,
                    planner_metadata=plan_metadata,
                    action_executed=False,
                    next_state_hint="waiting_for_human_approval",
                    requires_human_approval=True,
                    approval_kind="send_to_gpt",
                    reason_code="human_approval_required",
                    run_status=status,
                    metadata=metadata,
                )
            if normalized_decision == "rejected":
                return SupervisionStepResult(
                    ok=True,
                    run_id=run_id,
                    planner_action=action_value,
                    planner_reason_code=plan.reason,
                    planner_metadata=plan_metadata,
                    action_executed=False,
                    next_state_hint="declined",
                    reason_code="human_declined",
                    run_status=status,
                    metadata=metadata,
                )
        else:
            if before_action_callback is not None:
                before_action_callback(plan, run, events)
            auto_send_safe, auto_send_reason = send_auto_safety_evaluator(plan, events)
            if not auto_send_safe:
                event = auto_stop_recorder(run_id, plan, auto_send_reason, ledger=ledger)
                return SupervisionStepResult(
                    ok=False,
                    run_id=run_id,
                    planner_action=action_value,
                    planner_reason_code=plan.reason,
                    planner_metadata=plan_metadata,
                    action_executed=False,
                    next_state_hint="blocked",
                    blocked=True,
                    reason_code=auto_send_reason,
                    error_message="Codex result requires human approval before ChatGPT submission.",
                    events_written=[event],
                    run_status=status,
                    metadata={**metadata, "auto_send_safe": False, "auto_send_reason": auto_send_reason},
                )
        return _run_chatgpt_handoff_transaction(
            run_id=run_id,
            run=run,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            app_name=app_name,
            mode=mode,
            capture_timeout_seconds=capture_timeout_seconds,
            capture_stable_seconds=capture_stable_seconds,
            ledger=ledger,
            submit_service=submit_service,
            capture_service=capture_service,
            extraction_service=extraction_service,
            destination_gate_service=destination_gate_service,
            destination_adapter_factory=destination_adapter_factory,
            before_action_callback=before_action_callback,
            events=events,
        )

    if action == SuperviseAction.CAPTURE_GPT_RESPONSE:
        return _run_chatgpt_handoff_transaction(
            run_id=run_id,
            run=run,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            app_name=app_name,
            mode=mode,
            capture_timeout_seconds=capture_timeout_seconds,
            capture_stable_seconds=capture_stable_seconds,
            ledger=ledger,
            submit_service=submit_service,
            capture_service=capture_service,
            extraction_service=extraction_service,
            destination_gate_service=destination_gate_service,
            destination_adapter_factory=destination_adapter_factory,
            before_action_callback=before_action_callback,
            events=events,
        )

    if action == SuperviseAction.EXTRACT_NEXT_PROMPT:
        return _run_chatgpt_handoff_transaction(
            run_id=run_id,
            run=run,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            app_name=app_name,
            mode=mode,
            capture_timeout_seconds=capture_timeout_seconds,
            capture_stable_seconds=capture_stable_seconds,
            ledger=ledger,
            submit_service=submit_service,
            capture_service=capture_service,
            extraction_service=extraction_service,
            destination_gate_service=destination_gate_service,
            destination_adapter_factory=destination_adapter_factory,
            before_action_callback=before_action_callback,
            events=events,
        )

    if action == SuperviseAction.ASK_RUN_PROMPT:
        approved_codex_event_id = _event_id_from_value(plan.event_ids.get("codex_exec_finished"))
        if approved_codex_event_id < 0:
            approved_codex_event_id = _latest_event_id(events, "codex_exec_finished")
        run_metadata = {**metadata, "approved_codex_event_id": approved_codex_event_id}
        if mode == "human":
            if normalized_decision is None:
                if before_action_callback is not None:
                    before_action_callback(plan, run, events)
                return SupervisionStepResult(
                    ok=True,
                    run_id=run_id,
                    planner_action=action_value,
                    planner_reason_code=plan.reason,
                    planner_metadata=plan_metadata,
                    action_executed=False,
                    next_state_hint="waiting_for_human_approval",
                    requires_human_approval=True,
                    approval_kind="run_prompt",
                    reason_code="human_approval_required",
                    run_status=status,
                    metadata=run_metadata,
                )
            if normalized_decision == "rejected":
                return SupervisionStepResult(
                    ok=True,
                    run_id=run_id,
                    planner_action=action_value,
                    planner_reason_code=plan.reason,
                    planner_metadata=plan_metadata,
                    action_executed=False,
                    next_state_hint="declined",
                    reason_code="human_declined",
                    run_status=status,
                    metadata=run_metadata,
                )
        else:
            if before_action_callback is not None:
                before_action_callback(plan, run, events)
            if not bool(getattr(plan, "prompt_auto_run_safe", False)):
                reason = getattr(plan, "prompt_auto_run_reason", "") or "prompt_not_routine_safe"
                event = auto_stop_recorder(run_id, plan, reason, ledger=ledger)
                return SupervisionStepResult(
                    ok=False,
                    run_id=run_id,
                    planner_action=action_value,
                    planner_reason_code=plan.reason,
                    planner_metadata=plan_metadata,
                    action_executed=False,
                    next_state_hint="blocked",
                    blocked=True,
                    reason_code=reason,
                    error_message="Extracted prompt requires human approval before Codex execution.",
                    events_written=[event],
                    run_status=status,
                    metadata={**run_metadata, "prompt_auto_run_safe": False, "prompt_auto_run_reason": reason},
                )
        result = extracted_prompt_execution_service(
            run_id,
            run,
            repo_path_text,
            sandbox,
            timeout,
            expected_extraction_event_id=plan.event_ids.get("next_codex_prompt_extracted"),
            expected_prompt_sha256=plan.prompt_sha,
            expected_prompt_text=plan.prompt_text,
            expected_extraction_method=plan.extraction_method,
            approval_mode=mode,
            pre_run_policy=getattr(plan, "pre_run_policy", {}),
            expected_scope=getattr(plan, "expected_scope", {}),
            ledger=ledger,
        )
        ok = _result_ok(result)
        return SupervisionStepResult(
            ok=ok,
            run_id=run_id,
            planner_action=action_value,
            planner_reason_code=plan.reason,
            planner_metadata=plan_metadata,
            action_executed=True,
            action_result=result,
            next_state_hint="ask_send_to_gpt" if ok else "blocked",
            blocked=not ok,
            reason_code=_result_reason_code(result, "extracted_codex_prompt_run_failed"),
            error_message=_result_error_message(result),
            events_written=_result_events_written(result),
            run_status=status,
            metadata=run_metadata,
        )

    return SupervisionStepResult(
        ok=False,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=False,
        terminal=True,
        blocked=True,
        reason_code="unknown_action",
        error_message=f"Unknown supervise action: {plan.action}",
        run_status=status,
        metadata=metadata,
    )
