"""Reusable run services shared by CLI and future local controllers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from agent import ledger as default_ledger
from agent.run_state import RunStatus


class HumanDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    COMPLETE_REVIEW = "complete_review"


class HumanDecisionLedger(Protocol):
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        final_summary: str | None = None,
        error: str | None = None,
    ) -> Any: ...

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


class CreateRunLedger(Protocol):
    def create_run(self, user_instruction: str) -> str: ...

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


RUN_CREATED_EVENT_TYPE = "run_created"
RUN_CREATED_MESSAGE = "Run created."


@dataclass(frozen=True)
class CreateRunResult:
    ok: bool
    run_id: str | None = None
    user_instruction: str | None = None
    initial_status: str | None = None
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class HumanDecisionResult:
    ok: bool
    run_id: str
    decision: str
    previous_status: str | None = None
    next_status: str | None = None
    event_type: str | None = None
    event_id: int | None = None
    reason_code: str | None = None
    error_message: str | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class _HumanDecisionSpec:
    allowed_statuses: frozenset[str]
    next_status: RunStatus
    event_type: str
    message: str
    rejected_event_type: str
    action_label: str


_HUMAN_DECISION_SPECS: dict[HumanDecision, _HumanDecisionSpec] = {
    HumanDecision.APPROVE: _HumanDecisionSpec(
        allowed_statuses=frozenset(
            {
                RunStatus.WAITING_FOR_APPROVAL.value,
                RunStatus.NEEDS_REVIEW.value,
            }
        ),
        next_status=RunStatus.APPROVED,
        event_type="human_approval",
        message="Run approved by user.",
        rejected_event_type="human_approval_rejected_by_state",
        action_label="approve",
    ),
    HumanDecision.REJECT: _HumanDecisionSpec(
        allowed_statuses=frozenset(
            {
                RunStatus.WAITING_FOR_APPROVAL.value,
                RunStatus.NEEDS_REVIEW.value,
            }
        ),
        next_status=RunStatus.REJECTED,
        event_type="human_rejection",
        message="Run rejected by user.",
        rejected_event_type="human_rejection_rejected_by_state",
        action_label="reject",
    ),
    HumanDecision.COMPLETE_REVIEW: _HumanDecisionSpec(
        allowed_statuses=frozenset({RunStatus.NEEDS_REVIEW.value}),
        next_status=RunStatus.COMPLETED,
        event_type="human_review_completed",
        message="Run review completed by user.",
        rejected_event_type="human_review_completion_rejected_by_state",
        action_label="complete review for",
    ),
}


def create_run_service(
    user_instruction: str,
    *,
    ledger: CreateRunLedger = default_ledger,
) -> CreateRunResult:
    """Create a run and record the same creation event as the CLI start command."""

    try:
        run_id = ledger.create_run(user_instruction)
    except Exception as exc:
        return CreateRunResult(
            ok=False,
            user_instruction=user_instruction,
            reason_code="run_create_failed",
            error_message=str(exc),
        )

    event_id = _event_id(
        ledger.add_event(
            run_id,
            RUN_CREATED_EVENT_TYPE,
            RUN_CREATED_MESSAGE,
        )
    )
    return CreateRunResult(
        ok=True,
        run_id=run_id,
        user_instruction=user_instruction,
        initial_status=RunStatus.CREATED.value,
        event_type=RUN_CREATED_EVENT_TYPE,
        event_id=event_id,
        message=RUN_CREATED_MESSAGE,
        metadata=None,
    )


def resolve_human_decision(
    run_id: str,
    decision: HumanDecision | str,
    *,
    note: str = "",
    ledger: HumanDecisionLedger = default_ledger,
) -> HumanDecisionResult:
    """Apply an approve/reject/complete-review decision to a run."""

    normalized_decision = _normalize_human_decision(decision)
    decision_value = _decision_value(decision)
    if normalized_decision is None:
        return HumanDecisionResult(
            ok=False,
            run_id=run_id,
            decision=decision_value,
            reason_code="unknown_decision",
            error_message=f"Unknown human decision: {decision_value}",
        )

    spec = _HUMAN_DECISION_SPECS[normalized_decision]
    run = ledger.get_run(run_id)
    if run is None:
        return HumanDecisionResult(
            ok=False,
            run_id=run_id,
            decision=normalized_decision.value,
            reason_code="run_not_found",
            error_message=f"Run not found: {run_id}",
        )

    previous_status = str(run["status"])
    if previous_status not in spec.allowed_statuses:
        allowed_statuses_text = ", ".join(sorted(spec.allowed_statuses))
        error_message = (
            f"Cannot {spec.action_label} run from current status {previous_status!r}. "
            f"Allowed statuses: {allowed_statuses_text}."
        )
        metadata = {"current_status": previous_status, "note": note}
        event_id = _event_id(
            ledger.add_event(
                run_id,
                spec.rejected_event_type,
                error_message,
                metadata=metadata,
            )
        )
        return HumanDecisionResult(
            ok=False,
            run_id=run_id,
            decision=normalized_decision.value,
            previous_status=previous_status,
            event_type=spec.rejected_event_type,
            event_id=event_id,
            reason_code="invalid_status",
            error_message=error_message,
            message=error_message,
            metadata=metadata,
        )

    ledger.update_run_status(
        run_id,
        spec.next_status,
        final_summary=run.get("final_summary"),
        error=run.get("error"),
    )
    metadata = {
        "previous_status": previous_status,
        "next_status": spec.next_status.value,
        "note": note,
    }
    event_id = _event_id(
        ledger.add_event(
            run_id,
            spec.event_type,
            spec.message,
            metadata=metadata,
        )
    )
    return HumanDecisionResult(
        ok=True,
        run_id=run_id,
        decision=normalized_decision.value,
        previous_status=previous_status,
        next_status=spec.next_status.value,
        event_type=spec.event_type,
        event_id=event_id,
        message=spec.message,
        metadata=metadata,
    )


def _normalize_human_decision(decision: HumanDecision | str) -> HumanDecision | None:
    value = _decision_value(decision)
    if value == "complete-review":
        value = HumanDecision.COMPLETE_REVIEW.value
    try:
        return HumanDecision(value)
    except ValueError:
        return None


def _decision_value(decision: HumanDecision | str) -> str:
    if isinstance(decision, HumanDecision):
        return decision.value
    return str(decision)


def _event_id(add_event_result: Any) -> int | None:
    if isinstance(add_event_result, int):
        return add_event_result
    if isinstance(add_event_result, dict):
        event_id = add_event_result.get("id")
        if isinstance(event_id, int):
            return event_id
    return None
