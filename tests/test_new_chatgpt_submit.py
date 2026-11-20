from __future__ import annotations

import unittest

from agent.new_chatgpt_app import PreflightResult
from agent.new_chatgpt_submit import (
    OUTCOME_SENT,
    REASON_COMPOSER_NOT_FOUND,
    REASON_EMPTY_MESSAGE,
    REASON_NOT_REGISTERED,
    REASON_SEND_NOT_ENABLED,
    REASON_VALUE_MISMATCH,
    composer_is_empty,
    submit_message_new_chatgpt,
)
from tests.new_chatgpt_ax_fixtures import FakeBackend, build_app_tree, user_turn

MARKER = "SUBMISSION_MARKER_BEGIN\nrun_id=r1\nnonce=abc\npayload_sha256=00\nSUBMISSION_MARKER_END"
MESSAGE = "Feedback line one\n\n- a bullet  with  double spaces\n\n" + MARKER


class Harness:
    def __init__(self, **tree_kwargs) -> None:
        self.tree = build_app_tree(**tree_kwargs)
        self.backend = FakeBackend(self.tree.app)
        self.now = 0.0
        self.preflight_calls: list[dict] = []
        self.preflight_ok = True

    def wire(self, **kwargs) -> "Harness":
        self.tree.wire_composer(self.backend, **kwargs)
        return self

    def preflight(self, **kwargs) -> PreflightResult:
        self.preflight_calls.append(kwargs)
        if not self.preflight_ok:
            return PreflightResult(ok=False, reason_code="new_chatgpt_wrong_mode", error="Codex mode")
        self.tree.app.attrs["AXEnhancedUserInterface"] = True  # what the real preflight does
        return PreflightResult(ok=True, pid=4242)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.tree.tick()

    def clock(self) -> float:
        return self.now

    def submit(self, message: str = MESSAGE, **kwargs):
        kwargs.setdefault("marker_text", MARKER)
        return submit_message_new_chatgpt(
            message, backend=self.backend, preflight=self.preflight, sleep=self.sleep, clock=self.clock, **kwargs
        )

    def send_presses(self) -> int:
        return sum(1 for node, action in self.backend.actions if node is self.tree.send and action == "AXPress")


class SubmitTests(unittest.TestCase):
    def test_sends_and_confirms(self) -> None:
        h = Harness().wire()

        result = h.submit()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.outcome, OUTCOME_SENT)
        self.assertTrue(result.value_readback_matches)
        self.assertTrue(result.marker_found_in_new_turn)
        self.assertTrue(result.stop_seen)
        self.assertTrue(result.composer_cleared)
        self.assertEqual(result.send_press_count, 1)
        self.assertEqual(result.user_turns_after, result.user_turns_before + 1)
        self.assertEqual(result.method, "ax_set_value_press_send_new_chatgpt")

    def test_message_is_sent_byte_identical(self) -> None:
        h = Harness().wire()

        h.submit()

        sent = h.tree.turn_list.children[-3].children[0].children[0].children[0].attrs["AXValue"]
        self.assertEqual(sent, MESSAGE)

    def test_uses_no_clipboard_or_keystrokes_only_value_and_press(self) -> None:
        h = Harness().wire()

        h.submit()

        self.assertEqual([action for _, action in h.backend.actions], ["AXPress"])
        value_sets = [call for call in h.backend.set_calls if call[1] == "AXValue"]
        self.assertEqual(len(value_sets), 1)
        self.assertIs(value_sets[0][0], h.tree.composer)

    def test_brings_app_to_front(self) -> None:
        h = Harness().wire()

        h.submit()

        self.assertTrue(all(call["bring_to_front"] for call in h.preflight_calls))

    def test_marker_defaults_to_whole_message(self) -> None:
        h = Harness().wire()

        result = h.submit("What is a safe way to run shell commands?", marker_text=None)

        self.assertTrue(result.ok, result.error)

    def test_trailing_newline_readback_is_accepted(self) -> None:
        h = Harness().wire(readback_suffix="\n")

        result = h.submit()

        self.assertTrue(result.ok, result.error)

    def test_composer_dropping_the_trailing_newline_is_accepted(self) -> None:
        # Live 2026-11-20: the loop's message ends with "\n" and the composer drops it.
        h = Harness().wire(drop_trailing_newlines=True)

        result = h.submit(MESSAGE + "\n")

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.value_readback_matches)

    def test_inner_whitespace_must_still_match(self) -> None:
        from agent.new_chatgpt_submit import _value_matches

        self.assertTrue(_value_matches("a\n\n\nb", "a\n\n\nb\n"))
        self.assertTrue(_value_matches("a\nb\n", "a\nb"))
        self.assertFalse(_value_matches("a\n\nb", "a\n\n\nb"))
        self.assertFalse(_value_matches("a b", "a  b"))
        self.assertFalse(_value_matches(" a", "a"))

    def test_wrong_readback_fails_before_pressing_send(self) -> None:
        h = Harness().wire(readback_suffix=" (mangled)")

        result = h.submit()

        self.assertEqual(result.reason_code, REASON_VALUE_MISMATCH)
        self.assertEqual(result.set_value_attempts, 2)
        self.assertEqual(h.send_presses(), 0)

    def test_draft_is_overwritten_and_recorded_by_length_only(self) -> None:
        h = Harness().wire()
        h.tree.composer.attrs["AXValue"] = "half-written draft"

        result = h.submit()

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.draft_overwritten)
        self.assertEqual(result.draft_length, len("half-written draft"))
        self.assertNotIn("half-written draft", repr(result.to_dict()))

    def test_press_ignored_because_enhanced_ui_was_off_is_retried_once(self) -> None:
        h = Harness().wire(presses_needed=2)

        result = h.submit()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.send_press_count, 2)
        self.assertEqual(result.retry_reason, "enhanced_ui_was_off")
        self.assertEqual(len(h.preflight_calls), 2)  # preflight re-run before the retry

    def test_never_more_than_two_presses(self) -> None:
        h = Harness().wire(presses_needed=99)

        result = h.submit(confirm_timeout_seconds=1.0)

        self.assertEqual(result.reason_code, REASON_NOT_REGISTERED)
        self.assertEqual(h.send_presses(), 2)

    def test_slow_app_is_waited_for_without_pressing_again(self) -> None:
        # The press works, but the new turn shows up only after ~5 s (20 polls).
        h = Harness().wire(update_after_ticks=20)

        result = h.submit()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(h.send_presses(), 1)

    def test_app_slower_than_the_timeout_fails_without_a_second_press(self) -> None:
        # The press registered but nothing shows within the window: report, never re-send.
        h = Harness().wire(update_after_ticks=200)

        result = h.submit(confirm_timeout_seconds=2.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_NOT_REGISTERED)
        self.assertEqual(h.send_presses(), 1)
        self.assertIsNone(result.retry_reason)

    def test_earlier_identical_message_does_not_count_as_sent(self) -> None:
        # The same text was already sent earlier in this chat.
        h = Harness(turns=user_turn(MESSAGE)).wire(presses_needed=99)

        result = h.submit(confirm_timeout_seconds=1.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_NOT_REGISTERED)

    def test_send_into_empty_chat(self) -> None:
        h = Harness(turns=[]).wire()

        result = h.submit()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.user_turns_before, 0)
        self.assertEqual(result.user_turns_after, 1)

    def test_send_without_stop_but_composer_cleared_counts(self) -> None:
        h = Harness().wire(show_stop=False)

        result = h.submit()

        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.stop_seen)
        self.assertTrue(result.composer_cleared)

    def test_send_that_stays_disabled_fails(self) -> None:
        h = Harness().wire()
        h.backend.on_set_string = lambda node, name, value: node.attrs.__setitem__(name, value)  # Send never enables

        result = h.submit()

        self.assertEqual(result.reason_code, REASON_SEND_NOT_ENABLED)
        self.assertEqual(h.send_presses(), 0)

    def test_two_composers_fail_closed(self) -> None:
        h = Harness(composer_count=2).wire()

        result = h.submit()

        self.assertEqual(result.reason_code, REASON_COMPOSER_NOT_FOUND)
        self.assertEqual(h.backend.set_calls, [])

    def test_failed_preflight_touches_nothing(self) -> None:
        h = Harness().wire()
        h.preflight_ok = False

        result = h.submit()

        self.assertEqual(result.reason_code, "new_chatgpt_wrong_mode")
        self.assertEqual(h.backend.set_calls, [])
        self.assertEqual(h.backend.actions, [])

    def test_blank_message_is_rejected(self) -> None:
        h = Harness().wire()

        result = h.submit("   \n")

        self.assertEqual(result.reason_code, REASON_EMPTY_MESSAGE)
        self.assertEqual(h.preflight_calls, [])

    def test_composer_empty_rule(self) -> None:
        self.assertTrue(composer_is_empty(""))
        self.assertTrue(composer_is_empty("Ask ChatGPT\n"))
        self.assertTrue(composer_is_empty("Ask ChatGPT"))
        self.assertFalse(composer_is_empty("Ask ChatGPT about this"))


if __name__ == "__main__":
    unittest.main()
