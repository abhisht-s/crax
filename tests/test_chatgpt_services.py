from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from agent import cli
from agent.chatgpt_services import (
    ExtractNextCodexPromptServiceResult,
    GPT_RESPONSE_CAPTURE_FAILED_EVENT_TYPE,
    GPT_RESPONSE_CAPTURE_STARTED_EVENT_TYPE,
    GPT_RESPONSE_CAPTURED_EVENT_TYPE,
    NEXT_CODEX_PROMPT_EXTRACTED_EVENT_TYPE,
    NEXT_CODEX_PROMPT_EXTRACTED_MESSAGE,
    capture_chatgpt_response_service,
    extract_next_codex_prompt_service,
    submit_feedback_to_chatgpt_service,
)
from agent.gpt_feedback import MAX_CLEAN_FINAL_MESSAGE_CHARS


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _event(event_id: int, event_type: str, metadata: dict) -> dict:
    return {
        "id": event_id,
        "event_type": event_type,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


def _submission(event_id: int = 1) -> dict:
    return _event(
        event_id,
        "gpt_feedback_submission_verified",
        {"reason_code": "chatgpt_submission_verified"},
    )


def _capture(submission: dict, text: str, event_id: int = 2) -> dict:
    return _event(
        event_id,
        "gpt_response_captured",
        {
            "matched_submission_event_id": submission["id"],
            "response_text": text,
            "response_sha256": _sha(text),
        },
    )


def _sentinel_response(prompt: str = "Say exactly: extracted") -> str:
    return f"BEGIN_NEXT_CODEX_PROMPT\n{prompt}\nEND_NEXT_CODEX_PROMPT"


def _labeled_response(prompt: str = "Say exactly: fenced") -> str:
    return f"Next Codex prompt:\n```text\n{prompt}\n```"


CAPTURE_MARKER = "AGENT_SUBMISSION\nrun_id=run-1\nnonce=n\npayload_sha256=p\nEND_AGENT_SUBMISSION"


def _verified_submission_with_marker(event_id: int = 1, **metadata_extra: object) -> dict:
    metadata = {
        "reason_code": "chatgpt_submission_verified",
        "submission_marker_text": CAPTURE_MARKER,
        "submission_marker_sha256": _sha(CAPTURE_MARKER),
        "submission_marker_nonce": "n",
        "submission_marker_payload_sha256": "p",
        "feedback_payload_sha256": "feedback-sha",
        "feedback_payload_version": "compact_wrapper_v2_submission_marker",
        "feedback_payload_length": 123,
    }
    metadata.update(metadata_extra)
    return _event(event_id, "gpt_feedback_submission_verified", metadata)


def _activation(frontmost: bool = True) -> dict:
    return {
        "activated": frontmost,
        "app_name": "ChatGPT",
        "frontmost_app": "ChatGPT" if frontmost else "Finder",
        "is_frontmost": frontmost,
        "error": None if frontmost else "Expected frontmost app 'ChatGPT', got 'Finder'.",
    }


def _successful_capture_result(response: str = "Thinking") -> dict:
    return {
        "ok": True,
        "source": "chatgpt_desktop_ax",
        "capture_format": "rendered_ax_text",
        "response_text": response,
        "response_length": len(response),
        "response_sha256": _sha(response),
        "matched_candidate_index": 0,
        "matched_candidate_path": "FW.0",
        "response_candidate_index": 1,
        "response_candidate_path": "FW.1",
        "candidate_count": 2,
        "stable": True,
        "stable_seconds": 2.0,
        "successful_polls": 3,
        "poll_interval_seconds": 1.0,
        "timeout_seconds": None,
        "match_score": 1.0,
        "ax_stats": {"candidate_count": 2},
        "format_warning": "Captured text is rendered macOS Accessibility text; Markdown and code formatting may be lossy.",
        "sentinel_state": "complete_sentinel_stable",
        "reason_code": "complete_sentinel_stable",
        "post_feedback_candidate_summaries": [
            {
                "index": 1,
                "path": "FW.1",
                "length": len(response),
                "sha256": _sha(response),
                "text_preview_repr": repr(response),
                "sentinel_status": "valid_complete_sentinel",
                "candidate_classification": "content",
                "classification_reason": "not_known_ui_chrome",
            }
        ],
    }


def _failed_capture_result(reason_code: str, sentinel_state: str, error: str = "capture failed") -> dict:
    return {
        "ok": False,
        "source": "chatgpt_desktop_ax",
        "capture_format": "rendered_ax_text",
        "matched_feedback": True,
        "matched_submission_marker": True,
        "matched_candidate_index": 0,
        "matched_candidate_path": "FW.0",
        "candidate_count": 2,
        "stable": False,
        "sentinel_required": True,
        "sentinel_state": sentinel_state,
        "reason_code": reason_code,
        "stable_seconds": 2.0,
        "successful_polls": 0,
        "poll_interval_seconds": 1.0,
        "timeout_seconds": None,
        "ax_stats": {"candidate_count": 2},
        "post_feedback_candidate_summaries": [
            {
                "index": 1,
                "path": "FW.1",
                "length": 8,
                "sha256": _sha("Thinking"),
                "text_preview_repr": "'Thinking'",
                "sentinel_status": "no_markers",
                "candidate_classification": "content",
                "classification_reason": "not_known_ui_chrome",
            }
        ],
        "error": error,
    }


class FakeLedger:
    def __init__(self, events: list[dict], operations: list[str] | None = None) -> None:
        self.events = list(events)
        self.added_events: list[dict] = []
        self.status_updates: list[dict] = []
        self.operations = operations
        self._next_id = max(
            [int(event.get("id") or 0) for event in events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1

    def get_run(self, run_id: str) -> dict:
        return {"id": run_id, "status": "completed"}

    def list_events(self, run_id: str) -> list[dict]:
        return self.events

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> dict:
        event = {
            "id": self._next_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        self._next_id += 1
        self.added_events.append(event)
        self.events.append(event)
        if self.operations is not None:
            self.operations.append("event")
        return event


class RecordingArtifactWriter:
    def __init__(self, *, fail: bool = False, operations: list[str] | None = None) -> None:
        self.fail = fail
        self.writes: list[tuple[Path, str]] = []
        self.operations = operations

    def __call__(self, output_path: Path, text: str) -> Path:
        self.writes.append((output_path, text))
        if self.operations is not None:
            self.operations.append("artifact")
        if self.fail:
            raise OSError("disk full")
        return output_path


def _submission_base_events(stdout: str = "stdout\n", stderr: str = "stderr\n") -> list[dict]:
    return [
        _event(
            1,
            "codex_exec_finished",
            {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": 0,
                "timed_out": False,
                "validation_error": None,
                "final_message": "Clean final assistant message.\n",
                "final_message_path": "/tmp/run-1/final-message.md",
                "final_message_status": "valid",
                "final_message_error": None,
                "final_message_length": len("Clean final assistant message.\n"),
            },
        ),
        _event(2, "changed_file_classification", {"total_files": 0, "files": []}),
        _event(3, "prompt_repo_impact_diagnostics", {"flags": [], "attention_level": "ok"}),
        _event(
            4,
            "supervision_decision",
            {"decision": "continue", "approval_required": False, "needs_review": False},
        ),
    ]


def _submission_run() -> dict:
    return {"id": "run-1", "status": "completed"}


def _submission_activation(frontmost: bool = True) -> dict:
    return {
        "activated": frontmost,
        "app_name": "ChatGPT",
        "frontmost_app": "ChatGPT" if frontmost else "Finder",
        "is_frontmost": frontmost,
        "error": None if frontmost else "Expected frontmost app 'ChatGPT', got 'Finder'.",
    }


def _submission_observation(
    marker: str,
    *,
    ok: bool = True,
    composer: bool = True,
    composer_text: str = "",
    text_input_candidates: bool = True,
    candidates: list[str] | None = None,
    send_button: bool = False,
) -> dict:
    candidates = candidates or []
    focused_composer = (
        {"path": "FW.1", "role": "AXTextArea", "focused": True, "text": composer_text}
        if composer
        else None
    )
    text_inputs = []
    if text_input_candidates:
        text_inputs.append({"path": "FW.1", "role": "AXTextArea", "focused": composer, "text": composer_text})
    return {
        "ok": ok,
        "method": "fake_ax",
        "focused_element": focused_composer,
        "focused_composer": focused_composer,
        "text_input_candidates": text_inputs,
        "button_candidates": [{"path": "FW.2", "role": "AXButton", "enabled": True}] if send_button else [],
        "send_button": {"path": "FW.2", "role": "AXButton", "enabled": True} if send_button else None,
        "message_candidates": [
            {"index": index, "path": f"FW.{index + 3}", "role": "AXStaticText", "text": text}
            for index, text in enumerate(candidates)
        ],
        "marker_text_present_in_composer": marker in composer_text,
        "marker_text_candidate_count": sum(1 for text in candidates if marker in text),
        "ax_stats": {"candidate_count": len(candidates)},
        "error": None if ok else "inspection failed",
    }


class SubmitFeedbackToChatGPTServiceTests(unittest.TestCase):
    def _service(
        self,
        *,
        ledger: FakeLedger | None = None,
        copy_result: dict | None = None,
        activation_result: dict | None = None,
        inspection_function=None,
        paste_result: dict | None = None,
        ax_send_result: dict | None = None,
        enter_result: dict | None = None,
        verification_function=None,
        use_default_submission_verifier: bool = False,
        operations: list[str] | None = None,
        artifact_writer=None,
        **kwargs,
    ):
        ledger = ledger or FakeLedger(_submission_base_events(), operations=operations)
        copied_texts: list[str] = []
        send_calls: list[str] = []
        enter_calls: list[str] = []

        def copy_function(text: str) -> dict:
            copied_texts.append(text)
            if operations is not None:
                operations.append("copy")
            return copy_result or {"copied": True, "method": "pbcopy", "error": None}

        def activate_function(app_name: str) -> dict:
            if operations is not None:
                operations.append("activate")
            return activation_result or _submission_activation(True)

        def paste_function() -> dict:
            if operations is not None:
                operations.append("paste")
            if paste_result is not None:
                return paste_result
            return {"pasted": True, "method": "paste", "error": None}

        def ax_send_function(app_name: str, path: str) -> dict:
            send_calls.append(path)
            if operations is not None:
                operations.append("ax_send")
            if ax_send_result is not None:
                return ax_send_result
            return {"pressed": True, "method": "macos_accessibility_axpress_send_button", "path": path, "error": None}

        def enter_function() -> dict:
            enter_calls.append("enter")
            if operations is not None:
                operations.append("enter")
            if enter_result is not None:
                return enter_result
            return {"submitted": True, "method": "enter", "error": None}

        if inspection_function is None:
            calls = {"count": 0}

            def inspection_function(app_name: str, marker_text: str | None = None) -> dict:
                marker = str(marker_text)
                calls["count"] += 1
                if calls["count"] == 1:
                    return _submission_observation(marker, composer_text="")
                if calls["count"] == 2:
                    return _submission_observation(marker, composer_text=marker)
                return _submission_observation(marker, composer_text=marker, send_button=True)

        if verification_function is None and not use_default_submission_verifier:
            verification_function = lambda app_name, marker_text: {
                "ok": True,
                "reason_code": "chatgpt_submission_verified",
                "status": {"composer_contains_marker": False, "submitted_candidate_count": 1},
            }

        clock = {"now": 0.0}

        def monotonic() -> float:
            value = clock["now"]
            clock["now"] += 0.1
            return value

        result = submit_feedback_to_chatgpt_service(
            "run-1",
            _submission_run(),
            ledger=ledger,
            clipboard_copy_function=copy_function,
            activation_function=activate_function,
            submission_ui_inspection_function=inspection_function,
            paste_function=paste_function,
            ax_send_button_function=ax_send_function,
            enter_function=enter_function,
            artifact_writer=artifact_writer,
            monotonic_function=monotonic,
            sleep_function=lambda seconds: None,
            paste_verify_timeout_seconds=None,
            submission_verify_timeout_seconds=None,
            submission_verification_function=verification_function,
            **kwargs,
        )
        return result, ledger, copied_texts, send_calls, enter_calls

    def test_success_writes_golden_event_order_and_returns_marker_metadata(self) -> None:
        result, ledger, copied_texts, send_calls, enter_calls = self._service()

        self.assertTrue(result.ok)
        self.assertEqual(
            [event["event_type"] for event in ledger.added_events],
            [
                "gpt_feedback_generated",
                "gpt_feedback_copied",
                "gpt_feedback_pasted",
                "gpt_feedback_submit_input_sent",
                "gpt_feedback_submission_verified",
            ],
        )
        self.assertNotIn("gpt_feedback_submission_failed", [event["event_type"] for event in ledger.added_events])
        self.assertNotIn("gpt_feedback_submission_ambiguous", [event["event_type"] for event in ledger.added_events])
        self.assertEqual(copied_texts, [result.feedback_message])
        self.assertTrue(str(result.feedback_message).startswith("AGENT_SUBMISSION\n"))
        self.assertIn("Clean final assistant message.", str(result.feedback_message))
        self.assertNotIn("stdout\n", str(result.feedback_message))
        self.assertEqual(send_calls, ["FW.2"])
        self.assertEqual(enter_calls, [])
        verified = ledger.added_events[-1]
        metadata = verified["metadata"]
        self.assertEqual(result.event_type, "gpt_feedback_submission_verified")
        self.assertEqual(result.verified_event_id, verified["id"])
        self.assertEqual(result.metadata, metadata)
        self.assertEqual(result.reason_code, "chatgpt_submission_verified")
        self.assertEqual(result.submission_marker_text, metadata["submission_marker_text"])
        self.assertEqual(result.submission_marker_sha256, metadata["submission_marker_sha256"])
        self.assertEqual(result.submission_nonce, metadata["submission_marker_nonce"])
        self.assertEqual(result.feedback_payload_sha256, metadata["feedback_payload_sha256"])
        self.assertEqual(result.feedback_payload_length, metadata["feedback_payload_length"])
        self.assertEqual(ledger.status_updates, [])

    def test_non_submittable_feedback_fails_before_copy_paste_or_submit(self) -> None:
        oversized = "x" * (MAX_CLEAN_FINAL_MESSAGE_CHARS + 1)
        ledger = FakeLedger(
            _submission_base_events(stdout="raw stdout must stay local\n", stderr="raw stderr must stay local\n")
        )
        metadata = json.loads(ledger.events[0]["metadata_json"])
        metadata.update(
            {
                "final_message": oversized,
                "final_message_length": len(oversized),
                "final_message_status": "valid",
            }
        )
        ledger.events[0]["metadata_json"] = json.dumps(metadata, sort_keys=True)

        result, ledger, copied_texts, send_calls, enter_calls = self._service(ledger=ledger)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "codex_final_message_oversize")
        self.assertIsNone(result.feedback_message)
        self.assertEqual(copied_texts, [])
        self.assertEqual(send_calls, [])
        self.assertEqual(enter_calls, [])
        self.assertEqual([event["event_type"] for event in ledger.added_events], ["gpt_feedback_generation_failed"])
        failure_metadata = ledger.added_events[0]["metadata"]
        self.assertTrue(failure_metadata["clipboard_copy_skipped"])
        self.assertEqual(failure_metadata["feedback_payload_length"], 0)
        self.assertNotIn(oversized, json.dumps(failure_metadata))

    def test_output_artifact_write_happens_before_generated_event_and_errors_before_events(self) -> None:
        operations: list[str] = []
        writes: list[tuple[str, str]] = []

        def artifact_writer(path_text: str, message: str) -> Path:
            operations.append("artifact")
            writes.append((path_text, message))
            return Path(path_text)

        result, ledger, _copied, _send_calls, _enter_calls = self._service(
            operations=operations,
            artifact_writer=artifact_writer,
            output_path_text="out.txt",
        )

        self.assertTrue(result.ok)
        self.assertEqual(operations[0], "artifact")
        self.assertEqual(operations[1], "event")
        self.assertEqual(writes, [("out.txt", result.feedback_message)])
        self.assertEqual(ledger.added_events[0]["metadata"]["output_path"], "out.txt")

        failing_ledger = FakeLedger(_submission_base_events())

        def failing_writer(path_text: str, message: str) -> Path:
            raise OSError("disk full")

        with self.assertRaises(OSError):
            submit_feedback_to_chatgpt_service(
                "run-1",
                _submission_run(),
                ledger=failing_ledger,
                artifact_writer=failing_writer,
                output_path_text="out.txt",
            )
        self.assertEqual(failing_ledger.added_events, [])

    def test_pre_send_failures_preserve_event_order_and_do_not_send(self) -> None:
        cases = [
            (
                "copy",
                {
                    "copy_result": {"copied": False, "method": "pbcopy", "error": "copy failed"},
                },
                ["gpt_feedback_generated", "gpt_feedback_copied", "gpt_feedback_pasted", "gpt_feedback_submission_failed"],
                "clipboard_copy_failed",
            ),
            (
                "not_frontmost",
                {"activation_result": _submission_activation(False)},
                ["gpt_feedback_generated", "gpt_feedback_copied", "gpt_feedback_pasted", "gpt_feedback_submission_failed"],
                "chatgpt_not_frontmost",
            ),
            (
                "composer_not_found",
                {
                    "inspection_function": lambda app_name, marker_text=None: _submission_observation(
                        str(marker_text), ok=True, composer=False, text_input_candidates=False
                    )
                },
                ["gpt_feedback_generated", "gpt_feedback_copied", "gpt_feedback_submission_failed"],
                "chatgpt_composer_not_found",
            ),
            (
                "composer_not_focused",
                {
                    "inspection_function": lambda app_name, marker_text=None: _submission_observation(
                        str(marker_text), ok=True, composer=False, text_input_candidates=True
                    )
                },
                ["gpt_feedback_generated", "gpt_feedback_copied", "gpt_feedback_submission_failed"],
                "chatgpt_composer_not_focused",
            ),
            (
                "paste_failed",
                {"paste_result": {"pasted": False, "method": "paste", "error": "paste failed"}},
                ["gpt_feedback_generated", "gpt_feedback_copied", "gpt_feedback_pasted", "gpt_feedback_submission_failed"],
                "chatgpt_paste_input_failed",
            ),
        ]

        for name, options, event_order, reason in cases:
            with self.subTest(name):
                result, ledger, _copied, send_calls, enter_calls = self._service(**options)
                self.assertFalse(result.ok)
                self.assertEqual([event["event_type"] for event in ledger.added_events], event_order)
                self.assertEqual(send_calls, [])
                self.assertEqual(enter_calls, [])
                self.assertIsNone(result.verified_event_id)
                self.assertIsNone(result.ambiguous_event_id)
                self.assertEqual(result.failure_event_id, ledger.added_events[-1]["id"])
                self.assertEqual(result.reason_code, reason)
                self.assertEqual(ledger.added_events[-1]["metadata"]["reason_code"], reason)
                self.assertEqual(ledger.status_updates, [])

    def test_post_send_not_verified_writes_failure_without_verified_event(self) -> None:
        result, ledger, _copied, _send_calls, enter_calls = self._service(
            inspection_function=self._enter_inspection_function(),
            verification_function=lambda app_name, marker_text: {
                "ok": False,
                "reason_code": "chatgpt_submission_not_verified",
                "status": {"composer_contains_marker": True, "submitted_candidate_count": 0},
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(enter_calls, ["enter"])
        self.assertEqual(ledger.added_events[-1]["event_type"], "gpt_feedback_submission_failed")
        self.assertEqual(result.reason_code, "chatgpt_submission_not_verified")
        self.assertIsNone(result.verified_event_id)
        self.assertEqual(result.failure_event_id, ledger.added_events[-1]["id"])

    def test_post_send_submitted_marker_not_found_writes_failure(self) -> None:
        result, ledger, _copied, _send_calls, _enter_calls = self._service(
            verification_function=lambda app_name, marker_text: {
                "ok": False,
                "reason_code": "chatgpt_submission_not_verified",
                "status": {"composer_contains_marker": False, "submitted_candidate_count": 0},
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(ledger.added_events[-1]["event_type"], "gpt_feedback_submission_failed")
        self.assertEqual(ledger.added_events[-1]["metadata"]["reason_code"], "chatgpt_submission_not_verified")
        self.assertIsNone(result.verified_event_id)

    def test_post_send_ambiguous_marker_writes_ambiguous_terminal_event(self) -> None:
        result, ledger, _copied, _send_calls, _enter_calls = self._service(
            verification_function=lambda app_name, marker_text: {
                "ok": False,
                "reason_code": "chatgpt_submission_ambiguous",
                "status": {"composer_contains_marker": True, "submitted_candidate_count": 1},
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(ledger.added_events[-1]["event_type"], "gpt_feedback_submission_ambiguous")
        self.assertEqual(result.ambiguous_event_id, ledger.added_events[-1]["id"])
        self.assertIsNone(result.failure_event_id)
        self.assertIsNone(result.verified_event_id)

    def test_duplicate_marker_candidates_are_ambiguous_with_default_verifier(self) -> None:
        calls = {"count": 0}

        def inspect(app_name: str, marker_text: str | None = None) -> dict:
            marker = str(marker_text)
            calls["count"] += 1
            if calls["count"] == 1:
                return _submission_observation(marker, composer_text="")
            if calls["count"] == 2:
                return _submission_observation(marker, composer_text=marker)
            if calls["count"] == 3:
                return _submission_observation(marker, composer_text=marker)
            return _submission_observation(marker, composer_text="", candidates=[marker, marker])

        result, ledger, _copied, _send_calls, enter_calls = self._service(
            inspection_function=inspect,
            use_default_submission_verifier=True,
            enter_result={"submitted": True, "method": "enter", "error": None},
        )

        self.assertFalse(result.ok)
        self.assertEqual(enter_calls, ["enter"])
        self.assertEqual(ledger.added_events[-1]["event_type"], "gpt_feedback_submission_ambiguous")
        self.assertEqual(result.reason_code, "chatgpt_submission_ambiguous")

    def test_fallback_enter_is_limited_to_one_attempt_and_can_verify(self) -> None:
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

        def verify(app_name: str, marker_text: str) -> dict:
            return verification_results.pop(0)

        result, ledger, _copied, send_calls, enter_calls = self._service(
            ax_send_result={"pressed": True, "method": "macos_accessibility_axpress_send_button", "error": None},
            verification_function=verify,
        )

        self.assertTrue(result.ok)
        self.assertEqual(send_calls, ["FW.2"])
        self.assertEqual(enter_calls, ["enter"])
        self.assertEqual(
            [event["event_type"] for event in ledger.added_events],
            [
                "gpt_feedback_generated",
                "gpt_feedback_copied",
                "gpt_feedback_pasted",
                "gpt_feedback_submit_input_sent",
                "gpt_feedback_submit_input_sent",
                "gpt_feedback_submission_verified",
            ],
        )
        self.assertEqual(ledger.added_events[-1]["metadata"]["fallback_attempt_count"], 1)
        self.assertEqual(verification_results, [])

    def test_fallback_enter_failure_does_not_verify_or_retry_again(self) -> None:
        verify_calls = []

        def verify(app_name: str, marker_text: str) -> dict:
            verify_calls.append("verify")
            return {
                "ok": False,
                "reason_code": "chatgpt_submission_not_verified",
                "status": {"composer_contains_marker": True, "submitted_candidate_count": 0},
            }

        result, ledger, _copied, _send_calls, enter_calls = self._service(
            verification_function=verify,
            enter_result={"submitted": False, "method": "enter", "error": "enter failed"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(enter_calls, ["enter"])
        self.assertEqual(verify_calls, ["verify"])
        self.assertEqual([event["event_type"] for event in ledger.added_events].count("gpt_feedback_submit_input_sent"), 2)
        self.assertEqual(ledger.added_events[-1]["event_type"], "gpt_feedback_submission_failed")
        self.assertEqual(ledger.added_events[-1]["metadata"]["fallback_attempt_count"], 1)

    def test_default_submission_verifier_is_bounded_when_never_submitted(self) -> None:
        # Regression: previously the verifier ignored its timeout and looped
        # forever when a paste never actually submitted, hanging the handoff and
        # holding the UI lease. It must now return not-verified after a bounded
        # poll budget.
        from agent.chatgpt_services import _verify_submission_marker

        marker = "AGENT_SUBMISSION\nrun_id=r\nnonce=n"
        calls = {"count": 0}

        def inspect(app_name: str, marker_text: str | None = None) -> dict:
            calls["count"] += 1
            return _submission_observation(str(marker_text), composer_text=str(marker_text))

        result = _verify_submission_marker(
            "ChatGPT",
            marker,
            inspection_function=inspect,
            monotonic_function=lambda: 0.0,
            sleep_function=lambda _seconds: None,
            timeout_seconds=None,
            poll_interval_seconds=0.0,
            max_polls=3,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "chatgpt_submission_not_verified")
        self.assertEqual(result["poll_count"], 3)
        self.assertEqual(calls["count"], 3)
        self.assertTrue(result["status"]["composer_contains_marker"])

    def test_send_selection_skips_message_action_button_and_uses_enter(self) -> None:
        # Regression: the run pressed a transcript reply control ("Memory
        # updated" with Good/Bad response actions) instead of the composer Send
        # button, so nothing submitted. Such controls must be rejected in favor
        # of Return.
        from agent.chatgpt_services import _select_send_input_method

        observation = _submission_observation("m", composer_text="m")
        observation["send_button"] = {
            "path": "FW.9",
            "role": "AXButton",
            "description": "Memory updated",
            "actions": ["AXShowMenu", "AXPress", "Name:Good response", "Name:Read Aloud"],
        }
        send_calls: list[str] = []
        enter_calls: list[str] = []

        result, _summary = _select_send_input_method(
            "ChatGPT",
            "m",
            inspection_function=lambda app, marker_text=None: observation,
            ax_send_button_function=lambda app, path: send_calls.append(path)
            or {"pressed": True, "method": "macos_accessibility_axpress_send_button", "path": path},
            enter_function=lambda: enter_calls.append("enter")
            or {"submitted": True, "method": "enter", "error": None},
        )

        self.assertEqual(send_calls, [])
        self.assertEqual(enter_calls, ["enter"])
        self.assertEqual(result["method"], "enter")

    def test_send_selection_uses_valid_send_button(self) -> None:
        from agent.chatgpt_services import _select_send_input_method

        observation = _submission_observation("m", composer_text="m", send_button=True)
        send_calls: list[str] = []

        result, _summary = _select_send_input_method(
            "ChatGPT",
            "m",
            inspection_function=lambda app, marker_text=None: observation,
            ax_send_button_function=lambda app, path: send_calls.append(path)
            or {"pressed": True, "method": "macos_accessibility_axpress_send_button", "path": path},
            enter_function=lambda: {"submitted": True, "method": "enter", "error": None},
        )

        self.assertEqual(send_calls, ["FW.2"])
        self.assertEqual(result["method"], "macos_accessibility_axpress_send_button")

    def test_default_verifier_fails_closed_when_submission_never_registers(self) -> None:
        # End-to-end: paste succeeds, the marker sits in the composer, but the
        # submit never registers. The service must fall back to Enter once and
        # then fail closed with a terminal event (not hang).
        def inspect(app_name: str, marker_text: str | None = None) -> dict:
            marker = str(marker_text)
            if inspect.calls == 0:
                inspect.calls += 1
                return _submission_observation(marker, composer_text="", send_button=True)
            inspect.calls += 1
            return _submission_observation(marker, composer_text=marker, send_button=True)

        inspect.calls = 0

        result, ledger, _copied, send_calls, enter_calls = self._service(
            inspection_function=inspect,
            use_default_submission_verifier=True,
            ax_send_result={"pressed": True, "method": "macos_accessibility_axpress_send_button", "error": None},
            enter_result={"submitted": True, "method": "enter", "error": None},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chatgpt_submission_not_verified")
        self.assertEqual(ledger.added_events[-1]["event_type"], "gpt_feedback_submission_failed")
        self.assertEqual(send_calls, ["FW.2"])
        self.assertEqual(enter_calls, ["enter"])

    def test_send_waits_until_draft_marker_verification(self) -> None:
        calls = {"count": 0}

        def inspect(app_name: str, marker_text: str | None = None) -> dict:
            marker = str(marker_text)
            calls["count"] += 1
            if calls["count"] <= 3:
                return _submission_observation(marker, composer_text="")
            return _submission_observation(marker, composer_text=marker, send_button=True)

        result, ledger, _copied, send_calls, enter_calls = self._service(
            inspection_function=inspect,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.reason_code, "chatgpt_submission_verified")
        self.assertEqual(send_calls, ["FW.2"])
        self.assertEqual(enter_calls, [])
        self.assertIn("gpt_feedback_submit_input_sent", [event["event_type"] for event in ledger.added_events])

    def test_no_verified_event_without_submitted_marker_verification(self) -> None:
        result, ledger, _copied, _send_calls, _enter_calls = self._service(
            verification_function=lambda app_name, marker_text: {
                "ok": False,
                "reason_code": "chatgpt_submission_not_verified",
                "status": {"composer_contains_marker": False, "submitted_candidate_count": 0},
            }
        )

        self.assertFalse(result.ok)
        self.assertNotIn("gpt_feedback_submission_verified", [event["event_type"] for event in ledger.added_events])

    def test_unexpected_exception_does_not_add_invented_terminal_event(self) -> None:
        ledger = FakeLedger(_submission_base_events())

        def paste_raises() -> dict:
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            submit_feedback_to_chatgpt_service(
                "run-1",
                _submission_run(),
                ledger=ledger,
                clipboard_copy_function=lambda text: {"copied": True, "method": "pbcopy", "error": None},
                activation_function=lambda app_name: _submission_activation(True),
                submission_ui_inspection_function=lambda app_name, marker_text=None: _submission_observation(
                    str(marker_text), composer_text=""
                ),
                paste_function=paste_raises,
            )

        self.assertEqual(
            [event["event_type"] for event in ledger.added_events],
            ["gpt_feedback_generated", "gpt_feedback_copied"],
        )

    def test_response_capture_can_use_verified_event_anchor(self) -> None:
        result, ledger, _copied, _send_calls, _enter_calls = self._service()
        self.assertTrue(result.ok)
        capture_result = capture_chatgpt_response_service(
            "run-1",
            ledger=ledger,
            activation_function=lambda app_name: _submission_activation(True),
            capture_function=lambda *args, **kwargs: _successful_capture_result("assistant response"),
        )

        self.assertTrue(capture_result.ok)
        self.assertEqual(capture_result.submission_event_id, result.verified_event_id)
        self.assertEqual(capture_result.matched_submission_marker_details["submission_marker_text"], result.submission_marker_text)

    def _enter_inspection_function(self):
        calls = {"count": 0}

        def inspect(app_name: str, marker_text: str | None = None) -> dict:
            marker = str(marker_text)
            calls["count"] += 1
            if calls["count"] == 1:
                return _submission_observation(marker, composer_text="")
            if calls["count"] == 2:
                return _submission_observation(marker, composer_text=marker)
            return _submission_observation(marker, composer_text=marker, send_button=False)

        return inspect


class CaptureChatGPTResponseServiceTests(unittest.TestCase):
    def test_success_writes_started_then_captured_and_returns_metadata(self) -> None:
        ledger = FakeLedger([_verified_submission_with_marker()])
        capture_result = _successful_capture_result(_sentinel_response("Say exactly: captured"))
        capture_calls: list[dict] = []

        def capture_function(*args, **kwargs):
            capture_calls.append({"args": args, "kwargs": kwargs})
            return capture_result

        result = capture_chatgpt_response_service(
            "run-1",
            timeout_seconds=None,
            stable_seconds=2.0,
            require_sentinel_response=True,
            ledger=ledger,
            activation_function=lambda app_name: _activation(True),
            capture_function=capture_function,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.persisted)
        self.assertEqual(result.reason_code, "complete_sentinel_stable")
        self.assertEqual(result.event_type, GPT_RESPONSE_CAPTURED_EVENT_TYPE)
        self.assertEqual(result.submission_event_id, 1)
        self.assertEqual(result.capture_started_event_id, 2)
        self.assertEqual(result.capture_event_id, 3)
        self.assertIsNone(result.failure_event_id)
        self.assertEqual(result.captured_response_text, capture_result["response_text"])
        self.assertEqual(result.captured_response_sha256, capture_result["response_sha256"])
        self.assertEqual(result.sentinel_state, "complete_sentinel_stable")
        self.assertTrue(result.stable)
        self.assertEqual(result.candidate_summaries, capture_result["post_feedback_candidate_summaries"])
        self.assertEqual(result.raw_capture_result, capture_result)
        self.assertEqual(
            [event["event_type"] for event in ledger.added_events],
            [GPT_RESPONSE_CAPTURE_STARTED_EVENT_TYPE, GPT_RESPONSE_CAPTURED_EVENT_TYPE],
        )
        self.assertEqual(ledger.added_events[0]["message"], "Assistant response capture started after verified ChatGPT submission.")
        self.assertEqual(ledger.added_events[1]["message"], "Captured GPT response from ChatGPT desktop accessibility tree.")
        self.assertEqual(capture_calls[0]["args"], ("",))
        self.assertIsNone(capture_calls[0]["kwargs"]["timeout_seconds"])
        self.assertEqual(capture_calls[0]["kwargs"]["submission_marker_text"], CAPTURE_MARKER)
        self.assertTrue(capture_calls[0]["kwargs"]["require_sentinel_response"])
        started_metadata = ledger.added_events[0]["metadata"]
        self.assertEqual(started_metadata["matched_submission_event_id"], 1)
        self.assertEqual(started_metadata["source_event_type"], "gpt_feedback_submission_verified")
        self.assertEqual(started_metadata["submission_marker_text"], CAPTURE_MARKER)
        self.assertEqual(started_metadata["sentinel_required"], True)
        captured_metadata = ledger.added_events[1]["metadata"]
        self.assertEqual(captured_metadata["response_text"], capture_result["response_text"])
        self.assertEqual(captured_metadata["response_sha256"], capture_result["response_sha256"])
        self.assertEqual(captured_metadata["matched_submission_event_id"], 1)
        self.assertEqual(captured_metadata["sentinel_state"], "complete_sentinel_stable")
        self.assertEqual(ledger.status_updates, [])

    def test_pre_start_failures_write_no_capture_events(self) -> None:
        cases = [
            ("no verified", [], "no_verified_submission"),
            (
                "missing marker",
                [_verified_submission_with_marker(submission_marker_text=None, submission_marker_sha256=None)],
                "missing_submission_marker",
            ),
            (
                "marker sha mismatch",
                [_verified_submission_with_marker(submission_marker_sha256="wrong")],
                "submission_marker_sha_mismatch",
            ),
        ]
        for _name, events, reason_code in cases:
            with self.subTest(_name):
                ledger = FakeLedger(events)
                result = capture_chatgpt_response_service(
                    "run-1",
                    ledger=ledger,
                    activation_function=lambda app_name: _activation(True),
                    capture_function=lambda *args, **kwargs: self.fail("capture should not start"),
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason_code)
                self.assertFalse(result.persisted)
                self.assertIsNone(result.capture_started_event_id)
                self.assertIsNone(result.capture_event_id)
                self.assertIsNone(result.failure_event_id)
                self.assertEqual(ledger.added_events, [])
                self.assertEqual(ledger.status_updates, [])

    def test_not_frontmost_is_structured_pre_start_failure_without_events(self) -> None:
        ledger = FakeLedger([_verified_submission_with_marker()])

        result = capture_chatgpt_response_service(
            "run-1",
            ledger=ledger,
            activation_function=lambda app_name: _activation(False),
            capture_function=lambda *args, **kwargs: self.fail("capture should not start"),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chatgpt_not_frontmost")
        self.assertEqual(result.activation_result, _activation(False))
        self.assertEqual(result.raw_capture_result["candidate_count"], 0)
        self.assertEqual(result.raw_capture_result["error"], "Expected frontmost app 'ChatGPT', got 'Finder'.")
        self.assertEqual(ledger.added_events, [])
        self.assertEqual(ledger.status_updates, [])

    def test_post_start_failures_write_started_then_one_failure(self) -> None:
        cases = [
            ("stable malformed sentinel", "sentinel_malformed_stable", "stable_malformed_sentinel"),
            ("multiple complete sentinels", "multiple_complete_sentinels", "multiple_complete_sentinels"),
        ]
        for _name, reason_code, sentinel_state in cases:
            with self.subTest(_name):
                ledger = FakeLedger([_verified_submission_with_marker()])
                capture_result = _failed_capture_result(reason_code, sentinel_state, error=_name)

                result = capture_chatgpt_response_service(
                    "run-1",
                    require_sentinel_response=True,
                    ledger=ledger,
                    activation_function=lambda app_name: _activation(True),
                    capture_function=lambda *args, **kwargs: capture_result,
                )

                self.assertFalse(result.ok)
                self.assertTrue(result.persisted)
                self.assertEqual(result.reason_code, reason_code)
                self.assertEqual(result.error_message, _name)
                self.assertEqual(result.event_type, GPT_RESPONSE_CAPTURE_FAILED_EVENT_TYPE)
                self.assertEqual(result.capture_started_event_id, 2)
                self.assertEqual(result.failure_event_id, 3)
                self.assertIsNone(result.capture_event_id)
                self.assertEqual(
                    [event["event_type"] for event in ledger.added_events],
                    [GPT_RESPONSE_CAPTURE_STARTED_EVENT_TYPE, GPT_RESPONSE_CAPTURE_FAILED_EVENT_TYPE],
                )
                self.assertNotIn(GPT_RESPONSE_CAPTURED_EVENT_TYPE, [event["event_type"] for event in ledger.added_events])
                failure_metadata = ledger.added_events[1]["metadata"]
                self.assertEqual(failure_metadata["reason_code"], reason_code)
                self.assertEqual(failure_metadata["error"], _name)
                self.assertEqual(failure_metadata["sentinel_state"], sentinel_state)
                self.assertEqual(failure_metadata["matched_submission_event_id"], 1)
                self.assertEqual(failure_metadata["candidate_count"], 2)
                self.assertEqual(failure_metadata["post_feedback_candidate_summaries"], capture_result["post_feedback_candidate_summaries"])
                self.assertEqual(failure_metadata["stability"]["stable"], False)
                self.assertEqual(failure_metadata["ax_stats"], {"candidate_count": 2})
                self.assertEqual(result.candidate_summaries, capture_result["post_feedback_candidate_summaries"])
                self.assertEqual(ledger.status_updates, [])

    def test_unexpected_capture_exception_bubbles_without_terminal_failure_event(self) -> None:
        ledger = FakeLedger([_verified_submission_with_marker()])

        def capture_function(*args, **kwargs):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            capture_chatgpt_response_service(
                "run-1",
                ledger=ledger,
                activation_function=lambda app_name: _activation(True),
                capture_function=capture_function,
            )

        self.assertEqual(
            [event["event_type"] for event in ledger.added_events],
            [GPT_RESPONSE_CAPTURE_STARTED_EVENT_TYPE],
        )
        self.assertEqual(ledger.status_updates, [])

    def test_cli_command_pre_start_failure_keeps_existing_output_and_exit(self) -> None:
        ledger = FakeLedger([])
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli, "ledger", ledger),
            mock.patch.object(sys, "argv", ["agent-loop", "capture-gpt-response-from-chatgpt-ax", "run-1", "--confirm-capture"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_context,
        ):
            cli.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "Stopped: no verified ChatGPT submission was found for this run.\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(ledger.added_events, [])


class ExtractNextCodexPromptServiceTests(unittest.TestCase):
    def _ledger_for_response(self, response: str) -> FakeLedger:
        submission = _submission()
        return FakeLedger([submission, _capture(submission, response)])

    def test_confirmed_sentinel_extraction_writes_artifact_then_one_event(self) -> None:
        prompt = "Say exactly: extracted"
        operations: list[str] = []
        submission = _submission()
        ledger = FakeLedger([submission, _capture(submission, _sentinel_response(prompt))], operations)
        writer = RecordingArtifactWriter(operations=operations)

        result = extract_next_codex_prompt_service(
            "run-1",
            require_sentinel=True,
            confirm_extract=True,
            ledger=ledger,
            artifact_writer=writer,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.persisted)
        self.assertTrue(result.artifact_written)
        self.assertEqual(writer.writes, [(Path("data") / "runs" / "run-1" / "next_codex_prompt.md", prompt)])
        self.assertEqual(operations, ["artifact", "event"])
        self.assertEqual(len(ledger.added_events), 1)
        event = ledger.added_events[0]
        self.assertEqual(event["event_type"], NEXT_CODEX_PROMPT_EXTRACTED_EVENT_TYPE)
        self.assertEqual(event["message"], NEXT_CODEX_PROMPT_EXTRACTED_MESSAGE)
        self.assertEqual(result.event_type, NEXT_CODEX_PROMPT_EXTRACTED_EVENT_TYPE)
        self.assertEqual(result.event_id, event["id"])
        self.assertEqual(result.output_path, Path("data") / "runs" / "run-1" / "next_codex_prompt.md")
        metadata = event["metadata"]
        self.assertEqual(metadata["source_event_id"], 2)
        self.assertEqual(metadata["source_event_type"], "gpt_response_captured")
        self.assertEqual(metadata["source_response_sha256"], _sha(_sentinel_response(prompt)))
        self.assertEqual(metadata["matched_submission_event_id"], 1)
        self.assertEqual(metadata["extraction_method"], "sentinel_block")
        self.assertEqual(metadata["prompt_text"], prompt)
        self.assertEqual(metadata["prompt_path"], "data/runs/run-1/next_codex_prompt.md")
        self.assertEqual(metadata["prompt_length"], len(prompt))
        self.assertEqual(metadata["prompt_sha256"], _sha(prompt))
        self.assertEqual(metadata["prompt_count_detected"], 1)
        self.assertEqual(metadata["selected_prompt_index"], 0)
        self.assertEqual(metadata["safety_status"], "requires_human_review")
        self.assertEqual(metadata["warnings"], [])
        self.assertEqual(result.metadata, metadata)

    def test_preview_only_extraction_does_not_write_or_persist(self) -> None:
        prompt = "Say exactly: preview"
        ledger = self._ledger_for_response(_sentinel_response(prompt))
        writer = RecordingArtifactWriter()

        result = extract_next_codex_prompt_service(
            "run-1",
            require_sentinel=False,
            confirm_extract=False,
            ledger=ledger,
            artifact_writer=writer,
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.persisted)
        self.assertFalse(result.artifact_written)
        self.assertEqual(result.prompt_text, prompt)
        self.assertIsNone(result.output_path)
        self.assertIsNone(result.event_type)
        self.assertEqual(writer.writes, [])
        self.assertEqual(ledger.added_events, [])

    def test_labeled_fallback_allowed_only_when_sentinel_not_required(self) -> None:
        prompt = "Say exactly: fenced"
        ledger = self._ledger_for_response(_labeled_response(prompt))
        writer = RecordingArtifactWriter()

        allowed = extract_next_codex_prompt_service(
            "run-1",
            require_sentinel=False,
            confirm_extract=True,
            ledger=ledger,
            artifact_writer=writer,
        )

        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.extraction_method, "labeled_fenced_code_block")
        self.assertEqual(writer.writes, [(Path("data") / "runs" / "run-1" / "next_codex_prompt.md", prompt)])
        self.assertEqual(len(ledger.added_events), 1)

        rejecting_ledger = self._ledger_for_response(_labeled_response(prompt))
        rejecting_writer = RecordingArtifactWriter()
        rejected = extract_next_codex_prompt_service(
            "run-1",
            require_sentinel=True,
            confirm_extract=True,
            ledger=rejecting_ledger,
            artifact_writer=rejecting_writer,
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.reason_code, "sentinel_required")
        self.assertFalse(rejected.persisted)
        self.assertFalse(rejected.artifact_written)
        self.assertEqual(rejecting_writer.writes, [])
        self.assertEqual(rejecting_ledger.added_events, [])

    def test_selection_failure_does_not_write_event_or_artifact(self) -> None:
        cases = [
            ("no verified submission", []),
            ("no matching capture", [_submission()]),
        ]
        for _name, events in cases:
            with self.subTest(_name):
                ledger = FakeLedger(events)
                writer = RecordingArtifactWriter()

                result = extract_next_codex_prompt_service(
                    "run-1",
                    require_sentinel=True,
                    confirm_extract=True,
                    ledger=ledger,
                    artifact_writer=writer,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "no_valid_captured_response")
                self.assertFalse(result.persisted)
                self.assertFalse(result.artifact_written)
                self.assertEqual(writer.writes, [])
                self.assertEqual(ledger.added_events, [])
                self.assertEqual(ledger.status_updates, [])

    def test_parser_failures_do_not_write_event_or_artifact(self) -> None:
        cases = [
            ("malformed reversed", "END_NEXT_CODEX_PROMPT\nBEGIN_NEXT_CODEX_PROMPT\nprompt"),
            ("begin without end", "BEGIN_NEXT_CODEX_PROMPT\nprompt"),
            (
                "multiple complete",
                "BEGIN_NEXT_CODEX_PROMPT\nA\nEND_NEXT_CODEX_PROMPT\n"
                "BEGIN_NEXT_CODEX_PROMPT\nB\nEND_NEXT_CODEX_PROMPT",
            ),
            ("empty prompt", "BEGIN_NEXT_CODEX_PROMPT\n\nEND_NEXT_CODEX_PROMPT"),
        ]
        for _name, response in cases:
            with self.subTest(_name):
                ledger = self._ledger_for_response(response)
                writer = RecordingArtifactWriter()

                result = extract_next_codex_prompt_service(
                    "run-1",
                    require_sentinel=True,
                    confirm_extract=True,
                    ledger=ledger,
                    artifact_writer=writer,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "extraction_failed")
                self.assertFalse(result.persisted)
                self.assertFalse(result.artifact_written)
                self.assertEqual(writer.writes, [])
                self.assertEqual(ledger.added_events, [])
                self.assertEqual(ledger.status_updates, [])

    def test_artifact_writer_failure_does_not_append_event(self) -> None:
        ledger = self._ledger_for_response(_sentinel_response())
        writer = RecordingArtifactWriter(fail=True)

        result = extract_next_codex_prompt_service(
            "run-1",
            require_sentinel=True,
            confirm_extract=True,
            output_path_text="~/next.md",
            ledger=ledger,
            artifact_writer=writer,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact_write_failed")
        self.assertEqual(result.error_message, "disk full")
        self.assertFalse(result.persisted)
        self.assertFalse(result.artifact_written)
        self.assertEqual(writer.writes, [(Path("~/next.md").expanduser(), "Say exactly: extracted")])
        self.assertEqual(ledger.added_events, [])
        self.assertEqual(ledger.status_updates, [])

    def test_explicit_output_path_creates_parent_overwrites_and_preserves_exact_utf8_text(self) -> None:
        prompt = "Say exactly: cafe\nwithout extra newline"
        ledger = self._ledger_for_response(_sentinel_response(prompt))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "nested" / "next.md"
            output_path.parent.mkdir(parents=True)
            output_path.write_text("old content\n", encoding="utf-8")

            result = extract_next_codex_prompt_service(
                "run-1",
                require_sentinel=True,
                confirm_extract=True,
                output_path_text=str(output_path),
                ledger=ledger,
            )

            self.assertTrue(result.ok)
            self.assertTrue(result.persisted)
            self.assertEqual(result.output_path, output_path)
            self.assertEqual(output_path.read_text(encoding="utf-8"), prompt)
            self.assertFalse(output_path.read_text(encoding="utf-8").endswith("\n"))
            self.assertEqual(ledger.added_events[0]["metadata"]["prompt_path"], str(output_path))

    def test_explicit_output_path_creates_missing_parents(self) -> None:
        prompt = "Say exactly: parent dirs"
        ledger = self._ledger_for_response(_sentinel_response(prompt))
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "missing" / "parents" / "next.md"

            result = extract_next_codex_prompt_service(
                "run-1",
                require_sentinel=True,
                confirm_extract=True,
                output_path_text=str(output_path),
                ledger=ledger,
            )

            self.assertTrue(result.ok)
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_text(encoding="utf-8"), prompt)

    def test_one_service_call_writes_one_artifact_and_one_event(self) -> None:
        ledger = self._ledger_for_response(_sentinel_response())
        writer = RecordingArtifactWriter()

        extract_next_codex_prompt_service(
            "run-1",
            require_sentinel=True,
            confirm_extract=True,
            ledger=ledger,
            artifact_writer=writer,
        )

        self.assertEqual(len(writer.writes), 1)
        self.assertEqual(len(ledger.added_events), 1)


class ExtractNextCodexPromptCliCompatibilityTests(unittest.TestCase):
    def test_cli_preview_output_succeeds_without_event_or_artifact(self) -> None:
        prompt = "Say exactly: preview"
        submission = _submission()
        ledger = FakeLedger([submission, _capture(submission, _sentinel_response(prompt))])
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli, "ledger", ledger),
            mock.patch.object(sys, "argv", ["agent-loop", "extract-next-codex-prompt", "run-1"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_context,
        ):
            cli.main()

        self.assertEqual(exit_context.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        output = stdout.getvalue()
        self.assertIn("run_id: run-1", output)
        self.assertIn("source_event_id: 2", output)
        self.assertIn("matched_submission_event_id: 1", output)
        self.assertIn("extraction_method: sentinel_block", output)
        self.assertIn("prompt_found: true", output)
        self.assertIn("output_path: \n", output)
        self.assertIn("ledger_event: \n", output)
        self.assertIn("No Codex execution was performed.", output)
        self.assertEqual(ledger.added_events, [])

    def test_cli_wrapper_preserves_artifact_write_oserror_behavior(self) -> None:
        result = ExtractNextCodexPromptServiceResult(
            ok=False,
            run_id="run-1",
            reason_code="artifact_write_failed",
            error_message="disk full",
        )

        with mock.patch.object(cli, "extract_next_codex_prompt_service", return_value=result):
            with self.assertRaises(OSError) as error_context:
                cli._extract_next_codex_prompt_flow("run-1", confirm_extract=True)

        self.assertEqual(str(error_context.exception), "disk full")

    def test_cli_command_maps_artifact_write_failure_to_existing_exit_message(self) -> None:
        result = ExtractNextCodexPromptServiceResult(
            ok=False,
            run_id="run-1",
            reason_code="artifact_write_failed",
            error_message="disk full",
        )
        ledger = FakeLedger([])
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli, "ledger", ledger),
            mock.patch.object(cli, "extract_next_codex_prompt_service", return_value=result),
            mock.patch.object(sys, "argv", ["agent-loop", "extract-next-codex-prompt", "run-1", "--confirm-extract"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_context,
        ):
            cli.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Failed to write extracted Codex prompt output: disk full\n")
        self.assertEqual(ledger.added_events, [])


if __name__ == "__main__":
    unittest.main()
