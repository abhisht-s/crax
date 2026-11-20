"""Destination snapshot adapter for the new ChatGPT desktop app (``com.openai.codex``).

Step 4 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``. Produces the existing
:class:`agent.chatgpt_destination_gate.ChatGPTDestinationSnapshot`, so the gate
logic in ``agent/chatgpt_destination_gate.py`` is reused unchanged. Standalone;
not wired into the supervision loop yet.

Evidence sources (all read live on 2026-11-20):

- **Chat:** the web area title, the window title and the title-bar
  ``AXPopUpButton`` all equal the open conversation's title.
- **Project:** the app shows the open chat's project nowhere outside the
  sidebar. The project is the one whose ``"Chats in <project>"`` list (inside
  the sidebar's "Projects" section) holds the row with ``AXARIACurrent = "page"``.
  Older builds also had a title-bar ``AXImage "Project: <name>"``; when present
  it must agree.

The new app has no authoritative "<chat>, <project>" source like Classic's
window title item, so ``active_conversation_chat_title`` and
``active_conversation_project_title`` are deliberately left empty. That keeps
the gate on its strict path: project candidate, current chat row, title-bar
header, composer, transcript and conversation surface must all agree.

The adapter runs preflight first, by default with ``bring_to_front=True``,
because the AX tree goes stale while the window is off screen. It never
presses anything. Preflight may focus the composer to close a stray menu, as
documented in Step 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent import new_chatgpt_labels as labels
from agent.chatgpt_destination_gate import ChatGPTDestinationSnapshot, DestinationEvidenceCandidate
from agent.new_chatgpt_app import (
    REASON_ACCESSIBILITY_NOT_TRUSTED,
    REASON_AX_NOT_ENABLED,
    REASON_NO_APP_ELEMENT,
    REASON_NOT_FRONTMOST,
    REASON_NOT_RUNNING,
    REASON_WINDOW_NOT_ATTACHED,
    AXBackend,
    CtypesAXBackend,
    PreflightResult,
    _window,
    preflight_new_chatgpt,
)
from agent.new_chatgpt_navigation import (
    ARIA_CURRENT_PAGE,
    find_projects_section,
    is_chat_row,
    is_project_row,
)
from agent.new_chatgpt_reader import AXNodeData, locate_anchors, snapshot_region

ADAPTER_METHOD = "ax_destination_snapshot_new_chatgpt"
DEFAULT_STABILITY_READS = 2
DEFAULT_STABILITY_INTERVAL_SECONDS = 0.2

# Title-bar buttons that are never the conversation title.
_BANNER_CONTROL_LABELS = frozenset({"Back", "Forward", "Hide sidebar", "Show sidebar", "Share", "More", "New tab", "Toggle pinned summary"})


@dataclass(frozen=True)
class _Observation:
    """One read of the destination evidence. ``signature`` drives the stability check."""

    complete: bool
    truncated: bool
    uncertainty: tuple[str, ...]
    web_area_title: str
    window_title: str
    project_candidates: tuple[DestinationEvidenceCandidate, ...]
    chat_row_candidates: tuple[DestinationEvidenceCandidate, ...]
    header_candidates: tuple[DestinationEvidenceCandidate, ...]
    composer_count: int
    transcript_available: bool
    surface_available: bool
    evidence: dict = field(default_factory=dict, compare=False)

    @property
    def signature(self) -> tuple:
        return (
            self.complete,
            self.truncated,
            self.uncertainty,
            self.web_area_title,
            self.window_title,
            self.project_candidates,
            self.chat_row_candidates,
            self.header_candidates,
            self.composer_count,
            self.transcript_available,
            self.surface_available,
        )


class NewChatGPTDestinationSnapshotAdapter:
    """``ChatGPTDestinationReadOnlyAdapter`` for the new ChatGPT app."""

    def __init__(
        self,
        *,
        backend: AXBackend | None = None,
        preflight: Callable[..., PreflightResult] = preflight_new_chatgpt,
        bring_to_front: bool = True,
        stability_reads: int = DEFAULT_STABILITY_READS,
        stability_interval_seconds: float = DEFAULT_STABILITY_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._backend = backend
        self._preflight = preflight
        self._bring_to_front = bring_to_front
        self._stability_reads = max(1, stability_reads)
        self._stability_interval_seconds = stability_interval_seconds
        self._sleep = sleep
        self.last_evidence: dict = {}

    def read_destination_snapshot(self) -> ChatGPTDestinationSnapshot:
        backend = self._backend or CtypesAXBackend()
        pre = self._preflight(backend=backend, sleep=self._sleep, bring_to_front=self._bring_to_front)
        self.last_evidence = {"method": ADAPTER_METHOD, "preflight": pre.to_dict()}
        if not pre.ok:
            return _snapshot_for_failed_preflight(pre)

        app = backend.application(pre.pid)
        observations: list[_Observation] = []
        for index in range(self._stability_reads):
            if index:
                self._sleep(self._stability_interval_seconds)
            observations.append(_observe(backend, app))
        primary = observations[-1]
        stable = all(item.signature == primary.signature for item in observations)
        self.last_evidence.update(primary.evidence)
        self.last_evidence["stable"] = stable
        self.last_evidence["uncertainty"] = list(primary.uncertainty)

        return ChatGPTDestinationSnapshot(
            process_running=True,
            window_available=True,
            accessibility_available=True,
            snapshot_stable=stable,
            snapshot_complete=primary.complete,
            ax_tree_truncated=primary.truncated,
            uncertainty_present=bool(primary.uncertainty),
            active_project_candidates=primary.project_candidates,
            selected_chat_row_candidates=primary.chat_row_candidates,
            conversation_header_candidates=primary.header_candidates,
            composer_available=primary.composer_count == 1,
            transcript_available=primary.transcript_available,
            conversation_surface_available=primary.surface_available,
            composer_candidate_count=primary.composer_count,
            conversation_surface_candidate_count=1 if primary.surface_available else 0,
            # Deliberately empty: no authoritative "<chat>, <project>" source exists
            # in the new app, so the gate must use its strict evidence path.
            active_conversation_chat_title="",
            active_conversation_project_title="",
        )


def _snapshot_for_failed_preflight(pre: PreflightResult) -> ChatGPTDestinationSnapshot:
    reason = pre.reason_code
    if reason == REASON_NOT_RUNNING:
        return ChatGPTDestinationSnapshot(False, False, False, False, False)
    if reason in (REASON_ACCESSIBILITY_NOT_TRUSTED, REASON_AX_NOT_ENABLED, REASON_NO_APP_ELEMENT):
        return ChatGPTDestinationSnapshot(True, True, False, False, False)
    if reason in (REASON_WINDOW_NOT_ATTACHED, REASON_NOT_FRONTMOST):
        return ChatGPTDestinationSnapshot(True, False, True, False, False)
    # Wrong product mode, unknown mode, a menu that stays open, or an AX error:
    # the app is reachable but its state can't be trusted.
    return ChatGPTDestinationSnapshot(True, True, True, False, False, uncertainty_present=True)


def _observe(backend: AXBackend, app: Any) -> _Observation:
    anchors = locate_anchors(backend, app)
    window = _window(backend, app)
    window_title = backend.attribute(window, "AXTitle") if window is not None else None
    window_title = window_title.strip() if isinstance(window_title, str) else ""
    web_title = anchors.web_area_title.strip()
    uncertainty: list[str] = []

    sidebar = snapshot_region(backend, anchors.sidebar)
    banner = snapshot_region(backend, anchors.banner)
    complete = anchors.search_complete and sidebar.complete and banner.complete
    # A region that was found but cut off by a walk limit. A missing region
    # makes the snapshot incomplete instead.
    truncated = any(region.root is not None and not region.complete for region in (sidebar, banner))

    project_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    chat_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    active_projects: list[str] = []
    if sidebar.root is not None:
        project_candidates, chat_candidates, active_projects, sidebar_issues = _sidebar_evidence(sidebar.root, web_title)
        uncertainty += sidebar_issues

    header_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    banner_projects: list[str] = []
    if banner.root is not None:
        header_candidates, banner_projects = _banner_evidence(banner.root, web_title, window_title)
    if len(banner_projects) > 1 or (banner_projects and banner_projects != active_projects):
        uncertainty.append("banner_project_disagrees_with_sidebar")
    if web_title and window_title and web_title != window_title:
        uncertainty.append("window_title_disagrees_with_web_area")

    evidence = {
        "anchors": anchors.summary(),
        "sidebar_nodes": sidebar.node_count,
        "banner_nodes": banner.node_count,
        "active_project_count": len(active_projects),
        "current_chat_row_count": sum(1 for c in chat_candidates if c.active),
        "header_candidate_count": len(header_candidates),
        "banner_project_label_present": bool(banner_projects),
    }
    return _Observation(
        complete=complete,
        truncated=truncated,
        uncertainty=tuple(uncertainty),
        web_area_title=web_title,
        window_title=window_title,
        project_candidates=project_candidates,
        chat_row_candidates=chat_candidates,
        header_candidates=header_candidates,
        composer_count=anchors.composer_candidate_count,
        transcript_available=anchors.turn_list is not None,
        surface_available=anchors.chat_panel is not None,
        evidence=evidence,
    )


def _sidebar_evidence(
    sidebar: AXNodeData, web_title: str
) -> tuple[tuple[DestinationEvidenceCandidate, ...], tuple[DestinationEvidenceCandidate, ...], list[str], list[str]]:
    issues: list[str] = []
    found = find_projects_section(sidebar)
    if found is None:
        return (), (), [], ["projects_section_not_found"]
    _header, section = found

    project_names = [node.label for node in section.iter() if is_project_row(node)]
    lists: dict[str, list[AXNodeData]] = {}
    for node in section.iter():
        if node.role == "AXList":
            name = labels.PROJECT_CHATS_LIST.parse(node.label)
            if name:
                lists.setdefault(name, []).append(node)

    # Which project lists hold the current row (aria-current="page")?
    current_by_project: dict[str, list[AXNodeData]] = {}
    for name, nodes in lists.items():
        for chat_list in nodes:
            for row in chat_list.iter():
                if is_chat_row(row) and row.aria_current == ARIA_CURRENT_PAGE:
                    current_by_project.setdefault(name, []).append(row)
    active_projects = sorted(current_by_project)
    current_rows = [row for rows in current_by_project.values() for row in rows]
    if len(current_rows) > 1:
        issues.append("multiple_current_rows_in_projects_section")

    project_candidates = tuple(
        DestinationEvidenceCandidate(
            title=name,
            active=name in current_by_project,
            selected=False,
            identity_confirmed=True,  # the row carries "Project actions for <name>"
            actionable_destination_evidence=True,
            project_chats_list_confirmed=(len(lists.get(name, [])) == 1 and name in current_by_project) if name in current_by_project else None,
        )
        for name in project_names
    )

    chat_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    if len(active_projects) == 1 and len(lists.get(active_projects[0], [])) == 1:
        chat_list = lists[active_projects[0]][0]
        chat_candidates = tuple(
            DestinationEvidenceCandidate(
                title=row.label,
                active=row.aria_current == ARIA_CURRENT_PAGE,
                selected=row.aria_current == ARIA_CURRENT_PAGE,
                identity_confirmed=row.aria_current == ARIA_CURRENT_PAGE and bool(web_title) and row.label == web_title,
                actionable_destination_evidence=True,
            )
            for row in chat_list.iter()
            if is_chat_row(row)
        )
    return project_candidates, chat_candidates, active_projects, issues


def _banner_evidence(
    banner: AXNodeData, web_title: str, window_title: str
) -> tuple[tuple[DestinationEvidenceCandidate, ...], list[str]]:
    headers = []
    for node in banner.iter():
        if node.role not in ("AXPopUpButton", "AXButton") or not node.label or node.label in _BANNER_CONTROL_LABELS:
            continue
        # 2026-11-20 build: the title is an AXPopUpButton. 2026-11-18 build: an
        # AXButton next to "Project: <name>". Accept either, only when it names the open chat.
        if node.role == "AXButton" and node.label != web_title:
            continue
        confirmed = bool(web_title) and node.label == web_title and (not window_title or window_title == web_title)
        headers.append(
            DestinationEvidenceCandidate(
                title=node.label,
                active=True,
                selected=False,
                identity_confirmed=confirmed,
                actionable_destination_evidence=True,
            )
        )
    projects = [
        name
        for node in banner.iter()
        if node.role == "AXImage" and (name := labels.BANNER_PROJECT.parse(node.label))
    ]
    return tuple(headers), projects


if __name__ == "__main__":  # pragma: no cover - read-only live check, run only when asked
    import argparse
    import json
    from dataclasses import asdict

    from agent.chatgpt_destination_gate import DestinationLeaseContext, verify_chatgpt_destination_snapshot

    parser = argparse.ArgumentParser(description="Check the new ChatGPT app is on the bound project chat (read-only).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--no-bring-to-front", action="store_true")
    args = parser.parse_args()

    @dataclass(frozen=True)
    class _Binding:
        project_title: str
        chat_title: str

    adapter = NewChatGPTDestinationSnapshotAdapter(bring_to_front=not args.no_bring_to_front)
    lease = DestinationLeaseContext(owning_run_id="live-check", lease_token="live-check", owner_pid=0, acquired_at="now")
    gate = verify_chatgpt_destination_snapshot(
        run_id="live-check", binding=_Binding(args.project, args.chat), lease_context=lease, adapter=adapter
    )
    evidence = dict(adapter.last_evidence)
    evidence.pop("preflight", None)
    print(json.dumps({"gate_ok": gate.ok, "state": gate.state, "reason_code": gate.reason_code,
                      "evidence_summary": asdict(gate.evidence_summary), "adapter_evidence": evidence}, indent=2))
