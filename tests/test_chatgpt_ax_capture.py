from __future__ import annotations

import argparse
import io
import json
import unittest
from unittest import mock

from agent import chatgpt_ax_capture as ax
from agent import cli
from agent.supervise import SuperviseAction, SupervisePlan


FEEDBACK = (
    "Codex finished. Here is the output:\n"
    "SUPERVISE_SENTINEL_STEP_1_OK\n"
    "Run metadata: "
    + ("anchor " * 20)
)
MARKER = "AGENT_SUBMISSION\nrun_id=run-1\nnonce=n\npayload_sha256=p\nEND_AGENT_SUBMISSION"
SENTINEL_RESPONSE = (
    "BEGIN_NEXT_CODEX_PROMPT\n"
    "Say exactly: SUPERVISE_SENTINEL_STEP_2_OK\n"
    "END_NEXT_CODEX_PROMPT"
)


def _candidate(index: int, text: str) -> ax.TextCandidate:
    return ax.TextCandidate(
        index=index,
        path=f"FW.{index}",
        text=text,
        text_node_paths=(f"FW.{index}.1",),
    )


def _events_with_successful_submission() -> list[dict]:
    return [
        {
            "id": 1,
            "event_type": "gpt_feedback_submission_verified",
            "metadata_json": json.dumps(
                {
                    "reason_code": "chatgpt_submission_verified",
                    "submission_marker_text": MARKER,
                    "submission_marker_sha256": ax.hashlib.sha256(MARKER.encode("utf-8")).hexdigest(),
                    "message": FEEDBACK,
                },
                sort_keys=True,
            ),
        }
    ]


class _FakeAXReader:
    def __init__(self, snapshots: list[list[ax.TextCandidate]]) -> None:
        self._snapshots = snapshots
        self.calls = 0

    def collect_text_candidates(self) -> tuple[list[ax.TextCandidate], dict]:
        index = min(self.calls, len(self._snapshots) - 1)
        self.calls += 1
        candidates = self._snapshots[index]
        return candidates, {
            "candidate_count": len(candidates),
            "visited_nodes": len(candidates),
            "text_node_count": len(candidates),
            "max_depth": 18,
            "max_nodes": 1200,
        }


class _FakeClock:
    def __init__(self, step: float = 0.25) -> None:
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class _FakeLedger:
    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.added_events: list[tuple] = []

    def get_run(self, run_id: str) -> dict:
        return {"id": run_id, "status": "completed"}

    def list_events(self, run_id: str) -> list[dict]:
        return self._events

    def add_event(self, *args, **kwargs) -> None:
        self.added_events.append((args, kwargs))


class ChatGPTAXCaptureTests(unittest.TestCase):
    def test_sentinel_required_selects_sentinel_after_thinking(self) -> None:
        match = ax.find_response_candidate_after_marker(
            [
                _candidate(0, MARKER + "\n" + FEEDBACK),
                _candidate(1, "Thinking"),
                _candidate(2, SENTINEL_RESPONSE),
            ],
            MARKER,
            require_sentinel_response=True,
        )

        self.assertTrue(match["ok"])
        self.assertEqual(match["response_candidate"].text, SENTINEL_RESPONSE)
        self.assertEqual(match["response_candidate"].index, 2)
        self.assertEqual(match["sentinel_state"], "complete_sentinel_unstable")

    def test_missing_end_marker_is_provisional_streaming_state(self) -> None:
        partial = "BEGIN_NEXT_CODEX_PROMPT\nSay exactly: still streaming"
        match = ax.find_response_candidate_after_marker(
            [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, partial)],
            MARKER,
            require_sentinel_response=True,
        )

        self.assertFalse(match["ok"])
        self.assertFalse(match["fatal"])
        self.assertTrue(match["provisional"])
        self.assertEqual(match["sentinel_state"], "streaming_incomplete_sentinel")
        self.assertEqual(match["reason_code"], "streaming_incomplete_sentinel")

    def test_sentinel_required_waits_for_later_stable_sentinel(self) -> None:
        reader = _FakeAXReader(
            [
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, "Thinking")],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, "Thinking")],
                [
                    _candidate(0, MARKER + "\n" + FEEDBACK),
                    _candidate(1, "Thinking"),
                    _candidate(2, SENTINEL_RESPONSE),
                ],
                [
                    _candidate(0, MARKER + "\n" + FEEDBACK),
                    _candidate(1, "Thinking"),
                    _candidate(2, SENTINEL_RESPONSE),
                ],
            ]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=5.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["response_text"], SENTINEL_RESPONSE)
        self.assertEqual(result["response_candidate_index"], 2)
        self.assertGreaterEqual(reader.calls, 4)
        self.assertEqual(result["sentinel_state"], "complete_sentinel_stable")

    def test_partial_sentinel_then_complete_stable_succeeds(self) -> None:
        partial = "BEGIN_NEXT_CODEX_PROMPT\nSay exactly: still streaming"
        reader = _FakeAXReader(
            [
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, partial)],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, SENTINEL_RESPONSE)],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, SENTINEL_RESPONSE)],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, SENTINEL_RESPONSE)],
            ]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=5.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["response_text"], SENTINEL_RESPONSE)
        self.assertEqual(result["reason_code"], "complete_sentinel_stable")

    def test_sentinel_required_times_out_on_only_thinking(self) -> None:
        reader = _FakeAXReader(
            [
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, "Thinking")],
            ]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=1.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["matched_feedback"])
        self.assertEqual(result["matched_candidate_index"], 0)
        self.assertEqual(result["reason_code"], "sentinel_not_found_timeout")
        self.assertEqual(result["sentinel_state"], "sentinel_pending")
        self.assertEqual(result["post_feedback_candidate_summaries"][0]["sentinel_status"], "no_markers")
        self.assertEqual(result["post_feedback_candidate_summaries"][0]["text_preview_repr"], "'Thinking'")

    def test_incomplete_sentinel_times_out_with_specific_reason(self) -> None:
        partial = "BEGIN_NEXT_CODEX_PROMPT\nSay exactly: still streaming"
        reader = _FakeAXReader(
            [[_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, partial)]]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=1.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "sentinel_incomplete_timeout")
        self.assertEqual(result["sentinel_state"], "streaming_incomplete_sentinel")
        self.assertNotIn("response_text", result)

    def test_changing_complete_sentinel_captures_only_final_stable_text(self) -> None:
        first = SENTINEL_RESPONSE.replace("STEP_2", "EARLY")
        final = SENTINEL_RESPONSE.replace("STEP_2", "FINAL")
        reader = _FakeAXReader(
            [
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, first)],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, final)],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, final)],
                [_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, final)],
            ]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=5.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["response_text"], final)
        self.assertNotEqual(result["response_text"], first)

    def test_sentinel_required_rejects_two_complete_sentinel_candidates(self) -> None:
        match = ax.find_response_candidate_after_marker(
            [
                _candidate(0, MARKER + "\n" + FEEDBACK),
                _candidate(1, SENTINEL_RESPONSE),
                _candidate(2, SENTINEL_RESPONSE.replace("STEP_2", "STEP_3")),
            ],
            MARKER,
            require_sentinel_response=True,
        )

        self.assertFalse(match["ok"])
        self.assertIn("Multiple complete sentinel", match["error"])

    def test_sentinel_required_rejects_completed_malformed_markers_after_stable_observation(self) -> None:
        cases = [
            "END_NEXT_CODEX_PROMPT\nSay exactly: missing begin",
            "END_NEXT_CODEX_PROMPT\nBEGIN_NEXT_CODEX_PROMPT\nSay exactly: reversed",
            "BEGIN_NEXT_CODEX_PROMPT\n\nEND_NEXT_CODEX_PROMPT",
        ]

        for text in cases:
            with self.subTest(text=text):
                reader = _FakeAXReader(
                    [[_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, text)]]
                )
                clock = _FakeClock()
                with (
                    mock.patch.object(ax, "_AXReader", return_value=reader),
                    mock.patch.object(ax.time, "monotonic", clock.monotonic),
                    mock.patch.object(ax.time, "sleep", clock.sleep),
                ):
                    result = ax.capture_response_after_feedback(
                        FEEDBACK,
                        timeout_seconds=5.0,
                        stable_seconds=0.5,
                        poll_interval_seconds=0.0,
                        require_sentinel_response=True,
                        submission_marker_text=MARKER,
                    )
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason_code"], "sentinel_malformed_stable")
                self.assertEqual(result["sentinel_state"], "stable_malformed_sentinel")

    def test_two_complete_sentinel_responses_remain_fail_closed(self) -> None:
        match = ax.find_response_candidate_after_marker(
            [
                _candidate(0, MARKER + "\n" + FEEDBACK),
                _candidate(1, SENTINEL_RESPONSE),
                _candidate(2, SENTINEL_RESPONSE.replace("STEP_2", "STEP_3")),
            ],
            MARKER,
            require_sentinel_response=True,
        )

        self.assertFalse(match["ok"])
        self.assertTrue(match["fatal"])
        self.assertEqual(match["reason_code"], "multiple_complete_sentinels")
        self.assertEqual(match["sentinel_state"], "multiple_complete_sentinels")

    def test_status_and_chrome_candidates_do_not_cause_malformed_failure(self) -> None:
        reader = _FakeAXReader(
            [
                [
                    _candidate(0, MARKER + "\n" + FEEDBACK),
                    _candidate(1, "Thought for 4s"),
                    _candidate(2, "Search the web"),
                ]
            ]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=1.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "sentinel_not_found_timeout")
        self.assertEqual(result["sentinel_state"], "sentinel_pending")
        summaries = result["post_feedback_candidate_summaries"]
        self.assertEqual(summaries[0]["candidate_classification"], "ui_status")
        self.assertEqual(summaries[1]["candidate_classification"], "ui_chrome")
        self.assertTrue(
            all(summary["sentinel_status"] == "no_markers" for summary in summaries)
        )

    def test_complete_sentinel_not_stable_before_timeout_uses_stability_timeout(self) -> None:
        reader = _FakeAXReader(
            [[_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, SENTINEL_RESPONSE)]]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=1.0,
                stable_seconds=10.0,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "response_not_stable_timeout")
        self.assertEqual(result["sentinel_state"], "complete_sentinel_unstable")

    def test_anchor_pending_timeout_reports_sentinel_not_found(self) -> None:
        reader = _FakeAXReader([[_candidate(0, "Conversation without anchor")]])
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=1.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "sentinel_not_found_timeout")
        self.assertEqual(result["sentinel_state"], "anchor_pending")

    def test_multiple_markers_in_single_candidate_remain_malformed_after_stable_observation(self) -> None:
        text = (
            "BEGIN_NEXT_CODEX_PROMPT\nA\nEND_NEXT_CODEX_PROMPT\n"
            "BEGIN_NEXT_CODEX_PROMPT\nB\nEND_NEXT_CODEX_PROMPT"
        )
        reader = _FakeAXReader(
            [[_candidate(0, MARKER + "\n" + FEEDBACK), _candidate(1, text)]]
        )
        clock = _FakeClock()

        with (
            mock.patch.object(ax, "_AXReader", return_value=reader),
            mock.patch.object(ax.time, "monotonic", clock.monotonic),
            mock.patch.object(ax.time, "sleep", clock.sleep),
        ):
            result = ax.capture_response_after_feedback(
                FEEDBACK,
                timeout_seconds=5.0,
                stable_seconds=0.5,
                poll_interval_seconds=0.0,
                require_sentinel_response=True,
                submission_marker_text=MARKER,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "sentinel_malformed_stable")
        self.assertEqual(
            result["post_feedback_candidate_summaries"][0]["sentinel_status"],
            "multiple_sentinel_pairs",
        )

    def test_default_capture_selection_still_selects_first_following_text(self) -> None:
        match = ax.find_response_candidate_after_marker(
            [
                _candidate(0, MARKER + "\n" + FEEDBACK),
                _candidate(1, "Thinking"),
                _candidate(2, SENTINEL_RESPONSE),
            ],
            MARKER,
            require_sentinel_response=False,
        )

        self.assertTrue(match["ok"])
        self.assertEqual(match["response_candidate"].text, "Thinking")


class ChatGPTAXCLIWiringTests(unittest.TestCase):
    def _run_record(self) -> dict:
        return {"id": "run-1", "status": "completed"}

    def _base_events(self) -> list[dict]:
        return [
            {
                "id": 1,
                "event_type": "codex_exec_finished",
                "metadata_json": json.dumps(
                    {
                        "stdout": "raw stdout\n" * 120,
                        "stderr": "raw stderr\n",
                        "exit_code": 0,
                        "timed_out": False,
                    },
                    sort_keys=True,
                ),
            },
            {
                "id": 2,
                "event_type": "changed_file_classification",
                "metadata_json": json.dumps({"files": []}, sort_keys=True),
            },
            {
                "id": 3,
                "event_type": "prompt_repo_impact_diagnostics",
                "metadata_json": json.dumps({"flags": []}, sort_keys=True),
            },
            {
                "id": 4,
                "event_type": "supervision_decision",
                "metadata_json": json.dumps({"decision": "continue"}, sort_keys=True),
            },
        ]

    def _activation(self) -> dict:
        return {
            "activated": True,
            "frontmost_app": "ChatGPT",
            "is_frontmost": True,
            "error": None,
        }

    def _observation(
        self,
        marker: str,
        *,
        composer_text: str = "",
        candidates: list[str] | None = None,
        send_button: bool = False,
    ) -> dict:
        candidates = candidates or []
        return {
            "ok": True,
            "method": "fake_ax",
            "focused_element": {"path": "FW.1", "role": "AXTextArea", "focused": True, "text": composer_text},
            "focused_composer": {"path": "FW.1", "role": "AXTextArea", "focused": True, "text": composer_text},
            "text_input_candidates": [{"path": "FW.1", "role": "AXTextArea", "focused": True, "text": composer_text}],
            "button_candidates": [{"path": "FW.2", "role": "AXButton", "enabled": True}] if send_button else [],
            "send_button": {"path": "FW.2", "role": "AXButton", "enabled": True} if send_button else None,
            "message_candidates": [
                {"index": index, "path": f"FW.{index + 3}", "role": "AXStaticText", "text": text}
                for index, text in enumerate(candidates)
            ],
            "marker_text_present_in_composer": marker in composer_text,
            "marker_text_candidate_count": sum(1 for text in candidates if marker in text),
            "ax_stats": {"candidate_count": len(candidates)},
            "error": None,
        }

    def test_submit_flow_fails_before_send_when_paste_marker_not_visible(self) -> None:
        fake_ledger = _FakeLedger(self._base_events())
        stdout = io.StringIO()
        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "copy_to_clipboard", return_value={"copied": True, "method": "pbcopy", "error": None}),
            mock.patch.object(cli, "activate_chatgpt", return_value=self._activation()),
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app", return_value={"pasted": True, "method": "paste", "error": None}),
            mock.patch.object(cli, "press_enter_in_frontmost_app") as enter,
            mock.patch.object(cli, "inspect_chatgpt_submission_ui") as inspect,
            mock.patch.object(cli, "CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS", 0.0),
            mock.patch.object(cli.time, "sleep", return_value=None),
            mock.patch("sys.stdout", new=stdout),
        ):
            inspect.side_effect = lambda app_name, marker_text=None: self._observation(str(marker_text), composer_text="")
            ok = cli._submit_feedback_to_chatgpt_flow("run-1", self._run_record(), "ChatGPT", approval_mode="auto")

        self.assertFalse(ok)
        enter.assert_not_called()
        self.assertEqual(fake_ledger.added_events[-1][0][1], "gpt_feedback_submission_failed")
        self.assertIn("chatgpt_paste_not_visible", stdout.getvalue())

    def test_enter_input_sent_but_marker_remains_in_composer_is_not_verified(self) -> None:
        fake_ledger = _FakeLedger(self._base_events())
        marker_holder = {}

        def inspect_side_effect(app_name, marker_text=None):
            marker_holder["marker"] = str(marker_text)
            return self._observation(str(marker_text), composer_text=str(marker_text))

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "copy_to_clipboard", return_value={"copied": True, "method": "pbcopy", "error": None}),
            mock.patch.object(cli, "activate_chatgpt", return_value=self._activation()),
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app", return_value={"pasted": True, "method": "paste", "error": None}),
            mock.patch.object(cli, "press_enter_in_frontmost_app", return_value={"submitted": True, "method": "enter", "error": None}),
            mock.patch.object(cli, "inspect_chatgpt_submission_ui", side_effect=inspect_side_effect),
            mock.patch.object(cli, "CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS", 0.0),
            mock.patch.object(cli.time, "sleep", return_value=None),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            ok = cli._submit_feedback_to_chatgpt_flow("run-1", self._run_record(), "ChatGPT", approval_mode="auto")

        self.assertFalse(ok)
        event_types = [event[0][1] for event in fake_ledger.added_events]
        self.assertIn("gpt_feedback_submit_input_sent", event_types)
        self.assertNotIn("gpt_feedback_submission_verified", event_types)

    def test_send_button_axpress_verifies_submission(self) -> None:
        fake_ledger = _FakeLedger(self._base_events())
        calls = {"count": 0}

        def inspect_side_effect(app_name, marker_text=None):
            marker = str(marker_text)
            calls["count"] += 1
            if calls["count"] <= 2:
                return self._observation(marker, composer_text=marker, send_button=True)
            return self._observation(marker, composer_text="", candidates=[f"user message\n{marker}"], send_button=True)

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "copy_to_clipboard", return_value={"copied": True, "method": "pbcopy", "error": None}),
            mock.patch.object(cli, "activate_chatgpt", return_value=self._activation()),
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app", return_value={"pasted": True, "method": "paste", "error": None}),
            mock.patch.object(cli, "press_chatgpt_send_button", return_value={"pressed": True, "method": "macos_accessibility_axpress_send_button", "error": None}),
            mock.patch.object(cli, "press_enter_in_frontmost_app") as enter,
            mock.patch.object(cli, "inspect_chatgpt_submission_ui", side_effect=inspect_side_effect),
            mock.patch.object(cli.time, "sleep", return_value=None),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            ok = cli._submit_feedback_to_chatgpt_flow("run-1", self._run_record(), "ChatGPT", approval_mode="auto")

        self.assertTrue(ok)
        enter.assert_not_called()
        self.assertEqual(fake_ledger.added_events[-1][0][1], "gpt_feedback_submission_verified")

    def test_fallback_retry_only_when_marker_is_provably_unsent(self) -> None:
        fake_ledger = _FakeLedger(self._base_events())

        def inspect_side_effect(app_name, marker_text=None):
            marker = str(marker_text)
            return self._observation(marker, composer_text=marker, send_button=True)

        verification_results = [
            {
                "ok": False,
                "reason_code": "chatgpt_submission_not_verified",
                "status": {"composer_contains_marker": True, "submitted_candidate_count": 0},
            },
            {
                "ok": True,
                "reason_code": "chatgpt_submission_verified",
                "status": {"composer_contains_marker": False, "submitted_candidate_count": 1},
            },
        ]

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "copy_to_clipboard", return_value={"copied": True, "method": "pbcopy", "error": None}),
            mock.patch.object(cli, "activate_chatgpt", return_value=self._activation()),
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app", return_value={"pasted": True, "method": "paste", "error": None}),
            mock.patch.object(cli, "press_chatgpt_send_button", return_value={"pressed": True, "method": "macos_accessibility_axpress_send_button", "error": None}),
            mock.patch.object(cli, "press_enter_in_frontmost_app", return_value={"submitted": True, "method": "enter", "error": None}) as enter,
            mock.patch.object(cli, "inspect_chatgpt_submission_ui", side_effect=inspect_side_effect),
            mock.patch.object(cli, "_verify_submission_marker", side_effect=verification_results),
            mock.patch.object(cli.time, "sleep", return_value=None),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            ok = cli._submit_feedback_to_chatgpt_flow("run-1", self._run_record(), "ChatGPT", approval_mode="auto")

        self.assertTrue(ok)
        enter.assert_called_once()
        verified_metadata = fake_ledger.added_events[-1][0][3]
        self.assertEqual(verified_metadata["fallback_attempt_count"], 1)

    def test_enter_fallback_verifies_when_send_button_unavailable(self) -> None:
        fake_ledger = _FakeLedger(self._base_events())
        calls = {"count": 0}

        def inspect_side_effect(app_name, marker_text=None):
            marker = str(marker_text)
            calls["count"] += 1
            if calls["count"] <= 2:
                return self._observation(marker, composer_text=marker)
            return self._observation(marker, composer_text="", candidates=[f"user message\n{marker}"])

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "copy_to_clipboard", return_value={"copied": True, "method": "pbcopy", "error": None}),
            mock.patch.object(cli, "activate_chatgpt", return_value=self._activation()),
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app", return_value={"pasted": True, "method": "paste", "error": None}),
            mock.patch.object(cli, "press_enter_in_frontmost_app", return_value={"submitted": True, "method": "enter", "error": None}) as enter,
            mock.patch.object(cli, "inspect_chatgpt_submission_ui", side_effect=inspect_side_effect),
            mock.patch.object(cli.time, "sleep", return_value=None),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            ok = cli._submit_feedback_to_chatgpt_flow("run-1", self._run_record(), "ChatGPT", approval_mode="auto")

        self.assertTrue(ok)
        enter.assert_called_once()
        self.assertEqual(fake_ledger.added_events[-1][0][1], "gpt_feedback_submission_verified")

    def test_submission_marker_multiple_candidates_is_ambiguous(self) -> None:
        marker = MARKER
        status = cli._submission_verification_status(
            self._observation(marker, composer_text="", candidates=[marker, marker]),
            marker,
        )
        self.assertFalse(status["verified"])
        self.assertTrue(status["ambiguous"])

    def test_submission_marker_in_composer_and_candidate_is_ambiguous(self) -> None:
        marker = MARKER
        status = cli._submission_verification_status(
            self._observation(marker, composer_text=marker, candidates=[marker]),
            marker,
        )
        self.assertFalse(status["verified"])
        self.assertTrue(status["ambiguous"])

    def test_planner_ignores_submit_input_event_without_verified_submission(self) -> None:
        from agent.supervise import detect_next_supervise_action

        events = [
            {
                "id": 1,
                "event_type": "codex_exec_finished",
                "metadata_json": json.dumps({"found": True, "exit_code": 0, "timed_out": False, "validation_error": None}),
            },
            {
                "id": 2,
                "event_type": "prompt_repo_impact_diagnostics",
                "metadata_json": json.dumps({"flags": [], "attention_level": "ok"}),
            },
            {
                "id": 3,
                "event_type": "changed_file_classification",
                "metadata_json": json.dumps({"total_files": 0, "files": []}),
            },
            {
                "id": 4,
                "event_type": "supervision_decision",
                "metadata_json": json.dumps({"decision": "continue", "approval_required": False, "needs_review": False}),
            },
            {
                "id": 5,
                "event_type": "gpt_feedback_submit_input_sent",
                "metadata_json": json.dumps({"submit_input_result": {"submit_input_sent": True}}),
            },
        ]
        plan = detect_next_supervise_action({"id": "run-1", "status": "completed"}, events, "/tmp")
        self.assertEqual(plan.action.value, "ask_send_to_gpt")

    def test_capture_flow_default_does_not_require_sentinel(self) -> None:
        fake_ledger = _FakeLedger(_events_with_successful_submission())
        activation = {
            "activated": True,
            "frontmost_app": "ChatGPT",
            "is_frontmost": True,
            "error": None,
        }
        capture_result = {
            "ok": True,
            "source": "chatgpt_desktop_ax",
            "capture_format": "rendered_ax_text",
            "response_text": "Thinking",
            "response_length": 8,
            "response_sha256": ax.hashlib.sha256(b"Thinking").hexdigest(),
            "matched_candidate_index": 0,
            "matched_candidate_path": "FW.0",
            "response_candidate_index": 1,
            "response_candidate_path": "FW.1",
            "candidate_count": 2,
            "stable": True,
            "stable_seconds": 0.0,
            "successful_polls": 2,
            "poll_interval_seconds": 1.0,
            "timeout_seconds": 60.0,
            "match_score": 1.0,
            "ax_stats": {"candidate_count": 2},
            "format_warning": "warning",
        }

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "activate_chatgpt", return_value=activation),
            mock.patch.object(cli, "capture_response_after_feedback", return_value=capture_result) as capture,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            ok = cli._capture_gpt_response_from_chatgpt_ax_flow(
                "run-1",
                {"id": "run-1", "status": "completed"},
                "ChatGPT",
                60.0,
                0.0,
            )

        self.assertTrue(ok)
        self.assertEqual(capture.call_args.kwargs["require_sentinel_response"], False)
        self.assertEqual(capture.call_args.kwargs["submission_marker_text"], MARKER)

    def test_supervise_capture_wiring_requires_sentinel(self) -> None:
        args = argparse.Namespace(
            run_id="run-1",
            repo="/tmp",
            sandbox="read-only",
            app_name="ChatGPT",
            capture_timeout_seconds=60.0,
            stable_seconds=2.0,
            timeout=300,
        )
        plans = [
            SupervisePlan(
                action=SuperviseAction.CAPTURE_GPT_RESPONSE,
                reason="feedback_submitted_capture_needed",
            ),
        ]

        with (
            mock.patch.object(cli.ledger, "get_run", return_value={"id": "run-1", "status": "completed"}),
            mock.patch.object(cli.ledger, "list_events", return_value=[]),
            mock.patch.object(cli, "detect_next_supervise_action", side_effect=plans),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", return_value=False) as capture_flow,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        self.assertEqual(capture_flow.call_args.kwargs["require_sentinel_response"], True)

    def test_failed_sentinel_required_capture_writes_failed_ledger_event(self) -> None:
        fake_ledger = _FakeLedger(_events_with_successful_submission())
        activation = {
            "activated": True,
            "frontmost_app": "ChatGPT",
            "is_frontmost": True,
            "error": None,
        }
        capture_result = {
            "ok": False,
            "matched_feedback": True,
            "matched_candidate_index": 0,
            "matched_candidate_path": "FW.0",
            "candidate_count": 2,
            "stable": False,
            "sentinel_required": True,
            "sentinel_state": "sentinel_pending",
            "reason_code": "sentinel_not_found_timeout",
            "stable_seconds": 2.0,
            "successful_polls": 0,
            "poll_interval_seconds": 1.0,
            "timeout_seconds": 60.0,
            "ax_stats": {"candidate_count": 2},
            "post_feedback_candidate_summaries": [
                {
                    "index": 1,
                    "path": "FW.1",
                    "length": 8,
                    "sha256": ax.hashlib.sha256(b"Thinking").hexdigest(),
                    "text_preview_repr": "'Thinking'",
                    "sentinel_status": "no_markers",
                    "candidate_classification": "content",
                    "classification_reason": "not_known_ui_chrome",
                }
            ],
            "error": "No complete sentinel-wrapped assistant response was found after the matched feedback.",
        }
        stdout = io.StringIO()

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "activate_chatgpt", return_value=activation),
            mock.patch.object(cli, "capture_response_after_feedback", return_value=capture_result),
            mock.patch("sys.stdout", new=stdout),
        ):
            ok = cli._capture_gpt_response_from_chatgpt_ax_flow(
                "run-1",
                {"id": "run-1", "status": "completed"},
                "ChatGPT",
                60.0,
                2.0,
                require_sentinel_response=True,
            )

        self.assertFalse(ok)
        self.assertEqual(len(fake_ledger.added_events), 2)
        self.assertEqual(fake_ledger.added_events[0][0][1], "gpt_response_capture_started")
        self.assertEqual(fake_ledger.added_events[1][0][1], "gpt_response_capture_failed")
        self.assertNotIn(
            "gpt_response_captured",
            [event[0][1] for event in fake_ledger.added_events],
        )
        failed_metadata = fake_ledger.added_events[1][0][3]
        self.assertEqual(failed_metadata["reason_code"], "sentinel_not_found_timeout")
        self.assertEqual(failed_metadata["sentinel_state"], "sentinel_pending")
        self.assertEqual(failed_metadata["matched_submission_event_id"], 1)
        self.assertEqual(failed_metadata["candidate_count"], 2)
        self.assertEqual(failed_metadata["matched_candidate_index"], 0)
        self.assertEqual(failed_metadata["matched_candidate_path"], "FW.0")
        self.assertEqual(failed_metadata["stability"]["stable_seconds"], 2.0)
        self.assertEqual(failed_metadata["ax_stats"], {"candidate_count": 2})
        output = stdout.getvalue()
        self.assertIn("matched_candidate_index: 0", output)
        self.assertIn("candidate_count: 2", output)
        self.assertIn("capture_reason: sentinel_not_found_timeout", output)
        self.assertIn("sentinel_state: sentinel_pending", output)
        self.assertIn("post_feedback_candidate: index=1", output)
        self.assertIn("sentinel_status=no_markers", output)


if __name__ == "__main__":
    unittest.main()
