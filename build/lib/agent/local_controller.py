from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.initial_codex_run_services import execute_initial_direct_codex_run_service
from agent.run_services import (
    ALLOWED_CODEX_MODEL_SELECTIONS,
    ALLOWED_EXECUTION_PROFILE_SANDBOXES,
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
from agent.supervise import SuperviseAction, SupervisePlan, detect_next_supervise_action
from agent.supervision_services import run_supervision_step as run_supervision_step_service
from agent.supervision_services import send_plan_auto_safe


LOCAL_CONTROLLER_ALLOWED_SANDBOXES = tuple(
    sandbox
    for sandbox in ALLOWED_EXECUTION_PROFILE_SANDBOXES
    if sandbox != "danger-full-access"
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
LOCAL_CONTROLLER_STATE_BLOCKED = "blocked"
LOCAL_CONTROLLER_STATE_FAILED = "failed"
LOCAL_CONTROLLER_STATE_COMPLETED = "completed"
LOCAL_CONTROLLER_TERMINAL_STATES = (
    LOCAL_CONTROLLER_STATE_BLOCKED,
    LOCAL_CONTROLLER_STATE_FAILED,
    LOCAL_CONTROLLER_STATE_COMPLETED,
)
LOCAL_CONTROLLER_INITIAL_CODEX_TIMEOUT_SECONDS: float | None = None

EVENT_METADATA_PREVIEW_LIMIT = 1200
TEXT_METADATA_PREVIEW_LIMIT = 240
TOKEN_ENTROPY_BYTES = 32
EXCEPTION_MESSAGE_PREVIEW_LIMIT = 500


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
        confirm_full_access=False,
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
    ) -> None:
        self.session = session or LocalControllerSession()
        self.ledger = ledger
        self.read_model_builder = read_model_builder or build_local_controller_read_model
        self.run_supervision_step = supervision_step
        self.initial_run_executor = initial_run_executor or (
            lambda **kwargs: default_initial_run_executor(**kwargs, ledger=self.ledger)
        )
        self._lock = threading.Lock()
        self.action_running = False
        self.current_worker: threading.Thread | None = None
        self.current_action_kind: str | None = None
        self.current_action_started_at: str | None = None
        self.last_action_result_summary: dict[str, Any] | None = None
        self.last_exception_summary: dict[str, Any] | None = None
        self.automatic_burst_count = 0
        self.automatic_burst_reason: str | None = None

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
                return LocalControllerOperationResult(
                    ok=False,
                    reason_code="active_run_exists",
                    error_message="A local controller run is already active.",
                    run_id=self.session.active_run_id,
                    controller_state=self.session.controller_state,
                )

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
            summary = _action_result_summary(result, "initial_run")
            with self._lock:
                self.last_action_result_summary = summary
            if not _result_ok(result):
                with self._lock:
                    self.session.controller_state = LOCAL_CONTROLLER_STATE_FAILED
                return
            self._automatic_progress_loop(run_id, starting_burst_count=0)
        except Exception as exc:
            self._record_worker_exception(exc)
        finally:
            self._clear_action_running()

    def _routine_worker(self, run_id: str, starting_burst_count: int) -> None:
        try:
            self._automatic_progress_loop(run_id, starting_burst_count=starting_burst_count)
        except Exception as exc:
            self._record_worker_exception(exc)
        finally:
            self._clear_action_running()

    def _approval_worker(self, snapshot: PendingApprovalSnapshot, decision: str) -> None:
        try:
            with self._lock:
                if self.session.pending_approval == snapshot:
                    self.session.pending_approval = None
            read_model = self._build_read_model(snapshot.run_id)
            if not read_model.configuration_complete or not read_model.repository_path or not read_model.sandbox:
                with self._lock:
                    self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
                    self.last_action_result_summary = {
                        "kind": "approval_decision",
                        "reason_code": "run_configuration_missing",
                    }
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
                ledger=self.ledger,
            )
            with self._lock:
                self.last_action_result_summary = _action_result_summary(result, "approval_decision")
            refreshed = self._build_read_model(snapshot.run_id)
            self._commit_state_from_read_model(refreshed, allow_pending_snapshot=False)
        except Exception as exc:
            self._record_worker_exception(exc)
        finally:
            self._clear_action_running()

    def _automatic_progress_loop(self, run_id: str, *, starting_burst_count: int) -> None:
        burst_count = starting_burst_count
        while True:
            read_model = self._build_read_model(run_id)
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
                with self._lock:
                    self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
                    self.last_action_result_summary = {
                        "kind": "routine_progress",
                        "reason_code": "run_configuration_missing",
                    }
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
                ledger=self.ledger,
            )
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
                    with self._lock:
                        self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
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
            elif read_model.blocked or read_model.terminal:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
            else:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_IDLE

    def _store_pending_approval(self, read_model: LocalControllerReadModel) -> None:
        snapshot_result = create_pending_approval_snapshot(read_model)
        with self._lock:
            if snapshot_result.ok:
                self.session.pending_approval = snapshot_result.snapshot
                self.session.controller_state = LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
                self.last_action_result_summary = {
                    "kind": "approval_gate",
                    "reason_code": "human_approval_required",
                    "approval_kind": snapshot_result.snapshot.approval_kind
                    if snapshot_result.snapshot
                    else None,
                }
            else:
                self.session.controller_state = LOCAL_CONTROLLER_STATE_BLOCKED
                self.last_action_result_summary = {
                    "kind": "approval_gate",
                    "reason_code": snapshot_result.reason_code,
                    "error_message": snapshot_result.error_message,
                }

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

    def _record_worker_exception(self, exc: Exception) -> None:
        with self._lock:
            self.last_exception_summary = _exception_summary(exc)
            self.session.controller_state = LOCAL_CONTROLLER_STATE_FAILED

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
) -> StartRequestValidationResult:
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
    if sandbox_text == "danger-full-access":
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            initial_instruction=instruction_text,
            sandbox=sandbox_text,
            reason_code="danger_full_access_not_available_in_local_controller",
            error_message="danger-full-access is not available through the local controller.",
        )
    if sandbox_text not in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
        return StartRequestValidationResult(
            ok=False,
            repository_path=str(resolved_path),
            initial_instruction=instruction_text,
            sandbox=sandbox_text,
            reason_code="invalid_browser_sandbox",
            error_message="Invalid browser sandbox. Allowed values: read-only, workspace-write.",
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
    if start_request.sandbox == "danger-full-access":
        return LocalControllerRunStartResult(
            ok=False,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code="danger_full_access_not_available_in_local_controller",
            error_message="danger-full-access is not available through the local controller.",
        )
    if start_request.sandbox not in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
        return LocalControllerRunStartResult(
            ok=False,
            repository_path=start_request.repository_path,
            sandbox=start_request.sandbox,
            initial_instruction=start_request.initial_instruction,
            reason_code="invalid_browser_sandbox",
            error_message="Invalid browser sandbox. Allowed values: read-only, workspace-write.",
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
            approval_policy=CODEX_DEFAULT_SELECTION,
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
        "browser_safe_sandbox": True,
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
    )

    return LocalControllerReadModel(
        run_id=run_id,
        run_status=str(run.get("status")) if isinstance(run, dict) else None,
        initial_instruction=str(run.get("user_instruction")) if isinstance(run, dict) else None,
        repository_path=repository_path,
        sandbox=sandbox,
        execution_profile=execution_profile,
        destination_binding=destination_binding,
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
        if metadata.get("browser_safe_sandbox") is not True:
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
    terminal = status in {"failed", "rejected"} or (
        action == SuperviseAction.STOP and not idle_waiting_initial
    )
    blocked = not completed and (
        status in {"failed", "rejected", "needs_review", "waiting_for_approval"}
        or (action == SuperviseAction.STOP and not idle_waiting_initial)
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
    if status == "created":
        return "idle"
    if blocked:
        if status == "needs_review":
            return "review_required"
        return "blocked"
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
) -> str | None:
    if run is None:
        return "Run not found."
    if configuration_reason is not None:
        return "Local controller run metadata is missing or incomplete."
    if requires_approval:
        return "Human approval is required before this action can run."
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


def _result_ok(result: Any) -> bool:
    ok = getattr(result, "ok", None)
    if ok is not None:
        return bool(ok)
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result == 0
    return bool(result)


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
