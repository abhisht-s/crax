from __future__ import annotations

import argparse
import json
import shutil
import shlex
import sys
import time
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from agent.chatgpt_ax_capture import (
    DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    DEFAULT_STABLE_SECONDS,
    capture_response_after_feedback,
)
from agent.clipboard import copy_to_clipboard
from agent.codex_terminal import (
    ALLOWED_CODEX_SANDBOXES,
    check_codex_environment,
    run_codex_exec,
    run_command,
)
from agent.continuation_policy import can_continue_run
from agent.file_classifier import classify_changed_files
from agent.gpt_feedback import build_gpt_feedback_message
from agent.git_snapshot import (
    attributable_paths,
    capture_git_snapshot,
    capture_invocation_git_state,
    compute_invocation_delta,
)
from agent.mac_app_control import activate_chatgpt
from agent.mac_paste import (
    ENTER_METHOD,
    PASTE_METHOD,
    paste_clipboard_to_frontmost_app,
    press_enter_in_frontmost_app,
)
from agent.mac_ui_inspect import (
    inspect_chatgpt_submission_ui,
    inspect_chatgpt_ui,
    press_chatgpt_send_button,
)
from agent.prompt_extraction import (
    find_latest_valid_captured_response,
    extract_next_codex_prompt_from_text,
    select_latest_valid_extracted_codex_prompt,
    select_valid_extracted_codex_prompt_event,
    sha256_text,
)
from agent.prompt_contract import parse_prompt_contract
from agent.risk_policy import evaluate_supervision_decision
from agent.run_diagnostics import analyze_prompt_repo_impact
from agent.run_state import RunStatus
from agent.run_status_policy import status_from_supervision_decision
from agent.supervise import SuperviseAction, detect_next_supervise_action
from agent.workspace_write_policy import (
    POLICY_VERSION as WORKSPACE_WRITE_POLICY_VERSION,
    diff_content_flags,
    verify_workspace_write_post_run,
)
from agent import ledger


DEFAULT_SHELL_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 300
CHATGPT_TARGET_PASTE_MARKER = "WATCH_TO_CODEX_STAGE_5_6B_TARGET_PASTE_TEST_DO_NOT_SUBMIT"
CHATGPT_TARGET_PASTE_DELAY_SECONDS = 0.3
CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS = 5.0
CHATGPT_PASTE_VERIFY_POLL_SECONDS = 0.15
CHATGPT_POST_PASTE_SETTLE_SECONDS = 0.5
CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS = 15.0
CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS = 0.35


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the local run ledger database.")

    start_parser = subparsers.add_parser("start", help="Create a new run.")
    start_parser.add_argument("instruction", help="User instruction for the run.")

    show_parser = subparsers.add_parser("show", help="Show a run and its events.")
    show_parser.add_argument("run_id", help="Run ID to show.")

    gpt_feedback_parser = subparsers.add_parser(
        "gpt-feedback",
        help="Generate a GPT feedback message from the latest Codex run output.",
    )
    gpt_feedback_parser.add_argument("run_id", help="Run ID to generate feedback for.")
    gpt_feedback_parser.add_argument(
        "--output",
        help="Optional path to write the generated feedback message.",
    )
    gpt_feedback_parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy the generated GPT feedback message to the macOS clipboard.",
    )

    paste_feedback_parser = subparsers.add_parser(
        "paste-feedback",
        help="Paste the generated GPT feedback message into the frontmost focused app.",
    )
    paste_feedback_parser.add_argument("run_id", help="Run ID to generate and paste feedback for.")
    paste_feedback_parser.add_argument(
        "--copy-first",
        action="store_true",
        required=True,
        help="Required: copy the generated GPT feedback message before pasting it.",
    )
    paste_feedback_parser.add_argument(
        "--output",
        help="Optional path to write the generated feedback message.",
    )

    paste_feedback_to_chatgpt_parser = subparsers.add_parser(
        "paste-feedback-to-chatgpt",
        help="Generate, copy, activate ChatGPT, verify frontmost, and paste feedback without submitting.",
    )
    paste_feedback_to_chatgpt_parser.add_argument(
        "run_id",
        help="Run ID to generate and paste feedback for.",
    )
    paste_feedback_to_chatgpt_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    paste_feedback_to_chatgpt_parser.add_argument(
        "--output",
        help="Optional path to write the generated feedback message.",
    )
    paste_feedback_to_chatgpt_parser.add_argument(
        "--confirm-paste",
        action="store_true",
        help="Required: confirm GPT feedback should be pasted into ChatGPT.",
    )

    submit_feedback_to_chatgpt_parser = subparsers.add_parser(
        "submit-feedback-to-chatgpt",
        help="Generate, copy, activate ChatGPT, paste feedback, and submit after explicit confirmation.",
    )
    submit_feedback_to_chatgpt_parser.add_argument(
        "run_id",
        help="Run ID to generate and submit feedback for.",
    )
    submit_feedback_to_chatgpt_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    submit_feedback_to_chatgpt_parser.add_argument(
        "--output",
        help="Optional path to write the generated feedback message.",
    )
    submit_feedback_to_chatgpt_parser.add_argument(
        "--confirm-submit",
        action="store_true",
        help="Required: confirm GPT feedback should be submitted to ChatGPT.",
    )

    capture_gpt_response_ax_parser = subparsers.add_parser(
        "capture-gpt-response-from-chatgpt-ax",
        help="Capture ChatGPT's visible assistant response through desktop Accessibility after explicit confirmation.",
    )
    capture_gpt_response_ax_parser.add_argument(
        "run_id",
        help="Run ID whose submitted GPT feedback should be matched before capture.",
    )
    capture_gpt_response_ax_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    capture_gpt_response_ax_parser.add_argument(
        "--confirm-capture",
        action="store_true",
        help="Required: confirm ChatGPT desktop AX response capture should run.",
    )
    capture_gpt_response_ax_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        help=f"Maximum seconds to wait for a stable response. Default: {DEFAULT_CAPTURE_TIMEOUT_SECONDS:g}.",
    )
    capture_gpt_response_ax_parser.add_argument(
        "--stable-seconds",
        type=float,
        default=DEFAULT_STABLE_SECONDS,
        help=f"Seconds the response text must remain stable. Default: {DEFAULT_STABLE_SECONDS:g}.",
    )

    extract_next_codex_prompt_parser = subparsers.add_parser(
        "extract-next-codex-prompt",
        help="Extract and preview a next Codex prompt from the latest captured GPT response without running Codex.",
    )
    extract_next_codex_prompt_parser.add_argument(
        "run_id",
        help="Run ID whose captured GPT response should be inspected.",
    )
    extract_next_codex_prompt_parser.add_argument(
        "--confirm-extract",
        action="store_true",
        help="Required to write the extracted prompt artifact and ledger event.",
    )
    extract_next_codex_prompt_parser.add_argument(
        "--output",
        help="Optional path to write the extracted prompt when --confirm-extract is present.",
    )

    submit_feedback_parser = subparsers.add_parser(
        "submit-feedback",
        help="Paste the generated GPT feedback message and press Enter after explicit confirmation.",
    )
    submit_feedback_parser.add_argument("run_id", help="Run ID to generate and submit feedback for.")
    submit_feedback_parser.add_argument(
        "--copy-first",
        action="store_true",
        help="Required: copy the generated GPT feedback message before pasting it.",
    )
    submit_feedback_parser.add_argument(
        "--confirm-submit",
        action="store_true",
        help="Required: confirm that Enter should be sent to the focused app.",
    )

    activate_chatgpt_parser = subparsers.add_parser(
        "activate-chatgpt",
        help="Bring the ChatGPT desktop app to the front without pasting or submitting.",
    )
    activate_chatgpt_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )

    inspect_chatgpt_ui_parser = subparsers.add_parser(
        "inspect-chatgpt-ui",
        help="Read-only diagnostic for ChatGPT accessibility UI elements.",
    )
    inspect_chatgpt_ui_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )

    test_chatgpt_target_paste_parser = subparsers.add_parser(
        "test-chatgpt-target-paste",
        help="Copy and paste a fixed marker into the active ChatGPT input without submitting.",
    )
    test_chatgpt_target_paste_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    test_chatgpt_target_paste_parser.add_argument(
        "--confirm-paste",
        action="store_true",
        help="Required: confirm the fixed marker should be pasted into ChatGPT.",
    )

    can_continue_parser = subparsers.add_parser(
        "can-continue",
        help="Check whether a run may continue to the next automated step.",
    )
    can_continue_parser.add_argument("run_id", help="Run ID to check.")

    approve_parser = subparsers.add_parser("approve", help="Approve a flagged run.")
    approve_parser.add_argument("run_id", help="Run ID to approve.")
    approve_parser.add_argument("--note", default="", help="Optional human approval note.")

    reject_parser = subparsers.add_parser("reject", help="Reject a flagged run.")
    reject_parser.add_argument("run_id", help="Run ID to reject.")
    reject_parser.add_argument("--note", default="", help="Optional human rejection note.")

    complete_review_parser = subparsers.add_parser(
        "complete-review",
        help="Mark a needs_review run as reviewed and completed.",
    )
    complete_review_parser.add_argument("run_id", help="Run ID to complete review for.")
    complete_review_parser.add_argument("--note", default="", help="Optional human review note.")

    codex_check_parser = subparsers.add_parser(
        "codex-check",
        help="Check local Codex CLI availability without running prompts.",
    )
    codex_check_parser.add_argument("run_id", help="Run ID for this Codex check.")

    codex_run_parser = subparsers.add_parser(
        "codex-run",
        help="Run Codex exec and record the transcript.",
    )
    codex_run_parser.add_argument("run_id", help="Run ID for this Codex exec.")
    codex_run_parser.add_argument("--prompt", required=True, help="Prompt to pass to Codex exec.")
    codex_run_parser.add_argument("--repo", help="Repository/workdir for Codex exec. Default: current directory.")
    codex_run_parser.add_argument("--cwd", help=argparse.SUPPRESS)
    codex_run_parser.add_argument(
        "--sandbox",
        default="read-only",
        help="Codex sandbox mode: read-only, workspace-write, or danger-full-access. Default: read-only.",
    )
    codex_run_parser.add_argument(
        "--confirm-full-access",
        action="store_true",
        help="Required with --sandbox danger-full-access.",
    )
    codex_run_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
        help=f"Timeout in seconds. Default: {DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS}.",
    )
    codex_run_parser.add_argument(
        "--no-supervise",
        action="store_true",
        help="Do not automatically enter the supervised ChatGPT handoff after a successful Codex run.",
    )
    codex_run_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Ask for y/N confirmation before each otherwise-safe supervised handoff step.",
    )

    run_extracted_codex_prompt_parser = subparsers.add_parser(
        "run-extracted-codex-prompt",
        help="Run the latest extracted Codex prompt after explicit human confirmation.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "run_id",
        help="Run ID containing next_codex_prompt_extracted.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "--repo",
        required=True,
        help="Explicit repository/workdir for Codex exec.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "--sandbox",
        default="read-only",
        help="Codex sandbox mode: read-only, workspace-write, or danger-full-access. Default: read-only.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "--confirm-run",
        action="store_true",
        help="Required: confirm the extracted prompt should be run through Codex.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "--confirm-full-access",
        action="store_true",
        help="Required with --sandbox danger-full-access.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "--expect-prompt-sha256",
        help="Optional expected SHA-256 for the selected extracted prompt.",
    )
    run_extracted_codex_prompt_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
        help=f"Timeout in seconds. Default: {DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS}.",
    )

    supervise_parser = subparsers.add_parser(
        "supervise",
        help="Continue a supervised run to the next safe action with simple y/n gates.",
    )
    supervise_parser.add_argument("run_id", help="Run ID to supervise.")
    supervise_parser.add_argument(
        "--repo",
        required=True,
        help="Explicit repository/workdir for extracted Codex prompt execution.",
    )
    supervise_parser.add_argument(
        "--sandbox",
        default="read-only",
        help="Sandbox for extracted prompt runs: read-only or workspace-write. Default: read-only.",
    )
    supervise_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    supervise_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS,
        help=f"Codex timeout in seconds. Default: {DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS}.",
    )
    supervise_parser.add_argument(
        "--capture-timeout-seconds",
        type=float,
        default=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        help=f"Maximum seconds to wait for a stable ChatGPT response. Default: {DEFAULT_CAPTURE_TIMEOUT_SECONDS:g}.",
    )
    supervise_parser.add_argument(
        "--stable-seconds",
        type=float,
        default=DEFAULT_STABLE_SECONDS,
        help=f"Seconds the response text must remain stable. Default: {DEFAULT_STABLE_SECONDS:g}.",
    )
    supervise_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Ask for y/N confirmation before each otherwise-safe supervised handoff step.",
    )

    run_shell_parser = subparsers.add_parser(
        "run-shell",
        help="Run a non-interactive shell command and record the transcript.",
    )
    run_shell_parser.add_argument("run_id", help="Run ID for this shell command.")
    run_shell_parser.add_argument(
        "shell_command",
        nargs=argparse.REMAINDER,
        help="Command to execute, usually after --.",
    )

    return parser


def _print_run(run: dict, events: list[dict]) -> None:
    print("Run")
    print(f"  id: {run['id']}")
    print(f"  status: {run['status']}")
    print(f"  created_at: {run['created_at']}")
    print(f"  updated_at: {run['updated_at']}")
    print(f"  user_instruction: {run['user_instruction']}")
    print(f"  final_summary: {run['final_summary'] or ''}")
    print(f"  error: {run['error'] or ''}")
    print()
    print("Events")

    if not events:
        print("  (none)")
        return

    for event in events:
        print(f"  [{event['id']}] {event['created_at']} {event['event_type']}")
        print(f"      message: {event['message']}")
        if event["metadata_json"]:
            print(f"      metadata: {event['metadata_json']}")


def _normalize_shell_command(raw_command: list[str]) -> list[str]:
    if raw_command and raw_command[0] in {"--", "–"}:
        return raw_command[1:]
    return raw_command


def _format_command(command: list[str]) -> str:
    return shlex.join(command)


def _print_shell_result(result: dict) -> None:
    print("stdout:")
    print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
    print("stderr:", file=sys.stderr)
    print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n", file=sys.stderr)
    print(f"exit_code: {result['exit_code']}")
    print(f"timed_out: {result['timed_out']}")


def _first_lines(value: str, count: int = 8) -> str:
    return "\n".join(value.splitlines()[:count])


def _command_output(result: dict | None) -> str:
    if result is None:
        return ""
    return result["stdout"] or result["stderr"]


def _event_metadata(event: dict) -> dict:
    metadata_json = event.get("metadata_json")
    if not metadata_json:
        return {}
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _latest_verified_gpt_feedback_submission(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "gpt_feedback_submission_verified":
            continue
        metadata = _event_metadata(event)
        if metadata.get("reason_code") == "chatgpt_submission_verified":
            return event
    return None


def _latest_successful_gpt_feedback_submission(events: list[dict]) -> dict | None:
    return _latest_verified_gpt_feedback_submission(events)


def _latest_feedback_generation_before_submission(
    events: list[dict],
    submission_event: dict,
) -> dict | None:
    submission_event_id = submission_event.get("id")
    for event in reversed(events):
        if event.get("event_type") != "gpt_feedback_generated":
            continue
        if isinstance(submission_event_id, int) and event.get("id", 0) > submission_event_id:
            continue
        return event
    return None


def _feedback_text_from_event_metadata(
    events: list[dict],
    submission_event: dict,
) -> tuple[str | None, dict | None]:
    submission_event_id = submission_event.get("id")
    text_keys = (
        "submitted_feedback_text",
        "feedback_text",
        "feedback_message",
        "message",
    )
    event_types = {
        "gpt_feedback_generated",
        "gpt_feedback_copied",
        "gpt_feedback_pasted",
        "gpt_feedback_submitted",
    }

    for event in reversed(events):
        if event.get("event_type") not in event_types:
            continue
        if isinstance(submission_event_id, int) and event.get("id", 0) > submission_event_id:
            continue
        metadata = _event_metadata(event)
        for key in text_keys:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value, event

    return None, None


def _marker_metadata(feedback: dict) -> dict:
    return {
        "submission_marker_text": feedback["submission_marker_text"],
        "submission_marker_sha256": feedback["submission_marker_sha256"],
        "submission_marker_nonce": feedback["submission_marker_nonce"],
        "submission_marker_payload_sha256": feedback["submission_marker_payload_sha256"],
        "payload_without_marker_sha256": feedback["payload_without_marker_sha256"],
        "feedback_payload_version": feedback["feedback_payload_version"],
        "feedback_payload_sha256": feedback["feedback_payload_sha256"],
        "feedback_payload_length": feedback["feedback_payload_length"],
    }


def _submission_ui_observation_summary(observation: dict, marker_text: str | None = None) -> dict:
    composer = observation.get("focused_composer")
    send_button = observation.get("send_button")
    message_candidates = observation.get("message_candidates") or []
    marker_candidates = []
    if marker_text:
        for candidate in message_candidates:
            text = candidate.get("text") if isinstance(candidate, dict) else ""
            if isinstance(text, str) and marker_text in text:
                marker_candidates.append(_candidate_observation_summary(candidate, marker_text))

    return {
        "ok": bool(observation.get("ok")),
        "method": observation.get("method"),
        "error": observation.get("error"),
        "focused_element": _element_observation_summary(observation.get("focused_element"), marker_text),
        "focused_composer": _element_observation_summary(composer, marker_text),
        "text_input_candidate_count": len(observation.get("text_input_candidates") or []),
        "button_candidate_count": len(observation.get("button_candidates") or []),
        "send_button": _element_observation_summary(send_button, marker_text, include_text=False),
        "message_candidate_count": len(message_candidates),
        "marker_text_present_in_composer": bool(observation.get("marker_text_present_in_composer")),
        "marker_text_candidate_count": len(marker_candidates),
        "marker_candidates": marker_candidates[:5],
        "ax_stats": observation.get("ax_stats") or {},
    }


def _element_observation_summary(
    element: object,
    marker_text: str | None = None,
    include_text: bool = True,
) -> dict | None:
    if not isinstance(element, dict):
        return None
    text = str(element.get("text") or element.get("value") or "")
    summary = {
        "path": element.get("path"),
        "role": element.get("role"),
        "subrole": element.get("subrole"),
        "title": element.get("title"),
        "description": element.get("description"),
        "identifier": element.get("identifier"),
        "enabled": element.get("enabled"),
        "focused": element.get("focused"),
        "actions": element.get("actions") or [],
    }
    if include_text:
        summary.update(
            {
                "text_length": len(text),
                "text_sha256": sha256_text(text) if text else "",
                "contains_marker": bool(marker_text and marker_text in text),
            }
        )
    return summary


def _candidate_observation_summary(candidate: dict, marker_text: str | None = None) -> dict:
    text = str(candidate.get("text") or "")
    return {
        "index": candidate.get("index"),
        "path": candidate.get("path"),
        "role": candidate.get("role"),
        "subrole": candidate.get("subrole"),
        "text_length": len(text),
        "text_sha256": sha256_text(text) if text else "",
        "contains_marker": bool(marker_text and marker_text in text),
    }


def _focused_composer_from_observation(observation: dict) -> dict | None:
    composer = observation.get("focused_composer")
    return composer if isinstance(composer, dict) else None


def _wait_for_pasted_marker(app_name: str, marker_text: str) -> dict:
    deadline = time.monotonic() + CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS
    polls = 0
    last_observation: dict = {}
    while time.monotonic() <= deadline:
        polls += 1
        observation = inspect_chatgpt_submission_ui(app_name, marker_text=marker_text)
        last_observation = observation
        composer = _focused_composer_from_observation(observation)
        if composer is not None and marker_text in str(composer.get("text") or composer.get("value") or ""):
            return {
                "ok": True,
                "reason_code": "chatgpt_draft_pasted",
                "poll_count": polls,
                "timeout_seconds": CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS,
                "poll_interval_seconds": CHATGPT_PASTE_VERIFY_POLL_SECONDS,
                "observation": _submission_ui_observation_summary(observation, marker_text),
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(CHATGPT_PASTE_VERIFY_POLL_SECONDS, remaining))

    return {
        "ok": False,
        "reason_code": "chatgpt_paste_not_visible",
        "poll_count": polls,
        "timeout_seconds": CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS,
        "poll_interval_seconds": CHATGPT_PASTE_VERIFY_POLL_SECONDS,
        "observation": _submission_ui_observation_summary(last_observation, marker_text),
    }


def _submission_verification_status(observation: dict, marker_text: str) -> dict:
    composer = _focused_composer_from_observation(observation)
    composer_text = str((composer or {}).get("text") or (composer or {}).get("value") or "")
    composer_contains = marker_text in composer_text if composer is not None else False
    candidates = [
        candidate
        for candidate in observation.get("message_candidates") or []
        if isinstance(candidate, dict)
        and candidate.get("path") != (composer or {}).get("path")
        and marker_text in str(candidate.get("text") or "")
    ]

    if len(candidates) == 1 and not composer_contains:
        return {
            "verified": True,
            "ambiguous": False,
            "reason_code": "chatgpt_submission_verified",
            "composer_contains_marker": False,
            "submitted_candidate_count": 1,
            "submitted_candidate": _candidate_observation_summary(candidates[0], marker_text),
        }
    if len(candidates) > 1 or (len(candidates) >= 1 and composer_contains):
        return {
            "verified": False,
            "ambiguous": True,
            "reason_code": "chatgpt_submission_ambiguous",
            "composer_contains_marker": composer_contains,
            "submitted_candidate_count": len(candidates),
            "submitted_candidates": [_candidate_observation_summary(item, marker_text) for item in candidates[:5]],
        }
    return {
        "verified": False,
        "ambiguous": False,
        "reason_code": "chatgpt_submission_not_verified",
        "composer_contains_marker": composer_contains,
        "submitted_candidate_count": len(candidates),
        "submitted_candidates": [_candidate_observation_summary(item, marker_text) for item in candidates[:5]],
    }


def _verify_submission_marker(app_name: str, marker_text: str) -> dict:
    deadline = time.monotonic() + CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS
    polls = 0
    last_observation: dict = {}
    last_status: dict = {}
    while time.monotonic() <= deadline:
        polls += 1
        observation = inspect_chatgpt_submission_ui(app_name, marker_text=marker_text)
        last_observation = observation
        status = _submission_verification_status(observation, marker_text)
        last_status = status
        if status["verified"] or status["ambiguous"]:
            return {
                "ok": bool(status["verified"]),
                "reason_code": status["reason_code"],
                "poll_count": polls,
                "timeout_seconds": CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS,
                "poll_interval_seconds": CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS,
                "status": status,
                "observation": _submission_ui_observation_summary(observation, marker_text),
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS, remaining))

    return {
        "ok": False,
        "reason_code": last_status.get("reason_code") or "chatgpt_submission_not_verified",
        "poll_count": polls,
        "timeout_seconds": CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS,
        "poll_interval_seconds": CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS,
        "status": last_status,
        "observation": _submission_ui_observation_summary(last_observation, marker_text),
    }


def _select_send_input_method(app_name: str, marker_text: str) -> tuple[dict, dict]:
    observation = inspect_chatgpt_submission_ui(app_name, marker_text=marker_text)
    send_button = observation.get("send_button") if isinstance(observation, dict) else None
    if isinstance(send_button, dict) and send_button.get("path"):
        result = press_chatgpt_send_button(app_name, str(send_button["path"]))
        return result, _submission_ui_observation_summary(observation, marker_text)
    return press_enter_in_frontmost_app(), _submission_ui_observation_summary(observation, marker_text)


def _submit_input_sent_ok(send_result: dict) -> bool:
    if "pressed" in send_result:
        return bool(send_result.get("pressed"))
    return bool(send_result.get("submitted"))


def _send_result_method(send_result: dict) -> str | None:
    if send_result.get("method"):
        return str(send_result["method"])
    return None


def _print_codex_check_result(result: dict) -> None:
    print(f"found: {result['found']}")
    print(f"codex_path: {result['codex_path'] or ''}")

    print("help first lines:")
    help_lines = _first_lines(_command_output(result["help"]))
    print(help_lines if help_lines else "  (none)")

    if result["doctor"] is not None:
        doctor = result["doctor"]
        print("doctor:")
        print(f"  exit_code: {doctor['exit_code']}")
        print(f"  timed_out: {doctor['timed_out']}")
        doctor_lines = _first_lines(_command_output(doctor))
        print("  output first lines:")
        if doctor_lines:
            for line in doctor_lines.splitlines():
                print(f"    {line}")
        else:
            print("    (none)")


def _print_codex_exec_result(result: dict) -> None:
    print(f"repo_path: {result['repo_path']}")
    print(f"sandbox: {result['sandbox']}")
    print(f"found: {result['found']}")
    print(f"codex_path: {result['codex_path'] or ''}")
    print(f"exit_code: {result['exit_code']}")
    print(f"timed_out: {result['timed_out']}")
    if result["validation_error"]:
        print(f"validation_error: {result['validation_error']}")
    print("stdout:")
    print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")

    if result["stderr"]:
        print("stderr:", file=sys.stderr)
        print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n", file=sys.stderr)


def _short_hash(value: str | None) -> str | None:
    return value[:12] if value else None


def _working_tree_dirty(snapshot: dict) -> bool:
    return bool(snapshot["status_short"].strip())


def _changed_files_count(snapshot: dict) -> int:
    return len([line for line in snapshot["diff_name_only"].splitlines() if line.strip()])


def _changed_file_paths(snapshot: dict) -> list[str]:
    diff_name_only = snapshot.get("diff_name_only") or ""
    return [line.strip() for line in diff_name_only.splitlines() if line.strip()]


def _snapshot_message(snapshot: dict) -> str:
    branch = snapshot["branch"] or "None"
    head = _short_hash(snapshot["head"]) or "None"
    dirty = str(_working_tree_dirty(snapshot)).lower()
    return (
        f"repo_path={snapshot['repo_path']} branch={branch} "
        f"head={head} dirty={dirty}"
    )


def _print_git_snapshot_summary(snapshot: dict, label: str) -> None:
    print(f"Git {label} snapshot:")
    print(f"repo_path: {snapshot['repo_path']}")
    print(f"is_git_repo: {str(snapshot['is_git_repo']).lower()}")
    print(f"branch: {snapshot['branch'] or ''}")
    print(f"head: {_short_hash(snapshot['head']) or ''}")
    print(f"dirty: {str(_working_tree_dirty(snapshot)).lower()}")
    print(f"changed_files_count: {_changed_files_count(snapshot)}")
    if snapshot["validation_error"]:
        print(f"validation_error: {snapshot['validation_error']}")
    sys.stdout.flush()


def _classification_message(classification: dict) -> str:
    return (
        f"total_files={classification['total_files']} "
        f"category_counts={classification['counts_by_category']} "
        f"risk_counts={classification['counts_by_risk_level']} "
        f"high_risk_file_count={len(classification['high_risk_files'])}"
    )


def _print_changed_file_classification(classification: dict) -> None:
    print("Changed-file classification:")
    print(f"total_files: {classification['total_files']}")
    print(f"counts_by_category: {classification['counts_by_category']}")
    print(f"counts_by_risk_level: {classification['counts_by_risk_level']}")
    print(f"high_risk_files: {classification['high_risk_files']}")
    sys.stdout.flush()


def _print_governance_observation(observation: dict) -> None:
    print("Run governance observation:")
    print(f"prompt_contract_confidence: {observation['prompt_contract_confidence']}")
    print(f"scope_observation: {observation['scope_observation']}")
    print(f"attributable_changed_files: {observation['attributable_changed_files']}")
    print(f"preexisting_changed_files: {observation['preexisting_changed_files']}")
    print(f"preexisting_untracked_files: {observation['preexisting_untracked_files']}")
    print(f"contract_mismatches: {observation['contract_mismatches']}")
    print(f"objective_failures: {observation['objective_failures']}")
    sys.stdout.flush()


def _diagnostics_message(diagnostics: dict | None) -> str:
    if diagnostics is None:
        return "diagnostics_unavailable"
    return (
        f"outcome={diagnostics['outcome']} "
        f"attention_level={diagnostics['attention_level']} "
        f"flags={diagnostics['flags']}"
    )


def _print_prompt_repo_impact_diagnostics(diagnostics: dict | None) -> None:
    print("Prompt/repo impact diagnostics:")
    if diagnostics is None:
        print("unavailable")
        sys.stdout.flush()
        return
    print(f"outcome: {diagnostics['outcome']}")
    print(f"attention_level: {diagnostics['attention_level']}")
    print(f"prompt_intents: {diagnostics['prompt_intents']}")
    print(f"flags: {diagnostics['flags']}")
    print(f"messages: {diagnostics['messages']}")
    sys.stdout.flush()


def _supervision_decision_message(decision: dict) -> str:
    return (
        f"decision={decision['decision']} "
        f"attention_level={decision['attention_level']} "
        f"approval_required={decision['approval_required']} "
        f"reasons={decision['reasons']}"
    )


def _print_supervision_decision(decision: dict) -> None:
    print("Supervision decision:")
    print(f"decision: {decision['decision']}")
    print(f"attention_level: {decision['attention_level']}")
    print(f"approval_required: {decision['approval_required']}")
    print(f"needs_review: {decision['needs_review']}")
    print(f"reasons: {decision['reasons']}")
    print(f"messages: {decision['messages']}")
    sys.stdout.flush()


def _run_status_transition_message(transition: dict) -> str:
    return (
        f"previous_status={transition['previous_status']} "
        f"next_status={transition['next_status']} "
        f"reason={transition['reason']}"
    )


def _print_run_status_transition(transition: dict) -> None:
    print("Run status transition:")
    print(f"previous_status: {transition['previous_status']}")
    print(f"next_status: {transition['next_status']}")
    print(f"reason: {transition['reason']}")
    print(f"should_auto_complete: {transition['should_auto_complete']}")
    sys.stdout.flush()


def _continuation_check_message(result: dict) -> str:
    return (
        f"can_continue={result['can_continue']} "
        f"status={result['status']} "
        f"reason={result['reason']}"
    )


def _print_continuation_check(run_id: str, result: dict) -> None:
    print(f"run_id: {run_id}")
    print(f"status: {result['status']}")
    print(f"can_continue: {result['can_continue']}")
    print(f"reason: {result['reason']}")
    print(f"required_action: {result['required_action'] or ''}")
    sys.stdout.flush()


def _print_activate_chatgpt_result(result: dict) -> None:
    print(f"app_name: {result['app_name']}")
    print(f"activated: {str(result['activated']).lower()}")
    print(f"frontmost_app: {result['frontmost_app'] or ''}")
    print(f"is_frontmost: {str(result['is_frontmost']).lower()}")
    print(f"error: {result['error'] or ''}")
    sys.stdout.flush()


def _ui_element_label(element: dict) -> str:
    labels = []
    for key in ("role", "subrole", "name", "title", "description", "help", "value"):
        value = element.get(key)
        if value:
            labels.append(f"{key}={value!r}")
    if element.get("enabled") is not None:
        labels.append(f"enabled={str(element['enabled']).lower()}")
    if element.get("focused") is not None:
        labels.append(f"focused={str(element['focused']).lower()}")
    if element.get("x") is not None and element.get("y") is not None:
        labels.append(f"pos=({element['x']},{element['y']})")
    if element.get("width") is not None and element.get("height") is not None:
        labels.append(f"size=({element['width']}x{element['height']})")
    return ", ".join(labels) if labels else "(no accessible details)"


def _print_ui_element_list(title: str, elements: list[dict], limit: int = 12) -> None:
    print(f"{title}: {len(elements)}")
    for index, element in enumerate(elements[:limit], start=1):
        window_index = element.get("window_index")
        depth = element.get("depth")
        prefix = f"  {index}."
        if window_index is not None:
            prefix += f" window={window_index}"
        if depth is not None:
            prefix += f" depth={depth}"
        print(f"{prefix} {_ui_element_label(element)}")
    if len(elements) > limit:
        print(f"  ... {len(elements) - limit} more omitted")


def _print_inspect_chatgpt_ui_result(result: dict) -> None:
    print(f"activated: {str(result['activated']).lower()}")
    print(f"frontmost_app: {result['frontmost_app'] or ''}")
    _print_ui_element_list("windows", result["windows"])
    print("focused_element:")
    if result["focused_element"] is None:
        print("  (none)")
    else:
        print(f"  {_ui_element_label(result['focused_element'])}")
    _print_ui_element_list("text_input_candidates", result["text_input_candidates"])
    _print_ui_element_list("button_candidates", result.get("button_candidates", []), limit=16)
    print(f"errors: {len(result['errors'])}")
    for error in result["errors"]:
        print(f"  {error}")
    sys.stdout.flush()


def _print_chatgpt_target_paste_result(
    activation_result: dict,
    copy_result: dict,
    paste_result: dict,
    marker: str,
) -> None:
    print(f"activated: {str(activation_result['activated']).lower()}")
    print(f"frontmost_app: {activation_result['frontmost_app'] or ''}")
    print(f"is_frontmost: {str(activation_result['is_frontmost']).lower()}")
    print(f"copied: {str(copy_result['copied']).lower()}")
    print(f"copy_method: {copy_result['method'] or ''}")
    print(f"pasted: {str(paste_result['pasted']).lower()}")
    print(f"paste_method: {paste_result['method'] or ''}")
    print(f"marker: {marker}")
    print(f"activation_error: {activation_result['error'] or ''}")
    print(f"copy_error: {copy_result['error'] or ''}")
    print(f"paste_error: {paste_result['error'] or ''}")
    print("No submit/Enter was sent.")
    sys.stdout.flush()


def _print_chatgpt_feedback_paste_result(
    run_id: str,
    copy_result: dict,
    activation_result: dict,
    paste_result: dict,
    output_path: Path | None,
    error: str | None = None,
) -> None:
    print(f"run_id: {run_id}")
    print(f"copied: {str(copy_result['copied']).lower()}")
    print(f"copy_method: {copy_result['method'] or ''}")
    print(f"activated: {str(activation_result['activated']).lower()}")
    print(f"frontmost_app: {activation_result['frontmost_app'] or ''}")
    print(f"is_frontmost: {str(activation_result['is_frontmost']).lower()}")
    print(f"pasted: {str(paste_result['pasted']).lower()}")
    print(f"paste_method: {paste_result['method'] or ''}")
    print(f"output_path: {str(output_path) if output_path is not None else ''}")
    print(f"copy_error: {copy_result['error'] or ''}")
    print(f"activation_error: {activation_result['error'] or ''}")
    print(f"paste_error: {paste_result['error'] or ''}")
    print(f"error: {error or ''}")
    print("No submit/Enter was sent.")
    sys.stdout.flush()


def _print_chatgpt_feedback_submit_result(
    run_id: str,
    copy_result: dict,
    activation_result: dict,
    paste_result: dict,
    submit_result: dict,
    output_path: Path | None,
    error: str | None = None,
    verification: dict | None = None,
) -> None:
    print(f"run_id: {run_id}")
    print(f"copied: {str(copy_result['copied']).lower()}")
    print(f"copy_method: {copy_result['method'] or ''}")
    print(f"activated: {str(activation_result['activated']).lower()}")
    print(f"frontmost_app: {activation_result['frontmost_app'] or ''}")
    print(f"is_frontmost: {str(activation_result['is_frontmost']).lower()}")
    print(f"pasted: {str(paste_result['pasted']).lower()}")
    print(f"paste_method: {paste_result['method'] or ''}")
    submit_input_sent = submit_result.get("submit_input_sent")
    if submit_input_sent is None:
        submit_input_sent = bool(submit_result.get("submitted") or submit_result.get("pressed"))
    print(f"submit_input_sent: {str(bool(submit_input_sent)).lower()}")
    print(f"submit_method: {submit_result['method'] or ''}")
    if verification is not None:
        print(f"submission_verified: {str(bool(verification.get('ok'))).lower()}")
        print(f"submission_reason: {verification.get('reason_code') or ''}")
    print(f"output_path: {str(output_path) if output_path is not None else ''}")
    print(f"copy_error: {copy_result['error'] or ''}")
    print(f"activation_error: {activation_result['error'] or ''}")
    print(f"paste_error: {paste_result['error'] or ''}")
    print(f"submit_error: {submit_result['error'] or ''}")
    print(f"error: {error or ''}")
    if verification is not None and verification.get("ok"):
        print("Feedback submission verified through ChatGPT Accessibility state.")
    elif submit_input_sent:
        print("Submit input sent; ChatGPT submission was not verified.")
    else:
        print("Submit input was not sent.")
    sys.stdout.flush()


def _print_chatgpt_ax_capture_result(
    run_id: str,
    activation_result: dict,
    capture_result: dict,
    ledger_event: str | None,
) -> None:
    print(f"run_id: {run_id}")
    print(f"activated: {str(activation_result['activated']).lower()}")
    print(f"frontmost_app: {activation_result['frontmost_app'] or ''}")
    print(f"is_frontmost: {str(activation_result['is_frontmost']).lower()}")
    print(f"matched_feedback: {str(capture_result.get('matched_feedback', False)).lower()}")
    print(f"matched_submission_marker: {str(capture_result.get('matched_submission_marker', False)).lower()}")
    print(f"candidate_count: {capture_result.get('candidate_count', 0)}")
    print(f"response_length: {capture_result.get('response_length', 0)}")
    print(f"response_sha256: {capture_result.get('response_sha256', '')}")
    print(f"stable: {str(capture_result.get('stable', False)).lower()}")
    print(f"capture_reason: {capture_result.get('reason_code', '')}")
    print(f"sentinel_state: {capture_result.get('sentinel_state', '')}")
    print(f"ledger_event: {ledger_event or ''}")
    if capture_result.get("sentinel_required"):
        print("sentinel_required: true")
    if "matched_candidate_index" in capture_result:
        print(f"matched_candidate_index: {capture_result.get('matched_candidate_index')}")
    if "matched_candidate_path" in capture_result:
        print(f"matched_candidate_path: {capture_result.get('matched_candidate_path')}")
    for summary in capture_result.get("post_feedback_candidate_summaries", ()):
        omitted_count = summary.get("omitted_count")
        if omitted_count is not None:
            print(f"post_feedback_candidate_omitted_count: {omitted_count}")
            continue
        print(
            "post_feedback_candidate: "
            f"index={summary.get('index')} "
            f"path={summary.get('path')} "
            f"length={summary.get('length')} "
            f"sha256={summary.get('sha256')} "
            f"sentinel_status={summary.get('sentinel_status')} "
            f"classification={summary.get('candidate_classification')} "
            f"classification_reason={summary.get('classification_reason')} "
            f"preview={summary.get('text_preview_repr')}"
        )
    print(f"activation_error: {activation_result['error'] or ''}")
    print(f"error: {capture_result.get('error', '')}")
    sys.stdout.flush()


def _prompt_preview(prompt_text: str, limit: int = 500) -> str:
    if len(prompt_text) <= limit:
        return prompt_text
    return f"{prompt_text[:limit]}\n... (truncated)"


def _print_next_codex_prompt_extraction_result(
    run_id: str,
    selection: object,
    extraction: object | None,
    output_path: Path | None = None,
    ledger_event: str | None = None,
) -> None:
    source_event = getattr(selection, "source_event", None)
    submitted_event = getattr(selection, "submitted_event", None)
    source_event_id = source_event.get("id") if isinstance(source_event, dict) else None
    submitted_event_id = submitted_event.get("id") if isinstance(submitted_event, dict) else None
    selection_warnings = list(getattr(selection, "warnings", ()))
    extraction_warnings = list(getattr(extraction, "warnings", ())) if extraction is not None else []
    error = getattr(extraction, "error", None) if extraction is not None else getattr(selection, "error", None)
    prompt_text = getattr(extraction, "prompt_text", "") if extraction is not None else ""
    selected_prompt_index = (
        getattr(extraction, "selected_prompt_index", None) if extraction is not None else None
    )

    print(f"run_id: {run_id}")
    print(f"source_event_id: {source_event_id or ''}")
    print(f"matched_submission_event_id: {submitted_event_id or ''}")
    print(f"extraction_method: {getattr(extraction, 'extraction_method', None) or ''}")
    print(f"prompt_found: {str(bool(getattr(extraction, 'ok', False))).lower()}")
    print(f"prompt_length: {getattr(extraction, 'prompt_length', 0) if extraction is not None else 0}")
    print(f"prompt_sha256: {getattr(extraction, 'prompt_sha256', '') if extraction is not None else ''}")
    print(
        "safety_status: "
        f"{getattr(extraction, 'safety_status', 'requires_human_review') if extraction is not None else 'requires_human_review'}"
    )
    print(f"prompt_count_detected: {getattr(extraction, 'prompt_count_detected', 0) if extraction is not None else 0}")
    print(f"selected_prompt_index: {selected_prompt_index if selected_prompt_index is not None else ''}")
    print(f"output_path: {str(output_path) if output_path is not None else ''}")
    print(f"ledger_event: {ledger_event or ''}")
    for warning in selection_warnings + extraction_warnings:
        print(f"warning: {warning}")
    print(f"error: {error or ''}")
    if prompt_text:
        print("prompt_preview:")
        print(_prompt_preview(prompt_text))
    print("No Codex execution was performed.")
    sys.stdout.flush()


def _print_human_decision(previous_status: str, next_status: str, note: str) -> None:
    print(f"previous_status: {previous_status}")
    print(f"next_status: {next_status}")
    print(f"note: {note}")
    sys.stdout.flush()


def _write_feedback_output(output_path_text: str, message: str) -> Path:
    output_path = Path(output_path_text).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(message, encoding="utf-8")
    return output_path


def _write_text_output(output_path: Path, text: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def _resolve_flagged_run(
    run_id: str,
    run: dict,
    note: str,
    allowed_statuses: set[str],
    next_status: RunStatus,
    allowed_event_type: str,
    allowed_message: str,
    rejected_event_type: str,
    action_label: str,
) -> None:
    current_status = run["status"]
    if current_status not in allowed_statuses:
        allowed_statuses_text = ", ".join(sorted(allowed_statuses))
        message = (
            f"Cannot {action_label} run from current status "
            f"{current_status!r}. Allowed statuses: {allowed_statuses_text}."
        )
        ledger.add_event(
            run_id,
            rejected_event_type,
            message,
            {
                "current_status": current_status,
                "note": note,
            },
        )
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)

    previous_status = current_status
    ledger.update_run_status(
        run_id,
        next_status,
        final_summary=run["final_summary"],
        error=run["error"],
    )
    ledger.add_event(
        run_id,
        allowed_event_type,
        allowed_message,
        {
            "previous_status": previous_status,
            "next_status": next_status.value,
            "note": note,
        },
    )
    _print_human_decision(previous_status, next_status.value, note)


def _codex_exec_validation_result(
    prompt: str,
    repo_path: str,
    sandbox: str,
    validation_error: str,
) -> dict:
    now = datetime.now(UTC).isoformat()
    codex_path = shutil.which("codex")
    return {
        "mode": "exec",
        "found": codex_path is not None,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, prompt],
        "exit_code": 2,
        "stdout": "",
        "stderr": f"{validation_error}\n",
        "timed_out": False,
        "started_at": now,
        "finished_at": now,
        "validation_error": validation_error,
    }


def _delta_name_status(delta: dict | None) -> str:
    if not isinstance(delta, dict):
        return ""
    statuses = {
        "modified": "M",
        "added": "A",
        "deleted": "D",
        "renamed": "R",
    }
    lines = []
    for detail in delta.get("path_delta_details", []):
        if not isinstance(detail, dict):
            continue
        path = str(detail.get("path") or "").strip()
        if not path:
            continue
        status = statuses.get(str(detail.get("change_type") or ""), "M")
        lines.append(f"{status}\t{path}")
    return "\n".join(lines)


def _delta_diff_text(delta: dict | None) -> str:
    if not isinstance(delta, dict):
        return ""
    chunks = []
    for detail in delta.get("path_delta_details", []):
        if isinstance(detail, dict) and isinstance(detail.get("diff_unified_zero"), str):
            chunks.append(detail["diff_unified_zero"])
    return "\n".join(chunk for chunk in chunks if chunk)


def _path_is_related_focused_test(path: str) -> bool:
    lower = path.lower()
    return lower.startswith("tests/") or "/tests/" in f"/{lower}/" or Path(path).name.lower().startswith("test_")


def _contract_allowed_path_mismatches(contract: dict, paths: list[str]) -> list[dict]:
    allowed_items = contract.get("allowed_paths")
    if not isinstance(allowed_items, list) or not allowed_items:
        return []
    allowed = {str(item.get("path") or "") for item in allowed_items if isinstance(item, dict)}
    allowed_names = {Path(path).name for path in allowed if path}
    groups = contract.get("allowed_path_groups")
    if not isinstance(groups, list):
        groups = []
    allows_related_tests = any(
        isinstance(item, dict) and item.get("kind") == "related_focused_tests"
        for item in groups
    )
    mismatches = []
    for path in paths:
        if path in allowed or Path(path).name in allowed_names:
            continue
        if allows_related_tests and _path_is_related_focused_test(path):
            continue
        mismatches.append({"type": "path_outside_explicit_contract", "path": path})
    return mismatches


def _path_matches_excluded_area(path: str, category: str | None, area: str) -> bool:
    lower = path.lower()
    category = category or ""
    if area == "database":
        return category == "database_migration" or lower.endswith(".sql") or "migration" in lower or "database" in lower
    if area == "configuration":
        return category in {"config", "dependency_manifest", "build_or_ci"} or lower.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"))
    if area == "auth":
        return category == "auth_security" or "auth" in lower or "session" in lower
    if area == "infrastructure":
        return category == "infrastructure" or "deploy" in lower or "docker" in lower or ".github/" in lower
    if area == "backend":
        return any(marker in lower for marker in ("server/", "api/", "backend/", "workers/", "functions/", "database/", "supabase/"))
    if area == "networking":
        return any(marker in lower for marker in ("network", "http", "api/", "client", "request"))
    return False


def _contract_exclusion_mismatches(contract: dict, paths: list[str], classification: dict | None) -> list[dict]:
    excluded = contract.get("excluded_areas")
    if not isinstance(excluded, list) or not excluded:
        return []
    files = classification.get("files") if isinstance(classification, dict) else []
    categories = {
        str(file.get("path") or ""): str(file.get("category") or "")
        for file in files
        if isinstance(file, dict)
    }
    mismatches = []
    for item in excluded:
        if not isinstance(item, dict):
            continue
        area = str(item.get("area") or "")
        for path in paths:
            if _path_matches_excluded_area(path, categories.get(path), area):
                mismatches.append({"type": "excluded_area_changed", "area": area, "path": path})
    return mismatches


def _explicit_guardrails(contract: dict) -> list[str]:
    guardrails = []
    read_only = contract.get("read_only") if isinstance(contract.get("read_only"), dict) else {}
    if read_only.get("explicit"):
        guardrails.append("read_only")
    for item in contract.get("allowed_paths", []):
        if isinstance(item, dict) and item.get("path"):
            guardrails.append(f"{item.get('mode') or 'allowed'}:{item['path']}")
    for item in contract.get("excluded_areas", []):
        if isinstance(item, dict) and item.get("area"):
            guardrails.append(f"exclude:{item['area']}")
    return guardrails


def _build_governance_observation(
    contract: dict,
    delta: dict | None,
    classification: dict | None,
    sandbox: str,
    before_snapshot: dict | None,
) -> dict:
    paths = attributable_paths(delta)
    read_only = contract.get("read_only") if isinstance(contract.get("read_only"), dict) else {}
    contract_mismatches = []
    if read_only.get("explicit") and paths:
        contract_mismatches.append({"type": "explicit_read_only_changed_files", "paths": paths})
    contract_mismatches.extend(_contract_allowed_path_mismatches(contract, paths))
    contract_mismatches.extend(_contract_exclusion_mismatches(contract, paths, classification))

    diff_text = _delta_diff_text(delta)
    content_flags = diff_content_flags(diff_text)
    objective_failures = []
    path_safety = contract.get("path_safety") if isinstance(contract.get("path_safety"), dict) else {}
    if path_safety.get("valid") is False:
        objective_failures.append("invalid_contract_path")
    if sandbox == "read-only" and paths:
        objective_failures.append("read_only_sandbox_attributable_write")
    if "high_confidence_secret_literal" in content_flags:
        objective_failures.append("high_confidence_secret_literal")

    scope_observation = "matched"
    if contract_mismatches:
        scope_observation = "partially_matched"
    if not paths and _explicit_guardrails(contract):
        scope_observation = "not_evaluable" if delta and delta.get("validation_error") else "matched"

    observation_flags = []
    if before_snapshot and _working_tree_dirty(before_snapshot):
        observation_flags.append("repo_dirty_before_codex")
    observation_flags.extend(flag for flag in content_flags if flag != "high_confidence_secret_literal")

    return {
        "governance_version": "explicit_contract_delta_v1",
        "prompt_contract_confidence": contract.get("confidence", "low"),
        "scope_observation": scope_observation,
        "attributable_changed_files": paths,
        "preexisting_changed_files": (delta or {}).get("preexisting_changed_files", []),
        "preexisting_untracked_files": (delta or {}).get("preexisting_untracked_files", []),
        "explicit_guardrails": _explicit_guardrails(contract),
        "observation_flags": observation_flags,
        "contract_mismatches": contract_mismatches,
        "objective_failures": objective_failures,
        "requires_future_review": bool(contract_mismatches),
    }


def _governance_transition_if_blocking(observation: dict, current_transition: dict) -> dict:
    objective_failures = observation.get("objective_failures")
    if not objective_failures:
        return current_transition
    return {
        **current_transition,
        "next_status": RunStatus.NEEDS_REVIEW.value,
        "reason": "objective_governance_failure",
        "decision": "objective_failure",
        "approval_required": False,
        "needs_review": True,
        "should_auto_complete": False,
        "objective_failures": objective_failures,
    }


def _run_codex_exec_flow(
    run_id: str,
    run: dict,
    prompt: str,
    repo_path_text: str,
    sandbox: str,
    timeout: int,
    confirm_full_access: bool,
) -> dict:
    prompt_contract = parse_prompt_contract(prompt, sandbox).to_dict()
    git_snapshot = capture_git_snapshot(repo_path_text)
    invocation_state_before = capture_invocation_git_state(repo_path_text)
    after_git_snapshot = None
    invocation_state_after = None
    invocation_delta = None
    governance_observation = None
    changed_file_classification = None
    ledger.add_event(
        run_id,
        "git_snapshot_before_codex",
        _snapshot_message(git_snapshot),
        git_snapshot,
    )
    _print_git_snapshot_summary(git_snapshot, "before")

    ledger.add_event(
        run_id,
        "prompt_contract_parsed",
        f"Parsed prompt contract confidence={prompt_contract['confidence']}.",
        prompt_contract,
    )
    ledger.add_event(
        run_id,
        "invocation_git_state_before",
        "Captured pre-Codex invocation git state.",
        invocation_state_before,
    )

    ledger.add_event(
        run_id,
        "codex_exec_started",
        "Running Codex exec.",
        {
            "prompt": prompt,
            "repo_path": repo_path_text,
            "timeout": timeout,
            "sandbox": sandbox,
            "prompt_contract": prompt_contract,
        },
    )

    validation_error = None
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        validation_error = (
            "Invalid Codex sandbox. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_SANDBOXES)}."
        )
    elif not prompt_contract["path_safety"]["valid"]:
        invalid_paths = ", ".join(prompt_contract["path_safety"]["invalid_paths"])
        validation_error = f"Prompt contract contains invalid path references: {invalid_paths}"
    elif sandbox == "danger-full-access" and not confirm_full_access:
        validation_error = "Codex sandbox danger-full-access requires --confirm-full-access."

    if validation_error is None:
        result = run_codex_exec(
            prompt,
            repo_path=repo_path_text,
            timeout_seconds=timeout,
            sandbox=sandbox,
        )
    else:
        result = _codex_exec_validation_result(
            prompt,
            repo_path=repo_path_text,
            sandbox=sandbox,
            validation_error=validation_error,
        )

    validation_message = (
        f" validation_error={result['validation_error']}"
        if result["validation_error"]
        else ""
    )
    ledger.add_event(
        run_id,
        "codex_exec_finished",
        (
            f"found={result['found']} exit_code={result['exit_code']} "
            f"timed_out={result['timed_out']} repo_path={result['repo_path']} "
            f"sandbox={result['sandbox']}{validation_message}"
        ),
        result,
    )
    if result["validation_error"]:
        print(f"error: {result['validation_error']}", file=sys.stderr)
    _print_codex_exec_result(result)

    if not result["validation_error"]:
        after_git_snapshot = capture_git_snapshot(repo_path_text)
        invocation_state_after = capture_invocation_git_state(repo_path_text)
        invocation_delta = compute_invocation_delta(invocation_state_before, invocation_state_after)
        ledger.add_event(
            run_id,
            "git_snapshot_after_codex",
            _snapshot_message(after_git_snapshot),
            after_git_snapshot,
        )
        _print_git_snapshot_summary(after_git_snapshot, "after")
        ledger.add_event(
            run_id,
            "invocation_git_state_after",
            "Captured post-Codex invocation git state.",
            invocation_state_after,
        )
        ledger.add_event(
            run_id,
            "invocation_delta_attributed",
            (
                "Attributed invocation delta "
                f"files={len(attributable_paths(invocation_delta))}."
            ),
            invocation_delta,
        )

        changed_file_classification = classify_changed_files(
            attributable_paths(invocation_delta)
        )
        ledger.add_event(
            run_id,
            "changed_file_classification",
            _classification_message(changed_file_classification),
            changed_file_classification,
        )
        _print_changed_file_classification(changed_file_classification)

    try:
        prompt_repo_impact_diagnostics = analyze_prompt_repo_impact(
            prompt,
            result,
            git_snapshot,
            after_git_snapshot,
            changed_file_classification,
        )
    except Exception as exc:
        prompt_repo_impact_diagnostics = None
        print(f"warning: prompt/repo impact diagnostics unavailable: {exc}", file=sys.stderr)
    ledger.add_event(
        run_id,
        "prompt_repo_impact_diagnostics",
        _diagnostics_message(prompt_repo_impact_diagnostics),
        prompt_repo_impact_diagnostics,
    )
    _print_prompt_repo_impact_diagnostics(prompt_repo_impact_diagnostics)

    supervision_decision = evaluate_supervision_decision(prompt_repo_impact_diagnostics)
    ledger.add_event(
        run_id,
        "supervision_decision",
        _supervision_decision_message(supervision_decision),
        supervision_decision,
    )
    _print_supervision_decision(supervision_decision)

    transition = status_from_supervision_decision(supervision_decision, result)
    if not result["validation_error"]:
        governance_observation = _build_governance_observation(
            prompt_contract,
            invocation_delta,
            changed_file_classification,
            sandbox,
            git_snapshot,
        )
        ledger.add_event(
            run_id,
            "run_governance_observation",
            (
                "Recorded explicit-contract and attributable-delta governance "
                f"observation scope={governance_observation['scope_observation']}."
            ),
            governance_observation,
        )
        _print_governance_observation(governance_observation)
        transition = _governance_transition_if_blocking(governance_observation, transition)
        if sandbox == "workspace-write":
            _verify_auto_workspace_write_result(
                run_id,
                repo_path_text,
                sha256_text(prompt),
                {},
                changed_file_classification,
                invocation_delta,
            )
    transition = {
        **transition,
        "previous_status": run["status"],
        "next_status": transition["next_status"],
    }
    ledger.update_run_status(run_id, RunStatus(transition["next_status"]))
    ledger.add_event(
        run_id,
        "run_status_transition",
        _run_status_transition_message(transition),
        transition,
    )
    _print_run_status_transition(transition)

    return {
        "result": result,
        "git_snapshot": git_snapshot,
        "after_git_snapshot": after_git_snapshot,
        "invocation_state_before": invocation_state_before,
        "invocation_state_after": invocation_state_after,
        "invocation_delta": invocation_delta,
        "prompt_contract": prompt_contract,
        "governance_observation": governance_observation,
        "changed_file_classification": changed_file_classification,
        "prompt_repo_impact_diagnostics": prompt_repo_impact_diagnostics,
        "supervision_decision": supervision_decision,
        "transition": transition,
    }


def _print_extracted_codex_prompt_preview(
    run_id: str,
    selection: object,
    repo_path: str,
    sandbox: str,
) -> None:
    event = getattr(selection, "event", None)
    extraction_event_id = event.get("id") if isinstance(event, dict) else None
    print(f"run_id: {run_id}")
    print(f"extraction_event_id: {extraction_event_id or ''}")
    print(f"prompt_sha256: {getattr(selection, 'prompt_sha256', '')}")
    print(f"prompt_length: {getattr(selection, 'prompt_length', 0)}")
    print(f"repo_path: {repo_path}")
    print(f"sandbox: {sandbox}")
    for warning in getattr(selection, "warnings", ()):
        print(f"warning: {warning}")
    print("prompt_preview:")
    print(_prompt_preview(getattr(selection, "prompt_text", "")))
    sys.stdout.flush()


def _print_extracted_codex_prompt_run_result(
    run_id: str,
    selection: object,
    repo_path: str,
    sandbox: str,
    flow: dict,
) -> None:
    event = getattr(selection, "event", None)
    extraction_event_id = event.get("id") if isinstance(event, dict) else None
    result = flow["result"]
    supervision_decision = flow["supervision_decision"] or {}
    transition = flow["transition"] or {}
    print("Extracted Codex prompt run:")
    print(f"run_id: {run_id}")
    print(f"extraction_event_id: {extraction_event_id or ''}")
    print(f"prompt_sha256: {getattr(selection, 'prompt_sha256', '')}")
    print(f"prompt_length: {getattr(selection, 'prompt_length', 0)}")
    print(f"repo_path: {repo_path}")
    print(f"sandbox: {sandbox}")
    print(f"codex_exit_code: {result['exit_code']}")
    print(f"codex_timed_out: {result['timed_out']}")
    print(f"supervision_decision: {supervision_decision.get('decision', '')}")
    print(f"status: {transition.get('next_status', '')}")
    print("Extracted prompt was run only because --confirm-run was provided.")
    sys.stdout.flush()


def _confirm_yes_no(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Stopped. No action was taken.")
        return False
    return answer in {"y", "yes"}


def _print_supervise_stop(plan: object) -> None:
    print("Stopped.")
    print(f"Reason: {getattr(plan, 'stop_message', '') or getattr(plan, 'reason', '')}")
    status = getattr(plan, "status", "")
    if status:
        print(f"Status: {status}")
    for warning in getattr(plan, "warnings", ()):
        print(f"Warning: {warning}")
    print("No ChatGPT submission or Codex execution was performed.")
    sys.stdout.flush()


def _print_supervise_send_gate(run_id: str, plan: object) -> None:
    print("Codex result is ready.")
    print()
    print(f"Run: {run_id}")
    print(f"Exit code: {getattr(plan, 'codex_exit_code', '')}")
    print(f"Sandbox used: {getattr(plan, 'codex_sandbox', '')}")
    changed_files_count = getattr(plan, "changed_files_count", None)
    print(f"Changed files: {changed_files_count if changed_files_count is not None else 'unknown'}")
    print(f"Supervision: {getattr(plan, 'supervision_decision', '') or 'unknown'}")
    warnings = tuple(getattr(plan, "warnings", ()))
    print(f"Warnings: {', '.join(warnings) if warnings else 'none'}")
    print()
    sys.stdout.flush()


def _print_supervise_run_gate(plan: object, repo_snapshot: dict | None) -> None:
    print("ChatGPT provided the next Codex prompt.")
    print()
    print("Prompt preview:")
    print(getattr(plan, "prompt_preview", "") or "(empty)")
    print()
    print(f"Prompt SHA-256: {getattr(plan, 'prompt_sha', '')}")
    print(f"Repo: {getattr(plan, 'repo_path', '')}")
    print(f"Sandbox: {getattr(plan, 'sandbox', '')}")
    policy_reason = getattr(plan, "prompt_auto_run_reason", "")
    if policy_reason:
        print(f"Auto policy: {policy_reason}")

    warnings = list(getattr(plan, "warnings", ()))
    if repo_snapshot is not None and _working_tree_dirty(repo_snapshot):
        warnings.append("working tree is currently dirty")
    print(f"Warnings: {', '.join(warnings) if warnings else 'none'}")

    if repo_snapshot is not None and _working_tree_dirty(repo_snapshot):
        print("Current git status:")
        status_short = repo_snapshot.get("status_short") or ""
        for line in status_short.splitlines()[:20]:
            print(f"  {line}")
        if len(status_short.splitlines()) > 20:
            print("  ... (truncated)")
    print()
    sys.stdout.flush()


def _print_supervise_sentinel_requirement() -> None:
    print("Expected exactly one next prompt wrapped as:")
    print("BEGIN_NEXT_CODEX_PROMPT")
    print("...")
    print("END_NEXT_CODEX_PROMPT")
    sys.stdout.flush()


def _submit_feedback_to_chatgpt_flow(
    run_id: str,
    run: dict,
    app_name: str,
    output_path_text: str | None = None,
    approval_mode: str = "human",
) -> bool:
    events = ledger.list_events(run_id)
    feedback = build_gpt_feedback_message(run, events)
    output_path = None

    if output_path_text:
        output_path = _write_feedback_output(output_path_text, feedback["message"])

    marker_metadata = _marker_metadata(feedback)
    generation_metadata = {
        "run_id": feedback["run_id"],
        "status": feedback["status"],
        "codex_exit_code": feedback["codex_exit_code"],
        "codex_timed_out": feedback["codex_timed_out"],
        "changed_files": feedback["changed_files"],
        "message_length": len(feedback["message"]),
        "feedback_payload_version": feedback["feedback_payload_version"],
        "feedback_payload_sha256": feedback["feedback_payload_sha256"],
        "feedback_payload_length": feedback["feedback_payload_length"],
        **marker_metadata,
        "approval_mode": approval_mode,
        "target": "ChatGPT",
        "app_name": app_name,
        "targeted_chatgpt": True,
    }
    if output_path is not None:
        generation_metadata["output_path"] = str(output_path)
    ledger.add_event(
        run_id,
        "gpt_feedback_generated",
        "Generated GPT feedback message for ChatGPT-targeted submit.",
        generation_metadata,
    )

    copy_result = copy_to_clipboard(feedback["message"])
    ledger.add_event(
        run_id,
        "gpt_feedback_copied",
        (
            "Copied GPT feedback message to clipboard for ChatGPT-targeted submit."
            if copy_result["copied"]
            else f"Failed to copy GPT feedback message to clipboard: {copy_result['error']}"
        ),
        {
            "run_id": feedback["run_id"],
            "copied": copy_result["copied"],
            "method": copy_result["method"],
            "error": copy_result["error"],
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
        },
    )

    if not copy_result["copied"]:
        activation_result = {
            "activated": False,
            "app_name": app_name,
            "frontmost_app": None,
            "is_frontmost": False,
            "activation_result": None,
            "frontmost_result": None,
            "error": "Skipped activation because copying GPT feedback to clipboard failed.",
        }
        paste_result = {
            "pasted": False,
            "method": PASTE_METHOD,
            "error": "Skipped paste because copying GPT feedback to clipboard failed.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        submit_result = {
            "submit_input_sent": False,
            "method": ENTER_METHOD,
            "error": "Skipped submit because copying GPT feedback to clipboard failed.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        common_metadata = {
            "run_id": feedback["run_id"],
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "clipboard_copy_failed",
        }
        ledger.add_event(run_id, "gpt_feedback_pasted", "Skipped ChatGPT-targeted paste because copying GPT feedback failed.", common_metadata)
        ledger.add_event(run_id, "gpt_feedback_submission_failed", "ChatGPT feedback submission failed before submit input.", common_metadata)
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, copy_result["error"])
        return False

    activation_result = activate_chatgpt(app_name)
    if not activation_result["is_frontmost"]:
        paste_result = {
            "pasted": False,
            "method": PASTE_METHOD,
            "error": "Skipped paste because ChatGPT was not frontmost.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        submit_result = {
            "submit_input_sent": False,
            "method": ENTER_METHOD,
            "error": "Skipped submit because ChatGPT was not frontmost.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        common_metadata = {
            "run_id": feedback["run_id"],
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_not_frontmost",
        }
        ledger.add_event(run_id, "gpt_feedback_pasted", "Skipped ChatGPT-targeted paste because ChatGPT was not frontmost.", common_metadata)
        ledger.add_event(run_id, "gpt_feedback_submission_failed", "ChatGPT feedback submission failed before submit input.", common_metadata)
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, activation_result["error"])
        return False

    initial_observation = inspect_chatgpt_submission_ui(app_name, marker_text=feedback["submission_marker_text"])
    focused_composer = _focused_composer_from_observation(initial_observation)
    if not initial_observation.get("ok") or focused_composer is None:
        reason_code = (
            "chatgpt_composer_not_focused"
            if (initial_observation.get("text_input_candidates") or [])
            else "chatgpt_composer_not_found"
        )
        paste_result = {
            "pasted": False,
            "method": PASTE_METHOD,
            "error": "Skipped paste because focused ChatGPT composer could not be verified.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        submit_result = {
            "submit_input_sent": False,
            "method": ENTER_METHOD,
            "error": "Skipped submit because focused ChatGPT composer could not be verified.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "composer_observation": _submission_ui_observation_summary(initial_observation, feedback["submission_marker_text"]),
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": reason_code,
        }
        ledger.add_event(run_id, "gpt_feedback_submission_failed", f"ChatGPT feedback submission failed: {reason_code}.", failure_metadata)
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, reason_code)
        return False

    paste_result = paste_clipboard_to_frontmost_app()
    ledger.add_event(
        run_id,
        "gpt_feedback_pasted",
        (
            "Pasted GPT feedback into ChatGPT for submit."
            if paste_result["pasted"]
            else "Failed to paste GPT feedback into ChatGPT for submit."
        ),
        {
            "run_id": feedback["run_id"],
            "activation_result": activation_result,
            "paste_result": paste_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
        },
    )

    if not paste_result["pasted"]:
        submit_result = {
            "submit_input_sent": False,
            "method": ENTER_METHOD,
            "error": "Skipped submit because paste failed.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_paste_input_failed",
        }
        ledger.add_event(run_id, "gpt_feedback_submission_failed", "ChatGPT feedback submission failed before submit input.", failure_metadata)
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, paste_result["error"])
        return False

    paste_verification = _wait_for_pasted_marker(app_name, feedback["submission_marker_text"])
    if not paste_verification["ok"]:
        submit_result = {
            "submit_input_sent": False,
            "method": ENTER_METHOD,
            "error": "Skipped submit because pasted marker was not visible in the composer.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_paste_not_visible",
        }
        ledger.add_event(run_id, "gpt_feedback_submission_failed", "ChatGPT feedback submission failed: pasted marker was not visible.", failure_metadata)
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, "chatgpt_paste_not_visible")
        return False

    time.sleep(CHATGPT_POST_PASTE_SETTLE_SECONDS)
    send_result, send_observation = _select_send_input_method(app_name, feedback["submission_marker_text"])
    submit_input_sent = _submit_input_sent_ok(send_result)
    submit_result = {
        **send_result,
        "submit_input_sent": submit_input_sent,
    }
    ledger.add_event(
        run_id,
        "gpt_feedback_submit_input_sent",
        (
            "Submit input sent; awaiting ChatGPT submission verification."
            if submit_input_sent
            else "Submit input was not confirmed."
        ),
        {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "send_observation": send_observation,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "submit_input_sent" if submit_input_sent else "chatgpt_submit_input_not_confirmed",
        },
    )

    if not submit_input_sent:
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "reason_code": "chatgpt_submit_input_not_confirmed",
        }
        ledger.add_event(run_id, "gpt_feedback_submission_failed", "ChatGPT submit input was not confirmed.", failure_metadata)
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, "chatgpt_submit_input_not_confirmed")
        return False

    verification = _verify_submission_marker(app_name, feedback["submission_marker_text"])
    fallback_attempt_count = 0
    if (
        not verification["ok"]
        and verification.get("reason_code") == "chatgpt_submission_not_verified"
        and verification.get("status", {}).get("composer_contains_marker") is True
        and verification.get("status", {}).get("submitted_candidate_count") == 0
        and _send_result_method(send_result) == "macos_accessibility_axpress_send_button"
    ):
        fallback_attempt_count = 1
        fallback_result = press_enter_in_frontmost_app()
        fallback_submit_result = {**fallback_result, "submit_input_sent": _submit_input_sent_ok(fallback_result)}
        ledger.add_event(
            run_id,
            "gpt_feedback_submit_input_sent",
            "Fallback submit input sent; awaiting ChatGPT submission verification.",
            {
                "run_id": feedback["run_id"],
                "approval_mode": approval_mode,
                "activation_result": activation_result,
                "paste_result": paste_result,
                "paste_verification": paste_verification,
                "submit_input_result": fallback_submit_result,
                "previous_verification": verification,
                "fallback_attempt_count": fallback_attempt_count,
                "message_length": len(feedback["message"]),
                **marker_metadata,
                "target": "ChatGPT",
                "app_name": app_name,
                "targeted_chatgpt": True,
                "reason_code": "submit_input_sent" if fallback_submit_result["submit_input_sent"] else "chatgpt_submit_input_not_confirmed",
            },
        )
        submit_result = fallback_submit_result
        if fallback_submit_result["submit_input_sent"]:
            verification = _verify_submission_marker(app_name, feedback["submission_marker_text"])

    terminal_error = paste_result["error"] or submit_result.get("error")
    if verification["ok"]:
        ledger.add_event(
            run_id,
            "gpt_feedback_submission_verified",
            "Feedback submission verified; waiting for ChatGPT response.",
            {
                "run_id": feedback["run_id"],
                "approval_mode": approval_mode,
                "human_confirmed": approval_mode == "human",
                "auto_executed": approval_mode == "auto",
                "activation_result": activation_result,
                "paste_result": paste_result,
                "paste_verification": paste_verification,
                "submit_input_result": submit_result,
                "submission_verification": verification,
                "fallback_attempt_count": fallback_attempt_count,
                "message_length": len(feedback["message"]),
                **marker_metadata,
                "target": "ChatGPT",
                "app_name": app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
                "reason_code": "chatgpt_submission_verified",
            },
        )
        _print_chatgpt_feedback_submit_result(run_id, copy_result, activation_result, paste_result, submit_result, output_path, terminal_error, verification)
        return True

    event_type = (
        "gpt_feedback_submission_ambiguous"
        if verification.get("reason_code") == "chatgpt_submission_ambiguous"
        else "gpt_feedback_submission_failed"
    )
    ledger.add_event(
        run_id,
        event_type,
        f"ChatGPT feedback submission was not verified: {verification.get('reason_code')}.",
        {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "submission_verification": verification,
            "fallback_attempt_count": fallback_attempt_count,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": verification.get("reason_code") or "chatgpt_submission_not_verified",
        },
    )

    _print_chatgpt_feedback_submit_result(
        run_id,
        copy_result,
        activation_result,
        paste_result,
        submit_result,
        output_path,
        terminal_error or verification.get("reason_code"),
        verification,
    )
    return False


def _capture_gpt_response_from_chatgpt_ax_flow(
    run_id: str,
    run: dict,
    app_name: str,
    timeout_seconds: float,
    stable_seconds: float,
    require_sentinel_response: bool = False,
) -> bool:
    events = ledger.list_events(run_id)
    submitted_event = _latest_verified_gpt_feedback_submission(events)
    if submitted_event is None:
        print("Stopped: no verified ChatGPT submission was found for this run.")
        return False

    submitted_metadata = _event_metadata(submitted_event)
    submission_marker_text = submitted_metadata.get("submission_marker_text")
    submission_marker_sha256 = submitted_metadata.get("submission_marker_sha256")
    if not isinstance(submission_marker_text, str) or not submission_marker_text.strip():
        print("Stopped: verified submission event did not include a submission marker.")
        return False
    if not isinstance(submission_marker_sha256, str) or sha256_text(submission_marker_text) != submission_marker_sha256:
        print("Stopped: verified submission marker hash did not match marker text.")
        return False

    activation_result = activate_chatgpt(app_name)
    if not activation_result["is_frontmost"]:
        capture_result = {
            "matched_feedback": False,
            "candidate_count": 0,
            "response_length": 0,
            "response_sha256": "",
            "stable": False,
            "sentinel_required": require_sentinel_response,
            "error": activation_result["error"] or "ChatGPT was not frontmost.",
        }
        _print_chatgpt_ax_capture_result(run_id, activation_result, capture_result, None)
        return False

    ledger.add_event(
        run_id,
        "gpt_response_capture_started",
        "Assistant response capture started after verified ChatGPT submission.",
        {
            "run_id": run_id,
            "app_name": app_name,
            "matched_submission_event_id": submitted_event.get("id"),
            "source_event_type": "gpt_feedback_submission_verified",
            "submission_marker_text": submission_marker_text,
            "submission_marker_sha256": submission_marker_sha256,
            "submission_marker_nonce": submitted_metadata.get("submission_marker_nonce"),
            "submission_marker_payload_sha256": submitted_metadata.get("submission_marker_payload_sha256"),
            "feedback_payload_sha256": submitted_metadata.get("feedback_payload_sha256"),
            "feedback_payload_version": submitted_metadata.get("feedback_payload_version"),
            "feedback_payload_length": submitted_metadata.get("feedback_payload_length"),
            "timeout_seconds": timeout_seconds,
            "stable_seconds": stable_seconds,
            "sentinel_required": require_sentinel_response,
            "activation_result": activation_result,
        },
    )

    capture_result = capture_response_after_feedback(
        "",
        app_name=app_name,
        timeout_seconds=timeout_seconds,
        stable_seconds=stable_seconds,
        require_sentinel_response=require_sentinel_response,
        submission_marker_text=submission_marker_text,
    )
    if not capture_result["ok"]:
        event_type = "gpt_response_capture_failed"
        ledger.add_event(
            run_id,
            event_type,
            "Failed to capture GPT response from ChatGPT desktop accessibility tree.",
            {
                "run_id": run_id,
                "source": capture_result.get("source"),
                "app_name": app_name,
                "reason_code": capture_result.get("reason_code"),
                "error": capture_result.get("error"),
                "matched_submission_event_id": submitted_event.get("id"),
                "matched_submission_event_type": "gpt_feedback_submission_verified",
                "submission_marker_text": submission_marker_text,
                "submission_marker_sha256": submission_marker_sha256,
                "submission_marker_nonce": submitted_metadata.get("submission_marker_nonce"),
                "submission_marker_payload_sha256": submitted_metadata.get("submission_marker_payload_sha256"),
                "feedback_payload_sha256": submitted_metadata.get("feedback_payload_sha256"),
                "feedback_payload_version": submitted_metadata.get("feedback_payload_version"),
                "feedback_payload_length": submitted_metadata.get("feedback_payload_length"),
                "matched_candidate_index": capture_result.get("matched_candidate_index"),
                "matched_candidate_path": capture_result.get("matched_candidate_path"),
                "response_candidate_index": capture_result.get("response_candidate_index"),
                "response_candidate_path": capture_result.get("response_candidate_path"),
                "candidate_count": capture_result.get("candidate_count", 0),
                "sentinel_state": capture_result.get("sentinel_state"),
                "sentinel_required": require_sentinel_response,
                "post_feedback_candidate_summaries": capture_result.get("post_feedback_candidate_summaries", []),
                "stability": {
                    "stable": capture_result.get("stable", False),
                    "stable_seconds": capture_result.get("stable_seconds"),
                    "successful_polls": capture_result.get("successful_polls"),
                    "poll_interval_seconds": capture_result.get("poll_interval_seconds"),
                    "timeout_seconds": capture_result.get("timeout_seconds"),
                },
                "ax_stats": capture_result.get("ax_stats", {}),
            },
        )
        _print_chatgpt_ax_capture_result(run_id, activation_result, capture_result, event_type)
        return False

    event_type = "gpt_response_captured"
    ledger.add_event(
        run_id,
        event_type,
        "Captured GPT response from ChatGPT desktop accessibility tree.",
        {
            "run_id": run_id,
            "source": capture_result["source"],
            "app_name": app_name,
            "response_text": capture_result["response_text"],
            "response_length": capture_result["response_length"],
            "response_sha256": capture_result["response_sha256"],
            "matched_submission_event_id": submitted_event.get("id"),
            "matched_submission_event_type": "gpt_feedback_submission_verified",
            "submission_marker_text": submission_marker_text,
            "submission_marker_sha256": submission_marker_sha256,
            "submission_marker_nonce": submitted_metadata.get("submission_marker_nonce"),
            "submission_marker_payload_sha256": submitted_metadata.get("submission_marker_payload_sha256"),
            "matched_candidate_index": capture_result["matched_candidate_index"],
            "matched_candidate_path": capture_result["matched_candidate_path"],
            "response_candidate_index": capture_result["response_candidate_index"],
            "response_candidate_path": capture_result["response_candidate_path"],
            "candidate_count": capture_result["candidate_count"],
            "stability": {
                "stable": capture_result["stable"],
                "stable_seconds": capture_result["stable_seconds"],
                "successful_polls": capture_result["successful_polls"],
                "poll_interval_seconds": capture_result["poll_interval_seconds"],
                "timeout_seconds": capture_result["timeout_seconds"],
            },
            "match_score": capture_result["match_score"],
            "capture_format": capture_result["capture_format"],
            "format_warning": capture_result["format_warning"],
            "ax_stats": capture_result["ax_stats"],
            "sentinel_state": capture_result.get("sentinel_state"),
            "reason_code": capture_result.get("reason_code"),
        },
    )
    _print_chatgpt_ax_capture_result(run_id, activation_result, capture_result, event_type)
    return True


def _extract_next_codex_prompt_flow(
    run_id: str,
    require_sentinel: bool = False,
    confirm_extract: bool = True,
    output_path_text: str | None = None,
) -> bool:
    events = ledger.list_events(run_id)
    selection = find_latest_valid_captured_response(events)
    if not selection.ok:
        _print_next_codex_prompt_extraction_result(run_id, selection, None)
        return False

    extraction = extract_next_codex_prompt_from_text(selection.response_text)
    if not extraction.ok:
        _print_next_codex_prompt_extraction_result(run_id, selection, extraction)
        return False
    if require_sentinel and extraction.extraction_method != "sentinel_block":
        _print_next_codex_prompt_extraction_result(run_id, selection, extraction)
        print("Stopped: ChatGPT did not provide a sentinel-wrapped next Codex prompt.")
        _print_supervise_sentinel_requirement()
        return False

    output_path = None
    ledger_event = None
    if confirm_extract:
        output_path = (
            Path(output_path_text).expanduser()
            if output_path_text
            else Path("data") / "runs" / run_id / "next_codex_prompt.md"
        )
        output_path = _write_text_output(output_path, extraction.prompt_text)

        ledger_event = "next_codex_prompt_extracted"
        warnings = [*selection.warnings, *extraction.warnings]
        ledger.add_event(
            run_id,
            ledger_event,
            "Extracted next Codex prompt from captured GPT response.",
            {
                "source_event_id": selection.source_event["id"] if selection.source_event else None,
                "source_event_type": "gpt_response_captured",
                "source_response_sha256": selection.response_sha256,
                "matched_submission_event_id": (
                    selection.submitted_event["id"] if selection.submitted_event else None
                ),
                "extraction_method": extraction.extraction_method,
                "prompt_text": extraction.prompt_text,
                "prompt_path": str(output_path),
                "prompt_length": extraction.prompt_length,
                "prompt_sha256": extraction.prompt_sha256,
                "prompt_count_detected": extraction.prompt_count_detected,
                "selected_prompt_index": extraction.selected_prompt_index,
                "safety_status": extraction.safety_status,
                "warnings": warnings,
            },
        )
    _print_next_codex_prompt_extraction_result(
        run_id,
        selection,
        extraction,
        output_path=output_path,
        ledger_event=ledger_event,
    )
    return True


def _run_extracted_codex_prompt_flow(
    run_id: str,
    run: dict,
    repo_path_text: str,
    sandbox: str,
    timeout: int,
    expected_extraction_event_id: int | None = None,
    expected_prompt_sha256: str | None = None,
    expected_prompt_text: str | None = None,
    expected_extraction_method: str | None = None,
    allow_full_access: bool = False,
    confirm_full_access: bool = False,
    approval_mode: str = "human",
    pre_run_policy: dict | None = None,
    expected_scope: dict | None = None,
) -> int:
    current_run = ledger.get_run(run_id)
    if current_run is None:
        print(f"Run not found: {run_id}", file=sys.stderr)
        return 1
    run = current_run

    continuation = can_continue_run(run["status"])
    if not continuation["can_continue"]:
        print(
            "Run cannot continue: "
            f"status={continuation['status']} "
            f"reason={continuation['reason']} "
            f"required_action={continuation['required_action'] or ''}",
            file=sys.stderr,
        )
        return 2

    repo_path = Path(repo_path_text).expanduser().resolve(strict=False)
    repo_path_text = str(repo_path)
    if not repo_path.exists():
        print(f"Repo path does not exist: {repo_path_text}", file=sys.stderr)
        return 2
    if not repo_path.is_dir():
        print(f"Repo path is not a directory: {repo_path_text}", file=sys.stderr)
        return 2
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        print(
            "Invalid Codex sandbox. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_SANDBOXES)}.",
            file=sys.stderr,
        )
        return 2
    if sandbox == "danger-full-access" and not allow_full_access:
        print("The supervise command does not support danger-full-access in v0.1.", file=sys.stderr)
        return 2
    if sandbox == "danger-full-access" and not confirm_full_access:
        print("Codex sandbox danger-full-access requires --confirm-full-access.", file=sys.stderr)
        return 2

    events = ledger.list_events(run_id)
    if expected_extraction_event_id is None:
        selection = select_latest_valid_extracted_codex_prompt(
            events,
            expect_prompt_sha256=expected_prompt_sha256,
        )
    else:
        latest_extraction_event_id = -1
        for event in events:
            if event.get("event_type") != "next_codex_prompt_extracted":
                continue
            try:
                latest_extraction_event_id = max(latest_extraction_event_id, int(event.get("id") or -1))
            except (TypeError, ValueError):
                continue
        if latest_extraction_event_id > expected_extraction_event_id:
            print("Stopped: the next prompt changed after it was shown for approval.")
            print("No Codex run was started.")
            print("Run supervise again to review the current prompt.")
            return 1

        extraction_event = None
        for event in events:
            try:
                event_id = int(event.get("id") or -1)
            except (TypeError, ValueError):
                event_id = -1
            if event_id == expected_extraction_event_id:
                extraction_event = event
                break
        if extraction_event is None:
            print("Stopped: the next prompt changed after it was shown for approval.")
            print("No Codex run was started.")
            print("Run supervise again to review the current prompt.")
            return 1
        selection = select_valid_extracted_codex_prompt_event(
            events,
            extraction_event,
            expect_prompt_sha256=expected_prompt_sha256,
        )
    if not selection.ok:
        event_id = selection.event.get("id") if selection.event else ""
        print(f"extraction_event_id: {event_id}", file=sys.stderr)
        for warning in selection.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(f"Invalid extracted Codex prompt: {selection.error}", file=sys.stderr)
        return 1

    selected_event_id = selection.event.get("id") if selection.event else None
    if expected_extraction_event_id is not None:
        try:
            selected_event_id_int = int(selected_event_id)
        except (TypeError, ValueError):
            selected_event_id_int = -1
        if selected_event_id_int != expected_extraction_event_id:
            print("Stopped: the next prompt changed after it was shown for approval.")
            print("No Codex run was started.")
            print("Run supervise again to review the current prompt.")
            return 1
    if expected_prompt_sha256 is not None and selection.prompt_sha256 != expected_prompt_sha256:
        print("Stopped: the next prompt changed after it was shown for approval.")
        print("No Codex run was started.")
        print("Run supervise again to review the current prompt.")
        return 1
    if expected_prompt_text is not None:
        if selection.prompt_text != expected_prompt_text or sha256_text(selection.prompt_text) != selection.prompt_sha256:
            print("Stopped: the next prompt changed after it was shown for approval.")
            print("No Codex run was started.")
            print("Run supervise again to review the current prompt.")
            return 1
    if sha256_text(selection.prompt_text) != selection.prompt_sha256:
        print("Stopped: the selected prompt failed SHA validation.")
        print("No Codex run was started.")
        return 1
    if expected_extraction_method is not None:
        selected_method = selection.metadata.get("extraction_method")
        if selected_method != expected_extraction_method:
            print("Stopped: the next prompt changed after it was shown for approval.")
            print("No Codex run was started.")
            print("Run supervise again to review the current prompt.")
            return 1

    _print_extracted_codex_prompt_preview(run_id, selection, repo_path_text, sandbox)

    extraction_event_id = selection.event.get("id") if selection.event else None
    prompt_contract = parse_prompt_contract(selection.prompt_text, sandbox).to_dict()
    selection_metadata = {
        "extraction_event_id": extraction_event_id,
        "prompt_sha256": selection.prompt_sha256,
        "prompt_length": selection.prompt_length,
        "repo_path": repo_path_text,
        "sandbox": sandbox,
        "prompt_contract": prompt_contract,
        "source_event_id": selection.source_event_id,
        "matched_submission_event_id": selection.matched_submission_event_id,
        "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
        "pre_run_policy": pre_run_policy or {},
        "expected_scope": expected_scope or {},
        "auto_run_allowed": approval_mode == "auto",
        "reason_code": (pre_run_policy or {}).get("reason_code"),
    }
    ledger.add_event(
        run_id,
        "extracted_codex_prompt_selected",
        (
            "Selected extracted Codex prompt for automatic execution."
            if approval_mode == "auto"
            else "Selected extracted Codex prompt for human-confirmed execution."
        ),
        {
            **selection_metadata,
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
        },
    )
    ledger.add_event(
        run_id,
        "extracted_codex_prompt_run_started",
        (
            "Running extracted Codex prompt automatically after routine-safe classification."
            if approval_mode == "auto"
            else "Running extracted Codex prompt after explicit human confirmation."
        ),
        {
            **selection_metadata,
            "timeout": timeout,
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
        },
    )

    flow = _run_codex_exec_flow(
        run_id,
        run,
        selection.prompt_text,
        repo_path_text,
        sandbox,
        timeout,
        confirm_full_access=confirm_full_access,
    )

    result = flow["result"]
    supervision_decision = flow["supervision_decision"] or {}
    transition = flow["transition"] or {}
    ledger.add_event(
        run_id,
        "extracted_codex_prompt_run_finished",
        "Finished extracted Codex prompt execution.",
        {
            **selection_metadata,
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "status": transition.get("next_status"),
            "supervision_decision": supervision_decision.get("decision"),
            "validation_error": result["validation_error"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
        },
    )

    _print_extracted_codex_prompt_run_result(run_id, selection, repo_path_text, sandbox, flow)

    if result["validation_error"]:
        return 2
    if not result["found"]:
        return 1
    if result["timed_out"]:
        return 124
    if result["exit_code"] != 0:
        return result["exit_code"] or 1
    governance_observation = flow.get("governance_observation") or {}
    if governance_observation.get("objective_failures"):
        return 1

    return 0


def _latest_event_id(events: list[dict], event_type: str) -> int:
    latest_id = -1
    for event in events:
        if event.get("event_type") != event_type:
            continue
        try:
            latest_id = max(latest_id, int(event.get("id") or -1))
        except (TypeError, ValueError):
            continue
    return latest_id


def _event_id_from_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _latest_matching_workspace_write_post_run_policy(events: list[dict], codex_event_id: int) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "workspace_write_post_run_policy":
            continue
        metadata = _event_metadata(event)
        if _event_id_from_value(metadata.get("codex_exec_finished_event_id")) == codex_event_id:
            return metadata
    return None


def _verify_auto_workspace_write_result(
    run_id: str,
    repo_path_text: str,
    prompt_sha256: str,
    expected_scope: dict,
    changed_file_classification: dict | None,
    invocation_delta: dict | None = None,
) -> dict:
    attributable = attributable_paths(invocation_delta)
    diff_metadata = {
        "repo_path": repo_path_text,
        "name_status": _delta_name_status(invocation_delta),
        "changed_paths": attributable,
        "diff_unified_zero": _delta_diff_text(invocation_delta),
        "commands": {},
        "validation_error": (invocation_delta or {}).get("validation_error"),
        "captured_at": datetime.now(UTC).isoformat(),
        "source": "invocation_delta",
    }
    ledger.add_event(
        run_id,
        "workspace_write_diff_metadata_captured",
        (
            "Captured workspace-write attributable diff metadata."
            if not diff_metadata.get("validation_error")
            else f"Failed to capture workspace-write diff metadata: {diff_metadata.get('validation_error')}"
        ),
        {
            "run_id": run_id,
            "prompt_sha256": prompt_sha256,
            "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
            **diff_metadata,
        },
    )

    events = ledger.list_events(run_id)
    codex_event_id = _latest_event_id(events, "codex_exec_finished")
    if diff_metadata.get("validation_error"):
        post_run_policy = {
            "tier": "post_run_human_required",
            "allowed": False,
            "reason_code": "post_run_diff_metadata_unavailable",
            "policy_version": WORKSPACE_WRITE_POLICY_VERSION,
            "expected_scope": expected_scope,
            "changed_files": [],
            "unexpected_files": [],
            "prohibited_files": [],
            "name_status_summary": [],
            "diff_content_flags": [],
        }
    else:
        verification = verify_workspace_write_post_run(
            expected_scope,
            diff_metadata.get("changed_paths") or [],
            diff_metadata.get("name_status") or "",
            diff_metadata.get("diff_unified_zero") or "",
            changed_file_classification,
        )
        post_run_policy = verification.to_dict()

    post_run_metadata = {
        "run_id": run_id,
        "prompt_sha256": prompt_sha256,
        "codex_exec_finished_event_id": codex_event_id,
        "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
        "post_run_policy": post_run_policy,
        "auto_submit_allowed": bool(post_run_policy.get("allowed")),
        "loop_continuation_allowed": bool(post_run_policy.get("allowed")),
    }
    ledger.add_event(
        run_id,
        "workspace_write_post_run_policy",
        (
            "Workspace-write post-run diff stayed within auto-approved scope."
            if post_run_policy.get("allowed")
            else "Workspace-write post-run diff requires human review."
        ),
        post_run_metadata,
    )

    if not post_run_policy.get("allowed"):
        human_metadata = {
            "run_id": run_id,
            "prompt_sha256": prompt_sha256,
            "codex_exec_finished_event_id": codex_event_id,
            "workspace_write_policy_version": WORKSPACE_WRITE_POLICY_VERSION,
            "reason_code": post_run_policy.get("reason_code"),
            "changed_files": post_run_policy.get("changed_files", []),
            "unexpected_files": post_run_policy.get("unexpected_files", []),
            "prohibited_files": post_run_policy.get("prohibited_files", []),
            "name_status_summary": post_run_policy.get("name_status_summary", []),
            "diff_content_flags": post_run_policy.get("diff_content_flags", []),
            "expected_scope": expected_scope,
            "post_run_policy": post_run_policy,
        }
        ledger.add_event(
            run_id,
            "human_required_after_write",
            "Codex completed, but an objective workspace-write post-run failure was detected.",
            human_metadata,
        )
        print("Stopped: Codex completed, but an objective workspace-write post-run failure was detected.")
        print(f"Reason: {post_run_policy.get('reason_code')}")
        print("No ChatGPT submission or further Codex execution was performed.")
        sys.stdout.flush()

    return post_run_policy


def _supervise_approval_mode(args: argparse.Namespace) -> str:
    return "human" if bool(getattr(args, "interactive", False)) else "auto"


def _record_supervise_auto_stop(run_id: str, plan: object, reason: str | None = None) -> None:
    ledger.add_event(
        run_id,
        "supervise_auto_stopped",
        "Automatic supervise stopped at a mandatory human gate.",
        {
            "run_id": run_id,
            "approval_mode": "auto",
            "automatic_stop_reason": reason or getattr(plan, "reason", ""),
            "plan_reason": getattr(plan, "reason", ""),
            "stop_message": getattr(plan, "stop_message", ""),
            "action": str(getattr(plan, "action", "")),
            "event_ids": getattr(plan, "event_ids", {}),
            "prompt_sha256": getattr(plan, "prompt_sha", ""),
            "prompt_auto_run_safe": bool(getattr(plan, "prompt_auto_run_safe", False)),
            "prompt_auto_run_reason": getattr(plan, "prompt_auto_run_reason", ""),
        },
    )


def _send_plan_auto_safe(plan: object, events: list[dict]) -> tuple[bool, str]:
    sandbox = getattr(plan, "sandbox", "")
    if sandbox == "workspace-write":
        codex_event_id = _event_id_from_value(getattr(plan, "event_ids", {}).get("codex_exec_finished"))
        post_run_policy = _latest_matching_workspace_write_post_run_policy(events, codex_event_id)
        policy = post_run_policy.get("post_run_policy") if isinstance(post_run_policy, dict) else None
        if isinstance(policy, dict) and policy.get("allowed") is True:
            return True, "workspace_write_post_run_verified"
        return False, "workspace_write_post_run_verification_missing_or_failed"
    if sandbox != "read-only":
        return False, "auto_submit_requires_read_only_sandbox"
    changed_files_count = getattr(plan, "changed_files_count", None)
    if changed_files_count is None:
        return False, "changed_files_unknown"
    if changed_files_count > 0:
        return False, "codex_result_changed_files"
    return True, "routine_clean_read_only_result"


def _run_supervise_command(args: argparse.Namespace) -> int:
    if args.capture_timeout_seconds <= 0:
        print("error: --capture-timeout-seconds must be greater than 0.", file=sys.stderr)
        return 2
    if args.stable_seconds < 0:
        print("error: --stable-seconds must be greater than or equal to 0.", file=sys.stderr)
        return 2

    approval_mode = _supervise_approval_mode(args)

    while True:
        run = ledger.get_run(args.run_id)
        events = ledger.list_events(args.run_id) if run is not None else []
        plan = detect_next_supervise_action(run, events, args.repo, sandbox=args.sandbox)

        if plan.action == SuperviseAction.STOP:
            if approval_mode == "auto":
                _record_supervise_auto_stop(args.run_id, plan)
            _print_supervise_stop(plan)
            if plan.reason in {"non_sentinel_prompt", "invalid_extracted_prompt"}:
                _print_supervise_sentinel_requirement()
            return 1

        if plan.action == SuperviseAction.ASK_SEND_TO_GPT:
            _print_supervise_send_gate(args.run_id, plan)
            if approval_mode == "auto":
                auto_send_safe, auto_send_reason = _send_plan_auto_safe(plan, events)
                if not auto_send_safe:
                    _record_supervise_auto_stop(args.run_id, plan, auto_send_reason)
                    print("Stopped: Codex result requires human approval before ChatGPT submission.")
                    print(f"Reason: {auto_send_reason}")
                    return 1
            if approval_mode == "human" and not _confirm_yes_no("Send Codex result to ChatGPT?"):
                print("Stopped. Feedback was not submitted to ChatGPT.")
                return 0
            if not _submit_feedback_to_chatgpt_flow(
                args.run_id,
                run,
                args.app_name,
                approval_mode=approval_mode,
            ):
                print("Stopped: failed to submit feedback to ChatGPT.")
                return 1
            continue

        if plan.action == SuperviseAction.CAPTURE_GPT_RESPONSE:
            print("Waiting for ChatGPT to finish responding, then capturing the visible reply.")
            print("The correct ChatGPT chat must already be open and visible.")
            if not _capture_gpt_response_from_chatgpt_ax_flow(
                args.run_id,
                run,
                args.app_name,
                args.capture_timeout_seconds,
                args.stable_seconds,
                require_sentinel_response=True,
            ):
                print("Stopped: could not safely capture ChatGPT's response.")
                print(
                    "Recovery: verify the intended ChatGPT chat is open and visible, then run "
                    f"agent-loop capture-gpt-response-from-chatgpt-ax {args.run_id} --confirm-capture"
                )
                return 1
            continue

        if plan.action == SuperviseAction.EXTRACT_NEXT_PROMPT:
            print("Extracting the next Codex prompt from the captured ChatGPT response.")
            if not _extract_next_codex_prompt_flow(args.run_id, require_sentinel=True):
                print("Stopped: no valid sentinel-wrapped next Codex prompt was extracted.")
                print(
                    "Recovery: ask ChatGPT for a sentinel-wrapped prompt or run "
                    f"agent-loop extract-next-codex-prompt {args.run_id} --confirm-extract for diagnostics."
                )
                return 1
            continue

        if plan.action == SuperviseAction.ASK_RUN_PROMPT:
            approved_codex_event_id = _event_id_from_value(plan.event_ids.get("codex_exec_finished"))
            if approved_codex_event_id < 0:
                approved_codex_event_id = _latest_event_id(events, "codex_exec_finished")
            repo_snapshot = capture_git_snapshot(args.repo)
            _print_supervise_run_gate(plan, repo_snapshot)
            if approval_mode == "auto" and not bool(getattr(plan, "prompt_auto_run_safe", False)):
                _record_supervise_auto_stop(
                    args.run_id,
                    plan,
                    getattr(plan, "prompt_auto_run_reason", "") or "prompt_not_routine_safe",
                )
                print("Stopped: extracted prompt requires human approval before Codex execution.")
                print(f"Reason: {getattr(plan, 'prompt_auto_run_reason', '') or 'prompt_not_routine_safe'}")
                return 1
            if approval_mode == "human" and not _confirm_yes_no("Run this prompt in Codex?"):
                print("Stopped. Codex was not run.")
                return 0
            exit_code = _run_extracted_codex_prompt_flow(
                args.run_id,
                run,
                args.repo,
                args.sandbox,
                args.timeout,
                expected_extraction_event_id=plan.event_ids.get("next_codex_prompt_extracted"),
                expected_prompt_sha256=plan.prompt_sha,
                expected_prompt_text=plan.prompt_text,
                expected_extraction_method=plan.extraction_method,
                approval_mode=approval_mode,
                pre_run_policy=getattr(plan, "pre_run_policy", {}),
                expected_scope=getattr(plan, "expected_scope", {}),
            )
            if exit_code != 0:
                return exit_code
            refreshed_events = ledger.list_events(args.run_id)
            latest_codex_event_id = _latest_event_id(refreshed_events, "codex_exec_finished")
            if latest_codex_event_id <= approved_codex_event_id:
                print(
                    "Stopped: extracted Codex prompt returned success but no newer "
                    "codex_exec_finished event was recorded.",
                    file=sys.stderr,
                )
                print("No further supervise iteration can be selected safely.", file=sys.stderr)
                return 1
            continue

        _print_supervise_stop(
            type(
                "UnknownPlan",
                (),
                {
                    "reason": "unknown_action",
                    "stop_message": f"Unknown supervise action: {plan.action}",
                    "status": "",
                    "warnings": (),
                },
            )()
        )
        return 1


def _supervise_args_for_codex_run(args: argparse.Namespace, repo_path_text: str) -> argparse.Namespace:
    return argparse.Namespace(
        run_id=args.run_id,
        repo=repo_path_text,
        sandbox=args.sandbox,
        app_name="ChatGPT",
        timeout=args.timeout,
        capture_timeout_seconds=DEFAULT_CAPTURE_TIMEOUT_SECONDS,
        stable_seconds=DEFAULT_STABLE_SECONDS,
        interactive=bool(getattr(args, "interactive", False)),
    )


def _codex_run_auto_supervise_exit_code(
    args: argparse.Namespace,
    repo_path_text: str,
    flow: dict,
) -> int | None:
    if getattr(args, "no_supervise", False):
        return None

    result = flow.get("result") or {}
    if result.get("validation_error"):
        return None
    if result.get("found") is not True:
        return None
    if bool(result.get("timed_out")):
        return None
    if result.get("exit_code") != 0:
        return None

    transition = flow.get("transition") or {}
    if transition.get("next_status") != RunStatus.COMPLETED.value:
        return None

    supervision_decision = flow.get("supervision_decision") or {}
    if supervision_decision.get("decision") not in {"continue", "record_only"}:
        return None
    if bool(supervision_decision.get("needs_review")):
        return None
    if bool(supervision_decision.get("approval_required")):
        return None

    if args.sandbox == "danger-full-access":
        return None

    run = ledger.get_run(args.run_id)
    events = ledger.list_events(args.run_id) if run is not None else []
    plan = detect_next_supervise_action(run, events, repo_path_text, sandbox=args.sandbox)
    if plan.action != SuperviseAction.ASK_SEND_TO_GPT:
        return None

    return _run_supervise_command(_supervise_args_for_codex_run(args, repo_path_text))


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "init":
        ledger.init_db()
        print(f"Database initialized: {ledger.DB_PATH}")
        return

    if args.command == "start":
        run_id = ledger.create_run(args.instruction)
        ledger.add_event(run_id, "run_created", "Run created.")
        print(run_id)
        return

    if args.command == "show":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _print_run(run, ledger.list_events(args.run_id))
        return

    if args.command == "gpt-feedback":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        feedback = build_gpt_feedback_message(run, ledger.list_events(args.run_id))
        marker_metadata = _marker_metadata(feedback)

        if args.output:
            try:
                output_path = _write_feedback_output(args.output, feedback["message"])
            except OSError as exc:
                parser.exit(1, f"Failed to write GPT feedback output: {exc}\n")

        ledger.add_event(
            args.run_id,
            "gpt_feedback_generated",
            "Generated GPT feedback message.",
            {
                "run_id": feedback["run_id"],
                "status": feedback["status"],
                "codex_exit_code": feedback["codex_exit_code"],
                "codex_timed_out": feedback["codex_timed_out"],
                "changed_files": feedback["changed_files"],
                "message_length": len(feedback["message"]),
                **marker_metadata,
            },
        )

        copy_result = None
        if args.copy:
            copy_result = copy_to_clipboard(feedback["message"])
            copy_message = (
                "Copied GPT feedback message to clipboard."
                if copy_result["copied"]
                else f"Failed to copy GPT feedback message to clipboard: {copy_result['error']}"
            )
            ledger.add_event(
                args.run_id,
                "gpt_feedback_copied",
                copy_message,
                {
                    "copied": copy_result["copied"],
                    "method": copy_result["method"],
                    "error": copy_result["error"],
                    "message_length": len(feedback["message"]),
                    **marker_metadata,
                },
            )

        print(feedback["message"])
        if args.output:
            print(f"wrote: {output_path}")
        if copy_result is not None:
            if copy_result["copied"]:
                print(f"copied: true (method: {copy_result['method']})")
            else:
                print(f"copied: false ({copy_result['error']})")
                sys.exit(1)
        return

    if args.command == "paste-feedback-to-chatgpt":
        if not args.confirm_paste:
            parser.exit(
                2,
                "error: paste-feedback-to-chatgpt requires --confirm-paste. "
                "No feedback was generated, copied, pasted, submitted, or sent.\n",
            )

        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        events = ledger.list_events(args.run_id)
        feedback = build_gpt_feedback_message(run, events)
        marker_metadata = _marker_metadata(feedback)
        output_path = None

        if args.output:
            try:
                output_path = _write_feedback_output(args.output, feedback["message"])
            except OSError as exc:
                parser.exit(1, f"Failed to write GPT feedback output: {exc}\n")

        generation_metadata = {
            "run_id": feedback["run_id"],
            "status": feedback["status"],
            "codex_exit_code": feedback["codex_exit_code"],
            "codex_timed_out": feedback["codex_timed_out"],
            "changed_files": feedback["changed_files"],
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": args.app_name,
            "targeted_chatgpt": True,
        }
        if output_path is not None:
            generation_metadata["output_path"] = str(output_path)
        ledger.add_event(
            args.run_id,
            "gpt_feedback_generated",
            "Generated GPT feedback message for ChatGPT-targeted paste.",
            generation_metadata,
        )

        copy_result = copy_to_clipboard(feedback["message"])
        copy_message = (
            "Copied GPT feedback message to clipboard for ChatGPT-targeted paste."
            if copy_result["copied"]
            else f"Failed to copy GPT feedback message to clipboard: {copy_result['error']}"
        )
        ledger.add_event(
            args.run_id,
            "gpt_feedback_copied",
            copy_message,
            {
                "run_id": feedback["run_id"],
                "copied": copy_result["copied"],
                "method": copy_result["method"],
                "error": copy_result["error"],
                "message_length": len(feedback["message"]),
                **marker_metadata,
                "target": "ChatGPT",
                "app_name": args.app_name,
                "targeted_chatgpt": True,
            },
        )

        if not copy_result["copied"]:
            activation_result = {
                "activated": False,
                "app_name": args.app_name,
                "frontmost_app": None,
                "is_frontmost": False,
                "activation_result": None,
                "frontmost_result": None,
                "error": "Skipped activation because copying GPT feedback to clipboard failed.",
            }
            paste_result = {
                "pasted": False,
                "method": PASTE_METHOD,
                "error": "Skipped paste because copying GPT feedback to clipboard failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            ledger.add_event(
                args.run_id,
                "gpt_feedback_pasted",
                "Skipped ChatGPT-targeted paste because copying GPT feedback failed.",
                {
                    "run_id": feedback["run_id"],
                    "paste_result": paste_result,
                    "activation_result": activation_result,
                    "message_length": len(feedback["message"]),
                    **marker_metadata,
                    "target": "ChatGPT",
                    "app_name": args.app_name,
                    "targeted_chatgpt": True,
                    "output_path": str(output_path) if output_path is not None else None,
                },
            )
            _print_chatgpt_feedback_paste_result(
                args.run_id,
                copy_result,
                activation_result,
                paste_result,
                output_path,
                copy_result["error"],
            )
            raise SystemExit(1)

        activation_result = activate_chatgpt(args.app_name)
        if not activation_result["is_frontmost"]:
            paste_result = {
                "pasted": False,
                "method": PASTE_METHOD,
                "error": "Skipped paste because ChatGPT was not frontmost.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            ledger.add_event(
                args.run_id,
                "gpt_feedback_pasted",
                "Skipped ChatGPT-targeted paste because ChatGPT was not frontmost.",
                {
                    "run_id": feedback["run_id"],
                    "paste_result": paste_result,
                    "activation_result": activation_result,
                    "message_length": len(feedback["message"]),
                    **marker_metadata,
                    "target": "ChatGPT",
                    "app_name": args.app_name,
                    "targeted_chatgpt": True,
                    "output_path": str(output_path) if output_path is not None else None,
                },
            )
            _print_chatgpt_feedback_paste_result(
                args.run_id,
                copy_result,
                activation_result,
                paste_result,
                output_path,
                activation_result["error"],
            )
            raise SystemExit(1)

        paste_result = paste_clipboard_to_frontmost_app()
        ledger.add_event(
            args.run_id,
            "gpt_feedback_pasted",
            (
                "Pasted GPT feedback into ChatGPT."
                if paste_result["pasted"]
                else "Failed to paste GPT feedback into ChatGPT."
            ),
            {
                "run_id": feedback["run_id"],
                "paste_result": paste_result,
                "activation_result": activation_result,
                "message_length": len(feedback["message"]),
                **marker_metadata,
                "target": "ChatGPT",
                "app_name": args.app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
            },
        )
        _print_chatgpt_feedback_paste_result(
            args.run_id,
            copy_result,
            activation_result,
            paste_result,
            output_path,
            paste_result["error"],
        )
        raise SystemExit(0 if paste_result["pasted"] else 1)

    if args.command == "submit-feedback-to-chatgpt":
        if not args.confirm_submit:
            parser.exit(
                2,
                "error: submit-feedback-to-chatgpt requires --confirm-submit. "
                "No feedback was generated, copied, pasted, submitted, or sent.\n",
            )

        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        try:
            ok = _submit_feedback_to_chatgpt_flow(
                args.run_id,
                run,
                args.app_name,
                output_path_text=args.output,
            )
        except OSError as exc:
            parser.exit(1, f"Failed to write GPT feedback output: {exc}\n")
        raise SystemExit(0 if ok else 1)

    if args.command == "capture-gpt-response-from-chatgpt-ax":
        if not args.confirm_capture:
            parser.exit(
                2,
                "error: capture-gpt-response-from-chatgpt-ax requires --confirm-capture. "
                "No ChatGPT activation, AX inspection, ledger write, clipboard access, paste, "
                "Enter, submit, or send action was performed.\n",
            )
        if args.timeout_seconds <= 0:
            parser.exit(2, "error: --timeout-seconds must be greater than 0.\n")
        if args.stable_seconds < 0:
            parser.exit(2, "error: --stable-seconds must be greater than or equal to 0.\n")

        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        ok = _capture_gpt_response_from_chatgpt_ax_flow(
            args.run_id,
            run,
            args.app_name,
            args.timeout_seconds,
            args.stable_seconds,
        )
        raise SystemExit(0 if ok else 1)

    if args.command == "extract-next-codex-prompt":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        try:
            ok = _extract_next_codex_prompt_flow(
                args.run_id,
                confirm_extract=args.confirm_extract,
                output_path_text=args.output,
            )
        except OSError as exc:
            parser.exit(1, f"Failed to write extracted Codex prompt output: {exc}\n")
        raise SystemExit(0 if ok else 1)

    if args.command == "paste-feedback":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        feedback = build_gpt_feedback_message(run, ledger.list_events(args.run_id))
        marker_metadata = _marker_metadata(feedback)
        output_path = None

        if args.output:
            try:
                output_path = _write_feedback_output(args.output, feedback["message"])
            except OSError as exc:
                parser.exit(1, f"Failed to write GPT feedback output: {exc}\n")

        copied_first = bool(args.copy_first)
        copy_result = copy_to_clipboard(feedback["message"])
        copy_message = (
            "Copied GPT feedback message to clipboard."
            if copy_result["copied"]
            else f"Failed to copy GPT feedback message to clipboard: {copy_result['error']}"
        )
        ledger.add_event(
            args.run_id,
            "gpt_feedback_copied",
            copy_message,
            {
                "run_id": feedback["run_id"],
                "copied": copy_result["copied"],
                "method": copy_result["method"],
                "error": copy_result["error"],
                "message_length": len(feedback["message"]),
                **marker_metadata,
            },
        )

        if not copy_result["copied"]:
            paste_result = {
                "pasted": False,
                "method": PASTE_METHOD,
                "error": "Skipped paste because copying GPT feedback to clipboard failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            paste_metadata = {
                "run_id": feedback["run_id"],
                "copied_first": copied_first,
                "paste_result": paste_result,
                "message_length": len(feedback["message"]),
                **marker_metadata,
            }
            if output_path is not None:
                paste_metadata["output_path"] = str(output_path)
            ledger.add_event(
                args.run_id,
                "gpt_feedback_pasted",
                "Failed to paste GPT feedback into frontmost app.",
                paste_metadata,
            )
            print(f"copied_first: {str(copied_first).lower()}")
            print("pasted: false")
            print(f"method: {paste_result['method']}")
            print(f"error: {copy_result['error']}")
            print("note: No submit/Enter was sent.")
            if output_path is not None:
                print(f"wrote: {output_path}")
            raise SystemExit(1)

        print(
            "Paste target must already be focused. This command will paste into "
            "the current focused text field and will not press Enter."
        )
        paste_result = paste_clipboard_to_frontmost_app()
        paste_metadata = {
            "run_id": feedback["run_id"],
            "copied_first": copied_first,
            "paste_result": paste_result,
            "message_length": len(feedback["message"]),
            **marker_metadata,
        }
        if output_path is not None:
            paste_metadata["output_path"] = str(output_path)
        ledger.add_event(
            args.run_id,
            "gpt_feedback_pasted",
            (
                "Pasted GPT feedback into frontmost app."
                if paste_result["pasted"]
                else "Failed to paste GPT feedback into frontmost app."
            ),
            paste_metadata,
        )

        print(f"copied_first: {str(copied_first).lower()}")
        print(f"pasted: {str(paste_result['pasted']).lower()}")
        print(f"method: {paste_result['method']}")
        print(f"error: {paste_result['error'] or ''}")
        print("note: No submit/Enter was sent.")
        if output_path is not None:
            print(f"wrote: {output_path}")
        raise SystemExit(0 if paste_result["pasted"] else 1)

    if args.command == "submit-feedback":
        missing_flags = []
        if not args.copy_first:
            missing_flags.append("--copy-first")
        if not args.confirm_submit:
            missing_flags.append("--confirm-submit")
        if missing_flags:
            parser.exit(
                2,
                "error: submit-feedback requires "
                f"{' and '.join(missing_flags)}. No copy, paste, or Enter was sent.\n",
            )

        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        feedback = build_gpt_feedback_message(run, ledger.list_events(args.run_id))
        marker_metadata = _marker_metadata(feedback)
        copied_first = bool(args.copy_first)

        print("The current focused text field will receive the GPT feedback and Enter will be pressed.")
        print("warning: This sends Enter to the currently focused app.")

        copy_result = copy_to_clipboard(feedback["message"])
        copy_message = (
            "Copied GPT feedback message to clipboard."
            if copy_result["copied"]
            else f"Failed to copy GPT feedback message to clipboard: {copy_result['error']}"
        )
        ledger.add_event(
            args.run_id,
            "gpt_feedback_copied",
            copy_message,
            {
                "run_id": feedback["run_id"],
                "copied": copy_result["copied"],
                "method": copy_result["method"],
                "error": copy_result["error"],
                "message_length": len(feedback["message"]),
                **marker_metadata,
            },
        )

        if copy_result["copied"]:
            paste_result = paste_clipboard_to_frontmost_app()
            paste_message = (
                "Pasted GPT feedback into frontmost app."
                if paste_result["pasted"]
                else "Failed to paste GPT feedback into frontmost app."
            )
        else:
            paste_result = {
                "pasted": False,
                "method": PASTE_METHOD,
                "error": "Skipped paste because copying GPT feedback to clipboard failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            paste_message = "Skipped paste because copying GPT feedback to clipboard failed."

        ledger.add_event(
            args.run_id,
            "gpt_feedback_pasted",
            paste_message,
            {
                "run_id": feedback["run_id"],
                "copied_first": copied_first,
                "paste_result": paste_result,
                "message_length": len(feedback["message"]),
                **marker_metadata,
            },
        )

        if copy_result["copied"] and paste_result["pasted"]:
            submit_result = press_enter_in_frontmost_app()
            submit_input_result = {
                **submit_result,
                "submit_input_sent": bool(submit_result["submitted"]),
            }
            submit_message = (
                "Sent submit input by pressing Enter in frontmost app."
                if submit_result["submitted"]
                else "Failed to send submit input by pressing Enter in frontmost app."
            )
        else:
            submit_input_result = {
                "submit_input_sent": False,
                "method": ENTER_METHOD,
                "error": "Skipped submit because copy or paste failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            submit_result = {**submit_input_result, "submitted": False}
            submit_message = "Skipped submit input because copy or paste failed."

        ledger.add_event(
            args.run_id,
            "gpt_feedback_submit_input_sent",
            submit_message,
            {
                "run_id": feedback["run_id"],
                "confirm_submit": bool(args.confirm_submit),
                "submit_input_result": submit_input_result,
                "message_length": len(feedback["message"]),
                **marker_metadata,
            },
        )

        print(f"copied_first: {str(copied_first).lower()}")
        print(f"copied: {str(copy_result['copied']).lower()}")
        print(f"copy_method: {copy_result['method'] or ''}")
        print(f"copy_error: {copy_result['error'] or ''}")
        print(f"pasted: {str(paste_result['pasted']).lower()}")
        print(f"paste_method: {paste_result['method']}")
        print(f"paste_error: {paste_result['error'] or ''}")
        print(f"submit_input_sent: {str(submit_input_result['submit_input_sent']).lower()}")
        print(f"submit_method: {submit_input_result['method']}")
        print(f"submit_error: {submit_input_result['error'] or ''}")
        print("note: Submit input was sent only as a key event; ChatGPT submission was not verified.")
        raise SystemExit(
            0
            if copy_result["copied"] and paste_result["pasted"] and submit_input_result["submit_input_sent"]
            else 1
        )

    if args.command == "activate-chatgpt":
        result = activate_chatgpt(args.app_name)
        _print_activate_chatgpt_result(result)
        raise SystemExit(0 if result["is_frontmost"] else 1)

    if args.command == "inspect-chatgpt-ui":
        result = inspect_chatgpt_ui(args.app_name)
        _print_inspect_chatgpt_ui_result(result)
        is_frontmost = bool(result["activation_result"]["is_frontmost"])
        raise SystemExit(0 if result["activated"] and is_frontmost else 1)

    if args.command == "test-chatgpt-target-paste":
        if not args.confirm_paste:
            parser.exit(
                2,
                "error: test-chatgpt-target-paste requires --confirm-paste. "
                "No copy, paste, or Enter was sent.\n",
            )

        marker = CHATGPT_TARGET_PASTE_MARKER
        activation_result = activate_chatgpt(args.app_name)
        if not activation_result["is_frontmost"]:
            copy_result = {
                "copied": False,
                "method": None,
                "error": "Skipped copy because ChatGPT was not frontmost.",
            }
            paste_result = {
                "pasted": False,
                "method": PASTE_METHOD,
                "error": "Skipped paste because ChatGPT was not frontmost.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            _print_chatgpt_target_paste_result(
                activation_result,
                copy_result,
                paste_result,
                marker,
            )
            raise SystemExit(1)

        time.sleep(CHATGPT_TARGET_PASTE_DELAY_SECONDS)
        copy_result = copy_to_clipboard(marker)
        if not copy_result["copied"]:
            paste_result = {
                "pasted": False,
                "method": PASTE_METHOD,
                "error": "Skipped paste because copying marker to clipboard failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            _print_chatgpt_target_paste_result(
                activation_result,
                copy_result,
                paste_result,
                marker,
            )
            raise SystemExit(1)

        time.sleep(CHATGPT_TARGET_PASTE_DELAY_SECONDS)
        paste_result = paste_clipboard_to_frontmost_app()
        _print_chatgpt_target_paste_result(
            activation_result,
            copy_result,
            paste_result,
            marker,
        )
        raise SystemExit(
            0
            if activation_result["activated"]
            and activation_result["is_frontmost"]
            and copy_result["copied"]
            and paste_result["pasted"]
            else 1
        )

    if args.command == "can-continue":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        result = can_continue_run(run["status"])
        ledger.add_event(
            args.run_id,
            "continuation_check",
            _continuation_check_message(result),
            result,
        )
        _print_continuation_check(args.run_id, result)
        raise SystemExit(0 if result["can_continue"] else 2)

    if args.command == "approve":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _resolve_flagged_run(
            args.run_id,
            run,
            args.note,
            {RunStatus.WAITING_FOR_APPROVAL.value, RunStatus.NEEDS_REVIEW.value},
            RunStatus.APPROVED,
            "human_approval",
            "Run approved by user.",
            "human_approval_rejected_by_state",
            "approve",
        )
        return

    if args.command == "reject":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _resolve_flagged_run(
            args.run_id,
            run,
            args.note,
            {RunStatus.WAITING_FOR_APPROVAL.value, RunStatus.NEEDS_REVIEW.value},
            RunStatus.REJECTED,
            "human_rejection",
            "Run rejected by user.",
            "human_rejection_rejected_by_state",
            "reject",
        )
        return

    if args.command == "complete-review":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        _resolve_flagged_run(
            args.run_id,
            run,
            args.note,
            {RunStatus.NEEDS_REVIEW.value},
            RunStatus.COMPLETED,
            "human_review_completed",
            "Run review completed by user.",
            "human_review_completion_rejected_by_state",
            "complete review for",
        )
        return

    if args.command == "supervise":
        raise SystemExit(_run_supervise_command(args))

    if args.command == "run-shell":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        command = _normalize_shell_command(args.shell_command)
        if not command:
            parser.exit(2, "Missing shell command. Usage: agent-loop run-shell <run_id> -- <command...>\n")

        cwd = None
        timeout_seconds = DEFAULT_SHELL_TIMEOUT_SECONDS
        ledger.add_event(
            args.run_id,
            "shell_command_started",
            _format_command(command),
            {
                "command": command,
                "cwd": cwd,
                "timeout": timeout_seconds,
            },
        )

        result = run_command(command, cwd=cwd, timeout_seconds=timeout_seconds)
        ledger.add_event(
            args.run_id,
            "shell_command_finished",
            f"exit_code={result['exit_code']} timed_out={result['timed_out']}",
            result,
        )
        _print_shell_result(result)

        if result["timed_out"]:
            raise SystemExit(124)
        raise SystemExit(result["exit_code"] or 0)

    if args.command == "run-extracted-codex-prompt":
        if not args.confirm_run:
            parser.exit(
                2,
                "error: --confirm-run is required. No ledger write, git snapshot, "
                "prompt artifact read, or Codex execution was performed.\n",
            )

        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        raise SystemExit(
            _run_extracted_codex_prompt_flow(
                args.run_id,
                run,
                args.repo,
                args.sandbox,
                args.timeout,
                expected_prompt_sha256=args.expect_prompt_sha256,
                allow_full_access=True,
                confirm_full_access=args.confirm_full_access,
            )
        )

    if args.command == "codex-run":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        requested_repo_path = args.repo if args.repo is not None else args.cwd
        if requested_repo_path:
            repo_path = Path(requested_repo_path).expanduser().resolve(strict=False)
        else:
            repo_path = Path.cwd().resolve()
        repo_path_text = str(repo_path)
        sandbox = args.sandbox

        flow = _run_codex_exec_flow(
            args.run_id,
            run,
            args.prompt,
            repo_path_text,
            sandbox,
            args.timeout,
            args.confirm_full_access,
        )
        result = flow["result"]

        if result["validation_error"]:
            raise SystemExit(2)
        if not result["found"]:
            raise SystemExit(1)
        if result["timed_out"]:
            raise SystemExit(124)
        if result["exit_code"] == 0:
            supervise_exit_code = _codex_run_auto_supervise_exit_code(
                args,
                repo_path_text,
                flow,
            )
            if supervise_exit_code is not None:
                raise SystemExit(supervise_exit_code)
        raise SystemExit(result["exit_code"] or 0)

    if args.command == "codex-check":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        timeout_seconds = DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS
        ledger.add_event(
            args.run_id,
            "codex_check_started",
            "Checking local Codex CLI availability.",
            {"timeout": timeout_seconds},
        )

        result = check_codex_environment(timeout_seconds=timeout_seconds)
        if result["found"]:
            message = f"found=True codex_path={result['codex_path']}"
        else:
            message = "found=False"

        ledger.add_event(
            args.run_id,
            "codex_check_finished",
            message,
            result,
        )
        _print_codex_check_result(result)

        raise SystemExit(0 if result["found"] else 1)


if __name__ == "__main__":
    main()
