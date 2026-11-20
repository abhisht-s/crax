"""Step 8: the new ChatGPT app behind the unchanged Classic submit/capture/extract services."""

from __future__ import annotations

import hashlib
import json
import unittest
from unittest import mock

from agent.chatgpt_ax_capture import COMPLETED_WITHOUT_PROMPT_REASON_CODE
from agent.chatgpt_services import (
    capture_chatgpt_response_service,
    extract_next_codex_prompt_service,
    submit_feedback_to_chatgpt_service,
)
from agent.gpt_feedback import _build_submission_marker
from agent.new_chatgpt_app import PreflightResult
from agent.new_chatgpt_capture import CaptureResult
from agent.new_chatgpt_loop import (
    ENTER_NOT_SUPPORTED_METHOD,
    NewChatGPTUIOperations,
    classic_capture_result,
    new_capture_service,
    new_destination_adapter_factory,
    new_destination_navigation_service,
    new_submit_service,
    new_target_services,
)
from agent.new_chatgpt_navigation import NavigationResult
from agent.run_services import RunDestinationBinding
from tests.new_chatgpt_ax_fixtures import FakeBackend, build_app_tree
from tests.test_new_chatgpt_capture import code_block, paragraph, reply_parts

RUN_ID = "run-1"
PROMPT_LINES = ["BEGIN_NEXT_CODEX_PROMPT", "", "TASK: Verify Stage 1.", "", "  keep indent", "", "END_NEXT_CODEX_PROMPT"]


class Ledger:
    """Minimal ledger with the real event shape (id, event_type, metadata_json)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def list_events(self, run_id: str) -> list[dict]:
        return [dict(event) for event in self.events]

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> int:
        event_id = len(self.events) + 1
        self.events.append(
            {
                "id": event_id,
                "run_id": run_id,
                "event_type": event_type,
                "message": message,
                "metadata_json": json.dumps(metadata, sort_keys=True) if metadata is not None else None,
            }
        )
        return event_id

    def types(self) -> list[str]:
        return [event["event_type"] for event in self.events]

    def metadata(self, event_type: str) -> dict:
        return json.loads(next(e for e in self.events if e["event_type"] == event_type)["metadata_json"])


def feedback_builder(run: dict, events: list[dict]) -> dict:
    payload = "Codex finished.\n\nTests: 12 passed.\nChanged files: none."
    payload_sha = hashlib.sha256(payload.encode()).hexdigest()
    marker = _build_submission_marker(RUN_ID, payload_sha, nonce="nonce-1")
    return {
        "run_id": RUN_ID,
        "status": "completed",
        "codex_exit_code": 0,
        "codex_timed_out": False,
        "changed_files": [],
        "message": payload + "\n\n" + marker["submission_marker_text"],
        "feedback_payload_version": 1,
        "feedback_payload_sha256": payload_sha,
        "feedback_payload_length": len(payload),
        "payload_without_marker_sha256": payload_sha,
        "submittable": True,
        **marker,
    }


class FakeNewApp:
    def __init__(self, **wire_kwargs) -> None:
        self.tree = build_app_tree()
        self.backend = FakeBackend(self.tree.app)
        self.tree.wire_composer(self.backend, **wire_kwargs)
        self.now = 0.0
        self.preflight_calls: list[dict] = []

    def preflight(self, **kwargs) -> PreflightResult:
        self.preflight_calls.append(kwargs)
        self.tree.app.attrs["AXEnhancedUserInterface"] = True
        self.tree.app.attrs["AXFrontmost"] = True
        return PreflightResult(ok=True, pid=4242, frontmost=True, bring_to_front=True, product_mode="ChatGPT")

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.tree.tick()

    def clock(self) -> float:
        return self.now

    def operations(self) -> NewChatGPTUIOperations:
        return NewChatGPTUIOperations(backend=self.backend, preflight=self.preflight, sleep=self.sleep, clock=self.clock)

    def reply(self, body) -> None:
        self.tree.turn_list.children.extend(reply_parts(body))
        self.tree.send.attrs["AXDescription"] = self.tree.send.attrs["AXTitle"] = "Send"
        self.tree.send.attrs["AXEnabled"] = False

    def submit(self, ledger: Ledger, ops: NewChatGPTUIOperations | None = None):
        return submit_feedback_to_chatgpt_service(
            RUN_ID,
            {"id": RUN_ID, "status": "completed"},
            app_name="ChatGPT",
            approval_mode="auto",
            ledger=ledger,
            feedback_builder=feedback_builder,
            monotonic_function=self.clock,
            sleep_function=self.sleep,
            **(ops or self.operations()).submit_service_kwargs(),
        )

    def capture(self, ledger: Ledger, *, require_sentinel: bool = True):
        ops = self.operations()
        return capture_chatgpt_response_service(
            RUN_ID,
            app_name="ChatGPT",
            stable_seconds=0.0,
            require_sentinel_response=require_sentinel,
            ledger=ledger,
            activation_function=ops.activate,
            capture_function=ops.capture,
        )


class ClassicServicesOnTheNewAppTests(unittest.TestCase):
    def test_full_handoff_submit_capture_extract(self) -> None:
        app = FakeNewApp()
        ledger = Ledger()

        submitted = app.submit(ledger)

        self.assertTrue(submitted.ok, getattr(submitted, "reason_code", None))
        self.assertEqual(
            ledger.types(),
            [
                "gpt_feedback_generated",
                "gpt_feedback_copied",
                "gpt_feedback_pasted",
                "gpt_feedback_submit_input_sent",
                "gpt_feedback_submission_verified",
            ],
        )
        verified = ledger.metadata("gpt_feedback_submission_verified")
        self.assertEqual(verified["reason_code"], "chatgpt_submission_verified")
        self.assertIn("nonce=nonce-1", verified["submission_marker_text"])
        self.assertEqual(verified["paste_result"]["method"], "ax_set_value_new_chatgpt")
        self.assertEqual(verified["submit_input_result"]["method"], "ax_press_send_new_chatgpt")
        self.assertEqual(ledger.metadata("gpt_feedback_copied")["method"], "new_chatgpt_no_clipboard_staged")
        self.assertEqual(verified["fallback_attempt_count"], 0)

        app.reply([paragraph("Looks good."), code_block(PROMPT_LINES)])
        captured = app.capture(ledger)

        self.assertTrue(captured.ok, captured.reason_code)
        self.assertEqual(ledger.types()[-2:], ["gpt_response_capture_started", "gpt_response_captured"])
        capture_meta = ledger.metadata("gpt_response_captured")
        self.assertEqual(capture_meta["sentinel_state"], "complete_sentinel_stable")
        self.assertEqual(capture_meta["response_sha256"], hashlib.sha256(capture_meta["response_text"].encode()).hexdigest())

        extracted = extract_next_codex_prompt_service(RUN_ID, require_sentinel=True, ledger=ledger)

        self.assertTrue(extracted.ok, extracted.reason_code)
        self.assertEqual(extracted.extraction.prompt_text, "TASK: Verify Stage 1.\n\n  keep indent")

    def test_message_reaches_the_app_byte_identical_without_clipboard(self) -> None:
        app = FakeNewApp()
        ledger = Ledger()

        with mock.patch("agent.chatgpt_services.copy_to_clipboard") as clipboard, mock.patch(
            "agent.chatgpt_services.paste_clipboard_to_frontmost_app"
        ) as paste, mock.patch("agent.chatgpt_services.press_enter_in_frontmost_app") as enter:
            app.submit(ledger)

        clipboard.assert_not_called()
        paste.assert_not_called()
        enter.assert_not_called()
        sent = app.tree.turn_list.children[-3].children[0].children[0].children[0].attrs["AXValue"]
        self.assertEqual(sent, feedback_builder({}, [])["message"])

    def test_real_feedback_shape_with_trailing_newline_is_sent(self) -> None:
        # Regression for the live run on 2026-11-20: the loop's feedback ends with
        # "\n", the composer drops it, and the read-back must still pass.
        app = FakeNewApp(drop_trailing_newlines=True)
        ledger = Ledger()
        builder = lambda run, events: {**feedback_builder(run, events), "message": feedback_builder(run, events)["message"] + "\n"}  # noqa: E731

        submitted = submit_feedback_to_chatgpt_service(
            RUN_ID, {"id": RUN_ID}, app_name="ChatGPT", approval_mode="auto", ledger=ledger,
            feedback_builder=builder, monotonic_function=app.clock, sleep_function=app.sleep,
            **app.operations().submit_service_kwargs(),
        )

        self.assertTrue(submitted.ok, submitted.reason_code)
        self.assertEqual(ledger.types()[-1], "gpt_feedback_submission_verified")
        self.assertEqual(len([e for e in ledger.types() if e == "gpt_feedback_generated"]), 1)

    def test_reply_without_prompt_stops_the_session_with_the_classic_code(self) -> None:
        app = FakeNewApp()
        ledger = Ledger()
        app.submit(ledger)
        app.reply([paragraph("Which stage do you mean?")])

        captured = app.capture(ledger)

        self.assertFalse(captured.ok)
        self.assertEqual(captured.reason_code, COMPLETED_WITHOUT_PROMPT_REASON_CODE)
        self.assertEqual(ledger.types()[-1], "gpt_response_capture_failed")

    def test_press_ignored_with_enhanced_ui_off_is_retried_once_and_verified(self) -> None:
        app = FakeNewApp(presses_needed=2)
        ledger = Ledger()

        submitted = app.submit(ledger)

        self.assertTrue(submitted.ok)
        result = ledger.metadata("gpt_feedback_submission_verified")["submit_input_result"]
        self.assertEqual(result["press_count"], 2)
        self.assertEqual(result["retry_reason"], "enhanced_ui_was_off")

    def test_press_with_no_effect_and_ui_on_is_never_repeated_or_entered(self) -> None:
        app = FakeNewApp(presses_needed=99)
        ledger = Ledger()
        ops = app.operations()
        ops._press_effect_timeout = 0.5
        app.tree.app.attrs["AXEnhancedUserInterface"] = True
        original_press = app.backend.press_handlers[id(app.tree.send)]

        def press_keeping_ui_on():
            result = original_press()
            app.tree.app.attrs["AXEnhancedUserInterface"] = True  # not the known cause
            return result

        app.backend.press_handlers[id(app.tree.send)] = press_keeping_ui_on
        submitted = app.submit(ledger, ops)

        self.assertFalse(submitted.ok)
        presses = [node for node, action in app.backend.actions if node is app.tree.send]
        self.assertEqual(len(presses), 1)
        self.assertNotIn("Fallback submit input sent", [e["message"] for e in ledger.events])

    def test_app_not_ready_fails_before_touching_the_composer(self) -> None:
        app = FakeNewApp()
        app.preflight = lambda **_: PreflightResult(ok=False, reason_code="new_chatgpt_wrong_mode", error="Codex mode")
        ledger = Ledger()

        submitted = app.submit(ledger)

        self.assertFalse(submitted.ok)
        self.assertEqual(submitted.reason_code, "chatgpt_not_frontmost")
        self.assertFalse([c for c in app.backend.set_calls if c[1] == "AXValue"])
        self.assertEqual(ledger.metadata("gpt_feedback_submission_failed")["activation_result"]["preflight_reason_code"], "new_chatgpt_wrong_mode")

    def test_enter_is_never_sent(self) -> None:
        result = NewChatGPTUIOperations().enter_not_supported()

        self.assertFalse(result["submitted"])
        self.assertEqual(result["method"], ENTER_NOT_SUPPORTED_METHOD)


class CaptureResultMappingTests(unittest.TestCase):
    def mapped(self, **fields) -> dict:
        captured = CaptureResult(ok=False, **fields)
        return classic_capture_result(captured, require_sentinel=True, stable_seconds=0.0, poll_interval_seconds=1.0, marker_used=True)

    def test_cancel_and_ambiguity_use_classic_codes(self) -> None:
        self.assertEqual(self.mapped(reason_code="operator_cancelled")["reason_code"], "operator_cancelled")
        self.assertEqual(self.mapped(reason_code="marker_turn_ambiguous")["reason_code"], "multiple_submission_marker_candidates")

    def test_malformed_prompt(self) -> None:
        mapped = self.mapped(reason_code="sentinel_prompt_malformed")

        self.assertEqual(mapped["reason_code"], "sentinel_malformed_stable")
        self.assertEqual(mapped["sentinel_state"], "stable_malformed_sentinel")


class NavigationMappingTests(unittest.TestCase):
    binding = RunDestinationBinding(project_title="<p>", chat_title="<c>")

    def nav(self, result: NavigationResult):
        return new_destination_navigation_service("run-1", self.binding, None, app_name="ChatGPT", ledger=None, opener=lambda p, c: result)

    def test_opened(self) -> None:
        r = self.nav(NavigationResult(ok=True, outcome="opened", presses=["chat"], chat_row_count=1, confirmed=True))
        self.assertTrue(r.ok)
        self.assertTrue(r.action_posted)
        self.assertTrue(r.navigator_confirmed)

    def test_already_open(self) -> None:
        r = self.nav(NavigationResult(ok=True, outcome="already_open", chat_row_count=1, confirmed=True))
        self.assertTrue(r.ok)
        self.assertFalse(r.action_posted)

    def test_pressed_but_unconfirmed_lets_the_gate_decide(self) -> None:
        r = self.nav(NavigationResult(ok=False, reason_code="new_chatgpt_nav_not_confirmed", presses=["chat"], chat_row_count=1))
        self.assertTrue(r.ok)
        self.assertFalse(r.navigator_confirmed)

    def test_not_found_fails(self) -> None:
        r = self.nav(NavigationResult(ok=False, reason_code="new_chatgpt_nav_chat_not_found", error="no chat"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, "destination_navigation_action_not_performed")
        self.assertIn("new_chatgpt_nav_chat_not_found", r.error_message)


class ServiceWiringTests(unittest.TestCase):
    def test_new_target_services(self) -> None:
        services = new_target_services()

        self.assertEqual(services.target, "new")
        self.assertIs(services.submit_service, new_submit_service)
        self.assertIs(services.capture_service, new_capture_service)
        self.assertIs(services.destination_adapter_factory, new_destination_adapter_factory)
        self.assertIs(services.destination_navigation_service, new_destination_navigation_service)

    def test_destination_adapter_is_the_new_app_adapter(self) -> None:
        from agent.new_chatgpt_destination import NewChatGPTDestinationSnapshotAdapter

        self.assertIsInstance(new_destination_adapter_factory(), NewChatGPTDestinationSnapshotAdapter)

    def test_capture_service_keeps_classic_semantics(self) -> None:
        ops = NewChatGPTUIOperations()
        with mock.patch("agent.chatgpt_services.capture_chatgpt_response_service", return_value="r") as capture:
            new_capture_service("run-1", {}, "ChatGPT", 42.0, 3.0, require_sentinel_response=True, ledger="L", operations=ops)

        kwargs = capture.call_args.kwargs
        self.assertIsNone(kwargs["timeout_seconds"])
        self.assertEqual(kwargs["capture_function"], ops.capture)
        self.assertEqual(kwargs["activation_function"], ops.activate)


if __name__ == "__main__":
    unittest.main()
