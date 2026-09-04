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
from agent.chatgpt_desktop_mutex import (
    ChatGPTDesktopMutex,
    ChatGPTDesktopMutexHold,
    handoff_claim_owner_identifier,
)
from agent.chatgpt_services import (
    capture_chatgpt_response_service,
    extract_next_codex_prompt_service,
    submit_feedback_to_chatgpt_service,
)
from agent.extracted_prompt_services import execute_extracted_codex_prompt_service
from agent.run_services import (
    DestinationBindingLookupStatus,
    RunDestinationBinding,
    acquire_chatgpt_ui_lease,
    get_run_destination_binding,
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

# --- Stage 3A: operator-approved navigation-before-gate handoff -------------
#
# Bounded handoff phase vocabulary. This is intentionally a small enum of state
# labels (not a prose trace) so the local read model can surface where the
# most recent handoff transaction stopped. Navigation, when it runs, is a
# separate actor invoked *before* the read-only destination gate; the gate is
# never given navigation behavior.
HANDOFF_PHASE_NAVIGATION_NOT_REQUESTED = "navigation_not_requested"
HANDOFF_PHASE_NAVIGATION_STARTED = "navigation_started"
HANDOFF_PHASE_NAVIGATION_SUCCEEDED = "navigation_succeeded"
HANDOFF_PHASE_NAVIGATION_FAILED = "navigation_failed"
HANDOFF_PHASE_VERIFICATION_STARTED = "verification_started"
HANDOFF_PHASE_VERIFICATION_FAILED = "verification_failed"
HANDOFF_PHASE_VERIFICATION_SUCCEEDED = "verification_succeeded"
HANDOFF_PHASE_SUBMISSION_STARTED = "submission_started"
HANDOFF_PHASE_CAPTURE_STARTED = "capture_started"
HANDOFF_PHASE_CONTINUATION_STARTED = "continuation_started"

HANDOFF_PHASES = frozenset(
    {
        HANDOFF_PHASE_NAVIGATION_NOT_REQUESTED,
        HANDOFF_PHASE_NAVIGATION_STARTED,
        HANDOFF_PHASE_NAVIGATION_SUCCEEDED,
        HANDOFF_PHASE_NAVIGATION_FAILED,
        HANDOFF_PHASE_VERIFICATION_STARTED,
        HANDOFF_PHASE_VERIFICATION_FAILED,
        HANDOFF_PHASE_VERIFICATION_SUCCEEDED,
        HANDOFF_PHASE_SUBMISSION_STARTED,
        HANDOFF_PHASE_CAPTURE_STARTED,
        HANDOFF_PHASE_CONTINUATION_STARTED,
    }
)

CHATGPT_HANDOFF_PHASE_EVENT_TYPE = "chatgpt_handoff_phase"
CHATGPT_HANDOFF_PHASE_MESSAGE = "ChatGPT handoff phase state recorded."
CHATGPT_DESTINATION_NAVIGATION_ATTEMPT_EVENT_TYPE = "chatgpt_destination_navigation_attempt"
CHATGPT_DESTINATION_NAVIGATION_ATTEMPT_MESSAGE = (
    "Operator-approved autonomous ChatGPT destination navigation attempted."
)
CHATGPT_DESTINATION_NAVIGATION_BLOCKED_STATUS_ERROR = (
    "Automatic ChatGPT handoff blocked because operator-approved destination "
    "navigation did not reach the bound destination; manual review is required "
    "before retry."
)
CHATGPT_HANDOFF_MAX_UI_ATTEMPTS = 5
CHATGPT_HANDOFF_QUEUE_ENQUEUE_SOURCE = "supervision_handoff"
CHATGPT_HANDOFF_YIELD_REASON_CODE = "chatgpt_handoff_yielded_retryable_ui_failure"
CHATGPT_HANDOFF_SLICE_COMPLETED_REASON_CODE = "chatgpt_handoff_slice_completed"
CHATGPT_WAIT_REASON_CODES = frozenset(
    {
        "chatgpt_ui_lease_already_held",
        "chatgpt_desktop_mutex_already_held",
        "chatgpt_handoff_queue_not_head",
        "chatgpt_handoff_queue_head_already_claimed",
        CHATGPT_HANDOFF_YIELD_REASON_CODE,
    }
)

# Navigator outcomes that count as a confirmed chat open. Any other outcome is
# treated as fail-closed navigation.
CHAT_OPENED_NAVIGATION_OUTCOMES = frozenset(
    {
        "chat_opened_via_axpress",
        "chat_opened_via_validated_click",
        "chat_opened_after_scrolling_via_axpress",
        "chat_opened_after_scrolling_via_validated_click",
    }
)


@dataclass(frozen=True)
class NavigationAttemptResult:
    # ``ok`` means an Accessibility or validated-click action path reported
    # success against the re-resolved target, so the authoritative read-only
    # destination gate may now run. It does NOT prove a physical UI change or
    # destination open; only the gate decides that. ``navigator_confirmed``
    # records the navigator's own non-authoritative observation.
    ok: bool
    outcome: str
    reason_code: str | None = None
    error_message: str | None = None
    action_posted: bool = False
    navigator_confirmed: bool = False
    navigation_action_diagnostics: dict[str, Any] = field(default_factory=dict)


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
    waiting_for_chatgpt: bool = False
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
    if sandbox == "danger-full-access":
        return True, "danger_full_access_auto_submit"
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
        confirm_full_access=sandbox == "danger-full-access",
        allow_full_access=sandbox == "danger-full-access",
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


def _navigation_attempt_from_open_result(result: dict) -> NavigationAttemptResult:
    """Map the navigator's raw open-result into a handoff navigation outcome.

    The authoritative post-navigation proof is the read-only destination gate,
    not the navigator's own confirmation heuristic (which the audit showed is
    mismatched with ChatGPT Desktop's real accessibility tree). So navigation is
    treated as *performed* — and the gate is allowed to run — whenever the
    Accessibility or validated-click action path reported success against the
    exact re-resolved target (``chat_open_action_posted``), regardless of
    whether the navigator's own secondary heuristic recognized a resulting UI
    change. When the action path could not report success (target not
    resolved/interactable, action failed, click failed, etc.) navigation is a
    genuine failure and the flow stays fail-closed before the gate.
    """

    outcome = str(result.get("outcome") or "") or "navigation_incomplete"
    navigator_confirmed = result.get("ok") is True and outcome in CHAT_OPENED_NAVIGATION_OUTCOMES
    action_posted = result.get("chat_open_action_posted") is True
    diagnostics = _navigation_action_diagnostics_from_open_result(result)
    if action_posted:
        return NavigationAttemptResult(
            ok=True,
            outcome=outcome,
            action_posted=True,
            navigator_confirmed=navigator_confirmed,
            navigation_action_diagnostics=diagnostics,
        )
    return NavigationAttemptResult(
        ok=False,
        outcome=outcome,
        reason_code="destination_navigation_action_not_performed",
        action_posted=False,
        navigator_confirmed=False,
        navigation_action_diagnostics=diagnostics,
    )


_NAVIGATION_ACTION_DIAGNOSTIC_DEFAULTS = {
    "target_detected": False,
    "target_candidate_count": 0,
    "actionable_element_resolved": False,
    "selected_element_role": "",
    "selected_relation": "",
    "available_ax_actions": [],
    "chosen_method": "",
    "axpress_attempted": False,
    "axpress_result": "not_attempted",
    "ax_error_code": None,
    "ui_changed_after_action": False,
    "destination_confirmed": False,
    "final_reresolution_status": "not_attempted",
    "project_open_outcome": "",
    "project_open_target_match_count": 0,
    "project_open_truncated_by_node_limit": False,
    "project_open_truncated_by_depth_limit": False,
    "project_open_stability_status": "",
}


def _navigation_action_diagnostics_from_open_result(result: dict) -> dict[str, Any]:
    """Return bounded structural action evidence without retaining AX text."""

    diagnostics = dict(_NAVIGATION_ACTION_DIAGNOSTIC_DEFAULTS)
    diagnostics["target_detected"] = result.get("target_detected") is True
    diagnostics["target_candidate_count"] = max(0, int(result.get("target_candidate_count") or 0))
    diagnostics["actionable_element_resolved"] = result.get("actionable_element_resolved") is True
    diagnostics["selected_element_role"] = str(result.get("selected_element_role") or "")[:80]
    relation = str(result.get("selected_relation") or "")
    diagnostics["selected_relation"] = relation if relation in {"", "row_node", "title_node"} else ""
    diagnostics["available_ax_actions"] = [
        str(action)[:80]
        for action in (result.get("available_ax_actions") or [])[:12]
        if isinstance(action, str)
    ]
    diagnostics["chosen_method"] = str(result.get("chosen_method") or "")[:80]
    diagnostics["axpress_attempted"] = result.get("axpress_attempted") is True
    diagnostics["axpress_result"] = str(result.get("axpress_result") or "not_attempted")[:40]
    error_code = result.get("ax_error_code")
    diagnostics["ax_error_code"] = error_code if isinstance(error_code, int) else None
    diagnostics["ui_changed_after_action"] = result.get("ui_changed_after_action") is True
    diagnostics["destination_confirmed"] = result.get("destination_confirmed") is True
    diagnostics["final_reresolution_status"] = str(result.get("final_reresolution_status") or "not_attempted")[:80]
    project_result = result.get("project_open_result")
    if not isinstance(project_result, dict):
        project_result = {}
    project_traversal = project_result.get("traversal")
    if not isinstance(project_traversal, dict):
        project_traversal = {}
    diagnostics["project_open_outcome"] = str(
        project_result.get("outcome") or result.get("project_open_outcome") or ""
    )[:80]
    diagnostics["project_open_target_match_count"] = max(
        0,
        int(
            project_result.get("target_match_count")
            or result.get("project_open_target_match_count")
            or 0
        ),
    )
    diagnostics["project_open_truncated_by_node_limit"] = bool(
        project_traversal.get("truncated_by_node_limit")
        or result.get("project_open_truncated_by_node_limit") is True
    )
    diagnostics["project_open_truncated_by_depth_limit"] = bool(
        project_traversal.get("truncated_by_depth_limit")
        or result.get("project_open_truncated_by_depth_limit") is True
    )
    diagnostics["project_open_stability_status"] = str(
        project_result.get("activation_stability_status")
        or result.get("project_open_stability_status")
        or ""
    )[:40]
    return diagnostics


def _default_destination_navigation_service(
    run_id: str,
    binding: RunDestinationBinding,
    lease_context: DestinationLeaseContext | None,
    *,
    app_name: str,
    ledger: Any,
) -> NavigationAttemptResult:
    """Invoke the existing Project/chat navigator for one bound destination.

    This is the only place the autonomous loop reaches live ChatGPT Desktop
    navigation, and it only runs when the caller has explicitly opted into
    operator-approved navigation. It performs at most one navigation attempt.
    Whether the resulting destination is correct is decided solely by the
    downstream read-only gate; this service only reports whether the open action
    was performed against the exact target.
    """

    del run_id, lease_context, ledger
    from agent.chatgpt_navigation_diagnostic import open_chatgpt_project_chat

    try:
        result = open_chatgpt_project_chat(
            project_title=binding.project_title,
            chat_title=binding.chat_title,
            confirm_open_chat=True,
            app_name=app_name,
        )
    except Exception as exc:
        return NavigationAttemptResult(
            ok=False,
            outcome="navigation_actor_exception",
            reason_code="destination_navigation_actor_exception",
            error_message=str(exc),
        )

    return _navigation_attempt_from_open_result(result)


def _resolve_run_destination_binding(
    run_id: str,
    *,
    ledger: Any,
) -> RunDestinationBinding | None:
    lookup = get_run_destination_binding(run_id, ledger=ledger)
    if lookup.status == DestinationBindingLookupStatus.PRESENT and lookup.binding is not None:
        return lookup.binding
    return None


def _navigation_summary(nav_result: NavigationAttemptResult) -> dict[str, Any]:
    return {
        "ok": bool(nav_result.ok),
        "outcome": nav_result.outcome,
        "reason_code": nav_result.reason_code,
        "action_posted": bool(nav_result.action_posted),
        "navigator_confirmed": bool(nav_result.navigator_confirmed),
    }


def _record_navigation_attempt_event(
    run_id: str,
    binding: RunDestinationBinding | None,
    nav_result: NavigationAttemptResult,
    *,
    attempt_number: int | None = None,
    max_attempts: int | None = None,
    ledger: Any,
) -> dict[str, Any]:
    diagnostics = _navigation_action_diagnostics_from_open_result(nav_result.navigation_action_diagnostics)
    metadata = {
        "run_id": run_id,
        "binding_project_title": getattr(binding, "project_title", None),
        "binding_chat_title": getattr(binding, "chat_title", None),
        "operator_approved": True,
        "navigation_ok": bool(nav_result.ok),
        "navigation_outcome": nav_result.outcome,
        "reason_code": nav_result.reason_code,
        "navigation_action_posted": bool(nav_result.action_posted),
        "navigator_confirmed": bool(nav_result.navigator_confirmed),
        "destination_verified": False,
        "navigation_action_diagnostics": diagnostics,
    }
    if attempt_number is not None:
        metadata["attempt_number"] = attempt_number
    if max_attempts is not None:
        metadata["max_attempts"] = max_attempts
    event_id = ledger.add_event(
        run_id,
        CHATGPT_DESTINATION_NAVIGATION_ATTEMPT_EVENT_TYPE,
        CHATGPT_DESTINATION_NAVIGATION_ATTEMPT_MESSAGE,
        metadata,
    )
    return {
        "event_type": CHATGPT_DESTINATION_NAVIGATION_ATTEMPT_EVENT_TYPE,
        "event_id": event_id if isinstance(event_id, int) else None,
        "metadata": metadata,
    }


def _record_handoff_phase_event(
    run_id: str,
    phase: str,
    *,
    navigation_operator_approved: bool,
    navigation_summary: dict[str, Any] | None,
    ledger: Any,
) -> dict[str, Any]:
    metadata = {
        "run_id": run_id,
        "handoff_phase": phase,
        "navigation_operator_approved": bool(navigation_operator_approved),
        "navigation": navigation_summary,
    }
    event_id = ledger.add_event(
        run_id,
        CHATGPT_HANDOFF_PHASE_EVENT_TYPE,
        CHATGPT_HANDOFF_PHASE_MESSAGE,
        metadata,
    )
    return {
        "event_type": CHATGPT_HANDOFF_PHASE_EVENT_TYPE,
        "event_id": event_id if isinstance(event_id, int) else None,
        "metadata": metadata,
    }


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


def _exception_service_result(
    *,
    reason_code: str,
    error_message: str,
) -> Any:
    return type(
        "_ExceptionServiceResult",
        (),
        {
            "ok": False,
            "reason_code": reason_code,
            "error_message": error_message,
            "event_type": None,
            "event_id": None,
            "metadata": {
                "reason_code": reason_code,
                "error": error_message,
            },
        },
    )()


def _chatgpt_submit_failure_retryable(result: Any) -> bool:
    # Payload construction failures are deterministic; cursor/focus retries can
    # only help once there is a valid message ready to transfer.
    return getattr(result, "event_type", None) != "gpt_feedback_generation_failed"


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


def _destination_gate_failure_fingerprint(
    gate_result: DestinationGateResult,
) -> tuple[str, str]:
    return (
        str(gate_result.reason_code or "destination_verification_unavailable"),
        repr(_evidence_summary_metadata(gate_result.evidence_summary)),
    )


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
    navigation_summary: dict[str, Any] | None = None,
    attempt_number: int | None = None,
    max_attempts: int | None = None,
    will_retry: bool = False,
    ledger: Any,
) -> dict[str, Any]:
    metadata = _destination_gate_metadata(gate_result, acquire_result)
    metadata["navigation"] = navigation_summary
    if attempt_number is not None:
        metadata["attempt_number"] = attempt_number
    if max_attempts is not None:
        metadata["max_attempts"] = max_attempts
    metadata["will_retry"] = bool(will_retry)
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
    error: str = CHATGPT_DESTINATION_GATE_BLOCKED_STATUS_ERROR,
    ledger: Any,
) -> str | None:
    if run is None or not hasattr(ledger, "update_run_status"):
        return None
    try:
        ledger.update_run_status(
            run_id,
            RunStatus.NEEDS_REVIEW,
            final_summary=run.get("final_summary"),
            error=error,
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
    navigation_summary: dict[str, Any] | None = None,
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
                "navigation": navigation_summary,
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


def _navigation_blocked_result(
    *,
    run_id: str,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    acquire_result: Any,
    navigation_summary: dict[str, Any],
    reason_code: str,
    error_message: str | None,
    events_written: list[dict[str, Any]],
) -> SupervisionStepResult:
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
        error_message=error_message or reason_code,
        events_written=events_written,
        run_status=status,
        metadata={
            **metadata,
            "chatgpt_destination_navigation": {
                **navigation_summary,
                "operator_approved": True,
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
    waiting = reason_code == "chatgpt_ui_lease_already_held"
    return SupervisionStepResult(
        ok=waiting,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=False,
        next_state_hint=action_value if waiting else "blocked",
        blocked=not waiting,
        waiting_for_chatgpt=waiting,
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
                "waiting_for_chatgpt": waiting,
            },
        },
    )


def _chatgpt_wait_result(
    *,
    run_id: str,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    reason_code: str,
    error_message: str | None = None,
    events_written: list[dict[str, Any]] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> SupervisionStepResult:
    return SupervisionStepResult(
        ok=True,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=False,
        next_state_hint=action_value,
        blocked=False,
        waiting_for_chatgpt=True,
        reason_code=reason_code,
        error_message=error_message or reason_code,
        events_written=list(events_written or []),
        run_status=status,
        metadata={
            **metadata,
            "waiting_for_chatgpt": True,
            **(extra_metadata or {}),
        },
    )


def _handoff_queue_available(ledger: Any) -> bool:
    return all(
        callable(getattr(ledger, name, None))
        for name in (
            "enqueue_chatgpt_handoff",
            "claim_chatgpt_handoff_for_run",
            "complete_chatgpt_handoff",
            "block_chatgpt_handoff",
        )
    )


def _enqueue_and_claim_chatgpt_handoff(
    run_id: str,
    *,
    action_value: str,
    claim_owner_identifier: str,
    ledger: Any,
) -> Any:
    enqueue_result = ledger.enqueue_chatgpt_handoff(
        run_id,
        enqueue_source=action_value or CHATGPT_HANDOFF_QUEUE_ENQUEUE_SOURCE,
    )
    enqueue_status = str(getattr(enqueue_result, "status", "") or "")
    if enqueue_status in {
        str(default_ledger.AtomicChatGPTHandoffQueueStatus.RUN_NOT_FOUND),
        str(default_ledger.AtomicChatGPTHandoffQueueStatus.INVALID),
        str(default_ledger.AtomicChatGPTHandoffQueueStatus.OPERATIONAL_FAILURE),
    }:
        return enqueue_result
    return ledger.claim_chatgpt_handoff_for_run(
        run_id,
        claim_owner_identifier=claim_owner_identifier,
    )


def _queue_result_is_waiting(result: Any) -> bool:
    status = str(getattr(result, "status", "") or "")
    return status in {
        str(default_ledger.AtomicChatGPTHandoffQueueStatus.WAITING),
        str(default_ledger.AtomicChatGPTHandoffQueueStatus.WAITING_FOR_ACTIVE_CLAIM),
    }


def _queue_result_is_claimed(result: Any) -> bool:
    return str(getattr(result, "status", "") or "") == str(
        default_ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED
    )


def _finish_claimed_handoff_queue(
    queue_sequence: int | None,
    *,
    claim_owner_identifier: str,
    terminal: str,
    reason_code: str,
    lease_correlation: dict[str, object] | None,
    ledger: Any,
) -> None:
    if not isinstance(queue_sequence, int) or queue_sequence <= 0:
        return
    if not _handoff_queue_available(ledger):
        return
    finisher = (
        ledger.complete_chatgpt_handoff
        if terminal == "completed"
        else ledger.block_chatgpt_handoff
    )
    finisher(
        queue_sequence,
        claim_owner_identifier=claim_owner_identifier,
        reason_code=reason_code,
        lease_correlation=lease_correlation,
    )


def _chatgpt_submit_is_uncertain(result: Any) -> bool:
    reason_code = _result_reason_code(result, "")
    event_type = str(getattr(result, "event_type", "") or "")
    return (
        reason_code in {"chatgpt_submission_ambiguous", "chatgpt_submission_not_verified"}
        or event_type == "gpt_feedback_submission_ambiguous"
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
    destination_gate_service: Callable[..., Any],
    destination_adapter_factory: Callable[[], Any],
    destination_navigation_service: Callable[..., Any],
    allow_destination_navigation: bool,
    before_action_callback: Callable[[SupervisePlan, dict | None, list[dict]], None] | None,
    events: list[dict],
    desktop_mutex: Any,
    controller_instance_id: str | None,
) -> SupervisionStepResult:
    claim_owner_identifier = handoff_claim_owner_identifier(
        run_id,
        controller_instance_id=controller_instance_id,
    )
    queue_sequence: int | None = None
    queue_terminal: tuple[str, str] | None = None
    mutex_hold: ChatGPTDesktopMutexHold | None = None

    if _handoff_queue_available(ledger):
        queue_result = _enqueue_and_claim_chatgpt_handoff(
            run_id,
            action_value=action_value,
            claim_owner_identifier=claim_owner_identifier,
            ledger=ledger,
        )
        if _queue_result_is_waiting(queue_result):
            return _chatgpt_wait_result(
                run_id=run_id,
                plan=plan,
                action_value=action_value,
                plan_metadata=plan_metadata,
                status=status,
                metadata=metadata,
                reason_code=_result_reason_code(
                    queue_result,
                    "chatgpt_handoff_queue_not_head",
                ),
                error_message=_result_error_message(queue_result),
                extra_metadata={
                    "chatgpt_handoff_queue": {
                        "status": str(getattr(queue_result, "status", "")),
                        "head_run_id": getattr(queue_result, "head_run_id", None),
                        "queue_sequence": getattr(queue_result, "queue_sequence", None),
                    }
                },
            )
        if not _queue_result_is_claimed(queue_result):
            return SupervisionStepResult(
                ok=False,
                run_id=run_id,
                planner_action=action_value,
                planner_reason_code=plan.reason,
                planner_metadata=plan_metadata,
                action_executed=False,
                next_state_hint="blocked",
                blocked=True,
                reason_code=_result_reason_code(
                    queue_result,
                    "chatgpt_handoff_queue_claim_failed",
                ),
                error_message=_result_error_message(queue_result),
                events_written=_result_events_written(queue_result),
                run_status=status,
                metadata=metadata,
            )
        queue_sequence = getattr(queue_result, "queue_sequence", None)

    mutex = desktop_mutex if desktop_mutex is not None else ChatGPTDesktopMutex()
    mutex_hold = mutex.acquire(
        run_id,
        controller_instance_id=controller_instance_id,
    )
    if not mutex_hold.ok:
        reason_code = mutex_hold.reason_code or "chatgpt_desktop_mutex_already_held"
        if reason_code == "chatgpt_desktop_mutex_already_held":
            return _chatgpt_wait_result(
                run_id=run_id,
                plan=plan,
                action_value=action_value,
                plan_metadata=plan_metadata,
                status=status,
                metadata=metadata,
                reason_code=reason_code,
                error_message=mutex_hold.error_message,
                extra_metadata={
                    "chatgpt_desktop_mutex": {
                        "acquire_ok": False,
                        "reason_code": reason_code,
                        "owner_is_live": mutex_hold.owner_is_live,
                        "active_owner": mutex_hold.active_owner,
                    }
                },
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
            error_message=mutex_hold.error_message,
            run_status=status,
            metadata=metadata,
        )

    acquire_result = acquire_chatgpt_ui_lease(
        run_id,
        reason=CHATGPT_UI_LEASE_HANDOFF_REASON,
        source=CHATGPT_UI_LEASE_HANDOFF_SOURCE,
        ledger=ledger,
    )
    if not _result_ok(acquire_result):
        mutex_hold.release()
        mutex_hold = None
        denied = _chatgpt_lease_denied_result(
            run_id=run_id,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            acquire_result=acquire_result,
        )
        return denied

    lease_token = getattr(acquire_result, "lease_token", None)
    if not isinstance(lease_token, str) or lease_token.strip() == "":
        mutex_hold.release()
        mutex_hold = None
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
        phase = HANDOFF_PHASE_NAVIGATION_NOT_REQUESTED
        navigation_summary: dict[str, Any] | None = None

        # --- Separate navigation actor (operator-approved only) -------------
        # Runs after the UI lease is held and strictly before the read-only
        # destination gate. A ChatGPT UI handoff is fragile when the operator is
        # also using the pointer, so the send path retries the entire
        # navigate -> verify -> submit sequence before giving up.
        max_attempts = (
            CHATGPT_HANDOFF_MAX_UI_ATTEMPTS
            if plan.action == SuperviseAction.ASK_SEND_TO_GPT
            else 1
        )
        handoff_attempts: list[dict[str, Any]] = []
        current_result: Any = None
        current_stage = str(plan.action)
        previous_gate_failure_fingerprint: tuple[str, str] | None = None

        for attempt_number in range(1, max_attempts + 1):
            current_stage = str(plan.action)
            attempt_can_retry = (
                plan.action == SuperviseAction.ASK_SEND_TO_GPT
                and attempt_number < max_attempts
            )
            attempt_summary: dict[str, Any] = {
                "attempt_number": attempt_number,
                "max_attempts": max_attempts,
            }

            if allow_destination_navigation:
                phase = HANDOFF_PHASE_NAVIGATION_STARTED
                binding = _resolve_run_destination_binding(run_id, ledger=ledger)
                if binding is None:
                    nav_result = NavigationAttemptResult(
                        ok=False,
                        outcome="destination_binding_unavailable",
                        reason_code="destination_binding_unavailable",
                    )
                else:
                    try:
                        nav_result = destination_navigation_service(
                            run_id,
                            binding,
                            lease_context,
                            app_name=app_name,
                            ledger=ledger,
                        )
                    except Exception as nav_exc:
                        nav_result = NavigationAttemptResult(
                            ok=False,
                            outcome="navigation_actor_exception",
                            reason_code="destination_navigation_actor_exception",
                            error_message=str(nav_exc),
                        )
                navigation_summary = _navigation_summary(nav_result)
                attempt_summary["navigation"] = navigation_summary
                nav_event = _record_navigation_attempt_event(
                    run_id,
                    binding,
                    nav_result,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    ledger=ledger,
                )
                events_written.append(nav_event)
                if not nav_result.ok:
                    phase = HANDOFF_PHASE_NAVIGATION_FAILED
                    attempt_summary["stage"] = "navigation"
                    attempt_summary["ok"] = False
                    attempt_summary["reason_code"] = (
                        nav_result.reason_code or "destination_navigation_incomplete"
                    )
                    handoff_attempts.append(attempt_summary)
                    if attempt_can_retry:
                        continue
                    queue_terminal = (
                        "completed",
                        CHATGPT_HANDOFF_YIELD_REASON_CODE,
                    )
                    final_result = _chatgpt_wait_result(
                        run_id=run_id,
                        plan=plan,
                        action_value=action_value,
                        plan_metadata=plan_metadata,
                        status=status,
                        metadata=metadata,
                        reason_code=nav_result.reason_code or "destination_navigation_incomplete",
                        error_message=nav_result.error_message,
                        events_written=events_written,
                        extra_metadata={
                            "chatgpt_handoff_yield": {
                                "reason_code": CHATGPT_HANDOFF_YIELD_REASON_CODE,
                                "failed_stage": "navigation",
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
                    break
                phase = HANDOFF_PHASE_NAVIGATION_SUCCEEDED

            # --- Read-only destination gate (fresh post-navigation evidence) ----
            # The gate builds a fresh adapter and reads a fresh snapshot; when
            # navigation just ran, that snapshot is the fresh post-navigation
            # evidence. The gate remains a pure verifier and is never given
            # navigation behavior.
            phase = HANDOFF_PHASE_VERIFICATION_STARTED
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
                phase = HANDOFF_PHASE_VERIFICATION_FAILED
                reason_code = gate_result.reason_code or "destination_verification_unavailable"
                failure_fingerprint = _destination_gate_failure_fingerprint(gate_result)
                repeated_deterministic_failure = (
                    failure_fingerprint == previous_gate_failure_fingerprint
                )
                previous_gate_failure_fingerprint = failure_fingerprint
                will_retry = bool(
                    attempt_can_retry and not repeated_deterministic_failure
                )
                attempt_summary["stage"] = "verification"
                attempt_summary["ok"] = False
                attempt_summary["reason_code"] = reason_code
                attempt_summary["navigation"] = navigation_summary
                attempt_summary["repeated_deterministic_failure"] = (
                    repeated_deterministic_failure
                )
                handoff_attempts.append(attempt_summary)
                gate_event = _record_destination_gate_blocked_event(
                    gate_result,
                    acquire_result,
                    navigation_summary=navigation_summary,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    will_retry=will_retry,
                    ledger=ledger,
                )
                events_written.append(gate_event)
                if will_retry:
                    continue
                queue_terminal = (
                    "completed",
                    CHATGPT_HANDOFF_YIELD_REASON_CODE,
                )
                final_result = _chatgpt_wait_result(
                    run_id=run_id,
                    plan=plan,
                    action_value=action_value,
                    plan_metadata=plan_metadata,
                    status=status,
                    metadata=metadata,
                    reason_code=reason_code,
                    error_message=gate_result.reason_code or "destination_verification_unavailable",
                    events_written=events_written,
                    extra_metadata={
                        "chatgpt_handoff_yield": {
                            "reason_code": CHATGPT_HANDOFF_YIELD_REASON_CODE,
                            "failed_stage": "verification",
                        },
                        "chatgpt_destination_gate": {
                            **_destination_gate_metadata(gate_result, acquire_result),
                            "feedback_ui_handoff_skipped": True,
                            "navigation": navigation_summary,
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
                break

            phase = HANDOFF_PHASE_VERIFICATION_SUCCEEDED
            if before_action_callback is not None:
                before_action_callback(plan, run, events)

            if plan.action != SuperviseAction.ASK_SEND_TO_GPT:
                attempt_summary["stage"] = "verification"
                attempt_summary["ok"] = True
                attempt_summary["reason_code"] = "destination_verified"
                attempt_summary["navigation"] = navigation_summary
                handoff_attempts.append(attempt_summary)
                break

            phase = HANDOFF_PHASE_SUBMISSION_STARTED
            try:
                current_result = submit_service(
                    run_id,
                    run,
                    app_name,
                    approval_mode=mode,
                    ledger=ledger,
                )
            except Exception as submit_exc:
                current_result = _exception_service_result(
                    reason_code="submit_feedback_exception",
                    error_message=str(submit_exc),
                )
            event = _event_from_service_result(current_result)
            if event is not None:
                events_written.append(event)
            if not _result_ok(current_result):
                reason_code = _result_reason_code(current_result, "submit_feedback_failed")
                retryable_failure = (
                    _chatgpt_submit_failure_retryable(current_result)
                    and not _chatgpt_submit_is_uncertain(current_result)
                )
                attempt_summary["stage"] = "submission"
                attempt_summary["ok"] = False
                attempt_summary["reason_code"] = reason_code
                attempt_summary["retryable"] = retryable_failure
                attempt_summary["navigation"] = navigation_summary
                handoff_attempts.append(attempt_summary)
                if attempt_can_retry and retryable_failure:
                    continue
                if _chatgpt_submit_is_uncertain(current_result):
                    queue_terminal = (
                        "blocked",
                        _result_reason_code(current_result, "chatgpt_submission_uncertain"),
                    )
                    final_result = _chatgpt_step_result(
                        ok=False,
                        run_id=run_id,
                        plan=plan,
                        action_value=action_value,
                        plan_metadata=plan_metadata,
                        status=status,
                        metadata=metadata,
                        result=current_result,
                        next_state_hint="blocked",
                        default_reason_code="submit_feedback_failed",
                        events_written=events_written,
                    )
                    break
                if retryable_failure:
                    queue_terminal = (
                        "completed",
                        CHATGPT_HANDOFF_YIELD_REASON_CODE,
                    )
                    final_result = _chatgpt_wait_result(
                        run_id=run_id,
                        plan=plan,
                        action_value=action_value,
                        plan_metadata=plan_metadata,
                        status=status,
                        metadata=metadata,
                        reason_code=reason_code,
                        error_message=_result_error_message(current_result),
                        events_written=events_written,
                        extra_metadata={
                            "chatgpt_handoff_yield": {
                                "reason_code": CHATGPT_HANDOFF_YIELD_REASON_CODE,
                                "failed_stage": "submission",
                            }
                        },
                    )
                    break
                queue_terminal = (
                    "blocked",
                    reason_code,
                )
                final_result = _chatgpt_step_result(
                    ok=False,
                    run_id=run_id,
                    plan=plan,
                    action_value=action_value,
                    plan_metadata=plan_metadata,
                    status=status,
                    metadata=metadata,
                    result=current_result,
                    next_state_hint="blocked",
                    default_reason_code="submit_feedback_failed",
                    events_written=events_written,
                )
                break

            attempt_summary["stage"] = "submission"
            attempt_summary["ok"] = True
            attempt_summary["reason_code"] = _result_reason_code(
                current_result,
                "chatgpt_submission_verified",
            )
            attempt_summary["navigation"] = navigation_summary
            handoff_attempts.append(attempt_summary)
            current_stage = str(SuperviseAction.CAPTURE_GPT_RESPONSE)
            break

        if final_result is None and current_stage == str(SuperviseAction.CAPTURE_GPT_RESPONSE):
            phase = HANDOFF_PHASE_CAPTURE_STARTED
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
                queue_terminal = (
                    "blocked",
                    _result_reason_code(current_result, "capture_gpt_response_failed"),
                )
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
                queue_terminal = (
                    "completed",
                    CHATGPT_HANDOFF_SLICE_COMPLETED_REASON_CODE,
                )
                final_result = _chatgpt_step_result(
                    ok=True,
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

        if final_result is None:
            queue_terminal = (
                "blocked",
                "unknown_chatgpt_handoff_action",
            )
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
                events_written=events_written,
            )

        # One compact, bounded phase marker per transaction so the local read
        # model can surface where the handoff stopped (not a prose trace).
        phase_event = _record_handoff_phase_event(
            run_id,
            phase,
            navigation_operator_approved=allow_destination_navigation,
            navigation_summary=navigation_summary,
            ledger=ledger,
        )
        events_written.append(phase_event)
        final_result = replace(
            final_result,
            metadata={
                **final_result.metadata,
                "chatgpt_handoff_retry": {
                    "attempt_count": len(handoff_attempts),
                    "max_attempts": max_attempts,
                    "attempts": handoff_attempts,
                },
                "chatgpt_handoff_phase": {
                    "phase": phase,
                    "navigation_operator_approved": bool(allow_destination_navigation),
                    "navigation": navigation_summary,
                },
            },
        )
    except BaseException as exc:
        original_exc = exc
        raise
    finally:
        if original_exc is not None and queue_terminal is None:
            queue_terminal = (
                "completed",
                CHATGPT_HANDOFF_YIELD_REASON_CODE,
            )
        try:
            if queue_terminal is not None:
                _finish_claimed_handoff_queue(
                    queue_sequence,
                    claim_owner_identifier=claim_owner_identifier,
                    terminal=queue_terminal[0],
                    reason_code=queue_terminal[1],
                    lease_correlation={
                        "owning_run_id": run_id,
                        "lease_event_id": getattr(acquire_result, "event_id", None),
                    },
                    ledger=ledger,
                )
        except Exception:
            pass
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
        if mutex_hold is not None:
            try:
                mutex_hold.release()
            except Exception:
                pass

    if final_result is None:
        raise RuntimeError("ChatGPT handoff transaction produced no result")
    if release_result is not None:
        final_result = _append_release_metadata(
            final_result,
            release_result,
            release_event,
        )
    return final_result


def _run_extract_next_prompt(
    *,
    run_id: str,
    plan: SupervisePlan,
    action_value: str,
    plan_metadata: dict[str, Any],
    status: str | None,
    metadata: dict[str, Any],
    extraction_service: Callable[..., Any],
    ledger: Any,
) -> SupervisionStepResult:
    current_result = extraction_service(
        run_id,
        require_sentinel=True,
        confirm_extract=True,
        ledger=ledger,
    )
    events_written = _result_events_written(current_result)
    event = _event_from_service_result(current_result)
    if event is not None and event not in events_written:
        events_written.append(event)
    ok = _result_ok(current_result)
    return SupervisionStepResult(
        ok=ok,
        run_id=run_id,
        planner_action=action_value,
        planner_reason_code=plan.reason,
        planner_metadata=plan_metadata,
        action_executed=True,
        action_result=current_result,
        next_state_hint="ask_run_prompt" if ok else "blocked",
        blocked=not ok,
        waiting_for_chatgpt=False,
        reason_code=_result_reason_code(current_result, "extract_next_prompt_failed"),
        error_message=_result_error_message(current_result),
        events_written=events_written,
        run_status=status,
        metadata={
            **metadata,
            "chatgpt_ui_lease": {
                "acquire_ok": False,
                "skipped": True,
                "reason_code": "extract_is_ledger_only",
            },
            "chatgpt_handoff_queue": {"skipped": True},
        },
    )


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
    destination_navigation_service: Callable[..., Any] = _default_destination_navigation_service,
    allow_destination_navigation: bool = False,
    send_auto_safety_evaluator: Callable[[object, list[dict]], tuple[bool, str]] = send_plan_auto_safe,
    auto_stop_recorder: Callable[..., dict[str, Any]] = record_supervise_auto_stop,
    before_action_callback: Callable[[SupervisePlan, dict | None, list[dict]], None] | None = None,
    desktop_mutex: Any | None = None,
    controller_instance_id: str | None = None,
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
            destination_gate_service=destination_gate_service,
            destination_adapter_factory=destination_adapter_factory,
            destination_navigation_service=destination_navigation_service,
            allow_destination_navigation=allow_destination_navigation,
            before_action_callback=before_action_callback,
            events=events,
            desktop_mutex=desktop_mutex if desktop_mutex is not None else ChatGPTDesktopMutex(),
            controller_instance_id=controller_instance_id,
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
            destination_gate_service=destination_gate_service,
            destination_adapter_factory=destination_adapter_factory,
            destination_navigation_service=destination_navigation_service,
            allow_destination_navigation=allow_destination_navigation,
            before_action_callback=before_action_callback,
            events=events,
            desktop_mutex=desktop_mutex if desktop_mutex is not None else ChatGPTDesktopMutex(),
            controller_instance_id=controller_instance_id,
        )

    if action == SuperviseAction.EXTRACT_NEXT_PROMPT:
        if before_action_callback is not None:
            before_action_callback(plan, run, events)
        return _run_extract_next_prompt(
            run_id=run_id,
            plan=plan,
            action_value=action_value,
            plan_metadata=plan_metadata,
            status=status,
            metadata=metadata,
            extraction_service=extraction_service,
            ledger=ledger,
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
