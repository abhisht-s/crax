"""Open a bound project chat in the new ChatGPT desktop app (``com.openai.codex``).

Step 3 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``. Standalone; not wired into
the supervision loop yet.

How it works (sidebar structure read live on 2026-11-20):

- The "Projects" sidebar section has a header button "Projects" (``AXExpanded``).
- Each project is an ``AXButton "<project>"`` (``AXExpanded``) that contains an
  ``AXPopUpButton "Project actions for <project>"``. When expanded it is followed
  by ``AXList "Chats in <project>"`` whose items are chat rows
  (``AXButton "<chat>"`` containing ``AXPopUpButton "Chat actions"``), ending with
  ``AXButton "Show more"`` when more chats exist.
- The open conversation's row carries ``AXARIACurrent = "page"``.
- The same chat can also appear under "Pinned" and "Recents", and projects can
  be pinned too. Only rows inside the "Projects" section are ever used.

Only the "Projects" header, the target project, its "Show more" buttons and the
target chat row are ever pressed. The app is brought to the front first (unless
dry-running): presses work in the background, but the AX tree does not update
while the window is off screen, so the switch could not be confirmed. Navigation counts as confirmed only when the
target row is current *and* the window's web area title equals the chat title.
Whether the destination is the right one for a run is still decided by the
read-only destination gate (Step 4), not here.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent import new_chatgpt_labels as labels
from agent.new_chatgpt_app import AXBackend, CtypesAXBackend, PreflightResult, preflight_new_chatgpt
from agent.new_chatgpt_reader import AXNodeData, locate_anchors, snapshot_region

NAVIGATION_METHOD = "ax_sidebar_open_new_chatgpt"

PROJECTS_SECTION = labels.Label("Projects")
PROJECT_ACTIONS = labels.Label("Project actions for {project}")
CHAT_ACTIONS = labels.Label("Chat actions")
ARIA_CURRENT_PAGE = "page"
NAV_SECTION_DOM_CLASS = "group/nav-section"

OUTCOME_ALREADY_OPEN = "already_open"
OUTCOME_OPENED = "opened"
OUTCOME_DRY_RUN_WOULD_OPEN = "dry_run_would_open"
OUTCOME_DRY_RUN_NEEDS_PRESS = "dry_run_needs_press"
OUTCOME_FAILED = "failed"

REASON_INVALID_BINDING = "new_chatgpt_nav_invalid_binding"
REASON_SIDEBAR_NOT_FOUND = "new_chatgpt_nav_sidebar_not_found"
REASON_SIDEBAR_INCOMPLETE = "new_chatgpt_nav_sidebar_incomplete"
REASON_PROJECTS_SECTION_NOT_FOUND = "new_chatgpt_nav_projects_section_not_found"
REASON_PROJECT_NOT_FOUND = "new_chatgpt_nav_project_not_found"
REASON_PROJECT_AMBIGUOUS = "new_chatgpt_nav_project_ambiguous"
REASON_CHATS_LIST_NOT_FOUND = "new_chatgpt_nav_project_chats_list_not_found"
REASON_CHATS_LIST_AMBIGUOUS = "new_chatgpt_nav_project_chats_list_ambiguous"
REASON_CHAT_NOT_FOUND = "new_chatgpt_nav_chat_not_found"
REASON_CHAT_AMBIGUOUS = "new_chatgpt_nav_chat_ambiguous"
REASON_PRESS_FAILED = "new_chatgpt_nav_press_failed"
REASON_NOT_CONFIRMED = "new_chatgpt_nav_not_confirmed"
REASON_STEP_LIMIT = "new_chatgpt_nav_step_limit"

DEFAULT_MAX_SHOW_MORE_PRESSES = 5
DEFAULT_SETTLE_SECONDS = 0.4
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 5.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25


@dataclass
class NavigationResult:
    ok: bool
    outcome: str = OUTCOME_FAILED
    reason_code: str | None = None
    error: str | None = None
    method: str = NAVIGATION_METHOD
    dry_run: bool = False
    preflight: dict | None = None
    presses: list[str] = field(default_factory=list)
    show_more_presses: int = 0
    project_row_count: int = 0
    chat_row_count: int = 0
    confirmed: bool = False
    confirm_wait_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _SidebarView:
    """What the sidebar shows for one project, from a single snapshot."""

    section_header: AXNodeData | None
    section: AXNodeData | None
    project_rows: tuple[AXNodeData, ...] = ()
    chat_lists: tuple[AXNodeData, ...] = ()
    chat_rows: tuple[AXNodeData, ...] = ()
    chats_show_more: AXNodeData | None = None
    projects_show_more: AXNodeData | None = None


def open_project_chat(
    project_title: str,
    chat_title: str,
    *,
    dry_run: bool = False,
    bring_to_front: bool | None = None,
    backend: AXBackend | None = None,
    preflight: Callable[..., PreflightResult] = preflight_new_chatgpt,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_show_more_presses: int = DEFAULT_MAX_SHOW_MORE_PRESSES,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> NavigationResult:
    """Open ``chat_title`` inside ``project_title`` from the sidebar's Projects section.

    ``dry_run=True`` presses nothing; it reports whether the chat row is visible and
    would be pressed, or which press would be needed first.

    ``bring_to_front`` defaults to ``not dry_run``: the press works in the
    background, but the AX tree only updates while the window is on screen, so
    confirming the switch needs the app in front (seen live 2026-11-20). A dry
    run stays read-only and does not change focus.
    """

    result = NavigationResult(ok=False, dry_run=dry_run)
    project = project_title.strip() if isinstance(project_title, str) else ""
    chat = chat_title.strip() if isinstance(chat_title, str) else ""
    if not project or not chat:
        return _fail(result, REASON_INVALID_BINDING, "Both a project title and a chat title are required.")

    try:
        if backend is None:
            backend = CtypesAXBackend()
        if bring_to_front is None:
            bring_to_front = not dry_run
        pre = preflight(backend=backend, sleep=sleep, clock=clock, bring_to_front=bring_to_front)
        result.preflight = pre.to_dict()
        if not pre.ok:
            return _fail(result, pre.reason_code or REASON_SIDEBAR_NOT_FOUND, pre.error or "Preflight failed.")
        app = backend.application(pre.pid)
        return _navigate(
            result,
            backend=backend,
            app=app,
            project=project,
            chat=chat,
            sleep=sleep,
            clock=clock,
            max_show_more_presses=max_show_more_presses,
            settle_seconds=settle_seconds,
            confirm_timeout_seconds=confirm_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed with evidence
        return _fail(result, "new_chatgpt_ax_error", f"{type(exc).__name__}: {exc}")


def _navigate(
    result: NavigationResult,
    *,
    backend: AXBackend,
    app: Any,
    project: str,
    chat: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    max_show_more_presses: int,
    settle_seconds: float,
    confirm_timeout_seconds: float,
    poll_interval_seconds: float,
) -> NavigationResult:
    # Each pass reads the sidebar fresh, then does at most one press that reveals
    # more of it (section header, project, or a "Show more"), or opens the chat.
    max_passes = max_show_more_presses * 2 + 4
    for _ in range(max_passes):
        view, failure = _read_view(backend, app, project, chat)
        if failure:
            return _fail(result, *failure)
        result.project_row_count = len(view.project_rows)
        result.chat_row_count = len(view.chat_rows)

        if view.section_header is not None and view.section_header.expanded is False:
            if not _reveal(result, backend, view.section_header, "projects_section", sleep, settle_seconds):
                return result
            continue

        if len(view.project_rows) > 1:
            return _fail(result, REASON_PROJECT_AMBIGUOUS, f"{len(view.project_rows)} projects named {project!r} in the Projects section.")
        if not view.project_rows:
            if view.projects_show_more is not None and result.show_more_presses < max_show_more_presses:
                result.show_more_presses += 1
                if not _reveal(result, backend, view.projects_show_more, "projects_show_more", sleep, settle_seconds):
                    return result
                continue
            return _fail(result, REASON_PROJECT_NOT_FOUND, f"No project named {project!r} in the Projects section.")

        project_row = view.project_rows[0]
        if len(view.chat_lists) > 1:
            return _fail(result, REASON_CHATS_LIST_AMBIGUOUS, f"{len(view.chat_lists)} chat lists for project {project!r}.")
        if not view.chat_lists:
            if project_row.expanded is False and "project" not in result.presses:
                if not _reveal(result, backend, project_row, "project", sleep, settle_seconds):
                    return result
                continue
            return _fail(result, REASON_CHATS_LIST_NOT_FOUND, f"Project {project!r} shows no chat list.")

        if len(view.chat_rows) > 1:
            return _fail(result, REASON_CHAT_AMBIGUOUS, f"{len(view.chat_rows)} chats named {chat!r} in project {project!r}.")
        if not view.chat_rows:
            if view.chats_show_more is not None and result.show_more_presses < max_show_more_presses:
                result.show_more_presses += 1
                if not _reveal(result, backend, view.chats_show_more, "chats_show_more", sleep, settle_seconds):
                    return result
                continue
            return _fail(result, REASON_CHAT_NOT_FOUND, f"No chat named {chat!r} in project {project!r}.")

        row = view.chat_rows[0]
        if row.aria_current == ARIA_CURRENT_PAGE and _web_area_title(backend, app) == chat:
            result.ok, result.outcome, result.confirmed = True, OUTCOME_ALREADY_OPEN, True
            return result
        if result.dry_run:
            result.ok, result.outcome = True, OUTCOME_DRY_RUN_WOULD_OPEN
            return result

        error = backend.perform(row.element, "AXPress")
        result.presses.append("chat")
        if error != 0:
            return _fail(result, REASON_PRESS_FAILED, f"AXPress on the chat row returned AXError {error}.")
        return _confirm(result, backend, app, project, chat, sleep, clock, confirm_timeout_seconds, poll_interval_seconds)

    return _fail(result, REASON_STEP_LIMIT, "Gave up after too many sidebar steps.")


def _reveal(
    result: NavigationResult,
    backend: AXBackend,
    node: AXNodeData,
    what: str,
    sleep: Callable[[float], None],
    settle_seconds: float,
) -> bool:
    """Press something that reveals more sidebar; False (with result filled) to stop."""

    if result.dry_run:
        result.ok, result.outcome = True, OUTCOME_DRY_RUN_NEEDS_PRESS
        result.presses.append(f"would_press:{what}")
        return False
    error = backend.perform(node.element, "AXPress")
    result.presses.append(what)
    if error != 0:
        _fail(result, REASON_PRESS_FAILED, f"AXPress on {what} returned AXError {error}.")
        return False
    sleep(settle_seconds)
    return True


def _confirm(
    result: NavigationResult,
    backend: AXBackend,
    app: Any,
    project: str,
    chat: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> NavigationResult:
    started = clock()
    while True:
        view, failure = _read_view(backend, app, project, chat)
        if not failure and len(view.chat_rows) == 1 and view.chat_rows[0].aria_current == ARIA_CURRENT_PAGE:
            if _web_area_title(backend, app) == chat:
                result.ok, result.outcome, result.confirmed = True, OUTCOME_OPENED, True
                result.confirm_wait_seconds = round(max(clock() - started, 0.0), 3)
                return result
        if clock() - started >= timeout_seconds:
            result.confirm_wait_seconds = round(max(clock() - started, 0.0), 3)
            return _fail(result, REASON_NOT_CONFIRMED, "The chat row was pressed but the app did not show that chat as open.")
        sleep(poll_interval_seconds)


def _web_area_title(backend: AXBackend, app: Any) -> str:
    return locate_anchors(backend, app).web_area_title


def _read_view(backend: AXBackend, app: Any, project: str, chat: str) -> tuple[_SidebarView, tuple[str, str] | None]:
    anchors = locate_anchors(backend, app)
    if anchors.sidebar is None:
        return _SidebarView(None, None), (REASON_SIDEBAR_NOT_FOUND, "The app sidebar was not found.")
    snapshot = snapshot_region(backend, anchors.sidebar)
    if not snapshot.complete:
        return _SidebarView(None, None), (REASON_SIDEBAR_INCOMPLETE, "The sidebar could not be read completely.")
    return _project_view(snapshot.root, project, chat)


def find_projects_section(sidebar: AXNodeData) -> tuple[AXNodeData, AXNodeData] | None:
    """Return ``(header, section)`` for the sidebar's "Projects" section, or None.

    None when there isn't exactly one "Projects" header button (with ``AXExpanded``).
    """

    headers = [
        node
        for node in sidebar.iter()
        if node.role == "AXButton" and node.has_label(PROJECTS_SECTION.text) and node.expanded is not None
    ]
    if len(headers) != 1:
        return None
    return headers[0], _section_of(headers[0], _parent_map(sidebar))


def is_project_row(node: AXNodeData) -> bool:
    """A project button: labelled ``<project>`` and holding "Project actions for <project>"."""

    return (
        node.role == "AXButton"
        and bool(node.label)
        and node.find_first(lambda n: n.role == "AXPopUpButton" and n.has_label(PROJECT_ACTIONS.text.format(project=node.label)))
        is not None
    )


def is_chat_row(node: AXNodeData) -> bool:
    """A chat row: a labelled button holding a "Chat actions" menu button."""

    return (
        node.role == "AXButton"
        and bool(node.label)
        and node.find_first(lambda n: n.role == "AXPopUpButton" and n.has_label(CHAT_ACTIONS.text)) is not None
    )


def _project_view(sidebar: AXNodeData, project: str, chat: str) -> tuple[_SidebarView, tuple[str, str] | None]:
    found = find_projects_section(sidebar)
    if found is None:
        count = sum(
            1 for node in sidebar.iter() if node.role == "AXButton" and node.has_label(PROJECTS_SECTION.text) and node.expanded is not None
        )
        return _SidebarView(None, None), (
            REASON_PROJECTS_SECTION_NOT_FOUND,
            f"Expected one 'Projects' section header, found {count}.",
        )
    header, section = found
    if header.expanded is False:
        return _SidebarView(header, section), None

    project_rows = tuple(node for node in section.iter() if node.has_label(project) and is_project_row(node))
    list_label = labels.PROJECT_CHATS_LIST.text.format(project=project)
    chat_lists = tuple(node for node in section.iter() if node.role == "AXList" and node.has_label(list_label))
    chat_rows: tuple[AXNodeData, ...] = ()
    chats_show_more = None
    if len(chat_lists) == 1:
        chat_rows = tuple(node for node in chat_lists[0].iter() if node.has_label(chat) and is_chat_row(node))
        chats_show_more = _last_show_more(chat_lists[0])
    projects_show_more = _projects_show_more(section)
    return (
        _SidebarView(
            section_header=header,
            section=section,
            project_rows=project_rows,
            chat_lists=chat_lists,
            chat_rows=chat_rows,
            chats_show_more=chats_show_more,
            projects_show_more=projects_show_more,
        ),
        None,
    )


def _parent_map(root: AXNodeData) -> dict[int, AXNodeData]:
    parents: dict[int, AXNodeData] = {}
    for node in root.iter():
        for child in node.children:
            parents[id(child)] = node
    return parents


def _section_of(header: AXNodeData, parents: dict[int, AXNodeData]) -> AXNodeData:
    """The nav section that holds ``header``: nearest ``group/nav-section`` ancestor, else the parent."""

    node = parents.get(id(header))
    while node is not None:
        if NAV_SECTION_DOM_CLASS in node.dom_classes:
            return node
        node = parents.get(id(node))
    return parents.get(id(header), header)


def _is_show_more(node: AXNodeData) -> bool:
    return node.role == "AXButton" and node.has_label(labels.SHOW_MORE.text)


def _last_show_more(container: AXNodeData) -> AXNodeData | None:
    found = [node for node in container.iter() if _is_show_more(node)]
    return found[-1] if found else None


def _projects_show_more(section: AXNodeData) -> AXNodeData | None:
    """A "Show more" in the Projects section that is not inside any project's chat list."""

    inside_lists: set[int] = set()
    for node in section.iter():
        if node.role == "AXList" and labels.PROJECT_CHATS_LIST.parse(node.label):
            inside_lists.update(id(item) for item in node.iter())
    candidates = [node for node in section.iter() if _is_show_more(node) and id(node) not in inside_lists]
    return candidates[-1] if candidates else None


def _fail(result: NavigationResult, reason_code: str, error: str) -> NavigationResult:
    result.ok = False
    result.outcome = OUTCOME_FAILED
    result.reason_code = reason_code
    result.error = error
    return result


if __name__ == "__main__":  # pragma: no cover - live use, run only when asked
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Open a project chat in the new ChatGPT app.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Press nothing; report what would be pressed.")
    args = parser.parse_args()
    outcome = open_project_chat(args.project, args.chat, dry_run=args.dry_run).to_dict()
    outcome.pop("preflight", None)
    print(json.dumps(outcome, indent=2))
