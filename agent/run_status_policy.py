from __future__ import annotations


def _codex_validation_failed(codex_result: dict | None) -> bool:
    if codex_result is None:
        return False
    return bool(codex_result.get("validation_error"))


def status_from_supervision_decision(
    supervision_decision: dict | None,
    codex_result: dict | None = None,
) -> dict:
    validation_failed = _codex_validation_failed(codex_result)

    if supervision_decision is None:
        transition = {
            "next_status": "needs_review",
            "reason": "supervision_decision_missing",
            "decision": None,
            "approval_required": False,
            "needs_review": True,
            "should_auto_complete": False,
        }
    else:
        decision = supervision_decision.get("decision")
        approval_required = bool(supervision_decision.get("approval_required"))
        needs_review = bool(supervision_decision.get("needs_review"))

        if decision == "continue":
            transition = {
                "next_status": "completed",
                "reason": "supervision_decision_continue",
                "decision": decision,
                "approval_required": approval_required,
                "needs_review": needs_review,
                "should_auto_complete": True,
            }
        elif decision == "record_only":
            transition = {
                "next_status": "completed",
                "reason": "record_only_context_flags",
                "decision": decision,
                "approval_required": approval_required,
                "needs_review": needs_review,
                "should_auto_complete": True,
            }
        elif decision == "needs_review":
            transition = {
                "next_status": "needs_review",
                "reason": "supervision_decision_needs_review",
                "decision": decision,
                "approval_required": approval_required,
                "needs_review": True,
                "should_auto_complete": False,
            }
        elif decision == "approval_required":
            transition = {
                "next_status": "waiting_for_approval",
                "reason": "supervision_decision_approval_required",
                "decision": decision,
                "approval_required": True,
                "needs_review": True,
                "should_auto_complete": False,
            }
        else:
            transition = {
                "next_status": "needs_review",
                "reason": "unknown_supervision_decision",
                "decision": decision,
                "approval_required": approval_required,
                "needs_review": True,
                "should_auto_complete": False,
            }

    if validation_failed:
        transition = {
            **transition,
            "reason": "codex_validation_failed_before_running",
            "needs_review": True,
            "should_auto_complete": False,
        }
        if transition["decision"] == "approval_required":
            transition["next_status"] = "waiting_for_approval"
            transition["approval_required"] = True
        else:
            transition["next_status"] = "needs_review"

    return transition
