"""Stage 5: multi-session start semantics and identity rules.

Proves the explicit additional-session start contract, the atomic ledger
conversation claim, start failure atomicity, and the forced-navigation rule
for multi-live mode. Deterministic fakes only; the real ledger is exercised
against a temporary database. No live desktop automation.

Coverage map (Stage 5 acceptance):
- legacy/default start stays conservative: any live session rejects it
- explicit additional starts work up to the configured cap; over-cap starts
  return session_capacity_reached; rejected starts launch no worker/Codex
- same project + different chats allowed; same repo allowed; same live
  conversation rejected
- racing starts (in-process, cross-controller on one ledger database, and
  raw ledger claims) have exactly one winner
- failed starts release the durable conversation claim and leave no runtime
- multi-live mode forces destination navigation and the forced mode latches
  per run, surviving sibling shutdown and controller restart
"""

from __future__ import annotations

import dataclasses
import threading
import time
import unittest
from tempfile import TemporaryDirectory

from agent import ledger as ledger_module
from agent.local_controller import (
    LOCAL_CONTROLLER_RUN_START_FAILED_EVENT_TYPE,
    LocalController,
    LocalControllerSession,
    start_local_controller_run,
    validate_local_controller_start_request,
)
from agent.run_state import RunStatus
from tests.test_multi_session_concurrency import (
    WAIT_TIMEOUT_SECONDS,
    MultiRunFakeLedger,
    PerRunInitialExecutor,
    SessionWorld,
    _blocked_model,
    _completed_model,
    _join_workers,
    _make_controller,
    _routine_model,
    _start,
    _success_step_result,
    _temporary_real_ledger,
    _wait_until,
)


# ---------------------------------------------------------------------------
# Default vs explicit additional start contract
# ---------------------------------------------------------------------------


class DefaultVersusAdditionalStartTests(unittest.TestCase):
    def test_legacy_start_alone_still_works(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            controller = _make_controller(world, max_sessions=4)

            result = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(result.ok)
            self.assertEqual(result.reason_code, "started")
            _join_workers(controller)

    def test_legacy_start_with_live_session_returns_active_run_exists(self) -> None:
        """A stale client or double submit must never silently create B."""
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)

            first = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(first.ok)
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))

            # Even with three free slots the legacy body is rejected.
            second = _start(controller, repo, chat="Chat B", additional=False)
            self.assertFalse(second.ok)
            self.assertEqual(second.reason_code, "active_run_exists")
            self.assertEqual(second.run_id, first.run_id)
            self.assertEqual(len(controller._sessions), 1)
            self.assertEqual(executor.calls, ["run-1"])
            self.assertTrue(controller._sessions[first.run_id].action_running)

            record_a.release.set()
            _join_workers(controller)

    def test_repeated_legacy_submits_create_exactly_one_session(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            executor.configure("run-1", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)

            barrier = threading.Barrier(2)
            results: list = [None, None]

            def submit(index: int) -> None:
                barrier.wait(WAIT_TIMEOUT_SECONDS)
                results[index] = _start(
                    controller, repo, chat="Chat A", additional=False
                )

            threads = [
                threading.Thread(target=submit, args=(index,)) for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT_TIMEOUT_SECONDS)

            oks = [result for result in results if result.ok]
            rejects = [result for result in results if not result.ok]
            self.assertEqual(len(oks), 1)
            self.assertEqual(len(rejects), 1)
            self.assertEqual(rejects[0].reason_code, "active_run_exists")
            self.assertEqual(len(controller._sessions), 1)

            executor.records["run-1"].release.set()
            _join_workers(controller)

    def test_explicit_additional_start_creates_b_while_a_remains_alive(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)

            first = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            worker_a = controller._sessions[first.run_id].current_worker

            second = _start(controller, repo, chat="Chat B", additional=True)
            self.assertTrue(second.ok)
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            # A is untouched: same worker, still running, not cancelled.
            runtime_a = controller._sessions[first.run_id]
            self.assertIs(runtime_a.current_worker, worker_a)
            self.assertTrue(worker_a.is_alive())
            self.assertTrue(runtime_a.action_running)
            self.assertFalse(runtime_a.cancel_requested.is_set())
            # Focus moves to the new run; that is UI/default-route state only.
            self.assertEqual(controller.session.active_run_id, second.run_id)

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

    def test_additional_starts_fill_configured_cap_then_capacity_rejected(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            records = [
                executor.configure(f"run-{index}", blocking=True)
                for index in range(1, 4)
            ]
            controller = _make_controller(world, executor=executor, max_sessions=3)

            first = _start(controller, repo, chat="Chat 1", additional=False)
            self.assertTrue(first.ok)
            for index in (2, 3):
                result = _start(controller, repo, chat=f"Chat {index}", additional=True)
                self.assertTrue(result.ok)
            self.assertEqual(len(controller._sessions), 3)

            fourth = _start(controller, repo, chat="Chat 4", additional=True)
            self.assertFalse(fourth.ok)
            self.assertEqual(fourth.reason_code, "session_capacity_reached")
            # The rejected start launched nothing.
            self.assertEqual(len(controller._sessions), 3)
            self.assertEqual(executor.calls, ["run-1", "run-2", "run-3"])

            for record in records:
                record.release.set()
            _join_workers(controller)

    def test_same_project_and_same_repo_allowed_same_conversation_rejected(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)

            first = _start(
                controller, repo, chat="Dev Internal App", project="craxii", additional=False
            )
            second = _start(
                controller, repo, chat="Other Chat", project="craxii", additional=True
            )
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)

            duplicate = _start(
                controller, repo, chat="Dev Internal App", project="craxii", additional=True
            )
            self.assertFalse(duplicate.ok)
            self.assertEqual(duplicate.reason_code, "duplicate_chatgpt_conversation")
            self.assertEqual(duplicate.run_id, first.run_id)
            self.assertEqual(len(controller._sessions), 2)
            self.assertEqual(executor.calls, ["run-1", "run-2"])

            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

    def test_racing_additional_starts_for_same_chat_have_one_winner(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            executor.configure("run-1", blocking=True)
            executor.configure("run-2", blocking=True)
            controller = _make_controller(world, executor=executor, max_sessions=4)

            barrier = threading.Barrier(2)
            results: list = [None, None]

            def submit(index: int) -> None:
                barrier.wait(WAIT_TIMEOUT_SECONDS)
                results[index] = _start(
                    controller, repo, chat="Race Chat", additional=True
                )

            threads = [
                threading.Thread(target=submit, args=(index,)) for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT_TIMEOUT_SECONDS)

            oks = [result for result in results if result.ok]
            losers = [result for result in results if not result.ok]
            self.assertEqual(len(oks), 1)
            self.assertEqual(len(losers), 1)
            self.assertEqual(losers[0].reason_code, "duplicate_chatgpt_conversation")
            self.assertEqual(len(controller._sessions), 1)
            self.assertEqual(len(executor.calls), 1)
            for record in executor.records.values():
                record.release.set()
            _join_workers(controller)

    def test_starting_c_and_d_keeps_focus_mirrors_consistent(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            records = [
                executor.configure(f"run-{index}", blocking=True)
                for index in range(1, 5)
            ]
            controller = _make_controller(world, executor=executor, max_sessions=4)

            results = [_start(controller, repo, chat="Chat 1", additional=False)]
            for index in (2, 3, 4):
                results.append(
                    _start(controller, repo, chat=f"Chat {index}", additional=True)
                )
            for result in results:
                self.assertTrue(result.ok)

            # Focus follows the newest start; every earlier session's runtime
            # is untouched and the focused mirror matches the focused runtime.
            self.assertEqual(controller.session.active_run_id, results[-1].run_id)
            focused_runtime = controller._sessions[results[-1].run_id]
            self.assertIs(controller.cancel_requested, focused_runtime.cancel_requested)
            for earlier in results[:-1]:
                runtime = controller._sessions[earlier.run_id]
                self.assertTrue(runtime.action_running)
                self.assertFalse(runtime.cancel_requested.is_set())

            for record in records:
                record.release.set()
            _join_workers(controller)


# ---------------------------------------------------------------------------
# Atomic ledger conversation claim
# ---------------------------------------------------------------------------


class ConversationClaimLedgerTests(unittest.TestCase):
    def test_claim_then_duplicate_for_live_owner(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            run_b = ledger_module.create_run("Task B")
            first = ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat A")
            self.assertEqual(
                first.status, ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            second = ledger_module.claim_chatgpt_conversation(run_b, "craxii", "Chat A")
            self.assertEqual(
                second.status, ledger_module.AtomicConversationClaimStatus.DUPLICATE
            )
            self.assertEqual(second.owning_run_id, run_a)
            self.assertEqual(second.reason_code, "duplicate_chatgpt_conversation")

    def test_claim_is_idempotent_for_the_same_run(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat A")
            repeat = ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat A")
            self.assertEqual(
                repeat.status, ledger_module.AtomicConversationClaimStatus.IDEMPOTENT
            )

    def test_different_chats_and_projects_claim_independently(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            run_b = ledger_module.create_run("Task B")
            run_c = ledger_module.create_run("Task C")
            self.assertEqual(
                ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat A").status,
                ledger_module.AtomicConversationClaimStatus.CLAIMED,
            )
            # Same project, different chat.
            self.assertEqual(
                ledger_module.claim_chatgpt_conversation(run_b, "craxii", "Chat B").status,
                ledger_module.AtomicConversationClaimStatus.CLAIMED,
            )
            # Same chat title, different project.
            self.assertEqual(
                ledger_module.claim_chatgpt_conversation(run_c, "Other", "Chat A").status,
                ledger_module.AtomicConversationClaimStatus.CLAIMED,
            )

    def test_concurrent_claims_for_same_conversation_have_one_winner(self) -> None:
        with _temporary_real_ledger():
            run_ids = [ledger_module.create_run(f"Task {index}") for index in range(4)]
            barrier = threading.Barrier(len(run_ids))
            results: list = [None] * len(run_ids)

            def claim(index: int) -> None:
                barrier.wait(WAIT_TIMEOUT_SECONDS)
                results[index] = ledger_module.claim_chatgpt_conversation(
                    run_ids[index], "craxii", "Race Chat"
                )

            threads = [
                threading.Thread(target=claim, args=(index,))
                for index in range(len(run_ids))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT_TIMEOUT_SECONDS)

            statuses = [result.status for result in results]
            self.assertEqual(
                statuses.count(ledger_module.AtomicConversationClaimStatus.CLAIMED), 1
            )
            self.assertEqual(
                statuses.count(ledger_module.AtomicConversationClaimStatus.DUPLICATE),
                len(run_ids) - 1,
            )
            winner_run_id = next(
                result.run_id
                for result in results
                if result.status == ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            for result in results:
                if result.status == ledger_module.AtomicConversationClaimStatus.DUPLICATE:
                    self.assertEqual(result.owning_run_id, winner_run_id)

    def test_claim_reclaims_when_owning_run_is_replaceable(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            run_b = ledger_module.create_run("Task B")
            ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat A")
            ledger_module.update_run_status(run_a, RunStatus.NEEDS_REVIEW)
            result = ledger_module.claim_chatgpt_conversation(run_b, "craxii", "Chat A")
            self.assertEqual(
                result.status, ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            self.assertEqual(result.reclaimed_from_run_id, run_a)
            self.assertEqual(
                result.reclaim_reason_code,
                ledger_module.CONVERSATION_CLAIM_OWNER_RUN_REPLACEABLE_REASON_CODE,
            )

    def test_completed_mid_loop_status_does_not_give_up_the_claim(self) -> None:
        """Run status `completed` is mid-loop; a live owner keeps the chat."""
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            run_b = ledger_module.create_run("Task B")
            claimed = ledger_module.claim_chatgpt_conversation(
                run_a, "craxii", "Chat A"
            )
            self.assertEqual(
                claimed.status, ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            ledger_module.update_run_status(run_a, RunStatus.COMPLETED)
            result = ledger_module.claim_chatgpt_conversation(
                run_b, "craxii", "Chat A"
            )
            self.assertEqual(
                result.status, ledger_module.AtomicConversationClaimStatus.DUPLICATE
            )
            self.assertEqual(result.owning_run_id, run_a)

    def test_claim_reclaims_when_owner_process_is_provably_dead(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            run_b = ledger_module.create_run("Task B")
            # Fabricate a claim recorded by a process instance that no longer
            # exists: an impossible pid fails pid_is_alive, which is provable
            # death. The owning run stays non-replaceable to isolate the path.
            ledger_module.add_event(
                run_a,
                ledger_module.CHATGPT_CONVERSATION_CLAIMED_EVENT_TYPE,
                ledger_module.CHATGPT_CONVERSATION_CLAIMED_MESSAGE,
                {
                    "schema_version": ledger_module.CHATGPT_CONVERSATION_CLAIM_SCHEMA_VERSION,
                    "project_title": "craxii",
                    "chat_title": "Chat A",
                    "owner_identity": {
                        "pid": 4194000,
                        "boot_id": "boot-dead",
                        "process_start_identity": "start-dead",
                    },
                },
            )
            result = ledger_module.claim_chatgpt_conversation(run_b, "craxii", "Chat A")
            self.assertEqual(
                result.status, ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            self.assertEqual(result.reclaimed_from_run_id, run_a)
            self.assertEqual(
                result.reclaim_reason_code,
                ledger_module.CONVERSATION_CLAIM_OWNER_PROCESS_DEAD_REASON_CODE,
            )

    def test_release_frees_the_conversation_and_is_idempotent(self) -> None:
        with _temporary_real_ledger():
            run_a = ledger_module.create_run("Task A")
            run_b = ledger_module.create_run("Task B")
            ledger_module.claim_chatgpt_conversation(run_a, "craxii", "Chat A")
            released = ledger_module.release_chatgpt_conversation_claim(
                run_a, reason="test_release"
            )
            self.assertEqual(
                released.status, ledger_module.AtomicConversationClaimStatus.RELEASED
            )
            self.assertEqual(released.released_identities, (("craxii", "Chat A"),))
            again = ledger_module.release_chatgpt_conversation_claim(run_a)
            self.assertEqual(
                again.status,
                ledger_module.AtomicConversationClaimStatus.IDEMPOTENT_RELEASE,
            )
            reclaim = ledger_module.claim_chatgpt_conversation(run_b, "craxii", "Chat A")
            self.assertEqual(
                reclaim.status, ledger_module.AtomicConversationClaimStatus.CLAIMED
            )
            self.assertIsNone(reclaim.reclaimed_from_run_id)


# ---------------------------------------------------------------------------
# Cross-controller enforcement on one ledger database
# ---------------------------------------------------------------------------


class CrossControllerSameChatTests(unittest.TestCase):
    """Two LocalController instances sharing one ledger database model two
    CRAX processes sharing one worktree. Separate worktrees use separate
    ledger databases and are outside this claim's reach (documented)."""

    def test_second_controller_cannot_start_a_live_conversation(self) -> None:
        with TemporaryDirectory() as repo, _temporary_real_ledger():
            world_a = SessionWorld()
            world_a.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor_a = PerRunInitialExecutor()
            controller_a = _make_controller(
                world_a, ledger=ledger_module, executor=executor_a, max_sessions=4
            )
            world_b = SessionWorld()
            world_b.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor_b = PerRunInitialExecutor()
            controller_b = _make_controller(
                world_b, ledger=ledger_module, executor=executor_b, max_sessions=4
            )

            first = _start(controller_a, repo, chat="Shared Chat", additional=False)
            self.assertTrue(first.ok)

            # Controller B has no in-process knowledge of A's session, so this
            # exercises the durable ledger claim, not the registry check.
            duplicate = _start(controller_b, repo, chat="Shared Chat", additional=False)
            self.assertFalse(duplicate.ok)
            self.assertEqual(duplicate.reason_code, "duplicate_chatgpt_conversation")
            self.assertEqual(len(controller_b._sessions), 0)
            self.assertEqual(executor_b.calls, [])

            # A different conversation still starts from controller B.
            other = _start(controller_b, repo, chat="Other Chat", additional=False)
            self.assertTrue(other.ok)
            _join_workers(controller_a)
            _join_workers(controller_b)

    def test_racing_starts_across_controllers_have_one_winner(self) -> None:
        with TemporaryDirectory() as repo, _temporary_real_ledger():
            controllers = []
            executors = []
            for _ in range(2):
                world = SessionWorld()
                world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
                executor = PerRunInitialExecutor(block_unconfigured=True)
                executors.append(executor)
                controllers.append(
                    _make_controller(
                        world, ledger=ledger_module, executor=executor, max_sessions=4
                    )
                )

            barrier = threading.Barrier(2)
            results: list = [None, None]

            def submit(index: int) -> None:
                barrier.wait(WAIT_TIMEOUT_SECONDS)
                results[index] = _start(
                    controllers[index], repo, chat="Race Chat", additional=False
                )

            threads = [
                threading.Thread(target=submit, args=(index,)) for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT_TIMEOUT_SECONDS)

            oks = [result for result in results if result.ok]
            losers = [result for result in results if not result.ok]
            self.assertEqual(len(oks), 1)
            self.assertEqual(len(losers), 1)
            self.assertEqual(losers[0].reason_code, "duplicate_chatgpt_conversation")
            self.assertEqual(
                sum(len(controller._sessions) for controller in controllers), 1
            )
            for executor in executors:
                executor.release_all()
            for controller in controllers:
                _join_workers(controller)
            # Exactly one Codex executor ran: the winner's. The loser never
            # created a worker, so after joining there can be no late call.
            self.assertTrue(
                _wait_until(
                    lambda: sum(len(executor.calls) for executor in executors) == 1
                )
            )
            self.assertEqual(sum(len(executor.calls) for executor in executors), 1)

    def test_cancelled_conversation_can_be_restarted_by_another_controller(self) -> None:
        with TemporaryDirectory() as repo, _temporary_real_ledger():
            world_a = SessionWorld()
            world_a.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor_a = PerRunInitialExecutor()
            controller_a = _make_controller(
                world_a, ledger=ledger_module, executor=executor_a, max_sessions=4
            )
            world_b = SessionWorld()
            world_b.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            controller_b = _make_controller(
                world_b, ledger=ledger_module, executor=PerRunInitialExecutor(), max_sessions=4
            )

            first = _start(controller_a, repo, chat="Shared Chat", additional=False)
            self.assertTrue(first.ok)
            _join_workers(controller_a)
            cancel = controller_a.request_cancel(first.run_id)
            self.assertTrue(cancel.ok)

            restart = _start(controller_b, repo, chat="Shared Chat", additional=False)
            self.assertTrue(restart.ok)
            _join_workers(controller_b)


# ---------------------------------------------------------------------------
# Start failure atomicity
# ---------------------------------------------------------------------------


class _ClaimRecordingLedger(MultiRunFakeLedger):
    """Fake ledger that also implements the durable conversation claim."""

    def __init__(self) -> None:
        super().__init__()
        self.claim_calls: list[tuple[str, str, str]] = []
        self.release_calls: list[tuple[str, str | None]] = []
        self.claim_status = ledger_module.AtomicConversationClaimStatus.CLAIMED
        self.fail_destination_binding = False

    def claim_chatgpt_conversation(
        self,
        run_id: str,
        project_title: str,
        chat_title: str,
        *,
        controller_instance_id: str | None = None,
    ):
        self.claim_calls.append((run_id, project_title, chat_title))
        return ledger_module.AtomicConversationClaimResult(
            status=self.claim_status,
            run_id=run_id,
            project_title=project_title,
            chat_title=chat_title,
            owning_run_id="run-owner" if self.claim_status
            == ledger_module.AtomicConversationClaimStatus.DUPLICATE else run_id,
            reason_code=(
                "duplicate_chatgpt_conversation"
                if self.claim_status
                == ledger_module.AtomicConversationClaimStatus.DUPLICATE
                else None
            ),
            error_message=(
                "A live session already owns this ChatGPT project and chat."
                if self.claim_status
                == ledger_module.AtomicConversationClaimStatus.DUPLICATE
                else None
            ),
        )

    def release_chatgpt_conversation_claim(self, run_id: str, *, reason: str | None = None):
        self.release_calls.append((run_id, reason))
        return ledger_module.AtomicConversationClaimResult(
            status=ledger_module.AtomicConversationClaimStatus.RELEASED,
            run_id=run_id,
        )

    def bind_run_destination(self, run_id: str, project_title: str, chat_title: str):
        if self.fail_destination_binding:
            return ledger_module.AtomicDestinationBindingResult(
                status=ledger_module.AtomicDestinationBindingStatus.OPERATIONAL_FAILURE,
                run_id=run_id,
                reason_code="destination_binding_transaction_failed",
                error_message="Injected binding failure.",
            )
        return super().bind_run_destination(run_id, project_title, chat_title)


class StartFailureAtomicityTests(unittest.TestCase):
    def _validated_request(self, repo: str):
        return validate_local_controller_start_request(
            repo,
            "Task",
            "read-only",
            project_title="craxii",
            chat_title="Chat A",
        )

    def test_duplicate_claim_fails_start_before_binding_and_launches_nothing(self) -> None:
        with TemporaryDirectory() as repo:
            ledger = _ClaimRecordingLedger()
            ledger.claim_status = ledger_module.AtomicConversationClaimStatus.DUPLICATE
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            controller = _make_controller(
                world, ledger=ledger, executor=executor, max_sessions=4
            )

            result = _start(controller, repo, chat="Chat A", additional=False)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_code, "duplicate_chatgpt_conversation")
            self.assertEqual(len(ledger.claim_calls), 1)
            # The losing run never binds, never registers, never runs Codex.
            self.assertEqual(controller._sessions, {})
            self.assertIsNone(controller.session.active_run_id)
            self.assertEqual(executor.calls, [])
            binding_events = [
                event
                for event in ledger.added_events
                if event["event_type"] == "run_destination_bound"
            ]
            self.assertEqual(binding_events, [])
            failure_events = [
                event
                for event in ledger.added_events
                if event["event_type"] == LOCAL_CONTROLLER_RUN_START_FAILED_EVENT_TYPE
            ]
            self.assertEqual(len(failure_events), 1)

    def test_failed_binding_after_claim_releases_the_claim(self) -> None:
        with TemporaryDirectory() as repo:
            ledger = _ClaimRecordingLedger()
            ledger.fail_destination_binding = True
            request = self._validated_request(repo)
            self.assertTrue(request.ok)

            result = start_local_controller_run(
                LocalControllerSession(),
                request,
                ledger=ledger,
                controller_instance_id="test-controller",
            )
            self.assertFalse(result.ok)
            self.assertEqual(len(ledger.claim_calls), 1)
            self.assertEqual(
                ledger.release_calls,
                [(result.run_id, "run_start_failed")],
            )

    def test_failed_start_via_controller_leaves_no_live_session_state(self) -> None:
        with TemporaryDirectory() as repo:
            ledger = _ClaimRecordingLedger()
            ledger.fail_destination_binding = True
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            controller = _make_controller(
                world, ledger=ledger, executor=executor, max_sessions=4
            )

            result = _start(controller, repo, chat="Chat A", additional=False)
            self.assertFalse(result.ok)
            self.assertEqual(controller._sessions, {})
            self.assertIsNone(controller.session.active_run_id)
            self.assertEqual(executor.calls, [])
            self.assertEqual(len(ledger.release_calls), 1)

            # The slot and the conversation are both still available.
            ledger.fail_destination_binding = False
            retry = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(retry.ok)
            _join_workers(controller)

    def test_cancel_releases_the_conversation_claim(self) -> None:
        with TemporaryDirectory() as repo:
            ledger = _ClaimRecordingLedger()
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            controller = _make_controller(
                world, ledger=ledger, executor=PerRunInitialExecutor(), max_sessions=4
            )
            result = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(result.ok)
            _join_workers(controller)

            cancel = controller.request_cancel(result.run_id)
            self.assertTrue(cancel.ok)
            self.assertIn(
                (result.run_id, "run_failed"),
                ledger.release_calls,
            )


# ---------------------------------------------------------------------------
# Forced navigation in multi-live mode
# ---------------------------------------------------------------------------


class ForcedNavigationTests(unittest.TestCase):
    def test_single_live_session_keeps_configured_navigation(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            world.set_model("run-1", _routine_model("run-1", repo))
            world.queue_step(
                "run-1",
                _success_step_result(),
                next_model=_completed_model("run-1", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=4)

            result = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(result.ok)
            self.assertTrue(
                _wait_until(lambda: len(world.calls_for("run-1")) >= 1)
            )
            _join_workers(controller)
            self.assertIs(
                world.calls_for("run-1")[0]["allow_destination_navigation"], False
            )

    def test_multi_live_forces_navigation_and_latch_survives_sibling_stop(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            world.set_model("run-1", _routine_model("run-1", repo))
            world.set_model("run-2", _completed_model("run-2", repo))

            step1_entered = threading.Event()
            step1_gate = threading.Event()
            step2_entered = threading.Event()
            step2_gate = threading.Event()
            world.queue_step(
                "run-1",
                _success_step_result(),
                next_model=_routine_model("run-1", repo),
                entered=step1_entered,
                gate=step1_gate,
            )
            world.queue_step(
                "run-1",
                _success_step_result(),
                next_model=_completed_model("run-1", repo),
                entered=step2_entered,
                gate=step2_gate,
            )
            controller = _make_controller(world, executor=executor, max_sessions=4)

            # A starts alone with navigation disabled; B starts explicitly.
            first = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(record_a.entered.wait(WAIT_TIMEOUT_SECONDS))
            second = _start(controller, repo, chat="Chat B", additional=True)
            self.assertTrue(second.ok)
            self.assertTrue(record_b.entered.wait(WAIT_TIMEOUT_SECONDS))

            # Both live runtimes are latched at start time.
            self.assertTrue(
                controller._sessions[first.run_id].navigation_forced_multi_live
            )
            self.assertTrue(
                controller._sessions[second.run_id].navigation_forced_multi_live
            )

            # A's first handoff while B is live is forced to navigate.
            record_a.release.set()
            self.assertTrue(step1_entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertIs(
                world.calls_for(first.run_id)[0]["allow_destination_navigation"],
                True,
            )

            # B stops (terminal in the ledger, worker finished); A is alone
            # again but remains safely forced for the rest of its run.
            record_b.release.set()
            controller.ledger.set_run_status(second.run_id, RunStatus.COMPLETED.value)
            self.assertTrue(
                _wait_until(
                    lambda: len(controller._live_session_ids_locked()) == 1
                )
            )
            step1_gate.set()
            self.assertTrue(step2_entered.wait(WAIT_TIMEOUT_SECONDS))
            self.assertIs(
                world.calls_for(first.run_id)[1]["allow_destination_navigation"],
                True,
            )
            step2_gate.set()
            _join_workers(controller)

            # The latch is persisted with the session snapshot.
            snapshot = controller.ledger.controller_snapshot
            self.assertIs(
                snapshot["sessions"][first.run_id]["navigation_forced_multi_live"],
                True,
            )

    def test_configured_navigation_on_stays_on_for_single_session(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            executor = PerRunInitialExecutor()
            model = dataclasses.replace(
                _routine_model("run-1", repo), allow_destination_navigation=True
            )
            world.set_model("run-1", model)
            world.queue_step(
                "run-1",
                _success_step_result(),
                next_model=_completed_model("run-1", repo),
            )
            controller = _make_controller(world, executor=executor, max_sessions=4)
            result = _start(controller, repo, chat="Chat A", additional=False)
            self.assertTrue(result.ok)
            self.assertTrue(_wait_until(lambda: len(world.calls_for("run-1")) >= 1))
            _join_workers(controller)
            self.assertIs(
                world.calls_for("run-1")[0]["allow_destination_navigation"], True
            )

    def test_forced_navigation_latch_restores_after_restart(self) -> None:
        with TemporaryDirectory() as repo:
            world = SessionWorld()
            world.default_model_factory = lambda run_id: _completed_model(run_id, repo)
            executor = PerRunInitialExecutor()
            record_a = executor.configure("run-1", blocking=True)
            record_b = executor.configure("run-2", blocking=True)
            ledger = MultiRunFakeLedger()
            controller = _make_controller(
                world, ledger=ledger, executor=executor, max_sessions=4
            )
            first = _start(controller, repo, chat="Chat A", additional=False)
            second = _start(controller, repo, chat="Chat B", additional=True)
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            record_a.release.set()
            record_b.release.set()
            _join_workers(controller)

            world.set_model(first.run_id, _blocked_model(first.run_id, repo))
            world.set_model(second.run_id, _blocked_model(second.run_id, repo))
            restored = LocalController(
                ledger=ledger,
                read_model_builder=world.read_model_builder,
                supervision_step=world.supervision_step,
                initial_run_executor=executor,
                max_active_sessions=4,
                chatgpt_wait_sleeper=lambda seconds: time.sleep(0.001),
                desktop_mutex=object(),
            )
            runtime = restored._sessions.get(second.run_id)
            self.assertIsNotNone(runtime)
            self.assertTrue(runtime.navigation_forced_multi_live)
