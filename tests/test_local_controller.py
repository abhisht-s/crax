from __future__ import annotations

import json
import threading
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from agent.local_controller import (
    LOCAL_CONTROLLER_ALLOWED_SANDBOXES,
    LOCAL_CONTROLLER_METADATA_VERSION,
    LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE,
    LOCAL_CONTROLLER_SOURCE,
    InitialRunExecutionResult,
    LocalController,
    LocalControllerEventTimelineRow,
    LocalControllerReadModel,
    LocalControllerSession,
    StartRequestValidationResult,
    build_local_controller_read_model,
    create_pending_approval_snapshot,
    start_local_controller_run,
    validate_local_controller_start_request,
)
from agent.run_state import RunStatus
from agent.supervise import SuperviseAction, SupervisePlan


class FakeLedger:
    def __init__(self, run: dict | None = None, events: list[dict] | None = None) -> None:
        self.run = run
        self.events = list(events or [])
        self.added_events: list[dict] = []
        self.create_run_calls: list[str] = []
        self.get_run_calls: list[str] = []
        self.list_events_calls: list[str] = []
        self._next_run_number = 1
        self._next_event_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1

    def create_run(self, user_instruction: str) -> str:
        self.create_run_calls.append(user_instruction)
        run_id = f"run-{self._next_run_number}"
        self._next_run_number += 1
        self.run = {
            "id": run_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "status": RunStatus.CREATED.value,
            "user_instruction": user_instruction,
            "final_summary": None,
            "error": None,
        }
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        self.get_run_calls.append(run_id)
        if self.run is None or self.run.get("id") != run_id:
            return None
        return self.run

    def list_events(self, run_id: str) -> list[dict]:
        self.list_events_calls.append(run_id)
        return self.events

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None):
        event = _event(self._next_event_id, event_type, metadata, message=message, run_id=run_id)
        self._next_event_id += 1
        self.events.append(event)
        self.added_events.append(event)
        return event


class Planner:
    def __init__(self, plan: SupervisePlan) -> None:
        self.plan = plan
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> SupervisePlan:
        self.calls.append((args, kwargs))
        return self.plan


@dataclass
class FakeStepResult:
    ok: bool = True
    reason_code: str | None = "step_ok"
    error_message: str | None = None
    action_executed: bool = True
    terminal: bool = False
    completed: bool = False
    blocked: bool = False
    requires_human_approval: bool = False
    planner_action: str | None = "capture_gpt_response"
    planner_reason_code: str | None = "routine"
    next_state_hint: str | None = None
    run_status: str | None = "completed"


class StepRecorder:
    def __init__(self, result: object | None = None, *, exception: Exception | None = None) -> None:
        self.result = result if result is not None else FakeStepResult()
        self.exception = exception
        self.calls: list[tuple[tuple, dict]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.entered.set()
        if self.block:
            self.release.wait(2)
        if self.exception is not None:
            raise self.exception
        return self.result


class ReadModelSequence:
    def __init__(self, *models: LocalControllerReadModel, repeat_last: bool = True) -> None:
        self.models = list(models)
        self.repeat_last = repeat_last
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> LocalControllerReadModel:
        self.calls.append((args, kwargs))
        if not self.models:
            raise AssertionError("read model sequence is empty")
        if len(self.models) == 1 and self.repeat_last:
            return self.models[0]
        return self.models.pop(0)


def _event(
    event_id: int,
    event_type: str,
    metadata: dict | None = None,
    *,
    message: str | None = None,
    run_id: str = "run-1",
) -> dict:
    return {
        "id": event_id,
        "run_id": run_id,
        "created_at": f"2026-01-01T00:00:{event_id:02d}+00:00",
        "event_type": event_type,
        "message": message or event_type,
        "metadata": metadata,
        "metadata_json": json.dumps(metadata or {}, sort_keys=True),
    }


def _run(status: str = RunStatus.COMPLETED.value) -> dict:
    return {
        "id": "run-1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "status": status,
        "user_instruction": "Initial task",
        "final_summary": None,
        "error": None,
    }


def _controller_event(repo_path: str, sandbox: str = "read-only", event_id: int = 1) -> dict:
    resolved_repo_path = str(Path(repo_path).expanduser().resolve(strict=False))
    return _event(
        event_id,
        LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE,
        {
            "metadata_version": LOCAL_CONTROLLER_METADATA_VERSION,
            "repository_path": resolved_repo_path,
            "sandbox": sandbox,
            "source": LOCAL_CONTROLLER_SOURCE,
            "controller_mode": "browser_v1",
            "browser_safe_sandbox": True,
        },
        message="Local controller run initialized.",
    )


def _send_plan(*, changed_files_count: int | None = 1) -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.ASK_SEND_TO_GPT,
        reason="codex_result_ready",
        event_ids={"codex_exec_finished": 10},
        repo_path="/repo",
        sandbox="read-only",
        status="completed",
        codex_exit_code=0,
        codex_timed_out=False,
        codex_sandbox="read-only",
        changed_files_count=changed_files_count,
        supervision_decision="continue",
    )


def _run_prompt_plan(prompt: str = "Run this") -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.ASK_RUN_PROMPT,
        reason="fresh_sentinel_prompt_ready",
        event_ids={
            "codex_exec_finished": 10,
            "gpt_feedback_submission_verified": 11,
            "gpt_response_captured": 12,
            "next_codex_prompt_extracted": 13,
        },
        prompt_preview=prompt,
        prompt_text=prompt,
        prompt_sha=_sha(prompt),
        extraction_method="sentinel_block",
        repo_path="/repo",
        sandbox="read-only",
        status="completed",
        prompt_auto_run_safe=False,
        prompt_auto_run_reason="workspace_write_scope_not_inferred",
    )


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model(
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
        run_id="run-1",
        run_status="completed",
        initial_instruction="Initial task",
        repository_path=repo_path,
        sandbox=sandbox,
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


class LocalControllerSessionTests(unittest.TestCase):
    def test_session_token_is_high_entropy_and_initial_state_is_empty(self) -> None:
        session = LocalControllerSession()

        self.assertGreaterEqual(len(session.token), 40)
        self.assertNotEqual(session.token, LocalControllerSession().token)
        self.assertIsNone(session.active_run_id)
        self.assertIsNone(session.pending_approval)
        self.assertEqual(session.controller_state, "idle")

    def test_token_is_not_written_to_ledger_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            session = LocalControllerSession()
            ledger = FakeLedger()
            request = validate_local_controller_start_request(repo, "Task", "read-only")

            result = start_local_controller_run(session, request, ledger=ledger)

        self.assertTrue(result.ok)
        metadata_text = json.dumps([event.get("metadata") for event in ledger.events], sort_keys=True)
        self.assertNotIn(session.token, metadata_text)
        self.assertNotIn(session.session_id, metadata_text)
        self.assertNotIn("pending_approval", metadata_text)


class LocalControllerValidationTests(unittest.TestCase):
    def test_valid_existing_directory_for_allowed_sandboxes(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            for sandbox in LOCAL_CONTROLLER_ALLOWED_SANDBOXES:
                with self.subTest(sandbox=sandbox):
                    result = validate_local_controller_start_request(repo, " Task ", sandbox)
                    self.assertTrue(result.ok)
                    self.assertEqual(result.repository_path, str(Path(repo).resolve(strict=False)))
                    self.assertEqual(result.initial_instruction, "Task")
                    self.assertEqual(result.sandbox, sandbox)

    def test_validation_failures_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            file_path = Path(repo) / "file.txt"
            file_path.write_text("x", encoding="utf-8")

            cases = [
                ("", "Task", "read-only", "repository_path_required"),
                (str(Path(repo) / "missing"), "Task", "read-only", "repository_path_not_found"),
                (str(file_path), "Task", "read-only", "repository_path_not_directory"),
                (repo, " ", "read-only", "initial_instruction_required"),
                (repo, "Task", "bad", "invalid_browser_sandbox"),
                (repo, "Task", "danger-full-access", "danger_full_access_not_available_in_local_controller"),
            ]
            for path, instruction, sandbox, reason in cases:
                with self.subTest(reason=reason):
                    result = validate_local_controller_start_request(path, instruction, sandbox)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.reason_code, reason)


class LocalControllerRunCreationTests(unittest.TestCase):
    def test_start_creates_run_and_writes_only_controller_metadata_event(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            session = LocalControllerSession()
            ledger = FakeLedger()
            request = validate_local_controller_start_request(repo, "Task", "workspace-write")

            with (
                mock.patch("agent.codex_services.execute_codex_direct_service") as codex,
                mock.patch("agent.chatgpt_services.submit_feedback_to_chatgpt_service") as submit,
                mock.patch("agent.chatgpt_services.capture_chatgpt_response_service") as capture,
                mock.patch("agent.chatgpt_services.extract_next_codex_prompt_service") as extract,
            ):
                result = start_local_controller_run(session, request, ledger=ledger)

        self.assertTrue(result.ok)
        self.assertEqual(ledger.create_run_calls, ["Task"])
        self.assertEqual(len([event for event in ledger.events if event["event_type"] == "run_created"]), 1)
        controller_events = [event for event in ledger.events if event["event_type"] == LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE]
        self.assertEqual(len(controller_events), 1)
        metadata = controller_events[0]["metadata"]
        self.assertEqual(metadata["metadata_version"], LOCAL_CONTROLLER_METADATA_VERSION)
        self.assertEqual(metadata["repository_path"], str(Path(repo).resolve(strict=False)))
        self.assertEqual(metadata["sandbox"], "workspace-write")
        self.assertEqual(metadata["source"], LOCAL_CONTROLLER_SOURCE)
        self.assertTrue(metadata["browser_safe_sandbox"])
        self.assertNotIn("token", metadata)
        self.assertNotIn("session_id", metadata)
        self.assertNotIn("pending_approval", metadata)
        self.assertEqual(session.active_run_id, "run-1")
        self.assertEqual(session.controller_state, "idle")
        self.assertIsNone(session.pending_approval)
        self.assertEqual(ledger.run["status"], RunStatus.CREATED.value)
        codex.assert_not_called()
        submit.assert_not_called()
        capture.assert_not_called()
        extract.assert_not_called()

    def test_start_defensively_rejects_fabricated_full_access_validated_request(self) -> None:
        session = LocalControllerSession()
        ledger = FakeLedger()
        request = StartRequestValidationResult(
            ok=True,
            repository_path="/tmp",
            initial_instruction="Task",
            sandbox="danger-full-access",
        )

        result = start_local_controller_run(session, request, ledger=ledger)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "danger_full_access_not_available_in_local_controller")
        self.assertEqual(ledger.create_run_calls, [])
        self.assertEqual(ledger.events, [])


class LocalControllerReadModelTests(unittest.TestCase):
    def test_read_model_rehydrates_repo_and_sandbox_from_controller_event_not_session(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            events = [_controller_event(repo, "workspace-write"), _event(2, "codex_exec_finished", {"exit_code": 0})]
            ledger = FakeLedger(_run(), events)
            session = LocalControllerSession()
            session.active_run_id = "different-run"
            planner = Planner(_send_plan())

            model = build_local_controller_read_model(
                "run-1",
                ledger=ledger,
                session=session,
                planner=planner,
            )

        self.assertTrue(model.configuration_complete)
        self.assertEqual(model.repository_path, str(Path(repo).resolve(strict=False)))
        self.assertEqual(model.sandbox, "workspace-write")
        self.assertEqual(planner.calls[0][0][2], str(Path(repo).resolve(strict=False)))
        self.assertEqual(planner.calls[0][1]["sandbox"], "workspace-write")
        self.assertEqual(model.controller_runtime["active_run_id"], "different-run")

    def test_missing_controller_metadata_is_configuration_incomplete_and_does_not_plan(self) -> None:
        ledger = FakeLedger(_run(), [_event(1, "run_created", None)])
        planner = Planner(_send_plan())

        model = build_local_controller_read_model("run-1", ledger=ledger, planner=planner)

        self.assertFalse(model.configuration_complete)
        self.assertEqual(model.current_stage, "run_configuration_missing")
        self.assertTrue(model.blocked)
        self.assertEqual(planner.calls, [])

    def test_latest_event_id_and_timeline_order_are_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            events = [
                _controller_event(repo, event_id=4),
                _event(7, "codex_exec_finished", {"exit_code": 0, "stdout": "hello"}),
                _event(8, "supervision_decision", {"decision": "continue"}),
            ]
            ledger = FakeLedger(_run(), events)
            model = build_local_controller_read_model("run-1", ledger=ledger, planner=Planner(_send_plan()))

        self.assertEqual(model.latest_event_id, 8)
        self.assertEqual([row.event_id for row in model.event_timeline], [4, 7, 8])
        self.assertEqual(ledger.added_events, [])


class LocalControllerSummaryTests(unittest.TestCase):
    def test_summaries_and_timeline_exclude_raw_output_and_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            events = [
                _controller_event(repo, event_id=1),
                _event(
                    2,
                    "codex_exec_finished",
                    {
                        "found": True,
                        "exit_code": 0,
                        "timed_out": False,
                        "validation_error": None,
                        "sandbox": "read-only",
                        "repo_path": repo,
                        "stdout": "secret stdout" * 80,
                        "stderr": "secret stderr" * 80,
                    },
                ),
                _event(
                    3,
                    "gpt_feedback_submission_verified",
                    {
                        "reason_code": "chatgpt_submission_verified",
                        "submission_marker_sha256": "marker-sha",
                        "feedback_payload_length": 123,
                    },
                ),
                _event(
                    4,
                    "gpt_response_captured",
                    {
                        "response_text": "full response" * 80,
                        "response_sha256": "response-sha",
                        "sentinel_state": "complete_sentinel_stable",
                        "stability": {"stable": True},
                        "reason_code": "complete_sentinel_stable",
                    },
                ),
                _event(
                    5,
                    "next_codex_prompt_extracted",
                    {
                        "prompt_text": "full prompt" * 80,
                        "prompt_sha256": "prompt-sha",
                        "prompt_length": 99,
                        "extraction_method": "sentinel_block",
                        "source_event_id": 4,
                        "warnings": ["warn"],
                    },
                ),
                _event(6, "supervision_decision", {"decision": "continue"}),
                _event(
                    7,
                    "workspace_write_post_run_policy",
                    {"post_run_policy": {"allowed": False, "reason_code": "post_run_diff_metadata_unavailable"}},
                ),
                _event(
                    8,
                    "run_status_transition",
                    {"next_status": "needs_review", "reason": "objective_governance_failure", "needs_review": True},
                ),
            ]
            model = build_local_controller_read_model("run-1", ledger=FakeLedger(_run(), events), planner=Planner(_send_plan()))

        self.assertEqual(model.latest_codex_result["stdout_length"], len("secret stdout" * 80))
        self.assertNotIn("stdout", model.latest_codex_result)
        self.assertEqual(model.latest_chatgpt_submission["state"], "verified")
        self.assertEqual(model.latest_chatgpt_capture["state"], "captured")
        self.assertEqual(model.latest_chatgpt_capture["response_sha256"], "response-sha")
        self.assertEqual(model.latest_prompt_extraction["prompt_sha256"], "prompt-sha")
        self.assertEqual(model.latest_prompt_extraction["source_capture_event_id"], 4)
        self.assertEqual(model.latest_governance["next_status"], "needs_review")
        self.assertTrue(model.latest_governance["human_review_required"])

        timeline_text = json.dumps([row.metadata_preview for row in model.event_timeline], sort_keys=True)
        self.assertNotIn("secret stdout", timeline_text)
        self.assertNotIn("secret stderr", timeline_text)
        self.assertNotIn("full response", timeline_text)
        self.assertNotIn("full prompt", timeline_text)
        self.assertIn("stdout_length", timeline_text)
        self.assertIn("prompt_text_length", timeline_text)


class LocalControllerApprovalSnapshotTests(unittest.TestCase):
    def test_send_snapshot_captures_planner_identity_in_memory_only(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            events = [_controller_event(repo), _event(10, "codex_exec_finished", {"exit_code": 0})]
            ledger = FakeLedger(_run(), events)
            model = build_local_controller_read_model("run-1", ledger=ledger, planner=Planner(_send_plan(changed_files_count=1)))

            result = create_pending_approval_snapshot(model)

        self.assertTrue(model.requires_human_approval)
        self.assertTrue(result.ok)
        snapshot = result.snapshot
        self.assertEqual(snapshot.approval_kind, "send_to_gpt")
        self.assertEqual(snapshot.planner_action, "ask_send_to_gpt")
        self.assertEqual(snapshot.planner_reason_code, "codex_result_ready")
        self.assertEqual(snapshot.latest_event_id, 10)
        self.assertIsNone(snapshot.expected_extraction_event_id)
        self.assertEqual(ledger.added_events, [])

    def test_run_snapshot_captures_prompt_and_extraction_identity(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            events = [_controller_event(repo), _event(13, "next_codex_prompt_extracted", {"prompt_sha256": "old"})]
            ledger = FakeLedger(_run(), events)
            model = build_local_controller_read_model("run-1", ledger=ledger, planner=Planner(_run_prompt_plan("Run A")))

            result = create_pending_approval_snapshot(model)

        self.assertTrue(result.ok)
        snapshot = result.snapshot
        self.assertEqual(snapshot.approval_kind, "run_prompt")
        self.assertEqual(snapshot.expected_extraction_event_id, 13)
        self.assertEqual(snapshot.expected_prompt_sha256, _sha("Run A"))
        self.assertEqual(snapshot.expected_prompt_text_sha256, _sha("Run A"))
        self.assertEqual(snapshot.expected_extraction_method, "sentinel_block")

    def test_no_snapshot_for_capture_extract_or_terminal_and_identity_changes_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            events = [_controller_event(repo)]
            capture_model = build_local_controller_read_model(
                "run-1",
                ledger=FakeLedger(_run(), events),
                planner=Planner(SupervisePlan(SuperviseAction.CAPTURE_GPT_RESPONSE, "capture")),
            )
            extract_model = build_local_controller_read_model(
                "run-1",
                ledger=FakeLedger(_run(), events),
                planner=Planner(SupervisePlan(SuperviseAction.EXTRACT_NEXT_PROMPT, "extract")),
            )
            terminal_model = build_local_controller_read_model(
                "run-1",
                ledger=FakeLedger(_run(), events),
                planner=Planner(SupervisePlan(SuperviseAction.STOP, "needs_review", stop_message="review")),
            )
            first = build_local_controller_read_model(
                "run-1",
                ledger=FakeLedger(_run(), events),
                planner=Planner(_run_prompt_plan("Run A")),
            )
            second = build_local_controller_read_model(
                "run-1",
                ledger=FakeLedger(_run(), events),
                planner=Planner(_run_prompt_plan("Run B")),
            )

        self.assertFalse(create_pending_approval_snapshot(capture_model).ok)
        self.assertFalse(create_pending_approval_snapshot(extract_model).ok)
        self.assertFalse(create_pending_approval_snapshot(terminal_model).ok)
        self.assertNotEqual(
            create_pending_approval_snapshot(first).snapshot.expected_prompt_sha256,
            create_pending_approval_snapshot(second).snapshot.expected_prompt_sha256,
        )


class LocalControllerExplicitNonActionTests(unittest.TestCase):
    def test_read_model_building_does_not_mutate_or_call_action_paths(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            ledger = FakeLedger(_run(), [_controller_event(repo), _event(2, "codex_exec_finished", {"exit_code": 0})])
            planner = Planner(_send_plan())

            with (
                mock.patch("subprocess.run") as subprocess_run,
                mock.patch("agent.codex_services.execute_codex_direct_service") as codex,
                mock.patch("agent.chatgpt_services.submit_feedback_to_chatgpt_service") as submit,
                mock.patch("agent.chatgpt_services.capture_chatgpt_response_service") as capture,
                mock.patch("agent.chatgpt_services.extract_next_codex_prompt_service") as extract,
                mock.patch("agent.supervision_services.run_supervision_step") as step,
            ):
                model = build_local_controller_read_model("run-1", ledger=ledger, planner=planner)

        self.assertEqual(ledger.added_events, [])
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(model.planner_action, "ask_send_to_gpt")
        subprocess_run.assert_not_called()
        codex.assert_not_called()
        submit.assert_not_called()
        capture.assert_not_called()
        extract.assert_not_called()
        step.assert_not_called()


class BlockingInitialExecutor:
    def __init__(self, result: object | None = None, *, exception: Exception | None = None) -> None:
        self.result = result if result is not None else InitialRunExecutionResult(ok=True)
        self.exception = exception
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        self.release.wait(2)
        if self.exception is not None:
            raise self.exception
        return self.result


class LocalControllerStateMachineTests(unittest.TestCase):
    def test_one_active_run_guard_blocks_second_start_and_race(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            executor = BlockingInitialExecutor()
            ledger = FakeLedger()
            controller = LocalController(ledger=ledger, initial_run_executor=executor)

            first = controller.start_run(
                repository_path=repo,
                initial_instruction="Task",
                sandbox="read-only",
            )
            self.assertTrue(executor.entered.wait(1))
            second = controller.start_run(
                repository_path=repo,
                initial_instruction="Task 2",
                sandbox="read-only",
            )

            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(second.reason_code, "active_run_exists")
            self.assertEqual(ledger.create_run_calls, ["Task"])
            self.assertEqual(
                len([event for event in ledger.events if event["event_type"] == LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE]),
                1,
            )
            executor.release.set()
            controller.current_worker.join(1)

        with tempfile.TemporaryDirectory() as repo:
            race_executor = BlockingInitialExecutor()
            race_ledger = FakeLedger()
            race_controller = LocalController(ledger=race_ledger, initial_run_executor=race_executor)
            results = []

            def start() -> None:
                results.append(
                    race_controller.start_run(
                        repository_path=repo,
                        initial_instruction="Race",
                        sandbox="read-only",
                    )
                )

            threads = [threading.Thread(target=start), threading.Thread(target=start)]
            for thread in threads:
                thread.start()
            self.assertTrue(race_executor.entered.wait(1))
            for thread in threads:
                thread.join(1)
            race_executor.release.set()
            race_controller.current_worker.join(1)

            self.assertEqual(sum(1 for result in results if result.ok), 1)
            self.assertEqual(sum(1 for result in results if result.reason_code == "active_run_exists"), 1)
            self.assertEqual(len(race_ledger.create_run_calls), 1)

    def test_initial_worker_is_async_and_uses_safe_executor_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            executor = BlockingInitialExecutor()
            step = StepRecorder()
            controller = LocalController(
                ledger=FakeLedger(),
                initial_run_executor=executor,
                supervision_step=step,
            )

            result = controller.start_run(
                repository_path=repo,
                initial_instruction="Task",
                sandbox="workspace-write",
                timeout_seconds=123,
            )

            self.assertTrue(result.ok)
            self.assertTrue(executor.entered.wait(1))
            state = controller.get_current_state("run-1").read_model.controller_runtime
            self.assertEqual(state["controller_state"], "starting_initial_codex")
            self.assertTrue(state["action_running"])
            self.assertEqual(state["current_action_kind"], "initial_codex")
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(executor.calls[0]["run_id"], "run-1")
            self.assertEqual(executor.calls[0]["initial_instruction"], "Task")
            self.assertEqual(executor.calls[0]["sandbox"], "workspace-write")
            self.assertEqual(executor.calls[0]["timeout_seconds"], 123)
            self.assertEqual(step.calls, [])

            executor.release.set()
            controller.current_worker.join(1)
            self.assertFalse(controller.action_running)

    def test_default_initial_executor_delegates_to_public_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            ledger = FakeLedger()
            read_models = ReadModelSequence(_model(action="stop", reason="no_action", stage="idle"))
            controller = LocalController(ledger=ledger, read_model_builder=read_models)
            with mock.patch(
                "agent.local_controller.execute_initial_direct_codex_run_service",
                return_value=InitialRunExecutionResult(ok=True, reason_code="supervision_decision_continue"),
            ) as coordinator:
                result = controller.start_run(
                    repository_path=repo,
                    initial_instruction="Task",
                    sandbox="read-only",
                    timeout_seconds=123,
                )
            controller.current_worker.join(1)
            state = controller.get_current_state("run-1").read_model.controller_runtime

        self.assertTrue(result.ok)
        coordinator.assert_called_once()
        args, kwargs = coordinator.call_args
        self.assertEqual(args[:5], ("run-1", ledger.run, "Task", str(Path(repo).resolve()), "read-only"))
        self.assertEqual(args[5], 123)
        self.assertIs(kwargs["ledger"], ledger)
        self.assertEqual(state["controller_state"], "idle")
        self.assertGreaterEqual(len(read_models.calls), 2)

    def test_routine_progression_calls_step_once_then_stops_at_approval(self) -> None:
        routine = _model(action="capture_gpt_response", routine=True, stage="routine_action_available")
        approval = _model(
            action="ask_send_to_gpt",
            reason="codex_result_ready",
            approval=True,
            approval_kind="send_to_gpt",
            stage="waiting_for_approval",
            planner_metadata={
                "action": "ask_send_to_gpt",
                "reason": "codex_result_ready",
                "event_ids": {"codex_exec_finished": 99},
                "prompt_sha": "",
            },
            latest_event_id=99,
        )
        read_models = ReadModelSequence(routine, routine, approval, approval, repeat_last=False)
        step = StepRecorder()
        session = LocalControllerSession(active_run_id="run-1")
        controller = LocalController(
            session=session,
            ledger=FakeLedger(_run()),
            read_model_builder=read_models,
            supervision_step=step,
            initial_run_executor=InitialRunExecutionResult,
        )

        result = controller.request_automatic_progress()
        controller.current_worker.join(1)

        self.assertTrue(result.ok)
        self.assertEqual(len(step.calls), 1)
        self.assertGreaterEqual(len(read_models.calls), 3)
        self.assertEqual(controller.session.controller_state, "waiting_for_approval")
        self.assertIsNotNone(controller.session.pending_approval)
        self.assertEqual(controller.session.pending_approval.latest_event_id, 99)

    def test_routine_progression_stops_at_blocked_and_respects_burst_limit(self) -> None:
        routine = _model(action="extract_next_prompt", routine=True, stage="routine_action_available")
        blocked = _model(
            action="stop",
            reason="needs_review",
            terminal=True,
            blocked=True,
            stage="blocked",
        )
        step = StepRecorder()
        controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(routine, routine, blocked, blocked, repeat_last=False),
            supervision_step=step,
        )

        controller.request_automatic_progress()
        controller.current_worker.join(1)

        self.assertEqual(len(step.calls), 1)
        self.assertEqual(controller.session.controller_state, "blocked")

        burst_step = StepRecorder()
        burst_controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(routine),
            supervision_step=burst_step,
            max_automatic_steps_per_worker=2,
        )
        burst_controller.request_automatic_progress()
        burst_controller.current_worker.join(1)

        self.assertEqual(len(burst_step.calls), 2)
        self.assertEqual(burst_controller.session.controller_state, "idle")
        self.assertEqual(burst_controller.automatic_burst_reason, "automatic_step_burst_limit_reached")

    def test_pending_approval_lifecycle_and_snapshot_identity(self) -> None:
        approval_model = _model(
            action="ask_run_prompt",
            reason="fresh_sentinel_prompt_ready",
            approval=True,
            approval_kind="run_prompt",
            stage="waiting_for_approval",
            planner_metadata={
                "action": "ask_run_prompt",
                "reason": "fresh_sentinel_prompt_ready",
                "event_ids": {"next_codex_prompt_extracted": 13, "codex_exec_finished": 10},
                "prompt_text": "Run A",
                "prompt_sha": _sha("Run A"),
                "extraction_method": "sentinel_block",
            },
            latest_event_id=13,
        )
        step = StepRecorder()
        controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(approval_model),
            supervision_step=step,
        )

        progress = controller.request_automatic_progress()
        self.assertFalse(progress.ok)
        self.assertEqual(progress.reason_code, "human_approval_required")
        self.assertEqual(step.calls, [])
        self.assertIsNotNone(controller.session.pending_approval)
        snapshot = controller.session.pending_approval

        decision = controller.submit_approval_decision("approved")
        controller.current_worker.join(1)

        self.assertTrue(decision.ok)
        self.assertEqual(len(step.calls), 1)
        args, kwargs = step.calls[0]
        self.assertEqual(args[:3], ("run-1", "/tmp", "read-only"))
        self.assertEqual(kwargs["approval_decision"], "approved")
        self.assertEqual(kwargs["expected_planner_action"], snapshot.planner_action)
        self.assertEqual(kwargs["expected_event_ids"], snapshot.planner_metadata["event_ids"])
        self.assertEqual(kwargs["expected_prompt_sha256"], _sha("Run A"))
        self.assertIsNone(controller.session.pending_approval)

    def test_rejected_and_invalid_approval_cases_are_structured(self) -> None:
        approval_model = _model(
            action="ask_send_to_gpt",
            reason="codex_result_ready",
            approval=True,
            approval_kind="send_to_gpt",
            planner_metadata={
                "action": "ask_send_to_gpt",
                "reason": "codex_result_ready",
                "event_ids": {"codex_exec_finished": 10},
            },
        )
        step = StepRecorder()
        controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(approval_model),
            supervision_step=step,
        )
        self.assertEqual(controller.submit_approval_decision("yes").reason_code, "invalid_approval_decision")
        self.assertEqual(controller.submit_approval_decision("approved").reason_code, "no_pending_approval")

        controller.request_automatic_progress()
        result = controller.submit_approval_decision("rejected")
        controller.current_worker.join(1)

        self.assertTrue(result.ok)
        self.assertEqual(step.calls[0][1]["approval_decision"], "rejected")
        self.assertEqual(len(controller.ledger.added_events), 0)

    def test_fresh_controller_reconstructs_state_but_not_pending_snapshot(self) -> None:
        approval_model = _model(
            action="ask_send_to_gpt",
            reason="codex_result_ready",
            approval=True,
            approval_kind="send_to_gpt",
            planner_metadata={
                "action": "ask_send_to_gpt",
                "reason": "codex_result_ready",
                "event_ids": {"codex_exec_finished": 10},
            },
        )
        step = StepRecorder()
        controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(approval_model),
            supervision_step=step,
        )

        state = controller.get_current_state("run-1")

        self.assertTrue(state.ok)
        self.assertTrue(state.read_model.requires_human_approval)
        self.assertFalse(state.read_model.controller_runtime["pending_approval_available"])
        self.assertEqual(
            state.read_model.controller_runtime["notice"],
            "approval_snapshot_unavailable_after_controller_restart",
        )
        self.assertEqual(step.calls, [])

    def test_concurrent_approval_and_progress_requests_do_not_launch_extra_workers(self) -> None:
        approval_model = _model(
            action="ask_send_to_gpt",
            reason="codex_result_ready",
            approval=True,
            approval_kind="send_to_gpt",
            planner_metadata={
                "action": "ask_send_to_gpt",
                "reason": "codex_result_ready",
                "event_ids": {"codex_exec_finished": 10},
            },
        )
        step = StepRecorder()
        step.block = True
        controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(approval_model),
            supervision_step=step,
        )
        controller.request_automatic_progress()

        first = controller.submit_approval_decision("approved")
        second = controller.submit_approval_decision("approved")
        self.assertTrue(step.entered.wait(1))
        progress = controller.request_automatic_progress()
        step.release.set()
        controller.current_worker.join(1)

        self.assertTrue(first.ok)
        self.assertEqual(second.reason_code, "action_already_running")
        self.assertEqual(progress.reason_code, "action_already_running")
        self.assertEqual(len(step.calls), 1)
        self.assertFalse(controller.action_running)

    def test_action_running_clears_after_normal_return_and_exception(self) -> None:
        routine = _model(action="capture_gpt_response", routine=True, stage="routine_action_available")
        idle = _model(action="stop", reason="no_action", stage="idle")
        controller = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(routine, routine, idle, repeat_last=False),
            supervision_step=StepRecorder(),
        )
        controller.request_automatic_progress()
        controller.current_worker.join(1)
        self.assertFalse(controller.action_running)

        failing = LocalController(
            session=LocalControllerSession(active_run_id="run-1"),
            ledger=FakeLedger(_run()),
            read_model_builder=ReadModelSequence(routine),
            supervision_step=StepRecorder(exception=RuntimeError("step failed")),
        )
        failing.request_automatic_progress()
        failing.current_worker.join(1)
        self.assertFalse(failing.action_running)
        self.assertEqual(failing.session.controller_state, "failed")
        self.assertEqual(failing.last_exception_summary["type"], "RuntimeError")

    def test_worker_failures_are_visible_without_ledger_failure_events(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            initial = BlockingInitialExecutor(exception=RuntimeError("initial failed"))
            controller = LocalController(ledger=FakeLedger(), initial_run_executor=initial)
            controller.start_run(repository_path=repo, initial_instruction="Task", sandbox="read-only")
            self.assertTrue(initial.entered.wait(1))
            initial.release.set()
            controller.current_worker.join(1)

            self.assertEqual(controller.session.controller_state, "failed")
            self.assertEqual(controller.last_exception_summary["type"], "RuntimeError")
            self.assertNotIn(
                "controller_failed",
                [event["event_type"] for event in controller.ledger.events],
            )

    def test_controller_uses_only_injected_execution_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            initial = BlockingInitialExecutor()
            step = StepRecorder()
            controller = LocalController(
                ledger=FakeLedger(),
                initial_run_executor=initial,
                supervision_step=step,
            )
            with (
                mock.patch("subprocess.run") as subprocess_run,
                mock.patch("builtins.input") as input_call,
                mock.patch("agent.codex_services.execute_codex_direct_service") as codex,
                mock.patch("agent.chatgpt_services.submit_feedback_to_chatgpt_service") as submit,
                mock.patch("agent.chatgpt_services.capture_chatgpt_response_service") as capture,
                mock.patch("agent.chatgpt_services.extract_next_codex_prompt_service") as extract,
            ):
                controller.start_run(repository_path=repo, initial_instruction="Task", sandbox="read-only")
                self.assertTrue(initial.entered.wait(1))
                initial.release.set()
                controller.current_worker.join(1)

            subprocess_run.assert_not_called()
            input_call.assert_not_called()
            codex.assert_not_called()
            submit.assert_not_called()
            capture.assert_not_called()
            extract.assert_not_called()
            self.assertEqual(len(initial.calls), 1)


if __name__ == "__main__":
    unittest.main()
