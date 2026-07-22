from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.file_classifier import classify_changed_files
from agent.git_snapshot import (
    attributable_paths,
    capture_git_snapshot,
    capture_invocation_git_state,
    compute_invocation_delta,
)
from agent.prompt_extraction import sha256_text
from agent.risk_policy import evaluate_supervision_decision
from agent.run_diagnostics import analyze_prompt_repo_impact
from agent.run_state import RunStatus
from agent.run_status_policy import status_from_supervision_decision
from agent.workspace_write_policy import (
    POLICY_VERSION as WORKSPACE_WRITE_POLICY_VERSION,
    diff_content_flags,
    verify_workspace_write_post_run,
)


@dataclass(frozen=True)
class PostCodexGovernanceCallbacks:
    diagnostics_warning: Callable[[Exception], None] | None = None
    git_after_captured: Callable[[dict], None] | None = None
    changed_file_classification_recorded: Callable[[dict], None] | None = None
    diagnostics_recorded: Callable[[dict | None], None] | None = None
    supervision_decision_recorded: Callable[[dict], None] | None = None
    governance_observation_recorded: Callable[[dict], None] | None = None
    workspace_write_human_required: Callable[[dict], None] | None = None
    status_transition_recorded: Callable[[dict], None] | None = None


@dataclass(frozen=True)
class PostCodexGovernanceResult:
    ok: bool
    run_id: str
    reason_code: str | None
    error_message: str | None
    raw_execution_result: dict[str, Any]
    git_before: dict[str, Any] | None
    git_after: dict[str, Any] | None
    invocation_state_before: dict[str, Any] | None
    invocation_state_after: dict[str, Any] | None
    invocation_delta: dict[str, Any] | None
    changed_file_classification: dict[str, Any] | None
    diagnostics: dict[str, Any] | None
    supervision_decision: dict[str, Any] | None
    workspace_write_pre_run_result: dict[str, Any] | None
    workspace_write_post_run_result: dict[str, Any] | None
    governance_observation: dict[str, Any] | None
    previous_status: str | None
    next_status: str | None
    status_transition_event_id: int | None
    events_written: list[dict[str, Any]] = field(default_factory=list)
    auto_supervision_allowed: bool | None = None
    human_review_required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    persisted: bool = False


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _event_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _record_event(
    event_ledger: Any,
    events_written: list[dict[str, Any]],
    run_id: str,
    event_type: str,
    message: str,
    metadata: dict[str, Any] | None,
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


def _working_tree_dirty(snapshot: dict) -> bool:
    return bool(snapshot["status_short"].strip())


def _short_hash(value: str | None) -> str | None:
    if not value:
        return None
    return value[:12]


def _snapshot_message(snapshot: dict) -> str:
    branch = snapshot["branch"] or "None"
    head = _short_hash(snapshot["head"]) or "None"
    dirty = str(_working_tree_dirty(snapshot)).lower()
    return (
        f"repo_path={snapshot['repo_path']} branch={branch} "
        f"head={head} dirty={dirty}"
    )


def _classification_message(classification: dict) -> str:
    return (
        f"total_files={classification['total_files']} "
        f"category_counts={classification['counts_by_category']} "
        f"risk_counts={classification['counts_by_risk_level']} "
        f"high_risk_file_count={len(classification['high_risk_files'])}"
    )


def _diagnostics_message(diagnostics: dict | None) -> str:
    if diagnostics is None:
        return "diagnostics_unavailable"
    return (
        f"outcome={diagnostics['outcome']} "
        f"attention_level={diagnostics['attention_level']} "
        f"flags={diagnostics['flags']}"
    )


def _supervision_decision_message(decision: dict) -> str:
    return (
        f"decision={decision['decision']} "
        f"attention_level={decision['attention_level']} "
        f"approval_required={decision['approval_required']} "
        f"reasons={decision['reasons']}"
    )


def _run_status_transition_message(transition: dict) -> str:
    return (
        f"previous_status={transition['previous_status']} "
        f"next_status={transition['next_status']} "
        f"reason={transition['reason']}"
    )


def _delta_name_status(delta: dict | None) -> str:
    if not isinstance(delta, dict):
        return ""
    statuses = {
        "modified": "M",
        "added": "A",
        "deleted": "D",
        "renamed": "R",
    }
    lines = []
    for detail in delta.get("path_delta_details", []):
        if not isinstance(detail, dict):
            continue
        path = str(detail.get("path") or "").strip()
        if not path:
            continue
        status = statuses.get(str(detail.get("change_type") or ""), "M")
        lines.append(f"{status}\t{path}")
    return "\n".join(lines)


def _delta_diff_text(delta: dict | None) -> str:
    if not isinstance(delta, dict):
        return ""
    chunks = []
    for detail in delta.get("path_delta_details", []):
        if isinstance(detail, dict) and isinstance(detail.get("diff_unified_zero"), str):
            chunks.append(detail["diff_unified_zero"])
    return "\n".join(chunk for chunk in chunks if chunk)


def _path_is_related_focused_test(path: str) -> bool:
    lower = path.lower()
    return lower.startswith("tests/") or "/tests/" in f"/{lower}/" or Path(path).name.lower().startswith("test_")


def _contract_allowed_path_mismatches(contract: dict, paths: list[str]) -> list[dict]:
    allowed_items = contract.get("allowed_paths")
    if not isinstance(allowed_items, list) or not allowed_items:
        return []
    allowed = {str(item.get("path") or "") for item in allowed_items if isinstance(item, dict)}
    allowed_names = {Path(path).name for path in allowed if path}
    groups = contract.get("allowed_path_groups")
    if not isinstance(groups, list):
        groups = []
    allows_related_tests = any(
        isinstance(item, dict) and item.get("kind") == "related_focused_tests"
        for item in groups
    )
    mismatches = []
    for path in paths:
        if path in allowed or Path(path).name in allowed_names:
            continue
        if allows_related_tests and _path_is_related_focused_test(path):
            continue
        mismatches.append({"type": "path_outside_explicit_contract", "path": path})
    return mismatches


def _path_matches_excluded_area(path: str, category: str | None, area: str) -> bool:
    lower = path.lower()
    category = category or ""
    if area == "database":
        return category == "database_migration" or lower.endswith(".sql") or "migration" in lower or "database" in lower
    if area == "configuration":
        return category in {"config", "dependency_manifest", "build_or_ci"} or lower.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"))
    if area == "auth":
        return category == "auth_security" or "auth" in lower or "session" in lower
    if area == "infrastructure":
        return category == "infrastructure" or "deploy" in lower or "docker" in lower or ".github/" in lower
    if area == "backend":
        return any(marker in lower for marker in ("server/", "api/", "backend/", "workers/", "functions/", "database/", "supabase/"))
    if area == "networking":
        return any(marker in lower for marker in ("network", "http", "api/", "client", "request"))
    return False


def _contract_exclusion_mismatches(contract: dict, paths: list[str], classification: dict | None) -> list[dict]:
    excluded = contract.get("excluded_areas")
    if not isinstance(excluded, list) or not excluded:
        return []
    files = classification.get("files") if isinstance(classification, dict) else []
    categories = {
        str(file.get("path") or ""): str(file.get("category") or "")
        for file in files
        if isinstance(file, dict)
    }
    mismatches = []
    for item in excluded:
        if not isinstance(item, dict):
            continue
        area = str(item.get("area") or "")
        for path in paths:
            if _path_matches_excluded_area(path, categories.get(path), area):
                mismatches.append({"type": "excluded_area_changed", "area": area, "path": path})
    return mismatches


def _explicit_guardrails(contract: dict) -> list[str]:
    guardrails = []
    read_only = contract.get("read_only") if isinstance(contract.get("read_only"), dict) else {}
    if read_only.get("explicit"):
        guardrails.append("read_only")
    for item in contract.get("allowed_paths", []):
        if isinstance(item, dict) and item.get("path"):
            guardrails.append(f"{item.get('mode') or 'allowed'}:{item['path']}")
    for item in contract.get("excluded_areas", []):
        if isinstance(item, dict) and item.get("area"):
            guardrails.append(f"exclude:{item['area']}")
    return guardrails


def _build_governance_observation(
    contract: dict,
    delta: dict | None,
    classification: dict | None,
    sandbox: str,
    before_snapshot: dict | None,
) -> dict:
    paths = attributable_paths(delta)
    read_only = contract.get("read_only") if isinstance(contract.get("read_only"), dict) else {}
    contract_mismatches = []
    if read_only.get("explicit") and paths:
        contract_mismatches.append({"type": "explicit_read_only_changed_files", "paths": paths})
    contract_mismatches.extend(_contract_allowed_path_mismatches(contract, paths))
    contract_mismatches.extend(_contract_exclusion_mismatches(contract, paths, classification))

    diff_text = _delta_diff_text(delta)
    content_flags = diff_content_flags(diff_text)
    objective_failures = []
    path_safety = contract.get("path_safety") if isinstance(contract.get("path_safety"), dict) else {}
    if sandbox != "danger-full-access":
        if path_safety.get("valid") is False:
            objective_failures.append("invalid_contract_path")
        if sandbox == "read-only" and paths:
            objective_failures.append("read_only_sandbox_attributable_write")
        if "high_confidence_secret_literal" in content_flags:
            objective_failures.append("high_confidence_secret_literal")

    scope_observation = "matched"
    if contract_mismatches:
        scope_observation = "partially_matched"
    if not paths and _explicit_guardrails(contract):
        scope_observation = "not_evaluable" if delta and delta.get("validation_error") else "matched"

    observation_flags = []
    if sandbox == "danger-full-access":
        observation_flags.append("autonomous_full_access_policy_bypass")
    if before_snapshot and _working_tree_dirty(before_snapshot):
        observation_flags.append("repo_dirty_before_codex")
    observation_flags.extend(flag for flag in content_flags if flag != "high_confidence_secret_literal")

    return {
        "governance_version": "explicit_contract_delta_v1",
        "prompt_contract_confidence": contract.get("confidence", "low"),
        "scope_observation": scope_observation,
        "attributable_changed_files": paths,
        "preexisting_changed_files": (delta or {}).get("preexisting_changed_files", []),
        "preexisting_untracked_files": (delta or {}).get("preexisting_untracked_files", []),
        "explicit_guardrails": _explicit_guardrails(contract),
        "observation_flags": observation_flags,
        "contract_mismatches": contract_mismatches,
        "objective_failures": objective_failures,
        "requires_future_review": bool(contract_mismatches),
    }


def _governance_transition_if_blocking(observation: dict, current_transition: dict) -> dict:
    objective_failures = observation.get("objective_failures")
    if not objective_failures:
        return current_transition
    return {
        **current_transition,
        "next_status": RunStatus.NEEDS_REVIEW.value,
        "reason": "objective_governance_failure",
        "decision": "objective_failure",
        "approval_required": False,
        "needs_review": True,
        "should_auto_complete": False,
        "objective_failures": objective_failures,
    }


def _verify_auto_workspace_write_result(
    run_id: str,
    repo_path_text: str,
    prompt_sha256: str,
    expected_scope: dict,
    changed_file_classification: dict | None,
    invocation_delta: dict | None,
    *,
    event_ledger: Any,
    events_written: list[dict[str, Any]],
    post_run_evaluator: Callable[..., Any],
    callbacks: PostCodexGovernanceCallbacks | None,
) -> dict:
    attributable = attributable_paths(invocation_delta)
    diff_metadata = {
        "repo_path": repo_path_text,
        "name_status": _delta_name_status(invocation_delta),
        "changed_paths": attributable,
        "diff_unified_zero": _delta_diff_text(invocation_delta),
        "commands": {},
        "validation_error": (invocation_delta or {}).get("validation_error"),
        "captured_at": _utc_now(),
        "source": "invocation_delta",
    }
    _record_event(
        event_ledger,
        events_written,
        run_id,
        "workspace_write_diff_metadata_captured",
        (
            "Captured workspace-write attributable diff metadata."
            if not diff_metadata.get("validation_error")
            else f"Failed to capture workspace-write diff metadata: {diff_metadata.get('validation_error')}"
        ),
        {
            "run_id": run_id,
            "prompt_sha256": prompt_sha256,
            "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
            **diff_metadata,
        },
    )

    events = event_ledger.list_events(run_id)
    codex_event_id = _latest_event_id(events, "codex_exec_finished")
    if diff_metadata.get("validation_error"):
        post_run_policy = {
            "tier": "workspace_write_scoped_auto",
            "allowed": True,
            "reason_code": "post_run_observations_recorded",
            "policy_version": WORKSPACE_WRITE_POLICY_VERSION,
            "expected_scope": expected_scope,
            "changed_files": [],
            "unexpected_files": [],
            "prohibited_files": [],
            "name_status_summary": [],
            "diff_content_flags": [],
            "matched_rules": ["safety_classifiers_disabled"],
        }
    else:
        verification = post_run_evaluator(
            expected_scope,
            diff_metadata.get("changed_paths") or [],
            diff_metadata.get("name_status") or "",
            diff_metadata.get("diff_unified_zero") or "",
            changed_file_classification,
        )
        post_run_policy = verification.to_dict()

    post_run_metadata = {
        "run_id": run_id,
        "prompt_sha256": prompt_sha256,
        "codex_exec_finished_event_id": codex_event_id,
        "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
        "post_run_policy": post_run_policy,
        "auto_submit_allowed": bool(post_run_policy.get("allowed")),
        "loop_continuation_allowed": bool(post_run_policy.get("allowed")),
    }
    _record_event(
        event_ledger,
        events_written,
        run_id,
        "workspace_write_post_run_policy",
        (
            "Workspace-write post-run diff stayed within auto-approved scope."
            if post_run_policy.get("allowed")
            else "Workspace-write post-run diff requires human review."
        ),
        post_run_metadata,
    )

    if not post_run_policy.get("allowed"):
        human_metadata = {
            "run_id": run_id,
            "prompt_sha256": prompt_sha256,
            "codex_exec_finished_event_id": codex_event_id,
            "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
            "reason_code": post_run_policy.get("reason_code"),
            "changed_files": post_run_policy.get("changed_files", []),
            "unexpected_files": post_run_policy.get("unexpected_files", []),
            "prohibited_files": post_run_policy.get("prohibited_files", []),
            "name_status_summary": post_run_policy.get("name_status_summary", []),
            "diff_content_flags": post_run_policy.get("diff_content_flags", []),
            "expected_scope": expected_scope,
            "post_run_policy": post_run_policy,
        }
        _record_event(
            event_ledger,
            events_written,
            run_id,
            "human_required_after_write",
            "Codex completed, but an objective workspace-write post-run failure was detected.",
            human_metadata,
        )
        if callbacks and callbacks.workspace_write_human_required:
            callbacks.workspace_write_human_required(post_run_policy)

    return post_run_policy


def _latest_event_id(events: list[dict], event_type: str) -> int:
    latest_id = -1
    for event in events:
        if event.get("event_type") != event_type:
            continue
        try:
            latest_id = max(latest_id, int(event.get("id") or -1))
        except (TypeError, ValueError):
            continue
    return latest_id


def _auto_supervision_allowed(
    result: dict,
    transition: dict,
    supervision_decision: dict,
    sandbox: str,
) -> bool:
    if result.get("validation_error"):
        return False
    if result.get("found") is not True:
        return False
    if bool(result.get("timed_out")):
        return False
    if result.get("exit_code") != 0:
        return False
    if transition.get("next_status") != RunStatus.COMPLETED.value:
        return False
    if supervision_decision.get("decision") not in {"continue", "record_only"}:
        return False
    if bool(supervision_decision.get("needs_review")):
        return False
    if bool(supervision_decision.get("approval_required")):
        return False
    return True


def apply_post_codex_governance_service(
    run_id: str,
    run: dict[str, Any],
    prompt: str,
    repo_path: str,
    sandbox: str,
    prompt_contract: dict[str, Any],
    raw_execution_result: dict[str, Any],
    git_before: dict[str, Any] | None,
    invocation_state_before: dict[str, Any] | None,
    *,
    expected_scope: dict[str, Any] | None = None,
    workspace_write_pre_run_result: dict[str, Any] | None = None,
    ledger: Any = default_ledger,
    git_snapshot_function: Callable[[str], dict[str, Any]] = capture_git_snapshot,
    invocation_state_function: Callable[[str], dict[str, Any]] = capture_invocation_git_state,
    delta_function: Callable[[dict | None, dict | None], dict[str, Any]] = compute_invocation_delta,
    file_classifier_function: Callable[[list[str]], dict[str, Any]] = classify_changed_files,
    diagnostics_evaluator: Callable[..., dict[str, Any]] = analyze_prompt_repo_impact,
    supervision_decision_evaluator: Callable[[dict | None], dict[str, Any]] = evaluate_supervision_decision,
    workspace_write_post_run_evaluator: Callable[..., Any] = verify_workspace_write_post_run,
    status_policy_function: Callable[[dict | None, dict | None], dict[str, Any]] = status_from_supervision_decision,
    status_update_function: Callable[[str, RunStatus], Any] | None = None,
    prompt_hash_function: Callable[[str], str] = sha256_text,
    callbacks: PostCodexGovernanceCallbacks | None = None,
) -> PostCodexGovernanceResult:
    events_written: list[dict[str, Any]] = []
    result = raw_execution_result
    git_after = None
    invocation_state_after = None
    invocation_delta = None
    governance_observation = None
    changed_file_classification = None
    workspace_write_post_run_result = None

    if not result["validation_error"]:
        git_after = git_snapshot_function(repo_path)
        invocation_state_after = invocation_state_function(repo_path)
        invocation_delta = delta_function(invocation_state_before, invocation_state_after)
        _record_event(
            ledger,
            events_written,
            run_id,
            "git_snapshot_after_codex",
            _snapshot_message(git_after),
            git_after,
        )
        if callbacks and callbacks.git_after_captured:
            callbacks.git_after_captured(git_after)
        _record_event(
            ledger,
            events_written,
            run_id,
            "invocation_git_state_after",
            "Captured post-Codex invocation git state.",
            invocation_state_after,
        )
        _record_event(
            ledger,
            events_written,
            run_id,
            "invocation_delta_attributed",
            (
                "Attributed invocation delta "
                f"files={len(attributable_paths(invocation_delta))}."
            ),
            invocation_delta,
        )

        changed_file_classification = file_classifier_function(
            attributable_paths(invocation_delta)
        )
        _record_event(
            ledger,
            events_written,
            run_id,
            "changed_file_classification",
            _classification_message(changed_file_classification),
            changed_file_classification,
        )
        if callbacks and callbacks.changed_file_classification_recorded:
            callbacks.changed_file_classification_recorded(changed_file_classification)

    try:
        diagnostics = diagnostics_evaluator(
            prompt,
            result,
            git_before,
            git_after,
            changed_file_classification,
        )
    except Exception as exc:
        diagnostics = None
        if callbacks and callbacks.diagnostics_warning:
            callbacks.diagnostics_warning(exc)
    _record_event(
        ledger,
        events_written,
        run_id,
        "prompt_repo_impact_diagnostics",
        _diagnostics_message(diagnostics),
        diagnostics,
    )
    if callbacks and callbacks.diagnostics_recorded:
        callbacks.diagnostics_recorded(diagnostics)

    supervision_decision = supervision_decision_evaluator(diagnostics)
    _record_event(
        ledger,
        events_written,
        run_id,
        "supervision_decision",
        _supervision_decision_message(supervision_decision),
        supervision_decision,
    )
    if callbacks and callbacks.supervision_decision_recorded:
        callbacks.supervision_decision_recorded(supervision_decision)

    transition = status_policy_function(supervision_decision, result)
    if not result["validation_error"]:
        governance_observation = _build_governance_observation(
            prompt_contract,
            invocation_delta,
            changed_file_classification,
            sandbox,
            git_before,
        )
        _record_event(
            ledger,
            events_written,
            run_id,
            "run_governance_observation",
            (
                "Recorded explicit-contract and attributable-delta governance "
                f"observation scope={governance_observation['scope_observation']}."
            ),
            governance_observation,
        )
        if callbacks and callbacks.governance_observation_recorded:
            callbacks.governance_observation_recorded(governance_observation)
        transition = _governance_transition_if_blocking(governance_observation, transition)
        if sandbox == "workspace-write":
            workspace_write_post_run_result = _verify_auto_workspace_write_result(
                run_id,
                repo_path,
                prompt_hash_function(prompt),
                expected_scope or {},
                changed_file_classification,
                invocation_delta,
                event_ledger=ledger,
                events_written=events_written,
                post_run_evaluator=workspace_write_post_run_evaluator,
                callbacks=callbacks,
            )
    transition = {
        **transition,
        "previous_status": run["status"],
        "next_status": transition["next_status"],
    }
    update_status = status_update_function or ledger.update_run_status
    update_status(run_id, RunStatus(transition["next_status"]))
    status_transition_event_id = _record_event(
        ledger,
        events_written,
        run_id,
        "run_status_transition",
        _run_status_transition_message(transition),
        transition,
    )
    if callbacks and callbacks.status_transition_recorded:
        callbacks.status_transition_recorded(transition)

    human_review_required = bool(
        transition.get("needs_review")
        or transition.get("approval_required")
        or (
            isinstance(workspace_write_post_run_result, dict)
            and not workspace_write_post_run_result.get("allowed", True)
        )
    )
    return PostCodexGovernanceResult(
        ok=True,
        run_id=run_id,
        reason_code=transition.get("reason"),
        error_message=None,
        raw_execution_result=result,
        git_before=git_before,
        git_after=git_after,
        invocation_state_before=invocation_state_before,
        invocation_state_after=invocation_state_after,
        invocation_delta=invocation_delta,
        changed_file_classification=changed_file_classification,
        diagnostics=diagnostics,
        supervision_decision=supervision_decision,
        workspace_write_pre_run_result=workspace_write_pre_run_result,
        workspace_write_post_run_result=workspace_write_post_run_result,
        governance_observation=governance_observation,
        previous_status=transition.get("previous_status"),
        next_status=transition.get("next_status"),
        status_transition_event_id=status_transition_event_id,
        events_written=events_written,
        auto_supervision_allowed=_auto_supervision_allowed(result, transition, supervision_decision, sandbox),
        human_review_required=human_review_required,
        metadata={
            "transition": transition,
            "expected_scope": expected_scope or {},
        },
        persisted=True,
    )
