"""Stage 6: multi-session restore and global durability safety.

Deterministic fakes plus a temporary real ledger. No live desktop automation.

Coverage map (Stage 6 acceptance):
- a terminal focused run no longer drops live siblings
- every persisted runtime is reconciled independently
- mixed ChatGPT/Codex/approval/retry states recover independently
- queue loss does not lose the logical handoff; stale queue does not duplicate
- conversation claim is verified before resumed advancement
- needs_review resume cannot share a chat with a later live owner
- critical ledger write failure blocks new Codex/ChatGPT mutations globally
- telemetry/snapshot failures stay non-critical
- repeated restore is idempotent
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from agent import ledger as ledger_module
from agent.codex_invocation import STATUS_COMPLETE, STATUS_LIVE, STATUS_UNCERTAIN
from agent.local_controller import (
    CONVERSATION_CLAIM_CONFLICT_REASON_CODE,
    DurabilityGuardedLedger,
    LEDGER_DURABILITY_BLOCKED_REASON_CODE,
    LOCAL_CONTROLLER_STATE_BLOCKED,
    LOCAL_CONTROLLER_STATE_COMPLETED,
    LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL,
    LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY,
    LedgerDurabilityGuard,
    LocalController,
    LocalControllerSession,
)
from agent.run_state import RunStatus
from tests.test_multi_session_concurrency import (
    WAIT_TIMEOUT_SECONDS,
    MultiRunFakeLedger,
    PerRunInitialExecutor,
    SessionWorld,
    _approval_model,
    _blocked_model,
    _completed_model,
    _join_workers,
    _read_model,
    _routine_model,
    _success_step_result,
    _temporary_real_ledger,
    _wait_step_result,
    _wait_until,
)


def _payload(
    *,
    state: str,
    chat: str,
    pending: dict | None = None,
    latch: bool = False,
) -> dict:
    return {
        "controller_state": state,
        "pending_approval": pending,
        "repository_path": "/tmp",
        "sandbox": "read-only",
        "project_title": "craxii",
        "chat_title": chat,
        "allow_destination_navigation": False,
        "navigation_forced_multi_live": latch,
    }


def _approval_pending(run_id: str, *, prompt_sha: str | None = None) -> dict:
    return {
        "run_id": run_id,
        "approval_kind": "send_to_gpt",
        "planner_action": "ask_send_to_gpt",
        "planner_reason_code": "codex_result_ready",
        "planner_metadata": {
            "action": "ask_send_to_gpt",
            "reason": "codex_result_ready",
            "event_ids": {"codex_exec_finished": 10},
            "prompt_sha": prompt_sha,
        },
        "latest_event_id": 10,
        "expected_extraction_event_id": None,
        "expected_prompt_sha256": prompt_sha,
        "expected_prompt_text_sha256": None,
        "expected_extraction_method": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _retry_model(run_id: str, repo: str, *, routine: bool = False) -> object:
    return replace(
        _read_model(
            run_id,
            action="ask_send_to_gpt",
            reason="chatgpt_handoff_yielded_retryable_ui_failure",
            routine=routine,
            stage="waiting_for_retry" if not routine else "routine_action_available",
            repo_path=repo,
        ),
        latest_failure={
            "event_id": 7,
            "retryable": True,
            "retry_classification": "retryable",
            "action_executed": False,
            "reason_code": "chatgpt_handoff_yielded_retryable_ui_failure",
        },
    )


def _send_model(run_id: str, repo: str) -> object:
    return _read_model(
        run_id,
        action="ask_send_to_gpt",
        reason="codex_result_ready",
        routine=True,
        stage="routine_action_available",
        repo_path=repo,
    )


def _extract_model(run_id: str, repo: str) -> object:
    return _read_model(
        run_id,
        action="extract_next_prompt",
        reason="gpt_response_captured_extract_needed",
        routine=True,
        stage="routine_action_available",
        repo_path=repo,
    )


def _restore_controller(
    ledger: object,
    world: SessionWorld,
    *,
    executor: object | None = None,
    sleeper: object | None = None,
    max_sessions: int = 4,
) -> LocalController:
    return LocalController(
        ledger=ledger,
        read_model_builder=world.read_model_builder,
        supervision_step=world.supervision_step,
        initial_run_executor=executor if executor is not None else PerRunInitialExecutor(),
        max_active_sessions=max_sessions,
        chatgpt_wait_sleeper=sleeper if sleeper is not None else (lambda seconds: None),
        desktop_mutex=object(),
    )


def _seed_runs(ledger: MultiRunFakeLedger, count: int) -> list[str]:
    return [ledger.create_run(f"Task {index}") for index in range(count)]


class TerminalFocusedRestoreTests(unittest.TestCase):
    def test_terminal_focused_run_does_not_drop_live_siblings(self) -> None:
        ledger = MultiRunFakeLedger()
        run_a, run_b, run_c, run_d = _seed_runs(ledger, 4)
        ledger.set_run_status(run_a, RunStatus.FAILED.value)
        ledger.set_run_status(run_b, RunStatus.RUNNING.value)
        ledger.set_run_status(run_c, RunStatus.WAITING_FOR_APPROVAL.value)
        ledger.set_run_status(run_d, RunStatus.COMPLETED.value)
        ledger.controller_snapshot = {
            "schema_version": 2,
            "active_run_id": run_a,
            "controller_state": LOCAL_CONTROLLER_STATE_COMPLETED,
            "sessions": {
                run_a: _payload(state="completed", chat="Chat A"),
                run_b: _payload(state="running_routine_action", chat="Chat B"),
                run_c: _payload(
                    state="waiting_for_approval",
                    chat="Chat C",
                    pending=_approval_pending(run_c),
                ),
                run_d: _payload(state="waiting_for_retry", chat="Chat D"),
            },
        }
        world = SessionWorld()
        world.set_model(run_b, _send_model(run_b, "/tmp"))
        world.set_model(run_c, _approval_model(run_c, "/tmp"))
        world.set_model(run_d, _retry_model(run_d, "/tmp"))
        world.queue_step(
            run_b, _success_step_result(), next_model=_completed_model(run_b, "/tmp")
        )

        controller = _restore_controller(ledger, world)
        _join_workers(controller)

        self.assertNotIn(run_a, controller._sessions)
        self.assertIn(run_b, controller._sessions)
        self.assertIn(run_c, controller._sessions)
        self.assertIn(run_d, controller._sessions)
        self.assertEqual(
            controller._sessions[run_c].controller_state,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL,
        )
        self.assertIsNotNone(controller._sessions[run_c].pending_approval)
        self.assertEqual(
            controller._sessions[run_d].controller_state,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY,
        )
        self.assertNotEqual(controller.session.active_run_id, run_a)
        self.assertIn(controller.session.active_run_id, {run_b, run_c, run_d})

    def test_all_persisted_runtimes_are_independently_reconciled(self) -> None:
        ledger = MultiRunFakeLedger()
        run_a, run_b = _seed_runs(ledger, 2)
        ledger.controller_snapshot = {
            "active_run_id": run_a,
            "sessions": {
                run_a: _payload(state="running_routine_action", chat="Chat A"),
                run_b: _payload(state="running_routine_action", chat="Chat B"),
            },
        }
        world = SessionWorld()
        world.set_model(run_a, _send_model(run_a, "/tmp"))
        world.set_model(run_b, _extract_model(run_b, "/tmp"))
        world.queue_step(
            run_a, _success_step_result(), next_model=_completed_model(run_a, "/tmp")
        )
        world.queue_step(
            run_b, _success_step_result(), next_model=_completed_model(run_b, "/tmp")
        )

        controller = _restore_controller(ledger, world)
        self.assertTrue(
            _wait_until(lambda: len(world.calls_for(run_a)) >= 1 and len(world.calls_for(run_b)) >= 1)
        )
        _join_workers(controller)
        self.assertEqual(len(world.calls_for(run_a)), 1)
        self.assertEqual(len(world.calls_for(run_b)), 1)


class MixedStateRestoreTests(unittest.TestCase):
    def test_scenario_a_mixed_chatgpt_codex_approval_states(self) -> None:
        ledger = MultiRunFakeLedger()
        run_a, run_b, run_c, run_d = _seed_runs(ledger, 4)
        ledger.controller_snapshot = {
            "active_run_id": run_a,
            "sessions": {
                run_a: _payload(state="running_routine_action", chat="Chat A"),
                run_b: _payload(state="running_routine_action", chat="Chat B"),
                run_c: _payload(state="running_routine_action", chat="Chat C"),
                run_d: _payload(
                    state="waiting_for_approval",
                    chat="Chat D",
                    pending=_approval_pending(run_d),
                ),
            },
        }
        world = SessionWorld()
        world.set_model(run_a, _routine_model(run_a, "/tmp"))
        world.set_model(run_b, _send_model(run_b, "/tmp"))
        world.set_model(run_c, _extract_model(run_c, "/tmp"))
        world.set_model(run_d, _approval_model(run_d, "/tmp"))
        for run_id in (run_a, run_b, run_c):
            world.queue_step(
                run_id,
                _success_step_result(),
                next_model=_completed_model(run_id, "/tmp"),
            )
        live_item = SimpleNamespace(status=STATUS_LIVE, invocation_id="inv-c")
        reconcile_calls: list[str] = []

        def fake_open(run_id: str, events=None):
            del events
            return live_item if run_id == run_c else None

        def fake_reconcile(run_id: str, ledger=None):
            del ledger
            reconcile_calls.append(run_id)
            return SimpleNamespace(ok=True, reason_code="observed")

        with mock.patch(
            "agent.local_controller.latest_open_invocation", side_effect=fake_open
        ), mock.patch(
            "agent.local_controller.reconcile_codex_invocation",
            side_effect=fake_reconcile,
        ):
            controller = _restore_controller(ledger, world)
            self.assertTrue(
                _wait_until(lambda: run_c in reconcile_calls)
            )
            _join_workers(controller)

        self.assertEqual(
            controller._sessions[run_d].controller_state,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL,
        )
        self.assertEqual(world.calls_for(run_a)[0]["run_id"], run_a)
        self.assertEqual(world.calls_for(run_b)[0]["run_id"], run_b)
        self.assertIn(run_c, reconcile_calls)
        self.assertEqual(world.calls_for(run_d), [])

    def test_verified_submit_restores_to_capture_not_send(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="running_routine_action", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _routine_model(run_id, "/tmp"))
        recorded: list[str] = []

        def step(run_id: str, *args, **kwargs):
            del args, kwargs
            recorded.append(world.models[run_id].planner_action)
            world.set_model(run_id, _completed_model(run_id, "/tmp"))
            return _success_step_result()

        world.supervision_step = step
        controller = _restore_controller(ledger, world)
        _join_workers(controller)
        self.assertEqual(recorded, ["capture_gpt_response"])

    def test_captured_response_restores_to_extract_not_chatgpt(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="running_routine_action", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _extract_model(run_id, "/tmp"))
        recorded: list[str] = []

        def step(run_id: str, *args, **kwargs):
            del args, kwargs
            recorded.append(world.models[run_id].planner_action)
            world.set_model(run_id, _completed_model(run_id, "/tmp"))
            return _success_step_result()

        world.supervision_step = step
        controller = _restore_controller(ledger, world)
        _join_workers(controller)
        self.assertEqual(recorded, ["extract_next_prompt"])

    def test_extracted_prompt_with_no_codex_start_launches_once(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="running_routine_action", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(
            run_id,
            _read_model(
                run_id,
                action="ask_run_prompt",
                reason="fresh_sentinel_prompt_ready",
                routine=True,
                stage="routine_action_available",
            ),
        )
        world.queue_step(
            run_id, _success_step_result(), next_model=_completed_model(run_id, "/tmp")
        )
        controller = _restore_controller(ledger, world)
        _join_workers(controller)
        self.assertEqual(len(world.calls_for(run_id)), 1)

    def test_complete_invocation_finalizes_idempotently(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="running_routine_action", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _completed_model(run_id, "/tmp"))
        reconcile_calls: list[str] = []
        open_calls = {"n": 0}

        def fake_open(target: str, events=None):
            del events
            open_calls["n"] += 1
            if target == run_id and open_calls["n"] == 1:
                return SimpleNamespace(status=STATUS_COMPLETE, invocation_id="inv-1")
            return None

        def fake_reconcile(target: str, ledger=None):
            del ledger
            reconcile_calls.append(target)
            return SimpleNamespace(ok=True)

        with mock.patch(
            "agent.local_controller.latest_open_invocation", side_effect=fake_open
        ), mock.patch(
            "agent.local_controller.reconcile_codex_invocation",
            side_effect=fake_reconcile,
        ):
            controller = _restore_controller(ledger, world)
            _join_workers(controller)
            again = _restore_controller(ledger, world)
            _join_workers(again)

        self.assertEqual(reconcile_calls, [run_id])
        self.assertEqual(world.calls_for(run_id), [])

    def test_uncertain_invocation_stays_blocked_and_never_reruns(self) -> None:
        ledger = MultiRunFakeLedger()
        run_a, run_b, run_c = _seed_runs(ledger, 3)
        ledger.controller_snapshot = {
            "active_run_id": run_a,
            "sessions": {
                run_a: _payload(state="running_routine_action", chat="Chat A"),
                run_b: _payload(state="running_routine_action", chat="Chat B"),
                run_c: _payload(state="running_routine_action", chat="Chat C"),
            },
        }
        world = SessionWorld()
        world.set_model(run_a, _send_model(run_a, "/tmp"))
        world.set_model(run_b, _routine_model(run_b, "/tmp"))
        world.set_model(run_c, _send_model(run_c, "/tmp"))
        world.queue_step(
            run_b, _success_step_result(), next_model=_completed_model(run_b, "/tmp")
        )
        world.queue_step(
            run_c, _success_step_result(), next_model=_completed_model(run_c, "/tmp")
        )

        def fake_open(run_id: str, events=None):
            del events
            if run_id == run_a:
                return SimpleNamespace(
                    status=STATUS_UNCERTAIN, invocation_id="inv-uncertain"
                )
            return None

        with mock.patch(
            "agent.local_controller.latest_open_invocation", side_effect=fake_open
        ), mock.patch(
            "agent.local_controller.reconcile_codex_invocation",
            return_value=SimpleNamespace(ok=False, reason_code="uncertain"),
        ):
            controller = _restore_controller(ledger, world)
            _join_workers(controller)

        self.assertEqual(
            controller._sessions[run_a].controller_state, LOCAL_CONTROLLER_STATE_BLOCKED
        )
        self.assertEqual(world.calls_for(run_a), [])
        self.assertEqual(len(world.calls_for(run_b)), 1)
        self.assertEqual(len(world.calls_for(run_c)), 1)

    def test_approval_restores_only_when_integrity_matches(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        pending = _approval_pending(run_id)
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(
                    state="waiting_for_approval",
                    chat="Chat A",
                    pending=pending,
                ),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _approval_model(run_id, "/tmp"))
        controller = _restore_controller(ledger, world)
        runtime = controller._sessions[run_id]
        self.assertEqual(
            runtime.controller_state, LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
        )
        self.assertEqual(runtime.pending_approval.run_id, run_id)
        self.assertEqual(runtime.pending_approval.planner_action, "ask_send_to_gpt")
        self.assertEqual(world.calls_for(run_id), [])

    def test_stale_approval_is_rejected_and_rebuilt_from_evidence(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        stale = _approval_pending(run_id, prompt_sha="a" * 64)
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(
                    state="waiting_for_approval",
                    chat="Chat A",
                    pending=stale,
                ),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _approval_model(run_id, "/tmp"))
        controller = _restore_controller(ledger, world)
        runtime = controller._sessions[run_id]
        self.assertEqual(
            runtime.controller_state, LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
        )
        self.assertIsNotNone(runtime.pending_approval)
        self.assertNotEqual(runtime.pending_approval.expected_prompt_sha256, "a" * 64)

    def test_retryable_ui_wait_auto_retries_after_restart(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="waiting_for_retry", chat="Chat A"),
            },
        }
        world = SessionWorld()
        healthy = {"ok": False}
        sleeps: list[float] = []
        calls: list[str] = []
        world.set_model(run_id, _retry_model(run_id, "/tmp", routine=True))

        def step(target: str, *args, **kwargs):
            del args, kwargs
            calls.append(target)
            if not healthy["ok"]:
                return _wait_step_result("ask_send_to_gpt")
            world.set_model(target, _completed_model(target, "/tmp"))
            return _success_step_result("ask_send_to_gpt")

        world.supervision_step = step

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                healthy["ok"] = True

        controller = _restore_controller(ledger, world, sleeper=sleeper)
        _join_workers(controller)
        self.assertGreaterEqual(min(sleeps), 0.5)
        self.assertLessEqual(max(sleeps), 8.0)
        self.assertEqual(calls, [run_id, run_id])
        self.assertEqual(
            controller._sessions[run_id].controller_state,
            LOCAL_CONTROLLER_STATE_COMPLETED,
        )

    def test_uncertain_chatgpt_submit_does_not_auto_resend(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="running_routine_action", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(
            run_id,
            replace(
                _send_model(run_id, "/tmp"),
                latest_chatgpt_submission={"status": "ambiguous"},
                latest_failure={
                    "event_id": 9,
                    "retryable": False,
                    "retry_classification": "reconcile",
                    "reason_code": "chatgpt_submission_ambiguous",
                    "action_executed": True,
                },
            ),
        )
        controller = _restore_controller(ledger, world)
        _join_workers(controller)
        self.assertEqual(
            controller._sessions[run_id].controller_state, LOCAL_CONTROLLER_STATE_BLOCKED
        )
        self.assertEqual(world.calls_for(run_id), [])

    def test_conversation_claim_conflict_does_not_auto_advance(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="waiting_for_retry", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(
            run_id,
            replace(
                _send_model(run_id, "/tmp"),
                latest_failure={
                    "event_id": 4,
                    "retryable": False,
                    "retry_classification": "review_required",
                    "reason_code": CONVERSATION_CLAIM_CONFLICT_REASON_CODE,
                    "action_executed": False,
                },
            ),
        )
        controller = _restore_controller(ledger, world)
        _join_workers(controller)
        self.assertEqual(
            controller._sessions[run_id].controller_state, LOCAL_CONTROLLER_STATE_BLOCKED
        )
        self.assertEqual(world.calls_for(run_id), [])

    def test_cancelled_session_remains_absorbing(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.set_run_status(run_id, RunStatus.FAILED.value)
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="blocked", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _send_model(run_id, "/tmp"))
        controller = _restore_controller(ledger, world)
        self.assertNotIn(run_id, controller._sessions)
        self.assertEqual(world.calls_for(run_id), [])


class QueueReconstructionTests(unittest.TestCase):
    def test_queue_loss_does_not_lose_handoff_and_stale_queue_is_idempotent(self) -> None:
        with _temporary_real_ledger(), TemporaryDirectory() as repo:
            run_id = ledger_module.create_run("Task")
            ledger_module.update_run_status(run_id, RunStatus.COMPLETED)
            ledger_module.save_local_controller_snapshot(
                {
                    "active_run_id": run_id,
                    "sessions": {
                        run_id: _payload(state="running_routine_action", chat="Chat A"),
                    },
                }
            )
            world = SessionWorld()
            world.set_model(run_id, _send_model(run_id, repo))
            enqueues: list[str] = []

            def step(target: str, *args, **kwargs):
                del args
                ledger = kwargs.get("ledger") or ledger_module
                result = ledger.enqueue_chatgpt_handoff(
                    target, enqueue_source="ask_send_to_gpt"
                )
                enqueues.append(str(result.status))
                world.set_model(target, _completed_model(target, repo))
                return _success_step_result()

            world.supervision_step = step
            first = _restore_controller(ledger_module, world)
            _join_workers(first)
            self.assertEqual(enqueues, ["enqueued"])

            world.set_model(run_id, _send_model(run_id, repo))
            second = _restore_controller(ledger_module, world)
            _join_workers(second)
            self.assertEqual(enqueues, ["enqueued", "idempotent"])

            stale = ledger_module.enqueue_chatgpt_handoff(
                run_id, enqueue_source="ask_send_to_gpt"
            )
            self.assertEqual(
                stale.status, ledger_module.AtomicChatGPTHandoffQueueStatus.IDEMPOTENT
            )

    def test_terminal_run_does_not_reenqueue(self) -> None:
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("Task")
            ledger_module.update_run_status(run_id, RunStatus.FAILED)
            ledger_module.save_local_controller_snapshot(
                {
                    "active_run_id": run_id,
                    "sessions": {
                        run_id: _payload(state="blocked", chat="Chat A"),
                    },
                }
            )
            world = SessionWorld()
            world.set_model(run_id, _send_model(run_id, "/tmp"))
            controller = _restore_controller(ledger_module, world)
            _join_workers(controller)
            enqueue = ledger_module.enqueue_chatgpt_handoff(
                run_id, enqueue_source="ask_send_to_gpt"
            )
            self.assertEqual(
                enqueue.status,
                ledger_module.AtomicChatGPTHandoffQueueStatus.RUN_TERMINAL,
            )


class WorkerAndIdempotencyTests(unittest.TestCase):
    def test_restore_does_not_duplicate_workers_for_one_session(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="running_routine_action", chat="Chat A"),
            },
        }
        world = SessionWorld()
        gate = threading.Event()
        entered = threading.Event()
        world.set_model(run_id, _send_model(run_id, "/tmp"))
        world.queue_step(
            run_id,
            _success_step_result(),
            next_model=_completed_model(run_id, "/tmp"),
            entered=entered,
            gate=gate,
        )
        controller = _restore_controller(ledger, world)
        self.assertTrue(entered.wait(WAIT_TIMEOUT_SECONDS))
        runtime = controller._sessions[run_id]
        self.assertTrue(runtime.action_running)
        self.assertIsNotNone(runtime.current_worker)
        workers = [
            item.current_worker
            for item in controller._sessions.values()
            if item.current_worker is not None
        ]
        self.assertEqual(len(workers), 1)
        gate.set()
        _join_workers(controller)

    def test_repeated_restore_is_idempotent(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(
                    state="waiting_for_approval",
                    chat="Chat A",
                    pending=_approval_pending(run_id),
                ),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _approval_model(run_id, "/tmp"))
        first = _restore_controller(ledger, world)
        second = _restore_controller(ledger, world)
        self.assertEqual(list(first._sessions), [run_id])
        self.assertEqual(list(second._sessions), [run_id])
        self.assertEqual(
            first._sessions[run_id].controller_state,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL,
        )
        self.assertEqual(
            second._sessions[run_id].controller_state,
            LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL,
        )
        self.assertEqual(world.calls_for(run_id), [])


class ConversationClaimResumeTests(unittest.TestCase):
    def test_needs_review_resume_fails_closed_when_later_run_owns_chat(self) -> None:
        with _temporary_real_ledger(), TemporaryDirectory() as repo:
            run_a = ledger_module.create_run("A")
            run_b = ledger_module.create_run("B")
            ledger_module.bind_run_destination(run_a, "craxii", "Chat X")
            ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat X")
            ledger_module.update_run_status(run_a, RunStatus.NEEDS_REVIEW)
            reclaimed = ledger_module.claim_chatgpt_conversation(
                run_b, "craxii", "Chat X"
            )
            self.assertEqual(
                reclaimed.status, ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            ledger_module.add_event(
                run_a,
                "local_controller_action_failed",
                "Controller action paused after failure.",
                {
                    "schema_version": 1,
                    "action_key": "ask_send_to_gpt",
                    "source": "automatic_progress",
                    "reason_code": "chatgpt_handoff_yielded_retryable_ui_failure",
                    "error_message": "UI yielded.",
                    "retry_classification": "retryable",
                    "retryable": True,
                    "recovery_message": "Retry.",
                    "action_executed": False,
                    "run_status_before_action": "completed",
                    "run_status_after_action": "needs_review",
                    "source_event_ids": {"codex_exec_finished": 10},
                },
            )
            events = ledger_module.list_events(run_a)
            failure_id = max(
                event["id"]
                for event in events
                if event["event_type"] == "local_controller_action_failed"
            )
            world = SessionWorld()
            world.set_model(run_a, _send_model(run_a, repo))
            controller = LocalController(
                session=LocalControllerSession(
                    active_run_id=run_a,
                    controller_state=LOCAL_CONTROLLER_STATE_WAITING_FOR_RETRY,
                ),
                ledger=ledger_module,
                read_model_builder=world.read_model_builder,
                supervision_step=world.supervision_step,
                initial_run_executor=PerRunInitialExecutor(),
                max_active_sessions=4,
                chatgpt_wait_sleeper=lambda seconds: None,
                desktop_mutex=object(),
            )
            runtime = controller._sessions[run_a]
            runtime.project_title = "craxii"
            runtime.chat_title = "Chat X"
            result = controller.retry_failed_action(failure_id, run_a)
            self.assertFalse(result.ok)
            self.assertEqual(
                result.reason_code, CONVERSATION_CLAIM_CONFLICT_REASON_CODE
            )
            self.assertEqual(world.calls_for(run_a), [])

    def test_restore_reconstructs_from_conversation_claim_without_snapshot(self) -> None:
        with _temporary_real_ledger(), TemporaryDirectory() as repo:
            run_id = ledger_module.create_run("Task")
            ledger_module.bind_run_destination(run_id, "craxii", "Chat A")
            ledger_module.claim_chatgpt_conversation(run_id, "craxii", "Chat A")
            world = SessionWorld()
            world.set_model(run_id, _blocked_model(run_id, repo))
            controller = _restore_controller(ledger_module, world)
            self.assertIn(run_id, controller._sessions)
            self.assertEqual(
                controller._sessions[run_id].project_title, "craxii"
            )
            self.assertEqual(controller._sessions[run_id].chat_title, "Chat A")


class DurabilitySafetyTests(unittest.TestCase):
    def test_critical_write_failure_blocks_new_codex_and_chatgpt_globally(self) -> None:
        class UnhealthyLedger(MultiRunFakeLedger):
            def check_durable_write_health(self) -> bool:
                return False

        ledger = UnhealthyLedger()
        world = SessionWorld()
        with TemporaryDirectory() as repo:
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            guard = LedgerDurabilityGuard()
            guard.record_critical_failure("add_event", "disk full")
            wrapped = DurabilityGuardedLedger(ledger, guard)
            controller = LocalController(
                session=LocalControllerSession(),
                ledger=wrapped,
                read_model_builder=world.read_model_builder,
                supervision_step=world.supervision_step,
                initial_run_executor=PerRunInitialExecutor(),
                max_active_sessions=4,
                chatgpt_wait_sleeper=lambda seconds: None,
                desktop_mutex=object(),
            )
            started = controller.start_run(
                repository_path=repo,
                initial_instruction="Task",
                project_title="craxii",
                chat_title="Chat A",
                sandbox="read-only",
            )
            self.assertFalse(started.ok)
            self.assertEqual(started.reason_code, LEDGER_DURABILITY_BLOCKED_REASON_CODE)
            self.assertEqual(controller._sessions, {})

    def test_durability_block_pauses_healthy_siblings_then_recovers(self) -> None:
        class RecoveringLedger(MultiRunFakeLedger):
            def __init__(self) -> None:
                super().__init__()
                self.health = False

            def check_durable_write_health(self) -> bool:
                return self.health

        ledger = RecoveringLedger()
        run_a, run_b = _seed_runs(ledger, 2)
        ledger.controller_snapshot = {
            "active_run_id": run_a,
            "sessions": {
                run_a: _payload(state="running_routine_action", chat="Chat A"),
                run_b: _payload(state="running_routine_action", chat="Chat B"),
            },
        }
        world = SessionWorld()
        world.set_model(run_a, _send_model(run_a, "/tmp"))
        world.set_model(run_b, _send_model(run_b, "/tmp"))
        world.queue_step(
            run_a, _success_step_result(), next_model=_completed_model(run_a, "/tmp")
        )
        world.queue_step(
            run_b, _success_step_result(), next_model=_completed_model(run_b, "/tmp")
        )
        guard = LedgerDurabilityGuard()
        guard.record_critical_failure("add_event", "disk full")
        wrapped = DurabilityGuardedLedger(ledger, guard)
        sleeps: list[float] = []

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                ledger.health = True

        controller = LocalController(
            ledger=wrapped,
            read_model_builder=world.read_model_builder,
            supervision_step=world.supervision_step,
            initial_run_executor=PerRunInitialExecutor(),
            max_active_sessions=4,
            chatgpt_wait_sleeper=sleeper,
            desktop_mutex=object(),
        )
        self.assertTrue(_wait_until(lambda: len(sleeps) >= 2))
        _join_workers(controller)
        self.assertFalse(controller.durability.is_blocked)
        self.assertEqual(len(world.calls_for(run_a)), 1)
        self.assertEqual(len(world.calls_for(run_b)), 1)
        self.assertGreaterEqual(min(sleeps), 0.5)

    def test_progress_and_snapshot_failures_are_noncritical(self) -> None:
        class TelemetryLedger(MultiRunFakeLedger):
            def add_codex_progress_event(self, *args, **kwargs):
                del args, kwargs
                raise sqlite3.Error("progress boom")

            def save_local_controller_snapshot(self, snapshot):
                del snapshot
                raise sqlite3.Error("snapshot boom")

        ledger = TelemetryLedger()
        controller = LocalController(
            session=LocalControllerSession(),
            ledger=ledger,
            read_model_builder=SessionWorld().read_model_builder,
            supervision_step=SessionWorld().supervision_step,
            initial_run_executor=PerRunInitialExecutor(),
            desktop_mutex=object(),
        )
        with self.assertRaises(sqlite3.Error):
            controller.ledger.add_codex_progress_event("run-1", "inv", {})
        self.assertFalse(controller.durability.is_blocked)
        self.assertGreaterEqual(
            controller.durability.status()["noncritical_failure_count"], 1
        )
        with self.assertRaises(sqlite3.Error):
            controller.ledger.save_local_controller_snapshot({"active_run_id": None})
        self.assertFalse(controller.durability.is_blocked)
        self.assertGreaterEqual(
            controller.durability.status()["noncritical_failure_count"], 2
        )

    def test_already_running_work_is_not_replayed_while_blocked(self) -> None:
        ledger = MultiRunFakeLedger()
        world = SessionWorld()
        executor = PerRunInitialExecutor()
        record = executor.configure("run-1", blocking=True)
        world.set_model("run-1", _completed_model("run-1", "/tmp"))
        controller = LocalController(
            session=LocalControllerSession(),
            ledger=ledger,
            read_model_builder=world.read_model_builder,
            supervision_step=world.supervision_step,
            initial_run_executor=executor,
            max_active_sessions=4,
            chatgpt_wait_sleeper=lambda seconds: None,
            desktop_mutex=object(),
        )
        with TemporaryDirectory() as repo:
            started = controller.start_run(
                repository_path=repo,
                initial_instruction="Task",
                project_title="craxii",
                chat_title="Chat A",
                sandbox="read-only",
            )
            self.assertTrue(started.ok)
            self.assertTrue(record.entered.wait(WAIT_TIMEOUT_SECONDS))
            controller.durability.record_critical_failure("add_event", "disk full")
            self.assertEqual(executor.calls, ["run-1"])
            record.release.set()
            _join_workers(controller)
            self.assertEqual(executor.calls, ["run-1"])


class LegacySnapshotRestoreTests(unittest.TestCase):
    def test_old_snapshot_without_sessions_still_restores_one_runtime(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "controller_state": "starting_initial_codex",
            "pending_approval": None,
        }
        world = SessionWorld()
        world.set_model(run_id, _blocked_model(run_id, "/tmp"))
        controller = _restore_controller(ledger, world)
        self.assertEqual(controller.session.active_run_id, run_id)
        self.assertIn(run_id, controller._sessions)


class LedgerCandidateDiscoveryTests(unittest.TestCase):
    def test_corrupt_snapshot_with_healthy_ledger_still_discovers_sessions(self) -> None:
        class CorruptSnapshotLedger(MultiRunFakeLedger):
            def load_local_controller_snapshot(self):
                raise sqlite3.Error("snapshot corrupt")

        ledger = CorruptSnapshotLedger()
        run_id = ledger.create_run("Task")
        ledger.set_run_status(run_id, RunStatus.COMPLETED.value)
        world = SessionWorld()
        world.set_model(run_id, _send_model(run_id, "/tmp"))
        world.queue_step(
            run_id, _success_step_result(), next_model=_completed_model(run_id, "/tmp")
        )
        controller = _restore_controller(ledger, world)
        self.assertIn(run_id, controller._sessions)
        _join_workers(controller)
        self.assertEqual(len(world.calls_for(run_id)), 1)
        self.assertFalse(controller.durability.is_blocked)

    def test_empty_snapshot_discovers_mid_loop_completed_run(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.set_run_status(run_id, RunStatus.COMPLETED.value)
        world = SessionWorld()
        world.set_model(run_id, _send_model(run_id, "/tmp"))
        world.queue_step(
            run_id, _success_step_result(), next_model=_completed_model(run_id, "/tmp")
        )
        controller = _restore_controller(ledger, world)
        self.assertIn(run_id, controller._sessions)
        _join_workers(controller)
        self.assertEqual(len(world.calls_for(run_id)), 1)

    def test_missing_snapshot_restores_waiting_approval(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.set_run_status(run_id, RunStatus.WAITING_FOR_APPROVAL.value)
        world = SessionWorld()
        world.set_model(run_id, _approval_model(run_id, "/tmp"))
        controller = _restore_controller(ledger, world)
        runtime = controller._sessions[run_id]
        self.assertEqual(
            runtime.controller_state, LOCAL_CONTROLLER_STATE_WAITING_FOR_APPROVAL
        )
        self.assertIsNotNone(runtime.pending_approval)
        self.assertEqual(world.calls_for(run_id), [])

    def test_missing_snapshot_restores_retryable_run(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        world = SessionWorld()
        world.set_model(run_id, _retry_model(run_id, "/tmp", routine=True))
        world.queue_step(
            run_id, _success_step_result(), next_model=_completed_model(run_id, "/tmp")
        )
        sleeps: list[float] = []
        controller = _restore_controller(
            ledger, world, sleeper=lambda seconds: sleeps.append(seconds)
        )
        _join_workers(controller)
        self.assertEqual(len(world.calls_for(run_id)), 1)
        self.assertGreaterEqual(min(sleeps), 0.5)

    def test_historical_finished_and_cancelled_runs_are_not_resurrected(self) -> None:
        ledger = MultiRunFakeLedger()
        finished = ledger.create_run("Finished")
        cancelled = ledger.create_run("Cancelled")
        live = ledger.create_run("Live")
        ledger.set_run_status(cancelled, RunStatus.REJECTED.value)
        world = SessionWorld()
        world.set_model(finished, _completed_model(finished, "/tmp"))
        world.set_model(cancelled, _send_model(cancelled, "/tmp"))
        world.set_model(live, _send_model(live, "/tmp"))
        world.queue_step(
            live, _success_step_result(), next_model=_completed_model(live, "/tmp")
        )
        controller = _restore_controller(ledger, world)
        _join_workers(controller)
        self.assertNotIn(finished, controller._sessions)
        self.assertNotIn(cancelled, controller._sessions)
        self.assertIn(live, controller._sessions)
        self.assertEqual(world.calls_for(finished), [])
        self.assertEqual(world.calls_for(cancelled), [])
        self.assertEqual(len(world.calls_for(live)), 1)

    def test_ledger_candidate_enumeration_failure_fails_closed(self) -> None:
        class UnreadableRunsLedger(MultiRunFakeLedger):
            def list_restore_candidate_runs(self) -> list[dict[str, str]]:
                raise sqlite3.Error("runs unreadable")

            def check_durable_write_health(self) -> bool:
                return False

        ledger = UnreadableRunsLedger()
        run_id = ledger.create_run("Task")
        world = SessionWorld()
        world.set_model(run_id, _send_model(run_id, "/tmp"))
        controller = _restore_controller(ledger, world)
        self.assertTrue(controller.durability.is_blocked)
        self.assertEqual(controller._sessions, {})
        self.assertEqual(world.calls_for(run_id), [])
        with TemporaryDirectory() as repo:
            started = controller.start_run(
                repository_path=repo,
                initial_instruction="Task",
                project_title="craxii",
                chat_title="Chat A",
                sandbox="read-only",
            )
        self.assertFalse(started.ok)
        self.assertEqual(started.reason_code, LEDGER_DURABILITY_BLOCKED_REASON_CODE)

    def test_repeated_auto_retry_restore_is_idempotent(self) -> None:
        ledger = MultiRunFakeLedger()
        run_id = ledger.create_run("Task")
        ledger.controller_snapshot = {
            "active_run_id": run_id,
            "sessions": {
                run_id: _payload(state="waiting_for_retry", chat="Chat A"),
            },
        }
        world = SessionWorld()
        world.set_model(run_id, _retry_model(run_id, "/tmp", routine=True))
        world.queue_step(
            run_id, _success_step_result(), next_model=_completed_model(run_id, "/tmp")
        )
        first = _restore_controller(ledger, world)
        _join_workers(first)
        self.assertEqual(len(world.calls_for(run_id)), 1)
        second = _restore_controller(ledger, world)
        _join_workers(second)
        self.assertEqual(len(world.calls_for(run_id)), 1)
        self.assertNotIn(run_id, second._sessions)


if __name__ == "__main__":
    unittest.main()
