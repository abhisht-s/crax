from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field

from agent.supervise import SuperviseAction, SupervisePlan
from agent.supervision_services import run_supervision_step


@dataclass
class ActionRecorder:
    result: object = True
    exception: Exception | None = None
    calls: list[tuple[tuple, dict]] = field(default_factory=list)

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exception is not None:
            raise self.exception
        return self.result


class FakeLedger:
    def __init__(self, run: dict | None = None, events: list[dict] | None = None) -> None:
        self.run = run if run is not None else {"id": "run-1", "status": "completed"}
        self.events = list(events or [])
        self.added_events: list[dict] = []
        self.get_run_calls: list[str] = []
        self.list_events_calls: list[str] = []
        self._next_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1

    def get_run(self, run_id: str) -> dict | None:
        self.get_run_calls.append(run_id)
        return self.run

    def list_events(self, run_id: str) -> list[dict]:
        self.list_events_calls.append(run_id)
        return self.events

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        event = {
            "id": self._next_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        self._next_id += 1
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


def _planner_for(plan: SupervisePlan) -> Planner:
    return Planner(plan)


def _send_plan(*, changed_files_count: int | None = 0, sandbox: str = "read-only") -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.ASK_SEND_TO_GPT,
        reason="codex_result_ready",
        event_ids={"codex_exec_finished": 10},
        repo_path="/repo",
        sandbox=sandbox,
        status="completed",
        codex_exit_code=0,
        codex_timed_out=False,
        codex_sandbox=sandbox,
        changed_files_count=changed_files_count,
        supervision_decision="continue",
    )


def _capture_plan() -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.CAPTURE_GPT_RESPONSE,
        reason="feedback_submitted_capture_needed",
        event_ids={"codex_exec_finished": 10, "gpt_feedback_submission_verified": 11},
        repo_path="/repo",
        sandbox="read-only",
        status="completed",
    )


def _extract_plan() -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.EXTRACT_NEXT_PROMPT,
        reason="gpt_response_captured_extract_needed",
        event_ids={
            "codex_exec_finished": 10,
            "gpt_feedback_submission_verified": 11,
            "gpt_response_captured": 12,
        },
        repo_path="/repo",
        sandbox="read-only",
        status="completed",
    )


def _run_plan(*, prompt_auto_run_safe: bool = True, reason: str = "caller_selected_read_only_sandbox") -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.ASK_RUN_PROMPT,
        reason="fresh_sentinel_prompt_ready",
        event_ids={
            "codex_exec_finished": 10,
            "gpt_feedback_submission_verified": 11,
            "gpt_response_captured": 12,
            "next_codex_prompt_extracted": 13,
        },
        prompt_preview="Run this",
        prompt_text="Run this",
        prompt_sha="sha-1",
        extraction_method="sentinel_block",
        repo_path="/repo",
        sandbox="read-only",
        status="completed",
        prompt_auto_run_safe=prompt_auto_run_safe,
        prompt_auto_run_reason=reason,
        pre_run_policy={"allowed": prompt_auto_run_safe, "reason_code": reason},
        expected_scope={"explicit_files": ["agent/foo.py"]},
    )


def _stop_plan(reason: str = "needs_review") -> SupervisePlan:
    return SupervisePlan(
        action=SuperviseAction.STOP,
        reason=reason,
        stop_message=f"Stopped: {reason}",
        repo_path="/repo",
        sandbox="read-only",
        status="completed",
    )


class SupervisionStepServiceTests(unittest.TestCase):
    def _run_step(self, plan: SupervisePlan, **kwargs):
        planner = _planner_for(plan)
        submit = kwargs.pop("submit_service", ActionRecorder())
        capture = kwargs.pop("capture_service", ActionRecorder())
        extract = kwargs.pop("extraction_service", ActionRecorder())
        run_prompt = kwargs.pop("extracted_prompt_execution_service", ActionRecorder())
        ledger = kwargs.pop("ledger", FakeLedger())
        result = run_supervision_step(
            "run-1",
            "/repo",
            "read-only",
            ledger=ledger,
            planner=planner,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
            extracted_prompt_execution_service=run_prompt,
            **kwargs,
        )
        return result, planner, ledger, submit, capture, extract, run_prompt

    def test_automatic_safe_send_calls_submit_once_and_does_not_replan(self) -> None:
        result, planner, _ledger, submit, capture, extract, run_prompt = self._run_step(_send_plan())

        self.assertTrue(result.ok)
        self.assertTrue(result.action_executed)
        self.assertEqual(result.planner_action, "ask_send_to_gpt")
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(submit.calls), 1)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertEqual(len(run_prompt.calls), 0)
        self.assertEqual(submit.calls[0][1]["approval_mode"], "auto")

    def test_automatic_unsafe_send_records_auto_stop_and_calls_no_action(self) -> None:
        result, planner, ledger, submit, capture, extract, run_prompt = self._run_step(
            _send_plan(changed_files_count=1)
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.action_executed)
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason_code, "codex_result_changed_files")
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertEqual(len(run_prompt.calls), 0)
        self.assertEqual([event["event_type"] for event in ledger.added_events], ["supervise_auto_stopped"])
        self.assertEqual(
            ledger.added_events[0]["metadata"]["automatic_stop_reason"],
            "codex_result_changed_files",
        )

    def test_interactive_send_requires_approval_then_approved_or_rejected(self) -> None:
        pending, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            approval_mode="interactive",
        )
        self.assertTrue(pending.requires_human_approval)
        self.assertEqual(pending.approval_kind, "send_to_gpt")
        self.assertEqual(len(submit.calls), 0)

        approved, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            approval_mode="interactive",
            approval_decision="approved",
        )
        self.assertTrue(approved.ok)
        self.assertEqual(len(submit.calls), 1)
        self.assertEqual(submit.calls[0][1]["approval_mode"], "human")

        rejected, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            approval_mode="interactive",
            approval_decision="rejected",
        )
        self.assertTrue(rejected.ok)
        self.assertFalse(rejected.action_executed)
        self.assertEqual(rejected.reason_code, "human_declined")
        self.assertEqual(len(submit.calls), 0)

    def test_capture_called_once_with_sentinel_requirement_and_failure_maps(self) -> None:
        capture = ActionRecorder(result=False)
        result, planner, _ledger, submit, capture, extract, run_prompt = self._run_step(
            _capture_plan(),
            capture_service=capture,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "capture_gpt_response_failed")
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(capture.calls), 1)
        self.assertTrue(capture.calls[0][1]["require_sentinel_response"])
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertEqual(len(run_prompt.calls), 0)

    def test_extract_called_once_with_sentinel_and_confirmed_persistence(self) -> None:
        result, planner, _ledger, submit, capture, extract, run_prompt = self._run_step(_extract_plan())

        self.assertTrue(result.ok)
        self.assertEqual(result.next_state_hint, "ask_run_prompt")
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(extract.calls), 1)
        self.assertTrue(extract.calls[0][1]["require_sentinel"])
        self.assertTrue(extract.calls[0][1]["confirm_extract"])
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(run_prompt.calls), 0)

    def test_run_action_auto_safe_and_unsafe_and_metadata_passthrough(self) -> None:
        safe, _planner, _ledger, _submit, _capture, _extract, run_prompt = self._run_step(_run_plan())
        self.assertTrue(safe.ok)
        self.assertEqual(len(run_prompt.calls), 1)
        kwargs = run_prompt.calls[0][1]
        self.assertEqual(kwargs["expected_extraction_event_id"], 13)
        self.assertEqual(kwargs["expected_prompt_sha256"], "sha-1")
        self.assertEqual(kwargs["expected_prompt_text"], "Run this")
        self.assertEqual(kwargs["expected_extraction_method"], "sentinel_block")
        self.assertEqual(kwargs["pre_run_policy"], {"allowed": True, "reason_code": "caller_selected_read_only_sandbox"})
        self.assertEqual(kwargs["expected_scope"], {"explicit_files": ["agent/foo.py"]})

        unsafe, _planner, ledger, _submit, _capture, _extract, run_prompt = self._run_step(
            _run_plan(prompt_auto_run_safe=False, reason="workspace_write_scope_not_inferred")
        )
        self.assertFalse(unsafe.ok)
        self.assertTrue(unsafe.blocked)
        self.assertEqual(unsafe.reason_code, "workspace_write_scope_not_inferred")
        self.assertEqual(len(run_prompt.calls), 0)
        self.assertEqual([event["event_type"] for event in ledger.added_events], ["supervise_auto_stopped"])

    def test_interactive_run_requires_approval_then_approved_or_rejected(self) -> None:
        pending, _planner, _ledger, *_rest, run_prompt = self._run_step(
            _run_plan(),
            approval_mode="interactive",
        )
        self.assertTrue(pending.requires_human_approval)
        self.assertEqual(pending.approval_kind, "run_prompt")
        self.assertEqual(len(run_prompt.calls), 0)

        approved, _planner, _ledger, *_rest, run_prompt = self._run_step(
            _run_plan(),
            approval_mode="interactive",
            approval_decision="approved",
        )
        self.assertTrue(approved.ok)
        self.assertEqual(len(run_prompt.calls), 1)
        self.assertEqual(run_prompt.calls[0][1]["approval_mode"], "human")

        rejected, _planner, _ledger, *_rest, run_prompt = self._run_step(
            _run_plan(),
            approval_mode="interactive",
            approval_decision="rejected",
        )
        self.assertTrue(rejected.ok)
        self.assertFalse(rejected.action_executed)
        self.assertEqual(len(run_prompt.calls), 0)

    def test_stop_states_execute_no_action_and_distinguish_completed_from_blocked(self) -> None:
        blocked, _planner, _ledger, submit, capture, extract, run_prompt = self._run_step(
            _stop_plan("danger_full_access_blocked")
        )
        self.assertFalse(blocked.ok)
        self.assertTrue(blocked.terminal)
        self.assertTrue(blocked.blocked)
        self.assertFalse(blocked.completed)
        self.assertEqual(len(submit.calls) + len(capture.calls) + len(extract.calls) + len(run_prompt.calls), 0)

        completed, _planner, _ledger, submit, capture, extract, run_prompt = self._run_step(
            _stop_plan("extracted_prompt_already_run")
        )
        self.assertTrue(completed.ok)
        self.assertTrue(completed.terminal)
        self.assertTrue(completed.completed)
        self.assertFalse(completed.blocked)
        self.assertEqual(len(submit.calls) + len(capture.calls) + len(extract.calls) + len(run_prompt.calls), 0)

    def test_auto_stop_for_planner_stop_is_written_once_per_step(self) -> None:
        result, _planner, ledger, *_ = self._run_step(_stop_plan("needs_review"))

        self.assertFalse(result.ok)
        self.assertEqual([event["event_type"] for event in ledger.added_events], ["supervise_auto_stopped"])
        self.assertEqual(len(result.events_written), 1)

    def test_invalid_approval_decision_returns_failure_without_planning_or_action(self) -> None:
        planner = _planner_for(_send_plan())
        submit = ActionRecorder()
        ledger = FakeLedger()

        result = run_supervision_step(
            "run-1",
            "/repo",
            "read-only",
            approval_decision="yes",
            ledger=ledger,
            planner=planner,
            submit_service=submit,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "invalid_approval_decision")
        self.assertEqual(len(planner.calls), 0)
        self.assertEqual(len(submit.calls), 0)

    def test_expected_action_failure_is_structured_and_unexpected_exception_bubbles(self) -> None:
        failed_submit = ActionRecorder(result=False)
        result, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            submit_service=failed_submit,
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.action_executed)
        self.assertEqual(result.reason_code, "submit_feedback_failed")
        self.assertEqual(len(submit.calls), 1)

        exploding_submit = ActionRecorder(exception=RuntimeError("boom"))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self._run_step(_send_plan(), submit_service=exploding_submit)

    def test_state_change_after_approval_blocks_without_action(self) -> None:
        result, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            approval_mode="interactive",
            approval_decision="approved",
            expected_planner_action="ask_send_to_gpt",
            expected_event_ids={"codex_exec_finished": 99},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "planner_event_ids_changed_after_approval")
        self.assertFalse(result.action_executed)
        self.assertEqual(len(submit.calls), 0)

    def test_before_action_callback_runs_once_and_service_does_not_call_unselected_actions(self) -> None:
        callback_calls: list[str] = []

        def callback(plan: SupervisePlan, run: dict | None, events: list[dict]) -> None:
            del run, events
            callback_calls.append(str(plan.action))

        result, planner, _ledger, submit, capture, extract, run_prompt = self._run_step(
            _capture_plan(),
            before_action_callback=callback,
        )

        self.assertTrue(result.ok)
        self.assertEqual(callback_calls, ["capture_gpt_response"])
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(capture.calls), 1)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertEqual(len(run_prompt.calls), 0)


if __name__ == "__main__":
    unittest.main()
