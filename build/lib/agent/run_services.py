"""Reusable run services shared by CLI and future local controllers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Protocol

from agent import ledger as default_ledger
from agent.chatgpt_destination_gate import (
    ChatGPTDestinationReadOnlyAdapter,
    DestinationGateResult,
    DestinationLeaseContext,
    destination_gate_failure,
    verify_chatgpt_destination_snapshot,
)
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


class DestinationBindingLedger(Protocol):
    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def bind_run_destination(
        self,
        run_id: str,
        project_title: str,
        chat_title: str,
    ) -> Any: ...


class ExecutionProfileLedger(Protocol):
    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def bind_run_execution_profile(
        self,
        run_id: str,
        sandbox: str,
        model: str,
        reasoning_effort: str,
        approval_policy: str,
        profile_source: str,
    ) -> Any: ...


class ChatGPTUILeaseLedger(Protocol):
    def list_chatgpt_ui_lease_events(self) -> list[dict[str, Any]]: ...

    def acquire_chatgpt_ui_lease(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ) -> Any: ...

    def release_chatgpt_ui_lease(
        self,
        lease_token: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ) -> Any: ...


class ChatGPTDestinationGateLedger(Protocol):
    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def list_chatgpt_ui_lease_events(self) -> list[dict[str, Any]]: ...


class ChatGPTHandoffQueueLedger(Protocol):
    def list_chatgpt_handoff_queue_events(self) -> list[dict[str, Any]]: ...

    def enqueue_chatgpt_handoff(
        self,
        run_id: str,
        *,
        enqueue_source: str,
    ) -> Any: ...

    def claim_next_chatgpt_handoff(
        self,
        *,
        claim_owner_identifier: str,
    ) -> Any: ...

    def complete_chatgpt_handoff(
        self,
        queue_sequence: int,
        *,
        claim_owner_identifier: str,
        reason_code: str,
        lease_correlation: dict[str, object] | None = None,
    ) -> Any: ...

    def block_chatgpt_handoff(
        self,
        queue_sequence: int,
        *,
        claim_owner_identifier: str,
        reason_code: str,
        lease_correlation: dict[str, object] | None = None,
    ) -> Any: ...


RUN_CREATED_EVENT_TYPE = "run_created"
RUN_CREATED_MESSAGE = "Run created."
RUN_DESTINATION_BOUND_EVENT_TYPE = default_ledger.RUN_DESTINATION_BOUND_EVENT_TYPE
RUN_DESTINATION_BOUND_MESSAGE = default_ledger.RUN_DESTINATION_BOUND_MESSAGE
RUN_DESTINATION_BOUND_SCHEMA_VERSION = default_ledger.RUN_DESTINATION_BOUND_SCHEMA_VERSION
RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE = (
    default_ledger.RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE
)
RUN_EXECUTION_PROFILE_SELECTED_MESSAGE = (
    default_ledger.RUN_EXECUTION_PROFILE_SELECTED_MESSAGE
)
RUN_EXECUTION_PROFILE_SCHEMA_VERSION = default_ledger.RUN_EXECUTION_PROFILE_SCHEMA_VERSION
CODEX_DEFAULT_SELECTION = default_ledger.CODEX_DEFAULT_SELECTION
ALLOWED_EXECUTION_PROFILE_SANDBOXES = (
    default_ledger.ALLOWED_EXECUTION_PROFILE_SANDBOXES
)
ALLOWED_CODEX_MODEL_SELECTIONS = default_ledger.ALLOWED_CODEX_MODEL_SELECTIONS
ALLOWED_REASONING_EFFORT_SELECTIONS = (
    default_ledger.ALLOWED_REASONING_EFFORT_SELECTIONS
)
ALLOWED_APPROVAL_POLICY_SELECTIONS = (
    default_ledger.ALLOWED_APPROVAL_POLICY_SELECTIONS
)
ALLOWED_EXECUTION_PROFILE_SOURCES = default_ledger.ALLOWED_EXECUTION_PROFILE_SOURCES
CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE = (
    default_ledger.CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE
)
CHATGPT_UI_LEASE_ACQUIRED_MESSAGE = (
    default_ledger.CHATGPT_UI_LEASE_ACQUIRED_MESSAGE
)
CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE = (
    default_ledger.CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE
)
CHATGPT_UI_LEASE_RELEASED_MESSAGE = (
    default_ledger.CHATGPT_UI_LEASE_RELEASED_MESSAGE
)
CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE = (
    default_ledger.CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE
)
CHATGPT_UI_LEASE_ACQUIRE_DENIED_MESSAGE = (
    default_ledger.CHATGPT_UI_LEASE_ACQUIRE_DENIED_MESSAGE
)
CHATGPT_UI_LEASE_SCHEMA_VERSION = default_ledger.CHATGPT_UI_LEASE_SCHEMA_VERSION
CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION = (
    default_ledger.CHATGPT_HANDOFF_QUEUE_SCHEMA_VERSION
)
CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE = (
    default_ledger.CHATGPT_HANDOFF_ENQUEUED_EVENT_TYPE
)
CHATGPT_HANDOFF_ENQUEUED_MESSAGE = default_ledger.CHATGPT_HANDOFF_ENQUEUED_MESSAGE
CHATGPT_HANDOFF_CLAIMED_EVENT_TYPE = (
    default_ledger.CHATGPT_HANDOFF_CLAIMED_EVENT_TYPE
)
CHATGPT_HANDOFF_CLAIMED_MESSAGE = default_ledger.CHATGPT_HANDOFF_CLAIMED_MESSAGE
CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE = (
    default_ledger.CHATGPT_HANDOFF_COMPLETED_EVENT_TYPE
)
CHATGPT_HANDOFF_COMPLETED_MESSAGE = default_ledger.CHATGPT_HANDOFF_COMPLETED_MESSAGE
CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE = default_ledger.CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE
CHATGPT_HANDOFF_BLOCKED_MESSAGE = default_ledger.CHATGPT_HANDOFF_BLOCKED_MESSAGE
CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_EVENT_TYPE = (
    default_ledger.CHATGPT_HANDOFF_CLAIM_UNAVAILABLE_EVENT_TYPE
)
CHATGPT_HANDOFF_CLAIM_DENIED_EVENT_TYPE = (
    default_ledger.CHATGPT_HANDOFF_CLAIM_DENIED_EVENT_TYPE
)
CHATGPT_HANDOFF_QUEUE_EVENT_TYPES = default_ledger.CHATGPT_HANDOFF_QUEUE_EVENT_TYPES
CHATGPT_HANDOFF_QUEUE_STATE_EVENT_TYPES = (
    default_ledger.CHATGPT_HANDOFF_QUEUE_STATE_EVENT_TYPES
)


class DestinationBindingLookupStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"


class ExecutionProfileLookupStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"


class ChatGPTUILeaseLookupStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    INVALID = "invalid"


class ChatGPTHandoffQueueItemStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ChatGPTHandoffQueueLookupStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"


def execution_profile_options() -> dict[str, Any]:
    """Return UI-safe execution profile choices from validation constants."""

    return {
        "sandbox_options": list(ALLOWED_EXECUTION_PROFILE_SANDBOXES),
        "model_options": list(ALLOWED_CODEX_MODEL_SELECTIONS),
        "reasoning_effort": CODEX_DEFAULT_SELECTION,
        "approval_policy": CODEX_DEFAULT_SELECTION,
    }


@dataclass(frozen=True)
class RunDestinationBinding:
    project_title: str
    chat_title: str

    def __post_init__(self) -> None:
        if not isinstance(self.project_title, str):
            raise TypeError("project_title must be a string")
        if not isinstance(self.chat_title, str):
            raise TypeError("chat_title must be a string")

        project_title = self.project_title.strip()
        chat_title = self.chat_title.strip()
        if project_title == "":
            raise ValueError("project_title must not be empty")
        if chat_title == "":
            raise ValueError("chat_title must not be empty")

        object.__setattr__(self, "project_title", project_title)
        object.__setattr__(self, "chat_title", chat_title)


@dataclass(frozen=True)
class RunExecutionProfile:
    sandbox: str
    model: str = CODEX_DEFAULT_SELECTION
    reasoning_effort: str = CODEX_DEFAULT_SELECTION
    approval_policy: str = CODEX_DEFAULT_SELECTION
    profile_source: str = "system_default"

    def __post_init__(self) -> None:
        _require_allowed_value(
            "sandbox",
            self.sandbox,
            ALLOWED_EXECUTION_PROFILE_SANDBOXES,
        )
        _require_allowed_value("model", self.model, ALLOWED_CODEX_MODEL_SELECTIONS)
        _require_allowed_value(
            "reasoning_effort",
            self.reasoning_effort,
            ALLOWED_REASONING_EFFORT_SELECTIONS,
        )
        _require_allowed_value(
            "approval_policy",
            self.approval_policy,
            ALLOWED_APPROVAL_POLICY_SELECTIONS,
        )
        _require_allowed_value(
            "profile_source",
            self.profile_source,
            ALLOWED_EXECUTION_PROFILE_SOURCES,
        )


@dataclass(frozen=True)
class ChatGPTUILeaseOwner:
    owning_run_id: str
    owner_pid: int
    acquired_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.owning_run_id, str):
            raise TypeError("owning_run_id must be a string")
        if not isinstance(self.owner_pid, int):
            raise TypeError("owner_pid must be an integer")
        if not isinstance(self.acquired_at, str):
            raise TypeError("acquired_at must be a string")

        owning_run_id = self.owning_run_id.strip()
        acquired_at = self.acquired_at.strip()
        if owning_run_id == "":
            raise ValueError("owning_run_id must not be empty")
        if acquired_at == "":
            raise ValueError("acquired_at must not be empty")

        object.__setattr__(self, "owning_run_id", owning_run_id)
        object.__setattr__(self, "acquired_at", acquired_at)


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
    destination_binding: RunDestinationBinding | None = None
    destination_event_id: int | None = None
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DestinationBindingLookupResult:
    status: DestinationBindingLookupStatus
    run_id: str
    binding: RunDestinationBinding | None = None
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BindRunDestinationResult:
    ok: bool
    run_id: str
    binding: RunDestinationBinding | None = None
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ExecutionProfileLookupResult:
    status: ExecutionProfileLookupStatus
    run_id: str
    profile: RunExecutionProfile | None = None
    legacy_compatibility_profile: RunExecutionProfile | None = None
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SelectRunExecutionProfileResult:
    ok: bool
    run_id: str
    profile: RunExecutionProfile | None = None
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChatGPTUILeaseLookupResult:
    status: ChatGPTUILeaseLookupStatus
    active_owner: ChatGPTUILeaseOwner | None = None
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChatGPTHandoffQueueItem:
    run_id: str
    queue_entry_id: str
    queue_sequence: int
    status: ChatGPTHandoffQueueItemStatus
    enqueue_source: str
    claim_owner_identifier: str | None = None
    terminal_outcome: str | None = None
    terminal_reason_code: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChatGPTHandoffQueueLookupResult:
    status: ChatGPTHandoffQueueLookupStatus
    items: tuple[ChatGPTHandoffQueueItem, ...] = ()
    run_id: str | None = None
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChatGPTHandoffQueueOperationResult:
    ok: bool
    run_id: str | None = None
    queue_entry_id: str | None = None
    queue_sequence: int | None = None
    status: str | None = None
    enqueue_source: str | None = None
    claim_owner_identifier: str | None = None
    terminal_outcome: str | None = None
    terminal_reason_code: str | None = None
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AcquireChatGPTUILeaseResult:
    ok: bool
    run_id: str
    lease_token: str | None = None
    owner: ChatGPTUILeaseOwner | None = None
    active_owner: ChatGPTUILeaseOwner | None = None
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReleaseChatGPTUILeaseResult:
    ok: bool
    lease_token: str
    owner: ChatGPTUILeaseOwner | None = None
    active_owner: ChatGPTUILeaseOwner | None = None
    event_type: str | None = None
    event_id: int | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    event_written: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


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
    project_title: str | None = None,
    chat_title: str | None = None,
    ledger: CreateRunLedger = default_ledger,
) -> CreateRunResult:
    """Create a run and record the same creation event as the CLI start command."""

    destination_binding: RunDestinationBinding | None = None
    if (project_title is None) != (chat_title is None):
        return CreateRunResult(
            ok=False,
            user_instruction=user_instruction,
            reason_code="partial_destination",
            error_message="Both project_title and chat_title are required together.",
        )
    if project_title is not None and chat_title is not None:
        try:
            destination_binding = RunDestinationBinding(project_title, chat_title)
        except (TypeError, ValueError) as exc:
            return CreateRunResult(
                ok=False,
                user_instruction=user_instruction,
                reason_code="invalid_destination",
                error_message=str(exc),
            )

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
    destination_event_id = None
    if destination_binding is not None:
        destination_event_id = _event_id(
            ledger.add_event(
                run_id,
                RUN_DESTINATION_BOUND_EVENT_TYPE,
                RUN_DESTINATION_BOUND_MESSAGE,
                metadata=_destination_binding_metadata(destination_binding),
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
        destination_binding=destination_binding,
        destination_event_id=destination_event_id,
    )


def bind_run_destination(
    run_id: str,
    project_title: str,
    chat_title: str,
    *,
    ledger: DestinationBindingLedger = default_ledger,
) -> BindRunDestinationResult:
    """Durably bind a run to one exact Project/chat destination."""

    try:
        requested_binding = RunDestinationBinding(project_title, chat_title)
    except (TypeError, ValueError) as exc:
        return BindRunDestinationResult(
            ok=False,
            run_id=run_id,
            reason_code="invalid_destination",
            error_message=str(exc),
        )

    ledger_result = ledger.bind_run_destination(
        run_id,
        requested_binding.project_title,
        requested_binding.chat_title,
    )
    status = str(ledger_result.status)

    if status == default_ledger.AtomicDestinationBindingStatus.RUN_NOT_FOUND:
        return BindRunDestinationResult(
            ok=False,
            run_id=run_id,
            reason_code="run_not_found",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicDestinationBindingStatus.INVALID:
        return BindRunDestinationResult(
            ok=False,
            run_id=run_id,
            reason_code="destination_binding_invalid",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicDestinationBindingStatus.DIFFERENT_DESTINATION:
        existing_binding = _destination_binding_from_ledger_result(ledger_result)
        return BindRunDestinationResult(
            ok=False,
            run_id=run_id,
            binding=existing_binding,
            reason_code="destination_already_bound_to_different_destination",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicDestinationBindingStatus.OPERATIONAL_FAILURE:
        return BindRunDestinationResult(
            ok=False,
            run_id=run_id,
            reason_code=ledger_result.reason_code or "destination_binding_failed",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicDestinationBindingStatus.IDEMPOTENT:
        existing_binding = _destination_binding_from_ledger_result(ledger_result)
        metadata = _destination_binding_metadata(existing_binding)
        return BindRunDestinationResult(
            ok=True,
            run_id=run_id,
            binding=existing_binding,
            event_type=RUN_DESTINATION_BOUND_EVENT_TYPE,
            message=RUN_DESTINATION_BOUND_MESSAGE,
            metadata=metadata,
            event_written=False,
        )

    if status != default_ledger.AtomicDestinationBindingStatus.BOUND:
        return BindRunDestinationResult(
            ok=False,
            run_id=run_id,
            reason_code="destination_binding_failed",
            error_message=f"Unknown destination binding ledger status: {status}",
        )

    binding = _destination_binding_from_ledger_result(ledger_result)
    metadata = _destination_binding_metadata(binding)
    return BindRunDestinationResult(
        ok=True,
        run_id=run_id,
        binding=binding,
        event_type=RUN_DESTINATION_BOUND_EVENT_TYPE,
        event_id=ledger_result.event_id,
        message=RUN_DESTINATION_BOUND_MESSAGE,
        metadata=metadata,
        event_written=bool(ledger_result.event_written),
    )


def get_run_destination_binding(
    run_id: str,
    *,
    ledger: DestinationBindingLedger = default_ledger,
) -> DestinationBindingLookupResult:
    """Reconstruct a run destination binding solely from durable ledger events."""

    binding_events = [
        event
        for event in ledger.list_events(run_id)
        if event.get("event_type") == RUN_DESTINATION_BOUND_EVENT_TYPE
    ]
    event_ids = tuple(
        event_id for event in binding_events if (event_id := _raw_event_id(event)) is not None
    )
    if not binding_events:
        return DestinationBindingLookupResult(
            status=DestinationBindingLookupStatus.MISSING,
            run_id=run_id,
        )

    bindings: list[RunDestinationBinding] = []
    for event in binding_events:
        metadata = _metadata_from_event(event)
        binding = _destination_binding_from_metadata(metadata)
        if binding is None:
            return DestinationBindingLookupResult(
                status=DestinationBindingLookupStatus.INVALID,
                run_id=run_id,
                reason_code="malformed_destination_binding_event",
                error_message="Run destination binding event metadata is malformed.",
                event_ids=event_ids,
            )
        bindings.append(binding)

    unique_bindings = frozenset(bindings)
    if len(unique_bindings) != 1:
        return DestinationBindingLookupResult(
            status=DestinationBindingLookupStatus.INVALID,
            run_id=run_id,
            reason_code="contradictory_destination_binding_events",
            error_message="Run destination binding events contain conflicting destinations.",
            event_ids=event_ids,
        )

    return DestinationBindingLookupResult(
        status=DestinationBindingLookupStatus.PRESENT,
        run_id=run_id,
        binding=bindings[0],
        event_ids=event_ids,
    )


def select_run_execution_profile(
    run_id: str,
    profile: RunExecutionProfile,
    *,
    ledger: ExecutionProfileLedger = default_ledger,
) -> SelectRunExecutionProfileResult:
    """Durably select one immutable execution profile for a run."""

    if not isinstance(profile, RunExecutionProfile):
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            reason_code="invalid_execution_profile",
            error_message="profile must be a RunExecutionProfile",
        )

    ledger_result = ledger.bind_run_execution_profile(
        run_id,
        profile.sandbox,
        profile.model,
        profile.reasoning_effort,
        profile.approval_policy,
        profile.profile_source,
    )
    status = str(ledger_result.status)

    if status == default_ledger.AtomicExecutionProfileStatus.RUN_NOT_FOUND:
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            reason_code="run_not_found",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicExecutionProfileStatus.INVALID:
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            reason_code=ledger_result.reason_code or "execution_profile_invalid",
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicExecutionProfileStatus.EXECUTION_STARTED:
        existing_profile = _execution_profile_from_ledger_result(ledger_result)
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            profile=existing_profile,
            reason_code=(
                ledger_result.reason_code
                or "execution_profile_immutable_after_codex_exec_started"
            ),
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicExecutionProfileStatus.DIFFERENT_PROFILE:
        existing_profile = _execution_profile_from_ledger_result(ledger_result)
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            profile=existing_profile,
            reason_code=(
                ledger_result.reason_code
                or "execution_profile_already_selected_different_profile"
            ),
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicExecutionProfileStatus.OPERATIONAL_FAILURE:
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            reason_code=ledger_result.reason_code or "execution_profile_failed",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicExecutionProfileStatus.IDEMPOTENT:
        existing_profile = _execution_profile_from_ledger_result(ledger_result)
        metadata = _execution_profile_metadata(existing_profile)
        return SelectRunExecutionProfileResult(
            ok=True,
            run_id=run_id,
            profile=existing_profile,
            event_type=RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
            message=RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
            metadata=metadata,
            event_written=False,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status != default_ledger.AtomicExecutionProfileStatus.SELECTED:
        return SelectRunExecutionProfileResult(
            ok=False,
            run_id=run_id,
            reason_code="execution_profile_failed",
            error_message=f"Unknown execution profile ledger status: {status}",
        )

    selected_profile = _execution_profile_from_ledger_result(ledger_result)
    metadata = _execution_profile_metadata(selected_profile)
    return SelectRunExecutionProfileResult(
        ok=True,
        run_id=run_id,
        profile=selected_profile,
        event_type=RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
        event_id=ledger_result.event_id,
        message=RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
        metadata=metadata,
        event_written=bool(ledger_result.event_written),
    )


def get_run_execution_profile(
    run_id: str,
    *,
    ledger: ExecutionProfileLedger = default_ledger,
) -> ExecutionProfileLookupResult:
    """Reconstruct a run execution profile solely from durable ledger events."""

    profile_events = [
        event
        for event in ledger.list_events(run_id)
        if event.get("event_type") == RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE
    ]
    event_ids = tuple(
        event_id for event in profile_events if (event_id := _raw_event_id(event)) is not None
    )
    if not profile_events:
        return ExecutionProfileLookupResult(
            status=ExecutionProfileLookupStatus.MISSING,
            run_id=run_id,
            legacy_compatibility_profile=_legacy_compatibility_execution_profile(),
        )

    profiles: list[RunExecutionProfile] = []
    for event in profile_events:
        metadata = _metadata_from_event(event)
        profile = _execution_profile_from_metadata(metadata)
        if profile is None:
            return ExecutionProfileLookupResult(
                status=ExecutionProfileLookupStatus.INVALID,
                run_id=run_id,
                reason_code="malformed_execution_profile_event",
                error_message="Run execution profile event metadata is malformed.",
                event_ids=event_ids,
            )
        profiles.append(profile)

    unique_profiles = frozenset(profiles)
    if len(unique_profiles) != 1:
        return ExecutionProfileLookupResult(
            status=ExecutionProfileLookupStatus.INVALID,
            run_id=run_id,
            reason_code="contradictory_execution_profile_events",
            error_message="Run execution profile events contain conflicting profiles.",
            event_ids=event_ids,
        )

    return ExecutionProfileLookupResult(
        status=ExecutionProfileLookupStatus.PRESENT,
        run_id=run_id,
        profile=profiles[0],
        event_ids=event_ids,
    )


def acquire_chatgpt_ui_lease(
    run_id: str,
    *,
    reason: str | None = None,
    source: str | None = None,
    ledger: ChatGPTUILeaseLedger = default_ledger,
) -> AcquireChatGPTUILeaseResult:
    """Acquire the process-global ChatGPT Desktop UI lease for a local run."""

    if not isinstance(run_id, str) or run_id.strip() == "":
        return AcquireChatGPTUILeaseResult(
            ok=False,
            run_id=str(run_id),
            reason_code="invalid_run_id",
            error_message="run_id must be a non-empty string",
        )
    normalized_reason = _optional_text(reason)
    normalized_source = _optional_text(source)
    if normalized_reason is False or normalized_source is False:
        return AcquireChatGPTUILeaseResult(
            ok=False,
            run_id=run_id,
            reason_code="invalid_lease_metadata",
            error_message="reason and source must be strings when provided",
        )

    ledger_result = ledger.acquire_chatgpt_ui_lease(
        run_id.strip(),
        reason=normalized_reason,
        source=normalized_source,
    )
    status = str(ledger_result.status)

    if status == default_ledger.AtomicChatGPTUILeaseStatus.ACQUIRED:
        owner = _chatgpt_ui_lease_owner_from_ledger_result(ledger_result)
        metadata = _chatgpt_ui_lease_acquired_metadata(
            ledger_result.lease_token,
            owner,
            reason=normalized_reason,
            source=normalized_source,
        )
        return AcquireChatGPTUILeaseResult(
            ok=True,
            run_id=run_id.strip(),
            lease_token=ledger_result.lease_token,
            owner=owner,
            event_type=CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
            event_id=ledger_result.event_id,
            message=CHATGPT_UI_LEASE_ACQUIRED_MESSAGE,
            metadata=metadata,
            event_written=bool(ledger_result.event_written),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.ALREADY_HELD:
        active_owner = _chatgpt_ui_lease_owner_from_ledger_result(ledger_result)
        return AcquireChatGPTUILeaseResult(
            ok=False,
            run_id=run_id.strip(),
            active_owner=active_owner,
            event_type=CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE,
            event_id=ledger_result.event_id,
            message=CHATGPT_UI_LEASE_ACQUIRE_DENIED_MESSAGE,
            event_written=bool(ledger_result.event_written),
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_already_held",
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.RUN_NOT_FOUND:
        return AcquireChatGPTUILeaseResult(
            ok=False,
            run_id=run_id.strip(),
            reason_code="run_not_found",
            error_message=ledger_result.error_message,
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.INVALID:
        return AcquireChatGPTUILeaseResult(
            ok=False,
            run_id=run_id.strip(),
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_invalid",
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.OPERATIONAL_FAILURE:
        return AcquireChatGPTUILeaseResult(
            ok=False,
            run_id=run_id.strip(),
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_acquire_failed",
            error_message=ledger_result.error_message,
        )

    return AcquireChatGPTUILeaseResult(
        ok=False,
        run_id=run_id.strip(),
        reason_code="chatgpt_ui_lease_acquire_failed",
        error_message=f"Unknown ChatGPT UI lease ledger status: {status}",
    )


def release_chatgpt_ui_lease(
    lease_token: str,
    *,
    reason: str | None = None,
    source: str | None = None,
    ledger: ChatGPTUILeaseLedger = default_ledger,
) -> ReleaseChatGPTUILeaseResult:
    """Release the process-global ChatGPT Desktop UI lease with its token."""

    if not isinstance(lease_token, str) or lease_token.strip() == "":
        return ReleaseChatGPTUILeaseResult(
            ok=False,
            lease_token=str(lease_token),
            reason_code="invalid_lease_token",
            error_message="lease_token must be a non-empty string",
        )
    normalized_reason = _optional_text(reason)
    normalized_source = _optional_text(source)
    if normalized_reason is False or normalized_source is False:
        return ReleaseChatGPTUILeaseResult(
            ok=False,
            lease_token=lease_token,
            reason_code="invalid_lease_metadata",
            error_message="reason and source must be strings when provided",
        )

    token = lease_token.strip()
    ledger_result = ledger.release_chatgpt_ui_lease(
        token,
        reason=normalized_reason,
        source=normalized_source,
    )
    status = str(ledger_result.status)

    if status == default_ledger.AtomicChatGPTUILeaseStatus.RELEASED:
        owner = _chatgpt_ui_lease_owner_from_ledger_result(ledger_result)
        metadata = _chatgpt_ui_lease_released_metadata(
            token,
            owner,
            ledger_result.released_at,
            reason=normalized_reason,
            source=normalized_source,
        )
        return ReleaseChatGPTUILeaseResult(
            ok=True,
            lease_token=token,
            owner=owner,
            event_type=CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
            event_id=ledger_result.event_id,
            message=CHATGPT_UI_LEASE_RELEASED_MESSAGE,
            metadata=metadata,
            event_written=bool(ledger_result.event_written),
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.IDEMPOTENT_RELEASE:
        return ReleaseChatGPTUILeaseResult(
            ok=True,
            lease_token=token,
            event_written=False,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.TOKEN_MISMATCH:
        active_owner = _chatgpt_ui_lease_owner_from_ledger_result(ledger_result)
        return ReleaseChatGPTUILeaseResult(
            ok=False,
            lease_token=token,
            active_owner=active_owner,
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_token_mismatch",
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.MISSING:
        return ReleaseChatGPTUILeaseResult(
            ok=False,
            lease_token=token,
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_not_active",
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.INVALID:
        return ReleaseChatGPTUILeaseResult(
            ok=False,
            lease_token=token,
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_invalid",
            error_message=ledger_result.error_message,
            event_ids=tuple(ledger_result.event_ids),
        )

    if status == default_ledger.AtomicChatGPTUILeaseStatus.OPERATIONAL_FAILURE:
        return ReleaseChatGPTUILeaseResult(
            ok=False,
            lease_token=token,
            reason_code=ledger_result.reason_code or "chatgpt_ui_lease_release_failed",
            error_message=ledger_result.error_message,
        )

    return ReleaseChatGPTUILeaseResult(
        ok=False,
        lease_token=token,
        reason_code="chatgpt_ui_lease_release_failed",
        error_message=f"Unknown ChatGPT UI lease ledger status: {status}",
    )


def get_chatgpt_ui_lease(
    *,
    ledger: ChatGPTUILeaseLedger = default_ledger,
) -> ChatGPTUILeaseLookupResult:
    """Return the non-sensitive current ChatGPT Desktop UI lease state."""

    lease_events = [
        event
        for event in ledger.list_chatgpt_ui_lease_events()
        if event.get("event_type")
        in {
            CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
            CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
        }
    ]
    return _reconstruct_chatgpt_ui_lease_lookup(lease_events)


def verify_chatgpt_destination_for_run(
    run_id: str,
    lease_context: DestinationLeaseContext | None,
    adapter: ChatGPTDestinationReadOnlyAdapter | None = None,
    *,
    adapter_factory: Callable[[], ChatGPTDestinationReadOnlyAdapter] | None = None,
    ledger: ChatGPTDestinationGateLedger = default_ledger,
) -> DestinationGateResult:
    """Fail-closed service entry point for exact ChatGPT destination proof."""

    binding_lookup = get_run_destination_binding(run_id, ledger=ledger)
    if binding_lookup.status == DestinationBindingLookupStatus.MISSING:
        return destination_gate_failure(
            run_id=run_id,
            reason_code="destination_binding_missing",
            binding=None,
            lease_context=lease_context,
        )
    if binding_lookup.status == DestinationBindingLookupStatus.INVALID:
        return destination_gate_failure(
            run_id=run_id,
            reason_code="destination_binding_invalid",
            binding=None,
            lease_context=lease_context,
        )

    binding = binding_lookup.binding
    if (
        binding is None
        or lease_context is None
        or lease_context.owning_run_id != run_id
        or lease_context.lease_token.strip() == ""
    ):
        return destination_gate_failure(
            run_id=run_id,
            reason_code="destination_lease_invalid_or_mismatched",
            binding=binding,
            lease_context=lease_context,
        )

    lease_lookup = get_chatgpt_ui_lease(ledger=ledger)
    if (
        lease_lookup.status != ChatGPTUILeaseLookupStatus.ACTIVE
        or lease_lookup.active_owner is None
        or lease_lookup.active_owner.owning_run_id != lease_context.owning_run_id
        or lease_lookup.active_owner.owner_pid != lease_context.owner_pid
        or lease_lookup.active_owner.acquired_at != lease_context.acquired_at
        or not _active_chatgpt_ui_lease_token_matches(lease_context, ledger)
    ):
        return destination_gate_failure(
            run_id=run_id,
            reason_code="destination_lease_invalid_or_mismatched",
            binding=binding,
            lease_context=lease_context,
        )

    if adapter is None:
        if adapter_factory is None:
            return destination_gate_failure(
                run_id=run_id,
                reason_code="destination_verification_unavailable",
                binding=binding,
                lease_context=lease_context,
            )
        try:
            adapter = adapter_factory()
        except Exception:
            return destination_gate_failure(
                run_id=run_id,
                reason_code="destination_verification_unavailable",
                binding=binding,
                lease_context=lease_context,
            )

    return verify_chatgpt_destination_snapshot(
        run_id=run_id,
        binding=binding,
        lease_context=lease_context,
        adapter=adapter,
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


def _destination_binding_metadata(binding: RunDestinationBinding) -> dict[str, Any]:
    return {
        "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
        "project_title": binding.project_title,
        "chat_title": binding.chat_title,
    }


def _execution_profile_metadata(profile: RunExecutionProfile) -> dict[str, Any]:
    return {
        "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
        "sandbox": profile.sandbox,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "approval_policy": profile.approval_policy,
        "profile_source": profile.profile_source,
    }


def _chatgpt_ui_lease_acquired_metadata(
    lease_token: str | None,
    owner: ChatGPTUILeaseOwner,
    *,
    reason: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    lease_token_sha256 = (
        default_ledger.chatgpt_ui_lease_token_fingerprint(lease_token)
        if isinstance(lease_token, str) and lease_token
        else None
    )
    return _compact_optional_metadata(
        {
            "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
            "lease_token_sha256": lease_token_sha256,
            "owner_pid": owner.owner_pid,
            "owning_run_id": owner.owning_run_id,
            "acquired_at": owner.acquired_at,
            "reason": reason,
            "source": source,
        }
    )


def _chatgpt_ui_lease_released_metadata(
    lease_token: str,
    owner: ChatGPTUILeaseOwner,
    released_at: str | None,
    *,
    reason: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return _compact_optional_metadata(
        {
            "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
            "lease_token_sha256": default_ledger.chatgpt_ui_lease_token_fingerprint(
                lease_token
            ),
            "owner_pid": owner.owner_pid,
            "owning_run_id": owner.owning_run_id,
            "acquired_at": owner.acquired_at,
            "released_at": released_at,
            "reason": reason,
            "source": source,
        }
    )


def _destination_binding_from_ledger_result(
    ledger_result: Any,
) -> RunDestinationBinding:
    return RunDestinationBinding(
        ledger_result.project_title,
        ledger_result.chat_title,
    )


def _execution_profile_from_ledger_result(
    ledger_result: Any,
) -> RunExecutionProfile | None:
    if ledger_result.sandbox is None:
        return None
    return RunExecutionProfile(
        ledger_result.sandbox,
        ledger_result.model,
        ledger_result.reasoning_effort,
        ledger_result.approval_policy,
        ledger_result.profile_source,
    )


def _chatgpt_ui_lease_owner_from_ledger_result(
    ledger_result: Any,
) -> ChatGPTUILeaseOwner:
    return ChatGPTUILeaseOwner(
        ledger_result.owning_run_id,
        ledger_result.owner_pid,
        ledger_result.acquired_at,
    )


def _metadata_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata

    metadata_json = event.get("metadata_json")
    if not isinstance(metadata_json, str):
        return None
    try:
        decoded = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _reconstruct_chatgpt_ui_lease_lookup(
    lease_events: list[dict[str, Any]],
) -> ChatGPTUILeaseLookupResult:
    event_ids = tuple(
        event_id for event in lease_events if (event_id := _raw_event_id(event)) is not None
    )
    if not lease_events:
        return ChatGPTUILeaseLookupResult(
            status=ChatGPTUILeaseLookupStatus.MISSING,
        )

    active_token_sha256: str | None = None
    active_owner: ChatGPTUILeaseOwner | None = None
    acquired_by_token_sha256: dict[str, ChatGPTUILeaseOwner] = {}

    for event in lease_events:
        event_type = event.get("event_type")
        if event_type == CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE:
            metadata = _metadata_from_event(event)
            parsed = _chatgpt_ui_lease_acquire_from_metadata(metadata, event)
            if parsed is None:
                return ChatGPTUILeaseLookupResult(
                    status=ChatGPTUILeaseLookupStatus.INVALID,
                    reason_code="malformed_chatgpt_ui_lease_acquire_event",
                    error_message=(
                        "ChatGPT Desktop UI lease acquire event metadata is malformed."
                    ),
                    event_ids=event_ids,
                )
            lease_token_sha256, owner = parsed
            if lease_token_sha256 in acquired_by_token_sha256:
                return ChatGPTUILeaseLookupResult(
                    status=ChatGPTUILeaseLookupStatus.INVALID,
                    reason_code="duplicate_chatgpt_ui_lease_acquire_token",
                    error_message=(
                        "ChatGPT Desktop UI lease history contains a duplicate lease token."
                    ),
                    event_ids=event_ids,
                )
            if active_token_sha256 is not None:
                return ChatGPTUILeaseLookupResult(
                    status=ChatGPTUILeaseLookupStatus.INVALID,
                    reason_code="contradictory_active_chatgpt_ui_lease_events",
                    error_message=(
                        "ChatGPT Desktop UI lease history contains multiple active leases."
                    ),
                    event_ids=event_ids,
                )
            acquired_by_token_sha256[lease_token_sha256] = owner
            active_token_sha256 = lease_token_sha256
            active_owner = owner
            continue

        if event_type == CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE:
            metadata = _metadata_from_event(event)
            parsed = _chatgpt_ui_lease_release_from_metadata(metadata, event)
            if parsed is None:
                return ChatGPTUILeaseLookupResult(
                    status=ChatGPTUILeaseLookupStatus.INVALID,
                    reason_code="malformed_chatgpt_ui_lease_release_event",
                    error_message=(
                        "ChatGPT Desktop UI lease release event metadata is malformed."
                    ),
                    event_ids=event_ids,
                )
            lease_token_sha256, owner = parsed
            acquired_owner = acquired_by_token_sha256.get(lease_token_sha256)
            if acquired_owner is None:
                return ChatGPTUILeaseLookupResult(
                    status=ChatGPTUILeaseLookupStatus.INVALID,
                    reason_code="chatgpt_ui_lease_release_without_acquire",
                    error_message=(
                        "ChatGPT Desktop UI lease release event has no matching acquire event."
                    ),
                    event_ids=event_ids,
                )
            if owner != acquired_owner:
                return ChatGPTUILeaseLookupResult(
                    status=ChatGPTUILeaseLookupStatus.INVALID,
                    reason_code="contradictory_chatgpt_ui_lease_release_event",
                    error_message=(
                        "ChatGPT Desktop UI lease release event does not match its acquire event."
                    ),
                    event_ids=event_ids,
                )
            if active_token_sha256 == lease_token_sha256:
                active_token_sha256 = None
                active_owner = None
            continue

    if active_owner is None:
        return ChatGPTUILeaseLookupResult(
            status=ChatGPTUILeaseLookupStatus.MISSING,
            event_ids=event_ids,
        )
    return ChatGPTUILeaseLookupResult(
        status=ChatGPTUILeaseLookupStatus.ACTIVE,
        active_owner=active_owner,
        event_ids=event_ids,
    )


def _active_chatgpt_ui_lease_token_matches(
    lease_context: DestinationLeaseContext,
    ledger: ChatGPTUILeaseLedger,
) -> bool:
    active_token_sha256: str | None = None
    for event in ledger.list_chatgpt_ui_lease_events():
        event_type = event.get("event_type")
        if event_type == CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE:
            parsed = _chatgpt_ui_lease_acquire_from_metadata(
                _metadata_from_event(event),
                event,
            )
            if parsed is None:
                return False
            active_token_sha256, _owner = parsed
            continue
        if event_type == CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE:
            parsed = _chatgpt_ui_lease_release_from_metadata(
                _metadata_from_event(event),
                event,
            )
            if parsed is None:
                return False
            lease_token_sha256, _owner = parsed
            if active_token_sha256 == lease_token_sha256:
                active_token_sha256 = None
    return active_token_sha256 == default_ledger.chatgpt_ui_lease_token_fingerprint(
        lease_context.lease_token
    )


def _chatgpt_ui_lease_acquire_from_metadata(
    metadata: dict[str, Any] | None,
    event: dict[str, Any],
) -> tuple[str, ChatGPTUILeaseOwner] | None:
    if metadata is None:
        return None
    if metadata.get("schema_version") != CHATGPT_UI_LEASE_SCHEMA_VERSION:
        return None
    lease_token_sha256 = _lease_token_sha256_from_metadata(metadata)
    owner = _chatgpt_ui_lease_owner_from_metadata(metadata, event)
    if not isinstance(lease_token_sha256, str) or owner is None:
        return None
    return lease_token_sha256, owner


def _chatgpt_ui_lease_release_from_metadata(
    metadata: dict[str, Any] | None,
    event: dict[str, Any],
) -> tuple[str, ChatGPTUILeaseOwner] | None:
    if metadata is None:
        return None
    if metadata.get("schema_version") != CHATGPT_UI_LEASE_SCHEMA_VERSION:
        return None
    lease_token_sha256 = _lease_token_sha256_from_metadata(metadata)
    released_at = metadata.get("released_at")
    owner = _chatgpt_ui_lease_owner_from_metadata(metadata, event)
    if not isinstance(lease_token_sha256, str) or owner is None:
        return None
    if not isinstance(released_at, str) or released_at.strip() == "":
        return None
    return lease_token_sha256, owner


def _lease_token_sha256_from_metadata(metadata: dict[str, Any]) -> str | None:
    fingerprint = metadata.get("lease_token_sha256")
    if isinstance(fingerprint, str) and _valid_sha256(fingerprint):
        return fingerprint
    historical_raw = metadata.get("lease_token")
    if isinstance(historical_raw, str) and historical_raw:
        return default_ledger.chatgpt_ui_lease_token_fingerprint(historical_raw)
    return None


def _valid_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _chatgpt_ui_lease_owner_from_metadata(
    metadata: dict[str, Any],
    event: dict[str, Any],
) -> ChatGPTUILeaseOwner | None:
    try:
        owner = ChatGPTUILeaseOwner(
            metadata["owning_run_id"],
            metadata["owner_pid"],
            metadata["acquired_at"],
        )
    except (KeyError, TypeError, ValueError):
        return None
    if event.get("run_id") != owner.owning_run_id:
        return None
    return owner


def _destination_binding_from_metadata(
    metadata: dict[str, Any] | None,
) -> RunDestinationBinding | None:
    if metadata is None:
        return None
    if metadata.get("schema_version") != RUN_DESTINATION_BOUND_SCHEMA_VERSION:
        return None
    try:
        return RunDestinationBinding(
            metadata["project_title"],
            metadata["chat_title"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _execution_profile_from_metadata(
    metadata: dict[str, Any] | None,
) -> RunExecutionProfile | None:
    if metadata is None:
        return None
    expected_keys = {
        "schema_version",
        "sandbox",
        "model",
        "reasoning_effort",
        "approval_policy",
        "profile_source",
    }
    if set(metadata) != expected_keys:
        return None
    if metadata.get("schema_version") != RUN_EXECUTION_PROFILE_SCHEMA_VERSION:
        return None
    try:
        return RunExecutionProfile(
            metadata["sandbox"],
            metadata["model"],
            metadata["reasoning_effort"],
            metadata["approval_policy"],
            metadata["profile_source"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _legacy_compatibility_execution_profile() -> RunExecutionProfile:
    return RunExecutionProfile(
        "read-only",
        CODEX_DEFAULT_SELECTION,
        CODEX_DEFAULT_SELECTION,
        CODEX_DEFAULT_SELECTION,
        "legacy_compatibility",
    )


def _require_allowed_value(
    field_name: str,
    value: object,
    allowed_values: tuple[str, ...],
) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value not in allowed_values:
        allowed_text = ", ".join(allowed_values)
        raise ValueError(f"{field_name} must be one of: {allowed_text}")


def _raw_event_id(event: dict[str, Any]) -> int | None:
    event_id = event.get("id")
    if isinstance(event_id, int):
        return event_id
    return None


def _event_id(add_event_result: Any) -> int | None:
    if isinstance(add_event_result, int):
        return add_event_result
    if isinstance(add_event_result, dict):
        event_id = add_event_result.get("id")
        if isinstance(event_id, int):
            return event_id
    return None


def _optional_text(value: str | None) -> str | None | bool:
    if value is None:
        return None
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return stripped or None


def _compact_optional_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if value is not None and value != ""
    }
