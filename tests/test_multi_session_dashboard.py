"""Stage 7: multi-session management API and dashboard contracts.

Proves the operator read/control surface over the Stage 1–6 engine:
session list, per-run reads, focus-only switching, isolated stop/approve,
capacity, durability visibility, and ChatGPT queue attribution.

Deterministic fakes only. No live ChatGPT.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent import ledger as ledger_module
from agent.local_controller import (
    CONVERSATION_CLAIM_CONFLICT_REASON_CODE,
    session_operator_view,
)
from agent.local_server import LOCAL_SERVER_TOKEN_HEADER, LocalControllerServer
from tests.local_socket_test_support import LocalSocketBindingTestCase, requires_localhost_ephemeral_bind
from tests.test_multi_session_concurrency import (
    WAIT_TIMEOUT_SECONDS,
    PerRunInitialExecutor,
    SessionWorld,
    _approval_model,
    _completed_model,
    _join_workers,
    _make_controller,
    _start,
    _success_step_result,
    _temporary_real_ledger,
    _wait_until,
)


def _http_json(port: int, method: str, path: str, token: str, body: dict | None = None):
    headers = {
        "Host": f"127.0.0.1:{port}",
        LOCAL_SERVER_TOKEN_HEADER: token,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
    finally:
        conn.close()
    payload = json.loads(raw.decode("utf-8")) if raw else None
    return response.status, payload


class SessionOperatorViewTests(unittest.TestCase):
    def test_chatgpt_wait_is_normal_wait_not_error(self) -> None:
        view = session_operator_view(
            controller_state="running_routine_action",
            runtime={"action_running": True, "waiting_for_chatgpt": True},
            read_model=None,
            waiting_for_chatgpt=True,
            owns_lease=False,
            queue_is_head=False,
            queue_status="pending",
        )
        self.assertEqual(view["operator_status"], "waiting_for_chatgpt")
        self.assertEqual(view["operator_status_label"], "Waiting for ChatGPT")
        self.assertEqual(view["operator_tone"], "wait")
        self.assertFalse(view["needs_user_action"])

    def test_lease_owner_is_using_chatgpt(self) -> None:
        view = session_operator_view(
            controller_state="running_routine_action",
            runtime={"action_running": True},
            read_model=None,
            waiting_for_chatgpt=True,
            owns_lease=True,
            queue_is_head=True,
            queue_status="claimed",
        )
        self.assertEqual(view["operator_status"], "using_chatgpt")
        self.assertEqual(view["operator_tone"], "ok")
        self.assertFalse(view["needs_user_action"])

    def test_operator_cancel_beats_working_and_chatgpt_lease(self) -> None:
        view = session_operator_view(
            controller_state="running_routine_action",
            runtime={
                "action_running": True,
                "cancel_requested": True,
                "automatic_burst_reason": "operator_cancelled",
            },
            read_model=None,
            waiting_for_chatgpt=True,
            owns_lease=True,
            queue_is_head=True,
            queue_status="claimed",
        )
        self.assertEqual(view["operator_status"], "stopped")
        self.assertEqual(view["operator_status_label"], "Stopped")
        self.assertEqual(view["operator_tone"], "error")
        self.assertFalse(view["needs_user_action"])

    def test_conversation_claim_conflict_is_distinct_attention(self) -> None:
        model = replace(
            _approval_model("run-x"),
            requires_human_approval=False,
            latest_failure={
                "reason_code": CONVERSATION_CLAIM_CONFLICT_REASON_CODE,
                "retry_classification": "reconcile",
                "retryable": False,
                "error_message": "Another session owns this chat.",
            },
        )
        view = session_operator_view(
            controller_state="blocked",
            runtime={},
            read_model=model,
            waiting_for_chatgpt=False,
            owns_lease=False,
            queue_is_head=False,
            queue_status=None,
        )
        self.assertEqual(view["operator_status"], "conversation_claim_conflict")
        self.assertEqual(view["operator_tone"], "attention")
        self.assertTrue(view["needs_user_action"])

    def test_reconcile_classification_is_not_ordinary_wait(self) -> None:
        model = replace(
            _approval_model("run-x"),
            requires_human_approval=False,
            latest_failure={
                "reason_code": "chatgpt_submission_uncertain",
                "retry_classification": "reconcile",
                "retryable": False,
                "error_message": "Submission outcome is uncertain.",
            },
        )
        view = session_operator_view(
            controller_state="waiting_for_retry",
            runtime={},
            read_model=model,
            waiting_for_chatgpt=False,
            owns_lease=False,
            queue_is_head=False,
            queue_status=None,
        )
        self.assertEqual(view["operator_status"], "reconciliation_needed")
        self.assertEqual(view["operator_tone"], "attention")
        self.assertTrue(view["needs_user_action"])


class SessionListAndFocusTests(unittest.TestCase):
    def test_list_sessions_returns_independent_cards_and_focus(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)

            first = _start(controller, repo, chat="Chat A", additional=False)
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            listed = controller.list_sessions()
            self.assertTrue(listed.ok)
            sessions = listed.metadata["sessions"]
            ids = [item["run_id"] for item in sessions]
            self.assertEqual(ids, [first.run_id, second.run_id])
            by_id = {item["run_id"]: item for item in sessions}
            self.assertTrue(by_id[second.run_id]["focused"])
            self.assertFalse(by_id[first.run_id]["focused"])
            self.assertEqual(listed.metadata["max_active_sessions"], 4)
            self.assertEqual(listed.metadata["live_session_count"], 2)
            self.assertEqual(listed.metadata["session_capacity_remaining"], 2)
            self.assertEqual(by_id[first.run_id]["chat_title"], "Chat A")
            self.assertEqual(by_id[second.run_id]["chat_title"], "Chat B")
            self.assertEqual(
                Path(by_id[first.run_id]["repository_path"]).resolve(),
                Path(repo).resolve(),
            )
            self.assertTrue(by_id[first.run_id]["codex_running"] or by_id[first.run_id]["action_running"])
            self.assertTrue(by_id[second.run_id]["codex_running"] or by_id[second.run_id]["action_running"])

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

    def test_focus_switch_does_not_alter_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)
            first = _start(controller, repo, chat="Chat A", additional=False)
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            before_a = {
                "state": controller._sessions[first.run_id].controller_state,
                "running": controller._sessions[first.run_id].action_running,
                "kind": controller._sessions[first.run_id].current_action_kind,
            }
            focused = controller.focus_run(first.run_id)
            self.assertTrue(focused.ok)
            self.assertEqual(focused.run_id, first.run_id)
            self.assertEqual(controller.session.active_run_id, first.run_id)
            self.assertEqual(controller._sessions[first.run_id].controller_state, before_a["state"])
            self.assertEqual(controller._sessions[first.run_id].action_running, before_a["running"])
            self.assertTrue(controller._sessions[second.run_id].action_running)
            self.assertEqual(
                controller._sessions[second.run_id].controller_state,
                "starting_initial_codex",
            )
            self.assertIs(controller._sessions[second.run_id].cancel_requested.is_set(), False)

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

    def test_unknown_run_reads_and_focus_fail_closed(self) -> None:
        controller = _make_controller(SessionWorld(), max_sessions=2)
        missing = controller.get_run_state("missing-run")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.reason_code, "run_not_found")
        focused = controller.focus_run("missing-run")
        self.assertFalse(focused.ok)
        self.assertEqual(focused.reason_code, "run_not_found")
        progress = controller.get_run_progress("missing-run")
        self.assertFalse(progress.ok)
        self.assertEqual(progress.reason_code, "run_not_found")

    def test_stop_a_does_not_stop_b(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)
            first = _start(controller, repo, chat="Chat A", additional=False)
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            cancelled = controller.request_cancel(first.run_id)
            self.assertTrue(cancelled.ok)
            self.assertEqual(cancelled.run_id, first.run_id)
            self.assertTrue(controller._sessions[second.run_id].action_running)
            self.assertFalse(controller._sessions[second.run_id].cancel_requested.is_set())

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

    def test_stop_means_complete_death_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)
            first = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertTrue(controller._sessions[first.run_id].action_running)

            cancelled = controller.request_cancel(first.run_id)
            self.assertTrue(cancelled.ok)
            runtime = controller._sessions[first.run_id]
            self.assertTrue(runtime.cancel_requested.is_set())
            self.assertFalse(runtime.action_running)
            self.assertFalse(runtime.waiting_for_chatgpt)
            self.assertEqual(runtime.controller_state, "blocked")
            listed = controller.list_sessions()
            session = listed.metadata["sessions"][0]
            self.assertEqual(session["run_id"], first.run_id)
            self.assertEqual(session["operator_status"], "stopped")
            self.assertFalse(session["live"])
            self.assertFalse(session["action_running"])
            self.assertEqual(listed.metadata["live_session_count"], 0)

            record_a.release.set()
            _join_workers(controller)

    def test_approve_and_reject_target_exact_run(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _approval_model("run-1", repo))
            world.set_model("run-2", _approval_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=2)
            first = _start(controller, repo, chat="Chat A", additional=False)
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(
                    lambda: controller._sessions[first.run_id].pending_approval is not None
                    and controller._sessions[second.run_id].pending_approval is not None
                    and not controller._sessions[first.run_id].action_running
                    and not controller._sessions[second.run_id].action_running
                )
            )
            snapshot_b = controller._sessions[second.run_id].pending_approval
            world.queue_step(
                first.run_id,
                _success_step_result("ask_send_to_gpt"),
                next_model=_completed_model(first.run_id, repo),
            )
            approved = controller.submit_approval_decision("approved", first.run_id)
            self.assertTrue(approved.ok)
            self.assertEqual(approved.run_id, first.run_id)
            self.assertTrue(
                _wait_until(lambda: controller._sessions[first.run_id].controller_state == "completed")
            )
            self.assertEqual(controller._sessions[second.run_id].controller_state, "waiting_for_approval")
            self.assertIs(controller._sessions[second.run_id].pending_approval, snapshot_b)

            rejected = controller.submit_approval_decision("rejected", second.run_id)
            self.assertTrue(rejected.ok)
            self.assertEqual(rejected.run_id, second.run_id)

    def test_durability_block_is_global_on_the_list_payload(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            controller = _make_controller(world, max_sessions=2)
            started = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(started.ok)
            _join_workers(controller)
            controller.durability.record_critical_failure("add_event", "disk full")
            listed = controller.list_sessions()
            durability = listed.metadata["ledger_durability"]
            self.assertTrue(durability["blocked"])
            self.assertEqual(listed.metadata["sessions"][0]["run_id"], started.run_id)
            self.assertNotEqual(
                listed.metadata["sessions"][0]["operator_status"],
                "failed",
            )


class ChatGPTQueueAttributionTests(unittest.TestCase):
    def test_queue_snapshot_is_fifo_and_attributed_to_runs(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("A")
            run_b = ledger_module.create_run("B")
            ledger_module.enqueue_chatgpt_handoff(run_a, enqueue_source="ready")
            ledger_module.enqueue_chatgpt_handoff(run_b, enqueue_source="ready")
            claimed = ledger_module.claim_chatgpt_handoff_for_run(
                run_a, claim_owner_identifier="owner-a"
            )
            self.assertEqual(claimed.status, ledger_module.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            snapshot = ledger_module.describe_chatgpt_handoff_queue()
            self.assertTrue(snapshot["ok"])
            self.assertEqual(snapshot["head_run_id"], run_a)
            self.assertEqual(snapshot["entries"][0]["run_id"], run_a)
            self.assertEqual(snapshot["entries"][0]["position"], 1)
            self.assertTrue(snapshot["entries"][0]["is_head"])
            self.assertEqual(snapshot["entries"][1]["run_id"], run_b)
            self.assertEqual(snapshot["entries"][1]["position"], 2)
            waiting = ledger_module.claim_chatgpt_handoff_for_run(
                run_b, claim_owner_identifier="owner-b"
            )
            self.assertEqual(waiting.status, ledger_module.AtomicChatGPTHandoffQueueStatus.WAITING)
            after = ledger_module.describe_chatgpt_handoff_queue()
            self.assertEqual(after["entries"][1]["run_id"], run_b)


@requires_localhost_ephemeral_bind
class MultiSessionHttpIntegrationTests(LocalSocketBindingTestCase):
    def test_http_list_focus_and_per_run_controls(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _approval_model("run-1", repo))
            world.set_model("run-2", _approval_model("run-2", repo))
            controller = _make_controller(world, executor=executor, max_sessions=4)
            first = _start(controller, repo, chat="Chat A", additional=False)
            second = _start(controller, repo, chat="Chat B")
            self.assertTrue(
                _wait_until(
                    lambda: controller._sessions[first.run_id].pending_approval is not None
                    and controller._sessions[second.run_id].pending_approval is not None
                    and not controller._sessions[first.run_id].action_running
                )
            )
            server = LocalControllerServer(controller=controller, port=0)
            server.start()
            try:
                token = controller.session.token
                status, payload = _http_json(server.port, "GET", "/api/runs", token)
                self.assertEqual(status, 200)
                ids = [item["run_id"] for item in payload["sessions"]]
                self.assertEqual(set(ids), {first.run_id, second.run_id})
                self.assertEqual(payload["focused_run_id"], second.run_id)
                self.assertEqual(payload["max_active_sessions"], 4)

                status, payload = _http_json(
                    server.port, "POST", f"/api/runs/{first.run_id}/focus", token, {}
                )
                self.assertEqual(status, 200)
                self.assertEqual(controller.session.active_run_id, first.run_id)
                self.assertEqual(
                    controller._sessions[second.run_id].controller_state,
                    "waiting_for_approval",
                )

                status, payload = _http_json(
                    server.port, "GET", f"/api/runs/{second.run_id}", token
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["run_id"], second.run_id)
                self.assertEqual(payload["state"]["run_id"], second.run_id)

                snapshot_b = controller._sessions[second.run_id].pending_approval
                status, payload = _http_json(
                    server.port,
                    "POST",
                    f"/api/runs/{first.run_id}/approval",
                    token,
                    {"decision": "rejected"},
                )
                self.assertEqual(status, 202)
                self.assertIs(controller._sessions[second.run_id].pending_approval, snapshot_b)

                status, payload = _http_json(
                    server.port, "GET", "/api/runs/missing-id", token
                )
                self.assertEqual(status, 404)
                self.assertEqual(payload["reason_code"], "run_not_found")
            finally:
                server.shutdown()
                _join_workers(controller)
