from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.codex_account_limits import fetch_account_rate_limits_resets_at
from agent.codex_quota_resume_services import execute_codex_quota_resume_service
from agent.codex_quota_wait import (
    CODEX_QUOTA_WAIT_CANCELLED_EVENT_TYPE,
    CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
    active_quota_wait,
    decide_quota_wait,
    looks_like_usage_limit,
    quota_wait_client_message,
    quota_wait_count,
    quota_wait_fields,
)
from agent.codex_terminal import terminate_codex_run
from agent.initial_codex_run_services import execute_initial_direct_codex_run_service
from agent.run_services import (
    ALLOWED_CODEX_MODEL_SELECTIONS,
    CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE,
    CODEX_DEFAULT_SELECTION,
    DestinationBindingLookupStatus,
    ExecutionProfileLookupStatus,
    RunDestinationBinding,
    RunExecutionProfile,
    bind_run_destination,
    create_run_service,
    get_run_destination_binding,
    get_run_execution_profile,
    select_run_execution_profile,
)
from agent.run_state import RunStatus
from agent.supervise import SuperviseAction, SupervisePlan, detect_next_supervise_action
from agent.supervision_services import run_supervision_step as run_supervision_step_service
from agent.supervision_services import send_plan_auto_safe


LOCAL_CONTROLLER_ALLOWED_SANDBOXES = (
    "read-only",
    "workspace-write",
    "danger-full-access",
)
LOCAL_CONTROLLER_DEFAULT_SANDBOX = "read-only"
LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE = "local_controller_run_started"
LOCAL_CONTROLLER_RUN_STARTED_MESSAGE = "Local controller run initialized."
LOCAL_CONTROLLER_RUN_START_FAILED_EVENT_TYPE = "local_controller_run_start_failed"
LOCAL_CONTROLLER_RUN_START_FAILED_MESSAGE = "Local controller run failed to initialize."
LOCAL_CONTROLLER_METADATA_VERSION = "local_controller_v1"
LOCAL_CONTROLLER_SOURCE = "local_controller"
LOCAL_CONTROLLER_MODE = "browser_v1"
LOCAL_CONTROLLER_INITIAL_STATE = "idle"
LOCAL_CONTROLLER_STATE_IDLE = "idle"
LOCAL_CONTROLLER_STATE_STARTING_INITIAL_CODEX = "starting_initial_codex"
LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION = "running_routine_action"
LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL = "waiting_for_approval"
LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY = "waiting_for_retry"
LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET = "waiting_for_quota_reset"
LOCAL_CONTROLLER_STATE_BLOCKED = "blocked"
LOCAL_CONTROLLER_STATE_FAILED = "failed"
LOCAL_CONTROLLER_STATE_COMPLETED = "completed"
LOCAL_CONTROLLER_TERMINAL_STATES = (
    LOCAL_CONTROLLER_STATE_BLOCKED,
    LOCAL_CONTROLLER_STATE_FAILED,
    LOCAL_CONTROLLER_STATE_COMPLETED,
)
LOCAL_CONTROLLER_INITIAL_CODEX_TIMEOUT_SECONDS: float | None = None
LOCAL_CONTROLLER_ACTION_FAILED_EVENT_TYPE = "local_controller_action_failed"
LOCAL_CONTROLLER_ACTION_FAILED_MESSAGE = "Controller action paused after failure."
LOCAL_CONTROLLER_RETRY_REQUESTED_EVENT_TYPE = "local_controller_retry_requested"
LOCAL_CONTROLLER_RETRY_REQUESTED_MESSAGE = "Manual retry requested for failed controller action."
LOCAL_CONTROLLER_FAILURE_RESOLVED_EVENT_TYPE = "local_controller_failure_resolved"
LOCAL_CONTROLLER_FAILURE_RESOLVED_MESSAGE = "Failed controller action succeeded after manual retry."
LOCAL_CONTROLLER_RETRY_STATUS_RESTORED_EVENT_TYPE = "local_controller_retry_status_restored"
LOCAL_CONTROLLER_RETRY_SCHEMA_VERSION = 1
LOCAL_CONTROLLER_CANCEL_REQUESTED_EVENT_TYPE = "local_controller_cancel_requested"
LOCAL_CONTROLLER_CANCEL_REQUESTED_MESSAGE = "Operator requested cancellation."

EVENT_METADATA_PREVIEW_LIMIT = 1200
TEXT_METADATA_PREVIEW_LIMIT = 240
TOKEN_ENTROPY_BYTES = 32
EXCEPTION_MESSAGE_PREVIEW_LIMIT = 500
STALE_LEASE_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.REJECTED.value,
        RunStatus.NEEDS_REVIEW.value,
    }
)


@dataclass(frozen=True)
class StartRequestValidationResult:
    ok: bool
    repository_path: str | None = None
    initial_instruction: str | None = None
    sandbox: str | None = None
    model: str | None = None
    profile_source: str | None = None
    project_title: str | None = None
    chat_title: str | None = None
    allow_destination_navigation: bool = False
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class PendingApprovalSnapshot:
    run_id: str
    approval_kind: str
    planner_action: str
    planner_reason_code: str
    planner_metadata: dict[str, Any]
    latest_event_id: int
    expected_extraction_event_id: int | None
    expected_prompt_sha256: str | None
    expected_prompt_text_sha256: str | None
    expected_extraction_method: str | None
    created_at: str


@dataclass
class LocalControllerSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token: str = field(default_factory=lambda: secrets.token_urlsafe(TOKEN_ENTROPY_BYTES))
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    active_run_id: str | None = None
    controller_state: str = LOCAL_CONTROLLER_INITIAL_STATE
    pending_approval: PendingApprovalSnapshot | None = None


@dataclass(frozen=True)
class LocalControllerRunStartResult:
    ok: bool
    run_id: str | None = None
    repository_path: str | None = None
    sandbox: str | None = None
    initial_instruction: str | None = None
    event_type: str | None = None
    message: str | None = None
    metadata: dict[str, Any] | None = None
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class LocalControllerEventTimelineRow:
    event_id: int
    timestamp: str
    event_type: str
    message: str
    metadata_preview: dict[str, Any]
    full_metadata_available: bool


@dataclass(frozen=True)
class LocalControllerReadModel:
    run_id: str
    run_status: str | None
    initial_instruction: str | None
    repository_path: str | None
    sandbox: str | None
    execution_profile: dict[str, Any] | None
    destination_binding: dict[str, Any] | None
    allow_destination_navigation: bool
    latest_handoff_phase: dict[str, Any] | None
    latest_event_id: int
    planner_action: str | None
    planner_reason_code: str | None
    planner_metadata: dict[str, Any]
    current_stage: str
    routine_action_available: bool
    requires_human_approval: bool
    approval_kind: str | None
    terminal: bool
    blocked: bool
    completed: bool
    actionable_error_message: str | None
    latest_codex_result: dict[str, Any] | None
    latest_chatgpt_submission: dict[str, Any] | None
    latest_chatgpt_capture: dict[str, Any] | None
    latest_prompt_extraction: dict[str, Any] | None
    latest_governance: dict[str, Any] | None
    event_timeline: list[LocalControllerEventTimelineRow]
    controller_runtime: dict[str, Any]
    configuration_complete: bool
    latest_failure: dict[str, Any] | None = None
    quota_wait: dict[str, Any] | None = None


@dataclass(frozen=True)
class PendingApprovalSnapshotResult:
    ok: bool
    snapshot: PendingApprovalSnapshot | None = None
    reason_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class InitialRunExecutionResult:
    ok: bool
    reason_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalControllerOperationResult:
    ok: bool
    reason_code: str | None = None
    error_message: str | None = None
    run_id: str | None = None
    controller_state: str | None = None
    read_model: LocalControllerReadModel | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _ControllerFailureResult:
    ok: bool
    reason_code: str
    error_message: str
    retryable_override: bool | None = None
    action_executed: bool = False
    blocked: bool = True
    planner_action: str | None = None
    planner_reason_code: str | None = None
    next_state_hint: str = "waiting_for_retry"
    run_status: str | None = None
    planner_metadata: dict[str, Any] = field(default_factory=dict)


def default_initial_run_executor(
    *,
    run_id: str,
    run: dict[str, Any] | None,
    initial_instruction: str,
    repository_path: str,
    sandbox: str,
    timeout_seconds: float | None = None,
    ledger: Any = default_ledger,
) -> Any:
    del timeout_seconds
    return execute_initial_direct_codex_run_service(
        run_id,
        run or {"id": run_id, "status": ""},
        initial_instruction,
        repository_path,
        sandbox,
        LOCAL_CONTROLLER_INITIAL_CODEX_TIMEOUT_SECONDS,
        confirm_full_access=sandbox == "danger-full-access",
        ledger=ledger,
    )


class LocalController:
    def __init__(
        self,
        *,
        session: LocalControllerSession | None = None,
        ledger: Any = default_ledger,
        read_model_builder: Callable[..., LocalControllerReadModel] | None = None,
        supervision_step: Callable[..., Any] = run_supervision_step_service,
        initial_run_executor: Callable[..., Any] | None = None,
        quota_resume_executor: Callable[..., Any] | None = None,
        quota_wait_now: Callable[[], datetime] | None = None,
        rate_limits_reader: Callable[..., datetime | None] | None = None,
    ) -> None:
        restore_persisted_session = session is None
        self.session = session or LocalControllerSession()
        self.ledger = ledger
        self.read_model_builder = read_model_builder or build_local_controller_read_model
        self.run_supervision_step = supervision_step
        self.initial_run_executor = initial_run_executor or (
            lambda **kwargs: default_initial_run_executor(**kwargs, ledger=self.ledger)
        )
        self._lock = threading.Lock()
        self.cancel_requested = threading.Event()
        self.action_running = False
        self.current_worker: threading.Thread | None = None
        self.current_action_kind: str | None = None
        self.current_action_started_at: str | None = None
        self.last_action_result_summary: dict[str, Any] | None = None
        self.last_exception_summary: dict[str, Any] | None = None
        self.automatic_burst_count = 0
        self.automatic_burst_reason: str | None = None
        self._quota_wait: dict[str, Any] | None = None
        self._quota_wait_timer: threading.Timer | None = None
        self.quota_wait_now = quota_wait_now or (lambda: datetime.now(UTC))
        self.rate_limits_reader = rate_limits_reader or fetch_account_rate_limits_resets_at
        self.quota_resume_executor = quota_resume_executor or (
            lambda **kwargs: execute_codex_quota_resume_service(**kwargs, ledger=self.ledger)
        )
        if restore_persisted_session:
            self._restore_persisted_session()

    @property
    def controller_state(self) -> str:
        return self.session.controller_state

    def start_run(
        self,
        *,
        repository_path: str,
        initial_instruction: str,
        project_title: str,
        chat_title: str,
        sandbox: str | None = None,
        model: str | None = None,
        allow_destination_navigation: bool = False,
        timeout_seconds: float | None = None,
    ) -> LocalControllerOperationResult:
        del timeout_seconds
        validation = validate_local_controller_start_request(
            repository_path,
            initial_instruction,
            sandbox,
            model=model,
            project_title=project_title,
            chat_title=chat_title,
            allow_destination_navigation=allow_destination_navigation,
        )
        if not validation.ok:
            return LocalControllerOperationResult(
                ok=False,
                reason_code=validation.reason_code,
                error_message=validation.error_message,
                controller_state=self.session.controller_state,
            )

        with self._lock:
            if self.session.active_run_id is not None:
                active_run_id = self.session.active_run_id
                active_run = self.ledger.get_run(active_run_id)
                active_status = (
                    str(active_run.get("status") or "")
                    if isinstance(active_run, dict)
                    else ""
                )
                if (
                    self.action_running
                    or active_status
                    not in {
                        RunStatus.COMPLETED.value,
                        RunStatus.FAILED.value,
                        RunStatus.NEEDS_REVIEW.value,
                        RunStatus.REJECTED.value,
                    }
                ):
                    return LocalControllerOperationResult(
                        ok=False,
                        reason_code="active_run_exists",
                        error_message="A local controller run is already active.",
                        run_id=active_run_id,
                        controller_state=self.session.controller_state,
                    )
                self.session.active_run_id = None
                self.session.pending_approval = None
                self.session.controller_state = LOCAL_CONTROLLER_STATE_IDLE
                self._persist_session_locked()

            start_result = start_local_controller_run(
                self.session,
                validation,
                ledger=self.ledger,
            )
            if not start_result.ok:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code=start_result.reason_code,
                    error_message=start_result.error_message,
                    controller_state=self.session.controller_state,
                )

            run_id = start_result.run_id
            if run_id is None:
                raise ValueError("start_local_controller_run returned no run_id")
            self.session.controller_state = LOCAL_CONTROLLER_STATE_STARTING_INITIAL_CODEX
            self.session.pending_approval = None
            self.cancel_requested.clear()
            self._mark_action_running_locked("initial_codex")
            self.last_exception_summary = None
            self.last_action_result_summary = {
                "kind": "start_run",
                "reason_code": "local_controller_run_started",
            }
            worker = self._new_worker(
                self._initial_worker,
                run_id,
                validation.initial_instruction,
                validation.repository_path,
                validation.sandbox,
                None,
            )
            self.current_worker = worker
            self._persist_session_locked()
            worker.start()

        return LocalControllerOperationResult(
            ok=True,
            reason_code="started",
            run_id=run_id,
            controller_state=self.session.controller_state,
            read_model=self.get_current_state(run_id).read_model,
            metadata={"start_event_type": LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE},
        )

    def submit_approval_decision(self, decision: str) -> LocalControllerOperationResult:
        if decision not in {"approved", "rejected"}:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="invalid_approval_decision",
                error_message="Invalid approval decision. Expected approved or rejected.",
                controller_state=self.session.controller_state,
            )

        with self._lock:
            if self.action_running:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="action_already_running",
                    error_message="A controller action is already running.",
                    run_id=self.session.active_run_id,
                    controller_state=self.session.controller_state,
                )
            if (
                self.session.active_run_id is None
                or self.session.controller_state != LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
                or self.session.pending_approval is None
            ):
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="no_pending_approval",
                    error_message="No pending approval is available.",
                    run_id=self.session.active_run_id,
                    controller_state=self.session.controller_state,
                )

            snapshot = self.session.pending_approval
            self.session.controller_state = LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION
            self._mark_action_running_locked(f"approval_{snapshot.approval_kind}")
            self.last_exception_summary = None
            worker = self._new_worker(self._approval_worker, snapshot, decision)
            self.current_worker = worker
            worker.start()

        return LocalControllerOperationResult(
            ok=True,
            reason_code="approval_worker_started",
            run_id=snapshot.run_id,
            controller_state=self.session.controller_state,
            read_model=self.get_current_state(snapshot.run_id).read_model,
        )

    def request_automatic_progress(self) -> LocalControllerOperationResult:
        with self._lock:
            run_id = self.session.active_run_id
            if run_id is None:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="no_active_run",
                    error_message="No active run is available.",
                    controller_state=self.session.controller_state,
                )
            if self.action_running:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="action_already_running",
                    error_message="A controller action is already running.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            if self.session.pending_approval is not None:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="pending_approval_exists",
                    error_message="A pending approval must be handled before progress can continue.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            if self.session.controller_state == LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="manual_retry_required",
                    error_message="The last failed action is paused for an explicit manual retry.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            if self.session.controller_state == LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="waiting_for_quota_reset",
                    error_message="Codex is waiting for the usage-limit reset before resuming.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            if self.session.controller_state in LOCAL_CONTROLLER_TERMINAL_STATES:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="controller_state_terminal",
                    error_message="Controller state is terminal for automatic work.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )

        state = self.get_current_state(run_id)
        read_model = state.read_model
        if read_model is None:
            return state
        if read_model.requires_human_approval:
            self._store_pending_approval(read_model)
            return LocalControllerOperationResult(
                ok=False,
                reason_code="human_approval_required",
                error_message="Human approval is required before progress can continue.",
                run_id=run_id,
                controller_state=self.session.controller_state,
                read_model=self.get_current_state(run_id).read_model,
            )
        if not read_model.routine_action_available or read_model.blocked or read_model.completed or read_model.terminal:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="no_routine_action_available",
                error_message="No routine action is currently available.",
                run_id=run_id,
                controller_state=self.session.controller_state,
                read_model=read_model,
            )

        with self._lock:
            if self.action_running:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="action_already_running",
                    error_message="A controller action is already running.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            if self.session.pending_approval is not None:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="pending_approval_exists",
                    error_message="A pending approval must be handled before progress can continue.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            self.session.controller_state = LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION
            self._mark_action_running_locked("routine_progress")
            worker = self._new_worker(self._routine_worker, run_id, 0)
            self.current_worker = worker
            worker.start()

        return LocalControllerOperationResult(
            ok=True,
            reason_code="routine_worker_started",
            run_id=run_id,
            controller_state=self.session.controller_state,
            read_model=self.get_current_state(run_id).read_model,
        )

    def request_cancel(self) -> LocalControllerOperationResult:
        with self._lock:
            run_id = self.session.active_run_id
            if run_id is None:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="no_active_run",
                    error_message="No active run is available.",
                    controller_state=self.session.controller_state,
                )
            waiting_for_quota = (
                self.session.controller_state == LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET
            )
            self.cancel_requested.set()
            self.session.pending_approval = None
            self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
            self.automatic_burst_reason = "operator_cancelled"
            if waiting_for_quota:
                self._cancel_quota_wait_timer_locked()
                self._quota_wait = None
            self._persist_session_locked()

        if waiting_for_quota:
            self.ledger.add_event(
                run_id,
                CODEX_QUOTA_WAIT_CANCELLED_EVENT_TYPE,
                "Cancelled Codex usage-limit wait before resume.",
                {
                    "source": LOCAL_CONTROLLER_SOURCE,
                    "controller_mode": LOCAL_CONTROLLER_MODE,
                },
            )
            try:
                self.ledger.update_run_status(
                    run_id,
                    RunStatus.NEEDS_REVIEW,
                    error="Codex usage-limit wait cancelled by operator.",
                )
            except (AttributeError, TypeError):
                pass
            return LocalControllerOperationResult(
                ok=True,
                reason_code="quota_wait_cancelled",
                run_id=run_id,
                controller_state=LOCAL_CONTROLLER_STATE_BLOCKED,
                read_model=self.get_current_state(run_id).read_model,
            )

        termination = terminate_codex_run(run_id)
        self.ledger.add_event(
            run_id,
            LOCAL_CONTROLLER_CANCEL_REQUESTED_EVENT_TYPE,
            LOCAL_CONTROLLER_CANCEL_REQUESTED_MESSAGE,
            {
                "source": LOCAL_CONTROLLER_SOURCE,
                "controller_mode": LOCAL_CONTROLLER_MODE,
                "process_termination": termination,
            },
        )
        try:
            self.ledger.update_run_status(
                run_id,
                RunStatus.FAILED,
                error="Run cancelled by operator.",
            )
        except (AttributeError, TypeError):
            pass
        return LocalControllerOperationResult(
            ok=True,
            reason_code="cancel_requested",
            run_id=run_id,
            controller_state=LOCAL_CONTROLLER_STATE_BLOCKED,
            read_model=self.get_current_state(run_id).read_model,
            metadata={"process_termination": termination},
        )

    def retry_failed_action(self, failure_event_id: int) -> LocalControllerOperationResult:
        if isinstance(failure_event_id, bool) or not isinstance(failure_event_id, int) or failure_event_id <= 0:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="invalid_failure_event_id",
                error_message="failure_event_id must be a positive integer.",
                controller_state=self.session.controller_state,
            )

        with self._lock:
            run_id = self.session.active_run_id
            if run_id is None:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="no_active_run",
                    error_message="No active run is available.",
                    controller_state=self.session.controller_state,
                )
            if self.action_running:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="action_already_running",
                    error_message="A controller action is already running.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            failure = _latest_unresolved_action_failure(
                self.ledger.list_events(run_id),
            )
            if failure is None:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="no_retryable_failure",
                    error_message="No unresolved controller action failure is available.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                )
            if failure["event_id"] != failure_event_id:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="stale_failure_retry",
                    error_message="The requested failure is no longer the latest unresolved failure.",
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                    metadata={"latest_failure": failure},
                )
            if failure.get("retryable") is not True:
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="failure_requires_reconciliation",
                    error_message=str(
                        failure.get("recovery_message")
                        or "This failure cannot be retried safely without review."
                    ),
                    run_id=run_id,
                    controller_state=self.session.controller_state,
                    metadata={"latest_failure": failure},
                )

            restore_failure = self._restore_retryable_run_status(run_id, failure)
            if restore_failure is not None:
                return restore_failure

            self.ledger.add_event(
                run_id,
                LOCAL_CONTROLLER_RETRY_REQUESTED_EVENT_TYPE,
                LOCAL_CONTROLLER_RETRY_REQUESTED_MESSAGE,
                {
                    "schema_version": LOCAL_CONTROLLER_RETRY_SCHEMA_VERSION,
                    "failure_event_id": failure_event_id,
                    "action_key": failure.get("action_key"),
                    "reason_code": failure.get("reason_code"),
                    "requested_at": datetime.now(UTC).isoformat(),
                    "source": "local_dashboard",
                },
            )

            self.session.controller_state = LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION
            self.session.pending_approval = None
            self._mark_action_running_locked(f"retry:{failure.get('action_key') or 'failed_action'}")
            worker = self._new_worker(
                self._retry_worker,
                run_id,
                failure_event_id,
                failure,
            )
            self.current_worker = worker
            worker.start()

        return LocalControllerOperationResult(
            ok=True,
            reason_code="retry_worker_started",
            run_id=run_id,
            controller_state=self.session.controller_state,
            metadata={
                "failure_event_id": failure_event_id,
                "action_key": failure.get("action_key"),
            },
        )

    def _restore_retryable_run_status(
        self,
        run_id: str,
        failure: dict[str, Any],
    ) -> LocalControllerOperationResult | None:
        run = self.ledger.get_run(run_id)
        if not isinstance(run, dict):
            return LocalControllerOperationResult(
                ok=False,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
                run_id=run_id,
                controller_state=self.session.controller_state,
            )
        current_status = str(run.get("status") or "")
        previous_status = str(failure.get("run_status_before_action") or "")
        failure_status = str(failure.get("run_status_after_action") or "")
        if current_status != RunStatus.NEEDS_REVIEW.value:
            return None
        if (
            failure_status != RunStatus.NEEDS_REVIEW.value
            or previous_status not in {RunStatus.COMPLETED.value, RunStatus.APPROVED.value}
        ):
            return LocalControllerOperationResult(
                ok=False,
                reason_code="retry_status_restore_unsafe",
                error_message=(
                    "The current needs_review status cannot be tied safely to "
                    "this failed action, so it was not changed."
                ),
                run_id=run_id,
                controller_state=self.session.controller_state,
                metadata={"latest_failure": failure},
            )
        update_status = getattr(self.ledger, "update_run_status", None)
        if not callable(update_status):
            return LocalControllerOperationResult(
                ok=False,
                reason_code="retry_status_restore_unavailable",
                error_message="Run status restoration is unavailable.",
                run_id=run_id,
                controller_state=self.session.controller_state,
            )
        update_status(
            run_id,
            RunStatus(previous_status),
            final_summary=run.get("final_summary"),
            error=None,
        )
        self.ledger.add_event(
            run_id,
            LOCAL_CONTROLLER_RETRY_STATUS_RESTORED_EVENT_TYPE,
            "Retry restored the pre-failure run status.",
            {
                "schema_version": LOCAL_CONTROLLER_RETRY_SCHEMA_VERSION,
                "failure_event_id": failure.get("event_id"),
                "previous_status": current_status,
                "restored_status": previous_status,
            },
        )
        return None

    def get_chatgpt_ui_lease_status(self) -> LocalControllerOperationResult:
        lease = _chatgpt_ui_lease_status(ledger=self.ledger)
        return LocalControllerOperationResult(
            ok=True,
            reason_code="chatgpt_ui_lease_status_loaded",
            controller_state=self.session.controller_state,
            metadata={"chatgpt_ui_lease": lease},
        )

    def release_stale_chatgpt_ui_lease(
        self,
        *,
        owning_run_id: str,
        owner_pid: int,
        acquired_at: str,
        active_event_id: int,
        expected_lease_token_sha256: str,
        expected_run_status: str | None,
        confirm_stale: bool,
        reason: str,
        allow_owner_pid_alive: bool = False,
    ) -> LocalControllerOperationResult:
        if not confirm_stale:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="manual_stale_lease_confirmation_required",
                error_message="Manual stale ChatGPT UI lease release requires operator confirmation.",
                controller_state=self.session.controller_state,
                metadata={"chatgpt_ui_lease": _chatgpt_ui_lease_status(ledger=self.ledger)},
            )

        reason_text = str(reason or "").strip()
        if not reason_text:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="manual_stale_lease_reason_required",
                error_message="Manual stale ChatGPT UI lease release requires a human-readable reason.",
                controller_state=self.session.controller_state,
                metadata={"chatgpt_ui_lease": _chatgpt_ui_lease_status(ledger=self.ledger)},
            )

        lease = _chatgpt_ui_lease_status(ledger=self.ledger)
        mismatch = _lease_release_request_mismatch(
            lease,
            owning_run_id=owning_run_id,
            owner_pid=owner_pid,
            acquired_at=acquired_at,
            active_event_id=active_event_id,
            expected_lease_token_sha256=expected_lease_token_sha256,
            expected_run_status=expected_run_status,
        )
        if mismatch is not None:
            return LocalControllerOperationResult(
                ok=False,
                reason_code=mismatch,
                error_message="Active ChatGPT UI lease no longer matches the displayed lease.",
                controller_state=self.session.controller_state,
                metadata={"chatgpt_ui_lease": lease},
            )

        owner_pid_state = lease.get("owner_pid_state")
        if owner_pid_state == "alive" and not allow_owner_pid_alive:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="chatgpt_ui_lease_owner_pid_alive",
                error_message="Owner PID currently appears alive; manual release requires explicit PID-reuse override.",
                controller_state=self.session.controller_state,
                metadata={"chatgpt_ui_lease": lease},
            )
        if owner_pid_state == "unknown":
            return LocalControllerOperationResult(
                ok=False,
                reason_code="chatgpt_ui_lease_owner_pid_unknown",
                error_message="Owner PID liveness is unknown; manual release is not available from the dashboard.",
                controller_state=self.session.controller_state,
                metadata={"chatgpt_ui_lease": lease},
            )

        if lease.get("owning_run_status") not in STALE_LEASE_TERMINAL_RUN_STATUSES:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="chatgpt_ui_lease_owner_run_not_terminal",
                error_message="Owner run is not in a terminal or review state.",
                controller_state=self.session.controller_state,
                metadata={"chatgpt_ui_lease": lease},
            )

        result = self.ledger.manual_release_stale_chatgpt_ui_lease(
            owning_run_id=owning_run_id,
            owner_pid=owner_pid,
            acquired_at=acquired_at,
            active_event_id=active_event_id,
            expected_run_status=expected_run_status,
            expected_lease_token_sha256=expected_lease_token_sha256,
            reason=reason_text,
            source="local_dashboard_stale_release",
            confirm_stale=True,
        )
        ok = getattr(result, "status", None) == default_ledger.AtomicChatGPTUILeaseStatus.RELEASED
        refreshed = _chatgpt_ui_lease_status(ledger=self.ledger)
        return LocalControllerOperationResult(
            ok=ok,
            reason_code=None if ok else getattr(result, "reason_code", None) or "manual_stale_lease_release_failed",
            error_message=None if ok else getattr(result, "error_message", None),
            run_id=getattr(result, "run_id", None),
            controller_state=self.session.controller_state,
            metadata={
                "chatgpt_ui_lease": refreshed,
                "release": {
                    "status": str(getattr(result, "status", "")),
                    "event_written": bool(getattr(result, "event_written", False)),
                    "event_id": getattr(result, "event_id", None),
                    "run_id": getattr(result, "run_id", None),
                    "owning_run_id": getattr(result, "owning_run_id", None),
                    "owner_pid": getattr(result, "owner_pid", None),
                    "acquired_at": getattr(result, "acquired_at", None),
                    "released_at": getattr(result, "released_at", None),
                    "active_event_id": getattr(result, "active_event_id", None),
                    "run_status": getattr(result, "run_status", None),
                    "reason_code": getattr(result, "reason_code", None),
                    "error_message": getattr(result, "error_message", None),
                },
            },
        )

    def get_current_state(self, run_id: str | None = None) -> LocalControllerOperationResult:
        with self._lock:
            active_run_id = self.session.active_run_id
            target_run_id = run_id or active_run_id
            runtime = self._runtime_snapshot_locked()
        if target_run_id is None:
            return LocalControllerOperationResult(
                ok=False,
                reason_code="no_active_run",
                error_message="No active run is available.",
                controller_state=runtime["controller_state"],
            )

        read_model = self._build_read_model(target_run_id)
        if (
            read_model.requires_human_approval
            and not runtime["pending_approval_available"]
            and not runtime["action_running"]
        ):
            runtime = {
                **runtime,
                "notice": "approval_snapshot_unavailable_after_controller_restart",
            }
        read_model = replace(read_model, controller_runtime=runtime)
        return LocalControllerOperationResult(
            ok=True,
            reason_code="state_loaded",
            run_id=target_run_id,
            controller_state=runtime["controller_state"],
            read_model=read_model,
        )

    def get_current_progress(
        self,
        *,
        after_sequence: int = 0,
        limit: int = default_ledger.CODEX_PROGRESS_DEFAULT_LIMIT,
    ) -> LocalControllerOperationResult:
        with self._lock:
            run_id = self.session.active_run_id
            controller_state = self.session.controller_state
        if run_id is None:
            return LocalControllerOperationResult(
                ok=True,
                reason_code="no_active_run",
                controller_state=controller_state,
                metadata={
                    "progress": _codex_progress_payload(
                        None,
                        [],
                        after_sequence=after_sequence,
                    )
                },
            )
        return self.get_run_progress(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def get_run_progress(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = default_ledger.CODEX_PROGRESS_DEFAULT_LIMIT,
    ) -> LocalControllerOperationResult:
        try:
            events = self.ledger.list_codex_progress_events(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except AttributeError:
            events = []
        return LocalControllerOperationResult(
            ok=True,
            reason_code="progress_loaded",
            run_id=run_id,
            controller_state=self.session.controller_state,
            metadata={
                "progress": _codex_progress_payload(
                    run_id,
                    events,
                    after_sequence=after_sequence,
                )
            },
        )

    def _initial_worker(
        self,
        run_id: str,
        initial_instruction: str,
        repository_path: str,
        sandbox: str,
        timeout_seconds: float | None,
    ) -> None:
        try:
            run = self.ledger.get_run(run_id)
            result = self.initial_run_executor(
                run_id=run_id,
                run=run,
                initial_instruction=initial_instruction,
                repository_path=repository_path,
                sandbox=sandbox,
                timeout_seconds=None,
            )
            if self.cancel_requested.is_set():
                return
            summary = _action_result_summary(result, "initial_run")
            with self._lock:
                self.last_action_result_summary = summary
            if not _result_ok(result):
                if self._maybe_schedule_quota_wait(
                    run_id,
                    result,
                    repository_path=repository_path,
                    sandbox=sandbox,
                ):
                    return
                self._pause_for_action_failure(
                    run_id,
                    action_key="initial_codex",
                    result=result,
                    run_status_before_action=(
                        str(run.get("status") or "") if isinstance(run, dict) else None
                    ),
                    source="initial_worker",
                    retry_context={
                        "repository_path": repository_path,
                        "sandbox": sandbox,
                    },
                )
                return
            self._automatic_progress_loop(run_id, starting_burst_count=0)
        except Exception as exc:
            self._record_worker_exception(
                exc,
                run_id=run_id,
                action_key="initial_codex",
                retry_context={
                    "repository_path": repository_path,
                    "sandbox": sandbox,
                },
            )
        finally:
            self._clear_action_running()

    def _routine_worker(self, run_id: str, starting_burst_count: int) -> None:
        try:
            self._automatic_progress_loop(run_id, starting_burst_count=starting_burst_count)
        except Exception as exc:
            self._record_worker_exception(
                exc,
                run_id=run_id,
                action_key=self.current_action_kind or "routine_progress",
            )
        finally:
            self._clear_action_running()

    def _retry_worker(
        self,
        run_id: str,
        failure_event_id: int,
        failure: dict[str, Any],
    ) -> None:
        try:
            action_key = str(failure.get("action_key") or "")
            if action_key == "initial_codex":
                self._retry_initial_codex(run_id, failure_event_id, failure)
                return

            read_model = self._build_read_model(run_id)
            if not read_model.configuration_complete or not read_model.repository_path or not read_model.sandbox:
                self._pause_for_action_failure(
                    run_id,
                    action_key=action_key or "routine_progress",
                    result=_controller_failure_result(
                        "run_configuration_missing",
                        "Run configuration is missing; retry could not start.",
                    ),
                    run_status_before_action=read_model.run_status,
                    source="manual_retry",
                    supersedes_failure_event_id=failure_event_id,
                )
                return
            if read_model.planner_action != action_key:
                self._pause_for_action_failure(
                    run_id,
                    action_key=action_key or "routine_progress",
                    result=_controller_failure_result(
                        "retry_planner_action_changed",
                        (
                            "The planner no longer selects the failed action "
                            f"(expected {action_key!r}, got {read_model.planner_action!r})."
                        ),
                        retryable=False,
                    ),
                    run_status_before_action=read_model.run_status,
                    source="manual_retry",
                    supersedes_failure_event_id=failure_event_id,
                )
                return

            result = self.run_supervision_step(
                run_id,
                read_model.repository_path,
                read_model.sandbox,
                approval_mode="auto",
                expected_planner_action=action_key,
                expected_event_ids=_planner_event_ids(read_model),
                expected_prompt_sha256=_string_or_none(
                    read_model.planner_metadata.get("prompt_sha")
                ),
                allow_destination_navigation=read_model.allow_destination_navigation,
                ledger=self.ledger,
            )
            if self.cancel_requested.is_set():
                return
            with self._lock:
                self.last_action_result_summary = _action_result_summary(
                    result,
                    "manual_retry",
                )
            if not _result_ok(result) or getattr(result, "blocked", False):
                self._pause_for_action_failure(
                    run_id,
                    action_key=action_key,
                    result=result,
                    run_status_before_action=read_model.run_status,
                    source="manual_retry",
                    supersedes_failure_event_id=failure_event_id,
                )
                return

            self._record_failure_resolved(
                run_id,
                failure_event_id,
                action_key,
                result,
            )
            self._automatic_progress_loop(run_id, starting_burst_count=0)
        except Exception as exc:
            self._record_worker_exception(
                exc,
                run_id=run_id,
                action_key=str(failure.get("action_key") or "manual_retry"),
                retry_context={"retried_failure_event_id": failure_event_id},
                supersedes_failure_event_id=failure_event_id,
            )
        finally:
            self._clear_action_running()

    def _retry_initial_codex(
        self,
        run_id: str,
        failure_event_id: int,
        failure: dict[str, Any],
    ) -> None:
        read_model = self._build_read_model(run_id)
        run = self.ledger.get_run(run_id)
        if (
            not isinstance(run, dict)
            or not read_model.repository_path
            or not read_model.sandbox
            or not isinstance(run.get("user_instruction"), str)
        ):
            self._pause_for_action_failure(
                run_id,
                action_key="initial_codex",
                result=_controller_failure_result(
                    "run_configuration_missing",
                    "Initial Codex retry could not reconstruct the run configuration.",
                ),
                run_status_before_action=read_model.run_status,
                source="manual_retry",
                supersedes_failure_event_id=failure_event_id,
            )
            return
        result = self.initial_run_executor(
            run_id=run_id,
            run=run,
            initial_instruction=run["user_instruction"],
            repository_path=read_model.repository_path,
            sandbox=read_model.sandbox,
            timeout_seconds=None,
        )
        with self._lock:
            self.last_action_result_summary = _action_result_summary(
                result,
                "manual_retry_initial_run",
            )
        if not _result_ok(result):
            self._pause_for_action_failure(
                run_id,
                action_key="initial_codex",
                result=result,
                run_status_before_action=read_model.run_status,
                source="manual_retry",
                retry_context={
                    "repository_path": read_model.repository_path,
                    "sandbox": read_model.sandbox,
                },
                supersedes_failure_event_id=failure_event_id,
            )
            return
        self._record_failure_resolved(
            run_id,
            failure_event_id,
            "initial_codex",
            result,
        )
        self._automatic_progress_loop(run_id, starting_burst_count=0)

    def _approval_worker(self, snapshot: PendingApprovalSnapshot, decision: str) -> None:
        try:
            with self._lock:
                if self.session.pending_approval == snapshot:
                    self.session.pending_approval = None
            read_model = self._build_read_model(snapshot.run_id)
            if not read_model.configuration_complete or not read_model.repository_path or not read_model.sandbox:
                self._pause_for_action_failure(
                    snapshot.run_id,
                    action_key=snapshot.planner_action,
                    result=_controller_failure_result(
                        "run_configuration_missing",
                        "Run configuration is missing; the approved action could not run.",
                    ),
                    run_status_before_action=read_model.run_status,
                    source="approval_decision",
                )
                return
            event_ids = snapshot.planner_metadata.get("event_ids")
            event_ids = event_ids if isinstance(event_ids, dict) else {}
            result = self.run_supervision_step(
                snapshot.run_id,
                read_model.repository_path,
                read_model.sandbox,
                approval_mode="interactive",
                approval_decision=decision,
                expected_planner_action=snapshot.planner_action,
                expected_event_ids=event_ids,
                expected_prompt_sha256=snapshot.expected_prompt_sha256,
                allow_destination_navigation=read_model.allow_destination_navigation,
                ledger=self.ledger,
            )
            if self.cancel_requested.is_set():
                return
            with self._lock:
                self.last_action_result_summary = _action_result_summary(result, "approval_decision")
            if not _result_ok(result) or getattr(result, "blocked", False):
                self._pause_for_action_failure(
                    snapshot.run_id,
                    action_key=snapshot.planner_action,
                    result=result,
                    run_status_before_action=read_model.run_status,
                    source="approval_decision",
                )
                return
            refreshed = self._build_read_model(snapshot.run_id)
            self._commit_state_from_read_model(refreshed, allow_pending_snapshot=False)
        except Exception as exc:
            self._record_worker_exception(
                exc,
                run_id=snapshot.run_id,
                action_key=snapshot.planner_action,
                retry_context={"approval_decision": decision},
            )
        finally:
            self._clear_action_running()

    def _automatic_progress_loop(self, run_id: str, *, starting_burst_count: int) -> None:
        burst_count = starting_burst_count
        while True:
            if self.cancel_requested.is_set():
                return
            read_model = self._build_read_model(run_id)
            if read_model.planner_reason_code == "waiting_for_quota_reset":
                self._commit_state_from_read_model(read_model, allow_pending_snapshot=False)
                return
            if read_model.requires_human_approval:
                self._store_pending_approval(read_model)
                return
            if read_model.completed or read_model.blocked or read_model.terminal:
                self._commit_state_from_read_model(read_model, allow_pending_snapshot=False)
                return
            if not read_model.routine_action_available:
                self._commit_state_from_read_model(read_model, allow_pending_snapshot=False)
                return
            if not read_model.repository_path or not read_model.sandbox:
                self._pause_for_action_failure(
                    run_id,
                    action_key=read_model.planner_action or "routine_progress",
                    result=_controller_failure_result(
                        "run_configuration_missing",
                        "Run configuration is missing; routine work could not continue.",
                    ),
                    run_status_before_action=read_model.run_status,
                    source="automatic_progress",
                )
                return

            with self._lock:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION
                self.current_action_kind = read_model.planner_action or "routine_progress"
                self.automatic_burst_count = burst_count
            result = self.run_supervision_step(
                run_id,
                read_model.repository_path,
                read_model.sandbox,
                approval_mode="auto",
                allow_destination_navigation=read_model.allow_destination_navigation,
                ledger=self.ledger,
            )
            if self.cancel_requested.is_set():
                return
            burst_count += 1
            with self._lock:
                self.automatic_burst_count = burst_count
                self.last_action_result_summary = _action_result_summary(result, "routine_step")
            if (
                getattr(result, "blocked", False)
                or getattr(result, "terminal", False)
                or getattr(result, "completed", False)
                or not _result_ok(result)
            ):
                if getattr(result, "blocked", False) or not _result_ok(result):
                    if self._maybe_schedule_quota_wait(
                        run_id,
                        result,
                        repository_path=read_model.repository_path,
                        sandbox=read_model.sandbox,
                    ):
                        return
                    self._pause_for_action_failure(
                        run_id,
                        action_key=read_model.planner_action or "routine_progress",
                        result=result,
                        run_status_before_action=read_model.run_status,
                        source="automatic_progress",
                    )
                    return
                refreshed = self._build_read_model(run_id)
                self._commit_state_from_read_model(
                    refreshed,
                    allow_pending_snapshot=False,
                )
                return

    def _commit_state_from_read_model(
        self,
        read_model: LocalControllerReadModel,
        *,
        allow_pending_snapshot: bool,
    ) -> None:
        if allow_pending_snapshot and read_model.requires_human_approval:
            self._store_pending_approval(read_model)
            return
        with self._lock:
            if read_model.completed:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_COMPLETED
            elif read_model.planner_reason_code == "waiting_for_quota_reset":
                self.session.controller_state = LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET
            elif read_model.blocked or read_model.terminal:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
            else:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_IDLE
            self._persist_session_locked()

    def _store_pending_approval(self, read_model: LocalControllerReadModel) -> None:
        snapshot_result = create_pending_approval_snapshot(read_model)
        if snapshot_result.ok:
            with self._lock:
                self.session.pending_approval = snapshot_result.snapshot
                self.session.controller_state = LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
                self.last_action_result_summary = {
                    "kind": "approval_gate",
                    "reason_code": "human_approval_required",
                    "approval_kind": snapshot_result.snapshot.approval_kind
                    if snapshot_result.snapshot
                    else None,
                }
                self._persist_session_locked()
            return
        self._pause_for_action_failure(
            read_model.run_id,
            action_key=read_model.planner_action or "approval_snapshot",
            result=_controller_failure_result(
                snapshot_result.reason_code or "approval_snapshot_invalid",
                snapshot_result.error_message or "The approval snapshot could not be created.",
                retryable=False,
            ),
            run_status_before_action=read_model.run_status,
            source="approval_snapshot",
        )

    def _build_read_model(self, run_id: str) -> LocalControllerReadModel:
        return self.read_model_builder(run_id, ledger=self.ledger, session=self.session)

    def _new_worker(self, target: Callable[..., None], *args: Any) -> threading.Thread:
        # Daemon workers avoid hanging local process shutdown; Slice 10 does not implement termination.
        return threading.Thread(target=target, args=args, daemon=True)

    def _mark_action_running_locked(self, kind: str) -> None:
        self.action_running = True
        self.current_action_kind = kind
        self.current_action_started_at = datetime.now(UTC).isoformat()
        self.automatic_burst_count = 0
        self.automatic_burst_reason = None

    def _clear_action_running(self) -> None:
        with self._lock:
            self.action_running = False
            self.current_action_kind = None
            self.current_action_started_at = None
            self._persist_session_locked()

    def _maybe_schedule_quota_wait(
        self,
        run_id: str,
        result: Any,
        *,
        repository_path: str | None = None,
        sandbox: str | None = None,
    ) -> bool:
        events = self.ledger.list_events(run_id)
        # Result.error_message is often the missing final-message artifact, not
        # the usage-limit text from JSONL. Only use it as a veto after a wait
        # has already been scheduled, so a later generic resume failure does
        # not reuse stale usage-limit progress.
        if quota_wait_count(events) > 0:
            error_message = str(getattr(result, "error_message", "") or "")
            if error_message and not looks_like_usage_limit(error_message):
                return False
        clock = self.quota_wait_now()
        probe = decide_quota_wait(events, now=clock)
        if probe.reason_code in {"not_usage_limit", "quota_wait_limit_reached", "usage_limit_without_thread_id"}:
            return False
        rpc_time = None
        try:
            rpc_time = self.rate_limits_reader(now=clock)
        except TypeError:
            rpc_time = self.rate_limits_reader()
        except Exception:
            rpc_time = None
        decision = decide_quota_wait(events, now=clock, rate_limits_resets_at=rpc_time)
        if not decision.scheduled:
            return False
        recovered_repo, recovered_sandbox, _reason = _recover_run_configuration(events)
        repo_path = repository_path or recovered_repo
        sandbox_value = sandbox or recovered_sandbox or "read-only"
        if not repo_path:
            return False
        try:
            self.ledger.update_run_status(run_id, RunStatus.RUNNING)
        except (AttributeError, TypeError):
            pass
        metadata = {
            "thread_id": decision.thread_id,
            "resets_at": decision.resets_at,
            "resume_at": decision.resume_at,
            "source": decision.source,
            "invocation_id": decision.invocation_id,
            "repository_path": repo_path,
            "sandbox": sandbox_value,
            "error_text": decision.error_text,
        }
        event = self.ledger.add_event(
            run_id,
            CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
            "Waiting for Codex usage-limit reset before resuming the same session.",
            metadata,
        )
        fields = quota_wait_fields(event) or {
            "thread_id": decision.thread_id,
            "resume_at": decision.resume_at,
            "resets_at": decision.resets_at,
            "source": decision.source,
            "invocation_id": decision.invocation_id,
            "repository_path": repo_path,
            "sandbox": sandbox_value,
            "status": "waiting",
        }
        with self._lock:
            self._quota_wait = fields
            self.session.controller_state = LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET
            self._arm_quota_wait_timer_locked(str(fields["resume_at"]))
            self._persist_session_locked()
        return True

    def _arm_quota_wait_timer_locked(self, resume_at_iso: str) -> None:
        self._cancel_quota_wait_timer_locked()
        resume_at = _parse_iso_datetime(resume_at_iso)
        clock = self.quota_wait_now()
        delay = 0.0
        if resume_at is not None:
            delay = max(0.0, (resume_at - clock).total_seconds())
        run_id = self.session.active_run_id
        timer = threading.Timer(delay, self._quota_wait_timer_fired, args=(run_id,))
        timer.daemon = True
        self._quota_wait_timer = timer
        timer.start()

    def _cancel_quota_wait_timer_locked(self) -> None:
        timer = self._quota_wait_timer
        self._quota_wait_timer = None
        if timer is not None:
            timer.cancel()

    def _quota_wait_timer_fired(self, run_id: str | None) -> None:
        with self._lock:
            if run_id is None or self.session.active_run_id != run_id:
                return
            if self.session.controller_state != LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET:
                return
            if self.action_running:
                return
            self._quota_wait_timer = None
            self.session.controller_state = LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION
            self._mark_action_running_locked("quota_resume")
            worker = self._new_worker(self._quota_resume_worker, run_id)
            self.current_worker = worker
        worker.start()

    def _quota_resume_worker(self, run_id: str) -> None:
        try:
            result = self.quota_resume_executor(run_id=run_id, now=self.quota_wait_now())
            if self.cancel_requested.is_set():
                return
            if not _result_ok(result):
                events = self.ledger.list_events(run_id)
                previous_wait = None
                for event in reversed(events):
                    if event.get("event_type") == CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE:
                        previous_wait = quota_wait_fields(event)
                        break
                recovered_repo, recovered_sandbox, _reason = _recover_run_configuration(events)
                if self._maybe_schedule_quota_wait(
                    run_id,
                    result,
                    repository_path=(previous_wait or {}).get("repository_path") or recovered_repo,
                    sandbox=(previous_wait or {}).get("sandbox") or recovered_sandbox,
                ):
                    return
                run = self.ledger.get_run(run_id)
                self._pause_for_action_failure(
                    run_id,
                    action_key="quota_resume",
                    result=result,
                    run_status_before_action=(
                        str(run.get("status") or "") if isinstance(run, dict) else None
                    ),
                    source="quota_resume",
                    retry_context=previous_wait or {},
                )
                return
            self._automatic_progress_loop(run_id, starting_burst_count=0)
        except Exception as exc:
            self._record_worker_exception(
                exc,
                run_id=run_id,
                action_key="quota_resume",
            )
        finally:
            self._clear_action_running()

    def _persist_session_locked(self) -> None:
        writer = getattr(self.ledger, "save_local_controller_snapshot", None)
        if not callable(writer):
            return
        pending = asdict(self.session.pending_approval) if self.session.pending_approval else None
        try:
            writer(
                {
                    "active_run_id": self.session.active_run_id,
                    "controller_state": self.session.controller_state,
                    "pending_approval": pending,
                    "quota_wait": self._quota_wait,
                }
            )
        except Exception:
            return

    def _restore_persisted_session(self) -> None:
        reader = getattr(self.ledger, "load_local_controller_snapshot", None)
        if not callable(reader):
            return
        try:
            snapshot = reader()
        except Exception:
            return
        if not isinstance(snapshot, dict):
            return
        active_run_id = snapshot.get("active_run_id")
        if not isinstance(active_run_id, str) or not active_run_id:
            return
        active_run = self.ledger.get_run(active_run_id)
        if active_run is None:
            return
        active_status = str(active_run.get("status") or "")
        if active_status in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.NEEDS_REVIEW.value,
            RunStatus.REJECTED.value,
        }:
            self._persist_session_locked()
            return
        controller_state = snapshot.get("controller_state")
        if controller_state not in {
            LOCAL_CONTROLLER_STATE_IDLE,
            LOCAL_CONTROLLER_STATE_STARTING_INITIAL_CODEX,
            LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET,
            LOCAL_CONTROLLER_STATE_BLOCKED,
            LOCAL_CONTROLLER_STATE_FAILED,
            LOCAL_CONTROLLER_STATE_COMPLETED,
        }:
            controller_state = LOCAL_CONTROLLER_STATE_IDLE
        pending = _pending_approval_from_snapshot(snapshot.get("pending_approval"))
        if controller_state in {
            LOCAL_CONTROLLER_STATE_STARTING_INITIAL_CODEX,
            LOCAL_CONTROLLER_STATE_RUNNING_ROUTINE_ACTION,
        }:
            controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
            pending = None
        if (
            controller_state == LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
            and pending is None
        ):
            controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
        self.session.active_run_id = active_run_id
        self.session.controller_state = str(controller_state)
        self.session.pending_approval = pending
        wait_fields = quota_wait_fields(active_quota_wait(self.ledger.list_events(active_run_id)))
        snapshot_wait = snapshot.get("quota_wait")
        if wait_fields is None and isinstance(snapshot_wait, dict):
            thread_id = str(snapshot_wait.get("thread_id") or "").strip()
            resume_at = str(snapshot_wait.get("resume_at") or "").strip()
            if thread_id and resume_at:
                wait_fields = snapshot_wait
        self._quota_wait = wait_fields
        if controller_state == LOCAL_CONTROLLER_STATE_WAITING_FOR_QUOTA_RESET:
            if wait_fields is None:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
                self._quota_wait = None
            elif active_status == RunStatus.RUNNING.value:
                self._arm_quota_wait_timer_locked(str(wait_fields["resume_at"]))

    def _pause_for_action_failure(
        self,
        run_id: str,
        *,
        action_key: str,
        result: Any,
        run_status_before_action: str | None,
        source: str,
        retry_context: dict[str, Any] | None = None,
        supersedes_failure_event_id: int | None = None,
    ) -> dict[str, Any]:
        if self.cancel_requested.is_set():
            return {
                "reason_code": "operator_cancelled",
                "error_message": "Run cancelled by operator.",
                "retryable": False,
            }
        classification = _failure_retry_classification(
            action_key,
            result,
            retry_context=retry_context,
        )
        reason_code = (
            _string_or_none(getattr(result, "reason_code", None))
            or "controller_action_failed"
        )
        error_message = (
            _bounded_string(getattr(result, "error_message", None))
            or reason_code
        )
        planner_metadata = getattr(result, "planner_metadata", None)
        metadata = {
            "schema_version": LOCAL_CONTROLLER_RETRY_SCHEMA_VERSION,
            "failure_id": str(uuid.uuid4()),
            "run_id": run_id,
            "action_key": action_key,
            "source": source,
            "reason_code": reason_code,
            "error_message": error_message,
            "retry_classification": classification["classification"],
            "retryable": classification["retryable"],
            "recovery_message": classification["recovery_message"],
            "action_executed": bool(getattr(result, "action_executed", False)),
            "planner_action": _string_or_none(
                getattr(result, "planner_action", None)
            ),
            "planner_reason_code": _string_or_none(
                getattr(result, "planner_reason_code", None)
            ),
            "next_state_hint": _string_or_none(
                getattr(result, "next_state_hint", None)
            ),
            "run_status_before_action": run_status_before_action,
            "run_status_after_action": _string_or_none(
                getattr(result, "run_status", None)
            ),
            "source_event_ids": (
                planner_metadata.get("event_ids", {})
                if isinstance(planner_metadata, dict)
                and isinstance(planner_metadata.get("event_ids"), dict)
                else {}
            ),
            "supersedes_failure_event_id": supersedes_failure_event_id,
            "retry_context": _safe_preview_value(retry_context or {}),
        }
        self.ledger.add_event(
            run_id,
            LOCAL_CONTROLLER_ACTION_FAILED_EVENT_TYPE,
            LOCAL_CONTROLLER_ACTION_FAILED_MESSAGE,
            metadata,
        )
        with self._lock:
            self.last_action_result_summary = {
                "kind": source,
                "ok": False,
                "reason_code": reason_code,
                "error_message": error_message,
                "action_key": action_key,
                "retry_classification": classification["classification"],
                "retryable": classification["retryable"],
            }
            self.session.controller_state = (
                LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY
                if classification["retryable"]
                else LOCAL_CONTROLLER_STATE_BLOCKED
            )
            self._persist_session_locked()
        return metadata

    def _record_failure_resolved(
        self,
        run_id: str,
        failure_event_id: int,
        action_key: str,
        result: Any,
    ) -> None:
        self.ledger.add_event(
            run_id,
            LOCAL_CONTROLLER_FAILURE_RESOLVED_EVENT_TYPE,
            LOCAL_CONTROLLER_FAILURE_RESOLVED_MESSAGE,
            {
                "schema_version": LOCAL_CONTROLLER_RETRY_SCHEMA_VERSION,
                "failure_event_id": failure_event_id,
                "action_key": action_key,
                "reason_code": _string_or_none(
                    getattr(result, "reason_code", None)
                ),
                "resolved_at": datetime.now(UTC).isoformat(),
            },
        )

    def _record_worker_exception(
        self,
        exc: Exception,
        *,
        run_id: str | None = None,
        action_key: str | None = None,
        retry_context: dict[str, Any] | None = None,
        supersedes_failure_event_id: int | None = None,
    ) -> None:
        if run_id is not None and action_key is not None:
            self._pause_for_action_failure(
                run_id,
                action_key=action_key,
                result=_controller_failure_result(
                    "controller_worker_exception",
                    f"{type(exc).__name__}: {exc}",
                ),
                run_status_before_action=(
                    str((self.ledger.get_run(run_id) or {}).get("status") or "")
                ),
                source="worker_exception",
                retry_context=retry_context,
                supersedes_failure_event_id=supersedes_failure_event_id,
            )
        with self._lock:
            self.last_exception_summary = _exception_summary(exc)
            if run_id is None or action_key is None:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_FAILED
                self._persist_session_locked()

    def _runtime_snapshot_locked(self) -> dict[str, Any]:
        pending = self.session.pending_approval
        return {
            "controller_state": self.session.controller_state,
            "active_run_id": self.session.active_run_id,
            "pending_approval_available": pending is not None,
            "pending_approval_kind": pending.approval_kind if pending is not None else None,
            "session_age_seconds": _session_age_seconds(self.session.created_at),
            "action_running": self.action_running,
            "current_action_kind": self.current_action_kind,
            "current_action_started_at": self.current_action_started_at,
            "last_action_result_summary": self.last_action_result_summary,
            "last_exception_summary": self.last_exception_summary,
            "automatic_burst_count": self.automatic_burst_count,
            "automatic_burst_reason": self.automatic_burst_reason,
        }


def validate_local_controller_start_request(
    repository_path: str,
    initial_instruction: str,
    sandbox: str | None = None,
    *,
    model: str | None = None,
    project_title: str | None = None,
    chat_title: str | None = None,
    allow_destination_navigation: bool = False,
) -> StartRequestValidationResult:
    navigation_enabled = allow_destination_navigation is True
    path_text = str(repository_path or "").strip()
    if not path_text:
        return StartRequestValidationResult(
            ok=False,
            reason_code="repository_path_required",
            error_message="Repository path is required.",
        )

    resolved_path = Path(path_text).expanduser().resolve(strict=False)
    if not resolved_path.exists():
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            reason_code="repository_path_not_found",
            error_message=f"Repository path does not exist: {resolved_path}",
        )
    if not resolved_path.is_dir():
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            reason_code="repository_path_not_directory",
            error_message=f"Repository path is not a directory: {resolved_path}",
        )

    instruction_text = str(initial_instruction or "").strip()
    if not instruction_text:
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            sandbox=str(sandbox).strip() if sandbox is not None else None,
            model=str(model).strip() if model is not None else None,
            reason_code="initial_instruction_required",
            error_message="Initial instruction is required.",
        )

    sandbox_was_omitted = sandbox is None
    model_was_omitted = model is None
    sandbox_text = LOCAL_CONTROLLER_DEFAULT_SANDBOX if sandbox_was_omitted else str(sandbox or "").strip()
    if sandbox_text not in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            initial_instruction=instruction_text,
            sandbox=sandbox_text,
            reason_code="invalid_browser_sandbox",
            error_message=(
                "Invalid browser sandbox. Allowed values: "
                f"{', '.join(LOCAL_CONTROLLER_ALLOWED_SANDBOXES)}."
            ),
        )

    model_text = CODEX_DEFAULT_SELECTION if model_was_omitted else str(model or "").strip()
    if model_text not in ALLOWED_CODEX_MODEL_SELECTIONS:
        allowed_text = ", ".join(ALLOWED_CODEX_MODEL_SELECTIONS)
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            initial_instruction=instruction_text,
            sandbox=sandbox_text,
            model=model_text,
            reason_code="invalid_codex_model",
            error_message=f"Invalid Codex model. Allowed values: {allowed_text}.",
        )

    destination_result = _validate_destination_titles(project_title, chat_title)
    if not destination_result.ok:
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            initial_instruction=instruction_text,
            sandbox=sandbox_text,
            model=model_text,
            reason_code=destination_result.reason_code,
            error_message=destination_result.error_message,
        )

    return StartRequestValidationResult(
        ok=True,
        repository_path=str(resolved_path),
        initial_instruction=instruction_text,
        sandbox=sandbox_text,
        model=model_text,
        profile_source=(
            "system_default"
            if sandbox_was_omitted and model_was_omitted
            else "explicit_user_selection"
        ),
        project_title=destination_result.project_title,
        chat_title=destination_result.chat_title,
        allow_destination_navigation=navigation_enabled,
    )


def _validate_destination_titles(
    project_title: str | None,
    chat_title: str | None,
) -> StartRequestValidationResult:
    if project_title is None or chat_title is None:
        return StartRequestValidationResult(
            ok=False,
            reason_code="destination_required",
            error_message="ChatGPT Project title and ChatGPT chat title are required.",
        )
    try:
        binding = RunDestinationBinding(project_title, chat_title)
    except (TypeError, ValueError) as exc:
        return StartRequestValidationResult(
            ok=False,
            reason_code="invalid_destination",
            error_message=str(exc),
        )
    return StartRequestValidationResult(
        ok=True,
        project_title=binding.project_title,
        chat_title=binding.chat_title,
    )


def start_local_controller_run(
    session: LocalControllerSession,
    start_request: StartRequestValidationResult,
    *,
    ledger: Any = default_ledger,
    create_run: Callable[..., Any] = create_run_service,
) -> LocalControllerRunStartResult:
    if not start_request.ok:
        return LocalControllerRunStartResult(
            ok=False,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code=start_request.reason_code,
            error_message=start_request.error_message,
        )
    if (
        start_request.repository_path is None
        or start_request.initial_instruction is None
        or start_request.sandbox is None
        or start_request.project_title is None
        or start_request.chat_title is None
    ):
        raise ValueError("validated start request is missing required fields")
    if start_request.sandbox not in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
        return LocalControllerRunStartResult(
            ok=False,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code="invalid_browser_sandbox",
            error_message=(
                "Invalid browser sandbox. Allowed values: "
                f"{', '.join(LOCAL_CONTROLLER_ALLOWED_SANDBOXES)}."
            ),
        )

    model = start_request.model or CODEX_DEFAULT_SELECTION
    profile_source = start_request.profile_source or "explicit_user_selection"
    create_result = create_run(start_request.initial_instruction, ledger=ledger)
    if not getattr(create_result, "ok", False):
        return LocalControllerRunStartResult(
            ok=False,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code=getattr(create_result, "reason_code", None) or "run_create_failed",
            error_message=getattr(create_result, "error_message", None),
        )

    run_id = getattr(create_result, "run_id", None)
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("create_run returned no run_id")

    try:
        profile = RunExecutionProfile(
            sandbox=start_request.sandbox,
            model=model,
            reasoning_effort=CODEX_DEFAULT_SELECTION,
            approval_policy=(
                "never"
                if start_request.sandbox == "danger-full-access"
                else CODEX_DEFAULT_SELECTION
            ),
            profile_source=profile_source,
        )
    except (TypeError, ValueError) as exc:
        _record_local_controller_start_failure(
            ledger,
            run_id,
            "invalid_execution_profile",
            str(exc),
        )
        return LocalControllerRunStartResult(
            ok=False,
            run_id=run_id,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code="invalid_execution_profile",
            error_message=str(exc),
        )

    profile_result = select_run_execution_profile(run_id, profile, ledger=ledger)
    if not profile_result.ok:
        reason_code = profile_result.reason_code or "execution_profile_selection_failed"
        error_message = profile_result.error_message or "Failed to select run execution profile."
        _record_local_controller_start_failure(
            ledger,
            run_id,
            reason_code,
            error_message,
        )
        return LocalControllerRunStartResult(
            ok=False,
            run_id=run_id,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code=reason_code,
            error_message=error_message,
        )

    destination_result = bind_run_destination(
        run_id,
        start_request.project_title,
        start_request.chat_title,
        ledger=ledger,
    )
    if not destination_result.ok:
        reason_code = destination_result.reason_code or "destination_binding_failed"
        error_message = (
            destination_result.error_message
            or "Failed to bind run destination."
        )
        _record_local_controller_start_failure(
            ledger,
            run_id,
            reason_code,
            error_message,
        )
        return LocalControllerRunStartResult(
            ok=False,
            run_id=run_id,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code=reason_code,
            error_message=error_message,
        )

    metadata = {
        "metadata_version": LOCAL_CONTROLLER_METADATA_VERSION,
        "repository_path": start_request.repository_path,
        "sandbox": start_request.sandbox,
        "model": model,
        "source": LOCAL_CONTROLLER_SOURCE,
        "controller_mode": LOCAL_CONTROLLER_MODE,
        "browser_safe_sandbox": start_request.sandbox != "danger-full-access",
        "autonomous_full_access": start_request.sandbox == "danger-full-access",
        "allow_destination_navigation": bool(start_request.allow_destination_navigation),
    }
    ledger.add_event(
        run_id,
        LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE,
        LOCAL_CONTROLLER_RUN_STARTED_MESSAGE,
        metadata,
    )

    session.active_run_id = run_id
    session.controller_state = LOCAL_CONTROLLER_INITIAL_STATE
    session.pending_approval = None

    return LocalControllerRunStartResult(
        ok=True,
        run_id=run_id,
        repository_path=start_request.repository_path,
        sandbox=start_request.sandbox,
        initial_instruction=start_request.initial_instruction,
        event_type=LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE,
        message=LOCAL_CONTROLLER_RUN_STARTED_MESSAGE,
        metadata=metadata,
    )


def _record_local_controller_start_failure(
    ledger: Any,
    run_id: str,
    reason_code: str,
    error_message: str,
) -> None:
    ledger.add_event(
        run_id,
        LOCAL_CONTROLLER_RUN_START_FAILED_EVENT_TYPE,
        LOCAL_CONTROLLER_RUN_START_FAILED_MESSAGE,
        {
            "metadata_version": LOCAL_CONTROLLER_METADATA_VERSION,
            "reason_code": reason_code,
            "error_message": error_message,
            "source": LOCAL_CONTROLLER_SOURCE,
            "controller_mode": LOCAL_CONTROLLER_MODE,
        },
    )


def build_local_controller_read_model(
    run_id: str,
    *,
    ledger: Any = default_ledger,
    session: LocalControllerSession | None = None,
    planner: Callable[..., SupervisePlan] = detect_next_supervise_action,
    force_human_approval: bool = False,
) -> LocalControllerReadModel:
    run = ledger.get_run(run_id)
    events = ledger.list_events(run_id) if run is not None else []
    latest_event_id = _latest_event_id(events)
    repository_path, sandbox, configuration_reason = _recover_run_configuration(events)
    execution_profile = _execution_profile_summary(run_id, ledger=ledger) if run is not None else None
    destination_binding = _destination_binding_summary(run_id, ledger=ledger) if run is not None else None
    allow_destination_navigation = _recover_navigation_setting(events)
    latest_handoff_phase = _latest_handoff_phase(events)
    latest_failure = _latest_unresolved_action_failure(events)
    configuration_complete = configuration_reason is None

    plan = None
    if run is not None and configuration_complete:
        plan = planner(run, events, repository_path, sandbox=sandbox)

    planner_metadata = _plan_metadata(plan)
    planner_action = planner_metadata.get("action") if plan is not None else None
    planner_reason = planner_metadata.get("reason") if plan is not None else None
    routine_available, requires_approval, approval_kind = _planner_action_availability(
        plan,
        events,
        force_human_approval=force_human_approval,
    )
    terminal, blocked, completed = _terminal_flags(run, plan, configuration_complete)
    stage = _current_stage(
        run,
        plan,
        configuration_complete=configuration_complete,
        configuration_reason=configuration_reason,
        requires_approval=requires_approval,
        routine_available=routine_available,
        completed=completed,
        blocked=blocked,
    )
    error_message = _actionable_error_message(
        run,
        plan,
        configuration_reason=configuration_reason,
        requires_approval=requires_approval,
        latest_failure=latest_failure,
    )
    quota_wait = quota_wait_fields(active_quota_wait(events))
    if quota_wait is not None:
        quota_wait = {
            **quota_wait,
            "message": quota_wait_client_message(str(quota_wait["resume_at"])),
        }
        if latest_failure is None:
            error_message = quota_wait["message"]

    return LocalControllerReadModel(
        run_id=run_id,
        run_status=str(run.get("status")) if isinstance(run, dict) else None,
        initial_instruction=str(run.get("user_instruction")) if isinstance(run, dict) else None,
        repository_path=repository_path,
        sandbox=sandbox,
        execution_profile=execution_profile,
        destination_binding=destination_binding,
        allow_destination_navigation=allow_destination_navigation,
        latest_handoff_phase=latest_handoff_phase,
        latest_event_id=latest_event_id,
        planner_action=planner_action,
        planner_reason_code=planner_reason,
        planner_metadata=planner_metadata,
        current_stage=stage,
        routine_action_available=routine_available,
        requires_human_approval=requires_approval,
        approval_kind=approval_kind,
        terminal=terminal,
        blocked=blocked,
        completed=completed,
        actionable_error_message=error_message,
        latest_codex_result=_latest_codex_result(events),
        latest_chatgpt_submission=_latest_chatgpt_submission(events),
        latest_chatgpt_capture=_latest_chatgpt_capture(events),
        latest_prompt_extraction=_latest_prompt_extraction(events),
        latest_governance=_latest_governance(events),
        event_timeline=_event_timeline(events),
        controller_runtime=_controller_runtime(session),
        configuration_complete=configuration_complete,
        latest_failure=latest_failure,
        quota_wait=quota_wait,
    )


def create_pending_approval_snapshot(
    read_model: LocalControllerReadModel,
) -> PendingApprovalSnapshotResult:
    if not read_model.requires_human_approval:
        return PendingApprovalSnapshotResult(
            ok=False,
            reason_code="approval_not_required",
            error_message="No approval is required for the current read model.",
        )
    if read_model.planner_action == str(SuperviseAction.ASK_SEND_TO_GPT):
        approval_kind = "send_to_gpt"
    elif read_model.planner_action == str(SuperviseAction.ASK_RUN_PROMPT):
        approval_kind = "run_prompt"
    else:
        return PendingApprovalSnapshotResult(
            ok=False,
            reason_code="unsupported_approval_action",
            error_message="The current planner action does not support approval.",
        )

    event_ids = read_model.planner_metadata.get("event_ids")
    event_ids = event_ids if isinstance(event_ids, dict) else {}
    prompt_text = read_model.planner_metadata.get("prompt_text")
    prompt_text_sha = _sha256_text(prompt_text) if isinstance(prompt_text, str) and prompt_text else None
    extraction_event_id = _int_or_none(event_ids.get("next_codex_prompt_extracted"))

    snapshot = PendingApprovalSnapshot(
        run_id=read_model.run_id,
        approval_kind=approval_kind,
        planner_action=str(read_model.planner_action),
        planner_reason_code=str(read_model.planner_reason_code or ""),
        planner_metadata=dict(read_model.planner_metadata),
        latest_event_id=read_model.latest_event_id,
        expected_extraction_event_id=extraction_event_id,
        expected_prompt_sha256=_string_or_none(read_model.planner_metadata.get("prompt_sha")),
        expected_prompt_text_sha256=prompt_text_sha,
        expected_extraction_method=_string_or_none(read_model.planner_metadata.get("extraction_method")),
        created_at=datetime.now(UTC).isoformat(),
    )
    return PendingApprovalSnapshotResult(ok=True, snapshot=snapshot)


def _pending_approval_from_snapshot(value: object) -> PendingApprovalSnapshot | None:
    if not isinstance(value, dict):
        return None
    try:
        planner_metadata = value.get("planner_metadata")
        if not isinstance(planner_metadata, dict):
            return None
        return PendingApprovalSnapshot(
            run_id=str(value["run_id"]),
            approval_kind=str(value["approval_kind"]),
            planner_action=str(value["planner_action"]),
            planner_reason_code=str(value["planner_reason_code"]),
            planner_metadata=dict(planner_metadata),
            latest_event_id=int(value["latest_event_id"]),
            expected_extraction_event_id=_int_or_none(
                value.get("expected_extraction_event_id")
            ),
            expected_prompt_sha256=_string_or_none(
                value.get("expected_prompt_sha256")
            ),
            expected_prompt_text_sha256=_string_or_none(
                value.get("expected_prompt_text_sha256")
            ),
            expected_extraction_method=_string_or_none(
                value.get("expected_extraction_method")
            ),
            created_at=str(value["created_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _recover_run_configuration(events: list[dict]) -> tuple[str | None, str | None, str | None]:
    controller_event = _latest_valid_controller_started_event(events)
    if controller_event is not None:
        metadata = _event_metadata(controller_event)
        return str(metadata["repository_path"]), str(metadata["sandbox"]), None

    fallback = _latest_valid_repo_sandbox_event(events)
    if fallback is not None:
        return fallback

    return None, None, "run_configuration_missing"


def _execution_profile_summary(run_id: str, *, ledger: Any) -> dict[str, Any]:
    lookup = get_run_execution_profile(run_id, ledger=ledger)
    if lookup.status == ExecutionProfileLookupStatus.PRESENT and lookup.profile is not None:
        return _profile_summary(
            "present",
            lookup.profile,
            event_ids=lookup.event_ids,
        )
    if (
        lookup.status == ExecutionProfileLookupStatus.MISSING
        and lookup.legacy_compatibility_profile is not None
    ):
        return _profile_summary(
            "legacy_compatibility",
            lookup.legacy_compatibility_profile,
            event_ids=lookup.event_ids,
        )
    return {
        "status": "invalid",
        "reason_code": lookup.reason_code or "execution_profile_invalid",
        "error_message": lookup.error_message or "Run execution profile is invalid.",
        "event_ids": list(lookup.event_ids),
    }


def _destination_binding_summary(run_id: str, *, ledger: Any) -> dict[str, Any]:
    lookup = get_run_destination_binding(run_id, ledger=ledger)
    if lookup.status == DestinationBindingLookupStatus.PRESENT and lookup.binding is not None:
        return {
            "status": "present",
            "state_label": "Bound and valid",
            "project_title": lookup.binding.project_title,
            "chat_title": lookup.binding.chat_title,
            "event_ids": list(lookup.event_ids),
        }
    if lookup.status == DestinationBindingLookupStatus.MISSING:
        return {
            "status": "missing",
            "state_label": "Missing / legacy",
            "message": "No autonomous destination binding.",
            "event_ids": list(lookup.event_ids),
        }
    return {
        "status": "invalid",
        "state_label": "Invalid / contradictory",
        "reason_code": lookup.reason_code or "destination_binding_invalid",
        "error_message": lookup.error_message or "Run destination binding is invalid.",
        "event_ids": list(lookup.event_ids),
    }


def _recover_navigation_setting(events: list[dict]) -> bool:
    """Read the durable operator navigation approval flag.

    Fail-closed: navigation is only enabled when a valid controller-started
    event explicitly recorded ``allow_destination_navigation: true``. Runs that
    predate this setting (or whose start event lacks it) stay disabled.
    """

    controller_event = _latest_valid_controller_started_event(events)
    if controller_event is None:
        return False
    metadata = _event_metadata(controller_event)
    return metadata.get("allow_destination_navigation") is True


def _latest_handoff_phase(events: list[dict]) -> dict[str, Any] | None:
    event = _latest_event(events, "chatgpt_handoff_phase")
    if event is None:
        return None
    metadata = _event_metadata(event)
    navigation = metadata.get("navigation")
    return {
        "event_id": _event_id(event),
        "phase": metadata.get("handoff_phase"),
        "navigation_operator_approved": bool(metadata.get("navigation_operator_approved")),
        "navigation_outcome": navigation.get("outcome") if isinstance(navigation, dict) else None,
        "navigation_ok": navigation.get("ok") if isinstance(navigation, dict) else None,
    }


def _chatgpt_ui_lease_status(*, ledger: Any) -> dict[str, Any]:
    try:
        events = ledger.list_chatgpt_ui_lease_events()
        state = default_ledger._reconstruct_chatgpt_ui_lease_state(events)  # noqa: SLF001 - shared ledger reconstruction.
        latest_denial = _latest_chatgpt_ui_lease_denial(events)
    except Exception as exc:
        return {
            "status": "invalid",
            "active": False,
            "stale_status": "invalid",
            "release_allowed": False,
            "release_block_reason": "chatgpt_ui_lease_status_unavailable",
            "reason_code": "chatgpt_ui_lease_status_unavailable",
            "error_message": _bounded_string(str(exc)),
        }

    if state.status == default_ledger.AtomicChatGPTUILeaseStatus.MISSING:
        return {
            "status": "missing",
            "active": False,
            "stale_status": "not_active",
            "release_allowed": False,
            "release_block_reason": "chatgpt_ui_lease_not_active",
            "event_ids": list(state.event_ids),
            "latest_denial": latest_denial,
        }

    if state.status == default_ledger.AtomicChatGPTUILeaseStatus.INVALID:
        return {
            "status": "invalid",
            "active": False,
            "stale_status": "invalid",
            "release_allowed": False,
            "release_block_reason": state.reason_code or "chatgpt_ui_lease_invalid",
            "reason_code": state.reason_code,
            "error_message": state.error_message,
            "event_ids": list(state.event_ids),
            "latest_denial": latest_denial,
        }

    run_status = None
    if state.owning_run_id:
        try:
            run = ledger.get_run(state.owning_run_id)
        except Exception:
            run = None
        if isinstance(run, dict) and isinstance(run.get("status"), str):
            run_status = run["status"]

    pid_state = _owner_pid_state(state.owner_pid)
    release_allowed, block_reason = _stale_lease_release_availability(
        owner_pid_state=pid_state,
        run_status=run_status,
    )
    return {
        "status": "active",
        "active": True,
        "owning_run_id": state.owning_run_id,
        "owner_pid": state.owner_pid,
        "acquired_at": state.acquired_at,
        "active_event_id": state.active_event_id,
        "lease_token_sha256": state.lease_token,
        "owning_run_status": run_status,
        "owner_pid_state": pid_state,
        "owner_pid_alive": True if pid_state == "alive" else False if pid_state == "dead" else None,
        "stale_status": "recoverable" if release_allowed else "not_recoverable",
        "release_allowed": release_allowed,
        "release_block_reason": block_reason,
        "event_ids": list(state.event_ids),
        "latest_denial": latest_denial,
    }


def _codex_progress_payload(
    run_id: str | None,
    events: list[dict[str, Any]],
    *,
    after_sequence: int,
) -> dict[str, Any]:
    latest_sequence = max(
        [after_sequence]
        + [
            int(event["sequence"])
            for event in events
            if isinstance(event.get("sequence"), int)
        ]
    )
    return {
        "run_id": run_id,
        "after_sequence": after_sequence,
        "latest_sequence": latest_sequence,
        "events": events,
    }


def _latest_chatgpt_ui_lease_denial(events: list[dict]) -> dict[str, Any] | None:
    event = _latest_event(events, CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE)
    if event is None:
        return None
    metadata = _event_metadata(event)
    return {
        "event_id": _event_id(event),
        "run_id": event.get("run_id"),
        "created_at": event.get("created_at"),
        "requested_owning_run_id": metadata.get("requested_owning_run_id"),
        "request_owner_pid": metadata.get("request_owner_pid"),
        "denied_at": metadata.get("denied_at"),
        "active_owning_run_id": metadata.get("active_owning_run_id"),
        "active_owner_pid": metadata.get("active_owner_pid"),
        "active_acquired_at": metadata.get("active_acquired_at"),
    }


def _owner_pid_state(pid: int | None) -> str:
    if not isinstance(pid, int) or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def _stale_lease_release_availability(
    *,
    owner_pid_state: str,
    run_status: str | None,
) -> tuple[bool, str | None]:
    if owner_pid_state == "alive":
        return False, "owner_pid_alive"
    if owner_pid_state != "dead":
        return False, "owner_pid_unknown"
    if run_status is None:
        return False, "owner_run_status_unknown"
    if run_status not in STALE_LEASE_TERMINAL_RUN_STATUSES:
        return False, "owner_run_not_terminal"
    return True, None


def _lease_release_request_mismatch(
    lease: dict[str, Any],
    *,
    owning_run_id: str,
    owner_pid: int,
    acquired_at: str,
    active_event_id: int,
    expected_lease_token_sha256: str,
    expected_run_status: str | None,
) -> str | None:
    if lease.get("status") != "active" or not lease.get("active"):
        return "chatgpt_ui_lease_not_active"
    if lease.get("owning_run_id") != owning_run_id:
        return "active_chatgpt_ui_lease_mismatch"
    if lease.get("owner_pid") != owner_pid:
        return "active_chatgpt_ui_lease_mismatch"
    if lease.get("acquired_at") != acquired_at:
        return "active_chatgpt_ui_lease_mismatch"
    if lease.get("active_event_id") != active_event_id:
        return "active_chatgpt_ui_lease_mismatch"
    if lease.get("lease_token_sha256") != expected_lease_token_sha256:
        return "active_chatgpt_ui_lease_mismatch"
    current_status = lease.get("owning_run_status")
    if current_status is not None and expected_run_status is None:
        return "active_chatgpt_ui_lease_mismatch"
    if current_status != expected_run_status:
        return "active_chatgpt_ui_lease_mismatch"
    return None


def _profile_summary(
    status: str,
    profile: RunExecutionProfile,
    *,
    event_ids: tuple[int, ...],
) -> dict[str, Any]:
    return {
        "status": status,
        "sandbox": profile.sandbox,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "approval_policy": profile.approval_policy,
        "profile_source": profile.profile_source,
        "event_ids": list(event_ids),
    }


def _latest_valid_controller_started_event(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE:
            continue
        metadata = _event_metadata(event)
        if metadata.get("metadata_version") != LOCAL_CONTROLLER_METADATA_VERSION:
            continue
        if metadata.get("source") != LOCAL_CONTROLLER_SOURCE:
            continue
        browser_authorized = metadata.get("browser_safe_sandbox") is True
        autonomous_full_access = (
            metadata.get("autonomous_full_access") is True
            and metadata.get("sandbox") == "danger-full-access"
        )
        if not browser_authorized and not autonomous_full_access:
            continue
        repository_path = metadata.get("repository_path")
        sandbox = metadata.get("sandbox")
        if isinstance(repository_path, str) and repository_path.strip() and sandbox in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
            return event
    return None


def _latest_valid_repo_sandbox_event(events: list[dict]) -> tuple[str, str, None] | None:
    relevant = {
        "codex_exec_started",
        "codex_exec_finished",
        "extracted_codex_prompt_selected",
        "extracted_codex_prompt_run_started",
        "extracted_codex_prompt_run_finished",
    }
    for event in reversed(events):
        if event.get("event_type") not in relevant:
            continue
        metadata = _event_metadata(event)
        repository_path = metadata.get("repo_path")
        sandbox = metadata.get("sandbox")
        if isinstance(repository_path, str) and repository_path.strip() and sandbox in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
            return repository_path, sandbox, None
    return None


def _planner_action_availability(
    plan: SupervisePlan | None,
    events: list[dict],
    *,
    force_human_approval: bool,
) -> tuple[bool, bool, str | None]:
    if plan is None:
        return False, False, None

    if plan.action == SuperviseAction.ASK_SEND_TO_GPT:
        auto_safe, _reason = send_plan_auto_safe(plan, events)
        if force_human_approval or not auto_safe:
            return False, True, "send_to_gpt"
        return True, False, None

    if plan.action == SuperviseAction.ASK_RUN_PROMPT:
        auto_safe = bool(getattr(plan, "prompt_auto_run_safe", False))
        if force_human_approval or not auto_safe:
            return False, True, "run_prompt"
        return True, False, None

    if plan.action in {SuperviseAction.CAPTURE_GPT_RESPONSE, SuperviseAction.EXTRACT_NEXT_PROMPT}:
        return True, False, None

    return False, False, None


def _terminal_flags(
    run: dict | None,
    plan: SupervisePlan | None,
    configuration_complete: bool,
) -> tuple[bool, bool, bool]:
    if not configuration_complete:
        return False, True, False
    status = str(run.get("status") or "") if isinstance(run, dict) else ""
    reason = str(getattr(plan, "reason", "") or "")
    action = getattr(plan, "action", None)

    completed = action == SuperviseAction.STOP and reason == "extracted_prompt_already_run"
    idle_waiting_initial = status == "created" and action == SuperviseAction.STOP
    waiting_for_quota = action == SuperviseAction.STOP and reason == "waiting_for_quota_reset"
    terminal = status in {"failed", "rejected"} or (
        action == SuperviseAction.STOP and not idle_waiting_initial and not waiting_for_quota
    )
    blocked = not completed and (
        status in {"failed", "rejected", "needs_review", "waiting_for_approval"}
        or (action == SuperviseAction.STOP and not idle_waiting_initial and not waiting_for_quota)
    )
    return terminal, blocked, completed


def _current_stage(
    run: dict | None,
    plan: SupervisePlan | None,
    *,
    configuration_complete: bool,
    configuration_reason: str | None,
    requires_approval: bool,
    routine_available: bool,
    completed: bool,
    blocked: bool,
) -> str:
    if run is None:
        return "missing_run"
    if not configuration_complete:
        return configuration_reason or "configuration_incomplete"
    if completed:
        return "completed"
    if requires_approval:
        return "waiting_for_approval"
    status = str(run.get("status") or "")
    reason = str(getattr(plan, "reason", "") or "")
    if status == "created":
        return "idle"
    if blocked:
        if status == "needs_review":
            return "review_required"
        return "blocked"
    if reason == "waiting_for_quota_reset":
        return "waiting_for_quota_reset"
    if routine_available:
        return "routine_action_available"
    if plan is not None and plan.action == SuperviseAction.STOP:
        return "terminal"
    return "idle"


def _actionable_error_message(
    run: dict | None,
    plan: SupervisePlan | None,
    *,
    configuration_reason: str | None,
    requires_approval: bool,
    latest_failure: dict[str, Any] | None = None,
) -> str | None:
    if run is None:
        return "Run not found."
    if configuration_reason is not None:
        return "Local controller run metadata is missing or incomplete."
    if requires_approval:
        return "Human approval is required before this action can run."
    if latest_failure is not None:
        return str(
            latest_failure.get("error_message")
            or latest_failure.get("reason_code")
            or "The latest controller action failed."
        )
    stop_message = getattr(plan, "stop_message", "") if plan is not None else ""
    return stop_message or None


def _latest_codex_result(events: list[dict]) -> dict[str, Any] | None:
    event = _latest_event(events, "codex_exec_finished")
    if event is None:
        return None
    metadata = _event_metadata(event)
    stdout = metadata.get("stdout")
    stderr = metadata.get("stderr")
    return {
        "event_id": _event_id(event),
        "exit_code": metadata.get("exit_code"),
        "timed_out": bool(metadata.get("timed_out")),
        "found": metadata.get("found"),
        "validation_error": metadata.get("validation_error"),
        "sandbox": metadata.get("sandbox"),
        "repo_path": metadata.get("repo_path"),
        "stdout_length": len(stdout) if isinstance(stdout, str) else metadata.get("stdout_length"),
        "stderr_length": len(stderr) if isinstance(stderr, str) else metadata.get("stderr_length"),
    }


def _latest_chatgpt_submission(events: list[dict]) -> dict[str, Any] | None:
    event_types = {
        "gpt_feedback_submission_verified": "verified",
        "gpt_feedback_submission_failed": "failed",
        "gpt_feedback_submission_ambiguous": "ambiguous",
    }
    event = _latest_event_of_types(events, set(event_types))
    if event is None:
        return None
    metadata = _event_metadata(event)
    return {
        "event_id": _event_id(event),
        "state": event_types[str(event.get("event_type"))],
        "submission_marker_sha256": metadata.get("submission_marker_sha256"),
        "reason_code": metadata.get("reason_code"),
        "payload_length": metadata.get("feedback_payload_length"),
    }


def _latest_chatgpt_capture(events: list[dict]) -> dict[str, Any] | None:
    event_types = {
        "gpt_response_captured": "captured",
        "gpt_response_capture_failed": "failed",
        "gpt_response_capture_started": "started",
    }
    event = _latest_event_of_types(events, set(event_types))
    if event is None:
        return None
    metadata = _event_metadata(event)
    stability = metadata.get("stability") if isinstance(metadata.get("stability"), dict) else {}
    return {
        "event_id": _event_id(event),
        "state": event_types[str(event.get("event_type"))],
        "response_sha256": metadata.get("response_sha256"),
        "sentinel_state": metadata.get("sentinel_state"),
        "stable": stability.get("stable"),
        "reason_code": metadata.get("reason_code"),
    }


def _latest_prompt_extraction(events: list[dict]) -> dict[str, Any] | None:
    event = _latest_event(events, "next_codex_prompt_extracted")
    if event is None:
        return None
    metadata = _event_metadata(event)
    return {
        "event_id": _event_id(event),
        "prompt_sha256": metadata.get("prompt_sha256"),
        "prompt_length": metadata.get("prompt_length"),
        "method": metadata.get("extraction_method"),
        "source_capture_event_id": metadata.get("source_event_id"),
        "warnings": metadata.get("warnings") if isinstance(metadata.get("warnings"), list) else [],
    }


def _latest_governance(events: list[dict]) -> dict[str, Any] | None:
    transition_event = _latest_event(events, "run_status_transition")
    decision_event = _latest_event(events, "supervision_decision")
    post_run_event = _latest_event(events, "workspace_write_post_run_policy")
    if transition_event is None and decision_event is None and post_run_event is None:
        return None

    transition = _event_metadata(transition_event) if transition_event is not None else {}
    decision = _event_metadata(decision_event) if decision_event is not None else {}
    post_run = _event_metadata(post_run_event) if post_run_event is not None else {}
    post_run_policy = post_run.get("post_run_policy") if isinstance(post_run.get("post_run_policy"), dict) else {}
    human_review_required = bool(
        transition.get("needs_review")
        or transition.get("approval_required")
        or (post_run_policy and not post_run_policy.get("allowed", True))
    )
    return {
        "status_transition_event_id": _event_id(transition_event),
        "next_status": transition.get("next_status"),
        "status_transition_reason": transition.get("reason"),
        "supervision_decision_event_id": _event_id(decision_event),
        "supervision_decision": decision.get("decision"),
        "human_review_required": human_review_required,
        "workspace_write_post_run_reason": post_run_policy.get("reason_code"),
        "workspace_write_post_run_allowed": post_run_policy.get("allowed"),
    }


def _event_timeline(events: list[dict]) -> list[LocalControllerEventTimelineRow]:
    rows = []
    for event in events:
        metadata = _event_metadata(event)
        rows.append(
            LocalControllerEventTimelineRow(
                event_id=_event_id(event),
                timestamp=str(event.get("created_at") or ""),
                event_type=str(event.get("event_type") or ""),
                message=str(event.get("message") or ""),
                metadata_preview=_metadata_preview(metadata),
                full_metadata_available=bool(metadata),
            )
        )
    return rows


def _metadata_preview(metadata: dict[str, Any]) -> dict[str, Any]:
    if not metadata:
        return {}
    preview = _safe_preview_value(metadata)
    if isinstance(preview, dict):
        encoded = json.dumps(preview, sort_keys=True, default=str)
        if len(encoded) <= EVENT_METADATA_PREVIEW_LIMIT:
            return preview
        return {
            key: preview[key]
            for key in preview
            if key
            in {
                "reason_code",
                "exit_code",
                "timed_out",
                "found",
                "validation_error",
                "sandbox",
                "repo_path",
                "repository_path",
                "status",
                "next_status",
                "previous_status",
                "prompt_sha256",
                "source_event_id",
                "matched_submission_event_id",
                "event_ids",
                "decision",
                "approval_required",
                "needs_review",
            }
        }
    return {}


def _safe_preview_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in {"lease_token", "raw_lease_token", "token"}:
                continue
            if key_text in {"stdout", "stderr", "response_text", "prompt_text", "feedback_message", "message"}:
                if isinstance(item, str):
                    result[f"{key_text}_length"] = len(item)
                continue
            result[key_text] = _safe_preview_value(item)
        return result
    if isinstance(value, list):
        return [_safe_preview_value(item) for item in value[:10]]
    if isinstance(value, tuple):
        return [_safe_preview_value(item) for item in value[:10]]
    if isinstance(value, str) and len(value) > TEXT_METADATA_PREVIEW_LIMIT:
        return {
            "preview": value[:TEXT_METADATA_PREVIEW_LIMIT],
            "length": len(value),
            "truncated": True,
        }
    return value


def _controller_runtime(session: LocalControllerSession | None) -> dict[str, Any]:
    if session is None:
        return {
            "controller_state": None,
            "active_run_id": None,
            "pending_approval_available": False,
            "session_age_seconds": None,
        }
    return {
        "controller_state": session.controller_state,
        "active_run_id": session.active_run_id,
        "pending_approval_available": session.pending_approval is not None,
        "session_age_seconds": _session_age_seconds(session.created_at),
    }


def _session_age_seconds(created_at: str) -> float | None:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - created).total_seconds())


def _plan_metadata(plan: SupervisePlan | None) -> dict[str, Any]:
    if plan is None:
        return {}
    metadata = asdict(plan)
    metadata["action"] = str(plan.action)
    metadata["warnings"] = list(plan.warnings)
    return metadata


def _event_metadata(event: dict | None) -> dict[str, Any]:
    if event is None:
        return {}
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    metadata_json = event.get("metadata_json")
    if not metadata_json:
        return {}
    try:
        decoded = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _latest_event(events: list[dict], event_type: str) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _latest_event_of_types(events: list[dict], event_types: set[str]) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") in event_types:
            return event
    return None


def _latest_event_id(events: list[dict]) -> int:
    latest = -1
    for event in events:
        latest = max(latest, _event_id(event))
    return latest


def _event_id(event: dict | None) -> int:
    if event is None:
        return -1
    return _int_or_none(event.get("id")) if _int_or_none(event.get("id")) is not None else -1


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _result_ok(result: Any) -> bool:
    ok = getattr(result, "ok", None)
    if ok is not None:
        return bool(ok)
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result == 0
    return bool(result)


def _controller_failure_result(
    reason_code: str,
    error_message: str,
    *,
    retryable: bool | None = None,
) -> _ControllerFailureResult:
    return _ControllerFailureResult(
        ok=False,
        reason_code=reason_code,
        error_message=error_message,
        retryable_override=retryable,
    )


def _planner_event_ids(read_model: LocalControllerReadModel) -> dict[str, int]:
    raw = read_model.planner_metadata.get("event_ids")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _failure_retry_classification(
    action_key: str,
    result: Any,
    *,
    retry_context: dict[str, Any] | None,
) -> dict[str, Any]:
    override = getattr(result, "retryable_override", None)
    reason_code = str(getattr(result, "reason_code", "") or "")
    event_type = str(
        getattr(getattr(result, "action_result", None), "event_type", "")
        or getattr(result, "event_type", "")
        or ""
    )
    ambiguous_reasons = {
        "chatgpt_submission_ambiguous",
        "chatgpt_submission_not_verified",
        "extracted_prompt_run_incomplete",
        "extracted_codex_prompt_run_failed",
        "retry_planner_action_changed",
    }
    deterministic_reasons = {
        "gpt_feedback_not_submittable",
        "gpt_feedback_generation_failed",
        "captured_response_integrity_failed",
        "invalid_extracted_prompt",
        "selected_prompt_sha_validation_failed",
        "extracted_prompt_changed_after_approval",
    }
    safe_actions = {
        str(SuperviseAction.ASK_SEND_TO_GPT),
        str(SuperviseAction.CAPTURE_GPT_RESPONSE),
        str(SuperviseAction.EXTRACT_NEXT_PROMPT),
    }

    if override is not None:
        retryable = bool(override)
    elif (
        reason_code in ambiguous_reasons
        or event_type == "gpt_feedback_submission_ambiguous"
    ):
        return {
            "classification": "reconcile",
            "retryable": False,
            "recovery_message": (
                "This action may already have produced an external effect. "
                "Inspect and reconcile it before retrying."
            ),
        }
    elif reason_code in deterministic_reasons or event_type == "gpt_feedback_generation_failed":
        return {
            "classification": "retry_after_fix",
            "retryable": True,
            "recovery_message": (
                "Correct the reported blocker, then retry the same action."
            ),
        }
    elif action_key == "initial_codex":
        sandbox = str((retry_context or {}).get("sandbox") or "")
        retryable = sandbox == "read-only"
    elif action_key == str(SuperviseAction.ASK_RUN_PROMPT):
        retryable = not bool(getattr(result, "action_executed", False))
    else:
        retryable = action_key in safe_actions

    if retryable:
        return {
            "classification": "retryable",
            "retryable": True,
            "recovery_message": (
                "Remove the reported blocker, then retry this exact action."
            ),
        }
    return {
        "classification": "review_required",
        "retryable": False,
        "recovery_message": (
            "Retry is disabled because this action may have changed external "
            "state or the repository. Review and reconcile it first."
        ),
    }


def _latest_unresolved_action_failure(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    resolved_ids: set[int] = set()
    for event in events:
        if event.get("event_type") == LOCAL_CONTROLLER_ACTION_FAILED_EVENT_TYPE:
            metadata = _event_metadata(event)
            try:
                resolved_ids.add(int(metadata.get("supersedes_failure_event_id")))
            except (TypeError, ValueError):
                pass
            continue
        if event.get("event_type") != LOCAL_CONTROLLER_FAILURE_RESOLVED_EVENT_TYPE:
            continue
        metadata = _event_metadata(event)
        try:
            resolved_ids.add(int(metadata.get("failure_event_id")))
        except (TypeError, ValueError):
            continue

    for event in reversed(events):
        if event.get("event_type") != LOCAL_CONTROLLER_ACTION_FAILED_EVENT_TYPE:
            continue
        event_id = _event_id(event)
        if event_id in resolved_ids:
            continue
        metadata = _event_metadata(event)
        return {
            "event_id": event_id,
            "timestamp": event.get("created_at"),
            "message": event.get("message"),
            "action_key": metadata.get("action_key"),
            "source": metadata.get("source"),
            "reason_code": metadata.get("reason_code"),
            "error_message": metadata.get("error_message"),
            "retry_classification": metadata.get("retry_classification"),
            "retryable": metadata.get("retryable") is True,
            "recovery_message": metadata.get("recovery_message"),
            "action_executed": metadata.get("action_executed") is True,
            "planner_action": metadata.get("planner_action"),
            "planner_reason_code": metadata.get("planner_reason_code"),
            "run_status_before_action": metadata.get("run_status_before_action"),
            "run_status_after_action": metadata.get("run_status_after_action"),
            "source_event_ids": (
                metadata.get("source_event_ids")
                if isinstance(metadata.get("source_event_ids"), dict)
                else {}
            ),
        }
    return None


def _action_result_summary(result: Any, kind: str) -> dict[str, Any]:
    summary = {
        "kind": kind,
        "ok": _result_ok(result),
        "reason_code": _string_or_none(getattr(result, "reason_code", None)),
        "error_message": _bounded_string(getattr(result, "error_message", None)),
    }
    for name in (
        "planner_action",
        "planner_reason_code",
        "next_state_hint",
        "approval_kind",
        "run_status",
    ):
        value = getattr(result, name, None)
        if value is not None:
            summary[name] = value
    for name in ("action_executed", "terminal", "completed", "blocked", "requires_human_approval"):
        value = getattr(result, name, None)
        if isinstance(value, bool):
            summary[name] = value
    return {key: value for key, value in summary.items() if value is not None}


def _exception_summary(exc: Exception) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": _bounded_string(str(exc)) or "",
    }


def _bounded_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= EXCEPTION_MESSAGE_PREVIEW_LIMIT:
        return text
    return text[:EXCEPTION_MESSAGE_PREVIEW_LIMIT]
