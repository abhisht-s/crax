from __future__ import annotations


def can_continue_run(status: str | None) -> dict:
    rules = {
        "completed": {
            "can_continue": True,
            "reason": "run_completed",
            "required_action": None,
        },
        "approved": {
            "can_continue": True,
            "reason": "run_approved",
            "required_action": None,
        },
        "created": {
            "can_continue": False,
            "reason": "run_not_started",
            "required_action": "run_codex_or_start_next_step",
        },
        "running": {
            "can_continue": False,
            "reason": "run_still_running",
            "required_action": "wait_for_current_step",
        },
        "needs_review": {
            "can_continue": False,
            "reason": "run_needs_review",
            "required_action": "complete-review or reject",
        },
        "waiting_for_approval": {
            "can_continue": False,
            "reason": "run_waiting_for_approval",
            "required_action": "approve or reject",
        },
        "rejected": {
            "can_continue": False,
            "reason": "run_rejected",
            "required_action": "start_new_run_or_fix_manually",
        },
        "failed": {
            "can_continue": False,
            "reason": "run_failed",
            "required_action": "inspect_failure",
        },
    }

    result = rules.get(
        status,
        {
            "can_continue": False,
            "reason": "unknown_status",
            "required_action": "inspect_run",
        },
    )
    return {"status": status, **result}
