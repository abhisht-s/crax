"""Read-only ChatGPT Desktop destination snapshot adapter."""

from __future__ import annotations

import ctypes
import plistlib
import subprocess
import sys
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_long,
    c_ulong,
    c_void_p,
    create_string_buffer,
)
from dataclasses import dataclass
from typing import Protocol

from agent.chatgpt_destination_gate import (
    ChatGPTDestinationSnapshot,
    DestinationEvidenceCandidate,
)


DEFAULT_APP_NAME = "ChatGPT"
CHATGPT_BUNDLE_ID = "com.openai.chat"
DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_NODES = 900
DEFAULT_STABILITY_READS = 2
LABEL_LIMIT = 180
SIDEBAR_MAX_X = 420
MAIN_MIN_X = 260
TOP_MAX_Y = 170
BOTTOM_MIN_Y = 520
WINDOW_TITLE_FALLBACK_MAX_DEPTH = 6
WINDOW_TITLE_FALLBACK_TOP_TOLERANCE = 120.0
WINDOW_TITLE_FALLBACK_MAX_TOP_OFFSET = 180.0
WINDOW_TITLE_FALLBACK_MAX_HEIGHT = 120.0
ACTIONABLE_ROLES = {
    "AXButton",
    "AXCell",
    "AXGroup",
    "AXLink",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRow",
}
HEADER_ROLES = {"AXHeading"}
TEXT_ROLES = {"AXStaticText", "AXTextField"}
COMPOSER_ROLES = {"AXTextArea", "AXTextField"}
TRANSCRIPT_ROLES = {"AXScrollArea", "AXWebArea", "AXGroup"}
LIST_ROLES = {"AXList", "AXOutline", "AXScrollArea", "AXTable"}
CHAT_ROW_ROLES = {"AXButton", "AXCell", "AXGroup", "AXLink", "AXRow"}
GROUP_ROLES = {"AXGroup", "AXLayoutArea", "AXLayoutItem", "AXScrollArea"}


@dataclass(frozen=True)
class AXDestinationNode:
    path: str
    depth: int
    role: str = ""
    subrole: str = ""
    identifier: str = ""
    title: str = ""
    description: str = ""
    value: str = ""
    help: str = ""
    role_description: str = ""
    enabled: bool | None = None
    focused: bool | None = None
    selected: bool | None = None
    actions: tuple[str, ...] = ()
    direct_child_count: int | None = None
    frame: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class AXDestinationObservation:
    process_running: bool
    window_available: bool
    accessibility_available: bool
    nodes: tuple[AXDestinationNode, ...] = ()
    traversal_failed: bool = False
    truncated_by_node_limit: bool = False
    truncated_by_depth_limit: bool = False


class AXDestinationSnapshotQuery(Protocol):
    def read_observation(
        self,
        *,
        app_name: str,
        max_depth: int,
        max_nodes: int,
    ) -> AXDestinationObservation: ...


class ChatGPTAXDestinationSnapshotAdapter:
    def __init__(
        self,
        query: AXDestinationSnapshotQuery | None = None,
        *,
        app_name: str = DEFAULT_APP_NAME,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        stability_reads: int = DEFAULT_STABILITY_READS,
    ) -> None:
        self._query = query or _MacOSAXDestinationSnapshotQuery()
        self._app_name = app_name
        self._max_depth = max_depth
        self._max_nodes = max_nodes
        self._stability_reads = max(1, stability_reads)

    def read_destination_snapshot(self) -> ChatGPTDestinationSnapshot:
        observations = tuple(
            self._query.read_observation(
                app_name=self._app_name,
                max_depth=self._max_depth,
                max_nodes=self._max_nodes,
            )
            for _ in range(self._stability_reads)
        )
        primary = observations[-1]
        snapshot = _snapshot_from_observation(primary)
        stable = _observations_stable(observations)
        return ChatGPTDestinationSnapshot(
            process_running=snapshot.process_running,
            window_available=snapshot.window_available,
            accessibility_available=snapshot.accessibility_available,
            snapshot_stable=stable and snapshot.snapshot_stable,
            snapshot_complete=snapshot.snapshot_complete,
            ax_tree_truncated=snapshot.ax_tree_truncated,
            uncertainty_present=snapshot.uncertainty_present,
            active_project_candidates=snapshot.active_project_candidates,
            selected_chat_row_candidates=snapshot.selected_chat_row_candidates,
            conversation_header_candidates=snapshot.conversation_header_candidates,
            composer_available=snapshot.composer_available,
            transcript_available=snapshot.transcript_available,
            conversation_surface_available=snapshot.conversation_surface_available,
            composer_candidate_count=snapshot.composer_candidate_count,
            conversation_surface_candidate_count=snapshot.conversation_surface_candidate_count,
            active_conversation_chat_title=snapshot.active_conversation_chat_title,
            active_conversation_project_title=snapshot.active_conversation_project_title,
        )


def _snapshot_from_observation(observation: AXDestinationObservation) -> ChatGPTDestinationSnapshot:
    truncated = bool(
        observation.truncated_by_node_limit or observation.truncated_by_depth_limit
    )
    window_available = bool(
        observation.window_available
        or (observation.process_running and not observation.accessibility_available)
    )
    complete = bool(
        observation.process_running
        and window_available
        and observation.accessibility_available
        and not observation.traversal_failed
        and not truncated
    )
    nodes = tuple(observation.nodes)
    composer_candidates = _composer_candidates(nodes)
    conversation_surface_candidates = _conversation_surface_candidates(nodes)
    composer_available = len(composer_candidates) == 1
    transcript_available = len(conversation_surface_candidates) == 1
    active_chat_title, active_project_title = _active_conversation_identity(nodes)
    return ChatGPTDestinationSnapshot(
        process_running=bool(observation.process_running),
        window_available=window_available,
        accessibility_available=bool(observation.accessibility_available),
        snapshot_stable=not observation.traversal_failed,
        snapshot_complete=complete,
        ax_tree_truncated=truncated,
        uncertainty_present=bool(observation.traversal_failed),
        active_project_candidates=_active_project_candidates(nodes),
        selected_chat_row_candidates=_selected_chat_row_candidates(nodes),
        conversation_header_candidates=_conversation_header_candidates(nodes),
        composer_available=composer_available,
        transcript_available=transcript_available,
        conversation_surface_available=bool(
            window_available and composer_available and transcript_available
        ),
        composer_candidate_count=len(composer_candidates),
        conversation_surface_candidate_count=len(conversation_surface_candidates),
        active_conversation_chat_title=active_chat_title,
        active_conversation_project_title=active_project_title,
    )


def _active_project_candidates(
    nodes: tuple[AXDestinationNode, ...],
) -> tuple[DestinationEvidenceCandidate, ...]:
    chats_list_confirmed = _project_chats_list_confirmed(nodes)
    classes = _node_classes(nodes)
    labels_with_main_context = {
        _candidate_label(node)
        for node in nodes
        if "main_project_context" in classes.get(node.path, ())
        and _candidate_label(node)
        and not _generic_label(_candidate_label(node))
    }
    candidates: list[DestinationEvidenceCandidate] = []
    for node in nodes:
        label = _candidate_label(node)
        if not label or _generic_label(label):
            continue
        node_classes = classes.get(node.path, ())
        sidebar_project_control = "sidebar_project_control" in node_classes
        paired_main_context = label in labels_with_main_context
        context = _context_text(node)
        structural_context = _structural_context_text(node)
        project_context = bool(
            sidebar_project_control
            or "main_project_context" in node_classes
            or _has_any(
                structural_context,
                (
                    "project",
                    "projects",
                    "selected project",
                    "open project",
                    "current project",
                ),
            )
        )
        if "chat_row_like" in node_classes:
            continue
        active = _active_marker(node) or _has_any(context, ("open project", "current project"))
        actionable = _actionable(node)
        if sidebar_project_control and paired_main_context and chats_list_confirmed:
            candidates.append(
                DestinationEvidenceCandidate(
                    label,
                    active=True,
                    identity_confirmed=True,
                    actionable_destination_evidence=actionable,
                    project_chats_list_confirmed=True,
                )
            )
        elif active and project_context:
            candidates.append(
                DestinationEvidenceCandidate(
                    label,
                    active=True,
                    identity_confirmed=chats_list_confirmed,
                    actionable_destination_evidence=actionable,
                    project_chats_list_confirmed=chats_list_confirmed,
                )
            )
        elif (
            project_context
            and label
            and not ("main_project_context" in node_classes and paired_main_context)
        ):
            candidates.append(
                DestinationEvidenceCandidate(
                    label,
                    active=False,
                    identity_confirmed=False,
                    actionable_destination_evidence=False,
                    project_chats_list_confirmed=chats_list_confirmed,
                )
            )
    return tuple(candidates)


def _selected_chat_row_candidates(
    nodes: tuple[AXDestinationNode, ...],
) -> tuple[DestinationEvidenceCandidate, ...]:
    candidates: list[DestinationEvidenceCandidate] = []
    list_confirmed = _project_chats_list_confirmed(nodes)
    classes = _node_classes(nodes)
    for node in nodes:
        label = _candidate_label(node)
        if not label or _generic_label(label) or node.role not in CHAT_ROW_ROLES:
            continue
        if "chat_row_like" not in classes.get(node.path, ()):
            continue
        context = _context_text(node)
        if _has_any(context, ("project",)) and not _has_any(context, ("chat", "conversation")):
            continue
        row_context = _has_any(context, ("chat", "conversation")) or list_confirmed
        if not row_context:
            continue
        selected = _active_marker(node)
        actionable = _actionable(node)
        candidates.append(
            DestinationEvidenceCandidate(
                label,
                active=selected,
                selected=selected,
                identity_confirmed=bool(selected and actionable and list_confirmed),
                actionable_destination_evidence=bool(selected and actionable),
            )
        )
    return tuple(candidates)


def _conversation_header_candidates(
    nodes: tuple[AXDestinationNode, ...],
) -> tuple[DestinationEvidenceCandidate, ...]:
    candidates: list[DestinationEvidenceCandidate] = []
    classes = _node_classes(nodes)
    for node in nodes:
        label = _candidate_label(node)
        if not label or _generic_label(label):
            continue
        context = _context_text(node)
        header_context = "active_conversation_header_like" in classes.get(node.path, ())
        legacy_header_context = node.role in HEADER_ROLES or _has_any(
            context,
            ("conversation title", "chat title", "current conversation"),
        )
        if not header_context and not legacy_header_context:
            continue
        if "toolbar_button_like" in classes.get(node.path, ()):
            continue
        candidates.append(
            DestinationEvidenceCandidate(
                label,
                active=True,
                identity_confirmed=True,
                actionable_destination_evidence=True,
            )
        )
    return tuple(candidates)


def _composer_candidates(nodes: tuple[AXDestinationNode, ...]) -> tuple[AXDestinationNode, ...]:
    classes = _node_classes(nodes)
    return tuple(
        node for node in nodes if "composer_like" in classes.get(node.path, ())
    )


def _conversation_surface_candidates(
    nodes: tuple[AXDestinationNode, ...],
) -> tuple[AXDestinationNode, ...]:
    classes = _node_classes(nodes)
    return tuple(
        node
        for node in nodes
        if "conversation_surface_like" in classes.get(node.path, ())
    )


def _project_chats_list_confirmed(nodes: tuple[AXDestinationNode, ...]) -> bool:
    has_chats = False
    has_sources = False
    has_list = False
    for node in nodes:
        label = _visible_label(node).casefold()
        if label == "chats":
            has_chats = True
        if label == "sources":
            has_sources = True
        text = _context_text(node)
        if node.role in LIST_ROLES and _has_any(text, ("chat", "conversation", "project")):
            has_list = True
    return bool(has_chats and has_sources and has_list)


def _node_classes(nodes: tuple[AXDestinationNode, ...]) -> dict[str, frozenset[str]]:
    by_path = {node.path: node for node in nodes}
    window_frame = by_path.get("W").frame if by_path.get("W") is not None else None
    children_by_parent: dict[str, list[AXDestinationNode]] = {}
    for node in nodes:
        parent_path = _parent_path(node.path)
        if parent_path:
            children_by_parent.setdefault(parent_path, []).append(node)

    chats_list_confirmed = _project_chats_list_confirmed(nodes)
    classes: dict[str, frozenset[str]] = {}
    for node in nodes:
        parent = by_path.get(_parent_path(node.path))
        siblings = tuple(
            sibling
            for sibling in children_by_parent.get(_parent_path(node.path), ())
            if sibling.path != node.path
        )
        text = _context_text(node)
        frame_band = _frame_band(node.frame, window_frame=window_frame)
        node_classes: set[str] = set()

        if node.role in GROUP_ROLES:
            node_classes.add("generic_group")
        if _candidate_label(node) and not _actionable(node):
            node_classes.add("non_actionable_visible_label")
        if _sidebar_like(node, frame_band, parent, siblings):
            if node.role in ACTIONABLE_ROLES:
                node_classes.add("sidebar_project_control")
        if _main_content_like(node, frame_band, parent) and node.role in TEXT_ROLES:
            node_classes.add("main_project_context")
        if _chat_row_like(node, text, frame_band, parent, siblings, chats_list_confirmed):
            node_classes.add("chat_row_like")
        if _toolbar_button_like(node, text, frame_band, parent):
            node_classes.add("toolbar_button_like")
        if _active_conversation_header_like(node, text, frame_band, parent):
            node_classes.add("active_conversation_header_like")
        if _composer_like(node, text, frame_band, parent, siblings):
            node_classes.add("composer_like")
        if _conversation_surface_like(node, text, frame_band, parent):
            node_classes.add("conversation_surface_like")

        classes[node.path] = frozenset(node_classes)
    return classes


def _sidebar_like(
    node: AXDestinationNode,
    frame_band: str,
    parent: AXDestinationNode | None,
    siblings: tuple[AXDestinationNode, ...],
) -> bool:
    if frame_band.endswith("-left"):
        return True
    sibling_text = " ".join(_visible_label(sibling).casefold() for sibling in siblings)
    parent_text = _context_text(parent) if parent is not None else ""
    return bool(
        _has_any(sibling_text, ("chats", "sources", "projects"))
        or _has_any(parent_text, ("sidebar", "project", "chat list", "conversation list"))
    )


def _main_content_like(
    node: AXDestinationNode,
    frame_band: str,
    parent: AXDestinationNode | None,
) -> bool:
    if frame_band.endswith("-main") or frame_band.startswith("center"):
        return True
    parent_text = _context_text(parent) if parent is not None else ""
    return _has_any(parent_text, ("main", "content", "conversation"))


def _chat_row_like(
    node: AXDestinationNode,
    text: str,
    frame_band: str,
    parent: AXDestinationNode | None,
    siblings: tuple[AXDestinationNode, ...],
    chats_list_confirmed: bool,
) -> bool:
    if node.role not in CHAT_ROW_ROLES:
        return False
    if _toolbar_button_like(node, text, frame_band, parent):
        return False
    if _has_any(text, ("project",)) and not _has_any(text, ("chat", "conversation")):
        return False
    parent_text = _context_text(parent) if parent is not None else ""
    sibling_roles = {sibling.role for sibling in siblings}
    return bool(
        _has_any(text, ("chat row", "conversation row", "selected chat"))
        or _has_any(parent_text, ("chat", "conversation", "list", "outline", "table"))
        or bool(sibling_roles & CHAT_ROW_ROLES)
        or chats_list_confirmed
    )


def _toolbar_button_like(
    node: AXDestinationNode,
    text: str,
    frame_band: str,
    parent: AXDestinationNode | None,
) -> bool:
    if node.role not in {"AXButton", "AXMenuButton", "AXPopUpButton"}:
        return False
    parent_text = _context_text(parent) if parent is not None else ""
    return bool(frame_band.startswith("top") or _has_any(text + " " + parent_text, ("toolbar",)))


def _active_conversation_header_like(
    node: AXDestinationNode,
    text: str,
    frame_band: str,
    parent: AXDestinationNode | None,
) -> bool:
    if node.role in HEADER_ROLES:
        return True
    if node.role not in TEXT_ROLES:
        return False
    parent_text = _context_text(parent) if parent is not None else ""
    return bool(
        _main_content_like(node, frame_band, parent)
        and _has_any(
            text + " " + parent_text,
            ("header", "conversation title", "chat title", "current conversation"),
        )
    )


def _composer_like(
    node: AXDestinationNode,
    text: str,
    frame_band: str,
    parent: AXDestinationNode | None,
    siblings: tuple[AXDestinationNode, ...],
) -> bool:
    if node.role not in COMPOSER_ROLES:
        return False
    if _has_any(text, ("message chatgpt", "ask anything", "send a message", "composer")):
        return True
    parent_text = _context_text(parent) if parent is not None else ""
    sibling_text = " ".join(_context_text(sibling) for sibling in siblings)
    return bool(
        frame_band.startswith(("bottom", "main"))
        and (
            node.enabled is not False
            or _has_any(parent_text + " " + sibling_text, ("send", "dictate", "attach"))
        )
    )


def _conversation_surface_like(
    node: AXDestinationNode,
    text: str,
    frame_band: str,
    parent: AXDestinationNode | None,
) -> bool:
    if node.role not in TRANSCRIPT_ROLES:
        return False
    if node.role in LIST_ROLES and _has_any(text, ("project", "sources", "chat list")):
        return False
    if _has_any(text, ("transcript", "conversation", "messages")):
        return True
    parent_text = _context_text(parent) if parent is not None else ""
    return bool(
        (frame_band.endswith("-main") or frame_band.startswith("center"))
        and (
            (node.direct_child_count or 0) >= 2
            or _has_any(parent_text, ("main", "content", "conversation"))
        )
    )


def _observations_stable(observations: tuple[AXDestinationObservation, ...]) -> bool:
    if len(observations) <= 1:
        return True
    first = _observation_signature(observations[0])
    return all(_observation_signature(item) == first for item in observations[1:])


def _observation_signature(observation: AXDestinationObservation) -> tuple:
    classes = _node_classes(tuple(observation.nodes))
    return (
        observation.process_running,
        observation.window_available,
        observation.accessibility_available,
        observation.traversal_failed,
        observation.truncated_by_node_limit,
        observation.truncated_by_depth_limit,
        tuple(
            sorted(
                _node_signature(node, classes)
                for node in observation.nodes
                if _identity_relevant(node, classes)
            )
        ),
    )


def _node_signature(
    node: AXDestinationNode,
    classes: dict[str, frozenset[str]],
) -> tuple:
    return (
        node.depth,
        node.role,
        node.subrole,
        _path_class(node.path),
        _identifier_class(node.identifier),
        tuple(sorted(classes.get(node.path, ()))),
        _candidate_label(node) if _candidate_like(node) else "",
        _label_source(node),
        node.enabled,
        node.focused,
        node.selected,
        tuple(sorted(_action_class(action) for action in node.actions)),
        _child_count_bucket(node.direct_child_count),
        _frame_band(node.frame),
    )


def _identity_relevant(
    node: AXDestinationNode,
    classes: dict[str, frozenset[str]],
) -> bool:
    node_classes = classes.get(node.path, ())
    if node_classes:
        return True
    return _candidate_like(node)


def _candidate_like(node: AXDestinationNode) -> bool:
    text = _context_text(node)
    return bool(
        node.role in ACTIONABLE_ROLES
        or node.role in HEADER_ROLES
        or node.role in COMPOSER_ROLES
        or _has_any(text, ("project", "conversation title", "chat title", "composer"))
    )


def _frame_band(
    frame: tuple[float, float, float, float] | None,
    *,
    window_frame: tuple[float, float, float, float] | None = None,
) -> str:
    if frame is None:
        return "unknown"
    x, y, width, height = frame
    relative_x = x
    relative_y = y
    window_height = 0.0
    if window_frame is not None:
        window_x, window_y, _window_width, window_height = window_frame
        relative_x = x - window_x
        relative_y = y - window_y
    horizontal = "left" if relative_x < SIDEBAR_MAX_X and width < 720 else "main"
    if relative_x >= MAIN_MIN_X and width >= 320:
        horizontal = "main"
    top_threshold = min(TOP_MAX_Y, window_height * 0.25) if window_height > 0 else TOP_MAX_Y
    bottom_threshold = (
        min(BOTTOM_MIN_Y, window_height * 0.65)
        if window_height > 0
        else BOTTOM_MIN_Y
    )
    vertical = "top" if relative_y < top_threshold else "middle"
    if relative_y >= bottom_threshold:
        vertical = "bottom"
    return f"{vertical}-{horizontal}"


def _path_class(path: str) -> tuple[int, str]:
    parts = tuple(part for part in path.split(".") if part)
    return (len(parts), ".".join(parts[:2]))


def _parent_path(path: str) -> str:
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[0]


def _identifier_class(identifier: str) -> str:
    text = identifier.casefold()
    if not text:
        return ""
    for token in ("sidebar", "toolbar", "composer", "conversation", "project", "thread"):
        if token in text:
            return token
    return "present"


def _action_class(action: str) -> str:
    text = action.casefold()
    if "press" in text or "default" in text:
        return "default"
    if "show" in text or "menu" in text:
        return "menu"
    return "other"


def _child_count_bucket(count: int | None) -> str:
    if count is None:
        return "unknown"
    if count == 0:
        return "none"
    if count == 1:
        return "one"
    if count <= 4:
        return "few"
    return "many"


def _candidate_label(node: AXDestinationNode) -> str:
    if node.role in LIST_ROLES:
        return ""
    if node.role in COMPOSER_ROLES:
        return _visible_label(node, include_value=False)
    if node.role in HEADER_ROLES:
        return _visible_label(node)
    if node.role in ACTIONABLE_ROLES:
        return _visible_label(node)
    context = _context_text(node)
    if _has_any(context, ("conversation title", "chat title", "current conversation")):
        return _visible_label(node)
    if _has_any(context, ("project", "chat row", "conversation row")):
        return _visible_label(node)
    if node.role in TEXT_ROLES and _safe_value_label(node):
        return _visible_label(node)
    return ""


def _visible_label(
    node: AXDestinationNode,
    *,
    include_value: bool = True,
) -> str:
    values = (node.title, node.description, node.value if include_value else "")
    for value in values:
        label = _compact_label(value)
        if label:
            return label
    if _structurally_relevant_for_auxiliary_label(node):
        for value in (node.help, node.role_description):
            label = _compact_label(value)
            if label and not _generic_label(label):
                return label
    return ""


def _label_source(node: AXDestinationNode) -> str:
    if _compact_label(node.title):
        return "title"
    if _compact_label(node.description):
        return "description"
    if node.role not in COMPOSER_ROLES and _compact_label(node.value):
        return "value"
    if _structurally_relevant_for_auxiliary_label(node) and _compact_label(node.help):
        return "help"
    if (
        _structurally_relevant_for_auxiliary_label(node)
        and _compact_label(node.role_description)
    ):
        return "role_description"
    return ""


def _compact_label(value: str) -> str:
    label = " ".join(str(value or "").split())
    if not label:
        return ""
    if len(label) > LABEL_LIMIT:
        return ""
    return label


def _context_text(node: AXDestinationNode) -> str:
    return " ".join(
        part
        for part in (
            node.role,
            node.subrole,
            node.identifier,
            node.title,
            node.description,
            _safe_value_label(node),
            node.help,
            node.role_description,
        )
        if part
    ).casefold()


def _structural_context_text(node: AXDestinationNode) -> str:
    label = _candidate_label(node)
    return " ".join(
        part
        for part in (
            node.role,
            node.subrole,
            node.identifier,
            "" if node.title == label else node.title,
            "" if node.description == label else node.description,
            node.help,
            node.role_description,
        )
        if part
    ).casefold()


def _safe_value_label(node: AXDestinationNode) -> str:
    return _compact_label(node.value)


def _structurally_relevant_for_auxiliary_label(node: AXDestinationNode) -> bool:
    return bool(
        node.role in ACTIONABLE_ROLES
        or node.role in HEADER_ROLES
        or node.role in TEXT_ROLES
        or node.role in COMPOSER_ROLES
    )


def _has_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _active_marker(node: AXDestinationNode) -> bool:
    return bool(node.selected is True or node.focused is True or _has_any(_context_text(node), ("selected", "active", "current")))


def _window_title_item(node: AXDestinationNode) -> bool:
    """A macOS window toolbar/title-proxy item.

    Identified by toolbar-management accessibility actions (e.g. "Remove from
    toolbar" / "Move next") or by residing in the top window band. The window
    toolbar exposes the single currently-open conversation's identity, which is
    the authoritative signal for the active destination.
    """

    if node.role not in {"AXButton", "AXStaticText", "AXHeading"}:
        return False
    action_text = " ".join(node.actions).casefold()
    # ChatGPT keeps old transcript nodes in the AX tree with large negative Y
    # coordinates.  Frame-band classification alone can label those historical
    # nodes as "top", so only toolbar-management actions identify the current
    # window title item.
    return _has_any(action_text, ("toolbar", "move next", "move previous"))


def _active_conversation_identity(
    nodes: tuple[AXDestinationNode, ...],
) -> tuple[str, str]:
    """Return (chat_title, project_title) from the window title item, else ("","").

    ChatGPT Desktop's window toolbar exposes the open conversation as an exact
    ``"<chat title>, <project title>"`` label. Bound titles never contain commas
    (enforced at binding time), so the label splits cleanly into exactly two
    non-empty parts. This is the authoritative active-destination identity.
    """

    window = next(
        (
            node
            for node in nodes
            if node.path == "W" and node.role == "AXWindow"
        ),
        None,
    )
    window_frame = window.frame if window is not None else None
    action_identified: list[tuple[str, str]] = []
    structurally_identified: list[tuple[str, str]] = []
    for node in nodes:
        identity = _conversation_identity_from_title_node(node)
        if identity is None:
            continue
        if _window_title_item(node):
            action_identified.append(identity)
        elif _structural_window_title_item(node, window_frame):
            structurally_identified.append(identity)

    for candidates in (action_identified, structurally_identified):
        unique = tuple(dict.fromkeys(candidates))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            return "", ""
    return "", ""


def _conversation_identity_from_title_node(
    node: AXDestinationNode,
) -> tuple[str, str] | None:
    label = (
        _compact_label(node.description)
        or _compact_label(node.title)
        or _compact_label(node.value)
    )
    if not label or "," not in label:
        return None
    parts = [part.strip() for part in label.split(",")]
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def _structural_window_title_item(
    node: AXDestinationNode,
    window_frame: tuple[float, float, float, float] | None,
) -> bool:
    """Recognize the current title item when toolbar actions are no longer exposed.

    This covers builds that retain shallow, top-of-window title geometry while
    omitting the toolbar-management actions used by older builds. The fallback
    stays fail-closed: it requires one shallow title-like node with a valid frame
    in the window's title band. Deep or far-offscreen transcript text is rejected.
    """

    if node.role not in {"AXButton", "AXStaticText", "AXHeading"}:
        return False
    if node.depth > WINDOW_TITLE_FALLBACK_MAX_DEPTH:
        return False
    if node.frame is None or window_frame is None:
        return False
    node_x, node_y, node_width, node_height = node.frame
    window_x, window_y, window_width, window_height = window_frame
    if (
        node_width <= 0
        or node_height <= 0
        or window_width <= 0
        or window_height <= 0
        or node_height > WINDOW_TITLE_FALLBACK_MAX_HEIGHT
    ):
        return False
    if node_x + node_width <= window_x or node_x >= window_x + window_width:
        return False
    return bool(
        node_y >= window_y - WINDOW_TITLE_FALLBACK_TOP_TOLERANCE
        and node_y <= window_y + min(
            WINDOW_TITLE_FALLBACK_MAX_TOP_OFFSET,
            window_height * 0.25,
        )
    )


def _actionable(node: AXDestinationNode) -> bool:
    return bool(node.enabled is not False and node.role in ACTIONABLE_ROLES and node.actions)


def _generic_label(label: str) -> bool:
    return label.casefold() in {
        "chats",
        "sources",
        "new chat",
        "search",
        "library",
        "gpts",
        "projects",
    }


class _MacOSAXDestinationSnapshotQuery:
    def read_observation(
        self,
        *,
        app_name: str,
        max_depth: int,
        max_nodes: int,
    ) -> AXDestinationObservation:
        if sys.platform != "darwin":
            return AXDestinationObservation(
                process_running=False,
                window_available=False,
                accessibility_available=False,
                traversal_failed=True,
            )
        pid = _pid_for_app(app_name)
        if pid is None:
            return AXDestinationObservation(
                process_running=False,
                window_available=False,
                accessibility_available=True,
            )
        reader = _ReadOnlyAXTreeReader(max_depth=max_depth, max_nodes=max_nodes)
        return reader.read(pid)


def _pid_for_app(app_name: str) -> int | None:
    bundle_id = CHATGPT_BUNDLE_ID if app_name == DEFAULT_APP_NAME else ""
    if bundle_id:
        pid = resolve_classic_chatgpt_pid()
        if pid is not None:
            return pid
    names = [app_name]
    if app_name == DEFAULT_APP_NAME:
        names.append("ChatGPT Desktop")
    for name in names:
        result = subprocess.run(
            ["pgrep", "-x", name],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                try:
                    pid = int(line.strip())
                except ValueError:
                    continue
                if pid > 0:
                    if not bundle_id or _pid_bundle_id(pid) == bundle_id:
                        return pid
    return None


def resolve_classic_chatgpt_pid() -> int | None:
    """Return only the Classic ChatGPT process, never the Work/Codex app.

    Both desktop applications can present the localized name ``ChatGPT``.  AX
    readers that act on or capture from the user-facing ChatGPT conversation
    must therefore resolve the Classic bundle explicitly rather than selecting
    the first name match returned by System Events.
    """

    return _pid_for_bundle_id(CHATGPT_BUNDLE_ID)


def _pid_for_bundle_id(bundle_id: str) -> int | None:
    for pid in (_pid_for_bundle_id_with_lsappinfo(bundle_id), _pid_for_bundle_id_with_system_events(bundle_id)):
        if pid is not None and pid > 0:
            return pid
    return None


def _pid_for_bundle_id_with_lsappinfo(bundle_id: str) -> int | None:
    find_result = subprocess.run(
        ["lsappinfo", "find", f"bundleid={bundle_id}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if find_result.returncode != 0:
        return None
    app_ref = find_result.stdout.strip()
    if not app_ref:
        return None
    info_result = subprocess.run(
        ["lsappinfo", "info", "-only", "pid", app_ref],
        text=True,
        capture_output=True,
        check=False,
    )
    if info_result.returncode != 0:
        return None
    for token in info_result.stdout.replace("=", " ").split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def _pid_for_bundle_id_with_system_events(bundle_id: str) -> int | None:
    script = (
        'tell application "System Events" to get unix id of first process '
        f'whose bundle identifier is "{bundle_id}"'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_bundle_id(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "comm="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    executable = result.stdout.strip()
    marker = ".app/Contents/MacOS/"
    if marker not in executable:
        return ""
    app_root = executable.split(marker, 1)[0] + ".app"
    try:
        with open(f"{app_root}/Contents/Info.plist", "rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        return ""
    value = info.get("CFBundleIdentifier", "")
    return value if isinstance(value, str) else ""


class _CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class _CGSize(Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


class _ReadOnlyAXTreeReader:
    def __init__(self, *, max_depth: int, max_nodes: int) -> None:
        self._max_depth = max_depth
        self._max_nodes = max_nodes
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._attr_cache: dict[str, int] = {}
        self._configure()

    def _configure(self) -> None:
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
        self._ax.AXIsProcessTrusted.restype = c_bool
        self._ax.AXUIElementCreateApplication.argtypes = [c_int]
        self._ax.AXUIElementCreateApplication.restype = c_void_p
        self._ax.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyAttributeValue.restype = c_int
        self._ax.AXUIElementCopyActionNames.argtypes = [c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyActionNames.restype = c_int
        self._ax.AXValueGetValue.argtypes = [c_void_p, c_int, c_void_p]
        self._ax.AXValueGetValue.restype = c_bool
        self._string_type = self._cf.CFStringGetTypeID()
        self._array_type = self._cf.CFArrayGetTypeID()
        self._boolean_type = self._cf.CFBooleanGetTypeID()

    def read(self, pid: int) -> AXDestinationObservation:
        if not bool(self._ax.AXIsProcessTrusted()):
            return AXDestinationObservation(
                process_running=True,
                window_available=False,
                accessibility_available=False,
            )
        app = self._ax.AXUIElementCreateApplication(pid)
        if not app:
            return AXDestinationObservation(
                process_running=True,
                window_available=False,
                accessibility_available=True,
                traversal_failed=True,
            )
        window = self._window(app)
        if not window:
            return AXDestinationObservation(
                process_running=True,
                window_available=False,
                accessibility_available=True,
            )

        nodes: list[AXDestinationNode] = []
        state = {"truncated_by_node_limit": False, "truncated_by_depth_limit": False}
        self._walk(window, "W", 0, nodes, state)
        return AXDestinationObservation(
            process_running=True,
            window_available=True,
            accessibility_available=True,
            nodes=tuple(nodes),
            truncated_by_node_limit=bool(state["truncated_by_node_limit"]),
            truncated_by_depth_limit=bool(state["truncated_by_depth_limit"]),
        )

    def _window(self, app: int) -> int | None:
        window = self._copy_attribute(app, "AXFocusedWindow")
        if window:
            return window
        windows = self._array_values(self._copy_attribute(app, "AXWindows"))
        return windows[0] if windows else None

    def _walk(
        self,
        element: int,
        path: str,
        depth: int,
        nodes: list[AXDestinationNode],
        state: dict[str, bool],
    ) -> None:
        if len(nodes) >= self._max_nodes:
            state["truncated_by_node_limit"] = True
            return
        children = self._children(element)
        nodes.append(self._node(element, path, depth, len(children)))
        if depth >= self._max_depth:
            if children:
                state["truncated_by_depth_limit"] = True
            return
        for index, child in enumerate(children, start=1):
            if len(nodes) >= self._max_nodes:
                state["truncated_by_node_limit"] = True
                return
            self._walk(child, f"{path}.{index}", depth + 1, nodes, state)

    def _node(
        self,
        element: int,
        path: str,
        depth: int,
        direct_child_count: int,
    ) -> AXDestinationNode:
        return AXDestinationNode(
            path=path,
            depth=depth,
            role=self._cf_string(self._copy_attribute(element, "AXRole")),
            subrole=self._cf_string(self._copy_attribute(element, "AXSubrole")),
            identifier=self._cf_string(self._copy_attribute(element, "AXIdentifier")),
            title=self._cf_string(self._copy_attribute(element, "AXTitle")),
            description=self._cf_string(self._copy_attribute(element, "AXDescription")),
            value=self._cf_string(self._copy_attribute(element, "AXValue")),
            help=self._cf_string(self._copy_attribute(element, "AXHelp")),
            role_description=self._cf_string(
                self._copy_attribute(element, "AXRoleDescription")
            ),
            enabled=self._cf_bool(self._copy_attribute(element, "AXEnabled")),
            focused=self._cf_bool(self._copy_attribute(element, "AXFocused")),
            selected=self._cf_bool(self._copy_attribute(element, "AXSelected")),
            actions=self._array_strings(self._copy_actions(element)),
            direct_child_count=direct_child_count,
            frame=self._frame(element),
        )

    def _children(self, element: int) -> tuple[int, ...]:
        for name in ("AXChildren", "AXVisibleChildren"):
            children = self._array_values(self._copy_attribute(element, name))
            if children:
                return children
        return ()

    def _frame(self, element: int) -> tuple[float, float, float, float] | None:
        position_value = self._copy_attribute(element, "AXPosition")
        size_value = self._copy_attribute(element, "AXSize")
        if not position_value or not size_value:
            return None
        position = _CGPoint()
        size = _CGSize()
        try:
            position_ok = self._ax.AXValueGetValue(
                c_void_p(position_value),
                1,
                byref(position),
            )
            size_ok = self._ax.AXValueGetValue(
                c_void_p(size_value),
                2,
                byref(size),
            )
        except Exception:
            return None
        if not position_ok or not size_ok:
            return None
        return (position.x, position.y, size.width, size.height)

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
        return tuple(
            text
            for text in (self._cf_string(item) for item in self._array_values(value))
            if text
        )

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
