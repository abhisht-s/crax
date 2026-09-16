"""Stage 4: real concurrent session workers.

These tests prove that two to four actual controller runtimes/workers can
coexist and progress independently, with deterministic fake Codex and fake
ChatGPT/lane services. No live desktop automation is used anywhere.

Coverage map (Stage 4 acceptance):
- capacity: 1..4 live runtimes, fifth rejected, hard cap of four
- identity: same project + different chats allowed, same repo allowed,
  same chat rejected
- isolation: Codex overlap, ChatGPT wait, approval wait, retry/backoff,
  worker exception, cancellation, terminal absorption
- Stage 3 lane: real ledger FIFO queue serializes real session workers;
  cancel removes only the cancelled run's queued work; terminal runs are
  refused re-entry
- SQLite: concurrent multi-run writers stay stable within the busy timeout
- focused/current compatibility: focus follows the newest start while
  background sessions keep progressing
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from agent import ledger as ledger_module
from agent.local_controller import (
    DEFAULT_MAX_ACTIVE_SESSIONS,
    MAX_ACTIVE_SESSIONS_HARD_CAP,
    InitialRunExecutionResult,
    LocalController,
    LocalControllerEventTimelineRow,
    LocalControllerReadModel,
    LocalControllerSession,
)
from agent.run_services import (
    CODEX_DEFAULT_SELECTION,
    RUN_DESTINATION_BOUND_EVENT_TYPE,
    RUN_DESTINATION_BOUND_MESSAGE,
    RUN_DESTINATION_BOUND_SCHEMA_VERSION,
    RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
    RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
    RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
    DestinationBindingLookupStatus,
    ExecutionProfileLookupStatus,
    RunDestinationBinding,
    get_run_destination_binding,
    get_run_execution_profile,
)
from agent.run_state import RunStatus


WAIT_TIMEOUT_SECONDS = 5.0


def _wait_until(predicate: Callable[[], bool], timeout: float = WAIT_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------
# Multi-run fakes
# ---------------------------------------------------------------------------


class MultiRunFakeLedger:
    """Thread-safe fake ledger that stores many runs with per-run events."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.runs: dict[str, dict] = {}
        self.events_by_run: dict[str, list[dict]] = {}
        self.added_events: list[dict] = []
        self.update_run_status_calls: list[dict] = []
        self.controller_snapshot: dict | None = None
        self._next_run_number = 1
        self._next_event_id = 1

    def create_run(self, user_instruction: str) -> str:
        with self._lock:
            run_id = f"run-{self._next_run_number}"
            self._next_run_number += 1
            self.runs[run_id] = {
                "id": run_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "status": RunStatus.CREATED.value,
                "user_instruction": user_instruction,
                "final_summary": None,
                "error": None,
            }
            self.events_by_run.setdefault(run_id, [])
            return run_id

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            run = self.runs.get(run_id)
            return dict(run) if run is not None else None

    def set_run_status(self, run_id: str, status: str) -> None:
        with self._lock:
            self.runs[run_id]["status"] = status

    def list_events(self, run_id: str) -> list[dict]:
        with self._lock:
            return list(self.events_by_run.get(run_id, []))

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        with self._lock:
            event = {
                "id": self._next_event_id,
                "run_id": run_id,
                "created_at": f"2026-01-01T00:00:{self._next_event_id % 60:02d}+00:00",
                "event_type": event_type,
                "message": message,
                "metadata": metadata,
                "metadata_json": "{}",
            }
            self._next_event_id += 1
            self.events_by_run.setdefault(run_id, []).append(event)
            self.added_events.append(event)
            return event

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        final_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self.update_run_status_calls.append(
                {"run_id": run_id, "status": status, "final_summary": final_summary, "error": error}
            )
            run = self.runs.get(run_id)
            if run is not None:
                run["status"] = status.value
                run["final_summary"] = final_summary
                run["error"] = error

    def bind_run_destination(self, run_id: str, project_title: str, chat_title: str):
        existing = get_run_destination_binding(run_id, ledger=self)
        if existing.status == DestinationBindingLookupStatus.PRESENT:
            assert existing.binding is not None
            return ledger_module.AtomicDestinationBindingResult(
                status=ledger_module.AtomicDestinationBindingStatus.IDEMPOTENT,
                run_id=run_id,
                project_title=existing.binding.project_title,
                chat_title=existing.binding.chat_title,
                event_ids=existing.event_ids,
            )
        binding = RunDestinationBinding(project_title, chat_title)
        event_id = self.add_event(
            run_id,
            RUN_DESTINATION_BOUND_EVENT_TYPE,
            RUN_DESTINATION_BOUND_MESSAGE,
            {
                "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                "project_title": binding.project_title,
                "chat_title": binding.chat_title,
            },
        )["id"]
        return ledger_module.AtomicDestinationBindingResult(
            status=ledger_module.AtomicDestinationBindingStatus.BOUND,
            run_id=run_id,
            project_title=binding.project_title,
            chat_title=binding.chat_title,
            event_id=event_id,
            event_written=True,
        )

    def bind_run_execution_profile(
        self,
        run_id: str,
        sandbox: str,
        model: str,
        reasoning_effort: str,
        approval_policy: str,
        profile_source: str,
    ):
        existing = get_run_execution_profile(run_id, ledger=self)
        if existing.status == ExecutionProfileLookupStatus.PRESENT:
            assert existing.profile is not None
            return ledger_module.AtomicExecutionProfileResult(
                status=ledger_module.AtomicExecutionProfileStatus.IDEMPOTENT,
                run_id=run_id,
                sandbox=existing.profile.sandbox,
                model=existing.profile.model,
                reasoning_effort=existing.profile.reasoning_effort,
                approval_policy=existing.profile.approval_policy,
                profile_source=existing.profile.profile_source,
                event_ids=existing.event_ids,
            )
        event_id = self.add_event(
            run_id,
            RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
            RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
            {
                "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
                "sandbox": sandbox,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "approval_policy": approval_policy,
                "profile_source": profile_source,
            },
        )["id"]
        return ledger_module.AtomicExecutionProfileResult(
            status=ledger_module.AtomicExecutionProfileStatus.SELECTED,
            run_id=run_id,
            sandbox=sandbox,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            profile_source=profile_source,
            event_id=event_id,
            event_written=True,
        )

    def save_local_controller_snapshot(self, snapshot: dict) -> None:
        with self._lock:
            self.controller_snapshot = dict(snapshot)

    def load_local_controller_snapshot(self) -> dict | None:
        with self._lock:
            return dict(self.controller_snapshot) if self.controller_snapshot else None


@dataclass
class FakeStepResult:
    ok: bool = True
    reason_code: str | None = "step_ok"
    error_message: str | None = None
    action_executed: bool = True
    terminal: bool = False
    completed: bool = False
    blocked: bool = False
    waiting_for_chatgpt: bool = False
    requires_human_approval: bool = False
    planner_action: str | None = "capture_gpt_response"
    planner_reason_code: str | None = "routine"
    next_state_hint: str | None = None
    run_status: str | None = "completed"


def _read_model(
    run_id: str,
    *,
    action: str | None = "capture_gpt_response",
    reason: str | None = "routine",
    routine: bool = False,
    approval: bool = False,
    approval_kind: str | None = None,
    terminal: bool = False,
    blocked: bool = False,
    completed: bool = False,
    stage: str = "idle",
    repo_path: str = "/tmp",
    sandbox: str = "read-only",
    latest_event_id: int = 10,
    planner_metadata: dict | None = None,
) -> LocalControllerReadModel:
    metadata = planner_metadata
    if metadata is None:
        metadata = {
            "action": action,
            "reason": reason,
            "event_ids": {"codex_exec_finished": 10},
        }
    return LocalControllerReadModel(
        run_id=run_id,
        run_status="completed",
        initial_instruction="Initial task",
        repository_path=repo_path,
        sandbox=sandbox,
        execution_profile={
            "model": CODEX_DEFAULT_SELECTION,
            "sandbox": sandbox,
            "status": ExecutionProfileLookupStatus.PRESENT.value,
        },
        destination_binding={
            "status": "present",
            "state_label": "Bound and valid",
            "project_title": "Project",
            "chat_title": "Chat",
        },
        allow_destination_navigation=False,
        latest_handoff_phase=None,
        latest_event_id=latest_event_id,
        planner_action=action,
        planner_reason_code=reason,
        planner_metadata=metadata,
        current_stage=stage,
        routine_action_available=routine,
        requires_human_approval=approval,
        approval_kind=approval_kind,
        terminal=terminal,
        blocked=blocked,
        completed=completed,
        actionable_error_message=None,
        latest_codex_result=None,
        latest_chatgpt_submission=None,
        latest_chatgpt_capture=None,
        latest_prompt_extraction=None,
        latest_governance=None,
        event_timeline=[
            LocalControllerEventTimelineRow(
                event_id=latest_event_id,
                timestamp="2026-01-01T00:00:00+00:00",
                event_type="test",
                message="test",
                metadata_preview={},
                full_metadata_available=False,
            )
        ],
        controller_runtime={},
        configuration_complete=True,
    )


def _routine_model(run_id: str, repo_path: str = "/tmp") -> LocalControllerReadModel:
    return _read_model(
        run_id,
        action="capture_gpt_response",
        routine=True,
        stage="routine_action_available",
        repo_path=repo_path,
    )


def _completed_model(run_id: str, repo_path: str = "/tmp") -> LocalControllerReadModel:
    return _read_model(
        run_id,
        action="stop",
        reason="extracted_prompt_already_run",
        completed=True,
        terminal=True,
        stage="completed",
        repo_path=repo_path,
    )


def _blocked_model(run_id: str, repo_path: str = "/tmp") -> LocalControllerReadModel:
    return _read_model(
        run_id,
        action="stop",
        reason="run_blocked",
        blocked=True,
        terminal=True,
        stage="blocked",
        repo_path=repo_path,
    )


def _approval_model(run_id: str, repo_path: str = "/tmp") -> LocalControllerReadModel:
    return _read_model(
        run_id,
        action="ask_send_to_gpt",
        reason="codex_result_ready",
        approval=True,
        approval_kind="send_to_gpt",
        stage="waiting_for_approval",
        repo_path=repo_path,
    )


@dataclass
class _StepScript:
    result: Any
    next_model: LocalControllerReadModel | None = None
    entered: threading.Event | None = None
    gate: threading.Event | None = None
    exception: Exception | None = None


class SessionWorld:
    """Per-run scripted read models and supervision steps.

    The read-model builder is pure (no consumption), so concurrent
    ``get_current_state`` calls from the test thread cannot disturb a
    worker's script. Steps advance the run's model explicitly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.models: dict[str, LocalControllerReadModel] = {}
        self._steps: dict[str, list[_StepScript]] = {}
        self._repeat: dict[str, Any] = {}
        self.step_calls: list[dict] = []
        self.default_model_factory: Callable[[str], LocalControllerReadModel] | None = None

    def set_model(self, run_id: str, model: LocalControllerReadModel) -> None:
        with self._lock:
            self.models[run_id] = model

    def queue_step(
        self,
        run_id: str,
        result: Any,
        *,
        next_model: LocalControllerReadModel | None = None,
        entered: threading.Event | None = None,
        gate: threading.Event | None = None,
        exception: Exception | None = None,
    ) -> None:
        with self._lock:
            self._steps.setdefault(run_id, []).append(
                _StepScript(result, next_model, entered, gate, exception)
            )

    def repeat_step(self, run_id: str, result: Any) -> None:
        with self._lock:
            self._repeat[run_id] = result

    def read_model_builder(self, run_id: str, **kwargs: Any) -> LocalControllerReadModel:
        with self._lock:
            model = self.models.get(run_id)
            if model is None and self.default_model_factory is not None:
                model = self.default_model_factory(run_id)
                self.models[run_id] = model
        if model is None:
            raise AssertionError(f"no scripted read model for {run_id}")
        return model

    def supervision_step(self, run_id: str, repository_path: str, sandbox: str, **kwargs: Any) -> Any:
        with self._lock:
            self.step_calls.append(
                {"run_id": run_id, "approval_mode": kwargs.get("approval_mode")}
            )
            scripts = self._steps.get(run_id)
            script = scripts.pop(0) if scripts else None
            repeat = self._repeat.get(run_id)
        if script is None:
            if repeat is None:
                raise AssertionError(f"no scripted supervision step for {run_id}")
            return repeat
        if script.entered is not None:
            script.entered.set()
        if script.gate is not None:
            script.gate.wait(WAIT_TIMEOUT_SECONDS * 2)
        if script.exception is not None:
            raise script.exception
        if script.next_model is not None:
            self.set_model(run_id, script.next_model)
        return script.result

    def calls_for(self, run_id: str) -> list[dict]:
        with self._lock:
            return [call for call in self.step_calls if call["run_id"] == run_id]


@dataclass
class _ExecutorRecord:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    blocking: bool = False
    result: Any = field(default_factory=lambda: InitialRunExecutionResult(ok=True))


class PerRunInitialExecutor:
    """Fake Codex initial executor keyed by run_id (blocking optional)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: dict[str, _ExecutorRecord] = {}
        self.calls: list[str] = []

    def configure(self, run_id: str, *, blocking: bool = False, result: Any = None) -> _ExecutorRecord:
        record = _ExecutorRecord(blocking=blocking)
        if result is not None:
            record.result = result
        with self._lock:
            self.records[run_id] = record
        return record

    def __call__(self, *, run_id: str, **kwargs: Any) -> Any:
        with self._lock:
            self.calls.append(run_id)
            record = self.records.get(run_id)
        if record is None:
            return InitialRunExecutionResult(ok=True)
        record.entered.set()
        if record.blocking:
            record.release.wait(WAIT_TIMEOUT_SECONDS * 2)
        return record.result


def _wait_step_result(action: str = "capture_gpt_response") -> FakeStepResult:
    return FakeStepResult(
        ok=True,
        waiting_for_chatgpt=True,
        blocked=False,
        action_executed=False,
        reason_code="chatgpt_handoff_queue_not_head",
        planner_action=action,
        next_state_hint=action,
    )


def _success_step_result(action: str = "capture_gpt_response") -> FakeStepResult:
    return FakeStepResult(ok=True, planner_action=action)


def _make_controller(
    world: SessionWorld,
    *,
    ledger: Any = None,
    executor: Any = None,
    max_sessions: int = 4,
    sleeper: Callable[[float], None] | None = None,
) -> LocalController:
    return LocalController(
        session=LocalControllerSession(),
        ledger=ledger if ledger is not None else MultiRunFakeLedger(),
        read_model_builder=world.read_model_builder,
        supervision_step=world.supervision_step,
        initial_run_executor=executor if executor is not None else PerRunInitialExecutor(),
        max_active_sessions=max_sessions,
        chatgpt_wait_sleeper=sleeper if sleeper is not None else (lambda seconds: time.sleep(0.001)),
        desktop_mutex=object(),
    )


def _start(
    controller: LocalController,
    repo: str,
    *,
    chat: str,
    project: str = "craxii",
    instruction: str = "Task",
):
    return controller.start_run(
        repository_path=repo,
        initial_instruction=instruction,
        project_title=project,
        chat_title=chat,
        sandbox="read-only",
    )


def _join_workers(controller: LocalController, timeout: float = WAIT_TIMEOUT_SECONDS) -> None:
    for runtime in list(controller._sessions.values()):
        worker = runtime.current_worker
        if worker is not None:
            worker.join(timeout)


def _runtime_state(controller: LocalController, run_id: str) -> str:
    runtime = controller._sessions.get(run_id)
    return runtime.controller_state if runtime is not None else "missing"


def _temporary_real_ledger():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "ledger.db"
    patcher = mock.patch.object(ledger_module, "DB_PATH", db_path)

    class _Context:
        def __enter__(self):
            patcher.__enter__()
            return db_path

        def __exit__(self, exc_type, exc, tb):
            patcher.__exit__(exc_type, exc, tb)
            tmpdir.cleanup()

    return _Context()


# ---------------------------------------------------------------------------
# Capacity and identity rules
# ---------------------------------------------------------------------------


class MultiSessionCapacityTests(unittest.TestCase):
    def test_capacity_is_configurable_and_hard_capped_at_four(self) -> None:
        world = SessionWorld()
        for configured in (1, 2, 3, 4):
            controller = _make_controller(world, max_sessions=configured)
            self.assertEqual(controller.max_active_sessions, configured)
        self.assertEqual(_make_controller(world, max_sessions=9).max_active_sessions, MAX_ACTIVE_SESSIONS_HARD_CAP)
        self.assertEqual(_make_controller(world, max_sessions=0).max_active_sessions, 1)
        self.assertEqual(MAX_ACTIVE_SESSIONS_HARD_CAP, 4)
        self.assertEqual(DEFAULT_MAX_ACTIVE_SESSIONS, 1)

    def test_two_live_runtimes_coexist_with_isolated_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            world.set_model("run-1", _completed_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            runtime_a = controller._sessions[first.run_id]
            runtime_b = controller._sessions[second.run_id]
            self.assertTrue(runtime_a.action_running)
            self.assertTrue(runtime_b.action_running)
            self.assertIsNot(runtime_a.current_worker, runtime_b.current_worker)
            self.assertIsNot(runtime_a.cancel_requested, runtime_b.cancel_requested)

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)
            self.assertEqual(_runtime_state(controller, first.run_id), "completed")
            self.assertEqual(_runtime_state(controller, second.run_id), "completed")

    def test_four_live_runtimes_coexist_and_fifth_start_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            records = []
            for index in range(1, 5):
                run_id = f"run-{index}"
                records.append(executor.configure(run_id, blocking=True))
                world.set_model(run_id, _completed_model(run_id, repo))
            controller = _make_controller(world, executor=executor, max_sessions=4)

            results = [
                _start(controller, repo, chat=f"Chat {index}") for index in range(1, 5)
            ]
            for result in results:
                self.assertTrue(result.ok)
            for record in records:
                self.assertTrue(record.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertEqual(len(controller._sessions), 4)

            fifth = _start(controller, repo, chat="Chat 5")
            self.assertFalse(fifth.ok)
            self.assertEqual(fifth.reason_code, "active_run_exists")

            for record in records:
                record.release.set()
            _join_workers(controller)

    def test_same_project_and_same_repo_allowed_same_chat_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            world.set_model("run-1", _completed_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=3)

            first = _start(controller, repo, chat="Dev Internal App", project="craxii")
            second = _start(controller, repo, chat="Other Chat", project="craxii")
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)

            collision = _start(controller, repo, chat="Dev Internal App", project="craxii")
            self.assertFalse(collision.ok)
            self.assertEqual(collision.reason_code, "duplicate_chatgpt_conversation")
            self.assertEqual(collision.run_id, first.run_id)

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

    def test_starting_b_does_not_evict_live_focused_a(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            world.set_model("run-1", _completed_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            worker_a = controller._sessions[first.run_id].current_worker

            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(second.ok)
            self.assertIn(first.run_id, controller._sessions)
            self.assertTrue(controller._sessions[first.run_id].action_running)
            self.assertTrue(worker_a.is_alive())
            self.assertEqual(controller.session.active_run_id, second.run_id)
            snapshot = controller.ledger.controller_snapshot
            self.assertIn(first.run_id, snapshot["sessions"])
            self.assertIn(second.run_id, snapshot["sessions"])

            record_a.release.set()
            _join_workers(controller)

    def test_terminal_focused_session_is_still_replaced_at_cap_one(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _completed_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=1)

            first = _start(controller, repo, chat="Chat A")
            self.assertTrue(first.ok)
            _join_workers(controller)
            controller.ledger.set_run_status(first.run_id, RunStatus.COMPLETED.value)

            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(second.ok)
            self.assertNotIn(first.run_id, controller._sessions)
            self.assertEqual(controller.session.active_run_id, second.run_id)
            _join_workers(controller)


# ---------------------------------------------------------------------------
# Worker isolation
# ---------------------------------------------------------------------------


class MultiSessionIsolationTests(unittest.TestCase):
    def test_codex_invocations_overlap_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            world.set_model("run-1", _completed_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=2)

            _start(controller, repo, chat="Chat A")
            _start(controller, repo, chat="Chat B")

            # Both fake Codex executions are inside their run at the same
            # time before either is released: true overlap, not turn-taking.
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertFalse(record_a.release.is_set())
            self.assertFalse(record_b.release.is_set())

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)
            self.assertEqual(_runtime_state(controller, "run-1"), "completed")
            self.assertEqual(_runtime_state(controller, "run-2"), "completed")

    def test_a_waiting_for_chatgpt_does_not_stop_b_codex(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.repeat_step("run-1", _wait_step_result())
            world.set_model("run-2", _routine_model("run-2", repo))
            world.queue_step(
                "run-2",
                _success_step_result(),
                next_model=_completed_model("run-2", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            self.assertTrue(
                _wait_until(
                    lambda: controller._sessions[first.run_id].chatgpt_wait_count >= 2
                )
            )

            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(second.ok)
            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, second.run_id) == "completed"
                )
            )

            runtime_a = controller._sessions[first.run_id]
            runtime_b = controller._sessions[second.run_id]
            self.assertTrue(runtime_a.waiting_for_chatgpt)
            self.assertEqual(runtime_b.chatgpt_wait_count, 0)
            self.assertNotEqual(runtime_a.controller_state, "waiting_for_retry")

            controller.request_cancel(first.run_id)
            _join_workers(controller)

    def test_a_waiting_for_approval_does_not_stop_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _approval_model("run-1", repo))
            for run_id in ("run-2", "run-3"):
                world.set_model(run_id, _routine_model(run_id, repo))
                world.queue_step(
                    run_id,
                    _success_step_result(),
                    next_model=_completed_model(run_id, repo),
                )
            controller = _make_controller(world, executor=executor, max_sessions=3)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            third = _start(controller, repo, chat="Chat C")

            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id)
                    == "waiting_for_approval"
                )
            )
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, third.run_id) == "completed")
            )
            runtime_a = controller._sessions[first.run_id]
            self.assertIsNotNone(runtime_a.pending_approval)
            self.assertEqual(runtime_a.pending_approval.run_id, first.run_id)
            _join_workers(controller)

    def test_a_failure_and_retry_state_do_not_pause_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.queue_step(
                "run-1",
                FakeStepResult(
                    ok=False,
                    reason_code="gpt_feedback_generation_failed",
                    error_message="scripted failure",
                    action_executed=False,
                ),
            )
            world.set_model("run-2", _routine_model("run-2", repo))
            world.queue_step(
                "run-2",
                _success_step_result(),
                next_model=_completed_model("run-2", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")

            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id)
                    == "waiting_for_retry"
                )
            )
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            ledger = controller.ledger
            failure_runs = {
                event["run_id"]
                for event in ledger.added_events
                if event["event_type"] == "local_controller_action_failed"
            }
            self.assertEqual(failure_runs, {first.run_id})
            _join_workers(controller)

    def test_a_chatgpt_backoff_sleep_does_not_delay_b(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.repeat_step("run-1", _wait_step_result())
            world.set_model("run-2", _routine_model("run-2", repo))
            world.queue_step(
                "run-2",
                _success_step_result(),
                next_model=_completed_model("run-2", repo),
            )
            sleep_hold = threading.Event()
            sleeping = threading.Event()

            def blocking_sleeper(seconds: float) -> None:
                sleeping.set()
                sleep_hold.wait(WAIT_TIMEOUT_SECONDS * 2)

            controller = _make_controller(
                world, executor=executor, max_sessions=2, sleeper=blocking_sleeper
            )

            first = _start(controller, repo, chat="Chat A")
            self.assertTrue(sleeping.wait(WAIT_TIMEOUT_SECONDS))

            # A is captive inside its own backoff sleep; B starts, runs
            # Codex, performs its step, and completes while A never wakes.
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            self.assertEqual(controller._sessions[second.run_id].chatgpt_wait_count, 0)

            controller.request_cancel(first.run_id)
            sleep_hold.set()
            _join_workers(controller)

    def test_worker_exception_in_a_does_not_mutate_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.queue_step(
                "run-1",
                FakeStepResult(),
                exception=RuntimeError("scripted worker crash"),
            )
            world.set_model("run-2", _routine_model("run-2", repo))
            world.queue_step(
                "run-2",
                _success_step_result(),
                next_model=_completed_model("run-2", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")

            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id)
                    in {"waiting_for_retry", "blocked"}
                )
            )
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            runtime_b = controller._sessions[second.run_id]
            self.assertIsNone(runtime_b.last_exception_summary)
            failure_runs = {
                event["run_id"]
                for event in controller.ledger.added_events
                if event["event_type"] == "local_controller_action_failed"
            }
            self.assertEqual(failure_runs, {first.run_id})
            _join_workers(controller)


# ---------------------------------------------------------------------------
# Cancellation isolation
# ---------------------------------------------------------------------------


class MultiSessionCancellationTests(unittest.TestCase):
    def test_cancel_a_terminates_only_a_codex_and_leaves_b_running(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            world.set_model("run-1", _completed_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            terminated: list[str] = []
            with mock.patch(
                "agent.local_controller.terminate_codex_run",
                side_effect=lambda run_id: terminated.append(run_id) or {"terminated": True},
            ):
                cancelled = controller.request_cancel(first.run_id)

            self.assertTrue(cancelled.ok)
            self.assertEqual(terminated, [first.run_id])
            self.assertTrue(controller._sessions[first.run_id].cancel_requested.is_set())
            self.assertFalse(controller._sessions[second.run_id].cancel_requested.is_set())
            self.assertEqual(_runtime_state(controller, first.run_id), "blocked")

            failed_run_ids = {
                call["run_id"] for call in controller.ledger.update_run_status_calls
            }
            self.assertEqual(failed_run_ids, {first.run_id})

            record_b.release.set()
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            record_a.release.set()
            _join_workers(controller)
            self.assertEqual(_runtime_state(controller, first.run_id), "blocked")

    def test_cancel_a_while_waiting_for_chatgpt_stops_only_a(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.repeat_step("run-1", _wait_step_result())
            world.set_model("run-2", _routine_model("run-2", repo))
            world.repeat_step("run-2", _wait_step_result())
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(lambda: controller._sessions[first.run_id].chatgpt_wait_count >= 1)
            )
            self.assertTrue(
                _wait_until(lambda: controller._sessions[second.run_id].chatgpt_wait_count >= 1)
            )

            controller.request_cancel(first.run_id)
            worker_a = controller._sessions[first.run_id].current_worker
            worker_a.join(WAIT_TIMEOUT_SECONDS)
            self.assertFalse(worker_a.is_alive())
            self.assertEqual(_runtime_state(controller, first.run_id), "blocked")

            # B keeps waiting normally: its wait counter keeps rising and it
            # never inherits A's cancellation or blocked state.
            waits_before = controller._sessions[second.run_id].chatgpt_wait_count
            self.assertTrue(
                _wait_until(
                    lambda: controller._sessions[second.run_id].chatgpt_wait_count
                    > waits_before
                )
            )
            self.assertFalse(controller._sessions[second.run_id].cancel_requested.is_set())

            controller.request_cancel(second.run_id)
            _join_workers(controller)

    def test_cancel_a_clears_only_a_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _approval_model("run-1", repo))
            world.set_model("run-2", _approval_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id)
                    == "waiting_for_approval"
                    and _runtime_state(controller, second.run_id)
                    == "waiting_for_approval"
                )
            )

            controller.request_cancel(first.run_id)
            self.assertIsNone(controller._sessions[first.run_id].pending_approval)
            self.assertEqual(_runtime_state(controller, first.run_id), "blocked")
            runtime_b = controller._sessions[second.run_id]
            self.assertIsNotNone(runtime_b.pending_approval)
            self.assertEqual(runtime_b.controller_state, "waiting_for_approval")
            _join_workers(controller)

    def test_cancel_a_in_waiting_for_retry_leaves_b_alone(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.queue_step(
                "run-1",
                FakeStepResult(
                    ok=False,
                    reason_code="gpt_feedback_generation_failed",
                    error_message="scripted failure",
                    action_executed=False,
                ),
            )
            world.set_model("run-2", _completed_model("run-2", repo))
            blocking_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id)
                    == "waiting_for_retry"
                )
            )
            self.assertTrue(blocking_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            controller.request_cancel(first.run_id)
            self.assertEqual(_runtime_state(controller, first.run_id), "blocked")
            self.assertTrue(controller._sessions[second.run_id].action_running)
            self.assertFalse(controller._sessions[second.run_id].cancel_requested.is_set())

            blocking_b.release.set()
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            _join_workers(controller)


# ---------------------------------------------------------------------------
# Approval isolation
# ---------------------------------------------------------------------------


class MultiSessionApprovalTests(unittest.TestCase):
    def _controller_with_two_pending_approvals(self, repo: str):
        world = SessionWorld()
        executor = PerRunInitialExecutor()
        world.set_model("run-1", _approval_model("run-1", repo))
        world.set_model("run-2", _approval_model("run-2", repo))
        controller = _make_controller(world, executor=executor, max_sessions=2)
        first = _start(controller, repo, chat="Chat A")
        second = _start(controller, repo, chat="Chat B")
        assert _wait_until(
            lambda: _runtime_state(controller, first.run_id) == "waiting_for_approval"
            and not controller._sessions[first.run_id].action_running
            and _runtime_state(controller, second.run_id) == "waiting_for_approval"
            and not controller._sessions[second.run_id].action_running
        )
        return world, controller, first.run_id, second.run_id

    def test_approving_a_leaves_b_pending_and_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world, controller, run_a, run_b = self._controller_with_two_pending_approvals(repo)
            snapshot_b = controller._sessions[run_b].pending_approval
            world.queue_step(
                run_a,
                _success_step_result("ask_send_to_gpt"),
                next_model=_completed_model(run_a, repo),
            )

            decided = controller.submit_approval_decision("approved", run_a)
            self.assertTrue(decided.ok)
            self.assertEqual(decided.run_id, run_a)
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, run_a) == "completed")
            )

            runtime_b = controller._sessions[run_b]
            self.assertEqual(runtime_b.controller_state, "waiting_for_approval")
            self.assertIs(runtime_b.pending_approval, snapshot_b)
            interactive_calls = [
                call
                for call in world.step_calls
                if call["approval_mode"] == "interactive"
            ]
            self.assertEqual([call["run_id"] for call in interactive_calls], [run_a])
            _join_workers(controller)

    def test_rejecting_a_changes_only_a(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world, controller, run_a, run_b = self._controller_with_two_pending_approvals(repo)
            world.queue_step(
                run_a,
                _success_step_result("ask_send_to_gpt"),
                next_model=_blocked_model(run_a, repo),
            )

            decided = controller.submit_approval_decision("rejected", run_a)
            self.assertTrue(decided.ok)
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, run_a) == "blocked")
            )
            runtime_b = controller._sessions[run_b]
            self.assertEqual(runtime_b.controller_state, "waiting_for_approval")
            self.assertIsNotNone(runtime_b.pending_approval)
            _join_workers(controller)

    def test_approval_targets_fail_closed_on_wrong_or_missing_run(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _approval_model("run-1", repo))
            world.set_model("run-2", _routine_model("run-2", repo))
            world.queue_step(
                "run-2",
                _success_step_result(),
                next_model=_completed_model("run-2", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=2)
            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id)
                    == "waiting_for_approval"
                    and _runtime_state(controller, second.run_id) == "completed"
                )
            )

            wrong_target = controller.submit_approval_decision("approved", second.run_id)
            self.assertFalse(wrong_target.ok)
            self.assertEqual(wrong_target.reason_code, "no_pending_approval")

            unknown = controller.submit_approval_decision("approved", "run-missing")
            self.assertFalse(unknown.ok)
            self.assertEqual(unknown.reason_code, "no_pending_approval")

            # A's approval is still intact after both failed attempts.
            runtime_a = controller._sessions[first.run_id]
            self.assertEqual(runtime_a.controller_state, "waiting_for_approval")
            self.assertIsNotNone(runtime_a.pending_approval)
            _join_workers(controller)


# ---------------------------------------------------------------------------
# Terminal absorption
# ---------------------------------------------------------------------------


class MultiSessionTerminalTests(unittest.TestCase):
    def test_terminal_a_is_absorbing_and_siblings_continue(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.repeat_step("run-1", _wait_step_result())
            world.set_model("run-2", _routine_model("run-2", repo))
            record_b = executor.configure("run-2", blocking=True)
            world.queue_step(
                "run-2",
                _success_step_result(),
                next_model=_completed_model("run-2", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(lambda: controller._sessions[first.run_id].chatgpt_wait_count >= 1)
            )
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            controller.request_cancel(first.run_id)
            worker_a = controller._sessions[first.run_id].current_worker
            worker_a.join(WAIT_TIMEOUT_SECONDS)
            self.assertFalse(worker_a.is_alive())

            # No tick, approval, or retry can restart the cancelled run.
            tick = controller.request_automatic_progress(first.run_id)
            self.assertFalse(tick.ok)
            self.assertEqual(tick.reason_code, "controller_state_terminal")
            approval = controller.submit_approval_decision("approved", first.run_id)
            self.assertFalse(approval.ok)

            record_b.release.set()
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, second.run_id) == "completed")
            )
            _join_workers(controller)


# ---------------------------------------------------------------------------
# Focused/current compatibility
# ---------------------------------------------------------------------------


class MultiSessionFocusCompatibilityTests(unittest.TestCase):
    def test_focus_follows_newest_start_while_background_progresses(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            resume_a = threading.Event()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.queue_step("run-1", _wait_step_result())
            world.queue_step("run-1", _wait_step_result())
            world.queue_step(
                "run-1",
                _success_step_result(),
                next_model=_completed_model("run-1", repo),
                gate=resume_a,
            )
            world.set_model("run-2", _completed_model("run-2", repo))
            record_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=2)

            first = _start(controller, repo, chat="Chat A")
            self.assertTrue(
                _wait_until(lambda: controller._sessions[first.run_id].chatgpt_wait_count >= 1)
            )

            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            # Legacy current-run APIs address the focused (newest) run.
            self.assertEqual(controller.session.active_run_id, second.run_id)
            current_state = controller.get_current_state()
            self.assertEqual(current_state.run_id, second.run_id)
            progress = controller.get_current_progress()
            self.assertEqual(progress.metadata["progress"]["run_id"], second.run_id)

            # The unfocused background session keeps progressing to
            # completion while focus stays on B.
            resume_a.set()
            self.assertTrue(
                _wait_until(lambda: _runtime_state(controller, first.run_id) == "completed")
            )
            self.assertEqual(controller.session.active_run_id, second.run_id)
            self.assertEqual(
                controller.session.controller_state,
                controller._sessions[second.run_id].controller_state,
            )

            record_b.release.set()
            _join_workers(controller)
            self.assertEqual(_runtime_state(controller, second.run_id), "completed")


# ---------------------------------------------------------------------------
# Stage 3 lane under real session workers (real ledger queue)
# ---------------------------------------------------------------------------


class RealQueueLaneStep:
    """Fake ChatGPT slice that takes turns on the real ledger handoff queue.

    Each call enqueues (idempotent while an entry is active) and claims only
    when this run is the FIFO head, mirroring the production lane order. A
    claimed slice can be held open to simulate lane occupancy.
    """

    def __init__(self, world: SessionWorld, repo: str) -> None:
        self.world = world
        self.repo = repo
        self._lock = threading.Lock()
        self.active_claims = 0
        self.max_active_claims = 0
        self.slices: list[str] = []
        self.permits: dict[str, threading.Event] = {}
        self.claimed_signals: dict[str, threading.Event] = {}
        self.hold_run_ids: set[str] = set()
        self.releases: dict[str, threading.Event] = {}

    def event_for(self, table: dict[str, threading.Event], run_id: str) -> threading.Event:
        with self._lock:
            return table.setdefault(run_id, threading.Event())

    def permit(self, run_id: str) -> None:
        self.event_for(self.permits, run_id).set()

    def hold(self, run_id: str) -> None:
        with self._lock:
            self.hold_run_ids.add(run_id)

    def release(self, run_id: str) -> None:
        self.event_for(self.releases, run_id).set()

    def __call__(self, run_id: str, repository_path: str, sandbox: str, **kwargs: Any) -> Any:
        self.event_for(self.permits, run_id).wait(WAIT_TIMEOUT_SECONDS * 2)
        ledger_module.enqueue_chatgpt_handoff(run_id, enqueue_source="stage4_test")
        claim = ledger_module.claim_chatgpt_handoff_for_run(
            run_id,
            claim_owner_identifier=f"stage4-{run_id}",
        )
        if claim.status != ledger_module.AtomicChatGPTHandoffQueueStatus.CLAIMED:
            return _wait_step_result()
        with self._lock:
            self.active_claims += 1
            self.max_active_claims = max(self.max_active_claims, self.active_claims)
            self.slices.append(run_id)
            should_hold = run_id in self.hold_run_ids
        self.event_for(self.claimed_signals, run_id).set()
        if should_hold:
            self.event_for(self.releases, run_id).wait(WAIT_TIMEOUT_SECONDS * 2)
        with self._lock:
            self.active_claims -= 1
        ledger_module.complete_chatgpt_handoff(
            claim.queue_sequence,
            claim_owner_identifier=f"stage4-{run_id}",
            reason_code="chatgpt_handoff_slice_completed",
        )
        self.world.set_model(run_id, _completed_model(run_id, self.repo))
        return _success_step_result()


class MultiSessionRealLaneTests(unittest.TestCase):
    def _real_lane_controller(self, repo: str):
        world = SessionWorld()
        world.default_model_factory = lambda run_id: _routine_model(run_id, repo)
        lane = RealQueueLaneStep(world, repo)
        controller = LocalController(
            session=LocalControllerSession(),
            ledger=ledger_module,
            read_model_builder=world.read_model_builder,
            supervision_step=lane,
            initial_run_executor=PerRunInitialExecutor(),
            max_active_sessions=4,
            chatgpt_wait_sleeper=lambda seconds: time.sleep(0.005),
            desktop_mutex=object(),
        )
        return world, lane, controller

    def test_real_queue_serializes_session_workers_in_fifo_order(self) -> None:
        with _temporary_real_ledger(), tempfile.TemporaryDirectory() as repo:
            world, lane, controller = self._real_lane_controller(repo)

            first = _start(controller, repo, chat="Chat A")
            lane.hold(first.run_id)
            lane.permit(first.run_id)
            self.assertTrue(
                lane.event_for(lane.claimed_signals, first.run_id).wait(WAIT_TIMEOUT_SECONDS)
            )

            second = _start(controller, repo, chat="Chat B")
            lane.permit(second.run_id)
            self.assertTrue(
                _wait_until(lambda: controller._sessions[second.run_id].chatgpt_wait_count >= 1)
            )

            third = _start(controller, repo, chat="Chat C")
            lane.permit(third.run_id)
            self.assertTrue(
                _wait_until(lambda: controller._sessions[third.run_id].chatgpt_wait_count >= 1)
            )

            lane.release(first.run_id)
            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id) == "completed"
                    and _runtime_state(controller, second.run_id) == "completed"
                    and _runtime_state(controller, third.run_id) == "completed",
                    timeout=15.0,
                )
            )

            self.assertEqual(lane.slices, [first.run_id, second.run_id, third.run_id])
            self.assertEqual(lane.max_active_claims, 1)
            _join_workers(controller)

    def test_cancel_queued_b_removes_only_bs_work_and_terminal_b_cannot_reenter(self) -> None:
        with _temporary_real_ledger(), tempfile.TemporaryDirectory() as repo:
            world, lane, controller = self._real_lane_controller(repo)

            first = _start(controller, repo, chat="Chat A")
            lane.hold(first.run_id)
            lane.permit(first.run_id)
            self.assertTrue(
                lane.event_for(lane.claimed_signals, first.run_id).wait(WAIT_TIMEOUT_SECONDS)
            )

            second = _start(controller, repo, chat="Chat B")
            lane.permit(second.run_id)
            self.assertTrue(
                _wait_until(lambda: controller._sessions[second.run_id].chatgpt_wait_count >= 1)
            )

            third = _start(controller, repo, chat="Chat C")
            lane.permit(third.run_id)
            self.assertTrue(
                _wait_until(lambda: controller._sessions[third.run_id].chatgpt_wait_count >= 1)
            )

            # Cancel B while it is queued behind A. Its run becomes terminal
            # (failed), its worker exits, and its pending queue entry must be
            # expired by the next claimer rather than ever being claimed.
            controller.request_cancel(second.run_id)
            worker_b = controller._sessions[second.run_id].current_worker
            worker_b.join(WAIT_TIMEOUT_SECONDS)
            self.assertFalse(worker_b.is_alive())

            lane.release(first.run_id)
            self.assertTrue(
                _wait_until(
                    lambda: _runtime_state(controller, first.run_id) == "completed"
                    and _runtime_state(controller, third.run_id) == "completed",
                    timeout=15.0,
                )
            )
            self.assertEqual(lane.slices, [first.run_id, third.run_id])
            self.assertNotIn(second.run_id, lane.slices)
            self.assertEqual(_runtime_state(controller, second.run_id), "blocked")

            # Terminal B is refused at both enqueue and claim: it can never
            # reacquire the ChatGPT lane later.
            reenqueue = ledger_module.enqueue_chatgpt_handoff(
                second.run_id, enqueue_source="stage4_terminal_probe"
            )
            self.assertEqual(
                reenqueue.status,
                ledger_module.AtomicChatGPTHandoffQueueStatus.RUN_TERMINAL,
            )
            reclaim = ledger_module.claim_chatgpt_handoff_for_run(
                second.run_id,
                claim_owner_identifier=f"stage4-{second.run_id}",
            )
            self.assertEqual(
                reclaim.status,
                ledger_module.AtomicChatGPTHandoffQueueStatus.RUN_TERMINAL,
            )
            _join_workers(controller)


# ---------------------------------------------------------------------------
# SQLite under concurrent multi-run writers
# ---------------------------------------------------------------------------


class SQLiteConcurrentWriterTests(unittest.TestCase):
    def test_concurrent_event_writes_for_four_runs_stay_stable(self) -> None:
        with _temporary_real_ledger():
            run_ids = [ledger_module.create_run(f"Concurrency run {i}") for i in range(4)]
            baseline = {
                run_id: len(ledger_module.list_events(run_id)) for run_id in run_ids
            }
            errors: list[BaseException] = []
            per_thread_events = 50

            def writer(run_id: str) -> None:
                try:
                    for index in range(per_thread_events):
                        ledger_module.add_event(
                            run_id,
                            "stage4_concurrent_write",
                            f"event {index}",
                            {"index": index},
                        )
                        if index % 10 == 0:
                            ledger_module.update_run_status(
                                run_id, RunStatus.RUNNING
                            )
                except BaseException as exc:  # noqa: BLE001 - collected for assertion
                    errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=(run_id,), daemon=True)
                for run_id in run_ids
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)

            self.assertEqual(errors, [])
            for run_id in run_ids:
                events = [
                    event
                    for event in ledger_module.list_events(run_id)
                    if event["event_type"] == "stage4_concurrent_write"
                ]
                self.assertEqual(len(events), per_thread_events)
                self.assertGreaterEqual(
                    len(ledger_module.list_events(run_id)), baseline[run_id]
                )

    def test_concurrent_queue_cycles_across_four_runs_complete(self) -> None:
        with _temporary_real_ledger():
            run_ids = [ledger_module.create_run(f"Queue run {i}") for i in range(4)]
            errors: list[BaseException] = []
            completions = {run_id: 0 for run_id in run_ids}
            cycles_per_run = 5

            def lane_worker(run_id: str) -> None:
                try:
                    deadline = time.monotonic() + 25
                    while completions[run_id] < cycles_per_run:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"lane cycles did not finish for {run_id}")
                        ledger_module.enqueue_chatgpt_handoff(
                            run_id, enqueue_source="stage4_sqlite_test"
                        )
                        claim = ledger_module.claim_chatgpt_handoff_for_run(
                            run_id,
                            claim_owner_identifier=f"sqlite-{run_id}",
                        )
                        if claim.status != ledger_module.AtomicChatGPTHandoffQueueStatus.CLAIMED:
                            time.sleep(0.002)
                            continue
                        ledger_module.complete_chatgpt_handoff(
                            claim.queue_sequence,
                            claim_owner_identifier=f"sqlite-{run_id}",
                            reason_code="chatgpt_handoff_slice_completed",
                        )
                        completions[run_id] += 1
                except BaseException as exc:  # noqa: BLE001 - collected for assertion
                    errors.append(exc)

            threads = [
                threading.Thread(target=lane_worker, args=(run_id,), daemon=True)
                for run_id in run_ids
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)

            self.assertEqual(errors, [])
            self.assertEqual(
                completions, {run_id: cycles_per_run for run_id in run_ids}
            )


if __name__ == "__main__":
    unittest.main()
