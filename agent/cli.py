from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from agent.cli_output import (
    _continuation_check_message,
    _format_command,
    _print_changed_file_classification,
    _print_codex_check_result,
    _print_codex_exec_result,
    _print_continuation_check,
    _print_git_snapshot_summary,
    _print_governance_observation,
    _print_manual_stale_lease_release_result,
    _print_prompt_repo_impact_diagnostics,
    _print_run,
    _print_run_status_transition,
    _print_shell_result,
    _print_supervision_decision,
    _print_workspace_write_human_required,
    _snapshot_message,
    _working_tree_dirty,
)
from agent.cli_parser import build_parser as _build_cli_parser
from agent.cli_run_lifecycle import (
    handle_can_continue_command,
    handle_human_decision_command,
    handle_init_command,
    handle_show_command,
    handle_start_command,
)
from agent.cli_terminal_commands import (
    handle_codex_check_command,
    handle_run_shell_command,
)
from agent.chatgpt_ax_capture import (
    DEFAULT_STABLE_SECONDS,
    capture_response_after_feedback,
)
from agent.chatgpt_navigation_diagnostic import (
    DEEP_INSPECTOR_OUTPUT_CHAR_GUARD,
    calibrate_chatgpt_sidebar_coordinate_mapping,
    diagnose_chatgpt_project_chat_rows,
    inspect_chatgpt_navigation_ui,
    inspect_chatgpt_project_chat_row_ax,
    inspect_chatgpt_project_visible_chats,
    inspect_chatgpt_sidebar_destination,
    open_chatgpt_project_chat,
    open_chatgpt_sidebar_destination,
    verify_chatgpt_sidebar_destination,
    verify_chatgpt_sidebar_frame_click,
    verify_current_cursor_click,
    verify_synthetic_click_delivery,
)
from agent.clipboard import copy_to_clipboard
from agent.codex_terminal import (
    check_codex_environment,
    run_command,
)
from agent.codex_services import execute_codex_direct_service
from agent.continuation_policy import can_continue_run
from agent.file_classifier import classify_changed_files
from agent.gpt_feedback import build_gpt_feedback_message
from agent.git_snapshot import (
    capture_git_snapshot,
    capture_invocation_git_state,
    compute_invocation_delta,
)
from agent.governance_services import (
    PostCodexGovernanceCallbacks,
    apply_post_codex_governance_service,
)
from agent.extracted_prompt_services import execute_extracted_codex_prompt_service
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
from agent.chatgpt_services import (
    capture_chatgpt_response_service,
    extract_next_codex_prompt_service,
    submit_feedback_to_chatgpt_service,
)
from agent.prompt_extraction import sha256_text
from agent.prompt_contract import parse_prompt_contract
from agent.risk_policy import evaluate_supervision_decision
from agent.run_diagnostics import analyze_prompt_repo_impact
from agent.run_state import RunStatus
from agent.run_services import (
    HumanDecision,
    HumanDecisionResult,
    create_run_service,
    resolve_human_decision,
)
from agent.run_status_policy import status_from_supervision_decision
from agent.supervise import SuperviseAction, detect_next_supervise_action
from agent.supervision_services import run_supervision_step
from agent import ledger


DEFAULT_SHELL_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS: int | None = None
DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS: int | None = None
CHATGPT_TARGET_PASTE_MARKER = "WATCH_TO_CODEX_STAGE_5_6B_TARGET_PASTE_TEST_DO_NOT_SUBMIT"
CHATGPT_TARGET_PASTE_DELAY_SECONDS = 0.3
CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS: float | None = None
CHATGPT_PASTE_VERIFY_POLL_SECONDS = 0.15
CHATGPT_POST_PASTE_SETTLE_SECONDS = 0.5
CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS: float | None = None
CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS = 0.35
CHATGPT_NAVIGATION_COMPACT_OUTPUT_CHAR_GUARD = 25_000


def _build_parser() -> argparse.ArgumentParser:
    return _build_cli_parser()


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _normalize_shell_command(raw_command: list[str]) -> list[str]:
    if raw_command and raw_command[0] in {"--", "–"}:
        return raw_command[1:]
    return raw_command


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
    polls = 0
    last_observation: dict = {}
    while True:
        polls += 1
        observation = inspect_chatgpt_submission_ui(app_name, marker_text=marker_text)
        last_observation = observation
        composer = _focused_composer_from_observation(observation)
        if composer is not None and marker_text in str(composer.get("text") or composer.get("value") or ""):
            return {
                "ok": True,
                "reason_code": "chatgpt_draft_pasted",
                "poll_count": polls,
                "timeout_seconds": None,
                "poll_interval_seconds": CHATGPT_PASTE_VERIFY_POLL_SECONDS,
                "observation": _submission_ui_observation_summary(observation, marker_text),
            }
        if CHATGPT_PASTE_VERIFY_POLL_SECONDS > 0:
            time.sleep(CHATGPT_PASTE_VERIFY_POLL_SECONDS)


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
    polls = 0
    last_observation: dict = {}
    last_status: dict = {}
    while True:
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
                "timeout_seconds": None,
                "poll_interval_seconds": CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS,
                "status": status,
                "observation": _submission_ui_observation_summary(observation, marker_text),
            }
        if CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS > 0:
            time.sleep(CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS)


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


def _print_inspect_chatgpt_navigation_ui_result(result: dict, include_json_details: bool = False) -> None:
    lines = _inspect_chatgpt_navigation_ui_result_lines(result)
    if include_json_details:
        lines.append("json_details:")
        lines.append(json.dumps(result, indent=2, sort_keys=True))
    print("\n".join(lines))
    sys.stdout.flush()


def _print_verify_chatgpt_sidebar_destination_result(result: dict) -> None:
    target = result.get("target") or {}
    pre = result.get("pre_action_snapshot") or {}
    post = result.get("post_action_snapshot") or {}
    lines = [
        "ChatGPT sidebar destination verification",
        f"status: {result.get('status') or ''}",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"app_name: {result.get('app_name') or ''}",
        f"kind: {result.get('kind') or ''}",
        f"title: {result.get('title') or ''}",
        f"pid_present: {str(bool(result.get('pid_present'))).lower()}",
        f"process_resolution_method: {result.get('process_resolution_method') or ''}",
        f"target_title_path: {target.get('title_ax_path') or ''}",
        f"target_action_path: {target.get('resolved_target_ax_path') or ''}",
        f"target_resolution_method: {target.get('resolution_method') or ''}",
        f"target_enabled: {target.get('enabled_state')}",
        f"target_actions: {target.get('available_action_names') or []}",
        f"pre_requested_visible: {str(bool(pre.get('requested_title_visible'))).lower()}",
        f"pre_requested_selected: {str(bool(pre.get('requested_title_selected'))).lower()}",
        f"post_requested_visible: {str(bool(post.get('requested_title_visible'))).lower()}",
        f"post_requested_selected: {str(bool(post.get('requested_title_selected'))).lower()}",
        f"actions_performed: {result.get('actions_performed') or []}",
        f"error: {result.get('error') or ''}",
    ]
    print("\n".join(lines))
    sys.stdout.flush()


def _print_open_chatgpt_sidebar_destination_result(result: dict) -> None:
    target = result.get("target") or {}
    activation = result.get("activation_result") or {}
    stability = result.get("activation_stability") or {}
    point = result.get("calculated_global_point") or {}
    post = result.get("post_action_evidence") or {}
    signals = post.get("signals") or []
    visible_chats = result.get("visible_chats") or []
    lines = [
        "ChatGPT sidebar destination open",
        f"requested_kind: {result.get('kind') or ''}",
        f"requested_title: {result.get('title') or ''}",
        (
            "activation_result: "
            f"activated={activation.get('activated')} "
            f"is_frontmost={activation.get('is_frontmost')} "
            f"frontmost_app={activation.get('frontmost_app') or ''} "
            f"error={activation.get('error') or ''}"
        ),
        (
            "stability: "
            f"status={stability.get('status') or ''} "
            f"samples={stability.get('samples', 0)} "
            f"error={stability.get('error') or ''}"
        ),
        f"chosen_method: {result.get('chosen_method') or ''}",
        f"target_title_path: {target.get('title_ax_path') or ''}",
        f"target_row_path: {target.get('row_ax_path') or ''}",
        f"title_role: {target.get('title_role') or ''}",
        f"row_role: {target.get('row_role') or ''}",
        f"title_actions: {target.get('title_actions') or []}",
        f"row_actions: {target.get('row_actions') or []}",
        f"title_frame: {_compact_plain_frame(result.get('title_frame') or {})}",
        f"row_frame: {_compact_plain_frame(result.get('row_frame') or {})}",
        f"chatgpt_ax_window_frame: {_compact_plain_frame(result.get('chatgpt_ax_window_frame') or {})}",
        f"chatgpt_windowserver_bounds: {_compact_plain_frame(result.get('chatgpt_windowserver_bounds') or {})}",
        f"calculated_global_point: x={point.get('x')} y={point.get('y')}",
        f"calculated_point_hit_test_relationship: {result.get('calculated_point_hit_test_relationship') or ''}",
        f"post_action_confirmed: {post.get('confirmed')}",
        f"post_action_evidence: {signals}",
        f"visible_chat_count: {result.get('visible_chat_count', 0)}",
        f"visible_chat_titles: {[chat.get('title') or '' for chat in visible_chats]}",
        f"final_outcome: {result.get('outcome') or ''}",
        f"actions_performed: {result.get('actions_performed') or []}",
        f"error: {result.get('error') or ''}",
    ]
    print("\n".join(lines))
    sys.stdout.flush()


def _compact_plain_frame(frame: dict) -> str:
    return (
        f"x={frame.get('x')} y={frame.get('y')} "
        f"width={frame.get('width')} height={frame.get('height')}"
    )


def _print_inspect_chatgpt_sidebar_destination_result(result: dict, include_json_details: bool = False) -> None:
    lines = _inspect_chatgpt_sidebar_destination_result_lines(result)
    if include_json_details:
        json_text = json.dumps(result, indent=2, sort_keys=True)
        remaining = max(0, DEEP_INSPECTOR_OUTPUT_CHAR_GUARD - _line_char_count(lines) - len("json_details:\n"))
        lines.append("json_details:")
        if len(json_text) <= remaining:
            lines.append(json_text)
        else:
            lines.append(json_text[:remaining])
            lines.append("(json details truncated by output guard)")
    print("\n".join(lines))
    sys.stdout.flush()


def _print_verify_chatgpt_sidebar_frame_click_result(result: dict) -> None:
    target = result.get("target") or {}
    frame_safety = result.get("frame_safety") or {}
    source_frame = frame_safety.get("source_frame") or {}
    sidebar_frame = frame_safety.get("chosen_sidebar_frame_geometry") or {}
    window_frame = frame_safety.get("focused_window_frame_geometry") or {}
    click_point = result.get("click_point") or {}
    post = result.get("post_click_evidence") or {}
    coords = result.get("coordinate_diagnostics") or {}
    lines = [
        "ChatGPT sidebar frame-click verification",
        f"status: {result.get('status') or ''}",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"dry_run: {str(not bool(result.get('confirm_frame_click'))).lower()}",
        f"app_name: {result.get('app_name') or ''}",
        f"kind: {result.get('kind') or ''}",
        f"title: {result.get('title') or ''}",
        f"pid_present: {str(bool(result.get('pid_present'))).lower()}",
        f"process_resolution_method: {result.get('process_resolution_method') or ''}",
        f"target_title_path: {target.get('title_ax_path') or ''}",
        f"target_row_path: {target.get('computed_row_ax_path') or ''}",
        f"chosen_source_frame_path: {target.get('source_frame_path') or ''}",
        f"chosen_source_frame_relation: {target.get('source_frame_relation') or ''}",
        f"chosen_sidebar_frame_path: {frame_safety.get('chosen_sidebar_frame_path') or ''}",
        f"chosen_sidebar_frame_role: {frame_safety.get('chosen_sidebar_frame_role') or ''}",
        (
            "chosen_sidebar_frame_geometry: "
            f"x={sidebar_frame.get('x')} y={sidebar_frame.get('y')} "
            f"width={sidebar_frame.get('width')} height={sidebar_frame.get('height')}"
        ),
        f"sidebar_containment_method: {frame_safety.get('sidebar_containment_method') or ''}",
        f"row_inside_chosen_sidebar_frame: {frame_safety.get('row_inside_chosen_sidebar_frame')}",
        (
            "focused_window_frame_geometry: "
            f"x={window_frame.get('x')} y={window_frame.get('y')} "
            f"width={window_frame.get('width')} height={window_frame.get('height')}"
        ),
        (
            "frame_geometry: "
            f"x={source_frame.get('x')} y={source_frame.get('y')} "
            f"width={source_frame.get('width')} height={source_frame.get('height')}"
        ),
        (
            "frame_checks: "
            f"valid={source_frame.get('valid')} "
            f"inside_window={source_frame.get('fully_inside_window')} "
            f"inside_sidebar={source_frame.get('inside_sidebar_or_list')} "
            f"large_enough={source_frame.get('large_enough_for_safe_interior_click')}"
        ),
        f"safe_click_point: x={click_point.get('x')} y={click_point.get('y')} ok={click_point.get('ok')}",
        f"safe_click_policy: {click_point.get('policy') or ''}",
        f"overflow_exclusion_zone: {click_point.get('overflow_exclusion_zone') or {}}",
        f"why_click_point_avoids_overflow_region: {frame_safety.get('why_click_point_avoids_overflow_region') or ''}",
        f"safety_checks_passed: {str(bool(frame_safety.get('safety_checks_passed'))).lower()}",
        *_coordinate_diagnostics_lines(coords),
        f"post_click_status: {post.get('status') or ''}",
        f"post_selection_or_focus_changed: {post.get('selection_or_focus_changed')}",
        f"post_observable_state_changed: {post.get('observable_state_changed')}",
        f"actions_performed: {result.get('actions_performed') or []}",
        f"error: {result.get('error') or ''}",
    ]
    print("\n".join(lines))
    sys.stdout.flush()


def _print_calibrate_chatgpt_sidebar_coordinate_mapping_result(result: dict) -> None:
    target = result.get("target") or {}
    cursor = result.get("current_global_physical_cursor_location") or {}
    hit = result.get("hit_test") or {}
    windowserver = result.get("windowserver_evidence") or {}
    chosen_ws = (windowserver.get("chosen_window") or {}).get("bounds") or {}
    frames = result.get("frame_evidence") or []
    title_frame = _first_frame_evidence(frames, "target_title_frame")
    row_frame = _first_frame_evidence(frames, "computed_row_frame")
    ax_window = _first_frame_evidence(frames, "chatgpt_ax_window_frame")
    calculated = result.get("calculated_global_click_point") or {}
    selected = result.get("selected_source_mapping_candidate") or {}
    selected_point = selected.get("candidate_point") or {}
    post = result.get("post_click_requested_destination_evidence") or {}
    lines = [
        "ChatGPT sidebar coordinate-mapping calibration",
        f"status: {result.get('status') or ''}",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"read_only: {str(bool(result.get('read_only'))).lower()}",
        f"app_name: {result.get('app_name') or ''}",
        f"kind: {result.get('kind') or ''}",
        f"title: {result.get('title') or ''}",
        f"pid_present: {str(bool(result.get('pid_present'))).lower()}",
        f"process_resolution_method: {result.get('process_resolution_method') or ''}",
        f"current_global_physical_cursor_location: x={cursor.get('x')} y={cursor.get('y')}",
        f"hit_test_path: {hit.get('path') or ''}",
        f"hit_test_role: {hit.get('role') or ''}",
        f"hit_test_subrole: {hit.get('subrole') or ''}",
        f"hit_test_relationship_to_requested_target: {result.get('hit_test_relationship_to_requested_target') or ''}",
        f"target_title_path: {target.get('title_ax_path') or ''}",
        f"target_row_path: {target.get('computed_row_ax_path') or ''}",
        f"target_title_frame: {_compact_calibration_frame(title_frame)}",
        f"row_frame: {_compact_calibration_frame(row_frame)}",
        (
            "chosen_chatgpt_windowserver_bounds: "
            f"x={chosen_ws.get('x')} y={chosen_ws.get('y')} "
            f"width={chosen_ws.get('width')} height={chosen_ws.get('height')}"
        ),
        f"chosen_chatgpt_ax_window_frame: {_compact_calibration_frame(ax_window)}",
        "mapping_candidates:",
    ]
    for candidate in result.get("mapping_candidates") or []:
        point = candidate.get("candidate_point") or {}
        lines.append(
            "  "
            f"{candidate.get('mapping_name') or ''}: "
            f"x={point.get('x')} y={point.get('y')} "
            f"distance_from_cursor_px={candidate.get('distance_from_cursor_px')} "
            f"inside_window={candidate.get('inside_actual_visible_chatgpt_window_bounds')} "
            f"inside_hit_relationship={candidate.get('inside_target_hit_test_relationship')} "
            f"inside_title={candidate.get('inside_target_title_frame_under_interpretation')} "
            f"inside_row={candidate.get('inside_target_row_frame_under_interpretation')}"
        )
    if result.get("confirm_calibration_click"):
        lines.extend(
            [
                (
                    "chosen_mapping_candidate: "
                    f"name={selected.get('mapping_name') or ''} "
                    f"classification={selected.get('classification_if_unique') or ''} "
                    f"x={selected_point.get('x')} y={selected_point.get('y')}"
                ),
                f"calculated_global_click_point: x={calculated.get('x')} y={calculated.get('y')}",
                f"click_count: {result.get('click_count')}",
                f"inter_click_delay_ms: {result.get('inter_click_delay_ms')}",
                (
                    "post_click_requested_destination_evidence: "
                    f"confirmed={post.get('active_destination_confirmed')} "
                    f"visible={post.get('requested_title_visible')} "
                    f"match_count={post.get('requested_title_match_count')} "
                    f"evidence={post.get('evidence') or []}"
                ),
                f"final_click_classification: {result.get('final_click_classification') or ''}",
            ]
        )
    lines.extend(
        [
            f"final_mapping_classification: {result.get('final_mapping_classification') or ''}",
            f"recommended_future_click_transform: {result.get('recommended_future_click_transform') or 'unresolved'}",
            f"actions_performed: {result.get('actions_performed') or []}",
            f"error: {result.get('error') or ''}",
            _calibration_non_action_line(bool(result.get("confirm_calibration_click"))),
            f"recommended_runtime_click_transform: {result.get('recommended_runtime_click_transform') or 'unresolved'}",
        ]
    )
    print("\n".join(lines))
    sys.stdout.flush()


def _print_inspect_chatgpt_project_visible_chats_result(result: dict) -> None:
    content = result.get("main_project_content") or result.get("project_content_container") or {}
    content_frame = content.get("frame") or {}
    chat_list = result.get("chat_list_container") or {}
    chat_list_frame = chat_list.get("frame") or {}
    lines = [
        "ChatGPT project visible chats",
        f"status: {result.get('status') or ''}",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"project_title: {result.get('project_title') or ''}",
        f"visible_chat_count: {result.get('visible_chat_count', 0)}",
        (
            "main_project_content_path: "
            f"{content.get('path') or ''}"
        ),
        (
            "main_project_content_frame: "
            f"{_compact_plain_frame(content_frame)}"
        ),
        (
            "project_content_container: "
            f"path={content.get('path') or ''} "
            f"frame={_compact_plain_frame(content_frame)}"
        ),
        (
            "chat_list_container_path: "
            f"{chat_list.get('path') or ''}"
        ),
        (
            "chat_list_container_frame: "
            f"{_compact_plain_frame(chat_list_frame)}"
        ),
        f"more_rows_may_exist_below: {result.get('more_rows_may_exist_below')}",
        f"project_chat_list_identity: {result.get('project_chat_list_identity') or 'not_confirmed'}",
        (
            "project_chat_list_container: "
            f"path={result.get('project_chat_list_container_path') or ''} "
            f"role={result.get('project_chat_list_container_role') or ''} "
            f"frame={_compact_plain_frame(result.get('project_chat_list_container_frame') or {})}"
        ),
        f"project_chat_row_shape_status: {result.get('project_chat_row_shape_status') or ''}",
        f"valid_project_chat_row_count: {result.get('valid_project_chat_row_count', 0)}",
        f"invalid_candidate_count: {result.get('invalid_candidate_count', 0)}",
        f"row_height_median: {result.get('row_height_median', 0.0)}",
        f"vertical_peer_list_confirmed: {str(bool(result.get('vertical_peer_list_confirmed'))).lower()}",
        f"chats_tab_active_evidence: {result.get('chats_tab_active_evidence') or ''}",
        f"identity_stability_samples: {result.get('identity_stability_samples', 1)}",
        f"identity_failure_reasons: {result.get('identity_failure_reasons') or []}",
        f"excluded_candidate_count: {sum((result.get('excluded_candidate_counts') or {}).values())}",
        f"excluded_candidate_reasons: {result.get('excluded_candidate_counts') or {}}",
        f"actions_performed: {result.get('actions_performed') or []}",
    ]
    for chat in result.get("visible_chats") or []:
        row_frame = chat.get("row_frame") or {}
        lines.append(f"{chat.get('ordinal')}. {chat.get('title') or ''}")
        if chat.get("preview"):
            lines.append(f"   preview: {chat.get('preview')}")
        lines.append(f"   row_frame: {_compact_plain_frame(row_frame)}")
        lines.append(f"   visibility: {chat.get('visibility') or ''}")
        lines.append(f"   path: {chat.get('path') or ''}")
        lines.append(f"   role: {chat.get('role') or ''} subrole: {chat.get('subrole') or ''}")
        lines.append(f"   display_title_source: {chat.get('display_title_source') or ''}")
        lines.append(f"   title_representation: {chat.get('title_representation') or ''}")
        lines.append(f"   preview_representation: {chat.get('preview_representation') or ''}")
        lines.append(f"   ax_press_available: {chat.get('ax_press_available')}")
    lines.append(f"error: {result.get('error') or ''}")
    print("\n".join(lines))
    sys.stdout.flush()


def _print_inspect_chatgpt_project_chat_row_ax_result(result: dict) -> None:
    lines = [
        "ChatGPT project chat row AX audit",
        f"status: {result.get('status') or ''}",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"project_title: {result.get('project_title') or ''}",
        f"chat_titles: {result.get('chat_titles') or []}",
        f"project_resolution_status: {result.get('project_resolution_status') or ''}",
        f"visible_chat_count: {result.get('visible_chat_count', 0)}",
        f"actions_performed: {result.get('actions_performed') or []}",
    ]
    for audit_index, audit in enumerate(result.get("row_audits") or [], start=1):
        row = audit.get("accepted_row") or {}
        summary = audit.get("summary") or {}
        lines.extend(
            [
                f"audit_{audit_index}_requested_chat_title: {audit.get('requested_chat_title') or ''}",
                f"audit_{audit_index}_status: {audit.get('status') or ''}",
                f"audit_{audit_index}_row_path: {row.get('row_path') or ''}",
                f"audit_{audit_index}_row_role: {row.get('row_role') or ''} subrole: {row.get('row_subrole') or ''}",
                f"audit_{audit_index}_row_frame: {_compact_plain_frame(row.get('row_frame') or {})}",
                f"audit_{audit_index}_resolver_title: {row.get('resolver_title') or ''}",
                f"audit_{audit_index}_raw_flattened_row_text: {audit.get('raw_flattened_row_text') or ''}",
                f"audit_{audit_index}_row_exposes_merged_text: {summary.get('row_exposes_merged_text')}",
                f"audit_{audit_index}_exact_title_node_paths: {summary.get('exact_title_node_paths') or []}",
                f"audit_{audit_index}_preview_like_node_paths: {summary.get('preview_like_node_paths') or []}",
                f"audit_{audit_index}_punctuation_only_node_paths: {summary.get('punctuation_only_node_paths') or []}",
            ]
        )
        for node in audit.get("nodes") or []:
            frame = node.get("frame") or {}
            lines.extend(
                [
                    (
                        f"  node path={node.get('path') or ''} "
                        f"depth={node.get('relative_depth')} child_index={node.get('child_index')} "
                        f"role={node.get('role') or ''} subrole={node.get('subrole') or ''}"
                    ),
                    f"    frame: {_compact_plain_frame(frame)} actions={node.get('actions') or []}",
                    f"    AXTitle: {node.get('AXTitle') or ''}",
                    f"    AXValue: {node.get('AXValue') or ''}",
                    f"    AXDescription: {node.get('AXDescription') or ''}",
                    f"    text_classification: {node.get('text_classification') or ''}",
                ]
            )
    lines.append(f"error: {result.get('error') or ''}")
    print("\n".join(lines))
    sys.stdout.flush()


def _print_diagnose_chatgpt_project_chat_rows_result(result: dict) -> None:
    summary = result.get("summary") or {}
    lines = [
        "ChatGPT Project Chat Row Diagnostic",
        "Project/list identity",
        f"requested_project_title: {result.get('requested_project_title') or ''}",
        f"project_identity_confirmed: {str(bool(result.get('project_identity_confirmed'))).lower()}",
        f"project_chat_list_identity: {result.get('project_chat_list_identity') or 'not_confirmed'}",
        f"project_chat_list_container_path: {result.get('project_chat_list_container_path') or ''}",
        f"project_chat_list_container_role: {result.get('project_chat_list_container_role') or ''}",
        f"project_chat_list_container_frame: {_compact_plain_frame(result.get('project_chat_list_container_frame') or {})}",
        f"identity_failure_reasons: {result.get('identity_failure_reasons') or []}",
        "Confirmed list viewport",
        f"frame: {_compact_plain_frame(result.get('project_chat_list_container_frame') or {})}",
        f"ax_nodes_inspected: {summary.get('ax_nodes_inspected', 0)}",
        f"contains_title_filter: {result.get('contains_title') or ''}",
        f"hidden_unrelated_band_count: {result.get('hidden_unrelated_band_count', 0)}",
        "Current resolver accepted rows",
    ]
    for row in result.get("current_resolver_accepted_rows") or []:
        lines.extend(
            [
                f"- ordinal={row.get('ordinal')} title={row.get('title') or ''}",
                f"  row_path: {row.get('row_path') or ''}",
                f"  row_frame: {_compact_plain_frame(row.get('row_frame') or {})}",
                f"  title_representation: {row.get('title_representation') or ''}",
            ]
        )
    if not result.get("current_resolver_accepted_rows"):
        lines.append("- none")

    lines.append("Visual row bands")
    for band in result.get("visual_row_bands") or []:
        lines.extend(
            [
                f"band_index: {band.get('band_index')}",
                f"band_frame: {_compact_plain_frame(band.get('band_frame') or {})}",
                f"band_height: {band.get('band_height')}",
                f"node_count: {band.get('node_count')}",
                f"outermost_candidate_path: {band.get('outermost_candidate_path') or ''}",
                f"outermost_candidate_role: {band.get('outermost_candidate_role') or ''}",
            ]
        )

    lines.append("Band candidate evidence")
    for band in result.get("visual_row_bands") or []:
        lines.append(f"band_index: {band.get('band_index')}")
        for node in band.get("nodes") or []:
            lines.extend(
                [
                    f"  node_index: {node.get('node_index')}",
                    f"    role: {node.get('role') or ''}",
                    f"    subrole: {node.get('subrole') or ''}",
                    f"    path: {node.get('path') or ''}",
                    f"    parent_path: {node.get('parent_path') or ''}",
                    f"    parent_role: {node.get('parent_role') or ''}",
                    f"    frame: {_compact_plain_frame(node.get('frame') or {})}",
                    f"    frame_height: {node.get('frame_height')}",
                    f"    actions: {node.get('actions') or []}",
                    f"    AXTitle: {node.get('AXTitle') or ''}",
                    f"    AXDescription: {node.get('AXDescription') or ''}",
                    f"    AXValue: {node.get('AXValue') or ''}",
                ]
            )
        for candidate in band.get("title_candidates") or []:
            lines.extend(
                [
                    "  title_candidate:",
                    f"    source_path: {candidate.get('source_path') or ''}",
                    f"    source_role: {candidate.get('source_role') or ''}",
                    f"    source_attribute: {candidate.get('source_attribute') or ''}",
                    f"    raw_text: {candidate.get('raw_text') or ''}",
                    f"    candidate_kind: {candidate.get('candidate_kind') or ''}",
                ]
            )

    lines.append("Current resolver comparison")
    for band in result.get("visual_row_bands") or []:
        comparison = band.get("current_resolver_comparison") or {}
        lines.extend(
            [
                f"band_index: {band.get('band_index')}",
                f"current_resolver_status: {comparison.get('current_resolver_status') or ''}",
                f"current_resolver_title: {comparison.get('current_resolver_title') or ''}",
                f"current_resolver_row_path: {comparison.get('current_resolver_row_path') or ''}",
                f"current_resolver_row_frame: {_compact_plain_frame(comparison.get('current_resolver_row_frame') or {})}",
                f"current_resolver_rejection_reasons: {comparison.get('current_resolver_rejection_reasons') or []}",
            ]
        )

    lines.append("Experimental canonical titles")
    for band in result.get("visual_row_bands") or []:
        canonical = band.get("experimental_canonical") or {}
        lines.extend(
            [
                f"band_index: {band.get('band_index')}",
                f"experimental_canonical_title: {canonical.get('experimental_canonical_title') or ''}",
                f"experimental_preview: {canonical.get('experimental_preview') or ''}",
                f"experimental_title_confidence: {canonical.get('experimental_title_confidence') or 'none'}",
            ]
        )

    lines.extend(
        [
            "Summary",
            f"ax_nodes_inspected: {summary.get('ax_nodes_inspected', 0)}",
            f"visual_bands_found: {summary.get('visual_bands_found', 0)}",
            f"bands_with_high_confidence_title: {summary.get('bands_with_high_confidence_title', 0)}",
            f"bands_accepted_by_current_resolver: {summary.get('bands_accepted_by_current_resolver', 0)}",
            f"bands_not_seen_by_current_resolver: {summary.get('bands_not_seen_by_current_resolver', 0)}",
            f"bands_rejected_by_current_resolver: {summary.get('bands_rejected_by_current_resolver', 0)}",
            f"filtered_bands_printed: {summary.get('filtered_bands_printed', 0)}",
            f"final_outcome: {result.get('final_outcome') or result.get('status') or ''}",
            f"actions_performed: {result.get('actions_performed') or []}",
            f"error: {result.get('error') or ''}",
        ]
    )
    print("\n".join(lines))
    sys.stdout.flush()


def _print_open_chatgpt_project_chat_result(result: dict) -> None:
    project = result.get("project_open_result") or {}
    row = result.get("matched_chat_row") or {}
    point = result.get("calculated_global_point") or {}
    post = result.get("post_action_evidence") or {}
    lines = [
        "ChatGPT project chat open",
        f"requested_project_title: {result.get('project_title') or ''}",
        f"requested_chat_title: {result.get('chat_title') or ''}",
        f"project_open_result: outcome={project.get('outcome') or ''} ok={project.get('ok')} visible_chat_count={project.get('visible_chat_count', 0)}",
        f"project_chat_list_identity: {result.get('project_chat_list_identity') or 'not_confirmed'}",
        (
            "project_chat_list_container: "
            f"path={result.get('project_chat_list_container_path') or ''} "
            f"role={result.get('project_chat_list_container_role') or ''}"
        ),
        f"project_chat_row_shape_status: {result.get('project_chat_row_shape_status') or ''}",
        f"valid_project_chat_row_count: {result.get('valid_project_chat_row_count', 0)}",
        f"invalid_candidate_count: {result.get('invalid_candidate_count', 0)}",
        f"row_height_median: {result.get('row_height_median', 0.0)}",
        f"vertical_peer_list_confirmed: {str(bool(result.get('vertical_peer_list_confirmed'))).lower()}",
        f"chats_tab_active_evidence: {result.get('chats_tab_active_evidence') or ''}",
        f"identity_stability_samples: {result.get('identity_stability_samples', 1)}",
        f"identity_failure_reasons: {result.get('identity_failure_reasons') or []}",
        f"initial_visible_chat_count: {result.get('initial_visible_chat_count', result.get('visible_chat_count', 0))}",
        f"visible_chat_count: {result.get('visible_chat_count', 0)}",
        f"targeting_visible_chat_count: {result.get('targeting_visible_chat_count', result.get('visible_chat_count', 0))}",
        f"scroll_iterations_attempted: {result.get('scroll_iterations_attempted', 0)}",
        f"max_scroll_iterations: {result.get('max_scroll_iterations', 0)}",
        f"search_cycles_attempted: {result.get('search_cycles_attempted', 0)}",
        f"max_search_cycles: {result.get('max_search_cycles', 0)}",
        f"configured_max_search_cycles: {result.get('configured_max_search_cycles', result.get('max_search_cycles', 0))}",
        f"configured_max_search_elapsed_seconds: {result.get('configured_max_search_elapsed_seconds', 0.0)}",
        f"scroll_pulses_posted: {result.get('scroll_pulses_posted', 0)}",
        f"scroll_method_used: {result.get('scroll_method_used') or ''}",
        f"computed_scroll_delta_y: {result.get('computed_scroll_delta_y', 0)}",
        f"median_visible_row_height: {result.get('median_visible_row_height', 0.0)}",
        f"scan_continuity: {result.get('scan_continuity') or 'confirmed'}",
        f"recovery_scroll_pulses_posted: {result.get('recovery_scroll_pulses_posted', 0)}",
        f"initial_hydration_status: {result.get('initial_hydration_status') or ''}",
        f"hydration_events_observed: {result.get('hydration_events_observed', 0)}",
        f"reset_events_observed: {result.get('reset_events_observed', 0)}",
        f"unique_accessibility_rows_seen: {result.get('unique_accessibility_rows_seen', 0)}",
        f"unique_effective_viewports_seen: {result.get('unique_effective_viewports_seen', 0)}",
        f"new_accessibility_rows_seen: {result.get('new_accessibility_rows_seen', 0)}",
        f"target_match_checked_on_samples: {result.get('target_match_checked_on_samples', 0)}",
        f"hydration_samples_taken: {result.get('hydration_samples_taken', 0)}",
        f"settled_cycles_completed: {result.get('settled_cycles_completed', 0)}",
        f"progressful_cycles_completed: {result.get('progressful_cycles_completed', 0)}",
        f"target_initially_visible: {result.get('target_initially_visible')}",
        f"target_found_after_scrolling: {result.get('target_found_after_scrolling')}",
        f"unique_chat_titles_printed: {result.get('unique_chat_titles_printed', 0)}",
        f"target_exact_match_detected: {str(bool(result.get('target_exact_match_detected'))).lower()}",
        f"target_detected_in: {result.get('target_detected_in') or ''}",
        f"target_detected_cycle: {result.get('target_detected_cycle', 0)}",
        f"scroll_pulses_after_target_detection: {result.get('scroll_pulses_after_target_detection', 0)}",
        f"target_alignment_required: {str(bool(result.get('target_alignment_required'))).lower()}",
        f"target_alignment_method: {result.get('target_alignment_method') or 'none'}",
        f"target_alignment_posted: {str(bool(result.get('target_alignment_posted'))).lower()}",
        f"target_alignment_row_path: {result.get('target_alignment_row_path') or ''}",
        f"target_alignment_pre_visibility: {result.get('target_alignment_pre_visibility') or 'not_visible'}",
        f"target_alignment_post_visibility: {result.get('target_alignment_post_visibility') or 'not_visible'}",
        f"target_alignment_fresh_re_resolution_confirmed: {str(bool(result.get('target_alignment_fresh_re_resolution_confirmed'))).lower()}",
        f"total_unique_valid_chats_discovered: {result.get('total_unique_valid_chats_discovered', result.get('unique_chat_titles_printed', 0))}",
        f"search_elapsed_seconds: {result.get('search_elapsed_seconds', 0.0)}",
        f"unique_row_count_seen: {result.get('unique_row_count_seen', 0)}",
        f"matched_chat_row_path: {row.get('row_path') or row.get('path') or ''}",
        f"matched_chat_title_path: {row.get('title_path') or ''}",
        f"matched_chat_row_frame: {_compact_plain_frame(row.get('row_frame') or {})}",
        f"matched_canonical_title: {row.get('title') or ''}",
        f"matched_chat_title: {result.get('matched_chat_title') or row.get('title') or ''}",
        f"matched_title_representation: {result.get('matched_title_representation') or row.get('title_representation') or ''}",
        f"matched_accessibility_text_truncated: {result.get('matched_accessibility_text_truncated') or ''}",
        f"matched_visibility: {row.get('visibility') or ''}",
        f"chosen_method: {result.get('chosen_method') or ''}",
    ]
    if result.get("outcome") in {
        "chat_not_found_in_project",
        "chat_not_currently_visible_and_scroll_unavailable",
        "chat_list_scroll_target_not_found",
        "chat_list_scroll_failed",
        "chat_list_scroll_no_progress",
        "chat_list_scan_continuity_not_confirmed",
        "chat_list_end_reached_without_match",
        "chat_search_budget_exhausted_without_confirmed_end",
        "chat_search_time_budget_exhausted_while_list_progressing",
    }:
        lines.extend(
            [
                f"visible_title_count_seen: {result.get('visible_title_count_seen', 0)}",
                f"end_of_list_state: {result.get('end_of_list_state') or 'unknown'}",
                f"previous_settled_viewport_signature: {result.get('previous_settled_viewport_signature') or ''}",
                f"current_settled_viewport_signature: {result.get('current_settled_viewport_signature') or ''}",
                f"overlap_row_count: {result.get('overlap_row_count', 0)}",
                f"overlap_adjacency_confirmed: {result.get('overlap_adjacency_confirmed', True)}",
            ]
        )
    if result.get("target_found_during_hydration_cycle"):
        lines.append(f"target_found_during_hydration_cycle: {result.get('target_found_during_hydration_cycle')}")
    for summary in result.get("search_cycle_summaries") or []:
        lines.append(str(summary))
    if result.get("visible_chat_count_stage_explanation"):
        lines.append(f"visible_chat_count_stage_explanation: {result.get('visible_chat_count_stage_explanation')}")
    if result.get("outcome") in {"chat_not_currently_visible", "chat_title_not_unambiguously_representable_by_accessibility"}:
        lines.extend(
            [
                f"canonical_visible_chat_titles_considered: {result.get('canonical_visible_chat_titles_considered') or []}",
                f"canonical_visible_chat_count_considered: {result.get('canonical_visible_chat_count_considered', 0)}",
                f"resolver_snapshot_id: {result.get('resolver_snapshot_id') or ''}",
                f"visible_chat_accessibility_representation_summary: {result.get('visible_chat_accessibility_representation_summary') or []}",
            ]
        )
    if result.get("chosen_method") == "validated_geometry_click" or point.get("x") is not None or result.get("calculated_point_hit_test_relationship"):
        lines.extend(
            [
                f"calculated_global_point: x={point.get('x')} y={point.get('y')}",
                f"calculated_point_hit_test_relationship: {result.get('calculated_point_hit_test_relationship') or ''}",
            ]
        )
    lines.extend(
        [
            f"post_action_evidence: {post.get('signals') or []}",
            f"final_outcome: {result.get('outcome') or ''}",
            f"actions_performed: {result.get('actions_performed') or []}",
            f"error: {result.get('error') or ''}",
        ]
    )
    print("\n".join(lines))
    sys.stdout.flush()


def _print_live_project_chat_discovery_lines(lines: list[str]) -> None:
    for line in lines:
        print(line)
    sys.stdout.flush()
    sys.stdout.flush()


def _calibration_non_action_line(confirmed: bool) -> str:
    if confirmed:
        return "No cursor movement, cursor warp, app activation, focus change, menu opening, scroll, typing, paste, AppleScript, browser automation, or persistent calibration write was performed."
    return "No cursor movement, click, event post, app activation, focus change, selection change, menu opening, scroll, typing, paste, AppleScript, browser automation, or persistent calibration write was performed."


def _first_frame_evidence(frames: list[dict], source: str) -> dict:
    for frame in frames:
        if frame.get("source") == source:
            return frame
    return {}


def _compact_calibration_frame(frame: dict) -> str:
    return (
        f"source={frame.get('source') or ''} "
        f"path={frame.get('ax_path') or ''} "
        f"x={frame.get('x')} y={frame.get('y')} "
        f"width={frame.get('width')} height={frame.get('height')} "
        f"contains_cursor={frame.get('contains_global_physical_cursor')} "
        f"contains_title={frame.get('contains_target_title_frame')} "
        f"contains_row={frame.get('contains_target_row_frame')} "
        f"confidence={frame.get('coordinate_space_confidence') or ''}"
    )


def _coordinate_diagnostics_lines(coords: dict) -> list[str]:
    if not coords:
        return []

    def _frame(label: str, frame: dict) -> str:
        frame = frame or {}
        return (
            f"{label}: x={frame.get('x')} y={frame.get('y')} "
            f"width={frame.get('width')} height={frame.get('height')}"
        )

    def _point(label: str, point: dict) -> str:
        point = point or {}
        return f"{label}: x={point.get('x')} y={point.get('y')}"

    raw_in = coords.get("raw_point_containment") or {}
    inverted_in = coords.get("inverted_point_containment") or {}
    assessment = coords.get("assessment") or {}
    lines = [
        "coordinate_diagnostics:",
        "  " + _frame("raw_ax_row_frame", coords.get("raw_ax_row_frame")),
        "  " + _frame("ax_sidebar_frame", coords.get("ax_sidebar_frame")),
        "  " + _frame("focused_window_frame", coords.get("focused_window_frame")),
        "  " + _frame("primary_display_bounds", coords.get("primary_display_bounds")),
        "  " + _point("current_mouse_location", coords.get("current_mouse_location")),
        "  " + _point("intended_event_point", coords.get("intended_event_point")),
        "  " + _point("vertically_inverted_candidate_point", coords.get("vertically_inverted_candidate_point")),
        (
            "  raw_point_inside: "
            f"row={raw_in.get('in_ax_row_frame')} "
            f"sidebar={raw_in.get('in_ax_sidebar_frame')} "
            f"window={raw_in.get('in_focused_window_frame')}"
        ),
        (
            "  inverted_point_inside: "
            f"row={inverted_in.get('in_ax_row_frame')} "
            f"sidebar={inverted_in.get('in_ax_sidebar_frame')} "
            f"window={inverted_in.get('in_focused_window_frame')}"
        ),
        (
            "  assessment: "
            f"raw_point_matches_ax_frame={assessment.get('raw_point_matches_ax_frame')} "
            f"inverted_point_matches_ax_frame={assessment.get('inverted_point_matches_ax_frame')} "
            f"neither_point_matches_ax_frame={assessment.get('neither_point_matches_ax_frame')} "
            f"ambiguous_coordinate_mapping={assessment.get('ambiguous_coordinate_mapping')}"
        ),
        f"  cursor_unmoved: {str(bool(coords.get('cursor_unmoved'))).lower()}",
    ]
    if coords.get("probe_error"):
        lines.append(f"  coordinate_probe_error: {coords.get('probe_error')}")
    lines.append(
        f"recommended_click_coordinate_mapping: {coords.get('recommended_click_coordinate_mapping') or 'unresolved'}"
    )
    return lines


def _inspect_chatgpt_sidebar_destination_result_lines(result: dict) -> list[str]:
    target = result.get("target") or {}
    scope = result.get("scope") or {}
    assessment = result.get("primary_selection_assessment") or {}
    frame_evidence = result.get("frame_evidence") or {}
    click_source = frame_evidence.get("chosen_click_source") or {}
    click_point = frame_evidence.get("computed_safe_click_point") or {}
    lines = [
        "ChatGPT sidebar destination deep inspection",
        f"status: {result.get('status') or ''}",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"read_only: {str(bool(result.get('read_only'))).lower()}",
        f"app_name: {result.get('app_name') or ''}",
        f"kind: {result.get('kind') or ''}",
        f"title: {result.get('title') or ''}",
        f"pid_present: {str(bool(result.get('pid_present'))).lower()}",
        f"process_resolution_method: {result.get('process_resolution_method') or ''}",
        f"target_title_path: {target.get('title_ax_path') or ''}",
        f"target_row_path: {target.get('computed_row_ax_path') or ''}",
        f"existing_resolution_method: {target.get('current_resolution_method') or ''}",
        f"retained_elements: {scope.get('retained_element_count', 0)}",
        f"row_descendants: {scope.get('row_descendant_count', 0)}",
        f"siblings: {scope.get('sibling_count', 0)}",
        f"related_elements: {scope.get('related_count', 0)}",
        f"primary_selection_classification: {assessment.get('classification') or ''}",
        f"frame_title_node: {_compact_frame_evidence(frame_evidence.get('title_node') or {})}",
        f"frame_computed_row: {_compact_frame_evidence(frame_evidence.get('computed_row_node') or {})}",
        f"frame_nearest_visible_ancestor: {_compact_frame_evidence(frame_evidence.get('nearest_visible_ancestor_with_usable_frame') or {})}",
        f"frame_sidebar_or_list: {_compact_frame_evidence(frame_evidence.get('sidebar_or_list') or {})}",
        f"frame_focused_window: {_compact_frame_evidence(frame_evidence.get('focused_window') or {})}",
        (
            "computed_safe_click_point: "
            f"x={click_point.get('x')} y={click_point.get('y')} ok={click_point.get('ok')} "
            f"source={click_source.get('source_path') or ''}"
        ),
        f"error: {result.get('error') or ''}",
        "No app activation, focus change, selection change, menu opening, clipboard, typing, paste, click, keypress, or UI action was performed.",
    ]
    controls = assessment.get("viable_candidate_controls") or []
    lines.append(f"viable candidate controls: {len(controls)}")
    for index, control in enumerate(controls[:8], start=1):
        lines.append(
            "  "
            f"{index}. path={control.get('target_path') or ''} "
            f"relation={control.get('relation_to_requested_title') or ''} "
            f"confidence={control.get('confidence') or ''}"
        )
        lines.append(f"     actions={control.get('concrete_advertised_actions') or []}")
        lines.append(f"     settable={control.get('supported_and_settable_selection_focus_attributes') or {}}")
        lines.append(f"     why={control.get('why_primary_selection') or ''}")
    lines.append("retained local elements:")
    for index, element in enumerate((result.get("elements") or [])[:80], start=1):
        lines.extend(_sidebar_destination_element_lines(index, element))
        if _line_char_count(lines) >= DEEP_INSPECTOR_OUTPUT_CHAR_GUARD:
            lines.append("(remaining elements omitted by output guard)")
            break
    return lines[:]


def _compact_frame_evidence(item: dict) -> str:
    frame = item.get("frame") or {}
    if not frame:
        frame = item
    return (
        f"path={item.get('path') or ''} "
        f"x={frame.get('x')} y={frame.get('y')} "
        f"width={frame.get('width')} height={frame.get('height')} "
        f"valid={frame.get('valid')} "
        f"inside_window={frame.get('fully_inside_window')} "
        f"inside_sidebar={frame.get('inside_sidebar_or_list')} "
        f"large_enough={frame.get('large_enough_for_safe_interior_click')}"
    )


def _sidebar_destination_element_lines(index: int, element: dict) -> list[str]:
    title = element.get("title") or {}
    value = element.get("value") or {}
    supported = element.get("supported_attributes") or {}
    settable = element.get("settable_attributes") or {}
    row_structure = element.get("row_structure") or {}
    linked = element.get("linked_elements") or []
    return [
        (
            "  "
            f"{index}. path={element.get('path') or ''} "
            f"relation={element.get('relation_to_requested_title') or ''} "
            f"role={element.get('role') or ''} "
            f"subrole={element.get('subrole') or ''}"
        ),
        (
            "     "
            f"enabled={element.get('enabled')} focused={element.get('focused')} selected={element.get('selected')} "
            f"parent={element.get('parent_path') or ''}"
        ),
        (
            "     "
            f"title_literal={title.get('literal')!r} title_redacted={title.get('redacted')} "
            f"value_length={value.get('normalized_length', 0)} value_redacted={value.get('redacted')}"
        ),
        f"     actions={element.get('actions') or []}",
        f"     action_descriptions={element.get('action_descriptions') or {}}",
        (
            "     "
            f"children={element.get('direct_children_count', 0)} "
            f"visible_children={element.get('visible_children_count', 0)}"
        ),
        (
            "     "
            f"supported_focus_selection={{"
            f"'AXFocused': {supported.get('AXFocused')}, "
            f"'AXSelected': {supported.get('AXSelected')}, "
            f"'AXSelectedChildren': {supported.get('AXSelectedChildren')}, "
            f"'AXSelectedRows': {supported.get('AXSelectedRows')}"
            f"}}"
        ),
        f"     settable={settable}",
        (
            "     "
            f"row_paths={row_structure.get('AXRows') or []} "
            f"visible_rows={row_structure.get('AXVisibleRows') or []} "
            f"selected_rows={row_structure.get('AXSelectedRows') or []} "
            f"selected_children={row_structure.get('AXSelectedChildren') or []}"
        ),
        (
            "     "
            f"linked={[(item.get('attribute'), item.get('path')) for item in linked[:8]]}"
        ),
    ]


def _sidebar_destination_action_notice(kind: str, title: str) -> None:
    print(f'Explicit sidebar destination verification authorized for: {kind} "{title}".')
    sys.stdout.flush()


def _sidebar_frame_click_notice(kind: str, title: str) -> None:
    print(f'Explicit frame-click verification authorized for: {kind} "{title}".')
    sys.stdout.flush()


def _open_chatgpt_sidebar_destination_notice() -> None:
    print("Explicit ChatGPT sidebar destination open authorized.")
    sys.stdout.flush()


def _open_chatgpt_project_chat_notice() -> None:
    print("Explicit ChatGPT project chat open authorized.")
    sys.stdout.flush()


def _synthetic_click_probe_notice() -> None:
    print("Explicit synthetic-click probe authorized.")
    sys.stdout.flush()


def _current_cursor_click_notice() -> None:
    print("Explicit current-cursor click authorized.")
    sys.stdout.flush()


def _coordinate_calibration_click_notice() -> None:
    print("Explicit coordinate-calibration click authorized.")
    sys.stdout.flush()


def _print_verify_synthetic_click_delivery_result(result: dict) -> None:
    button_frame = result.get("button_frame") or {}
    window_frame = result.get("window_frame") or {}
    click_point = result.get("click_point") or {}
    permission = result.get("permission_preflight_state") or {}
    lines = [
        f"calculator_app_name: {result.get('app_name') or ''}",
        f"calculator_window_title: {result.get('calculator_window_title') or ''}",
        f"button_title: {result.get('digit_button_title') or ''}",
        f"pre_display_value: {result.get('pre_display_value')}",
        f"post_display_value: {result.get('post_display_value')}",
        (
            "button_frame: "
            f"x={button_frame.get('x')} y={button_frame.get('y')} "
            f"width={button_frame.get('width')} height={button_frame.get('height')}"
        ),
        (
            "window_frame: "
            f"x={window_frame.get('x')} y={window_frame.get('y')} "
            f"width={window_frame.get('width')} height={window_frame.get('height')}"
        ),
        f"click_point: x={click_point.get('x')} y={click_point.get('y')}",
        f"click_point_inside_button: {result.get('click_point_inside_button')}",
        f"click_point_inside_window: {result.get('click_point_inside_window')}",
        f"event_source_type: {result.get('event_source_type') or ''}",
        f"event_posting_target: {result.get('event_posting_target') or ''}",
        f"post_event_permission_available: {result.get('post_event_permission_available')}",
        f"permission_preflight_state: available={permission.get('available')} error={permission.get('error') or ''}",
        f"emitted_actions: {result.get('actions_performed') or []}",
        f"outcome: {result.get('status') or ''}",
    ]
    print("\n".join(lines))
    sys.stdout.flush()


def _print_verify_current_cursor_click_result(result: dict) -> None:
    cursor = result.get("current_cursor_location") or {}
    permission = result.get("permission_preflight_state") or {}
    lines = [
        "Warning: confirmed mode clicks whatever is currently under the physical cursor.",
        f"status: {result.get('status') or ''}",
        f"dry_run: {str(not bool(result.get('confirm_current_cursor_click'))).lower()}",
        f"current_cursor_location: x={cursor.get('x')} y={cursor.get('y')}",
        f"click_count: {result.get('click_count')}",
        f"inter_click_delay_ms: {result.get('inter_click_delay_ms')}",
        f"event_source_type: {result.get('event_source_type') or ''}",
        f"event_posting_target: {result.get('event_posting_target') or ''}",
        f"permission_preflight_state: available={permission.get('available')} error={permission.get('error') or ''}",
        f"actions_performed: {result.get('actions_performed') or []}",
        f"outcome: {result.get('status') or ''}",
        f"error: {result.get('error') or ''}",
    ]
    print("\n".join(lines))
    sys.stdout.flush()


def _inspect_chatgpt_navigation_ui_result_lines(result: dict) -> list[str]:
    traversal = result.get("traversal") or {}
    category_limits = result.get("category_limits") or {}
    lines = [
        "ChatGPT navigation UI diagnostic",
        f"ok: {str(bool(result.get('ok'))).lower()}",
        f"reason_code: {result.get('reason_code') or ''}",
        f"app_name: {result.get('app_name') or ''}",
        f"process_resolution_method: {result.get('process_resolution_method') or ''}",
        f"pid_present: {str(bool(result.get('pid_present'))).lower()}",
        f"window_available: {str(bool(result.get('window_available'))).lower()}",
        f"visited_nodes: {traversal.get('visited_nodes', 0)}",
        f"emitted_nodes: {traversal.get('emitted_nodes', 0)}",
    ]
    for key, label in (
        ("current_chat_identity_candidates", "current_chat_identity_candidates"),
        ("chat_history_candidates", "chat_history_candidates"),
        ("project_candidates", "project_candidates"),
        ("search_candidates", "search_candidates"),
        ("sidebar_candidates", "sidebar_candidates"),
        ("navigation_candidates", "navigation_candidates"),
        ("ambiguous_navigation_relevant_controls", "ambiguous_navigation_relevant_controls"),
    ):
        limits = category_limits.get(key) or {}
        total = limits.get("total", len(result.get(key) or []))
        omitted = limits.get("omitted", 0)
        lines.append(f"{label}: {total} total, {len(result.get(key) or [])} shown, {omitted} omitted")
    lines.append(f"error: {result.get('error') or ''}")
    lines.append(
        "No app activation, focus change, clipboard, typing, paste, click, keypress, ledger write, or UI action was performed."
    )
    if result.get("visible_navigation_title_disclosure_enabled"):
        lines.append(result.get("visible_navigation_title_disclosure_notice") or "")
    for key, title in (
        ("current_chat_identity_candidates", "current-chat identity"),
        ("chat_history_candidates", "chat history"),
        ("project_candidates", "projects"),
        ("search_candidates", "search"),
        ("sidebar_candidates", "sidebar"),
        ("navigation_candidates", "navigation"),
        ("ambiguous_navigation_relevant_controls", "ambiguous navigation-relevant controls"),
    ):
        lines.extend(_navigation_candidate_section_lines(title, result.get(key) or []))
    filtering = result.get("filtering_summary") or {}
    if filtering:
        lines.append("filtering_summary:")
        for key in sorted(filtering):
            lines.append(f"  {key}: {filtering[key]}")
    if result.get("visible_navigation_title_disclosure_enabled"):
        lines.extend(_visible_navigation_title_inventory_lines(result, _line_char_count(lines)))
    return lines


def _line_char_count(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _navigation_candidate_section_lines(title: str, candidates: list[dict]) -> list[str]:
    lines = [f"{title}:"]
    if not candidates:
        lines.append("  (none)")
        return lines
    for index, candidate in enumerate(candidates, start=1):
        label = candidate.get("label") or {}
        relationship = candidate.get("relationship") or {}
        lines.append(
            "  "
            f"{index}. path={candidate.get('path') or ''} "
            f"role={candidate.get('role') or ''} "
            f"subrole={candidate.get('subrole') or ''} "
            f"confidence={candidate.get('confidence') or ''}"
        )
        lines.append(
            "     "
            f"enabled={candidate.get('enabled')} "
            f"focused={candidate.get('focused')} "
            f"actionable={candidate.get('appears_actionable')} "
            f"future={candidate.get('future_explicit_approval_relevance') or ''}"
        )
        lines.append(
            "     "
            f"label_literal={label.get('literal')!r} "
            f"label_redacted={label.get('redacted')} "
            f"label_classification={label.get('classification') or ''} "
            f"label_length={label.get('normalized_length', 0)} "
            f"label_sha256={label.get('sha256') or ''}"
        )
        lines.append(f"     evidence={candidate.get('evidence_codes') or []}")
        lines.append(f"     actions={candidate.get('actions') or []}")
        lines.append(
            "     "
            f"parent={relationship.get('parent_path') or ''} "
            f"container={relationship.get('container_path') or ''} "
            f"list={relationship.get('list_path') or ''}"
        )
    return lines


def _visible_navigation_title_inventory_lines(result: dict, current_chars: int) -> list[str]:
    limits = result.get("visible_title_category_limits") or {}
    lines = ["visible navigation title inventory:"]
    remaining_chars = max(0, CHATGPT_NAVIGATION_COMPACT_OUTPUT_CHAR_GUARD - current_chars - _line_char_count(lines))
    for key, title in (
        ("visible_chat_title_candidates", "visible chat title candidates"),
        ("visible_project_title_candidates", "visible project title candidates"),
        ("visible_search_result_candidates", "visible search result candidates"),
        ("actionable_parent_candidates", "actionable parent candidates"),
        ("visible_navigation_section_labels", "visible navigation section labels"),
    ):
        category_limits = limits.get(key) or {}
        total = category_limits.get("total", len(result.get(key) or []))
        pre_omitted = category_limits.get("omitted", 0)
        candidates = result.get(key) or []
        if key == "actionable_parent_candidates":
            rendered, guard_omitted = _bounded_candidate_lines(
                candidates,
                _actionable_parent_inventory_entry_lines,
                remaining_chars,
            )
        else:
            rendered, guard_omitted = _bounded_candidate_lines(
                candidates,
                _title_candidate_inventory_entry_lines,
                remaining_chars,
            )
        shown = len(candidates) - guard_omitted
        omitted = pre_omitted + guard_omitted
        header = f"{title}: {total} total, {shown} shown, {omitted} omitted"
        lines.append(header)
        remaining_chars = max(0, remaining_chars - len(header) - 1)
        if candidates and rendered:
            lines.extend(rendered)
            remaining_chars = max(0, remaining_chars - _line_char_count(rendered))
        elif candidates:
            line = "  (omitted by compact output size guard)"
            lines.append(line)
            remaining_chars = max(0, remaining_chars - len(line) - 1)
        else:
            line = "  (none)"
            lines.append(line)
            remaining_chars = max(0, remaining_chars - len(line) - 1)
    return lines


def _bounded_candidate_lines(
    candidates: list[dict],
    renderer: object,
    remaining_chars: int,
) -> tuple[list[str], int]:
    lines: list[str] = []
    omitted = 0
    for index, candidate in enumerate(candidates, start=1):
        entry = renderer(index, candidate)
        entry_size = _line_char_count(entry)
        if entry_size > remaining_chars:
            omitted = len(candidates) - index + 1
            break
        lines.extend(entry)
        remaining_chars -= entry_size
    return lines, omitted


def _title_candidate_inventory_entry_lines(index: int, candidate: dict) -> list[str]:
    ancestor = candidate.get("nearest_actionable_ancestor") or {}
    container = candidate.get("nearest_list_container") or {}
    return [
        (
            "  "
            f"{index}. title={candidate.get('exact_title')!r} "
            f"path={candidate.get('path') or ''} "
            f"role={candidate.get('role') or ''} "
            f"subrole={candidate.get('subrole') or ''}"
        ),
        (
            "     "
            f"classification={candidate.get('classification') or ''} "
            f"confidence={candidate.get('confidence') or ''} "
            f"source={candidate.get('title_source_attribute') or ''} "
            f"capability={candidate.get('capability_assessment') or ''}"
        ),
        (
            "     "
            f"enabled={candidate.get('enabled')} "
            f"focused={candidate.get('focused')} "
            f"title_actionable={candidate.get('title_candidate_actionable')} "
            f"parent_actionable={candidate.get('parent_appears_actionable')}"
        ),
        f"     actions={candidate.get('actions') or []}",
        (
            "     "
            f"ancestor_path={ancestor.get('path') or ''} "
            f"ancestor_role={ancestor.get('role') or ''} "
            f"ancestor_actions={ancestor.get('actions') or []}"
        ),
        (
            "     "
            f"list_path={container.get('path') or ''} "
            f"list_role={container.get('role') or ''} "
            f"list_purpose={container.get('purpose') or ''}"
        ),
        f"     evidence={candidate.get('evidence_codes') or []}",
    ]


def _actionable_parent_inventory_entry_lines(index: int, candidate: dict) -> list[str]:
    container = candidate.get("nearest_list_container") or {}
    return [
        (
            "  "
            f"{index}. path={candidate.get('path') or ''} "
            f"role={candidate.get('role') or ''} "
            f"subrole={candidate.get('subrole') or ''} "
            f"capability={candidate.get('capability_assessment') or ''}"
        ),
        (
            "     "
            f"enabled={candidate.get('enabled')} "
            f"focused={candidate.get('focused')} "
            f"actions={candidate.get('actions') or []}"
        ),
        (
            "     "
            f"list_path={container.get('path') or ''} "
            f"list_role={container.get('role') or ''} "
            f"list_purpose={container.get('purpose') or ''}"
        ),
        (
            "     "
            f"example_child_title_path={candidate.get('example_child_title_path') or ''} "
            f"evidence={candidate.get('evidence_codes') or []}"
        ),
    ]


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


def _handle_human_decision_result(
    parser: argparse.ArgumentParser,
    result: HumanDecisionResult,
) -> None:
    if result.reason_code == "run_not_found":
        parser.exit(1, f"Run not found: {result.run_id}\n")

    if not result.ok:
        print(f"error: {result.error_message}", file=sys.stderr)
        raise SystemExit(1)

    _print_human_decision(
        result.previous_status or "",
        result.next_status or "",
        (result.metadata or {}).get("note", ""),
    )


def _decision_from_event_type(event_type: str) -> HumanDecision | None:
    if event_type == "human_approval":
        return HumanDecision.APPROVE
    if event_type == "human_rejection":
        return HumanDecision.REJECT
    if event_type == "human_review_completed":
        return HumanDecision.COMPLETE_REVIEW
    return None


class _PreloadedHumanDecisionLedger:
    def __init__(self, run_id: str, run: dict, delegate: object) -> None:
        self._run_id = run_id
        self._run = run
        self._delegate = delegate

    def get_run(self, run_id: str) -> dict | None:
        if run_id == self._run_id:
            return self._run
        return self._delegate.get_run(run_id)

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        final_summary: str | None = None,
        error: str | None = None,
    ) -> object:
        return self._delegate.update_run_status(
            run_id,
            status,
            final_summary=final_summary,
            error=error,
        )

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        metadata: dict | None = None,
    ) -> object:
        return self._delegate.add_event(
            run_id,
            event_type,
            message,
            metadata,
        )


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
    decision = _decision_from_event_type(allowed_event_type)
    if decision is None:
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
        return

    result = resolve_human_decision(
        run_id,
        decision,
        note=note,
        ledger=_PreloadedHumanDecisionLedger(run_id, run, ledger),
    )
    if not result.ok:
        print(f"error: {result.error_message}", file=sys.stderr)
        raise SystemExit(1)
    _print_human_decision(
        result.previous_status or "",
        result.next_status or "",
        (result.metadata or {}).get("note", ""),
    )


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


def _run_codex_exec_flow(
    run_id: str,
    run: dict,
    prompt: str,
    repo_path_text: str,
    sandbox: str,
    timeout: float | None,
    confirm_full_access: bool,
) -> dict:
    prompt_contract = parse_prompt_contract(prompt, sandbox).to_dict()
    git_snapshot = capture_git_snapshot(repo_path_text)
    invocation_state_before = capture_invocation_git_state(repo_path_text)
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

    validation_error = None
    if not prompt_contract["path_safety"]["valid"]:
        invalid_paths = ", ".join(prompt_contract["path_safety"]["invalid_paths"])
        validation_error = f"Prompt contract contains invalid path references: {invalid_paths}"
    elif sandbox == "danger-full-access" and not confirm_full_access:
        validation_error = "Codex sandbox danger-full-access requires --confirm-full-access."

    codex_execution = execute_codex_direct_service(
        run_id,
        prompt,
        repo_path_text,
        sandbox,
        timeout,
        prompt_contract,
        confirm_full_access=confirm_full_access,
        preflight_validation_error=validation_error,
        ledger=ledger,
    )
    result = codex_execution.raw_process_result or {}
    if result["validation_error"]:
        print(f"error: {result['validation_error']}", file=sys.stderr)
    _print_codex_exec_result(result)

    governance_result = apply_post_codex_governance_service(
        run_id,
        run,
        prompt,
        repo_path_text,
        sandbox,
        prompt_contract,
        result,
        git_snapshot,
        invocation_state_before,
        expected_scope={},
        ledger=ledger,
        git_snapshot_function=capture_git_snapshot,
        invocation_state_function=capture_invocation_git_state,
        delta_function=compute_invocation_delta,
        file_classifier_function=classify_changed_files,
        diagnostics_evaluator=analyze_prompt_repo_impact,
        supervision_decision_evaluator=evaluate_supervision_decision,
        status_policy_function=status_from_supervision_decision,
        callbacks=PostCodexGovernanceCallbacks(
            diagnostics_warning=lambda exc: print(
                f"warning: prompt/repo impact diagnostics unavailable: {exc}",
                file=sys.stderr,
            ),
            git_after_captured=lambda snapshot: _print_git_snapshot_summary(snapshot, "after"),
            changed_file_classification_recorded=_print_changed_file_classification,
            diagnostics_recorded=_print_prompt_repo_impact_diagnostics,
            supervision_decision_recorded=_print_supervision_decision,
            governance_observation_recorded=_print_governance_observation,
            workspace_write_human_required=_print_workspace_write_human_required,
            status_transition_recorded=_print_run_status_transition,
        ),
    )
    transition = governance_result.metadata["transition"]

    return {
        "result": result,
        "git_snapshot": git_snapshot,
        "after_git_snapshot": governance_result.git_after,
        "invocation_state_before": invocation_state_before,
        "invocation_state_after": governance_result.invocation_state_after,
        "invocation_delta": governance_result.invocation_delta,
        "prompt_contract": prompt_contract,
        "governance_observation": governance_result.governance_observation,
        "changed_file_classification": governance_result.changed_file_classification,
        "prompt_repo_impact_diagnostics": governance_result.diagnostics,
        "supervision_decision": governance_result.supervision_decision,
        "transition": transition,
        "post_codex_governance": governance_result,
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
    result = submit_feedback_to_chatgpt_service(
        run_id,
        run,
        app_name=app_name,
        output_path_text=output_path_text,
        approval_mode=approval_mode,
        ledger=ledger,
        feedback_builder=build_gpt_feedback_message,
        clipboard_copy_function=copy_to_clipboard,
        activation_function=activate_chatgpt,
        submission_ui_inspection_function=inspect_chatgpt_submission_ui,
        paste_function=paste_clipboard_to_frontmost_app,
        ax_send_button_function=press_chatgpt_send_button,
        enter_function=press_enter_in_frontmost_app,
        artifact_writer=_write_feedback_output,
        monotonic_function=time.monotonic,
        sleep_function=time.sleep,
        paste_verify_timeout_seconds=CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS,
        paste_verify_poll_seconds=CHATGPT_PASTE_VERIFY_POLL_SECONDS,
        post_paste_settle_seconds=CHATGPT_POST_PASTE_SETTLE_SECONDS,
        submission_verify_timeout_seconds=CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS,
        submission_verify_poll_seconds=CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS,
        submission_verification_function=_verify_submission_marker,
    )
    _print_chatgpt_feedback_submit_result(
        run_id,
        result.copy_result or {},
        result.activation_result or {},
        result.paste_result or {},
        result.send_result or {},
        result.output_path,
        result.error_message,
        result.verification_result,
    )
    return result.ok


def _capture_gpt_response_from_chatgpt_ax_flow(
    run_id: str,
    run: dict,
    app_name: str,
    timeout_seconds: float | None,
    stable_seconds: float,
    require_sentinel_response: bool = False,
) -> bool:
    del timeout_seconds
    result = capture_chatgpt_response_service(
        run_id,
        app_name=app_name,
        timeout_seconds=None,
        stable_seconds=stable_seconds,
        require_sentinel_response=require_sentinel_response,
        ledger=ledger,
        activation_function=activate_chatgpt,
        capture_function=capture_response_after_feedback,
        hash_function=sha256_text,
    )

    if result.reason_code == "no_verified_submission":
        print("Stopped: no verified ChatGPT submission was found for this run.")
        return False
    if result.reason_code == "missing_submission_marker":
        print("Stopped: verified submission event did not include a submission marker.")
        return False
    if result.reason_code == "submission_marker_sha_mismatch":
        print("Stopped: verified submission marker hash did not match marker text.")
        return False
    if result.reason_code == "chatgpt_not_frontmost":
        _print_chatgpt_ax_capture_result(
            run_id,
            result.activation_result or {},
            result.raw_capture_result or {},
            None,
        )
        return False
    _print_chatgpt_ax_capture_result(
        run_id,
        result.activation_result or {},
        result.raw_capture_result or {},
        result.event_type,
    )
    return result.ok


def _extract_next_codex_prompt_flow(
    run_id: str,
    require_sentinel: bool = False,
    confirm_extract: bool = True,
    output_path_text: str | None = None,
) -> bool:
    result = extract_next_codex_prompt_service(
        run_id,
        require_sentinel=require_sentinel,
        confirm_extract=confirm_extract,
        output_path_text=output_path_text,
        ledger=ledger,
    )
    if result.reason_code == "artifact_write_failed":
        raise OSError(result.error_message or "failed to write extracted Codex prompt output")

    _print_next_codex_prompt_extraction_result(
        run_id,
        result.selection,
        result.extraction,
        output_path=result.output_path,
        ledger_event=result.event_type,
    )
    if result.reason_code == "sentinel_required":
        print("Stopped: ChatGPT did not provide a sentinel-wrapped next Codex prompt.")
        _print_supervise_sentinel_requirement()
    return result.ok


def _run_extracted_codex_prompt_flow(
    run_id: str,
    run: dict,
    repo_path_text: str,
    sandbox: str,
    timeout: float | None,
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
    service_result = execute_extracted_codex_prompt_service(
        run_id,
        run,
        repo_path_text,
        sandbox,
        timeout,
        confirm_full_access=confirm_full_access,
        allow_full_access=allow_full_access,
        approval_mode=approval_mode,
        expected_extraction_event_id=expected_extraction_event_id,
        expected_prompt_sha256=expected_prompt_sha256,
        expected_prompt_text=expected_prompt_text,
        expected_extraction_method=expected_extraction_method,
        workspace_write_pre_run_policy=pre_run_policy,
        expected_scope=expected_scope,
        ledger=ledger,
        codex_flow_coordinator=_run_codex_exec_flow,
        hash_function=sha256_text,
        prompt_preview_callback=_print_extracted_codex_prompt_preview,
    )

    if service_result.codex_flow_result is not None and service_result.selection is not None:
        _print_extracted_codex_prompt_run_result(
            run_id,
            service_result.selection,
            service_result.metadata.get("selection_metadata", {}).get("repo_path", repo_path_text),
            sandbox,
            service_result.codex_flow_result,
        )

    if service_result.reason_code == "run_not_found":
        print(service_result.error_message, file=sys.stderr)
    elif service_result.reason_code == "continuation_denied":
        print(service_result.error_message, file=sys.stderr)
    elif service_result.reason_code in {
        "repo_missing",
        "repo_not_directory",
        "invalid_sandbox",
        "danger_full_access_blocked",
        "full_access_confirmation_required",
    }:
        print(service_result.error_message, file=sys.stderr)
    elif service_result.reason_code == "invalid_extracted_prompt":
        print(f"extraction_event_id: {service_result.selected_event_id or ''}", file=sys.stderr)
        for warning in service_result.metadata.get("selection_warnings", ()):
            print(f"warning: {warning}", file=sys.stderr)
        print(f"Invalid extracted Codex prompt: {service_result.error_message}", file=sys.stderr)
    elif service_result.reason_code == "extracted_prompt_changed_after_approval":
        print("Stopped: the next prompt changed after it was shown for approval.")
        print("No Codex run was started.")
        print("Run supervise again to review the current prompt.")
    elif service_result.reason_code == "selected_prompt_sha_validation_failed":
        print("Stopped: the selected prompt failed SHA validation.")
        print("No Codex run was started.")

    return service_result.exit_code or 0


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
    if args.stable_seconds < 0:
        print("error: --stable-seconds must be greater than or equal to 0.", file=sys.stderr)
        return 2

    approval_mode = _supervise_approval_mode(args)

    def submit_service(
        run_id: str,
        run: dict,
        app_name: str,
        *,
        approval_mode: str,
        ledger: object,
    ) -> bool:
        del ledger
        return _submit_feedback_to_chatgpt_flow(
            run_id,
            run,
            app_name,
            approval_mode=approval_mode,
        )

    def capture_service(
        run_id: str,
        run: dict,
        app_name: str,
        timeout_seconds: float | None,
        stable_seconds: float,
        *,
        require_sentinel_response: bool,
        ledger: object,
    ) -> bool:
        del ledger
        return _capture_gpt_response_from_chatgpt_ax_flow(
            run_id,
            run,
            app_name,
            timeout_seconds,
            stable_seconds,
            require_sentinel_response=require_sentinel_response,
        )

    def extraction_service(
        run_id: str,
        *,
        require_sentinel: bool,
        confirm_extract: bool,
        ledger: object,
    ) -> bool:
        del ledger
        return _extract_next_codex_prompt_flow(
            run_id,
            require_sentinel=require_sentinel,
            confirm_extract=confirm_extract,
        )

    def run_prompt_service(
        run_id: str,
        run: dict,
        repo_path_text: str,
        sandbox: str,
        timeout: float | None,
        *,
        expected_extraction_event_id: int | None,
        expected_prompt_sha256: str | None,
        expected_prompt_text: str | None,
        expected_extraction_method: str | None,
        approval_mode: str,
        pre_run_policy: dict | None,
        expected_scope: dict | None,
        ledger: object,
    ) -> int:
        del ledger
        return _run_extracted_codex_prompt_flow(
            run_id,
            run,
            repo_path_text,
            sandbox,
            timeout,
            expected_extraction_event_id=expected_extraction_event_id,
            expected_prompt_sha256=expected_prompt_sha256,
            expected_prompt_text=expected_prompt_text,
            expected_extraction_method=expected_extraction_method,
            approval_mode=approval_mode,
            pre_run_policy=pre_run_policy,
            expected_scope=expected_scope,
        )

    def auto_stop_recorder(
        run_id: str,
        plan: object,
        reason: str | None = None,
        *,
        ledger: object,
    ) -> dict:
        del ledger
        _record_supervise_auto_stop(run_id, plan, reason)
        return {
            "event_type": "supervise_auto_stopped",
            "event_id": None,
            "message": "Automatic supervise stopped at a mandatory human gate.",
            "metadata": {
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
        }

    def before_action(plan: object, run: dict | None, events: list[dict]) -> None:
        del run, events
        if plan.action == SuperviseAction.ASK_SEND_TO_GPT:
            _print_supervise_send_gate(args.run_id, plan)
        elif plan.action == SuperviseAction.CAPTURE_GPT_RESPONSE:
            print("Waiting for ChatGPT to finish responding, then capturing the visible reply.")
            print("The correct ChatGPT chat must already be open and visible.")
        elif plan.action == SuperviseAction.EXTRACT_NEXT_PROMPT:
            print("Extracting the next Codex prompt from the captured ChatGPT response.")
        elif plan.action == SuperviseAction.ASK_RUN_PROMPT:
            repo_snapshot = capture_git_snapshot(args.repo)
            _print_supervise_run_gate(plan, repo_snapshot)

    def run_step(
        *,
        approval_decision: str | None = None,
        expected_plan: object | None = None,
        emit_pre_action: bool = True,
    ):
        expected_event_ids = None
        expected_prompt_sha256 = None
        expected_planner_action = None
        if expected_plan is not None:
            expected_planner_action = str(getattr(expected_plan, "action", ""))
            expected_event_ids = getattr(expected_plan, "event_ids", {})
            expected_prompt_sha256 = getattr(expected_plan, "prompt_sha", "") or None
        return run_supervision_step(
            args.run_id,
            args.repo,
            args.sandbox,
            approval_mode=approval_mode,
            approval_decision=approval_decision,
            expected_planner_action=expected_planner_action,
            expected_event_ids=expected_event_ids,
            expected_prompt_sha256=expected_prompt_sha256,
            app_name=args.app_name,
            timeout=None,
            capture_timeout_seconds=None,
            capture_stable_seconds=args.stable_seconds,
            ledger=ledger,
            planner=detect_next_supervise_action,
            submit_service=submit_service,
            capture_service=capture_service,
            extraction_service=extraction_service,
            extracted_prompt_execution_service=run_prompt_service,
            send_auto_safety_evaluator=_send_plan_auto_safe,
            auto_stop_recorder=auto_stop_recorder,
            before_action_callback=before_action if emit_pre_action else None,
        )

    while True:
        step = run_step()
        plan = step.metadata.get("plan")

        if step.requires_human_approval:
            if step.approval_kind == "send_to_gpt":
                if not _confirm_yes_no("Send Codex result to ChatGPT?"):
                    run_step(approval_decision="rejected", emit_pre_action=False)
                    print("Stopped. Feedback was not submitted to ChatGPT.")
                    return 0
                step = run_step(
                    approval_decision="approved",
                    expected_plan=plan,
                    emit_pre_action=False,
                )
            elif step.approval_kind == "run_prompt":
                if not _confirm_yes_no("Run this prompt in Codex?"):
                    run_step(approval_decision="rejected", emit_pre_action=False)
                    print("Stopped. Codex was not run.")
                    return 0
                step = run_step(
                    approval_decision="approved",
                    expected_plan=plan,
                    emit_pre_action=False,
                )
            else:
                _print_supervise_stop(
                    type(
                        "UnknownApproval",
                        (),
                        {
                            "reason": step.reason_code or "approval_required",
                            "stop_message": step.error_message or "Unknown approval requirement.",
                            "status": step.run_status or "",
                            "warnings": (),
                        },
                    )()
                )
                return 1
            plan = step.metadata.get("plan")

        if step.terminal and step.planner_action == str(SuperviseAction.STOP):
            _print_supervise_stop(plan)
            if getattr(plan, "reason", "") in {"non_sentinel_prompt", "invalid_extracted_prompt"}:
                _print_supervise_sentinel_requirement()
            return 1

        if step.planner_action == str(SuperviseAction.ASK_SEND_TO_GPT):
            if not step.action_executed:
                print("Stopped: Codex result requires human approval before ChatGPT submission.")
                print(f"Reason: {step.reason_code or ''}")
                return 1
            if not step.ok:
                print("Stopped: failed to submit feedback to ChatGPT.")
                return 1
            continue

        if step.planner_action == str(SuperviseAction.CAPTURE_GPT_RESPONSE):
            if not step.ok:
                print("Stopped: could not safely capture ChatGPT's response.")
                print(
                    "Recovery: verify the intended ChatGPT chat is open and visible, then run "
                    f"agent-loop capture-gpt-response-from-chatgpt-ax {args.run_id} --confirm-capture"
                )
                return 1
            continue

        if step.planner_action == str(SuperviseAction.EXTRACT_NEXT_PROMPT):
            if not step.ok:
                print("Stopped: no valid sentinel-wrapped next Codex prompt was extracted.")
                print(
                    "Recovery: ask ChatGPT for a sentinel-wrapped prompt or run "
                    f"agent-loop extract-next-codex-prompt {args.run_id} --confirm-extract for diagnostics."
                )
                return 1
            continue

        if step.planner_action == str(SuperviseAction.ASK_RUN_PROMPT):
            if not step.action_executed:
                print("Stopped: extracted prompt requires human approval before Codex execution.")
                print(f"Reason: {step.reason_code or ''}")
                return 1
            exit_code = step.action_result if isinstance(step.action_result, int) else 0
            if exit_code != 0:
                return exit_code
            refreshed_events = ledger.list_events(args.run_id)
            approved_codex_event_id = step.metadata.get("approved_codex_event_id")
            if not isinstance(approved_codex_event_id, int):
                approved_codex_event_id = -1
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
                    "stop_message": step.error_message or f"Unknown supervise action: {step.planner_action}",
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
        timeout=None,
        capture_timeout_seconds=None,
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
        handle_init_command(args, ledger=ledger)
        return

    if args.command == "start":
        handle_start_command(
            args,
            ledger=ledger,
            create_run_service=create_run_service,
        )
        return

    if args.command == "show":
        handle_show_command(
            args,
            parser=parser,
            ledger=ledger,
            print_run=_print_run,
        )
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
        if args.stable_seconds < 0:
            parser.exit(2, "error: --stable-seconds must be greater than or equal to 0.\n")

        run = ledger.get_run(args.run_id)
        if run is None:
            parser.exit(1, f"Run not found: {args.run_id}\n")

        ok = _capture_gpt_response_from_chatgpt_ax_flow(
            args.run_id,
            run,
            args.app_name,
            None,
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

    if args.command == "inspect-chatgpt-navigation-ui":
        if args.max_depth < 0:
            parser.exit(2, "error: inspect-chatgpt-navigation-ui requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: inspect-chatgpt-navigation-ui requires --max-nodes > 0.\n")
        result = inspect_chatgpt_navigation_ui(
            app_name=args.app_name,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            include_visible_navigation_titles=args.include_visible_navigation_titles,
        )
        _print_inspect_chatgpt_navigation_ui_result(result, include_json_details=args.include_json_details)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "verify-chatgpt-sidebar-destination":
        title = " ".join((args.title or "").split())
        if not title:
            parser.exit(2, "error: verify-chatgpt-sidebar-destination requires a non-empty --title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: verify-chatgpt-sidebar-destination requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: verify-chatgpt-sidebar-destination requires --max-nodes > 0.\n")
        result = verify_chatgpt_sidebar_destination(
            app_name=args.app_name,
            kind=args.kind,
            title=title,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            before_action_callback=_sidebar_destination_action_notice,
        )
        _print_verify_chatgpt_sidebar_destination_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "open-chatgpt-sidebar-destination":
        title = " ".join((args.title or "").split())
        if not title:
            parser.exit(2, "error: open-chatgpt-sidebar-destination requires a non-empty --title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: open-chatgpt-sidebar-destination requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: open-chatgpt-sidebar-destination requires --max-nodes > 0.\n")
        result = open_chatgpt_sidebar_destination(
            app_name=args.app_name,
            kind=args.kind,
            title=title,
            confirm_open_destination=args.confirm_open_destination,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            activation_function=activate_chatgpt,
            before_action_callback=_open_chatgpt_sidebar_destination_notice,
        )
        _print_open_chatgpt_sidebar_destination_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "inspect-chatgpt-sidebar-destination":
        title = " ".join((args.title or "").split())
        if not title:
            parser.exit(2, "error: inspect-chatgpt-sidebar-destination requires a non-empty --title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: inspect-chatgpt-sidebar-destination requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: inspect-chatgpt-sidebar-destination requires --max-nodes > 0.\n")
        result = inspect_chatgpt_sidebar_destination(
            app_name=args.app_name,
            kind=args.kind,
            title=title,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
        )
        _print_inspect_chatgpt_sidebar_destination_result(result, include_json_details=args.include_json_details)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "inspect-chatgpt-project-visible-chats":
        project_title = " ".join((args.project_title or "").split())
        if not project_title:
            parser.exit(2, "error: inspect-chatgpt-project-visible-chats requires a non-empty --project-title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: inspect-chatgpt-project-visible-chats requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: inspect-chatgpt-project-visible-chats requires --max-nodes > 0.\n")
        result = inspect_chatgpt_project_visible_chats(
            app_name=args.app_name,
            project_title=project_title,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
        )
        _print_inspect_chatgpt_project_visible_chats_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "inspect-chatgpt-project-chat-row-ax":
        project_title = " ".join((args.project_title or "").split())
        chat_titles = [" ".join((title or "").split()) for title in args.chat_title or []]
        chat_titles = [title for title in chat_titles if title]
        if not project_title:
            parser.exit(2, "error: inspect-chatgpt-project-chat-row-ax requires a non-empty --project-title.\n")
        if not chat_titles:
            parser.exit(2, "error: inspect-chatgpt-project-chat-row-ax requires at least one non-empty --chat-title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: inspect-chatgpt-project-chat-row-ax requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: inspect-chatgpt-project-chat-row-ax requires --max-nodes > 0.\n")
        result = inspect_chatgpt_project_chat_row_ax(
            app_name=args.app_name,
            project_title=project_title,
            chat_titles=chat_titles,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
        )
        _print_inspect_chatgpt_project_chat_row_ax_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "diagnose-chatgpt-project-chat-rows":
        project_title = " ".join((args.project_title or "").split())
        contains_title = " ".join((args.contains_title or "").split())
        if not project_title:
            parser.exit(2, "error: diagnose-chatgpt-project-chat-rows requires a non-empty --project-title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: diagnose-chatgpt-project-chat-rows requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: diagnose-chatgpt-project-chat-rows requires --max-nodes > 0.\n")
        result = diagnose_chatgpt_project_chat_rows(
            app_name=args.app_name,
            project_title=project_title,
            contains_title=contains_title,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
        )
        _print_diagnose_chatgpt_project_chat_rows_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "open-chatgpt-project-chat":
        project_title = " ".join((args.project_title or "").split())
        chat_title = " ".join((args.chat_title or "").split())
        if not project_title:
            parser.exit(2, "error: open-chatgpt-project-chat requires a non-empty --project-title.\n")
        if not chat_title:
            parser.exit(2, "error: open-chatgpt-project-chat requires a non-empty --chat-title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: open-chatgpt-project-chat requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: open-chatgpt-project-chat requires --max-nodes > 0.\n")
        if args.confirm_open_chat:
            _open_chatgpt_project_chat_notice()
        result = open_chatgpt_project_chat(
            app_name=args.app_name,
            project_title=project_title,
            chat_title=chat_title,
            confirm_open_chat=args.confirm_open_chat,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            activation_function=activate_chatgpt,
            discovery_output_function=_print_live_project_chat_discovery_lines,
        )
        _print_open_chatgpt_project_chat_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "calibrate-chatgpt-sidebar-coordinate-mapping":
        title = " ".join((args.title or "").split())
        if not title:
            parser.exit(2, "error: calibrate-chatgpt-sidebar-coordinate-mapping requires a non-empty --title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: calibrate-chatgpt-sidebar-coordinate-mapping requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: calibrate-chatgpt-sidebar-coordinate-mapping requires --max-nodes > 0.\n")
        result = calibrate_chatgpt_sidebar_coordinate_mapping(
            app_name=args.app_name,
            kind=args.kind,
            title=title,
            confirm_calibration_click=args.confirm_calibration_click,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            before_click_callback=_coordinate_calibration_click_notice,
        )
        _print_calibrate_chatgpt_sidebar_coordinate_mapping_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "verify-chatgpt-sidebar-frame-click":
        title = " ".join((args.title or "").split())
        if not title:
            parser.exit(2, "error: verify-chatgpt-sidebar-frame-click requires a non-empty --title.\n")
        if args.max_depth < 0:
            parser.exit(2, "error: verify-chatgpt-sidebar-frame-click requires --max-depth >= 0.\n")
        if args.max_nodes <= 0:
            parser.exit(2, "error: verify-chatgpt-sidebar-frame-click requires --max-nodes > 0.\n")
        result = verify_chatgpt_sidebar_frame_click(
            app_name=args.app_name,
            kind=args.kind,
            title=title,
            confirm_frame_click=args.confirm_frame_click,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            before_click_callback=_sidebar_frame_click_notice,
        )
        _print_verify_chatgpt_sidebar_frame_click_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "verify-synthetic-click-delivery":
        result = verify_synthetic_click_delivery(
            confirm_synthetic_click_probe=args.confirm_synthetic_click_probe,
            before_click_callback=_synthetic_click_probe_notice,
        )
        _print_verify_synthetic_click_delivery_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

    if args.command == "verify-current-cursor-click":
        result = verify_current_cursor_click(
            confirm_current_cursor_click=args.confirm_current_cursor_click,
            before_click_callback=_current_cursor_click_notice,
        )
        _print_verify_current_cursor_click_result(result)
        raise SystemExit(0 if result.get("ok") else 1)

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
        handle_can_continue_command(
            args,
            parser=parser,
            ledger=ledger,
            can_continue_run=can_continue_run,
            continuation_check_message=_continuation_check_message,
            print_continuation_check=_print_continuation_check,
        )

    if args.command == "release-stale-chatgpt-ui-lease":
        if not args.confirm_stale:
            parser.exit(
                2,
                "error: release-stale-chatgpt-ui-lease requires --confirm-stale. "
                "No lease release event was written.\n",
            )
        if args.owner_pid <= 0:
            parser.exit(2, "error: --owner-pid must be a positive integer.\n")
        if args.active_event_id <= 0:
            parser.exit(2, "error: --active-event-id must be a positive integer.\n")
        if _pid_exists(args.owner_pid) and not args.allow_owner_pid_alive:
            parser.exit(
                2,
                (
                    f"error: owner PID {args.owner_pid} currently exists. "
                    "No lease release event was written. Verify whether this is the original "
                    "owner process or PID reuse; rerun with --allow-owner-pid-alive only after "
                    "that separate verification.\n"
                ),
            )

        result = ledger.manual_release_stale_chatgpt_ui_lease(
            owning_run_id=args.owning_run_id,
            owner_pid=args.owner_pid,
            acquired_at=args.acquired_at,
            active_event_id=args.active_event_id,
            expected_run_status=args.expected_run_status,
            expected_lease_token_sha256=args.expected_lease_token_sha256,
            reason=args.reason,
            source=args.source,
            confirm_stale=True,
        )
        _print_manual_stale_lease_release_result(result)
        raise SystemExit(
            0
            if result.status == ledger.AtomicChatGPTUILeaseStatus.RELEASED
            else 1
        )

    if args.command == "approve":
        handle_human_decision_command(
            args,
            parser=parser,
            decision=HumanDecision.APPROVE,
            resolve_human_decision=resolve_human_decision,
            handle_human_decision_result=_handle_human_decision_result,
        )
        return

    if args.command == "reject":
        handle_human_decision_command(
            args,
            parser=parser,
            decision=HumanDecision.REJECT,
            resolve_human_decision=resolve_human_decision,
            handle_human_decision_result=_handle_human_decision_result,
        )
        return

    if args.command == "complete-review":
        handle_human_decision_command(
            args,
            parser=parser,
            decision=HumanDecision.COMPLETE_REVIEW,
            resolve_human_decision=resolve_human_decision,
            handle_human_decision_result=_handle_human_decision_result,
        )
        return

    if args.command == "supervise":
        raise SystemExit(_run_supervise_command(args))

    if args.command == "run-shell":
        handle_run_shell_command(
            args,
            parser=parser,
            ledger=ledger,
            run_command=run_command,
            format_command=_format_command,
            print_shell_result=_print_shell_result,
            normalize_shell_command=_normalize_shell_command,
            timeout_seconds=DEFAULT_SHELL_TIMEOUT_SECONDS,
        )
        return

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
                None,
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
            None,
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
        handle_codex_check_command(
            args,
            parser=parser,
            ledger=ledger,
            check_codex_environment=check_codex_environment,
            print_codex_check_result=_print_codex_check_result,
            timeout_seconds=DEFAULT_CODEX_CHECK_TIMEOUT_SECONDS,
        )
        return


if __name__ == "__main__":
    main()
