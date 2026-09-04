from __future__ import annotations

import argparse

from agent.chatgpt_ax_capture import DEFAULT_STABLE_SECONDS
from agent.chatgpt_navigation_diagnostic import (
    DEFAULT_MAX_DEPTH as DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
    DEFAULT_MAX_NODES as DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
)


def build_parser() -> argparse.ArgumentParser:
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
        default=None,
        help="Deprecated; ignored. ChatGPT response capture has no elapsed-time deadline.",
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

    inspect_chatgpt_navigation_ui_parser = subparsers.add_parser(
        "inspect-chatgpt-navigation-ui",
        help="Read-only structural diagnostic for ChatGPT navigation accessibility UI.",
        description="Read-only structural diagnostic for ChatGPT navigation accessibility UI.",
    )
    inspect_chatgpt_navigation_ui_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    inspect_chatgpt_navigation_ui_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    inspect_chatgpt_navigation_ui_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )
    inspect_chatgpt_navigation_ui_parser.add_argument(
        "--include-visible-navigation-titles",
        action="store_true",
        help="Explicit opt-in: disclose exact visible chat/project titles only from validated navigation list structures.",
    )
    inspect_chatgpt_navigation_ui_parser.add_argument(
        "--include-json-details",
        action="store_true",
        help="Include bounded structured JSON details. Default output is compact human-readable text only.",
    )

    verify_chatgpt_sidebar_destination_parser = subparsers.add_parser(
        "verify-chatgpt-sidebar-destination",
        help="Explicitly press one visible ChatGPT sidebar destination and verify the result.",
        description=(
            "Explicit one-destination UI action and verification for a currently visible ChatGPT "
            "Projects or Recents entry. This is separate from the read-only inventory command."
        ),
    )
    verify_chatgpt_sidebar_destination_parser.add_argument(
        "--kind",
        choices=("project", "chat"),
        required=True,
        help="Destination kind. Must be exactly project or chat.",
    )
    verify_chatgpt_sidebar_destination_parser.add_argument(
        "--title",
        required=True,
        help="Exact non-empty visible sidebar title to verify.",
    )
    verify_chatgpt_sidebar_destination_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    verify_chatgpt_sidebar_destination_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    verify_chatgpt_sidebar_destination_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    open_chatgpt_sidebar_destination_parser = subparsers.add_parser(
        "open-chatgpt-sidebar-destination",
        help="Open one exact currently visible ChatGPT sidebar project or chat destination.",
        description=(
            "Production ChatGPT sidebar destination opener. Dry-run by default; requires "
            "--confirm-open-destination before any ChatGPT activation, AXPress, or native click is performed."
        ),
    )
    open_chatgpt_sidebar_destination_parser.add_argument(
        "--kind",
        choices=("project", "chat"),
        required=True,
        help="Destination kind. Must be exactly project or chat.",
    )
    open_chatgpt_sidebar_destination_parser.add_argument(
        "--title",
        required=True,
        help="Exact non-empty visible sidebar title to open.",
    )
    open_chatgpt_sidebar_destination_parser.add_argument(
        "--confirm-open-destination",
        action="store_true",
        help="Required to activate ChatGPT and open the destination.",
    )
    open_chatgpt_sidebar_destination_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    open_chatgpt_sidebar_destination_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    open_chatgpt_sidebar_destination_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    inspect_chatgpt_sidebar_destination_parser = subparsers.add_parser(
        "inspect-chatgpt-sidebar-destination",
        help="Read-only deep AX inspection for one visible ChatGPT sidebar destination.",
        description=(
            "Read-only deep Accessibility inspection for exactly one currently visible ChatGPT "
            "Projects or Recents destination. No UI action is performed."
        ),
    )
    inspect_chatgpt_sidebar_destination_parser.add_argument(
        "--kind",
        choices=("project", "chat"),
        required=True,
        help="Destination kind. Must be exactly project or chat.",
    )
    inspect_chatgpt_sidebar_destination_parser.add_argument(
        "--title",
        required=True,
        help="Exact non-empty visible sidebar title to inspect.",
    )
    inspect_chatgpt_sidebar_destination_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    inspect_chatgpt_sidebar_destination_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    inspect_chatgpt_sidebar_destination_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )
    inspect_chatgpt_sidebar_destination_parser.add_argument(
        "--include-json-details",
        action="store_true",
        help="Include capped structured JSON details for the retained local AX evidence.",
    )

    inspect_chatgpt_project_visible_chats_parser = subparsers.add_parser(
        "inspect-chatgpt-project-visible-chats",
        help="Read-only inspector for currently visible chat rows in an already-open ChatGPT project.",
        description=(
            "Read-only AX inspection for visible chat rows in the currently open ChatGPT project. "
            "This command does not activate ChatGPT, click, scroll, press keys, paste, or open chats."
        ),
    )
    inspect_chatgpt_project_visible_chats_parser.add_argument(
        "--project-title",
        required=True,
        help="Exact title of the already-open ChatGPT project.",
    )
    inspect_chatgpt_project_visible_chats_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    inspect_chatgpt_project_visible_chats_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    inspect_chatgpt_project_visible_chats_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    inspect_chatgpt_project_chat_row_ax_parser = subparsers.add_parser(
        "inspect-chatgpt-project-chat-row-ax",
        help="Read-only bounded AX subtree audit for exact visible ChatGPT project chat rows.",
        description=(
            "Read-only AX structure audit for accepted visible project chat rows. "
            "No app activation, click, scroll, keypress, paste, cursor read, or workflow action is performed."
        ),
    )
    inspect_chatgpt_project_chat_row_ax_parser.add_argument(
        "--project-title",
        required=True,
        help="Exact title of the already-open ChatGPT project.",
    )
    inspect_chatgpt_project_chat_row_ax_parser.add_argument(
        "--chat-title",
        action="append",
        required=True,
        help="Requested visible chat title to audit. May be supplied more than once.",
    )
    inspect_chatgpt_project_chat_row_ax_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    inspect_chatgpt_project_chat_row_ax_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    inspect_chatgpt_project_chat_row_ax_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    diagnose_chatgpt_project_chat_rows_parser = subparsers.add_parser(
        "diagnose-chatgpt-project-chat-rows",
        help="Read-only visual-row diagnostic for an already-open ChatGPT project Chats list.",
        description=(
            "Read-only AX diagnostic for every visible visual row band in the confirmed project Chats list. "
            "No app activation, click, scroll, AXPress, keyboard, paste, cursor, screenshot, OCR, browser, or workflow action is performed."
        ),
    )
    diagnose_chatgpt_project_chat_rows_parser.add_argument(
        "--project-title",
        required=True,
        help="Exact title of the already-open ChatGPT project.",
    )
    diagnose_chatgpt_project_chat_rows_parser.add_argument(
        "--contains-title",
        default="",
        help="Optional diagnostic output filter. Collection and matching remain unchanged.",
    )
    diagnose_chatgpt_project_chat_rows_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    diagnose_chatgpt_project_chat_rows_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    diagnose_chatgpt_project_chat_rows_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    open_chatgpt_project_chat_parser = subparsers.add_parser(
        "open-chatgpt-project-chat",
        help="Open one exact currently visible chat inside an exact ChatGPT project.",
        description=(
            "Autonomous ChatGPT project chat opener. It opens/confirms the exact project, resolves "
            "currently visible project chat rows, and opens one exact visible chat only when "
            "--confirm-open-chat is supplied."
        ),
    )
    open_chatgpt_project_chat_parser.add_argument(
        "--project-title",
        required=True,
        help="Exact ChatGPT project title to open first.",
    )
    open_chatgpt_project_chat_parser.add_argument(
        "--chat-title",
        required=True,
        help="Exact currently visible project chat title to open.",
    )
    open_chatgpt_project_chat_parser.add_argument(
        "--confirm-open-chat",
        action="store_true",
        help="Required to open the project and then perform one exact chat opening action.",
    )
    open_chatgpt_project_chat_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to activate. Default: ChatGPT.",
    )
    open_chatgpt_project_chat_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    open_chatgpt_project_chat_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    calibrate_chatgpt_sidebar_coordinate_mapping_parser = subparsers.add_parser(
        "calibrate-chatgpt-sidebar-coordinate-mapping",
        help="Read-only calibration inspector for ChatGPT sidebar coordinate mapping.",
        description=(
            "Read-only manual coordinate calibration. Place the physical cursor over the exact visible "
            "ChatGPT sidebar title before running. The command captures AX hit-test and frame evidence "
            "without clicking, moving the cursor, focusing, scrolling, typing, or changing UI state."
        ),
    )
    calibrate_chatgpt_sidebar_coordinate_mapping_parser.add_argument(
        "--kind",
        choices=("project", "chat"),
        required=True,
        help="Destination kind. Must be exactly project or chat.",
    )
    calibrate_chatgpt_sidebar_coordinate_mapping_parser.add_argument(
        "--title",
        required=True,
        help="Exact non-empty visible sidebar title to calibrate.",
    )
    calibrate_chatgpt_sidebar_coordinate_mapping_parser.add_argument(
        "--confirm-calibration-click",
        action="store_true",
        help="Required to emit two calculated native clicks and confirm mapping from post-click UI state.",
    )
    calibrate_chatgpt_sidebar_coordinate_mapping_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    calibrate_chatgpt_sidebar_coordinate_mapping_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    calibrate_chatgpt_sidebar_coordinate_mapping_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    verify_chatgpt_sidebar_frame_click_parser = subparsers.add_parser(
        "verify-chatgpt-sidebar-frame-click",
        help="Dry-run or explicitly perform one frame-derived primary sidebar row click.",
        description=(
            "Frame-resolved ChatGPT sidebar destination verification. Dry-run by default; "
            "requires --confirm-frame-click before any native mouse event is posted."
        ),
    )
    verify_chatgpt_sidebar_frame_click_parser.add_argument(
        "--kind",
        choices=("project", "chat"),
        required=True,
        help="Destination kind. Must be exactly project or chat.",
    )
    verify_chatgpt_sidebar_frame_click_parser.add_argument(
        "--title",
        required=True,
        help="Exact non-empty visible sidebar title to resolve immediately before clicking.",
    )
    verify_chatgpt_sidebar_frame_click_parser.add_argument(
        "--confirm-frame-click",
        action="store_true",
        help="Required to post one native CoreGraphics left mouse down/up at the resolved safe point.",
    )
    verify_chatgpt_sidebar_frame_click_parser.add_argument(
        "--app-name",
        default="ChatGPT",
        help="macOS application name to inspect. Default: ChatGPT.",
    )
    verify_chatgpt_sidebar_frame_click_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH,
        help=f"Maximum AX tree depth to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_DEPTH}.",
    )
    verify_chatgpt_sidebar_frame_click_parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES,
        help=f"Maximum AX nodes to inspect. Default: {DEFAULT_NAVIGATION_DIAGNOSTIC_MAX_NODES}.",
    )

    verify_synthetic_click_delivery_parser = subparsers.add_parser(
        "verify-synthetic-click-delivery",
        help="Manual diagnostic: determine whether a single synthetic click is actually delivered.",
        description=(
            "Use macOS Calculator as the controlled click-delivery target and determine whether one "
            "synthetic CoreGraphics click on the visible digit 7 button changes Calculator's display. "
            "Dry-run by default; requires --confirm-synthetic-click-probe to emit exactly one click "
            "through the same unchanged click service used by verify-chatgpt-sidebar-frame-click. "
            "This is a manual diagnostic command only."
        ),
    )
    verify_synthetic_click_delivery_parser.add_argument(
        "--confirm-synthetic-click-probe",
        action="store_true",
        help="Required to post exactly one native CoreGraphics left mouse down/up onto Calculator digit 7.",
    )

    verify_current_cursor_click_parser = subparsers.add_parser(
        "verify-current-cursor-click",
        help="Manual diagnostic: post one synthetic click at the current physical cursor location.",
        description=(
            "Blind manual CoreGraphics click diagnostic. Dry-run reads only the current cursor location. "
            "Confirmed mode posts exactly one left mouse down/up at the current physical cursor location "
            "through the same unchanged click service used by verify-chatgpt-sidebar-frame-click."
        ),
    )
    verify_current_cursor_click_parser.add_argument(
        "--confirm-current-cursor-click",
        action="store_true",
        help="Required to post exactly one native CoreGraphics left mouse down/up at the current cursor location.",
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

    release_stale_lease_parser = subparsers.add_parser(
        "release-stale-chatgpt-ui-lease",
        help="Manually append a release event for an operator-confirmed stale ChatGPT UI lease.",
    )
    release_stale_lease_parser.add_argument(
        "--owning-run-id",
        required=True,
        help="Expected run ID that currently owns the active lease.",
    )
    release_stale_lease_parser.add_argument(
        "--owner-pid",
        required=True,
        type=int,
        help="Expected owner PID recorded on the active lease.",
    )
    release_stale_lease_parser.add_argument(
        "--acquired-at",
        required=True,
        help="Expected acquired_at timestamp recorded on the active lease.",
    )
    release_stale_lease_parser.add_argument(
        "--active-event-id",
        "--lease-event-id",
        dest="active_event_id",
        required=True,
        type=int,
        help="Expected event id of the active chatgpt_ui_lease_acquired event.",
    )
    release_stale_lease_parser.add_argument(
        "--expected-run-status",
        help="Optional expected current status of the owning run, for example completed.",
    )
    release_stale_lease_parser.add_argument(
        "--expected-lease-token-sha256",
        help="Optional expected active lease token fingerprint.",
    )
    release_stale_lease_parser.add_argument(
        "--reason",
        required=True,
        help="Operator-visible reason for the manual stale lease release.",
    )
    release_stale_lease_parser.add_argument(
        "--source",
        default="manual_stale_release",
        help="Release source metadata. Default: manual_stale_release.",
    )
    release_stale_lease_parser.add_argument(
        "--confirm-stale",
        action="store_true",
        help="Required. Confirms the active owner was manually verified stale.",
    )
    release_stale_lease_parser.add_argument(
        "--allow-owner-pid-alive",
        action="store_true",
        help="Allow release even if the owner PID currently exists after separate manual PID-reuse verification.",
    )

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
        default=None,
        help="Deprecated; ignored. Codex execution has no elapsed-time deadline.",
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
        default=None,
        help="Deprecated; ignored. Codex execution has no elapsed-time deadline.",
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
        default=None,
        help="Deprecated; ignored. Codex execution has no elapsed-time deadline.",
    )
    supervise_parser.add_argument(
        "--capture-timeout-seconds",
        type=float,
        default=None,
        help="Deprecated; ignored. ChatGPT response capture has no elapsed-time deadline.",
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
