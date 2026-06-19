from __future__ import annotations

import argparse
import json
import shutil
import shlex
import sys
import time
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
from agent.git_snapshot import capture_git_snapshot
from agent.mac_app_control import activate_chatgpt
from agent.mac_paste import (
    ENTER_METHOD,
    PASTE_METHOD,
    paste_clipboard_to_frontmost_app,
    press_enter_in_frontmost_app,
)
from agent.mac_ui_inspect import inspect_chatgpt_ui
from agent.prompt_extraction import (
    find_latest_valid_captured_response,
    extract_next_codex_prompt_from_text,
)
from agent.risk_policy import evaluate_supervision_decision
from agent.run_diagnostics import analyze_prompt_repo_impact
from agent.run_state import RunStatus
from agent.run_status_policy import status_from_supervision_decision
from agent import ledger


DEFAULT_SHELL_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 300
CHATGPT_TARGET_PASTE_MARKER = "WATCH_TO_CODEX_STAGE_5_6B_TARGET_PASTE_TEST_DO_NOT_SUBMIT"
CHATGPT_TARGET_PASTE_DELAY_SECONDS = 0.3


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


def _latest_successful_gpt_feedback_submission(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "gpt_feedback_submitted":
            continue
        metadata = _event_metadata(event)
        submit_result = metadata.get("submit_result")
        if isinstance(submit_result, dict):
            if submit_result.get("submitted") is True:
                return event
            continue
        return event
    return None


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
) -> None:
    print(f"run_id: {run_id}")
    print(f"copied: {str(copy_result['copied']).lower()}")
    print(f"copy_method: {copy_result['method'] or ''}")
    print(f"activated: {str(activation_result['activated']).lower()}")
    print(f"frontmost_app: {activation_result['frontmost_app'] or ''}")
    print(f"is_frontmost: {str(activation_result['is_frontmost']).lower()}")
    print(f"pasted: {str(paste_result['pasted']).lower()}")
    print(f"paste_method: {paste_result['method'] or ''}")
    print(f"submitted: {str(submit_result['submitted']).lower()}")
    print(f"submit_method: {submit_result['method'] or ''}")
    print(f"output_path: {str(output_path) if output_path is not None else ''}")
    print(f"copy_error: {copy_result['error'] or ''}")
    print(f"activation_error: {activation_result['error'] or ''}")
    print(f"paste_error: {paste_result['error'] or ''}")
    print(f"submit_error: {submit_result['error'] or ''}")
    print(f"error: {error or ''}")
    if submit_result["submitted"]:
        print("Feedback was submitted to ChatGPT only because --confirm-submit was provided.")
    else:
        print("Feedback was not submitted to ChatGPT.")
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
    print(f"candidate_count: {capture_result.get('candidate_count', 0)}")
    print(f"response_length: {capture_result.get('response_length', 0)}")
    print(f"response_sha256: {capture_result.get('response_sha256', '')}")
    print(f"stable: {str(capture_result.get('stable', False)).lower()}")
    print(f"ledger_event: {ledger_event or ''}")
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

        events = ledger.list_events(args.run_id)
        feedback = build_gpt_feedback_message(run, events)
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
            "target": "ChatGPT",
            "app_name": args.app_name,
            "targeted_chatgpt": True,
        }
        if output_path is not None:
            generation_metadata["output_path"] = str(output_path)
        ledger.add_event(
            args.run_id,
            "gpt_feedback_generated",
            "Generated GPT feedback message for ChatGPT-targeted submit.",
            generation_metadata,
        )

        copy_result = copy_to_clipboard(feedback["message"])
        copy_message = (
            "Copied GPT feedback message to clipboard for ChatGPT-targeted submit."
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
                "target": "ChatGPT",
                "app_name": args.app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
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
            submit_result = {
                "submitted": False,
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
                "submit_result": submit_result,
                "message_length": len(feedback["message"]),
                "target": "ChatGPT",
                "app_name": args.app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
            }
            ledger.add_event(
                args.run_id,
                "gpt_feedback_pasted",
                "Skipped ChatGPT-targeted paste because copying GPT feedback failed.",
                common_metadata,
            )
            ledger.add_event(
                args.run_id,
                "gpt_feedback_submitted",
                "Skipped ChatGPT-targeted submit because copying GPT feedback failed.",
                common_metadata,
            )
            _print_chatgpt_feedback_submit_result(
                args.run_id,
                copy_result,
                activation_result,
                paste_result,
                submit_result,
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
            submit_result = {
                "submitted": False,
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
                "submit_result": submit_result,
                "message_length": len(feedback["message"]),
                "target": "ChatGPT",
                "app_name": args.app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
            }
            ledger.add_event(
                args.run_id,
                "gpt_feedback_pasted",
                "Skipped ChatGPT-targeted paste because ChatGPT was not frontmost.",
                common_metadata,
            )
            ledger.add_event(
                args.run_id,
                "gpt_feedback_submitted",
                "Skipped ChatGPT-targeted submit because ChatGPT was not frontmost.",
                common_metadata,
            )
            _print_chatgpt_feedback_submit_result(
                args.run_id,
                copy_result,
                activation_result,
                paste_result,
                submit_result,
                output_path,
                activation_result["error"],
            )
            raise SystemExit(1)

        paste_result = paste_clipboard_to_frontmost_app()
        paste_metadata = {
            "run_id": feedback["run_id"],
            "activation_result": activation_result,
            "paste_result": paste_result,
            "message_length": len(feedback["message"]),
            "target": "ChatGPT",
            "app_name": args.app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
        }
        ledger.add_event(
            args.run_id,
            "gpt_feedback_pasted",
            (
                "Pasted GPT feedback into ChatGPT for submit."
                if paste_result["pasted"]
                else "Failed to paste GPT feedback into ChatGPT for submit."
            ),
            paste_metadata,
        )

        if paste_result["pasted"]:
            submit_result = press_enter_in_frontmost_app()
            submit_message = (
                "Submitted GPT feedback to ChatGPT by pressing Enter."
                if submit_result["submitted"]
                else "Failed to submit GPT feedback to ChatGPT by pressing Enter."
            )
        else:
            submit_result = {
                "submitted": False,
                "method": ENTER_METHOD,
                "error": "Skipped submit because paste failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            submit_message = "Skipped ChatGPT-targeted submit because paste failed."

        ledger.add_event(
            args.run_id,
            "gpt_feedback_submitted",
            submit_message,
            {
                "run_id": feedback["run_id"],
                "confirm_submit": bool(args.confirm_submit),
                "activation_result": activation_result,
                "paste_result": paste_result,
                "submit_result": submit_result,
                "message_length": len(feedback["message"]),
                "target": "ChatGPT",
                "app_name": args.app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
            },
        )

        _print_chatgpt_feedback_submit_result(
            args.run_id,
            copy_result,
            activation_result,
            paste_result,
            submit_result,
            output_path,
            paste_result["error"] or submit_result["error"],
        )
        raise SystemExit(
            0
            if (
                copy_result["copied"]
                and activation_result["is_frontmost"]
                and paste_result["pasted"]
                and submit_result["submitted"]
            )
            else 1
        )

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

        events = ledger.list_events(args.run_id)
        submitted_event = _latest_successful_gpt_feedback_submission(events)
        if submitted_event is None:
            parser.exit(
                1,
                "No successful gpt_feedback_submitted event was found for this run. "
                "No ChatGPT AX inspection was performed.\n",
            )

        feedback_text, feedback_event = _feedback_text_from_event_metadata(events, submitted_event)
        if feedback_text is None:
            feedback_event = _latest_feedback_generation_before_submission(events, submitted_event)
            feedback_text = build_gpt_feedback_message(run, events)["message"]

        activation_result = activate_chatgpt(args.app_name)
        if not activation_result["is_frontmost"]:
            capture_result = {
                "matched_feedback": False,
                "candidate_count": 0,
                "response_length": 0,
                "response_sha256": "",
                "stable": False,
                "error": activation_result["error"] or "ChatGPT was not frontmost.",
            }
            _print_chatgpt_ax_capture_result(
                args.run_id,
                activation_result,
                capture_result,
                None,
            )
            raise SystemExit(1)

        capture_result = capture_response_after_feedback(
            feedback_text,
            app_name=args.app_name,
            timeout_seconds=args.timeout_seconds,
            stable_seconds=args.stable_seconds,
        )
        if not capture_result["ok"]:
            _print_chatgpt_ax_capture_result(
                args.run_id,
                activation_result,
                capture_result,
                None,
            )
            raise SystemExit(1)

        event_type = "gpt_response_captured"
        ledger.add_event(
            args.run_id,
            event_type,
            "Captured GPT response from ChatGPT desktop accessibility tree.",
            {
                "run_id": args.run_id,
                "source": capture_result["source"],
                "app_name": args.app_name,
                "response_text": capture_result["response_text"],
                "response_length": capture_result["response_length"],
                "response_sha256": capture_result["response_sha256"],
                "matched_submission_event_id": submitted_event.get("id"),
                "matched_feedback_event_id": feedback_event.get("id") if feedback_event else None,
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
            },
        )
        _print_chatgpt_ax_capture_result(
            args.run_id,
            activation_result,
            capture_result,
            event_type,
        )
        return

    if args.command == "extract-next-codex-prompt":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        events = ledger.list_events(args.run_id)
        selection = find_latest_valid_captured_response(events)
        if not selection.ok:
            _print_next_codex_prompt_extraction_result(args.run_id, selection, None)
            raise SystemExit(1)

        extraction = extract_next_codex_prompt_from_text(selection.response_text)
        if not extraction.ok:
            _print_next_codex_prompt_extraction_result(args.run_id, selection, extraction)
            raise SystemExit(1)

        output_path = None
        ledger_event = None
        if args.confirm_extract:
            output_path = (
                Path(args.output).expanduser()
                if args.output
                else Path("data") / "runs" / args.run_id / "next_codex_prompt.md"
            )
            try:
                output_path = _write_text_output(output_path, extraction.prompt_text)
            except OSError as exc:
                parser.exit(1, f"Failed to write extracted Codex prompt output: {exc}\n")

            ledger_event = "next_codex_prompt_extracted"
            warnings = [*selection.warnings, *extraction.warnings]
            ledger.add_event(
                args.run_id,
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
            args.run_id,
            selection,
            extraction,
            output_path=output_path,
            ledger_event=ledger_event,
        )
        return

    if args.command == "paste-feedback":
        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        feedback = build_gpt_feedback_message(run, ledger.list_events(args.run_id))
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
            },
        )

        if copy_result["copied"] and paste_result["pasted"]:
            submit_result = press_enter_in_frontmost_app()
            submit_message = (
                "Submitted GPT feedback by pressing Enter in frontmost app."
                if submit_result["submitted"]
                else "Failed to submit GPT feedback by pressing Enter in frontmost app."
            )
        else:
            submit_result = {
                "submitted": False,
                "method": ENTER_METHOD,
                "error": "Skipped submit because copy or paste failed.",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
            submit_message = "Skipped submit because copy or paste failed."

        ledger.add_event(
            args.run_id,
            "gpt_feedback_submitted",
            submit_message,
            {
                "run_id": feedback["run_id"],
                "confirm_submit": bool(args.confirm_submit),
                "submit_result": submit_result,
                "message_length": len(feedback["message"]),
            },
        )

        print(f"copied_first: {str(copied_first).lower()}")
        print(f"copied: {str(copy_result['copied']).lower()}")
        print(f"copy_method: {copy_result['method'] or ''}")
        print(f"copy_error: {copy_result['error'] or ''}")
        print(f"pasted: {str(paste_result['pasted']).lower()}")
        print(f"paste_method: {paste_result['method']}")
        print(f"paste_error: {paste_result['error'] or ''}")
        print(f"submitted: {str(submit_result['submitted']).lower()}")
        print(f"submit_method: {submit_result['method']}")
        print(f"submit_error: {submit_result['error'] or ''}")
        raise SystemExit(
            0
            if copy_result["copied"] and paste_result["pasted"] and submit_result["submitted"]
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

        git_snapshot = capture_git_snapshot(repo_path_text)
        after_git_snapshot = None
        changed_file_classification = None
        ledger.add_event(
            args.run_id,
            "git_snapshot_before_codex",
            _snapshot_message(git_snapshot),
            git_snapshot,
        )
        _print_git_snapshot_summary(git_snapshot, "before")

        ledger.add_event(
            args.run_id,
            "codex_exec_started",
            "Running Codex exec.",
            {
                "prompt": args.prompt,
                "repo_path": repo_path_text,
                "timeout": args.timeout,
                "sandbox": sandbox,
            },
        )

        validation_error = None
        if sandbox not in ALLOWED_CODEX_SANDBOXES:
            validation_error = (
                "Invalid Codex sandbox. Allowed values: "
                f"{', '.join(ALLOWED_CODEX_SANDBOXES)}."
            )
        elif sandbox == "danger-full-access" and not args.confirm_full_access:
            validation_error = "Codex sandbox danger-full-access requires --confirm-full-access."

        if validation_error is None:
            result = run_codex_exec(
                args.prompt,
                repo_path=repo_path_text,
                timeout_seconds=args.timeout,
                sandbox=sandbox,
            )
        else:
            result = _codex_exec_validation_result(
                args.prompt,
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
            args.run_id,
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
            ledger.add_event(
                args.run_id,
                "git_snapshot_after_codex",
                _snapshot_message(after_git_snapshot),
                after_git_snapshot,
            )
            _print_git_snapshot_summary(after_git_snapshot, "after")

            changed_file_classification = classify_changed_files(
                _changed_file_paths(after_git_snapshot)
            )
            ledger.add_event(
                args.run_id,
                "changed_file_classification",
                _classification_message(changed_file_classification),
                changed_file_classification,
            )
            _print_changed_file_classification(changed_file_classification)

        try:
            prompt_repo_impact_diagnostics = analyze_prompt_repo_impact(
                args.prompt,
                result,
                git_snapshot,
                after_git_snapshot,
                changed_file_classification,
            )
        except Exception as exc:
            prompt_repo_impact_diagnostics = None
            print(f"warning: prompt/repo impact diagnostics unavailable: {exc}", file=sys.stderr)
        ledger.add_event(
            args.run_id,
            "prompt_repo_impact_diagnostics",
            _diagnostics_message(prompt_repo_impact_diagnostics),
            prompt_repo_impact_diagnostics,
        )
        _print_prompt_repo_impact_diagnostics(prompt_repo_impact_diagnostics)

        supervision_decision = evaluate_supervision_decision(prompt_repo_impact_diagnostics)
        ledger.add_event(
            args.run_id,
            "supervision_decision",
            _supervision_decision_message(supervision_decision),
            supervision_decision,
        )
        _print_supervision_decision(supervision_decision)

        transition = status_from_supervision_decision(supervision_decision, result)
        transition = {
            **transition,
            "previous_status": run["status"],
            "next_status": transition["next_status"],
        }
        ledger.update_run_status(args.run_id, RunStatus(transition["next_status"]))
        ledger.add_event(
            args.run_id,
            "run_status_transition",
            _run_status_transition_message(transition),
            transition,
        )
        _print_run_status_transition(transition)

        if result["validation_error"]:
            raise SystemExit(2)
        if not result["found"]:
            raise SystemExit(1)
        if result["timed_out"]:
            raise SystemExit(124)
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
