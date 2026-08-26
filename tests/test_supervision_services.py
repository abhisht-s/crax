from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from unittest import mock

from agent import ledger as default_ledger
from agent.chatgpt_destination_gate import (
    ChatGPTDestinationSnapshot,
    DestinationEvidenceCandidate,
)
from agent.supervise import SuperviseAction, SupervisePlan
from agent.supervision_services import (
    CHATGPT_HANDOFF_MAX_UI_ATTEMPTS,
    HANDOFF_PHASES,
    _default_run_prompt_service,
    run_supervision_step,
    send_plan_auto_safe,
)
from agent.run_services import verify_chatgpt_destination_for_run


def _raw_token(label: str) -> str:
    return f"unit-{default_ledger.chatgpt_ui_lease_token_fingerprint(label)}"


class AutonomousFullAccessWiringTests(unittest.TestCase):
    def test_full_access_handoff_never_requires_human_approval(self) -> None:
        for changed_files_count in (None, 0, 1, 500):
            with self.subTest(changed_files_count=changed_files_count):
                allowed, reason = send_plan_auto_safe(
                    _send_plan(
                        changed_files_count=changed_files_count,
                        sandbox="danger-full-access",
                    ),
                    [],
                )

                self.assertTrue(allowed)
                self.assertEqual(reason, "danger_full_access_auto_submit")

    def test_default_prompt_runner_authorizes_full_access_without_per_run_approval(self) -> None:
        with mock.patch(
            "agent.supervision_services.execute_extracted_codex_prompt_service",
            return_value=ServiceResult(ok=True),
        ) as execute:
            _default_run_prompt_service(
                "run-1",
                {"id": "run-1"},
                "/tmp/repo",
                "danger-full-access",
                None,
                expected_extraction_event_id=1,
                expected_prompt_sha256="abc",
                expected_prompt_text="Task",
                expected_extraction_method="sentinel",
                approval_mode="auto",
                pre_run_policy={},
                expected_scope={},
                ledger=object(),
            )

        self.assertTrue(execute.call_args.kwargs["confirm_full_access"])
        self.assertTrue(execute.call_args.kwargs["allow_full_access"])


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


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    reason_code: str | None = None
    error_message: str | None = None
    event_type: str | None = None
    event_id: int | None = None
    metadata: dict | None = None


class FakeLedger:
    def __init__(
        self,
        run: dict | None = None,
        events: list[dict] | None = None,
        *,
        lease_already_held: bool = False,
        release_fails: bool = False,
        release_raises: bool = False,
        destination_binding: tuple[str, str] | None = ("Project Alpha", "Main Chat"),
        lease_owner_run_id: str | None = None,
    ) -> None:
        self.run = run if run is not None else {"id": "run-1", "status": "completed"}
        self.events = list(events or [])
        self.added_events: list[dict] = []
        self.get_run_calls: list[str] = []
        self.list_events_calls: list[str] = []
        self.operations: list[str] = []
        self.lease_already_held = lease_already_held
        self.release_fails = release_fails
        self.release_raises = release_raises
        self.lease_token = _raw_token("supervision")
        self.lease_owner_run_id = lease_owner_run_id
        self._next_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1
        if destination_binding is not None and events is None:
            self.add_event(
                "run-1",
                default_ledger.RUN_DESTINATION_BOUND_EVENT_TYPE,
                default_ledger.RUN_DESTINATION_BOUND_MESSAGE,
                metadata={
                    "schema_version": default_ledger.RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                    "project_title": destination_binding[0],
                    "chat_title": destination_binding[1],
                },
            )
            self.added_events.clear()

    def get_run(self, run_id: str) -> dict | None:
        self.get_run_calls.append(run_id)
        return self.run

    def list_events(self, run_id: str) -> list[dict]:
        self.list_events_calls.append(run_id)
        return self.events

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        self.operations.append(f"event:{event_type}")
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

    def update_run_status(
        self,
        run_id: str,
        status: object,
        *,
        final_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        self.operations.append(f"status:{getattr(status, 'value', status)}")
        if self.run is not None and self.run.get("id") == run_id:
            self.run = {
                **self.run,
                "status": str(getattr(status, "value", status)),
                "final_summary": final_summary,
                "error": error,
            }

    def acquire_chatgpt_ui_lease(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ):
        self.operations.append("acquire_lease")
        if self.lease_already_held:
            event = self.add_event(
                run_id,
                default_ledger.CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE,
                default_ledger.CHATGPT_UI_LEASE_ACQUIRE_DENIED_MESSAGE,
                metadata={
                    "reason_code": "chatgpt_ui_lease_already_held",
                    "reason": reason,
                    "source": source,
                },
            )
            return default_ledger.AtomicChatGPTUILeaseResult(
                status=default_ledger.AtomicChatGPTUILeaseStatus.ALREADY_HELD,
                run_id=run_id,
                owner_pid=111,
                owning_run_id="other-run",
                acquired_at="2026-01-01T00:00:00+00:00",
                event_id=event["id"],
                event_written=True,
                reason_code="chatgpt_ui_lease_already_held",
                error_message="ChatGPT Desktop UI lease is already held.",
                event_ids=(event["id"],),
            )
        event = self.add_event(
            run_id,
            default_ledger.CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
            default_ledger.CHATGPT_UI_LEASE_ACQUIRED_MESSAGE,
            metadata={
                "schema_version": default_ledger.CHATGPT_UI_LEASE_SCHEMA_VERSION,
                "lease_token_sha256": default_ledger.chatgpt_ui_lease_token_fingerprint(
                    self.lease_token
                ),
                "owner_pid": 222,
                "owning_run_id": self.lease_owner_run_id or run_id,
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "reason": reason,
                "source": source,
            },
        )
        return default_ledger.AtomicChatGPTUILeaseResult(
            status=default_ledger.AtomicChatGPTUILeaseStatus.ACQUIRED,
            run_id=run_id,
            lease_token=self.lease_token,
            owner_pid=222,
            owning_run_id=self.lease_owner_run_id or run_id,
            acquired_at="2026-01-01T00:00:00+00:00",
            event_id=event["id"],
            event_written=True,
        )

    def release_chatgpt_ui_lease(
        self,
        lease_token: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ):
        self.operations.append("release_lease")
        if self.release_raises:
            raise RuntimeError("release exploded")
        if self.release_fails:
            return default_ledger.AtomicChatGPTUILeaseResult(
                status=default_ledger.AtomicChatGPTUILeaseStatus.OPERATIONAL_FAILURE,
                lease_token=lease_token,
                reason_code="chatgpt_ui_lease_release_failed",
                error_message="release failed",
            )
        event = self.add_event(
            "run-1",
            default_ledger.CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
            default_ledger.CHATGPT_UI_LEASE_RELEASED_MESSAGE,
            metadata={
                "schema_version": default_ledger.CHATGPT_UI_LEASE_SCHEMA_VERSION,
                "lease_token_sha256": default_ledger.chatgpt_ui_lease_token_fingerprint(
                    lease_token
                ),
                "owner_pid": 222,
                "owning_run_id": "run-1",
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "released_at": "2026-01-01T00:01:00+00:00",
                "reason": reason,
                "source": source,
            },
        )
        return default_ledger.AtomicChatGPTUILeaseResult(
            status=default_ledger.AtomicChatGPTUILeaseStatus.RELEASED,
            lease_token=lease_token,
            owner_pid=222,
            owning_run_id="run-1",
            acquired_at="2026-01-01T00:00:00+00:00",
            released_at="2026-01-01T00:01:00+00:00",
            event_id=event["id"],
            event_written=True,
        )

    def list_chatgpt_ui_lease_events(self) -> list[dict]:
        return [
            event
            for event in self.events
            if event["event_type"]
            in {
                default_ledger.CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
                default_ledger.CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
            }
        ]


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


class FakeDestinationAdapter:
    def __init__(
        self,
        snapshot: ChatGPTDestinationSnapshot,
        operations: list[str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.operations = operations
        self.read_count = 0

    def read_destination_snapshot(self) -> ChatGPTDestinationSnapshot:
        self.read_count += 1
        if self.operations is not None:
            self.operations.append("read_only_destination_gate")
        return self.snapshot


def _destination_adapter_factory(
    snapshot: ChatGPTDestinationSnapshot | None = None,
    operations: list[str] | None = None,
):
    return lambda: FakeDestinationAdapter(snapshot or _exact_destination_snapshot(), operations)


def _exact_destination_snapshot(
    *,
    project_title: str = "Project Alpha",
    chat_title: str = "Main Chat",
) -> ChatGPTDestinationSnapshot:
    return ChatGPTDestinationSnapshot(
        process_running=True,
        window_available=True,
        accessibility_available=True,
        snapshot_stable=True,
        snapshot_complete=True,
        active_project_candidates=(
            DestinationEvidenceCandidate(
                project_title,
                active=True,
                identity_confirmed=True,
                actionable_destination_evidence=True,
                project_chats_list_confirmed=True,
            ),
        ),
        selected_chat_row_candidates=(
            DestinationEvidenceCandidate(
                chat_title,
                active=True,
                selected=True,
                identity_confirmed=True,
                actionable_destination_evidence=True,
            ),
        ),
        conversation_header_candidates=(
            DestinationEvidenceCandidate(
                chat_title,
                active=True,
                identity_confirmed=True,
                actionable_destination_evidence=True,
            ),
        ),
        composer_available=True,
        transcript_available=True,
        conversation_surface_available=True,
    )


class SupervisionStepServiceTests(unittest.TestCase):
    def _run_step(self, plan: SupervisePlan, **kwargs):
        planner = _planner_for(plan)
        submit = kwargs.pop("submit_service", ActionRecorder())
        capture = kwargs.pop("capture_service", ActionRecorder())
        extract = kwargs.pop("extraction_service", ActionRecorder())
        run_prompt = kwargs.pop("extracted_prompt_execution_service", ActionRecorder())
        ledger = kwargs.pop("ledger", FakeLedger())
        destination_adapter_factory = kwargs.pop(
            "destination_adapter_factory",
            _destination_adapter_factory(),
        )
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
            destination_adapter_factory=destination_adapter_factory,
            **kwargs,
        )
        return result, planner, ledger, submit, capture, extract, run_prompt

    def test_automatic_safe_send_calls_submit_once_and_does_not_replan(self) -> None:
        result, planner, ledger, submit, capture, extract, run_prompt = self._run_step(_send_plan())

        self.assertTrue(result.ok)
        self.assertTrue(result.action_executed)
        self.assertEqual(result.planner_action, "ask_send_to_gpt")
        self.assertEqual(result.next_state_hint, "ask_run_prompt")
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(submit.calls), 1)
        self.assertEqual(len(capture.calls), 1)
        self.assertEqual(len(extract.calls), 1)
        self.assertEqual(len(run_prompt.calls), 0)
        self.assertEqual(submit.calls[0][1]["approval_mode"], "auto")
        self.assertEqual(
            [op for op in ledger.operations if op in {"acquire_lease", "release_lease"}],
            ["acquire_lease", "release_lease"],
        )

    def test_full_access_send_with_changes_runs_without_human_approval(self) -> None:
        result, _planner, ledger, submit, capture, extract, run_prompt = self._run_step(
            _send_plan(
                changed_files_count=500,
                sandbox="danger-full-access",
            )
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.action_executed)
        self.assertFalse(result.requires_human_approval)
        self.assertEqual(len(submit.calls), 1)
        self.assertEqual(submit.calls[0][1]["approval_mode"], "auto")
        self.assertEqual(len(capture.calls), 1)
        self.assertEqual(len(extract.calls), 1)
        self.assertEqual(len(run_prompt.calls), 0)
        self.assertNotIn(
            "supervise_auto_stopped",
            [event["event_type"] for event in ledger.added_events],
        )

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

    def test_run_action_ignores_prompt_auto_run_safety_flag_and_preserves_metadata(self) -> None:
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

        stale_false, _planner, ledger, _submit, _capture, _extract, run_prompt = self._run_step(
            _run_plan(prompt_auto_run_safe=False, reason="workspace_write_scope_not_inferred")
        )
        self.assertTrue(stale_false.ok)
        self.assertFalse(stale_false.blocked)
        self.assertEqual(len(run_prompt.calls), 1)
        self.assertEqual(ledger.added_events, [])

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

    def test_expected_action_failure_retries_before_structured_failure(self) -> None:
        failed_submit = ActionRecorder(result=False)
        result, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            submit_service=failed_submit,
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.action_executed)
        self.assertEqual(result.reason_code, "submit_feedback_failed")
        self.assertEqual(len(submit.calls), CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)
        self.assertEqual(
            result.metadata["chatgpt_handoff_retry"]["attempt_count"],
            CHATGPT_HANDOFF_MAX_UI_ATTEMPTS,
        )

        exploding_submit = ActionRecorder(exception=RuntimeError("boom"))
        exception_result, _planner, _ledger, submit, *_ = self._run_step(
            _send_plan(),
            submit_service=exploding_submit,
        )
        self.assertFalse(exception_result.ok)
        self.assertEqual(exception_result.reason_code, "submit_feedback_exception")
        self.assertEqual(len(submit.calls), CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)

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
        self.assertEqual(len(extract.calls), 1)
        self.assertEqual(len(run_prompt.calls), 0)

    def test_successful_handoff_holds_one_lease_until_after_extraction(self) -> None:
        ledger = FakeLedger()

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit_feedback")
            return ServiceResult(True, event_type="gpt_feedback_submission_verified", event_id=10)

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture_response")
            return ServiceResult(True, event_type="gpt_response_captured", event_id=11)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("extract_prompt")
            return ServiceResult(True, event_type="next_codex_prompt_extracted", event_id=12)

        result, _planner, ledger, *_ = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
            destination_adapter_factory=_destination_adapter_factory(
                operations=ledger.operations,
            ),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.next_state_hint, "ask_run_prompt")
        ordered = [
            op
            for op in ledger.operations
            if op
            in {
                "acquire_lease",
                "destination_binding_lookup",
                "read_only_destination_gate",
                "submit_feedback",
                "capture_response",
                "extract_prompt",
                "release_lease",
            }
        ]
        self.assertEqual(
            ordered,
            [
                "acquire_lease",
                "read_only_destination_gate",
                "submit_feedback",
                "capture_response",
                "extract_prompt",
                "release_lease",
            ],
        )

    def test_submit_failure_retries_until_verified_submission(self) -> None:
        ledger = FakeLedger()
        submit_attempts: list[str] = []

        def submit(*args, **kwargs):
            del args, kwargs
            submit_attempts.append("submit")
            ledger.operations.append("submit_feedback")
            if len(submit_attempts) < CHATGPT_HANDOFF_MAX_UI_ATTEMPTS:
                return ServiceResult(
                    False,
                    reason_code="chatgpt_paste_input_failed",
                    error_message="paste interrupted",
                    event_type="gpt_feedback_submission_failed",
                    event_id=20 + len(submit_attempts),
                )
            return ServiceResult(
                True,
                reason_code="chatgpt_submission_verified",
                event_type="gpt_feedback_submission_verified",
                event_id=99,
            )

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture_response")
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("extract_prompt")
            return ServiceResult(True)

        result, _planner, ledger, *_ = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(submit_attempts), CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)
        self.assertEqual(ledger.operations.count("capture_response"), 1)
        self.assertEqual(ledger.operations.count("extract_prompt"), 1)
        retry = result.metadata["chatgpt_handoff_retry"]
        self.assertEqual(retry["attempt_count"], CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)
        self.assertTrue(retry["attempts"][-1]["ok"])

    def test_successful_handoff_order_includes_binding_lookup_and_gate_before_feedback(self) -> None:
        ledger = FakeLedger()

        def gate(run_id, lease_context, *, adapter_factory, ledger):
            ledger.operations.append("destination_binding_lookup")
            return verify_chatgpt_destination_for_run(
                run_id,
                lease_context,
                adapter_factory=adapter_factory,
                ledger=ledger,
            )

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("feedback_generation")
            return ServiceResult(True)

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit_capture_extract")
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit_capture_extract")
            return ServiceResult(True)

        result, _planner, ledger, *_ = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
            destination_gate_service=gate,
            destination_adapter_factory=_destination_adapter_factory(
                operations=ledger.operations,
            ),
        )

        self.assertTrue(result.ok)
        ordered = [
            op
            for op in ledger.operations
            if op
            in {
                "acquire_lease",
                "destination_binding_lookup",
                "read_only_destination_gate",
                "feedback_generation",
                "submit_capture_extract",
                "release_lease",
            }
        ]
        self.assertEqual(
            ordered,
            [
                "acquire_lease",
                "destination_binding_lookup",
                "read_only_destination_gate",
                "feedback_generation",
                "submit_capture_extract",
                "submit_capture_extract",
                "release_lease",
            ],
        )

    def test_active_lease_blocks_before_clipboard_or_ui_operations(self) -> None:
        ledger = FakeLedger(lease_already_held=True)
        submit = ActionRecorder(exception=AssertionError("submit should not run"))
        capture = ActionRecorder(exception=AssertionError("capture should not run"))
        extract = ActionRecorder(exception=AssertionError("extract should not run"))
        adapter_calls: list[str] = []

        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
            destination_adapter_factory=_destination_adapter_factory(
                operations=adapter_calls,
            ),
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason_code, "chatgpt_ui_lease_already_held")
        self.assertFalse(result.action_executed)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertEqual(adapter_calls, [])
        self.assertNotIn("release_lease", ledger.operations)

    def test_gate_failure_blocks_before_feedback_clipboard_ui_capture_or_extract(self) -> None:
        ledger = FakeLedger()
        submit = ActionRecorder(exception=AssertionError("submit should not run"))
        capture = ActionRecorder(exception=AssertionError("capture should not run"))
        extract = ActionRecorder(exception=AssertionError("extract should not run"))

        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
            destination_adapter_factory=_destination_adapter_factory(
                _exact_destination_snapshot(project_title="Other Project"),
            ),
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertFalse(result.action_executed)
        self.assertEqual(result.reason_code, "project_not_active")
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertIn("release_lease", ledger.operations)
        self.assertIn("status:needs_review", ledger.operations)
        self.assertEqual(result.run_status, "needs_review")
        self.assertEqual(ledger.run["status"], "needs_review")
        blocked_events = [
            event
            for event in ledger.added_events
            if event["event_type"] == "chatgpt_destination_gate_blocked_handoff"
        ]
        self.assertEqual(len(blocked_events), 2)
        self.assertEqual(
            [event["metadata"]["attempt_number"] for event in blocked_events],
            [1, 2],
        )
        self.assertEqual(
            [event["metadata"]["will_retry"] for event in blocked_events],
            [True, False],
        )
        metadata = blocked_events[0]["metadata"]
        self.assertEqual(metadata["run_id"], "run-1")
        self.assertEqual(metadata["binding_project_title"], "Project Alpha")
        self.assertEqual(metadata["binding_chat_title"], "Main Chat")
        self.assertEqual(metadata["gate_reason_code"], "project_not_active")
        self.assertTrue(metadata["feedback_ui_handoff_skipped"])
        self.assertNotIn(ledger.lease_token, json.dumps(metadata, sort_keys=True))

    def test_wrong_project_wrong_chat_and_truncated_evidence_block(self) -> None:
        cases = [
            (
                "wrong project",
                _exact_destination_snapshot(project_title="Other Project"),
                "project_not_active",
            ),
            (
                "wrong chat",
                _exact_destination_snapshot(chat_title="Other Chat"),
                "chat_not_active",
            ),
            (
                "truncated evidence",
                ChatGPTDestinationSnapshot(
                    process_running=True,
                    window_available=True,
                    accessibility_available=True,
                    snapshot_stable=False,
                    snapshot_complete=False,
                    ax_tree_truncated=True,
                ),
                "ax_tree_truncated_or_unstable",
            ),
        ]
        for label, snapshot, reason in cases:
            with self.subTest(label=label):
                result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
                    _send_plan(),
                    ledger=FakeLedger(),
                    destination_adapter_factory=_destination_adapter_factory(snapshot),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason)
                self.assertEqual(len(submit.calls), 0)
                self.assertEqual(len(capture.calls), 0)
                self.assertEqual(len(extract.calls), 0)
                self.assertIn("release_lease", ledger.operations)

    def test_missing_destination_binding_blocks_after_lease_and_before_adapter(self) -> None:
        adapter_calls: list[str] = []
        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=FakeLedger(destination_binding=None),
            destination_adapter_factory=_destination_adapter_factory(
                operations=adapter_calls,
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_binding_missing")
        self.assertEqual(adapter_calls, [])
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertIn("release_lease", ledger.operations)

    def test_invalid_destination_binding_blocks_after_lease_and_before_adapter(self) -> None:
        invalid_event = {
            "id": 1,
            "run_id": "run-1",
            "event_type": default_ledger.RUN_DESTINATION_BOUND_EVENT_TYPE,
            "metadata": {
                "schema_version": default_ledger.RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                "project_title": "Project Alpha",
            },
        }
        adapter_calls: list[str] = []
        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=FakeLedger(events=[invalid_event]),
            destination_adapter_factory=_destination_adapter_factory(
                operations=adapter_calls,
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_binding_invalid")
        self.assertEqual(adapter_calls, [])
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertIn("release_lease", ledger.operations)

    def test_lease_mismatch_blocks_before_adapter_access(self) -> None:
        adapter_calls: list[str] = []

        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=FakeLedger(lease_owner_run_id="other-run"),
            destination_adapter_factory=_destination_adapter_factory(
                operations=adapter_calls,
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_lease_invalid_or_mismatched")
        self.assertEqual(adapter_calls, [])
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertIn("release_lease", ledger.operations)

    def test_lease_denial_blocks_before_gate_or_adapter_access(self) -> None:
        def gate(*args, **kwargs):
            del args, kwargs
            raise AssertionError("gate should not run")

        adapter_calls: list[str] = []
        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=FakeLedger(lease_already_held=True),
            destination_gate_service=gate,
            destination_adapter_factory=_destination_adapter_factory(
                operations=adapter_calls,
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chatgpt_ui_lease_already_held")
        self.assertEqual(adapter_calls, [])
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertNotIn("release_lease", ledger.operations)

    def test_gate_integration_adds_no_navigation_clipboard_or_deadline_operations(self) -> None:
        ledger = FakeLedger()
        result, _planner, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            destination_adapter_factory=_destination_adapter_factory(
                _exact_destination_snapshot(project_title="Other Project"),
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        forbidden = (
            "navigate",
            "click",
            "scroll",
            "focus",
            "clipboard",
            "paste",
            "submit",
            "timeout",
            "deadline",
        )
        self.assertFalse(
            any(fragment in op for op in ledger.operations for fragment in forbidden)
        )

    def test_feedback_generation_failure_releases_and_stops_before_ui_followups(self) -> None:
        ledger = FakeLedger()

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("feedback_generation")
            return ServiceResult(
                False,
                reason_code="codex_final_message_missing",
                error_message="missing final message",
                event_type="gpt_feedback_generation_failed",
                event_id=20,
            )

        capture = ActionRecorder(exception=AssertionError("capture should not run"))
        extract = ActionRecorder(exception=AssertionError("extract should not run"))

        result, _planner, ledger, _submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "codex_final_message_missing")
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertEqual(
            [op for op in ledger.operations if op in {"acquire_lease", "feedback_generation", "release_lease"}],
            ["acquire_lease", "feedback_generation", "release_lease"],
        )

    def test_capture_failure_releases_lease(self) -> None:
        ledger = FakeLedger()

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit_feedback")
            return ServiceResult(True)

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture_response")
            return ServiceResult(False, reason_code="capture_failed", error_message="capture failed")

        extract = ActionRecorder(exception=AssertionError("extract should not run"))

        result, _planner, ledger, _submit, _capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "capture_failed")
        self.assertEqual(len(extract.calls), 0)
        self.assertIn("release_lease", ledger.operations)
        self.assertLess(ledger.operations.index("capture_response"), ledger.operations.index("release_lease"))

    def test_extraction_failure_releases_lease(self) -> None:
        ledger = FakeLedger()

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture_response")
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("extract_prompt")
            return ServiceResult(False, reason_code="extraction_failed", error_message="bad response")

        result, _planner, ledger, submit, _capture, _extract, _run_prompt = self._run_step(
            _capture_plan(),
            ledger=ledger,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "extraction_failed")
        self.assertEqual(len(submit.calls), 0)
        self.assertIn("release_lease", ledger.operations)
        self.assertLess(ledger.operations.index("extract_prompt"), ledger.operations.index("release_lease"))

    def test_submit_exception_retries_then_releases_lease_without_crashing(self) -> None:
        ledger = FakeLedger()

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit_feedback")
            raise RuntimeError("boom")

        result, _planner, ledger, *_ = self._run_step(
            _send_plan(),
            ledger=ledger,
            submit_service=submit,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "submit_feedback_exception")
        self.assertEqual(
            [op for op in ledger.operations if op in {"acquire_lease", "submit_feedback", "release_lease"}],
            ["acquire_lease"]
            + ["submit_feedback"] * CHATGPT_HANDOFF_MAX_UI_ATTEMPTS
            + ["release_lease"],
        )

    def test_release_failure_is_surfaced_without_forced_takeover(self) -> None:
        ledger = FakeLedger(release_fails=True)

        result, _planner, ledger, *_ = self._run_step(_extract_plan(), ledger=ledger)

        self.assertTrue(result.ok)
        self.assertEqual(
            result.metadata["chatgpt_ui_lease"]["release_reason_code"],
            "chatgpt_ui_lease_release_failed",
        )
        self.assertIn(
            "chatgpt_ui_lease_release_failed",
            [event["event_type"] for event in ledger.added_events],
        )
        self.assertNotIn("takeover", " ".join(ledger.operations))

    def test_no_lease_timeout_or_deadline_operation_is_added(self) -> None:
        ledger = FakeLedger()

        result, _planner, ledger, *_ = self._run_step(_extract_plan(), ledger=ledger)

        self.assertTrue(result.ok)
        forbidden = ("timeout", "deadline", "expires", "heartbeat", "takeover")
        self.assertFalse(any(fragment in op for op in ledger.operations for fragment in forbidden))


from agent.supervision_services import NavigationAttemptResult


@dataclass
class NavRecorder:
    result: object = None
    exception: Exception | None = None
    operations: list[str] | None = None
    calls: list[tuple] = field(default_factory=list)

    def __call__(self, run_id, binding, lease_context, *, app_name, ledger):
        self.calls.append((run_id, binding, app_name))
        if self.operations is not None:
            self.operations.append("navigate")
        if self.exception is not None:
            raise self.exception
        if self.result is None:
            return NavigationAttemptResult(ok=True, outcome="chat_opened_via_axpress")
        return self.result


class NavigationBeforeGateTests(unittest.TestCase):
    def _run_step(self, plan: SupervisePlan, **kwargs):
        planner = _planner_for(plan)
        submit = kwargs.pop("submit_service", ActionRecorder())
        capture = kwargs.pop("capture_service", ActionRecorder())
        extract = kwargs.pop("extraction_service", ActionRecorder())
        run_prompt = kwargs.pop("extracted_prompt_execution_service", ActionRecorder())
        ledger = kwargs.pop("ledger", FakeLedger())
        destination_adapter_factory = kwargs.pop(
            "destination_adapter_factory",
            _destination_adapter_factory(operations=ledger.operations),
        )
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
            destination_adapter_factory=destination_adapter_factory,
            **kwargs,
        )
        return result, ledger, submit, capture, extract, run_prompt

    def _phase_event(self, ledger: FakeLedger) -> dict:
        events = [e for e in ledger.added_events if e["event_type"] == "chatgpt_handoff_phase"]
        self.assertEqual(len(events), 1)
        return events[0]

    def test_disabled_navigation_never_calls_navigator_and_stays_gate_first(self) -> None:
        nav = NavRecorder(exception=AssertionError("navigator must not run when disabled"))
        ledger = FakeLedger()
        result, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            destination_navigation_service=nav,
            destination_adapter_factory=_destination_adapter_factory(
                _exact_destination_snapshot(project_title="Other Project"),
                operations=ledger.operations,
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "project_not_active")
        self.assertEqual(len(nav.calls), 0)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertNotIn("navigate", ledger.operations)
        self.assertIn("release_lease", ledger.operations)
        self.assertEqual(self._phase_event(ledger)["metadata"]["handoff_phase"], "verification_failed")

    def test_enabled_navigation_orders_lease_navigate_gate_submit(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(operations=ledger.operations)

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit")
            return ServiceResult(True)

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture")
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("extract")
            return ServiceResult(True)

        result, ledger, _submit, _capture, _extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(nav.calls), 1)
        ordered = [
            op
            for op in ledger.operations
            if op
            in {"acquire_lease", "navigate", "read_only_destination_gate", "submit", "release_lease"}
        ]
        self.assertEqual(
            ordered,
            ["acquire_lease", "navigate", "read_only_destination_gate", "submit", "release_lease"],
        )
        self.assertEqual(self._phase_event(ledger)["metadata"]["handoff_phase"], "continuation_started")

    def test_navigation_failure_retries_until_destination_opens(self) -> None:
        ledger = FakeLedger()
        nav_calls: list[str] = []

        def nav(run_id, binding, lease_context, *, app_name, ledger):
            del run_id, binding, lease_context, app_name
            nav_calls.append("navigate")
            ledger.operations.append("navigate")
            if len(nav_calls) < CHATGPT_HANDOFF_MAX_UI_ATTEMPTS:
                return NavigationAttemptResult(
                    ok=False,
                    outcome="target_not_interactable",
                    reason_code="destination_navigation_action_not_performed",
                )
            return NavigationAttemptResult(
                ok=True,
                outcome="chat_opened_via_axpress",
                action_posted=True,
                navigator_confirmed=True,
            )

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit")
            return ServiceResult(True)

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture")
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("extract")
            return ServiceResult(True)

        result, ledger, _submit, _capture, _extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(nav_calls), CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)
        self.assertEqual(ledger.operations.count("submit"), 1)
        self.assertEqual(self._phase_event(ledger)["metadata"]["handoff_phase"], "continuation_started")

    def test_navigation_failure_blocks_before_gate_and_releases_lease(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(
            result=NavigationAttemptResult(
                ok=False,
                outcome="project_opened_but_chats_not_available",
                reason_code="destination_navigation_incomplete",
            ),
            operations=ledger.operations,
        )
        submit = ActionRecorder(exception=AssertionError("submit should not run"))
        capture = ActionRecorder(exception=AssertionError("capture should not run"))
        extract = ActionRecorder(exception=AssertionError("extract should not run"))

        result, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason_code, "destination_navigation_incomplete")
        self.assertEqual(result.run_status, "needs_review")
        self.assertEqual(len(nav.calls), CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertNotIn("read_only_destination_gate", ledger.operations)
        self.assertIn("release_lease", ledger.operations)
        attempt = [
            e for e in ledger.added_events if e["event_type"] == "chatgpt_destination_navigation_attempt"
        ]
        self.assertEqual(len(attempt), CHATGPT_HANDOFF_MAX_UI_ATTEMPTS)
        self.assertEqual(
            [event["metadata"]["attempt_number"] for event in attempt],
            list(range(1, CHATGPT_HANDOFF_MAX_UI_ATTEMPTS + 1)),
        )
        self.assertFalse(attempt[-1]["metadata"]["navigation_ok"])
        diagnostics = attempt[-1]["metadata"]["navigation_action_diagnostics"]
        self.assertEqual(
            set(diagnostics),
            {
                "target_detected",
                "target_candidate_count",
                "actionable_element_resolved",
                "selected_element_role",
                "selected_relation",
                "available_ax_actions",
                "chosen_method",
                "axpress_attempted",
                "axpress_result",
                "ax_error_code",
                "ui_changed_after_action",
                "destination_confirmed",
                "final_reresolution_status",
                "project_open_outcome",
                "project_open_target_match_count",
                "project_open_truncated_by_node_limit",
                "project_open_truncated_by_depth_limit",
                "project_open_stability_status",
            },
        )
        self.assertFalse(diagnostics["destination_confirmed"])
        self.assertEqual(self._phase_event(ledger)["metadata"]["handoff_phase"], "navigation_failed")

    def test_navigation_success_but_gate_failure_blocks_and_releases_lease(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(operations=ledger.operations)
        submit = ActionRecorder(exception=AssertionError("submit should not run"))
        capture = ActionRecorder(exception=AssertionError("capture should not run"))

        result, ledger, submit, capture, _extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            submit_service=submit,
            capture_service=capture,
            destination_adapter_factory=_destination_adapter_factory(
                _exact_destination_snapshot(chat_title="Other Chat"),
                operations=ledger.operations,
            ),
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason_code, "chat_not_active")
        self.assertEqual(result.run_status, "needs_review")
        self.assertEqual(len(nav.calls), 2)
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertIn("navigate", ledger.operations)
        self.assertEqual(
            ledger.operations.count("read_only_destination_gate"),
            2,
        )
        self.assertIn("release_lease", ledger.operations)
        blocked = [
            e for e in ledger.added_events if e["event_type"] == "chatgpt_destination_gate_blocked_handoff"
        ]
        self.assertEqual(len(blocked), 2)
        self.assertEqual(blocked[0]["metadata"]["navigation"]["ok"], True)
        self.assertTrue(blocked[0]["metadata"]["will_retry"])
        self.assertFalse(blocked[1]["metadata"]["will_retry"])
        self.assertEqual(self._phase_event(ledger)["metadata"]["handoff_phase"], "verification_failed")

    def test_navigation_runs_at_most_once_per_transaction(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(operations=ledger.operations)

        def capture(*args, **kwargs):
            del args, kwargs
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            return ServiceResult(True)

        result, ledger, _submit, _capture, _extract, _run_prompt = self._run_step(
            _capture_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(nav.calls), 1)
        self.assertEqual(ledger.operations.count("navigate"), 1)

    def test_navigation_enabled_adds_no_timeout_or_deadline_operation(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(operations=ledger.operations)

        result, ledger, *_ = self._run_step(
            _extract_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
        )

        self.assertTrue(result.ok)
        forbidden = ("timeout", "deadline", "expires", "heartbeat", "takeover")
        self.assertFalse(any(fragment in op for op in ledger.operations for fragment in forbidden))

    def test_phase_event_exposes_bounded_state_without_raw_evidence(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(operations=ledger.operations)

        _result, ledger, *_ = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            destination_adapter_factory=_destination_adapter_factory(
                _exact_destination_snapshot(project_title="Other Project"),
                operations=ledger.operations,
            ),
        )

        metadata = self._phase_event(ledger)["metadata"]
        self.assertEqual(
            set(metadata),
            {"run_id", "handoff_phase", "navigation_operator_approved", "navigation"},
        )
        self.assertTrue(metadata["navigation_operator_approved"])
        self.assertIn(metadata["handoff_phase"], HANDOFF_PHASES)
        self.assertEqual(
            set(metadata["navigation"]),
            {"ok", "outcome", "reason_code", "action_posted", "navigator_confirmed"},
        )
        encoded = json.dumps(metadata, sort_keys=True)
        self.assertNotIn(ledger.lease_token, encoded)
        for forbidden in ("prompt_text", "response_text", "ax_tree", "transcript", "stdout"):
            self.assertNotIn(forbidden, encoded)

    def test_action_posted_without_navigator_confirmation_reaches_submit_via_gate(self) -> None:
        # Real-run scenario f85f7126: the chat-open action was posted but the
        # navigator's own heuristic did not confirm it. The authoritative gate
        # (here proving the exact destination) must now decide, and submission
        # must proceed.
        ledger = FakeLedger()
        nav = NavRecorder(
            result=NavigationAttemptResult(
                ok=True,
                outcome="action_posted_but_chat_not_confirmed",
                action_posted=True,
                navigator_confirmed=False,
            ),
            operations=ledger.operations,
        )

        def submit(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("submit")
            return ServiceResult(True)

        def capture(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("capture")
            return ServiceResult(True)

        def extract(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("extract")
            return ServiceResult(True)

        result, ledger, _submit, _capture, _extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            submit_service=submit,
            capture_service=capture,
            extraction_service=extract,
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(nav.calls), 1)
        ordered = [
            op
            for op in ledger.operations
            if op in {"navigate", "read_only_destination_gate", "submit", "capture", "extract"}
        ]
        self.assertEqual(
            ordered, ["navigate", "read_only_destination_gate", "submit", "capture", "extract"]
        )
        phase = self._phase_event(ledger)["metadata"]
        self.assertEqual(phase["handoff_phase"], "continuation_started")
        self.assertFalse(phase["navigation"]["navigator_confirmed"])
        self.assertTrue(phase["navigation"]["action_posted"])

    def test_action_posted_then_gate_unavailable_blocks_submission(self) -> None:
        ledger = FakeLedger()
        nav = NavRecorder(
            result=NavigationAttemptResult(
                ok=True,
                outcome="action_posted_but_chat_not_confirmed",
                action_posted=True,
            ),
            operations=ledger.operations,
        )
        submit = ActionRecorder(exception=AssertionError("submit should not run"))

        def gate(*args, **kwargs):
            del args, kwargs
            ledger.operations.append("read_only_destination_gate")
            raise RuntimeError("adapter exploded")

        result, ledger, submit, capture, extract, _run_prompt = self._run_step(
            _send_plan(),
            ledger=ledger,
            allow_destination_navigation=True,
            destination_navigation_service=nav,
            destination_gate_service=gate,
            submit_service=submit,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason_code, "destination_verification_unavailable")
        self.assertEqual(result.run_status, "needs_review")
        self.assertEqual(len(submit.calls), 0)
        self.assertEqual(len(capture.calls), 0)
        self.assertEqual(len(extract.calls), 0)
        self.assertIn("release_lease", ledger.operations)
        self.assertEqual(self._phase_event(ledger)["metadata"]["handoff_phase"], "verification_failed")

    def test_navigation_result_mapping_defers_unconfirmed_open_to_authoritative_gate(self) -> None:
        from agent.supervision_services import _navigation_attempt_from_open_result

        # Confirmed open: performed + navigator-confirmed.
        confirmed = _navigation_attempt_from_open_result(
            {"ok": True, "outcome": "chat_opened_via_axpress", "chat_open_action_posted": True}
        )
        self.assertTrue(confirmed.ok)
        self.assertTrue(confirmed.action_posted)
        self.assertTrue(confirmed.navigator_confirmed)

        # Real-run: action posted, navigator heuristic did NOT confirm -> defer to gate.
        deferred = _navigation_attempt_from_open_result(
            {
                "ok": False,
                "outcome": "action_posted_but_chat_not_confirmed",
                "chat_open_action_posted": True,
            }
        )
        self.assertTrue(deferred.ok)
        self.assertTrue(deferred.action_posted)
        self.assertFalse(deferred.navigator_confirmed)
        self.assertFalse(deferred.navigation_action_diagnostics["destination_confirmed"])
        self.assertEqual(deferred.navigation_action_diagnostics["axpress_result"], "not_attempted")

        instrumented = _navigation_attempt_from_open_result(
            {
                "ok": False,
                "outcome": "action_posted_but_chat_not_confirmed",
                "chat_open_action_posted": True,
                "target_detected": True,
                "target_candidate_count": 1,
                "actionable_element_resolved": True,
                "selected_element_role": "AXButton",
                "selected_relation": "row_node",
                "available_ax_actions": ["AXPress"],
                "chosen_method": "axpress",
                "axpress_attempted": True,
                "axpress_result": "success",
                "ax_error_code": 0,
                "ui_changed_after_action": False,
                "destination_confirmed": False,
                "final_reresolution_status": "confirmed",
            }
        )
        self.assertTrue(instrumented.action_posted)
        self.assertEqual(instrumented.navigation_action_diagnostics["selected_relation"], "row_node")
        self.assertEqual(instrumented.navigation_action_diagnostics["ax_error_code"], 0)
        self.assertFalse(instrumented.navigation_action_diagnostics["ui_changed_after_action"])

        project_failure = _navigation_attempt_from_open_result(
            {
                "ok": False,
                "outcome": "project_open_failed",
                "chat_open_action_posted": False,
                "project_open_result": {
                    "outcome": "target_absent",
                    "target_match_count": 0,
                    "activation_stability_status": "stable",
                    "traversal": {
                        "truncated_by_node_limit": True,
                        "truncated_by_depth_limit": False,
                    },
                },
            }
        )
        project_diagnostics = project_failure.navigation_action_diagnostics
        self.assertEqual(project_diagnostics["project_open_outcome"], "target_absent")
        self.assertEqual(project_diagnostics["project_open_target_match_count"], 0)
        self.assertTrue(project_diagnostics["project_open_truncated_by_node_limit"])
        self.assertFalse(project_diagnostics["project_open_truncated_by_depth_limit"])
        self.assertEqual(project_diagnostics["project_open_stability_status"], "stable")

        # Action never posted -> genuine navigation failure, stays fail-closed.
        for outcome in ("project_open_failed", "chat_row_not_interactable", "click_posting_failed"):
            attempt = _navigation_attempt_from_open_result(
                {"ok": False, "outcome": outcome, "chat_open_action_posted": False}
            )
            self.assertFalse(attempt.ok, outcome)
            self.assertFalse(attempt.action_posted, outcome)
            self.assertEqual(attempt.reason_code, "destination_navigation_action_not_performed")

    def test_gate_module_source_contains_no_navigation_behavior(self) -> None:
        import inspect

        import agent.chatgpt_destination_gate as gate_module

        source = inspect.getsource(gate_module)
        for forbidden in (
            "open_chatgpt_project_chat",
            "chatgpt_navigation_diagnostic",
            "AXPress",
            "scroll",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
