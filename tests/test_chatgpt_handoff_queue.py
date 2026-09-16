from __future__ import annotations

import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import os

from agent import ledger
from agent.chatgpt_desktop_mutex import handoff_claim_owner_identifier
from agent.run_state import RunStatus


def _dead_claim_owner(run_id: str) -> str:
    # Boot mismatch proves the owner instance is gone; if the current boot id
    # is unknowable the differing start identity proves pid reuse instead.
    return (
        "controller:crashed|boot:kern.bootsessionuuid=some-previous-boot"
        f"|pid:{os.getpid()}|start:Thu Jan  1 00:00:00 1970|run:{run_id}"
    )


def _temporary_ledger():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "ledger.db"
    patcher = mock.patch.object(ledger, "DB_PATH", db_path)

    class _Context:
        def __enter__(self):
            patcher.__enter__()
            return db_path

        def __exit__(self, exc_type, exc, tb):
            patcher.__exit__(exc_type, exc, tb)
            tmpdir.cleanup()

    return _Context()


class ChatGPTHandoffQueueTests(unittest.TestCase):
    def test_fifo_claim_is_readiness_order_d_then_b_then_c(self) -> None:
        with _temporary_ledger():
            run_d = ledger.create_run("D")
            run_b = ledger.create_run("B")
            run_c = ledger.create_run("C")
            ledger.enqueue_chatgpt_handoff(run_d, enqueue_source="ready")
            ledger.enqueue_chatgpt_handoff(run_b, enqueue_source="ready")
            ledger.enqueue_chatgpt_handoff(run_c, enqueue_source="ready")

            claimed_d = ledger.claim_chatgpt_handoff_for_run(
                run_d, claim_owner_identifier="owner-d"
            )
            waiting_b = ledger.claim_chatgpt_handoff_for_run(
                run_b, claim_owner_identifier="owner-b"
            )
            waiting_c = ledger.claim_chatgpt_handoff_for_run(
                run_c, claim_owner_identifier="owner-c"
            )
            self.assertEqual(claimed_d.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            self.assertEqual(waiting_b.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertEqual(waiting_b.head_run_id, run_d)
            self.assertEqual(waiting_c.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertFalse(waiting_b.event_written)

            ledger.complete_chatgpt_handoff(
                claimed_d.queue_sequence,
                claim_owner_identifier="owner-d",
                reason_code="chatgpt_handoff_slice_completed",
            )
            claimed_b = ledger.claim_chatgpt_handoff_for_run(
                run_b, claim_owner_identifier="owner-b"
            )
            still_waiting_c = ledger.claim_chatgpt_handoff_for_run(
                run_c, claim_owner_identifier="owner-c"
            )
            self.assertEqual(claimed_b.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            self.assertEqual(still_waiting_c.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            snapshot = ledger.describe_chatgpt_handoff_queue()
            self.assertTrue(snapshot["ok"])
            self.assertEqual(snapshot["head_run_id"], run_b)
            self.assertEqual(
                [entry["run_id"] for entry in snapshot["entries"]],
                [run_b, run_c],
            )
            self.assertEqual(still_waiting_c.head_run_id, run_b)

            ledger.complete_chatgpt_handoff(
                claimed_b.queue_sequence,
                claim_owner_identifier="owner-b",
                reason_code="chatgpt_handoff_slice_completed",
            )
            claimed_c = ledger.claim_chatgpt_handoff_for_run(
                run_c, claim_owner_identifier="owner-c"
            )
            self.assertEqual(claimed_c.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            self.assertEqual(claimed_c.run_id, run_c)

    def test_immediate_rerequest_joins_the_tail(self) -> None:
        with _temporary_ledger():
            run_a = ledger.create_run("A")
            run_d = ledger.create_run("D")
            first = ledger.enqueue_chatgpt_handoff(run_a, enqueue_source="first")
            ledger.enqueue_chatgpt_handoff(run_d, enqueue_source="queued")
            claimed = ledger.claim_chatgpt_handoff_for_run(
                run_a, claim_owner_identifier="owner-a"
            )
            ledger.complete_chatgpt_handoff(
                claimed.queue_sequence,
                claim_owner_identifier="owner-a",
                reason_code="chatgpt_handoff_slice_completed",
            )
            rerequest = ledger.enqueue_chatgpt_handoff(run_a, enqueue_source="again")
            self.assertEqual(rerequest.status, ledger.AtomicChatGPTHandoffQueueStatus.ENQUEUED)
            self.assertGreater(rerequest.queue_sequence, first.queue_sequence)

            head = ledger.claim_chatgpt_handoff_for_run(
                run_d, claim_owner_identifier="owner-d"
            )
            waiting_a = ledger.claim_chatgpt_handoff_for_run(
                run_a, claim_owner_identifier="owner-a"
            )
            self.assertEqual(head.run_id, run_d)
            self.assertEqual(waiting_a.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertEqual(waiting_a.head_run_id, run_d)

    def test_failed_head_yields_so_the_next_queued_run_can_claim(self) -> None:
        with _temporary_ledger():
            run_head = ledger.create_run("head")
            run_next = ledger.create_run("next")
            ledger.enqueue_chatgpt_handoff(run_head, enqueue_source="send")
            ledger.enqueue_chatgpt_handoff(run_next, enqueue_source="send")
            claimed = ledger.claim_chatgpt_handoff_for_run(
                run_head, claim_owner_identifier="owner-head"
            )
            ledger.complete_chatgpt_handoff(
                claimed.queue_sequence,
                claim_owner_identifier="owner-head",
                reason_code="chatgpt_handoff_yielded_retryable_ui_failure",
            )
            claimed_next = ledger.claim_chatgpt_handoff_for_run(
                run_next, claim_owner_identifier="owner-next"
            )
            self.assertEqual(claimed_next.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            self.assertEqual(claimed_next.run_id, run_next)

            requeued = ledger.enqueue_chatgpt_handoff(run_head, enqueue_source="retry")
            waiting_head = ledger.claim_chatgpt_handoff_for_run(
                run_head, claim_owner_identifier="owner-head"
            )
            self.assertEqual(requeued.status, ledger.AtomicChatGPTHandoffQueueStatus.ENQUEUED)
            self.assertEqual(waiting_head.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertEqual(waiting_head.head_run_id, run_next)

    def test_claim_next_is_not_used_to_steal_another_run(self) -> None:
        with _temporary_ledger():
            run_d = ledger.create_run("D")
            run_b = ledger.create_run("B")
            ledger.enqueue_chatgpt_handoff(run_d, enqueue_source="ready")
            ledger.enqueue_chatgpt_handoff(run_b, enqueue_source="ready")
            stolen = ledger.claim_next_chatgpt_handoff(claim_owner_identifier="owner-b")
            scoped = ledger.claim_chatgpt_handoff_for_run(
                run_b, claim_owner_identifier="owner-b"
            )
            self.assertEqual(stolen.run_id, run_d)
            self.assertEqual(scoped.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertEqual(scoped.head_run_id, run_d)

    def test_one_active_entry_per_run_is_idempotent(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("one")
            first = ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="send")
            second = ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="send")
            self.assertEqual(first.status, ledger.AtomicChatGPTHandoffQueueStatus.ENQUEUED)
            self.assertEqual(second.status, ledger.AtomicChatGPTHandoffQueueStatus.IDEMPOTENT)
            self.assertEqual(second.queue_sequence, first.queue_sequence)
            self.assertFalse(second.event_written)

    def test_concurrent_queue_writes_do_not_raise_busy(self) -> None:
        with _temporary_ledger():
            run_ids = [ledger.create_run(f"run-{index}") for index in range(8)]
            barrier = threading.Barrier(8)

            def write(run_id: str):
                barrier.wait(timeout=5)
                enqueued = ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="ready")
                ledger.add_event(
                    run_id,
                    "test_event",
                    "concurrent write",
                    metadata={"run_id": run_id},
                )
                return enqueued

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = [
                    future.result(timeout=15)
                    for future in [executor.submit(write, run_id) for run_id in run_ids]
                ]

            self.assertTrue(
                all(
                    result.status == ledger.AtomicChatGPTHandoffQueueStatus.ENQUEUED
                    for result in results
                )
            )
            claimed = ledger.claim_next_chatgpt_handoff(claim_owner_identifier="owner")
            self.assertEqual(claimed.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)

    def test_simultaneous_claim_attempts_have_one_winner(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("contested")
            ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="ready")
            barrier = threading.Barrier(2)

            def race(owner: str):
                barrier.wait(timeout=5)
                return ledger.claim_chatgpt_handoff_for_run(
                    run_id, claim_owner_identifier=owner
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = [
                    future.result(timeout=15)
                    for future in [
                        executor.submit(race, "owner-one"),
                        executor.submit(race, "owner-two"),
                    ]
                ]

            statuses = sorted(str(result.status) for result in results)
            self.assertEqual(
                statuses,
                sorted(
                    [
                        str(ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED),
                        str(ledger.AtomicChatGPTHandoffQueueStatus.OWNER_MISMATCH),
                    ]
                ),
            )

    def test_repeated_fast_rerequest_cannot_starve_an_older_waiter(self) -> None:
        with _temporary_ledger():
            run_fast = ledger.create_run("fast")
            run_slow = ledger.create_run("slow")
            ledger.enqueue_chatgpt_handoff(run_fast, enqueue_source="ready")
            ledger.enqueue_chatgpt_handoff(run_slow, enqueue_source="ready")

            for _cycle in range(3):
                claimed = ledger.claim_chatgpt_handoff_for_run(
                    run_fast, claim_owner_identifier="owner-fast"
                )
                if claimed.status != ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED:
                    break
                ledger.complete_chatgpt_handoff(
                    claimed.queue_sequence,
                    claim_owner_identifier="owner-fast",
                    reason_code="chatgpt_handoff_slice_completed",
                )
                ledger.enqueue_chatgpt_handoff(run_fast, enqueue_source="again")

            slow_claim = ledger.claim_chatgpt_handoff_for_run(
                run_slow, claim_owner_identifier="owner-slow"
            )
            self.assertEqual(
                slow_claim.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED
            )
            fast_wait = ledger.claim_chatgpt_handoff_for_run(
                run_fast, claim_owner_identifier="owner-fast"
            )
            self.assertEqual(
                fast_wait.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING
            )
            self.assertEqual(fast_wait.head_run_id, run_slow)

    def test_dead_claim_owner_head_is_expired_so_followers_proceed(self) -> None:
        with _temporary_ledger():
            run_dead = ledger.create_run("crashed")
            run_next = ledger.create_run("next")
            ledger.enqueue_chatgpt_handoff(run_dead, enqueue_source="send")
            ledger.enqueue_chatgpt_handoff(run_next, enqueue_source="send")
            claimed = ledger.claim_chatgpt_handoff_for_run(
                run_dead, claim_owner_identifier=_dead_claim_owner(run_dead)
            )
            self.assertEqual(claimed.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)

            follower = ledger.claim_chatgpt_handoff_for_run(
                run_next, claim_owner_identifier="owner-next"
            )

            self.assertEqual(follower.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            self.assertEqual(follower.run_id, run_next)
            blocked_events = [
                event
                for event in ledger.list_events(run_dead)
                if event["event_type"] == ledger.CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE
            ]
            self.assertEqual(len(blocked_events), 1)
            self.assertIn(
                ledger.CHATGPT_HANDOFF_CLAIM_OWNER_DEAD_REASON_CODE,
                blocked_events[0]["metadata_json"],
            )

    def test_live_claim_owner_head_is_never_stolen(self) -> None:
        with _temporary_ledger():
            run_live = ledger.create_run("live")
            run_next = ledger.create_run("next")
            ledger.enqueue_chatgpt_handoff(run_live, enqueue_source="send")
            ledger.enqueue_chatgpt_handoff(run_next, enqueue_source="send")
            live_owner = handoff_claim_owner_identifier(run_live)
            claimed = ledger.claim_chatgpt_handoff_for_run(
                run_live, claim_owner_identifier=live_owner
            )
            self.assertEqual(claimed.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)

            follower = ledger.claim_chatgpt_handoff_for_run(
                run_next, claim_owner_identifier="owner-next"
            )

            self.assertEqual(follower.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertEqual(follower.head_run_id, run_live)

    def test_unparseable_claim_owner_fails_closed_as_not_dead(self) -> None:
        with _temporary_ledger():
            run_head = ledger.create_run("head")
            run_next = ledger.create_run("next")
            ledger.enqueue_chatgpt_handoff(run_head, enqueue_source="send")
            ledger.enqueue_chatgpt_handoff(run_next, enqueue_source="send")
            ledger.claim_chatgpt_handoff_for_run(
                run_head, claim_owner_identifier="owner-opaque"
            )

            follower = ledger.claim_chatgpt_handoff_for_run(
                run_next, claim_owner_identifier="owner-next"
            )

            self.assertEqual(follower.status, ledger.AtomicChatGPTHandoffQueueStatus.WAITING)
            self.assertEqual(follower.head_run_id, run_head)

    def test_crashed_run_self_heals_its_dead_claim_on_reenqueue(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("restarted")
            ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="send")
            claimed = ledger.claim_chatgpt_handoff_for_run(
                run_id, claim_owner_identifier=_dead_claim_owner(run_id)
            )
            self.assertEqual(claimed.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)

            reenqueued = ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="retry")
            self.assertEqual(
                reenqueued.status, ledger.AtomicChatGPTHandoffQueueStatus.ENQUEUED
            )
            reclaimed = ledger.claim_chatgpt_handoff_for_run(
                run_id, claim_owner_identifier="owner-after-restart"
            )
            self.assertEqual(
                reclaimed.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED
            )
            self.assertEqual(reclaimed.claim_owner_identifier, "owner-after-restart")

    def test_terminal_run_pending_head_is_expired_for_followers(self) -> None:
        with _temporary_ledger():
            run_cancelled = ledger.create_run("cancelled")
            run_next = ledger.create_run("next")
            ledger.enqueue_chatgpt_handoff(run_cancelled, enqueue_source="send")
            ledger.enqueue_chatgpt_handoff(run_next, enqueue_source="send")
            ledger.update_run_status(run_cancelled, RunStatus.REJECTED)

            follower = ledger.claim_chatgpt_handoff_for_run(
                run_next, claim_owner_identifier="owner-next"
            )

            self.assertEqual(follower.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            self.assertEqual(follower.run_id, run_next)
            blocked_events = [
                event
                for event in ledger.list_events(run_cancelled)
                if event["event_type"] == ledger.CHATGPT_HANDOFF_BLOCKED_EVENT_TYPE
            ]
            self.assertEqual(len(blocked_events), 1)
            self.assertIn(
                ledger.CHATGPT_HANDOFF_RUN_TERMINAL_REASON_CODE,
                blocked_events[0]["metadata_json"],
            )

    def test_completed_mid_loop_status_can_enqueue_a_handoff(self) -> None:
        """Governance `completed` is mid-loop, not queue-terminal."""
        with _temporary_ledger():
            run_id = ledger.create_run("mid-loop")
            ledger.update_run_status(run_id, RunStatus.COMPLETED)
            enqueue = ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="send")
            self.assertEqual(
                enqueue.status, ledger.AtomicChatGPTHandoffQueueStatus.ENQUEUED
            )
            claim = ledger.claim_chatgpt_handoff_for_run(
                run_id, claim_owner_identifier="owner-mid"
            )
            self.assertEqual(
                claim.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED
            )

    def test_terminal_run_cannot_enqueue_or_claim_a_handoff(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("finished")
            ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="send")
            ledger.update_run_status(run_id, RunStatus.FAILED)

            enqueue = ledger.enqueue_chatgpt_handoff(run_id, enqueue_source="late")
            claim = ledger.claim_chatgpt_handoff_for_run(
                run_id, claim_owner_identifier="owner-late"
            )

            self.assertEqual(
                enqueue.status, ledger.AtomicChatGPTHandoffQueueStatus.RUN_TERMINAL
            )
            self.assertEqual(
                claim.status, ledger.AtomicChatGPTHandoffQueueStatus.RUN_TERMINAL
            )
            # The refused claim also expired the leftover pending entry so it
            # cannot block followers.
            run_next = ledger.create_run("next")
            ledger.enqueue_chatgpt_handoff(run_next, enqueue_source="send")
            follower = ledger.claim_chatgpt_handoff_for_run(
                run_next, claim_owner_identifier="owner-next"
            )
            self.assertEqual(follower.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)

    def test_busy_timeout_is_configured_without_enabling_wal(self) -> None:
        self.assertEqual(ledger.SQLITE_BUSY_TIMEOUT_SECONDS, 10.0)
        with _temporary_ledger():
            connection = ledger._connect()
            try:
                busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
                journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            finally:
                connection.close()
            self.assertEqual(busy_timeout, 10000)
            self.assertNotEqual(journal_mode, "wal")
