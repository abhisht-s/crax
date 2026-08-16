from __future__ import annotations

import ctypes
import hashlib
import math
import re
import sys
import time
from collections import Counter
from ctypes import POINTER, Structure, byref, c_bool, c_char_p, c_double, c_float, c_int, c_long, c_uint32, c_ulong, c_void_p, create_string_buffer
from dataclasses import dataclass, replace
from typing import Protocol


PROCESS_RESOLUTION_METHOD = "nsworkspace_running_applications"
CLASSIC_CHATGPT_BUNDLE_IDS = {"com.openai.chat", "com.openai.chatgpt"}
AX_READ_METHOD = "macos_accessibility_read_only_navigation_tree"
PRIVACY_POLICY_VERSION = "chatgpt_navigation_ui_tree_v1"
DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_NODES = 900
MAX_CANDIDATES_PER_CATEGORY = 10
MAX_GENERIC_CONTROLS = 20
MAX_ACTIONS_PER_ELEMENT = 12
MAX_ACTION_NAME_LENGTH = 96
MAX_PATH_LENGTH = 160
MAX_ROLE_LENGTH = 64
LONG_TEXT_REDACTION_THRESHOLD = 160
VISIBLE_NAVIGATION_TITLE_MAX_LENGTH = 180
PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH = 180
PROJECT_VISIBLE_CHAT_PREVIEW_MAX_LENGTH = 120
PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE = 6.0
# Forward Chats-list identity gate. The project chat-list container must be
# independently resolved (structurally anchored below the project Chats/Sources
# tabs and inside the project content pane) before any candidate chat row is
# admitted. Generic composer/transcript/sidebar controls such as "Attach" or
# "Work with Apps" are short and ungrouped; live project chat rows are tall,
# vertically stacked peer buttons. These thresholds gate row admission on real
# list structure rather than on a locale-specific denylist.
PROJECT_CHAT_ROW_MIN_HEIGHT = 40.0
PROJECT_CHATS_LIST_MIN_HEIGHT = 60.0
PROJECT_CHATS_LIST_MIN_WIDTH = 160.0
PROJECT_CHAT_ROW_ALIGNMENT_TOLERANCE = 80.0
PROJECT_CHATS_LIST_CONTAINER_ROLES = {"AXScrollArea", "AXList", "AXTable", "AXOutline", "AXGroup"}
# Composer/input/transcript roles that must never sit inside a Chats-list
# container nor be an ancestor of a valid project chat row. A scroll bar is a
# normal part of a scroll-area list and is intentionally excluded here.
PROJECT_CHAT_INPUT_OR_TRANSCRIPT_ROLES = {"AXTextArea", "AXTextField", "AXWebArea"}
PROJECT_CHAT_TRANSCRIPT_IDENTIFIER_TOKENS = ("transcript", "conversation", "composer", "message-composer")
PROJECT_CHAT_NON_ROW_CONTROL_ROLES = {"AXScrollBar", "AXValueIndicator", "AXIncrementPage", "AXDecrementPage"}
MAX_VISIBLE_TITLE_CANDIDATES = 30
MAX_ACTIONABLE_ANCESTOR_DEPTH = 6
VERIFY_SETTLE_SECONDS = 1.0
ACTIONABLE_ROLES = {"AXButton", "AXMenuButton", "AXPopUpButton", "AXLink", "AXTextField"}
CONTAINER_ROLES = {"AXApplication", "AXWindow", "AXGroup", "AXScrollArea", "AXList", "AXTable", "AXOutline"}
TEXTLIKE_ROLES = {"AXStaticText", "AXTextField", "AXTextArea", "AXHeading"}
VALUE_LENGTH_ONLY_ROLES = {"AXTextArea", "AXTextField"}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
LISTLIKE_ROLES = {"AXList", "AXTable", "AXOutline", "AXCollectionList", "AXSectionList"}
LISTLIKE_SUBROLES = {"AXCollectionList", "AXSectionList"}
ALTERNATE_SIDEBAR_SURFACE_ROLES = {"AXScrollArea", "AXGroup"}
ALTERNATE_SIDEBAR_MAX_WINDOW_WIDTH_FRACTION = 0.5
ALTERNATE_SIDEBAR_MAX_WIDTH = 520.0
ALTERNATE_SIDEBAR_LEFT_EDGE_TOLERANCE = 80.0
ROWLIKE_ROLES = {"AXButton", "AXGroup", "AXStaticText", "AXHeading", "AXLink"}
SELECTION_ACTIONS = {"AXPress", "AXConfirm", "AXPick"}
MENU_ONLY_ACTIONS = {"AXShowMenu", "AXShowContextMenu", "AXCancel"}
CHAT_SECTION_LABELS = {"history", "recents", "recent", "recent chats", "chats"}
PROJECT_SECTION_LABELS = {"projects"}
GENERIC_VISIBLE_NAVIGATION_LABELS = {
    "back",
    "chatgpt",
    "gpts",
    "history",
    "library",
    "new chat",
    "new project",
    "projects",
    "recents",
    "recent",
    "recent chats",
    "search",
    "sidebar",
}
VERIFY_DESTINATION_STATUSES = {
    "verified_destination_changed",
    "destination_changed_but_identity_unverified",
    "action_performed_no_observable_change",
    "target_not_found",
    "target_ambiguous",
    "target_not_actionable",
    "accessibility_failure",
}
DEEP_INSPECTOR_ROW_DESCENDANT_MAX_DEPTH = 5
DEEP_INSPECTOR_ROW_DESCENDANT_MAX_NODES = 80
DEEP_INSPECTOR_SIBLING_MAX_NODES = 30
DEEP_INSPECTOR_RELATED_MAX_NODES = 40
DEEP_INSPECTOR_OUTPUT_CHAR_GUARD = 20_000
DEEP_INSPECTOR_ATTRIBUTE_NAMES_MAX = 80
DEEP_INSPECTOR_PARAMETERIZED_NAMES_MAX = 40
DEEP_INSPECTOR_ACTION_DESCRIPTION_MAX = 160
SELECTION_FOCUS_ATTRIBUTES = ("AXFocused", "AXSelected", "AXSelectedChildren", "AXSelectedRows")
ROW_STRUCTURE_ATTRIBUTES = ("AXRows", "AXVisibleRows", "AXSelectedRows", "AXSelectedChildren")
LINKED_UI_ATTRIBUTES = (
    "AXTitleUIElement",
    "AXServesAsTitleForUIElements",
    "AXLinkedUIElements",
    "AXOverflowButton",
    "AXShownMenuUIElement",
    "AXMenuItemPrimaryUIElement",
)
DEEP_INSPECTOR_RELEVANT_ATTRIBUTES = SELECTION_FOCUS_ATTRIBUTES + ROW_STRUCTURE_ATTRIBUTES + LINKED_UI_ATTRIBUTES
MIN_SAFE_ROW_CLICK_WIDTH = 96.0
MIN_SAFE_ROW_CLICK_HEIGHT = 18.0
SAFE_CLICK_EDGE_INSET = 8.0
SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH = 44.0
SAFE_CLICK_LEFT_FRACTION = 0.33
FRAME_MATERIAL_CHANGE_TOLERANCE = 1.0
FRAME_CONTAINMENT_TOLERANCE = 2.0
FRAME_CLICK_SETTLE_SECONDS = 1.0
FRAME_CLICK_STATUSES = {
    "verified_selection_changed",
    "destination_changed_but_identity_unverified",
    "click_performed_no_observable_change",
    "target_not_found",
    "target_ambiguous",
    "target_frame_invalid",
    "target_not_visible",
    "permission_denied",
    "accessibility_failure",
}
AUTONOMOUS_OPEN_STABILITY_SAMPLE_COUNT = 3
AUTONOMOUS_OPEN_STABILITY_POLL_SECONDS = 0.12
AUTONOMOUS_OPEN_SETTLE_SECONDS = 0.35
AUTONOMOUS_OPEN_POST_ACTION_SETTLE_SECONDS = 0.5
AUTONOMOUS_OPEN_OUTCOMES = {
    "dry_run_ready",
    "destination_opened_via_axpress",
    "destination_opened_via_validated_click",
    "destination_opened_and_visible_chats_resolved",
    "destination_opened_with_empty_visible_chat_list",
    "project_opened_but_visible_chats_not_resolved",
    "project_chat_list_identity_not_confirmed",
    "action_posted_but_destination_not_confirmed",
    "target_absent",
    "target_ambiguous",
    "target_offscreen",
    "unstable_chatgpt_ui",
    "activation_failed",
    "safe_click_point_unavailable",
    "calculated_point_hit_test_mismatch",
    "click_posting_failed",
    "post_action_inspection_unavailable",
}
PROJECT_VISIBLE_CHAT_INSPECTION_OUTCOMES = {
    "visible_chats_found",
    "chatgpt_not_running",
    "accessibility_not_trusted",
    "project_not_open",
    "project_identity_ambiguous",
    "project_open_but_chats_tab_not_confirmed",
    "project_chat_list_not_found",
    "project_chat_list_identity_not_confirmed",
    "visible_chat_rows_not_found",
    "inspection_unavailable",
}
PROJECT_CHAT_ROW_DIAGNOSTIC_OUTCOMES = {
    "diagnostic_ready",
    "chatgpt_not_running",
    "accessibility_not_trusted",
    "project_chat_list_identity_not_confirmed",
    "inspection_unavailable",
}
PROJECT_CHAT_ROW_AX_AUDIT_OUTCOMES = {
    "row_audit_ready",
    "chatgpt_not_running",
    "accessibility_not_trusted",
    "project_not_open",
    "project_open_but_chats_tab_not_confirmed",
    "project_chat_list_not_found",
    "visible_chat_rows_not_found",
    "audit_row_not_found",
    "audit_row_ambiguous",
    "inspection_unavailable",
}
PROJECT_CHAT_OPEN_OUTCOMES = {
    "dry_run_ready",
    "project_open_failed",
    "project_opened_but_chats_not_available",
    "project_chat_list_identity_not_confirmed",
    "chat_not_currently_visible",
    "chat_not_found_in_project",
    "chat_not_currently_visible_and_scroll_unavailable",
    "chat_title_not_unambiguously_representable_by_accessibility",
    "chat_title_ambiguous",
    "chat_row_not_interactable",
    "chat_list_scroll_target_not_found",
    "chat_list_scroll_failed",
    "chat_list_scroll_no_progress",
    "chat_list_scan_continuity_not_confirmed",
    "chat_list_end_reached_without_match",
    "chat_search_budget_exhausted_without_confirmed_end",
    "chat_search_time_budget_exhausted_while_list_progressing",
    "chat_opened_via_axpress",
    "chat_opened_via_validated_click",
    "chat_opened_after_scrolling_via_axpress",
    "chat_opened_after_scrolling_via_validated_click",
    "target_detected_but_not_stably_re_resolved",
    "target_alignment_not_supported",
    "target_alignment_action_post_failed",
    "target_alignment_posted_but_target_not_fully_visible",
    "target_alignment_posted_but_target_not_re_resolved",
    "action_posted_but_chat_not_confirmed",
    "safe_click_point_unavailable",
    "calculated_point_hit_test_mismatch",
    "click_posting_failed",
    "post_action_inspection_unavailable",
}
MAX_PROJECT_CHAT_SEARCH_CYCLES = 60
MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS = 90.0
PROJECT_CHAT_FINAL_RE_RESOLUTION_MAX_RETRIES = 2
PROJECT_CHAT_FINAL_RE_RESOLUTION_RETRY_DELAY_SECONDS = 1.0
INITIAL_PROJECT_CHAT_HYDRATION_TIMEOUT_SECONDS = 2.0
POST_SCROLL_HYDRATION_TIMEOUT_SECONDS = 4.0
HYDRATION_SAMPLE_INTERVAL_SECONDS = 0.2
REQUIRED_STABLE_SAMPLES_AFTER_CHANGE = 3
MAX_CHAT_SEARCH_CYCLES = MAX_PROJECT_CHAT_SEARCH_CYCLES
INITIAL_LIST_HYDRATION_TIMEOUT_SECONDS = INITIAL_PROJECT_CHAT_HYDRATION_TIMEOUT_SECONDS
REQUIRED_STABLE_SAMPLES = REQUIRED_STABLE_SAMPLES_AFTER_CHANGE
NO_PROGRESS_CYCLE_THRESHOLD = 2
PROJECT_CHAT_SCROLL_MAX_ITERATIONS = MAX_PROJECT_CHAT_SEARCH_CYCLES
PROJECT_CHAT_SCROLL_NO_PROGRESS_LIMIT = NO_PROGRESS_CYCLE_THRESHOLD
PROJECT_CHAT_SCROLL_ACTIONS = ("AXScrollDown",)
# Dynamic, overlap-safe CoreGraphics scroll quantum. The CoreGraphics fallback
# no longer uses a fixed coarse pixel delta; the forward delta is derived per
# resolved viewport from the median visible row height so adjacent settled
# viewports retain meaningful shared rows.
PROJECT_CHAT_SCROLL_MAX_ROW_HEIGHTS_PER_PULSE = 0.75
PROJECT_CHAT_SCROLL_MIN_PIXEL_DELTA = 24
PROJECT_CHAT_SCROLL_MAX_PIXEL_DELTA = 180
PROJECT_CHAT_REQUIRED_OVERLAP_ROWS = 2
PROJECT_CHAT_MAX_RECOVERY_PULSES_PER_CYCLE = 2
PROJECT_CHAT_SCROLL_QUANTUM_REDUCTION_FACTOR = 0.5
PROJECT_CHAT_END_ANCHOR_CYCLES_REQUIRED = 2
COORDINATE_MAPPING_TOLERANCE_PX = 3.0
COORDINATE_MAPPING_CLASSIFICATIONS = {
    "ax_frames_are_global",
    "target_frame_needs_chatgpt_window_translation",
    "target_frame_needs_windowserver_translation",
    "target_frame_needs_ancestor_translation",
    "scaled_coordinate_mapping_suspected",
    "vertical_inversion_suspected",
    "target_hit_test_matches_but_mapping_unresolved",
    "cursor_not_over_requested_target",
    "target_or_window_frame_unavailable",
    "ambiguous_coordinate_mapping",
}
CALIBRATION_CLICK_FINAL_CLASSIFICATIONS = {
    "click_confirmed_mapping_success",
    "click_posted_but_destination_not_confirmed",
    "click_posted_but_mapping_remains_ambiguous",
    "destination_not_resolved_before_click",
    "safe_click_point_unavailable",
    "click_posting_failed",
    "post_click_inspection_unavailable",
}
CALIBRATION_CONFIRMED_CLICK_COUNT = 2
CALIBRATION_INTER_CLICK_DELAY_SECONDS = 0.5
CALIBRATION_POST_CLICK_SETTLE_SECONDS = 0.5
CLICK_TRANSFORMS = {
    "ax_frames_are_global": "raw",
    "target_frame_needs_chatgpt_window_translation": "ax_window_translation",
    "target_frame_needs_windowserver_translation": "windowserver_translation",
    "target_frame_needs_ancestor_translation": "ancestor_translation",
}
TITLE_INVENTORY_CATEGORY_KEYS = (
    "visible_chat_title_candidates",
    "visible_project_title_candidates",
    "visible_navigation_section_labels",
    "visible_search_result_candidates",
    "actionable_parent_candidates",
)
TITLE_DISCLOSURE_NOTICE = "Visible navigation-title disclosure enabled by explicit local user request."
SAFE_GENERIC_LABELS = {
    "back": "Back",
    "chatgpt": "ChatGPT",
    "history": "History",
    "library": "Library",
    "new chat": "New chat",
    "projects": "Projects",
    "search": "Search",
    "sidebar": "Sidebar",
}
SAFE_GENERIC_LABEL_ALIASES = {
    "chats": "History",
    "close sidebar": "Sidebar",
    "new chat button": "New chat",
    "open sidebar": "Sidebar",
    "search chats": "Search",
    "search chatgpt": "Search",
}


@dataclass(frozen=True)
class ProcessResolution:
    pid: int | None
    method: str
    error: str | None = None


@dataclass(frozen=True)
class AXElementSnapshot:
    path: str
    depth: int
    role: str = ""
    subrole: str = ""
    identifier: str = ""
    title: str = ""
    description: str = ""
    value: str = ""
    enabled: bool | None = None
    focused: bool | None = None
    actions: tuple[str, ...] = ()
    selected: bool | None = None
    attribute_names: tuple[str, ...] = ()
    parameterized_attribute_names: tuple[str, ...] = ()
    settable_attribute_names: tuple[str, ...] = ()
    action_descriptions: tuple[tuple[str, str], ...] = ()
    linked_element_paths: tuple[tuple[str, str], ...] = ()
    row_paths: tuple[str, ...] = ()
    visible_row_paths: tuple[str, ...] = ()
    selected_row_paths: tuple[str, ...] = ()
    selected_child_paths: tuple[str, ...] = ()
    direct_child_count: int | None = None
    visible_child_count: int | None = None
    frame: tuple[float, float, float, float] | None = None
    native_id: int | None = None


class AXDiagnosticError(RuntimeError):
    pass


class _CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class _CGSize(Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


class _CGRect(Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


class _TreeAdapter(Protocol):
    def snapshot(self, element: object, path: str, depth: int) -> AXElementSnapshot:
        ...

    def children(self, element: object) -> list[object]:
        ...


def inspect_chatgpt_navigation_ui(
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    *,
    include_visible_navigation_titles: bool = False,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
) -> dict:
    result = _base_result(app_name, max_depth, max_nodes, include_visible_navigation_titles)
    if max_depth < 0 or max_nodes <= 0:
        result.update(
            {
                "reason_code": "invalid_limits",
                "error": "max_depth must be >= 0 and max_nodes must be > 0.",
            }
        )
        return result

    if sys.platform != "darwin":
        result.update(
            {
                "reason_code": "unsupported_platform",
                "error": "ChatGPT navigation UI inspection is only supported on macOS.",
            }
        )
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update(
            {
                "reason_code": "process_resolution_failed",
                "error": str(exc),
                "process_resolution_method": PROCESS_RESOLUTION_METHOD,
            }
        )
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update(
            {
                "reason_code": "process_not_found",
                "error": process.error or f"No running application named {app_name!r} was found.",
            }
        )
        return result

    factory = reader_factory or _ReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        snapshots, stats, window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update(
            {
                "reason_code": "ax_read_failed",
                "error": str(exc),
                "pid_present": True,
            }
        )
        return result

    classified = classify_navigation_snapshots(
        snapshots,
        stats,
        window_metadata,
        include_visible_navigation_titles=include_visible_navigation_titles,
    )
    result.update(classified)
    result.update(
        {
            "ok": bool(classified.get("window_available")),
            "reason_code": "inspection_completed" if classified.get("window_available") else "window_unavailable",
            "error": None if classified.get("window_available") else "No focused or visible ChatGPT window was available.",
            "pid_present": True,
        }
    )
    return result


def resolve_chatgpt_process(app_name: str = "ChatGPT") -> ProcessResolution:
    if sys.platform != "darwin":
        return ProcessResolution(
            pid=None,
            method=PROCESS_RESOLUTION_METHOD,
            error="Unsupported platform.",
        )
    return _resolve_process_with_nsworkspace(app_name)


def classify_navigation_snapshots(
    snapshots: list[AXElementSnapshot],
    traversal_stats: dict | None = None,
    window_metadata: dict | None = None,
    *,
    include_visible_navigation_titles: bool = False,
) -> dict:
    stats = dict(traversal_stats or {})
    role_counts = Counter(snapshot.role or "" for snapshot in snapshots)
    subrole_counts = Counter(snapshot.subrole or "" for snapshot in snapshots if snapshot.subrole)
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    generic_controls = _generic_controls(snapshots)
    navigation_candidates = _navigation_candidates(snapshots, snapshots_by_path)
    search_candidates = _search_candidates(snapshots, snapshots_by_path)
    sidebar_candidates = _sidebar_candidates(snapshots, snapshots_by_path)
    chat_history_candidates = _chat_history_candidates(snapshots, snapshots_by_path)
    project_candidates = _project_candidates(snapshots, snapshots_by_path)
    current_chat_identity_candidates = _current_chat_identity_candidates(snapshots, snapshots_by_path)
    ambiguous = _ambiguous_controls(
        snapshots,
        navigation_candidates,
        search_candidates,
        project_candidates,
        sidebar_candidates,
        chat_history_candidates,
        current_chat_identity_candidates,
        snapshots_by_path,
    )
    categories = {
        "current_chat_identity_candidates": current_chat_identity_candidates,
        "chat_history_candidates": chat_history_candidates,
        "project_candidates": project_candidates,
        "search_candidates": search_candidates,
        "sidebar_candidates": sidebar_candidates,
        "navigation_candidates": navigation_candidates,
        "ambiguous_navigation_relevant_controls": ambiguous,
    }
    capped_categories = {name: _sort_and_cap_candidates(items) for name, items in categories.items()}
    category_limits = {
        name: {
            "total": len(categories[name]),
            "emitted": len(capped_categories[name]),
            "omitted": max(0, len(categories[name]) - len(capped_categories[name])),
            "cap": MAX_CANDIDATES_PER_CATEGORY,
        }
        for name in categories
    }

    result = {
        "window_available": bool(snapshots),
        "window_metadata": _sanitize_window_metadata(window_metadata or {}),
        "traversal": {
            "visited_nodes": stats.get("visited_nodes", len(snapshots)),
            "emitted_nodes": len(snapshots),
            "max_depth": stats.get("max_depth"),
            "max_nodes": stats.get("max_nodes"),
            "truncated_by_node_limit": bool(stats.get("truncated_by_node_limit")),
            "truncated_by_depth_limit": bool(stats.get("truncated_by_depth_limit")),
        },
        "role_counts": dict(role_counts),
        "subrole_counts": dict(subrole_counts),
        "generic_controls_observed": generic_controls,
        "navigation_candidates": capped_categories["navigation_candidates"],
        "sidebar_candidates": capped_categories["sidebar_candidates"],
        "chat_history_candidates": capped_categories["chat_history_candidates"],
        "project_candidates": capped_categories["project_candidates"],
        "search_candidates": capped_categories["search_candidates"],
        "current_chat_identity_candidates": capped_categories["current_chat_identity_candidates"],
        "ambiguous_navigation_relevant_controls": capped_categories["ambiguous_navigation_relevant_controls"],
        "ambiguous_unclassified_controls": capped_categories["ambiguous_navigation_relevant_controls"],
        "actionable_element_summaries": [],
        "category_limits": category_limits,
        "filtering_summary": _filtering_summary(snapshots, categories),
    }
    result.update(_visible_title_inventory_result(snapshots, snapshots_by_path, include_visible_navigation_titles))
    return result


def _base_result(
    app_name: str,
    max_depth: int,
    max_nodes: int,
    include_visible_navigation_titles: bool = False,
) -> dict:
    category_names = (
        "current_chat_identity_candidates",
        "chat_history_candidates",
        "project_candidates",
        "search_candidates",
        "sidebar_candidates",
        "navigation_candidates",
        "ambiguous_navigation_relevant_controls",
    )
    result = {
        "ok": False,
        "reason_code": "not_run",
        "error": None,
        "app_name": app_name,
        "method": AX_READ_METHOD,
        "process_resolution_method": None,
        "pid_present": False,
        "window_available": False,
        "traversal": {
            "visited_nodes": 0,
            "emitted_nodes": 0,
            "max_depth": max_depth,
            "max_nodes": max_nodes,
            "truncated_by_node_limit": False,
            "truncated_by_depth_limit": False,
        },
        "role_counts": {},
        "subrole_counts": {},
        "generic_controls_observed": [],
        "navigation_candidates": [],
        "sidebar_candidates": [],
        "chat_history_candidates": [],
        "project_candidates": [],
        "search_candidates": [],
        "current_chat_identity_candidates": [],
        "actionable_element_summaries": [],
        "ambiguous_unclassified_controls": [],
        "ambiguous_navigation_relevant_controls": [],
        "category_limits": {
            name: {"total": 0, "emitted": 0, "omitted": 0, "cap": MAX_CANDIDATES_PER_CATEGORY}
            for name in category_names
        },
        "filtering_summary": {
            "total_nodes_observed": 0,
            "candidate_nodes_before_caps": 0,
            "excluded_non_candidate_nodes": 0,
            "long_text_fields_redacted": 0,
            "redacted_text_fields": 0,
            "max_candidates_per_category": MAX_CANDIDATES_PER_CATEGORY,
            "long_text_redaction_threshold": LONG_TEXT_REDACTION_THRESHOLD,
            "long_text_redaction_samples": [],
        },
        "privacy_redaction_policy_version": PRIVACY_POLICY_VERSION,
        "read_only": True,
        "actions_performed": [],
    }
    result.update(_empty_visible_title_inventory(include_visible_navigation_titles))
    return result


def _collect_tree(
    root: object | None,
    adapter: _TreeAdapter,
    *,
    max_depth: int,
    max_nodes: int,
    root_path: str = "W",
) -> tuple[list[AXElementSnapshot], dict]:
    snapshots: list[AXElementSnapshot] = []
    visited_nodes = 0
    truncated_by_node_limit = False
    truncated_by_depth_limit = False

    def walk(element: object, path: str, depth: int) -> None:
        nonlocal visited_nodes, truncated_by_node_limit, truncated_by_depth_limit
        if visited_nodes >= max_nodes:
            truncated_by_node_limit = True
            return
        visited_nodes += 1
        snapshots.append(adapter.snapshot(element, path, depth))
        if depth >= max_depth:
            if adapter.children(element):
                truncated_by_depth_limit = True
            return
        for index, child in enumerate(adapter.children(element), start=1):
            if visited_nodes >= max_nodes:
                truncated_by_node_limit = True
                break
            walk(child, f"{path}.{index}", depth + 1)

    if root is not None:
        walk(root, root_path, 0)

    return snapshots, {
        "visited_nodes": visited_nodes,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "truncated_by_node_limit": truncated_by_node_limit,
        "truncated_by_depth_limit": truncated_by_depth_limit,
    }


def _normalized_label(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _label_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_literal(value: str) -> str | None:
    normalized = _normalized_label(value)
    lowered = normalized.lower()
    if lowered in SAFE_GENERIC_LABELS:
        return SAFE_GENERIC_LABELS[lowered]
    if lowered in SAFE_GENERIC_LABEL_ALIASES:
        return SAFE_GENERIC_LABEL_ALIASES[lowered]
    return None


def _label_report(value: str, classification: str = "unknown") -> dict:
    normalized = _normalized_label(value)
    if not normalized:
        return {
            "literal": "",
            "classification": "unknown",
            "normalized_length": 0,
            "sha256": "",
            "redacted": False,
        }
    literal = _safe_literal(normalized)
    if literal is not None:
        return {
            "literal": literal,
            "classification": "generic_control_label",
            "normalized_length": len(normalized),
            "sha256": _label_digest(normalized),
            "redacted": False,
        }
    return {
        "literal": None,
        "classification": classification,
        "normalized_length": len(normalized),
        "sha256": _label_digest(normalized),
        "redacted": True,
    }


def _label_classification(snapshot: AXElementSnapshot) -> str:
    text = _raw_text(snapshot).lower()
    if "project" in text:
        return "possible_project_label"
    if "chat" in text or "conversation" in text:
        return "possible_chat_label"
    if snapshot.role in ACTIONABLE_ROLES:
        return "possible_navigation_label"
    return "unknown"


def _raw_text(snapshot: AXElementSnapshot) -> str:
    return " ".join(
        part
        for part in (
            snapshot.identifier,
            snapshot.title,
            snapshot.description,
            "" if snapshot.role in VALUE_LENGTH_ONLY_ROLES else snapshot.value,
        )
        if part
    )


def _safe_search_text(snapshot: AXElementSnapshot) -> str:
    return _navigation_metadata_text(snapshot).lower()


def _navigation_metadata_text(snapshot: AXElementSnapshot) -> str:
    values = [snapshot.identifier, snapshot.title, snapshot.description]
    value = _normalized_label(snapshot.value)
    if value and (_safe_literal(value) or (snapshot.role not in TEXTLIKE_ROLES and len(value) <= LONG_TEXT_REDACTION_THRESHOLD)):
        values.append(value)
    return " ".join(_normalized_label(value) for value in values if value)


def _bounded_text(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit]


def _sanitized_element(snapshot: AXElementSnapshot) -> dict:
    classification = _label_classification(snapshot)
    data = {
        "path": snapshot.path,
        "depth": snapshot.depth,
        "role": snapshot.role,
        "subrole": snapshot.subrole,
        "identifier": _label_report(snapshot.identifier, classification),
        "title": _label_report(snapshot.title, classification),
        "description": _label_report(snapshot.description, classification),
        "enabled": snapshot.enabled,
        "focused": snapshot.focused,
        "actions": list(snapshot.actions),
        "value_length": len(snapshot.value or ""),
    }
    if snapshot.role not in VALUE_LENGTH_ONLY_ROLES and _safe_literal(snapshot.value):
        data["value"] = _label_report(snapshot.value, classification)
    elif snapshot.value:
        data["value"] = {
            "literal": None,
            "classification": classification,
            "normalized_length": len(_normalized_label(snapshot.value)),
            "sha256": _label_digest(_normalized_label(snapshot.value)),
            "redacted": True,
        }
    else:
        data["value"] = _label_report("", classification)
    return data


def _candidate(
    snapshot: AXElementSnapshot,
    category: str,
    confidence: str,
    evidence: list[str],
    snapshots_by_path: dict[str, AXElementSnapshot],
    *,
    relationship: dict | None = None,
) -> dict:
    actionable = snapshot.role in ACTIONABLE_ROLES or bool(snapshot.actions)
    return {
        "category": category,
        "confidence": confidence,
        "evidence_codes": sorted(set(evidence)),
        "path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
        "role": _bounded_text(snapshot.role, MAX_ROLE_LENGTH),
        "subrole": _bounded_text(snapshot.subrole, MAX_ROLE_LENGTH),
        "enabled": snapshot.enabled,
        "focused": snapshot.focused,
        "actions": _safe_actions(snapshot.actions),
        "label": _candidate_label(snapshot),
        "relationship": relationship or _relationship(snapshot, snapshots_by_path),
        "appears_actionable": actionable,
        "observation_safety": "observation_only",
        "future_explicit_approval_relevance": _future_relevance(category, confidence),
    }


def _candidate_label(snapshot: AXElementSnapshot) -> dict:
    classification = _label_classification(snapshot)
    for source_name, value in (
        ("identifier", snapshot.identifier),
        ("title", snapshot.title),
        ("description", snapshot.description),
        ("value", snapshot.value),
    ):
        literal = _safe_literal(value)
        if literal is not None:
            report = _label_report(value, classification)
            report["source"] = source_name
            return report
    for source_name, value in (
        ("identifier", snapshot.identifier),
        ("title", snapshot.title),
        ("description", snapshot.description),
        ("value", snapshot.value),
    ):
        if _normalized_label(value):
            report = _label_report(value, classification)
            report["source"] = source_name
            return report
    report = _label_report("", classification)
    report["source"] = "none"
    return report


def _safe_actions(actions: tuple[str, ...]) -> list[str]:
    bounded = sorted({_bounded_text(action, MAX_ACTION_NAME_LENGTH) for action in actions if action})
    return bounded[:MAX_ACTIONS_PER_ELEMENT]


def _future_relevance(category: str, confidence: str) -> str:
    if confidence == "high" or category in {
        "possible_chat_history_row",
        "possible_chat_history_container",
        "possible_project_row",
        "possible_project_container",
    }:
        return "potentially_relevant_with_future_explicit_approval"
    return "observe_only_until_more_evidence"


def _relationship(snapshot: AXElementSnapshot, snapshots_by_path: dict[str, AXElementSnapshot]) -> dict:
    parent_path = _parent_path(snapshot.path)
    parent = snapshots_by_path.get(parent_path or "")
    nearest_container = _nearest_ancestor(snapshot.path, snapshots_by_path, CONTAINER_ROLES)
    nearest_list = _nearest_ancestor(snapshot.path, snapshots_by_path, {"AXList", "AXTable", "AXOutline"})
    return {
        "parent_path": _bounded_text(parent_path or "", MAX_PATH_LENGTH),
        "parent_role": _bounded_text(parent.role if parent else "", MAX_ROLE_LENGTH),
        "container_path": _bounded_text(nearest_container.path if nearest_container else "", MAX_PATH_LENGTH),
        "container_role": _bounded_text(nearest_container.role if nearest_container else "", MAX_ROLE_LENGTH),
        "list_path": _bounded_text(nearest_list.path if nearest_list else "", MAX_PATH_LENGTH),
        "list_role": _bounded_text(nearest_list.role if nearest_list else "", MAX_ROLE_LENGTH),
    }


def _parent_path(path: str) -> str | None:
    if "." not in path:
        return None
    return path.rsplit(".", 1)[0]


def _ancestor_paths(path: str) -> list[str]:
    paths = []
    current = _parent_path(path)
    while current:
        paths.append(current)
        current = _parent_path(current)
    return paths


def _nearest_ancestor(
    path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
    roles: set[str],
) -> AXElementSnapshot | None:
    for ancestor_path in _ancestor_paths(path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot and snapshot.role in roles:
            return snapshot
    return None


def _generic_controls(snapshots: list[AXElementSnapshot]) -> list[dict]:
    observed: dict[str, dict] = {}
    for snapshot in snapshots:
        for source, value in (
            ("identifier", snapshot.identifier),
            ("title", snapshot.title),
            ("description", snapshot.description),
            ("value", snapshot.value),
        ):
            literal = _safe_literal(value)
            if literal is None:
                continue
            observed.setdefault(
                literal,
                {
                    "label": literal,
                    "paths": [],
                    "sources": [],
                },
            )
            if len(observed[literal]["paths"]) < MAX_CANDIDATES_PER_CATEGORY:
                observed[literal]["paths"].append(_bounded_text(snapshot.path, MAX_PATH_LENGTH))
            observed[literal]["sources"].append(source)
    return sorted(observed.values(), key=lambda item: item["label"])[:MAX_GENERIC_CONTROLS]


def _navigation_candidates(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[dict]:
    candidates = []
    for snapshot in snapshots:
        text = _safe_search_text(snapshot)
        literal_values = {
            literal
            for value in (snapshot.identifier, snapshot.title, snapshot.description, snapshot.value)
            if (literal := _safe_literal(value))
        }
        evidence = []
        for literal in sorted(literal_values):
            if literal in {"Back", "History", "Library", "New chat", "Projects", "Sidebar"}:
                evidence.append(f"generic_label:{literal.lower().replace(' ', '_')}")
        if any(token in text for token in ("sidebar", "navigation")):
            evidence.append("metadata_contains_navigation_token")
        if evidence:
            confidence = "high" if snapshot.role in ACTIONABLE_ROLES or snapshot.role in CONTAINER_ROLES else "medium"
            candidates.append(_candidate(snapshot, "possible_navigation_control", confidence, evidence, snapshots_by_path))
    return candidates


def _search_candidates(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[dict]:
    candidates = []
    for snapshot in snapshots:
        text = _safe_search_text(snapshot)
        evidence = []
        if "search" in text:
            evidence.append("metadata_contains_search")
        if snapshot.role == "AXTextField" and "search" in text:
            evidence.append("search_text_field")
        if snapshot.subrole == "AXSearchField":
            evidence.append("search_field_subrole")
        safe_search_label = any(
            _safe_literal(value) == "Search"
            for value in (snapshot.identifier, snapshot.title, snapshot.description, snapshot.value)
        )
        if evidence and (snapshot.role == "AXTextField" or snapshot.subrole == "AXSearchField" or safe_search_label):
            confidence = "high" if snapshot.role == "AXTextField" or snapshot.subrole == "AXSearchField" else "medium"
            candidates.append(_candidate(snapshot, "possible_search_control", confidence, evidence, snapshots_by_path))
    return candidates


def _sidebar_candidates(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot] | None = None,
) -> list[dict]:
    snapshots_by_path = snapshots_by_path or {snapshot.path: snapshot for snapshot in snapshots}
    candidates = []
    for snapshot in snapshots:
        text = _safe_search_text(snapshot)
        if "sidebar" not in text:
            continue
        candidates.append(
            _candidate(
                snapshot,
                "possible_sidebar_surface",
                "medium" if snapshot.role in CONTAINER_ROLES else "low",
                ["metadata_contains_sidebar"],
                snapshots_by_path,
            )
        )
    return candidates


def _chat_history_candidates(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[dict]:
    candidates = []
    container_paths: set[str] = set()
    for snapshot in snapshots:
        text = _safe_search_text(snapshot)
        has_history_label = any(
            _safe_literal(value) == "History"
            for value in (snapshot.identifier, snapshot.title, snapshot.description, snapshot.value)
        )
        if not has_history_label and not any(token in text for token in ("history", "conversation")):
            continue
        if snapshot.role not in CONTAINER_ROLES:
            continue
        container_paths.add(snapshot.path)
        candidates.append(
            _candidate(
                snapshot,
                "possible_chat_history_container",
                "medium",
                ["history_container_metadata"],
                snapshots_by_path,
            )
        )
    for snapshot in snapshots:
        if snapshot.role not in ACTIONABLE_ROLES and snapshot.role not in {"AXGroup", "AXStaticText"}:
            continue
        ancestor = next((path for path in _ancestor_paths(snapshot.path) if path in container_paths), None)
        if not ancestor or snapshot.path == ancestor:
            continue
        candidates.append(
            _candidate(
                snapshot,
                "possible_chat_history_row",
                "low" if snapshot.role == "AXStaticText" else "medium",
                ["descendant_of_history_container"],
                snapshots_by_path,
                relationship={**_relationship(snapshot, snapshots_by_path), "matched_container_path": ancestor},
            )
        )
    return candidates


def _project_candidates(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[dict]:
    candidates = []
    container_paths: set[str] = set()
    for snapshot in snapshots:
        text = _safe_search_text(snapshot)
        has_projects_label = any(
            _safe_literal(value) == "Projects"
            for value in (snapshot.identifier, snapshot.title, snapshot.description, snapshot.value)
        )
        if not has_projects_label and "project" not in text:
            continue
        if snapshot.role not in CONTAINER_ROLES:
            continue
        container_paths.add(snapshot.path)
        candidates.append(
            _candidate(
                snapshot,
                "possible_project_container",
                "medium",
                ["project_container_metadata"],
                snapshots_by_path,
            )
        )
    for snapshot in snapshots:
        if snapshot.role not in ACTIONABLE_ROLES and snapshot.role not in {"AXGroup", "AXStaticText"}:
            continue
        ancestor = next((path for path in _ancestor_paths(snapshot.path) if path in container_paths), None)
        if not ancestor or snapshot.path == ancestor:
            continue
        candidates.append(
            _candidate(
                snapshot,
                "possible_project_row",
                "low" if snapshot.role == "AXStaticText" else "medium",
                ["descendant_of_project_container"],
                snapshots_by_path,
                relationship={**_relationship(snapshot, snapshots_by_path), "matched_container_path": ancestor},
            )
        )
    return candidates


def _current_chat_identity_candidates(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[dict]:
    candidates = []
    for snapshot in snapshots:
        if snapshot.role not in {"AXHeading", "AXStaticText"}:
            continue
        text = _normalized_label(snapshot.title or snapshot.description or snapshot.value)
        if not text or _safe_literal(text):
            continue
        if snapshot.depth > 5 or len(text) > LONG_TEXT_REDACTION_THRESHOLD:
            continue
        candidates.append(
            _candidate(
                snapshot,
                "possible_current_chat_identity",
                "low",
                ["top_level_non_generic_heading_or_static_text"],
                snapshots_by_path,
            )
        )
    return candidates


def _ambiguous_controls(
    snapshots: list[AXElementSnapshot],
    *candidate_buckets: list[dict] | dict[str, AXElementSnapshot],
) -> list[dict]:
    snapshots_by_path = candidate_buckets[-1]
    assert isinstance(snapshots_by_path, dict)
    buckets = candidate_buckets[:-1]
    classified_paths = {
        item["path"]
        for bucket in buckets
        for item in bucket
    }
    ambiguous = []
    for snapshot in snapshots:
        if snapshot.path in classified_paths:
            continue
        if snapshot.role not in ACTIONABLE_ROLES and not snapshot.actions:
            continue
        safe_labels = {
            literal
            for value in (snapshot.identifier, snapshot.title, snapshot.description, snapshot.value)
            if (literal := _safe_literal(value))
        }
        if safe_labels.isdisjoint({"Back", "History", "Library", "New chat", "Projects", "Search", "Sidebar"}):
            continue
        ambiguous.append(
            _candidate(
                snapshot,
                "ambiguous_navigation_relevant_control",
                "low",
                ["safe_generic_navigation_label_but_weak_structure"],
                snapshots_by_path,
            )
        )
    return ambiguous


def _sort_and_cap_candidates(candidates: list[dict]) -> list[dict]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            CONFIDENCE_ORDER.get(str(item.get("confidence")), 99),
            _path_sort_key(str(item.get("path") or "")),
        ),
    )
    return ordered[:MAX_CANDIDATES_PER_CATEGORY]


def _path_sort_key(path: str) -> tuple:
    parts: list[tuple[int, object]] = []
    for part in (path or "").split("."):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


def _empty_visible_title_inventory(include_titles: bool) -> dict:
    return {
        "visible_navigation_title_disclosure_enabled": bool(include_titles),
        "visible_navigation_title_disclosure_notice": TITLE_DISCLOSURE_NOTICE if include_titles else "",
        "visible_chat_title_candidates": [],
        "visible_project_title_candidates": [],
        "visible_navigation_section_labels": [],
        "visible_search_result_candidates": [],
        "actionable_parent_candidates": [],
        "visible_title_category_limits": {
            key: {"total": 0, "emitted": 0, "omitted": 0, "cap": MAX_VISIBLE_TITLE_CANDIDATES}
            for key in TITLE_INVENTORY_CATEGORY_KEYS
        },
    }


def _visible_title_inventory_result(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    include_titles: bool,
) -> dict:
    result = _empty_visible_title_inventory(include_titles)
    if not include_titles:
        return result

    categories: dict[str, list[dict]] = {
        "visible_chat_title_candidates": [],
        "visible_project_title_candidates": [],
        "visible_navigation_section_labels": [],
        "visible_search_result_candidates": [],
        "actionable_parent_candidates": [],
    }
    actionable_parent_paths: set[str] = set()
    sections = _visible_navigation_sections(snapshots, snapshots_by_path)
    sections_by_path = {section["heading_path"]: section for section in sections}

    for snapshot in snapshots:
        title = _visible_title_source(snapshot)
        if title is None:
            continue
        section = sections_by_path.get(snapshot.path)
        context = _section_context(section) if section else _visible_title_section_context(snapshot, snapshots_by_path, sections)
        classification = _visible_title_classification(snapshot, title["text"], context, bool(section))
        if classification is None:
            continue
        if classification not in {
            "visible_chat_title_candidate",
            "visible_project_title_candidate",
            "visible_navigation_section_label",
            "visible_search_result_candidate",
        }:
            continue
        if classification != "visible_navigation_section_label" and _is_generic_visible_navigation_title(title["text"]):
            continue

        candidate = _visible_title_candidate(snapshot, title, classification, context, snapshots_by_path)
        bucket = _title_bucket_for_classification(classification)
        categories[bucket].append(candidate)
        parent = candidate["nearest_actionable_ancestor"]
        if parent["path"] and parent["path"] not in actionable_parent_paths:
            actionable_parent_paths.add(parent["path"])
            categories["actionable_parent_candidates"].append(
                _actionable_parent_candidate(parent, candidate, snapshots_by_path)
            )

    capped = {key: _sort_and_cap_title_candidates(items) for key, items in categories.items()}
    result.update(capped)
    result["visible_title_category_limits"] = {
        key: {
            "total": len(categories[key]),
            "emitted": len(capped[key]),
            "omitted": max(0, len(categories[key]) - len(capped[key])),
            "cap": MAX_VISIBLE_TITLE_CANDIDATES,
        }
        for key in TITLE_INVENTORY_CATEGORY_KEYS
    }
    return result


def _is_generic_visible_navigation_title(text: str) -> bool:
    normalized = _normalized_label(text).lower()
    return normalized in GENERIC_VISIBLE_NAVIGATION_LABELS or _safe_literal(text) is not None


def _visible_title_source(snapshot: AXElementSnapshot) -> dict | None:
    if snapshot.role in {"AXTextArea", "AXTextField"}:
        return None
    for source, value in (
        ("title", snapshot.title),
        ("description", snapshot.description),
        ("value", snapshot.value),
        ("identifier", snapshot.identifier),
    ):
        normalized = _normalized_label(value)
        if not normalized:
            continue
        if not 1 <= len(normalized) <= VISIBLE_NAVIGATION_TITLE_MAX_LENGTH:
            continue
        if _looks_like_message_or_code(normalized):
            continue
        return {"source": source, "text": normalized}
    return None


def _looks_like_message_or_code(text: str) -> bool:
    lowered = text.lower()
    if "\n" in text or "\r" in text:
        return True
    if "```" in text or "function " in lowered or "class " in lowered:
        return True
    if lowered.startswith(("you:", "assistant:", "user:", "system:")):
        return True
    if any(token in lowered for token in ("composer", "message body", "draft message")):
        return True
    return False


def _is_listlike(snapshot: AXElementSnapshot) -> bool:
    return snapshot.role in LISTLIKE_ROLES or snapshot.subrole in LISTLIKE_SUBROLES


def _recognized_navigation_list_context(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict | None:
    list_snapshot = snapshot if _is_listlike(snapshot) else _nearest_listlike_ancestor(snapshot.path, snapshots_by_path)
    if list_snapshot is None:
        return None
    purpose = _list_purpose(list_snapshot)
    if purpose is None:
        purpose = _list_purpose_from_nearby_labels(list_snapshot, snapshots_by_path)
    if purpose is None:
        return None
    return {
        "purpose": purpose,
        "list_path": _bounded_text(list_snapshot.path, MAX_PATH_LENGTH),
        "list_role": _bounded_text(list_snapshot.role, MAX_ROLE_LENGTH),
        "list_subrole": _bounded_text(list_snapshot.subrole, MAX_ROLE_LENGTH),
    }


def _nearest_listlike_ancestor(
    path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> AXElementSnapshot | None:
    for ancestor_path in _ancestor_paths(path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot and _is_listlike(snapshot):
            return snapshot
    return None


def _list_purpose(snapshot: AXElementSnapshot) -> str | None:
    values = (snapshot.identifier, snapshot.title, snapshot.description, snapshot.value)
    safe_labels = {_safe_literal(value) for value in values}
    text = _navigation_metadata_text(snapshot).lower()
    if "History" in safe_labels or "history" in text:
        return "chat_history"
    if "Projects" in safe_labels or "project" in text:
        return "projects"
    if "search" in text:
        return "search_results"
    if "navigation" in text or "sidebar" in text:
        return "navigation"
    return None


def _list_purpose_from_nearby_labels(
    list_snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> str | None:
    parent_path = _parent_path(list_snapshot.path)
    if not parent_path:
        return None
    prefix = parent_path + "."
    for snapshot in snapshots_by_path.values():
        if not snapshot.path.startswith(prefix):
            continue
        if snapshot.path == list_snapshot.path or snapshot.path.startswith(list_snapshot.path + "."):
            continue
        label = _safe_literal(snapshot.title or snapshot.description or snapshot.value or snapshot.identifier)
        if label == "History":
            return "chat_history"
        if label == "Projects":
            return "projects"
    return None


def _visible_title_classification(
    snapshot: AXElementSnapshot,
    exact_title: str,
    context: dict | None,
    is_section_heading: bool = False,
) -> str | None:
    if context is None:
        return None
    if (
        is_section_heading
        and context["purpose"] != "search_results"
        and snapshot.role in {"AXHeading", "AXStaticText", "AXList", "AXGroup"}
    ):
        return "visible_navigation_section_label"
    if _is_listlike(snapshot):
        return None
    if _is_generic_visible_navigation_title(exact_title):
        return None
    purpose = context["purpose"]
    if purpose == "chat_history":
        return "visible_chat_title_candidate"
    if purpose == "projects":
        return "visible_project_title_candidate"
    if purpose == "search_results":
        return "visible_search_result_candidate"
    return None


def _visible_navigation_sections(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[dict]:
    sections = []
    for snapshot in snapshots:
        title = _visible_title_source(snapshot)
        if title is None:
            continue
        purpose = _section_purpose_from_title(title["text"])
        if purpose is None:
            continue
        container_path = _section_container_path(snapshot, snapshots_by_path)
        if not container_path:
            continue
        row_path = _row_path_under_container(snapshot.path, container_path)
        sections.append(
            {
                "heading_path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
                "heading_title": title["text"],
                "purpose": purpose,
                "container_path": _bounded_text(container_path, MAX_PATH_LENGTH),
                "container_role": _bounded_text(snapshots_by_path.get(container_path, AXElementSnapshot(container_path, 0)).role, MAX_ROLE_LENGTH),
                "container_subrole": _bounded_text(snapshots_by_path.get(container_path, AXElementSnapshot(container_path, 0)).subrole, MAX_ROLE_LENGTH),
                "row_path": _bounded_text(row_path, MAX_PATH_LENGTH),
            }
        )
    return sorted(sections, key=lambda item: _path_sort_key(item["heading_path"]))


def _section_purpose_from_title(title: str) -> str | None:
    normalized = _normalized_label(title).lower()
    if normalized in PROJECT_SECTION_LABELS:
        return "projects"
    if normalized in CHAT_SECTION_LABELS:
        return "chat_history"
    if normalized in {"search results", "search"}:
        return "search_results"
    return None


def _section_container_path(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> str | None:
    if _is_listlike(snapshot):
        return snapshot.path
    list_ancestor = _nearest_listlike_ancestor(snapshot.path, snapshots_by_path)
    if list_ancestor is not None:
        return list_ancestor.path
    alternate_sidebar = _nearest_alternate_sidebar_surface_ancestor(snapshot.path, snapshots_by_path)
    if alternate_sidebar is not None:
        return alternate_sidebar.path
    parent = _parent_path(snapshot.path)
    return parent


def _nearest_alternate_sidebar_surface_ancestor(
    path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> AXElementSnapshot | None:
    """Resolve nested ChatGPT sidebar wrappers without broadening globally."""

    for ancestor_path in _ancestor_paths(path):
        ancestor = snapshots_by_path.get(ancestor_path)
        if ancestor is None or ancestor.role not in ALTERNATE_SIDEBAR_SURFACE_ROLES:
            continue
        if _alternate_sidebar_surface_supported(ancestor, snapshots_by_path):
            return ancestor
    return None


def _alternate_sidebar_surface_supported(
    surface: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> bool:
    surface_frame = _frame_tuple(surface.frame)
    window = snapshots_by_path.get("W")
    window_frame = _frame_tuple(window.frame if window is not None else None)
    if not _frame_is_valid(surface_frame) or not _frame_is_valid(window_frame):
        return False
    if not _frame_contains_with_tolerance(window_frame, surface_frame, FRAME_CONTAINMENT_TOLERANCE):
        return False

    surface_x, _surface_y, surface_width, _surface_height = surface_frame
    window_x, _window_y, window_width, _window_height = window_frame
    max_width = min(
        ALTERNATE_SIDEBAR_MAX_WIDTH,
        window_width * ALTERNATE_SIDEBAR_MAX_WINDOW_WIDTH_FRACTION,
    )
    if surface_width > max_width:
        return False
    if abs(surface_x - window_x) > ALTERNATE_SIDEBAR_LEFT_EDGE_TOLERANCE:
        return False

    metadata = _navigation_metadata_text(surface).casefold()
    if "sidebar" in metadata or "navigation" in metadata:
        return True

    labels = _alternate_sidebar_navigation_labels(surface.path, snapshots_by_path)
    section_labels = labels.intersection({"projects", "history", "recents", "recent chats", "chats"})
    return "projects" in section_labels and len(section_labels) >= 2


def _alternate_sidebar_navigation_labels(
    surface_path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> set[str]:
    prefix = surface_path + "."
    recognized = {
        "projects",
        "history",
        "recents",
        "recent chats",
        "chats",
        "new chat",
        "new project",
        "search",
        "library",
        "gpts",
    }
    labels: set[str] = set()
    for snapshot in snapshots_by_path.values():
        if not snapshot.path.startswith(prefix):
            continue
        for value in (snapshot.title, snapshot.description, snapshot.value, snapshot.identifier):
            normalized = _normalized_label(value).casefold()
            if normalized in recognized:
                labels.add(normalized)
    return labels


def _row_path_under_container(path: str, container_path: str) -> str:
    if path == container_path:
        return path
    prefix = container_path + "."
    if not path.startswith(prefix):
        return path
    remainder = path[len(prefix):]
    first = remainder.split(".", 1)[0]
    return f"{container_path}.{first}"


def _visible_title_section_context(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    sections: list[dict],
) -> dict | None:
    container_path = _section_container_path(snapshot, snapshots_by_path)
    if not container_path:
        return None
    row_path = _row_path_under_container(snapshot.path, container_path)
    preceding = [
        section
        for section in sections
        if section["container_path"] == container_path
        and _path_sort_key(section["row_path"]) <= _path_sort_key(row_path)
    ]
    if not preceding:
        return None
    return _section_context(max(preceding, key=lambda item: _path_sort_key(item["row_path"])))


def _section_context(section: dict | None) -> dict | None:
    if not section:
        return None
    return {
        "purpose": section["purpose"],
        "list_path": section["container_path"],
        "list_role": section["container_role"],
        "list_subrole": section["container_subrole"],
        "section_heading_path": section["heading_path"],
        "section_heading_title": section["heading_title"],
    }


def _title_bucket_for_classification(classification: str) -> str:
    return {
        "visible_chat_title_candidate": "visible_chat_title_candidates",
        "visible_project_title_candidate": "visible_project_title_candidates",
        "visible_navigation_section_label": "visible_navigation_section_labels",
        "visible_search_result_candidate": "visible_search_result_candidates",
    }[classification]


def _visible_title_candidate(
    snapshot: AXElementSnapshot,
    title: dict,
    classification: str,
    context: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict:
    direct_actions = _safe_actions(snapshot.actions)
    nearest_parent = _nearest_actionable_ancestor(snapshot, snapshots_by_path)
    parent_actions = nearest_parent["actions"] if nearest_parent else []
    candidate_actionable = bool(SELECTION_ACTIONS.intersection(direct_actions))
    parent_actionable = bool(parent_actions)
    destination_kind = "project" if classification == "visible_project_title_candidate" else "chat"
    target_resolution = _resolve_visible_title_action_target(snapshot, destination_kind, title["text"], context, snapshots_by_path)
    return {
        "exact_title": title["text"],
        "path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
        "role": _bounded_text(snapshot.role, MAX_ROLE_LENGTH),
        "subrole": _bounded_text(snapshot.subrole, MAX_ROLE_LENGTH),
        "enabled": snapshot.enabled,
        "focused": snapshot.focused,
        "actions": direct_actions,
        "nearest_actionable_ancestor": nearest_parent or _empty_actionable_ancestor(),
        "nearest_list_container": {
            "path": context["list_path"],
            "role": context["list_role"],
            "subrole": context["list_subrole"],
            "purpose": context["purpose"],
        },
        "classification": classification,
        "confidence": "high" if classification in {"visible_chat_title_candidate", "visible_project_title_candidate"} else "medium",
        "evidence_codes": _visible_title_evidence(snapshot, classification, context, candidate_actionable, parent_actionable),
        "title_source_attribute": title["source"],
        "title_candidate_actionable": candidate_actionable,
        "parent_appears_actionable": parent_actionable,
        "capability_assessment": target_resolution["resolution_method"],
        "action_target_resolution": target_resolution,
    }


def _resolve_visible_title_action_target(
    snapshot: AXElementSnapshot,
    destination_kind: str,
    exact_title: str,
    context: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict:
    chain = _bounded_title_target_chain(snapshot, context, snapshots_by_path)
    direct_actions = _safe_actions(snapshot.actions)
    direct_press = snapshot.enabled is not False and "AXPress" in direct_actions
    if direct_press:
        return _target_resolution(
            destination_kind,
            exact_title,
            snapshot,
            snapshot,
            "direct_press_target",
            "high",
            ["title_node_exposes_AXPress"],
        )

    press_targets = [
        item
        for item in chain
        if item.path != snapshot.path and item.enabled is not False and "AXPress" in _safe_actions(item.actions)
    ]
    if len(press_targets) > 1:
        return _target_resolution(
            destination_kind,
            exact_title,
            snapshot,
            press_targets[0],
            "ambiguous_target",
            "low",
            ["multiple_bounded_ancestor_press_targets"],
        )
    if len(press_targets) == 1:
        target = press_targets[0]
        actions = _safe_actions(target.actions)
        if "AXSetFocus" in actions and target.focused is False:
            return _target_resolution(
                destination_kind,
                exact_title,
                snapshot,
                target,
                "focusable_then_press_target",
                "medium",
                ["target_explicitly_supports_AXSetFocus_and_AXPress", "target_not_observed_focused"],
            )
        return _target_resolution(
            destination_kind,
            exact_title,
            snapshot,
            target,
            "row_press_target",
            "medium",
            ["bounded_row_or_ancestor_exposes_AXPress"],
        )

    menu_targets = [
        item
        for item in chain
        if item.enabled is not False and set(_safe_actions(item.actions)).intersection(MENU_ONLY_ACTIONS)
    ]
    if menu_targets:
        return _target_resolution(
            destination_kind,
            exact_title,
            snapshot,
            menu_targets[0],
            "menu_only_target",
            "low",
            ["bounded_target_exposes_menu_action_only"],
        )

    return _target_resolution(
        destination_kind,
        exact_title,
        snapshot,
        snapshot,
        "no_verified_target",
        "low",
        ["no_bounded_target_exposes_AXPress"],
    )


def _bounded_title_target_chain(
    snapshot: AXElementSnapshot,
    context: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[AXElementSnapshot]:
    paths = [snapshot.path]
    container_path = context.get("list_path") or ""
    if container_path:
        row_path = _row_path_under_container(snapshot.path, container_path)
        if row_path not in paths:
            paths.append(row_path)
    for depth, ancestor_path in enumerate(_ancestor_paths(snapshot.path), start=1):
        if depth > MAX_ACTIONABLE_ANCESTOR_DEPTH:
            break
        if ancestor_path not in paths:
            paths.append(ancestor_path)
        if ancestor_path == container_path:
            break
    return [snapshots_by_path[path] for path in paths if path in snapshots_by_path]


def _target_resolution(
    destination_kind: str,
    exact_title: str,
    title_snapshot: AXElementSnapshot,
    target_snapshot: AXElementSnapshot,
    method: str,
    confidence: str,
    evidence: list[str],
) -> dict:
    actions = _safe_actions(target_snapshot.actions)
    return {
        "destination_kind": destination_kind,
        "exact_visible_title": exact_title,
        "title_ax_path": _bounded_text(title_snapshot.path, MAX_PATH_LENGTH),
        "resolved_target_ax_path": _bounded_text(target_snapshot.path, MAX_PATH_LENGTH),
        "resolution_method": method,
        "enabled_state": target_snapshot.enabled,
        "focused_state": target_snapshot.focused,
        "available_action_names": actions,
        "ax_press_available": "AXPress" in actions,
        "ax_set_focus_available": "AXSetFocus" in actions,
        "menu_only": bool(set(actions).intersection(MENU_ONLY_ACTIONS)) and "AXPress" not in actions,
        "confidence": confidence,
        "evidence": sorted(set(evidence)),
    }


def _visible_title_evidence(
    snapshot: AXElementSnapshot,
    classification: str,
    context: dict,
    candidate_actionable: bool,
    parent_actionable: bool,
) -> list[str]:
    evidence = [
        f"classification:{classification}",
        f"inside_list_purpose:{context['purpose']}",
        f"list_role:{context['list_role'] or context['list_subrole']}",
        "short_bounded_title",
    ]
    if _parent_path(snapshot.path) == context["list_path"]:
        evidence.append("direct_child_of_list")
    else:
        evidence.append("descendant_of_list")
    if candidate_actionable:
        evidence.append("title_node_exposes_selection_action")
    if parent_actionable:
        evidence.append("ancestor_exposes_selection_action")
    return sorted(set(evidence))


def _capability_assessment(
    candidate_actionable: bool,
    parent_actionable: bool,
    has_non_selection_action: bool,
) -> str:
    if candidate_actionable or parent_actionable:
        return "candidate_may_be_selectable_but_unverified"
    if has_non_selection_action:
        return "ambiguous"
    return "not_actionable"


def _empty_actionable_ancestor() -> dict:
    return {
        "path": "",
        "role": "",
        "subrole": "",
        "enabled": None,
        "actions": [],
        "relationship": "none_discovered",
    }


def _nearest_actionable_ancestor(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict | None:
    for depth, ancestor_path in enumerate(_ancestor_paths(snapshot.path), start=1):
        if depth > MAX_ACTIONABLE_ANCESTOR_DEPTH:
            break
        ancestor = snapshots_by_path.get(ancestor_path)
        if ancestor is None:
            continue
        actions = _safe_actions(ancestor.actions)
        if ancestor.enabled is not False and SELECTION_ACTIONS.intersection(actions):
            return {
                "path": _bounded_text(ancestor.path, MAX_PATH_LENGTH),
                "role": _bounded_text(ancestor.role, MAX_ROLE_LENGTH),
                "subrole": _bounded_text(ancestor.subrole, MAX_ROLE_LENGTH),
                "enabled": ancestor.enabled,
                "actions": actions,
                "relationship": "ancestor_exposes_selection_action",
            }
    return None


def _actionable_parent_candidate(
    parent: dict,
    title_candidate: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict:
    snapshot = snapshots_by_path.get(parent["path"])
    context = (
        _recognized_navigation_list_context(snapshot, snapshots_by_path)
        if snapshot is not None
        else None
    )
    return {
        "path": parent["path"],
        "role": parent["role"],
        "subrole": parent["subrole"],
        "enabled": parent["enabled"],
        "focused": snapshot.focused if snapshot is not None else None,
        "actions": parent["actions"],
        "classification": "actionable_parent_candidate",
        "confidence": "medium",
        "evidence_codes": [
            "ancestor_of_visible_title_candidate",
            "ancestor_exposes_selection_action",
            f"child_classification:{title_candidate['classification']}",
        ],
        "nearest_list_container": {
            "path": context["list_path"] if context else "",
            "role": context["list_role"] if context else "",
            "subrole": context["list_subrole"] if context else "",
            "purpose": context["purpose"] if context else "",
        },
        "example_child_title_path": title_candidate["path"],
        "capability_assessment": "candidate_may_be_selectable_but_unverified",
    }


def _sort_and_cap_title_candidates(candidates: list[dict]) -> list[dict]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            CONFIDENCE_ORDER.get(str(item.get("confidence")), 99),
            _path_sort_key(str(item.get("path") or "")),
            str(item.get("exact_title") or ""),
        ),
    )
    return ordered[:MAX_VISIBLE_TITLE_CANDIDATES]


def inspect_chatgpt_sidebar_destination(
    *,
    kind: str,
    title: str,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
) -> dict:
    requested_title = _normalized_label(title)
    result = _base_sidebar_destination_inspection_result(kind, requested_title, app_name)
    if kind not in {"project", "chat"} or not requested_title:
        result.update({"status": "target_not_found", "error": "kind must be project or chat and title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "inaccessible_or_unsupported", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "inaccessible_or_unsupported", "error": "ChatGPT sidebar destination inspection is only supported on macOS."})
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"status": "inaccessible_or_unsupported", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"status": "inaccessible_or_unsupported", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _DetailedReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        snapshots, stats, window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update({"status": "inaccessible_or_unsupported", "error": str(exc), "pid_present": True})
        return result

    classified = classify_navigation_snapshots(
        snapshots,
        stats,
        window_metadata,
        include_visible_navigation_titles=True,
    )
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    result["window_available"] = bool(classified.get("window_available"))
    result["traversal"] = classified.get("traversal") or {}
    if not matches:
        result.update({"status": "target_not_found", "error": "No exactly matching visible sidebar destination was found."})
        return result
    if len(matches) > 1:
        result.update({"status": "target_ambiguous", "error": "More than one matching visible sidebar destination was found."})
        return result

    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    candidate = matches[0]
    local_scope = _sidebar_destination_local_scope(candidate, snapshots, snapshots_by_path)
    result["target"] = _deep_inspection_target_summary(candidate, local_scope)
    result["scope"] = _deep_inspection_scope_summary(local_scope)
    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    sidebar_context = _resolve_sidebar_containment_context(local_scope["row_path"], snapshots_by_path, window_frame)
    result["frame_evidence"] = _destination_frame_evidence(
        candidate,
        snapshots_by_path,
        window_frame=window_frame,
        sidebar_context=sidebar_context,
    )
    result["elements"] = [
        _deep_inspection_element(snapshot, local_scope, requested_title, snapshots_by_path)
        for snapshot in local_scope["ordered_snapshots"]
    ]
    result["primary_selection_assessment"] = _assess_primary_selection_path(result["elements"], candidate)
    result["status"] = result["primary_selection_assessment"]["classification"]
    result["ok"] = result["status"] not in {"target_not_found", "target_ambiguous", "inaccessible_or_unsupported"}
    result["output_bounds"] = {
        "element_count": len(result["elements"]),
        "row_descendant_max_depth": DEEP_INSPECTOR_ROW_DESCENDANT_MAX_DEPTH,
        "row_descendant_max_nodes": DEEP_INSPECTOR_ROW_DESCENDANT_MAX_NODES,
        "sibling_max_nodes": DEEP_INSPECTOR_SIBLING_MAX_NODES,
    }
    return result


def _base_sidebar_destination_inspection_result(kind: str, title: str, app_name: str) -> dict:
    return {
        "ok": False,
        "status": "inaccessible_or_unsupported",
        "app_name": app_name,
        "kind": kind,
        "title": title,
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "window_available": False,
        "traversal": {},
        "target": {},
        "scope": {},
        "elements": [],
        "primary_selection_assessment": {
            "classification": "inaccessible_or_unsupported",
            "viable_candidate_controls": [],
            "evidence": [],
        },
        "actions_performed": [],
        "read_only": True,
        "error": "",
    }


def _sidebar_destination_local_scope(
    candidate: dict,
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict:
    title_path = str(candidate.get("path") or "")
    container = candidate.get("nearest_list_container") or {}
    list_path = str(container.get("path") or "")
    row_path = _row_path_under_container(title_path, list_path) if list_path else title_path
    retained: dict[str, str] = {}

    def add(path: str, relation: str) -> None:
        if path and path in snapshots_by_path:
            retained.setdefault(path, relation)

    add(title_path, "title_node")
    add(row_path, "computed_row_node")

    descendant_count = 0
    row_depth = snapshots_by_path.get(row_path, AXElementSnapshot(row_path, 0)).depth
    for snapshot in sorted(snapshots, key=lambda item: _path_sort_key(item.path)):
        if snapshot.path == row_path or not snapshot.path.startswith(row_path + "."):
            continue
        if snapshot.depth - row_depth > DEEP_INSPECTOR_ROW_DESCENDANT_MAX_DEPTH:
            continue
        if descendant_count >= DEEP_INSPECTOR_ROW_DESCENDANT_MAX_NODES:
            break
        add(snapshot.path, "row_descendant")
        descendant_count += 1

    for ancestor_path in _ancestor_paths(title_path):
        if ancestor_path in snapshots_by_path:
            add(ancestor_path, "ancestor")
        if ancestor_path == list_path:
            break

    row_parent = _parent_path(row_path)
    sibling_count = 0
    if row_parent:
        for snapshot in sorted(snapshots, key=lambda item: _path_sort_key(item.path)):
            if _parent_path(snapshot.path) != row_parent:
                continue
            if sibling_count >= DEEP_INSPECTOR_SIBLING_MAX_NODES:
                break
            add(snapshot.path, "row_container_sibling")
            sibling_count += 1

    related_count = 0
    index = 0
    while index < len(retained) and related_count < DEEP_INSPECTOR_RELATED_MAX_NODES:
        path = list(retained)[index]
        index += 1
        snapshot = snapshots_by_path.get(path)
        if snapshot is None:
            continue
        if retained.get(path) == "row_container_sibling":
            continue
        related_items = [(path, "linked_ui_element") for path in _snapshot_linked_paths(snapshot)]
        related_items.extend((path, "row_structure_element") for path in _snapshot_row_structure_paths(snapshot))
        for related_path, relation in related_items:
            if related_count >= DEEP_INSPECTOR_RELATED_MAX_NODES:
                break
            if related_path in snapshots_by_path and related_path not in retained:
                add(related_path, relation)
                related_count += 1

    ordered_paths = sorted(retained, key=_path_sort_key)
    return {
        "title_path": title_path,
        "row_path": row_path,
        "list_path": list_path,
        "relations": retained,
        "ordered_snapshots": [snapshots_by_path[path] for path in ordered_paths],
        "descendant_count": descendant_count,
        "sibling_count": sibling_count,
        "related_count": related_count,
    }


def _snapshot_linked_paths(snapshot: AXElementSnapshot) -> list[str]:
    paths = []
    for _, path in snapshot.linked_element_paths:
        paths.append(path)
    return paths


def _snapshot_row_structure_paths(snapshot: AXElementSnapshot) -> list[str]:
    paths: list[str] = []
    for collection in (snapshot.row_paths, snapshot.visible_row_paths, snapshot.selected_row_paths, snapshot.selected_child_paths):
        paths.extend(collection)
    return paths


def _deep_inspection_target_summary(candidate: dict, scope: dict) -> dict:
    resolution = candidate.get("action_target_resolution") or {}
    return {
        "kind": resolution.get("destination_kind") or "",
        "title": candidate.get("exact_title") or "",
        "title_ax_path": scope["title_path"],
        "computed_row_ax_path": scope["row_path"],
        "list_ax_path": scope["list_path"],
        "current_resolution_method": resolution.get("resolution_method") or "",
        "current_resolved_target_ax_path": resolution.get("resolved_target_ax_path") or "",
    }


def _deep_inspection_scope_summary(scope: dict) -> dict:
    return {
        "title_path": scope["title_path"],
        "row_path": scope["row_path"],
        "list_path": scope["list_path"],
        "retained_element_count": len(scope["ordered_snapshots"]),
        "row_descendant_count": scope["descendant_count"],
        "sibling_count": scope["sibling_count"],
        "related_count": scope["related_count"],
    }


def _window_frame_from_metadata(
    metadata: dict,
    snapshots: list[AXElementSnapshot],
) -> tuple[float, float, float, float] | None:
    window = metadata.get("window") if isinstance(metadata, dict) else None
    if isinstance(window, AXElementSnapshot):
        return _frame_tuple(window.frame)
    if snapshots:
        return _frame_tuple(snapshots[0].frame)
    return None


def _destination_frame_evidence(
    candidate: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
    *,
    window_frame: tuple[float, float, float, float] | None,
    sidebar_context: dict | None = None,
) -> dict:
    title_path = str(candidate.get("path") or "")
    container = candidate.get("nearest_list_container") or {}
    list_path = str(container.get("path") or "")
    row_path = _row_path_under_container(title_path, list_path) if list_path else title_path
    ancestor_path = _nearest_usable_frame_ancestor_path(title_path, snapshots_by_path, stop_path=list_path)
    sidebar_context = sidebar_context or _resolve_sidebar_containment_context(row_path, snapshots_by_path, window_frame)
    sidebar_frame = sidebar_context.get("frame")
    source = _select_frame_click_source(candidate, snapshots_by_path, window_frame=window_frame, sidebar_context=sidebar_context)
    click_point = _compute_safe_click_point(source.get("source_frame"))
    return {
        "title_node": {
            "path": title_path,
            "frame": _frame_report(
                snapshots_by_path.get(title_path, AXElementSnapshot(title_path, 0)).frame,
                window_frame=window_frame,
                sidebar_frame=sidebar_frame,
            ),
        },
        "computed_row_node": {
            "path": row_path,
            "frame": _frame_report(
                snapshots_by_path.get(row_path, AXElementSnapshot(row_path, 0)).frame,
                window_frame=window_frame,
                sidebar_frame=sidebar_frame,
            ),
        },
        "nearest_visible_ancestor_with_usable_frame": {
            "path": ancestor_path,
            "frame": _frame_report(
                snapshots_by_path.get(ancestor_path, AXElementSnapshot(ancestor_path, 0)).frame if ancestor_path else None,
                window_frame=window_frame,
                sidebar_frame=sidebar_frame,
            ),
        },
        "enclosing_sidebar_or_list": {
            "path": sidebar_context.get("path") or list_path,
            "role": sidebar_context.get("role") or "",
            "frame": _frame_report(sidebar_frame, window_frame=window_frame, sidebar_frame=sidebar_frame),
            "sidebar_containment_method": sidebar_context.get("method") or "",
            "row_inside_chosen_sidebar_frame": bool(sidebar_context.get("row_inside_chosen_sidebar_frame")),
        },
        "sidebar_or_list": {
            "path": sidebar_context.get("path") or list_path,
            "role": sidebar_context.get("role") or "",
            "frame": _frame_report(sidebar_frame, window_frame=window_frame, sidebar_frame=sidebar_frame),
            "sidebar_containment_method": sidebar_context.get("method") or "",
            "row_inside_chosen_sidebar_frame": bool(sidebar_context.get("row_inside_chosen_sidebar_frame")),
        },
        "focused_chatgpt_window": {
            "path": "W",
            "frame": _frame_report(window_frame, window_frame=window_frame, sidebar_frame=sidebar_frame),
        },
        "focused_window": {
            "path": "W",
            "frame": _frame_report(window_frame, window_frame=window_frame, sidebar_frame=sidebar_frame),
        },
        "chosen_click_source": {
            "path": source.get("source_path") or "",
            "relation": source.get("source_relation") or "",
            "frame": source.get("frame_report") or _empty_frame_report(),
        },
        "sidebar_containment": _sidebar_context_report(sidebar_context, window_frame),
        "computed_safe_click_point": click_point,
        "safety_checks_passed": bool(click_point.get("ok")) and _frame_report_passes_for_click(source.get("frame_report") or {}),
    }


def _deep_inspection_element(
    snapshot: AXElementSnapshot,
    scope: dict,
    requested_title: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict:
    parent_path = _parent_path(snapshot.path) or ""
    attribute_names = _safe_attribute_names(snapshot.attribute_names)
    parameterized_names = _safe_parameterized_attribute_names(snapshot.parameterized_attribute_names)
    settable = {name: name in snapshot.settable_attribute_names for name in SELECTION_FOCUS_ATTRIBUTES}
    relation = scope["relations"].get(snapshot.path, "retained")
    return {
        "path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
        "relation_to_requested_title": relation,
        "parent_path": _bounded_text(parent_path, MAX_PATH_LENGTH),
        "role": _bounded_text(snapshot.role, MAX_ROLE_LENGTH),
        "subrole": _bounded_text(snapshot.subrole, MAX_ROLE_LENGTH),
        "enabled": snapshot.enabled,
        "focused": snapshot.focused,
        "selected": snapshot.selected,
        "frame": _frame_report(snapshot.frame),
        "title": _deep_label_report(snapshot.title, requested_title, _label_classification(snapshot)),
        "description": _deep_label_report(snapshot.description, requested_title, _label_classification(snapshot)),
        "value": _deep_label_report(snapshot.value, requested_title, _label_classification(snapshot)),
        "actions": _safe_actions(snapshot.actions),
        "action_descriptions": _safe_action_descriptions(snapshot.action_descriptions),
        "direct_children_count": _element_child_count(snapshot, snapshots_by_path, "AXChildren"),
        "visible_children_count": _element_child_count(snapshot, snapshots_by_path, "AXVisibleChildren"),
        "supported_attributes": {name: name in attribute_names for name in DEEP_INSPECTOR_RELEVANT_ATTRIBUTES},
        "supported_parameterized_attributes": parameterized_names,
        "settable_attributes": settable,
        "row_structure": {
            "AXRows": _bounded_paths(snapshot.row_paths),
            "AXVisibleRows": _bounded_paths(snapshot.visible_row_paths),
            "AXSelectedRows": _bounded_paths(snapshot.selected_row_paths),
            "AXSelectedChildren": _bounded_paths(snapshot.selected_child_paths),
        },
        "linked_elements": _linked_element_reports(snapshot, snapshots_by_path, requested_title),
    }


def _safe_attribute_names(names: tuple[str, ...]) -> list[str]:
    return sorted({_bounded_text(name, MAX_ACTION_NAME_LENGTH) for name in names if name})[:DEEP_INSPECTOR_ATTRIBUTE_NAMES_MAX]


def _safe_parameterized_attribute_names(names: tuple[str, ...]) -> list[str]:
    return sorted({_bounded_text(name, MAX_ACTION_NAME_LENGTH) for name in names if name})[:DEEP_INSPECTOR_PARAMETERIZED_NAMES_MAX]


def _safe_action_descriptions(descriptions: tuple[tuple[str, str], ...]) -> dict:
    return {
        _bounded_text(action, MAX_ACTION_NAME_LENGTH): _bounded_text(_normalized_label(description), DEEP_INSPECTOR_ACTION_DESCRIPTION_MAX)
        for action, description in descriptions
        if action and description
    }


def _deep_label_report(value: str, requested_title: str, classification: str) -> dict:
    normalized = _normalized_label(value)
    if normalized and normalized == requested_title:
        return {
            "literal": requested_title,
            "classification": "requested_visible_destination_title",
            "normalized_length": len(normalized),
            "sha256": _label_digest(normalized),
            "redacted": False,
            "source_allowed": "exact_requested_title",
        }
    return _label_report(value, classification)


def _element_child_count(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    child_attribute: str,
) -> int:
    if child_attribute == "AXChildren" and snapshot.direct_child_count is not None:
        return snapshot.direct_child_count
    if child_attribute == "AXVisibleChildren" and snapshot.visible_child_count is not None:
        return snapshot.visible_child_count
    return sum(1 for path in snapshots_by_path if _parent_path(path) == snapshot.path)


def _bounded_paths(paths: tuple[str, ...]) -> list[str]:
    return [_bounded_text(path, MAX_PATH_LENGTH) for path in paths[:DEEP_INSPECTOR_RELATED_MAX_NODES]]


def _linked_element_reports(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    requested_title: str,
) -> list[dict]:
    reports = []
    for attribute, path in snapshot.linked_element_paths[:DEEP_INSPECTOR_RELATED_MAX_NODES]:
        linked = snapshots_by_path.get(path)
        report = {
            "attribute": _bounded_text(attribute, MAX_ACTION_NAME_LENGTH),
            "path": _bounded_text(path, MAX_PATH_LENGTH),
        }
        if linked is not None:
            report.update(
                {
                    "role": _bounded_text(linked.role, MAX_ROLE_LENGTH),
                    "subrole": _bounded_text(linked.subrole, MAX_ROLE_LENGTH),
                    "enabled": linked.enabled,
                    "focused": linked.focused,
                    "selected": linked.selected,
                    "actions": _safe_actions(linked.actions),
                    "title": _deep_label_report(linked.title, requested_title, _label_classification(linked)),
                    "description": _deep_label_report(linked.description, requested_title, _label_classification(linked)),
                    "value": _deep_label_report(linked.value, requested_title, _label_classification(linked)),
                }
            )
        reports.append(report)
    return reports


def _assess_primary_selection_path(elements: list[dict], candidate: dict) -> dict:
    viable_controls = []
    menu_controls = []
    for element in elements:
        actions = set(element.get("actions") or [])
        relation = str(element.get("relation_to_requested_title") or "")
        primary_relation = relation in {"title_node", "computed_row_node", "row_descendant", "linked_ui_element"}
        is_overflow = bool(element["supported_attributes"].get("AXOverflowButton")) or relation == "linked_ui_element" and "overflow" in str(element.get("path", "")).lower()
        if primary_relation and "AXPress" in actions and not is_overflow:
            viable_controls.append(
                _primary_selection_candidate(
                    element,
                    "advertised AXPress on retained title/row structure",
                    "high" if relation in {"title_node", "computed_row_node", "row_descendant"} else "medium",
                )
            )
        elif primary_relation and set(element.get("settable_attributes") or {}).intersection({"AXFocused", "AXSelected", "AXSelectedChildren", "AXSelectedRows"}):
            settable = element.get("settable_attributes") or {}
            if any(settable.get(name) for name in SELECTION_FOCUS_ATTRIBUTES) and not is_overflow:
                viable_controls.append(
                    _primary_selection_candidate(
                        element,
                        "documented focus or selection attribute is supported and settable",
                        "medium",
                    )
                )
        if primary_relation and (actions and actions.issubset(MENU_ONLY_ACTIONS) or ("AXShowMenu" in actions and "AXPress" not in actions)):
            menu_controls.append(
                _primary_selection_candidate(
                    element,
                    "advertised action opens menu or alternate UI only",
                    "low",
                )
            )

    press_controls = [control for control in viable_controls if "AXPress" in control["concrete_advertised_actions"]]
    focus_selection_controls = [
        control
        for control in viable_controls
        if "AXPress" not in control["concrete_advertised_actions"]
    ]
    if len(press_controls) == 1:
        classification = "verified_press_target_found"
    elif len(press_controls) > 1:
        classification = "ambiguous_primary_selection_path"
    elif focus_selection_controls:
        classification = "verified_focus_or_selection_path_found" if len(focus_selection_controls) == 1 else "ambiguous_primary_selection_path"
    elif menu_controls:
        classification = "menu_only_target"
    else:
        classification = "no_primary_selection_path_found"
    return {
        "classification": classification,
        "viable_candidate_controls": viable_controls if viable_controls else menu_controls,
        "evidence": [
            f"existing_resolution:{(candidate.get('action_target_resolution') or {}).get('resolution_method') or ''}",
            f"viable_controls:{len(viable_controls)}",
            f"menu_only_controls:{len(menu_controls)}",
        ],
    }


def inspect_chatgpt_project_visible_chats(
    *,
    project_title: str,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
) -> dict:
    requested_title = _normalized_label(project_title)
    result = _base_project_visible_chats_result(requested_title, app_name)
    if not requested_title:
        result.update({"status": "project_not_open", "error": "project_title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "inspection_unavailable", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "inspection_unavailable", "error": "ChatGPT project chat inspection is only supported on macOS."})
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"status": "inspection_unavailable", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"status": "chatgpt_not_running", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _DetailedReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        snapshots, stats, window_metadata = reader.collect(process.pid)
    except Exception as exc:
        status = "accessibility_not_trusted" if _looks_like_accessibility_trust_error(str(exc)) else "inspection_unavailable"
        result.update({"status": status, "error": str(exc), "pid_present": True})
        return result

    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    result["traversal"] = {
        "visited_nodes": stats.get("visited_nodes", len(snapshots)),
        "emitted_nodes": len(snapshots),
        "max_depth": stats.get("max_depth"),
        "max_nodes": stats.get("max_nodes"),
        "truncated_by_node_limit": bool(stats.get("truncated_by_node_limit")),
        "truncated_by_depth_limit": bool(stats.get("truncated_by_depth_limit")),
    }
    result["window_frame"] = _frame_geometry_report(window_frame)

    resolution = resolve_open_project_content_and_visible_chats(
        requested_title,
        snapshots,
        window_frame,
        traversal_stats=stats,
        window_metadata=window_metadata,
    )
    result.update(_project_visible_chats_resolution_fields(resolution))
    result["status"] = resolution.get("status") or "inspection_unavailable"
    result["ok"] = result["status"] == "visible_chats_found"
    result["error"] = resolution.get("error") or ""
    return result


def inspect_chatgpt_project_chat_row_ax(
    *,
    project_title: str,
    chat_titles: list[str] | tuple[str, ...],
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
) -> dict:
    requested_project = _normalized_label(project_title)
    requested_titles = [_normalized_label(title) for title in chat_titles or [] if _normalized_label(title)]
    result = _base_project_chat_row_ax_audit_result(requested_project, requested_titles, app_name)
    if not requested_project or not requested_titles:
        result.update({"status": "inspection_unavailable", "error": "project_title and at least one chat_title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "inspection_unavailable", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "inspection_unavailable", "error": "ChatGPT project chat row AX audit is only supported on macOS."})
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"status": "inspection_unavailable", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"status": "chatgpt_not_running", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _DetailedReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        snapshots, stats, window_metadata = reader.collect(process.pid)
    except Exception as exc:
        status = "accessibility_not_trusted" if _looks_like_accessibility_trust_error(str(exc)) else "inspection_unavailable"
        result.update({"status": status, "error": str(exc), "pid_present": True})
        return result

    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    result["traversal"] = {
        "visited_nodes": stats.get("visited_nodes", len(snapshots)),
        "emitted_nodes": len(snapshots),
        "max_depth": stats.get("max_depth"),
        "max_nodes": stats.get("max_nodes"),
        "truncated_by_node_limit": bool(stats.get("truncated_by_node_limit")),
        "truncated_by_depth_limit": bool(stats.get("truncated_by_depth_limit")),
    }
    result["window_frame"] = _frame_geometry_report(window_frame)
    resolution = resolve_open_project_content_and_visible_chats(
        requested_project,
        snapshots,
        window_frame,
        traversal_stats=stats,
        window_metadata=window_metadata,
    )
    result["project_resolution_status"] = resolution.get("status") or ""
    result["visible_chat_count"] = int(resolution.get("visible_chat_count") or 0)
    result["accepted_visible_chat_rows"] = [
        {
            "ordinal": row.get("ordinal"),
            "title": row.get("title") or "",
            "row_path": row.get("row_path") or row.get("path") or "",
            "row_frame": row.get("row_frame") or _frame_geometry_report(None),
        }
        for row in resolution.get("visible_chats") or []
    ]
    if resolution.get("status") != "visible_chats_found":
        result.update({"status": _project_chat_row_ax_status_from_resolution(resolution), "error": resolution.get("error") or ""})
        return result

    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    audits = []
    for chat_title in requested_titles:
        audit = _project_chat_row_ax_audit_for_title(chat_title, resolution.get("visible_chats") or [], snapshots_by_path)
        if audit.get("status") != "row_audit_ready":
            result.update({"status": audit.get("status") or "inspection_unavailable", "error": audit.get("error") or ""})
            result["row_audits"] = audits + [audit]
            return result
        audits.append(audit)

    result["row_audits"] = audits
    result.update({"ok": True, "status": "row_audit_ready", "error": ""})
    return result


def diagnose_chatgpt_project_chat_rows(
    *,
    project_title: str,
    contains_title: str = "",
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
) -> dict:
    requested_project = _normalized_label(project_title)
    filter_text = _normalized_label(contains_title)
    result = _base_project_chat_row_diagnostic_result(requested_project, filter_text, app_name)
    if not requested_project:
        result.update({"status": "inspection_unavailable", "error": "project_title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "inspection_unavailable", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "inspection_unavailable", "error": "ChatGPT project chat row diagnostic is only supported on macOS."})
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"status": "inspection_unavailable", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"status": "chatgpt_not_running", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _DetailedReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        snapshots, stats, window_metadata = reader.collect(process.pid)
    except Exception as exc:
        status = "accessibility_not_trusted" if _looks_like_accessibility_trust_error(str(exc)) else "inspection_unavailable"
        result.update({"status": status, "error": str(exc), "pid_present": True})
        return result

    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    result["traversal"] = {
        "visited_nodes": stats.get("visited_nodes", len(snapshots)),
        "emitted_nodes": len(snapshots),
        "max_depth": stats.get("max_depth"),
        "max_nodes": stats.get("max_nodes"),
        "truncated_by_node_limit": bool(stats.get("truncated_by_node_limit")),
        "truncated_by_depth_limit": bool(stats.get("truncated_by_depth_limit")),
    }
    result["window_frame"] = _frame_geometry_report(window_frame)
    diagnostic = diagnose_chatgpt_project_chat_rows_from_snapshots(
        requested_project,
        snapshots,
        window_frame,
        traversal_stats=stats,
        window_metadata=window_metadata,
        contains_title=filter_text,
    )
    result.update(diagnostic)
    result["app_name"] = app_name
    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    return result


def diagnose_chatgpt_project_chat_rows_from_snapshots(
    requested_project_title: str,
    current_chatgpt_ax_tree: list[AXElementSnapshot],
    current_chatgpt_window_bounds: tuple[float, float, float, float] | None,
    *,
    traversal_stats: dict | None = None,
    window_metadata: dict | None = None,
    contains_title: str = "",
) -> dict:
    requested_project = _normalized_label(requested_project_title)
    filter_text = _normalized_label(contains_title)
    result = _base_project_chat_row_diagnostic_result(requested_project, filter_text, "ChatGPT")
    snapshots = list(current_chatgpt_ax_tree or [])
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    window_frame = _frame_tuple(current_chatgpt_window_bounds)
    result["traversal"] = {
        "visited_nodes": (traversal_stats or {}).get("visited_nodes", len(snapshots)),
        "emitted_nodes": len(snapshots),
        "max_depth": (traversal_stats or {}).get("max_depth"),
        "max_nodes": (traversal_stats or {}).get("max_nodes"),
        "truncated_by_node_limit": bool((traversal_stats or {}).get("truncated_by_node_limit")),
        "truncated_by_depth_limit": bool((traversal_stats or {}).get("truncated_by_depth_limit")),
    }
    result["window_frame"] = _frame_geometry_report(window_frame)
    result["window_metadata_source"] = (window_metadata or {}).get("window_source") or ""
    resolution = resolve_open_project_content_and_visible_chats(
        requested_project,
        snapshots,
        window_frame,
        traversal_stats=traversal_stats,
        window_metadata=window_metadata,
    )
    result["project_resolution_status"] = resolution.get("status") or ""
    result["project_identity_confirmed"] = bool(resolution.get("project_identity_confirmed"))
    result["project_chat_list_identity"] = resolution.get("project_chat_list_identity") or "not_confirmed"
    result["project_chat_list_container_path"] = resolution.get("project_chat_list_container_path") or ""
    result["project_chat_list_container_role"] = resolution.get("project_chat_list_container_role") or ""
    result["project_chat_list_container_frame"] = resolution.get("project_chat_list_container_frame") or _frame_geometry_report(None)
    result["identity_failure_reasons"] = list(resolution.get("identity_failure_reasons") or [])
    result["current_resolver_accepted_rows"] = _diagnostic_current_resolver_rows(resolution)
    if not result["project_identity_confirmed"] or result["project_chat_list_identity"] != "confirmed":
        result.update(
            {
                "ok": False,
                "status": "project_chat_list_identity_not_confirmed",
                "final_outcome": "project_chat_list_identity_not_confirmed",
                "error": resolution.get("error") or "Project Chats-list identity could not be confirmed.",
            }
        )
        result["summary"] = _diagnostic_summary(result, filtered_bands_printed=0)
        return result

    viewport_frame = _frame_tuple((resolution.get("chat_list_container") or {}).get("frame")) or _frame_tuple(result["project_chat_list_container_frame"])
    container_path = result["project_chat_list_container_path"]
    inspected_nodes = _diagnostic_nodes_inside_confirmed_list(snapshots, snapshots_by_path, container_path, viewport_frame)
    bands = _diagnostic_visual_row_bands(inspected_nodes, snapshots_by_path, viewport_frame)
    accepted_rows = resolution.get("visible_chats") or []
    for band in bands:
        band["title_candidates"] = _diagnostic_band_title_candidates(band, snapshots_by_path)
        band["current_resolver_comparison"] = _diagnostic_band_current_resolver_comparison(
            band,
            accepted_rows,
            snapshots_by_path,
            container_path,
            viewport_frame,
        )
        band["experimental_canonical"] = _diagnostic_experimental_canonical_title(band["title_candidates"])

    printed_indexes = _diagnostic_filtered_band_indexes(bands, filter_text)
    filtered_nodes = [
        node
        for node in inspected_nodes
        if not filter_text or any(node["path"] in band.get("node_paths", []) for band in bands if band["band_index"] in printed_indexes)
    ]
    result.update(
        {
            "ok": True,
            "status": "diagnostic_ready",
            "final_outcome": "diagnostic_ready",
            "confirmed_list_nodes": filtered_nodes,
            "visual_row_bands": [band for band in bands if band["band_index"] in printed_indexes],
            "all_visual_band_count": len(bands),
            "hidden_unrelated_band_count": max(0, len(bands) - len(printed_indexes)),
            "collection_counts_before_filter": {
                "ax_nodes_inspected": len(inspected_nodes),
                "visual_bands_found": len(bands),
            },
            "error": "",
        }
    )
    result["summary"] = _diagnostic_summary(result, filtered_bands_printed=len(printed_indexes), all_bands=bands)
    return result


def _base_project_chat_row_diagnostic_result(project_title: str, contains_title: str, app_name: str) -> dict:
    return {
        "ok": False,
        "status": "inspection_unavailable",
        "final_outcome": "",
        "app_name": app_name,
        "requested_project_title": project_title,
        "contains_title": contains_title,
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "window_frame": _frame_geometry_report(None),
        "traversal": {},
        "project_resolution_status": "",
        "project_identity_confirmed": False,
        "project_chat_list_identity": "not_confirmed",
        "project_chat_list_container_path": "",
        "project_chat_list_container_role": "",
        "project_chat_list_container_frame": _frame_geometry_report(None),
        "identity_failure_reasons": [],
        "current_resolver_accepted_rows": [],
        "confirmed_list_nodes": [],
        "visual_row_bands": [],
        "all_visual_band_count": 0,
        "hidden_unrelated_band_count": 0,
        "collection_counts_before_filter": {"ax_nodes_inspected": 0, "visual_bands_found": 0},
        "summary": {},
        "actions_performed": [],
        "read_only": True,
        "error": "",
    }


def _diagnostic_current_resolver_rows(resolution: dict) -> list[dict]:
    rows = []
    for row in resolution.get("visible_chats") or []:
        rows.append(
            {
                "ordinal": row.get("ordinal"),
                "title": row.get("title") or "",
                "row_path": row.get("row_path") or row.get("path") or "",
                "row_role": row.get("row_role") or row.get("role") or "",
                "row_frame": row.get("row_frame") or _frame_geometry_report(None),
                "title_path": row.get("title_path") or "",
                "title_representation": row.get("title_representation") or "",
                "preview_representation": row.get("preview_representation") or "",
            }
        )
    return rows


def _diagnostic_nodes_inside_confirmed_list(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    container_path: str,
    viewport_frame: tuple[float, float, float, float] | None,
) -> list[dict]:
    nodes = []
    for snapshot in sorted(snapshots, key=lambda item: _path_sort_key(item.path)):
        if not _project_node_within_container(snapshot, container_path):
            continue
        frame = _frame_tuple(snapshot.frame)
        if not _diagnostic_frame_meaningfully_intersects(viewport_frame, frame):
            continue
        parent_path = _parent_path(snapshot.path) or ""
        parent = snapshots_by_path.get(parent_path)
        nodes.append(
            {
                "node_index": len(nodes) + 1,
                "path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
                "parent_path": _bounded_text(parent_path, MAX_PATH_LENGTH),
                "parent_role": _bounded_text(parent.role if parent else "", MAX_ROLE_LENGTH),
                "role": _bounded_text(snapshot.role, MAX_ROLE_LENGTH),
                "subrole": _bounded_text(snapshot.subrole, MAX_ROLE_LENGTH),
                "frame": _frame_geometry_report(frame),
                "frame_height": round(float(frame[3]), 2) if frame else None,
                "actions": _safe_actions(snapshot.actions),
                "AXTitle": _bounded_text(_normalized_label(snapshot.title), PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
                "AXDescription": _bounded_text(_normalized_label(snapshot.description), PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
                "AXValue": _bounded_text(_normalized_label(snapshot.value), PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
            }
        )
    return nodes


def _diagnostic_frame_meaningfully_intersects(
    viewport_frame: tuple[float, float, float, float] | None,
    frame: tuple[float, float, float, float] | None,
) -> bool:
    viewport_frame = _frame_tuple(viewport_frame)
    frame = _frame_tuple(frame)
    if viewport_frame is None or frame is None or not _frame_is_valid(frame):
        return False
    if not _frame_intersects(viewport_frame, frame):
        return False
    ax, ay, aw, ah = viewport_frame
    bx, by, bw, bh = frame
    overlap_width = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_height = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    overlap_area = overlap_width * overlap_height
    return overlap_area >= max(1.0, min(aw * ah, bw * bh) * 0.05)


def _diagnostic_visual_row_bands(
    nodes: list[dict],
    snapshots_by_path: dict[str, AXElementSnapshot],
    viewport_frame: tuple[float, float, float, float] | None,
) -> list[dict]:
    framed = []
    for node in nodes:
        frame = _frame_tuple(node.get("frame"))
        if not _frame_is_valid(frame) or not _frame_intersects(viewport_frame, frame):
            continue
        if not _diagnostic_node_can_contribute_to_row_band(node, snapshots_by_path, viewport_frame):
            continue
        framed.append((node, frame))
    framed.sort(key=lambda item: (item[1][1] + item[1][3] / 2.0, item[1][0], _path_sort_key(item[0]["path"])))
    raw_bands: list[list[tuple[dict, tuple[float, float, float, float]]]] = []
    for item in framed:
        node, frame = item
        placed = False
        for band in raw_bands:
            if _diagnostic_frame_belongs_to_band(frame, [band_item[1] for band_item in band]):
                band.append(item)
                placed = True
                break
        if not placed:
            raw_bands.append([item])

    bands = []
    for band_items in raw_bands:
        band_items.sort(key=lambda item: (_path_sort_key(item[0]["path"]), item[1][1], item[1][0]))
        band_frame = _diagnostic_union_frame([item[1] for item in band_items])
        if not _frame_intersects(viewport_frame, band_frame):
            continue
        outer = _diagnostic_outermost_band_candidate([item[0] for item in band_items], snapshots_by_path)
        bands.append(
            {
                "band_index": len(bands) + 1,
                "band_frame": _frame_geometry_report(band_frame),
                "band_height": round(float(band_frame[3]), 2) if band_frame else None,
                "node_count": len(band_items),
                "node_paths": [item[0]["path"] for item in band_items],
                "nodes": [item[0] for item in band_items],
                "outermost_candidate_path": outer.path if outer else "",
                "outermost_candidate_role": outer.role if outer else "",
            }
        )
    bands.sort(key=lambda band: (_frame_tuple(band["band_frame"])[1] if _frame_tuple(band["band_frame"]) else 0.0, _path_sort_key(band["outermost_candidate_path"])))
    for index, band in enumerate(bands, start=1):
        band["band_index"] = index
    return bands


def _diagnostic_node_can_contribute_to_row_band(
    node: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
    viewport_frame: tuple[float, float, float, float] | None,
) -> bool:
    role = str(node.get("role") or "")
    if role in PROJECT_CHAT_NON_ROW_CONTROL_ROLES:
        return False
    frame = _frame_tuple(node.get("frame"))
    viewport = _frame_tuple(viewport_frame)
    if frame is None or viewport is None:
        return False
    max_reasonable_height = max(PROJECT_CHAT_ROW_MIN_HEIGHT * 3.0, viewport[3] * 0.95)
    if frame[3] > max_reasonable_height:
        return False
    snapshot = snapshots_by_path.get(node.get("path") or "")
    actions = _safe_actions(snapshot.actions if snapshot else node.get("actions") or [])
    text = _normalized_label(" ".join(str(node.get(key) or "") for key in ("AXTitle", "AXDescription", "AXValue")))
    if role in {"AXButton", "AXLink"} and ("AXPress" in actions or text):
        return True
    if role in {"AXGroup", "AXRow", "AXCell"} and (frame[3] >= PROJECT_CHAT_ROW_MIN_HEIGHT or "AXPress" in actions or text):
        return True
    if role in TEXTLIKE_ROLES and text:
        return True
    if text:
        return True
    return False


def _diagnostic_frame_belongs_to_band(
    frame: tuple[float, float, float, float],
    band_frames: list[tuple[float, float, float, float]],
) -> bool:
    center_y = frame[1] + frame[3] / 2.0
    for existing in band_frames:
        existing_center_y = existing[1] + existing[3] / 2.0
        overlap = max(0.0, min(frame[1] + frame[3], existing[1] + existing[3]) - max(frame[1], existing[1]))
        overlap_ratio = overlap / max(1.0, min(frame[3], existing[3]))
        close_center = abs(center_y - existing_center_y) <= max(18.0, min(frame[3], existing[3]) * 0.75)
        if overlap_ratio >= 0.35 or close_center:
            return True
    return False


def _diagnostic_union_frame(frames: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    normalized = [_frame_tuple(frame) for frame in frames if _frame_tuple(frame) is not None]
    if not normalized:
        return None
    left = min(frame[0] for frame in normalized)
    top = min(frame[1] for frame in normalized)
    right = max(frame[0] + frame[2] for frame in normalized)
    bottom = max(frame[1] + frame[3] for frame in normalized)
    return (left, top, right - left, bottom - top)


def _diagnostic_outermost_band_candidate(nodes: list[dict], snapshots_by_path: dict[str, AXElementSnapshot]) -> AXElementSnapshot | None:
    candidates = []
    for node in nodes:
        snapshot = snapshots_by_path.get(node.get("path") or "")
        if snapshot is None:
            continue
        if snapshot.role in {"AXButton", "AXLink", "AXGroup", "AXRow", "AXCell"} or "AXPress" in _safe_actions(snapshot.actions):
            candidates.append(snapshot)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.depth, -_frame_area(_frame_tuple(item.frame)), _path_sort_key(item.path)))[0]


def _diagnostic_band_title_candidates(band: dict, snapshots_by_path: dict[str, AXElementSnapshot]) -> list[dict]:
    candidates = []
    seen: set[tuple[str, str, str]] = set()
    for path in band.get("node_paths") or []:
        snapshot = snapshots_by_path.get(path)
        if snapshot is None or snapshot.role not in {"AXButton", "AXLink", "AXStaticText", "AXGroup", "AXRow", "AXCell"}:
            continue
        for attribute, raw in (("AXTitle", snapshot.title), ("AXDescription", snapshot.description), ("AXValue", snapshot.value)):
            text = _normalized_label(raw)
            if not text:
                continue
            key = (snapshot.path, attribute, text)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "source_path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
                    "source_role": _bounded_text(snapshot.role, MAX_ROLE_LENGTH),
                    "source_attribute": attribute,
                    "raw_text": _bounded_text(text, PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
                    "candidate_kind": _diagnostic_candidate_kind(text),
                }
            )
    return candidates


def _diagnostic_candidate_kind(text: str) -> str:
    normalized = _normalized_label(text)
    if not normalized or _is_punctuation_or_separator_only(normalized):
        return "ambiguous"
    if _looks_like_message_or_code(normalized):
        return "preview_like"
    if ", " in normalized:
        prefix, _sep, suffix = normalized.partition(", ")
        if _diagnostic_text_is_title_like(prefix) and suffix:
            return "ambiguous"
    if _diagnostic_text_is_title_like(normalized):
        return "title_like"
    return "preview_like" if len(normalized) > 90 else "ambiguous"


def _diagnostic_text_is_title_like(text: str) -> bool:
    normalized = _normalized_label(text)
    if not normalized or len(normalized) > 90:
        return False
    if _is_punctuation_or_separator_only(normalized) or _looks_like_message_or_code(normalized):
        return False
    if _is_generic_visible_navigation_title(normalized):
        return False
    words = normalized.split()
    if len(words) > 12:
        return False
    return True


def _diagnostic_band_current_resolver_comparison(
    band: dict,
    accepted_rows: list[dict],
    snapshots_by_path: dict[str, AXElementSnapshot],
    container_path: str,
    viewport_frame: tuple[float, float, float, float] | None,
) -> dict:
    accepted = _diagnostic_accepted_row_for_band(band, accepted_rows)
    if accepted:
        return {
            "current_resolver_status": "accepted_currently",
            "current_resolver_title": accepted.get("title") or "",
            "current_resolver_row_path": accepted.get("row_path") or accepted.get("path") or "",
            "current_resolver_row_frame": accepted.get("row_frame") or _frame_geometry_report(None),
            "current_resolver_rejection_reasons": [],
        }
    reasons = []
    saw_current_candidate = False
    for node in band.get("nodes") or []:
        snapshot = snapshots_by_path.get(node.get("path") or "")
        if snapshot is None:
            continue
        title_text = _project_visible_row_title(snapshot)
        node_reasons = _diagnostic_current_candidate_rejection_reasons(snapshot, snapshots_by_path, container_path, viewport_frame)
        if title_text:
            saw_current_candidate = True
        reasons.extend(node_reasons)
    reasons = list(dict.fromkeys(reasons))
    if not saw_current_candidate:
        status = "not_seen_by_current_resolver"
        if not reasons:
            reasons = ["unsupported_candidate_role"]
    else:
        status = "rejected_currently"
    return {
        "current_resolver_status": status,
        "current_resolver_title": "",
        "current_resolver_row_path": "",
        "current_resolver_row_frame": _frame_geometry_report(None),
        "current_resolver_rejection_reasons": reasons,
    }


def _diagnostic_accepted_row_for_band(band: dict, accepted_rows: list[dict]) -> dict | None:
    node_paths = set(band.get("node_paths") or [])
    band_frame = _frame_tuple(band.get("band_frame"))
    matches = []
    for row in accepted_rows:
        row_path = row.get("row_path") or row.get("path") or ""
        title_path = row.get("title_path") or ""
        row_frame = _frame_tuple(row.get("row_frame"))
        if row_path in node_paths or title_path in node_paths or _diagnostic_vertical_overlap_ratio(band_frame, row_frame) >= 0.55:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    return None


def _diagnostic_vertical_overlap_ratio(
    a: tuple[float, float, float, float] | None,
    b: tuple[float, float, float, float] | None,
) -> float:
    a = _frame_tuple(a)
    b = _frame_tuple(b)
    if a is None or b is None:
        return 0.0
    overlap = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return overlap / max(1.0, min(a[3], b[3]))


def _diagnostic_current_candidate_rejection_reasons(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    container_path: str,
    viewport_frame: tuple[float, float, float, float] | None,
) -> list[str]:
    reasons = []
    frame = _frame_tuple(snapshot.frame)
    title_info = _project_visible_row_title_info(snapshot)
    text = _normalized_label(title_info.get("title") or snapshot.title or snapshot.description or ("" if snapshot.role in VALUE_LENGTH_ONLY_ROLES else snapshot.value))
    if snapshot.role not in TEXTLIKE_ROLES and snapshot.role not in {"AXButton", "AXLink"}:
        reasons.append("unsupported_candidate_role")
    if not text:
        reasons.append("empty_title_description_value")
    if not _project_node_within_container(snapshot, container_path):
        reasons.append("not_descendant_of_confirmed_list")
    if frame is None:
        reasons.append("candidate_frame_missing")
    elif not _frame_intersects(viewport_frame, frame):
        reasons.append("candidate_outside_viewport")
    elif frame[3] < PROJECT_CHAT_ROW_MIN_HEIGHT and snapshot.role in {"AXButton", "AXLink"}:
        reasons.append("below_minimum_row_height")
    if text and _is_generic_visible_navigation_title(text):
        reasons.append("generic_navigation_label")
    if text and _looks_like_message_or_code(text):
        reasons.append("canonical_title_message_or_code_like")
    title_text = _project_visible_row_title(snapshot)
    if title_text:
        row_node = _project_chat_row_container(snapshot, snapshots_by_path, viewport_frame, 0.0, {"paths": [], "frames": []})
        if row_node is None:
            reasons.append("unsupported_candidate_role")
        else:
            row_frame = _frame_tuple(row_node.frame)
            if row_frame is None:
                reasons.append("candidate_frame_missing")
            elif row_frame[3] < PROJECT_CHAT_ROW_MIN_HEIGHT:
                reasons.append("row_container_frame_below_minimum")
    return list(dict.fromkeys(reasons))


def _diagnostic_experimental_canonical_title(candidates: list[dict]) -> dict:
    preview = ""
    for candidate in candidates:
        text = _normalized_label(candidate.get("raw_text") or "")
        attr = candidate.get("source_attribute") or ""
        if attr == "AXTitle" and _project_chat_canonical_title_eligible(text):
            return {"experimental_canonical_title": text, "experimental_preview": preview, "experimental_title_confidence": "high"}
    for candidate in candidates:
        text = _normalized_label(candidate.get("raw_text") or "")
        role = candidate.get("source_role") or ""
        if role in {"AXStaticText", "AXGroup", "AXRow", "AXCell"} and _project_chat_canonical_title_eligible(text):
            return {"experimental_canonical_title": text, "experimental_preview": preview, "experimental_title_confidence": "medium"}
    for candidate in candidates:
        text = _normalized_label(candidate.get("raw_text") or "")
        if candidate.get("source_attribute") == "AXValue" and _project_chat_canonical_title_eligible(text):
            return {"experimental_canonical_title": text, "experimental_preview": preview, "experimental_title_confidence": "medium"}
    for candidate in candidates:
        text = _normalized_label(candidate.get("raw_text") or "")
        if candidate.get("source_attribute") != "AXDescription" or ", " not in text:
            continue
        title, _sep, suffix = text.partition(", ")
        if _project_chat_canonical_title_eligible(title):
            return {
                "experimental_canonical_title": title,
                "experimental_preview": _truncate_project_preview(suffix),
                "experimental_title_confidence": "medium",
            }
    return {"experimental_canonical_title": "", "experimental_preview": "", "experimental_title_confidence": "none"}


def _diagnostic_filtered_band_indexes(bands: list[dict], contains_title: str) -> set[int]:
    if not contains_title:
        return {int(band["band_index"]) for band in bands}
    needle = contains_title.casefold()
    indexes = set()
    for band in bands:
        haystacks = []
        for node in band.get("nodes") or []:
            haystacks.extend([node.get("AXTitle") or "", node.get("AXDescription") or "", node.get("AXValue") or ""])
        for candidate in band.get("title_candidates") or []:
            haystacks.append(candidate.get("raw_text") or "")
        comparison = band.get("current_resolver_comparison") or {}
        canonical = band.get("experimental_canonical") or {}
        haystacks.extend([comparison.get("current_resolver_title") or "", canonical.get("experimental_canonical_title") or ""])
        if any(needle in text.casefold() for text in haystacks):
            indexes.add(int(band["band_index"]))
    return indexes


def _diagnostic_summary(result: dict, *, filtered_bands_printed: int, all_bands: list[dict] | None = None) -> dict:
    bands = all_bands if all_bands is not None else result.get("visual_row_bands") or []
    summary_source = result.get("collection_counts_before_filter") or {}
    return {
        "ax_nodes_inspected": int(summary_source.get("ax_nodes_inspected") or len(result.get("confirmed_list_nodes") or [])),
        "visual_bands_found": int(summary_source.get("visual_bands_found") or len(bands)),
        "bands_with_high_confidence_title": sum(1 for band in bands if (band.get("experimental_canonical") or {}).get("experimental_title_confidence") == "high"),
        "bands_accepted_by_current_resolver": sum(1 for band in bands if (band.get("current_resolver_comparison") or {}).get("current_resolver_status") == "accepted_currently"),
        "bands_not_seen_by_current_resolver": sum(1 for band in bands if (band.get("current_resolver_comparison") or {}).get("current_resolver_status") == "not_seen_by_current_resolver"),
        "bands_rejected_by_current_resolver": sum(1 for band in bands if (band.get("current_resolver_comparison") or {}).get("current_resolver_status") == "rejected_currently"),
        "filtered_bands_printed": filtered_bands_printed,
    }


def _base_project_chat_row_ax_audit_result(project_title: str, chat_titles: list[str], app_name: str) -> dict:
    return {
        "ok": False,
        "status": "inspection_unavailable",
        "app_name": app_name,
        "project_title": project_title,
        "chat_titles": chat_titles,
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "window_frame": _frame_geometry_report(None),
        "traversal": {},
        "project_resolution_status": "",
        "visible_chat_count": 0,
        "accepted_visible_chat_rows": [],
        "row_audits": [],
        "actions_performed": [],
        "read_only": True,
        "error": "",
    }


def _project_chat_row_ax_status_from_resolution(resolution: dict) -> str:
    status = resolution.get("status") or ""
    if status in PROJECT_CHAT_ROW_AX_AUDIT_OUTCOMES:
        return status
    return "inspection_unavailable"


def _project_chat_row_ax_audit_for_title(
    chat_title: str,
    visible_rows: list[dict],
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> dict:
    matches = []
    for row in visible_rows:
        row_path = row.get("row_path") or row.get("path") or ""
        row_snapshot = snapshots_by_path.get(row_path)
        row_text = _project_chat_row_flattened_text(row_snapshot, snapshots_by_path)
        if chat_title in row_text:
            matches.append((row, row_snapshot, row_text))
    if not matches:
        return {
            "status": "audit_row_not_found",
            "requested_chat_title": chat_title,
            "error": "No accepted visible project chat row subtree contained the requested audit title.",
        }
    if len(matches) > 1:
        return {
            "status": "audit_row_ambiguous",
            "requested_chat_title": chat_title,
            "match_count": len(matches),
            "matching_row_paths": [row.get("row_path") or row.get("path") or "" for row, _snapshot, _text in matches],
            "error": "More than one accepted visible project chat row subtree contained the requested audit title.",
        }
    row, row_snapshot, row_text = matches[0]
    if row_snapshot is None:
        return {
            "status": "audit_row_not_found",
            "requested_chat_title": chat_title,
            "error": "Accepted row path was not present in the retained AX snapshot.",
        }
    row_frame = _frame_tuple(row_snapshot.frame)
    nodes = _project_chat_row_audit_nodes(row_snapshot, snapshots_by_path, chat_title)
    return {
        "status": "row_audit_ready",
        "requested_chat_title": chat_title,
        "accepted_row": {
            "ordinal": row.get("ordinal"),
            "resolver_title": row.get("title") or "",
            "resolver_preview": row.get("preview") or "",
            "row_path": row_snapshot.path,
            "row_role": row_snapshot.role,
            "row_subrole": row_snapshot.subrole,
            "row_frame": _frame_geometry_report(row_frame),
        },
        "raw_flattened_row_text": row_text,
        "node_count": len(nodes),
        "nodes": nodes,
        "summary": _project_chat_row_audit_summary(nodes, chat_title),
    }


def _project_chat_row_flattened_text(
    row_snapshot: AXElementSnapshot | None,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> str:
    if row_snapshot is None:
        return ""
    texts = []
    for snapshot in _project_chat_row_subtree_snapshots(row_snapshot, snapshots_by_path, max_depth=4):
        text = _project_text(snapshot)
        if text:
            texts.append(text)
    return _normalized_label(", ".join(dict.fromkeys(texts)))


def _project_chat_row_subtree_snapshots(
    row_snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    *,
    max_depth: int,
) -> list[AXElementSnapshot]:
    row_frame = _frame_tuple(row_snapshot.frame)
    row_depth = row_snapshot.depth
    result = []
    for snapshot in sorted(snapshots_by_path.values(), key=lambda item: _path_sort_key(item.path)):
        if snapshot.path != row_snapshot.path and not snapshot.path.startswith(row_snapshot.path + "."):
            continue
        if snapshot.depth - row_depth > max_depth:
            continue
        frame = _frame_tuple(snapshot.frame)
        if snapshot.path != row_snapshot.path and not (
            _frame_contains_with_tolerance(row_frame, frame, FRAME_CONTAINMENT_TOLERANCE)
            or _frame_intersects(row_frame, frame)
        ):
            continue
        result.append(snapshot)
    return result


def _project_chat_row_audit_nodes(
    row_snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    chat_title: str,
) -> list[dict]:
    subtree = _project_chat_row_subtree_snapshots(row_snapshot, snapshots_by_path, max_depth=4)
    order_by_parent: Counter[str] = Counter()
    nodes = []
    for snapshot in subtree:
        parent = _parent_path(snapshot.path)
        child_index = int(order_by_parent[parent])
        order_by_parent[parent] += 1
        text = _project_text(snapshot)
        nodes.append(
            {
                "path": _bounded_text(snapshot.path, MAX_PATH_LENGTH),
                "relative_depth": snapshot.depth - row_snapshot.depth,
                "child_index": child_index,
                "parent_path": _bounded_text(parent, MAX_PATH_LENGTH),
                "role": _bounded_text(snapshot.role, MAX_ROLE_LENGTH),
                "subrole": _bounded_text(snapshot.subrole, MAX_ROLE_LENGTH),
                "AXTitle": _bounded_text(_normalized_label(snapshot.title), PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
                "AXValue": _bounded_text(_normalized_label(snapshot.value), PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
                "AXDescription": _bounded_text(_normalized_label(snapshot.description), PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH),
                "frame": _frame_geometry_report(snapshot.frame),
                "actions": _safe_actions(snapshot.actions),
                "text_classification": _project_chat_row_audit_text_classification(text, chat_title),
            }
        )
    return nodes


def _project_chat_row_audit_text_classification(text: str, chat_title: str) -> str:
    normalized = _normalized_label(text)
    if not normalized:
        return "empty"
    if _is_punctuation_or_separator_only(normalized):
        return "punctuation-only"
    if normalized == chat_title:
        return "title-like"
    return "preview-like"


def _project_chat_row_audit_summary(nodes: list[dict], chat_title: str) -> dict:
    title_nodes = [node for node in nodes if node.get("text_classification") == "title-like"]
    preview_nodes = [node for node in nodes if node.get("text_classification") == "preview-like"]
    punctuation_nodes = [node for node in nodes if node.get("text_classification") == "punctuation-only"]
    row_node = nodes[0] if nodes else {}
    row_text_values = [
        row_node.get("AXTitle") or "",
        row_node.get("AXValue") or "",
        row_node.get("AXDescription") or "",
    ]
    return {
        "row_exposes_requested_title_exactly": chat_title in row_text_values,
        "row_exposes_merged_text": any(chat_title in value and value != chat_title for value in row_text_values),
        "exact_title_node_paths": [node.get("path") for node in title_nodes],
        "preview_like_node_paths": [node.get("path") for node in preview_nodes],
        "punctuation_only_node_paths": [node.get("path") for node in punctuation_nodes],
    }


def resolve_open_project_content_and_visible_chats(
    requested_project_title: str,
    current_chatgpt_ax_tree: list[AXElementSnapshot],
    current_chatgpt_window_bounds: tuple[float, float, float, float] | None,
    *,
    traversal_stats: dict | None = None,
    window_metadata: dict | None = None,
) -> dict:
    requested_title = _normalized_label(requested_project_title)
    snapshots = list(current_chatgpt_ax_tree or [])
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    window_frame = _frame_tuple(current_chatgpt_window_bounds)
    result = _base_project_visible_chats_resolution(requested_title)
    result["window_frame"] = _frame_geometry_report(window_frame)
    result["traversal"] = {
        "visited_nodes": (traversal_stats or {}).get("visited_nodes", len(snapshots)),
        "emitted_nodes": len(snapshots),
        "max_depth": (traversal_stats or {}).get("max_depth"),
        "max_nodes": (traversal_stats or {}).get("max_nodes"),
        "truncated_by_node_limit": bool((traversal_stats or {}).get("truncated_by_node_limit")),
        "truncated_by_depth_limit": bool((traversal_stats or {}).get("truncated_by_depth_limit")),
    }
    result["window_metadata_source"] = (window_metadata or {}).get("window_source") or ""
    if not requested_title or not snapshots:
        result.update({"status": "inspection_unavailable", "error": "Project title or AX tree was unavailable."})
        return result

    sidebar = _project_navigation_sidebar_context(snapshots, snapshots_by_path, window_frame)
    identity = _resolve_open_project_identity(snapshots, snapshots_by_path, requested_title, window_frame, sidebar)
    result["excluded_candidate_counts"] = dict(identity.get("excluded_candidate_counts") or {})
    result["sidebar_title_seen"] = bool(identity.get("sidebar_title_seen"))
    result["chats_tab_confirmed"] = bool(identity.get("chats_tab_confirmed"))
    result["sources_tab_visible"] = bool(identity.get("sources_tab_visible"))
    if identity["status"] != "ok":
        result.update({"status": identity["status"], "error": identity.get("error", "")})
        return result

    result["project_identity_confirmed"] = True
    content = _project_content_context(identity["header"], snapshots, snapshots_by_path, window_frame, sidebar, identity.get("tab_context"))
    if content["status"] != "ok":
        result.update({"status": "project_chat_list_not_found", "error": content.get("error", "")})
        return result
    result["project_content_container"] = _project_container_report(content["content_container"])
    result["main_project_content"] = result["project_content_container"]

    # Forward-resolve the Chats-list container before any candidate row, anchored
    # structurally to the proved project content pane and the Chats/Sources tab
    # band. It is never reverse-derived from admitted rows.
    chats_list_container = _forward_resolve_project_chats_list_container(snapshots, snapshots_by_path, content, sidebar)
    tab_context = identity.get("tab_context") or {}
    result["chats_tab_active_evidence"] = _project_chats_tab_active_evidence(tab_context, snapshots_by_path)
    result["project_chat_list_container_path"] = _bounded_text(str(chats_list_container.get("path") or ""), MAX_PATH_LENGTH)
    result["project_chat_list_container_role"] = _bounded_text(str(chats_list_container.get("role") or ""), MAX_ROLE_LENGTH)
    result["project_chat_list_container_frame"] = _frame_geometry_report(_frame_tuple(chats_list_container.get("frame")))

    if not chats_list_container.get("path"):
        result["chat_list_container"] = _project_container_report({})
        result.update(_project_chat_list_identity_not_confirmed_fields(["no_forward_resolved_chats_list_container"], row_shape_status="insufficient_rows"))
        return result

    rows_result = _visible_project_chat_rows(
        snapshots,
        snapshots_by_path,
        requested_title,
        identity["header"],
        content,
        sidebar,
        chats_list_container,
    )
    result["excluded_candidate_counts"] = _merge_reason_counts(
        result["excluded_candidate_counts"],
        rows_result["excluded_candidate_counts"],
    )
    result["chat_list_container"] = _project_container_report(rows_result.get("chat_list_container") or chats_list_container)
    result["more_rows_may_exist_below"] = rows_result.get("more_rows_may_exist_below", "unknown")
    result["chats_area_confirmed"] = bool(rows_result.get("chats_area_confirmed"))
    result["project_chat_row_shape_status"] = rows_result.get("project_chat_row_shape_status") or "insufficient_rows"
    result["valid_project_chat_row_count"] = int(rows_result.get("valid_project_chat_row_count") or 0)
    result["invalid_candidate_count"] = int(rows_result.get("invalid_candidate_count") or 0)
    result["row_height_median"] = float(rows_result.get("row_height_median") or 0.0)
    result["vertical_peer_list_confirmed"] = bool(rows_result.get("vertical_peer_list_confirmed"))

    if rows_result["status"] == "contaminated_chat_list_candidates":
        reason = rows_result.get("row_shape_failure_reason") or ""
        if reason == "candidate_row_height_below_minimum":
            reasons = ["candidate_row_height_below_minimum"]
        elif reason in {"horizontal_alignment_inconsistent", "rows_not_vertically_ordered", "rows_overlap_vertically", "vertical_peer_list_not_confirmed"}:
            reasons = ["vertical_peer_list_not_confirmed"]
        else:
            reasons = ["candidate_rows_not_descendants_of_chats_list"]
        result.update(_project_chat_list_identity_not_confirmed_fields(reasons, row_shape_status="invalid"))
        return result

    if rows_result["status"] == "visible_chat_rows_not_found":
        # An empty but structurally proved Chats list is a legitimate state. It
        # cannot target a chat, so the existing empty-list outcome is preserved.
        result["project_chat_list_identity"] = "confirmed"
        result.update({"status": "visible_chat_rows_not_found", "error": rows_result.get("error", "")})
        return result

    if rows_result["status"] != "ok":
        result.update({"status": rows_result["status"], "error": rows_result.get("error", "")})
        return result

    evidence_failures = _project_chat_list_active_evidence_failures(result)
    if evidence_failures:
        result.update(_project_chat_list_identity_not_confirmed_fields(evidence_failures, row_shape_status="invalid"))
        return result

    result["project_chat_list_identity"] = "confirmed"
    result["visible_chats"] = rows_result["visible_chats"]
    result["visible_chat_count"] = len(result["visible_chats"])
    result.update({"ok": True, "status": "visible_chats_found", "error": ""})
    return result


def _project_chats_tab_active_evidence(tab_context: dict, snapshots_by_path: dict[str, AXElementSnapshot]) -> str:
    chats_path = str(tab_context.get("chats_tab_path") or "")
    snapshot = snapshots_by_path.get(chats_path)
    if snapshot is not None:
        if snapshot.selected is True:
            return "chats_tab_selected"
        if snapshot.focused is True:
            return "chats_tab_focused"
    if chats_path:
        return "chats_tab_present_with_resolved_list"
    return "structural_chats_list_below_tabs"


def _project_chat_list_active_evidence_failures(result: dict) -> list[str]:
    failures = []
    if not result.get("project_identity_confirmed"):
        failures.append("project_identity_not_confirmed")
    if not result.get("chats_tab_confirmed"):
        failures.append("chats_tab_not_present")
    if not result.get("sources_tab_visible"):
        failures.append("sources_tab_not_present")
    if not result.get("chats_area_confirmed"):
        failures.append("chats_list_container_not_confirmed")
    if int(result.get("valid_project_chat_row_count") or 0) < 1:
        failures.append("no_valid_project_chat_row")
    if not result.get("vertical_peer_list_confirmed"):
        failures.append("vertical_peer_list_not_confirmed")
    return failures


def _project_chat_list_identity_not_confirmed_fields(reasons: list[str], *, row_shape_status: str) -> dict:
    deduped = list(dict.fromkeys(reason for reason in reasons if reason))
    return {
        "ok": False,
        "status": "project_chat_list_identity_not_confirmed",
        "project_chat_list_identity": "not_confirmed",
        "project_chat_row_shape_status": row_shape_status,
        "identity_failure_reasons": deduped,
        "visible_chats": [],
        "visible_chat_count": 0,
        "chats_area_confirmed": False,
        "error": "Project Chats-list identity could not be confirmed: " + ", ".join(deduped),
    }


def _project_visible_chats_resolution_fields(resolution: dict) -> dict:
    return {
        "window_frame": resolution.get("window_frame") or _frame_geometry_report(None),
        "traversal": resolution.get("traversal") or {},
        "project_content_container": resolution.get("project_content_container") or _project_container_report({}),
        "main_project_content": resolution.get("main_project_content") or resolution.get("project_content_container") or _project_container_report({}),
        "chat_list_container": resolution.get("chat_list_container") or _project_container_report({}),
        "visible_chat_count": int(resolution.get("visible_chat_count") or 0),
        "visible_chats": resolution.get("visible_chats") or [],
        "more_rows_may_exist_below": resolution.get("more_rows_may_exist_below", "unknown"),
        "excluded_candidate_counts": resolution.get("excluded_candidate_counts") or {},
        "project_identity_confirmed": bool(resolution.get("project_identity_confirmed")),
        "chats_tab_confirmed": bool(resolution.get("chats_tab_confirmed")),
        "sources_tab_visible": bool(resolution.get("sources_tab_visible")),
        "chats_area_confirmed": bool(resolution.get("chats_area_confirmed")),
        "sidebar_title_seen": bool(resolution.get("sidebar_title_seen")),
        "project_chat_list_identity": resolution.get("project_chat_list_identity") or "not_confirmed",
        "project_chat_list_container_path": resolution.get("project_chat_list_container_path") or "",
        "project_chat_list_container_role": resolution.get("project_chat_list_container_role") or "",
        "project_chat_list_container_frame": resolution.get("project_chat_list_container_frame") or _frame_geometry_report(None),
        "project_chat_row_shape_status": resolution.get("project_chat_row_shape_status") or "insufficient_rows",
        "valid_project_chat_row_count": int(resolution.get("valid_project_chat_row_count") or 0),
        "invalid_candidate_count": int(resolution.get("invalid_candidate_count") or 0),
        "row_height_median": float(resolution.get("row_height_median") or 0.0),
        "vertical_peer_list_confirmed": bool(resolution.get("vertical_peer_list_confirmed")),
        "chats_tab_active_evidence": resolution.get("chats_tab_active_evidence") or "",
        "identity_stability_samples": int(resolution.get("identity_stability_samples") or 1),
        "identity_failure_reasons": list(resolution.get("identity_failure_reasons") or []),
    }


def _base_project_visible_chats_resolution(project_title: str) -> dict:
    return {
        "ok": False,
        "status": "inspection_unavailable",
        "project_title": project_title,
        "window_frame": _frame_geometry_report(None),
        "traversal": {},
        "project_content_container": _project_container_report({}),
        "main_project_content": _project_container_report({}),
        "chat_list_container": _project_container_report({}),
        "visible_chat_count": 0,
        "visible_chats": [],
        "more_rows_may_exist_below": "unknown",
        "excluded_candidate_counts": {},
        "project_identity_confirmed": False,
        "chats_tab_confirmed": False,
        "sources_tab_visible": False,
        "chats_area_confirmed": False,
        "sidebar_title_seen": False,
        "project_chat_list_identity": "not_confirmed",
        "project_chat_list_container_path": "",
        "project_chat_list_container_role": "",
        "project_chat_list_container_frame": _frame_geometry_report(None),
        "project_chat_row_shape_status": "insufficient_rows",
        "valid_project_chat_row_count": 0,
        "invalid_candidate_count": 0,
        "row_height_median": 0.0,
        "vertical_peer_list_confirmed": False,
        "chats_tab_active_evidence": "",
        "identity_stability_samples": 1,
        "identity_failure_reasons": [],
        "error": "",
    }


def _base_project_visible_chats_result(project_title: str, app_name: str) -> dict:
    return {
        "ok": False,
        "status": "inspection_unavailable",
        "app_name": app_name,
        "project_title": project_title,
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "window_frame": _frame_geometry_report(None),
        "traversal": {},
        "project_content_container": _project_container_report({}),
        "main_project_content": _project_container_report({}),
        "chat_list_container": _project_container_report({}),
        "visible_chat_count": 0,
        "visible_chats": [],
        "more_rows_may_exist_below": "unknown",
        "excluded_candidate_counts": {},
        "actions_performed": [],
        "read_only": True,
        "error": "",
    }


def _looks_like_accessibility_trust_error(text: str) -> bool:
    lowered = text.casefold()
    return any(token in lowered for token in ("accessibility", "trusted", "permission", "not authorized", "denied"))


def _project_navigation_sidebar_context(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
) -> dict:
    sections = _visible_navigation_sections(snapshots, snapshots_by_path)
    paths: set[str] = set()
    frames = []
    for section in sections:
        if section.get("purpose") not in {"projects", "chat_history", "navigation"}:
            continue
        path = str(section.get("container_path") or "")
        if not path:
            continue
        snapshot = snapshots_by_path.get(path)
        frame = _frame_tuple(snapshot.frame if snapshot else None)
        if not _project_frame_looks_like_sidebar(frame, window_frame):
            continue
        paths.add(path)
        frames.append(frame)
    for snapshot in snapshots:
        frame = _frame_tuple(snapshot.frame)
        if frame is None or window_frame is None:
            continue
        text = _normalized_label(snapshot.identifier or snapshot.title or snapshot.description or snapshot.value).casefold()
        if (
            ("sidebar" in text or "navigation" in text)
            and snapshot.role in CONTAINER_ROLES
            and _frame_contains(window_frame, frame)
            and _project_frame_looks_like_sidebar(frame, window_frame)
        ):
            paths.add(snapshot.path)
            frames.append(frame)
    return {
        "paths": sorted(paths, key=_path_sort_key),
        "frames": frames,
        "right_edge": max((frame[0] + frame[2] for frame in frames), default=None),
    }


def _project_frame_looks_like_sidebar(
    frame: tuple[float, float, float, float] | None,
    window_frame: tuple[float, float, float, float] | None,
) -> bool:
    frame = _frame_tuple(frame)
    window_frame = _frame_tuple(window_frame)
    if frame is None or window_frame is None:
        return False
    wx, _wy, ww, _wh = window_frame
    x, _y, width, _height = frame
    return x <= wx + 80.0 and width <= min(420.0, ww * 0.45)


def _resolve_open_project_identity(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    project_title: str,
    window_frame: tuple[float, float, float, float] | None,
    sidebar: dict,
) -> dict:
    excluded: Counter[str] = Counter()
    candidates = []
    unconfirmed_candidates = []
    sidebar_title_seen = False
    for snapshot in snapshots:
        if _project_text(snapshot) != project_title:
            continue
        if _project_snapshot_in_sidebar(snapshot, sidebar):
            sidebar_title_seen = True
            excluded["left_navigation_sidebar"] += 1
            continue
        frame = _project_header_frame(snapshot, snapshots_by_path)
        if not _frame_is_valid(frame) or not _frame_intersects(window_frame, frame):
            excluded["invalid_or_offscreen_frame"] += 1
            continue
        if snapshot.role in {"AXTextArea", "AXTextField", "AXScrollBar"}:
            excluded["input_or_bar"] += 1
            continue
        tab_context = _project_header_tab_context(snapshot, snapshots, snapshots_by_path, window_frame, sidebar)
        candidate = {"snapshot": snapshot, "frame": frame, "tab_context": tab_context}
        if tab_context.get("chats_tab_visible") and tab_context.get("sources_tab_visible"):
            candidates.append(candidate)
        else:
            unconfirmed_candidates.append(candidate)
    if not candidates:
        if unconfirmed_candidates:
            return {
                "status": "project_open_but_chats_tab_not_confirmed",
                "error": "The requested project title was found in the main content area, but Chats/Sources project tabs were not confirmed.",
                "excluded_candidate_counts": dict(excluded),
                "sidebar_title_seen": sidebar_title_seen,
                "chats_tab_confirmed": False,
                "sources_tab_visible": False,
            }
        return {
            "status": "project_not_open",
            "error": "The requested project title was not found in the main project content/header area.",
            "excluded_candidate_counts": dict(excluded),
            "sidebar_title_seen": sidebar_title_seen,
            "chats_tab_confirmed": False,
            "sources_tab_visible": False,
        }
    if len(candidates) > 1:
        return {
            "status": "project_identity_ambiguous",
            "error": "More than one matching project title was found in the main content/header area.",
            "excluded_candidate_counts": dict(excluded),
            "sidebar_title_seen": sidebar_title_seen,
            "chats_tab_confirmed": False,
            "sources_tab_visible": False,
        }
    chosen = candidates[0]
    return {
        "status": "ok",
        "header": chosen["snapshot"],
        "header_frame": chosen["frame"],
        "tab_context": chosen["tab_context"],
        "excluded_candidate_counts": dict(excluded),
        "sidebar_title_seen": sidebar_title_seen,
        "chats_tab_confirmed": bool(chosen["tab_context"].get("chats_tab_visible")),
        "sources_tab_visible": bool(chosen["tab_context"].get("sources_tab_visible")),
    }


def _project_header_frame(
    snapshot: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> tuple[float, float, float, float] | None:
    frame = _frame_tuple(snapshot.frame)
    if _frame_is_valid(frame):
        return frame
    for ancestor_path in _ancestor_paths(snapshot.path):
        ancestor = snapshots_by_path.get(ancestor_path)
        ancestor_frame = _frame_tuple(ancestor.frame if ancestor else None)
        if _frame_is_valid(ancestor_frame):
            return ancestor_frame
    return None


def _project_header_tab_context(
    header: AXElementSnapshot,
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
    sidebar: dict,
) -> dict:
    header_frame = _project_header_frame(header, snapshots_by_path)
    if header_frame is None:
        return {"chats_tab_visible": False, "sources_tab_visible": False, "tabs_bottom": 0.0}
    tabs: dict[str, AXElementSnapshot] = {}
    for snapshot in snapshots:
        text = _project_text(snapshot)
        if text not in {"Chats", "Sources"}:
            continue
        frame = _frame_tuple(snapshot.frame)
        if not _frame_is_valid(frame) or _project_snapshot_in_sidebar(snapshot, sidebar):
            continue
        if not _frame_intersects(window_frame, frame):
            continue
        if not _project_tab_geometrically_follows_header(frame, header_frame, sidebar):
            continue
        existing = tabs.get(text)
        if existing is None or _frame_tuple(snapshot.frame)[1] < _frame_tuple(existing.frame)[1]:
            tabs[text] = snapshot
    tabs_bottom = max((_frame_tuple(tab.frame)[1] + _frame_tuple(tab.frame)[3] for tab in tabs.values()), default=0.0)
    return {
        "chats_tab_visible": "Chats" in tabs,
        "sources_tab_visible": "Sources" in tabs,
        "tabs_bottom": tabs_bottom,
        "chats_tab_path": (tabs.get("Chats").path if tabs.get("Chats") else ""),
        "sources_tab_path": (tabs.get("Sources").path if tabs.get("Sources") else ""),
    }


def _project_tab_geometrically_follows_header(
    tab_frame: tuple[float, float, float, float] | None,
    header_frame: tuple[float, float, float, float] | None,
    sidebar: dict,
) -> bool:
    tab_frame = _frame_tuple(tab_frame)
    header_frame = _frame_tuple(header_frame)
    if tab_frame is None or header_frame is None:
        return False
    sidebar_right = sidebar.get("right_edge")
    if sidebar_right is not None and tab_frame[0] < float(sidebar_right) - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
        return False
    header_bottom = header_frame[1] + header_frame[3]
    tab_mid_y = tab_frame[1] + tab_frame[3] / 2.0
    if tab_mid_y < header_frame[1] - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
        return False
    if tab_frame[1] > header_bottom + 180.0:
        return False
    header_left = header_frame[0]
    header_right = header_frame[0] + max(header_frame[2], 360.0)
    tab_mid_x = tab_frame[0] + tab_frame[2] / 2.0
    return header_left - 80.0 <= tab_mid_x <= header_right + 260.0


def _project_content_context(
    header: AXElementSnapshot,
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
    sidebar: dict,
    tab_context: dict | None = None,
) -> dict:
    header_frame = _project_header_frame(header, snapshots_by_path)
    content_container = _project_content_container(header, snapshots_by_path, window_frame, sidebar)
    if not content_container:
        return {"status": "missing", "error": "Could not resolve a project content container around the project header."}
    content_frame = _frame_tuple(content_container.get("frame"))
    tab_bottom = float((tab_context or {}).get("tabs_bottom") or 0.0) or _project_tabs_bottom(snapshots, content_frame, header_frame, sidebar)
    header_bottom = header_frame[1] + header_frame[3] if header_frame else (content_frame[1] if content_frame else 0.0)
    # Current ChatGPT builds can expose the project name as AXWindow.AXTitle
    # instead of a project heading within the content pane.  The window bottom
    # is not a content header boundary; once Chats/Sources are independently
    # proved, their tab band is the safe lower boundary for the chat list.
    if header.role == "AXWindow" and tab_bottom > 0.0:
        list_top = tab_bottom
    else:
        list_top = max(header_bottom, tab_bottom)
    fallback_list = {
        "path": content_container.get("path") or "",
        "role": content_container.get("role") or "",
        "subrole": content_container.get("subrole") or "",
        "frame": content_frame,
    }
    return {
        "status": "ok",
        "header": header,
        "content_container": content_container,
        "chat_list_container": fallback_list,
        "content_frame": content_frame,
        "list_top": list_top,
    }


def _project_content_container(
    header: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
    sidebar: dict,
) -> dict:
    header_frame = _project_header_frame(header, snapshots_by_path)
    candidates = []
    for ancestor_path in _ancestor_paths(header.path):
        if ancestor_path == "W":
            break
        snapshot = snapshots_by_path.get(ancestor_path)
        frame = _frame_tuple(snapshot.frame if snapshot else None)
        if snapshot is None or not _frame_is_valid(frame):
            continue
        if _project_snapshot_in_sidebar(snapshot, sidebar):
            continue
        if not _frame_contains_with_tolerance(frame, header_frame, FRAME_CONTAINMENT_TOLERANCE):
            continue
        if window_frame is not None and not _frame_intersects(window_frame, frame):
            continue
        candidates.append(snapshot)
    if candidates:
        chosen = sorted(
            candidates,
            key=lambda item: (
                0 if item.role in {"AXScrollArea", "AXGroup", "AXList"} else 1,
                -_frame_area(_frame_tuple(item.frame)),
                _path_sort_key(item.path),
            ),
        )[0]
        return {"path": chosen.path, "role": chosen.role, "subrole": chosen.subrole, "frame": _frame_tuple(chosen.frame)}
    if window_frame is not None:
        return {"path": "W", "role": "AXWindow", "subrole": "", "frame": window_frame}
    return {}


def _project_tabs_bottom(
    snapshots: list[AXElementSnapshot],
    content_frame: tuple[float, float, float, float] | None,
    header_frame: tuple[float, float, float, float] | None,
    sidebar: dict,
) -> float:
    if content_frame is None or header_frame is None:
        return 0.0
    header_bottom = header_frame[1] + header_frame[3]
    bottoms = []
    for snapshot in snapshots:
        text = _project_text(snapshot)
        if text not in {"Chats", "Sources"}:
            continue
        frame = _frame_tuple(snapshot.frame)
        if frame is None or not _frame_intersects(content_frame, frame) or _project_snapshot_in_sidebar(snapshot, sidebar):
            continue
        if frame[1] + frame[3] >= header_bottom - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            bottoms.append(frame[1] + frame[3])
    return max(bottoms, default=header_bottom)


def _forward_resolve_project_chats_list_container(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    content: dict,
    sidebar: dict,
) -> dict:
    """Independently resolve the project Chats-list container *before* any row.

    The container is resolved forward from the proved project content pane and the
    Chats/Sources tab band, never reverse-derived from admitted rows. The whole AX
    window, the sidebar, the project header/tab band, and composer/input/transcript
    regions are explicitly rejected as Chats-list sources.
    """
    content_container = content.get("content_container") or {}
    content_path = str(content_container.get("path") or "")
    content_frame = _frame_tuple(content.get("content_frame"))
    list_top = float(content.get("list_top") or 0.0)
    if content_frame is None or not content_path:
        return {}
    candidates = []
    for snapshot in snapshots:
        if snapshot.role not in PROJECT_CHATS_LIST_CONTAINER_ROLES:
            continue
        if snapshot.path == "W" or snapshot.role == "AXWindow":
            continue
        if snapshot.path == content_path:
            continue
        frame = _frame_tuple(snapshot.frame)
        if not _frame_is_valid(frame):
            continue
        within_content = snapshot.path.startswith(content_path + ".") or _frame_contains_with_tolerance(content_frame, frame, FRAME_CONTAINMENT_TOLERANCE)
        if not within_content:
            continue
        if _project_snapshot_in_sidebar(snapshot, sidebar):
            continue
        if frame[1] < list_top - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            continue
        if frame[3] < PROJECT_CHATS_LIST_MIN_HEIGHT or frame[2] < PROJECT_CHATS_LIST_MIN_WIDTH:
            continue
        if _project_container_is_input_or_transcript(snapshot, snapshots):
            continue
        row_like = _project_container_rowlike_descendant_count(snapshot, snapshots)
        candidates.append((snapshot, frame, row_like))
    if not candidates:
        return {}

    def _rank(item: tuple[AXElementSnapshot, tuple[float, float, float, float], int]):
        snapshot, frame, row_like = item
        role_rank = 0 if snapshot.role in {"AXScrollArea", "AXList", "AXTable", "AXOutline"} else 1
        return (role_rank, -row_like, -frame[3], frame[1], _path_sort_key(snapshot.path))

    chosen = sorted(candidates, key=_rank)[0][0]
    return {
        "path": chosen.path,
        "role": chosen.role,
        "subrole": chosen.subrole,
        "frame": _frame_tuple(chosen.frame),
    }


def _project_container_is_input_or_transcript(
    container: AXElementSnapshot,
    snapshots: list[AXElementSnapshot],
) -> bool:
    own_label = _normalized_label(container.identifier or container.title or container.description).casefold()
    if own_label and any(token in own_label for token in PROJECT_CHAT_TRANSCRIPT_IDENTIFIER_TOKENS):
        return True
    prefix = container.path + "."
    for snapshot in snapshots:
        if not snapshot.path.startswith(prefix):
            continue
        if snapshot.role in PROJECT_CHAT_INPUT_OR_TRANSCRIPT_ROLES:
            return True
        if snapshot.role in TEXTLIKE_ROLES and _looks_like_message_or_code(_project_text(snapshot)):
            return True
    return False


def _project_container_rowlike_descendant_count(
    container: AXElementSnapshot,
    snapshots: list[AXElementSnapshot],
) -> int:
    prefix = container.path + "."
    count = 0
    for snapshot in snapshots:
        if not snapshot.path.startswith(prefix):
            continue
        frame = _frame_tuple(snapshot.frame)
        if not _frame_is_valid(frame) or frame[3] < PROJECT_CHAT_ROW_MIN_HEIGHT:
            continue
        if snapshot.role in {"AXButton", "AXGroup", "AXCell", "AXRow", "AXLink"} or "AXPress" in _safe_actions(snapshot.actions):
            count += 1
    return count


def _project_node_within_container(node: AXElementSnapshot, container_path: str) -> bool:
    return bool(container_path) and node.path.startswith(container_path + ".")


def _project_node_within_container_path(node_path: str, container_path: str) -> bool:
    return bool(container_path) and bool(node_path) and str(node_path).startswith(str(container_path) + ".")


def _project_node_in_input_or_transcript(
    node: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> bool:
    for path in [node.path] + _ancestor_paths(node.path):
        snapshot = snapshots_by_path.get(path)
        if snapshot is None:
            continue
        if snapshot.role in PROJECT_CHAT_INPUT_OR_TRANSCRIPT_ROLES:
            return True
    return False


def _project_row_height_median(rows: list[dict]) -> float:
    heights = sorted(
        _frame_tuple(row["row_node"].frame)[3]
        for row in rows
        if _frame_tuple(row["row_node"].frame) is not None
    )
    if not heights:
        return 0.0
    return round(float(heights[len(heights) // 2]), 2)


def _project_vertical_peer_list_status(rows: list[dict]) -> dict:
    frames = [frame for frame in (_frame_tuple(row["row_node"].frame) for row in rows) if frame is not None]
    if not frames:
        return {"confirmed": False, "reason": "no_rows"}
    if len(frames) == 1:
        return {"confirmed": True, "reason": ""}
    ordered = sorted(frames, key=lambda frame: frame[1])
    xs = sorted(frame[0] for frame in ordered)
    median_x = xs[len(xs) // 2]
    if any(abs(frame[0] - median_x) > PROJECT_CHAT_ROW_ALIGNMENT_TOLERANCE for frame in ordered):
        return {"confirmed": False, "reason": "horizontal_alignment_inconsistent"}
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt[1] <= prev[1] + PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            return {"confirmed": False, "reason": "rows_not_vertically_ordered"}
        if nxt[1] < prev[1] + prev[3] * 0.4:
            return {"confirmed": False, "reason": "rows_overlap_vertically"}
    return {"confirmed": True, "reason": ""}


def _visible_project_chat_rows(
    snapshots: list[AXElementSnapshot],
    snapshots_by_path: dict[str, AXElementSnapshot],
    project_title: str,
    header: AXElementSnapshot,
    content: dict,
    sidebar: dict,
    chats_list_container: dict,
) -> dict:
    excluded: Counter[str] = Counter()
    container_path = str(chats_list_container.get("path") or "")
    container_frame = _frame_tuple(chats_list_container.get("frame"))
    content_frame = _frame_tuple(content.get("content_frame"))
    viewport_frame = container_frame or content_frame
    list_top = float(content.get("list_top") or 0.0)
    raw_rows: dict[str, dict] = {}
    invalid_candidates = 0
    for title_node in snapshots:
        title_info = _project_visible_row_title_info(title_node)
        title_text = title_info.get("title") or ""
        if not title_text:
            excluded["not_row_title_text"] += 1
            continue
        if title_text in {project_title, "Chats", "Sources"} or _is_generic_visible_navigation_title(title_text):
            excluded["header_tab_or_control"] += 1
            continue
        if _project_snapshot_in_sidebar(title_node, sidebar):
            excluded["left_navigation_sidebar"] += 1
            continue
        if not _project_node_within_container(title_node, container_path):
            excluded["outside_forward_resolved_chats_list"] += 1
            continue
        title_frame = _frame_tuple(title_node.frame)
        if not _frame_is_valid(title_frame) or not _frame_intersects(viewport_frame, title_frame):
            excluded["outside_project_content_viewport"] += 1
            continue
        if title_frame[1] < list_top - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            excluded["above_project_chat_list"] += 1
            continue
        row_node = _project_chat_row_container(title_node, snapshots_by_path, viewport_frame, list_top, sidebar)
        if row_node is None or not _project_node_within_container(row_node, container_path):
            excluded["row_outside_chats_list_container"] += 1
            continue
        if _project_node_in_input_or_transcript(row_node, snapshots_by_path):
            excluded["composer_or_transcript_subtree"] += 1
            continue
        row_frame = _frame_tuple(row_node.frame)
        if row_frame is None or not _frame_intersects(viewport_frame, row_frame):
            excluded["row_not_visible"] += 1
            continue
        if row_frame[1] < list_top - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            excluded["above_project_chat_list"] += 1
            continue
        if row_frame[3] < PROJECT_CHAT_ROW_MIN_HEIGHT:
            excluded["row_height_below_minimum"] += 1
            invalid_candidates += 1
            continue
        existing = raw_rows.get(row_node.path)
        candidate = {
            "row_node": row_node,
            "title_node": title_node,
            "title": title_text,
            "preview": title_info.get("preview") or _project_row_preview(row_node, title_node, snapshots, project_title),
            "title_source_attribute": title_info.get("source_attribute") or "",
            "title_representation": title_info.get("title_representation") or "",
            "preview_representation": title_info.get("preview_representation") or "",
        }
        if existing is None or title_frame[1] < _frame_tuple(existing["title_node"].frame)[1]:
            raw_rows[row_node.path] = candidate

    rows = list(raw_rows.values())
    rows = _filter_project_chat_row_geometry(rows)
    peer_status = _project_vertical_peer_list_status(rows)
    row_height_median = _project_row_height_median(rows)
    base_diagnostics = {
        "excluded_candidate_counts": dict(excluded),
        "chat_list_container": chats_list_container,
        "valid_project_chat_row_count": len(rows),
        "invalid_candidate_count": invalid_candidates,
        "row_height_median": row_height_median,
        "vertical_peer_list_confirmed": bool(peer_status["confirmed"]),
    }
    if not rows:
        # An empty but structurally proved Chats list is a legitimate state; a
        # populated frame whose every candidate failed the row-shape gate is
        # contamination and must fail closed.
        contaminated = invalid_candidates > 0
        return {
            **base_diagnostics,
            "status": "contaminated_chat_list_candidates" if contaminated else "visible_chat_rows_not_found",
            "error": (
                "Project content candidates failed the structural chat-row gate."
                if contaminated
                else "No visible project chat rows were found beneath the resolved Chats-list container."
            ),
            "visible_chats": [],
            "more_rows_may_exist_below": _project_more_rows_indicator([], snapshots, chats_list_container, content),
            "chats_area_confirmed": not contaminated,
            "project_chat_row_shape_status": "invalid" if contaminated else "insufficient_rows",
            "row_shape_failure_reason": "candidate_row_height_below_minimum" if contaminated else "",
        }
    if not peer_status["confirmed"]:
        return {
            **base_diagnostics,
            "status": "contaminated_chat_list_candidates",
            "error": "Resolved candidate rows did not form a vertically ordered project chat list.",
            "visible_chats": [],
            "more_rows_may_exist_below": _project_more_rows_indicator([], snapshots, chats_list_container, content),
            "chats_area_confirmed": False,
            "project_chat_row_shape_status": "invalid",
            "row_shape_failure_reason": peer_status.get("reason") or "vertical_peer_list_not_confirmed",
        }
    rows.sort(key=lambda item: (_frame_tuple(item["row_node"].frame)[1], _frame_tuple(item["row_node"].frame)[0], _path_sort_key(item["row_node"].path)))
    visible_chats = [
        _project_visible_chat_row_report(index, row, viewport_frame)
        for index, row in enumerate(rows, start=1)
    ]
    return {
        **base_diagnostics,
        "status": "ok",
        "visible_chats": visible_chats,
        "more_rows_may_exist_below": _project_more_rows_indicator(rows, snapshots, chats_list_container, content),
        "chats_area_confirmed": True,
        "project_chat_row_shape_status": "valid",
        "row_shape_failure_reason": "",
    }


def _project_text(snapshot: AXElementSnapshot) -> str:
    return _normalized_label(snapshot.title or snapshot.description or ("" if snapshot.role in VALUE_LENGTH_ONLY_ROLES else snapshot.value) or snapshot.identifier)


def _project_visible_row_title(snapshot: AXElementSnapshot) -> str:
    return _project_visible_row_title_info(snapshot).get("title") or ""


def _project_visible_row_title_info(snapshot: AXElementSnapshot) -> dict:
    empty = {
        "title": "",
        "preview": "",
        "raw_text": "",
        "source_attribute": "",
        "title_representation": "unresolved",
        "preview_representation": "unavailable",
    }
    if snapshot.role in PROJECT_CHAT_NON_ROW_CONTROL_ROLES or snapshot.role in {"AXTextArea", "AXTextField"}:
        return empty
    if snapshot.role not in TEXTLIKE_ROLES and snapshot.role not in {"AXButton", "AXLink"}:
        return empty
    if snapshot.role in {"AXButton", "AXLink"} and not (snapshot.title or snapshot.description or snapshot.value):
        return empty
    text = _project_chat_accessibility_text_parts(snapshot)
    title = text.get("title") or ""
    if not _project_chat_canonical_title_eligible(title):
        return empty
    return text


def _project_chat_accessibility_text_parts(snapshot: AXElementSnapshot) -> dict:
    title = _normalized_label(snapshot.title)
    if title:
        return {
            "title": title,
            "preview": "",
            "raw_text": title,
            "source_attribute": "AXTitle",
            "title_representation": "exact_axtitle",
            "preview_representation": "unavailable",
        }
    description = _normalized_label(snapshot.description)
    if description:
        canonical = description
        preview = ""
        title_representation = "exact_accessibility_text"
        preview_representation = "unavailable"
        if ", " in description:
            prefix, _separator, suffix = description.partition(", ")
            canonical = _normalized_label(prefix)
            preview = _truncate_project_preview(suffix)
            title_representation = "canonical_accessibility_description_prefix"
            preview_representation = "merged_accessibility_suffix"
        return {
            "title": canonical,
            "preview": preview,
            "raw_text": description,
            "source_attribute": "AXDescription",
            "title_representation": title_representation,
            "preview_representation": preview_representation,
        }
    value = _normalized_label("" if snapshot.role in VALUE_LENGTH_ONLY_ROLES else snapshot.value)
    if value and _project_chat_canonical_title_eligible(value):
        return {
            "title": value,
            "preview": "",
            "raw_text": value,
            "source_attribute": "AXValue",
            "title_representation": "exact_axvalue",
            "preview_representation": "unavailable",
        }
    return {
        "title": "",
        "preview": "",
        "raw_text": "",
        "source_attribute": "",
        "title_representation": "unresolved",
        "preview_representation": "unavailable",
    }


def _project_chat_canonical_title_eligible(title: str) -> bool:
    text = _normalized_label(title)
    if not text or len(text) > PROJECT_VISIBLE_CHAT_TITLE_MAX_LENGTH:
        return False
    if _is_punctuation_or_separator_only(text):
        return False
    if _is_generic_visible_navigation_title(text):
        return False
    if _looks_like_message_or_code(text):
        return False
    return True


def _is_punctuation_or_separator_only(text: str) -> bool:
    normalized = _normalized_label(text)
    if not normalized:
        return True
    return re.search(r"[A-Za-z0-9]", normalized) is None


def _project_snapshot_in_sidebar(snapshot: AXElementSnapshot, sidebar: dict) -> bool:
    paths = sidebar.get("paths") or []
    if any(snapshot.path == path or snapshot.path.startswith(path + ".") for path in paths):
        return True
    frame = _frame_tuple(snapshot.frame)
    return frame is not None and any(_frame_contains_with_tolerance(sidebar_frame, frame, FRAME_CONTAINMENT_TOLERANCE) for sidebar_frame in sidebar.get("frames") or [])


def _project_chat_row_container(
    title_node: AXElementSnapshot,
    snapshots_by_path: dict[str, AXElementSnapshot],
    content_frame: tuple[float, float, float, float] | None,
    list_top: float,
    sidebar: dict,
) -> AXElementSnapshot | None:
    title_frame = _frame_tuple(title_node.frame)
    candidates = []
    for ancestor_path in _ancestor_paths(title_node.path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot is None or snapshot.path == "W":
            break
        frame = _frame_tuple(snapshot.frame)
        if not _frame_is_valid(frame) or not _frame_intersects(content_frame, frame):
            continue
        if _project_snapshot_in_sidebar(snapshot, sidebar):
            continue
        if frame[1] < list_top - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            continue
        if not _frame_contains_with_tolerance(frame, title_frame, FRAME_CONTAINMENT_TOLERANCE):
            continue
        if snapshot.role in {"AXButton", "AXGroup", "AXCell", "AXRow", "AXLink"} or "AXPress" in _safe_actions(snapshot.actions):
            candidates.append(snapshot)
    if candidates:
        actionable_rows = [
            candidate
            for candidate in candidates
            if _project_direct_actionable_chat_row_candidate(candidate, content_frame, list_top, sidebar)
        ]
        if actionable_rows:
            return sorted(actionable_rows, key=lambda item: (-_frame_area(_frame_tuple(item.frame)), _path_sort_key(item.path)))[0]
        return sorted(candidates, key=lambda item: (_frame_area(_frame_tuple(item.frame)), -len(item.path)))[0]
    if _project_direct_actionable_chat_row_candidate(title_node, content_frame, list_top, sidebar):
        return title_node
    return title_node if title_node.role in {"AXButton", "AXLink"} and _frame_is_valid(title_node.frame) else None


def _project_direct_actionable_chat_row_candidate(
    snapshot: AXElementSnapshot,
    viewport_frame: tuple[float, float, float, float] | None,
    list_top: float,
    sidebar: dict,
) -> bool:
    if snapshot.role in PROJECT_CHAT_NON_ROW_CONTROL_ROLES:
        return False
    if snapshot.role not in {"AXButton", "AXLink"}:
        return False
    if "AXPress" not in _safe_actions(snapshot.actions):
        return False
    frame = _frame_tuple(snapshot.frame)
    if not _frame_is_valid(frame) or not _frame_intersects(viewport_frame, frame):
        return False
    if frame[1] < list_top - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
        return False
    if frame[3] < PROJECT_CHAT_ROW_MIN_HEIGHT:
        return False
    if _project_snapshot_in_sidebar(snapshot, sidebar):
        return False
    return bool(_project_visible_row_title(snapshot))


def _project_row_preview(
    row_node: AXElementSnapshot,
    title_node: AXElementSnapshot,
    snapshots: list[AXElementSnapshot],
    project_title: str,
) -> str:
    row_frame = _frame_tuple(row_node.frame)
    title_frame = _frame_tuple(title_node.frame)
    if row_frame is None or title_frame is None:
        return ""
    previews = []
    for snapshot in snapshots:
        if snapshot.path == title_node.path or not snapshot.path.startswith(row_node.path + "."):
            continue
        if snapshot.role not in TEXTLIKE_ROLES or snapshot.role in VALUE_LENGTH_ONLY_ROLES:
            continue
        text = _project_text(snapshot)
        if not text or text == project_title or text in {"Chats", "Sources"} or text == _project_text(title_node):
            continue
        frame = _frame_tuple(snapshot.frame)
        if frame is None or not _frame_contains_with_tolerance(row_frame, frame, FRAME_CONTAINMENT_TOLERANCE):
            continue
        if frame[1] <= title_frame[1]:
            continue
        previews.append((frame[1], _truncate_project_preview(text)))
    if not previews:
        return ""
    return sorted(previews, key=lambda item: item[0])[0][1]


def _truncate_project_preview(text: str) -> str:
    normalized = _normalized_label(text)
    if len(normalized) <= PROJECT_VISIBLE_CHAT_PREVIEW_MAX_LENGTH:
        return normalized
    return normalized[: PROJECT_VISIBLE_CHAT_PREVIEW_MAX_LENGTH - 3].rstrip() + "..."


def _filter_project_chat_row_geometry(rows: list[dict]) -> list[dict]:
    if len(rows) <= 2:
        return rows
    xs = sorted(_frame_tuple(row["row_node"].frame)[0] for row in rows if _frame_tuple(row["row_node"].frame) is not None)
    widths = sorted(_frame_tuple(row["row_node"].frame)[2] for row in rows if _frame_tuple(row["row_node"].frame) is not None)
    median_x = xs[len(xs) // 2]
    median_width = widths[len(widths) // 2]
    filtered = []
    for row in rows:
        frame = _frame_tuple(row["row_node"].frame)
        if frame is None:
            continue
        width_tolerance = max(80.0, median_width * 0.45)
        if abs(frame[0] - median_x) <= 80.0 and abs(frame[2] - median_width) <= width_tolerance:
            filtered.append(row)
    return filtered or rows


def _project_chat_list_container(rows: list[dict], snapshots_by_path: dict[str, AXElementSnapshot], content: dict) -> dict:
    row_paths = [row["row_node"].path for row in rows]
    common = _common_ancestor_path(row_paths)
    for path in [common] + _ancestor_paths(common):
        snapshot = snapshots_by_path.get(path)
        frame = _frame_tuple(snapshot.frame if snapshot else None)
        if snapshot is None or not _frame_is_valid(frame):
            continue
        if snapshot.path == "W":
            break
        if snapshot.role in {"AXScrollArea", "AXList", "AXTable", "AXOutline", "AXGroup"}:
            return {"path": snapshot.path, "role": snapshot.role, "subrole": snapshot.subrole, "frame": frame}
    return content.get("chat_list_container") or content.get("content_container") or {}


def _project_empty_chat_list_container(
    snapshots: list[AXElementSnapshot],
    content: dict,
    sidebar: dict,
) -> dict:
    content_frame = _frame_tuple(content.get("content_frame"))
    list_top = float(content.get("list_top") or 0.0)
    candidates = []
    for snapshot in snapshots:
        frame = _frame_tuple(snapshot.frame)
        if snapshot.role not in {"AXScrollArea", "AXList", "AXTable", "AXOutline", "AXGroup"}:
            continue
        if not _frame_is_valid(frame) or not _frame_intersects(content_frame, frame):
            continue
        if _project_snapshot_in_sidebar(snapshot, sidebar):
            continue
        if frame[1] < list_top - PROJECT_CHAT_ROW_GEOMETRY_TOLERANCE:
            continue
        if frame[3] < 40.0 or frame[2] < 120.0:
            continue
        candidates.append(snapshot)
    if not candidates:
        return {}
    chosen = sorted(candidates, key=lambda item: (-_frame_area(_frame_tuple(item.frame)), _path_sort_key(item.path)))[0]
    return {"path": chosen.path, "role": chosen.role, "subrole": chosen.subrole, "frame": _frame_tuple(chosen.frame)}


def _common_ancestor_path(paths: list[str]) -> str:
    if not paths:
        return ""
    split_paths = [path.split(".") for path in paths]
    common = []
    for parts in zip(*split_paths):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return ".".join(common)


def _project_visible_chat_row_report(index: int, row: dict, viewport_frame: tuple[float, float, float, float] | None) -> dict:
    row_node = row["row_node"]
    title_node = row["title_node"]
    row_frame = _frame_tuple(row_node.frame)
    title_frame = _frame_tuple(title_node.frame)
    row_actions = _safe_actions(row_node.actions)
    title_actions = _safe_actions(title_node.actions)
    accessibility_text = _project_accessibility_row_text(row_node, title_node)
    display = _project_chat_row_display_title(row, accessibility_text, row_node, title_node)
    return {
        "ordinal": index,
        "title": display["title"],
        "display_title_source": display["source"],
        "title_source_attribute": row.get("title_source_attribute") or "",
        "preview": row.get("preview") or "",
        "accessibility_row_text": accessibility_text,
        "title_representation": display["title_representation"],
        "preview_representation": display["preview_representation"],
        "path": _bounded_text(row_node.path, MAX_PATH_LENGTH),
        "row_path": _bounded_text(row_node.path, MAX_PATH_LENGTH),
        "title_path": _bounded_text(title_node.path, MAX_PATH_LENGTH),
        "role": _bounded_text(row_node.role, MAX_ROLE_LENGTH),
        "subrole": _bounded_text(row_node.subrole, MAX_ROLE_LENGTH),
        "row_role": _bounded_text(row_node.role, MAX_ROLE_LENGTH),
        "row_subrole": _bounded_text(row_node.subrole, MAX_ROLE_LENGTH),
        "title_role": _bounded_text(title_node.role, MAX_ROLE_LENGTH),
        "title_subrole": _bounded_text(title_node.subrole, MAX_ROLE_LENGTH),
        "row_frame": _frame_geometry_report(row_frame),
        "title_frame": _frame_geometry_report(title_frame),
        "visibility": "fully_visible" if _frame_contains(viewport_frame, row_frame) else "partially_clipped",
        "actions": row_actions,
        "action_names": row_actions,
        "row_action_names": row_actions,
        "title_action_names": title_actions,
        "ax_press_available": "AXPress" in row_actions or "AXPress" in title_actions,
    }


def _project_accessibility_row_text(row_node: AXElementSnapshot, title_node: AXElementSnapshot) -> str:
    row_text = _project_text(row_node)
    if row_text:
        return row_text
    if row_node.path != title_node.path:
        return _project_text(title_node)
    return ""


def _project_chat_row_display_title(
    row: dict,
    accessibility_text: str,
    row_node: AXElementSnapshot,
    title_node: AXElementSnapshot,
) -> dict:
    resolved_title = _normalized_label(row.get("title") or "")
    if resolved_title:
        return {
            "title": resolved_title,
            "source": row.get("title_source_attribute") or "canonical_accessibility_text",
            "title_representation": row.get("title_representation") or "canonical_accessibility_text",
            "preview_representation": row.get("preview_representation") or "unavailable",
        }
    text = _normalized_label(accessibility_text)
    if text and row_node.path == title_node.path and ", " in text:
        prefix, _separator, _suffix = text.partition(", ")
        prefix = _normalized_label(prefix)
        if prefix and not _is_punctuation_or_separator_only(prefix):
            return {
                "title": prefix,
                "source": "merged_accessibility_description_prefix",
                "title_representation": "unresolved",
                "preview_representation": "merged_accessibility_suffix",
            }
    if text and text == resolved_title:
        return {
            "title": resolved_title,
            "source": "exact_accessibility_text",
            "title_representation": "exact_accessibility_text",
            "preview_representation": "unavailable",
        }
    return {
        "title": resolved_title,
        "source": "resolved_accessibility_text",
        "title_representation": "unresolved",
        "preview_representation": "unavailable",
    }


def _project_more_rows_indicator(
    rows: list[dict],
    snapshots: list[AXElementSnapshot],
    chat_list_container: dict,
    content: dict,
) -> object:
    viewport = _frame_tuple(chat_list_container.get("frame")) or _frame_tuple(content.get("content_frame"))
    if viewport is None:
        return "unknown"
    bottom = viewport[1] + viewport[3]
    if any((_frame_tuple(row["row_node"].frame) or (0, 0, 0, 0))[1] + (_frame_tuple(row["row_node"].frame) or (0, 0, 0, 0))[3] > bottom + FRAME_CONTAINMENT_TOLERANCE for row in rows):
        return True
    for snapshot in snapshots:
        if snapshot.role != "AXScrollBar" or not snapshot.path.startswith(str(chat_list_container.get("path") or "") + "."):
            continue
        value = _normalized_label(snapshot.value or snapshot.title or snapshot.description).casefold()
        if value in {"1", "1.0", "100%", "bottom"}:
            return False
        if value:
            return True
    return "unknown"


def _project_container_report(container: dict) -> dict:
    return {
        "path": _bounded_text(str(container.get("path") or ""), MAX_PATH_LENGTH),
        "role": _bounded_text(str(container.get("role") or ""), MAX_ROLE_LENGTH),
        "subrole": _bounded_text(str(container.get("subrole") or ""), MAX_ROLE_LENGTH),
        "frame": _frame_geometry_report(_frame_tuple(container.get("frame"))),
    }


def _merge_reason_counts(left: dict, right: dict) -> dict:
    merged = Counter(left or {})
    merged.update(right or {})
    return dict(sorted(merged.items()))


def open_chatgpt_sidebar_destination(
    *,
    kind: str,
    title: str,
    confirm_open_destination: bool = False,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    activation_function: object | None = None,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
    click_service_factory: object | None = None,
    display_probe_factory: object | None = None,
    windowserver_probe_factory: object | None = None,
    sleep_function: object | None = None,
    before_action_callback: object | None = None,
) -> dict:
    requested_title = _normalized_label(title)
    result = _base_autonomous_open_result(kind, requested_title, app_name, confirm_open_destination)
    if kind not in {"project", "chat"} or not requested_title:
        result.update({"outcome": "target_absent", "error": "kind must be project or chat and title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"outcome": "post_action_inspection_unavailable", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"outcome": "activation_failed", "error": "ChatGPT sidebar destination open is only supported on macOS."})
        return result

    sleeper = sleep_function or time.sleep
    if confirm_open_destination:
        try:
            activator = activation_function
            if activator is None:
                from agent.mac_app_control import activate_chatgpt as activator

            activation_result = activator(app_name)
        except Exception as exc:
            activation_result = {"activated": False, "is_frontmost": False, "app_name": app_name, "error": str(exc)}
        result["activation_result"] = _autonomous_activation_summary(activation_result)
        if not bool(activation_result.get("is_frontmost")):
            result.update({"outcome": "activation_failed", "error": activation_result.get("error") or "ChatGPT could not be brought frontmost."})
            return result
        sleeper(min(AUTONOMOUS_OPEN_SETTLE_SECONDS, 1.0))
    else:
        result["activation_result"] = {
            "activated": False,
            "is_frontmost": False,
            "app_name": app_name,
            "frontmost_app": None,
            "error": "skipped_dry_run",
        }

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"outcome": "activation_failed", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"outcome": "activation_failed", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _AutonomousSidebarAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
    except Exception as exc:
        result.update({"outcome": "post_action_inspection_unavailable", "error": str(exc), "pid_present": True})
        return result

    stable = _stable_chatgpt_geometry_sample(
        reader,
        process.pid,
        windowserver_probe_factory or _WindowServerBoundsProbe,
        sample_count=AUTONOMOUS_OPEN_STABILITY_SAMPLE_COUNT,
        sleep_function=sleeper,
    )
    result["activation_stability"] = _autonomous_stability_summary(stable)
    if stable["status"] != "stable":
        result.update({"outcome": "unstable_chatgpt_ui", "error": stable.get("error") or "ChatGPT AX/window geometry did not stabilize."})
        return result

    plan = _autonomous_destination_plan(
        stable["snapshots"],
        stable["stats"],
        stable["window_metadata"],
        kind,
        requested_title,
        stable["windowserver_bounds"],
        display_probe_factory or _CoreGraphicsDisplayProbe,
    )
    result.update(_autonomous_plan_result(plan))
    if plan["status"] != "ready":
        result.update({"outcome": plan["status"], "error": plan.get("error", "")})
        return result

    axpress_target = plan.get("axpress_target") or {}
    if axpress_target.get("path"):
        result["chosen_method"] = "axpress"
        if not confirm_open_destination:
            result.update({"ok": True, "outcome": "dry_run_ready"})
            return result
        try:
            if before_action_callback is not None:
                before_action_callback()
            _invoke_reader_ax_action(reader, axpress_target["path"], "AXPress")
            result["actions_performed"].append({"path": axpress_target["path"], "action": "AXPress"})
        except Exception as exc:
            result["axpress_attempt"] = {"ok": False, "error": str(exc), "target": axpress_target}
        else:
            result["axpress_attempt"] = {"ok": True, "error": "", "target": axpress_target}
            post = _autonomous_post_action_inspection(
                reader,
                process.pid,
                kind,
                requested_title,
                plan.get("pre_action_state") or {},
                windowserver_probe_factory=windowserver_probe_factory or _WindowServerBoundsProbe,
                sleep_function=sleeper,
            )
            result["post_action_evidence"] = post
            if post.get("inspection_available") and post.get("confirmed"):
                result.update(_autonomous_project_chat_result_fields(post))
                result.update({"ok": True, "outcome": post.get("open_outcome") or "destination_opened_via_axpress"})
                return result
            if not post.get("inspection_available"):
                result.update({"outcome": "post_action_inspection_unavailable", "error": post.get("error") or "Post-action AX inspection was unavailable."})
                return result
            if post.get("open_outcome") in {"project_opened_but_visible_chats_not_resolved", "project_chat_list_identity_not_confirmed"}:
                result.update(_autonomous_project_chat_result_fields(post))
                result.update({"outcome": post.get("open_outcome"), "error": post.get("reason") or "Project opened, but the Chats list could not be confirmed."})
                return result

    click_result = _autonomous_validated_click(
        reader,
        process.pid,
        kind,
        requested_title,
        plan.get("pre_action_state") or {},
        click_service_factory or _CoreGraphicsFrameClickService,
        display_probe_factory or _CoreGraphicsDisplayProbe,
        windowserver_probe_factory or _WindowServerBoundsProbe,
        confirm_open_destination=confirm_open_destination,
        sleep_function=sleeper,
        before_action_callback=before_action_callback if not result["actions_performed"] else None,
    )
    result.update(_autonomous_click_result_fields(click_result))
    if click_result["status"] != "ready":
        if result["actions_performed"]:
            result.update({"outcome": "action_posted_but_destination_not_confirmed", "error": click_result.get("error", "")})
        else:
            result.update({"outcome": click_result["status"], "error": click_result.get("error", "")})
        return result
    result["chosen_method"] = "validated_geometry_click"
    if not confirm_open_destination:
        result.update({"ok": True, "outcome": "dry_run_ready"})
        return result

    result["actions_performed"].extend(click_result.get("actions_performed") or [])
    post = click_result.get("post_action_evidence") or {}
    result["post_action_evidence"] = post
    if post.get("inspection_available") and post.get("confirmed"):
        result.update(_autonomous_project_chat_result_fields(post))
        result.update({"ok": True, "outcome": post.get("open_outcome") or "destination_opened_via_validated_click"})
    elif not post.get("inspection_available"):
        result.update({"outcome": "post_action_inspection_unavailable", "error": post.get("error") or "Post-action AX inspection was unavailable."})
    else:
        result.update(_autonomous_project_chat_result_fields(post))
        project_outcome = post.get("open_outcome")
        if project_outcome in {"project_opened_but_visible_chats_not_resolved", "project_chat_list_identity_not_confirmed"}:
            result.update({"outcome": project_outcome, "error": post.get("reason") or "Project opened, but the Chats list could not be confirmed."})
        else:
            result.update({"outcome": "action_posted_but_destination_not_confirmed", "error": post.get("reason") or "Destination was not confirmed after action."})
    return result


def open_chatgpt_project_chat(
    *,
    project_title: str,
    chat_title: str,
    confirm_open_chat: bool = False,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    activation_function: object | None = None,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
    click_service_factory: object | None = None,
    scroll_service_factory: object | None = None,
    display_probe_factory: object | None = None,
    windowserver_probe_factory: object | None = None,
    sleep_function: object | None = None,
    open_project_function: object | None = None,
    before_action_callback: object | None = None,
    discovery_output_function: object | None = None,
) -> dict:
    requested_project = _normalized_label(project_title)
    requested_chat = _normalized_label(chat_title)
    result = _base_project_chat_open_result(requested_project, requested_chat, app_name, confirm_open_chat)
    if not requested_project or not requested_chat:
        result.update({"outcome": "project_open_failed", "error": "project_title and chat_title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"outcome": "post_action_inspection_unavailable", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"outcome": "project_open_failed", "error": "ChatGPT project chat open is only supported on macOS."})
        return result

    sleeper = sleep_function or time.sleep
    if confirm_open_chat and before_action_callback is not None:
        before_action_callback()

    project_opener = open_project_function or open_chatgpt_sidebar_destination
    try:
        project_result = project_opener(
            kind="project",
            title=requested_project,
            confirm_open_destination=bool(confirm_open_chat),
            app_name=app_name,
            max_depth=max_depth,
            max_nodes=max_nodes,
            activation_function=activation_function,
            process_resolver=process_resolver,
            reader_factory=reader_factory,
            click_service_factory=click_service_factory,
            display_probe_factory=display_probe_factory,
            windowserver_probe_factory=windowserver_probe_factory,
            sleep_function=sleeper,
            before_action_callback=None,
        )
    except TypeError:
        project_result = project_opener(
            kind="project",
            title=requested_project,
            confirm_open_destination=bool(confirm_open_chat),
            app_name=app_name,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    except Exception as exc:
        result.update({"outcome": "project_open_failed", "error": str(exc)})
        return result

    result["project_open_result"] = _project_chat_open_project_summary(project_result)
    result["visible_chat_count"] = int(project_result.get("visible_chat_count") or 0)
    result["visible_chats"] = project_result.get("visible_chats") or []
    for key in ("project_content_container", "main_project_content", "chat_list_container"):
        if project_result.get(key):
            result[key] = project_result.get(key)
    for key in (
        "project_chat_list_identity",
        "project_chat_list_container_path",
        "project_chat_list_container_role",
        "project_chat_row_shape_status",
        "valid_project_chat_row_count",
        "invalid_candidate_count",
        "row_height_median",
        "vertical_peer_list_confirmed",
        "chats_tab_active_evidence",
        "identity_stability_samples",
        "identity_failure_reasons",
    ):
        if key in project_result:
            result[key] = project_result.get(key)

    if not _project_open_result_allows_chat_targeting(project_result):
        result.update(_project_chat_open_project_failure(project_result))
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"outcome": "post_action_inspection_unavailable", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result
    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"outcome": "post_action_inspection_unavailable", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _AutonomousSidebarAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
    except Exception as exc:
        result.update({"outcome": "post_action_inspection_unavailable", "error": str(exc), "pid_present": True})
        return result

    discovery_state = _project_chat_discovery_state(discovery_output_function)
    plan = _fresh_project_chat_targeting_plan(
        reader,
        process.pid,
        requested_project,
        requested_chat,
        display_probe_factory or _CoreGraphicsDisplayProbe,
        windowserver_probe_factory or _WindowServerBoundsProbe,
        sleeper,
    )
    result.update(_project_chat_plan_result_fields(plan))
    _apply_project_chat_search_observation(result, plan)
    result["initial_visible_chat_count"] = int(result.get("targeting_visible_chat_count") or 0)
    result["scroll_target_available"] = _project_chat_scroll_target(plan).get("status") == "ready"
    _apply_project_chat_count_stage_explanation(result)
    _emit_project_chat_discovered_titles(discovery_state, plan, cycle=0, initial=True)
    initial_detection = _project_chat_target_detection_from_plan(plan, requested_chat, "initial", 0)
    _apply_project_chat_target_detection_record(result, initial_detection)
    result["target_initially_visible"] = bool(initial_detection.get("target_exact_match_detected"))
    if initial_detection.get("target_exact_match_detected"):
        _emit_project_chat_target_detected(discovery_state, requested_chat, "initial", 0)
    if plan["status"] != "ready" and not initial_detection.get("target_exact_match_detected"):
        if not confirm_open_chat:
            _apply_project_chat_discovery_result_fields(result, discovery_state)
            result.update({"outcome": plan["status"], "error": plan.get("error", "")})
            return result
        if plan["status"] != "chat_not_currently_visible":
            _apply_project_chat_discovery_result_fields(result, discovery_state)
            result.update({"outcome": plan["status"], "error": plan.get("error", "")})
            return result
        search = _bounded_project_chat_scroll_search(
            reader,
            process.pid,
            requested_project,
            requested_chat,
            plan,
            display_probe_factory or _CoreGraphicsDisplayProbe,
            windowserver_probe_factory or _WindowServerBoundsProbe,
            scroll_service_factory or _CoreGraphicsScrollService,
            sleeper,
            discovery_state=discovery_state,
        )
        result.update(_project_chat_scroll_search_result_fields(search))
        _apply_project_chat_target_detection_result_fields(result, search)
        result["actions_performed"].extend(search.get("actions_performed") or [])
        if search["status"] != "ready":
            _apply_project_chat_discovery_result_fields(result, discovery_state)
            result.update({"outcome": search["status"], "error": search.get("error", "")})
            return result
        plan = search["plan"]
        result.update(_project_chat_plan_result_fields(plan))
        _apply_project_chat_count_stage_explanation(result)

    opened = _open_ready_project_chat_plan(
        result,
        reader,
        process.pid,
        requested_project,
        requested_chat,
        plan,
        confirm_open_chat=confirm_open_chat,
        scrolled_before_match=bool(result.get("target_found_after_scrolling")),
        click_service_factory=click_service_factory or _CoreGraphicsFrameClickService,
        display_probe_factory=display_probe_factory or _CoreGraphicsDisplayProbe,
        windowserver_probe_factory=windowserver_probe_factory or _WindowServerBoundsProbe,
        sleep_function=sleeper,
    )
    _apply_project_chat_discovery_result_fields(opened, discovery_state)
    return opened


def _base_project_chat_open_result(project_title: str, chat_title: str, app_name: str, confirm_open_chat: bool) -> dict:
    return {
        "ok": False,
        "outcome": "project_open_failed",
        "app_name": app_name,
        "project_title": project_title,
        "chat_title": chat_title,
        "confirm_open_chat": bool(confirm_open_chat),
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "project_open_result": {},
        "matched_chat_row": {},
        # True once an AXPress or validated-click action path reported success
        # for the exact freshly re-resolved target. It is not proof of a
        # physical UI change or destination confirmation.
        "chat_open_action_posted": False,
        "visible_chat_count": 0,
        "visible_chats": [],
        "targeting_visible_chat_count": 0,
        "initial_visible_chat_count": 0,
        "max_scroll_iterations": PROJECT_CHAT_SCROLL_MAX_ITERATIONS,
        "scroll_iterations_attempted": 0,
        "max_search_cycles": MAX_CHAT_SEARCH_CYCLES,
        "configured_max_search_cycles": MAX_PROJECT_CHAT_SEARCH_CYCLES,
        "configured_max_search_elapsed_seconds": MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS,
        "search_cycles_attempted": 0,
        "scroll_pulses_posted": 0,
        "scroll_method_used": "",
        "scroll_target_available": False,
        "target_initially_visible": False,
        "target_found_after_scrolling": False,
        "initial_hydration_status": "",
        "hydration_events_observed": 0,
        "reset_events_observed": 0,
        "unique_accessibility_rows_seen": 0,
        "unique_effective_viewports_seen": 0,
        "new_accessibility_rows_seen": 0,
        "target_match_checked_on_samples": 0,
        "hydration_samples_taken": 0,
        "settled_cycles_completed": 0,
        "progressful_cycles_completed": 0,
        "target_found_during_hydration_cycle": 0,
        "unique_chat_titles_printed": 0,
        "total_unique_valid_chats_discovered": 0,
        "target_exact_match_detected": False,
        "target_detected_in": "",
        "target_detected_cycle": 0,
        "normalized_rows_evaluated_for_target": 0,
        "target_detection_snapshot_generation": "",
        "target_detection_row_path": "",
        "target_detection_row_frame": _frame_geometry_report(None),
        "target_detection_canonical_title": "",
        "fresh_target_re_resolution_confirmed": False,
        "final_re_resolution_retry_attempts": 0,
        "final_re_resolution_max_retries": PROJECT_CHAT_FINAL_RE_RESOLUTION_MAX_RETRIES,
        "final_re_resolution_re_resolved": False,
        "final_re_resolution_action_posted": False,
        "scroll_pulses_after_target_detection": 0,
        "target_alignment_required": False,
        "target_alignment_method": "none",
        "target_alignment_posted": False,
        "target_alignment_row_path": "",
        "target_alignment_pre_visibility": "not_visible",
        "target_alignment_post_visibility": "not_visible",
        "target_alignment_fresh_re_resolution_confirmed": False,
        "search_elapsed_seconds": 0.0,
        "search_cycle_summaries": [],
        "unique_row_count_seen": 0,
        "visible_title_count_seen": 0,
        "end_of_list_state": "unknown",
        "computed_scroll_delta_y": 0,
        "median_visible_row_height": 0.0,
        "previous_settled_viewport_signature": "",
        "current_settled_viewport_signature": "",
        "overlap_row_count": 0,
        "overlap_adjacency_confirmed": True,
        "scan_continuity": "confirmed",
        "recovery_scroll_pulses_posted": 0,
        "canonical_visible_chat_titles_considered": [],
        "canonical_visible_chat_count_considered": 0,
        "resolver_snapshot_id": "",
        "visible_chat_count_stage_explanation": "",
        "project_content_container": _project_container_report({}),
        "main_project_content": _project_container_report({}),
        "chat_list_container": _project_container_report({}),
        "project_chat_list_identity": "not_confirmed",
        "project_chat_list_container_path": "",
        "project_chat_list_container_role": "",
        "project_chat_row_shape_status": "insufficient_rows",
        "valid_project_chat_row_count": 0,
        "invalid_candidate_count": 0,
        "row_height_median": 0.0,
        "vertical_peer_list_confirmed": False,
        "chats_tab_active_evidence": "",
        "identity_stability_samples": 1,
        "identity_failure_reasons": [],
        "chosen_method": "",
        "axpress_attempt": {},
        "axpress_post_action_evidence": {},
        # These fields distinguish a selected target, an attempted native AX
        # action, and observed UI/destination evidence.  They are intentionally
        # structural only; no unrelated AX text is retained here.
        "target_detected": False,
        "target_candidate_count": 0,
        "actionable_element_resolved": False,
        "selected_element_role": "",
        "selected_relation": "",
        "available_ax_actions": [],
        "axpress_attempted": False,
        "axpress_result": "not_attempted",
        "ax_error_code": None,
        "ui_changed_after_action": False,
        "destination_confirmed": False,
        "final_reresolution_status": "not_attempted",
        "calculated_global_point": _xy_report(None),
        "calculated_point_hit_test": {},
        "calculated_point_hit_test_relationship": "",
        "post_action_evidence": {},
        "actions_performed": [],
        "error": "",
    }


def _project_chat_open_project_summary(project_result: dict) -> dict:
    traversal = project_result.get("traversal") or {}
    return {
        "ok": bool(project_result.get("ok")),
        "outcome": project_result.get("outcome") or "",
        "chosen_method": project_result.get("chosen_method") or "",
        "target_match_count": max(0, int(project_result.get("target_match_count") or 0)),
        "activation_stability_status": str((project_result.get("activation_stability") or {}).get("status") or ""),
        "traversal": {
            "truncated_by_node_limit": bool(traversal.get("truncated_by_node_limit")),
            "truncated_by_depth_limit": bool(traversal.get("truncated_by_depth_limit")),
        },
        "visible_chat_count": int(project_result.get("visible_chat_count") or 0),
        "post_action_confirmed": bool(project_result.get("post_action_confirmed")),
        "error": project_result.get("error") or "",
    }


def _apply_project_chat_count_stage_explanation(result: dict) -> None:
    project_count = int((result.get("project_open_result") or {}).get("visible_chat_count") or 0)
    targeting_count = int(result.get("targeting_visible_chat_count") or result.get("visible_chat_count") or 0)
    result["visible_chat_count"] = targeting_count
    if project_count != targeting_count:
        result["visible_chat_count_stage_explanation"] = (
            "project_open_result.visible_chat_count was captured during project-open confirmation; "
            "visible_chat_count is from the fresh targeting AX snapshot after project-open settled."
        )
    else:
        result["visible_chat_count_stage_explanation"] = ""


def _project_open_result_allows_chat_targeting(project_result: dict) -> bool:
    if project_result.get("outcome") == "dry_run_ready":
        return True
    return project_result.get("outcome") == "destination_opened_and_visible_chats_resolved" and int(project_result.get("visible_chat_count") or 0) > 0


def _project_chat_open_project_failure(project_result: dict) -> dict:
    outcome = project_result.get("outcome") or ""
    if outcome == "project_chat_list_identity_not_confirmed":
        return {"outcome": "project_chat_list_identity_not_confirmed", "error": project_result.get("error") or "Project Chats-list identity could not be confirmed; no chat interaction was attempted."}
    if outcome in {"destination_opened_with_empty_visible_chat_list", "project_opened_but_visible_chats_not_resolved"}:
        return {"outcome": "project_opened_but_chats_not_available", "error": project_result.get("error") or "Project opened, but visible chats were unavailable."}
    if outcome == "destination_opened_and_visible_chats_resolved" and int(project_result.get("visible_chat_count") or 0) <= 0:
        return {"outcome": "project_opened_but_chats_not_available", "error": "Project opened, but no visible chat rows were resolved."}
    return {"outcome": "project_open_failed", "error": project_result.get("error") or "Project opening could not be confirmed."}


def _project_chat_discovery_state(output_function: object | None) -> dict:
    return {
        "output_function": output_function,
        "initial_printed": False,
        "printed_titles": [],
        "printed_title_set": set(),
        "target_detection_printed": False,
    }


def _project_chat_plan_has_confirmed_list(plan: dict) -> bool:
    resolution = plan.get("project_chat_resolution") or {}
    return resolution.get("project_chat_list_identity") == "confirmed" and bool((resolution.get("chat_list_container") or {}).get("path"))


def _project_chat_discoverable_titles(plan: dict) -> list[str]:
    titles: list[str] = []
    for row in _project_chat_normalized_rows(plan):
        title = _normalized_label(row.get("canonical_title") or "")
        if title:
            titles.append(title)
    return titles


def _project_chat_normalized_rows(plan: dict) -> list[dict]:
    if not _project_chat_plan_has_confirmed_list(plan):
        return []
    snapshot_generation = plan.get("resolver_snapshot_id") or ""
    rows = []
    for row in (plan.get("project_chat_resolution") or {}).get("visible_chats") or []:
        rows.append(
            {
                "canonical_title": _normalized_label(row.get("title") or row.get("canonical_title") or ""),
                "preview": row.get("preview") or "",
                "raw_accessibility_text": row.get("raw_accessibility_text") or row.get("accessibility_row_text") or "",
                "row_path": row.get("row_path") or row.get("path") or "",
                "row_frame": row.get("row_frame") or _frame_geometry_report(None),
                "row_role": row.get("row_role") or row.get("role") or "",
                "row_actions": row.get("row_action_names") or row.get("action_names") or row.get("actions") or [],
                "visibility": row.get("visibility") or "",
                "snapshot_generation": snapshot_generation,
                "source_row": row,
            }
        )
    return rows


def _project_chat_target_detection_from_plan(plan: dict, requested_title: str, detected_in: str, cycle: int) -> dict:
    rows = _project_chat_normalized_rows(plan)
    matches = []
    for row in rows:
        canonical_title = _normalized_label(row.get("canonical_title") or "")
        raw_text = _normalized_label(row.get("raw_accessibility_text") or "")
        matched = canonical_title == requested_title
        if not matched and not canonical_title:
            matched = raw_text == requested_title or ("," not in requested_title and raw_text.startswith(requested_title + ", "))
        if not matched:
            continue
        matches.append((row, canonical_title))
    if len(matches) == 1:
        row, canonical_title = matches[0]
        return {
            "target_exact_match_detected": True,
            "target_detected_in": detected_in,
            "target_detected_cycle": int(cycle),
            "normalized_rows_evaluated_for_target": len(rows),
            "target_detection_snapshot_generation": row.get("snapshot_generation") or "",
            "target_detection_row_path": row.get("row_path") or "",
            "target_detection_row_frame": row.get("row_frame") or _frame_geometry_report(None),
            "target_detection_canonical_title": canonical_title or requested_title,
        }
    return {
        "target_exact_match_detected": False,
        "normalized_rows_evaluated_for_target": len(rows),
        "target_detection_snapshot_generation": plan.get("resolver_snapshot_id") or "",
        "target_detection_row_path": "",
        "target_detection_row_frame": _frame_geometry_report(None),
        "target_detection_canonical_title": "",
    }


def _project_chat_target_detection_from_observation(observation: dict, detected_in: str, cycle: int) -> dict:
    return {
        "target_exact_match_detected": bool(observation.get("target_exact_match_detected") or observation.get("target_found")),
        "target_detected_in": observation.get("target_detected_in") or detected_in,
        "target_detected_cycle": int(cycle),
        "normalized_rows_evaluated_for_target": int(observation.get("normalized_rows_evaluated_for_target") or 0),
        "target_detection_snapshot_generation": observation.get("target_detection_snapshot_generation") or "",
        "target_detection_row_path": observation.get("target_detection_row_path") or "",
        "target_detection_row_frame": observation.get("target_detection_row_frame") or _frame_geometry_report(None),
        "target_detection_canonical_title": observation.get("target_detection_canonical_title") or "",
    }


def _emit_project_chat_lines(state: dict, lines: list[str]) -> None:
    if not lines:
        return
    output_function = state.get("output_function")
    if output_function is None:
        return
    output_function(lines)


def _emit_project_chat_discovered_titles(state: dict, plan: dict, *, cycle: int, initial: bool = False) -> None:
    if not state:
        return
    if not _project_chat_plan_has_confirmed_list(plan):
        return
    printed: list[str] = state["printed_titles"]
    printed_set: set[str] = state["printed_title_set"]
    new_titles = []
    for title in _project_chat_discoverable_titles(plan):
        if title in printed_set:
            continue
        printed_set.add(title)
        printed.append(title)
        new_titles.append(title)
    if initial and not state["initial_printed"]:
        state["initial_printed"] = True
        lines = ["Chats discovered:"]
        lines.extend(f"{printed.index(title) + 1}. {title}" for title in new_titles)
        _emit_project_chat_lines(state, lines)
        return
    if not new_titles:
        return
    if not state["initial_printed"]:
        state["initial_printed"] = True
        lines = ["Chats discovered:"]
    else:
        lines = [f"Chats discovered after cycle {cycle}:"]
    lines.extend(f"{printed.index(title) + 1}. {title}" for title in new_titles)
    _emit_project_chat_lines(state, lines)


def _emit_project_chat_target_detected(state: dict, title: str, detected_in: str, cycle: int) -> None:
    if not state:
        return
    if state.get("target_detection_printed"):
        return
    state["target_detection_printed"] = True
    _emit_project_chat_lines(
        state,
        [
            f"target_exact_match_detected: {title}",
            f"target_detected_in: {detected_in}",
            f"target_detected_cycle: {int(cycle)}",
        ],
    )


def _mark_project_chat_target_detected(target: dict, detected_in: str, cycle: int) -> None:
    if target.get("target_exact_match_detected"):
        return
    target["target_exact_match_detected"] = True
    target["target_detected_in"] = detected_in
    target["target_detected_cycle"] = int(cycle)
    target["scroll_pulses_after_target_detection"] = 0


def _apply_project_chat_target_detection_record(target: dict, detection: dict) -> None:
    target["normalized_rows_evaluated_for_target"] = int(detection.get("normalized_rows_evaluated_for_target") or target.get("normalized_rows_evaluated_for_target") or 0)
    if detection.get("target_detection_snapshot_generation"):
        target["target_detection_snapshot_generation"] = detection.get("target_detection_snapshot_generation") or ""
    if not detection.get("target_exact_match_detected"):
        return
    _mark_project_chat_target_detected(target, detection.get("target_detected_in") or "", int(detection.get("target_detected_cycle") or 0))
    target["target_detection_row_path"] = detection.get("target_detection_row_path") or ""
    target["target_detection_row_frame"] = detection.get("target_detection_row_frame") or _frame_geometry_report(None)
    target["target_detection_canonical_title"] = detection.get("target_detection_canonical_title") or ""


def _apply_project_chat_target_detection_result_fields(result: dict, source: dict) -> None:
    if not source.get("target_exact_match_detected"):
        return
    _apply_project_chat_target_detection_record(result, source)
    result["scroll_pulses_after_target_detection"] = int(source.get("scroll_pulses_after_target_detection") or 0)


def _apply_project_chat_discovery_result_fields(result: dict, state: dict) -> None:
    count = len(state.get("printed_titles") or [])
    result["unique_chat_titles_printed"] = count
    result["total_unique_valid_chats_discovered"] = count


def _open_ready_project_chat_plan(
    result: dict,
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    plan: dict,
    *,
    confirm_open_chat: bool,
    scrolled_before_match: bool,
    click_service_factory: object,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
) -> dict:
    axpress_target = plan.get("axpress_target") or {}
    result["chosen_method"] = "axpress" if axpress_target.get("path") else "validated_geometry_click"
    if not confirm_open_chat:
        result.update({"ok": True, "outcome": "dry_run_ready", "actions_performed": []})
        return result

    fresh_plan = _fresh_project_chat_targeting_plan(
        reader,
        pid,
        project_title,
        chat_title,
        display_probe_factory,
        windowserver_probe_factory,
        sleep_function,
    )
    if fresh_plan["status"] != "ready":
        alignment = _attempt_project_chat_target_alignment(
            result,
            reader,
            pid,
            project_title,
            chat_title,
            fresh_plan,
            display_probe_factory,
            windowserver_probe_factory,
            sleep_function,
        )
        if alignment.get("status") == "ready":
            fresh_plan = alignment["plan"]
        elif alignment.get("handled"):
            if alignment.get("outcome") == "target_detected_but_not_stably_re_resolved":
                retry = _retry_detected_project_chat_fresh_re_resolution(
                    result,
                    reader,
                    pid,
                    project_title,
                    chat_title,
                    alignment.get("plan") or fresh_plan,
                    display_probe_factory,
                    windowserver_probe_factory,
                    sleep_function,
                    expected_plan=alignment.get("plan"),
                )
                if retry.get("status") == "ready":
                    fresh_plan = retry["plan"]
                elif retry.get("handled"):
                    if retry.get("plan"):
                        result.update(_project_chat_plan_result_fields(retry["plan"]))
                        _apply_project_chat_count_stage_explanation(result)
                    result["fresh_target_re_resolution_confirmed"] = bool(retry.get("fresh_re_resolution_confirmed"))
                    result["final_re_resolution_re_resolved"] = bool(retry.get("fresh_re_resolution_confirmed"))
                    result["final_reresolution_status"] = f"failed:{retry.get('outcome') or (retry.get('plan') or {}).get('status') or 'unresolved'}"
                    result.update({"outcome": retry.get("outcome") or "target_detected_but_not_stably_re_resolved", "error": retry.get("error") or ""})
                    return result
            if fresh_plan["status"] == "ready":
                pass
            else:
                result["fresh_target_re_resolution_confirmed"] = bool(alignment.get("fresh_re_resolution_confirmed"))
                result["final_reresolution_status"] = f"failed:{alignment.get('outcome') or (alignment.get('plan') or {}).get('status') or 'unresolved'}"
                if alignment.get("plan"):
                    result.update(_project_chat_plan_result_fields(alignment["plan"]))
                    _apply_project_chat_count_stage_explanation(result)
                result.update({"outcome": alignment.get("outcome") or "target_detected_but_not_stably_re_resolved", "error": alignment.get("error") or ""})
                return result
        if fresh_plan["status"] != "ready":
            retry = _retry_detected_project_chat_fresh_re_resolution(
                result,
                reader,
                pid,
                project_title,
                chat_title,
                fresh_plan,
                display_probe_factory,
                windowserver_probe_factory,
                sleep_function,
            )
            if retry.get("status") == "ready":
                fresh_plan = retry["plan"]
            elif retry.get("handled"):
                if retry.get("plan"):
                    result.update(_project_chat_plan_result_fields(retry["plan"]))
                    _apply_project_chat_count_stage_explanation(result)
                result["fresh_target_re_resolution_confirmed"] = bool(retry.get("fresh_re_resolution_confirmed"))
                result["final_re_resolution_re_resolved"] = bool(retry.get("fresh_re_resolution_confirmed"))
                result["final_reresolution_status"] = f"failed:{retry.get('outcome') or (retry.get('plan') or {}).get('status') or 'unresolved'}"
                result.update({"outcome": retry.get("outcome") or "target_detected_but_not_stably_re_resolved", "error": retry.get("error") or ""})
                return result
    if fresh_plan["status"] != "ready":
        result["fresh_target_re_resolution_confirmed"] = False
        result["final_reresolution_status"] = f"failed:{fresh_plan['status']}"
        result.update(_project_chat_plan_result_fields(fresh_plan))
        _apply_project_chat_count_stage_explanation(result)
        if result.get("target_exact_match_detected"):
            result.update(
                {
                    "outcome": "target_detected_but_not_stably_re_resolved",
                    "error": fresh_plan.get("error") or "The exact target was detected but was not re-resolved in the fresh pre-action snapshot.",
                }
            )
        else:
            result.update({"outcome": fresh_plan["status"], "error": fresh_plan.get("error", "")})
        return result
    result["fresh_target_re_resolution_confirmed"] = True
    result["final_re_resolution_re_resolved"] = True
    result["final_reresolution_status"] = "confirmed"
    result.update(_project_chat_plan_result_fields(fresh_plan))
    _apply_project_chat_count_stage_explanation(result)
    _record_project_chat_target_alignment_not_required(result, fresh_plan)
    if not result.get("target_exact_match_detected") and _project_chat_plan_materially_changed(plan, fresh_plan):
        result.update(_project_chat_plan_result_fields(fresh_plan))
        _apply_project_chat_count_stage_explanation(result)
        result.update({"outcome": "chat_row_not_interactable", "error": "Resolved chat row path or frame changed before action."})
        return result

    axpress_target = fresh_plan.get("axpress_target") or {}
    if axpress_target.get("path"):
        result["chosen_method"] = "axpress"
        result["axpress_attempted"] = True
        try:
            _invoke_reader_ax_action(reader, axpress_target["path"], "AXPress")
            result["actions_performed"].append({"path": axpress_target["path"], "action": "AXPress"})
        except Exception as exc:
            error_code = _reader_last_ax_action_error_code(reader, axpress_target["path"], "AXPress")
            result["axpress_attempt"] = {"ok": False, "error": str(exc), "error_code": error_code, "target": axpress_target}
            result["axpress_result"] = "failed"
            result["ax_error_code"] = error_code
        else:
            error_code = _reader_last_ax_action_error_code(reader, axpress_target["path"], "AXPress")
            result["axpress_attempt"] = {"ok": True, "error": "", "error_code": error_code, "target": axpress_target}
            result["axpress_result"] = "success"
            result["ax_error_code"] = error_code
            result["chat_open_action_posted"] = True
            result["final_re_resolution_action_posted"] = True
            post = _project_chat_post_action_inspection(
                reader,
                pid,
                project_title,
                chat_title,
                fresh_plan,
                windowserver_probe_factory=windowserver_probe_factory,
                sleep_function=sleep_function,
            )
            result["post_action_evidence"] = post
            _apply_project_chat_post_action_diagnostics(result, post)
            if post.get("inspection_available") and post.get("confirmed"):
                result.update({"ok": True, "outcome": "chat_opened_after_scrolling_via_axpress" if scrolled_before_match else "chat_opened_via_axpress"})
                return result
            if not post.get("inspection_available"):
                result.update({"outcome": "post_action_inspection_unavailable", "error": post.get("error") or "Post-action AX inspection was unavailable."})
                return result
            # AXPress can return success without activating the row in current
            # ChatGPT builds. Preserve the failed observation and try exactly
            # one existing hit-test-validated geometry click below; no blind
            # coordinate action is introduced here.
            result["axpress_post_action_evidence"] = post

    stable_plan = _fresh_project_chat_open_plan(
        reader,
        pid,
        project_title,
        chat_title,
        display_probe_factory,
        windowserver_probe_factory,
        sleep_function,
    )
    if stable_plan["status"] != "ready":
        result.update(_project_chat_plan_result_fields(stable_plan))
        _apply_project_chat_count_stage_explanation(result)
        result.update({"outcome": stable_plan["status"], "error": stable_plan.get("error", "")})
        return result
    if _project_chat_plan_materially_changed(fresh_plan, stable_plan):
        result.update(_project_chat_plan_result_fields(stable_plan))
        _apply_project_chat_count_stage_explanation(result)
        result.update({"outcome": "chat_row_not_interactable", "error": "Resolved chat row path or frame changed before click."})
        return result
    click_plan = _project_chat_validated_click_plan(stable_plan, reader, pid, chat_title)
    result.update(_project_chat_click_result_fields(click_plan))
    if click_plan["status"] != "ready":
        result.update({"outcome": click_plan["status"], "error": click_plan.get("error", "")})
        return result
    result["chosen_method"] = (
        "axpress_then_validated_geometry_click"
        if result.get("axpress_attempted")
        else "validated_geometry_click"
    )
    try:
        clicker = click_service_factory()
        if not clicker.has_permission():
            result.update({"outcome": "click_posting_failed", "error": "CoreGraphics post-event permission is unavailable."})
            return result
        posted = clicker.left_click(click_plan["calculated_global_point"]["x"], click_plan["calculated_global_point"]["y"])
    except Exception as exc:
        result.update({"outcome": "click_posting_failed", "error": str(exc)})
        return result
    if not posted.get("ok"):
        result.update({"outcome": "click_posting_failed", "error": posted.get("error") or "CoreGraphics click could not be posted."})
        return result
    result["actions_performed"].extend(posted.get("actions_performed") or [])
    result["chat_open_action_posted"] = True
    result["final_re_resolution_action_posted"] = True
    post = _project_chat_post_action_inspection(
        reader,
        pid,
        project_title,
        chat_title,
        stable_plan,
        windowserver_probe_factory=windowserver_probe_factory,
        sleep_function=sleep_function,
    )
    result["post_action_evidence"] = post
    _apply_project_chat_post_action_diagnostics(result, post)
    if post.get("inspection_available") and post.get("confirmed"):
        result.update({"ok": True, "outcome": "chat_opened_after_scrolling_via_validated_click" if scrolled_before_match else "chat_opened_via_validated_click"})
    elif not post.get("inspection_available"):
        result.update({"outcome": "post_action_inspection_unavailable", "error": post.get("error") or "Post-action AX inspection was unavailable."})
    else:
        result.update({"outcome": "action_posted_but_chat_not_confirmed", "error": post.get("reason") or "Chat open was not confirmed after click."})
    return result


def _retry_detected_project_chat_fresh_re_resolution(
    result: dict,
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    failed_plan: dict,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
    *,
    expected_plan: dict | None = None,
) -> dict:
    if not result.get("target_exact_match_detected"):
        return {"handled": False, "plan": failed_plan}

    max_retries = max(0, int(PROJECT_CHAT_FINAL_RE_RESOLUTION_MAX_RETRIES))
    result["final_re_resolution_max_retries"] = max_retries
    if max_retries <= 0:
        return _project_chat_detected_target_still_unresolved(failed_plan)

    last_plan = failed_plan
    for attempt in range(1, max_retries + 1):
        sleep_function(PROJECT_CHAT_FINAL_RE_RESOLUTION_RETRY_DELAY_SECONDS)
        retry_plan = _fresh_project_chat_targeting_plan(
            reader,
            pid,
            project_title,
            chat_title,
            display_probe_factory,
            windowserver_probe_factory,
            sleep_function,
        )
        last_plan = retry_plan
        result["final_re_resolution_retry_attempts"] = attempt
        if retry_plan.get("status") == "ready":
            if _project_chat_retry_plan_matches_detected_target(result, retry_plan, expected_plan=expected_plan or failed_plan):
                return {"handled": True, "status": "ready", "plan": retry_plan, "fresh_re_resolution_confirmed": True}
            return {
                "handled": True,
                "plan": retry_plan,
                "fresh_re_resolution_confirmed": False,
                "outcome": "target_detected_but_not_stably_re_resolved",
                "error": "The exact target was re-resolved on retry, but it did not match the previously detected chat row.",
            }
        if retry_plan.get("status") == "chat_title_ambiguous":
            return {
                "handled": True,
                "plan": retry_plan,
                "fresh_re_resolution_confirmed": False,
                "outcome": "target_detected_but_not_stably_re_resolved",
                "error": retry_plan.get("error") or "The detected target became ambiguous during fresh pre-action re-resolution.",
            }

    return _project_chat_detected_target_still_unresolved(last_plan)


def _project_chat_detected_target_still_unresolved(plan: dict) -> dict:
    return {
        "handled": True,
        "plan": plan,
        "fresh_re_resolution_confirmed": False,
        "outcome": "target_detected_but_not_stably_re_resolved",
        "error": plan.get("error") or "The exact target was detected but was not re-resolved in fresh pre-action snapshots.",
    }


def _project_chat_retry_plan_matches_detected_target(result: dict, plan: dict, *, expected_plan: dict | None = None) -> bool:
    row = plan.get("matched_chat_row") or {}
    if not row:
        return False
    expected_row = (expected_plan or {}).get("matched_chat_row") or {}
    expected_title = _normalized_label(expected_row.get("title") or "")
    detected_title = expected_title or _normalized_label(result.get("target_detection_canonical_title") or result.get("chat_title") or "")
    row_title = _normalized_label(row.get("title") or "")
    if not detected_title or row_title != detected_title:
        return False
    return True


def _record_project_chat_target_alignment_not_required(result: dict, plan: dict) -> None:
    if result.get("target_alignment_posted"):
        return
    result["target_alignment_required"] = False
    result["target_alignment_method"] = "none"
    result["target_alignment_posted"] = False
    result["target_alignment_row_path"] = ""
    result["target_alignment_pre_visibility"] = _project_chat_plan_matched_visibility(plan)
    result["target_alignment_post_visibility"] = _project_chat_plan_matched_visibility(plan)
    result["target_alignment_fresh_re_resolution_confirmed"] = bool(plan.get("matched_chat_row"))


def _attempt_project_chat_target_alignment(
    result: dict,
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    fresh_plan: dict,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
) -> dict:
    if not result.get("target_exact_match_detected"):
        return {"handled": False}
    if result.get("target_alignment_posted"):
        return {
            "handled": True,
            "fresh_re_resolution_confirmed": False,
            "plan": fresh_plan,
            "outcome": "target_alignment_not_supported",
            "error": "A target-alignment action was already posted during this command execution.",
        }
    row = fresh_plan.get("matched_chat_row") or {}
    pre_visibility = _project_chat_plan_matched_visibility(fresh_plan)
    if pre_visibility != "partially_clipped" or not row:
        return {"handled": False}

    result["target_alignment_required"] = True
    result["target_alignment_method"] = "none"
    result["target_alignment_pre_visibility"] = pre_visibility
    result["target_alignment_post_visibility"] = pre_visibility
    result["target_alignment_row_path"] = row.get("row_path") or row.get("path") or ""

    target = _project_chat_alignment_target(fresh_plan)
    if not target:
        return {
            "handled": True,
            "fresh_re_resolution_confirmed": True,
            "plan": fresh_plan,
            "outcome": "target_alignment_not_supported",
            "error": "The partially clipped exact target row does not expose AXScrollToVisible.",
        }

    result["target_alignment_method"] = "axscrolltovisible"
    context = _project_chat_alignment_action_context(fresh_plan, chat_title, result)
    if not _project_chat_alignment_policy_conditions_satisfied(context):
        return {
            "handled": True,
            "fresh_re_resolution_confirmed": True,
            "plan": fresh_plan,
            "outcome": "target_alignment_not_supported",
            "error": "Target alignment policy conditions were not satisfied.",
        }
    try:
        _invoke_reader_ax_action(reader, target["path"], "AXScrollToVisible", action_context=context)
    except Exception as exc:
        return {
            "handled": True,
            "fresh_re_resolution_confirmed": True,
            "plan": fresh_plan,
            "outcome": "target_alignment_action_post_failed",
            "error": str(exc),
        }

    result["target_alignment_posted"] = True
    result["actions_performed"].append({"path": target["path"], "action": "AXScrollToVisible"})
    sleep_function(min(AUTONOMOUS_OPEN_POST_ACTION_SETTLE_SECONDS, 1.0))

    post_plan = _fresh_project_chat_targeting_plan(
        reader,
        pid,
        project_title,
        chat_title,
        display_probe_factory,
        windowserver_probe_factory,
        sleep_function,
    )
    post_visibility = _project_chat_plan_matched_visibility(post_plan)
    re_resolved = bool(post_plan.get("matched_chat_row"))
    result["target_alignment_post_visibility"] = post_visibility
    result["target_alignment_fresh_re_resolution_confirmed"] = re_resolved
    if post_plan.get("status") == "ready":
        return {"handled": True, "status": "ready", "plan": post_plan, "fresh_re_resolution_confirmed": True}
    if not re_resolved:
        return {
            "handled": True,
            "fresh_re_resolution_confirmed": False,
            "plan": post_plan,
            "outcome": "target_alignment_posted_but_target_not_re_resolved",
            "error": post_plan.get("error") or "The exact target was not re-resolved after AXScrollToVisible.",
        }
    if post_visibility != "fully_visible":
        return {
            "handled": True,
            "fresh_re_resolution_confirmed": True,
            "plan": post_plan,
            "outcome": "target_alignment_posted_but_target_not_fully_visible",
            "error": post_plan.get("error") or "The exact target remained outside the fully visible project Chats-list viewport after AXScrollToVisible.",
        }
    return {
        "handled": True,
        "fresh_re_resolution_confirmed": True,
        "plan": post_plan,
        "outcome": "target_detected_but_not_stably_re_resolved",
        "error": post_plan.get("error") or "The exact target was re-resolved after AXScrollToVisible but was not safely actionable.",
    }


def _project_chat_alignment_target(plan: dict) -> dict:
    row = plan.get("matched_chat_row") or {}
    row_path = row.get("row_path") or row.get("path") or ""
    row_snapshot = plan.get("row_snapshot")
    actions = _safe_actions(row_snapshot.actions if row_snapshot is not None else row.get("row_action_names") or row.get("action_names") or [])
    if row_path and "AXScrollToVisible" in actions:
        return {"path": row_path, "action": "AXScrollToVisible"}
    return {}


def _project_chat_alignment_action_context(plan: dict, requested_title: str, result: dict) -> dict:
    row = plan.get("matched_chat_row") or {}
    row_path = row.get("row_path") or row.get("path") or ""
    container_path = ((plan.get("project_chat_resolution") or {}).get("chat_list_container") or {}).get("path") or ""
    return {
        "kind": "exact_project_chat_target_alignment",
        "target_path": row_path,
        "requested_title": requested_title,
        "canonical_title": _normalized_label(row.get("title") or ""),
        "exact_target_detected": bool(result.get("target_exact_match_detected")),
        "fresh_re_resolution_confirmed": bool(row),
        "confirmed_chat_list_container_path": container_path,
        "target_descends_from_confirmed_chat_list": bool(container_path and _project_node_within_container_path(row_path, container_path)),
        "visibility": _project_chat_plan_matched_visibility(plan),
        "row_actions": _safe_actions((plan.get("row_snapshot").actions if plan.get("row_snapshot") is not None else row.get("row_action_names") or row.get("action_names") or [])),
        "alignment_already_posted": bool(result.get("target_alignment_posted")),
    }


def _project_chat_alignment_policy_conditions_satisfied(context: dict) -> bool:
    if context.get("kind") != "exact_project_chat_target_alignment":
        return False
    target_path = str(context.get("target_path") or "")
    container_path = str(context.get("confirmed_chat_list_container_path") or "")
    if not target_path or not container_path:
        return False
    return (
        bool(context.get("exact_target_detected"))
        and bool(context.get("fresh_re_resolution_confirmed"))
        and _normalized_label(context.get("canonical_title") or "") == _normalized_label(context.get("requested_title") or "")
        and bool(context.get("target_descends_from_confirmed_chat_list"))
        and _project_node_within_container_path(target_path, container_path)
        and context.get("visibility") == "partially_clipped"
        and "AXScrollToVisible" in _safe_actions(context.get("row_actions") or [])
        and not bool(context.get("alignment_already_posted"))
    )


def _project_chat_plan_matched_visibility(plan: dict) -> str:
    row = plan.get("matched_chat_row") or {}
    if not row:
        return "not_visible"
    visibility = row.get("visibility") or ""
    if visibility in {"fully_visible", "partially_clipped", "not_visible"}:
        return visibility
    viewport_frame = _frame_tuple(plan.get("viewport_frame"))
    row_frame = _frame_tuple(row.get("row_frame"))
    if _frame_contains(viewport_frame, row_frame):
        return "fully_visible"
    if _frame_intersects(viewport_frame, row_frame):
        return "partially_clipped"
    return "not_visible"


def _bounded_project_chat_scroll_search(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    initial_plan: dict,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    scroll_service_factory: object,
    sleep_function: object,
    *,
    discovery_state: dict | None = None,
) -> dict:
    started = time.monotonic()
    seen_signatures: set[str] = set()
    seen_titles: set[str] = set()
    seen_accessibility_rows: set[str] = set()
    seen_effective_viewports: set[str] = set()
    _accumulate_project_chat_seen_rows(initial_plan, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
    initial_accessibility_row_count = len(seen_accessibility_rows)
    scroll_target = _project_chat_scroll_target(initial_plan)
    if scroll_target.get("status") != "ready":
        outcome = "chat_list_scroll_target_not_found" if scroll_target.get("status") == "target_not_found" else "chat_not_currently_visible_and_scroll_unavailable"
        return _project_chat_scroll_search_result(outcome, initial_plan, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports, scroll_target, 0, 0, "unknown", scroll_target.get("error") or "", started_at=started, initial_accessibility_row_count=initial_accessibility_row_count, target_match_checked_on_samples=1)

    actions_performed: list[dict] = []
    cycle_summaries: list[str] = []
    hydration_events_observed = 0
    reset_events_observed = 0
    target_match_checked_on_samples = 1
    hydration_samples_taken = 0
    settled_cycles_completed = 0
    progressful_cycles_completed = 0
    target_found_during_hydration_cycle = 0
    initial_hydration_status = ""
    bottom_evidence_seen = _project_chat_end_of_list_confirmed(initial_plan)
    current_plan = initial_plan
    target_detection_record: dict = _project_chat_target_detection_from_plan(initial_plan, chat_title, "initial", 0)
    continuity_report = {
        "previous_settled_viewport_signature": _project_chat_effective_viewport_signature(initial_plan),
        "current_settled_viewport_signature": _project_chat_effective_viewport_signature(initial_plan),
        "overlap_row_count": 0,
        "overlap_adjacency_confirmed": True,
        "scan_continuity": "confirmed",
        "recovery_scroll_pulses_posted": 0,
    }

    def finish(status: str, plan: dict, cycles: int, scroll_pulses: int, end_state: str, error: str) -> dict:
        return _project_chat_scroll_search_result(
            status,
            plan,
            seen_signatures,
            seen_titles,
            seen_accessibility_rows,
            seen_effective_viewports,
            scroll_target,
            cycles,
            scroll_pulses,
            end_state,
            error,
            actions_performed,
            started_at=started,
            initial_hydration_status=initial_hydration_status,
            hydration_events=hydration_events_observed,
            reset_events=reset_events_observed,
            cycle_summaries=cycle_summaries,
            initial_accessibility_row_count=initial_accessibility_row_count,
            target_match_checked_on_samples=target_match_checked_on_samples,
            hydration_samples_taken=hydration_samples_taken,
            settled_cycles_completed=settled_cycles_completed,
            progressful_cycles_completed=progressful_cycles_completed,
            target_found_during_hydration_cycle=target_found_during_hydration_cycle,
            continuity_report=continuity_report,
            target_detection_record=target_detection_record,
        )

    initial_observation = _observe_project_chat_list_hydration(
        reader,
        pid,
        project_title,
        chat_title,
        current_plan,
        current_plan,
        display_probe_factory,
        windowserver_probe_factory,
        sleep_function,
        timeout_seconds=INITIAL_PROJECT_CHAT_HYDRATION_TIMEOUT_SECONDS,
        known_accessibility_rows=seen_accessibility_rows,
        discovery_state=discovery_state,
        discovery_cycle=0,
        target_detection_stage="hydration",
    )
    current_plan = initial_observation.get("plan") or current_plan
    initial_hydration_status = initial_observation.get("classification") or ""
    _accumulate_observed_project_chat_rows(initial_observation, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
    hydration_events_observed += int(initial_observation.get("hydration_events_observed") or 0)
    reset_events_observed += int(initial_observation.get("reset_events_observed") or 0)
    target_match_checked_on_samples += int(initial_observation.get("target_match_checked_on_samples") or 0)
    hydration_samples_taken += int(initial_observation.get("samples_taken") or 0)
    if _project_chat_end_of_list_confirmed(current_plan):
        bottom_evidence_seen = True
    if current_plan.get("status") == "ready" or initial_observation.get("target_found"):
        stable_found = current_plan
        _accumulate_project_chat_seen_rows(stable_found, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
        detection_in = initial_observation.get("target_detected_in") or "hydration"
        target_detection_record = _project_chat_target_detection_from_observation(initial_observation, detection_in, 0)
        _emit_project_chat_target_detected(discovery_state or {}, chat_title, detection_in, 0)
        return {
            **finish("ready", stable_found, 0, 0, "confirmed" if _project_chat_end_of_list_confirmed(stable_found) else "unknown", ""),
            "plan": stable_found,
            "target_found_after_scrolling": False,
            "target_exact_match_detected": True,
            "target_detected_in": detection_in,
            "target_detected_cycle": 0,
            "scroll_pulses_after_target_detection": 0,
        }
    if current_plan.get("status") not in {"chat_not_currently_visible", "chat_title_not_unambiguously_representable_by_accessibility"}:
        return finish(current_plan["status"], current_plan, 0, 0, "unknown", current_plan.get("error") or "")

    cycle = 0
    last_cycle_progressful = bool(initial_observation.get("meaningful_change"))
    quiet_streak = 0
    end_anchor_streak = 0
    while True:
        elapsed = max(0.0, time.monotonic() - started)
        if elapsed >= MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS:
            outcome = "chat_search_time_budget_exhausted_while_list_progressing" if last_cycle_progressful else "chat_search_budget_exhausted_without_confirmed_end"
            error = (
                "Total bounded project-chat search time elapsed while list rows or viewport state were still changing."
                if last_cycle_progressful
                else "Total bounded project-chat search time elapsed without confirming the end of the project chat list."
            )
            return finish(outcome, current_plan, cycle, cycle, "not_confirmed" if bottom_evidence_seen else "unknown", error)
        if cycle >= MAX_PROJECT_CHAT_SEARCH_CYCLES and not last_cycle_progressful:
            return finish(
                "chat_search_budget_exhausted_without_confirmed_end",
                current_plan,
                cycle,
                cycle,
                "not_confirmed" if bottom_evidence_seen else "unknown",
                "Maximum bounded search cycles were reached after the list stopped making meaningful progress.",
            )

        cycle += 1
        current_plan = _fresh_project_chat_targeting_plan(
            reader,
            pid,
            project_title,
            chat_title,
            display_probe_factory,
            windowserver_probe_factory,
            sleep_function,
        )
        target_match_checked_on_samples += 1
        _accumulate_project_chat_seen_rows(current_plan, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
        _emit_project_chat_discovered_titles(discovery_state or {}, current_plan, cycle=cycle)
        pre_scroll_detection = _project_chat_target_detection_from_plan(current_plan, chat_title, "pre_scroll", cycle)
        if current_plan["status"] == "ready" or pre_scroll_detection.get("target_exact_match_detected"):
            stable_found = current_plan
            _accumulate_project_chat_seen_rows(stable_found, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
            target_detection_record = pre_scroll_detection
            _emit_project_chat_target_detected(discovery_state or {}, chat_title, "pre_scroll", cycle)
            return {
                **finish("ready", stable_found, cycle - 1, cycle - 1, "confirmed" if _project_chat_end_of_list_confirmed(stable_found) else "unknown", ""),
                "plan": stable_found,
                "target_found_after_scrolling": bool(actions_performed),
                "target_exact_match_detected": True,
                "target_detected_in": "pre_scroll",
                "target_detected_cycle": cycle,
                "scroll_pulses_after_target_detection": 0,
            }
        if current_plan["status"] not in {"chat_not_currently_visible", "chat_title_not_unambiguously_representable_by_accessibility"}:
            return finish(current_plan["status"], current_plan, cycle - 1, cycle - 1, "unknown", current_plan.get("error") or "")
        refreshed_target = _project_chat_scroll_target(current_plan)
        if refreshed_target.get("status") == "ready":
            scroll_target = refreshed_target
        else:
            outcome = "chat_list_scroll_target_not_found" if refreshed_target.get("status") == "target_not_found" else "chat_list_scroll_failed"
            return finish(outcome, current_plan, cycle - 1, cycle - 1, "not_confirmed" if bottom_evidence_seen else "unknown", refreshed_target.get("error") or "Project chat list could not be reconfirmed for scrolling.")

        # Pre-scroll settled viewport: the contiguity baseline for this cycle.
        pre_scroll_state = _project_chat_effective_list_state(current_plan)
        scroll_method = scroll_target.get("method") or ""
        step = _perform_project_chat_scroll_step(reader, scroll_target, scroll_service_factory)
        if step.get("status") != "ready":
            return finish("chat_list_scroll_failed", current_plan, cycle - 1, cycle - 1, "not_confirmed" if bottom_evidence_seen else "unknown", step.get("error") or "")
        actions_performed.extend(step.get("actions_performed") or [])
        cycle_progressful = False
        cycle_new_rows = 0
        cycle_reset_then_changed = False
        cycle_settled = False
        cycle_classification = "list_hydrating"
        cycle_hydration_events = 0
        cycle_reset_events = 0
        hydration_extensions = 0
        observation = _observe_project_chat_list_hydration(
            reader,
            pid,
            project_title,
            chat_title,
            current_plan,
            current_plan,
            display_probe_factory,
            windowserver_probe_factory,
            sleep_function,
            timeout_seconds=POST_SCROLL_HYDRATION_TIMEOUT_SECONDS,
            known_accessibility_rows=seen_accessibility_rows,
            require_change_before_early_settle=True,
            discovery_state=discovery_state,
            discovery_cycle=cycle,
            target_detection_stage="hydration",
        )
        while True:
            current_plan = observation.get("plan") or current_plan
            _accumulate_observed_project_chat_rows(observation, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
            cycle_classification = observation.get("classification") or "list_unavailable"
            hydration_events_observed += int(observation.get("hydration_events_observed") or 0)
            reset_events_observed += int(observation.get("reset_events_observed") or 0)
            cycle_hydration_events += int(observation.get("hydration_events_observed") or 0)
            cycle_reset_events += int(observation.get("reset_events_observed") or 0)
            target_match_checked_on_samples += int(observation.get("target_match_checked_on_samples") or 0)
            hydration_samples_taken += int(observation.get("samples_taken") or 0)
            cycle_new_rows += int(observation.get("new_accessibility_rows") or 0)
            cycle_reset_then_changed = cycle_reset_then_changed or bool(observation.get("reset_then_changed"))
            cycle_progressful = cycle_progressful or bool(observation.get("meaningful_change")) or int(observation.get("new_accessibility_rows") or 0) > 0 or bool(observation.get("reset_then_changed"))
            cycle_settled = bool(observation.get("settled"))
            if current_plan.get("status") == "ready" or cycle_settled:
                break
            if max(0.0, time.monotonic() - started) >= MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS:
                break
            hydration_extensions += 1
            if hydration_extensions > max(1, int(math.ceil(MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS / max(POST_SCROLL_HYDRATION_TIMEOUT_SECONDS, 0.01)))):
                break
            observation = _observe_project_chat_list_hydration(
                reader,
                pid,
                project_title,
                chat_title,
                current_plan,
                current_plan,
                display_probe_factory,
                windowserver_probe_factory,
                sleep_function,
                timeout_seconds=POST_SCROLL_HYDRATION_TIMEOUT_SECONDS,
                known_accessibility_rows=seen_accessibility_rows,
                require_change_before_early_settle=False,
                discovery_state=discovery_state,
                discovery_cycle=cycle,
                target_detection_stage="hydration",
            )
        if cycle_settled:
            settled_cycles_completed += 1
        if cycle_progressful:
            progressful_cycles_completed += 1

        if current_plan["status"] == "ready" or observation.get("target_found"):
            stable_found = current_plan
            _accumulate_project_chat_seen_rows(stable_found, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
            if observation.get("target_found") and not target_found_during_hydration_cycle:
                target_found_during_hydration_cycle = cycle
            detection_in = observation.get("target_detected_in") or ("settled" if cycle_settled else "hydration")
            target_detection_record = _project_chat_target_detection_from_observation(observation, detection_in, cycle)
            _emit_project_chat_target_detected(discovery_state or {}, chat_title, detection_in, cycle)
            return {
                **finish("ready", stable_found, cycle, cycle, "confirmed" if _project_chat_end_of_list_confirmed(stable_found) else ("not_confirmed" if bottom_evidence_seen else "unknown"), ""),
                "plan": stable_found,
                "target_found_after_scrolling": True,
                "target_exact_match_detected": True,
                "target_detected_in": detection_in,
                "target_detected_cycle": cycle,
                "scroll_pulses_after_target_detection": 0,
            }

        # Verify overlap between the pre-scroll and post-scroll settled viewports.
        # Overlap is enforced only for the CoreGraphics micro-scroll, whose pixel
        # quantum we control; the semantic AXScrollDown path defers to the app's
        # own controlled scroll amount.
        post_scroll_state = _project_chat_effective_list_state(current_plan)
        overlap = _project_chat_viewport_overlap(pre_scroll_state, post_scroll_state)
        moved = pre_scroll_state.get("ordered_signature") != post_scroll_state.get("ordered_signature")
        both_have_rows = bool(pre_scroll_state.get("row_count")) and bool(post_scroll_state.get("row_count"))
        cycle_continuity = "confirmed"
        cycle_recovery_pulses = 0
        recovery: dict = {}
        if scroll_method == "coregraphics_scroll" and moved and both_have_rows and not overlap.get("adjacency_confirmed"):
            recovery = _recover_project_chat_scan_continuity(
                reader,
                pid,
                project_title,
                chat_title,
                current_plan,
                pre_scroll_state,
                display_probe_factory,
                windowserver_probe_factory,
                scroll_service_factory,
                sleep_function,
                started,
                current_quantum=abs(int(scroll_target.get("computed_scroll_delta_y") or _project_chat_computed_scroll_delta_y(0.0))),
                seen_signatures=seen_signatures,
                seen_titles=seen_titles,
                seen_accessibility_rows=seen_accessibility_rows,
                seen_effective_viewports=seen_effective_viewports,
                actions_performed=actions_performed,
                discovery_state=discovery_state,
                discovery_cycle=cycle,
            )
            current_plan = recovery.get("plan") or current_plan
            hydration_events_observed += int(recovery.get("hydration_events") or 0)
            reset_events_observed += int(recovery.get("reset_events") or 0)
            cycle_hydration_events += int(recovery.get("hydration_events") or 0)
            cycle_reset_events += int(recovery.get("reset_events") or 0)
            target_match_checked_on_samples += int(recovery.get("target_match_checked") or 0)
            hydration_samples_taken += int(recovery.get("hydration_samples") or 0)
            cycle_recovery_pulses = int(recovery.get("recovery_pulses") or 0)
            continuity_report["recovery_scroll_pulses_posted"] += cycle_recovery_pulses
            overlap = recovery.get("overlap") or overlap
            post_scroll_state = recovery.get("post_state") or post_scroll_state
            cycle_progressful = True
            if recovery.get("target_found") and current_plan.get("status") != "ready":
                # Recovery surfaced the target as a canonical row; refresh below.
                current_plan = recovery.get("plan") or current_plan
            cycle_continuity = "confirmed" if recovery.get("restored") else "not_confirmed"

        continuity_report["previous_settled_viewport_signature"] = str(pre_scroll_state.get("ordered_signature"))
        continuity_report["current_settled_viewport_signature"] = str(post_scroll_state.get("ordered_signature"))
        continuity_report["overlap_row_count"] = int(overlap.get("overlap_row_count") or 0)
        continuity_report["overlap_adjacency_confirmed"] = bool(overlap.get("adjacency_confirmed") or (scroll_method == "coregraphics_scroll" and cycle_continuity == "confirmed" and not moved))
        continuity_report["scan_continuity"] = cycle_continuity if cycle_continuity != "confirmed" else "confirmed"

        if cycle_recovery_pulses:
            cycle_summaries.append(
                f"cycle_{cycle}_recovery: reverse_micro_scroll_posted -> {'overlap_confirmed -> settled' if cycle_continuity == 'confirmed' else 'overlap_missing'}"
            )
        if scroll_method == "coregraphics_scroll":
            scroll_word = "micro_scroll_posted"
        else:
            scroll_word = "scroll_posted"
        if scroll_method == "coregraphics_scroll" and moved and both_have_rows:
            overlap_word = "overlap_confirmed" if cycle_continuity == "confirmed" else "overlap_missing"
            cycle_summaries.append(
                f"cycle_{cycle}: {scroll_word} -> {overlap_word} -> {cycle_new_rows}_new_rows -> {'settled' if cycle_settled else 'still_hydrating'}"
                if cycle_continuity == "confirmed"
                else f"cycle_{cycle}: {scroll_word} -> overlap_missing -> recovery_required"
            )
        else:
            cycle_state = "reset_then_changed" if cycle_reset_then_changed else ("hydrating" if cycle_classification == "list_hydrating" else cycle_classification)
            cycle_summaries.append(f"cycle_{cycle}: {scroll_word} -> {cycle_state} -> {cycle_new_rows}_new_rows -> {'settled' if cycle_settled else 'still_hydrating'}")

        if cycle_continuity == "not_confirmed":
            continuity_report["scan_continuity"] = "not_confirmed"
            return finish(
                "chat_list_scan_continuity_not_confirmed",
                current_plan,
                cycle,
                cycle,
                "not_confirmed" if bottom_evidence_seen else "unknown",
                "Adjacent settled viewports did not retain contiguous shared rows and bounded recovery could not restore continuous coverage; intervening rows cannot be asserted as scanned.",
            )

        if current_plan["status"] == "ready" or observation.get("target_found") or recovery.get("target_found"):
            stable_found = current_plan
            _accumulate_project_chat_seen_rows(stable_found, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
            if observation.get("target_found") and not target_found_during_hydration_cycle:
                target_found_during_hydration_cycle = cycle
            detection_in = (
                recovery.get("target_detected_in")
                if "recovery" in locals() and isinstance(recovery, dict) and recovery.get("target_found")
                else observation.get("target_detected_in")
            ) or ("settled" if cycle_settled else "hydration")
            target_detection_record = (
                _project_chat_target_detection_from_observation(recovery, detection_in, cycle)
                if recovery.get("target_found")
                else _project_chat_target_detection_from_observation(observation, detection_in, cycle)
            )
            _emit_project_chat_target_detected(discovery_state or {}, chat_title, detection_in, cycle)
            return {
                **finish("ready", stable_found, cycle, cycle, "confirmed" if _project_chat_end_of_list_confirmed(stable_found) else ("not_confirmed" if bottom_evidence_seen else "unknown"), ""),
                "plan": stable_found,
                "target_found_after_scrolling": True,
                "target_exact_match_detected": True,
                "target_detected_in": detection_in,
                "target_detected_cycle": cycle,
                "scroll_pulses_after_target_detection": 0,
            }
        if current_plan["status"] not in {"chat_not_currently_visible", "chat_title_not_unambiguously_representable_by_accessibility"}:
            return finish(current_plan["status"], current_plan, cycle, cycle, "not_confirmed" if bottom_evidence_seen else "unknown", current_plan.get("error") or "")

        # --- Valid stopping rules -------------------------------------------------
        no_meaningful_change = bool(observation.get("no_meaningful_change"))
        scroll_posted_this_cycle = bool(step.get("actions_performed"))
        no_cycle_event = cycle_hydration_events == 0 and cycle_reset_events == 0 and not cycle_reset_then_changed
        # "Quiet": real scoped scroll posted, continuity confirmed, no new rows,
        # no meaningful ordered movement, no reset/hydration event.
        quiet_cycle = (
            scroll_posted_this_cycle
            and cycle_continuity == "confirmed"
            and cycle_new_rows == 0
            and no_meaningful_change
            and not moved
            and no_cycle_event
            and cycle_settled
        )
        pre_anchors = _project_chat_viewport_anchor_texts(pre_scroll_state)
        post_anchors = _project_chat_viewport_anchor_texts(post_scroll_state)
        anchors_unchanged = pre_anchors == post_anchors and pre_anchors != ("", "")
        scrollbar_unchanged = pre_scroll_state.get("scrollbar_state") == post_scroll_state.get("scrollbar_state")
        end_qualifying_cycle = quiet_cycle and anchors_unchanged and scrollbar_unchanged

        quiet_streak = quiet_streak + 1 if quiet_cycle else 0
        end_anchor_streak = end_anchor_streak + 1 if end_qualifying_cycle else 0

        if _project_chat_end_of_list_confirmed(current_plan):
            bottom_evidence_seen = True
        # Scrollbar/bottom evidence end (kept), now also requires confirmed continuity.
        if bottom_evidence_seen and quiet_cycle and _project_chat_end_of_list_confirmed(current_plan):
            return finish("chat_list_end_reached_without_match", current_plan, cycle, cycle, "confirmed", "End of project chat list was confirmed by bottom evidence plus a continuity-confirmed unchanged scroll cycle.")
        # Anchor-based end: top-most and bottom-most rows unchanged across two
        # complete forward scroll-plus-settle cycles.
        if end_anchor_streak >= PROJECT_CHAT_END_ANCHOR_CYCLES_REQUIRED:
            return finish("chat_list_end_reached_without_match", current_plan, cycle, cycle, "confirmed", "End of project chat list was confirmed: top-most and bottom-most rows stayed fixed across two continuity-confirmed forward scroll cycles.")
        # No-progress only after two quiet, continuity-confirmed cycles.
        if quiet_streak >= NO_PROGRESS_CYCLE_THRESHOLD:
            return finish("chat_list_scroll_no_progress", current_plan, cycle, cycle, "not_confirmed", "Two complete continuity-confirmed scroll-plus-settle cycles exposed no new rows, no ordered viewport movement, and no reset/hydration events.")

        elapsed = max(0.0, time.monotonic() - started)
        if elapsed >= MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS:
            outcome = "chat_search_time_budget_exhausted_while_list_progressing" if cycle_progressful or not cycle_settled else "chat_search_budget_exhausted_without_confirmed_end"
            error = (
                "Total bounded project-chat search time elapsed while list rows or viewport state were still changing."
                if outcome == "chat_search_time_budget_exhausted_while_list_progressing"
                else "Total bounded project-chat search time elapsed without confirming the end of the project chat list."
            )
            return finish(outcome, current_plan, cycle, cycle, "not_confirmed" if bottom_evidence_seen else "unknown", error)
        if cycle >= MAX_PROJECT_CHAT_SEARCH_CYCLES and not cycle_progressful:
            return finish(
                "chat_search_budget_exhausted_without_confirmed_end",
                current_plan,
                cycle,
                cycle,
                "not_confirmed" if bottom_evidence_seen else "unknown",
                "Maximum bounded search cycles were reached after a settled cycle with no continuing meaningful progress.",
            )
        last_cycle_progressful = cycle_progressful


def _recover_project_chat_scan_continuity(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    current_plan: dict,
    pre_scroll_state: dict,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    scroll_service_factory: object,
    sleep_function: object,
    started: float,
    *,
    current_quantum: int,
    seen_signatures: set[str],
    seen_titles: set[str],
    seen_accessibility_rows: set[str],
    seen_effective_viewports: set[str],
    actions_performed: list[dict],
    discovery_state: dict | None = None,
    discovery_cycle: int = 0,
) -> dict:
    """Restore contiguous coverage after a CoreGraphics jump skipped rows.

    Each attempt reduces the next pixel quantum and posts one small reverse
    list-only pulse (strictly inside the confirmed chat-list viewport), then
    re-settles and re-checks overlap against the pre-scroll viewport. Returns
    once overlap is restored, the target appears, or the bounded recovery budget
    is exhausted.
    """
    hydration_events = 0
    reset_events = 0
    target_match_checked = 0
    hydration_samples = 0
    recovery_pulses = 0
    quantum = max(PROJECT_CHAT_SCROLL_MIN_PIXEL_DELTA, int(current_quantum))
    overlap = _project_chat_viewport_overlap(pre_scroll_state, _project_chat_effective_list_state(current_plan))
    post_state = _project_chat_effective_list_state(current_plan)
    restored = False
    target_found = False
    target_detection_record: dict = {}
    for _attempt in range(PROJECT_CHAT_MAX_RECOVERY_PULSES_PER_CYCLE):
        if max(0.0, time.monotonic() - started) >= MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS:
            break
        quantum = max(PROJECT_CHAT_SCROLL_MIN_PIXEL_DELTA, int(round(quantum * PROJECT_CHAT_SCROLL_QUANTUM_REDUCTION_FACTOR)))
        reverse_target = _project_chat_scroll_target(current_plan)
        if reverse_target.get("status") != "ready":
            break
        step = _perform_project_chat_scroll_step(reader, reverse_target, scroll_service_factory, delta_y_override=quantum)
        if step.get("status") != "ready":
            break
        recovery_pulses += 1
        actions_performed.extend(step.get("actions_performed") or [])
        observation = _observe_project_chat_list_hydration(
            reader,
            pid,
            project_title,
            chat_title,
            current_plan,
            current_plan,
            display_probe_factory,
            windowserver_probe_factory,
            sleep_function,
            timeout_seconds=POST_SCROLL_HYDRATION_TIMEOUT_SECONDS,
            known_accessibility_rows=seen_accessibility_rows,
            require_change_before_early_settle=False,
            discovery_state=discovery_state,
            discovery_cycle=discovery_cycle,
            target_detection_stage="recovery",
        )
        current_plan = observation.get("plan") or current_plan
        _accumulate_observed_project_chat_rows(observation, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)
        hydration_events += int(observation.get("hydration_events_observed") or 0)
        reset_events += int(observation.get("reset_events_observed") or 0)
        target_match_checked += int(observation.get("target_match_checked_on_samples") or 0)
        hydration_samples += int(observation.get("samples_taken") or 0)
        post_state = _project_chat_effective_list_state(current_plan)
        overlap = _project_chat_viewport_overlap(pre_scroll_state, post_state)
        if current_plan.get("status") == "ready" or observation.get("target_found"):
            target_found = True
            target_detection_record = _project_chat_target_detection_from_observation(observation, "recovery", discovery_cycle)
            restored = True
            break
        if overlap.get("adjacency_confirmed"):
            restored = True
            break
    return {
        "restored": bool(restored),
        "plan": current_plan,
        "overlap": overlap,
        "post_state": post_state,
        "target_found": bool(target_found),
        "target_detected_in": "recovery" if target_found else "",
        "target_exact_match_detected": bool(target_detection_record.get("target_exact_match_detected")),
        "normalized_rows_evaluated_for_target": int(target_detection_record.get("normalized_rows_evaluated_for_target") or 0),
        "target_detection_snapshot_generation": target_detection_record.get("target_detection_snapshot_generation") or "",
        "target_detection_row_path": target_detection_record.get("target_detection_row_path") or "",
        "target_detection_row_frame": target_detection_record.get("target_detection_row_frame") or _frame_geometry_report(None),
        "target_detection_canonical_title": target_detection_record.get("target_detection_canonical_title") or "",
        "hydration_events": hydration_events,
        "reset_events": reset_events,
        "target_match_checked": target_match_checked,
        "hydration_samples": hydration_samples,
        "recovery_pulses": recovery_pulses,
        "reduced_quantum": quantum,
    }


def _project_chat_scroll_search_result(
    status: str,
    plan: dict,
    seen_signatures: set[str],
    seen_titles: set[str],
    seen_accessibility_rows: set[str],
    seen_effective_viewports: set[str],
    scroll_target: dict,
    cycles: int,
    scroll_pulses: int,
    end_state: str,
    error: str,
    actions_performed: list[dict] | None = None,
    *,
    started_at: float | None = None,
    initial_hydration_status: str = "",
    hydration_events: int = 0,
    reset_events: int = 0,
    cycle_summaries: list[str] | None = None,
    initial_accessibility_row_count: int = 0,
    target_match_checked_on_samples: int = 0,
    hydration_samples_taken: int = 0,
    settled_cycles_completed: int = 0,
    progressful_cycles_completed: int = 0,
    target_found_during_hydration_cycle: int = 0,
    continuity_report: dict | None = None,
    target_detection_record: dict | None = None,
) -> dict:
    continuity_report = continuity_report or {}
    result = {
        "status": status,
        "plan": plan,
        "scroll_iterations_attempted": int(scroll_pulses),
        "max_scroll_iterations": PROJECT_CHAT_SCROLL_MAX_ITERATIONS,
        "search_cycles_attempted": int(cycles),
        "max_search_cycles": MAX_CHAT_SEARCH_CYCLES,
        "configured_max_search_cycles": MAX_PROJECT_CHAT_SEARCH_CYCLES,
        "configured_max_search_elapsed_seconds": MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS,
        "scroll_pulses_posted": int(scroll_pulses),
        "scroll_method_used": scroll_target.get("method") or "",
        "scroll_target_available": scroll_target.get("status") == "ready",
        "target_found_after_scrolling": False,
        "unique_row_count_seen": len(seen_signatures),
        "visible_title_count_seen": len(seen_titles),
        "initial_hydration_status": initial_hydration_status,
        "hydration_events_observed": int(hydration_events),
        "reset_events_observed": int(reset_events),
        "unique_accessibility_rows_seen": len(seen_accessibility_rows),
        "unique_effective_viewports_seen": len(seen_effective_viewports),
        "new_accessibility_rows_seen": max(0, len(seen_accessibility_rows) - int(initial_accessibility_row_count)),
        "target_match_checked_on_samples": int(target_match_checked_on_samples),
        "hydration_samples_taken": int(hydration_samples_taken),
        "settled_cycles_completed": int(settled_cycles_completed),
        "progressful_cycles_completed": int(progressful_cycles_completed),
        "target_found_during_hydration_cycle": int(target_found_during_hydration_cycle),
        "end_of_list_state": end_state,
        "computed_scroll_delta_y": int(scroll_target.get("computed_scroll_delta_y") or 0),
        "median_visible_row_height": float(scroll_target.get("median_visible_row_height") or 0.0),
        "previous_settled_viewport_signature": str(continuity_report.get("previous_settled_viewport_signature") or ""),
        "current_settled_viewport_signature": str(continuity_report.get("current_settled_viewport_signature") or ""),
        "overlap_row_count": int(continuity_report.get("overlap_row_count") or 0),
        "overlap_adjacency_confirmed": bool(continuity_report.get("overlap_adjacency_confirmed", True)),
        "scan_continuity": str(continuity_report.get("scan_continuity") or "confirmed"),
        "recovery_scroll_pulses_posted": int(continuity_report.get("recovery_scroll_pulses_posted") or 0),
        "search_elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3) if started_at is not None else 0.0,
        "search_cycle_summaries": cycle_summaries or [],
        "actions_performed": actions_performed or [],
        "error": error,
    }
    if target_detection_record:
        result.update(
            {
                "target_exact_match_detected": bool(target_detection_record.get("target_exact_match_detected")),
                "target_detected_in": target_detection_record.get("target_detected_in") or "",
                "target_detected_cycle": int(target_detection_record.get("target_detected_cycle") or 0),
                "normalized_rows_evaluated_for_target": int(target_detection_record.get("normalized_rows_evaluated_for_target") or 0),
                "target_detection_snapshot_generation": target_detection_record.get("target_detection_snapshot_generation") or "",
                "target_detection_row_path": target_detection_record.get("target_detection_row_path") or "",
                "target_detection_row_frame": target_detection_record.get("target_detection_row_frame") or _frame_geometry_report(None),
                "target_detection_canonical_title": target_detection_record.get("target_detection_canonical_title") or "",
            }
        )
    return result


def _observe_project_chat_list_hydration(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    prior_plan: dict,
    first_plan: dict,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
    *,
    timeout_seconds: float,
    known_accessibility_rows: set[str] | None = None,
    require_change_before_early_settle: bool = False,
    discovery_state: dict | None = None,
    discovery_cycle: int = 0,
    target_detection_stage: str = "hydration",
) -> dict:
    max_samples = max(REQUIRED_STABLE_SAMPLES_AFTER_CHANGE + 1, int(math.ceil(timeout_seconds / max(HYDRATION_SAMPLE_INTERVAL_SECONDS, 0.01))))
    plans: list[dict] = [first_plan]
    states = [_project_chat_effective_list_state(first_plan)]
    stable_samples = 1 if states[0].get("available") else 0
    reset_observed = False
    observed_changed = False
    observed_unavailable = not states[0].get("available")
    baseline = states[0]
    last_state = states[0]
    seen_state_keys = {_project_chat_effective_list_state(prior_plan).get("state_key") or "", baseline.get("state_key") or ""}
    known_rows = set(known_accessibility_rows or set())
    observed_rows = set(_project_chat_accessibility_row_texts_from_state(baseline))
    new_rows_seen: set[str] = set()
    target_detection_record = _project_chat_target_detection_from_plan(first_plan, chat_title, target_detection_stage, discovery_cycle)
    target_found = bool(target_detection_record.get("target_exact_match_detected")) or first_plan.get("status") == "ready"
    target_detected_in = target_detection_stage if target_found else ""
    if discovery_state is not None:
        _emit_project_chat_discovered_titles(discovery_state, first_plan, cycle=discovery_cycle)
    if target_found:
        return {
            "classification": "list_stable_no_change",
            "plan": first_plan,
            "plans": plans,
            "states": states,
            "new_accessibility_rows": 0,
            "no_meaningful_change": True,
            "reset_then_changed": False,
            "hydration_events_observed": 0,
            "reset_events_observed": 0,
            "list_unavailable": observed_unavailable,
            "settled": bool(states[0].get("available")),
            "meaningful_change": False,
            "target_found": True,
            "target_detected_in": target_detected_in,
            "target_exact_match_detected": bool(target_detection_record.get("target_exact_match_detected")),
            "normalized_rows_evaluated_for_target": int(target_detection_record.get("normalized_rows_evaluated_for_target") or 0),
            "target_detection_snapshot_generation": target_detection_record.get("target_detection_snapshot_generation") or "",
            "target_detection_row_path": target_detection_record.get("target_detection_row_path") or "",
            "target_detection_row_frame": target_detection_record.get("target_detection_row_frame") or _frame_geometry_report(None),
            "target_detection_canonical_title": target_detection_record.get("target_detection_canonical_title") or "",
            "samples_taken": 0,
            "target_match_checked_on_samples": 0,
        }
    samples_taken = 0
    target_match_checked = 0
    for sample_index in range(max_samples):
        sleep_function(HYDRATION_SAMPLE_INTERVAL_SECONDS)
        plan = _fresh_project_chat_targeting_plan(
            reader,
            pid,
            project_title,
            chat_title,
            display_probe_factory,
            windowserver_probe_factory,
            sleep_function,
        )
        state = _project_chat_effective_list_state(plan)
        plans.append(plan)
        states.append(state)
        if discovery_state is not None:
            _emit_project_chat_discovered_titles(discovery_state, plan, cycle=discovery_cycle)
        samples_taken += 1
        target_match_checked += 1
        sample_detection = _project_chat_target_detection_from_plan(plan, chat_title, target_detection_stage, discovery_cycle)
        if sample_detection.get("target_exact_match_detected"):
            target_detection_record = sample_detection
            target_found = True
            target_detected_in = target_detection_stage
            break
        if not state.get("available"):
            observed_unavailable = True
            stable_samples = 0
            last_state = state
            if plan.get("status") == "ready":
                target_found = True
                target_detected_in = target_detection_stage
                target_detection_record = sample_detection
                break
            continue
        state_key = state.get("state_key") or ""
        current_rows = _project_chat_accessibility_row_texts_from_state(state)
        newly_observed_rows = current_rows - known_rows - observed_rows - new_rows_seen
        if newly_observed_rows:
            new_rows_seen.update(newly_observed_rows)
        reset_like_state = state_key != (last_state.get("state_key") or "") and _project_chat_state_looks_like_reset(state, baseline, seen_state_keys, known_rows)
        if reset_like_state and not reset_observed:
            reset_observed = True
        if reset_observed and newly_observed_rows:
            observed_changed = True
        row_or_state_changed = (
            bool(newly_observed_rows)
            or state.get("ordered_signature") != last_state.get("ordered_signature")
            or state.get("row_count") != last_state.get("row_count")
            or state.get("viewport_state") != last_state.get("viewport_state")
            or state.get("scrollbar_state") != last_state.get("scrollbar_state")
            or reset_like_state
        )
        if row_or_state_changed:
            if state_key != (baseline.get("state_key") or ""):
                observed_changed = True
            stable_samples = 1
        elif state_key == (last_state.get("state_key") or ""):
            stable_samples += 1
        else:
            if state_key != (baseline.get("state_key") or ""):
                observed_changed = True
            stable_samples = 1
        observed_rows.update(current_rows)
        seen_state_keys.add(state_key)
        last_state = state
        if plan.get("status") == "ready":
            target_found = True
            target_detected_in = target_detection_stage
            target_detection_record = sample_detection
            break
        if stable_samples >= REQUIRED_STABLE_SAMPLES_AFTER_CHANGE and (observed_changed or not require_change_before_early_settle):
            break

    final_plan = plans[-1]
    final_state = states[-1]
    final_rows = _project_chat_accessibility_row_texts_from_state(final_state)
    reset_then_changed = bool(reset_observed and (final_rows - known_rows))
    if not final_state.get("available"):
        classification = "list_unavailable"
    elif reset_then_changed:
        classification = "list_reset_then_changed"
    elif target_found:
        classification = "list_advanced" if (observed_changed or final_state.get("state_key") != baseline.get("state_key")) else "list_stable_no_change"
    elif stable_samples < REQUIRED_STABLE_SAMPLES_AFTER_CHANGE:
        classification = "list_hydrating"
    elif final_state.get("state_key") == baseline.get("state_key"):
        classification = "list_stable_no_change"
    else:
        classification = "list_advanced"
    accessibility_rows_before = _project_chat_accessibility_row_texts_from_state(baseline)
    accessibility_rows_after: set[str] = set()
    for state in states:
        accessibility_rows_after.update(_project_chat_accessibility_row_texts_from_state(state))
    meaningful_change = any(
        (state.get("ordered_signature") != baseline.get("ordered_signature"))
        or (state.get("row_count") != baseline.get("row_count"))
        or (state.get("viewport_state") != baseline.get("viewport_state"))
        or (state.get("scrollbar_state") != baseline.get("scrollbar_state"))
        for state in states[1:]
    ) or bool(accessibility_rows_after - known_rows) or bool(reset_then_changed)
    return {
        "classification": classification,
        "plan": final_plan,
        "plans": plans,
        "states": states,
        "new_accessibility_rows": len(accessibility_rows_after - known_rows),
        "no_meaningful_change": not meaningful_change and stable_samples >= REQUIRED_STABLE_SAMPLES and final_state.get("state_key") == baseline.get("state_key"),
        "reset_then_changed": bool(reset_then_changed and final_state.get("state_key") != baseline.get("state_key")),
        "hydration_events_observed": 1 if classification in {"list_advanced", "list_reset_then_changed", "list_hydrating"} else 0,
        "reset_events_observed": 1 if reset_observed else 0,
        "list_unavailable": observed_unavailable,
        "settled": stable_samples >= REQUIRED_STABLE_SAMPLES_AFTER_CHANGE and final_state.get("available"),
        "meaningful_change": bool(meaningful_change),
        "target_found": bool(target_found),
        "target_detected_in": target_detected_in,
        "target_exact_match_detected": bool(target_detection_record.get("target_exact_match_detected")),
        "normalized_rows_evaluated_for_target": int(target_detection_record.get("normalized_rows_evaluated_for_target") or 0),
        "target_detection_snapshot_generation": target_detection_record.get("target_detection_snapshot_generation") or "",
        "target_detection_row_path": target_detection_record.get("target_detection_row_path") or "",
        "target_detection_row_frame": target_detection_record.get("target_detection_row_frame") or _frame_geometry_report(None),
        "target_detection_canonical_title": target_detection_record.get("target_detection_canonical_title") or "",
        "samples_taken": samples_taken,
        "target_match_checked_on_samples": target_match_checked,
    }


def _stable_found_project_chat_plan(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
    fallback_plan: dict,
) -> dict:
    stable = _fresh_project_chat_open_plan(
        reader,
        pid,
        project_title,
        chat_title,
        display_probe_factory,
        windowserver_probe_factory,
        sleep_function,
    )
    return stable if stable.get("status") == "ready" else fallback_plan


def _accumulate_observed_project_chat_rows(
    observation: dict,
    seen_signatures: set[str],
    seen_titles: set[str],
    seen_accessibility_rows: set[str],
    seen_effective_viewports: set[str],
) -> None:
    for plan in observation.get("plans") or []:
        _accumulate_project_chat_seen_rows(plan, seen_signatures, seen_titles, seen_accessibility_rows, seen_effective_viewports)


def _project_chat_effective_list_state(plan: dict) -> dict:
    resolution = plan.get("project_chat_resolution") or {}
    chat_list = resolution.get("chat_list_container") or {}
    rows = resolution.get("visible_chats") or []
    row_texts = tuple(_project_chat_accessibility_row_text(row) for row in rows if _project_chat_accessibility_row_text(row))
    ordered = tuple(_project_chat_ordered_row_signature(index, row) for index, row in enumerate(rows))
    viewport_state = _project_chat_effective_viewport_signature(plan)
    scrollbar_state = _project_chat_scrollbar_state(plan)
    available = bool(chat_list.get("path")) and resolution.get("status") == "visible_chats_found"
    state_key = "|".join((str(ordered), str(len(rows)), viewport_state, scrollbar_state))
    return {
        "available": available,
        "row_texts": row_texts,
        "ordered_signature": ordered,
        "row_count": len(rows),
        "viewport_state": viewport_state,
        "scrollbar_state": scrollbar_state,
        "state_key": state_key,
    }


def _project_chat_state_looks_like_reset(state: dict, baseline: dict, seen_state_keys: set[str], known_accessibility_rows: set[str]) -> bool:
    if not state.get("available"):
        return False
    state_key = state.get("state_key") or ""
    if state_key == (baseline.get("state_key") or ""):
        return False
    if state_key in seen_state_keys:
        return True
    baseline_rows = set(_project_chat_accessibility_row_texts_from_state(baseline))
    state_rows = set(_project_chat_accessibility_row_texts_from_state(state))
    if state_rows and known_accessibility_rows and state_rows.issubset(known_accessibility_rows):
        return True
    return bool(state_rows and baseline_rows and state_rows.issubset(baseline_rows))


def _project_chat_accessibility_row_texts_from_state(state: dict) -> set[str]:
    return {text for text in state.get("row_texts") or () if text}


def _project_chat_ordered_row_texts_from_state(state: dict) -> list[str]:
    return [text for text in state.get("row_texts") or () if text]


def _project_chat_viewport_anchor_texts(state: dict) -> tuple[str, str]:
    ordered = _project_chat_ordered_row_texts_from_state(state)
    if not ordered:
        return ("", "")
    return (ordered[0], ordered[-1])


def _project_chat_longest_contiguous_overlap(prior: list[str], current: list[str]) -> int:
    """Longest run of rows that appears contiguously, in order, in both viewports.

    Identity is the normalized accessibility row text, never an AX path, so
    virtualization path churn does not affect the result. Relative adjacency and
    order are preserved (this is a longest-common-substring over row texts, not a
    set intersection), so text merely appearing somewhere in both viewports is
    not treated as overlap.
    """
    if not prior or not current:
        return 0
    best = 0
    lengths = [0] * (len(current) + 1)
    for prior_text in prior:
        next_lengths = [0] * (len(current) + 1)
        for j, current_text in enumerate(current, start=1):
            if prior_text == current_text:
                next_lengths[j] = lengths[j - 1] + 1
                if next_lengths[j] > best:
                    best = next_lengths[j]
        lengths = next_lengths
    return best


def _project_chat_viewport_overlap(prior_state: dict, current_state: dict) -> dict:
    prior = _project_chat_ordered_row_texts_from_state(prior_state)
    current = _project_chat_ordered_row_texts_from_state(current_state)
    overlap_count = _project_chat_longest_contiguous_overlap(prior, current)
    # Require two shared adjacent rows when both viewports have enough rows; for
    # short viewports use the strongest feasible rule (every available row).
    required = min(PROJECT_CHAT_REQUIRED_OVERLAP_ROWS, len(prior), len(current))
    confirmed = required >= 1 and overlap_count >= required
    return {
        "overlap_row_count": int(overlap_count),
        "required_overlap_rows": int(required),
        "adjacency_confirmed": bool(confirmed),
        "prior_row_count": len(prior),
        "current_row_count": len(current),
    }


def _project_chat_ordered_row_signature(index: int, row: dict) -> str:
    text = _project_chat_accessibility_row_text(row)
    frame = _frame_tuple(row.get("row_frame"))
    if frame is None:
        frame_bucket = "frame:none"
    else:
        frame_bucket = "frame:%d:%d:%d:%d" % tuple(round(value / 12.0) for value in frame)
    return f"{index}:{text}|{frame_bucket}"


def _project_chat_effective_viewport_signature(plan: dict) -> str:
    resolution = plan.get("project_chat_resolution") or {}
    chat_list = resolution.get("chat_list_container") or {}
    frame = _frame_tuple(chat_list.get("frame"))
    if frame is None:
        frame_bucket = "viewport:none"
    else:
        frame_bucket = "viewport:%d:%d:%d:%d" % tuple(round(value / 12.0) for value in frame)
    return f"{frame_bucket}|more_below:{resolution.get('more_rows_may_exist_below')}"


def _project_chat_scrollbar_state(plan: dict) -> str:
    resolution = plan.get("project_chat_resolution") or {}
    chat_list = resolution.get("chat_list_container") or {}
    list_path = chat_list.get("path") or ""
    values: list[str] = []
    for snapshot in plan.get("snapshots") or []:
        if snapshot.role != "AXScrollBar":
            continue
        if list_path and not snapshot.path.startswith(list_path + "."):
            continue
        value = _normalized_label(str(snapshot.value or snapshot.title or snapshot.description or ""))
        values.append(value)
    return "scrollbar:" + ",".join(values)


def _project_chat_scroll_search_result_fields(search: dict) -> dict:
    return {
        "scroll_iterations_attempted": int(search.get("scroll_iterations_attempted") or 0),
        "max_scroll_iterations": int(search.get("max_scroll_iterations") or PROJECT_CHAT_SCROLL_MAX_ITERATIONS),
        "search_cycles_attempted": int(search.get("search_cycles_attempted") or 0),
        "max_search_cycles": int(search.get("max_search_cycles") or MAX_CHAT_SEARCH_CYCLES),
        "configured_max_search_cycles": int(search.get("configured_max_search_cycles") or MAX_PROJECT_CHAT_SEARCH_CYCLES),
        "configured_max_search_elapsed_seconds": float(search.get("configured_max_search_elapsed_seconds") or MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS),
        "scroll_pulses_posted": int(search.get("scroll_pulses_posted") or 0),
        "scroll_method_used": search.get("scroll_method_used") or "",
        "scroll_target_available": bool(search.get("scroll_target_available")),
        "target_found_after_scrolling": bool(search.get("target_found_after_scrolling")),
        "target_exact_match_detected": bool(search.get("target_exact_match_detected")),
        "target_detected_in": search.get("target_detected_in") or "",
        "target_detected_cycle": int(search.get("target_detected_cycle") or 0),
        "scroll_pulses_after_target_detection": int(search.get("scroll_pulses_after_target_detection") or 0),
        "initial_hydration_status": search.get("initial_hydration_status") or "",
        "hydration_events_observed": int(search.get("hydration_events_observed") or 0),
        "reset_events_observed": int(search.get("reset_events_observed") or 0),
        "unique_accessibility_rows_seen": int(search.get("unique_accessibility_rows_seen") or 0),
        "unique_effective_viewports_seen": int(search.get("unique_effective_viewports_seen") or 0),
        "new_accessibility_rows_seen": int(search.get("new_accessibility_rows_seen") or 0),
        "target_match_checked_on_samples": int(search.get("target_match_checked_on_samples") or 0),
        "hydration_samples_taken": int(search.get("hydration_samples_taken") or 0),
        "settled_cycles_completed": int(search.get("settled_cycles_completed") or 0),
        "progressful_cycles_completed": int(search.get("progressful_cycles_completed") or 0),
        "target_found_during_hydration_cycle": int(search.get("target_found_during_hydration_cycle") or 0),
        "search_elapsed_seconds": float(search.get("search_elapsed_seconds") or 0.0),
        "search_cycle_summaries": search.get("search_cycle_summaries") or [],
        "unique_row_count_seen": int(search.get("unique_row_count_seen") or 0),
        "visible_title_count_seen": int(search.get("visible_title_count_seen") or 0),
        "end_of_list_state": search.get("end_of_list_state") or "unknown",
        "computed_scroll_delta_y": int(search.get("computed_scroll_delta_y") or 0),
        "median_visible_row_height": float(search.get("median_visible_row_height") or 0.0),
        "previous_settled_viewport_signature": search.get("previous_settled_viewport_signature") or "",
        "current_settled_viewport_signature": search.get("current_settled_viewport_signature") or "",
        "overlap_row_count": int(search.get("overlap_row_count") or 0),
        "overlap_adjacency_confirmed": bool(search.get("overlap_adjacency_confirmed", True)),
        "scan_continuity": search.get("scan_continuity") or "confirmed",
        "recovery_scroll_pulses_posted": int(search.get("recovery_scroll_pulses_posted") or 0),
    }


def _apply_project_chat_search_observation(result: dict, plan: dict) -> None:
    signatures: set[str] = set()
    titles: set[str] = set()
    accessibility_rows: set[str] = set()
    effective_viewports: set[str] = set()
    _accumulate_project_chat_seen_rows(plan, signatures, titles, accessibility_rows, effective_viewports)
    result["unique_row_count_seen"] = len(signatures)
    result["visible_title_count_seen"] = len(titles)
    result["unique_accessibility_rows_seen"] = len(accessibility_rows)
    result["unique_effective_viewports_seen"] = len(effective_viewports)
    result["end_of_list_state"] = "confirmed" if _project_chat_end_of_list_confirmed(plan) else "unknown"


def _accumulate_project_chat_seen_rows(
    plan: dict,
    seen_signatures: set[str],
    seen_titles: set[str],
    seen_accessibility_rows: set[str] | None = None,
    seen_effective_viewports: set[str] | None = None,
) -> None:
    resolution = plan.get("project_chat_resolution") or {}
    if seen_effective_viewports is not None:
        viewport = _project_chat_effective_list_state(plan).get("state_key") or ""
        if viewport:
            seen_effective_viewports.add(viewport)
    for row in resolution.get("visible_chats") or []:
        signature = _project_chat_row_signature(row)
        if signature:
            seen_signatures.add(signature)
        accessibility_text = _project_chat_accessibility_row_text(row)
        if accessibility_text and seen_accessibility_rows is not None:
            seen_accessibility_rows.add(accessibility_text)
        title = _normalized_label(row.get("title") or "")
        if title:
            seen_titles.add(title)


def _project_chat_row_signature(row: dict) -> str:
    raw_text = _project_chat_accessibility_row_text(row)
    frame = _frame_tuple(row.get("row_frame"))
    if frame is None:
        frame_bucket = "frame:none"
    else:
        frame_bucket = "frame:%d:%d:%d:%d" % tuple(round(value / 12.0) for value in frame)
    if not raw_text:
        return ""
    return f"text:{raw_text}|{frame_bucket}"


def _project_chat_accessibility_row_text(row: dict) -> str:
    return _normalized_label(row.get("accessibility_row_text") or row.get("title") or "")


def _project_chat_end_of_list_confirmed(plan: dict) -> bool:
    resolution = plan.get("project_chat_resolution") or {}
    return resolution.get("more_rows_may_exist_below") is False


def _project_chat_scroll_target(plan: dict) -> dict:
    resolution = plan.get("project_chat_resolution") or {}
    snapshots_by_path = {snapshot.path: snapshot for snapshot in plan.get("snapshots") or []}
    chat_list = resolution.get("chat_list_container") or {}
    content = resolution.get("project_content_container") or resolution.get("main_project_content") or {}
    list_path = chat_list.get("path") or ""
    if not list_path:
        return {"status": "target_not_found", "error": "Project Chats list container was not resolved."}
    rows = resolution.get("visible_chats") or []
    row_frames = [_frame_tuple(row.get("row_frame")) for row in rows]
    row_frames = [frame for frame in row_frames if frame is not None]
    for path in _path_and_ancestors(list_path):
        snapshot = snapshots_by_path.get(path)
        if snapshot is None:
            continue
        target = _project_chat_scroll_target_from_snapshot(snapshot, row_frames, content, plan)
        if target.get("status") == "ready":
            return target
    return {"status": "target_unavailable", "error": "No confirmed project Chats list scroll target was available."}


def _path_and_ancestors(path: str) -> list[str]:
    parts = path.split(".")
    return [".".join(parts[:index]) for index in range(len(parts), 0, -1)]


def _project_chat_scroll_target_from_snapshot(
    snapshot: AXElementSnapshot,
    row_frames: list[tuple[float, float, float, float]],
    content: dict,
    plan: dict,
) -> dict:
    frame = _frame_tuple(snapshot.frame)
    content_frame = _frame_tuple(content.get("frame"))
    if snapshot.role not in {"AXScrollArea", "AXList", "AXTable", "AXOutline", "AXGroup"}:
        return {"status": "target_unavailable"}
    if not _frame_is_valid(frame) or not _frame_contains(content_frame, frame):
        return {"status": "target_unavailable"}
    if row_frames and not all(_frame_intersects(frame, row_frame) for row_frame in row_frames):
        return {"status": "target_unavailable"}
    median_row_height = _project_chat_median_row_height(row_frames)
    computed_delta = _project_chat_computed_scroll_delta_y(median_row_height)
    point = _project_chat_scroll_point(frame, plan)
    actions = _safe_actions(snapshot.actions)
    for action in PROJECT_CHAT_SCROLL_ACTIONS:
        if action in actions:
            return {
                "status": "ready",
                "method": "semantic_ax_scroll",
                "path": snapshot.path,
                "action": action,
                "frame": _frame_geometry_report(frame),
                "median_visible_row_height": round(median_row_height, 2),
                "computed_scroll_delta_y": computed_delta,
                # A CoreGraphics point is computed even for the semantic path so a
                # bounded reverse recovery pulse can restore contiguous coverage.
                "point": _xy_report(point) if point is not None else _xy_report(None),
            }
    if point is None:
        return {"status": "target_unavailable"}
    return {
        "status": "ready",
        "method": "coregraphics_scroll",
        "path": snapshot.path,
        "frame": _frame_geometry_report(frame),
        "point": _xy_report(point),
        "median_visible_row_height": round(median_row_height, 2),
        "computed_scroll_delta_y": computed_delta,
    }


def _project_chat_median_row_height(row_frames: list[tuple[float, float, float, float]]) -> float:
    heights = sorted(float(frame[3]) for frame in row_frames if frame is not None and frame[3] > 0)
    if not heights:
        return 0.0
    mid = len(heights) // 2
    if len(heights) % 2:
        return heights[mid]
    return (heights[mid - 1] + heights[mid]) / 2.0


def _project_chat_computed_scroll_delta_y(median_row_height: float) -> int:
    """Forward (downward) CoreGraphics pixel delta derived from row height.

    The magnitude advances at most ``PROJECT_CHAT_SCROLL_MAX_ROW_HEIGHTS_PER_PULSE``
    of a row so adjacent settled viewports keep meaningful shared rows. It is
    clamped to a conservative pixel band and never falls back to a fixed coarse
    value. A negative value scrolls the list downward.
    """
    if median_row_height and median_row_height > 0:
        raw = median_row_height * PROJECT_CHAT_SCROLL_MAX_ROW_HEIGHTS_PER_PULSE
    else:
        raw = float(PROJECT_CHAT_SCROLL_MIN_PIXEL_DELTA)
    clamped = max(float(PROJECT_CHAT_SCROLL_MIN_PIXEL_DELTA), min(float(PROJECT_CHAT_SCROLL_MAX_PIXEL_DELTA), raw))
    return -int(round(clamped))


def _project_chat_scroll_point(frame: tuple[float, float, float, float], plan: dict) -> tuple[float, float] | None:
    if not _frame_is_valid(frame):
        return None
    x = frame[0] + min(max(frame[2] * 0.5, SAFE_CLICK_EDGE_INSET), max(SAFE_CLICK_EDGE_INSET, frame[2] - SAFE_CLICK_EDGE_INSET))
    y = frame[1] + min(max(frame[3] * 0.72, SAFE_CLICK_EDGE_INSET), max(SAFE_CLICK_EDGE_INSET, frame[3] - SAFE_CLICK_EDGE_INSET))
    point = (x, y)
    if not _point_inside_frame(point, frame):
        return None
    if not _point_inside_frame(point, plan.get("ax_window_frame")) or not _point_inside_frame(point, plan.get("windowserver_bounds")):
        return None
    if not any(_point_inside_frame(point, display) for display in plan.get("display_bounds") or []):
        return None
    return point


def _perform_project_chat_scroll_step(
    reader: object,
    scroll_target: dict,
    scroll_service_factory: object,
    *,
    delta_y_override: int | None = None,
) -> dict:
    # A reverse recovery pulse always goes through CoreGraphics so it can be a
    # small, list-scoped amount regardless of the forward method.
    if scroll_target.get("method") == "semantic_ax_scroll" and delta_y_override is None:
        path = scroll_target.get("path") or ""
        action = scroll_target.get("action") or ""
        try:
            _invoke_reader_ax_action(reader, path, action)
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "actions_performed": []}
        return {"status": "ready", "error": "", "actions_performed": [{"path": path, "action": action}]}
    point = scroll_target.get("point") or {}
    if point.get("x") is None or point.get("y") is None:
        return {"status": "failed", "error": "No CoreGraphics scroll point was available for the project chat list.", "actions_performed": []}
    delta_y = int(delta_y_override) if delta_y_override is not None else int(scroll_target.get("computed_scroll_delta_y") or _project_chat_computed_scroll_delta_y(0.0))
    try:
        scroller = scroll_service_factory()
        if not scroller.has_permission():
            return {"status": "failed", "error": "CoreGraphics post-event permission is unavailable.", "actions_performed": []}
        posted = scroller.scroll_down(point["x"], point["y"], delta_y)
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "actions_performed": []}
    if not posted.get("ok"):
        return {"status": "failed", "error": posted.get("error") or "CoreGraphics scroll could not be posted.", "actions_performed": posted.get("actions_performed") or []}
    return {"status": "ready", "error": "", "actions_performed": posted.get("actions_performed") or []}


def _fresh_project_chat_targeting_plan(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
) -> dict:
    del sleep_function
    try:
        snapshots, stats, window_metadata = reader.collect(pid)
        ax_window_frame = _window_frame_from_metadata(window_metadata, snapshots)
        windows = windowserver_probe_factory().visible_windows_for_pid(pid)
        windowserver_window = _choose_windowserver_window_for_ax_frame(windows, ax_window_frame)
        windowserver_bounds = _frame_tuple((windowserver_window or {}).get("bounds"))
    except Exception as exc:
        return {"status": "post_action_inspection_unavailable", "error": str(exc)}
    if not snapshots or not _frame_is_valid(ax_window_frame) or not _frame_is_valid(windowserver_bounds):
        return {"status": "post_action_inspection_unavailable", "error": "Fresh ChatGPT AX tree or window bounds were unavailable."}
    return _project_chat_open_plan_from_snapshots(
        snapshots,
        stats,
        window_metadata,
        ax_window_frame,
        windowserver_bounds,
        project_title,
        chat_title,
        display_probe_factory,
    )


def _fresh_project_chat_open_plan(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    sleep_function: object,
) -> dict:
    stable = _stable_chatgpt_geometry_sample(
        reader,
        pid,
        windowserver_probe_factory,
        sample_count=2,
        sleep_function=sleep_function,
    )
    if stable["status"] != "stable":
        return {"status": "post_action_inspection_unavailable", "error": stable.get("error") or "Fresh ChatGPT geometry did not stabilize."}
    return _project_chat_open_plan_from_snapshots(
        stable["snapshots"],
        stable["stats"],
        stable["window_metadata"],
        stable.get("ax_window_frame") or _window_frame_from_metadata(stable["window_metadata"], stable["snapshots"]),
        stable.get("windowserver_bounds"),
        project_title,
        chat_title,
        display_probe_factory,
    )


def _project_chat_open_plan_from_snapshots(
    snapshots: list[AXElementSnapshot],
    stats: dict,
    window_metadata: dict,
    ax_window_frame: tuple[float, float, float, float] | None,
    windowserver_bounds: tuple[float, float, float, float] | None,
    project_title: str,
    chat_title: str,
    display_probe_factory: object,
) -> dict:
    resolution = resolve_open_project_content_and_visible_chats(
        project_title,
        snapshots,
        ax_window_frame,
        traversal_stats=stats,
        window_metadata=window_metadata,
    )
    base = {
        "status": "ready",
        "error": "",
        "snapshots": snapshots,
        "stats": stats,
        "window_metadata": window_metadata,
        "project_chat_resolution": resolution,
        "resolver_snapshot_id": _project_chat_resolver_snapshot_id(snapshots),
        "ax_window_frame": ax_window_frame,
        "windowserver_bounds": windowserver_bounds,
        "display_bounds": _collect_display_bounds_without_cursor(display_probe_factory),
        "target_candidate_count": 0,
    }
    if resolution.get("status") != "visible_chats_found":
        if resolution.get("status") == "project_chat_list_identity_not_confirmed":
            return {**base, "status": "project_chat_list_identity_not_confirmed", "error": resolution.get("error") or "Project Chats-list identity could not be confirmed; no chat interaction was attempted."}
        if resolution.get("project_identity_confirmed"):
            return {**base, "status": "project_opened_but_chats_not_available", "error": resolution.get("error") or "Project Chats content was not available."}
        return {**base, "status": "project_open_failed", "error": resolution.get("error") or "Project opening could not be confirmed."}

    canonical_rows = resolution.get("visible_chats") or []
    base["canonical_visible_chat_titles_considered"] = [row.get("title") or "" for row in canonical_rows]
    base["canonical_visible_chat_count_considered"] = len(canonical_rows)
    base["visible_chat_accessibility_representation_summary"] = _visible_chat_accessibility_representation_summary(canonical_rows)
    match_candidates = []
    malformed_rows = 0
    for row in canonical_rows:
        representation = _project_chat_row_match_representation(row, chat_title)
        if representation["matched"]:
            match_candidates.append((row, representation))
        elif representation["reason"] == "empty_or_malformed_accessibility_text":
            malformed_rows += 1
    matches = [item[0] for item in match_candidates]
    base["target_candidate_count"] = len(matches)
    if not matches:
        status = (
            "chat_title_not_unambiguously_representable_by_accessibility"
            if "," in chat_title or (canonical_rows and malformed_rows == len(canonical_rows))
            else "chat_not_currently_visible"
        )
        error = (
            "Requested chat title contains a comma and was not represented by an explicit exact AXTitle."
            if "," in chat_title
            else "Requested chat title was not represented by exactly matching visible accessibility row text."
        )
        return {**base, "status": status, "error": error}
    if len(matches) > 1:
        return {**base, "status": "chat_title_ambiguous", "error": "More than one currently visible chat row matched the requested title representation."}

    row = matches[0]
    matched_representation = match_candidates[0][1]
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    title_snapshot = snapshots_by_path.get(row.get("title_path") or "")
    row_snapshot = snapshots_by_path.get(row.get("row_path") or row.get("path") or "")
    if title_snapshot is None or row_snapshot is None:
        return {**base, "status": "chat_row_not_interactable", "error": "The matching canonical chat row did not retain title and row paths."}
    viewport_frame = _frame_tuple((resolution.get("chat_list_container") or {}).get("frame")) or _frame_tuple((resolution.get("project_content_container") or {}).get("frame"))
    matched_fields = {
        "matched_chat_row": row,
        "matched_title_representation": matched_representation.get("title_representation") or "",
        "matched_preview_representation": matched_representation.get("preview_representation") or "",
        "matched_accessibility_text": matched_representation.get("accessibility_row_text") or "",
        "matched_chat_title": chat_title,
        "title_snapshot": title_snapshot,
        "row_snapshot": row_snapshot,
        "viewport_frame": viewport_frame,
        "scope": {"title_path": title_snapshot.path, "row_path": row_snapshot.path},
        "axpress_target": _project_chat_axpress_target(title_snapshot, row_snapshot),
    }
    interactable = _project_chat_row_interactable(row, viewport_frame, ax_window_frame, windowserver_bounds, base["display_bounds"])
    if not interactable.get("ok"):
        return {
            **base,
            **matched_fields,
            "status": "chat_row_not_interactable",
            "error": interactable.get("error") or "The matching chat row is not safely interactable.",
        }
    return {
        **base,
        **matched_fields,
    }


def _project_chat_row_interactable(
    row: dict,
    viewport_frame: tuple[float, float, float, float] | None,
    ax_window_frame: tuple[float, float, float, float] | None,
    windowserver_bounds: tuple[float, float, float, float] | None,
    display_bounds: list[tuple[float, float, float, float]],
) -> dict:
    title_frame = _frame_tuple(row.get("title_frame"))
    row_frame = _frame_tuple(row.get("row_frame"))
    source_frame = title_frame if _frame_is_valid(title_frame) else row_frame
    if not _frame_is_valid(source_frame):
        return {"ok": False, "error": "The matching chat row has no usable title or row frame."}
    point = _compute_autonomous_safe_click_point(source_frame, "title_frame" if _frame_is_valid(title_frame) else "row_frame")
    point_tuple = _xy_point(point.get("x"), point.get("y"))
    if not point.get("ok") or point_tuple is None:
        return {"ok": False, "error": point.get("reason") or "A safe interior point could not be derived."}
    if not _point_inside_frame(point_tuple, source_frame):
        return {"ok": False, "error": "The derived point is outside the selected chat row/title frame."}
    if not _point_inside_frame(point_tuple, viewport_frame):
        return {"ok": False, "error": "The derived point is outside the visible project chat-list viewport."}
    if not _point_inside_frame(point_tuple, ax_window_frame) or not _point_inside_frame(point_tuple, windowserver_bounds):
        return {"ok": False, "error": "The derived point is outside the current ChatGPT window bounds."}
    if not any(_point_inside_frame(point_tuple, display) for display in display_bounds):
        return {"ok": False, "error": "The derived point is outside all available displays."}
    return {"ok": True, "point": point}


def _project_chat_axpress_target(title_snapshot: AXElementSnapshot, row_snapshot: AXElementSnapshot) -> dict:
    # The structurally resolved row is the primary activation target.  A text
    # child can advertise AXPress without being the control ChatGPT actually
    # uses to open the chat, so never prefer it over the enclosing row.
    row_actions = _safe_actions(row_snapshot.actions)
    if row_snapshot.enabled is not False and "AXPress" in row_actions:
        return {
            "path": row_snapshot.path,
            "relation": "row_node",
            "role": row_snapshot.role,
            "actions": row_actions,
        }

    title_actions = _safe_actions(title_snapshot.actions)
    title_is_actionable_control = title_snapshot.role in {"AXButton", "AXLink"}
    title_is_resolved_row = title_snapshot.path == row_snapshot.path
    if (
        title_snapshot.enabled is not False
        and "AXPress" in title_actions
        and (title_is_actionable_control or title_is_resolved_row)
    ):
        return {
            "path": title_snapshot.path,
            "relation": "title_node" if not title_is_resolved_row else "row_node",
            "role": title_snapshot.role,
            "actions": title_actions,
        }
    return {}


def _project_chat_row_match_representation(row: dict, requested_title: str) -> dict:
    raw_text = _normalized_label(row.get("accessibility_row_text") or "")
    canonical_title = _normalized_label(row.get("title") or "")
    title_representation = row.get("title_representation") or "unresolved"
    preview_representation = row.get("preview_representation") or "unavailable"
    if "," in requested_title:
        if canonical_title == requested_title and row.get("title_source_attribute") == "AXTitle":
            return {
                "matched": True,
                "reason": "",
                "accessibility_row_text": raw_text,
                "title_representation": "exact_axtitle",
                "preview_representation": preview_representation,
            }
        return {
            "matched": False,
            "reason": "comma_title_requires_explicit_exact_axtitle",
            "accessibility_row_text": raw_text,
            "title_representation": "unresolved",
            "preview_representation": "unavailable",
        }
    if canonical_title == requested_title:
        if title_representation == "canonical_accessibility_description_prefix":
            title_representation = "requested_exact_prefix_before_preview_separator"
        return {
            "matched": True,
            "reason": "",
            "accessibility_row_text": raw_text,
            "title_representation": title_representation,
            "preview_representation": preview_representation,
        }
    if not raw_text or _is_punctuation_or_separator_only(raw_text):
        return {
            "matched": False,
            "reason": "empty_or_malformed_accessibility_text",
            "accessibility_row_text": raw_text,
            "title_representation": "unresolved",
            "preview_representation": "unavailable",
        }
    if raw_text == requested_title:
        return {
            "matched": True,
            "reason": "",
            "accessibility_row_text": raw_text,
            "title_representation": "exact_accessibility_text",
            "preview_representation": "unavailable",
        }
    if "," not in requested_title and raw_text.startswith(requested_title + ", "):
        return {
            "matched": True,
            "reason": "",
            "accessibility_row_text": raw_text,
            "title_representation": "requested_exact_prefix_before_preview_separator",
            "preview_representation": "merged_accessibility_suffix",
        }
    return {
        "matched": False,
        "reason": "not_exact_accessibility_text_or_prefix_separator",
        "accessibility_row_text": raw_text,
        "title_representation": "unresolved",
        "preview_representation": "unavailable",
    }


def _visible_chat_accessibility_representation_summary(rows: list[dict]) -> list[dict]:
    summary = []
    for row in rows:
        raw_text = _normalized_label(row.get("accessibility_row_text") or "")
        summary.append(
            {
                "row_path": row.get("row_path") or row.get("path") or "",
                "title": row.get("title") or "",
                "title_representation": row.get("title_representation") or "unresolved",
                "preview_representation": row.get("preview_representation") or "unavailable",
                "accessibility_text_truncated": _truncate_accessibility_diagnostic_text(raw_text),
            }
        )
    return summary


def _truncate_accessibility_diagnostic_text(text: str, limit: int = 72) -> str:
    normalized = _normalized_label(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def _project_chat_plan_result_fields(plan: dict) -> dict:
    resolution = plan.get("project_chat_resolution") or {}
    fields = _project_visible_chats_resolution_fields(resolution) if resolution else {}
    row = plan.get("matched_chat_row") or {}
    axpress_target = plan.get("axpress_target") or {}
    selected_snapshot = None
    selected_path = str(axpress_target.get("path") or "")
    for snapshot in (plan.get("row_snapshot"), plan.get("title_snapshot")):
        if snapshot is not None and snapshot.path == selected_path:
            selected_snapshot = snapshot
            break
    available_actions = sorted(
        set(_safe_actions(getattr(plan.get("row_snapshot"), "actions", ())))
        | set(_safe_actions(getattr(plan.get("title_snapshot"), "actions", ())))
    )
    return {
        **fields,
        "matched_chat_row": _project_chat_row_summary(row),
        "matched_chat_title": plan.get("matched_chat_title") or "",
        "matched_title_representation": plan.get("matched_title_representation") or "",
        "matched_preview_representation": plan.get("matched_preview_representation") or "",
        "matched_accessibility_text_truncated": _truncate_accessibility_diagnostic_text(plan.get("matched_accessibility_text") or ""),
        "targeting_visible_chat_count": int(resolution.get("visible_chat_count") or 0),
        "canonical_visible_chat_titles_considered": plan.get("canonical_visible_chat_titles_considered") or [],
        "canonical_visible_chat_count_considered": int(plan.get("canonical_visible_chat_count_considered") or 0),
        "visible_chat_accessibility_representation_summary": plan.get("visible_chat_accessibility_representation_summary") or [],
        "resolver_snapshot_id": plan.get("resolver_snapshot_id") or "",
        "target_detected": int(plan.get("target_candidate_count") or 0) == 1,
        "target_candidate_count": int(plan.get("target_candidate_count") or 0),
        "actionable_element_resolved": bool(selected_path),
        "selected_element_role": (
            selected_snapshot.role if selected_snapshot is not None else str(axpress_target.get("role") or "")
        ),
        "selected_relation": str(axpress_target.get("relation") or ""),
        "available_ax_actions": available_actions,
    }


def _project_chat_resolver_snapshot_id(snapshots: list[AXElementSnapshot]) -> str:
    digest = hashlib.sha256()
    for snapshot in snapshots:
        frame = _frame_tuple(snapshot.frame)
        digest.update(snapshot.path.encode("utf-8", errors="ignore"))
        digest.update(b"|")
        digest.update(snapshot.role.encode("utf-8", errors="ignore"))
        digest.update(b"|")
        if frame is not None:
            digest.update(("%g,%g,%g,%g" % frame).encode("ascii"))
        digest.update(b"\n")
    return f"ax:{len(snapshots)}:{digest.hexdigest()[:10]}"


def _project_chat_row_summary(row: dict) -> dict:
    if not row:
        return {}
    return {
        "title": row.get("title") or "",
        "preview": row.get("preview") or "",
        "accessibility_row_text": row.get("accessibility_row_text") or "",
        "title_representation": row.get("title_representation") or "",
        "preview_representation": row.get("preview_representation") or "",
        "display_title_source": row.get("display_title_source") or "",
        "title_source_attribute": row.get("title_source_attribute") or "",
        "row_path": row.get("row_path") or row.get("path") or "",
        "title_path": row.get("title_path") or "",
        "row_frame": row.get("row_frame") or _frame_geometry_report(None),
        "title_frame": row.get("title_frame") or _frame_geometry_report(None),
        "row_frame_tuple": _frame_tuple(row.get("row_frame")),
        "title_frame_tuple": _frame_tuple(row.get("title_frame")),
        "role": row.get("role") or row.get("row_role") or "",
        "subrole": row.get("subrole") or row.get("row_subrole") or "",
        "title_role": row.get("title_role") or "",
        "title_subrole": row.get("title_subrole") or "",
        "action_names": row.get("action_names") or [],
        "title_action_names": row.get("title_action_names") or [],
        "visibility": row.get("visibility") or "",
    }


def _project_chat_plan_materially_changed(before: dict, after: dict) -> bool:
    before_row = before.get("matched_chat_row") or {}
    after_row = after.get("matched_chat_row") or {}
    if before_row.get("title") != after_row.get("title"):
        return True
    return _frames_materially_changed(_frame_tuple(before_row.get("row_frame")), _frame_tuple(after_row.get("row_frame"))) or _frames_materially_changed(
        _frame_tuple(before_row.get("title_frame")),
        _frame_tuple(after_row.get("title_frame")),
    )


def _project_chat_validated_click_plan(plan: dict, reader: object, pid: int, requested_chat: str) -> dict:
    row = plan.get("matched_chat_row") or {}
    title_frame = _frame_tuple(row.get("title_frame"))
    row_frame = _frame_tuple(row.get("row_frame"))
    source = {
        "relation": "title_frame" if _frame_is_valid(title_frame) else "row_frame",
        "path": row.get("title_path") if _frame_is_valid(title_frame) else (row.get("row_path") or row.get("path") or ""),
        "frame": title_frame if _frame_is_valid(title_frame) else row_frame,
    }
    point = _compute_autonomous_safe_click_point(source["frame"], source["relation"])
    point_tuple = _xy_point(point.get("x"), point.get("y"))
    if not point.get("ok") or point_tuple is None:
        return {"status": "safe_click_point_unavailable", "error": point.get("reason") or "Could not compute a safe click point.", "click_source": source, "calculated_global_point": point}
    if not _point_inside_frame(point_tuple, source["frame"]):
        return {"status": "safe_click_point_unavailable", "error": "Calculated point was outside the selected chat row/title.", "click_source": source, "calculated_global_point": point}
    if not _point_inside_frame(point_tuple, plan.get("windowserver_bounds")):
        return {"status": "safe_click_point_unavailable", "error": "Calculated point was outside ChatGPT WindowServer bounds.", "click_source": source, "calculated_global_point": point}
    if not _point_inside_frame(point_tuple, plan.get("viewport_frame")):
        return {"status": "safe_click_point_unavailable", "error": "Calculated point was outside the visible project chat-list viewport.", "click_source": source, "calculated_global_point": point}
    if not any(_point_inside_frame(point_tuple, display) for display in plan.get("display_bounds") or []):
        return {"status": "safe_click_point_unavailable", "error": "Calculated point was outside all available display bounds.", "click_source": source, "calculated_global_point": point}
    hit_test = _collect_calculated_point_hit_test(reader, pid, point_tuple, requested_chat)
    relationship = _hit_test_relationship(hit_test, plan.get("scope") or {}, {snapshot.path: snapshot for snapshot in plan.get("snapshots") or []})
    if not _hit_test_relationship_accepts_click(relationship):
        return {
            "status": "calculated_point_hit_test_mismatch",
            "error": "AX hit-test at the calculated point did not resolve to the requested chat row.",
            "click_source": source,
            "calculated_global_point": point,
            "calculated_point_hit_test": hit_test,
            "calculated_point_hit_test_relationship": relationship,
        }
    return {
        "status": "ready",
        "error": "",
        "click_source": source,
        "calculated_global_point": point,
        "calculated_point_hit_test": hit_test,
        "calculated_point_hit_test_relationship": relationship,
    }


def _project_chat_click_result_fields(click_plan: dict) -> dict:
    return {
        "calculated_global_point": click_plan.get("calculated_global_point") or _xy_report(None),
        "calculated_point_hit_test": click_plan.get("calculated_point_hit_test") or {},
        "calculated_point_hit_test_relationship": click_plan.get("calculated_point_hit_test_relationship") or "",
    }


def _project_chat_post_action_inspection(
    reader: object,
    pid: int,
    project_title: str,
    chat_title: str,
    pre_plan: dict,
    *,
    windowserver_probe_factory: object,
    sleep_function: object,
) -> dict:
    sleep_function(min(AUTONOMOUS_OPEN_POST_ACTION_SETTLE_SECONDS, 1.0))
    try:
        stable = _stable_chatgpt_geometry_sample(
            reader,
            pid,
            windowserver_probe_factory,
            sample_count=2,
            sleep_function=sleep_function,
        )
    except Exception as exc:
        return {"inspection_available": False, "confirmed": False, "error": str(exc), "signals": []}
    if stable["status"] != "stable":
        return {
            "inspection_available": False,
            "confirmed": False,
            "error": stable.get("error") or "Post-action ChatGPT UI geometry did not stabilize.",
            "signals": [],
        }
    snapshots = stable["snapshots"]
    resolution = resolve_open_project_content_and_visible_chats(
        project_title,
        snapshots,
        stable.get("ax_window_frame") or _window_frame_from_metadata(stable["window_metadata"], snapshots),
        traversal_stats=stable["stats"],
        window_metadata=stable["window_metadata"],
    )
    signals = _project_chat_verification_signals(snapshots, resolution, chat_title, pre_plan)
    signal_types = {signal.get("type") for signal in signals}
    verification_state = "project_list_active" if resolution.get("status") == "visible_chats_found" else "conversation_or_non_list_view_active"
    confirmed = (
        "active_conversation_identity_outside_chat_list" in signal_types
        and (
            "conversation_structure_present" in signal_types
            or "project_chat_list_not_primary_content" in signal_types
            or "requested_project_chat_row_selected_or_focused" in signal_types
        )
    ) or (
        "requested_project_chat_row_selected_or_focused" in signal_types
        and "conversation_structure_present" in signal_types
    )
    return {
        "inspection_available": True,
        "confirmed": confirmed,
        "verification_state": verification_state,
        "signals": signals,
        "project_chat_resolution_status": resolution.get("status") or "",
        "reason": "" if confirmed else "No reliable active conversation evidence was exposed after the chat open action.",
    }


def _project_chat_verification_signals(
    snapshots: list[AXElementSnapshot],
    resolution: dict,
    chat_title: str,
    pre_plan: dict,
) -> list[dict]:
    signals: list[dict] = []
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    pre_row = pre_plan.get("matched_chat_row") or {}
    row_path = pre_row.get("row_path") or pre_row.get("path") or ""
    title_path = pre_row.get("title_path") or ""
    list_path = ((resolution.get("chat_list_container") or {}).get("path") or (pre_plan.get("project_chat_resolution") or {}).get("chat_list_container", {}).get("path") or "")
    for path, relation in ((row_path, "row"), (title_path, "title")):
        snapshot = snapshots_by_path.get(path)
        if snapshot is not None and (snapshot.selected is True or snapshot.focused is True):
            signals.append({"type": "requested_project_chat_row_selected_or_focused", "relation": relation, "path": path})
            break
    for snapshot in snapshots:
        if snapshot.role not in {"AXHeading", "AXStaticText", "AXButton"}:
            continue
        if _project_text(snapshot) != chat_title:
            continue
        if _path_in_or_under(snapshot.path, row_path) or _path_in_or_under(snapshot.path, list_path):
            continue
        if snapshot.depth <= 8:
            signals.append({"type": "active_conversation_identity_outside_chat_list", "path": snapshot.path, "role": snapshot.role})
            break
    if _conversation_structure_present(snapshots, row_path, list_path):
        signals.append({"type": "conversation_structure_present"})
    if resolution.get("status") != "visible_chats_found" and _chat_title_visible_outside_paths(snapshots, chat_title, {row_path, list_path}):
        signals.append({"type": "project_chat_list_not_primary_content"})
    if _main_project_layout_materially_changed(resolution, pre_plan):
        signals.append({"type": "main_region_layout_materially_changed"})
    return signals


def _path_in_or_under(path: str, ancestor: str) -> bool:
    return bool(ancestor) and (path == ancestor or path.startswith(ancestor + "."))


def _chat_title_visible_outside_paths(snapshots: list[AXElementSnapshot], chat_title: str, excluded_paths: set[str]) -> bool:
    for snapshot in snapshots:
        if any(_path_in_or_under(snapshot.path, path) for path in excluded_paths if path):
            continue
        if _project_text(snapshot) == chat_title:
            return True
    return False


def _conversation_structure_present(snapshots: list[AXElementSnapshot], row_path: str, list_path: str) -> bool:
    message_like = 0
    composer_like = False
    for snapshot in snapshots:
        if _path_in_or_under(snapshot.path, row_path) or _path_in_or_under(snapshot.path, list_path):
            continue
        text = _normalized_label(snapshot.title or snapshot.description or snapshot.identifier).casefold()
        if snapshot.role in {"AXTextArea", "AXTextField"} and any(token in text for token in ("message", "ask", "composer", "prompt")):
            composer_like = True
        if snapshot.role in {"AXGroup", "AXStaticText", "AXTextArea"} and any(token in text for token in ("assistant", "user", "message")):
            message_like += 1
    return composer_like or message_like >= 1


def _main_project_layout_materially_changed(resolution: dict, pre_plan: dict) -> bool:
    before = _frame_tuple(((pre_plan.get("project_chat_resolution") or {}).get("chat_list_container") or {}).get("frame"))
    after = _frame_tuple((resolution.get("chat_list_container") or {}).get("frame"))
    if before is None:
        return False
    if after is None:
        return True
    return _frames_materially_changed(before, after)


def _base_autonomous_open_result(kind: str, title: str, app_name: str, confirm_open_destination: bool) -> dict:
    return {
        "ok": False,
        "outcome": "activation_failed",
        "app_name": app_name,
        "kind": kind,
        "title": title,
        "confirm_open_destination": bool(confirm_open_destination),
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "activation_result": {},
        "activation_stability": {},
        "traversal": {},
        "target_match_count": 0,
        "target": {},
        "title_frame": _frame_geometry_report(None),
        "row_frame": _frame_geometry_report(None),
        "chatgpt_ax_window_frame": _frame_geometry_report(None),
        "chatgpt_windowserver_bounds": _frame_geometry_report(None),
        "available_display_bounds": [],
        "chosen_method": "",
        "axpress_attempt": {},
        "calculated_global_point": _xy_report(None),
        "calculated_point_hit_test": {},
        "calculated_point_hit_test_relationship": "",
        "post_action_evidence": {},
        "post_action_confirmed": False,
        "visible_chat_count": 0,
        "visible_chats": [],
        "project_content_container": _project_container_report({}),
        "main_project_content": _project_container_report({}),
        "chat_list_container": _project_container_report({}),
        "more_rows_may_exist_below": "unknown",
        "actions_performed": [],
        "error": "",
    }


def _autonomous_activation_summary(activation_result: dict) -> dict:
    return {
        "activated": bool(activation_result.get("activated")),
        "is_frontmost": bool(activation_result.get("is_frontmost")),
        "app_name": activation_result.get("app_name") or "",
        "frontmost_app": activation_result.get("frontmost_app"),
        "error": activation_result.get("error") or "",
    }


def _stable_chatgpt_geometry_sample(
    reader: object,
    pid: int,
    windowserver_probe_factory: object,
    *,
    sample_count: int,
    sleep_function: object,
) -> dict:
    samples = []
    last_error = ""
    for index in range(max(2, sample_count)):
        try:
            snapshots, stats, window_metadata = reader.collect(pid)
            ax_window_frame = _window_frame_from_metadata(window_metadata, snapshots)
            windows = windowserver_probe_factory().visible_windows_for_pid(pid)
            windowserver_window = _choose_windowserver_window_for_ax_frame(windows, ax_window_frame)
            windowserver_bounds = _frame_tuple((windowserver_window or {}).get("bounds"))
        except Exception as exc:
            last_error = str(exc)
            break
        if not snapshots or not _frame_is_valid(ax_window_frame) or not _frame_is_valid(windowserver_bounds):
            last_error = "ChatGPT AX window frame or WindowServer bounds were unavailable."
            break
        samples.append(
            {
                "snapshots": snapshots,
                "stats": stats,
                "window_metadata": window_metadata,
                "ax_window_frame": ax_window_frame,
                "windowserver_window": windowserver_window or {},
                "windowserver_bounds": windowserver_bounds,
            }
        )
        if index < sample_count - 1:
            sleep_function(min(AUTONOMOUS_OPEN_STABILITY_POLL_SECONDS, 0.5))
    if len(samples) < max(2, sample_count):
        return {"status": "unstable", "error": last_error or "Insufficient bounded geometry samples."}
    first = samples[0]
    for sample in samples[1:]:
        if _frames_materially_changed(first["ax_window_frame"], sample["ax_window_frame"]) or _frames_materially_changed(
            first["windowserver_bounds"],
            sample["windowserver_bounds"],
        ):
            return {"status": "unstable", "error": "AX window frame or WindowServer bounds changed during stabilization.", "samples": len(samples)}
    latest = samples[-1]
    return {"status": "stable", "samples": len(samples), "all_samples": samples, **latest}


def _autonomous_stability_summary(stable: dict) -> dict:
    return {
        "status": stable.get("status") or "",
        "samples": stable.get("samples", 0),
        "ax_window_frame": _frame_geometry_report(stable.get("ax_window_frame")),
        "windowserver_bounds": _frame_geometry_report(stable.get("windowserver_bounds")),
        "windowserver_window_id": (stable.get("windowserver_window") or {}).get("window_id"),
        "error": stable.get("error") or "",
    }


def _choose_windowserver_window_for_ax_frame(
    windows: list[dict],
    ax_window_frame: tuple[float, float, float, float] | None,
) -> dict | None:
    visible = [window for window in windows if _frame_is_valid(_frame_tuple(window.get("bounds")))]
    if not visible:
        return None
    candidates = [
        window for window in visible if _frame_intersects(_frame_tuple(window.get("bounds")), ax_window_frame)
    ] or visible
    return sorted(
        candidates,
        key=lambda window: (
            0 if _frame_contains_with_tolerance(_frame_tuple(window.get("bounds")), ax_window_frame, FRAME_CONTAINMENT_TOLERANCE) else 1,
            0 if _frame_intersects(_frame_tuple(window.get("bounds")), ax_window_frame) else 1,
            -_frame_area(_frame_tuple(window.get("bounds"))),
            int(window.get("window_id") or 0),
        ),
    )[0]


def _autonomous_destination_plan(
    snapshots: list[AXElementSnapshot],
    stats: dict,
    window_metadata: dict,
    kind: str,
    requested_title: str,
    windowserver_bounds: tuple[float, float, float, float] | None,
    display_probe_factory: object,
) -> dict:
    classified = classify_navigation_snapshots(snapshots, stats, window_metadata, include_visible_navigation_titles=True)
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    base = {
        "status": "ready",
        "error": "",
        "snapshots": snapshots,
        "classified": classified,
        "window_metadata": window_metadata,
        "target_match_count": len(matches),
    }
    if not matches:
        return {**base, "status": "target_absent", "error": "No exactly matching visible sidebar destination was found."}
    if len(matches) > 1:
        return {**base, "status": "target_ambiguous", "error": "More than one matching visible sidebar destination was found."}

    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    candidate = matches[0]
    scope = _sidebar_destination_local_scope(candidate, snapshots, snapshots_by_path)
    title_snapshot = snapshots_by_path.get(scope["title_path"])
    row_snapshot = snapshots_by_path.get(scope["row_path"])
    title_frame = _frame_tuple(title_snapshot.frame if title_snapshot else None)
    row_frame = _frame_tuple(row_snapshot.frame if row_snapshot else None)
    ax_window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    display_bounds = _collect_display_bounds_without_cursor(display_probe_factory)
    visibility = _autonomous_target_visibility(
        title_frame,
        row_frame,
        ax_window_frame,
        windowserver_bounds,
        display_bounds,
    )
    if visibility["status"] != "ready":
        return {**base, "status": visibility["status"], "error": visibility["error"], "candidate": candidate, "scope": scope}

    elements = [
        _deep_inspection_element(snapshot, scope, requested_title, snapshots_by_path)
        for snapshot in scope["ordered_snapshots"]
    ]
    primary = _assess_primary_selection_path(elements, candidate)
    return {
        **base,
        "candidate": candidate,
        "scope": scope,
        "title_snapshot": title_snapshot,
        "row_snapshot": row_snapshot,
        "title_frame": title_frame,
        "row_frame": row_frame,
        "ax_window_frame": ax_window_frame,
        "windowserver_bounds": windowserver_bounds,
        "display_bounds": display_bounds,
        "axpress_target": _autonomous_axpress_target(primary),
        "pre_action_state": _autonomous_destination_state(snapshots, classified, kind, requested_title, scope),
    }


def _collect_display_bounds_without_cursor(display_probe_factory: object) -> list[tuple[float, float, float, float]]:
    try:
        probe = display_probe_factory()
        displays = []
        active = getattr(probe, "active_display_bounds", None)
        if active is not None:
            displays.extend(_frame_tuple(item.get("bounds")) for item in active() if isinstance(item, dict))
        primary = getattr(probe, "primary_display_bounds", None)
        if primary is not None:
            displays.append(_frame_tuple(primary()))
        unique = []
        for frame in displays:
            if frame is not None and _frame_is_valid(frame) and frame not in unique:
                unique.append(frame)
        return unique
    except Exception:
        return []


def _autonomous_target_visibility(
    title_frame: tuple[float, float, float, float] | None,
    row_frame: tuple[float, float, float, float] | None,
    ax_window_frame: tuple[float, float, float, float] | None,
    windowserver_bounds: tuple[float, float, float, float] | None,
    display_bounds: list[tuple[float, float, float, float]],
) -> dict:
    if not _frame_is_valid(title_frame) and not _frame_is_valid(row_frame):
        return {"status": "safe_click_point_unavailable", "error": "The target title and row frames were missing or invalid."}
    target_frame = title_frame if _frame_is_valid(title_frame) else row_frame
    if not _frame_contains(ax_window_frame, target_frame) or not _frame_contains(windowserver_bounds, target_frame):
        return {"status": "target_offscreen", "error": "Target frame was not within the visible ChatGPT AX and WindowServer bounds."}
    if not any(_frame_intersects(display, target_frame) for display in display_bounds):
        return {"status": "target_offscreen", "error": "Target frame was not on an available display."}
    return {"status": "ready", "error": ""}


def _autonomous_axpress_target(primary: dict) -> dict:
    controls = [
        control for control in primary.get("viable_candidate_controls") or [] if "AXPress" in (control.get("concrete_advertised_actions") or [])
    ]
    if not controls:
        return {}
    precedence = {"title_node": 0, "computed_row_node": 1, "row_descendant": 2, "linked_ui_element": 3}
    controls = sorted(
        controls,
        key=lambda item: (
            precedence.get(str(item.get("relation_to_requested_title") or ""), 99),
            str(item.get("target_path") or ""),
        ),
    )
    if len(controls) > 1 and precedence.get(str(controls[0].get("relation_to_requested_title") or ""), 99) == precedence.get(
        str(controls[1].get("relation_to_requested_title") or ""),
        99,
    ):
        return {}
    return {
        "path": controls[0].get("target_path") or "",
        "relation": controls[0].get("relation_to_requested_title") or "",
        "actions": controls[0].get("concrete_advertised_actions") or [],
        "confidence": controls[0].get("confidence") or "",
    }


def _autonomous_destination_state(
    snapshots: list[AXElementSnapshot],
    classified: dict,
    kind: str,
    requested_title: str,
    scope: dict,
) -> dict:
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    title = snapshots_by_path.get(scope.get("title_path") or "")
    row = snapshots_by_path.get(scope.get("row_path") or "")
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    selected_paths = []
    for snapshot in snapshots:
        if snapshot.selected is True or snapshot.focused is True:
            selected_paths.append(snapshot.path)
    return {
        "title_path": scope.get("title_path") or "",
        "row_path": scope.get("row_path") or "",
        "title_selected": title.selected if title else None,
        "title_focused": title.focused if title else None,
        "row_selected": row.selected if row else None,
        "row_focused": row.focused if row else None,
        "selected_paths": selected_paths[:DEEP_INSPECTOR_RELATED_MAX_NODES],
        "current_identity_matches_requested_title": _current_content_identity_matches(snapshots, requested_title, scope),
        "requested_title_match_count": len(matches),
    }


def _current_content_identity_matches(snapshots: list[AXElementSnapshot], requested_title: str, scope: dict) -> bool:
    title_path = str(scope.get("title_path") or "")
    row_path = str(scope.get("row_path") or "")
    list_path = str(scope.get("list_path") or "")
    for snapshot in snapshots:
        if snapshot.depth > 5 or snapshot.role not in {"AXHeading", "AXStaticText"}:
            continue
        if snapshot.path == title_path or snapshot.path.startswith(row_path + ".") or snapshot.path.startswith(list_path + "."):
            continue
        text = _normalized_label(snapshot.title or snapshot.description or snapshot.value)
        if text == requested_title:
            return True
    return False


def _autonomous_plan_result(plan: dict) -> dict:
    candidate = plan.get("candidate") or {}
    scope = plan.get("scope") or {}
    title_snapshot = plan.get("title_snapshot")
    row_snapshot = plan.get("row_snapshot")
    classified = plan.get("classified") or {}
    return {
        "traversal": classified.get("traversal") or {},
        "target_match_count": max(0, int(plan.get("target_match_count") or 0)),
        "target": {
            "kind": kind_from_candidate(candidate),
            "title": candidate.get("exact_title") or "",
            "title_ax_path": scope.get("title_path") or "",
            "row_ax_path": scope.get("row_path") or "",
            "title_role": title_snapshot.role if title_snapshot else "",
            "title_subrole": title_snapshot.subrole if title_snapshot else "",
            "title_actions": _safe_actions(title_snapshot.actions if title_snapshot else ()),
            "row_role": row_snapshot.role if row_snapshot else "",
            "row_subrole": row_snapshot.subrole if row_snapshot else "",
            "row_actions": _safe_actions(row_snapshot.actions if row_snapshot else ()),
            "axpress_target": plan.get("axpress_target") or {},
        },
        "title_frame": _frame_geometry_report(plan.get("title_frame")),
        "row_frame": _frame_geometry_report(plan.get("row_frame")),
        "chatgpt_ax_window_frame": _frame_geometry_report(plan.get("ax_window_frame")),
        "chatgpt_windowserver_bounds": _frame_geometry_report(plan.get("windowserver_bounds")),
        "available_display_bounds": [_frame_geometry_report(frame) for frame in plan.get("display_bounds") or []],
    }


def kind_from_candidate(candidate: dict) -> str:
    classification = str(candidate.get("classification") or "")
    if classification == "visible_project_title_candidate":
        return "project"
    if classification == "visible_chat_title_candidate":
        return "chat"
    return ""


def _autonomous_validated_click(
    reader: object,
    pid: int,
    kind: str,
    requested_title: str,
    pre_action_state: dict,
    click_service_factory: object,
    display_probe_factory: object,
    windowserver_probe_factory: object,
    *,
    confirm_open_destination: bool,
    sleep_function: object,
    before_action_callback: object | None,
) -> dict:
    stable = _stable_chatgpt_geometry_sample(
        reader,
        pid,
        windowserver_probe_factory,
        sample_count=2,
        sleep_function=sleep_function,
    )
    if stable["status"] != "stable":
        return {"status": "unstable_chatgpt_ui", "error": stable.get("error") or "Fresh geometry did not stabilize."}
    plan = _autonomous_destination_plan(
        stable["snapshots"],
        stable["stats"],
        stable["window_metadata"],
        kind,
        requested_title,
        stable["windowserver_bounds"],
        display_probe_factory,
    )
    if plan["status"] != "ready":
        return {"status": plan["status"], "error": plan.get("error", "")}
    click_plan = _autonomous_click_plan_from_destination_plan(plan, reader, pid, requested_title)
    if click_plan["status"] != "ready":
        return click_plan
    if not confirm_open_destination:
        return click_plan
    try:
        clicker = click_service_factory()
        if not clicker.has_permission():
            return {**click_plan, "status": "click_posting_failed", "error": "CoreGraphics post-event permission is unavailable."}
        if before_action_callback is not None:
            before_action_callback()
        posted = clicker.left_click(click_plan["calculated_global_point"]["x"], click_plan["calculated_global_point"]["y"])
    except Exception as exc:
        return {**click_plan, "status": "click_posting_failed", "error": str(exc)}
    if not posted.get("ok"):
        return {**click_plan, "status": "click_posting_failed", "error": posted.get("error") or "CoreGraphics click could not be posted."}
    post_evidence = _autonomous_post_action_inspection(
        reader,
        pid,
        kind,
        requested_title,
        pre_action_state,
        windowserver_probe_factory=windowserver_probe_factory,
        sleep_function=sleep_function,
    )
    return {
        **click_plan,
        "actions_performed": posted.get("actions_performed") or [],
        "post_action_evidence": post_evidence,
    }


def _autonomous_click_plan_from_destination_plan(plan: dict, reader: object, pid: int, requested_title: str) -> dict:
    source = _autonomous_click_source(plan)
    point = _compute_autonomous_safe_click_point(source.get("frame"), source.get("relation") or "")
    if not point.get("ok"):
        return {"status": "safe_click_point_unavailable", "error": point.get("reason") or "Could not compute a safe click point.", "click_source": source, "calculated_global_point": point}
    point_tuple = _xy_point(point.get("x"), point.get("y"))
    if not _point_inside_frame(point_tuple, source.get("frame")):
        return {"status": "safe_click_point_unavailable", "error": "Calculated point was outside the selected target frame.", "click_source": source, "calculated_global_point": point}
    if not _point_inside_frame(point_tuple, plan.get("windowserver_bounds")):
        return {"status": "target_offscreen", "error": "Calculated point was outside ChatGPT WindowServer bounds.", "click_source": source, "calculated_global_point": point}
    if not any(_point_inside_frame(point_tuple, display) for display in plan.get("display_bounds") or []):
        return {"status": "target_offscreen", "error": "Calculated point was outside all available display bounds.", "click_source": source, "calculated_global_point": point}
    hit_test = _collect_calculated_point_hit_test(reader, pid, point_tuple, requested_title)
    relationship = _hit_test_relationship(hit_test, plan.get("scope") or {}, {snapshot.path: snapshot for snapshot in plan.get("snapshots") or []})
    if not _hit_test_relationship_accepts_click(relationship):
        return {
            "status": "calculated_point_hit_test_mismatch",
            "error": "AX hit-test at the calculated point did not resolve to the requested destination.",
            "click_source": source,
            "calculated_global_point": point,
            "calculated_point_hit_test": hit_test,
            "calculated_point_hit_test_relationship": relationship,
        }
    return {
        "status": "ready",
        "error": "",
        "click_source": source,
        "calculated_global_point": point,
        "calculated_point_hit_test": hit_test,
        "calculated_point_hit_test_relationship": relationship,
    }


def _autonomous_click_source(plan: dict) -> dict:
    if _frame_is_valid(plan.get("title_frame")):
        return {"relation": "title_frame", "path": (plan.get("scope") or {}).get("title_path") or "", "frame": plan.get("title_frame")}
    if _frame_is_valid(plan.get("row_frame")):
        return {"relation": "row_frame", "path": (plan.get("scope") or {}).get("row_path") or "", "frame": plan.get("row_frame")}
    return {"relation": "", "path": "", "frame": None}


def _compute_autonomous_safe_click_point(frame: tuple[float, float, float, float] | None, relation: str) -> dict:
    normalized = _frame_tuple(frame)
    if normalized is None or not _frame_is_valid(normalized):
        return {"ok": False, "x": None, "y": None, "reason": "missing_or_invalid_frame"}
    if relation == "row_frame":
        return _compute_safe_click_point(normalized)
    x, y, width, height = normalized
    inset_x = min(SAFE_CLICK_EDGE_INSET, max(1.0, width * 0.2))
    inset_y = min(SAFE_CLICK_EDGE_INSET, max(1.0, height * 0.2))
    if width <= inset_x * 2 or height <= inset_y * 2:
        return {"ok": False, "x": None, "y": None, "reason": "safe_interior_region_too_small"}
    return {
        "ok": True,
        "x": round(x + min(max(width * SAFE_CLICK_LEFT_FRACTION, inset_x), width - inset_x), 2),
        "y": round(y + height / 2.0, 2),
        "reason": "fresh_title_frame_center_left_interior_point",
    }


def _collect_calculated_point_hit_test(
    reader: object,
    pid: int,
    point: tuple[float, float],
    requested_title: str,
) -> dict:
    hit_tester = getattr(reader, "hit_test_at_position", None)
    if hit_tester is None:
        return {"available": False, "error": "Reader does not support AX hit-testing.", "path": ""}
    try:
        hit = hit_tester(pid, point, requested_title)
    except Exception as exc:
        return {"available": False, "error": str(exc), "path": ""}
    if not isinstance(hit, dict):
        return {"available": False, "error": "AX hit-test returned an invalid result.", "path": ""}
    return _sanitize_hit_test_report(hit, requested_title)


def _autonomous_click_result_fields(click_result: dict) -> dict:
    return {
        "calculated_global_point": click_result.get("calculated_global_point") or _xy_report(None),
        "calculated_point_hit_test": click_result.get("calculated_point_hit_test") or {},
        "calculated_point_hit_test_relationship": click_result.get("calculated_point_hit_test_relationship") or "",
    }


def _autonomous_project_chat_result_fields(post_evidence: dict) -> dict:
    resolution = post_evidence.get("project_chat_resolution") or {}
    fields = _project_visible_chats_resolution_fields(resolution) if resolution else {}
    if not fields:
        fields = {
            "visible_chat_count": 0,
            "visible_chats": [],
            "project_content_container": _project_container_report({}),
            "main_project_content": _project_container_report({}),
            "chat_list_container": _project_container_report({}),
            "more_rows_may_exist_below": "unknown",
        }
    fields["post_action_confirmed"] = bool(post_evidence.get("confirmed"))
    return fields


def _autonomous_post_action_inspection(
    reader: object,
    pid: int,
    kind: str,
    requested_title: str,
    pre_state: dict,
    *,
    windowserver_probe_factory: object,
    sleep_function: object,
) -> dict:
    sleep_function(min(AUTONOMOUS_OPEN_POST_ACTION_SETTLE_SECONDS, 1.0))
    try:
        stable = _stable_chatgpt_geometry_sample(
            reader,
            pid,
            windowserver_probe_factory,
            sample_count=2,
            sleep_function=sleep_function,
        )
    except Exception as exc:
        return {"inspection_available": False, "confirmed": False, "error": str(exc), "signals": []}
    if stable["status"] != "stable":
        return {
            "inspection_available": False,
            "confirmed": False,
            "error": stable.get("error") or "Post-action ChatGPT UI geometry did not stabilize.",
            "signals": [],
        }
    snapshots = stable["snapshots"]
    stats = stable["stats"]
    window_metadata = stable["window_metadata"]
    if kind == "project":
        resolution = _confirm_stable_project_chat_list_identity(
            stable.get("all_samples") or [{"snapshots": snapshots, "stats": stats, "window_metadata": window_metadata, "ax_window_frame": stable.get("ax_window_frame")}],
            requested_title,
        )
        outcome = _autonomous_project_open_outcome_from_resolution(resolution)
        confirmed = outcome in {"destination_opened_and_visible_chats_resolved", "destination_opened_with_empty_visible_chat_list"}
        signals = _autonomous_project_open_signals(resolution)
        return {
            "inspection_available": True,
            "confirmed": confirmed,
            "signals": signals,
            "project_chat_resolution": resolution,
            "open_outcome": outcome,
            "visible_chat_count": int(resolution.get("visible_chat_count") or 0),
            "visible_chat_titles": [chat.get("title") or "" for chat in resolution.get("visible_chats") or []],
            "reason": "" if confirmed else resolution.get("error") or "Project content and visible chats were not fully confirmed.",
        }
    classified = classify_navigation_snapshots(snapshots, stats, window_metadata, include_visible_navigation_titles=True)
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    if len(matches) != 1:
        return {
            "inspection_available": True,
            "confirmed": False,
            "signals": [],
            "requested_title_match_count": len(matches),
            "reason": "Requested destination was not uniquely visible after the action.",
        }
    scope = _sidebar_destination_local_scope(matches[0], snapshots, {snapshot.path: snapshot for snapshot in snapshots})
    post_state = _autonomous_destination_state(snapshots, classified, kind, requested_title, scope)
    signals = _autonomous_verification_signals(pre_state, post_state)
    return {
        "inspection_available": True,
        "confirmed": len({signal["type"] for signal in signals}) >= 2,
        "signals": signals,
        "pre_state": pre_state,
        "post_state": post_state,
        "requested_title_match_count": len(matches),
        "reason": "" if signals else "No active destination evidence was exposed after the action.",
    }


def _autonomous_project_open_outcome_from_resolution(resolution: dict) -> str:
    status = resolution.get("status") or ""
    if status == "project_chat_list_identity_not_confirmed":
        return "project_chat_list_identity_not_confirmed"
    if status == "visible_chats_found":
        return "destination_opened_and_visible_chats_resolved"
    if (
        status == "visible_chat_rows_not_found"
        and resolution.get("project_identity_confirmed")
        and resolution.get("chats_tab_confirmed")
        and resolution.get("chats_area_confirmed")
    ):
        return "destination_opened_with_empty_visible_chat_list"
    if resolution.get("project_identity_confirmed"):
        return "project_opened_but_visible_chats_not_resolved"
    return "action_posted_but_destination_not_confirmed"


def _confirm_stable_project_chat_list_identity(samples: list[dict], requested_title: str) -> dict:
    """Re-resolve project + Chats-list identity across fresh AX samples.

    Target matching / scroll / AXPress / geometry-click may only proceed when the
    requested project identity, the forward-resolved Chats-list container, and the
    valid row set are stable and compatible across every sample. A transient
    conversation/composer snapshot in any sample fails the whole gate closed.
    """
    resolutions = []
    for sample in samples or []:
        snaps = sample.get("snapshots") or []
        window_metadata = sample.get("window_metadata") or {}
        resolutions.append(
            resolve_open_project_content_and_visible_chats(
                requested_title,
                snaps,
                sample.get("ax_window_frame") or _window_frame_from_metadata(window_metadata, snaps),
                traversal_stats=sample.get("stats") or {},
                window_metadata=window_metadata,
            )
        )
    if not resolutions:
        return resolve_open_project_content_and_visible_chats(requested_title, [], None)
    latest = resolutions[-1]
    latest["identity_stability_samples"] = len(resolutions)
    if len(resolutions) < 2 or latest.get("status") != "visible_chats_found":
        return latest
    confirmed_all = all(res.get("project_chat_list_identity") == "confirmed" for res in resolutions)
    container_paths = {res.get("project_chat_list_container_path") or "" for res in resolutions}
    container_roles = {res.get("project_chat_list_container_role") or "" for res in resolutions}
    row_counts = [int(res.get("valid_project_chat_row_count") or 0) for res in resolutions]
    stable_geometry = len(container_paths) == 1 and len(container_roles) == 1 and (max(row_counts) - min(row_counts)) <= 1
    if confirmed_all and stable_geometry:
        return latest
    reasons = []
    if not confirmed_all:
        reasons.append("list_identity_unstable_across_samples")
    if not stable_geometry:
        reasons.append("list_geometry_unstable_across_samples")
    latest.update(_project_chat_list_identity_not_confirmed_fields(reasons, row_shape_status="invalid"))
    latest["identity_stability_samples"] = len(resolutions)
    return latest


def _autonomous_project_open_signals(resolution: dict) -> list[dict]:
    signals = []
    content = resolution.get("project_content_container") or {}
    chat_list = resolution.get("chat_list_container") or {}
    if resolution.get("project_identity_confirmed"):
        signals.append({"type": "main_project_identity_confirmed", "path": content.get("path") or ""})
    if resolution.get("chats_tab_confirmed"):
        signals.append({"type": "main_project_chats_tab_confirmed"})
    if int(resolution.get("visible_chat_count") or 0) > 0:
        signals.append(
            {
                "type": "visible_project_chat_rows_resolved",
                "count": int(resolution.get("visible_chat_count") or 0),
                "path": chat_list.get("path") or "",
            }
        )
    elif resolution.get("chats_area_confirmed"):
        signals.append({"type": "empty_visible_project_chat_list_confirmed", "path": chat_list.get("path") or ""})
    return signals


def _autonomous_verification_signals(pre_state: dict, post_state: dict) -> list[dict]:
    signals = []
    if (
        (post_state.get("title_selected") is True and pre_state.get("title_selected") is not True)
        or (post_state.get("title_focused") is True and pre_state.get("title_focused") is not True)
    ):
        signals.append({"type": "requested_title_selected_or_focused", "path": post_state.get("title_path") or ""})
    if (
        (post_state.get("row_selected") is True and pre_state.get("row_selected") is not True)
        or (post_state.get("row_focused") is True and pre_state.get("row_focused") is not True)
    ):
        signals.append({"type": "requested_row_selected_or_focused", "path": post_state.get("row_path") or ""})
    selected_paths = set(post_state.get("selected_paths") or [])
    if (
        (post_state.get("row_path") in selected_paths or post_state.get("title_path") in selected_paths)
        and selected_paths != set(pre_state.get("selected_paths") or [])
    ):
        signals.append({"type": "selected_paths_include_requested_destination", "paths": sorted(selected_paths)})
    if post_state.get("current_identity_matches_requested_title") and not pre_state.get("current_identity_matches_requested_title"):
        signals.append({"type": "active_content_identity_matches_requested_title"})
    if selected_paths and selected_paths != set(pre_state.get("selected_paths") or []):
        signals.append({"type": "selection_relationship_changed", "paths": sorted(selected_paths)})
    return signals


def _primary_selection_candidate(element: dict, reason: str, confidence: str) -> dict:
    settable = element.get("settable_attributes") or {}
    return {
        "target_path": element.get("path") or "",
        "relation_to_requested_title": element.get("relation_to_requested_title") or "",
        "concrete_advertised_actions": element.get("actions") or [],
        "supported_and_settable_selection_focus_attributes": {
            name: bool(settable.get(name))
            for name in SELECTION_FOCUS_ATTRIBUTES
            if element.get("supported_attributes", {}).get(name) or settable.get(name)
        },
        "why_primary_selection": reason,
        "confidence": confidence,
    }


def verify_chatgpt_sidebar_frame_click(
    *,
    kind: str,
    title: str,
    confirm_frame_click: bool = False,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
    click_service_factory: object | None = None,
    display_probe_factory: object | None = None,
    settle_seconds: float = FRAME_CLICK_SETTLE_SECONDS,
    before_click_callback: object | None = None,
) -> dict:
    requested_title = _normalized_label(title)
    result = _base_frame_click_result(kind, requested_title, app_name, confirm_frame_click)
    if kind not in {"project", "chat"} or not requested_title:
        result.update({"status": "target_not_found", "error": "kind must be project or chat and title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "accessibility_failure", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "accessibility_failure", "error": "ChatGPT sidebar frame-click verification is only supported on macOS."})
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"status": "accessibility_failure", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _DetailedReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        pre_plan = _frame_click_plan(reader, process.pid, kind, requested_title)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "error": str(exc), "pid_present": True})
        return result

    result.update(_frame_click_plan_result(pre_plan, prefix="pre_click"))
    result["coordinate_diagnostics"] = _frame_click_coordinate_diagnostics(
        pre_plan,
        display_probe_factory or _CoreGraphicsDisplayProbe,
    )
    if pre_plan["status"] != "frame_click_ready":
        result["status"] = pre_plan["status"]
        result["error"] = pre_plan.get("error", "")
        return result
    result["status"] = "dry_run_ready"
    result["ok"] = True
    if not confirm_frame_click:
        return result

    try:
        fresh_plan = _frame_click_plan(reader, process.pid, kind, requested_title)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "ok": False, "error": str(exc)})
        return result
    result.update(_frame_click_plan_result(fresh_plan, prefix="fresh_click"))
    if fresh_plan["status"] != "frame_click_ready":
        result.update({"status": fresh_plan["status"], "ok": False, "error": fresh_plan.get("error", "")})
        return result
    if _frame_click_plan_materially_changed(pre_plan, fresh_plan):
        result.update({"status": "target_frame_invalid", "ok": False, "error": "Resolved title, AX path, frame, or click point changed before action."})
        return result

    try:
        clicker_factory = click_service_factory or _CoreGraphicsFrameClickService
        clicker = clicker_factory()
        if not clicker.has_permission():
            result.update({"status": "permission_denied", "ok": False, "error": "CoreGraphics post-event permission is unavailable."})
            return result
        if before_click_callback is not None:
            before_click_callback(kind, requested_title)
        click_result = clicker.left_click(fresh_plan["click_point"]["x"], fresh_plan["click_point"]["y"])
    except PermissionError as exc:
        result.update({"status": "permission_denied", "ok": False, "error": str(exc)})
        return result
    except Exception as exc:
        result.update({"status": "accessibility_failure", "ok": False, "error": str(exc)})
        return result

    if not click_result.get("ok"):
        result.update({"status": "permission_denied", "ok": False, "error": click_result.get("error") or "CoreGraphics click failed."})
        return result
    result["actions_performed"] = click_result.get("actions_performed") or []
    if settle_seconds > 0:
        time.sleep(min(settle_seconds, FRAME_CLICK_SETTLE_SECONDS))

    try:
        post_snapshots, post_stats, post_window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "ok": False, "error": str(exc)})
        return result
    post_classified = classify_navigation_snapshots(
        post_snapshots,
        post_stats,
        post_window_metadata,
        include_visible_navigation_titles=True,
    )
    result["post_click_evidence"] = _frame_click_post_evidence(
        pre_plan,
        post_snapshots,
        post_classified,
        kind,
        requested_title,
    )
    result["status"] = result["post_click_evidence"]["status"]
    result["ok"] = result["status"] == "verified_selection_changed"
    return result


def _base_frame_click_result(kind: str, title: str, app_name: str, confirm_frame_click: bool) -> dict:
    return {
        "ok": False,
        "status": "not_run",
        "app_name": app_name,
        "kind": kind,
        "title": title,
        "confirm_frame_click": bool(confirm_frame_click),
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "target": {},
        "frame_safety": {},
        "click_point": {},
        "pre_click_evidence": {},
        "fresh_click_evidence": {},
        "post_click_evidence": {},
        "coordinate_diagnostics": {},
        "actions_performed": [],
        "error": "",
    }


def _frame_click_plan(reader: object, pid: int, kind: str, requested_title: str) -> dict:
    snapshots, stats, window_metadata = reader.collect(pid)
    classified = classify_navigation_snapshots(
        snapshots,
        stats,
        window_metadata,
        include_visible_navigation_titles=True,
    )
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    base = {
        "snapshots": snapshots,
        "classified": classified,
        "window_metadata": window_metadata,
        "status": "frame_click_ready",
        "error": "",
    }
    if not matches:
        return {**base, "status": "target_not_found", "error": "No exactly matching visible sidebar destination was found."}
    if len(matches) > 1:
        return {**base, "status": "target_ambiguous", "error": "More than one matching visible sidebar destination was found."}

    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    candidate = matches[0]
    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    container = candidate.get("nearest_list_container") or {}
    list_path = str(container.get("path") or "")
    title_path = str(candidate.get("path") or "")
    row_path = _row_path_under_container(title_path, list_path) if list_path else title_path
    sidebar_context = _resolve_sidebar_containment_context(row_path, snapshots_by_path, window_frame)
    source = _select_frame_click_source(candidate, snapshots_by_path, window_frame=window_frame, sidebar_context=sidebar_context)
    click_point = _compute_safe_click_point(source.get("source_frame"))
    frame_evidence = _destination_frame_evidence(
        candidate,
        snapshots_by_path,
        window_frame=window_frame,
        sidebar_context=sidebar_context,
    )
    target_state = _frame_click_target_state(candidate, snapshots_by_path, source)
    status = "frame_click_ready"
    error = ""
    report = source.get("frame_report") or {}
    if not report.get("valid") or not report.get("large_enough_for_safe_interior_click") or not click_point.get("ok"):
        status = "target_frame_invalid"
        error = click_point.get("reason") or "Target frame is invalid or too small."
    elif not report.get("fully_inside_window") or not report.get("inside_sidebar_or_list"):
        status = "target_not_visible"
        error = "Target frame is not fully inside the focused ChatGPT window and sidebar/list surface."
    return {
        **base,
        "status": status,
        "error": error,
        "candidate": candidate,
        "source": source,
        "click_point": click_point,
        "frame_evidence": frame_evidence,
        "sidebar_context": sidebar_context,
        "target_state": target_state,
    }


def _frame_click_plan_result(plan: dict, *, prefix: str) -> dict:
    candidate = plan.get("candidate") or {}
    source = plan.get("source") or {}
    result = {
        f"{prefix}_evidence": {
            "status": plan.get("status") or "",
            "error": plan.get("error") or "",
            "target_state": plan.get("target_state") or {},
        }
    }
    if candidate:
        result["target"] = {
            "title": candidate.get("exact_title") or "",
            "title_ax_path": candidate.get("path") or "",
            "computed_row_ax_path": (source.get("row_path") or ""),
            "source_frame_path": source.get("source_path") or "",
            "source_frame_relation": source.get("source_relation") or "",
        }
    result["frame_safety"] = {
        "source_frame": source.get("frame_report") or _empty_frame_report(),
        "frame_evidence": plan.get("frame_evidence") or {},
        "safety_checks_passed": plan.get("status") == "frame_click_ready",
        "why_click_point_avoids_overflow_region": (plan.get("click_point") or {}).get("reason") or "",
    }
    result["frame_safety"].update(
        _sidebar_context_report(
            plan.get("sidebar_context") or {},
            _window_frame_from_metadata(plan.get("window_metadata") or {}, plan.get("snapshots") or []),
        )
    )
    result["click_point"] = plan.get("click_point") or _compute_safe_click_point(None)
    return result


def _frame_click_target_state(candidate: dict, snapshots_by_path: dict[str, AXElementSnapshot], source: dict) -> dict:
    title_path = str(candidate.get("path") or "")
    row_path = str(source.get("row_path") or "")
    source_path = str(source.get("source_path") or "")
    title_snapshot = snapshots_by_path.get(title_path)
    row_snapshot = snapshots_by_path.get(row_path)
    source_snapshot = snapshots_by_path.get(source_path)
    return {
        "title_ax_path": title_path,
        "row_ax_path": row_path,
        "source_ax_path": source_path,
        "title": candidate.get("exact_title") or "",
        "title_focused": title_snapshot.focused if title_snapshot else None,
        "title_selected": title_snapshot.selected if title_snapshot else None,
        "row_focused": row_snapshot.focused if row_snapshot else None,
        "row_selected": row_snapshot.selected if row_snapshot else None,
        "source_focused": source_snapshot.focused if source_snapshot else None,
        "source_selected": source_snapshot.selected if source_snapshot else None,
        "source_frame": source.get("source_frame"),
        "source_actions": _safe_actions(source_snapshot.actions if source_snapshot else ()),
    }


def _frame_click_plan_materially_changed(before: dict, after: dict) -> bool:
    before_candidate = before.get("candidate") or {}
    after_candidate = after.get("candidate") or {}
    if before_candidate.get("exact_title") != after_candidate.get("exact_title"):
        return True
    before_source = before.get("source") or {}
    after_source = after.get("source") or {}
    if before_source.get("source_path") != after_source.get("source_path"):
        return True
    if before_source.get("title_path") != after_source.get("title_path"):
        return True
    if _frames_materially_changed(before_source.get("source_frame"), after_source.get("source_frame")):
        return True
    before_point = before.get("click_point") or {}
    after_point = after.get("click_point") or {}
    return abs(float(before_point.get("x") or 0) - float(after_point.get("x") or 0)) > FRAME_MATERIAL_CHANGE_TOLERANCE or abs(
        float(before_point.get("y") or 0) - float(after_point.get("y") or 0)
    ) > FRAME_MATERIAL_CHANGE_TOLERANCE


def _frame_click_post_evidence(
    pre_plan: dict,
    post_snapshots: list[AXElementSnapshot],
    post_classified: dict,
    kind: str,
    requested_title: str,
) -> dict:
    post_matches = _matching_visible_destination_candidates(post_classified, kind, requested_title)
    snapshots_by_path = {snapshot.path: snapshot for snapshot in post_snapshots}
    pre_state = pre_plan.get("target_state") or {}
    post_state = {}
    if post_matches:
        candidate = post_matches[0]
        source = pre_plan.get("source") or {}
        post_state = _frame_click_target_state(candidate, snapshots_by_path, source)
    current_identity_match = _current_identity_matches(post_snapshots, requested_title)
    selection_changed = _state_selection_or_focus_changed(pre_state, post_state)
    state_changed = bool(post_state) and post_state != pre_state
    if selection_changed or current_identity_match:
        status = "verified_selection_changed"
    elif state_changed:
        status = "destination_changed_but_identity_unverified"
    else:
        status = "click_performed_no_observable_change"
    return {
        "status": status,
        "requested_title_visible": bool(post_matches),
        "current_identity_matches_requested_title": current_identity_match,
        "selection_or_focus_changed": selection_changed,
        "observable_state_changed": state_changed,
        "pre_state": pre_state,
        "post_state": post_state,
    }


def _state_selection_or_focus_changed(pre_state: dict, post_state: dict) -> bool:
    for key in ("title_focused", "title_selected", "row_focused", "row_selected", "source_focused", "source_selected"):
        if pre_state.get(key) is not True and post_state.get(key) is True:
            return True
    return False


def _current_identity_matches(snapshots: list[AXElementSnapshot], requested_title: str) -> bool:
    for snapshot in snapshots:
        if snapshot.depth > 5 or snapshot.role not in {"AXHeading", "AXStaticText"}:
            continue
        text = _normalized_label(snapshot.title or snapshot.description or snapshot.value)
        if text == requested_title:
            return True
    return False


def _frame_tuple(frame: tuple[float, float, float, float] | None) -> tuple[float, float, float, float] | None:
    if isinstance(frame, dict):
        frame = (
            frame.get("x"),
            frame.get("y"),
            frame.get("width"),
            frame.get("height"),
        )
    if frame is None or len(frame) != 4:
        return None
    try:
        x, y, width, height = (float(value) for value in frame)
    except (TypeError, ValueError):
        return None
    return (x, y, width, height)


def _frame_report(
    frame: tuple[float, float, float, float] | None,
    *,
    window_frame: tuple[float, float, float, float] | None = None,
    sidebar_frame: tuple[float, float, float, float] | None = None,
) -> dict:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return _empty_frame_report()
    x, y, width, height = normalized
    valid = all(math.isfinite(value) for value in (x, y, width, height)) and width > 0 and height > 0
    return {
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(width, 2),
        "height": round(height, 2),
        "valid": valid,
        "fully_inside_window": _frame_contains(window_frame, normalized) if window_frame else False,
        "partially_inside_window": _frame_intersects(window_frame, normalized) if window_frame else False,
        "inside_sidebar_or_list": _frame_contains_with_tolerance(sidebar_frame, normalized, FRAME_CONTAINMENT_TOLERANCE) if sidebar_frame else False,
        "partially_inside_sidebar_or_list": _frame_intersects(sidebar_frame, normalized) if sidebar_frame else False,
        "large_enough_for_safe_interior_click": width >= MIN_SAFE_ROW_CLICK_WIDTH and height >= MIN_SAFE_ROW_CLICK_HEIGHT,
    }


def _empty_frame_report() -> dict:
    return {
        "x": None,
        "y": None,
        "width": None,
        "height": None,
        "valid": False,
        "fully_inside_window": False,
        "partially_inside_window": False,
        "inside_sidebar_or_list": False,
        "partially_inside_sidebar_or_list": False,
        "large_enough_for_safe_interior_click": False,
    }


def _frame_is_valid(frame: tuple[float, float, float, float] | None) -> bool:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return False
    x, y, width, height = normalized
    return all(math.isfinite(value) for value in (x, y, width, height)) and width > 0 and height > 0


def _frame_contains(outer: tuple[float, float, float, float] | None, inner: tuple[float, float, float, float] | None) -> bool:
    return _frame_contains_with_tolerance(outer, inner, 0.0)


def _frame_contains_with_tolerance(
    outer: tuple[float, float, float, float] | None,
    inner: tuple[float, float, float, float] | None,
    tolerance: float,
) -> bool:
    outer = _frame_tuple(outer)
    inner = _frame_tuple(inner)
    if outer is None or inner is None:
        return False
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox - tolerance
        and iy >= oy - tolerance
        and ix + iw <= ox + ow + tolerance
        and iy + ih <= oy + oh + tolerance
    )


def _frame_intersects(a: tuple[float, float, float, float] | None, b: tuple[float, float, float, float] | None) -> bool:
    a = _frame_tuple(a)
    b = _frame_tuple(b)
    if a is None or b is None:
        return False
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _frames_materially_changed(before: tuple[float, float, float, float] | None, after: tuple[float, float, float, float] | None) -> bool:
    before = _frame_tuple(before)
    after = _frame_tuple(after)
    if before is None or after is None:
        return before != after
    return any(abs(a - b) > FRAME_MATERIAL_CHANGE_TOLERANCE for a, b in zip(before, after))


def _select_frame_click_source(
    candidate: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
    *,
    window_frame: tuple[float, float, float, float] | None,
    sidebar_context: dict,
) -> dict:
    title_path = str(candidate.get("path") or "")
    container = candidate.get("nearest_list_container") or {}
    list_path = str(container.get("path") or "")
    row_path = _row_path_under_container(title_path, list_path) if list_path else title_path
    sidebar_frame = sidebar_context.get("frame")
    candidates = [
        ("computed_row_node", row_path),
        ("exact_title_node", title_path),
    ]
    for relation, path in candidates:
        snapshot = snapshots_by_path.get(path or "")
        if snapshot is None:
            continue
        report = _frame_report(snapshot.frame, window_frame=window_frame, sidebar_frame=sidebar_frame)
        if _frame_report_passes_for_click(report):
            return {
                "source_relation": relation,
                "source_path": snapshot.path,
                "source_frame": _frame_tuple(snapshot.frame),
                "frame_report": report,
                "title_path": title_path,
                "row_path": row_path,
                "list_path": list_path,
            }
    fallback_snapshot = snapshots_by_path.get(row_path) or snapshots_by_path.get(title_path)
    return {
        "source_relation": "none",
        "source_path": fallback_snapshot.path if fallback_snapshot else "",
        "source_frame": _frame_tuple(fallback_snapshot.frame) if fallback_snapshot else None,
        "frame_report": _frame_report(fallback_snapshot.frame if fallback_snapshot else None, window_frame=window_frame, sidebar_frame=sidebar_frame),
        "title_path": title_path,
        "row_path": row_path,
        "list_path": list_path,
    }


def _resolve_sidebar_containment_context(
    row_path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
) -> dict:
    row_snapshot = snapshots_by_path.get(row_path)
    row_frame = _frame_tuple(row_snapshot.frame) if row_snapshot else None
    base = {
        "path": "",
        "role": "",
        "frame": None,
        "method": "none",
        "row_inside_chosen_sidebar_frame": False,
    }
    if row_frame is None or not _frame_is_valid(row_frame):
        return base

    for ancestor_path in _ancestor_paths(row_path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if not _snapshot_has_usable_containment_frame(snapshot, window_frame):
            continue
        frame = _frame_tuple(snapshot.frame)
        if _is_direct_list_surface(snapshot) and _frame_contains_with_tolerance(frame, row_frame, FRAME_CONTAINMENT_TOLERANCE):
            return {
                "path": snapshot.path,
                "role": snapshot.role or snapshot.subrole or "",
                "frame": frame,
                "method": "direct_enclosing_list_ancestor",
                "row_inside_chosen_sidebar_frame": True,
            }

    for ancestor_path in _ancestor_paths(row_path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if not _snapshot_has_usable_containment_frame(snapshot, window_frame):
            continue
        frame = _frame_tuple(snapshot.frame)
        if _frame_contains_with_tolerance(frame, row_frame, FRAME_CONTAINMENT_TOLERANCE):
            return {
                "path": snapshot.path,
                "role": snapshot.role or snapshot.subrole or "",
                "frame": frame,
                "method": "direct_enclosing_ancestor_frame",
                "row_inside_chosen_sidebar_frame": True,
            }

    return base


def _snapshot_has_usable_containment_frame(
    snapshot: AXElementSnapshot | None,
    window_frame: tuple[float, float, float, float] | None,
) -> bool:
    if snapshot is None or not _frame_is_valid(snapshot.frame):
        return False
    frame = _frame_tuple(snapshot.frame)
    if snapshot.path == "W" or not _frames_materially_changed(frame, window_frame):
        return False
    return _frame_contains(window_frame, frame)


def _is_direct_list_surface(snapshot: AXElementSnapshot) -> bool:
    return (
        snapshot.role in LISTLIKE_ROLES
        or snapshot.subrole in LISTLIKE_SUBROLES
        or bool(snapshot.row_paths)
        or bool(snapshot.visible_row_paths)
    )


def _sidebar_context_report(
    sidebar_context: dict,
    window_frame: tuple[float, float, float, float] | None,
) -> dict:
    frame = sidebar_context.get("frame")
    return {
        "chosen_sidebar_frame_path": sidebar_context.get("path") or "",
        "chosen_sidebar_frame_role": sidebar_context.get("role") or "",
        "chosen_sidebar_frame_geometry": _frame_report(frame, window_frame=window_frame, sidebar_frame=frame),
        "sidebar_containment_method": sidebar_context.get("method") or "none",
        "row_inside_chosen_sidebar_frame": bool(sidebar_context.get("row_inside_chosen_sidebar_frame")),
        "focused_window_frame_geometry": _frame_report(window_frame, window_frame=window_frame, sidebar_frame=frame),
    }


def _nearest_usable_frame_ancestor_path(
    path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
    *,
    stop_path: str,
) -> str:
    for ancestor_path in _ancestor_paths(path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot and _frame_tuple(snapshot.frame) is not None:
            return ancestor_path
        if ancestor_path == stop_path:
            break
    return ""


def _frame_report_passes_for_click(report: dict) -> bool:
    return (
        bool(report.get("valid"))
        and bool(report.get("fully_inside_window"))
        and bool(report.get("inside_sidebar_or_list"))
        and bool(report.get("large_enough_for_safe_interior_click"))
    )


def _compute_safe_click_point(frame: tuple[float, float, float, float] | None) -> dict:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return {
            "ok": False,
            "x": None,
            "y": None,
            "policy": _safe_click_policy_text(),
            "reason": "missing_or_invalid_frame",
            "overflow_exclusion_zone": {},
        }
    x, y, width, height = normalized
    left = x + SAFE_CLICK_EDGE_INSET
    right = x + width - SAFE_CLICK_EDGE_INSET - SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH
    top = y + SAFE_CLICK_EDGE_INSET
    bottom = y + height - SAFE_CLICK_EDGE_INSET
    if right <= left or bottom <= top:
        return {
            "ok": False,
            "x": None,
            "y": None,
            "policy": _safe_click_policy_text(),
            "reason": "safe_interior_region_too_small_after_edge_and_overflow_exclusions",
            "overflow_exclusion_zone": _overflow_zone_report(normalized),
        }
    click_x = min(max(x + width * SAFE_CLICK_LEFT_FRACTION, left), right)
    click_y = (top + bottom) / 2.0
    return {
        "ok": True,
        "x": round(click_x, 2),
        "y": round(click_y, 2),
        "policy": _safe_click_policy_text(),
        "reason": "center_left_interior_point_excludes_edges_and_right_overflow_zone",
        "overflow_exclusion_zone": _overflow_zone_report(normalized),
    }


def _safe_click_policy_text() -> str:
    return (
        "Use the fresh resolved row frame; inset all edges; reserve the rightmost "
        f"{SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH:g}px as overflow/menu exclusion; click at "
        f"{SAFE_CLICK_LEFT_FRACTION:.2f} row width clamped into the remaining left/center-left safe interior; "
        "use the vertical center of that safe interior."
    )


def _overflow_zone_report(frame: tuple[float, float, float, float]) -> dict:
    x, y, width, height = frame
    return {
        "x": round(x + max(0.0, width - SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH), 2),
        "y": round(y, 2),
        "width": round(min(width, SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH), 2),
        "height": round(height, 2),
    }


def verify_chatgpt_sidebar_destination(
    *,
    kind: str,
    title: str,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
    settle_seconds: float = VERIFY_SETTLE_SECONDS,
    before_action_callback: object | None = None,
) -> dict:
    requested_title = _normalized_label(title)
    result = _base_verify_destination_result(kind, requested_title, app_name)
    if kind not in {"project", "chat"} or not requested_title:
        result.update({"status": "target_not_found", "error": "kind must be project or chat and title must be non-empty."})
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "accessibility_failure", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "accessibility_failure", "error": "ChatGPT sidebar verification is only supported on macOS."})
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "error": str(exc), "process_resolution_method": PROCESS_RESOLUTION_METHOD})
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update({"status": "accessibility_failure", "error": process.error or f"No running application named {app_name!r} was found."})
        return result

    factory = reader_factory or _ActionAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        pre_snapshots, pre_stats, pre_window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "error": str(exc), "pid_present": True})
        return result

    pre_classified = classify_navigation_snapshots(
        pre_snapshots,
        pre_stats,
        pre_window_metadata,
        include_visible_navigation_titles=True,
    )
    matches = _matching_visible_destination_candidates(pre_classified, kind, requested_title)
    result["pre_action_snapshot"] = _verify_snapshot_summary(pre_classified, kind, requested_title)
    if not matches:
        result.update({"status": "target_not_found", "error": "No exactly matching visible sidebar destination was found."})
        return result
    if len(matches) > 1:
        result.update({"status": "target_ambiguous", "error": "More than one matching visible sidebar destination was found."})
        return result

    candidate = matches[0]
    target = candidate.get("action_target_resolution") or {}
    result["target"] = _verify_target_summary(candidate)
    if target.get("resolution_method") == "ambiguous_target":
        result.update({"status": "target_ambiguous", "error": "The matching destination has ambiguous action targets."})
        return result
    if target.get("resolution_method") not in {"direct_press_target", "row_press_target", "focusable_then_press_target"}:
        result.update({"status": "target_not_actionable", "error": "The matching destination does not expose a verified press target."})
        return result

    try:
        if before_action_callback is not None:
            before_action_callback(kind, requested_title)
        action_sequence = _perform_destination_action(reader, target)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "error": str(exc)})
        return result

    result["actions_performed"] = action_sequence
    if settle_seconds > 0:
        time.sleep(min(settle_seconds, VERIFY_SETTLE_SECONDS))

    try:
        post_snapshots, post_stats, post_window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update({"status": "accessibility_failure", "error": str(exc)})
        return result

    post_classified = classify_navigation_snapshots(
        post_snapshots,
        post_stats,
        post_window_metadata,
        include_visible_navigation_titles=True,
    )
    result["post_action_snapshot"] = _verify_snapshot_summary(post_classified, kind, requested_title)
    result["status"] = _verify_destination_status(result["pre_action_snapshot"], result["post_action_snapshot"], kind, requested_title)
    result["ok"] = result["status"] == "verified_destination_changed"
    return result


def _base_verify_destination_result(kind: str, title: str, app_name: str) -> dict:
    return {
        "ok": False,
        "status": "accessibility_failure",
        "app_name": app_name,
        "kind": kind,
        "title": title,
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "target": {},
        "pre_action_snapshot": {},
        "post_action_snapshot": {},
        "actions_performed": [],
        "error": "",
    }


def datetime_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _matching_visible_destination_candidates(classified: dict, kind: str, title: str) -> list[dict]:
    key = "visible_project_title_candidates" if kind == "project" else "visible_chat_title_candidates"
    return [
        candidate
        for candidate in classified.get(key) or []
        if candidate.get("exact_title") == title
    ]


def _verify_target_summary(candidate: dict) -> dict:
    resolution = candidate.get("action_target_resolution") or {}
    return {
        "kind": resolution.get("destination_kind") or "",
        "title": resolution.get("exact_visible_title") or "",
        "title_ax_path": resolution.get("title_ax_path") or candidate.get("path") or "",
        "resolved_target_ax_path": resolution.get("resolved_target_ax_path") or "",
        "resolution_method": resolution.get("resolution_method") or "",
        "enabled_state": resolution.get("enabled_state"),
        "focused_state": resolution.get("focused_state"),
        "available_action_names": resolution.get("available_action_names") or [],
        "ax_press_available": bool(resolution.get("ax_press_available")),
        "ax_set_focus_available": bool(resolution.get("ax_set_focus_available")),
        "menu_only": bool(resolution.get("menu_only")),
        "confidence": resolution.get("confidence") or "",
        "evidence": resolution.get("evidence") or [],
    }


def _verify_snapshot_summary(classified: dict, kind: str, title: str) -> dict:
    matches = _matching_visible_destination_candidates(classified, kind, title)
    selected_matches = [
        candidate
        for candidate in matches
        if _candidate_or_target_focused(candidate)
    ]
    any_selected = [
        candidate
        for bucket in ("visible_project_title_candidates", "visible_chat_title_candidates")
        for candidate in classified.get(bucket) or []
        if _candidate_or_target_focused(candidate)
    ]
    return {
        "timestamp": datetime_now_iso(),
        "window_available": bool(classified.get("window_available")),
        "window_metadata": classified.get("window_metadata") or {},
        "requested_title_visible": bool(matches),
        "requested_title_match_count": len(matches),
        "requested_title_selected": bool(selected_matches),
        "selected_candidate_path": selected_matches[0].get("path") if selected_matches else "",
        "selected_target_path": (
            (selected_matches[0].get("action_target_resolution") or {}).get("resolved_target_ax_path")
            if selected_matches
            else ""
        ),
        "any_selected_candidate_path": any_selected[0].get("path") if any_selected else "",
        "any_selected_target_path": (
            (any_selected[0].get("action_target_resolution") or {}).get("resolved_target_ax_path")
            if any_selected
            else ""
        ),
    }


def _candidate_or_target_focused(candidate: dict) -> bool:
    if candidate.get("focused") is True:
        return True
    target = candidate.get("action_target_resolution") or {}
    if target.get("focused_state") is True:
        return True
    parent = candidate.get("nearest_actionable_ancestor") or {}
    if parent.get("focused") is True:
        return True
    return False


def _perform_destination_action(reader: object, target: dict) -> list[dict]:
    path = str(target.get("resolved_target_ax_path") or "")
    method = str(target.get("resolution_method") or "")
    if not path:
        raise AXDiagnosticError("Resolved target path is empty.")
    actions: list[dict] = []
    if method == "focusable_then_press_target":
        _reader_perform_action(reader, path, "AXSetFocus")
        actions.append({"path": path, "action": "AXSetFocus"})
    _reader_perform_action(reader, path, "AXPress")
    actions.append({"path": path, "action": "AXPress"})
    return actions


def _reader_perform_action(reader: object, path: str, action: str, *, action_context: dict | None = None) -> None:
    performer = getattr(reader, "perform_action", None)
    if performer is None:
        raise AXDiagnosticError("Reader does not support explicit AX actions.")
    if action_context is None:
        result = performer(path, action)
    else:
        result = performer(path, action, action_context=action_context)
    if result is False:
        raise AXDiagnosticError(f"{action} failed for {path}.")


def _invoke_reader_ax_action(reader: object, path: str, action: str, *, action_context: dict | None = None) -> None:
    _reader_perform_action(reader, path, action, action_context=action_context)


def _reader_last_ax_action_error_code(reader: object, path: str, action: str) -> int | None:
    record = getattr(reader, "last_ax_action_result", None)
    if not isinstance(record, dict):
        return None
    if record.get("path") != path or record.get("action") != action:
        return None
    error_code = record.get("error_code")
    return int(error_code) if isinstance(error_code, int) else None


def _apply_project_chat_post_action_diagnostics(result: dict, post: dict) -> None:
    signals = post.get("signals") or []
    result["ui_changed_after_action"] = bool(signals)
    result["destination_confirmed"] = bool(post.get("inspection_available") and post.get("confirmed"))


def _verify_destination_status(pre: dict, post: dict, kind: str, title: str) -> str:
    if post.get("requested_title_selected"):
        if not pre.get("requested_title_selected"):
            return "verified_destination_changed"
        return "action_performed_no_observable_change"
    pre_selected = (pre.get("any_selected_candidate_path"), pre.get("any_selected_target_path"))
    post_selected = (post.get("any_selected_candidate_path"), post.get("any_selected_target_path"))
    if post_selected != pre_selected and any(post_selected):
        return "destination_changed_but_identity_unverified"
    return "action_performed_no_observable_change"


def _filtering_summary(snapshots: list[AXElementSnapshot], categories: dict[str, list[dict]]) -> dict:
    candidate_paths = {
        item["path"]
        for candidates in categories.values()
        for item in candidates
    }
    long_text_fields = 0
    redacted_text_fields = 0
    long_text_redaction_samples = []
    for snapshot in snapshots:
        classification = _label_classification(snapshot)
        for source, value in (
            ("identifier", snapshot.identifier),
            ("title", snapshot.title),
            ("description", snapshot.description),
            ("value", snapshot.value),
        ):
            normalized = _normalized_label(value)
            if not normalized:
                continue
            if len(normalized) > LONG_TEXT_REDACTION_THRESHOLD:
                long_text_fields += 1
                if len(long_text_redaction_samples) < 5:
                    report = _label_report(normalized, classification)
                    report["path"] = _bounded_text(snapshot.path, MAX_PATH_LENGTH)
                    report["role"] = _bounded_text(snapshot.role, MAX_ROLE_LENGTH)
                    report["source"] = source
                    long_text_redaction_samples.append(report)
            if _label_report(normalized, classification)["redacted"]:
                redacted_text_fields += 1
    return {
        "total_nodes_observed": len(snapshots),
        "candidate_nodes_before_caps": len(candidate_paths),
        "excluded_non_candidate_nodes": max(0, len(snapshots) - len(candidate_paths)),
        "long_text_fields_redacted": long_text_fields,
        "redacted_text_fields": redacted_text_fields,
        "max_candidates_per_category": MAX_CANDIDATES_PER_CATEGORY,
        "long_text_redaction_threshold": LONG_TEXT_REDACTION_THRESHOLD,
        "long_text_redaction_samples": long_text_redaction_samples,
    }


def _sanitize_window_metadata(metadata: dict) -> dict:
    sanitized = dict(metadata)
    window = sanitized.get("window")
    if isinstance(window, AXElementSnapshot):
        sanitized["window"] = _sanitized_element(window)
    elif isinstance(window, dict):
        if any(isinstance(window.get(key), dict) for key in ("identifier", "title", "description", "value")):
            sanitized["window"] = _sanitize_pre_sanitized_element_dict(window)
        else:
            sanitized["window"] = _sanitize_existing_element_dict(window)
    return sanitized


def _sanitize_pre_sanitized_element_dict(element: dict) -> dict:
    allowed = {
        "path",
        "depth",
        "role",
        "subrole",
        "enabled",
        "focused",
        "actions",
        "identifier",
        "title",
        "description",
        "value",
        "value_length",
    }
    sanitized = {key: value for key, value in element.items() if key in allowed}
    sanitized["path"] = _bounded_text(str(sanitized.get("path") or ""), MAX_PATH_LENGTH)
    sanitized["role"] = _bounded_text(str(sanitized.get("role") or ""), MAX_ROLE_LENGTH)
    sanitized["subrole"] = _bounded_text(str(sanitized.get("subrole") or ""), MAX_ROLE_LENGTH)
    sanitized["actions"] = _safe_actions(tuple(str(action) for action in sanitized.get("actions") or ()))
    return sanitized


def _sanitize_existing_element_dict(element: dict) -> dict:
    role = str(element.get("role") or "")
    synthetic = AXElementSnapshot(
        path=str(element.get("path") or ""),
        depth=int(element.get("depth") or 0),
        role=role,
        subrole=str(element.get("subrole") or ""),
        identifier=str(element.get("identifier") or ""),
        title=str(element.get("title") or ""),
        description=str(element.get("description") or ""),
        value=str(element.get("value") or ""),
        enabled=element.get("enabled") if isinstance(element.get("enabled"), bool) else None,
        focused=element.get("focused") if isinstance(element.get("focused"), bool) else None,
        actions=tuple(str(action) for action in element.get("actions") or ()),
    )
    return _sanitized_element(synthetic)


class _ReadOnlyAXReader:
    def __init__(self, app_name: str, max_depth: int, max_nodes: int) -> None:
        if sys.platform != "darwin":
            raise AXDiagnosticError("Unsupported platform.")
        self.app_name = app_name
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._attr_cache: dict[str, int] = {}
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._cf.CFGetTypeID.argtypes = [c_void_p]
        self._cf.CFGetTypeID.restype = c_ulong
        self._cf.CFStringGetTypeID.restype = c_ulong
        self._cf.CFArrayGetTypeID.restype = c_ulong
        self._cf.CFBooleanGetTypeID.restype = c_ulong
        self._cf.CFBooleanGetValue.argtypes = [c_void_p]
        self._cf.CFBooleanGetValue.restype = c_bool
        self._cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_ulong]
        self._cf.CFStringCreateWithCString.restype = c_void_p
        self._cf.CFStringGetLength.argtypes = [c_void_p]
        self._cf.CFStringGetLength.restype = c_long
        self._cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_ulong]
        self._cf.CFStringGetCString.restype = c_bool
        self._cf.CFArrayGetCount.argtypes = [c_void_p]
        self._cf.CFArrayGetCount.restype = c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        self._cf.CFArrayGetValueAtIndex.restype = c_void_p

        self._ax.AXUIElementCreateApplication.argtypes = [c_int]
        self._ax.AXUIElementCreateApplication.restype = c_void_p
        self._ax.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyAttributeValue.restype = c_int
        self._ax.AXUIElementCopyActionNames.argtypes = [c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyActionNames.restype = c_int

        self._string_type = self._cf.CFStringGetTypeID()
        self._array_type = self._cf.CFArrayGetTypeID()
        self._boolean_type = self._cf.CFBooleanGetTypeID()

    def collect(self, pid: int) -> tuple[list[AXElementSnapshot], dict, dict]:
        app_element = self._ax.AXUIElementCreateApplication(pid)
        if not app_element:
            raise AXDiagnosticError("Could not create AX application element.")
        window, window_source = self._window(app_element)
        if not window:
            return [], {
                "visited_nodes": 0,
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
                "truncated_by_node_limit": False,
                "truncated_by_depth_limit": False,
            }, {"window_source": "none"}
        snapshots, stats = _collect_tree(window, self, max_depth=self.max_depth, max_nodes=self.max_nodes)
        window_snapshot = snapshots[0] if snapshots else AXElementSnapshot(path="W", depth=0)
        return snapshots, stats, {
            "window_source": window_source,
            "window": _sanitized_element(window_snapshot),
        }

    def snapshot(self, element: object, path: str, depth: int) -> AXElementSnapshot:
        element_id = int(element)
        return AXElementSnapshot(
            path=path,
            depth=depth,
            role=self._cf_string(self._copy_attribute(element_id, "AXRole")),
            subrole=self._cf_string(self._copy_attribute(element_id, "AXSubrole")),
            identifier=self._cf_string(self._copy_attribute(element_id, "AXIdentifier")),
            title=self._cf_string(self._copy_attribute(element_id, "AXTitle")),
            description=self._cf_string(self._copy_attribute(element_id, "AXDescription")),
            value=self._cf_string(self._copy_attribute(element_id, "AXValue")),
            enabled=self._cf_bool(self._copy_attribute(element_id, "AXEnabled")),
            focused=self._cf_bool(self._copy_attribute(element_id, "AXFocused")),
            actions=self._array_strings(self._copy_actions(element_id)),
        )

    def children(self, element: object) -> list[object]:
        element_id = int(element)
        for name in ("AXChildren", "AXVisibleChildren"):
            value = self._copy_attribute(element_id, name)
            if not value:
                continue
            children = list(self._array_values(value))
            if children:
                return children
        return []

    def _window(self, app_element: int) -> tuple[int | None, str]:
        focused_window = self._copy_attribute(app_element, "AXFocusedWindow")
        if focused_window:
            return focused_window, "AXFocusedWindow"
        windows = list(self._array_values(self._copy_attribute(app_element, "AXWindows")))
        for window in windows:
            minimized = self._cf_bool(self._copy_attribute(window, "AXMinimized"))
            if minimized is not True:
                return window, "first_visible_AXWindow"
        return (windows[0], "first_AXWindow") if windows else (None, "none")

    def _attribute_ref(self, name: str) -> int:
        if name not in self._attr_cache:
            self._attr_cache[name] = self._cf.CFStringCreateWithCString(
                None,
                name.encode("utf-8"),
                0x08000100,
            )
        return self._attr_cache[name]

    def _copy_attribute(self, element: int, name: str) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyAttributeValue(
            c_void_p(element),
            c_void_p(self._attribute_ref(name)),
            byref(output),
        )
        if error != 0 or not output.value:
            return None
        return output.value

    def _copy_actions(self, element: int) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyActionNames(c_void_p(element), byref(output))
        if error != 0 or not output.value:
            return None
        return output.value

    def _cf_string(self, value: int | None) -> str:
        if not value:
            return ""
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._string_type:
                return ""
            length = self._cf.CFStringGetLength(c_void_p(value))
            buffer = create_string_buffer(max(16, length * 4 + 1))
            ok = self._cf.CFStringGetCString(c_void_p(value), buffer, len(buffer), 0x08000100)
            if not ok:
                return ""
            return buffer.value.decode("utf-8", "replace")
        except Exception:
            return ""

    def _cf_bool(self, value: int | None) -> bool | None:
        if not value:
            return None
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._boolean_type:
                return None
            return bool(self._cf.CFBooleanGetValue(c_void_p(value)))
        except Exception:
            return None

    def _array_strings(self, value: int | None) -> tuple[str, ...]:
        return tuple(text for text in (self._cf_string(item) for item in self._array_values(value)) if text)

    def _array_values(self, value: int | None) -> tuple[int, ...]:
        if not value:
            return ()
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._array_type:
                return ()
            return tuple(
                self._cf.CFArrayGetValueAtIndex(c_void_p(value), index)
                for index in range(self._cf.CFArrayGetCount(c_void_p(value)))
            )
        except Exception:
            return ()


class _DetailedReadOnlyAXReader(_ReadOnlyAXReader):
    def __init__(self, app_name: str, max_depth: int, max_nodes: int) -> None:
        self._elements_by_path: dict[str, int] = {}
        self._path_by_element: dict[int, str] = {}
        super().__init__(app_name, max_depth, max_nodes)

    def _configure_signatures(self) -> None:
        super()._configure_signatures()
        self._ax.AXUIElementCopyElementAtPosition.argtypes = [c_void_p, c_float, c_float, POINTER(c_void_p)]
        self._ax.AXUIElementCopyElementAtPosition.restype = c_int
        self._ax.AXUIElementCopyAttributeNames.argtypes = [c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyAttributeNames.restype = c_int
        self._ax.AXUIElementCopyParameterizedAttributeNames.argtypes = [c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyParameterizedAttributeNames.restype = c_int
        self._ax.AXUIElementIsAttributeSettable.argtypes = [c_void_p, c_void_p, POINTER(c_bool)]
        self._ax.AXUIElementIsAttributeSettable.restype = c_int
        self._ax.AXUIElementCopyActionDescription.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyActionDescription.restype = c_int
        self._ax.AXValueGetValue.argtypes = [c_void_p, c_int, c_void_p]
        self._ax.AXValueGetValue.restype = c_bool

    def collect(self, pid: int) -> tuple[list[AXElementSnapshot], dict, dict]:
        snapshots, stats, metadata = super().collect(pid)
        self._path_by_element = {element: path for path, element in self._elements_by_path.items()}
        return [self._enrich_relationship_paths(snapshot) for snapshot in snapshots], stats, metadata

    def snapshot(self, element: object, path: str, depth: int) -> AXElementSnapshot:
        element_id = int(element)
        self._elements_by_path[path] = element_id
        base = super().snapshot(element, path, depth)
        attribute_names = self._array_strings(self._copy_attribute_names(element_id))
        settable = tuple(
            name
            for name in SELECTION_FOCUS_ATTRIBUTES
            if name in attribute_names and self._is_attribute_settable(element_id, name)
        )
        return replace(
            base,
            native_id=element_id,
            selected=self._cf_bool(self._copy_attribute(element_id, "AXSelected")),
            attribute_names=tuple(_safe_attribute_names(attribute_names)),
            parameterized_attribute_names=tuple(
                _safe_parameterized_attribute_names(self._array_strings(self._copy_parameterized_attribute_names(element_id)))
            ),
            settable_attribute_names=settable,
            action_descriptions=self._action_description_pairs(element_id, base.actions),
            direct_child_count=self._array_count(self._copy_attribute(element_id, "AXChildren")),
            visible_child_count=self._array_count(self._copy_attribute(element_id, "AXVisibleChildren")),
            frame=self._copy_frame(element_id),
        )

    def _enrich_relationship_paths(self, snapshot: AXElementSnapshot) -> AXElementSnapshot:
        element = self._elements_by_path.get(snapshot.path)
        if element is None:
            return snapshot
        linked = []
        for attribute in LINKED_UI_ATTRIBUTES:
            linked.extend((attribute, path) for path in self._attribute_paths(element, attribute))
        return replace(
            snapshot,
            linked_element_paths=tuple(linked[:DEEP_INSPECTOR_RELATED_MAX_NODES]),
            row_paths=tuple(self._attribute_paths(element, "AXRows")[:DEEP_INSPECTOR_RELATED_MAX_NODES]),
            visible_row_paths=tuple(self._attribute_paths(element, "AXVisibleRows")[:DEEP_INSPECTOR_RELATED_MAX_NODES]),
            selected_row_paths=tuple(self._attribute_paths(element, "AXSelectedRows")[:DEEP_INSPECTOR_RELATED_MAX_NODES]),
            selected_child_paths=tuple(self._attribute_paths(element, "AXSelectedChildren")[:DEEP_INSPECTOR_RELATED_MAX_NODES]),
        )

    def _copy_attribute_names(self, element: int) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyAttributeNames(c_void_p(element), byref(output))
        if error != 0 or not output.value:
            return None
        return output.value

    def _copy_parameterized_attribute_names(self, element: int) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyParameterizedAttributeNames(c_void_p(element), byref(output))
        if error != 0 or not output.value:
            return None
        return output.value

    def _is_attribute_settable(self, element: int, attribute: str) -> bool:
        output = c_bool(False)
        error = self._ax.AXUIElementIsAttributeSettable(
            c_void_p(element),
            c_void_p(self._attribute_ref(attribute)),
            byref(output),
        )
        return error == 0 and bool(output.value)

    def _copy_action_description(self, element: int, action: str) -> str:
        output = c_void_p()
        error = self._ax.AXUIElementCopyActionDescription(
            c_void_p(element),
            c_void_p(self._attribute_ref(action)),
            byref(output),
        )
        if error != 0 or not output.value:
            return ""
        return self._cf_string(output.value)

    def _action_description_pairs(self, element: int, actions: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        pairs = []
        for action in _safe_actions(actions):
            description = self._copy_action_description(element, action)
            if description:
                pairs.append((action, description))
        return tuple(pairs)

    def _array_count(self, value: int | None) -> int | None:
        if not value:
            return None
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._array_type:
                return None
            return int(self._cf.CFArrayGetCount(c_void_p(value)))
        except Exception:
            return None

    def _attribute_paths(self, element: int, attribute: str) -> list[str]:
        value = self._copy_attribute(element, attribute)
        if not value:
            return []
        array_values = self._array_values(value)
        if array_values:
            return [path for item in array_values if (path := self._path_by_element.get(int(item)))]
        path = self._path_by_element.get(int(value))
        return [path] if path else []

    def _copy_frame(self, element: int) -> tuple[float, float, float, float] | None:
        value = self._copy_attribute(element, "AXFrame")
        if not value:
            return None
        rect = _CGRect()
        ok = self._ax.AXValueGetValue(c_void_p(value), c_int(3), byref(rect))
        if not ok:
            return None
        return (float(rect.origin.x), float(rect.origin.y), float(rect.size.width), float(rect.size.height))

    def hit_test_at_position(self, pid: int, point: tuple[float, float], requested_title: str) -> dict:
        app_element = self._ax.AXUIElementCreateApplication(pid)
        if not app_element:
            return {"available": False, "error": "Could not create AX application element.", "path": ""}
        output = c_void_p()
        error = self._ax.AXUIElementCopyElementAtPosition(
            c_void_p(app_element),
            c_float(float(point[0])),
            c_float(float(point[1])),
            byref(output),
        )
        if error != 0 or not output.value:
            return {"available": False, "error": f"AXUIElementCopyElementAtPosition failed: {error}", "path": ""}
        element = int(output.value)
        return {
            "available": True,
            "error": "",
            "path": self._path_for_element(element),
            "native_element_id": element,
            "role": _bounded_text(self._cf_string(self._copy_attribute(element, "AXRole")), MAX_ROLE_LENGTH),
            "subrole": _bounded_text(self._cf_string(self._copy_attribute(element, "AXSubrole")), MAX_ROLE_LENGTH),
            "frame": _frame_geometry_report(self._copy_frame(element)),
            "title": _hit_test_title_report(
                _normalized_label(
                    self._cf_string(self._copy_attribute(element, "AXTitle"))
                    or self._cf_string(self._copy_attribute(element, "AXDescription"))
                    or self._cf_string(self._copy_attribute(element, "AXValue"))
                ),
                requested_title,
            ),
            "parent_chain": self._hit_test_parent_chain(element, requested_title),
        }

    def _path_for_element(self, element: int) -> str:
        return self._path_by_element.get(element) or self._path_by_element.get(int(element)) or ""

    def _hit_test_parent_chain(self, element: int, requested_title: str) -> list[dict]:
        chain = []
        seen = {int(element)}
        current = element
        for _index in range(MAX_ACTIONABLE_ANCESTOR_DEPTH + 8):
            parent = self._copy_attribute(current, "AXParent")
            if not parent or int(parent) in seen:
                break
            seen.add(int(parent))
            role = self._cf_string(self._copy_attribute(parent, "AXRole"))
            chain.append(
                {
                    "path": self._path_for_element(parent),
                    "native_element_id": int(parent),
                    "role": _bounded_text(role, MAX_ROLE_LENGTH),
                    "subrole": _bounded_text(self._cf_string(self._copy_attribute(parent, "AXSubrole")), MAX_ROLE_LENGTH),
                    "frame": _frame_geometry_report(self._copy_frame(parent)),
                    "title": _hit_test_title_report(
                        _normalized_label(
                            self._cf_string(self._copy_attribute(parent, "AXTitle"))
                            or self._cf_string(self._copy_attribute(parent, "AXDescription"))
                            or self._cf_string(self._copy_attribute(parent, "AXValue"))
                        ),
                        requested_title,
                    ),
                }
            )
            if role in {"AXApplication", "AXWindow"}:
                break
            current = int(parent)
        return chain


class _ActionAXReader(_ReadOnlyAXReader):
    def __init__(self, app_name: str, max_depth: int, max_nodes: int) -> None:
        self._elements_by_path: dict[str, int] = {}
        super().__init__(app_name, max_depth, max_nodes)

    def _configure_signatures(self) -> None:
        super()._configure_signatures()
        self._ax.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
        self._ax.AXUIElementPerformAction.restype = c_int

    def snapshot(self, element: object, path: str, depth: int) -> AXElementSnapshot:
        self._elements_by_path[path] = int(element)
        return super().snapshot(element, path, depth)

    def perform_action(self, path: str, action: str, *, action_context: dict | None = None) -> bool:
        del action_context
        if action not in {"AXSetFocus", "AXPress"}:
            raise AXDiagnosticError("Unsupported explicit sidebar verification action.")
        element = self._elements_by_path.get(path)
        if not element:
            raise AXDiagnosticError(f"No AX element was captured for path {path}.")
        error = self._ax.AXUIElementPerformAction(
            c_void_p(element),
            c_void_p(self._attribute_ref(action)),
        )
        self.last_ax_action_result = {"path": path, "action": action, "error_code": int(error)}
        return error == 0


class _AutonomousSidebarAXReader(_DetailedReadOnlyAXReader):
    def _configure_signatures(self) -> None:
        super()._configure_signatures()
        self._ax.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
        self._ax.AXUIElementPerformAction.restype = c_int

    def perform_action(self, path: str, action: str, *, action_context: dict | None = None) -> bool:
        if action == "AXPress":
            pass
        elif action == "AXScrollToVisible":
            if not _autonomous_sidebar_axscrolltovisible_authorized(path, action_context):
                raise AXDiagnosticError("Unsupported autonomous sidebar action.")
        else:
            raise AXDiagnosticError("Unsupported autonomous sidebar action.")
        element = self._elements_by_path.get(path)
        if not element:
            raise AXDiagnosticError(f"No AX element was captured for path {path}.")
        error = self._ax.AXUIElementPerformAction(c_void_p(element), c_void_p(self._attribute_ref(action)))
        self.last_ax_action_result = {"path": path, "action": action, "error_code": int(error)}
        return error == 0


def _autonomous_sidebar_axscrolltovisible_authorized(path: str, action_context: dict | None) -> bool:
    context = action_context or {}
    if str(context.get("target_path") or "") != str(path or ""):
        return False
    return _project_chat_alignment_policy_conditions_satisfied(context)


def _frame_click_coordinate_diagnostics(plan: dict, display_probe_factory: object) -> dict:
    snapshots = plan.get("snapshots") or []
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    window_frame = _window_frame_from_metadata(plan.get("window_metadata") or {}, snapshots)
    source = plan.get("source") or {}
    sidebar_context = plan.get("sidebar_context") or {}
    sidebar_frame = _frame_tuple(sidebar_context.get("frame"))
    row_path = str(source.get("row_path") or "")
    row_snapshot = snapshots_by_path.get(row_path)
    row_frame = _frame_tuple(row_snapshot.frame) if row_snapshot else _frame_tuple(source.get("source_frame"))

    click_point = plan.get("click_point") or {}
    intended_point = _xy_point(click_point.get("x"), click_point.get("y"))

    display_bounds = None
    mouse_location = None
    probe_error = ""
    try:
        probe = display_probe_factory()
        display_bounds = _frame_tuple(probe.primary_display_bounds())
        mouse_location = probe.current_mouse_location()
    except Exception as exc:
        probe_error = str(exc)

    inverted_point = None
    if intended_point is not None and display_bounds is not None:
        _, display_y, _, display_height = display_bounds
        inverted_point = (intended_point[0], display_y + display_height - intended_point[1])

    raw_in_row = _point_inside_frame(intended_point, row_frame)
    inverted_in_row = _point_inside_frame(inverted_point, row_frame)
    raw_matches = bool(raw_in_row)
    inverted_matches = bool(inverted_in_row)

    if raw_matches and not inverted_matches:
        recommendation = "raw"
    elif inverted_matches and not raw_matches:
        recommendation = "vertically_inverted"
    else:
        recommendation = "unresolved"

    return {
        "raw_ax_row_frame": _frame_geometry_report(row_frame),
        "ax_sidebar_frame": _frame_geometry_report(sidebar_frame),
        "focused_window_frame": _frame_geometry_report(window_frame),
        "primary_display_bounds": _frame_geometry_report(display_bounds),
        "current_mouse_location": _xy_report(mouse_location),
        "intended_event_point": _xy_report(intended_point),
        "vertically_inverted_candidate_point": _xy_report(inverted_point),
        "raw_point_containment": {
            "in_ax_row_frame": raw_in_row,
            "in_ax_sidebar_frame": _point_inside_frame(intended_point, sidebar_frame),
            "in_focused_window_frame": _point_inside_frame(intended_point, window_frame),
        },
        "inverted_point_containment": {
            "in_ax_row_frame": inverted_in_row,
            "in_ax_sidebar_frame": _point_inside_frame(inverted_point, sidebar_frame),
            "in_focused_window_frame": _point_inside_frame(inverted_point, window_frame),
        },
        "assessment": {
            "raw_point_matches_ax_frame": raw_matches,
            "inverted_point_matches_ax_frame": inverted_matches,
            "neither_point_matches_ax_frame": not raw_matches and not inverted_matches,
            "ambiguous_coordinate_mapping": raw_matches and inverted_matches,
        },
        "recommended_click_coordinate_mapping": recommendation,
        "cursor_unmoved": True,
        "probe_error": probe_error,
    }


def _xy_point(x: object, y: object) -> tuple[float, float] | None:
    if x is None or y is None:
        return None
    try:
        return (float(x), float(y))
    except (TypeError, ValueError):
        return None


def _xy_report(point: tuple[float, float] | None) -> dict:
    if point is None:
        return {"x": None, "y": None}
    return {"x": round(point[0], 2), "y": round(point[1], 2)}


def _frame_geometry_report(frame: tuple[float, float, float, float] | None) -> dict:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return {"x": None, "y": None, "width": None, "height": None}
    x, y, width, height = normalized
    return {"x": round(x, 2), "y": round(y, 2), "width": round(width, 2), "height": round(height, 2)}


def _point_inside_frame(
    point: tuple[float, float] | None,
    frame: tuple[float, float, float, float] | None,
) -> bool:
    normalized = _frame_tuple(frame)
    if point is None or normalized is None:
        return False
    px, py = point
    x, y, width, height = normalized
    return x <= px <= x + width and y <= py <= y + height


def calibrate_chatgpt_sidebar_coordinate_mapping(
    *,
    kind: str,
    title: str,
    confirm_calibration_click: bool = False,
    app_name: str = "ChatGPT",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    process_resolver: object | None = None,
    reader_factory: object | None = None,
    display_probe_factory: object | None = None,
    windowserver_probe_factory: object | None = None,
    click_service_factory: object | None = None,
    sleep_function: object | None = None,
    post_click_settle_seconds: float = CALIBRATION_POST_CLICK_SETTLE_SECONDS,
    before_click_callback: object | None = None,
) -> dict:
    requested_title = _normalized_label(title)
    result = _base_coordinate_calibration_result(kind, requested_title, app_name)
    result["confirm_calibration_click"] = bool(confirm_calibration_click)
    if kind not in {"project", "chat"} or not requested_title:
        result.update(
            {
                "status": "target_not_found",
                "error": "kind must be project or chat and title must be non-empty.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result
    if max_depth < 0 or max_nodes <= 0:
        result.update(
            {
                "status": "accessibility_failure",
                "error": "max_depth must be >= 0 and max_nodes must be > 0.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result
    if sys.platform != "darwin":
        result.update(
            {
                "status": "accessibility_failure",
                "error": "ChatGPT sidebar coordinate calibration is only supported on macOS.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result

    resolver = process_resolver or resolve_chatgpt_process
    try:
        process = resolver(app_name)
    except Exception as exc:
        result.update(
            {
                "status": "accessibility_failure",
                "error": str(exc),
                "process_resolution_method": PROCESS_RESOLUTION_METHOD,
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result

    result["process_resolution_method"] = process.method
    result["pid_present"] = process.pid is not None
    if process.pid is None:
        result.update(
            {
                "status": "accessibility_failure",
                "error": process.error or f"No running application named {app_name!r} was found.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result

    display_evidence = _collect_calibration_display_evidence(display_probe_factory or _CoreGraphicsDisplayProbe)
    cursor_point = _xy_point(
        (display_evidence.get("current_global_physical_cursor_location") or {}).get("x"),
        (display_evidence.get("current_global_physical_cursor_location") or {}).get("y"),
    )
    result["current_global_physical_cursor_location"] = display_evidence.get("current_global_physical_cursor_location") or _xy_report(None)
    result["display_evidence"] = display_evidence
    if cursor_point is None:
        result.update(
            {
                "status": "cursor_location_unavailable",
                "error": display_evidence.get("error") or "Current cursor location was unavailable.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result

    factory = reader_factory or _DetailedReadOnlyAXReader
    try:
        reader = factory(app_name, max_depth, max_nodes)
        snapshots, stats, window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update(
            {
                "status": "accessibility_failure",
                "error": str(exc),
                "pid_present": True,
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result

    classified = classify_navigation_snapshots(
        snapshots,
        stats,
        window_metadata,
        include_visible_navigation_titles=True,
    )
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    result["window_available"] = bool(classified.get("window_available"))
    result["traversal"] = classified.get("traversal") or {}
    if not matches:
        result.update(
            {
                "status": "target_not_found",
                "error": "No exactly matching visible sidebar destination was found.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result
    if len(matches) > 1:
        result.update(
            {
                "status": "target_ambiguous",
                "error": "More than one matching visible sidebar destination was found.",
                "final_mapping_classification": "target_or_window_frame_unavailable",
                "final_click_classification": "destination_not_resolved_before_click" if confirm_calibration_click else "",
            }
        )
        return result

    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    candidate = matches[0]
    local_scope = _sidebar_destination_local_scope(candidate, snapshots, snapshots_by_path)
    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    windowserver_evidence = _collect_windowserver_evidence(
        process.pid,
        cursor_point,
        windowserver_probe_factory or _WindowServerBoundsProbe,
    )
    chosen_windowserver_frame = _frame_tuple((windowserver_evidence.get("chosen_window") or {}).get("bounds"))
    hit_test = _collect_calibration_hit_test(reader, process.pid, cursor_point, requested_title)
    relationship = _hit_test_relationship(hit_test, local_scope, snapshots_by_path)
    frame_evidence = _coordinate_calibration_frame_evidence(
        local_scope,
        snapshots_by_path,
        cursor_point=cursor_point,
        ax_window_frame=window_frame,
        windowserver_frame=chosen_windowserver_frame,
        display_evidence=display_evidence,
        windowserver_evidence=windowserver_evidence,
    )
    safe_target = _calibration_raw_target_point(local_scope, snapshots_by_path)
    candidates = _coordinate_mapping_candidates(
        raw_target_point=safe_target.get("point"),
        local_scope=local_scope,
        snapshots_by_path=snapshots_by_path,
        cursor_point=cursor_point,
        ax_window_frame=window_frame,
        windowserver_frame=chosen_windowserver_frame,
        display_frame=_frame_tuple(display_evidence.get("display_containing_cursor_bounds"))
        or _frame_tuple(display_evidence.get("primary_display_bounds")),
        hit_test_relationship=relationship,
    )
    classification = _classify_coordinate_mapping(
        candidates,
        hit_test_relationship=relationship,
        cursor_point=cursor_point,
        target_frame_available=bool(safe_target.get("point")),
        window_frame_available=bool(window_frame or chosen_windowserver_frame),
    )
    transform = CLICK_TRANSFORMS.get(classification, "unresolved")
    result.update(
        {
            "ok": classification in CLICK_TRANSFORMS,
            "status": "calibration_completed",
            "target": _coordinate_calibration_target_summary(candidate, local_scope),
            "hit_test": hit_test,
            "hit_test_relationship_to_requested_target": relationship,
            "frame_evidence": frame_evidence,
            "raw_target_safe_interior_point": _xy_report(safe_target.get("point")),
            "raw_target_point_source": safe_target.get("source") or "",
            "windowserver_evidence": windowserver_evidence,
            "mapping_candidates": candidates,
            "final_mapping_classification": classification,
            "recommended_future_click_transform": transform,
            "recommended_runtime_click_transform": transform,
            "error": "",
        }
    )
    if not confirm_calibration_click:
        return result

    click_selection = _select_calibration_click_candidate(candidates, classification)
    result["selected_source_mapping_candidate"] = click_selection.get("candidate") or {}
    result["calculated_global_click_point"] = _xy_report(click_selection.get("point"))
    result["click_count"] = CALIBRATION_CONFIRMED_CLICK_COUNT
    result["inter_click_delay_ms"] = int(CALIBRATION_INTER_CLICK_DELAY_SECONDS * 1000)
    if not click_selection.get("ok"):
        result.update(
            {
                "ok": False,
                "status": "safe_click_point_unavailable",
                "final_click_classification": "safe_click_point_unavailable",
                "recommended_runtime_click_transform": "unresolved",
                "error": click_selection.get("error") or "No safe calculated mapping candidate was available.",
            }
        )
        return result

    click_point = click_selection["point"]
    sleeper = sleep_function or time.sleep
    try:
        clicker = (click_service_factory or _CoreGraphicsFrameClickService)()
        if not clicker.has_permission():
            result.update(
                {
                    "ok": False,
                    "status": "click_posting_failed",
                    "final_click_classification": "click_posting_failed",
                    "recommended_runtime_click_transform": "unresolved",
                    "error": "CoreGraphics post-event permission is unavailable.",
                }
            )
            return result
        if before_click_callback is not None:
            before_click_callback()
        first_click = clicker.left_click(click_point[0], click_point[1])
        if not first_click.get("ok"):
            result.update(_calibration_click_failure(first_click))
            return result
        result["actions_performed"].extend(first_click.get("actions_performed") or [])
        sleeper(CALIBRATION_INTER_CLICK_DELAY_SECONDS)
        second_click = clicker.left_click(click_point[0], click_point[1])
        if not second_click.get("ok"):
            result.update(_calibration_click_failure(second_click))
            return result
        result["actions_performed"].extend(second_click.get("actions_performed") or [])
        if not _two_click_actions_match_point(result["actions_performed"], _xy_report(click_point)):
            result.update(
                {
                    "ok": False,
                    "status": "click_posting_failed",
                    "final_click_classification": "click_posting_failed",
                    "recommended_runtime_click_transform": "unresolved",
                    "error": "Posted actions did not match the calculated point and two-click sequence.",
                }
            )
            return result
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "status": "click_posting_failed",
                "final_click_classification": "click_posting_failed",
                "recommended_runtime_click_transform": "unresolved",
                "error": str(exc),
            }
        )
        return result

    if post_click_settle_seconds > 0:
        sleeper(min(post_click_settle_seconds, CALIBRATION_POST_CLICK_SETTLE_SECONDS))

    try:
        post_snapshots, post_stats, post_window_metadata = reader.collect(process.pid)
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "status": "post_click_inspection_unavailable",
                "final_click_classification": "post_click_inspection_unavailable",
                "recommended_runtime_click_transform": "unresolved",
                "error": str(exc),
            }
        )
        return result
    post_classified = classify_navigation_snapshots(
        post_snapshots,
        post_stats,
        post_window_metadata,
        include_visible_navigation_titles=True,
    )
    post_evidence = _post_click_destination_evidence(post_snapshots, post_classified, kind, requested_title)
    result["post_click_requested_destination_evidence"] = post_evidence
    final = _confirmed_calibration_final_classification(classification, post_evidence)
    result.update(
        {
            "ok": final == "click_confirmed_mapping_success",
            "status": final,
            "final_click_classification": final,
            "recommended_runtime_click_transform": click_selection.get("runtime_transform") if final == "click_confirmed_mapping_success" else "unresolved",
            "error": "" if final == "click_confirmed_mapping_success" else post_evidence.get("reason") or "Destination was not confirmed after the calculated clicks.",
        }
    )
    return result


def _base_coordinate_calibration_result(kind: str, title: str, app_name: str) -> dict:
    return {
        "ok": False,
        "status": "not_run",
        "app_name": app_name,
        "kind": kind,
        "title": title,
        "timestamp": datetime_now_iso(),
        "pid_present": False,
        "process_resolution_method": None,
        "window_available": False,
        "traversal": {},
        "current_global_physical_cursor_location": _xy_report(None),
        "target": {},
        "hit_test": {},
        "hit_test_relationship_to_requested_target": "unavailable",
        "frame_evidence": [],
        "display_evidence": {},
        "windowserver_evidence": {},
        "raw_target_safe_interior_point": _xy_report(None),
        "raw_target_point_source": "",
        "mapping_candidates": [],
        "final_mapping_classification": "target_or_window_frame_unavailable",
        "final_click_classification": "",
        "selected_source_mapping_candidate": {},
        "calculated_global_click_point": _xy_report(None),
        "click_count": 0,
        "inter_click_delay_ms": 0,
        "post_click_requested_destination_evidence": {},
        "recommended_future_click_transform": "unresolved",
        "recommended_runtime_click_transform": "unresolved",
        "read_only": True,
        "actions_performed": [],
        "error": "",
    }


def _collect_calibration_display_evidence(display_probe_factory: object) -> dict:
    result = {
        "current_global_physical_cursor_location": _xy_report(None),
        "primary_display_bounds": _frame_geometry_report(None),
        "display_containing_cursor_bounds": _frame_geometry_report(None),
        "error": "",
    }
    try:
        probe = display_probe_factory()
        cursor = probe.current_mouse_location()
        result["current_global_physical_cursor_location"] = _xy_report(cursor)
        result["primary_display_bounds"] = _frame_geometry_report(probe.primary_display_bounds())
        containing = getattr(probe, "display_bounds_containing_point", lambda point: None)(cursor)
        result["display_containing_cursor_bounds"] = _frame_geometry_report(containing)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _collect_windowserver_evidence(pid: int, cursor_point: tuple[float, float], probe_factory: object) -> dict:
    result = {"visible_windows": [], "chosen_window": {}, "error": ""}
    try:
        windows = probe_factory().visible_windows_for_pid(pid)
    except Exception as exc:
        result["error"] = str(exc)
        return result
    result["visible_windows"] = [_windowserver_window_report(window, cursor_point) for window in windows]
    chosen = _choose_windowserver_window(windows, cursor_point)
    result["chosen_window"] = _windowserver_window_report(chosen, cursor_point) if chosen else {}
    return result


def _windowserver_window_report(window: dict, cursor_point: tuple[float, float]) -> dict:
    bounds = _frame_tuple(window.get("bounds")) if window else None
    return {
        "window_id": window.get("window_id") if window else None,
        "owner_pid": window.get("owner_pid") if window else None,
        "layer": window.get("layer") if window else None,
        "onscreen": window.get("onscreen") if window else None,
        "bounds": _frame_geometry_report(bounds),
        "contains_cursor": _point_inside_frame(cursor_point, bounds),
    }


def _choose_windowserver_window(windows: list[dict], cursor_point: tuple[float, float]) -> dict | None:
    containing = [window for window in windows if _point_inside_frame(cursor_point, _frame_tuple(window.get("bounds")))]
    candidates = containing or windows
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda window: (
            0 if _point_inside_frame(cursor_point, _frame_tuple(window.get("bounds"))) else 1,
            -_frame_area(_frame_tuple(window.get("bounds"))),
            int(window.get("window_id") or 0),
        ),
    )[0]


def _collect_calibration_hit_test(
    reader: object,
    pid: int,
    cursor_point: tuple[float, float],
    requested_title: str,
) -> dict:
    hit_tester = getattr(reader, "hit_test_at_position", None)
    if hit_tester is None:
        return {"available": False, "error": "Reader does not support AX hit-testing.", "path": ""}
    try:
        hit = hit_tester(pid, cursor_point, requested_title)
    except Exception as exc:
        return {"available": False, "error": str(exc), "path": ""}
    if not isinstance(hit, dict):
        return {"available": False, "error": "AX hit-test returned an invalid result.", "path": ""}
    hit.setdefault("available", bool(hit.get("path")))
    hit.setdefault("error", "")
    hit.setdefault("path", "")
    hit.setdefault("role", "")
    hit.setdefault("subrole", "")
    hit.setdefault("title", _hit_test_title_report("", requested_title))
    hit.setdefault("parent_chain", [])
    return _sanitize_hit_test_report(hit, requested_title)


def _sanitize_hit_test_report(hit: dict, requested_title: str) -> dict:
    parent_chain = []
    for item in hit.get("parent_chain") or []:
        if not isinstance(item, dict):
            continue
        parent_chain.append(
            {
                "path": _bounded_text(str(item.get("path") or ""), MAX_PATH_LENGTH),
                "native_element_id": _int_or_none(item.get("native_element_id")),
                "role": _bounded_text(str(item.get("role") or ""), MAX_ROLE_LENGTH),
                "subrole": _bounded_text(str(item.get("subrole") or ""), MAX_ROLE_LENGTH),
                "frame": _frame_geometry_report(_frame_tuple(item.get("frame"))),
                "title": _hit_test_title_report(
                    _hit_test_title_text(item.get("title")),
                    requested_title,
                ),
            }
        )
    title_value = hit.get("title")
    if isinstance(title_value, dict):
        title_report = _hit_test_title_report(str(title_value.get("literal") or ""), requested_title)
    else:
        title_report = _hit_test_title_report(str(title_value or ""), requested_title)
    return {
        "available": bool(hit.get("available")),
        "error": str(hit.get("error") or ""),
        "path": _bounded_text(str(hit.get("path") or ""), MAX_PATH_LENGTH),
        "native_element_id": _int_or_none(hit.get("native_element_id")),
        "role": _bounded_text(str(hit.get("role") or ""), MAX_ROLE_LENGTH),
        "subrole": _bounded_text(str(hit.get("subrole") or ""), MAX_ROLE_LENGTH),
        "frame": _frame_geometry_report(_frame_tuple(hit.get("frame"))),
        "title": title_report,
        "parent_chain": parent_chain,
    }


def _hit_test_title_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("literal") or "")
    return str(value or "")


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hit_test_title_report(value: str, requested_title: str) -> dict:
    normalized = _normalized_label(value)
    if normalized and normalized == requested_title:
        return {
            "literal": requested_title,
            "redacted": False,
            "classification": "requested_visible_destination_title",
        }
    return {
        "literal": "",
        "redacted": bool(normalized),
        "classification": "redacted_unless_requested_title" if normalized else "empty",
    }


def _hit_test_relationship(hit_test: dict, scope: dict, snapshots_by_path: dict[str, AXElementSnapshot]) -> str:
    if not hit_test.get("available"):
        return "unavailable"
    path = str(hit_test.get("path") or "")
    parent_paths = [str(item.get("path") or "") for item in hit_test.get("parent_chain") or []]
    all_paths = [path] + parent_paths
    title_path = scope.get("title_path") or ""
    row_path = scope.get("row_path") or ""
    native_ids = {
        _int_or_none(hit_test.get("native_element_id")),
        *(_int_or_none(item.get("native_element_id")) for item in hit_test.get("parent_chain") or []),
    }
    title_snapshot = snapshots_by_path.get(title_path)
    row_snapshot = snapshots_by_path.get(row_path)
    if title_snapshot and title_snapshot.native_id is not None and title_snapshot.native_id in native_ids:
        return "exact_target_title"
    if row_snapshot and row_snapshot.native_id is not None and row_snapshot.native_id in native_ids:
        return "descendant_of_target_row"
    if path == title_path:
        return "exact_target_title"
    if path == row_path:
        return "descendant_of_target_row"
    if row_path and path.startswith(row_path + "."):
        return "descendant_of_target_row"
    if title_path and title_path in parent_paths:
        return "ancestor_of_target_title"
    if path and title_path and title_path.startswith(path + "."):
        return "ancestor_of_target_title"
    if row_path and row_path in parent_paths:
        return "descendant_of_target_row"
    geometry_relationship = _hit_test_geometry_relationship(hit_test, scope, snapshots_by_path)
    if geometry_relationship:
        return geometry_relationship
    row_parent = _parent_path(row_path)
    if row_parent and any(_parent_path(item) == row_parent for item in all_paths if item):
        return "sibling_or_nearby_unrelated_element"
    if path and path in snapshots_by_path:
        return "outside_resolved_target_structure"
    return "outside_resolved_target_structure"


def _hit_test_geometry_relationship(
    hit_test: dict,
    scope: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> str:
    hit_frame = _frame_tuple(hit_test.get("frame"))
    if hit_frame is None:
        for item in hit_test.get("parent_chain") or []:
            hit_frame = _frame_tuple(item.get("frame"))
            if hit_frame is not None:
                break
    if hit_frame is None:
        return ""
    role = str(hit_test.get("role") or "")
    if role not in ROWLIKE_ROLES and role not in ACTIONABLE_ROLES and role not in CONTAINER_ROLES:
        return ""
    title_path = str(scope.get("title_path") or "")
    row_path = str(scope.get("row_path") or "")
    title_frame = _frame_tuple((snapshots_by_path.get(title_path) or AXElementSnapshot(title_path, 0)).frame)
    row_frame = _frame_tuple((snapshots_by_path.get(row_path) or AXElementSnapshot(row_path, 0)).frame)
    if not (
        _frames_structurally_consistent(hit_frame, title_frame)
        or _frames_structurally_consistent(hit_frame, row_frame)
    ):
        return ""
    if _geometry_plausibly_matches_multiple_destinations(hit_frame, row_path, snapshots_by_path):
        return "sibling_or_nearby_unrelated_element"
    if _frames_structurally_consistent(hit_frame, title_frame):
        return "exact_target_title" if role in TEXTLIKE_ROLES else "ancestor_of_target_title"
    return "descendant_of_target_row"


def _frames_structurally_consistent(
    hit_frame: tuple[float, float, float, float] | None,
    target_frame: tuple[float, float, float, float] | None,
) -> bool:
    if hit_frame is None or target_frame is None:
        return False
    return (
        _frame_contains_with_tolerance(hit_frame, target_frame, FRAME_CONTAINMENT_TOLERANCE)
        or _frame_contains_with_tolerance(target_frame, hit_frame, FRAME_CONTAINMENT_TOLERANCE)
        or _frame_intersects(hit_frame, target_frame)
    )


def _geometry_plausibly_matches_multiple_destinations(
    hit_frame: tuple[float, float, float, float],
    row_path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> bool:
    row_parent = _parent_path(row_path)
    if not row_parent:
        return False
    matches = 0
    for snapshot in snapshots_by_path.values():
        if snapshot.path == row_path or _parent_path(snapshot.path) != row_parent:
            continue
        if snapshot.role not in ROWLIKE_ROLES and snapshot.role not in ACTIONABLE_ROLES:
            continue
        frame = _frame_tuple(snapshot.frame)
        if frame is not None and _frames_structurally_consistent(hit_frame, frame):
            matches += 1
    return matches > 0


def _coordinate_calibration_target_summary(candidate: dict, scope: dict) -> dict:
    return {
        "kind": candidate.get("classification") == "visible_project_title_candidate" and "project" or "chat",
        "title": candidate.get("exact_title") or "",
        "title_ax_path": scope.get("title_path") or "",
        "computed_row_ax_path": scope.get("row_path") or "",
        "list_ax_path": scope.get("list_path") or "",
    }


def _coordinate_calibration_frame_evidence(
    scope: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
    *,
    cursor_point: tuple[float, float],
    ax_window_frame: tuple[float, float, float, float] | None,
    windowserver_frame: tuple[float, float, float, float] | None,
    display_evidence: dict,
    windowserver_evidence: dict,
) -> list[dict]:
    title_path = str(scope.get("title_path") or "")
    row_path = str(scope.get("row_path") or "")
    title_frame = _frame_tuple((snapshots_by_path.get(title_path) or AXElementSnapshot(title_path, 0)).frame)
    row_frame = _frame_tuple((snapshots_by_path.get(row_path) or AXElementSnapshot(row_path, 0)).frame)
    frames: list[dict] = []

    def add(source: str, frame: tuple[float, float, float, float] | None, *, path: str = "", window_id: object = None, confidence: str = "unknown") -> None:
        frames.append(
            _calibration_frame_report(
                source,
                frame,
                cursor_point=cursor_point,
                title_frame=title_frame,
                row_frame=row_frame,
                path=path,
                window_id=window_id,
                coordinate_space_confidence=confidence,
            )
        )

    add("target_title_frame", title_frame, path=title_path, confidence=_ax_frame_confidence(title_frame, ax_window_frame, windowserver_frame))
    add("computed_row_frame", row_frame, path=row_path, confidence=_ax_frame_confidence(row_frame, ax_window_frame, windowserver_frame))
    for ancestor_path in _ancestor_paths(title_path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot is None:
            continue
        frame = _frame_tuple(snapshot.frame)
        if frame is not None:
            add(
                "bounded_target_ancestor_frame",
                frame,
                path=ancestor_path,
                confidence=_ax_frame_confidence(frame, ax_window_frame, windowserver_frame),
            )
        if ancestor_path == "W":
            break
    nearest = _nearest_scroll_list_section_ancestor(title_path, snapshots_by_path)
    if nearest is not None:
        add(
            "nearest_scroll_list_section_ancestor",
            _frame_tuple(nearest.frame),
            path=nearest.path,
            confidence=_ax_frame_confidence(_frame_tuple(nearest.frame), ax_window_frame, windowserver_frame),
        )
    add("chatgpt_ax_window_frame", ax_window_frame, path="W", confidence=_ax_frame_confidence(ax_window_frame, ax_window_frame, windowserver_frame))
    add("chatgpt_focused_ax_window_frame", ax_window_frame, path="W", confidence=_ax_frame_confidence(ax_window_frame, ax_window_frame, windowserver_frame))
    chosen = windowserver_evidence.get("chosen_window") or {}
    add(
        "chosen_chatgpt_windowserver_bounds",
        _frame_tuple(chosen.get("bounds")),
        window_id=chosen.get("window_id"),
        confidence="windowserver_global",
    )
    add("primary_display_bounds", _frame_tuple(display_evidence.get("primary_display_bounds")), confidence="display_global")
    add(
        "display_containing_cursor_bounds",
        _frame_tuple(display_evidence.get("display_containing_cursor_bounds")),
        confidence="display_global" if _frame_tuple(display_evidence.get("display_containing_cursor_bounds")) else "unavailable",
    )
    return frames


def _calibration_frame_report(
    source: str,
    frame: tuple[float, float, float, float] | None,
    *,
    cursor_point: tuple[float, float],
    title_frame: tuple[float, float, float, float] | None,
    row_frame: tuple[float, float, float, float] | None,
    path: str = "",
    window_id: object = None,
    coordinate_space_confidence: str = "unknown",
) -> dict:
    geometry = _frame_geometry_report(frame)
    return {
        "source": source,
        "ax_path": _bounded_text(path, MAX_PATH_LENGTH),
        "window_id": window_id,
        **geometry,
        "contains_global_physical_cursor": _point_inside_frame(cursor_point, frame),
        "contains_target_title_frame": _frame_contains_with_tolerance(frame, title_frame, FRAME_CONTAINMENT_TOLERANCE),
        "contains_target_row_frame": _frame_contains_with_tolerance(frame, row_frame, FRAME_CONTAINMENT_TOLERANCE),
        "coordinate_space_confidence": coordinate_space_confidence,
    }


def _ax_frame_confidence(
    frame: tuple[float, float, float, float] | None,
    ax_window_frame: tuple[float, float, float, float] | None,
    windowserver_frame: tuple[float, float, float, float] | None,
) -> str:
    if not _frame_is_valid(frame):
        return "unavailable"
    if windowserver_frame is not None and _frame_contains_with_tolerance(windowserver_frame, frame, FRAME_CONTAINMENT_TOLERANCE):
        return "possible_global_matches_windowserver"
    if ax_window_frame is not None and _frame_contains_with_tolerance(ax_window_frame, frame, FRAME_CONTAINMENT_TOLERANCE):
        return "possible_ax_window_global_or_same_origin"
    return "unknown_ax_coordinate_space"


def _nearest_scroll_list_section_ancestor(
    path: str,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> AXElementSnapshot | None:
    for ancestor_path in _ancestor_paths(path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot and (snapshot.role == "AXScrollArea" or _is_listlike(snapshot)):
            return snapshot
    return None


def _calibration_raw_target_point(scope: dict, snapshots_by_path: dict[str, AXElementSnapshot]) -> dict:
    row = snapshots_by_path.get(str(scope.get("row_path") or ""))
    title = snapshots_by_path.get(str(scope.get("title_path") or ""))
    for source, snapshot in (("target_title_frame", title), ("computed_row_frame", row)):
        point = _calibration_safe_interior_point(_frame_tuple(snapshot.frame) if snapshot else None)
        if point is not None:
            return {"source": source, "point": point}
    return {"source": "", "point": None}


def _calibration_safe_interior_point(frame: tuple[float, float, float, float] | None) -> tuple[float, float] | None:
    normalized = _frame_tuple(frame)
    if normalized is None or not _frame_is_valid(normalized):
        return None
    x, y, width, height = normalized
    inset = min(SAFE_CLICK_EDGE_INSET, max(1.0, width / 4.0), max(1.0, height / 4.0))
    overflow_exclusion = min(SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH, max(0.0, width / 3.0))
    left = x + inset
    right = x + width - inset - overflow_exclusion
    top = y + inset
    bottom = y + height - inset
    if right <= left or bottom <= top:
        return None
    return (min(max(x + width * SAFE_CLICK_LEFT_FRACTION, left), right), (top + bottom) / 2.0)


def _coordinate_mapping_candidates(
    *,
    raw_target_point: tuple[float, float] | None,
    local_scope: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
    cursor_point: tuple[float, float],
    ax_window_frame: tuple[float, float, float, float] | None,
    windowserver_frame: tuple[float, float, float, float] | None,
    display_frame: tuple[float, float, float, float] | None,
    hit_test_relationship: str,
) -> list[dict]:
    if raw_target_point is None:
        return []
    title_frame = _frame_tuple((snapshots_by_path.get(str(local_scope.get("title_path") or "")) or AXElementSnapshot("", 0)).frame)
    row_frame = _frame_tuple((snapshots_by_path.get(str(local_scope.get("row_path") or "")) or AXElementSnapshot("", 0)).frame)
    candidates = [
        _mapping_candidate(
            "raw_ax_interpretation",
            "ax_frames_are_global",
            raw_target_point,
            cursor_point,
            actual_window_frame=windowserver_frame,
            origin=(0.0, 0.0),
            title_frame=title_frame,
            row_frame=row_frame,
            hit_test_relationship=hit_test_relationship,
        )
    ]
    if ax_window_frame is not None:
        candidates.append(
            _mapping_candidate(
                "translation_by_chatgpt_ax_window_origin",
                "target_frame_needs_chatgpt_window_translation",
                _translate_point(raw_target_point, _frame_origin(ax_window_frame)),
                cursor_point,
                actual_window_frame=windowserver_frame,
                origin=_frame_origin(ax_window_frame),
                title_frame=title_frame,
                row_frame=row_frame,
                hit_test_relationship=hit_test_relationship,
            )
        )
    if windowserver_frame is not None:
        candidates.append(
            _mapping_candidate(
                "translation_by_windowserver_bounds_origin",
                "target_frame_needs_windowserver_translation",
                _translate_point(raw_target_point, _frame_origin(windowserver_frame)),
                cursor_point,
                actual_window_frame=windowserver_frame,
                origin=_frame_origin(windowserver_frame),
                title_frame=title_frame,
                row_frame=row_frame,
                hit_test_relationship=hit_test_relationship,
            )
        )
    for ancestor in _plausible_local_coordinate_ancestors(local_scope, snapshots_by_path):
        frame = _frame_tuple(ancestor.frame)
        if frame is None:
            continue
        candidates.append(
            _mapping_candidate(
                f"translation_by_ancestor_origin:{ancestor.path}",
                "target_frame_needs_ancestor_translation",
                _translate_point(raw_target_point, _frame_origin(frame)),
                cursor_point,
                actual_window_frame=windowserver_frame,
                origin=_frame_origin(frame),
                title_frame=title_frame,
                row_frame=row_frame,
                hit_test_relationship=hit_test_relationship,
                ancestor_path=ancestor.path,
            )
        )
    if display_frame is not None:
        inverted = []
        for candidate in candidates:
            point = _xy_point((candidate.get("candidate_point") or {}).get("x"), (candidate.get("candidate_point") or {}).get("y"))
            inverted_point = _vertically_inverted_point(point, display_frame)
            if inverted_point is None or not _point_inside_frame(inverted_point, display_frame):
                continue
            inverted.append(
                _mapping_candidate(
                    f"vertical_inversion_of:{candidate['mapping_name']}",
                    "vertical_inversion_suspected",
                    inverted_point,
                    cursor_point,
                    actual_window_frame=windowserver_frame,
                    origin=(0.0, 0.0),
                    title_frame=_vertically_inverted_frame(title_frame, display_frame),
                    row_frame=_vertically_inverted_frame(row_frame, display_frame),
                    hit_test_relationship=hit_test_relationship,
                )
            )
        candidates.extend(inverted)
    return candidates


def _mapping_candidate(
    mapping_name: str,
    classification: str,
    candidate_point: tuple[float, float],
    cursor_point: tuple[float, float],
    *,
    actual_window_frame: tuple[float, float, float, float] | None,
    origin: tuple[float, float],
    title_frame: tuple[float, float, float, float] | None,
    row_frame: tuple[float, float, float, float] | None,
    hit_test_relationship: str,
    ancestor_path: str = "",
) -> dict:
    translated_title = _translate_frame(title_frame, origin)
    translated_row = _translate_frame(row_frame, origin)
    distance = _point_distance(candidate_point, cursor_point)
    relationship_ok = _hit_test_relationship_accepts_click(hit_test_relationship)
    return {
        "mapping_name": mapping_name,
        "classification_if_unique": classification,
        "ancestor_ax_path": _bounded_text(ancestor_path, MAX_PATH_LENGTH),
        "candidate_point": _xy_report(candidate_point),
        "distance_from_cursor_px": round(distance, 2),
        "inside_actual_visible_chatgpt_window_bounds": _point_inside_frame(candidate_point, actual_window_frame),
        "inside_target_hit_test_relationship": relationship_ok,
        "inside_target_title_frame_under_interpretation": _point_inside_frame(candidate_point, translated_title),
        "inside_target_row_frame_under_interpretation": _point_inside_frame(candidate_point, translated_row),
        "candidate_explains_cursor_within_tolerance": distance <= COORDINATE_MAPPING_TOLERANCE_PX,
    }


def _plausible_local_coordinate_ancestors(
    scope: dict,
    snapshots_by_path: dict[str, AXElementSnapshot],
) -> list[AXElementSnapshot]:
    title_path = str(scope.get("title_path") or "")
    row_frame = _frame_tuple((snapshots_by_path.get(str(scope.get("row_path") or "")) or AXElementSnapshot("", 0)).frame)
    result = []
    seen: set[str] = set()
    for ancestor_path in _ancestor_paths(title_path):
        snapshot = snapshots_by_path.get(ancestor_path)
        if snapshot is None or ancestor_path in seen:
            continue
        seen.add(ancestor_path)
        frame = _frame_tuple(snapshot.frame)
        if frame is None or not _frame_is_valid(frame):
            continue
        if ancestor_path == "W" or (abs(frame[0]) <= COORDINATE_MAPPING_TOLERANCE_PX and abs(frame[1]) <= COORDINATE_MAPPING_TOLERANCE_PX):
            if ancestor_path == "W":
                break
            continue
        if row_frame is not None and not _frame_contains_with_tolerance(frame, row_frame, FRAME_CONTAINMENT_TOLERANCE):
            continue
        if snapshot.role in CONTAINER_ROLES or _is_listlike(snapshot):
            result.append(snapshot)
        if ancestor_path == "W":
            break
    return result


def _classify_coordinate_mapping(
    candidates: list[dict],
    *,
    hit_test_relationship: str,
    cursor_point: tuple[float, float],
    target_frame_available: bool,
    window_frame_available: bool,
) -> str:
    del cursor_point
    if not target_frame_available or not window_frame_available:
        return "target_or_window_frame_unavailable"
    if not _hit_test_relationship_accepts_click(hit_test_relationship):
        return "cursor_not_over_requested_target"
    winners = [
        candidate
        for candidate in candidates
        if candidate.get("candidate_explains_cursor_within_tolerance")
        and candidate.get("inside_target_hit_test_relationship")
        and (
            candidate.get("inside_target_row_frame_under_interpretation")
            or candidate.get("inside_target_title_frame_under_interpretation")
        )
    ]
    if not winners:
        return "target_hit_test_matches_but_mapping_unresolved"
    best_distance = min(float(candidate.get("distance_from_cursor_px") or 0.0) for candidate in winners)
    best = [
        candidate
        for candidate in winners
        if abs(float(candidate.get("distance_from_cursor_px") or 0.0) - best_distance) <= COORDINATE_MAPPING_TOLERANCE_PX
    ]
    if _candidate_points_equivalent(best):
        return _preferred_equivalent_candidate_class(best)
    classes = {str(candidate.get("classification_if_unique") or "") for candidate in best}
    if len(classes) != 1:
        return "ambiguous_coordinate_mapping"
    classification = next(iter(classes))
    if classification not in COORDINATE_MAPPING_CLASSIFICATIONS:
        return "ambiguous_coordinate_mapping"
    return classification


def _select_calibration_click_candidate(candidates: list[dict], classification: str) -> dict:
    eligible = [
        candidate
        for candidate in candidates
        if _xy_point((candidate.get("candidate_point") or {}).get("x"), (candidate.get("candidate_point") or {}).get("y")) is not None
        and candidate.get("inside_actual_visible_chatgpt_window_bounds")
        and candidate.get("inside_target_hit_test_relationship")
        and (
            candidate.get("inside_target_title_frame_under_interpretation")
            or candidate.get("inside_target_row_frame_under_interpretation")
        )
    ]
    if not eligible:
        return {"ok": False, "error": "No candidate point was inside the target interpretation and visible ChatGPT window."}
    preferred = [
        candidate
        for candidate in eligible
        if candidate.get("classification_if_unique") == classification
    ]
    pool = preferred or eligible
    pool = sorted(
        pool,
        key=lambda candidate: (
            0 if candidate.get("inside_target_title_frame_under_interpretation") else 1,
            float(candidate.get("distance_from_cursor_px") or 10_000_000.0),
            _candidate_transform_precedence(str(candidate.get("classification_if_unique") or "")),
            str(candidate.get("mapping_name") or ""),
        ),
    )
    chosen = pool[0]
    point = _xy_point((chosen.get("candidate_point") or {}).get("x"), (chosen.get("candidate_point") or {}).get("y"))
    transform = CLICK_TRANSFORMS.get(str(chosen.get("classification_if_unique") or ""), "unresolved")
    if point is None or transform == "unresolved":
        return {"ok": False, "error": "The best candidate did not produce a usable runtime transform."}
    return {
        "ok": True,
        "point": point,
        "candidate": chosen,
        "runtime_transform": transform,
    }


def _candidate_transform_precedence(classification: str) -> int:
    order = [
        "ax_frames_are_global",
        "target_frame_needs_chatgpt_window_translation",
        "target_frame_needs_windowserver_translation",
        "target_frame_needs_ancestor_translation",
    ]
    return order.index(classification) if classification in order else len(order)


def _calibration_click_failure(click_result: dict) -> dict:
    return {
        "ok": False,
        "status": "click_posting_failed",
        "final_click_classification": "click_posting_failed",
        "recommended_runtime_click_transform": "unresolved",
        "error": click_result.get("error") or "CoreGraphics click could not be posted.",
    }


def _post_click_destination_evidence(
    snapshots: list[AXElementSnapshot],
    classified: dict,
    kind: str,
    requested_title: str,
) -> dict:
    matches = _matching_visible_destination_candidates(classified, kind, requested_title)
    snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    active_evidence: list[dict] = []
    for candidate in matches:
        title_path = str(candidate.get("path") or "")
        container = candidate.get("nearest_list_container") or {}
        list_path = str(container.get("path") or "")
        row_path = _row_path_under_container(title_path, list_path) if list_path else title_path
        for relation, path in (("requested_title", title_path), ("requested_row", row_path)):
            snapshot = snapshots_by_path.get(path)
            if snapshot is None:
                continue
            if snapshot.selected is True or snapshot.focused is True:
                active_evidence.append(
                    {
                        "type": f"{relation}_selected_or_focused",
                        "path": path,
                        "selected": snapshot.selected,
                        "focused": snapshot.focused,
                    }
                )
        if _candidate_or_target_focused(candidate):
            active_evidence.append(
                {
                    "type": "candidate_or_action_target_focused",
                    "path": title_path,
                }
            )
    confirmed = bool(active_evidence)
    return {
        "requested_title_visible": bool(matches),
        "requested_title_match_count": len(matches),
        "active_destination_confirmed": confirmed,
        "evidence": active_evidence,
        "reason": "" if confirmed else "The requested destination was not observed selected, focused, or active after the calculated clicks.",
    }


def _confirmed_calibration_final_classification(pre_click_mapping_classification: str, post_evidence: dict) -> str:
    if bool(post_evidence.get("active_destination_confirmed")):
        return "click_confirmed_mapping_success"
    if pre_click_mapping_classification == "ambiguous_coordinate_mapping":
        return "click_posted_but_mapping_remains_ambiguous"
    return "click_posted_but_destination_not_confirmed"


def _candidate_points_equivalent(candidates: list[dict]) -> bool:
    points = [
        _xy_point((candidate.get("candidate_point") or {}).get("x"), (candidate.get("candidate_point") or {}).get("y"))
        for candidate in candidates
    ]
    points = [point for point in points if point is not None]
    if len(points) <= 1:
        return True
    first = points[0]
    return all(_point_distance(first, point) <= 0.01 for point in points[1:])


def _preferred_equivalent_candidate_class(candidates: list[dict]) -> str:
    precedence = [
        "ax_frames_are_global",
        "target_frame_needs_chatgpt_window_translation",
        "target_frame_needs_windowserver_translation",
        "target_frame_needs_ancestor_translation",
        "vertical_inversion_suspected",
    ]
    classes = {str(candidate.get("classification_if_unique") or "") for candidate in candidates}
    for classification in precedence:
        if classification in classes:
            return classification
    return "ambiguous_coordinate_mapping"


def _hit_test_relationship_accepts_click(relationship: str) -> bool:
    return relationship in {"exact_target_title", "descendant_of_target_row", "ancestor_of_target_title"}


def _translate_point(point: tuple[float, float], origin: tuple[float, float]) -> tuple[float, float]:
    return (point[0] + origin[0], point[1] + origin[1])


def _translate_frame(
    frame: tuple[float, float, float, float] | None,
    origin: tuple[float, float],
) -> tuple[float, float, float, float] | None:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return None
    x, y, width, height = normalized
    return (x + origin[0], y + origin[1], width, height)


def _frame_origin(frame: tuple[float, float, float, float]) -> tuple[float, float]:
    return (float(frame[0]), float(frame[1]))


def _point_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _frame_area(frame: tuple[float, float, float, float] | None) -> float:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return 0.0
    return max(0.0, normalized[2]) * max(0.0, normalized[3])


def _vertically_inverted_point(
    point: tuple[float, float] | None,
    display_frame: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    if point is None:
        return None
    _x, y, _width, height = display_frame
    return (point[0], y + height - point[1])


def _vertically_inverted_frame(
    frame: tuple[float, float, float, float] | None,
    display_frame: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    normalized = _frame_tuple(frame)
    if normalized is None:
        return None
    x, y, width, height = normalized
    _display_x, display_y, _display_width, display_height = display_frame
    return (x, display_y + display_height - (y + height), width, height)


class _CoreGraphicsDisplayProbe:
    """Read-only CoreGraphics probe for display bounds and the current mouse location.

    Reads state only: it never posts events and never warps or moves the cursor.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise AXDiagnosticError("Unsupported platform.")
        self._cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._cg.CGMainDisplayID.argtypes = []
        self._cg.CGMainDisplayID.restype = c_uint32
        self._cg.CGDisplayBounds.argtypes = [c_uint32]
        self._cg.CGDisplayBounds.restype = _CGRect
        self._cg.CGGetActiveDisplayList.argtypes = [c_uint32, POINTER(c_uint32), POINTER(c_uint32)]
        self._cg.CGGetActiveDisplayList.restype = c_int
        self._cg.CGEventCreate.argtypes = [c_void_p]
        self._cg.CGEventCreate.restype = c_void_p
        self._cg.CGEventGetLocation.argtypes = [c_void_p]
        self._cg.CGEventGetLocation.restype = _CGPoint
        self._cf.CFRelease.argtypes = [c_void_p]
        self._cf.CFRelease.restype = None

    def primary_display_bounds(self) -> tuple[float, float, float, float]:
        display_id = self._cg.CGMainDisplayID()
        rect = self._cg.CGDisplayBounds(c_uint32(display_id))
        return (float(rect.origin.x), float(rect.origin.y), float(rect.size.width), float(rect.size.height))

    def active_display_bounds(self) -> list[dict]:
        capacity = 16
        displays = (c_uint32 * capacity)()
        count = c_uint32(0)
        error = self._cg.CGGetActiveDisplayList(c_uint32(capacity), displays, byref(count))
        if error != 0:
            return []
        result = []
        for index in range(int(count.value)):
            display_id = int(displays[index])
            rect = self._cg.CGDisplayBounds(c_uint32(display_id))
            result.append(
                {
                    "display_id": display_id,
                    "bounds": (float(rect.origin.x), float(rect.origin.y), float(rect.size.width), float(rect.size.height)),
                }
            )
        return result

    def display_bounds_containing_point(self, point: tuple[float, float]) -> tuple[float, float, float, float] | None:
        for display in self.active_display_bounds():
            bounds = _frame_tuple(display.get("bounds"))
            if _point_inside_frame(point, bounds):
                return bounds
        return None

    def current_mouse_location(self) -> tuple[float, float]:
        event = self._cg.CGEventCreate(None)
        if not event:
            raise AXDiagnosticError("Could not read the current CoreGraphics mouse location.")
        try:
            location = self._cg.CGEventGetLocation(c_void_p(event))
            return (float(location.x), float(location.y))
        finally:
            self._cf.CFRelease(c_void_p(event))


class _CoreGraphicsFrameClickService:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise AXDiagnosticError("Unsupported platform.")
        self._cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._cg.CGPreflightPostEventAccess.argtypes = []
        self._cg.CGPreflightPostEventAccess.restype = c_bool
        self._cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_int, _CGPoint, c_int]
        self._cg.CGEventCreateMouseEvent.restype = c_void_p
        self._cg.CGEventPost.argtypes = [c_int, c_void_p]
        self._cg.CGEventPost.restype = None
        self._cf.CFRelease.argtypes = [c_void_p]
        self._cf.CFRelease.restype = None

    def has_permission(self) -> bool:
        return bool(self._cg.CGPreflightPostEventAccess())

    def left_click(self, x: float, y: float) -> dict:
        if not self.has_permission():
            return {"ok": False, "error": "CoreGraphics post-event permission is unavailable.", "actions_performed": []}
        point = _CGPoint(float(x), float(y))
        down = self._cg.CGEventCreateMouseEvent(None, c_int(1), point, c_int(0))
        up = self._cg.CGEventCreateMouseEvent(None, c_int(2), point, c_int(0))
        if not down or not up:
            for event in (down, up):
                if event:
                    self._cf.CFRelease(c_void_p(event))
            return {"ok": False, "error": "Could not create CoreGraphics mouse events.", "actions_performed": []}
        try:
            self._cg.CGEventPost(c_int(0), c_void_p(down))
            self._cg.CGEventPost(c_int(0), c_void_p(up))
        finally:
            self._cf.CFRelease(c_void_p(down))
            self._cf.CFRelease(c_void_p(up))
        return {
            "ok": True,
            "error": "",
            "actions_performed": [
                {"event": "left_mouse_down", "x": round(float(x), 2), "y": round(float(y), 2)},
                {"event": "left_mouse_up", "x": round(float(x), 2), "y": round(float(y), 2)},
            ],
        }


class _CoreGraphicsScrollService:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise AXDiagnosticError("Unsupported platform.")
        self._cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._cg.CGPreflightPostEventAccess.argtypes = []
        self._cg.CGPreflightPostEventAccess.restype = c_bool
        self._cg.CGEventCreateScrollWheelEvent.restype = c_void_p
        self._cg.CGEventSetLocation.argtypes = [c_void_p, _CGPoint]
        self._cg.CGEventSetLocation.restype = None
        self._cg.CGEventPost.argtypes = [c_int, c_void_p]
        self._cg.CGEventPost.restype = None
        self._cf.CFRelease.argtypes = [c_void_p]
        self._cf.CFRelease.restype = None

    def has_permission(self) -> bool:
        return bool(self._cg.CGPreflightPostEventAccess())

    def scroll_down(self, x: float, y: float, delta_y: int) -> dict:
        if not self.has_permission():
            return {"ok": False, "error": "CoreGraphics post-event permission is unavailable.", "actions_performed": []}
        point = _CGPoint(float(x), float(y))
        event = self._cg.CGEventCreateScrollWheelEvent(None, c_int(0), c_uint32(1), c_int(int(delta_y)))
        if not event:
            return {"ok": False, "error": "Could not create CoreGraphics scroll event.", "actions_performed": []}
        try:
            self._cg.CGEventSetLocation(c_void_p(event), point)
            self._cg.CGEventPost(c_int(0), c_void_p(event))
        finally:
            self._cf.CFRelease(c_void_p(event))
        return {
            "ok": True,
            "error": "",
            "actions_performed": [
                {
                    "event": "scroll_wheel",
                    "x": round(float(x), 2),
                    "y": round(float(y), 2),
                    "delta_y": int(delta_y),
                }
            ],
        }


class _WindowServerBoundsProbe:
    """Read-only WindowServer geometry probe for visible windows owned by a PID."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise AXDiagnosticError("Unsupported platform.")
        self._cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._configure_signatures()
        self._keys: dict[str, int] = {}

    def _configure_signatures(self) -> None:
        self._cg.CGWindowListCopyWindowInfo.argtypes = [c_uint32, c_uint32]
        self._cg.CGWindowListCopyWindowInfo.restype = c_void_p
        self._cf.CFArrayGetCount.argtypes = [c_void_p]
        self._cf.CFArrayGetCount.restype = c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        self._cf.CFArrayGetValueAtIndex.restype = c_void_p
        self._cf.CFDictionaryGetValueIfPresent.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        self._cf.CFDictionaryGetValueIfPresent.restype = c_bool
        self._cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
        self._cf.CFNumberGetValue.restype = c_bool
        self._cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_ulong]
        self._cf.CFStringCreateWithCString.restype = c_void_p
        self._cf.CFRelease.argtypes = [c_void_p]
        self._cf.CFRelease.restype = None

    def visible_windows_for_pid(self, pid: int) -> list[dict]:
        windows = self._cg.CGWindowListCopyWindowInfo(c_uint32(1), c_uint32(0))
        if not windows:
            return []
        try:
            result = []
            for index in range(self._cf.CFArrayGetCount(c_void_p(windows))):
                window = self._cf.CFArrayGetValueAtIndex(c_void_p(windows), index)
                if not window:
                    continue
                owner_pid = self._dict_int(window, "kCGWindowOwnerPID")
                if owner_pid != pid:
                    continue
                bounds = self._dict_bounds(window)
                if bounds is None or not _frame_is_valid(bounds):
                    continue
                onscreen = self._dict_int(window, "kCGWindowIsOnscreen")
                layer = self._dict_int(window, "kCGWindowLayer")
                if onscreen == 0 or layer not in {None, 0}:
                    continue
                result.append(
                    {
                        "window_id": self._dict_int(window, "kCGWindowNumber"),
                        "owner_pid": owner_pid,
                        "layer": layer,
                        "onscreen": bool(onscreen),
                        "bounds": bounds,
                    }
                )
            return result
        finally:
            self._cf.CFRelease(c_void_p(windows))

    def _dict_value(self, dictionary: int, key_name: str) -> int | None:
        output = c_void_p()
        key = self._key(key_name)
        if not key:
            return None
        ok = self._cf.CFDictionaryGetValueIfPresent(c_void_p(dictionary), c_void_p(key), byref(output))
        return output.value if ok and output.value else None

    def _dict_int(self, dictionary: int, key_name: str) -> int | None:
        value = self._dict_value(dictionary, key_name)
        if not value:
            return None
        output = c_int(0)
        ok = self._cf.CFNumberGetValue(c_void_p(value), c_int(9), byref(output))
        return int(output.value) if ok else None

    def _dict_float(self, dictionary: int, key_name: str) -> float | None:
        value = self._dict_value(dictionary, key_name)
        if not value:
            return None
        output = c_double(0)
        ok = self._cf.CFNumberGetValue(c_void_p(value), c_int(13), byref(output))
        return float(output.value) if ok else None

    def _dict_bounds(self, dictionary: int) -> tuple[float, float, float, float] | None:
        bounds = self._dict_value(dictionary, "kCGWindowBounds")
        if not bounds:
            return None
        x = self._dict_float(bounds, "X")
        y = self._dict_float(bounds, "Y")
        width = self._dict_float(bounds, "Width")
        height = self._dict_float(bounds, "Height")
        if None in {x, y, width, height}:
            return None
        return (float(x), float(y), float(width), float(height))

    def _key(self, name: str) -> int:
        if name not in self._keys:
            try:
                self._keys[name] = c_void_p.in_dll(self._cg, name).value
            except ValueError:
                self._keys[name] = self._cf.CFStringCreateWithCString(None, name.encode("utf-8"), 0x08000100)
        return self._keys[name]


def _resolve_process_with_nsworkspace(app_name: str) -> ProcessResolution:
    try:
        runtime = _ObjCRuntime()
        pid = runtime.find_running_application_pid(app_name)
    except Exception as exc:
        return ProcessResolution(pid=None, method=PROCESS_RESOLUTION_METHOD, error=str(exc))
    if pid is None:
        return ProcessResolution(
            pid=None,
            method=PROCESS_RESOLUTION_METHOD,
            error=f"No running application named {app_name!r} was found through NSWorkspace.",
        )
    return ProcessResolution(pid=pid, method=PROCESS_RESOLUTION_METHOD)


class _ObjCRuntime:
    def __init__(self) -> None:
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        self._objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        self._objc.objc_getClass.argtypes = [c_char_p]
        self._objc.objc_getClass.restype = c_void_p
        self._objc.sel_registerName.argtypes = [c_char_p]
        self._objc.sel_registerName.restype = c_void_p

    def find_running_application_pid(self, app_name: str) -> int | None:
        pool = self._send_id(self._class("NSAutoreleasePool"), "new")
        try:
            workspace = self._send_id(self._class("NSWorkspace"), "sharedWorkspace")
            applications = self._send_id(workspace, "runningApplications")
            count = self._send_ulong(applications, "count")
            matches: list[tuple[int, int]] = []
            for index in range(count):
                application = self._send_id_index(applications, "objectAtIndex:", index)
                localized_name = self._nsstring_to_str(self._send_id(application, "localizedName"))
                bundle_id = self._nsstring_to_str(self._send_id(application, "bundleIdentifier"))
                pid = self._send_int(application, "processIdentifier")
                if pid <= 0:
                    continue
                score = self._match_score(app_name, localized_name, bundle_id)
                if score:
                    matches.append((score, pid))
            if not matches:
                return None
            matches.sort(reverse=True)
            return matches[0][1]
        finally:
            if pool:
                self._send_void(pool, "drain")

    def launch_application(self, app_name: str) -> bool:
        pool = self._send_id(self._class("NSAutoreleasePool"), "new")
        try:
            workspace = self._send_id(self._class("NSWorkspace"), "sharedWorkspace")
            return self._send_bool_id(workspace, "launchApplication:", self._nsstring(app_name))
        finally:
            if pool:
                self._send_void(pool, "drain")

    def activate_application_pid(self, pid: int) -> bool:
        pool = self._send_id(self._class("NSAutoreleasePool"), "new")
        try:
            application = self._send_id_int(self._class("NSRunningApplication"), "runningApplicationWithProcessIdentifier:", pid)
            if not application:
                return False
            return self._send_bool_ulong(application, "activateWithOptions:", 3)
        finally:
            if pool:
                self._send_void(pool, "drain")

    def _match_score(self, app_name: str, localized_name: str, bundle_id: str) -> int:
        expected = app_name.casefold()
        name = localized_name.casefold()
        bundle = bundle_id.casefold()
        # Both the Classic desktop client and the separate Work/Codex client
        # currently advertise themselves as "ChatGPT" in NSWorkspace.  The
        # navigation and destination verifier must always address Classic; a
        # display-name match would otherwise choose whichever process has the
        # larger PID.  Refuse every non-Classic bundle rather than falling back
        # to the Work/Codex client.
        if expected == "chatgpt":
            return 200 if bundle in CLASSIC_CHATGPT_BUNDLE_IDS else 0
        if name == expected:
            return 100
        if expected == "calculator" and bundle == "com.apple.calculator":
            return 90
        return 0

    def _class(self, name: str) -> int:
        value = self._objc.objc_getClass(name.encode("utf-8"))
        if not value:
            raise AXDiagnosticError(f"Objective-C class not found: {name}")
        return value

    def _selector(self, name: str) -> int:
        return self._objc.sel_registerName(name.encode("utf-8"))

    def _send_id(self, receiver: int, selector: str) -> int:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p]
        self._objc.objc_msgSend.restype = c_void_p
        return self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector))) or 0

    def _send_id_index(self, receiver: int, selector: str, index: int) -> int:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p, c_ulong]
        self._objc.objc_msgSend.restype = c_void_p
        return self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector)), c_ulong(index)) or 0

    def _send_id_int(self, receiver: int, selector: str, value: int) -> int:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p, c_int]
        self._objc.objc_msgSend.restype = c_void_p
        return self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector)), c_int(value)) or 0

    def _send_ulong(self, receiver: int, selector: str) -> int:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p]
        self._objc.objc_msgSend.restype = c_ulong
        return int(self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector))))

    def _send_int(self, receiver: int, selector: str) -> int:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p]
        self._objc.objc_msgSend.restype = c_int
        return int(self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector))))

    def _send_bool_id(self, receiver: int, selector: str, value: int) -> bool:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p, c_void_p]
        self._objc.objc_msgSend.restype = c_bool
        return bool(self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector)), c_void_p(value)))

    def _send_bool_ulong(self, receiver: int, selector: str, value: int) -> bool:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p, c_ulong]
        self._objc.objc_msgSend.restype = c_bool
        return bool(self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector)), c_ulong(value)))

    def _send_void(self, receiver: int, selector: str) -> None:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p]
        self._objc.objc_msgSend.restype = None
        self._objc.objc_msgSend(c_void_p(receiver), c_void_p(self._selector(selector)))

    def _nsstring(self, text: str) -> int:
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p, c_char_p]
        self._objc.objc_msgSend.restype = c_void_p
        return self._objc.objc_msgSend(
            c_void_p(self._class("NSString")),
            c_void_p(self._selector("stringWithUTF8String:")),
            text.encode("utf-8"),
        ) or 0

    def _nsstring_to_str(self, value: int) -> str:
        if not value:
            return ""
        self._objc.objc_msgSend.argtypes = [c_void_p, c_void_p]
        self._objc.objc_msgSend.restype = c_char_p
        raw = self._objc.objc_msgSend(c_void_p(value), c_void_p(self._selector("UTF8String")))
        if not raw:
            return ""
        return raw.decode("utf-8", "replace")


# --- Calculator synthetic-click delivery probe (manual diagnostic only) --------

CALCULATOR_APP_NAME = "Calculator"
CALCULATOR_DIGIT_BUTTON_TITLE = "7"
CALCULATOR_LAUNCH_TIMEOUT_SECONDS = 5.0
CALCULATOR_ACTIVATION_SETTLE_SECONDS = 0.5
CALCULATOR_PROBE_SETTLE_SECONDS = 0.6
CALCULATOR_PROBE_MAX_SETTLE_SECONDS = 1.0
# Static description of the existing shared click path; reported but not altered.
SHARED_CLICK_EVENT_SOURCE_TYPE = "cg_null_default_event_source"
SHARED_CLICK_POSTING_TARGET = "kCGHIDEventTap"
SYNTHETIC_CLICK_OUTCOME_STATUSES = {
    "dry_run_ready",
    "synthetic_click_delivered",
    "synthetic_click_posted_no_probe_reaction",
    "calculator_not_available",
    "calculator_window_not_ready",
    "calculator_digit_target_ambiguous",
    "calculator_display_unreadable",
    "calculator_frame_invalid",
    "permission_or_event_source_unavailable",
    "calculator_accessibility_failure",
}
CURRENT_CURSOR_CLICK_OUTCOME_STATUSES = {
    "dry_run_ready",
    "synthetic_two_clicks_posted",
    "permission_or_event_source_unavailable",
    "cursor_location_unavailable",
    "click_posting_failed",
}
CURRENT_CURSOR_CONFIRMED_CLICK_COUNT = 2
CURRENT_CURSOR_INTER_CLICK_DELAY_SECONDS = 0.5


def verify_current_cursor_click(
    *,
    confirm_current_cursor_click: bool = False,
    display_probe_factory: object | None = None,
    click_service_factory: object | None = None,
    before_click_callback: object | None = None,
) -> dict:
    result = _base_current_cursor_click_result(confirm_current_cursor_click)
    if sys.platform != "darwin":
        result.update({"status": "cursor_location_unavailable", "error": "Current-cursor click probe is only supported on macOS."})
        return result

    try:
        display_probe = (display_probe_factory or _CoreGraphicsDisplayProbe)()
    except Exception as exc:
        result.update({"status": "cursor_location_unavailable", "error": str(exc)})
        return result

    try:
        clicker = (click_service_factory or _CoreGraphicsFrameClickService)()
        permission = bool(clicker.has_permission())
        result["post_event_permission_available"] = permission
        result["permission_preflight_state"] = {"available": permission, "error": ""}
    except Exception as exc:
        result["permission_preflight_state"] = {"available": False, "error": str(exc)}
        if confirm_current_cursor_click:
            result.update({"status": "permission_or_event_source_unavailable", "error": str(exc)})
            return result
        clicker = None

    if not confirm_current_cursor_click:
        try:
            result["current_cursor_location"] = _xy_report(display_probe.current_mouse_location())
        except Exception as exc:
            result.update({"status": "cursor_location_unavailable", "error": str(exc)})
            return result
        result.update({"status": "dry_run_ready", "ok": True})
        return result

    if not result["permission_preflight_state"].get("available"):
        result.update({"status": "permission_or_event_source_unavailable", "error": "CoreGraphics post-event permission is unavailable."})
        return result
    if clicker is None:
        result.update({"status": "permission_or_event_source_unavailable", "error": "CoreGraphics click service is unavailable."})
        return result

    if before_click_callback is not None:
        before_click_callback()

    try:
        cursor_location = display_probe.current_mouse_location()
    except Exception as exc:
        result.update({"status": "cursor_location_unavailable", "error": str(exc)})
        return result

    result["current_cursor_location"] = _xy_report(cursor_location)
    try:
        first_click_result = clicker.left_click(cursor_location[0], cursor_location[1])
        if not first_click_result.get("ok"):
            _apply_current_cursor_click_failure(result, first_click_result)
            return result
        result["actions_performed"].extend(first_click_result.get("actions_performed") or [])
        time.sleep(CURRENT_CURSOR_INTER_CLICK_DELAY_SECONDS)
        second_click_result = clicker.left_click(cursor_location[0], cursor_location[1])
    except PermissionError as exc:
        result.update({"status": "permission_or_event_source_unavailable", "error": str(exc)})
        return result
    except Exception as exc:
        result.update({"status": "click_posting_failed", "error": str(exc)})
        return result

    if not second_click_result.get("ok"):
        result["actions_performed"].extend(second_click_result.get("actions_performed") or [])
        _apply_current_cursor_click_failure(result, second_click_result)
        return result
    result["actions_performed"].extend(second_click_result.get("actions_performed") or [])
    if not _two_click_actions_match_point(result["actions_performed"], result["current_cursor_location"]):
        result.update({"status": "click_posting_failed", "error": "Posted actions did not match the final cursor location and two-click sequence."})
        return result

    result.update({"status": "synthetic_two_clicks_posted", "ok": True})
    return result


def _base_current_cursor_click_result(confirm_current_cursor_click: bool) -> dict:
    return {
        "ok": False,
        "status": "not_run",
        "confirm_current_cursor_click": bool(confirm_current_cursor_click),
        "timestamp": datetime_now_iso(),
        "current_cursor_location": _xy_report(None),
        "click_count": CURRENT_CURSOR_CONFIRMED_CLICK_COUNT,
        "inter_click_delay_ms": int(CURRENT_CURSOR_INTER_CLICK_DELAY_SECONDS * 1000),
        "event_source_type": SHARED_CLICK_EVENT_SOURCE_TYPE,
        "event_posting_target": SHARED_CLICK_POSTING_TARGET,
        "post_event_permission_available": None,
        "permission_preflight_state": {"available": None, "error": ""},
        "actions_performed": [],
        "error": "",
    }


def _apply_current_cursor_click_failure(result: dict, click_result: dict) -> None:
    error = click_result.get("error") or "CoreGraphics click could not be posted."
    status = "permission_or_event_source_unavailable" if "permission" in error.casefold() else "click_posting_failed"
    result.update({"status": status, "error": error})


def _two_click_actions_match_point(actions: list[dict], click_point: dict) -> bool:
    if len(actions) != 4:
        return False
    return _click_actions_match_point(actions[:2], click_point) and _click_actions_match_point(actions[2:], click_point)


def verify_synthetic_click_delivery(
    *,
    confirm_synthetic_click_probe: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    calculator_controller_factory: object | None = None,
    reader_factory: object | None = None,
    click_service_factory: object | None = None,
    settle_seconds: float = CALCULATOR_PROBE_SETTLE_SECONDS,
    before_click_callback: object | None = None,
) -> dict:
    result = _base_synthetic_click_result(confirm_synthetic_click_probe)
    if max_depth < 0 or max_nodes <= 0:
        result.update({"status": "calculator_accessibility_failure", "error": "max_depth must be >= 0 and max_nodes must be > 0."})
        return result
    if sys.platform != "darwin":
        result.update({"status": "calculator_not_available", "error": "Calculator synthetic-click delivery probe is only supported on macOS."})
        return result

    try:
        controller = (calculator_controller_factory or _CalculatorAppController)()
        process = controller.resolve_running()
        result["process_resolution_method"] = process.method
        result["pid_present"] = process.pid is not None
        pid = process.pid
        if pid is None:
            result["calculator_launched_by_probe"] = True
            if not controller.launch():
                result.update({"status": "calculator_not_available", "error": "Calculator could not be launched through NSWorkspace."})
                return result
            pid = controller.wait_for_running_pid(CALCULATOR_LAUNCH_TIMEOUT_SECONDS)
            result["pid_present"] = pid is not None
        if pid is None:
            result.update({"status": "calculator_not_available", "error": "Calculator was not available through NSWorkspace."})
            return result
        controller.activate(pid)
        if CALCULATOR_ACTIVATION_SETTLE_SECONDS > 0:
            time.sleep(CALCULATOR_ACTIVATION_SETTLE_SECONDS)
    except Exception as exc:
        result.update({"status": "calculator_not_available", "error": str(exc)})
        return result

    try:
        reader = (reader_factory or _DetailedReadOnlyAXReader)(CALCULATOR_APP_NAME, max_depth, max_nodes)
        pre_plan = _calculator_click_plan(reader, pid)
    except Exception as exc:
        result.update({"status": "calculator_accessibility_failure", "error": str(exc), "pid_present": True})
        return result

    _apply_calculator_plan_to_result(result, pre_plan)
    if pre_plan["status"] != "calculator_click_ready":
        result.update({"status": pre_plan["status"], "error": pre_plan.get("error", "")})
        return result

    try:
        clicker = (click_service_factory or _CoreGraphicsFrameClickService)()
        permission = bool(clicker.has_permission())
    except Exception as exc:
        result.update(
            {
                "status": "permission_or_event_source_unavailable",
                "permission_preflight_state": {"available": False, "error": str(exc)},
                "error": str(exc),
            }
        )
        return result

    result["event_source_type"] = SHARED_CLICK_EVENT_SOURCE_TYPE
    result["event_posting_target"] = SHARED_CLICK_POSTING_TARGET
    result["post_event_permission_available"] = permission
    result["permission_preflight_state"] = {"available": permission, "error": ""}
    result["readiness_checks_passed"] = True

    if not confirm_synthetic_click_probe:
        result.update({"status": "dry_run_ready", "ok": True})
        return result
    if not permission:
        result.update({"status": "permission_or_event_source_unavailable", "error": "CoreGraphics post-event permission is unavailable."})
        return result

    try:
        fresh_plan = _calculator_click_plan(reader, pid)
    except Exception as exc:
        result.update({"status": "calculator_accessibility_failure", "ok": False, "error": str(exc)})
        return result
    _apply_calculator_plan_to_result(result, fresh_plan)
    if fresh_plan["status"] != "calculator_click_ready":
        result.update({"status": fresh_plan["status"], "ok": False, "error": fresh_plan.get("error", "")})
        return result

    click_point = fresh_plan["click_point"]
    if before_click_callback is not None:
        before_click_callback()

    try:
        click_result = clicker.left_click(click_point["x"], click_point["y"])
    except PermissionError as exc:
        result.update({"status": "permission_or_event_source_unavailable", "error": str(exc)})
        return result
    except Exception as exc:
        result.update({"status": "calculator_accessibility_failure", "error": str(exc)})
        return result

    if not click_result.get("ok"):
        result.update(
            {
                "status": "permission_or_event_source_unavailable",
                "error": click_result.get("error") or "CoreGraphics click could not be posted.",
            }
        )
        return result

    result["actions_performed"] = click_result.get("actions_performed") or []
    if not _click_actions_match_point(result["actions_performed"], click_point):
        result.update({"status": "calculator_frame_invalid", "error": "Posted actions did not match the fresh frame-derived click point."})
        return result

    if settle_seconds > 0:
        time.sleep(min(settle_seconds, CALCULATOR_PROBE_MAX_SETTLE_SECONDS))

    try:
        post_display = _calculator_display_value_after_click(reader, pid)
    except Exception as exc:
        result.update({"status": "calculator_accessibility_failure", "ok": False, "error": str(exc)})
        return result
    if post_display is None:
        result.update({"status": "calculator_display_unreadable", "ok": False, "error": "Calculator display value was unreadable after the click."})
        return result

    result["post_display_value"] = post_display
    if result.get("pre_display_value") != post_display:
        result.update({"status": "synthetic_click_delivered", "ok": True})
    else:
        result.update({"status": "synthetic_click_posted_no_probe_reaction", "ok": False})
    return result


def _base_synthetic_click_result(confirm_synthetic_click_probe: bool) -> dict:
    return {
        "ok": False,
        "status": "not_run",
        "confirm_synthetic_click_probe": bool(confirm_synthetic_click_probe),
        "timestamp": datetime_now_iso(),
        "app_name": CALCULATOR_APP_NAME,
        "calculator_window_title": "",
        "calculator_launched_by_probe": False,
        "pid_present": False,
        "process_resolution_method": PROCESS_RESOLUTION_METHOD,
        "digit_button_title": CALCULATOR_DIGIT_BUTTON_TITLE,
        "visible_digit_button_matches": 0,
        "button_frame": _frame_geometry_report(None),
        "window_frame": _frame_geometry_report(None),
        "click_point": _xy_report(None),
        "click_point_inside_button": False,
        "click_point_inside_window": False,
        "pre_display_value": None,
        "post_display_value": None,
        "event_source_type": SHARED_CLICK_EVENT_SOURCE_TYPE,
        "event_posting_target": SHARED_CLICK_POSTING_TARGET,
        "post_event_permission_available": None,
        "permission_preflight_state": {"available": None, "error": ""},
        "readiness_checks_passed": False,
        "actions_performed": [],
        "error": "",
    }


def _apply_calculator_plan_to_result(result: dict, plan: dict) -> None:
    result["calculator_window_title"] = plan.get("window_title") or ""
    result["window_frame"] = _frame_geometry_report(plan.get("window_frame"))
    result["visible_digit_button_matches"] = int(plan.get("visible_digit_button_matches") or 0)
    result["button_frame"] = _frame_geometry_report(plan.get("button_frame"))
    result["click_point"] = plan.get("click_point") or _xy_report(None)
    result["click_point_inside_button"] = bool(plan.get("click_point_inside_button"))
    result["click_point_inside_window"] = bool(plan.get("click_point_inside_window"))
    if plan.get("display_value") is not None:
        result["pre_display_value"] = plan.get("display_value")


def _calculator_click_plan(reader: object, pid: int) -> dict:
    snapshots, _stats, window_metadata = reader.collect(pid)
    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    window_title = _calculator_window_title(window_metadata, snapshots)
    base = {
        "status": "calculator_click_ready",
        "error": "",
        "window_title": window_title,
        "window_frame": window_frame,
        "visible_digit_button_matches": 0,
        "button_frame": None,
        "click_point": _xy_report(None),
        "click_point_inside_button": False,
        "click_point_inside_window": False,
        "display_value": None,
    }
    if window_frame is None or not _frame_is_valid(window_frame):
        return {**base, "status": "calculator_window_not_ready", "error": "Calculator window frame was unavailable or invalid."}

    digit_buttons = _visible_calculator_digit_buttons(snapshots, window_frame)
    base["visible_digit_button_matches"] = len(digit_buttons)
    if len(digit_buttons) != 1:
        return {
            **base,
            "status": "calculator_digit_target_ambiguous",
            "error": "Expected exactly one visible Calculator digit button titled 7.",
        }

    display_value = _calculator_display_value(snapshots, window_frame)
    if display_value is None:
        return {**base, "status": "calculator_display_unreadable", "error": "Calculator display value was unreadable."}

    button = digit_buttons[0]
    button_frame = _frame_tuple(button.frame)
    click_point_tuple = _calculator_button_click_point(button_frame)
    click_point = _xy_report(click_point_tuple)
    inside_button = _point_inside_frame(click_point_tuple, button_frame)
    inside_window = _point_inside_frame(click_point_tuple, window_frame)
    if button_frame is None or not _frame_is_valid(button_frame) or click_point_tuple is None or not inside_button or not inside_window:
        return {
            **base,
            "status": "calculator_frame_invalid",
            "error": "Calculator digit 7 frame or conservative interior click point was invalid.",
            "button_frame": button_frame,
            "click_point": click_point,
            "click_point_inside_button": inside_button,
            "click_point_inside_window": inside_window,
            "display_value": display_value,
        }

    return {
        **base,
        "button_frame": button_frame,
        "click_point": click_point,
        "click_point_inside_button": inside_button,
        "click_point_inside_window": inside_window,
        "display_value": display_value,
    }


def _calculator_display_value_after_click(reader: object, pid: int) -> str | None:
    snapshots, _stats, window_metadata = reader.collect(pid)
    window_frame = _window_frame_from_metadata(window_metadata, snapshots)
    if window_frame is None or not _frame_is_valid(window_frame):
        return None
    return _calculator_display_value(snapshots, window_frame)


def _calculator_window_title(metadata: dict, snapshots: list[AXElementSnapshot]) -> str:
    window = metadata.get("window") if isinstance(metadata, dict) else None
    if isinstance(window, dict):
        title = window.get("title")
        if isinstance(title, dict):
            literal = title.get("literal")
            if isinstance(literal, str):
                return literal
    if snapshots:
        return snapshots[0].title
    return ""


def _visible_calculator_digit_buttons(
    snapshots: list[AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
) -> list[AXElementSnapshot]:
    matches = []
    for snapshot in snapshots:
        if snapshot.role != "AXButton" or _normalized_label(snapshot.title) != CALCULATOR_DIGIT_BUTTON_TITLE:
            continue
        frame = _frame_tuple(snapshot.frame)
        if snapshot.enabled is False or frame is None or not _frame_is_valid(frame):
            continue
        if not _frame_contains_with_tolerance(window_frame, frame, FRAME_CONTAINMENT_TOLERANCE):
            continue
        matches.append(snapshot)
    return matches


def _calculator_display_value(
    snapshots: list[AXElementSnapshot],
    window_frame: tuple[float, float, float, float] | None,
) -> str | None:
    candidates: list[tuple[int, str]] = []
    for snapshot in snapshots:
        if snapshot.role == "AXButton":
            continue
        frame = _frame_tuple(snapshot.frame)
        if frame is not None and window_frame is not None and not _frame_intersects(window_frame, frame):
            continue
        label_blob = _normalized_label(" ".join((snapshot.identifier, snapshot.title, snapshot.description)))
        for source, raw_value in (("value", snapshot.value), ("title", snapshot.title), ("description", snapshot.description)):
            value = _normalized_label(raw_value)
            if not value or not _calculator_display_text_plausible(value):
                continue
            score = 0
            if any(token in label_blob for token in ("display", "result", "entry", "input")):
                score += 100
            if source == "value":
                score += 30
            if snapshot.role in TEXTLIKE_ROLES or snapshot.role == "AXStaticText":
                score += 20
            if frame is not None and _frame_is_valid(frame):
                score += 10
            if snapshot.depth <= 4:
                score += 5
            candidates.append((score, value))
    if not candidates:
        return None
    best_score = max(score for score, _value in candidates)
    best_values = sorted({value for score, value in candidates if score == best_score})
    return best_values[0] if len(best_values) == 1 else None


def _calculator_display_text_plausible(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    lower = value.casefold()
    if lower in {"error", "not a number", "nan", "infinity", "undefined"}:
        return True
    return bool(re.search(r"\d", value))


def _calculator_button_click_point(frame: tuple[float, float, float, float] | None) -> tuple[float, float] | None:
    normalized = _frame_tuple(frame)
    if normalized is None or not _frame_is_valid(normalized):
        return None
    x, y, width, height = normalized
    inset = min(8.0, max(1.0, width / 4.0), max(1.0, height / 4.0))
    left = x + inset
    right = x + width - inset
    top = y + inset
    bottom = y + height - inset
    if right <= left or bottom <= top:
        return None
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _click_actions_match_point(actions: list[dict], click_point: dict) -> bool:
    if len(actions) != 2:
        return False
    expected = ("left_mouse_down", "left_mouse_up")
    try:
        x = round(float(click_point.get("x")), 2)
        y = round(float(click_point.get("y")), 2)
    except (TypeError, ValueError):
        return False
    for action, expected_event in zip(actions, expected, strict=True):
        if action.get("event") != expected_event:
            return False
        try:
            action_x = round(float(action.get("x")), 2)
            action_y = round(float(action.get("y")), 2)
        except (TypeError, ValueError):
            return False
        if action_x != x or action_y != y:
            return False
    return True


class _CalculatorAppController:
    def __init__(self) -> None:
        self._runtime = _ObjCRuntime()

    def resolve_running(self) -> ProcessResolution:
        try:
            pid = self._runtime.find_running_application_pid(CALCULATOR_APP_NAME)
        except Exception as exc:
            return ProcessResolution(pid=None, method=PROCESS_RESOLUTION_METHOD, error=str(exc))
        if pid is None:
            return ProcessResolution(
                pid=None,
                method=PROCESS_RESOLUTION_METHOD,
                error=f"No running application named {CALCULATOR_APP_NAME!r} was found through NSWorkspace.",
            )
        return ProcessResolution(pid=pid, method=PROCESS_RESOLUTION_METHOD)

    def launch(self) -> bool:
        return self._runtime.launch_application(CALCULATOR_APP_NAME)

    def activate(self, pid: int) -> bool:
        return self._runtime.activate_application_pid(pid)

    def wait_for_running_pid(self, timeout_seconds: float) -> int | None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() <= deadline:
            process = self.resolve_running()
            if process.pid is not None:
                return process.pid
            time.sleep(0.1)
        return None
