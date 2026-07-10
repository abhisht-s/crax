from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from agent import cli
from agent import codex_services
from agent import extracted_prompt_services


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("parser has no command subparsers")


def _parse(*argv: str) -> argparse.Namespace:
    return cli._build_parser().parse_args(list(argv))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot(repo_path: str = "/tmp/repo") -> dict:
    return {
        "repo_path": repo_path,
        "is_git_repo": True,
        "head": "abcdef1234567890",
        "branch": "main",
        "status_short": "",
        "diff_stat": "",
        "diff_name_only": "",
        "validation_error": None,
    }


def _invocation_state() -> dict:
    return {"validation_error": None, "paths": {}}


def _invocation_delta() -> dict:
    return {
        "attributable_changed_files": [],
        "attributable_added_files": [],
        "attributable_deleted_files": [],
        "attributable_renamed_files": [],
        "preexisting_changed_files": [],
        "preexisting_untracked_files": [],
        "path_delta_details": [],
        "validation_error": None,
    }


def _classification() -> dict:
    return {
        "total_files": 0,
        "files": [],
        "counts_by_category": {},
        "counts_by_risk_level": {},
        "high_risk_files": [],
    }


def _supervision_decision() -> dict:
    return {
        "decision": "continue",
        "attention_level": "ok",
        "approval_required": False,
        "needs_review": False,
        "reasons": [],
        "messages": [],
    }


def _transition(next_status: str = "completed") -> dict:
    return {
        "next_status": next_status,
        "reason": "supervision_decision_continue",
        "decision": "continue",
        "approval_required": False,
        "needs_review": False,
        "should_auto_complete": True,
    }


def _raw_codex_result(
    *,
    repo_path: str = "/tmp/repo",
    sandbox: str = "read-only",
    exit_code: int | None = 0,
    validation_error: str | None = None,
) -> dict:
    return {
        "mode": "exec",
        "found": True,
        "codex_path": "/usr/local/bin/codex",
        "prompt": "Say exactly: hello",
        "repo_path": repo_path,
        "sandbox": sandbox,
        "validation_error": validation_error,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, "Say exactly: hello"],
        "cwd": repo_path,
        "exit_code": exit_code,
        "stdout": "" if validation_error else "hello\n",
        "stderr": f"{validation_error}\n" if validation_error else "",
        "timed_out": False,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "final_message_path": f"{repo_path}/.codex-final-message.md",
        "final_message": "" if validation_error else "Final assistant summary.\n",
        "final_message_length": 0 if validation_error else len("Final assistant summary.\n"),
        "final_message_status": "invalid" if validation_error else "valid",
        "final_message_error": validation_error,
    }


def _submission(event_id: int = 1) -> dict:
    return {
        "id": event_id,
        "event_type": "gpt_feedback_submission_verified",
        "metadata_json": json.dumps({"reason_code": "chatgpt_submission_verified"}, sort_keys=True),
    }


def _feedback(message: str = "GPT feedback message.") -> dict:
    payload_sha = _sha(message)
    marker_text = "\n".join(
        [
            "AGENT_SUBMISSION",
            "run_id=run-1",
            "nonce=nonce-1",
            f"payload_sha256={payload_sha}",
            "END_AGENT_SUBMISSION",
        ]
    )
    return {
        "run_id": "run-1",
        "status": "completed",
        "codex_exit_code": 0,
        "codex_timed_out": False,
        "changed_files": ["agent/cli.py"],
        "message": message,
        "submission_marker_text": marker_text,
        "submission_marker_sha256": _sha(marker_text),
        "submission_marker_nonce": "nonce-1",
        "submission_marker_payload_sha256": payload_sha,
        "payload_without_marker_sha256": payload_sha,
        "feedback_payload_version": "test-feedback-v1",
        "feedback_payload_sha256": payload_sha,
        "feedback_payload_length": len(message),
    }


def _capture(submission: dict, event_id: int = 2, prompt: str = "Say exactly: extracted") -> dict:
    response = f"BEGIN_NEXT_CODEX_PROMPT\n{prompt}\nEND_NEXT_CODEX_PROMPT"
    return {
        "id": event_id,
        "event_type": "gpt_response_captured",
        "metadata_json": json.dumps(
            {
                "matched_submission_event_id": submission["id"],
                "response_text": response,
                "response_sha256": _sha(response),
            },
            sort_keys=True,
        ),
    }


def _extraction(capture: dict, submission: dict, event_id: int = 3, prompt: str = "Say exactly: extracted") -> dict:
    capture_metadata = json.loads(capture["metadata_json"])
    metadata = {
        "source_event_id": capture["id"],
        "source_event_type": "gpt_response_captured",
        "source_response_sha256": capture_metadata["response_sha256"],
        "matched_submission_event_id": submission["id"],
        "extraction_method": "sentinel_block",
        "prompt_text": prompt,
        "prompt_length": len(prompt),
        "prompt_sha256": _sha(prompt),
        "prompt_count_detected": 1,
        "selected_prompt_index": 0,
        "safety_status": "requires_human_review",
        "warnings": [],
    }
    return {
        "id": event_id,
        "event_type": "next_codex_prompt_extracted",
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


def _extracted_prompt_events(prompt: str = "Say exactly: extracted") -> list[dict]:
    submission = _submission()
    capture = _capture(submission, prompt=prompt)
    return [submission, capture, _extraction(capture, submission, prompt=prompt)]


class FakeLedger:
    def __init__(self, run: dict | None = None, events: list[dict] | None = None) -> None:
        self.run = run if run is not None else {"id": "run-1", "status": "created"}
        self.events = list(events or [])
        self.status_updates: list[tuple[str, object]] = []
        self._next_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1

    def get_run(self, run_id: str) -> dict | None:
        return self.run

    def list_events(self, run_id: str) -> list[dict]:
        return self.events

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
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
        return event

    def update_run_status(self, run_id: str, status: object) -> None:
        self.status_updates.append((run_id, status))
        self.run = {**(self.run or {}), "status": getattr(status, "value", str(status))}


class GuardLedger:
    def get_run(self, run_id: str) -> dict | None:
        raise AssertionError("ledger.get_run should not be reached")

    def list_events(self, run_id: str) -> list[dict]:
        raise AssertionError("ledger.list_events should not be reached")

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        raise AssertionError("ledger.add_event should not be reached")


class CliParserCharacterizationTests(unittest.TestCase):
    def test_parser_registered_command_surface_is_current_inventory(self) -> None:
        self.assertEqual(
            _command_names(cli._build_parser()),
            {
                "activate-chatgpt",
                "approve",
                "calibrate-chatgpt-sidebar-coordinate-mapping",
                "can-continue",
                "capture-gpt-response-from-chatgpt-ax",
                "codex-check",
                "codex-run",
                "complete-review",
                "diagnose-chatgpt-project-chat-rows",
                "extract-next-codex-prompt",
                "gpt-feedback",
                "init",
                "inspect-chatgpt-navigation-ui",
                "inspect-chatgpt-project-chat-row-ax",
                "inspect-chatgpt-project-visible-chats",
                "inspect-chatgpt-sidebar-destination",
                "inspect-chatgpt-ui",
                "open-chatgpt-project-chat",
                "open-chatgpt-sidebar-destination",
                "paste-feedback",
                "paste-feedback-to-chatgpt",
                "reject",
                "release-stale-chatgpt-ui-lease",
                "run-extracted-codex-prompt",
                "run-shell",
                "show",
                "start",
                "submit-feedback",
                "submit-feedback-to-chatgpt",
                "supervise",
                "test-chatgpt-target-paste",
                "verify-chatgpt-sidebar-destination",
                "verify-chatgpt-sidebar-frame-click",
                "verify-current-cursor-click",
                "verify-synthetic-click-delivery",
            },
        )

    def test_full_access_confirmation_flags_parse_without_enforcing_gate(self) -> None:
        codex_run = _parse(
            "codex-run",
            "run-1",
            "--prompt",
            "Say exactly: hello",
            "--sandbox",
            "danger-full-access",
        )
        confirmed_codex_run = _parse(
            "codex-run",
            "run-1",
            "--prompt",
            "Say exactly: hello",
            "--sandbox",
            "danger-full-access",
            "--confirm-full-access",
        )
        extracted = _parse(
            "run-extracted-codex-prompt",
            "run-1",
            "--repo",
            tempfile.gettempdir(),
            "--sandbox",
            "danger-full-access",
            "--confirm-run",
        )
        confirmed_extracted = _parse(
            "run-extracted-codex-prompt",
            "run-1",
            "--repo",
            tempfile.gettempdir(),
            "--sandbox",
            "danger-full-access",
            "--confirm-run",
            "--confirm-full-access",
        )

        self.assertEqual(codex_run.sandbox, "danger-full-access")
        self.assertFalse(codex_run.confirm_full_access)
        self.assertTrue(confirmed_codex_run.confirm_full_access)
        self.assertEqual(extracted.sandbox, "danger-full-access")
        self.assertTrue(extracted.confirm_run)
        self.assertFalse(extracted.confirm_full_access)
        self.assertTrue(confirmed_extracted.confirm_full_access)

    def test_risky_navigation_and_click_commands_are_dry_run_by_default_at_parse_level(self) -> None:
        cases = [
            (
                ("open-chatgpt-sidebar-destination", "--kind", "project", "--title", "PTG"),
                "confirm_open_destination",
                "--confirm-open-destination",
            ),
            (
                ("open-chatgpt-project-chat", "--project-title", "PTG", "--chat-title", "Design Review"),
                "confirm_open_chat",
                "--confirm-open-chat",
            ),
            (
                ("calibrate-chatgpt-sidebar-coordinate-mapping", "--kind", "chat", "--title", "Design Review"),
                "confirm_calibration_click",
                "--confirm-calibration-click",
            ),
            (
                ("verify-chatgpt-sidebar-frame-click", "--kind", "chat", "--title", "Design Review"),
                "confirm_frame_click",
                "--confirm-frame-click",
            ),
            (
                ("verify-synthetic-click-delivery",),
                "confirm_synthetic_click_probe",
                "--confirm-synthetic-click-probe",
            ),
            (
                ("verify-current-cursor-click",),
                "confirm_current_cursor_click",
                "--confirm-current-cursor-click",
            ),
        ]

        for argv, attribute, confirm_flag in cases:
            with self.subTest(command=argv[0]):
                self.assertFalse(getattr(_parse(*argv), attribute))
                self.assertTrue(getattr(_parse(*argv, confirm_flag), attribute))


class CliConfirmationGateCharacterizationTests(unittest.TestCase):
    def test_submit_feedback_requires_confirm_submit_before_any_side_effects(self) -> None:
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "submit-feedback", "run-1", "--copy-first"]),
            mock.patch.object(cli, "ledger", GuardLedger()),
            mock.patch.object(cli, "build_gpt_feedback_message") as build_feedback,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app") as paste_clipboard,
            mock.patch.object(cli, "press_enter_in_frontmost_app") as press_enter,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-submit", stderr.getvalue())
        build_feedback.assert_not_called()
        copy_to_clipboard.assert_not_called()
        paste_clipboard.assert_not_called()
        press_enter.assert_not_called()

    def test_test_chatgpt_target_paste_requires_confirmation_before_desktop_side_effects(self) -> None:
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "test-chatgpt-target-paste"]),
            mock.patch.object(cli, "activate_chatgpt") as activate_chatgpt,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app") as paste_clipboard,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-paste", stderr.getvalue())
        activate_chatgpt.assert_not_called()
        copy_to_clipboard.assert_not_called()
        paste_clipboard.assert_not_called()

    def test_verify_current_cursor_click_default_dispatches_unconfirmed_dry_run(self) -> None:
        result = {
            "ok": True,
            "confirm_current_cursor_click": False,
            "status": "dry_run",
            "current_cursor_location": {"x": 12, "y": 34},
            "click_count": 0,
            "inter_click_delay_ms": 0,
            "event_source_type": "hid",
            "event_posting_target": "current_cursor",
            "permission_preflight_state": {"available": True, "error": None},
            "actions_performed": [],
            "error": None,
        }
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "verify-current-cursor-click"]),
            mock.patch.object(cli, "verify_current_cursor_click", return_value=result) as verify_current_cursor_click,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        verify_current_cursor_click.assert_called_once()
        self.assertFalse(verify_current_cursor_click.call_args.kwargs["confirm_current_cursor_click"])
        self.assertIs(
            verify_current_cursor_click.call_args.kwargs["before_click_callback"],
            cli._current_cursor_click_notice,
        )
        self.assertIn("dry_run: true", stdout.getvalue())
        self.assertIn("current_cursor_location: x=12 y=34", stdout.getvalue())
        self.assertIn("actions_performed: []", stdout.getvalue())


class CliFeedbackCommandCharacterizationTests(unittest.TestCase):
    def test_gpt_feedback_success_generates_event_and_prints_without_output_or_clipboard(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "completed"})
        feedback = _feedback("Feedback body.")
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "gpt-feedback", "run-1"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "build_gpt_feedback_message", return_value=feedback) as build_feedback,
            mock.patch.object(cli, "_write_feedback_output") as write_feedback,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            contextlib.redirect_stdout(stdout),
        ):
            cli.main()

        build_feedback.assert_called_once()
        self.assertIs(build_feedback.call_args.args[0], fake_ledger.run)
        write_feedback.assert_not_called()
        copy_to_clipboard.assert_not_called()
        self.assertEqual(stdout.getvalue(), "Feedback body.\n")
        self.assertEqual([event["event_type"] for event in fake_ledger.events], ["gpt_feedback_generated"])
        self.assertEqual(fake_ledger.events[0]["metadata"]["message_length"], len(feedback["message"]))
        self.assertEqual(
            fake_ledger.events[0]["metadata"]["submission_marker_sha256"],
            feedback["submission_marker_sha256"],
        )

    def test_gpt_feedback_output_and_copy_use_patchable_helpers(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "completed"})
        feedback = _feedback("Feedback body.")
        output_path = Path("/tmp/fake-feedback.md")
        stdout = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "gpt-feedback", "run-1", "--output", str(output_path), "--copy"],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "build_gpt_feedback_message", return_value=feedback),
            mock.patch.object(cli, "_write_feedback_output", return_value=output_path) as write_feedback,
            mock.patch.object(
                cli,
                "copy_to_clipboard",
                return_value={"copied": True, "method": "fake-clipboard", "error": None},
            ) as copy_to_clipboard,
            contextlib.redirect_stdout(stdout),
        ):
            cli.main()

        write_feedback.assert_called_once_with(str(output_path), feedback["message"])
        copy_to_clipboard.assert_called_once_with(feedback["message"])
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            ["gpt_feedback_generated", "gpt_feedback_copied"],
        )
        self.assertEqual(fake_ledger.events[1]["metadata"]["method"], "fake-clipboard")
        output = stdout.getvalue()
        self.assertIn("Feedback body.\n", output)
        self.assertIn(f"wrote: {output_path}\n", output)
        self.assertIn("copied: true (method: fake-clipboard)\n", output)

    def test_gpt_feedback_missing_run_exits_before_generation_or_side_effects(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value=None),
            list_events=mock.Mock(side_effect=AssertionError("list_events should not be reached")),
            add_event=mock.Mock(side_effect=AssertionError("add_event should not be reached")),
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "gpt-feedback", "missing-run"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "build_gpt_feedback_message") as build_feedback,
            mock.patch.object(cli, "_write_feedback_output") as write_feedback,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        fake_ledger.get_run.assert_called_once_with("missing-run")
        fake_ledger.list_events.assert_not_called()
        fake_ledger.add_event.assert_not_called()
        build_feedback.assert_not_called()
        write_feedback.assert_not_called()
        copy_to_clipboard.assert_not_called()
        self.assertEqual(stderr.getvalue(), "Run not found: missing-run\n")

    def test_gpt_feedback_output_write_failure_exits_before_event_or_clipboard(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "completed"})
        feedback = _feedback("Feedback body.")
        stderr = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "gpt-feedback", "run-1", "--output", "/tmp/fake-feedback.md", "--copy"],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "build_gpt_feedback_message", return_value=feedback),
            mock.patch.object(cli, "_write_feedback_output", side_effect=OSError("read-only")) as write_feedback,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        write_feedback.assert_called_once_with("/tmp/fake-feedback.md", feedback["message"])
        copy_to_clipboard.assert_not_called()
        self.assertEqual(fake_ledger.events, [])
        self.assertEqual(stderr.getvalue(), "Failed to write GPT feedback output: read-only\n")

    def test_paste_feedback_requires_copy_first_before_ledger_or_desktop_side_effects(self) -> None:
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "paste-feedback", "run-1"]),
            mock.patch.object(cli, "ledger", GuardLedger()),
            mock.patch.object(cli, "build_gpt_feedback_message") as build_feedback,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app") as paste_clipboard,
            mock.patch.object(cli, "press_enter_in_frontmost_app") as press_enter,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--copy-first", stderr.getvalue())
        build_feedback.assert_not_called()
        copy_to_clipboard.assert_not_called()
        paste_clipboard.assert_not_called()
        press_enter.assert_not_called()

    def test_paste_feedback_copy_first_pastes_without_submit_input(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "completed"})
        feedback = _feedback("Feedback body.")
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "paste-feedback", "run-1", "--copy-first"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "build_gpt_feedback_message", return_value=feedback),
            mock.patch.object(
                cli,
                "copy_to_clipboard",
                return_value={"copied": True, "method": "fake-clipboard", "error": None},
            ) as copy_to_clipboard,
            mock.patch.object(
                cli,
                "paste_clipboard_to_frontmost_app",
                return_value={"pasted": True, "method": "fake-paste", "error": None, "stdout": "", "stderr": "", "exit_code": 0},
            ) as paste_clipboard,
            mock.patch.object(cli, "press_enter_in_frontmost_app") as press_enter,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        copy_to_clipboard.assert_called_once_with(feedback["message"])
        paste_clipboard.assert_called_once_with()
        press_enter.assert_not_called()
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            ["gpt_feedback_copied", "gpt_feedback_pasted"],
        )
        output = stdout.getvalue()
        self.assertIn("Paste target must already be focused.", output)
        self.assertIn("copied_first: true\n", output)
        self.assertIn("pasted: true\n", output)
        self.assertIn("note: No submit/Enter was sent.\n", output)

    def test_paste_feedback_to_chatgpt_requires_confirm_before_any_side_effects(self) -> None:
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "paste-feedback-to-chatgpt", "run-1"]),
            mock.patch.object(cli, "ledger", GuardLedger()),
            mock.patch.object(cli, "build_gpt_feedback_message") as build_feedback,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            mock.patch.object(cli, "activate_chatgpt") as activate_chatgpt,
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app") as paste_clipboard,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-paste", stderr.getvalue())
        build_feedback.assert_not_called()
        copy_to_clipboard.assert_not_called()
        activate_chatgpt.assert_not_called()
        paste_clipboard.assert_not_called()

    def test_paste_feedback_to_chatgpt_confirmed_path_uses_patchable_desktop_adapters(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "completed"})
        feedback = _feedback("Feedback body.")
        stdout = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "paste-feedback-to-chatgpt", "run-1", "--confirm-paste"],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "build_gpt_feedback_message", return_value=feedback),
            mock.patch.object(
                cli,
                "copy_to_clipboard",
                return_value={"copied": True, "method": "fake-clipboard", "error": None},
            ) as copy_to_clipboard,
            mock.patch.object(
                cli,
                "activate_chatgpt",
                return_value={
                    "activated": True,
                    "app_name": "ChatGPT",
                    "frontmost_app": "ChatGPT",
                    "is_frontmost": True,
                    "activation_result": None,
                    "frontmost_result": None,
                    "error": None,
                },
            ) as activate_chatgpt,
            mock.patch.object(
                cli,
                "paste_clipboard_to_frontmost_app",
                return_value={"pasted": True, "method": "fake-paste", "error": None, "stdout": "", "stderr": "", "exit_code": 0},
            ) as paste_clipboard,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        copy_to_clipboard.assert_called_once_with(feedback["message"])
        activate_chatgpt.assert_called_once_with("ChatGPT")
        paste_clipboard.assert_called_once_with()
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            ["gpt_feedback_generated", "gpt_feedback_copied", "gpt_feedback_pasted"],
        )
        output = stdout.getvalue()
        self.assertIn("run_id: run-1\n", output)
        self.assertIn("activated: true\n", output)
        self.assertIn("pasted: true\n", output)
        self.assertIn("No submit/Enter was sent.\n", output)

    def test_submit_feedback_to_chatgpt_requires_confirm_before_ledger_or_service(self) -> None:
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "submit-feedback-to-chatgpt", "run-1"]),
            mock.patch.object(cli, "ledger", GuardLedger()),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow") as submit_flow,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            mock.patch.object(cli, "activate_chatgpt") as activate_chatgpt,
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app") as paste_clipboard,
            mock.patch.object(cli, "press_chatgpt_send_button") as send_button,
            mock.patch.object(cli, "press_enter_in_frontmost_app") as press_enter,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-submit", stderr.getvalue())
        submit_flow.assert_not_called()
        copy_to_clipboard.assert_not_called()
        activate_chatgpt.assert_not_called()
        paste_clipboard.assert_not_called()
        send_button.assert_not_called()
        press_enter.assert_not_called()

    def test_submit_feedback_to_chatgpt_confirmed_path_delegates_to_patchable_flow(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "completed"})

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "submit-feedback-to-chatgpt", "run-1", "--confirm-submit"],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", return_value=True) as submit_flow,
            mock.patch.object(cli, "copy_to_clipboard") as copy_to_clipboard,
            mock.patch.object(cli, "activate_chatgpt") as activate_chatgpt,
            mock.patch.object(cli, "paste_clipboard_to_frontmost_app") as paste_clipboard,
            mock.patch.object(cli, "press_chatgpt_send_button") as send_button,
            mock.patch.object(cli, "press_enter_in_frontmost_app") as press_enter,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        submit_flow.assert_called_once_with(
            "run-1",
            fake_ledger.run,
            "ChatGPT",
            output_path_text=None,
        )
        copy_to_clipboard.assert_not_called()
        activate_chatgpt.assert_not_called()
        paste_clipboard.assert_not_called()
        send_button.assert_not_called()
        press_enter.assert_not_called()


class CliMainDispatchCharacterizationTests(unittest.TestCase):
    def test_init_dispatch_initializes_configured_ledger_and_prints_path(self) -> None:
        fake_ledger = types.SimpleNamespace(
            DB_PATH=Path("/tmp/fake-agent-ledger.db"),
            init_db=mock.Mock(),
        )
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "init"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            contextlib.redirect_stdout(stdout),
        ):
            cli.main()

        fake_ledger.init_db.assert_called_once_with()
        self.assertEqual(stdout.getvalue(), "Database initialized: /tmp/fake-agent-ledger.db\n")

    def test_start_dispatch_delegates_to_create_run_service_with_configured_ledger(self) -> None:
        fake_ledger = object()
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "start", "Do the focused thing"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(
                cli,
                "create_run_service",
                return_value=types.SimpleNamespace(ok=True, run_id="run-123", error_message=None),
            ) as create_run_service,
            contextlib.redirect_stdout(stdout),
        ):
            cli.main()

        create_run_service.assert_called_once_with("Do the focused thing", ledger=fake_ledger)
        self.assertEqual(stdout.getvalue(), "run-123\n")

    def test_start_service_failure_exits_one_and_writes_stderr(self) -> None:
        fake_ledger = object()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "start", "Do the focused thing"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(
                cli,
                "create_run_service",
                return_value=types.SimpleNamespace(ok=False, run_id=None, error_message="database is locked"),
            ) as create_run_service,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        create_run_service.assert_called_once_with("Do the focused thing", ledger=fake_ledger)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "error: database is locked\n")

    def test_show_dispatch_reads_run_and_events_from_configured_ledger(self) -> None:
        fake_ledger = FakeLedger(
            {
                "id": "run-1",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:01+00:00",
                "user_instruction": "Do the focused thing",
                "final_summary": "Done.",
                "error": None,
            },
            [
                {
                    "id": 7,
                    "created_at": "2026-01-01T00:00:01+00:00",
                    "event_type": "run_created",
                    "message": "Run created.",
                    "metadata_json": json.dumps({"source": "test"}, sort_keys=True),
                }
            ],
        )
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "show", "run-1"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            contextlib.redirect_stdout(stdout),
        ):
            cli.main()

        output = stdout.getvalue()
        self.assertIn("Run\n", output)
        self.assertIn("  id: run-1\n", output)
        self.assertIn("  status: completed\n", output)
        self.assertIn("Events\n", output)
        self.assertIn("  [7] 2026-01-01T00:00:01+00:00 run_created\n", output)
        self.assertIn('      metadata: {"source": "test"}\n', output)

    def test_show_missing_run_uses_parser_exit(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value=None),
            list_events=mock.Mock(side_effect=AssertionError("list_events should not be reached")),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "show", "missing-run"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        fake_ledger.get_run.assert_called_once_with("missing-run")
        fake_ledger.list_events.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Run not found: missing-run\n")

    def test_can_continue_dispatch_records_policy_result_and_uses_policy_exit_code(self) -> None:
        cases = [
            ("completed", 0, "run_completed", "can_continue=True status=completed reason=run_completed"),
            ("created", 2, "run_not_started", "can_continue=False status=created reason=run_not_started"),
        ]

        for status, expected_exit_code, expected_reason, expected_message in cases:
            with self.subTest(status=status):
                fake_ledger = FakeLedger({"id": "run-1", "status": status})
                stdout = io.StringIO()

                with (
                    mock.patch.object(cli.sys, "argv", ["agent-loop", "can-continue", "run-1"]),
                    mock.patch.object(cli, "ledger", fake_ledger),
                    contextlib.redirect_stdout(stdout),
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli.main()

                self.assertEqual(raised.exception.code, expected_exit_code)
                self.assertEqual(fake_ledger.events[-1]["event_type"], "continuation_check")
                self.assertEqual(fake_ledger.events[-1]["message"], expected_message)
                self.assertEqual(fake_ledger.events[-1]["metadata"]["reason"], expected_reason)
                self.assertIn(f"status: {status}\n", stdout.getvalue())
                self.assertIn(f"reason: {expected_reason}\n", stdout.getvalue())

    def test_can_continue_missing_run_uses_parser_exit(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value=None),
            add_event=mock.Mock(side_effect=AssertionError("add_event should not be reached")),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "can-continue", "missing-run"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        fake_ledger.get_run.assert_called_once_with("missing-run")
        fake_ledger.add_event.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Run not found: missing-run\n")

    def test_human_decision_commands_dispatch_to_resolver_with_current_decision_mapping(self) -> None:
        cases = [
            ("approve", cli.HumanDecision.APPROVE, "approved"),
            ("reject", cli.HumanDecision.REJECT, "rejected"),
            ("complete-review", cli.HumanDecision.COMPLETE_REVIEW, "completed"),
        ]

        for command, expected_decision, next_status in cases:
            with self.subTest(command=command):
                stdout = io.StringIO()
                result = types.SimpleNamespace(
                    ok=True,
                    run_id="run-1",
                    reason_code=None,
                    error_message=None,
                    previous_status="needs_review",
                    next_status=next_status,
                    metadata={"note": f"{command} note"},
                )

                with (
                    mock.patch.object(
                        cli.sys,
                        "argv",
                        ["agent-loop", command, "run-1", "--note", f"{command} note"],
                    ),
                    mock.patch.object(cli, "resolve_human_decision", return_value=result) as resolver,
                    contextlib.redirect_stdout(stdout),
                ):
                    cli.main()

                resolver.assert_called_once_with("run-1", expected_decision, note=f"{command} note")
                self.assertIn("previous_status: needs_review\n", stdout.getvalue())
                self.assertIn(f"next_status: {next_status}\n", stdout.getvalue())
                self.assertIn(f"note: {command} note\n", stdout.getvalue())

    def test_human_decision_resolver_failure_preserves_stderr_and_exit_code(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = types.SimpleNamespace(
            ok=False,
            run_id="run-1",
            reason_code="invalid_status",
            error_message="Cannot approve run from current status 'created'.",
            previous_status="created",
            next_status=None,
            metadata={"note": "not yet"},
        )

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "approve", "run-1", "--note", "not yet"]),
            mock.patch.object(cli, "resolve_human_decision", return_value=result) as resolver,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        resolver.assert_called_once_with("run-1", cli.HumanDecision.APPROVE, note="not yet")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "error: Cannot approve run from current status 'created'.\n",
        )


class RunShellCharacterizationTests(unittest.TestCase):
    def _shell_result(
        self,
        *,
        command: list[str],
        exit_code: int | None = 0,
        stdout: str = "shell output\n",
        stderr: str = "",
        timed_out: bool = False,
    ) -> dict:
        return {
            "command": command,
            "cwd": None,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
        }

    def test_run_shell_happy_path_records_events_runs_command_prints_output_and_exits_zero(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        command = ["printf", "ok"]
        result = self._shell_result(command=command, stdout="ok\n")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "run-shell", "run-1", "--", "--", *command]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "run_command", return_value=result) as run_command,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        run_command.assert_called_once_with(command, cwd=None, timeout_seconds=30)
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            ["shell_command_started", "shell_command_finished"],
        )
        self.assertEqual(fake_ledger.events[0]["message"], "printf ok")
        self.assertEqual(
            fake_ledger.events[0]["metadata"],
            {"command": command, "cwd": None, "timeout": 30},
        )
        self.assertEqual(fake_ledger.events[1]["message"], "exit_code=0 timed_out=False")
        self.assertEqual(fake_ledger.events[1]["metadata"], result)
        self.assertEqual(stdout.getvalue(), "stdout:\nok\nexit_code: 0\ntimed_out: False\n")
        self.assertEqual(stderr.getvalue(), "stderr:\n\n")

    def test_run_shell_nonzero_exit_preserves_command_exit_code(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        result = self._shell_result(
            command=["false"],
            exit_code=7,
            stdout="",
            stderr="failed\n",
        )

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "run-shell", "run-1", "--", "false"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "run_command", return_value=result) as run_command,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 7)
        run_command.assert_called_once_with(["false"], cwd=None, timeout_seconds=30)
        self.assertEqual(fake_ledger.events[-1]["metadata"]["exit_code"], 7)
        self.assertFalse(fake_ledger.events[-1]["metadata"]["timed_out"])

    def test_run_shell_timeout_exits_124_and_records_timeout_metadata(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        result = self._shell_result(
            command=["sleep", "60"],
            exit_code=None,
            stdout="partial\n",
            stderr="",
            timed_out=True,
        )

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "run-shell", "run-1", "--", "sleep", "60"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "run_command", return_value=result) as run_command,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 124)
        run_command.assert_called_once_with(["sleep", "60"], cwd=None, timeout_seconds=30)
        self.assertEqual(fake_ledger.events[-1]["message"], "exit_code=None timed_out=True")
        self.assertIsNone(fake_ledger.events[-1]["metadata"]["exit_code"])
        self.assertTrue(fake_ledger.events[-1]["metadata"]["timed_out"])
        self.assertEqual(fake_ledger.events[-1]["metadata"]["stdout"], "partial\n")

    def test_run_shell_missing_run_exits_one_without_command_or_events(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value=None),
            add_event=mock.Mock(side_effect=AssertionError("add_event should not be reached")),
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "run-shell", "missing-run", "--", "printf", "ok"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "run_command") as run_command,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        fake_ledger.get_run.assert_called_once_with("missing-run")
        fake_ledger.add_event.assert_not_called()
        run_command.assert_not_called()
        self.assertEqual(stderr.getvalue(), "Run not found: missing-run\n")

    def test_run_shell_missing_command_after_normalization_exits_two_without_running_command(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value={"id": "run-1", "status": "created"}),
            add_event=mock.Mock(side_effect=AssertionError("add_event should not be reached")),
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "run-shell", "run-1", "--"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "run_command") as run_command,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        fake_ledger.get_run.assert_called_once_with("run-1")
        fake_ledger.add_event.assert_not_called()
        run_command.assert_not_called()
        self.assertIn("Missing shell command.", stderr.getvalue())


class CodexCheckCharacterizationTests(unittest.TestCase):
    def _codex_check_result(self, *, found: bool) -> dict:
        if not found:
            return {
                "codex_path": None,
                "found": False,
                "help": None,
                "doctor": None,
                "timeout_seconds": None,
            }
        return {
            "codex_path": "/opt/homebrew/bin/codex",
            "found": True,
            "help": {
                "command": ["/opt/homebrew/bin/codex", "--help"],
                "cwd": None,
                "exit_code": 0,
                "stdout": "Codex help\nUsage: codex\n",
                "stderr": "",
                "timed_out": False,
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
            },
            "doctor": {
                "command": ["/opt/homebrew/bin/codex", "doctor"],
                "cwd": None,
                "exit_code": 0,
                "stdout": "Doctor ok\n",
                "stderr": "",
                "timed_out": False,
                "started_at": "2026-01-01T00:00:01+00:00",
                "finished_at": "2026-01-01T00:00:02+00:00",
            },
            "timeout_seconds": None,
        }

    def test_codex_check_found_records_events_prints_output_and_exits_zero(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        result = self._codex_check_result(found=True)
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "codex-check", "run-1"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "check_codex_environment", return_value=result) as check_codex_environment,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        check_codex_environment.assert_called_once_with(timeout_seconds=None)
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            ["codex_check_started", "codex_check_finished"],
        )
        self.assertEqual(fake_ledger.events[0]["message"], "Checking local Codex CLI availability.")
        self.assertEqual(fake_ledger.events[0]["metadata"], {"timeout": None})
        self.assertEqual(fake_ledger.events[1]["message"], "found=True codex_path=/opt/homebrew/bin/codex")
        self.assertEqual(fake_ledger.events[1]["metadata"], result)
        output = stdout.getvalue()
        self.assertIn("found: True\n", output)
        self.assertIn("codex_path: /opt/homebrew/bin/codex\n", output)
        self.assertIn("help first lines:\nCodex help\nUsage: codex\n", output)
        self.assertIn("doctor:\n  exit_code: 0\n  timed_out: False\n", output)
        self.assertIn("    Doctor ok\n", output)

    def test_codex_check_not_found_records_events_prints_output_and_exits_one(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        result = self._codex_check_result(found=False)
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "codex-check", "run-1"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "check_codex_environment", return_value=result) as check_codex_environment,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        check_codex_environment.assert_called_once_with(timeout_seconds=None)
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            ["codex_check_started", "codex_check_finished"],
        )
        self.assertEqual(fake_ledger.events[1]["message"], "found=False")
        self.assertEqual(fake_ledger.events[1]["metadata"], result)
        self.assertEqual(
            stdout.getvalue(),
            "found: False\ncodex_path: \nhelp first lines:\n  (none)\n",
        )

    def test_codex_check_missing_run_exits_one_without_check_or_events(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value=None),
            add_event=mock.Mock(side_effect=AssertionError("add_event should not be reached")),
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "codex-check", "missing-run"]),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "check_codex_environment") as check_codex_environment,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        fake_ledger.get_run.assert_called_once_with("missing-run")
        fake_ledger.add_event.assert_not_called()
        check_codex_environment.assert_not_called()
        self.assertEqual(stderr.getvalue(), "Run not found: missing-run\n")


class ReleaseStaleChatGPTUILeaseCharacterizationTests(unittest.TestCase):
    def _release_argv(self, *extra: str) -> list[str]:
        return [
            "agent-loop",
            "release-stale-chatgpt-ui-lease",
            "--owning-run-id",
            "run-1",
            "--owner-pid",
            "12345",
            "--acquired-at",
            "2026-01-01T00:00:00+00:00",
            "--active-event-id",
            "42",
            "--reason",
            "operator verified stale",
            *extra,
        ]

    def test_release_stale_lease_requires_confirm_before_pid_check_or_ledger_write(self) -> None:
        status_enum = cli.ledger.AtomicChatGPTUILeaseStatus
        fake_ledger = types.SimpleNamespace(
            AtomicChatGPTUILeaseStatus=status_enum,
            manual_release_stale_chatgpt_ui_lease=mock.Mock(
                side_effect=AssertionError("manual release should not be reached")
            ),
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", self._release_argv()),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_pid_exists") as pid_exists,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-stale", stderr.getvalue())
        pid_exists.assert_not_called()
        fake_ledger.manual_release_stale_chatgpt_ui_lease.assert_not_called()

    def test_release_stale_lease_success_prints_result_and_exits_zero(self) -> None:
        status_enum = cli.ledger.AtomicChatGPTUILeaseStatus
        result_type = cli.ledger.AtomicChatGPTUILeaseResult
        release_result = result_type(
            status=status_enum.RELEASED,
            run_id="run-1",
            owner_pid=12345,
            owning_run_id="run-1",
            acquired_at="2026-01-01T00:00:00+00:00",
            released_at="2026-01-01T00:05:00+00:00",
            active_event_id=42,
            run_status="completed",
            event_id=77,
            event_written=True,
        )
        fake_ledger = types.SimpleNamespace(
            AtomicChatGPTUILeaseStatus=status_enum,
            manual_release_stale_chatgpt_ui_lease=mock.Mock(return_value=release_result),
        )
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", self._release_argv("--confirm-stale")),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_pid_exists", return_value=False) as pid_exists,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        pid_exists.assert_called_once_with(12345)
        fake_ledger.manual_release_stale_chatgpt_ui_lease.assert_called_once_with(
            owning_run_id="run-1",
            owner_pid=12345,
            acquired_at="2026-01-01T00:00:00+00:00",
            active_event_id=42,
            expected_run_status=None,
            expected_lease_token_sha256=None,
            reason="operator verified stale",
            source="manual_stale_release",
            confirm_stale=True,
        )
        output = stdout.getvalue()
        self.assertIn("Manual ChatGPT UI lease release\n", output)
        self.assertIn("  status: released\n", output)
        self.assertIn("  event_written: true\n", output)
        self.assertIn("  event_id: 77\n", output)
        self.assertIn("  owning_run_id: run-1\n", output)

    def test_release_stale_lease_missing_active_lease_prints_failure_and_exits_one(self) -> None:
        status_enum = cli.ledger.AtomicChatGPTUILeaseStatus
        result_type = cli.ledger.AtomicChatGPTUILeaseResult
        release_result = result_type(
            status=status_enum.MISSING,
            run_id="run-1",
            reason_code="chatgpt_ui_lease_not_active",
            error_message="No ChatGPT Desktop UI lease is active.",
        )
        fake_ledger = types.SimpleNamespace(
            AtomicChatGPTUILeaseStatus=status_enum,
            manual_release_stale_chatgpt_ui_lease=mock.Mock(return_value=release_result),
        )
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", self._release_argv("--confirm-stale")),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_pid_exists", return_value=False),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        output = stdout.getvalue()
        self.assertIn("  status: missing\n", output)
        self.assertIn("  event_written: false\n", output)
        self.assertIn("  reason_code: chatgpt_ui_lease_not_active\n", output)
        self.assertIn("  error: No ChatGPT Desktop UI lease is active.\n", output)


class CodexRunCommandCharacterizationTests(unittest.TestCase):
    def test_codex_run_missing_run_exits_before_codex_flow(self) -> None:
        fake_ledger = types.SimpleNamespace(
            get_run=mock.Mock(return_value=None),
            list_events=mock.Mock(side_effect=AssertionError("list_events should not be reached")),
        )
        stderr = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "codex-run", "missing-run", "--prompt", "Say exactly: hello"],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_codex_exec_flow") as codex_flow,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        fake_ledger.get_run.assert_called_once_with("missing-run")
        fake_ledger.list_events.assert_not_called()
        codex_flow.assert_not_called()
        self.assertEqual(stderr.getvalue(), "Run not found: missing-run\n")

    def test_codex_run_validation_error_exits_two_without_auto_supervise(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        repo = str(Path(tempfile.gettempdir()).resolve())
        expected_flow = {
            "result": {
                "found": True,
                "exit_code": 2,
                "timed_out": False,
                "validation_error": "Codex sandbox danger-full-access requires --confirm-full-access.",
            },
            "supervision_decision": {"decision": "needs_review"},
            "transition": {"next_status": "needs_review"},
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "codex-run",
                    "run-1",
                    "--prompt",
                    "Say exactly: hello",
                    "--repo",
                    repo,
                    "--sandbox",
                    "danger-full-access",
                ],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_codex_exec_flow", return_value=expected_flow) as codex_flow,
            mock.patch.object(cli, "_codex_run_auto_supervise_exit_code") as auto_supervise,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        codex_flow.assert_called_once_with(
            "run-1",
            fake_ledger.run,
            "Say exactly: hello",
            repo,
            "danger-full-access",
            None,
            False,
        )
        auto_supervise.assert_not_called()

    def test_codex_run_success_uses_patchable_flow_from_agent_cli(self) -> None:
        fake_ledger = FakeLedger({"id": "run-1", "status": "created"})
        repo = str(Path(tempfile.gettempdir()).resolve())
        expected_flow = {
            "result": {
                "found": True,
                "exit_code": 0,
                "timed_out": False,
                "validation_error": None,
            },
            "supervision_decision": {"decision": "needs_review"},
            "transition": {"next_status": "needs_review"},
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "codex-run",
                    "run-1",
                    "--prompt",
                    "Say exactly: hello",
                    "--repo",
                    repo,
                    "--sandbox",
                    "workspace-write",
                    "--no-supervise",
                ],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_codex_exec_flow", return_value=expected_flow) as codex_flow,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        codex_flow.assert_called_once_with(
            "run-1",
            fake_ledger.run,
            "Say exactly: hello",
            repo,
            "workspace-write",
            None,
            False,
        )


class CodexExecFlowCharacterizationTests(unittest.TestCase):
    def test_unconfirmed_danger_full_access_is_passed_as_preflight_validation_error(self) -> None:
        fake_ledger = FakeLedger()
        expected_error = "Codex sandbox danger-full-access requires --confirm-full-access."
        raw = _raw_codex_result(
            sandbox="danger-full-access",
            exit_code=2,
            validation_error=expected_error,
        )
        execute_calls = []
        governance_calls = []

        def fake_execute(*args, **kwargs):
            execute_calls.append((args, kwargs))
            return types.SimpleNamespace(raw_process_result=raw)

        def fake_governance(*args, **kwargs):
            governance_calls.append((args, kwargs))
            return types.SimpleNamespace(
                git_after=None,
                invocation_state_after=None,
                invocation_delta=None,
                governance_observation=None,
                changed_file_classification=None,
                diagnostics={"outcome": "validation_error"},
                supervision_decision={"decision": "needs_review"},
                metadata={"transition": _transition("needs_review")},
            )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "capture_git_snapshot", return_value=_snapshot()),
            mock.patch.object(cli, "capture_invocation_git_state", return_value=_invocation_state()),
            mock.patch.object(cli, "execute_codex_direct_service", side_effect=fake_execute),
            mock.patch.object(cli, "apply_post_codex_governance_service", side_effect=fake_governance),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            flow = cli._run_codex_exec_flow(
                "run-1",
                {"id": "run-1", "status": "created"},
                "Say exactly: hello",
                "/tmp/repo",
                "danger-full-access",
                None,
                confirm_full_access=False,
            )

        self.assertEqual(flow["result"]["validation_error"], expected_error)
        self.assertEqual(execute_calls[0][1]["preflight_validation_error"], expected_error)
        self.assertFalse(execute_calls[0][1]["confirm_full_access"])
        self.assertIs(governance_calls[0][0][6], raw)
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            [
                "git_snapshot_before_codex",
                "prompt_contract_parsed",
                "invocation_git_state_before",
            ],
        )

    def test_unconfirmed_danger_full_access_preflight_avoids_codex_runner(self) -> None:
        fake_ledger = FakeLedger()
        expected_error = "Codex sandbox danger-full-access requires --confirm-full-access."
        codex_runner = mock.Mock(side_effect=AssertionError("Codex runner should not be called"))

        def validation_result_builder(prompt: str, repo_path: str, sandbox: str, validation_error: str) -> dict:
            return _raw_codex_result(
                repo_path=repo_path,
                sandbox=sandbox,
                exit_code=2,
                validation_error=validation_error,
            )

        def service_side_effect(*args, **kwargs):
            service_kwargs = dict(kwargs)
            service_kwargs.update(
                {
                    "codex_runner": codex_runner,
                    "validation_result_builder": validation_result_builder,
                    "monotonic_clock": mock.Mock(side_effect=[10.0, 10.0]),
                }
            )
            return codex_services.execute_codex_direct_service(*args, **service_kwargs)

        def fake_governance(*args, **kwargs):
            return types.SimpleNamespace(
                git_after=None,
                invocation_state_after=None,
                invocation_delta=None,
                governance_observation=None,
                changed_file_classification=None,
                diagnostics={"outcome": "validation_error"},
                supervision_decision={"decision": "needs_review"},
                metadata={"transition": _transition("needs_review")},
            )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "capture_git_snapshot", return_value=_snapshot()),
            mock.patch.object(cli, "capture_invocation_git_state", return_value=_invocation_state()),
            mock.patch.object(cli, "execute_codex_direct_service", side_effect=service_side_effect),
            mock.patch.object(cli, "apply_post_codex_governance_service", side_effect=fake_governance),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            flow = cli._run_codex_exec_flow(
                "run-1",
                {"id": "run-1", "status": "created"},
                "Say exactly: hello",
                "/tmp/repo",
                "danger-full-access",
                None,
                confirm_full_access=False,
            )

        codex_runner.assert_not_called()
        self.assertEqual(flow["result"]["validation_error"], expected_error)
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            [
                "git_snapshot_before_codex",
                "prompt_contract_parsed",
                "invocation_git_state_before",
                "codex_exec_started",
                "codex_exec_finished",
            ],
        )
        self.assertEqual(fake_ledger.events[-1]["metadata"]["validation_error"], expected_error)

    def test_diagnostics_exception_warns_and_governance_still_records_transition(self) -> None:
        fake_ledger = FakeLedger()
        raw = _raw_codex_result()

        def fake_execute(*args, **kwargs):
            event_ledger = kwargs["ledger"]
            event_ledger.add_event("run-1", "codex_exec_started", "Running Codex exec.", {})
            event_ledger.add_event("run-1", "codex_exec_finished", "found=True exit_code=0", raw)
            return types.SimpleNamespace(raw_process_result=raw)

        def raising_diagnostics(*args, **kwargs):
            raise RuntimeError("diagnostics offline")

        stderr = io.StringIO()
        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "capture_git_snapshot", side_effect=[_snapshot(), _snapshot()]),
            mock.patch.object(cli, "capture_invocation_git_state", side_effect=[_invocation_state(), _invocation_state()]),
            mock.patch.object(cli, "compute_invocation_delta", return_value=_invocation_delta()),
            mock.patch.object(cli, "classify_changed_files", return_value=_classification()),
            mock.patch.object(cli, "analyze_prompt_repo_impact", side_effect=raising_diagnostics),
            mock.patch.object(cli, "evaluate_supervision_decision", return_value=_supervision_decision()),
            mock.patch.object(cli, "status_from_supervision_decision", return_value=_transition()),
            mock.patch.object(cli, "execute_codex_direct_service", side_effect=fake_execute),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            flow = cli._run_codex_exec_flow(
                "run-1",
                {"id": "run-1", "status": "created"},
                "Say exactly: hello",
                "/tmp/repo",
                "read-only",
                None,
                confirm_full_access=False,
            )

        self.assertIsNone(flow["prompt_repo_impact_diagnostics"])
        self.assertIn("warning: prompt/repo impact diagnostics unavailable: diagnostics offline", stderr.getvalue())
        self.assertIn("run_status_transition", [event["event_type"] for event in fake_ledger.events])
        diagnostics_event = next(
            event for event in fake_ledger.events if event["event_type"] == "prompt_repo_impact_diagnostics"
        )
        self.assertEqual(diagnostics_event["message"], "diagnostics_unavailable")
        self.assertEqual(fake_ledger.status_updates[-1][0], "run-1")


class RunExtractedCodexPromptCharacterizationTests(unittest.TestCase):
    def test_default_extracted_prompt_coordinator_delegates_to_agent_cli_codex_flow(self) -> None:
        flow = {
            "result": {"found": True, "exit_code": 0, "timed_out": False, "validation_error": None},
            "supervision_decision": {"decision": "continue"},
            "transition": {"next_status": "completed"},
        }

        with mock.patch.object(cli, "_run_codex_exec_flow", return_value=flow) as coordinator:
            result = extracted_prompt_services._default_codex_flow_coordinator(
                "run-1",
                {"id": "run-1", "status": "completed"},
                "Say exactly: extracted",
                "/tmp/repo",
                "read-only",
                None,
                False,
            )

        self.assertIs(result, flow)
        coordinator.assert_called_once_with(
            "run-1",
            {"id": "run-1", "status": "completed"},
            "Say exactly: extracted",
            "/tmp/repo",
            "read-only",
            None,
            confirm_full_access=False,
        )

    def test_run_extracted_cli_requires_confirm_run_before_ledger_or_codex_flow(self) -> None:
        stderr = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "run-extracted-codex-prompt", "run-1", "--repo", tempfile.gettempdir()],
            ),
            mock.patch.object(cli, "ledger", GuardLedger()),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow") as run_flow,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm-run is required", stderr.getvalue())
        run_flow.assert_not_called()

    def test_run_extracted_cli_confirmed_path_passes_current_dependency_contract(self) -> None:
        fake_ledger = FakeLedger(
            {"id": "run-1", "status": "completed"},
            _extracted_prompt_events(),
        )
        repo = str(Path(tempfile.gettempdir()).resolve())

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "run-extracted-codex-prompt",
                    "run-1",
                    "--repo",
                    repo,
                    "--sandbox",
                    "danger-full-access",
                    "--confirm-run",
                    "--confirm-full-access",
                    "--expect-prompt-sha256",
                    _sha("Say exactly: extracted"),
                ],
            ),
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", return_value=0) as run_flow,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        run_flow.assert_called_once_with(
            "run-1",
            fake_ledger.run,
            repo,
            "danger-full-access",
            None,
            expected_prompt_sha256=_sha("Say exactly: extracted"),
            allow_full_access=True,
            confirm_full_access=True,
        )


if __name__ == "__main__":
    unittest.main()
