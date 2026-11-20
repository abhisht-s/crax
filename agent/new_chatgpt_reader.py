"""Accessibility tree reader for the new ChatGPT desktop app (``com.openai.codex``).

Step 2 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``. Standalone; not wired into
the supervision loop yet.

Two operations, both re-run from scratch on every call:

- :func:`locate_anchors` finds the app's regions by stable anchors (DOM ids, AX
  landmarks, roles and labels), never by index paths:
  sidebar ``#app-shell-sidebar``, title bar ``AXLandmarkBanner``, main
  ``AXLandmarkMain``, chat panel, turn list (the group whose children are the
  "You said:" / "ChatGPT said:" headings) and the composer (``AXTextArea``
  "Ask ChatGPT" with class ``ProseMirror``).

  The chat panel is the outermost element that holds both the transcript and
  the composer. App builds differ: the 2026-11-18 build marked it with a DOM id
  ending ``-chat-panel``; the 2026-11-20 build dropped that id. Both builds
  carry the class ``group/thread-scroll-layout`` on the layout inside it, so
  that class is the fallback anchor.
- :func:`snapshot_region` copies one region into immutable :class:`AXNodeData`
  in document order, reading each element's attributes in one AX round trip.
  Snapshots carry element handles for later actions but no index paths and no
  frames (off-screen frames are clamped and must not be used).

A walk that hits its depth or node limit is reported as incomplete; callers
must not act on incomplete data.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from agent import new_chatgpt_labels as labels
from agent.new_chatgpt_app import (
    COMPOSER_DOM_CLASS,
    ROOT_DOM_ID,
    SIDEBAR_DOM_ID,
    AXBackend,
    _web_content,
)

READER_METHOD = "ax_reader_new_chatgpt"

CHAT_PANEL_DOM_ID_SUFFIX = "-chat-panel"  # 2026-11-18 build only
THREAD_LAYOUT_DOM_CLASS = "group/thread-scroll-layout"  # 2026-11-18 and 2026-11-20 builds
BANNER_SUBROLE = "AXLandmarkBanner"
MAIN_SUBROLE = "AXLandmarkMain"

NODE_ATTRIBUTES: tuple[str, ...] = (
    "AXRole",
    "AXSubrole",
    "AXRoleDescription",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXDOMIdentifier",
    "AXDOMClassList",
    "AXEnabled",
    "AXSelected",
    "AXFocused",
    "AXExpanded",
    "AXARIACurrent",
    "AXChildren",
)
_LOCATE_ATTRIBUTES: tuple[str, ...] = (
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXDescription",
    "AXDOMIdentifier",
    "AXDOMClassList",
    "AXChildren",
)

# Chat content sits 26-31 levels below the window; leave generous headroom.
DEFAULT_REGION_MAX_DEPTH = 80
DEFAULT_REGION_MAX_NODES = 30000
DEFAULT_LOCATE_MAX_NODES = 30000


def read_attributes(backend: AXBackend, element: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """One round trip when the backend supports it, else one call per attribute."""

    bulk = getattr(backend, "attributes", None)
    if callable(bulk):
        return bulk(element, names)
    return {name: backend.attribute(element, name) for name in names}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _elements(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _classes(value: Any) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


# --- snapshots -----------------------------------------------------------------------


@dataclass(frozen=True)
class AXNodeData:
    role: str
    subrole: str = ""
    role_description: str = ""
    title: str = ""
    description: str = ""
    value: str | int | float | None = None
    dom_id: str = ""
    dom_classes: tuple[str, ...] = ()
    enabled: bool | None = None
    selected: bool | None = None
    focused: bool | None = None
    expanded: bool | None = None
    # aria-current; "page" marks the open conversation's sidebar row (seen 2026-11-20).
    aria_current: str = ""
    children: tuple["AXNodeData", ...] = ()
    child_count: int = 0
    children_truncated: bool = False
    element: Any = field(default=None, compare=False, repr=False)

    @property
    def label(self) -> str:
        """Accessible name: description first (web ``aria-label``), then title."""

        return self.description or self.title

    @property
    def text_value(self) -> str:
        return self.value if isinstance(self.value, str) else ""

    def has_label(self, *candidates: str) -> bool:
        return self.description in candidates or self.title in candidates

    def iter(self) -> Iterator["AXNodeData"]:
        """Pre-order (document order) traversal including this node."""

        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def find_all(self, predicate: Callable[["AXNodeData"], bool]) -> list["AXNodeData"]:
        return [node for node in self.iter() if predicate(node)]

    def find_first(self, predicate: Callable[["AXNodeData"], bool]) -> "AXNodeData | None":
        return next((node for node in self.iter() if predicate(node)), None)


@dataclass(frozen=True)
class RegionSnapshot:
    root: AXNodeData | None
    node_count: int
    truncated_by_depth: bool = False
    truncated_by_nodes: bool = False

    @property
    def complete(self) -> bool:
        return self.root is not None and not (self.truncated_by_depth or self.truncated_by_nodes)


def snapshot_region(
    backend: AXBackend,
    element: Any,
    *,
    max_depth: int = DEFAULT_REGION_MAX_DEPTH,
    max_nodes: int = DEFAULT_REGION_MAX_NODES,
) -> RegionSnapshot:
    """Copy ``element``'s subtree in document order, within the given limits."""

    if element is None:
        return RegionSnapshot(root=None, node_count=0)
    state = {"count": 0, "depth": False, "nodes": False}

    def build(current: Any, depth: int) -> AXNodeData:
        state["count"] += 1
        attrs = read_attributes(backend, current, NODE_ATTRIBUTES)
        child_elements = _elements(attrs.get("AXChildren"))
        children: list[AXNodeData] = []
        truncated = False
        if child_elements and depth >= max_depth:
            state["depth"] = True
            truncated = True
        else:
            for child in child_elements:
                if state["count"] >= max_nodes:
                    state["nodes"] = True
                    truncated = True
                    break
                children.append(build(child, depth + 1))
        raw_value = attrs.get("AXValue")
        return AXNodeData(
            role=_text(attrs.get("AXRole")),
            subrole=_text(attrs.get("AXSubrole")),
            role_description=_text(attrs.get("AXRoleDescription")),
            title=_text(attrs.get("AXTitle")),
            description=_text(attrs.get("AXDescription")),
            value=raw_value if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool) else None,
            dom_id=_text(attrs.get("AXDOMIdentifier")),
            dom_classes=_classes(attrs.get("AXDOMClassList")),
            enabled=_flag(attrs.get("AXEnabled")),
            selected=_flag(attrs.get("AXSelected")),
            focused=_flag(attrs.get("AXFocused")),
            expanded=_flag(attrs.get("AXExpanded")),
            aria_current=_text(attrs.get("AXARIACurrent")),
            children=tuple(children),
            child_count=len(child_elements),
            children_truncated=truncated,
            element=current,
        )

    root = build(element, 0)
    return RegionSnapshot(
        root=root,
        node_count=state["count"],
        truncated_by_depth=state["depth"],
        truncated_by_nodes=state["nodes"],
    )


# --- turns ---------------------------------------------------------------------------

USER_ROLE = "user"
ASSISTANT_ROLE = "assistant"


@dataclass(frozen=True)
class Turn:
    """One turn of the flat turn list: its heading and the sibling parts after it."""

    role: str
    heading: AXNodeData
    parts: tuple[AXNodeData, ...]

    def static_texts(self) -> list[str]:
        return [node.text_value for part in self.parts for node in part.iter() if node.role == "AXStaticText" and node.text_value]


def split_turns(turn_list: AXNodeData) -> list[Turn]:
    """Split the flat turn list at the "You said:" / "ChatGPT said:" headings.

    Parts before the first heading (if any) are ignored. Turns stay in document order.
    """

    turns: list[Turn] = []
    role: str | None = None
    heading: AXNodeData | None = None
    parts: list[AXNodeData] = []
    for child in turn_list.children:
        if child.role == "AXHeading" and child.has_label(labels.USER_TURN_HEADING.text, labels.ASSISTANT_TURN_HEADING.text):
            if heading is not None and role is not None:
                turns.append(Turn(role, heading, tuple(parts)))
            role = USER_ROLE if child.has_label(labels.USER_TURN_HEADING.text) else ASSISTANT_ROLE
            heading, parts = child, []
        elif heading is not None:
            parts.append(child)
    if heading is not None and role is not None:
        turns.append(Turn(role, heading, tuple(parts)))
    return turns


# --- anchors -------------------------------------------------------------------------


ANCHOR_NAMES = ("web_area", "root", "sidebar", "banner", "main", "chat_panel", "turn_list", "composer")


@dataclass
class NewChatGPTAnchors:
    """Element handles for the app's regions. Re-locate on every use; never cache."""

    web_area: Any = None
    top_group: Any = None
    root: Any = None
    sidebar: Any = None
    banner: Any = None
    main: Any = None
    chat_panel: Any = None
    turn_list: Any = None
    composer: Any = None
    web_area_title: str = ""
    nodes_visited: int = 0
    search_complete: bool = False
    turn_heading_count: int = 0
    composer_candidate_count: int = 0
    turn_list_candidate_count: int = 0

    def missing(self, *names: str) -> list[str]:
        return [name for name in (names or ANCHOR_NAMES) if getattr(self, name) is None]

    def summary(self) -> dict:
        """Evidence for the ledger: which anchors were found, never any content."""

        return {
            "method": READER_METHOD,
            "found": {name: getattr(self, name) is not None for name in ANCHOR_NAMES},
            "nodes_visited": self.nodes_visited,
            "search_complete": self.search_complete,
            "turn_heading_count": self.turn_heading_count,
            "composer_candidate_count": self.composer_candidate_count,
            "turn_list_candidate_count": self.turn_list_candidate_count,
        }


def locate_anchors(
    backend: AXBackend,
    app: Any,
    *,
    max_nodes: int = DEFAULT_LOCATE_MAX_NODES,
) -> NewChatGPTAnchors:
    """Find the app's regions from ``#root`` by stable anchors.

    The search skips the sidebar's and title bar's insides and the contents of
    individual turns, so its cost does not grow with the number of chats in the
    sidebar or the length of replies. A composer or turn list is only accepted
    when exactly one candidate exists inside the chat panel.
    """

    anchors = NewChatGPTAnchors()
    content = _web_content(backend, app)
    if content is None:
        return anchors
    anchors.web_area, anchors.top_group, anchors.root = content
    anchors.web_area_title = _text(backend.attribute(anchors.web_area, "AXTitle"))

    composers: list[Any] = []
    turn_lists: dict[int, Any] = {}
    turn_list_ids: set[int] = set()
    heading_count = 0

    # (element, parent, in_chat_panel, parent_is_turn_list)
    queue: deque[tuple[Any, Any, bool, bool]] = deque([(anchors.root, None, False, False)])
    visited = 0
    exhausted = True
    while queue:
        if visited >= max_nodes:
            exhausted = False
            break
        element, parent, in_panel, parent_is_turn_list = queue.popleft()
        visited += 1
        attrs = read_attributes(backend, element, _LOCATE_ATTRIBUTES)
        role = _text(attrs.get("AXRole"))
        subrole = _text(attrs.get("AXSubrole"))
        dom_id = _text(attrs.get("AXDOMIdentifier"))
        label_values = (_text(attrs.get("AXDescription")), _text(attrs.get("AXTitle")))

        descend = True
        if dom_id == SIDEBAR_DOM_ID:
            anchors.sidebar = anchors.sidebar or element
            descend = False
        elif subrole == BANNER_SUBROLE:
            anchors.banner = anchors.banner or element
            descend = False
        elif subrole == MAIN_SUBROLE:
            anchors.main = anchors.main or element

        if not in_panel and (
            (dom_id.endswith(CHAT_PANEL_DOM_ID_SUFFIX) and dom_id != CHAT_PANEL_DOM_ID_SUFFIX)
            or THREAD_LAYOUT_DOM_CLASS in _classes(attrs.get("AXDOMClassList"))
        ):
            # Breadth-first order reaches the outer -chat-panel element (when the
            # build has one) before the layout inside it.
            anchors.chat_panel = anchors.chat_panel or element
            in_panel = True

        if role == "AXHeading" and any(value in labels.TURN_HEADINGS for value in label_values) and parent is not None:
            heading_count += 1
            turn_lists.setdefault(id(parent), parent)
            turn_list_ids.add(id(parent))
            descend = False

        if (
            in_panel
            and role == "AXTextArea"
            and COMPOSER_DOM_CLASS in _classes(attrs.get("AXDOMClassList"))
            and labels.COMPOSER.text in label_values
            and not parent_is_turn_list
        ):
            composers.append(element)

        # Don't walk into turn bodies: anchors never live there, and long replies
        # would otherwise dominate the node budget.
        if parent_is_turn_list or (parent is not None and id(parent) in turn_list_ids):
            descend = False

        if descend:
            is_turn_list = id(element) in turn_list_ids
            for child in _elements(attrs.get("AXChildren")):
                queue.append((child, element, in_panel, is_turn_list))

    anchors.nodes_visited = visited
    anchors.search_complete = exhausted
    anchors.turn_heading_count = heading_count
    anchors.composer_candidate_count = len(composers)
    anchors.turn_list_candidate_count = len(turn_lists)
    if len(composers) == 1:
        anchors.composer = composers[0]
    if len(turn_lists) == 1:
        anchors.turn_list = next(iter(turn_lists.values()))
    return anchors


if __name__ == "__main__":  # pragma: no cover - read-only live check, run only when asked
    import json
    import time

    from agent.new_chatgpt_app import CtypesAXBackend, resolve_new_chatgpt_pid

    backend = CtypesAXBackend()
    pid = resolve_new_chatgpt_pid()
    if not pid:
        raise SystemExit("New ChatGPT app is not running.")
    app = backend.application(pid)
    started = time.monotonic()
    found = locate_anchors(backend, app)
    located_at = time.monotonic()
    report = {"pid": pid, "locate_seconds": round(located_at - started, 3), **found.summary(), "regions": {}}
    for name in ("sidebar", "banner", "chat_panel", "turn_list"):
        region_started = time.monotonic()
        snap = snapshot_region(backend, getattr(found, name))
        roles: dict[str, int] = {}
        if snap.root is not None:
            for node in snap.root.iter():
                roles[node.role] = roles.get(node.role, 0) + 1
        report["regions"][name] = {
            "node_count": snap.node_count,
            "complete": snap.complete,
            "seconds": round(time.monotonic() - region_started, 3),
            "top_roles": dict(sorted(roles.items(), key=lambda item: -item[1])[:8]),
        }
    print(json.dumps(report, indent=2))
