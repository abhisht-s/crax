"""Finished responses release the shared UI; no live desktop access."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import chatgpt_ax_capture as ax, ledger, local_controller
from agent.chatgpt_desktop_mutex import ChatGPTDesktopMutex
from agent.chatgpt_services import capture_chatgpt_response_service
from agent.run_services import get_chatgpt_ui_lease, ChatGPTUILeaseLookupStatus
from agent.run_state import RunStatus
from agent.supervision_services import run_supervision_step
from tests.test_chatgpt_ax_capture import MARKER, SENTINEL_RESPONSE, _FakeClock
from tests.test_chatgpt_handoff_queue import _temporary_ledger
from tests.test_supervision_services import _capture_plan, _destination_adapter_factory


def candidate(index, path, text):
    return ax.TextCandidate(index, path, text, (path + ".1",))


ANCHOR = candidate(0, "FW.1", MARKER)
REPLY = candidate(1, "FW.2.1", "All done. There is no next task.")


def controls(scope="FW.2", *, stop=False):
    result = [
        {"path": scope + ".2.1", "labels": ["Copy"], "enabled": True},
        {"path": scope + ".2.2", "labels": ["Good response"], "enabled": True},
        {"path": scope + ".2.3", "labels": ["Bad response"], "enabled": True},
    ]
    if stop:
        result.append({"path": "FW.9", "labels": ["Stop generating"], "enabled": True})
    return result


def snapshot(reply=REPLY, *, buttons=None, complete=True, anchor=ANCHOR):
    candidates = [anchor, reply]
    return candidates, {
        "candidate_count": len(candidates),
        "response_controls": controls() if buttons is None else buttons,
        "response_controls_complete": complete,
    }


class Reader:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.calls = 0

    def collect_text_candidates(self):
        item = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def capture(snapshots):
    reader = Reader(snapshots)
    clock = _FakeClock(step=0.25)
    with (
        mock.patch.object(ax, "_AXReader", return_value=reader),
        mock.patch.object(ax.time, "monotonic", clock.monotonic),
        mock.patch.object(ax.time, "sleep", clock.sleep),
    ):
        result = ax.capture_response_after_feedback(
            "", require_sentinel_response=True, submission_marker_text=MARKER,
            stable_seconds=0.5, poll_interval_seconds=0,
            # Regression against the old indefinite loop fails deterministically.
            should_stop=lambda: reader.calls >= 20,
        )
    return result, reader.calls


class ResponseCompletionTests(unittest.TestCase):
    def test_finished_response_without_markers_returns_attention_result(self):
        result, calls = capture([snapshot()])
        self.assertEqual(result["reason_code"], ax.COMPLETED_WITHOUT_PROMPT_REASON_CODE)
        self.assertFalse(result["ok"])
        self.assertTrue(result["stable"])
        self.assertIsNone(result["timeout_seconds"])
        self.assertEqual(result["completion_evidence"]["response_text"], REPLY.text)
        self.assertLess(calls, 20)

    def test_finished_response_with_unclosed_prompt_returns_attention_result(self):
        reply = replace(REPLY, text="BEGIN_NEXT_CODEX_PROMPT\nUnfinished instructions")
        result, _ = capture([snapshot(reply)])
        self.assertEqual(result["reason_code"], ax.COMPLETED_WITHOUT_PROMPT_REASON_CODE)

    def test_no_completion_evidence_still_waits_indefinitely(self):
        cases = [
            snapshot(buttons=[]),
            snapshot(buttons=controls(stop=True)),
            snapshot(complete=False),
            snapshot(buttons=controls("FW.0")),  # Older reply's footer.
            snapshot(buttons=controls()[:1]),  # A code-block Copy button.
            snapshot(buttons=controls()[1:]),  # Ratings alone are insufficient.
            snapshot(buttons=[{**item, "enabled": None} for item in controls()]),
            snapshot(anchor=replace(ANCHOR, text="Different submission")),
            snapshot(buttons=[{**item, "path": item["path"].replace("FW.2", "FW.20")} for item in controls()]),
        ]
        for observation in cases:
            with self.subTest(observation=observation):
                result, calls = capture([observation])
                self.assertEqual(result["reason_code"], "operator_cancelled")
                self.assertEqual(calls, 20)

    def test_text_mentions_of_completion_controls_do_not_count(self):
        reply = replace(REPLY, text="Copy\nGood response\nBad response\nRead aloud")
        result, _ = capture([snapshot(reply, buttons=[])])
        self.assertEqual(result["reason_code"], "operator_cancelled")

    def test_streaming_partial_prompt_keeps_waiting_then_valid_prompt_succeeds(self):
        partial = replace(REPLY, text="BEGIN_NEXT_CODEX_PROMPT\nStill writing")
        valid = replace(REPLY, text=SENTINEL_RESPONSE)
        result, calls = capture([snapshot(partial, buttons=controls(stop=True))] * 8 + [snapshot(valid)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason_code"], "complete_sentinel_stable")
        self.assertEqual(result["response_text"], SENTINEL_RESPONSE)
        self.assertGreater(calls, 8)

    def test_changing_text_or_lost_completion_evidence_resets_confirmation(self):
        for interruption in [
            snapshot(replace(REPLY, text="More text arrived")),
            snapshot(buttons=controls(stop=True)),
            snapshot(complete=False),
            snapshot(anchor=replace(ANCHOR, text="Marker temporarily unavailable")),
            ax.AXCaptureError("Temporary accessibility failure"),
        ]:
            with self.subTest(interruption=interruption):
                valid = snapshot(replace(REPLY, text=SENTINEL_RESPONSE))
                result, _ = capture([snapshot(), interruption, valid])
                self.assertTrue(result["ok"])

    def test_cancel_before_observation_still_wins(self):
        with mock.patch.object(ax, "_AXReader") as reader:
            result = ax.capture_response_after_feedback("", should_stop=lambda: True)
        self.assertEqual(result["reason_code"], "operator_cancelled")
        reader.return_value.collect_text_candidates.assert_not_called()


class CompletionReaderTests(unittest.TestCase):
    def reader(self, *, max_depth=18, max_nodes=1200):
        # Exercise the production traversal without loading macOS frameworks.
        reader = ax._AXReader.__new__(ax._AXReader)
        reader.max_depth, reader.max_nodes = max_depth, max_nodes
        reader._get_pid = lambda: 123
        reader._ax = SimpleNamespace(AXUIElementCreateApplication=lambda pid: "app")
        attributes = {
            "app": {"AXFocusedWindow": "window"},
            "window": {"AXRole": "AXWindow"},
            "message": {"AXRole": "AXGroup", "AXSubrole": "AXHostingView"},
            "text": {"AXRole": "AXStaticText", "AXValue": "A response"},
            "copy": {"AXRole": "AXButton", "AXDescription": "Copy", "AXEnabled": True},
            "good": {"AXRole": "AXButton", "AXDescription": "Good response", "AXEnabled": True},
            "bad": {"AXRole": "AXButton", "AXDescription": "Bad response", "AXEnabled": True},
            "stop": {"AXRole": "AXButton", "AXDescription": "Stop generating", "AXEnabled": True},
        }
        children = {"window": ["message", "stop"], "message": ["text", "copy", "good", "bad"]}
        reader._copy_attribute = lambda element, name: attributes[element].get(name)
        reader._cf_string = lambda value: value if isinstance(value, str) else ""
        reader._cf_bool = lambda value: value if isinstance(value, bool) else None
        reader._children = lambda element: children.get(element, [])
        return reader

    def test_collects_real_button_metadata_separately_from_response_text(self):
        candidates, stats = self.reader().collect_text_candidates()
        self.assertTrue(stats["response_controls_complete"])
        self.assertEqual([item.text for item in candidates], ["A response"])
        self.assertEqual(len(stats["response_controls"]), 4)
        self.assertEqual(stats["response_controls"][-1]["labels"][1], "Stop generating")

    def test_truncated_tree_cannot_prove_completion(self):
        for limits in [{"max_depth": 1}, {"max_nodes": 4}]:
            with self.subTest(limits=limits):
                _, stats = self.reader(**limits).collect_text_candidates()
                self.assertFalse(stats["response_controls_complete"])


class CompletedResponseHandoffTests(unittest.TestCase):
    def test_failure_frees_real_queue_lease_and_mutex_for_other_session(self):
        with _temporary_ledger(), tempfile.TemporaryDirectory() as directory:
            run_a, run_b = ledger.create_run("A"), ledger.create_run("B")
            for run_id in (run_a, run_b):
                ledger.update_run_status(run_id, RunStatus.COMPLETED)
                ledger.add_event(run_id, "run_destination_bound", "Bound", {
                    "schema_version": ledger.RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                    "project_title": "Project Alpha", "chat_title": "Main Chat",
                })
            ledger.add_event(run_a, "gpt_feedback_submission_verified", "Submitted", {
                "reason_code": "chatgpt_submission_verified",
                "submission_marker_text": MARKER,
                "submission_marker_sha256": ax.hashlib.sha256(MARKER.encode()).hexdigest(),
            })
            ledger.enqueue_chatgpt_handoff(run_a, enqueue_source="test")
            ledger.enqueue_chatgpt_handoff(run_b, enqueue_source="test")
            mutex = ChatGPTDesktopMutex(Path(directory) / "desktop.lock")
            reader, clock = Reader([snapshot()]), _FakeClock()
            with (
                mock.patch.object(ax, "_AXReader", return_value=reader),
                mock.patch.object(ax.time, "monotonic", clock.monotonic),
                mock.patch.object(ax.time, "sleep", clock.sleep),
                mock.patch("agent.chatgpt_services.activate_chatgpt", side_effect=AssertionError("No live UI")),
            ):
                def capture_service(run_id, *args, **kwargs):
                    return capture_chatgpt_response_service(
                        run_id, ledger=ledger, require_sentinel_response=True,
                        activation_function=lambda app: {"is_frontmost": True},
                        capture_function=lambda *a, **kw: ax.capture_response_after_feedback(
                            *a, **{**kw, "poll_interval_seconds": 0, "should_stop": lambda: reader.calls >= 30},
                        ),
                    )

                result = run_supervision_step(
                    run_a, directory, "read-only", ledger=ledger,
                    planner=lambda *a, **kw: _capture_plan(),
                    desktop_mutex=mutex, capture_service=capture_service,
                    destination_adapter_factory=_destination_adapter_factory(),
                    submit_service=mock.Mock(side_effect=AssertionError("Must not resubmit")),
                )
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_code, ax.COMPLETED_WITHOUT_PROMPT_REASON_CODE)
            self.assertEqual(get_chatgpt_ui_lease(ledger=ledger).status, ChatGPTUILeaseLookupStatus.MISSING)
            failures = [event for event in ledger.list_events(run_a) if event["event_type"] == "gpt_response_capture_failed"]
            self.assertEqual(len(failures), 1)
            self.assertIn("completion_evidence", failures[0]["metadata_json"])
            self.assertEqual(ledger.describe_chatgpt_handoff_queue()["head_run_id"], run_b)
            claim = ledger.claim_chatgpt_handoff_for_run(run_b, claim_owner_identifier="test-b")
            self.assertEqual(claim.status, ledger.AtomicChatGPTHandoffQueueStatus.CLAIMED)
            acquired = ledger.acquire_chatgpt_ui_lease(run_b)
            self.assertEqual(acquired.status, ledger.AtomicChatGPTUILeaseStatus.ACQUIRED)
            hold = mutex.acquire(run_b)
            try:
                self.assertTrue(hold.ok)
            finally:
                hold.release()
                ledger.release_chatgpt_ui_lease(acquired.lease_token)
            ledger.complete_chatgpt_handoff(
                claim.queue_sequence, claim_owner_identifier="test-b", reason_code="test_resources_verified",
            )
            # The sibling can now complete a normal handoff through the same
            # production orchestration, without changing session A's result.
            sibling = run_supervision_step(
                run_b, directory, "read-only", ledger=ledger,
                planner=lambda *a, **kw: _capture_plan(), desktop_mutex=mutex,
                destination_adapter_factory=_destination_adapter_factory(),
                capture_service=lambda *a, **kw: SimpleNamespace(ok=True),
                submit_service=mock.Mock(side_effect=AssertionError("Must not resubmit")),
            )
            self.assertTrue(sibling.ok)
            self.assertEqual(sibling.next_state_hint, "extract_next_prompt")
            self.assertIsNone(ledger.describe_chatgpt_handoff_queue()["head_run_id"])

    def test_finished_invalid_response_requires_manual_attention_even_after_restart(self):
        for action in ("ask_send_to_gpt", "capture_gpt_response"):
            with self.subTest(action=action):
                classification = local_controller._failure_retry_classification(
                    action, SimpleNamespace(reason_code=ax.COMPLETED_WITHOUT_PROMPT_REASON_CODE), retry_context=None,
                )
                self.assertEqual(classification["classification"], "retry_after_fix")
                self.assertFalse(local_controller._failure_is_automatically_retryable({
                    "retry_classification": classification["classification"],
                    "retryable": classification["retryable"],
                    "reason_code": ax.COMPLETED_WITHOUT_PROMPT_REASON_CODE,
                }))


if __name__ == "__main__":
    unittest.main()
