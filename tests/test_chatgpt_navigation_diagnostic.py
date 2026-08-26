from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from agent import chatgpt_navigation_diagnostic as nav
from agent import cli


class _FakeReader:
    def __init__(
        self,
        snapshots: list[nav.AXElementSnapshot],
        stats: dict | None = None,
        window_metadata: dict | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.stats = stats or {
            "visited_nodes": len(snapshots),
            "max_depth": 16,
            "max_nodes": 900,
            "truncated_by_node_limit": False,
            "truncated_by_depth_limit": False,
        }
        self.window_metadata = window_metadata or {"window_source": "synthetic"}

    def collect(self, pid: int) -> tuple[list[nav.AXElementSnapshot], dict, dict]:
        return self.snapshots, self.stats, self.window_metadata


class _Factory:
    def __init__(self, reader: _FakeReader) -> None:
        self.reader = reader
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, app_name: str, max_depth: int, max_nodes: int) -> _FakeReader:
        self.calls.append((app_name, max_depth, max_nodes))
        return self.reader


class _ActionReader:
    def __init__(self, snapshots_by_collect: list[list[nav.AXElementSnapshot]]) -> None:
        self.snapshots_by_collect = snapshots_by_collect
        self.collect_calls = 0
        self.actions: list[tuple[str, str]] = []
        self.action_contexts: list[dict | None] = []

    def collect(self, pid: int) -> tuple[list[nav.AXElementSnapshot], dict, dict]:
        index = min(self.collect_calls, len(self.snapshots_by_collect) - 1)
        snapshots = self.snapshots_by_collect[index]
        self.collect_calls += 1
        return snapshots, {
            "visited_nodes": len(snapshots),
            "max_depth": 16,
            "max_nodes": 900,
            "truncated_by_node_limit": False,
            "truncated_by_depth_limit": False,
        }, {"window_source": "synthetic", "window": snapshots[0] if snapshots else None}

    def perform_action(self, path: str, action: str, *, action_context: dict | None = None) -> bool:
        self.actions.append((path, action))
        self.action_contexts.append(action_context)
        if action == "AXPress":
            for index in range(self.collect_calls, len(self.snapshots_by_collect)):
                snapshots = self.snapshots_by_collect[index]
                if any(snapshot.identifier == "conversation-content" for snapshot in snapshots):
                    self.collect_calls = index
                    break
        return True


class _ActionFactory:
    def __init__(self, reader: _ActionReader) -> None:
        self.reader = reader

    def __call__(self, app_name: str, max_depth: int, max_nodes: int) -> _ActionReader:
        return self.reader


class _ClickService:
    def __init__(self, permitted: bool = True) -> None:
        self.permitted = permitted
        self.clicks: list[tuple[float, float]] = []

    def has_permission(self) -> bool:
        return self.permitted

    def left_click(self, x: float, y: float) -> dict:
        self.clicks.append((x, y))
        return {
            "ok": True,
            "error": "",
            "actions_performed": [
                {"event": "left_mouse_down", "x": x, "y": y},
                {"event": "left_mouse_up", "x": x, "y": y},
            ],
        }


class _ClickFactory:
    def __init__(self, service: _ClickService) -> None:
        self.service = service

    def __call__(self) -> _ClickService:
        return self.service


class _ScrollService:
    def __init__(self, permitted: bool = True) -> None:
        self.permitted = permitted
        self.scrolls: list[tuple[float, float, int]] = []

    def has_permission(self) -> bool:
        return self.permitted

    def scroll_down(self, x: float, y: float, delta_y: int) -> dict:
        self.scrolls.append((x, y, delta_y))
        return {
            "ok": True,
            "error": "",
            "actions_performed": [
                {"event": "scroll_wheel", "x": x, "y": y, "delta_y": delta_y},
            ],
        }


class _ScrollFactory:
    def __init__(self, service: _ScrollService) -> None:
        self.service = service

    def __call__(self) -> _ScrollService:
        return self.service


class _CalibrationReader(_ActionReader):
    def __init__(self, snapshots_by_collect: list[list[nav.AXElementSnapshot]], hit_result: dict) -> None:
        super().__init__(snapshots_by_collect)
        self.hit_result = hit_result
        self.hit_tests: list[tuple[int, tuple[float, float], str]] = []

    def hit_test_at_position(self, pid: int, point: tuple[float, float], requested_title: str) -> dict:
        self.hit_tests.append((pid, point, requested_title))
        return self.hit_result


class _AutonomousReader(_ActionReader):
    def __init__(self, snapshots_by_collect: list[list[nav.AXElementSnapshot]], hit_result: dict) -> None:
        super().__init__(snapshots_by_collect)
        self.hit_result = hit_result
        self.hit_tests: list[tuple[int, tuple[float, float], str]] = []

    def hit_test_at_position(self, pid: int, point: tuple[float, float], requested_title: str) -> dict:
        self.hit_tests.append((pid, point, requested_title))
        return self.hit_result


class _TimedActionReader(_AutonomousReader):
    def __init__(self, snapshots_by_collect: list[list[nav.AXElementSnapshot]], hit_result: dict) -> None:
        super().__init__(snapshots_by_collect, hit_result)
        self.action_collect_calls: list[int] = []

    def perform_action(self, path: str, action: str, *, action_context: dict | None = None) -> bool:
        self.action_collect_calls.append(self.collect_calls)
        return super().perform_action(path, action, action_context=action_context)


class _FailingAlignmentReader(_TimedActionReader):
    def perform_action(self, path: str, action: str, *, action_context: dict | None = None) -> bool:
        if action == "AXScrollToVisible":
            self.action_collect_calls.append(self.collect_calls)
            self.actions.append((path, action))
            self.action_contexts.append(action_context)
            return False
        return super().perform_action(path, action, action_context=action_context)


class _AXPerformActionRecorder:
    def __init__(self, error_code: int = 0) -> None:
        self.calls: list[tuple[int | None, int | None]] = []
        self.error_code = error_code

    def AXUIElementPerformAction(self, element: object, action_ref: object) -> int:
        self.calls.append((getattr(element, "value", None), getattr(action_ref, "value", None)))
        return self.error_code


class _DisplayProbe:
    def __init__(
        self,
        cursor: tuple[float, float] = (92.5, 128.0),
        primary: tuple[float, float, float, float] = (0, 0, 1920, 1080),
    ) -> None:
        self.cursor = cursor
        self.primary = primary

    def current_mouse_location(self) -> tuple[float, float]:
        return self.cursor

    def primary_display_bounds(self) -> tuple[float, float, float, float]:
        return self.primary

    def display_bounds_containing_point(self, point: tuple[float, float]) -> tuple[float, float, float, float] | None:
        return self.primary if nav._point_inside_frame(point, self.primary) else None


class _DisplayFactory:
    def __init__(self, probe: _DisplayProbe) -> None:
        self.probe = probe

    def __call__(self) -> _DisplayProbe:
        return self.probe


class _WindowServerProbe:
    def __init__(self, windows: list[dict]) -> None:
        self.windows = windows

    def visible_windows_for_pid(self, pid: int) -> list[dict]:
        return [
            {
                **window,
                "owner_pid": pid,
                "onscreen": window.get("onscreen", True),
                "layer": window.get("layer", 0),
            }
            for window in self.windows
        ]


class _WindowServerFactory:
    def __init__(self, probe: _WindowServerProbe) -> None:
        self.probe = probe

    def __call__(self) -> _WindowServerProbe:
        return self.probe


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _LiveOutputRecorder:
    def __init__(self) -> None:
        self.blocks: list[list[str]] = []

    def __call__(self, lines: list[str]) -> None:
        self.blocks.append(list(lines))

    @property
    def lines(self) -> list[str]:
        return [line for block in self.blocks for line in block]


class _TreeAdapter:
    def __init__(self, children_by_node: dict[str, list[str]]) -> None:
        self.children_by_node = children_by_node

    def snapshot(self, element: object, path: str, depth: int) -> nav.AXElementSnapshot:
        return nav.AXElementSnapshot(path=path, depth=depth, role="AXGroup", title=str(element))

    def children(self, element: object) -> list[object]:
        return self.children_by_node.get(str(element), [])


def _synthetic_snapshots() -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="Private Tax Chat"),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXGroup", identifier="sidebar"),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXButton", title="New chat", actions=("AXPress",)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXTextField", title="Search chats", value="composer secret"),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXList", title="History"),
        nav.AXElementSnapshot(path="W.1.3.1", depth=3, role="AXButton", title="Vacation plan with Alice", actions=("AXPress",)),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXList", title="Projects"),
        nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXStaticText", value="Super Secret Work Project"),
        nav.AXElementSnapshot(path="W.2", depth=1, role="AXHeading", value="Current Chat Sensitive Title"),
        nav.AXElementSnapshot(path="W.3", depth=1, role="AXTextArea", value="draft composer text"),
    ]


def _large_text_snapshots() -> list[nav.AXElementSnapshot]:
    long_text = "X" * 10_000
    snapshots = [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT"),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXList", title="History"),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXButton", title="Sensitive chat label", actions=("AXPress",)),
        nav.AXElementSnapshot(path="W.2", depth=1, role="AXTextField", title="Search", subrole="AXSearchField"),
        nav.AXElementSnapshot(path="W.3", depth=1, role="AXStaticText", value=long_text),
    ]
    for index in range(20):
        snapshots.append(
            nav.AXElementSnapshot(
                path=f"W.1.{index + 2}",
                depth=2,
                role="AXButton",
                title=f"Sensitive chat row {index}",
                actions=("AXPress",),
            )
        )
    return snapshots


def _visible_navigation_title_snapshots() -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT"),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXList", title="History", subrole="AXSectionList"),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXButton", title="Trip Planning", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXGroup", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.2.1", depth=3, role="AXStaticText", value="Nested Budget Chat", enabled=True),
        nav.AXElementSnapshot(path="W.2", depth=1, role="AXList", title="Projects", subrole="AXCollectionList"),
        nav.AXElementSnapshot(path="W.2.1", depth=2, role="AXGroup", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.2.1.1", depth=3, role="AXStaticText", value="Launch Plan", enabled=True),
        nav.AXElementSnapshot(path="W.3", depth=1, role="AXStaticText", value="Outside Visible Title"),
        nav.AXElementSnapshot(path="W.4", depth=1, role="AXList", title="History"),
        nav.AXElementSnapshot(path="W.4.1", depth=2, role="AXStaticText", value="M" * 181),
        nav.AXElementSnapshot(path="W.4.2", depth=2, role="AXStaticText", value="assistant: this is a message body"),
        nav.AXElementSnapshot(path="W.4.3", depth=2, role="AXTextArea", value="Draft composer visible title"),
        nav.AXElementSnapshot(path="W.5", depth=1, role="AXList", title="Search results"),
        nav.AXElementSnapshot(path="W.5.1", depth=2, role="AXStaticText", value="Search Result Chat"),
        nav.AXElementSnapshot(path="W.6", depth=1, role="AXButton", title="New chat", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.7", depth=1, role="AXButton", title="Search", actions=("AXPress",), enabled=True),
    ]


def _sectioned_sidebar_snapshots() -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT"),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXList", identifier="sidebar", subrole="AXSectionList"),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="Projects"),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXButton", title="New project", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXGroup", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.3.1", depth=3, role="AXStaticText", value="PTG Assistant", enabled=True),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXButton", title="Watch to Codex", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.5", depth=2, role="AXGroup", actions=("AXShowMenu",), enabled=True),
        nav.AXElementSnapshot(path="W.1.5.1", depth=3, role="AXStaticText", value="POE Studies", enabled=True),
        nav.AXElementSnapshot(path="W.1.6", depth=2, role="AXHeading", value="Recents"),
        nav.AXElementSnapshot(path="W.1.7", depth=2, role="AXButton", title="Markdown Formatting Guide", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.8", depth=2, role="AXGroup", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.8.1", depth=3, role="AXStaticText", value="Agent Loop Notes", enabled=True),
        nav.AXElementSnapshot(path="W.1.9", depth=2, role="AXButton", title="Library", actions=("AXPress",), enabled=True),
        nav.AXElementSnapshot(path="W.1.10", depth=2, role="AXButton", title="GPTs", actions=("AXPress",), enabled=True),
    ]


def _nested_scrollarea_sidebar_snapshots(*, sidebar_evidence: bool = True) -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(
            path="W.1",
            depth=1,
            role="AXScrollArea",
            identifier="sidebar" if sidebar_evidence else "",
            frame=(0, 0, 280 if sidebar_evidence else 1200, 900),
        ),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXGroup", frame=(0, 50, 280, 32)),
        nav.AXElementSnapshot(path="W.1.1.1", depth=3, role="AXHeading", value="Projects", frame=(12, 56, 240, 24)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXGroup", actions=("AXPress",), enabled=True, frame=(10, 92, 250, 34)),
        nav.AXElementSnapshot(path="W.1.2.1", depth=3, role="AXStaticText", value="PTG Assistant", enabled=True, frame=(22, 100, 160, 18)),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXGroup", frame=(0, 142, 280, 32)),
        nav.AXElementSnapshot(path="W.1.3.1", depth=3, role="AXHeading", value="Recents", frame=(12, 148, 240, 24)),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXButton", title="Agent Loop Notes", actions=("AXPress",), enabled=True, frame=(10, 180, 250, 34)),
    ]


def _post_selected_sidebar_snapshots(title: str = "Markdown Formatting Guide") -> list[nav.AXElementSnapshot]:
    snapshots = _sectioned_sidebar_snapshots()
    return [
        snapshot if snapshot.title != title and snapshot.value != title else nav.AXElementSnapshot(
            path=snapshot.path,
            depth=snapshot.depth,
            role=snapshot.role,
            subrole=snapshot.subrole,
            identifier=snapshot.identifier,
            title=snapshot.title,
            description=snapshot.description,
            value=snapshot.value,
            enabled=snapshot.enabled,
            focused=True,
            actions=snapshot.actions,
        )
        for snapshot in snapshots
    ]


def _detailed_sidebar_snapshots() -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(
            path="W.1",
            depth=1,
            role="AXList",
            identifier="sidebar",
            subrole="AXSectionList",
            attribute_names=("AXChildren", "AXVisibleChildren", "AXRows", "AXVisibleRows", "AXSelectedRows", "AXSelectedChildren"),
            settable_attribute_names=("AXSelectedChildren",),
            row_paths=("W.1.3", "W.1.7"),
            visible_row_paths=("W.1.3", "W.1.7"),
            selected_row_paths=("W.1.7",),
            selected_child_paths=("W.1.7",),
            direct_child_count=9,
            visible_child_count=9,
            frame=(0, 0, 280, 900),
        ),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="Projects", frame=(10, 60, 250, 22)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXButton", title="New project", actions=("AXPress",), enabled=True, frame=(10, 76, 250, 32)),
        nav.AXElementSnapshot(
            path="W.1.3",
            depth=2,
            role="AXButton",
            actions=("AXShowMenu",),
            action_descriptions=(("AXShowMenu", "show menu"),),
            enabled=True,
            attribute_names=("AXChildren", "AXVisibleChildren", "AXLinkedUIElements", "AXSelected", "AXFocused"),
            settable_attribute_names=("AXSelected",),
            linked_element_paths=(("AXLinkedUIElements", "W.1.3.3"),),
            direct_child_count=3,
            visible_child_count=3,
            frame=(10, 112, 250, 32),
        ),
        nav.AXElementSnapshot(
            path="W.1.3.1",
            depth=3,
            role="AXStaticText",
            value="PTG Assistant",
            enabled=True,
            attribute_names=("AXParent", "AXServesAsTitleForUIElements"),
            linked_element_paths=(("AXServesAsTitleForUIElements", "W.1.3.3"),),
            frame=(20, 120, 120, 16),
        ),
        nav.AXElementSnapshot(path="W.1.3.2", depth=3, role="AXStaticText", value="x" * 500, frame=(20, 136, 120, 10)),
        nav.AXElementSnapshot(
            path="W.1.3.3",
            depth=3,
            role="AXButton",
            title="Open",
            actions=("AXPress",),
            action_descriptions=(("AXPress", "press"),),
            enabled=True,
            attribute_names=("AXTitle", "AXEnabled", "AXFocused"),
            settable_attribute_names=("AXFocused",),
            frame=(216, 116, 36, 24),
        ),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXButton", title="Library", actions=("AXPress",), enabled=True, frame=(10, 148, 250, 32)),
        nav.AXElementSnapshot(path="W.1.5", depth=2, role="AXButton", title="GPTs", actions=("AXPress",), enabled=True, frame=(10, 184, 250, 32)),
        nav.AXElementSnapshot(path="W.1.6", depth=2, role="AXHeading", value="Recents", frame=(10, 220, 250, 22)),
        nav.AXElementSnapshot(
            path="W.1.7",
            depth=2,
            role="AXButton",
            title="Markdown Formatting Guide",
            actions=("AXShowMenu",),
            action_descriptions=(("AXShowMenu", "show menu"),),
            enabled=True,
            selected=True,
            attribute_names=("AXChildren", "AXVisibleChildren", "AXSelected", "AXFocused", "AXOverflowButton"),
            settable_attribute_names=(),
            linked_element_paths=(("AXOverflowButton", "W.1.7"),),
            direct_child_count=1,
            visible_child_count=1,
            frame=(10, 248, 250, 32),
        ),
        nav.AXElementSnapshot(path="W.1.7.1", depth=3, role="AXStaticText", value="", enabled=True, frame=(20, 256, 160, 16)),
        nav.AXElementSnapshot(path="W.1.8", depth=2, role="AXButton", title="Unrelated Confidential Chat", actions=("AXShowMenu",), enabled=True, frame=(10, 284, 250, 32)),
        nav.AXElementSnapshot(path="W.2", depth=1, role="AXStaticText", value="Conversation transcript secret should not appear", frame=(320, 80, 700, 40)),
    ]


def _post_frame_click_snapshots(title: str = "Markdown Formatting Guide") -> list[nav.AXElementSnapshot]:
    snapshots = _detailed_sidebar_snapshots()
    updated = []
    for snapshot in snapshots:
        if snapshot.title == title or snapshot.value == title or snapshot.path == "W.1.7":
            updated.append(
                nav.AXElementSnapshot(
                    path=snapshot.path,
                    depth=snapshot.depth,
                    role=snapshot.role,
                    subrole=snapshot.subrole,
                    identifier=snapshot.identifier,
                    title=snapshot.title,
                    description=snapshot.description,
                    value=snapshot.value,
                    enabled=snapshot.enabled,
                    focused=True,
                    actions=snapshot.actions,
                    selected=True,
                    attribute_names=snapshot.attribute_names,
                    parameterized_attribute_names=snapshot.parameterized_attribute_names,
                    settable_attribute_names=snapshot.settable_attribute_names,
                    action_descriptions=snapshot.action_descriptions,
                    linked_element_paths=snapshot.linked_element_paths,
                    row_paths=snapshot.row_paths,
                    visible_row_paths=snapshot.visible_row_paths,
                    selected_row_paths=snapshot.selected_row_paths,
                    selected_child_paths=snapshot.selected_child_paths,
                    direct_child_count=snapshot.direct_child_count,
                    visible_child_count=snapshot.visible_child_count,
                    frame=snapshot.frame,
                )
            )
        else:
            updated.append(snapshot)
    return updated


def _post_autonomous_open_snapshots(title: str = "PTG Assistant") -> list[nav.AXElementSnapshot]:
    snapshots = _detailed_sidebar_snapshots()
    updated = []
    for snapshot in snapshots:
        active = snapshot.title == title or snapshot.value == title
        if title == "PTG Assistant" and snapshot.path == "W.1.3":
            active = True
        if title == "Markdown Formatting Guide" and snapshot.path == "W.1.7":
            active = True
        if active:
            updated.append(
                nav.AXElementSnapshot(
                    path=snapshot.path,
                    depth=snapshot.depth,
                    role=snapshot.role,
                    subrole=snapshot.subrole,
                    identifier=snapshot.identifier,
                    title=snapshot.title,
                    description=snapshot.description,
                    value=snapshot.value,
                    enabled=snapshot.enabled,
                    focused=True,
                    actions=snapshot.actions,
                    selected=True,
                    attribute_names=snapshot.attribute_names,
                    parameterized_attribute_names=snapshot.parameterized_attribute_names,
                    settable_attribute_names=snapshot.settable_attribute_names,
                    action_descriptions=snapshot.action_descriptions,
                    linked_element_paths=snapshot.linked_element_paths,
                    row_paths=snapshot.row_paths,
                    visible_row_paths=snapshot.visible_row_paths,
                    selected_row_paths=snapshot.selected_row_paths,
                    selected_child_paths=snapshot.selected_child_paths,
                    direct_child_count=snapshot.direct_child_count,
                    visible_child_count=snapshot.visible_child_count,
                    frame=snapshot.frame,
                    native_id=snapshot.native_id,
                )
            )
        else:
            updated.append(snapshot)
    updated.append(nav.AXElementSnapshot(path="W.9", depth=1, role="AXHeading", value=title, frame=(320, 40, 500, 40)))
    return updated


def _project_visible_chats_snapshots(
    *,
    partial_last_row: bool = True,
    long_preview: bool = False,
) -> list[nav.AXElementSnapshot]:
    third_row_frame = (320, 620, 760, 80) if partial_last_row else (320, 300, 760, 70)
    third_title_frame = (340, 638, 360, 20) if partial_last_row else (340, 318, 360, 20)
    third_preview_frame = (340, 662, 650, 18) if partial_last_row else (340, 342, 650, 18)
    second_preview = (
        "okay, is there a 365 day lockout that applies to profile photo changes and verification state across clients, "
        "including moderation review queues, account trust flags, and mobile sync delays"
        if long_preview
        else "okay, is there a 365 day lockout..."
    )
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXList", identifier="sidebar", subrole="AXSectionList", frame=(0, 0, 280, 900)),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="Projects", frame=(12, 60, 240, 24)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXGroup", actions=("AXPress",), enabled=True, frame=(10, 92, 250, 34)),
        nav.AXElementSnapshot(path="W.1.2.1", depth=3, role="AXStaticText", value="PTG Assistant", frame=(22, 100, 160, 18)),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXHeading", value="Recents", frame=(12, 160, 240, 24)),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXButton", title="Unrelated Sidebar Chat", actions=("AXPress",), frame=(10, 190, 250, 34)),
        nav.AXElementSnapshot(path="W.2", depth=1, role="AXGroup", identifier="project-content", frame=(300, 0, 900, 900)),
        nav.AXElementSnapshot(path="W.2.1", depth=2, role="AXHeading", value="PTG Assistant", frame=(320, 40, 420, 38)),
        nav.AXElementSnapshot(path="W.2.2", depth=2, role="AXButton", title="Chats", actions=("AXPress",), frame=(320, 92, 80, 30)),
        nav.AXElementSnapshot(path="W.2.3", depth=2, role="AXButton", title="Sources", actions=("AXPress",), frame=(408, 92, 90, 30)),
        nav.AXElementSnapshot(path="W.2.4", depth=2, role="AXTextField", title="Search", value="Search project chats", frame=(760, 88, 240, 34)),
        nav.AXElementSnapshot(path="W.2.5", depth=2, role="AXScrollArea", subrole="AXList", frame=(320, 140, 760, 520)),
        nav.AXElementSnapshot(path="W.2.5.1", depth=3, role="AXGroup", actions=("AXPress",), enabled=True, frame=(320, 156, 760, 70)),
        nav.AXElementSnapshot(path="W.2.5.1.1", depth=4, role="AXStaticText", value="Apple Content Moderation Requirements", frame=(340, 174, 390, 20)),
        nav.AXElementSnapshot(path="W.2.5.1.2", depth=4, role="AXStaticText", value="READ-ONLY COMPLIANCE AUDIT should stay preview-sized", frame=(340, 198, 600, 18)),
        nav.AXElementSnapshot(path="W.2.5.2", depth=3, role="AXGroup", actions=("AXPress",), enabled=True, frame=(320, 232, 760, 70)),
        nav.AXElementSnapshot(path="W.2.5.2.1", depth=4, role="AXStaticText", value="Profile Photo Verification Change", frame=(340, 250, 360, 20)),
        nav.AXElementSnapshot(path="W.2.5.2.2", depth=4, role="AXStaticText", value=second_preview, frame=(340, 274, 650, 18)),
        nav.AXElementSnapshot(path="W.2.5.3", depth=3, role="AXGroup", actions=("AXPress",), enabled=True, frame=third_row_frame),
        nav.AXElementSnapshot(path="W.2.5.3.1", depth=4, role="AXStaticText", value="Partially Visible Below Fold", frame=third_title_frame),
        nav.AXElementSnapshot(path="W.2.5.3.2", depth=4, role="AXStaticText", value="This row is still in the viewport but clipped.", frame=third_preview_frame),
        nav.AXElementSnapshot(path="W.2.6", depth=2, role="AXStaticText", value="Composer draft must not be treated as a row", frame=(340, 820, 600, 28)),
        nav.AXElementSnapshot(path="W.2.7", depth=2, role="AXButton", title="New chat", actions=("AXPress",), frame=(980, 40, 90, 32)),
    ]


def _project_chat_opened_snapshots(title: str = "Profile Photo Verification Change") -> list[nav.AXElementSnapshot]:
    snapshots = []
    for snapshot in _project_visible_chats_snapshots(partial_last_row=False):
        selected = snapshot.selected
        focused = snapshot.focused
        if snapshot.path in {"W.2.5.2", "W.2.5.2.1"}:
            selected = True
            focused = True
        snapshots.append(
            nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                subrole=snapshot.subrole,
                identifier=snapshot.identifier,
                title=snapshot.title,
                description=snapshot.description,
                value=snapshot.value,
                enabled=snapshot.enabled,
                focused=focused,
                actions=snapshot.actions,
                selected=selected,
                frame=snapshot.frame,
            )
        )
    snapshots.extend(
        [
            nav.AXElementSnapshot(path="W.2.8", depth=2, role="AXHeading", value=title, frame=(320, 690, 520, 34)),
            nav.AXElementSnapshot(path="W.2.9", depth=2, role="AXTextArea", title="Message ChatGPT", value="", frame=(340, 820, 620, 44)),
        ]
    )
    return snapshots


def _project_visible_chats_without_axpress() -> list[nav.AXElementSnapshot]:
    snapshots = []
    for snapshot in _project_visible_chats_snapshots(partial_last_row=False):
        actions = () if snapshot.path.startswith("W.2.5.") else snapshot.actions
        snapshots.append(
            nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                subrole=snapshot.subrole,
                identifier=snapshot.identifier,
                title=snapshot.title,
                description=snapshot.description,
                value=snapshot.value,
                enabled=snapshot.enabled,
                focused=snapshot.focused,
                actions=actions,
                selected=snapshot.selected,
                frame=snapshot.frame,
            )
        )
    return snapshots


def _content_moderation_project_snapshots(*, title_plus_preview_container: bool = False) -> list[nav.AXElementSnapshot]:
    snapshots = []
    for snapshot in _project_visible_chats_snapshots(partial_last_row=False):
        value = snapshot.value
        if snapshot.path == "W.2.5.1.1":
            value = "Content Moderation"
        if snapshot.path == "W.2.5.1.2":
            value = "Policy review preview must not affect matching"
        snapshots.append(
            nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                subrole=snapshot.subrole,
                identifier=snapshot.identifier,
                title=snapshot.title,
                description=snapshot.description,
                value=value,
                enabled=snapshot.enabled,
                focused=snapshot.focused,
                actions=snapshot.actions,
                selected=snapshot.selected,
                frame=snapshot.frame,
            )
        )
    if title_plus_preview_container:
        snapshots.append(
            nav.AXElementSnapshot(
                path="W.2.5.1.9",
                depth=4,
                role="AXGroup",
                value="Content Moderation Policy review preview must not affect matching",
                frame=(340, 172, 640, 48),
            )
        )
    return snapshots


def _merged_button_project_chat_row_snapshots() -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXGroup", identifier="project-content", frame=(260, 0, 940, 900)),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="PTG Assistant", frame=(282, 40, 420, 38)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXButton", title="Chats", actions=("AXPress",), frame=(282, 92, 80, 30)),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXButton", title="Sources", actions=("AXPress",), frame=(370, 92, 90, 30)),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXScrollArea", subrole="AXList", frame=(282, 150, 779, 520)),
        nav.AXElementSnapshot(
            path="W.1.4.1",
            depth=3,
            role="AXButton",
            title="Content Moderation, wait so how do whatsapp and instagram and X function",
            actions=("AXPress",),
            enabled=True,
            frame=(282, 176, 779, 65),
        ),
        nav.AXElementSnapshot(path="W.1.4.1.1", depth=4, role="AXStaticText", value="Content Moderation", frame=(302, 190, 220, 20)),
        nav.AXElementSnapshot(path="W.1.4.1.2", depth=4, role="AXStaticText", value=",", frame=(522, 190, 8, 20)),
        nav.AXElementSnapshot(
            path="W.1.4.1.3",
            depth=4,
            role="AXStaticText",
            value="wait so how do whatsapp and instagram and X function",
            frame=(302, 214, 520, 18),
        ),
        nav.AXElementSnapshot(
            path="W.1.4.2",
            depth=3,
            role="AXButton",
            title="AWS Profile Photo Verification, okay, is there a 365 day lockout or something in our code? audit this",
            actions=("AXPress",),
            enabled=True,
            frame=(282, 308, 779, 65),
        ),
        nav.AXElementSnapshot(path="W.1.4.2.1", depth=4, role="AXStaticText", value="AWS Profile Photo Verification", frame=(302, 322, 300, 20)),
        nav.AXElementSnapshot(
            path="W.1.4.2.2",
            depth=4,
            role="AXStaticText",
            value="okay, is there a 365 day lockout or something in our code? audit this",
            frame=(302, 346, 610, 18),
        ),
        nav.AXElementSnapshot(path="W.1.5", depth=2, role="AXTextArea", value="Composer text must stay outside row audit", frame=(320, 820, 700, 44)),
    ]


def _leaf_merged_project_chat_row_snapshots() -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXGroup", identifier="project-content", frame=(260, 0, 940, 900)),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="PTG Assistant", frame=(282, 40, 420, 38)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXButton", title="Chats", actions=("AXPress",), frame=(282, 92, 80, 30)),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXButton", title="Sources", actions=("AXPress",), frame=(370, 92, 90, 30)),
        nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXScrollArea", subrole="AXList", frame=(282, 150, 779, 520)),
        nav.AXElementSnapshot(
            path="W.1.4.1",
            depth=3,
            role="AXButton",
            description="Content Moderation, wait so how do whatsapp and instagram and X function",
            actions=("AXPress",),
            enabled=True,
            frame=(282, 176, 779, 65),
        ),
        nav.AXElementSnapshot(
            path="W.1.4.2",
            depth=3,
            role="AXButton",
            description="AWS Profile Photo Verification, okay, is there a 365 day lockout or something in our code? audit this",
            actions=("AXPress",),
            enabled=True,
            frame=(282, 308, 779, 65),
        ),
    ]


def _exact_text_project_chat_row_snapshots(title: str = "Content Moderation") -> list[nav.AXElementSnapshot]:
    snapshots = _leaf_merged_project_chat_row_snapshots()
    updated = []
    for snapshot in snapshots:
        if snapshot.path == "W.1.4.1":
            updated.append(
                nav.AXElementSnapshot(
                    path=snapshot.path,
                    depth=snapshot.depth,
                    role=snapshot.role,
                    description=title,
                    actions=snapshot.actions,
                    enabled=snapshot.enabled,
                    frame=snapshot.frame,
                )
            )
        else:
            updated.append(snapshot)
    return updated


def _scrollable_project_chat_page(
    titles: list[str],
    *,
    path_prefix: str = "W.1.4",
    row_offset: int = 0,
    list_actions: tuple[str, ...] = ("AXScrollDown",),
    scrollbar_value: str | None = None,
) -> list[nav.AXElementSnapshot]:
    snapshots = [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(path="W.0", depth=1, role="AXList", identifier="sidebar", subrole="AXSectionList", frame=(0, 0, 260, 900), actions=("AXScrollDown",)),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXGroup", identifier="project-content", frame=(260, 0, 940, 900)),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="PTG Assistant", frame=(282, 40, 420, 38)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXButton", title="Chats", actions=("AXPress",), frame=(282, 92, 80, 30)),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXButton", title="Sources", actions=("AXPress",), frame=(370, 92, 90, 30)),
        nav.AXElementSnapshot(path=path_prefix, depth=2, role="AXScrollArea", subrole="AXList", actions=list_actions, frame=(282, 150, 779, 520)),
        nav.AXElementSnapshot(path="W.1.5", depth=2, role="AXScrollArea", identifier="conversation-transcript", actions=("AXScrollDown",), frame=(282, 700, 779, 140)),
    ]
    for index, title in enumerate(titles, start=1):
        y = 176 + (index - 1) * 82
        row_number = row_offset + index
        snapshots.append(
            nav.AXElementSnapshot(
                path=f"{path_prefix}.{row_number}",
                depth=3,
                role="AXButton",
                description=f"{title}, preview text must not drive matching",
                actions=("AXPress",),
                enabled=True,
                frame=(282, y, 779, 65),
            )
        )
    if scrollbar_value is not None:
        snapshots.append(nav.AXElementSnapshot(path=f"{path_prefix}.99", depth=3, role="AXScrollBar", value=scrollbar_value, frame=(1048, 150, 10, 520)))
    return snapshots


def _visual_row_diagnostic_project_chat_page() -> list[nav.AXElementSnapshot]:
    snapshots = _scrollable_project_chat_page(["Mock Data Insertion SQL", "Content Moderation"], list_actions=())
    snapshots.extend(
        [
            nav.AXElementSnapshot(path="W.1.4.0", depth=3, role="AXOpaqueProviderGroup", frame=(282, 150, 779, 1381.5)),
            nav.AXElementSnapshot(path="W.1.4.1.1", depth=4, role="AXStaticText", value="Mock Data Insertion SQL", frame=(302, 190, 240, 20)),
            nav.AXElementSnapshot(path="W.1.4.1.2", depth=4, role="AXGroup", title="Title Attr Candidate", description="Description Attr Candidate", value="Value Attr Candidate", frame=(302, 214, 420, 20)),
            nav.AXElementSnapshot(path="W.1.4.1.3", depth=4, role="AXRow", value="AXRow Candidate", frame=(302, 218, 320, 16)),
            nav.AXElementSnapshot(path="W.1.4.1.4", depth=4, role="AXCell", value="AXCell Candidate", frame=(632, 218, 220, 16)),
            nav.AXElementSnapshot(path="W.1.4.1.5", depth=4, role="AXLink", title="AXLink Candidate", frame=(862, 218, 150, 16)),
            nav.AXElementSnapshot(path="W.1.4.3", depth=3, role="AXGroup", frame=(282, 340, 779, 65)),
            nav.AXElementSnapshot(path="W.1.4.3.1", depth=4, role="AXButton", actions=("AXPress",), frame=(302, 356, 360, 18)),
            nav.AXElementSnapshot(path="W.1.4.3.1.1", depth=5, role="AXStaticText", value="Nested Small Wrapper", frame=(314, 356, 250, 18)),
            nav.AXElementSnapshot(path="W.1.4.98", depth=3, role="AXIncrementPage", frame=(1048, 150, 10, 32)),
            nav.AXElementSnapshot(path="W.1.4.99", depth=3, role="AXScrollBar", value="0.5", frame=(1048, 150, 10, 520)),
        ]
    )
    return snapshots


def _mock_data_sql_project_chat_page(preview: str) -> list[nav.AXElementSnapshot]:
    snapshots = _scrollable_project_chat_page([], list_actions=())
    snapshots.extend(
        [
            nav.AXElementSnapshot(path="W.1.4.0", depth=3, role="AXOpaqueProviderGroup", frame=(282, 150, 779, 1381.5)),
            nav.AXElementSnapshot(
                path="W.1.4.0.15",
                depth=4,
                role="AXButton",
                description=f"Mock Data Insertion SQL, {preview}",
                actions=("AXPress", "AXScrollToVisible", "AXShowMenu"),
                enabled=True,
                frame=(282, 441, 352, 64.5),
            ),
        ]
    )
    return snapshots


def _project_chat_page_with_single_row_frame(
    title: str,
    *,
    path: str,
    frame: tuple[float, float, float, float],
    actions: tuple[str, ...] = ("AXPress",),
) -> list[nav.AXElementSnapshot]:
    snapshots = _scrollable_project_chat_page([], list_actions=())
    snapshots.append(
        nav.AXElementSnapshot(
            path=path,
            depth=3,
            role="AXButton",
            description=f"{title}, preview text must not drive matching",
            actions=actions,
            enabled=True,
            frame=frame,
        )
    )
    return snapshots


def _alignment_dispatch_context(
    *,
    path: str = "W.1.4.11",
    container_path: str = "W.1.4",
    requested_title: str = "Mock Data Insertion SQL",
    canonical_title: str = "Mock Data Insertion SQL",
    exact_target_detected: bool = True,
    fresh_re_resolution_confirmed: bool = True,
    target_descends_from_confirmed_chat_list: bool = True,
    visibility: str = "partially_clipped",
    row_actions: tuple[str, ...] = ("AXPress", "AXScrollToVisible", "AXShowMenu"),
    alignment_already_posted: bool = False,
) -> dict:
    return {
        "kind": "exact_project_chat_target_alignment",
        "target_path": path,
        "requested_title": requested_title,
        "canonical_title": canonical_title,
        "exact_target_detected": exact_target_detected,
        "fresh_re_resolution_confirmed": fresh_re_resolution_confirmed,
        "confirmed_chat_list_container_path": container_path,
        "target_descends_from_confirmed_chat_list": target_descends_from_confirmed_chat_list,
        "visibility": visibility,
        "row_actions": row_actions,
        "alignment_already_posted": alignment_already_posted,
    }


def _autonomous_dispatch_reader(
    path: str = "W.1.4.11", *, error_code: int = 0
) -> tuple[nav._AutonomousSidebarAXReader, _AXPerformActionRecorder]:
    recorder = _AXPerformActionRecorder(error_code=error_code)
    reader = object.__new__(nav._AutonomousSidebarAXReader)
    reader._elements_by_path = {path: 111}
    reader._ax = recorder
    reader._attribute_ref = lambda action: 222 if action == "AXScrollToVisible" else 333
    return reader, recorder


def _scroll_opened_conversation_snapshots(title: str) -> list[nav.AXElementSnapshot]:
    return [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXGroup", identifier="conversation-content", frame=(260, 0, 940, 900)),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value=title, frame=(282, 40, 420, 38)),
        nav.AXElementSnapshot(path="W.1.2", depth=2, role="AXStaticText", value="assistant message", frame=(300, 180, 500, 30)),
        nav.AXElementSnapshot(path="W.1.3", depth=2, role="AXTextArea", title="Message ChatGPT", value="", frame=(320, 820, 620, 44)),
    ]


def _project_content_shell_without_list(
    extra: list[nav.AXElementSnapshot] | None = None,
) -> list[nav.AXElementSnapshot]:
    """Project content pane + header + Chats/Sources tabs, but no chat-list container."""
    snapshots = [
        nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT", frame=(0, 0, 1200, 900)),
        nav.AXElementSnapshot(path="W.1", depth=1, role="AXList", identifier="sidebar", subrole="AXSectionList", frame=(0, 0, 280, 900)),
        nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXHeading", value="Projects", frame=(12, 60, 240, 24)),
        nav.AXElementSnapshot(path="W.2", depth=1, role="AXGroup", identifier="project-content", frame=(300, 0, 900, 900)),
        nav.AXElementSnapshot(path="W.2.1", depth=2, role="AXHeading", value="PTG Assistant", frame=(320, 40, 420, 38)),
        nav.AXElementSnapshot(path="W.2.2", depth=2, role="AXButton", title="Chats", actions=("AXPress",), frame=(320, 92, 80, 30)),
        nav.AXElementSnapshot(path="W.2.3", depth=2, role="AXButton", title="Sources", actions=("AXPress",), frame=(408, 92, 90, 30)),
    ]
    if extra:
        snapshots.extend(extra)
    return snapshots


def _minimal_project_chat_resolution(title: str, *, count: int = 1) -> dict:
    rows = [
        {
            "ordinal": 1,
            "title": title,
            "preview": "preview must not be used",
            "accessibility_row_text": title,
            "title_representation": "exact_accessibility_text",
            "preview_representation": "unavailable",
            "path": "W.2.5.1",
            "row_path": "W.2.5.1",
            "title_path": "W.2.5.1.1",
            "role": "AXButton",
            "subrole": "",
            "row_role": "AXButton",
            "title_role": "AXStaticText",
            "row_frame": {"x": 320, "y": 156, "width": 760, "height": 70},
            "title_frame": {"x": 340, "y": 174, "width": 260, "height": 20},
            "visibility": "fully_visible",
            "actions": ["AXPress"],
            "action_names": ["AXPress"],
            "title_action_names": [],
            "ax_press_available": True,
        }
    ]
    for index in range(2, count + 1):
        rows.append(
            {
                **rows[0],
                "ordinal": index,
                "title": f"Other {index}",
                "path": f"W.2.5.{index}",
                "row_path": f"W.2.5.{index}",
                "title_path": f"W.2.5.{index}.1",
                "row_frame": {"x": 320, "y": 156 + index * 80, "width": 760, "height": 70},
                "title_frame": {"x": 340, "y": 174 + index * 80, "width": 260, "height": 20},
            }
        )
    return {
        "ok": True,
        "status": "visible_chats_found",
        "project_title": "PTG Assistant",
        "project_identity_confirmed": True,
        "chats_tab_confirmed": True,
        "chats_area_confirmed": True,
        "visible_chat_count": len(rows),
        "visible_chats": rows,
        "project_content_container": {"path": "W.2", "role": "AXGroup", "frame": {"x": 300, "y": 0, "width": 900, "height": 900}},
        "main_project_content": {"path": "W.2", "role": "AXGroup", "frame": {"x": 300, "y": 0, "width": 900, "height": 900}},
        "chat_list_container": {"path": "W.2.5", "role": "AXScrollArea", "frame": {"x": 320, "y": 140, "width": 760, "height": 520}},
        "more_rows_may_exist_below": "unknown",
    }


def _local_coordinate_sidebar_snapshots() -> list[nav.AXElementSnapshot]:
    updates = {
        "W": (100, 200, 1200, 900),
        "W.1": (0, 0, 280, 900),
        "W.1.1": (10, 60, 250, 22),
        "W.1.2": (10, 76, 250, 32),
        "W.1.3": (10, 112, 250, 32),
        "W.1.3.1": (20, 120, 120, 16),
        "W.1.3.2": (20, 136, 120, 10),
        "W.1.3.3": (216, 116, 36, 24),
        "W.1.4": (10, 148, 250, 32),
        "W.1.5": (10, 184, 250, 32),
        "W.1.6": (10, 220, 250, 22),
        "W.1.7": (10, 248, 250, 32),
        "W.1.7.1": (20, 256, 160, 16),
        "W.1.8": (10, 284, 250, 32),
        "W.2": (320, 80, 700, 40),
    }
    result = []
    for snapshot in _detailed_sidebar_snapshots():
        result.append(
            nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                subrole=snapshot.subrole,
                identifier=snapshot.identifier,
                title=snapshot.title,
                description=snapshot.description,
                value=snapshot.value,
                enabled=snapshot.enabled,
                focused=snapshot.focused,
                actions=snapshot.actions,
                selected=snapshot.selected,
                attribute_names=snapshot.attribute_names,
                parameterized_attribute_names=snapshot.parameterized_attribute_names,
                settable_attribute_names=snapshot.settable_attribute_names,
                action_descriptions=snapshot.action_descriptions,
                linked_element_paths=snapshot.linked_element_paths,
                row_paths=snapshot.row_paths,
                visible_row_paths=snapshot.visible_row_paths,
                selected_row_paths=snapshot.selected_row_paths,
                selected_child_paths=snapshot.selected_child_paths,
                direct_child_count=snapshot.direct_child_count,
                visible_child_count=snapshot.visible_child_count,
                frame=updates.get(snapshot.path, snapshot.frame),
            )
        )
    return result


def _title_inventory_candidate(title: str, path: str, classification: str) -> dict:
    return {
        "exact_title": title,
        "path": path,
        "role": "AXButton",
        "subrole": "",
        "enabled": True,
        "focused": None,
        "actions": ["AXShowMenu"],
        "nearest_actionable_ancestor": {
            "path": "",
            "role": "",
            "subrole": "",
            "enabled": None,
            "actions": [],
            "relationship": "none_discovered",
        },
        "nearest_list_container": {
            "path": "W.1",
            "role": "AXList",
            "subrole": "AXSectionList",
            "purpose": "chat_history" if classification == "visible_chat_title_candidate" else "projects",
        },
        "classification": classification,
        "confidence": "high",
        "evidence_codes": ["short_bounded_title"],
        "title_source_attribute": "description",
        "title_candidate_actionable": False,
        "parent_appears_actionable": False,
        "capability_assessment": "ambiguous",
    }


def _title_inventory_result() -> dict:
    result = nav._base_result("ChatGPT", 16, 900, include_visible_navigation_titles=True)
    result.update({"ok": True, "reason_code": "inspection_completed", "pid_present": True, "window_available": True})
    buckets = {
        "visible_chat_title_candidates": [
            _title_inventory_candidate("Chat Alpha", "W.1.1", "visible_chat_title_candidate")
        ],
        "visible_project_title_candidates": [
            _title_inventory_candidate("Project Beta", "W.2.1", "visible_project_title_candidate")
        ],
        "visible_search_result_candidates": [
            _title_inventory_candidate("Search Gamma", "W.3.1", "visible_search_result_candidate")
        ],
        "actionable_parent_candidates": [
            {
                "path": "W.1.2",
                "role": "AXGroup",
                "subrole": "",
                "enabled": True,
                "focused": None,
                "actions": ["AXPress"],
                "classification": "actionable_parent_candidate",
                "confidence": "medium",
                "evidence_codes": ["ancestor_exposes_selection_action"],
                "nearest_list_container": {
                    "path": "W.1",
                    "role": "AXList",
                    "subrole": "AXSectionList",
                    "purpose": "chat_history",
                },
                "example_child_title_path": "W.1.2.1",
                "capability_assessment": "candidate_may_be_selectable_but_unverified",
            }
        ],
        "visible_navigation_section_labels": [
            _title_inventory_candidate("History", "W.4.1", "visible_navigation_section_label")
        ],
    }
    for key, value in buckets.items():
        result[key] = value
        result["visible_title_category_limits"][key].update(
            {"total": len(value), "emitted": len(value), "omitted": 0}
        )
    return result


class ChatGPTNavigationDiagnosticTests(unittest.TestCase):
    def test_process_match_for_chatgpt_requires_classic_bundle(self) -> None:
        runtime = object.__new__(nav._ObjCRuntime)

        self.assertEqual(runtime._match_score("ChatGPT", "ChatGPT", "com.openai.chat"), 200)
        self.assertEqual(runtime._match_score("ChatGPT", "ChatGPT", "com.openai.codex"), 0)
        self.assertEqual(runtime._match_score("ChatGPT", "ChatGPT", "com.example.other"), 0)

    def test_non_macos_returns_structured_unsupported_result(self) -> None:
        with mock.patch.object(nav.sys, "platform", "linux"):
            result = nav.inspect_chatgpt_navigation_ui()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "unsupported_platform")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["actions_performed"], [])
        self.assertFalse(result["pid_present"])

    def test_process_resolution_failure_is_structured(self) -> None:
        def resolver(app_name: str) -> nav.ProcessResolution:
            return nav.ProcessResolution(pid=None, method="fake_nsworkspace", error="not running")

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_navigation_ui(process_resolver=resolver)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "process_not_found")
        self.assertEqual(result["process_resolution_method"], "fake_nsworkspace")
        self.assertFalse(result["pid_present"])
        self.assertEqual(result["error"], "not running")

    def test_successful_synthetic_snapshot_classification(self) -> None:
        reader = _FakeReader(_synthetic_snapshots())
        factory = _Factory(reader)

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_navigation_ui(
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake_nsworkspace"),
                reader_factory=factory,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason_code"], "inspection_completed")
        self.assertEqual(result["process_resolution_method"], "fake_nsworkspace")
        self.assertTrue(result["window_available"])
        self.assertGreaterEqual(len(result["navigation_candidates"]), 2)
        self.assertGreaterEqual(len(result["chat_history_candidates"]), 1)
        self.assertGreaterEqual(len(result["project_candidates"]), 1)
        self.assertGreaterEqual(len(result["search_candidates"]), 1)
        self.assertGreaterEqual(len(result["current_chat_identity_candidates"]), 1)
        self.assertEqual(result["actionable_element_summaries"], [])
        self.assertEqual(factory.calls, [("ChatGPT", nav.DEFAULT_MAX_DEPTH, nav.DEFAULT_MAX_NODES)])

    def test_sensitive_text_is_never_emitted_and_arbitrary_labels_are_redacted(self) -> None:
        result = nav.classify_navigation_snapshots(_synthetic_snapshots())
        encoded = json.dumps(result, sort_keys=True)

        for sensitive in (
            "Private Tax Chat",
            "Vacation plan with Alice",
            "Super Secret Work Project",
            "Current Chat Sensitive Title",
            "composer secret",
            "draft composer text",
        ):
            self.assertNotIn(sensitive, encoded)

        redacted = [
            candidate
            for bucket in (
                result["chat_history_candidates"],
                result["project_candidates"],
                result["current_chat_identity_candidates"],
            )
            for candidate in bucket
            if candidate["label"]["redacted"]
        ]
        self.assertTrue(redacted)
        self.assertTrue(all("sha256" in candidate["label"] for candidate in redacted))

    def test_safe_generic_labels_may_appear_literally(self) -> None:
        result = nav.classify_navigation_snapshots(_synthetic_snapshots())
        encoded = json.dumps(result, sort_keys=True)

        self.assertIn("New chat", encoded)
        self.assertIn("Search", encoded)
        self.assertIn("History", encoded)
        self.assertIn("Projects", encoded)
        self.assertIn("Sidebar", encoded)

    def test_large_arbitrary_text_never_appears_and_navigation_candidates_remain_visible(self) -> None:
        result = nav.classify_navigation_snapshots(_large_text_snapshots())
        encoded = json.dumps(result, sort_keys=True)

        self.assertNotIn("X" * 100, encoded)
        self.assertIn("94a4ce4b2055", encoded)
        self.assertEqual(result["filtering_summary"]["long_text_fields_redacted"], 1)
        self.assertGreaterEqual(len(result["search_candidates"]), 1)
        self.assertGreaterEqual(len(result["chat_history_candidates"]), 1)
        self.assertEqual(result["search_candidates"][0]["label"]["literal"], "Search")

    def test_category_caps_and_omitted_counts_work(self) -> None:
        result = nav.classify_navigation_snapshots(_large_text_snapshots())
        limits = result["category_limits"]["chat_history_candidates"]

        self.assertEqual(len(result["chat_history_candidates"]), nav.MAX_CANDIDATES_PER_CATEGORY)
        self.assertGreater(limits["total"], nav.MAX_CANDIDATES_PER_CATEGORY)
        self.assertEqual(limits["omitted"], limits["total"] - nav.MAX_CANDIDATES_PER_CATEGORY)

    def test_candidate_ordering_is_deterministic_by_confidence_then_path(self) -> None:
        snapshots = [
            nav.AXElementSnapshot(path="W.2", depth=1, role="AXButton", title="Search", actions=("AXPress",)),
            nav.AXElementSnapshot(path="W.1", depth=1, role="AXTextField", title="Search", subrole="AXSearchField"),
            nav.AXElementSnapshot(path="W.3", depth=1, role="AXButton", title="Search", actions=("AXPress",)),
        ]

        result = nav.classify_navigation_snapshots(snapshots)

        self.assertEqual([item["path"] for item in result["search_candidates"]], ["W.1", "W.2", "W.3"])

    def test_arbitrary_chat_and_project_labels_are_redacted(self) -> None:
        result = nav.classify_navigation_snapshots(_synthetic_snapshots())
        encoded = json.dumps(result, sort_keys=True)

        self.assertIn('"literal": null', encoded)
        self.assertNotIn("Vacation plan with Alice", encoded)
        self.assertNotIn("Super Secret Work Project", encoded)

    def test_default_mode_never_reveals_visible_chat_or_project_titles(self) -> None:
        result = nav.classify_navigation_snapshots(_visible_navigation_title_snapshots())
        encoded = json.dumps(result, sort_keys=True)

        self.assertFalse(result["visible_navigation_title_disclosure_enabled"])
        self.assertEqual(result["visible_chat_title_candidates"], [])
        self.assertEqual(result["visible_project_title_candidates"], [])
        self.assertNotIn("Trip Planning", encoded)
        self.assertNotIn("Launch Plan", encoded)
        self.assertNotIn("Nested Budget Chat", encoded)

    def test_opt_in_reveals_only_titles_inside_recognized_navigation_lists(self) -> None:
        result = nav.classify_navigation_snapshots(
            _visible_navigation_title_snapshots(),
            include_visible_navigation_titles=True,
        )
        encoded = json.dumps(result, sort_keys=True)

        self.assertTrue(result["visible_navigation_title_disclosure_enabled"])
        self.assertIn(nav.TITLE_DISCLOSURE_NOTICE, encoded)
        chat_titles = {candidate["exact_title"] for candidate in result["visible_chat_title_candidates"]}
        project_titles = {candidate["exact_title"] for candidate in result["visible_project_title_candidates"]}
        section_titles = {candidate["exact_title"] for candidate in result["visible_navigation_section_labels"]}
        search_titles = {candidate["exact_title"] for candidate in result["visible_search_result_candidates"]}
        self.assertEqual(chat_titles, {"Trip Planning", "Nested Budget Chat"})
        self.assertEqual(project_titles, {"Launch Plan"})
        self.assertEqual(section_titles, {"History", "Projects"})
        self.assertEqual(search_titles, {"Search Result Chat"})
        self.assertNotIn("Outside Visible Title", encoded)

    def test_section_boundaries_separate_projects_from_recents(self) -> None:
        result = nav.classify_navigation_snapshots(
            _sectioned_sidebar_snapshots(),
            include_visible_navigation_titles=True,
        )

        project_titles = {candidate["exact_title"] for candidate in result["visible_project_title_candidates"]}
        chat_titles = {candidate["exact_title"] for candidate in result["visible_chat_title_candidates"]}
        section_titles = {candidate["exact_title"] for candidate in result["visible_navigation_section_labels"]}

        self.assertEqual(project_titles, {"PTG Assistant", "Watch to Codex", "POE Studies"})
        self.assertEqual(chat_titles, {"Markdown Formatting Guide", "Agent Loop Notes"})
        self.assertEqual(section_titles, {"Projects", "Recents"})
        self.assertNotIn("Markdown Formatting Guide", project_titles)
        self.assertNotIn("New project", project_titles)
        self.assertNotIn("Library", chat_titles | project_titles)
        self.assertNotIn("GPTs", chat_titles | project_titles)

    def test_nested_scrollarea_sidebar_preserves_project_section_context(self) -> None:
        result = nav.classify_navigation_snapshots(
            _nested_scrollarea_sidebar_snapshots(),
            include_visible_navigation_titles=True,
        )

        self.assertEqual(
            [candidate["exact_title"] for candidate in result["visible_project_title_candidates"]],
            ["PTG Assistant"],
        )
        self.assertEqual(
            result["visible_project_title_candidates"][0]["nearest_list_container"]["path"],
            "W.1",
        )

    def test_nested_wide_scrollarea_without_sidebar_evidence_remains_unclassified(self) -> None:
        result = nav.classify_navigation_snapshots(
            _nested_scrollarea_sidebar_snapshots(sidebar_evidence=False),
            include_visible_navigation_titles=True,
        )

        self.assertEqual(result["visible_project_title_candidates"], [])

    def test_opt_in_keeps_long_composer_and_message_like_text_redacted(self) -> None:
        result = nav.classify_navigation_snapshots(
            _visible_navigation_title_snapshots(),
            include_visible_navigation_titles=True,
        )
        encoded = json.dumps(result, sort_keys=True)

        self.assertNotIn("M" * 100, encoded)
        self.assertNotIn("assistant: this is a message body", encoded)
        self.assertNotIn("Draft composer visible title", encoded)

    def test_ancestor_actionability_is_reported_without_claiming_safe_to_click(self) -> None:
        result = nav.classify_navigation_snapshots(
            _visible_navigation_title_snapshots(),
            include_visible_navigation_titles=True,
        )
        nested = next(
            candidate
            for candidate in result["visible_chat_title_candidates"]
            if candidate["exact_title"] == "Nested Budget Chat"
        )
        direct = next(
            candidate
            for candidate in result["visible_chat_title_candidates"]
            if candidate["exact_title"] == "Trip Planning"
        )

        self.assertFalse(nested["title_candidate_actionable"])
        self.assertTrue(nested["parent_appears_actionable"])
        self.assertEqual(nested["nearest_actionable_ancestor"]["path"], "W.1.2")
        self.assertIn("AXPress", nested["nearest_actionable_ancestor"]["actions"])
        self.assertEqual(nested["action_target_resolution"]["resolution_method"], "row_press_target")
        self.assertTrue(direct["title_candidate_actionable"])
        self.assertFalse(direct["parent_appears_actionable"])
        self.assertEqual(direct["action_target_resolution"]["resolution_method"], "direct_press_target")

    def test_action_target_resolution_distinguishes_supported_methods(self) -> None:
        snapshots = _sectioned_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.11", depth=2, role="AXGroup", actions=("AXSetFocus", "AXPress"), enabled=True, focused=False),
            nav.AXElementSnapshot(path="W.1.11.1", depth=3, role="AXStaticText", value="Focus Then Press", enabled=True),
            nav.AXElementSnapshot(path="W.1.12", depth=2, role="AXGroup", enabled=True),
            nav.AXElementSnapshot(path="W.1.12.1", depth=3, role="AXStaticText", value="No Target", enabled=True),
        ]
        result = nav.classify_navigation_snapshots(snapshots, include_visible_navigation_titles=True)
        by_title = {
            candidate["exact_title"]: candidate["action_target_resolution"]["resolution_method"]
            for candidate in result["visible_project_title_candidates"] + result["visible_chat_title_candidates"]
        }

        self.assertEqual(by_title["Watch to Codex"], "direct_press_target")
        self.assertEqual(by_title["PTG Assistant"], "row_press_target")
        self.assertEqual(by_title["POE Studies"], "menu_only_target")
        self.assertEqual(by_title["Focus Then Press"], "focusable_then_press_target")
        self.assertEqual(by_title["No Target"], "no_verified_target")

    def test_action_target_resolution_reports_ambiguous_ancestor_targets(self) -> None:
        snapshots = _sectioned_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.11", depth=2, role="AXGroup", actions=("AXPress",), enabled=True),
            nav.AXElementSnapshot(path="W.1.11.1", depth=3, role="AXGroup", actions=("AXPress",), enabled=True),
            nav.AXElementSnapshot(path="W.1.11.1.1", depth=4, role="AXStaticText", value="Ambiguous Target", enabled=True),
        ]

        result = nav.classify_navigation_snapshots(snapshots, include_visible_navigation_titles=True)
        candidate = next(
            item for item in result["visible_chat_title_candidates"] if item["exact_title"] == "Ambiguous Target"
        )

        self.assertEqual(candidate["action_target_resolution"]["resolution_method"], "ambiguous_target")

    def test_visible_title_category_caps_and_ordering_are_deterministic(self) -> None:
        snapshots = [nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="ChatGPT")]
        snapshots.append(nav.AXElementSnapshot(path="W.1", depth=1, role="AXList", title="History"))
        for index in range(35):
            snapshots.append(
                nav.AXElementSnapshot(
                    path=f"W.1.{index + 1}",
                    depth=2,
                    role="AXStaticText",
                    value=f"Chat {index:02d}",
                )
            )

        result = nav.classify_navigation_snapshots(snapshots, include_visible_navigation_titles=True)

        limits = result["visible_title_category_limits"]["visible_chat_title_candidates"]
        self.assertEqual(len(result["visible_chat_title_candidates"]), nav.MAX_VISIBLE_TITLE_CANDIDATES)
        self.assertEqual(limits["total"], 35)
        self.assertEqual(limits["omitted"], 5)
        self.assertEqual(result["visible_chat_title_candidates"][0]["path"], "W.1.1")
        self.assertEqual(result["visible_chat_title_candidates"][-1]["path"], "W.1.30")

    def test_read_only_diagnostic_sources_contain_no_action_or_persistence_capabilities(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        read_only_source = "\n".join(
            [
                source[source.index("def inspect_chatgpt_navigation_ui"): source.index("def resolve_chatgpt_process")],
                source[
                    source.index("def inspect_chatgpt_sidebar_destination"):
                    source.index("def open_chatgpt_sidebar_destination")
                ],
                source[
                    source.index("def _visible_title_inventory_result"):
                    source.index("def _visible_title_inventory")
                ],
            ]
        )
        banned = (
            "activate_app",
            "activate_chatgpt",
            "inspect_chatgpt_ui",
            "press_chatgpt_send_button",
            "paste_clipboard",
            "copy_to_clipboard",
            "press_enter",
            "pgrep",
            "subprocess",
            "ledger",
            "local_server",
            "write_text",
            ".write(",
            "open(",
        )
        for token in banned:
            self.assertNotIn(token, read_only_source)
        self.assertIn("class _ActionAXReader", source)

    def test_title_inventory_path_does_not_reference_action_api(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        inventory_source = source[
            source.index("def _visible_title_inventory_result"):
            source.index("def verify_chatgpt_sidebar_destination")
        ]
        self.assertNotIn("AXUIElementPerformAction", inventory_source)

    def test_result_schema_contains_only_bounded_candidate_fields(self) -> None:
        result = nav.classify_navigation_snapshots(_synthetic_snapshots())

        for key in (
            "current_chat_identity_candidates",
            "chat_history_candidates",
            "project_candidates",
            "search_candidates",
            "sidebar_candidates",
            "navigation_candidates",
            "ambiguous_navigation_relevant_controls",
        ):
            for candidate in result[key]:
                self.assertIn("path", candidate)
                self.assertIn("role", candidate)
                self.assertIn("subrole", candidate)
                self.assertIn("label", candidate)
                self.assertIn("evidence_codes", candidate)
                self.assertIn("relationship", candidate)
                self.assertNotIn("element", candidate)
                self.assertLessEqual(len(candidate["path"]), nav.MAX_PATH_LENGTH)

    def test_collect_tree_obeys_node_and_depth_bounds(self) -> None:
        adapter = _TreeAdapter(
            {
                "root": ["a", "b"],
                "a": ["a1", "a2"],
                "b": ["b1"],
            }
        )

        snapshots, stats = nav._collect_tree("root", adapter, max_depth=1, max_nodes=2)

        self.assertEqual([snapshot.path for snapshot in snapshots], ["W", "W.1"])
        self.assertEqual(stats["visited_nodes"], 2)
        self.assertTrue(stats["truncated_by_depth_limit"])
        self.assertTrue(stats["truncated_by_node_limit"])

    def test_verify_destination_fails_closed_for_missing_duplicate_and_nonactionable(self) -> None:
        resolver = lambda app_name: nav.ProcessResolution(pid=123, method="fake")
        missing_reader = _ActionReader([_sectioned_sidebar_snapshots()])
        duplicate_snapshots = _sectioned_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.11", depth=2, role="AXButton", title="Markdown Formatting Guide", actions=("AXPress",), enabled=True)
        ]
        duplicate_reader = _ActionReader([duplicate_snapshots])
        nonactionable_reader = _ActionReader([_sectioned_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            missing = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Not Visible",
                process_resolver=resolver,
                reader_factory=_ActionFactory(missing_reader),
                settle_seconds=0,
            )
            duplicate = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=resolver,
                reader_factory=_ActionFactory(duplicate_reader),
                settle_seconds=0,
            )
            nonactionable = nav.verify_chatgpt_sidebar_destination(
                kind="project",
                title="POE Studies",
                process_resolver=resolver,
                reader_factory=_ActionFactory(nonactionable_reader),
                settle_seconds=0,
            )

        self.assertEqual(missing["status"], "target_not_found")
        self.assertEqual(duplicate["status"], "target_ambiguous")
        self.assertEqual(nonactionable["status"], "target_not_actionable")
        self.assertEqual(missing_reader.actions, [])
        self.assertEqual(duplicate_reader.actions, [])
        self.assertEqual(nonactionable_reader.actions, [])

    def test_verify_destination_fails_closed_for_ambiguous_target(self) -> None:
        snapshots = _sectioned_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.11", depth=2, role="AXGroup", actions=("AXPress",), enabled=True),
            nav.AXElementSnapshot(path="W.1.11.1", depth=3, role="AXGroup", actions=("AXPress",), enabled=True),
            nav.AXElementSnapshot(path="W.1.11.1.1", depth=4, role="AXStaticText", value="Ambiguous Target", enabled=True),
        ]
        reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Ambiguous Target",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                settle_seconds=0,
            )

        self.assertEqual(result["status"], "target_ambiguous")
        self.assertEqual(reader.actions, [])

    def test_verify_destination_performs_one_direct_action_and_requires_post_evidence(self) -> None:
        reader = _ActionReader([_sectioned_sidebar_snapshots(), _post_selected_sidebar_snapshots()])
        notices: list[tuple[str, str]] = []

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                settle_seconds=0,
                before_action_callback=lambda kind, title: notices.append((kind, title)),
            )

        self.assertEqual(result["status"], "verified_destination_changed")
        self.assertEqual(reader.actions, [("W.1.7", "AXPress")])
        self.assertEqual(result["actions_performed"], [{"path": "W.1.7", "action": "AXPress"}])
        self.assertEqual(notices, [("chat", "Markdown Formatting Guide")])

    def test_verify_destination_never_reports_success_without_post_action_evidence(self) -> None:
        reader = _ActionReader([_sectioned_sidebar_snapshots(), _sectioned_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                settle_seconds=0,
            )

        self.assertEqual(result["status"], "action_performed_no_observable_change")
        self.assertFalse(result["ok"])
        self.assertEqual(reader.actions, [("W.1.7", "AXPress")])

    def test_verify_destination_reports_inconclusive_changed_identity_without_success(self) -> None:
        reader = _ActionReader([_sectioned_sidebar_snapshots(), _post_selected_sidebar_snapshots("Agent Loop Notes")])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                settle_seconds=0,
            )

        self.assertEqual(result["status"], "destination_changed_but_identity_unverified")
        self.assertFalse(result["ok"])

    def test_verify_destination_non_macos_and_accessibility_failure_are_safe(self) -> None:
        with mock.patch.object(nav.sys, "platform", "linux"):
            non_macos = nav.verify_chatgpt_sidebar_destination(kind="chat", title="Markdown Formatting Guide")

        with mock.patch.object(nav.sys, "platform", "darwin"):
            failure = nav.verify_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=lambda app_name, max_depth, max_nodes: (_ for _ in ()).throw(RuntimeError("AX denied")),
                settle_seconds=0,
            )

        self.assertEqual(non_macos["status"], "accessibility_failure")
        self.assertEqual(non_macos["actions_performed"], [])
        self.assertEqual(failure["status"], "accessibility_failure")
        self.assertEqual(failure["actions_performed"], [])

    def test_read_only_navigation_diagnostics_introduce_no_disallowed_ui_automation_channels(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        read_only_source = "\n".join(
            [
                source[source.index("def inspect_chatgpt_navigation_ui"): source.index("def resolve_chatgpt_process")],
                source[
                    source.index("def inspect_chatgpt_sidebar_destination"):
                    source.index("def verify_chatgpt_sidebar_frame_click")
                ],
            ]
        )
        for token in ("CGEvent", "osascript", "AppleScript", "selenium", "playwright", "pyautogui"):
            self.assertNotIn(token, read_only_source)

    def test_deep_inspector_resolves_exact_project_and_collects_local_evidence(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "verified_press_target_found")
        self.assertEqual(result["target"]["title_ax_path"], "W.1.3.1")
        paths = {element["path"] for element in result["elements"]}
        self.assertIn("W.1.3", paths)
        self.assertIn("W.1.3.3", paths)
        linked = next(element for element in result["elements"] if element["path"] == "W.1.3")
        self.assertEqual(linked["linked_elements"][0]["path"], "W.1.3.3")
        self.assertEqual(reader.actions, [])

    def test_deep_inspector_resolves_exact_chat_as_menu_only(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "menu_only_target")
        self.assertEqual(result["target"]["title_ax_path"], "W.1.7")
        self.assertEqual(result["primary_selection_assessment"]["viable_candidate_controls"][0]["concrete_advertised_actions"], ["AXShowMenu"])

    def test_deep_inspector_duplicate_and_missing_targets_fail_closed(self) -> None:
        duplicate = _detailed_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.9", depth=2, role="AXButton", title="Markdown Formatting Guide", actions=("AXShowMenu",), enabled=True)
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            missing = nav.inspect_chatgpt_sidebar_destination(
                kind="chat",
                title="Not Visible",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([_detailed_sidebar_snapshots()])),
            )
            ambiguous = nav.inspect_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([duplicate])),
            )

        self.assertEqual(missing["status"], "target_not_found")
        self.assertEqual(missing["actions_performed"], [])
        self.assertEqual(ambiguous["status"], "target_ambiguous")
        self.assertEqual(ambiguous["actions_performed"], [])

    def test_deep_inspector_bounds_descendants_siblings_and_related_rows(self) -> None:
        snapshots = _detailed_sidebar_snapshots()
        for index in range(120):
            snapshots.append(
                nav.AXElementSnapshot(
                    path=f"W.1.3.4.{index + 1}",
                    depth=4,
                    role="AXStaticText",
                    value=f"Deep {index}",
                )
            )
        for index in range(40):
            snapshots.append(
                nav.AXElementSnapshot(
                    path=f"W.1.{20 + index}",
                    depth=2,
                    role="AXButton",
                    title=f"Sibling Secret {index}",
                    actions=("AXShowMenu",),
                    enabled=True,
                )
            )
        reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertLessEqual(result["scope"]["row_descendant_count"], nav.DEEP_INSPECTOR_ROW_DESCENDANT_MAX_NODES)
        self.assertLessEqual(result["scope"]["sibling_count"], nav.DEEP_INSPECTOR_SIBLING_MAX_NODES)
        list_element = next(element for element in result["elements"] if element["path"] == "W.1")
        self.assertEqual(list_element["row_structure"]["AXRows"], ["W.1.3", "W.1.7"])
        self.assertEqual(list_element["row_structure"]["AXVisibleRows"], ["W.1.3", "W.1.7"])

    def test_deep_inspector_redacts_long_and_unrelated_text(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        encoded = json.dumps(result, sort_keys=True)
        self.assertIn("PTG Assistant", encoded)
        self.assertNotIn("Conversation transcript secret", encoded)
        self.assertNotIn("Unrelated Confidential Chat", encoded)
        self.assertNotIn("x" * 100, encoded)
        long_node = next(element for element in result["elements"] if element["path"] == "W.1.3.2")
        self.assertTrue(long_node["value"]["redacted"])

    def test_deep_inspector_reports_supported_and_settable_attributes(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        row = next(element for element in result["elements"] if element["path"] == "W.1.3")
        self.assertTrue(row["supported_attributes"]["AXSelected"])
        self.assertTrue(row["settable_attributes"]["AXSelected"])
        press = next(control for control in result["primary_selection_assessment"]["viable_candidate_controls"] if control["target_path"] == "W.1.3.3")
        self.assertTrue(press["supported_and_settable_selection_focus_attributes"]["AXFocused"])

    def test_deep_inspector_reports_frame_evidence(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        frame = result["frame_evidence"]["computed_row_node"]["frame"]
        self.assertTrue(frame["valid"])
        self.assertTrue(frame["fully_inside_window"])
        self.assertTrue(frame["inside_sidebar_or_list"])
        self.assertTrue(frame["large_enough_for_safe_interior_click"])
        self.assertEqual(result["frame_evidence"]["chosen_click_source"]["path"], "W.1.7")

    def test_deep_inspector_source_slice_does_not_perform_ax_actions(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        inspector_source = source[
            source.index("def inspect_chatgpt_sidebar_destination"):
            source.index("def verify_chatgpt_sidebar_destination")
        ]
        self.assertNotIn("AXUIElementPerformAction", inspector_source)
        self.assertNotIn("perform_action(", inspector_source)

    def test_deep_inspector_compact_output_is_bounded_and_deterministic(self) -> None:
        result = {
            "ok": True,
            "read_only": True,
            "status": "verified_press_target_found",
            "app_name": "ChatGPT",
            "kind": "project",
            "title": "PTG Assistant",
            "pid_present": True,
            "process_resolution_method": "fake",
            "target": {
                "title_ax_path": "W.1.3.1",
                "computed_row_ax_path": "W.1.3",
                "current_resolution_method": "menu_only_target",
            },
            "scope": {
                "retained_element_count": 1,
                "row_descendant_count": 1,
                "sibling_count": 1,
                "related_count": 1,
            },
            "primary_selection_assessment": {
                "classification": "verified_press_target_found",
                "viable_candidate_controls": [
                    {
                        "target_path": "W.1.3.3",
                        "relation_to_requested_title": "row_descendant",
                        "confidence": "high",
                        "concrete_advertised_actions": ["AXPress"],
                        "supported_and_settable_selection_focus_attributes": {"AXFocused": True},
                        "why_primary_selection": "advertised AXPress on retained title/row structure",
                    }
                ],
            },
            "elements": [],
            "error": "",
        }

        output = "\n".join(cli._inspect_chatgpt_sidebar_destination_result_lines(result))

        self.assertLessEqual(len(output), nav.DEEP_INSPECTOR_OUTPUT_CHAR_GUARD + 512)
        self.assertIn("primary_selection_classification: verified_press_target_found", output)
        self.assertIn("target_title_path: W.1.3.1", output)

    def test_coordinate_calibration_classifies_raw_global_frames(self) -> None:
        hit = {
            "available": True,
            "path": "W.1.3.1",
            "role": "AXStaticText",
            "subrole": "",
            "title": {"literal": "PTG Assistant"},
            "parent_chain": [{"path": "W.1.3", "role": "AXButton", "subrole": "", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([_detailed_sidebar_snapshots()], hit)

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(59.6, 128.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["final_mapping_classification"], "ax_frames_are_global")
        self.assertEqual(result["recommended_runtime_click_transform"], "raw")
        self.assertEqual(result["hit_test_relationship_to_requested_target"], "exact_target_title")
        self.assertEqual(result["actions_performed"], [])
        self.assertEqual(reader.actions, [])
        self.assertEqual(reader.hit_tests, [(123, (59.6, 128.0), "PTG Assistant")])

    def test_coordinate_calibration_classifies_windowserver_translation(self) -> None:
        snapshots = [
            snapshot if snapshot.path != "W" else nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                title=snapshot.title,
                frame=(0, 0, 1920, 1080),
            )
            for snapshot in _local_coordinate_sidebar_snapshots()
        ]
        hit = {
            "available": True,
            "path": "W.1.3",
            "role": "AXButton",
            "subrole": "",
            "title": {"literal": ""},
            "parent_chain": [{"path": "W.1", "role": "AXList", "subrole": "AXSectionList", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([snapshots], hit)

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(159.6, 328.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 10, "bounds": (100, 200, 1200, 900)}])
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["final_mapping_classification"], "target_frame_needs_windowserver_translation")
        self.assertEqual(result["recommended_runtime_click_transform"], "windowserver_translation")
        best = [
            item for item in result["mapping_candidates"]
            if item["classification_if_unique"] == "target_frame_needs_windowserver_translation"
        ][0]
        self.assertEqual(best["distance_from_cursor_px"], 0.0)

    def test_coordinate_calibration_fails_closed_for_duplicate_target(self) -> None:
        duplicate = _detailed_sidebar_snapshots() + [
            nav.AXElementSnapshot(
                path="W.1.4.1",
                depth=3,
                role="AXStaticText",
                value="PTG Assistant",
                enabled=True,
                frame=(10, 320, 250, 32),
            )
        ]
        reader = _CalibrationReader([duplicate], {"available": True, "path": "W.1.3.1"})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(92.5, 128.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
                ),
            )

        self.assertEqual(result["status"], "target_ambiguous")
        self.assertEqual(result["final_mapping_classification"], "target_or_window_frame_unavailable")
        self.assertEqual(result["actions_performed"], [])
        self.assertEqual(reader.actions, [])

    def test_coordinate_calibration_requires_cursor_over_requested_target_structure(self) -> None:
        hit = {
            "available": True,
            "path": "W.1.8",
            "role": "AXButton",
            "subrole": "",
            "title": {"literal": ""},
            "parent_chain": [{"path": "W.1", "role": "AXList", "subrole": "AXSectionList", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([_detailed_sidebar_snapshots()], hit)

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(92.5, 128.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["final_mapping_classification"], "cursor_not_over_requested_target")
        self.assertEqual(result["recommended_runtime_click_transform"], "unresolved")

    def test_coordinate_calibration_reports_ambiguous_mapping_when_distinct_candidates_match(self) -> None:
        candidates = [
            {
                "classification_if_unique": "ax_frames_are_global",
                "candidate_point": {"x": 100.0, "y": 100.0},
                "distance_from_cursor_px": 0.0,
                "inside_target_hit_test_relationship": True,
                "inside_target_row_frame_under_interpretation": True,
                "inside_target_title_frame_under_interpretation": False,
                "candidate_explains_cursor_within_tolerance": True,
            },
            {
                "classification_if_unique": "target_frame_needs_ancestor_translation",
                "candidate_point": {"x": 102.0, "y": 100.0},
                "distance_from_cursor_px": 2.0,
                "inside_target_hit_test_relationship": True,
                "inside_target_row_frame_under_interpretation": True,
                "inside_target_title_frame_under_interpretation": False,
                "candidate_explains_cursor_within_tolerance": True,
            },
        ]

        classification = nav._classify_coordinate_mapping(
            candidates,
            hit_test_relationship="descendant_of_target_row",
            cursor_point=(100.0, 100.0),
            target_frame_available=True,
            window_frame_available=True,
        )

        self.assertEqual(classification, "ambiguous_coordinate_mapping")

    def test_coordinate_calibration_confirmed_mode_posts_two_calculated_clicks_then_confirms(self) -> None:
        hit = {
            "available": True,
            "path": "W.1.3.1",
            "role": "AXStaticText",
            "subrole": "",
            "title": {"literal": "PTG Assistant"},
            "parent_chain": [{"path": "W.1.3", "role": "AXButton", "subrole": "", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([_detailed_sidebar_snapshots(), _post_selected_sidebar_snapshots("PTG Assistant")], hit)
        clicker = _ClickService()
        sleeper = _SleepRecorder()
        notices: list[str] = []

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                confirm_calibration_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(999.0, 999.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
                ),
                click_service_factory=_ClickFactory(clicker),
                sleep_function=sleeper,
                before_click_callback=lambda: notices.append("authorized"),
            )

        self.assertEqual(result["final_click_classification"], "click_confirmed_mapping_success")
        self.assertEqual(result["recommended_runtime_click_transform"], "raw")
        self.assertEqual(result["click_count"], 2)
        self.assertEqual(result["inter_click_delay_ms"], 500)
        self.assertEqual(clicker.clicks, [(59.6, 128.0), (59.6, 128.0)])
        self.assertEqual(sleeper.calls[0], 0.5)
        self.assertGreaterEqual(len(sleeper.calls), 2)
        self.assertEqual([event["event"] for event in result["actions_performed"]], ["left_mouse_down", "left_mouse_up", "left_mouse_down", "left_mouse_up"])
        self.assertEqual(reader.collect_calls, 2)
        self.assertEqual(notices, ["authorized"])

    def test_coordinate_calibration_confirmed_mode_does_not_use_current_cursor_as_click_target(self) -> None:
        hit = {
            "available": True,
            "path": "W.1.3.1",
            "role": "AXStaticText",
            "subrole": "",
            "title": {"literal": "PTG Assistant"},
            "parent_chain": [{"path": "W.1.3", "role": "AXButton", "subrole": "", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([_detailed_sidebar_snapshots(), _post_selected_sidebar_snapshots("PTG Assistant")], hit)
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                confirm_calibration_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(1.0, 1.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
                ),
                click_service_factory=_ClickFactory(clicker),
                sleep_function=_SleepRecorder(),
            )

        self.assertNotEqual(clicker.clicks[0], (1.0, 1.0))
        self.assertEqual(result["calculated_global_click_point"], {"x": 59.6, "y": 128.0})

    def test_coordinate_calibration_confirmed_mode_requires_post_click_destination_confirmation(self) -> None:
        hit = {
            "available": True,
            "path": "W.1.3.1",
            "role": "AXStaticText",
            "subrole": "",
            "title": {"literal": "PTG Assistant"},
            "parent_chain": [{"path": "W.1.3", "role": "AXButton", "subrole": "", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([_detailed_sidebar_snapshots(), _detailed_sidebar_snapshots()], hit)
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                confirm_calibration_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(59.6, 128.0))),
                windowserver_probe_factory=_WindowServerFactory(
                    _WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
                ),
                click_service_factory=_ClickFactory(clicker),
                sleep_function=_SleepRecorder(),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["final_click_classification"], "click_posted_but_destination_not_confirmed")
        self.assertEqual(len(clicker.clicks), 2)
        self.assertFalse(result["post_click_requested_destination_evidence"]["active_destination_confirmed"])
        self.assertEqual(result["recommended_runtime_click_transform"], "unresolved")

    def test_coordinate_calibration_confirmed_mode_fails_before_click_when_safe_point_unavailable(self) -> None:
        hit = {
            "available": True,
            "path": "W.1.3.1",
            "role": "AXStaticText",
            "subrole": "",
            "title": {"literal": "PTG Assistant"},
            "parent_chain": [{"path": "W.1.3", "role": "AXButton", "subrole": "", "title": {"literal": ""}}],
        }
        reader = _CalibrationReader([_detailed_sidebar_snapshots()], hit)
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.calibrate_chatgpt_sidebar_coordinate_mapping(
                kind="project",
                title="PTG Assistant",
                confirm_calibration_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe(cursor=(59.6, 128.0))),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([])),
                click_service_factory=_ClickFactory(clicker),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["final_click_classification"], "safe_click_point_unavailable")
        self.assertEqual(clicker.clicks, [])
        self.assertEqual(result["actions_performed"], [])

    def test_coordinate_calibration_hit_test_geometry_accepts_unnamed_button_over_target(self) -> None:
        snapshots = _detailed_sidebar_snapshots()
        scope = {"title_path": "W.1.3.1", "row_path": "W.1.3"}
        snapshots_by_path = {snapshot.path: snapshot for snapshot in snapshots}
        hit = {
            "available": True,
            "path": "",
            "role": "AXButton",
            "subrole": "",
            "frame": {"x": 10, "y": 112, "width": 250, "height": 32},
            "parent_chain": [],
        }

        relationship = nav._hit_test_relationship(hit, scope, snapshots_by_path)

        self.assertEqual(relationship, "ancestor_of_target_title")

    def test_coordinate_calibration_compact_output_has_required_final_transform_line(self) -> None:
        result = nav._base_coordinate_calibration_result("project", "PTG Assistant", "ChatGPT")
        result.update(
            {
                "status": "calibration_completed",
                "ok": True,
                "pid_present": True,
                "process_resolution_method": "fake",
                "current_global_physical_cursor_location": {"x": 92.5, "y": 128.0},
                "target": {"title_ax_path": "W.1.3.1", "computed_row_ax_path": "W.1.3"},
                "hit_test": {"path": "W.1.3.1", "role": "AXStaticText", "subrole": ""},
                "hit_test_relationship_to_requested_target": "exact_target_title",
                "frame_evidence": [
                    {"source": "target_title_frame", "ax_path": "W.1.3.1", "x": 20, "y": 120, "width": 120, "height": 16},
                    {"source": "computed_row_frame", "ax_path": "W.1.3", "x": 10, "y": 112, "width": 250, "height": 32},
                    {"source": "chatgpt_ax_window_frame", "ax_path": "W", "x": 0, "y": 0, "width": 1200, "height": 900},
                ],
                "windowserver_evidence": {"chosen_window": {"bounds": {"x": 0, "y": 0, "width": 1200, "height": 900}}},
                "mapping_candidates": [
                    {
                        "mapping_name": "raw_ax_interpretation",
                        "candidate_point": {"x": 92.5, "y": 128.0},
                        "distance_from_cursor_px": 0,
                        "inside_actual_visible_chatgpt_window_bounds": True,
                        "inside_target_hit_test_relationship": True,
                        "inside_target_title_frame_under_interpretation": True,
                        "inside_target_row_frame_under_interpretation": True,
                    }
                ],
                "final_mapping_classification": "ax_frames_are_global",
                "recommended_future_click_transform": "raw",
                "recommended_runtime_click_transform": "raw",
            }
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli._print_calibrate_chatgpt_sidebar_coordinate_mapping_result(result)

        text = stdout.getvalue()
        self.assertIn("final_mapping_classification: ax_frames_are_global", text)
        self.assertTrue(text.rstrip().endswith("recommended_runtime_click_transform: raw"))

    def test_coordinate_calibration_source_slice_does_not_post_events_or_perform_actions(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        calibration_source = source[
            source.index("def calibrate_chatgpt_sidebar_coordinate_mapping"):
            source.index("class _CoreGraphicsDisplayProbe")
        ]
        for token in (
            "AXUIElementPerformAction",
            "CGEventCreateMouseEvent",
            "CGEventPost",
            "CGWarpMouseCursorPosition",
            "activate_chatgpt",
            "paste_clipboard",
            "press_enter",
            "AppleScript",
            "osascript",
            "selenium",
            "playwright",
            "write_text",
        ):
            self.assertNotIn(token, calibration_source)
        self.assertIn("_CoreGraphicsFrameClickService", calibration_source)

    def test_project_visible_chats_requires_project_header_not_sidebar_title(self) -> None:
        snapshots = [
            snapshot
            for snapshot in _project_visible_chats_snapshots()
            if snapshot.path != "W.2.1"
        ]
        reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "project_not_open")
        self.assertFalse(result["ok"])
        self.assertEqual(result["actions_performed"], [])
        self.assertGreater(result["excluded_candidate_counts"]["left_navigation_sidebar"], 0)

    def test_project_visible_chats_reports_ambiguous_content_project_identity(self) -> None:
        snapshots = _project_visible_chats_snapshots() + [
            nav.AXElementSnapshot(path="W.3", depth=1, role="AXGroup", identifier="second-project-content", frame=(700, 0, 500, 360)),
            nav.AXElementSnapshot(path="W.3.1", depth=2, role="AXHeading", value="PTG Assistant", frame=(760, 42, 300, 38)),
            nav.AXElementSnapshot(path="W.3.2", depth=2, role="AXButton", title="Chats", actions=("AXPress",), frame=(760, 94, 80, 30)),
            nav.AXElementSnapshot(path="W.3.3", depth=2, role="AXButton", title="Sources", actions=("AXPress",), frame=(848, 94, 90, 30)),
        ]
        reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "project_identity_ambiguous")
        self.assertEqual(result["visible_chats"], [])

    def test_project_visible_chats_classifies_rows_and_excludes_header_tabs_sidebar_controls(self) -> None:
        reader = _ActionReader([_project_visible_chats_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "visible_chats_found")
        self.assertTrue(result["ok"])
        self.assertEqual(result["visible_chat_count"], 3)
        titles = [chat["title"] for chat in result["visible_chats"]]
        self.assertEqual(
            titles,
            [
                "Apple Content Moderation Requirements",
                "Profile Photo Verification Change",
                "Partially Visible Below Fold",
            ],
        )
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn("Unrelated Sidebar Chat", encoded)
        self.assertNotIn("Composer draft must not be treated as a row", encoded)
        self.assertNotIn('"title": "Chats"', encoded)
        self.assertNotIn('"title": "Sources"', encoded)
        self.assertEqual(reader.actions, [])
        self.assertEqual(result["actions_performed"], [])

    def test_shared_project_resolver_accepts_unexpected_header_role_with_tabs(self) -> None:
        snapshots = [
            nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role="AXUnknown" if snapshot.path == "W.2.1" else snapshot.role,
                subrole=snapshot.subrole,
                identifier=snapshot.identifier,
                title=snapshot.title,
                description=snapshot.description,
                value=snapshot.value,
                enabled=snapshot.enabled,
                actions=snapshot.actions,
                frame=snapshot.frame,
            )
            for snapshot in _project_visible_chats_snapshots()
        ]

        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))

        self.assertEqual(result["status"], "visible_chats_found")
        self.assertTrue(result["project_identity_confirmed"])
        self.assertTrue(result["chats_tab_confirmed"])
        self.assertEqual(result["visible_chat_count"], 3)

    def test_shared_project_resolver_requires_main_chats_sources_relationship(self) -> None:
        snapshots = [
            snapshot
            for snapshot in _project_visible_chats_snapshots()
            if snapshot.path not in {"W.2.2", "W.2.3"}
        ]

        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))

        self.assertEqual(result["status"], "project_open_but_chats_tab_not_confirmed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["visible_chats"], [])

    def test_project_visible_chats_preserves_top_to_bottom_order_and_partial_visibility(self) -> None:
        reader = _ActionReader([_project_visible_chats_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual([chat["ordinal"] for chat in result["visible_chats"]], [1, 2, 3])
        self.assertEqual(result["visible_chats"][0]["visibility"], "fully_visible")
        self.assertEqual(result["visible_chats"][2]["visibility"], "partially_clipped")
        self.assertEqual(result["more_rows_may_exist_below"], True)
        self.assertEqual(result["project_content_container"]["path"], "W.2")
        self.assertEqual(result["chat_list_container"]["path"], "W.2.5")

    def test_project_visible_chats_preview_association_and_truncation(self) -> None:
        reader = _ActionReader([_project_visible_chats_snapshots(long_preview=True)])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        preview = result["visible_chats"][1]["preview"]
        self.assertLessEqual(len(preview), nav.PROJECT_VISIBLE_CHAT_PREVIEW_MAX_LENGTH)
        self.assertTrue(preview.endswith("..."))
        self.assertIn("365 day lockout", preview)

    def test_project_chat_row_ax_audit_reports_merged_row_label_and_separate_descendants(self) -> None:
        reader = _ActionReader([_merged_button_project_chat_row_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_chat_row_ax(
                project_title="PTG Assistant",
                chat_titles=["Content Moderation"],
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "row_audit_ready")
        self.assertTrue(result["ok"])
        audit = result["row_audits"][0]
        self.assertEqual(audit["accepted_row"]["row_path"], "W.1.4.1")
        self.assertIn("Content Moderation, wait so", audit["raw_flattened_row_text"])
        self.assertTrue(audit["summary"]["row_exposes_merged_text"])
        self.assertEqual(audit["summary"]["exact_title_node_paths"], ["W.1.4.1.1"])
        self.assertIn("W.1.4.1.3", audit["summary"]["preview_like_node_paths"])
        self.assertIn("W.1.4.1.2", audit["summary"]["punctuation_only_node_paths"])
        node_by_path = {node["path"]: node for node in audit["nodes"]}
        self.assertEqual(node_by_path["W.1.4.1"]["role"], "AXButton")
        self.assertEqual(node_by_path["W.1.4.1"]["AXTitle"], "Content Moderation, wait so how do whatsapp and instagram and X function")
        self.assertEqual(node_by_path["W.1.4.1.1"]["text_classification"], "title-like")
        self.assertEqual(node_by_path["W.1.4.1.2"]["text_classification"], "punctuation-only")
        self.assertEqual(node_by_path["W.1.4.1.3"]["text_classification"], "preview-like")
        self.assertNotIn("Composer text must stay outside row audit", json.dumps(audit, sort_keys=True))

    def test_project_chat_row_ax_audit_supports_second_row_comparison(self) -> None:
        reader = _ActionReader([_merged_button_project_chat_row_snapshots()])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_chat_row_ax(
                project_title="PTG Assistant",
                chat_titles=["Content Moderation", "AWS Profile Photo Verification"],
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "row_audit_ready")
        self.assertEqual(len(result["row_audits"]), 2)
        first, second = result["row_audits"]
        self.assertEqual(first["accepted_row"]["row_path"], "W.1.4.1")
        self.assertEqual(second["accepted_row"]["row_path"], "W.1.4.2")
        self.assertEqual(second["summary"]["exact_title_node_paths"], ["W.1.4.2.1"])
        self.assertIn("W.1.4.2.2", second["summary"]["preview_like_node_paths"])

    def test_project_chat_row_ax_audit_fails_closed_for_ambiguous_row_subtree_match(self) -> None:
        snapshots = _merged_button_project_chat_row_snapshots() + [
            nav.AXElementSnapshot(
                path="W.1.4.3",
                depth=3,
                role="AXButton",
                title="Content Moderation, duplicate",
                actions=("AXPress",),
                enabled=True,
                frame=(282, 390, 779, 65),
            )
        ]
        reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_chat_row_ax(
                project_title="PTG Assistant",
                chat_titles=["Content Moderation"],
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "audit_row_ambiguous")
        self.assertFalse(result["ok"])
        self.assertEqual(result["row_audits"][0]["match_count"], 2)

    def test_project_chat_row_ax_source_slice_is_read_only(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        audit_source = source[
            source.index("def inspect_chatgpt_project_chat_row_ax"):
            source.index("def resolve_open_project_content_and_visible_chats")
        ]
        for token in (
            "activate_chatgpt",
            "perform_action",
            "left_click",
            "CGEvent",
            "CGWarpMouseCursorPosition",
            "current_mouse_location",
            "AXUIElementPerformAction",
            "paste_clipboard",
            "press_enter",
            "AXShowMenu",
            "osascript",
            "selenium",
            "playwright",
            "write_text",
        ):
            self.assertNotIn(token, audit_source)

    def test_project_visible_chats_canonical_model_separates_title_preview_and_excludes_punctuation(self) -> None:
        snapshots = _project_visible_chats_snapshots() + [
            nav.AXElementSnapshot(path="W.2.5.4", depth=3, role="AXGroup", actions=("AXPress",), enabled=True, frame=(320, 304, 760, 70)),
            nav.AXElementSnapshot(path="W.2.5.4.1", depth=4, role="AXStaticText", value=",", frame=(340, 322, 10, 20)),
            nav.AXElementSnapshot(path="W.2.5.4.2", depth=4, role="AXStaticText", value="Only punctuation should not become a row", frame=(340, 346, 440, 18)),
        ]

        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))

        titles = [chat["title"] for chat in result["visible_chats"]]
        self.assertNotIn(",", titles)
        chat = result["visible_chats"][1]
        self.assertEqual(chat["title"], "Profile Photo Verification Change")
        self.assertEqual(chat["preview"], "okay, is there a 365 day lockout...")
        self.assertEqual(chat["row_path"], "W.2.5.2")
        self.assertEqual(chat["title_path"], "W.2.5.2.1")
        self.assertEqual(chat["title_role"], "AXStaticText")
        self.assertEqual(chat["action_names"], ["AXPress"])

    def test_window_titled_project_uses_tabs_not_window_bottom_for_chat_list(self) -> None:
        snapshots = []
        for snapshot in _scrollable_project_chat_page(["AI Engineering Operator"]):
            if snapshot.path == "W":
                snapshots.append(
                    nav.AXElementSnapshot(
                        path="W", depth=0, role="AXWindow", title="Watch to Codex", frame=snapshot.frame
                    )
                )
            elif snapshot.path == "W.1.1":
                snapshots.append(
                    nav.AXElementSnapshot(
                        path=snapshot.path, depth=snapshot.depth, role="AXHeading", value="", frame=snapshot.frame
                    )
                )
            else:
                snapshots.append(snapshot)

        result = nav.resolve_open_project_content_and_visible_chats(
            "Watch to Codex", snapshots, (0, 0, 1200, 900)
        )

        self.assertEqual(result["status"], "visible_chats_found")
        self.assertEqual(result["project_content_container"]["path"], "W")
        self.assertEqual(result["chat_list_container"]["path"], "W.1.4")
        self.assertEqual(result["visible_chats"][0]["title"], "AI Engineering Operator")

    def test_project_chat_open_matches_clean_title_only_not_preview_or_partial(self) -> None:
        project_open = mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0, "visible_chats": []})
        reader = _AutonomousReader([_project_visible_chats_snapshots()] * 2, {"available": True, "path": "W.2.5.2.1"})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            preview = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="okay, is there a 365 day lockout...",
                confirm_open_chat=False,
                open_project_function=project_open,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )
            partial = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo",
                confirm_open_chat=False,
                open_project_function=project_open,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_project_visible_chats_snapshots()] * 2, {"available": True, "path": "W.2.5.2.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(preview["outcome"], "chat_title_not_unambiguously_representable_by_accessibility")
        self.assertEqual(partial["outcome"], "chat_not_currently_visible")
        self.assertEqual(preview["actions_performed"], [])
        self.assertEqual(partial["actions_performed"], [])

    def test_project_chat_open_preserves_bounded_project_failure_diagnostics(self) -> None:
        project_open = mock.Mock(
            return_value={
                "ok": False,
                "outcome": "target_absent",
                "target_match_count": 0,
                "activation_stability": {"status": "stable"},
                "traversal": {
                    "truncated_by_node_limit": True,
                    "truncated_by_depth_limit": False,
                },
                "error": "No exactly matching visible sidebar destination was found.",
            }
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Push Notification Analytics Planning",
                confirm_open_chat=True,
                open_project_function=project_open,
            )

        self.assertEqual(result["outcome"], "project_open_failed")
        self.assertEqual(result["project_open_result"]["outcome"], "target_absent")
        self.assertEqual(result["project_open_result"]["target_match_count"], 0)
        self.assertEqual(result["project_open_result"]["activation_stability_status"], "stable")
        self.assertTrue(result["project_open_result"]["traversal"]["truncated_by_node_limit"])
        self.assertFalse(result["project_open_result"]["traversal"]["truncated_by_depth_limit"])

    def test_project_chat_open_matches_exact_accessibility_row_text(self) -> None:
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_exact_text_project_chat_row_snapshots()], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["matched_chat_title"], "Content Moderation")
        self.assertEqual(result["matched_title_representation"], "exact_accessibility_text")

    def test_project_chat_open_matches_requested_exact_prefix_before_separator(self) -> None:
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_leaf_merged_project_chat_row_snapshots()], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["matched_chat_title"], "Content Moderation")
        self.assertEqual(result["matched_title_representation"], "requested_exact_prefix_before_preview_separator")
        self.assertTrue(result["matched_accessibility_text_truncated"].startswith("Content Moderation, wait so"))

    def test_project_chat_open_matches_aws_prefix_only_for_aws_request(self) -> None:
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="AWS Profile Photo Verification",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_leaf_merged_project_chat_row_snapshots()], {"available": True, "path": "W.1.4.2"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["matched_chat_row"]["row_path"], "W.1.4.2")
        self.assertEqual(result["matched_chat_title"], "AWS Profile Photo Verification")

    def test_project_chat_open_rejects_preview_only_and_partial_title_requests(self) -> None:
        with mock.patch.object(nav.sys, "platform", "darwin"):
            preview = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="wait so how do whatsapp",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_leaf_merged_project_chat_row_snapshots()], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )
            partial = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_leaf_merged_project_chat_row_snapshots()], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(preview["outcome"], "chat_not_currently_visible")
        self.assertEqual(partial["outcome"], "chat_not_currently_visible")
        self.assertEqual(preview["actions_performed"], [])
        self.assertEqual(partial["actions_performed"], [])

    def test_project_chat_open_rejects_requested_title_containing_comma(self) -> None:
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Policy, Safety and Trust",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_exact_text_project_chat_row_snapshots("Policy, Safety and Trust, preview")], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_title_not_unambiguously_representable_by_accessibility")
        self.assertEqual(result["actions_performed"], [])
        self.assertFalse(result["target_exact_match_detected"])

    def test_project_chat_open_duplicate_accessibility_prefix_matches_fail_ambiguous(self) -> None:
        snapshots = _leaf_merged_project_chat_row_snapshots() + [
            nav.AXElementSnapshot(
                path="W.1.4.3",
                depth=3,
                role="AXButton",
                description="Content Moderation, another visible preview",
                actions=("AXPress",),
                enabled=True,
                frame=(282, 390, 779, 65),
            )
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_title_ambiguous")
        self.assertEqual(result["actions_performed"], [])

    def test_project_chat_open_matches_content_moderation_from_same_resolver_output_as_inspector(self) -> None:
        snapshots = _content_moderation_project_snapshots()
        inspector_reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            inspector = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(inspector_reader),
            )
            opener = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.2.5.1.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(inspector["visible_chats"][0]["title"], "Content Moderation")
        self.assertEqual(opener["outcome"], "dry_run_ready")
        self.assertEqual(opener["matched_chat_row"]["title"], "Content Moderation")
        self.assertEqual(opener["matched_chat_row"]["row_path"], inspector["visible_chats"][0]["row_path"])

    def test_project_chat_open_uses_shared_resolver_rows_without_separate_canonical_construction(self) -> None:
        snapshots = _content_moderation_project_snapshots()
        resolver_output = _minimal_project_chat_resolution("Content Moderation")

        with mock.patch.object(nav.sys, "platform", "darwin"), mock.patch.object(
            nav,
            "resolve_open_project_content_and_visible_chats",
            return_value=resolver_output,
        ) as resolver:
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.2.5.1.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["matched_chat_row"]["title"], "Content Moderation")
        self.assertEqual(result["canonical_visible_chat_titles_considered"], ["Content Moderation"])

    def test_project_chat_open_title_plus_preview_container_does_not_break_exact_title_matching(self) -> None:
        snapshots = _content_moderation_project_snapshots(title_plus_preview_container=True)

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.2.5.1.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["matched_chat_row"]["title"], "Content Moderation")
        self.assertNotIn("Content Moderation Policy review preview", result["canonical_visible_chat_titles_considered"])

    def test_project_chat_open_uses_fresh_resolver_output_after_project_open_not_prior_visible_rows(self) -> None:
        snapshots = _content_moderation_project_snapshots()
        project_open = mock.Mock(
            return_value={
                "ok": True,
                "outcome": "destination_opened_and_visible_chats_resolved",
                "visible_chat_count": 1,
                "visible_chats": [{"title": "Old Project Result", "row_path": "W.old"}],
            }
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=project_open,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.2.5.1.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["matched_chat_row"]["title"], "Content Moderation")
        self.assertNotEqual(result["matched_chat_row"]["title"], "Old Project Result")

    def test_project_chat_open_surfaces_project_open_and_targeting_count_mismatch(self) -> None:
        snapshots = _content_moderation_project_snapshots()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 6}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.2.5.1.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["project_open_result"]["visible_chat_count"], 6)
        self.assertEqual(result["targeting_visible_chat_count"], result["visible_chat_count"])
        self.assertNotEqual(result["project_open_result"]["visible_chat_count"], result["targeting_visible_chat_count"])
        self.assertIn("fresh targeting AX snapshot", result["visible_chat_count_stage_explanation"])

    def test_project_chat_no_match_diagnostics_use_only_accepted_canonical_rows(self) -> None:
        snapshots = _content_moderation_project_snapshots() + [
            nav.AXElementSnapshot(path="W.2.5.9", depth=3, role="AXGroup", actions=("AXPress",), enabled=True, frame=(320, 460, 760, 70)),
            nav.AXElementSnapshot(path="W.2.5.9.1", depth=4, role="AXStaticText", value=",", frame=(340, 478, 10, 20)),
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Exact Title",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([snapshots], {"available": True, "path": "W.2.5.1.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_not_currently_visible")
        self.assertIn("Content Moderation", result["canonical_visible_chat_titles_considered"])
        self.assertNotIn(",", result["canonical_visible_chat_titles_considered"])
        self.assertNotIn("Policy review preview must not affect matching", result["canonical_visible_chat_titles_considered"])
        self.assertEqual(result["canonical_visible_chat_count_considered"], len(result["canonical_visible_chat_titles_considered"]))
        self.assertTrue(result["resolver_snapshot_id"].startswith("ax:"))

    def test_project_chat_open_duplicate_visible_titles_fail_closed(self) -> None:
        duplicate = _project_visible_chats_snapshots(partial_last_row=False) + [
            nav.AXElementSnapshot(path="W.2.5.4", depth=3, role="AXGroup", actions=("AXPress",), enabled=True, frame=(320, 380, 760, 70)),
            nav.AXElementSnapshot(path="W.2.5.4.1", depth=4, role="AXStaticText", value="Profile Photo Verification Change", frame=(340, 398, 360, 20)),
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready"}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([duplicate] * 2, {"available": True, "path": "W.2.5.2.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_title_ambiguous")
        self.assertEqual(result["actions_performed"], [])

    def test_project_chat_open_reuses_project_open_path_internally(self) -> None:
        project_open = mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=False,
                open_project_function=project_open,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_project_visible_chats_snapshots()] * 2, {"available": True, "path": "W.2.5.2.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        project_open.assert_called_once()
        self.assertEqual(project_open.call_args.kwargs["kind"], "project")
        self.assertEqual(project_open.call_args.kwargs["title"], "PTG Assistant")

    def test_project_chat_open_visible_first_snapshot_opens_without_scroll(self) -> None:
        reader = _AutonomousReader(
            [_scrollable_project_chat_page(["City-wise Restrictions"])] * 3
            + [_scroll_opened_conversation_snapshots("City-wise Restrictions")] * 2,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_via_axpress")
        self.assertEqual(result["scroll_iterations_attempted"], 0)
        self.assertTrue(result["target_initially_visible"])
        self.assertIn(("W.1.4.1", "AXPress"), reader.actions)

    def test_post_action_verification_accepts_deep_visible_conversation_title(self) -> None:
        snapshots = [
            nav.AXElementSnapshot(
                path="W",
                depth=0,
                role="AXWindow",
                title="ChatGPT",
                frame=(0, 0, 1200, 900),
            ),
            nav.AXElementSnapshot(
                path="W.1.1.3.1.1.1.2.1.1",
                depth=9,
                role="AXStaticText",
                value="City-wise Restrictions",
                frame=(320, 40, 500, 30),
            ),
            nav.AXElementSnapshot(
                path="W.1.2",
                depth=2,
                role="AXTextArea",
                title="Message ChatGPT",
                frame=(320, 820, 620, 44),
            ),
        ]

        signals = nav._project_chat_verification_signals(
            snapshots,
            {"status": "project_open_failed"},
            "City-wise Restrictions",
            {
                "matched_chat_row": {"row_path": "W.old-row"},
                "project_chat_resolution": {
                    "chat_list_container": {"path": "W.old-list"}
                },
            },
            ax_window_frame=(0, 0, 1200, 900),
        )

        self.assertIn(
            "active_conversation_identity_outside_chat_list",
            {signal["type"] for signal in signals},
        )

    def test_post_action_verification_rejects_deep_offscreen_conversation_title(self) -> None:
        snapshots = [
            nav.AXElementSnapshot(
                path="W",
                depth=0,
                role="AXWindow",
                title="ChatGPT",
                frame=(0, 0, 1200, 900),
            ),
            nav.AXElementSnapshot(
                path="W.1.1.3.1.1.1.2.1.1",
                depth=9,
                role="AXStaticText",
                value="City-wise Restrictions",
                frame=(320, -1985, 500, 30),
            ),
        ]

        signals = nav._project_chat_verification_signals(
            snapshots,
            {"status": "project_open_failed"},
            "City-wise Restrictions",
            {
                "matched_chat_row": {"row_path": "W.old-row"},
                "project_chat_resolution": {
                    "chat_list_container": {"path": "W.old-list"}
                },
            },
            ax_window_frame=(0, 0, 1200, 900),
        )

        self.assertNotIn(
            "active_conversation_identity_outside_chat_list",
            {signal["type"] for signal in signals},
        )

    def test_project_chat_open_reports_action_posted_when_open_action_is_pressed(self) -> None:
        # The structured chat_open_action_posted signal is emitted whenever the
        # open action is physically posted against the exact target row, which
        # lets the authoritative destination gate (not the navigator's own
        # heuristic) decide whether the supervised handoff may proceed.
        reader = _AutonomousReader(
            [_scrollable_project_chat_page(["City-wise Restrictions"])] * 3
            + [_scroll_opened_conversation_snapshots("City-wise Restrictions")] * 2,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertIs(result["chat_open_action_posted"], True)
        self.assertIn(("W.1.4.1", "AXPress"), reader.actions)

    def test_project_chat_axpress_prefers_enclosing_row_over_static_title_child(self) -> None:
        row = nav.AXElementSnapshot(
            path="W.1.4.1", depth=3, role="AXButton", actions=("AXPress",), enabled=True
        )
        title = nav.AXElementSnapshot(
            path="W.1.4.1.1", depth=4, role="AXStaticText", actions=("AXPress",), enabled=True
        )

        target = nav._project_chat_axpress_target(title, row)

        self.assertEqual(target["path"], row.path)
        self.assertEqual(target["relation"], "row_node")
        self.assertEqual(target["role"], "AXButton")

    def test_project_chat_axpress_rejects_static_title_and_allows_actionable_title_control(self) -> None:
        row_without_press = nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXGroup", enabled=True)
        static_title = nav.AXElementSnapshot(
            path="W.1.4.1.1", depth=4, role="AXStaticText", actions=("AXPress",), enabled=True
        )
        control_title = nav.AXElementSnapshot(
            path="W.1.4.1.1", depth=4, role="AXLink", actions=("AXPress",), enabled=True
        )

        self.assertEqual(nav._project_chat_axpress_target(static_title, row_without_press), {})
        target = nav._project_chat_axpress_target(control_title, row_without_press)
        self.assertEqual(target["path"], control_title.path)
        self.assertEqual(target["relation"], "title_node")

    def test_static_title_axpress_without_row_action_uses_validated_click_fallback(self) -> None:
        snapshots = []
        for snapshot in _project_visible_chats_without_axpress():
            if snapshot.path == "W.2.5.2.1":
                snapshots.append(
                    nav.AXElementSnapshot(
                        path=snapshot.path,
                        depth=snapshot.depth,
                        role="AXStaticText",
                        value=snapshot.value,
                        actions=("AXPress",),
                        enabled=True,
                        frame=snapshot.frame,
                    )
                )
            else:
                snapshots.append(snapshot)
        reader = _AutonomousReader(
            [snapshots] * 6,
            {"available": True, "path": "W.2.5.2.1", "role": "AXStaticText", "title": {"literal": "Profile Photo Verification Change"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["chosen_method"], "validated_geometry_click")
        self.assertNotIn(("W.2.5.2.1", "AXPress"), reader.actions)
        self.assertEqual(len(clicker.clicks), 1)

    def test_axpress_success_without_ui_change_records_unconfirmed_diagnostics(self) -> None:
        page = _scrollable_project_chat_page(["City-wise Restrictions"])
        reader = _AutonomousReader(
            [page] * 6,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "City-wise Restrictions"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "action_posted_but_chat_not_confirmed")
        self.assertEqual(result["axpress_result"], "success")
        self.assertEqual(result["chosen_method"], "axpress_then_validated_geometry_click")
        self.assertEqual(len(clicker.clicks), 1)
        self.assertFalse(result["axpress_post_action_evidence"]["confirmed"])
        self.assertFalse(result["ui_changed_after_action"])
        self.assertFalse(result["destination_confirmed"])

    def test_axpress_error_is_retained_and_does_not_post_action_without_click_fallback(self) -> None:
        class FailingPressReader(_AutonomousReader):
            def perform_action(self, path: str, action: str, *, action_context: dict | None = None) -> bool:
                self.actions.append((path, action))
                self.action_contexts.append(action_context)
                if action == "AXPress":
                    self.last_ax_action_result = {"path": path, "action": action, "error_code": -25205}
                    return False
                return True

        page = _scrollable_project_chat_page(["City-wise Restrictions"])
        reader = FailingPressReader(
            [page] * 5,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "City-wise Restrictions"}},
        )
        clicker = _ClickService(permitted=False)

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "click_posting_failed")
        self.assertTrue(result["axpress_attempted"])
        self.assertEqual(result["axpress_result"], "failed")
        self.assertEqual(result["ax_error_code"], -25205)
        self.assertFalse(result["chat_open_action_posted"])

    def test_project_chat_open_reports_no_action_posted_before_open_action(self) -> None:
        # A dry-run (confirm_open_chat=False) resolves the target but never posts
        # the open action, so the signal stays False and the handoff must not
        # treat it as navigation performed.
        reader = _AutonomousReader(
            [_scrollable_project_chat_page(["City-wise Restrictions"])] * 3,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertIs(result["chat_open_action_posted"], False)

    def test_project_chat_open_finds_target_after_one_controlled_scroll(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation", "AWS Profile Photo Verification"])
        page_2 = _scrollable_project_chat_page(["City-wise Restrictions", "Other Later Chat"], row_offset=10)
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_2, _scroll_opened_conversation_snapshots("City-wise Restrictions"), _scroll_opened_conversation_snapshots("City-wise Restrictions")],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 2}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["scroll_iterations_attempted"], 1)
        self.assertEqual(result["scroll_method_used"], "semantic_ax_scroll")
        self.assertFalse(result["target_initially_visible"])
        self.assertTrue(result["target_found_after_scrolling"])
        self.assertIn(("W.1.4", "AXScrollDown"), reader.actions)
        self.assertIn(("W.1.4.11", "AXPress"), reader.actions)

    def test_project_chat_open_finds_target_after_multiple_controlled_scrolls(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        page_3 = _scrollable_project_chat_page(["City-wise Restrictions"], row_offset=20)
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_3, page_3, page_3, page_3, page_3, opened, opened],
            {"available": True, "path": "W.1.4.21", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["scroll_iterations_attempted"], 2)
        self.assertEqual(reader.actions.count(("W.1.4", "AXScrollDown")), 2)
        self.assertIn(("W.1.4.21", "AXPress"), reader.actions)

    def test_project_chat_open_end_of_list_without_match_reports_end_reached(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        end_page = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10, scrollbar_value="1.0")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, end_page, end_page, end_page, end_page, end_page],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "AWS Profile Photo Verification, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_list_end_reached_without_match")
        self.assertEqual(result["end_of_list_state"], "confirmed")
        # Authoritative scrollbar bottom evidence plus one continuity-confirmed
        # unchanged forward cycle is sufficient to conclude the end.
        self.assertEqual(result["actions_performed"], [{"path": "W.1.4", "action": "AXScrollDown"}])
        self.assertEqual(result["scan_continuity"], "confirmed")

    def test_project_chat_repeated_identical_viewport_confirms_end_via_anchors(self) -> None:
        # Two complete forward scroll-plus-settle cycles whose top-most and
        # bottom-most rows never change confirm the end of the list via the
        # anchor-based fallback (no scrollbar bottom token is present here).
        page = _scrollable_project_chat_page(["Content Moderation"])
        reader = _AutonomousReader(
            [page, page, page, page, page, page, page],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Content Moderation, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_list_end_reached_without_match")
        self.assertEqual(result["end_of_list_state"], "confirmed")
        self.assertEqual(result["scroll_iterations_attempted"], 2)
        self.assertEqual(reader.actions.count(("W.1.4", "AXScrollDown")), 2)
        self.assertEqual(result["search_cycles_attempted"], 2)
        self.assertEqual(result["scroll_pulses_posted"], 2)
        self.assertNotEqual(result["outcome"], "chat_list_scroll_no_progress")

    def test_project_chat_scroll_reset_then_hydrate_exposes_new_rows_without_no_progress(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        page_3 = _scrollable_project_chat_page(["City-wise Restrictions"], row_offset=20)
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_2, page_2, page_2, page_1, page_3, page_3],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Content Moderation, preview text must not drive matching"}},
        )

        with mock.patch.object(nav, "MAX_PROJECT_CHAT_SEARCH_CYCLES", 2), mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Not In Loaded Rows",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_search_budget_exhausted_without_confirmed_end")
        self.assertGreaterEqual(result["reset_events_observed"], 1)
        self.assertGreaterEqual(result["hydration_events_observed"], 1)
        self.assertTrue(any("reset_then_changed" in summary for summary in result["search_cycle_summaries"]))
        self.assertNotEqual(result["outcome"], "chat_list_scroll_no_progress")

    def test_project_chat_target_appears_after_reset_and_hydrate_then_stops_scrolling(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        page_3 = _scrollable_project_chat_page(["City-wise Restrictions"], row_offset=20)
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_1, page_3, page_3, page_3, page_3, page_3, opened, opened],
            {"available": True, "path": "W.1.4.21", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(reader.actions.count(("W.1.4", "AXScrollDown")), 2)
        self.assertTrue(result["target_found_after_scrolling"])
        self.assertTrue(result["target_exact_match_detected"])
        self.assertEqual(result["target_detected_in"], "hydration")
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)

    def test_project_chat_no_progress_not_emitted_after_one_unchanged_hydration_cycle(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["City-wise Restrictions"], row_offset=10)
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_2, opened, opened],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["scroll_pulses_posted"], 1)
        self.assertEqual(result["target_found_during_hydration_cycle"], 1)
        self.assertTrue(result["target_exact_match_detected"])
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)

    def test_project_chat_search_budget_exhaustion_keeps_unknown_end_distinct(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        page_3 = _scrollable_project_chat_page(["Later Chat"], row_offset=20)
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_2, page_2, page_2, page_3, page_3],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Content Moderation, preview text must not drive matching"}},
        )

        with mock.patch.object(nav, "MAX_PROJECT_CHAT_SEARCH_CYCLES", 2), mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_search_budget_exhausted_without_confirmed_end")
        self.assertEqual(result["end_of_list_state"], "unknown")
        self.assertNotEqual(result["outcome"], "chat_not_found_in_project")

    def test_project_chat_hydration_uses_fresh_resolver_snapshots_between_sleeps(self) -> None:
        page = _scrollable_project_chat_page(["Content Moderation"])
        reader = _AutonomousReader(
            [page, page, page, page, page, page, page],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Content Moderation, preview text must not drive matching"}},
        )
        sleeper = _SleepRecorder()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=sleeper,
            )

        self.assertEqual(result["outcome"], "chat_list_end_reached_without_match")
        self.assertGreater(reader.collect_calls, result["scroll_pulses_posted"])
        self.assertGreaterEqual(len(sleeper.calls), result["scroll_pulses_posted"])

    def test_project_chat_target_found_in_intermediate_hydration_sample_opens_without_next_scroll(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        target_page = _scrollable_project_chat_page(["Mock Data Insertion SQL"], row_offset=10)
        opened = _scroll_opened_conversation_snapshots("Mock Data Insertion SQL")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, target_page, target_page, target_page, target_page, opened, opened, opened],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Mock Data Insertion SQL, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(reader.actions.count(("W.1.4", "AXScrollDown")), 1)
        self.assertEqual(result["target_found_during_hydration_cycle"], 1)
        self.assertIn(("W.1.4.11", "AXPress"), reader.actions)

    def test_project_chat_observer_marks_new_rows_as_progress_not_no_progress(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        first_plan = nav._project_chat_open_plan_from_snapshots(
            page_1,
            {"visited_nodes": len(page_1)},
            {"window_source": "synthetic", "window": page_1[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Missing Chat",
            _DisplayFactory(_DisplayProbe()),
        )
        reader = _AutonomousReader([page_2, page_2, page_2], {"available": True, "path": "W.1.4.11"})

        observation = nav._observe_project_chat_list_hydration(
            reader,
            123,
            "PTG Assistant",
            "Missing Chat",
            first_plan,
            first_plan,
            _DisplayFactory(_DisplayProbe()),
            _WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
            _SleepRecorder(),
            timeout_seconds=1.0,
            known_accessibility_rows={"Content Moderation, preview text must not drive matching"},
            require_change_before_early_settle=True,
        )

        self.assertGreater(observation["new_accessibility_rows"], 0)
        self.assertTrue(observation["meaningful_change"])
        self.assertFalse(observation["no_meaningful_change"])

    def test_project_chat_search_can_continue_past_twelve_cycles_within_time_budget(self) -> None:
        pages = [_scrollable_project_chat_page([f"Chat {index}"], row_offset=index * 10) for index in range(14)]
        target_page = _scrollable_project_chat_page(["Mock Data Insertion SQL"], row_offset=140)
        opened = _scroll_opened_conversation_snapshots("Mock Data Insertion SQL")
        snapshots: list[list[nav.AXElementSnapshot]] = [pages[0], pages[0], pages[0]]
        for index in range(1, 14):
            snapshots.extend([pages[index - 1], pages[index], pages[index], pages[index]])
        snapshots.extend([pages[13], target_page, target_page, target_page, target_page, opened, opened, opened])
        reader = _AutonomousReader(
            snapshots,
            {"available": True, "path": "W.1.4.141", "role": "AXButton", "title": {"literal": "Mock Data Insertion SQL, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertGreater(result["scroll_pulses_posted"], 12)
        self.assertEqual(result["configured_max_search_cycles"], 60)

    def test_project_chat_hydration_stability_counter_resets_on_viewport_changes(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        page_3 = _scrollable_project_chat_page(["Later Chat"], row_offset=20)
        first_plan = nav._project_chat_open_plan_from_snapshots(
            page_1,
            {"visited_nodes": len(page_1)},
            {"window_source": "synthetic", "window": page_1[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Missing Chat",
            _DisplayFactory(_DisplayProbe()),
        )
        reader = _AutonomousReader([page_2, page_3, page_3, page_3], {"available": True, "path": "W.1.4.21"})

        observation = nav._observe_project_chat_list_hydration(
            reader,
            123,
            "PTG Assistant",
            "Missing Chat",
            first_plan,
            first_plan,
            _DisplayFactory(_DisplayProbe()),
            _WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
            _SleepRecorder(),
            timeout_seconds=1.0,
            known_accessibility_rows={"Content Moderation, preview text must not drive matching"},
            require_change_before_early_settle=True,
        )

        self.assertEqual(observation["samples_taken"], 4)
        self.assertTrue(observation["settled"])
        self.assertEqual(observation["new_accessibility_rows"], 2)

    def test_project_chat_does_not_post_next_scroll_while_current_list_is_hydrating(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        page_3 = _scrollable_project_chat_page(["Later Chat"], row_offset=20)
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, page_2, page_3, page_3, page_3, page_3, page_3, page_3, page_3],
            {"available": True, "path": "W.1.4.21"},
        )

        with mock.patch.object(nav, "MAX_PROJECT_CHAT_SEARCH_CYCLES", 2), mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertGreaterEqual(result["scroll_pulses_posted"], 2)
        self.assertGreaterEqual(reader.action_collect_calls[1] - reader.action_collect_calls[0], 4)

    def test_project_chat_time_budget_exhausted_while_rows_still_progress_returns_distinct_outcome(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification"], row_offset=10)
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2],
            {"available": True, "path": "W.1.4.11"},
        )

        with mock.patch.object(nav, "MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS", 1.0), mock.patch.object(nav.time, "monotonic", side_effect=[0.0, 0.0, 2.0, 2.0]), mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_search_time_budget_exhausted_while_list_progressing")
        self.assertGreater(result["new_accessibility_rows_seen"], 0)

    def test_project_chat_virtualized_ax_path_changes_do_not_create_new_identity(self) -> None:
        row_a = {"title": "City-wise Restrictions", "accessibility_row_text": "City-wise Restrictions", "row_path": "W.1.4.1", "row_frame": {"x": 282, "y": 176, "width": 779, "height": 65}}
        row_b = {"title": "City-wise Restrictions", "accessibility_row_text": "City-wise Restrictions", "row_path": "W.1.4.99", "row_frame": {"x": 282, "y": 176, "width": 779, "height": 65}}

        self.assertEqual(nav._project_chat_row_signature(row_a), nav._project_chat_row_signature(row_b))

    def test_project_chat_strict_title_matching_remains_unchanged(self) -> None:
        row = {"accessibility_row_text": "City-wise Restrictions, preview text must not drive matching"}

        self.assertTrue(nav._project_chat_row_match_representation(row, "City-wise Restrictions")["matched"])
        self.assertFalse(nav._project_chat_row_match_representation(row, "preview text must not drive matching")["matched"])

    def test_direct_axbutton_merged_description_sql_preview_is_valid_project_chat_row(self) -> None:
        snapshots = _mock_data_sql_project_chat_page("SELECT * FROM users WHERE id = 1; ```sql INSERT INTO users VALUES (1); ```")
        resolution = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))

        self.assertEqual(resolution["status"], "visible_chats_found")
        self.assertEqual(resolution["visible_chat_count"], 1)
        row = resolution["visible_chats"][0]
        self.assertEqual(row["title"], "Mock Data Insertion SQL")
        self.assertEqual(row["preview"], "SELECT * FROM users WHERE id = 1; ```sql INSERT INTO users VALUES (1); ```")
        self.assertEqual(row["row_path"], "W.1.4.0.15")
        self.assertEqual(row["row_role"], "AXButton")
        self.assertTrue(row["ax_press_available"])
        self.assertEqual(row["title_representation"], "canonical_accessibility_description_prefix")

    def test_preview_code_json_links_and_message_prose_do_not_reject_valid_row(self) -> None:
        previews = [
            "function seed() { return fetch('https://example.test/mock.json'); }",
            '{"migration": "2026_01_01_seed_mock_data", "sql": "INSERT INTO t VALUES (1)"}',
            "user: run this SQL then check https://example.test/docs",
        ]
        for preview in previews:
            with self.subTest(preview=preview):
                resolution = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", _mock_data_sql_project_chat_page(preview), (0, 0, 1200, 900))
                self.assertEqual(resolution["status"], "visible_chats_found")
                self.assertEqual(resolution["visible_chats"][0]["title"], "Mock Data Insertion SQL")

    def test_canonical_title_eligibility_is_independent_of_preview_content(self) -> None:
        accepted = nav._project_visible_row_title(
            nav.AXElementSnapshot(
                path="W.1",
                depth=1,
                role="AXButton",
                description="Mock Data Insertion SQL, function seed() { return 'preview code'; }",
                actions=("AXPress",),
                frame=(282, 441, 352, 64.5),
            )
        )
        rejected = nav._project_visible_row_title(
            nav.AXElementSnapshot(
                path="W.2",
                depth=1,
                role="AXButton",
                description="function seed(), harmless preview",
                actions=("AXPress",),
                frame=(282, 522, 352, 64.5),
            )
        )

        self.assertEqual(accepted, "Mock Data Insertion SQL")
        self.assertEqual(rejected, "")

    def test_title_only_direct_row_remains_accepted(self) -> None:
        snapshots = _scrollable_project_chat_page([], list_actions=())
        snapshots.append(
            nav.AXElementSnapshot(
                path="W.1.4.7",
                depth=3,
                role="AXButton",
                title="Title Only Chat",
                actions=("AXPress",),
                frame=(282, 176, 779, 65),
            )
        )
        resolution = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))

        self.assertEqual(resolution["status"], "visible_chats_found")
        self.assertEqual(resolution["visible_chats"][0]["title"], "Title Only Chat")
        self.assertEqual(resolution["visible_chats"][0]["title_representation"], "exact_axtitle")

    def test_generic_composer_sidebar_and_page_controls_remain_rejected(self) -> None:
        snapshots = _scrollable_project_chat_page([], list_actions=())
        snapshots.extend(
            [
                nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXButton", title="Chats", actions=("AXPress",), frame=(282, 176, 779, 65)),
                nav.AXElementSnapshot(path="W.1.4.2", depth=3, role="AXTextArea", title="Message ChatGPT", frame=(282, 258, 779, 65)),
                nav.AXElementSnapshot(path="W.1.4.3", depth=3, role="AXIncrementPage", frame=(1048, 150, 10, 32)),
                nav.AXElementSnapshot(path="W.0.9", depth=2, role="AXButton", title="Sidebar Chat", actions=("AXPress",), frame=(20, 176, 220, 65)),
            ]
        )
        resolution = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))

        self.assertIn(resolution["status"], {"visible_chat_rows_not_found", "project_chat_list_identity_not_confirmed"})
        self.assertEqual(resolution.get("visible_chats") or [], [])

    def test_discovery_output_prints_canonical_title_only_never_sql_preview(self) -> None:
        preview = "SELECT * FROM seed_table; function seedMockData() { return true; }"
        page = _mock_data_sql_project_chat_page(preview)
        output = _LiveOutputRecorder()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([page], {"available": True, "path": "W.1.4.0.15"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertEqual(output.lines, ["Chats discovered:", "1. Mock Data Insertion SQL"])
        self.assertNotIn("SELECT", "\n".join(output.lines))
        self.assertEqual(result["unique_chat_titles_printed"], 1)

    def test_dispatch_allows_axscrolltovisible_only_with_exact_target_alignment_context(self) -> None:
        reader, recorder = _autonomous_dispatch_reader()

        self.assertTrue(reader.perform_action("W.1.4.11", "AXScrollToVisible", action_context=_alignment_dispatch_context()))

        self.assertEqual(recorder.calls, [(111, 222)])

    def test_dispatch_rejects_axscrolltovisible_without_explicit_alignment_context(self) -> None:
        reader, recorder = _autonomous_dispatch_reader()

        with self.assertRaisesRegex(nav.AXDiagnosticError, "Unsupported autonomous sidebar action"):
            reader.perform_action("W.1.4.11", "AXScrollToVisible")

        self.assertEqual(recorder.calls, [])

    def test_dispatch_rejects_axscrolltovisible_for_sidebar_project_button(self) -> None:
        reader, recorder = _autonomous_dispatch_reader(path="W.0.7")
        context = _alignment_dispatch_context(path="W.0.7", container_path="W.1.4", target_descends_from_confirmed_chat_list=False)

        with self.assertRaisesRegex(nav.AXDiagnosticError, "Unsupported autonomous sidebar action"):
            reader.perform_action("W.0.7", "AXScrollToVisible", action_context=context)

        self.assertEqual(recorder.calls, [])

    def test_dispatch_rejects_axscrolltovisible_without_exact_title_evidence(self) -> None:
        reader, recorder = _autonomous_dispatch_reader()
        context = _alignment_dispatch_context(exact_target_detected=False)

        with self.assertRaisesRegex(nav.AXDiagnosticError, "Unsupported autonomous sidebar action"):
            reader.perform_action("W.1.4.11", "AXScrollToVisible", action_context=context)

        self.assertEqual(recorder.calls, [])

    def test_dispatch_keeps_axshowmenu_rejected_even_with_alignment_context(self) -> None:
        reader, recorder = _autonomous_dispatch_reader()

        with self.assertRaisesRegex(nav.AXDiagnosticError, "Unsupported autonomous sidebar action"):
            reader.perform_action("W.1.4.11", "AXShowMenu", action_context=_alignment_dispatch_context())

        self.assertEqual(recorder.calls, [])

    def test_dispatch_rejects_second_axscrolltovisible_alignment_attempt(self) -> None:
        reader, recorder = _autonomous_dispatch_reader()
        context = _alignment_dispatch_context(alignment_already_posted=True)

        with self.assertRaisesRegex(nav.AXDiagnosticError, "Unsupported autonomous sidebar action"):
            reader.perform_action("W.1.4.11", "AXScrollToVisible", action_context=context)

        self.assertEqual(recorder.calls, [])

    def test_dispatch_existing_axpress_authorization_is_unchanged(self) -> None:
        reader, recorder = _autonomous_dispatch_reader()

        self.assertTrue(reader.perform_action("W.1.4.11", "AXPress"))

        self.assertEqual(recorder.calls, [(111, 333)])

    def test_dispatch_retains_nonzero_axpress_error_code(self) -> None:
        reader, recorder = _autonomous_dispatch_reader(error_code=-25205)

        self.assertFalse(reader.perform_action("W.1.4.11", "AXPress"))

        self.assertEqual(recorder.calls, [(111, 333)])
        self.assertEqual(
            reader.last_ax_action_result,
            {"path": "W.1.4.11", "action": "AXPress", "error_code": -25205},
        )

    def test_target_detection_on_canonical_title_posts_zero_additional_scrolls_and_fresh_axpress(self) -> None:
        preview = "SELECT * FROM users; function hydratePreview() { return true; }"
        page = _mock_data_sql_project_chat_page(preview)
        opened = _scroll_opened_conversation_snapshots("Mock Data Insertion SQL")
        output = _LiveOutputRecorder()
        reader = _TimedActionReader([page, page, opened, opened], {"available": True, "path": "W.1.4.0.15", "role": "AXButton"})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertEqual(result["outcome"], "chat_opened_via_axpress")
        self.assertTrue(result["target_exact_match_detected"])
        self.assertEqual(result["scroll_pulses_posted"], 0)
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)
        self.assertFalse(result["target_alignment_required"])
        self.assertEqual(result["target_alignment_method"], "none")
        self.assertFalse(result["target_alignment_posted"])
        self.assertNotIn(("W.1.4.0.15", "AXScrollToVisible"), reader.actions)
        self.assertIn("target_exact_match_detected: Mock Data Insertion SQL", output.lines)
        self.assertIn(("W.1.4.0.15", "AXPress"), reader.actions)
        self.assertGreaterEqual(reader.action_collect_calls[-1], 2)

    def test_partially_clipped_target_aligns_once_then_actions_fresh_visible_row(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.11",
            frame=(282, 650, 779, 65),
            actions=("AXPress", "AXScrollToVisible", "AXShowMenu"),
        )
        clipped_alignment_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.12",
            frame=(282, 650, 779, 65),
            actions=("AXPress", "AXScrollToVisible", "AXShowMenu"),
        )
        fresh_visible_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.21",
            frame=(282, 176, 779, 65),
        )
        opened = _scroll_opened_conversation_snapshots("Mock Data Insertion SQL")
        output = _LiveOutputRecorder()
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, clipped_target, clipped_alignment_target, fresh_visible_target, opened, opened],
            {"available": True, "path": "W.1.4.11", "role": "AXButton"},
        )
        scroller = _ScrollService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                scroll_service_factory=_ScrollFactory(scroller),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertTrue(result["target_exact_match_detected"])
        self.assertEqual(result["target_detected_in"], "hydration")
        self.assertEqual(result["target_detection_row_path"], "W.1.4.11")
        self.assertEqual(result["target_detection_canonical_title"], "Mock Data Insertion SQL")
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)
        self.assertEqual(reader.actions.count(("W.1.4", "AXScrollDown")), 1)
        self.assertEqual(scroller.scrolls, [])
        self.assertTrue(result["target_alignment_required"])
        self.assertEqual(result["target_alignment_method"], "axscrolltovisible")
        self.assertTrue(result["target_alignment_posted"])
        self.assertEqual(result["target_alignment_row_path"], "W.1.4.12")
        self.assertEqual(result["target_alignment_pre_visibility"], "partially_clipped")
        self.assertEqual(result["target_alignment_post_visibility"], "fully_visible")
        self.assertTrue(result["target_alignment_fresh_re_resolution_confirmed"])
        self.assertEqual(reader.actions.count(("W.1.4.12", "AXScrollToVisible")), 1)
        alignment_action_index = reader.actions.index(("W.1.4.12", "AXScrollToVisible"))
        alignment_context = reader.action_contexts[alignment_action_index] or {}
        self.assertEqual(alignment_context.get("kind"), "exact_project_chat_target_alignment")
        self.assertEqual(alignment_context.get("target_path"), "W.1.4.12")
        self.assertEqual(alignment_context.get("canonical_title"), "Mock Data Insertion SQL")
        self.assertEqual(alignment_context.get("requested_title"), "Mock Data Insertion SQL")
        self.assertEqual(alignment_context.get("visibility"), "partially_clipped")
        self.assertFalse(alignment_context.get("alignment_already_posted"))
        self.assertIn({"path": "W.1.4.12", "action": "AXScrollToVisible"}, result["actions_performed"])
        self.assertIn("target_exact_match_detected: Mock Data Insertion SQL", output.lines)
        self.assertEqual(sum(1 for line in output.lines if line.endswith(". Mock Data Insertion SQL")), 1)
        self.assertTrue(result["fresh_target_re_resolution_confirmed"])
        self.assertIn(("W.1.4.21", "AXPress"), reader.actions)
        self.assertNotIn(("W.1.4.11", "AXPress"), reader.actions)
        self.assertNotIn(("W.1.4.12", "AXPress"), reader.actions)

    def test_partially_clipped_target_without_axscrolltovisible_fails_closed(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.11",
            frame=(282, 650, 779, 65),
        )
        still_clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.11",
            frame=(282, 650, 779, 65),
        )
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, clipped_target, still_clipped_target, still_clipped_target],
            {"available": True, "path": "W.1.4.11", "role": "AXButton"},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "target_alignment_not_supported")
        self.assertTrue(result["target_exact_match_detected"])
        self.assertTrue(result["fresh_target_re_resolution_confirmed"])
        self.assertTrue(result["target_alignment_required"])
        self.assertEqual(result["target_alignment_method"], "none")
        self.assertFalse(result["target_alignment_posted"])
        self.assertEqual(result["target_alignment_pre_visibility"], "partially_clipped")
        self.assertEqual(result["target_alignment_post_visibility"], "partially_clipped")
        self.assertEqual(result["target_detection_row_path"], "W.1.4.11")
        self.assertNotIn(("W.1.4.11", "AXScrollToVisible"), reader.actions)
        self.assertNotIn(("W.1.4.11", "AXPress"), reader.actions)
        self.assertEqual(clicker.clicks, [])
        self.assertEqual(result["calculated_global_point"], nav._xy_report(None))

    def test_target_alignment_posted_true_only_after_successful_dispatch(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.11",
            frame=(282, 650, 779, 65),
            actions=("AXPress", "AXScrollToVisible"),
        )
        reader = _FailingAlignmentReader(
            [page_1, page_1, page_1, page_1, clipped_target, clipped_target],
            {"available": True, "path": "W.1.4.11", "role": "AXButton"},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "target_alignment_action_post_failed")
        self.assertTrue(result["target_alignment_required"])
        self.assertEqual(result["target_alignment_method"], "axscrolltovisible")
        self.assertFalse(result["target_alignment_posted"])
        self.assertNotIn({"path": "W.1.4.11", "action": "AXScrollToVisible"}, result["actions_performed"])
        self.assertEqual(reader.actions.count(("W.1.4.11", "AXScrollToVisible")), 1)
        self.assertEqual(clicker.clicks, [])

    def test_target_alignment_remaining_partially_clipped_fails_closed(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.11",
            frame=(282, 650, 779, 65),
            actions=("AXPress", "AXScrollToVisible"),
        )
        still_clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.12",
            frame=(282, 650, 779, 65),
            actions=("AXPress", "AXScrollToVisible"),
        )
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, clipped_target, clipped_target, still_clipped_target, still_clipped_target],
            {"available": True, "path": "W.1.4.11", "role": "AXButton"},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "target_alignment_posted_but_target_not_fully_visible")
        self.assertTrue(result["target_alignment_posted"])
        self.assertEqual(result["target_alignment_post_visibility"], "partially_clipped")
        self.assertTrue(result["target_alignment_fresh_re_resolution_confirmed"])
        self.assertEqual(reader.actions.count(("W.1.4.11", "AXScrollToVisible")), 1)
        self.assertNotIn(("W.1.4.12", "AXPress"), reader.actions)
        self.assertEqual(clicker.clicks, [])

    def test_target_alignment_disappearing_target_fails_closed(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        clipped_target = _project_chat_page_with_single_row_frame(
            "Mock Data Insertion SQL",
            path="W.1.4.11",
            frame=(282, 650, 779, 65),
            actions=("AXPress", "AXScrollToVisible"),
        )
        missing_after_alignment = _scrollable_project_chat_page(["Another Chat"])
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, clipped_target, clipped_target, missing_after_alignment, missing_after_alignment],
            {"available": True, "path": "W.1.4.11", "role": "AXButton"},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "target_alignment_posted_but_target_not_re_resolved")
        self.assertTrue(result["target_alignment_posted"])
        self.assertEqual(result["target_alignment_post_visibility"], "not_visible")
        self.assertFalse(result["target_alignment_fresh_re_resolution_confirmed"])
        self.assertEqual(reader.actions.count(("W.1.4.11", "AXScrollToVisible")), 1)
        self.assertNotIn(("W.1.4.11", "AXPress"), reader.actions)
        self.assertEqual(clicker.clicks, [])

    def test_comma_titles_fail_closed_without_axtitle_but_exact_axtitle_can_match(self) -> None:
        description_only = _scrollable_project_chat_page(["Alpha, Bravo"])
        description_plan = nav._project_chat_open_plan_from_snapshots(
            description_only,
            {"visited_nodes": len(description_only)},
            {"window_source": "synthetic", "window": description_only[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Alpha, Bravo",
            _DisplayFactory(_DisplayProbe()),
        )
        self.assertEqual(description_plan["status"], "chat_title_not_unambiguously_representable_by_accessibility")

        explicit_title = _scrollable_project_chat_page([], list_actions=())
        explicit_title.append(
            nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXButton", title="Alpha, Bravo", actions=("AXPress",), frame=(282, 176, 779, 65))
        )
        title_plan = nav._project_chat_open_plan_from_snapshots(
            explicit_title,
            {"visited_nodes": len(explicit_title)},
            {"window_source": "synthetic", "window": explicit_title[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Alpha, Bravo",
            _DisplayFactory(_DisplayProbe()),
        )
        self.assertEqual(title_plan["status"], "ready")
        self.assertEqual(title_plan["matched_title_representation"], "exact_axtitle")

    def test_diagnostic_visual_bands_exclude_giant_provider_scrollbar_and_page_controls(self) -> None:
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
        )

        band_paths = {path for band in result["visual_row_bands"] for path in band.get("node_paths", [])}
        self.assertNotIn("W.1.4.0", band_paths)
        self.assertNotIn("W.1.4.98", band_paths)
        self.assertNotIn("W.1.4.99", band_paths)
        self.assertTrue(any(path.startswith("W.1.4.1") for path in band_paths))

    def test_project_chat_scroll_target_is_confirmed_chat_list_not_sidebar_or_transcript(self) -> None:
        snapshots = _scrollable_project_chat_page(["Content Moderation"], list_actions=())
        plan = nav._project_chat_open_plan_from_snapshots(
            snapshots,
            {"visited_nodes": len(snapshots)},
            {"window_source": "synthetic", "window": snapshots[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "City-wise Restrictions",
            _DisplayFactory(_DisplayProbe()),
        )

        target = nav._project_chat_scroll_target(plan)

        self.assertEqual(target["status"], "ready")
        self.assertEqual(target["method"], "coregraphics_scroll")
        self.assertEqual(target["path"], "W.1.4")
        self.assertNotEqual(target["path"], "W.0")
        self.assertNotEqual(target["path"], "W.1.5")

    def test_project_chat_scroll_search_uses_fresh_rows_after_scroll(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        page_2 = _scrollable_project_chat_page(["City-wise Restrictions"], row_offset=40)
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_2, opened, opened],
            {"available": True, "path": "W.1.4.41", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["matched_chat_row"]["row_path"], "W.1.4.41")
        self.assertNotIn(("W.1.4.1", "AXPress"), reader.actions)

    def test_project_chat_scroll_fallback_uses_one_coregraphics_scroll_inside_chat_list(self) -> None:
        # Overlapping multi-row viewports: page_2 shares its first two rows with
        # page_1's last two rows, so a single overlap-safe micro-scroll advances
        # to the target without triggering recovery.
        page_1 = _scrollable_project_chat_page(["Alpha Chat", "Bravo Chat", "Charlie Chat"], list_actions=())
        page_2 = _scrollable_project_chat_page(["Bravo Chat", "Charlie Chat", "City-wise Restrictions"], row_offset=10, list_actions=())
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_2, opened, opened],
            {"available": True, "path": "W.1.4.13", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )
        scroller = _ScrollService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                scroll_service_factory=_ScrollFactory(scroller),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["scroll_method_used"], "coregraphics_scroll")
        self.assertEqual(len(scroller.scrolls), 1)
        x, y, _delta = scroller.scrolls[0]
        self.assertTrue(nav._point_inside_frame((x, y), (282, 150, 779, 520)))

    def test_live_project_chat_initial_valid_chats_print_once_in_order(self) -> None:
        page = _scrollable_project_chat_page(["Content Moderation", "AWS Profile Photo Verification"])
        output = _LiveOutputRecorder()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([page], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertEqual(
            output.lines,
            ["Chats discovered:", "1. Content Moderation", "2. AWS Profile Photo Verification"],
        )
        self.assertEqual(result["unique_chat_titles_printed"], 2)

    def test_live_project_chat_newly_exposed_chats_append_once_without_duplicates(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation", "AWS Profile Photo Verification"])
        page_2 = _scrollable_project_chat_page(["AWS Profile Photo Verification", "Create Hangout Draft Persistence"], row_offset=10)
        output = _LiveOutputRecorder()
        reader = _AutonomousReader([page_1, page_1, page_1, page_1, page_2, page_2, page_2], {"available": True, "path": "W.1.4.11"})

        with mock.patch.object(nav, "MAX_PROJECT_CHAT_SEARCH_CYCLES", 1), mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 2}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertIn(["Chats discovered after cycle 1:", "3. Create Hangout Draft Persistence"], output.blocks)
        self.assertEqual(output.lines.count("2. AWS Profile Photo Verification"), 1)
        self.assertEqual(result["unique_chat_titles_printed"], 3)

    def test_live_project_chat_does_not_print_composer_transcript_or_sidebar_controls(self) -> None:
        page = _scrollable_project_chat_page(["Content Moderation"]) + [
            nav.AXElementSnapshot(path="W.0.1", depth=2, role="AXButton", title="New chat", actions=("AXPress",), frame=(20, 20, 120, 32)),
            nav.AXElementSnapshot(path="W.1.5.1", depth=3, role="AXStaticText", value="Transcript text", frame=(300, 720, 300, 24)),
            nav.AXElementSnapshot(path="W.1.6", depth=2, role="AXTextArea", title="Message ChatGPT", frame=(320, 820, 620, 44)),
        ]
        output = _LiveOutputRecorder()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Missing Chat",
                confirm_open_chat=False,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([page], {"available": True, "path": "W.1.4.1"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertEqual(output.lines, ["Chats discovered:", "1. Content Moderation"])
        self.assertNotIn("New chat", "\n".join(output.lines))
        self.assertNotIn("Transcript text", "\n".join(output.lines))
        self.assertNotIn("Message ChatGPT", "\n".join(output.lines))

    def test_target_found_in_initial_row_posts_zero_scroll_events(self) -> None:
        page = _scrollable_project_chat_page(["City-wise Restrictions"])
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader([page, page, opened, opened], {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="City-wise Restrictions",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_via_axpress")
        self.assertTrue(result["target_exact_match_detected"])
        self.assertEqual(result["target_detected_in"], "initial")
        self.assertEqual(result["scroll_pulses_posted"], 0)
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)

    def test_target_found_during_hydration_stops_window_and_posts_no_further_scrolls(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        target_page = _scrollable_project_chat_page(["Mock Data Insertion SQL"], row_offset=10)
        opened = _scroll_opened_conversation_snapshots("Mock Data Insertion SQL")
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, target_page, target_page, opened, opened],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Mock Data Insertion SQL, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Mock Data Insertion SQL",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["target_detected_in"], "hydration")
        self.assertEqual(result["scroll_pulses_posted"], 1)
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)
        self.assertEqual(reader.actions[-1], ("W.1.4.11", "AXPress"))
        self.assertGreaterEqual(reader.action_collect_calls[-1], 5)

    def test_target_found_in_settled_post_scroll_row_posts_zero_additional_scrolls(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        target_page = _scrollable_project_chat_page(["Settled Target"], row_offset=10)
        opened = _scroll_opened_conversation_snapshots("Settled Target")
        reader = _AutonomousReader([page_1, page_1, page_1, target_page, opened, opened], {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Settled Target, preview text must not drive matching"}})
        ready_plan = nav._project_chat_open_plan_from_snapshots(
            target_page,
            {"visited_nodes": len(target_page)},
            {"window_source": "synthetic", "window": target_page[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Settled Target",
            _DisplayFactory(_DisplayProbe()),
        )

        initial_plan = nav._project_chat_open_plan_from_snapshots(
            page_1,
            {"visited_nodes": len(page_1)},
            {"window_source": "synthetic", "window": page_1[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Settled Target",
            _DisplayFactory(_DisplayProbe()),
        )

        initial_observation = {
            "classification": "list_stable_no_change",
            "plan": initial_plan,
            "plans": [initial_plan],
            "states": [nav._project_chat_effective_list_state(initial_plan)],
            "new_accessibility_rows": 0,
            "no_meaningful_change": True,
            "reset_then_changed": False,
            "hydration_events_observed": 0,
            "reset_events_observed": 0,
            "settled": True,
            "meaningful_change": False,
            "target_found": False,
            "samples_taken": 1,
            "target_match_checked_on_samples": 1,
        }

        def settled_observation():
            return {
                "classification": "list_advanced",
                "plan": ready_plan,
                "plans": [ready_plan],
                "states": [nav._project_chat_effective_list_state(ready_plan)],
                "new_accessibility_rows": 1,
                "no_meaningful_change": False,
                "reset_then_changed": False,
                "hydration_events_observed": 1,
                "reset_events_observed": 0,
                "settled": True,
                "meaningful_change": True,
                "target_found": False,
                "samples_taken": 1,
                "target_match_checked_on_samples": 1,
            }

        with mock.patch.object(nav, "_observe_project_chat_list_hydration", side_effect=[initial_observation, settled_observation()]), mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Settled Target",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["target_detected_in"], "settled")
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)

    def test_target_found_during_recovery_posts_no_further_recovery_or_forward_scrolls(self) -> None:
        page_1 = _scrollable_project_chat_page(["Alpha", "Bravo", "Charlie"], list_actions=())
        jump = _scrollable_project_chat_page(["Xray", "Yankee", "Zulu"], row_offset=10, list_actions=())
        target_page = _scrollable_project_chat_page(["Recovery Target"], row_offset=20, list_actions=())
        opened = _scroll_opened_conversation_snapshots("Recovery Target")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, jump, jump, jump, target_page, target_page, opened, opened],
            {"available": True, "path": "W.1.4.21", "role": "AXButton", "title": {"literal": "Recovery Target, preview text must not drive matching"}},
        )
        scroller = _ScrollService()
        result = self._run_open_chatgpt_project_chat(reader, chat_title="Recovery Target", scroller=scroller)

        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["target_detected_in"], "recovery")
        self.assertEqual(result["scroll_pulses_after_target_detection"], 0)
        self.assertEqual(result["scroll_pulses_posted"], 1)
        self.assertEqual(result["recovery_scroll_pulses_posted"], 1)
        self.assertEqual(len(scroller.scrolls), 2)

    def test_target_disappearance_before_fresh_re_resolution_fails_closed_without_click(self) -> None:
        page_1 = _scrollable_project_chat_page(["Content Moderation"])
        target_page = _scrollable_project_chat_page(["Transient Target"], row_offset=10)
        missing_page = _scrollable_project_chat_page(["Other Chat"], row_offset=20)
        reader = _TimedActionReader(
            [page_1, page_1, page_1, page_1, target_page, missing_page, missing_page],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Transient Target, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Transient Target",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "target_detected_but_not_stably_re_resolved")
        self.assertTrue(result["target_exact_match_detected"])
        self.assertNotIn(("W.1.4.11", "AXPress"), reader.actions)

    def test_detected_target_re_resolution_retry_can_proceed_to_existing_axpress_path(self) -> None:
        target_page = _scrollable_project_chat_page(["Patient Target"])
        missing_page = _scrollable_project_chat_page(["Other Chat"], row_offset=10)
        opened = _scroll_opened_conversation_snapshots("Patient Target")
        reader = _TimedActionReader(
            [target_page, missing_page, target_page, opened, opened],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Patient Target, preview text must not drive matching"}},
        )
        sleeper = _SleepRecorder()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Patient Target",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=sleeper,
            )

        self.assertEqual(result["outcome"], "chat_opened_via_axpress")
        self.assertEqual(result["final_re_resolution_retry_attempts"], 1)
        self.assertEqual(result["final_re_resolution_max_retries"], 2)
        self.assertTrue(result["final_re_resolution_re_resolved"])
        self.assertTrue(result["final_re_resolution_action_posted"])
        self.assertTrue(result["chat_open_action_posted"])
        self.assertIn(("W.1.4.1", "AXPress"), reader.actions)
        self.assertIn(nav.PROJECT_CHAT_FINAL_RE_RESOLUTION_RETRY_DELAY_SECONDS, sleeper.calls)

    def test_detected_target_re_resolution_retry_remains_bounded_when_missing(self) -> None:
        target_page = _scrollable_project_chat_page(["Patient Target"])
        missing_page = _scrollable_project_chat_page(["Other Chat"], row_offset=10)
        reader = _TimedActionReader(
            [target_page, missing_page, missing_page, missing_page, target_page],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Patient Target, preview text must not drive matching"}},
        )
        sleeper = _SleepRecorder()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Patient Target",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=sleeper,
            )

        self.assertEqual(result["outcome"], "target_detected_but_not_stably_re_resolved")
        self.assertEqual(result["final_re_resolution_retry_attempts"], 2)
        self.assertFalse(result["final_re_resolution_re_resolved"])
        self.assertFalse(result["final_re_resolution_action_posted"])
        self.assertFalse(result["chat_open_action_posted"])
        self.assertNotIn(("W.1.4.1", "AXPress"), reader.actions)
        self.assertEqual(sleeper.calls.count(nav.PROJECT_CHAT_FINAL_RE_RESOLUTION_RETRY_DELAY_SECONDS), 2)
        self.assertEqual(reader.collect_calls, 4)

    def test_detected_target_re_resolution_retry_fails_closed_when_ambiguous(self) -> None:
        target_page = _scrollable_project_chat_page(["Patient Target"])
        missing_page = _scrollable_project_chat_page(["Other Chat"], row_offset=10)
        ambiguous_page = _scrollable_project_chat_page(["Patient Target", "Patient Target"], row_offset=20)
        reader = _TimedActionReader(
            [target_page, missing_page, ambiguous_page],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Patient Target, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Patient Target",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "target_detected_but_not_stably_re_resolved")
        self.assertEqual(result["final_re_resolution_retry_attempts"], 1)
        self.assertFalse(result["final_re_resolution_re_resolved"])
        self.assertFalse(result["chat_open_action_posted"])
        self.assertEqual(reader.actions, [])

    def test_detected_target_re_resolution_retry_proceeds_when_only_row_path_changes(self) -> None:
        target_page = _scrollable_project_chat_page(["Patient Target"])
        missing_page = _scrollable_project_chat_page(["Other Chat"], row_offset=10)
        changed_path_page = _scrollable_project_chat_page(["Patient Target"], row_offset=20)
        opened = _scroll_opened_conversation_snapshots("Patient Target")
        reader = _TimedActionReader(
            [target_page, missing_page, changed_path_page, opened, opened],
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Patient Target, preview text must not drive matching"}},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Patient Target",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_via_axpress")
        self.assertEqual(result["target_detection_row_path"], "W.1.4.1")
        self.assertEqual(result["matched_chat_row"]["row_path"], "W.1.4.21")
        self.assertEqual(result["final_re_resolution_retry_attempts"], 1)
        self.assertTrue(result["final_re_resolution_re_resolved"])
        self.assertTrue(result["chat_open_action_posted"])
        self.assertIn(("W.1.4.21", "AXPress"), reader.actions)

    def test_final_re_resolution_retry_source_is_bounded_and_not_deadline_based(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        retry_source = source[
            source.index("def _retry_detected_project_chat_fresh_re_resolution"):
            source.index("def _project_chat_detected_target_still_unresolved")
        ]

        self.assertIn("PROJECT_CHAT_FINAL_RE_RESOLUTION_MAX_RETRIES", retry_source)
        self.assertIn("range(1, max_retries + 1)", retry_source)
        self.assertNotIn("while ", retry_source)
        self.assertNotIn("time.monotonic", retry_source)
        self.assertNotIn("MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS", retry_source)

    def test_identity_not_confirmed_prints_no_discovered_chats_and_posts_no_action(self) -> None:
        output = _LiveOutputRecorder()
        reader = _AutonomousReader([_scrollable_project_chat_page(["Content Moderation"])], {"available": True, "path": "W.1.4.1"})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Content Moderation",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": False, "outcome": "project_chat_list_identity_not_confirmed", "visible_chat_count": 0}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
                discovery_output_function=output,
            )

        self.assertEqual(result["outcome"], "project_chat_list_identity_not_confirmed")
        self.assertEqual(output.lines, [])
        self.assertEqual(reader.actions, [])
        self.assertEqual(result["unique_chat_titles_printed"], 0)
        self.assertFalse(result["target_detected"])
        self.assertFalse(result["actionable_element_resolved"])
        self.assertFalse(result["axpress_attempted"])
        self.assertEqual(result["final_reresolution_status"], "not_attempted")

    # --- Focused tests: overlap-safe scan continuity and valid termination ----

    def _run_open_chatgpt_project_chat(self, reader, *, chat_title, scroller=None, max_cycles=None):
        patches = [mock.patch.object(nav.sys, "platform", "darwin")]
        if max_cycles is not None:
            patches.append(mock.patch.object(nav, "MAX_PROJECT_CHAT_SEARCH_CYCLES", max_cycles))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            kwargs = dict(
                project_title="PTG Assistant",
                chat_title=chat_title,
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )
            if scroller is not None:
                kwargs["scroll_service_factory"] = _ScrollFactory(scroller)
            return nav.open_chatgpt_project_chat(**kwargs)

    @staticmethod
    def _effective_state(page: list) -> dict:
        plan = nav._project_chat_open_plan_from_snapshots(
            page,
            {"visited_nodes": len(page)},
            {"window_source": "synthetic", "window": page[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Unused Target",
            _DisplayFactory(_DisplayProbe()),
        )
        return nav._project_chat_effective_list_state(plan)

    def test_focused_coregraphics_delta_derived_from_row_height_not_minus_360(self) -> None:
        snapshots = _scrollable_project_chat_page(["Content Moderation"], list_actions=())
        plan = nav._project_chat_open_plan_from_snapshots(
            snapshots, {"visited_nodes": len(snapshots)}, {"window_source": "synthetic", "window": snapshots[0]},
            (0, 0, 1200, 900), (0, 0, 1200, 900), "PTG Assistant", "City-wise Restrictions", _DisplayFactory(_DisplayProbe()),
        )
        target = nav._project_chat_scroll_target(plan)
        self.assertEqual(target["method"], "coregraphics_scroll")
        # Row height is 65 -> 0.75 * 65 = 48.75 -> -49 (not the legacy fixed -360).
        self.assertEqual(target["median_visible_row_height"], 65.0)
        self.assertEqual(target["computed_scroll_delta_y"], -49)
        self.assertNotEqual(target["computed_scroll_delta_y"], -360)
        # A different row height yields a different delta; clamps are honored.
        self.assertEqual(nav._project_chat_computed_scroll_delta_y(120.0), -90)
        self.assertEqual(nav._project_chat_computed_scroll_delta_y(2.0), -nav.PROJECT_CHAT_SCROLL_MIN_PIXEL_DELTA)
        self.assertEqual(nav._project_chat_computed_scroll_delta_y(10000.0), -nav.PROJECT_CHAT_SCROLL_MAX_PIXEL_DELTA)

    def test_focused_two_ordered_shared_rows_establish_continuity(self) -> None:
        overlap = nav._project_chat_viewport_overlap({"row_texts": ("A", "B", "C")}, {"row_texts": ("B", "C", "D")})
        self.assertTrue(overlap["adjacency_confirmed"])
        self.assertEqual(overlap["overlap_row_count"], 2)
        # A single non-adjacent shared row is not sufficient.
        partial = nav._project_chat_viewport_overlap({"row_texts": ("A", "B", "C")}, {"row_texts": ("X", "B", "Z")})
        self.assertFalse(partial["adjacency_confirmed"])
        self.assertEqual(partial["overlap_row_count"], 1)
        # Short viewports use the strongest feasible rule (all available rows).
        short = nav._project_chat_viewport_overlap({"row_texts": ("A",)}, {"row_texts": ("A",)})
        self.assertTrue(short["adjacency_confirmed"])

    def test_focused_overlap_identity_is_row_text_not_ax_path(self) -> None:
        page_1 = _scrollable_project_chat_page(["Alpha", "Bravo", "Charlie"])
        page_2 = _scrollable_project_chat_page(["Bravo", "Charlie", "Delta"], row_offset=50)
        overlap = nav._project_chat_viewport_overlap(self._effective_state(page_1), self._effective_state(page_2))
        # page_2 rows live at entirely different AX paths (row_offset=50) yet
        # overlap is detected purely by normalized row text.
        self.assertTrue(overlap["adjacency_confirmed"])
        self.assertGreaterEqual(overlap["overlap_row_count"], 2)

    def test_focused_target_in_overlapping_viewport_opens_immediately(self) -> None:
        page_1 = _scrollable_project_chat_page(["Alpha Chat", "Bravo Chat", "Charlie Chat"], list_actions=())
        page_2 = _scrollable_project_chat_page(["Bravo Chat", "Charlie Chat", "City-wise Restrictions"], row_offset=10, list_actions=())
        opened = _scroll_opened_conversation_snapshots("City-wise Restrictions")
        reader = _AutonomousReader(
            [page_1, page_1, page_1, page_1, page_2, page_2, page_2, page_2, page_2, opened, opened],
            {"available": True, "path": "W.1.4.13", "role": "AXButton", "title": {"literal": "City-wise Restrictions, preview text must not drive matching"}},
        )
        scroller = _ScrollService()
        result = self._run_open_chatgpt_project_chat(reader, chat_title="City-wise Restrictions", scroller=scroller)
        self.assertEqual(result["outcome"], "chat_opened_after_scrolling_via_axpress")
        self.assertEqual(result["scan_continuity"], "confirmed")
        self.assertTrue(result["overlap_adjacency_confirmed"])
        self.assertEqual(result["recovery_scroll_pulses_posted"], 0)
        self.assertEqual(len(scroller.scrolls), 1)
        self.assertIn(("W.1.4.13", "AXPress"), reader.actions)

    def test_focused_overlap_gap_triggers_recovery_not_blind_forward(self) -> None:
        page_1 = _scrollable_project_chat_page(["Alpha", "Bravo", "Charlie"], list_actions=())
        jump = _scrollable_project_chat_page(["Xray", "Yankee", "Zulu"], row_offset=10, list_actions=())
        reader = _AutonomousReader(
            [page_1] * 8 + [jump],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Xray, preview text must not drive matching"}},
        )
        scroller = _ScrollService()
        result = self._run_open_chatgpt_project_chat(reader, chat_title="Missing Chat", scroller=scroller, max_cycles=4)
        # A reverse (positive-delta) recovery pulse was posted instead of blindly
        # scrolling further forward over a skipped range.
        self.assertGreaterEqual(result["recovery_scroll_pulses_posted"], 1)
        self.assertTrue(any(delta > 0 for _x, _y, delta in scroller.scrolls))
        self.assertTrue(any("recovery_required" in summary or "reverse_micro_scroll" in summary for summary in result["search_cycle_summaries"]))

    def test_focused_no_progress_cannot_fire_while_continuity_unconfirmed(self) -> None:
        page_1 = _scrollable_project_chat_page(["Alpha", "Bravo", "Charlie"], list_actions=())
        jump = _scrollable_project_chat_page(["Xray", "Yankee", "Zulu"], row_offset=10, list_actions=())
        reader = _AutonomousReader(
            [page_1] * 8 + [jump],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Xray, preview text must not drive matching"}},
        )
        result = self._run_open_chatgpt_project_chat(reader, chat_title="Missing Chat", scroller=_ScrollService(), max_cycles=4)
        self.assertEqual(result["outcome"], "chat_list_scan_continuity_not_confirmed")
        self.assertNotEqual(result["outcome"], "chat_list_scroll_no_progress")
        self.assertEqual(result["end_of_list_state"], "unknown")

    def test_focused_anchor_end_requires_two_unchanged_forward_cycles(self) -> None:
        page = _scrollable_project_chat_page(["Alpha", "Bravo", "Charlie"])
        reader = _AutonomousReader(
            [page] * 12,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Alpha, preview text must not drive matching"}},
        )
        result = self._run_open_chatgpt_project_chat(reader, chat_title="Missing Chat")
        self.assertEqual(result["outcome"], "chat_list_end_reached_without_match")
        self.assertEqual(result["end_of_list_state"], "confirmed")
        self.assertEqual(result["scroll_pulses_posted"], 2)

        # A single forward cycle is not enough to conclude the end.
        reader_one = _AutonomousReader(
            [page] * 12,
            {"available": True, "path": "W.1.4.1", "role": "AXButton", "title": {"literal": "Alpha, preview text must not drive matching"}},
        )
        one_cycle = self._run_open_chatgpt_project_chat(reader_one, chat_title="Missing Chat", max_cycles=1)
        self.assertNotEqual(one_cycle["outcome"], "chat_list_end_reached_without_match")

    def test_focused_insufficient_continuity_never_concludes_not_found(self) -> None:
        page_1 = _scrollable_project_chat_page(["Alpha", "Bravo", "Charlie"], list_actions=())
        jump = _scrollable_project_chat_page(["Xray", "Yankee", "Zulu"], row_offset=10, list_actions=())
        reader = _AutonomousReader(
            [page_1] * 8 + [jump],
            {"available": True, "path": "W.1.4.11", "role": "AXButton", "title": {"literal": "Xray, preview text must not drive matching"}},
        )
        result = self._run_open_chatgpt_project_chat(reader, chat_title="Missing Chat", scroller=_ScrollService(), max_cycles=4)
        self.assertEqual(result["outcome"], "chat_list_scan_continuity_not_confirmed")
        self.assertNotEqual(result["outcome"], "chat_not_found_in_project")
        self.assertEqual(result["scan_continuity"], "not_confirmed")

    def test_focused_strict_matching_axpress_and_safety_boundaries_unchanged(self) -> None:
        # Strict exact / exact-prefix matching and comma fail-closed are intact.
        comma_row = {"accessibility_row_text": "City-wise Restrictions, preview text must not drive matching"}
        self.assertTrue(nav._project_chat_row_match_representation(comma_row, "City-wise Restrictions")["matched"])
        self.assertFalse(nav._project_chat_row_match_representation(comma_row, "preview text must not drive matching")["matched"])
        # A requested title containing a comma stays fail-closed at the plan level.
        comma_plan = nav._project_chat_open_plan_from_snapshots(
            _scrollable_project_chat_page(["Alpha"]),
            {"visited_nodes": 1},
            {"window_source": "synthetic", "window": _scrollable_project_chat_page(["Alpha"])[0]},
            (0, 0, 1200, 900),
            (0, 0, 1200, 900),
            "PTG Assistant",
            "Alpha, Bravo",
            _DisplayFactory(_DisplayProbe()),
        )
        self.assertEqual(comma_plan["status"], "chat_title_not_unambiguously_representable_by_accessibility")
        # AXPress remains preferred when the row exposes it.
        row_node = nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXButton", actions=("AXPress",))
        title_node = nav.AXElementSnapshot(path="W.1.4.1.1", depth=4, role="AXStaticText")
        self.assertEqual(nav._project_chat_axpress_target(title_node, row_node)["relation"], "row_node")
        # The scan/recovery slice contains no keyboard, OCR, browser, or text-entry.
        source = Path(nav.__file__).read_text(encoding="utf-8")
        slice_source = source[
            source.index("def _bounded_project_chat_scroll_search"):
            source.index("def _project_chat_scroll_search_result(")
        ]
        for token in ("keyDown", "CGEventKeyboardEvent", "paste_clipboard", "press_enter", "screenshot", "ocr", "playwright", "selenium", "write_text"):
            self.assertNotIn(token, slice_source)

    def test_project_chat_open_prefers_axpress_and_requires_post_action_evidence(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3
            + [_project_visible_chats_snapshots()] * 2
            + [_project_visible_chats_snapshots()] * 2
            + [_project_chat_opened_snapshots()] * 2,
            {"available": True, "path": "W.2.5.2.1", "role": "AXStaticText", "title": {"literal": "Profile Photo Verification Change"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_via_axpress")
        self.assertEqual(result["chosen_method"], "axpress")
        self.assertIn(("W.2.5.2", "AXPress"), reader.actions)
        self.assertEqual(clicker.clicks, [])
        self.assertTrue(result["post_action_evidence"]["confirmed"])

    def test_project_chat_geometry_click_hit_tests_before_one_click(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3
            + [_project_visible_chats_without_axpress()] * 2
            + [_project_visible_chats_without_axpress()] * 4
            + [_project_chat_opened_snapshots()] * 2,
            {"available": True, "path": "W.2.5.2.1", "role": "AXStaticText", "title": {"literal": "Profile Photo Verification Change"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "chat_opened_via_validated_click")
        self.assertEqual(result["chosen_method"], "validated_geometry_click")
        self.assertEqual(len(clicker.clicks), 1)
        self.assertEqual(len(reader.hit_tests), 1)
        self.assertEqual(result["calculated_point_hit_test_relationship"], "exact_target_title")
        self.assertEqual([event["event"] for event in result["actions_performed"][-2:]], ["left_mouse_down", "left_mouse_up"])

    def test_project_chat_geometry_rejects_hit_test_mismatch_before_click(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3
            + [_project_visible_chats_without_axpress()] * 2
            + [_project_visible_chats_without_axpress()] * 4,
            {"available": True, "path": "W.2.5.1.1", "role": "AXStaticText", "title": {"literal": "Apple Content Moderation Requirements"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "calculated_point_hit_test_mismatch")
        self.assertEqual(clicker.clicks, [])

    def test_project_chat_success_not_reported_without_post_action_evidence(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3
            + [_project_visible_chats_without_axpress()] * 2
            + [_project_visible_chats_without_axpress()] * 4
            + [_project_visible_chats_without_axpress()] * 2,
            {"available": True, "path": "W.2.5.2.1", "role": "AXStaticText", "title": {"literal": "Profile Photo Verification Change"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Profile Photo Verification Change",
                confirm_open_chat=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "action_posted_but_chat_not_confirmed")
        self.assertEqual(len(clicker.clicks), 1)
        self.assertFalse(result["post_action_evidence"]["confirmed"])

    def test_project_chat_open_source_has_no_cursor_keyboard_text_input_ocr_or_browser(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        command_source = source[
            source.index("def open_chatgpt_project_chat"):
            source.index("def _base_autonomous_open_result")
        ]
        for token in (
            "CGEventGetLocation",
            "current_mouse_location",
            "CGWarpMouseCursorPosition",
            "keyboard",
            "press_enter",
            "paste_clipboard",
            "AXShowMenu",
            "screenshot",
            "ocr",
            "write_text",
            "osascript",
            "playwright",
            "selenium",
        ):
            self.assertNotIn(token, command_source)

    def test_project_visible_chats_more_rows_indicator_can_report_false_from_scrollbar(self) -> None:
        snapshots = _project_visible_chats_snapshots(partial_last_row=False) + [
            nav.AXElementSnapshot(path="W.2.5.4", depth=3, role="AXScrollBar", value="1.0", frame=(1064, 140, 10, 520))
        ]
        reader = _ActionReader([snapshots])

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.inspect_chatgpt_project_visible_chats(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["more_rows_may_exist_below"], False)
        self.assertTrue(all(chat["visibility"] == "fully_visible" for chat in result["visible_chats"]))

    def test_project_visible_chats_source_slice_has_no_action_event_scroll_or_cursor_api(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        inspector_source = source[
            source.index("def inspect_chatgpt_project_visible_chats"):
            source.index("def open_chatgpt_sidebar_destination")
        ]
        for token in (
            "activate_chatgpt",
            "perform_action",
            "left_click",
            "CGEvent",
            "CGWarpMouseCursorPosition",
            "current_mouse_location",
            "AXUIElementPerformAction",
            "paste_clipboard",
            "press_enter",
            "scroll",
            "AXPress)",
        ):
            self.assertNotIn(token, inspector_source)

    def test_project_visible_chats_cli_wiring_is_read_only(self) -> None:
        result = nav._base_project_visible_chats_result("PTG Assistant", "ChatGPT")
        result.update(
            {
                "ok": True,
                "status": "visible_chats_found",
                "visible_chat_count": 1,
                "project_content_container": {"path": "W.2", "frame": {"x": 300, "y": 0, "width": 900, "height": 900}},
                "chat_list_container": {"path": "W.2.5", "frame": {"x": 320, "y": 140, "width": 760, "height": 520}},
                "visible_chats": [
                    {
                        "ordinal": 1,
                        "title": "Apple Content Moderation Requirements",
                        "preview": "READ-ONLY COMPLIANCE AUDIT",
                        "row_frame": {"x": 320, "y": 156, "width": 760, "height": 70},
                        "visibility": "fully_visible",
                        "path": "W.2.5.1",
                        "role": "AXGroup",
                        "subrole": "",
                        "ax_press_available": True,
                    }
                ],
                "excluded_candidate_counts": {"header_tab_or_control": 2},
            }
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli._print_inspect_chatgpt_project_visible_chats_result(result)

        text = stdout.getvalue()
        self.assertIn("ChatGPT project visible chats", text)
        self.assertIn("actions_performed: []", text)
        self.assertIn("1. Apple Content Moderation Requirements", text)

    def test_project_chat_row_ax_cli_dispatches_read_only_audit_with_repeated_titles(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": True,
            "status": "row_audit_ready",
            "project_title": "PTG Assistant",
            "chat_titles": ["Content Moderation", "AWS Profile Photo Verification"],
            "project_resolution_status": "visible_chats_found",
            "visible_chat_count": 2,
            "actions_performed": [],
            "row_audits": [
                {
                    "requested_chat_title": "Content Moderation",
                    "status": "row_audit_ready",
                    "accepted_row": {
                        "row_path": "W.1.4.1",
                        "row_role": "AXButton",
                        "row_subrole": "",
                        "row_frame": {"x": 282, "y": 176, "width": 779, "height": 65},
                        "resolver_title": "Content Moderation, preview",
                    },
                    "raw_flattened_row_text": "Content Moderation, preview",
                    "summary": {
                        "row_exposes_merged_text": True,
                        "exact_title_node_paths": ["W.1.4.1.1"],
                        "preview_like_node_paths": ["W.1.4.1.3"],
                        "punctuation_only_node_paths": ["W.1.4.1.2"],
                    },
                    "nodes": [
                        {
                            "path": "W.1.4.1",
                            "relative_depth": 0,
                            "child_index": 0,
                            "role": "AXButton",
                            "subrole": "",
                            "frame": {"x": 282, "y": 176, "width": 779, "height": 65},
                            "actions": ["AXPress"],
                            "AXTitle": "Content Moderation, preview",
                            "AXValue": "",
                            "AXDescription": "",
                            "text_classification": "preview-like",
                        }
                    ],
                }
            ],
            "error": "",
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "inspect-chatgpt-project-chat-row-ax",
                    "--project-title",
                    "PTG Assistant",
                    "--chat-title",
                    "Content Moderation",
                    "--chat-title",
                    "AWS Profile Photo Verification",
                ],
            ),
            mock.patch.object(cli, "inspect_chatgpt_project_chat_row_ax", return_value=result) as inspect,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        inspect.assert_called_once_with(
            app_name="ChatGPT",
            project_title="PTG Assistant",
            chat_titles=["Content Moderation", "AWS Profile Photo Verification"],
            max_depth=16,
            max_nodes=900,
        )
        output = stdout.getvalue()
        self.assertIn("ChatGPT project chat row AX audit", output)
        self.assertIn("audit_1_row_path: W.1.4.1", output)
        self.assertIn("AXTitle: Content Moderation, preview", output)
        self.assertIn("actions_performed: []", output)

    def test_diagnose_project_chat_rows_cli_dispatches_read_only_visual_row_diagnostic(self) -> None:
        stdout = io.StringIO()
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
            contains_title="Mock Data Insertion SQL",
        )
        result["app_name"] = "ChatGPT"
        result["pid_present"] = True

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "diagnose-chatgpt-project-chat-rows",
                    "--project-title",
                    "PTG Assistant",
                    "--contains-title",
                    "Mock Data Insertion SQL",
                ],
            ),
            mock.patch.object(cli, "diagnose_chatgpt_project_chat_rows", return_value=result) as diagnose,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        diagnose.assert_called_once_with(
            app_name="ChatGPT",
            project_title="PTG Assistant",
            contains_title="Mock Data Insertion SQL",
            max_depth=16,
            max_nodes=900,
        )
        output = stdout.getvalue()
        for heading in (
            "ChatGPT Project Chat Row Diagnostic",
            "Project/list identity",
            "Confirmed list viewport",
            "Current resolver accepted rows",
            "Visual row bands",
            "Band candidate evidence",
            "Current resolver comparison",
            "Experimental canonical titles",
            "Summary",
        ):
            self.assertIn(heading, output)
        self.assertIn("filtered_bands_printed: 1", output)
        self.assertIn("final_outcome: diagnostic_ready", output)
        self.assertIn("actions_performed: []", output)

    def test_existing_project_chat_open_cli_dispatch_remains_unchanged(self) -> None:
        result = {"ok": False, "outcome": "dry_run_ready", "project_title": "PTG Assistant", "chat_title": "Content Moderation"}

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "open-chatgpt-project-chat",
                    "--project-title",
                    "PTG Assistant",
                    "--chat-title",
                    "Content Moderation",
                ],
            ),
            mock.patch.object(cli, "open_chatgpt_project_chat", return_value=result) as open_chat,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cli.main()

        open_chat.assert_called_once()
        _, kwargs = open_chat.call_args
        self.assertEqual(kwargs["project_title"], "PTG Assistant")
        self.assertEqual(kwargs["chat_title"], "Content Moderation")
        self.assertFalse(kwargs["confirm_open_chat"])

    def test_project_chat_open_cli_confirmation_notice_and_dispatch(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": True,
            "outcome": "chat_opened_via_axpress",
            "project_title": "PTG Assistant",
            "chat_title": "Profile Photo Verification Change",
            "project_open_result": {"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 3},
            "visible_chat_count": 3,
            "matched_chat_row": {
                "title": "Profile Photo Verification Change",
                "row_path": "W.2.5.2",
                "title_path": "W.2.5.2.1",
                "row_frame": {"x": 320, "y": 232, "width": 760, "height": 70},
                "visibility": "fully_visible",
            },
            "matched_chat_title": "Profile Photo Verification Change",
            "matched_title_representation": "requested_exact_prefix_before_preview_separator",
            "matched_accessibility_text_truncated": "Profile Photo Verification Change, okay, is there a 365 day lockout...",
            "chosen_method": "axpress",
            "post_action_evidence": {"signals": [{"type": "active_conversation_identity_outside_chat_list"}]},
            "actions_performed": [{"path": "W.2.5.2", "action": "AXPress"}],
            "error": "",
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "open-chatgpt-project-chat",
                    "--project-title",
                    "PTG Assistant",
                    "--chat-title",
                    "Profile Photo Verification Change",
                    "--confirm-open-chat",
                ],
            ),
            mock.patch.object(cli, "open_chatgpt_project_chat", return_value=result) as opener,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        opener.assert_called_once()
        self.assertTrue(opener.call_args.kwargs["confirm_open_chat"])
        self.assertEqual(opener.call_args.kwargs["project_title"], "PTG Assistant")
        self.assertEqual(opener.call_args.kwargs["chat_title"], "Profile Photo Verification Change")
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0], "Explicit ChatGPT project chat open authorized.")
        output = stdout.getvalue()
        self.assertIn("final_outcome: chat_opened_via_axpress", output)
        self.assertIn("matched_chat_title: Profile Photo Verification Change", output)
        self.assertIn("matched_title_representation: requested_exact_prefix_before_preview_separator", output)
        self.assertIn("matched_accessibility_text_truncated: Profile Photo Verification Change, okay", output)

    def test_project_chat_open_cli_dry_run_keeps_actions_empty(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": True,
            "outcome": "dry_run_ready",
            "project_title": "PTG Assistant",
            "chat_title": "Profile Photo Verification Change",
            "project_open_result": {"ok": True, "outcome": "dry_run_ready", "visible_chat_count": 3},
            "visible_chat_count": 3,
            "matched_chat_row": {
                "title": "Profile Photo Verification Change",
                "row_path": "W.2.5.2",
                "title_path": "W.2.5.2.1",
                "row_frame": {"x": 320, "y": 232, "width": 760, "height": 70},
                "visibility": "fully_visible",
            },
            "chosen_method": "axpress",
            "post_action_evidence": {},
            "actions_performed": [],
            "error": "",
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "open-chatgpt-project-chat",
                    "--project-title",
                    "PTG Assistant",
                    "--chat-title",
                    "Profile Photo Verification Change",
                ],
            ),
            mock.patch.object(cli, "open_chatgpt_project_chat", return_value=result) as opener,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(opener.call_args.kwargs["confirm_open_chat"])
        self.assertNotIn("Explicit ChatGPT project chat open authorized.", stdout.getvalue())
        self.assertIn("actions_performed: []", stdout.getvalue())

    def test_project_chat_open_printer_includes_no_match_canonical_diagnostics_only_for_no_match(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": False,
            "outcome": "chat_not_currently_visible",
            "project_title": "PTG Assistant",
            "chat_title": "Missing Exact Title",
            "project_open_result": {"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 6},
            "visible_chat_count": 3,
            "targeting_visible_chat_count": 3,
            "visible_chat_count_stage_explanation": "project_open_result.visible_chat_count was captured during project-open confirmation; visible_chat_count is from the fresh targeting AX snapshot after project-open settled.",
            "matched_chat_row": {},
            "chosen_method": "",
            "canonical_visible_chat_titles_considered": ["Content Moderation", "AWS Profile Photo Verification"],
            "canonical_visible_chat_count_considered": 2,
            "visible_chat_accessibility_representation_summary": [
                {
                    "row_path": "W.1.4.1",
                    "title": "Content Moderation",
                    "title_representation": "unresolved",
                    "preview_representation": "merged_accessibility_suffix",
                    "accessibility_text_truncated": "Content Moderation, redacted suffix",
                }
            ],
            "resolver_snapshot_id": "ax:42:abcdef1234",
            "post_action_evidence": {},
            "actions_performed": [],
            "error": "Requested chat title was not found among currently visible canonical chat rows.",
        }

        with contextlib.redirect_stdout(stdout):
            cli._print_open_chatgpt_project_chat_result(result)

        text = stdout.getvalue()
        self.assertIn("canonical_visible_chat_titles_considered: ['Content Moderation', 'AWS Profile Photo Verification']", text)
        self.assertIn("canonical_visible_chat_count_considered: 2", text)
        self.assertIn("resolver_snapshot_id: ax:42:abcdef1234", text)
        self.assertIn("visible_chat_accessibility_representation_summary:", text)
        self.assertIn("visible_chat_count_stage_explanation:", text)

    def test_project_chat_open_printer_includes_hydration_search_contract(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": False,
            "outcome": "chat_search_budget_exhausted_without_confirmed_end",
            "project_title": "PTG Assistant",
            "chat_title": "Missing Exact Title",
            "project_open_result": {"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 6},
            "visible_chat_count": 3,
            "targeting_visible_chat_count": 3,
            "search_cycles_attempted": 2,
            "max_search_cycles": 60,
            "configured_max_search_cycles": 60,
            "configured_max_search_elapsed_seconds": 90.0,
            "scroll_pulses_posted": 2,
            "scroll_method_used": "semantic_ax_scroll",
            "initial_hydration_status": "list_stable_no_change",
            "hydration_events_observed": 1,
            "reset_events_observed": 1,
            "unique_accessibility_rows_seen": 9,
            "unique_effective_viewports_seen": 3,
            "new_accessibility_rows_seen": 6,
            "target_match_checked_on_samples": 8,
            "hydration_samples_taken": 6,
            "settled_cycles_completed": 2,
            "progressful_cycles_completed": 1,
            "target_found_during_hydration_cycle": 2,
            "target_found_after_scrolling": False,
            "end_of_list_state": "unknown",
            "computed_scroll_delta_y": -48,
            "median_visible_row_height": 64.0,
            "previous_settled_viewport_signature": "viewport:23:12:64:43|more_below:True",
            "current_settled_viewport_signature": "viewport:23:18:64:43|more_below:True",
            "overlap_row_count": 2,
            "overlap_adjacency_confirmed": True,
            "scan_continuity": "confirmed",
            "recovery_scroll_pulses_posted": 0,
            "search_elapsed_seconds": 2.4,
            "search_cycle_summaries": ["cycle_2: micro_scroll_posted -> overlap_confirmed -> 4_new_rows -> settled"],
            "matched_chat_row": {},
            "post_action_evidence": {},
            "actions_performed": [{"path": "W.1.4", "action": "AXScrollDown"}],
            "error": "Maximum bounded search cycles were exhausted.",
        }

        with contextlib.redirect_stdout(stdout):
            cli._print_open_chatgpt_project_chat_result(result)

        text = stdout.getvalue()
        self.assertIn("search_cycles_attempted: 2", text)
        self.assertIn("max_search_cycles: 60", text)
        self.assertIn("configured_max_search_cycles: 60", text)
        self.assertIn("configured_max_search_elapsed_seconds: 90.0", text)
        self.assertIn("scroll_pulses_posted: 2", text)
        self.assertIn("initial_hydration_status: list_stable_no_change", text)
        self.assertIn("hydration_events_observed: 1", text)
        self.assertIn("reset_events_observed: 1", text)
        self.assertIn("unique_accessibility_rows_seen: 9", text)
        self.assertIn("unique_effective_viewports_seen: 3", text)
        self.assertIn("new_accessibility_rows_seen: 6", text)
        self.assertIn("target_match_checked_on_samples: 8", text)
        self.assertIn("hydration_samples_taken: 6", text)
        self.assertIn("settled_cycles_completed: 2", text)
        self.assertIn("progressful_cycles_completed: 1", text)
        self.assertIn("end_of_list_state: unknown", text)
        self.assertIn("computed_scroll_delta_y: -48", text)
        self.assertIn("median_visible_row_height: 64.0", text)
        self.assertIn("scan_continuity: confirmed", text)
        self.assertIn("recovery_scroll_pulses_posted: 0", text)
        self.assertIn("overlap_row_count: 2", text)
        self.assertIn("overlap_adjacency_confirmed: True", text)
        self.assertIn("current_settled_viewport_signature: viewport:23:18:64:43|more_below:True", text)
        self.assertIn("search_elapsed_seconds: 2.4", text)
        self.assertIn("target_found_during_hydration_cycle: 2", text)
        self.assertIn("cycle_2: micro_scroll_posted -> overlap_confirmed -> 4_new_rows -> settled", text)

    def test_project_chat_live_discovery_printer_flushes_incrementally(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), mock.patch.object(stdout, "flush", wraps=stdout.flush) as flushed:
            cli._print_live_project_chat_discovery_lines(["Chats discovered:", "1. Content Moderation"])

        self.assertEqual(stdout.getvalue(), "Chats discovered:\n1. Content Moderation\n")
        flushed.assert_called()

    def test_autonomous_command_parser_is_separate_from_manual_calibration(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "open-chatgpt-sidebar-destination",
                "--kind",
                "project",
                "--title",
                "PTG Assistant",
                "--confirm-open-destination",
            ]
        )

        self.assertEqual(args.command, "open-chatgpt-sidebar-destination")
        self.assertTrue(args.confirm_open_destination)
        self.assertFalse(hasattr(args, "confirm_calibration_click"))

    def test_autonomous_source_does_not_use_cursor_location_or_calibration_hit_test(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        autonomous_source = source[
            source.index("def open_chatgpt_sidebar_destination"):
            source.index("def _primary_selection_candidate")
        ]
        for token in (
            "CGEventGetLocation",
            "current_mouse_location",
            "_collect_calibration_hit_test",
            "_collect_calibration_display_evidence",
            "_classify_coordinate_mapping",
            "distance_from_cursor",
            "current_global_physical_cursor_location",
            "CGWarpMouseCursorPosition",
        ):
            self.assertNotIn(token, autonomous_source)

    def test_autonomous_dry_run_has_no_activation_or_action_side_effects(self) -> None:
        reader = _AutonomousReader([_detailed_sidebar_snapshots()] * 3, {"available": True, "path": "W.1.3.1"})
        activation = mock.Mock(return_value={"activated": True, "is_frontmost": True, "app_name": "ChatGPT", "error": ""})
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=False,
                activation_function=activation,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["chosen_method"], "axpress")
        self.assertEqual(result["actions_performed"], [])
        self.assertEqual(clicker.clicks, [])
        self.assertEqual(reader.actions, [])
        activation.assert_not_called()
        self.assertEqual(result["activation_result"]["error"], "skipped_dry_run")

    def test_autonomous_dry_run_accepts_nested_scrollarea_sidebar_project(self) -> None:
        snapshots = _nested_scrollarea_sidebar_snapshots()
        reader = _AutonomousReader([snapshots] * 3, {"available": True, "path": "W.1.2.1"})

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=False,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "dry_run_ready")
        self.assertEqual(result["chosen_method"], "axpress")
        self.assertEqual(result["target_match_count"], 1)
        self.assertEqual(result["actions_performed"], [])

    def test_autonomous_exact_title_missing_and_ambiguous_fail_closed(self) -> None:
        duplicate = _detailed_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXStaticText", value="PTG Assistant", enabled=True, frame=(20, 156, 120, 16))
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            missing = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="Not Visible",
                confirm_open_destination=False,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_detailed_sidebar_snapshots()] * 3, {"available": True, "path": ""})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )
            ambiguous = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=False,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([duplicate] * 3, {"available": True, "path": ""})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(missing["outcome"], "target_absent")
        self.assertEqual(ambiguous["outcome"], "target_ambiguous")
        self.assertEqual(missing["actions_performed"], [])
        self.assertEqual(ambiguous["actions_performed"], [])

    def test_autonomous_prefers_axpress_when_present_and_requires_post_evidence(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3 + [_project_visible_chats_snapshots()] * 2,
            {"available": True, "path": "W.1.3.1"},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "destination_opened_and_visible_chats_resolved")
        self.assertEqual(result["visible_chat_count"], 3)
        self.assertEqual(result["visible_chats"][0]["title"], "Apple Content Moderation Requirements")
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(clicker.clicks, [])
        self.assertGreaterEqual(len(result["post_action_evidence"]["signals"]), 3)

    def test_autonomous_open_invokes_shared_project_chat_resolver_after_action(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3 + [_project_visible_chats_snapshots()] * 2,
            {"available": True, "path": "W.1.3.1"},
        )

        with mock.patch.object(nav.sys, "platform", "darwin"), mock.patch.object(
            nav,
            "resolve_open_project_content_and_visible_chats",
            wraps=nav.resolve_open_project_content_and_visible_chats,
        ) as resolver:
            result = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(_ClickService()),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "destination_opened_and_visible_chats_resolved")
        self.assertGreaterEqual(resolver.call_count, 1)
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])

    def test_autonomous_project_open_partial_chat_resolution_returns_distinct_outcome_without_extra_click(self) -> None:
        unresolved_project = [
            snapshot
            for snapshot in _project_visible_chats_snapshots()
            if not snapshot.path.startswith("W.2.5")
        ]
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 3 + [unresolved_project] * 2,
            {"available": True, "path": "W.1.3.1"},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        # The project content pane is present but no Chats-list container can be
        # forward-resolved, so the identity gate fails closed without any extra
        # interaction.
        self.assertEqual(result["outcome"], "project_chat_list_identity_not_confirmed")
        self.assertEqual(result["visible_chat_count"], 0)
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(clicker.clicks, [])

    def test_autonomous_falls_back_to_geometry_after_unverified_axpress(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 7 + [_project_visible_chats_snapshots()] * 2,
            {"available": True, "path": "W.1.3.1", "role": "AXStaticText", "title": {"literal": "PTG Assistant"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project",
                title="PTG Assistant",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "destination_opened_and_visible_chats_resolved")
        self.assertEqual(result["visible_chat_count"], 3)
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(len(clicker.clicks), 1)
        self.assertEqual(result["calculated_point_hit_test_relationship"], "exact_target_title")

    def test_autonomous_geometry_click_uses_fresh_frame_hit_test_and_one_click(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 5 + [_post_autonomous_open_snapshots("Markdown Formatting Guide")],
            {"available": True, "path": "W.1.7", "role": "AXButton", "title": {"literal": "Markdown Formatting Guide"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "destination_opened_via_validated_click")
        self.assertEqual(result["chosen_method"], "validated_geometry_click")
        self.assertEqual(len(clicker.clicks), 1)
        self.assertEqual(reader.hit_tests[0][1], (92.5, 264.0))
        self.assertEqual(result["calculated_global_point"], {"ok": True, "x": 92.5, "y": 264.0, "reason": "fresh_title_frame_center_left_interior_point"})
        self.assertEqual([event["event"] for event in result["actions_performed"]], ["left_mouse_down", "left_mouse_up"])

    def test_autonomous_hit_test_must_pass_before_click(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 5,
            {"available": True, "path": "W.1.8", "role": "AXButton", "title": {"literal": ""}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "calculated_point_hit_test_mismatch")
        self.assertEqual(clicker.clicks, [])

    def test_autonomous_point_must_be_inside_windowserver_and_display_bounds(self) -> None:
        with mock.patch.object(nav.sys, "platform", "darwin"):
            window_fail = nav.open_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_open_destination=False,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_detailed_sidebar_snapshots()] * 3, {"available": True, "path": "W.1.7"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 50, 50)}])),
                sleep_function=_SleepRecorder(),
            )
            display_fail = nav.open_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_open_destination=False,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_AutonomousReader([_detailed_sidebar_snapshots()] * 3, {"available": True, "path": "W.1.7"})),
                display_probe_factory=_DisplayFactory(_DisplayProbe(primary=(1300, 0, 100, 100))),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(window_fail["outcome"], "target_offscreen")
        self.assertEqual(display_fail["outcome"], "target_offscreen")

    def test_autonomous_post_action_success_requires_actual_evidence(self) -> None:
        reader = _AutonomousReader(
            [_detailed_sidebar_snapshots()] * 6,
            {"available": True, "path": "W.1.7", "role": "AXButton", "title": {"literal": "Markdown Formatting Guide"}},
        )
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_open_destination=True,
                activation_function=lambda app_name: {"activated": True, "is_frontmost": True, "app_name": app_name, "frontmost_app": app_name, "error": ""},
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "action_posted_but_destination_not_confirmed")
        self.assertFalse(result["ok"])
        self.assertEqual(len(clicker.clicks), 1)
        self.assertFalse(result["post_action_evidence"]["confirmed"])

    def test_frame_click_dry_run_does_not_click_and_computes_safe_point(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots()])
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_frame_click=False,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                settle_seconds=0,
            )

        self.assertEqual(result["status"], "dry_run_ready")
        self.assertEqual(result["actions_performed"], [])
        self.assertEqual(clicker.clicks, [])
        self.assertTrue(result["frame_safety"]["safety_checks_passed"])
        point = result["click_point"]
        frame = result["frame_safety"]["source_frame"]
        self.assertGreater(point["x"], frame["x"] + nav.SAFE_CLICK_EDGE_INSET)
        self.assertLess(point["x"], frame["x"] + frame["width"] - nav.SAFE_CLICK_OVERFLOW_EXCLUSION_WIDTH)
        self.assertEqual(point["y"], frame["y"] + frame["height"] / 2)

    def test_frame_click_requires_exact_visible_title_and_fails_closed_for_missing_duplicate(self) -> None:
        duplicate = _detailed_sidebar_snapshots() + [
            nav.AXElementSnapshot(path="W.1.9", depth=2, role="AXButton", title="Markdown Formatting Guide", actions=("AXShowMenu",), enabled=True, frame=(10, 320, 250, 32))
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            missing = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Not Visible",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([_detailed_sidebar_snapshots()])),
                click_service_factory=_ClickFactory(_ClickService()),
                settle_seconds=0,
            )
            ambiguous = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([duplicate])),
                click_service_factory=_ClickFactory(_ClickService()),
                settle_seconds=0,
            )

        self.assertEqual(missing["status"], "target_not_found")
        self.assertEqual(ambiguous["status"], "target_ambiguous")
        self.assertEqual(missing["actions_performed"], [])
        self.assertEqual(ambiguous["actions_performed"], [])

    def test_frame_click_invalid_small_and_offscreen_frames_fail_closed(self) -> None:
        small = [
            snapshot if snapshot.path != "W.1.7" else nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                title=snapshot.title,
                value=snapshot.value,
                enabled=snapshot.enabled,
                actions=snapshot.actions,
                selected=snapshot.selected,
                attribute_names=snapshot.attribute_names,
                frame=(10, 248, 40, 10),
            )
            for snapshot in _detailed_sidebar_snapshots()
        ]
        offscreen = [
            snapshot if snapshot.path != "W.1.7" else nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                title=snapshot.title,
                value=snapshot.value,
                enabled=snapshot.enabled,
                actions=snapshot.actions,
                selected=snapshot.selected,
                attribute_names=snapshot.attribute_names,
                frame=(900, 248, 250, 32),
            )
            for snapshot in _detailed_sidebar_snapshots()
        ]

        with mock.patch.object(nav.sys, "platform", "darwin"):
            small_result = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([small])),
                click_service_factory=_ClickFactory(_ClickService()),
                settle_seconds=0,
            )
            offscreen_result = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([offscreen])),
                click_service_factory=_ClickFactory(_ClickService()),
                settle_seconds=0,
            )

        self.assertEqual(small_result["status"], "target_frame_invalid")
        self.assertEqual(offscreen_result["status"], "target_not_visible")

    def test_frame_click_re_resolution_detects_material_change_before_click(self) -> None:
        changed = [
            snapshot if snapshot.path != "W.1.7" else nav.AXElementSnapshot(
                path=snapshot.path,
                depth=snapshot.depth,
                role=snapshot.role,
                title=snapshot.title,
                value=snapshot.value,
                enabled=snapshot.enabled,
                actions=snapshot.actions,
                selected=snapshot.selected,
                attribute_names=snapshot.attribute_names,
                frame=(10, 400, 250, 32),
            )
            for snapshot in _detailed_sidebar_snapshots()
        ]
        reader = _ActionReader([_detailed_sidebar_snapshots(), changed])
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_frame_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                settle_seconds=0,
            )

        self.assertEqual(result["status"], "target_frame_invalid")
        self.assertEqual(clicker.clicks, [])

    def test_frame_click_confirm_posts_one_sequence_and_requires_post_evidence(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots(), _detailed_sidebar_snapshots(), _post_frame_click_snapshots()])
        clicker = _ClickService()
        notices: list[tuple[str, str]] = []

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_frame_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                settle_seconds=0,
                before_click_callback=lambda kind, title: notices.append((kind, title)),
            )

        self.assertEqual(result["status"], "verified_selection_changed")
        self.assertEqual(len(clicker.clicks), 1)
        self.assertEqual([event["event"] for event in result["actions_performed"]], ["left_mouse_down", "left_mouse_up"])
        self.assertEqual(notices, [("chat", "Markdown Formatting Guide")])

    def test_frame_click_success_not_reported_without_post_evidence(self) -> None:
        reader = _ActionReader([_detailed_sidebar_snapshots(), _detailed_sidebar_snapshots(), _detailed_sidebar_snapshots()])
        clicker = _ClickService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_frame_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                settle_seconds=0,
            )

        self.assertEqual(result["status"], "click_performed_no_observable_change")
        self.assertFalse(result["ok"])
        self.assertEqual(len(clicker.clicks), 1)

    def test_frame_click_permission_and_non_macos_fail_safely(self) -> None:
        with mock.patch.object(nav.sys, "platform", "linux"):
            non_macos = nav.verify_chatgpt_sidebar_frame_click(kind="chat", title="Markdown Formatting Guide")

        with mock.patch.object(nav.sys, "platform", "darwin"):
            denied = nav.verify_chatgpt_sidebar_frame_click(
                kind="chat",
                title="Markdown Formatting Guide",
                confirm_frame_click=True,
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(_ActionReader([_detailed_sidebar_snapshots(), _detailed_sidebar_snapshots()])),
                click_service_factory=_ClickFactory(_ClickService(permitted=False)),
                settle_seconds=0,
            )

        self.assertEqual(non_macos["status"], "accessibility_failure")
        self.assertEqual(denied["status"], "permission_denied")
        self.assertEqual(denied["actions_performed"], [])

    def test_frame_click_source_slice_avoids_disallowed_automation_channels(self) -> None:
        source = Path(nav.__file__).read_text(encoding="utf-8")
        frame_source = source[
            source.index("def verify_chatgpt_sidebar_frame_click"):
            source.index("def verify_chatgpt_sidebar_destination")
        ]
        for token in ("CGEventKeyboard", "ScrollWheel", "drag", "double", "osascript", "AppleScript", "clipboard", "selenium", "playwright"):
            self.assertNotIn(token, frame_source)

    # --- Project Chats-list identity gate -------------------------------------

    def test_identity_gate_rejects_composer_controls_as_project_chat_rows(self) -> None:
        # Composer controls placed outside the resolved Chats-list container must
        # never be admitted alongside the real rows.
        snapshots = _project_visible_chats_snapshots(partial_last_row=False) + [
            nav.AXElementSnapshot(path="W.2.20", depth=2, role="AXButton", title="Attach", actions=("AXPress",), frame=(340, 820, 60, 28)),
            nav.AXElementSnapshot(path="W.2.21", depth=2, role="AXButton", title="Work with Apps", actions=("AXPress",), frame=(410, 820, 130, 28)),
        ]
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))
        self.assertEqual(result["status"], "visible_chats_found")
        titles = [chat["title"] for chat in result["visible_chats"]]
        self.assertNotIn("Attach", titles)
        self.assertNotIn("Work with Apps", titles)
        self.assertEqual(result["valid_project_chat_row_count"], 3)

        # And when composer controls are the *only* candidates inside a list-shaped
        # frame, the gate fails closed because they are below the minimum row height.
        composer_only = _project_content_shell_without_list([
            nav.AXElementSnapshot(path="W.2.5", depth=2, role="AXScrollArea", subrole="AXList", frame=(320, 140, 760, 520)),
            nav.AXElementSnapshot(path="W.2.5.1", depth=3, role="AXButton", title="Attach", actions=("AXPress",), frame=(330, 150, 60, 15)),
            nav.AXElementSnapshot(path="W.2.5.2", depth=3, role="AXButton", title="Work with Apps", actions=("AXPress",), frame=(330, 172, 130, 15)),
        ])
        contaminated = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", composer_only, (0, 0, 1200, 900))
        self.assertEqual(contaminated["status"], "project_chat_list_identity_not_confirmed")
        self.assertEqual(contaminated["project_chat_list_identity"], "not_confirmed")
        self.assertIn("candidate_row_height_below_minimum", contaminated["identity_failure_reasons"])
        self.assertEqual(contaminated["visible_chats"], [])

    def test_identity_gate_never_accepts_whole_window_as_chats_list_container(self) -> None:
        snapshots = _project_content_shell_without_list([
            nav.AXElementSnapshot(path="W.2.5", depth=2, role="AXButton", title="Real Looking Chat One", actions=("AXPress",), frame=(320, 160, 760, 65)),
            nav.AXElementSnapshot(path="W.2.6", depth=2, role="AXButton", title="Real Looking Chat Two", actions=("AXPress",), frame=(320, 232, 760, 65)),
        ])
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))
        self.assertEqual(result["status"], "project_chat_list_identity_not_confirmed")
        self.assertIn("no_forward_resolved_chats_list_container", result["identity_failure_reasons"])
        self.assertEqual(result["project_chat_list_container_path"], "")
        self.assertNotEqual(result["project_chat_list_container_path"], "W")

    def test_identity_gate_rejects_rows_outside_resolved_container(self) -> None:
        snapshots = _project_visible_chats_snapshots(partial_last_row=False) + [
            nav.AXElementSnapshot(path="W.2.30", depth=2, role="AXButton", title="Outside Container Row", actions=("AXPress",), frame=(320, 760, 760, 65)),
        ]
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))
        self.assertEqual(result["status"], "visible_chats_found")
        titles = [chat["title"] for chat in result["visible_chats"]]
        self.assertNotIn("Outside Container Row", titles)
        self.assertEqual(result["valid_project_chat_row_count"], 3)
        self.assertGreaterEqual(result["excluded_candidate_counts"].get("outside_forward_resolved_chats_list", 0), 1)

    def test_identity_gate_rejects_rows_below_minimum_height(self) -> None:
        page = _scrollable_project_chat_page(["Alpha Chat", "Beta Chat"])
        page.append(
            nav.AXElementSnapshot(path="W.1.4.3", depth=3, role="AXButton", description="Gamma Chat, preview must not drive matching", actions=("AXPress",), enabled=True, frame=(282, 340, 779, 30))
        )
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", page, (0, 0, 1200, 900))
        self.assertEqual(result["status"], "visible_chats_found")
        self.assertEqual(result["valid_project_chat_row_count"], 2)
        self.assertEqual(result["invalid_candidate_count"], 1)
        titles = [chat["title"] for chat in result["visible_chats"]]
        self.assertNotIn("Gamma Chat", titles)

    def test_identity_gate_accepts_historical_style_list_with_merged_accessibility(self) -> None:
        page = _scrollable_project_chat_page(["Content Moderation", "AWS Profile Photo Verification"])
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", page, (0, 0, 1200, 900))
        self.assertEqual(result["status"], "visible_chats_found")
        self.assertEqual(result["project_chat_list_identity"], "confirmed")
        self.assertEqual(result["project_chat_row_shape_status"], "valid")
        self.assertEqual(result["valid_project_chat_row_count"], 2)
        self.assertEqual(result["row_height_median"], 65.0)
        self.assertTrue(result["vertical_peer_list_confirmed"])
        self.assertEqual(result["project_chat_list_container_role"], "AXScrollArea")
        self.assertEqual(
            [chat["title"] for chat in result["visible_chats"]],
            ["Content Moderation", "AWS Profile Photo Verification"],
        )

    def test_identity_gate_requires_vertical_peer_list_geometry(self) -> None:
        page = _scrollable_project_chat_page([])
        page.extend([
            nav.AXElementSnapshot(path="W.1.4.1", depth=3, role="AXButton", description="Overlap A, preview must not drive matching", actions=("AXPress",), enabled=True, frame=(282, 200, 779, 65)),
            nav.AXElementSnapshot(path="W.1.4.2", depth=3, role="AXButton", description="Overlap B, preview must not drive matching", actions=("AXPress",), enabled=True, frame=(282, 210, 779, 65)),
        ])
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", page, (0, 0, 1200, 900))
        self.assertEqual(result["status"], "project_chat_list_identity_not_confirmed")
        self.assertFalse(result["vertical_peer_list_confirmed"])
        self.assertIn("vertical_peer_list_not_confirmed", result["identity_failure_reasons"])

    def test_identity_gate_not_confirmed_from_project_title_and_tab_labels_alone(self) -> None:
        snapshots = _project_content_shell_without_list()
        result = nav.resolve_open_project_content_and_visible_chats("PTG Assistant", snapshots, (0, 0, 1200, 900))
        self.assertTrue(result["project_identity_confirmed"])
        self.assertTrue(result["chats_tab_confirmed"])
        self.assertTrue(result["sources_tab_visible"])
        self.assertEqual(result["project_chat_list_identity"], "not_confirmed")
        self.assertEqual(result["status"], "project_chat_list_identity_not_confirmed")
        self.assertIn("no_forward_resolved_chats_list_container", result["identity_failure_reasons"])

    def test_identity_stability_fails_closed_on_transient_conversation_sample(self) -> None:
        good = _scrollable_project_chat_page(["Stable Chat"])
        transient = _scroll_opened_conversation_snapshots("Stable Chat")

        def sample(snaps: list[nav.AXElementSnapshot]) -> dict:
            return {"snapshots": snaps, "stats": {}, "window_metadata": {}, "ax_window_frame": (0, 0, 1200, 900)}

        result = nav._confirm_stable_project_chat_list_identity([sample(transient), sample(good)], "PTG Assistant")
        self.assertEqual(result["identity_stability_samples"], 2)
        self.assertEqual(result["status"], "project_chat_list_identity_not_confirmed")
        self.assertIn("list_identity_unstable_across_samples", result["identity_failure_reasons"])

        # Two compatible confirmed samples remain confirmed.
        stable = nav._confirm_stable_project_chat_list_identity([sample(good), sample(good)], "PTG Assistant")
        self.assertEqual(stable["status"], "visible_chats_found")
        self.assertEqual(stable["identity_stability_samples"], 2)

    def test_identity_not_confirmed_posts_no_scroll_axpress_or_click(self) -> None:
        composer_only = _project_content_shell_without_list([
            nav.AXElementSnapshot(path="W.2.9", depth=2, role="AXTextArea", title="Message ChatGPT", value="", frame=(340, 820, 620, 44)),
        ])
        reader = _AutonomousReader([composer_only] * 8, {"available": False, "path": "", "role": "", "title": ""})
        clicker = _ClickService()
        scroller = _ScrollService()

        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_project_chat(
                project_title="PTG Assistant",
                chat_title="Any Chat",
                confirm_open_chat=True,
                open_project_function=mock.Mock(return_value={"ok": True, "outcome": "destination_opened_and_visible_chats_resolved", "visible_chat_count": 1}),
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                scroll_service_factory=_ScrollFactory(scroller),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])),
                sleep_function=_SleepRecorder(),
            )

        self.assertEqual(result["outcome"], "project_chat_list_identity_not_confirmed")
        self.assertEqual(result["actions_performed"], [])
        self.assertEqual(clicker.clicks, [])
        self.assertEqual(scroller.scrolls, [])
        self.assertEqual(reader.actions, [])
        self.assertEqual(result["scroll_pulses_posted"], 0)

    def test_correct_project_list_still_flows_into_matching_and_scroll_paths(self) -> None:
        # Canonical and historical-style fixtures both confirm identity and expose
        # their rows so downstream target-matching / scroll-search is reachable.
        canonical = nav.resolve_open_project_content_and_visible_chats(
            "PTG Assistant", _project_visible_chats_snapshots(partial_last_row=False), (0, 0, 1200, 900)
        )
        self.assertEqual(canonical["status"], "visible_chats_found")
        self.assertEqual(canonical["project_chat_list_identity"], "confirmed")
        self.assertEqual(canonical["valid_project_chat_row_count"], 3)

        scrollable = nav.resolve_open_project_content_and_visible_chats(
            "PTG Assistant", _scrollable_project_chat_page(["City-wise Restrictions"]), (0, 0, 1200, 900)
        )
        self.assertEqual(scrollable["status"], "visible_chats_found")
        self.assertEqual(scrollable["project_chat_list_identity"], "confirmed")
        self.assertEqual([chat["title"] for chat in scrollable["visible_chats"]], ["City-wise Restrictions"])

    def test_visual_row_diagnostic_is_read_only_and_reports_rows_without_actions(self) -> None:
        reader = _ActionReader([_visual_row_diagnostic_project_chat_page()])
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.diagnose_chatgpt_project_chat_rows(
                project_title="PTG Assistant",
                process_resolver=lambda app_name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
            )

        self.assertEqual(result["status"], "diagnostic_ready")
        self.assertEqual(result["actions_performed"], [])
        self.assertEqual(reader.actions, [])
        self.assertGreater(result["summary"]["ax_nodes_inspected"], 0)
        source = Path(nav.__file__).read_text(encoding="utf-8")
        diagnostic_source = source[
            source.index("def diagnose_chatgpt_project_chat_rows("):
            source.index("def _base_project_chat_row_ax_audit_result")
        ]
        for token in ("activate_chatgpt", "perform_action", "AXPress)", "left_click", "scroll_down", "paste", "cursor", "screenshot", "ocr", "selenium", "playwright"):
            self.assertNotIn(token, diagnostic_source)

    def test_visual_row_diagnostic_fails_closed_when_identity_not_confirmed(self) -> None:
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _project_content_shell_without_list(),
            (0, 0, 1200, 900),
        )

        self.assertEqual(result["status"], "project_chat_list_identity_not_confirmed")
        self.assertEqual(result["final_outcome"], "project_chat_list_identity_not_confirmed")
        self.assertEqual(result["visual_row_bands"], [])
        self.assertEqual(result["summary"]["filtered_bands_printed"], 0)

    def test_visual_row_diagnostic_collects_title_bearing_roles_and_attributes(self) -> None:
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
        )

        self.assertEqual(result["status"], "diagnostic_ready")
        first_band = result["visual_row_bands"][0]
        roles = {candidate["source_role"] for candidate in first_band["title_candidates"]}
        self.assertTrue({"AXGroup", "AXRow", "AXCell", "AXButton", "AXLink", "AXStaticText"}.issubset(roles))
        attrs = {(candidate["source_attribute"], candidate["raw_text"]) for candidate in first_band["title_candidates"]}
        self.assertIn(("AXTitle", "Title Attr Candidate"), attrs)
        self.assertIn(("AXDescription", "Description Attr Candidate"), attrs)
        self.assertIn(("AXValue", "Value Attr Candidate"), attrs)

    def test_visual_row_diagnostic_reports_nested_small_wrapper_rejected_with_outer_band_evidence(self) -> None:
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
        )

        band = next(
            band
            for band in result["visual_row_bands"]
            if any(candidate["raw_text"] == "Nested Small Wrapper" for candidate in band["title_candidates"])
        )
        comparison = band["current_resolver_comparison"]
        self.assertEqual(comparison["current_resolver_status"], "rejected_currently")
        self.assertIn("row_container_frame_below_minimum", comparison["current_resolver_rejection_reasons"])
        self.assertEqual(band["outermost_candidate_path"], "W.1.4.3")
        self.assertEqual(band["outermost_candidate_role"], "AXGroup")

    def test_visual_row_diagnostic_distinguishes_accepted_rejected_and_not_seen_bands(self) -> None:
        snapshots = _visual_row_diagnostic_project_chat_page()
        snapshots.append(nav.AXElementSnapshot(path="W.1.4.4", depth=3, role="AXImage", description="Decorative Image Label", frame=(302, 430, 240, 44)))
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots("PTG Assistant", snapshots, (0, 0, 1200, 900))

        statuses = [band["current_resolver_comparison"]["current_resolver_status"] for band in result["visual_row_bands"]]
        self.assertIn("accepted_currently", statuses)
        self.assertIn("rejected_currently", statuses)
        self.assertIn("not_seen_by_current_resolver", statuses)
        self.assertGreaterEqual(result["summary"]["bands_accepted_by_current_resolver"], 2)
        self.assertGreaterEqual(result["summary"]["bands_rejected_by_current_resolver"], 1)
        self.assertGreaterEqual(result["summary"]["bands_not_seen_by_current_resolver"], 1)

    def test_visual_row_diagnostic_contains_title_filters_output_not_collection(self) -> None:
        full = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
        )
        filtered = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
            contains_title="Mock Data Insertion SQL",
        )

        self.assertEqual(filtered["collection_counts_before_filter"], full["collection_counts_before_filter"])
        self.assertLess(len(filtered["visual_row_bands"]), len(full["visual_row_bands"]))
        self.assertGreater(filtered["hidden_unrelated_band_count"], 0)
        self.assertEqual(filtered["summary"]["filtered_bands_printed"], 1)
        self.assertIn("Mock Data Insertion SQL", json.dumps(filtered["visual_row_bands"]))
        self.assertNotIn("Nested Small Wrapper", json.dumps(filtered["visual_row_bands"]))

    def test_visual_row_diagnostic_experimental_titles_do_not_change_strict_matching(self) -> None:
        result = nav.diagnose_chatgpt_project_chat_rows_from_snapshots(
            "PTG Assistant",
            _visual_row_diagnostic_project_chat_page(),
            (0, 0, 1200, 900),
        )

        titles = [(band["experimental_canonical"]["experimental_canonical_title"], band["current_resolver_comparison"]["current_resolver_title"]) for band in result["visual_row_bands"]]
        self.assertIn(("Title Attr Candidate", "Mock Data Insertion SQL"), titles)
        rejected = next(item for item in titles if item[0] == "Nested Small Wrapper")
        self.assertEqual(rejected[1], "")
        self.assertEqual(result["actions_performed"], [])

    def test_identity_outcome_registered_and_resolver_avoids_disallowed_channels(self) -> None:
        for outcome_set in (
            nav.AUTONOMOUS_OPEN_OUTCOMES,
            nav.PROJECT_VISIBLE_CHAT_INSPECTION_OUTCOMES,
            nav.PROJECT_CHAT_OPEN_OUTCOMES,
        ):
            self.assertIn("project_chat_list_identity_not_confirmed", outcome_set)
        source = Path(nav.__file__).read_text(encoding="utf-8")
        resolver_source = source[
            source.index("def _forward_resolve_project_chats_list_container"):
            source.index("def _project_text(")
        ]
        for token in ("CGEventKeyboard", "ScrollWheel", "press_enter", "paste_clipboard", "screenshot", "ocr", "osascript", "selenium", "playwright"):
            self.assertNotIn(token, resolver_source)


class ChatGPTNavigationDiagnosticCLITests(unittest.TestCase):
    def test_cli_coordinate_calibration_dispatches_read_only_command(self) -> None:
        result = nav._base_coordinate_calibration_result("project", "PTG Assistant", "ChatGPT")
        result.update(
            {
                "ok": True,
                "status": "calibration_completed",
                "final_mapping_classification": "ax_frames_are_global",
                "recommended_future_click_transform": "raw",
                "recommended_runtime_click_transform": "raw",
                "hit_test_relationship_to_requested_target": "exact_target_title",
            }
        )
        stdout = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "calibrate-chatgpt-sidebar-coordinate-mapping",
                    "--kind",
                    "project",
                    "--title",
                    "PTG Assistant",
                ],
            ),
            mock.patch.object(cli, "calibrate_chatgpt_sidebar_coordinate_mapping", return_value=result) as calibrate,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        calibrate.assert_called_once_with(
            app_name="ChatGPT",
            kind="project",
            title="PTG Assistant",
            confirm_calibration_click=False,
            max_depth=16,
            max_nodes=900,
            before_click_callback=cli._coordinate_calibration_click_notice,
        )
        self.assertTrue(stdout.getvalue().rstrip().endswith("recommended_runtime_click_transform: raw"))

    def test_coordinate_calibration_click_notice_text_is_exact(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            cli._coordinate_calibration_click_notice()

        self.assertEqual(stdout.getvalue(), "Explicit coordinate-calibration click authorized.\n")

    def test_cli_success_exits_zero_and_prints_read_only_report(self) -> None:
        result = nav._base_result("ChatGPT", 16, 900)
        result.update({"ok": True, "reason_code": "inspection_completed", "pid_present": True, "window_available": True})
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "inspect-chatgpt-navigation-ui"]),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui", return_value=result) as inspect,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        inspect.assert_called_once_with(
            app_name="ChatGPT",
            max_depth=16,
            max_nodes=900,
            include_visible_navigation_titles=False,
        )
        output = stdout.getvalue()
        self.assertIn("No app activation, focus change, clipboard, typing, paste, click, keypress, ledger write, or UI action was performed.", output)
        self.assertIn("current-chat identity:", output)
        self.assertIn("filtering_summary:", output)
        self.assertNotIn("json_details:", output)

    def test_cli_json_details_are_explicit_opt_in(self) -> None:
        result = nav._base_result("ChatGPT", 16, 900)
        result.update({"ok": True, "reason_code": "inspection_completed", "pid_present": True, "window_available": True})
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "inspect-chatgpt-navigation-ui", "--include-json-details"]),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui", return_value=result),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        output = stdout.getvalue()
        self.assertIn("json_details:", output)
        self.assertIn('"read_only": true', output)

    def test_cli_failure_exits_one(self) -> None:
        result = nav._base_result("ChatGPT", 16, 900)
        result.update({"reason_code": "process_not_found", "error": "not running"})

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "inspect-chatgpt-navigation-ui"]),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui", return_value=result),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)

    def test_cli_invalid_arguments_exit_two_before_inspection(self) -> None:
        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "inspect-chatgpt-navigation-ui", "--max-nodes", "0"]),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui") as inspect,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        inspect.assert_not_called()

    def test_cli_passes_exact_ascii_visible_title_flag(self) -> None:
        result = nav._base_result("ChatGPT", 16, 900, include_visible_navigation_titles=True)
        result.update({"ok": True, "reason_code": "inspection_completed", "pid_present": True, "window_available": True})

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "inspect-chatgpt-navigation-ui", "--include-visible-navigation-titles"],
            ),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui", return_value=result) as inspect,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        inspect.assert_called_once_with(
            app_name="ChatGPT",
            max_depth=16,
            max_nodes=900,
            include_visible_navigation_titles=True,
        )

    def test_compact_title_inventory_prints_disclosed_titles_without_json(self) -> None:
        result = _title_inventory_result()
        stdout = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "inspect-chatgpt-navigation-ui", "--include-visible-navigation-titles"],
            ),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui", return_value=result),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        output = stdout.getvalue()
        self.assertIn(nav.TITLE_DISCLOSURE_NOTICE, output)
        self.assertIn("title='Chat Alpha'", output)
        self.assertIn("title='Project Beta'", output)
        self.assertIn("ancestor_path=", output)
        self.assertIn("list_path=W.1", output)
        self.assertIn("capability=ambiguous", output)
        self.assertNotIn("json_details:", output)

    def test_include_json_details_can_be_combined_with_title_inventory(self) -> None:
        result = _title_inventory_result()
        stdout = io.StringIO()

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "inspect-chatgpt-navigation-ui",
                    "--include-visible-navigation-titles",
                    "--include-json-details",
                ],
            ),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui", return_value=result),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        output = stdout.getvalue()
        self.assertIn("title='Chat Alpha'", output)
        self.assertIn("json_details:", output)
        self.assertIn('"exact_title": "Chat Alpha"', output)

    def test_compact_output_guard_bounds_many_title_candidates(self) -> None:
        result = _title_inventory_result()
        candidates = [
            _title_inventory_candidate(f"Project {index:02d}", f"W.2.{index}", "visible_project_title_candidate")
            for index in range(30)
        ]
        result["visible_project_title_candidates"] = candidates
        result["visible_title_category_limits"]["visible_project_title_candidates"].update(
            {"total": 30, "emitted": 30, "omitted": 0}
        )

        output = "\n".join(cli._inspect_chatgpt_navigation_ui_result_lines(result))

        self.assertLessEqual(len(output), cli.CHATGPT_NAVIGATION_COMPACT_OUTPUT_CHAR_GUARD + 512)
        self.assertIn("visible project title candidates: 30 total", output)
        self.assertIn("omitted", output)

    def test_output_guard_preserves_title_priority_order(self) -> None:
        result = _title_inventory_result()
        result["visible_project_title_candidates"] = [
            _title_inventory_candidate(f"Project {index:02d}", f"W.2.{index}", "visible_project_title_candidate")
            for index in range(10)
        ]
        result["visible_search_result_candidates"] = [
            _title_inventory_candidate("Search Later", "W.3.1", "visible_search_result_candidate")
        ]
        result["visible_navigation_section_labels"] = [
            _title_inventory_candidate("History", "W.4.1", "visible_navigation_section_label")
        ]
        for key in (
            "visible_project_title_candidates",
            "visible_search_result_candidates",
            "visible_navigation_section_labels",
        ):
            result["visible_title_category_limits"][key].update(
                {"total": len(result[key]), "emitted": len(result[key]), "omitted": 0}
            )

        with mock.patch.object(cli, "CHATGPT_NAVIGATION_COMPACT_OUTPUT_CHAR_GUARD", 9_000):
            output = "\n".join(cli._inspect_chatgpt_navigation_ui_result_lines(result))

        self.assertLess(output.index("visible chat title candidates"), output.index("visible project title candidates"))
        self.assertLess(output.index("visible project title candidates"), output.index("visible search result candidates"))
        self.assertLess(output.index("visible search result candidates"), output.index("actionable parent candidates"))
        self.assertLess(output.index("actionable parent candidates"), output.index("visible navigation section labels"))
        self.assertIn("title='Chat Alpha'", output)

    def test_cli_rejects_unicode_dash_visible_title_flag(self) -> None:
        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "inspect-chatgpt-navigation-ui", "–include-visible-navigation-titles"],
            ),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui") as inspect,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        inspect.assert_not_called()

    def test_cli_rejects_unicode_dash_json_details_flag(self) -> None:
        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "inspect-chatgpt-navigation-ui", "–include-json-details"],
            ),
            mock.patch.object(cli, "inspect_chatgpt_navigation_ui") as inspect,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        inspect.assert_not_called()

    def test_cli_help_exits_zero_and_mentions_navigation_command(self) -> None:
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "inspect-chatgpt-navigation-ui", "--help"]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Read-only structural diagnostic", stdout.getvalue())
        self.assertIn("--include-visible-navigation-titles", stdout.getvalue())
        self.assertIn("--include-json-details", stdout.getvalue())

    def test_verify_destination_cli_invokes_explicit_command_and_prints_notice(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": True,
            "status": "verified_destination_changed",
            "app_name": "ChatGPT",
            "kind": "project",
            "title": "PTG Assistant",
            "pid_present": True,
            "process_resolution_method": "fake",
            "target": {
                "title_ax_path": "W.1.3.1",
                "resolved_target_ax_path": "W.1.3",
                "resolution_method": "row_press_target",
                "enabled_state": True,
                "available_action_names": ["AXPress"],
            },
            "pre_action_snapshot": {"requested_title_visible": True, "requested_title_selected": False},
            "post_action_snapshot": {"requested_title_visible": True, "requested_title_selected": True},
            "actions_performed": [{"path": "W.1.3", "action": "AXPress"}],
            "error": "",
        }

        def fake_verify(**kwargs: object) -> dict:
            callback = kwargs["before_action_callback"]
            callback(kwargs["kind"], kwargs["title"])
            return result

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "verify-chatgpt-sidebar-destination",
                    "--kind",
                    "project",
                    "--title",
                    "PTG Assistant",
                ],
            ),
            mock.patch.object(cli, "verify_chatgpt_sidebar_destination", side_effect=fake_verify) as verify,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        verify.assert_called_once()
        output = stdout.getvalue()
        self.assertIn('Explicit sidebar destination verification authorized for: project "PTG Assistant".', output)
        self.assertIn("status: verified_destination_changed", output)
        self.assertIn("target_resolution_method: row_press_target", output)

    def test_verify_destination_cli_rejects_empty_title_before_calling_verifier(self) -> None:
        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "verify-chatgpt-sidebar-destination", "--kind", "chat", "--title", "   "],
            ),
            mock.patch.object(cli, "verify_chatgpt_sidebar_destination") as verify,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        verify.assert_not_called()

    def test_verify_destination_help_distinguishes_explicit_action(self) -> None:
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "verify-chatgpt-sidebar-destination", "--help"]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Explicit one-destination UI action", stdout.getvalue())
        self.assertIn("--kind", stdout.getvalue())
        self.assertIn("--title", stdout.getvalue())

    def test_inspect_sidebar_destination_cli_invokes_read_only_command(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": True,
            "read_only": True,
            "status": "menu_only_target",
            "app_name": "ChatGPT",
            "kind": "chat",
            "title": "Markdown Formatting Guide",
            "pid_present": True,
            "process_resolution_method": "fake",
            "target": {
                "title_ax_path": "W.1.7",
                "computed_row_ax_path": "W.1.7",
                "current_resolution_method": "menu_only_target",
            },
            "scope": {
                "retained_element_count": 1,
                "row_descendant_count": 0,
                "sibling_count": 1,
                "related_count": 0,
            },
            "primary_selection_assessment": {
                "classification": "menu_only_target",
                "viable_candidate_controls": [],
            },
            "frame_evidence": {
                "title_node": {
                    "path": "W.1.7",
                    "frame": {
                        "x": 10,
                        "y": 248,
                        "width": 250,
                        "height": 32,
                        "valid": True,
                        "fully_inside_window": True,
                        "inside_sidebar_or_list": True,
                        "large_enough_for_safe_interior_click": True,
                    },
                },
                "computed_row_node": {
                    "path": "W.1.7",
                    "frame": {
                        "x": 10,
                        "y": 248,
                        "width": 250,
                        "height": 32,
                        "valid": True,
                        "fully_inside_window": True,
                        "inside_sidebar_or_list": True,
                        "large_enough_for_safe_interior_click": True,
                    },
                },
                "nearest_visible_ancestor_with_usable_frame": {
                    "path": "W.1",
                    "frame": {"x": 0, "y": 0, "width": 280, "height": 900, "valid": True},
                },
                "sidebar_or_list": {
                    "path": "W.1",
                    "frame": {"x": 0, "y": 0, "width": 280, "height": 900, "valid": True},
                },
                "focused_window": {
                    "path": "W",
                    "frame": {"x": 0, "y": 0, "width": 1200, "height": 900, "valid": True},
                },
                "chosen_click_source": {"source_path": "W.1.7"},
                "computed_safe_click_point": {"x": 83.34, "y": 264.0, "ok": True},
            },
            "elements": [],
            "error": "",
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "inspect-chatgpt-sidebar-destination",
                    "--kind",
                    "chat",
                    "--title",
                    "Markdown Formatting Guide",
                ],
            ),
            mock.patch.object(cli, "inspect_chatgpt_sidebar_destination", return_value=result) as inspect,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        inspect.assert_called_once_with(
            app_name="ChatGPT",
            kind="chat",
            title="Markdown Formatting Guide",
            max_depth=16,
            max_nodes=900,
        )
        output = stdout.getvalue()
        self.assertIn("ChatGPT sidebar destination deep inspection", output)
        self.assertIn("frame_computed_row: path=W.1.7", output)
        self.assertIn("computed_safe_click_point: x=83.34 y=264.0 ok=True", output)
        self.assertIn("No app activation, focus change, selection change, menu opening", output)
        self.assertNotIn("json_details:", output)

    def test_inspect_sidebar_destination_cli_rejects_empty_title_before_calling_inspector(self) -> None:
        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "inspect-chatgpt-sidebar-destination", "--kind", "project", "--title", "   "],
            ),
            mock.patch.object(cli, "inspect_chatgpt_sidebar_destination") as inspect,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        inspect.assert_not_called()

    def test_inspect_sidebar_destination_help_mentions_read_only_and_json_details(self) -> None:
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "inspect-chatgpt-sidebar-destination", "--help"]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Read-only deep Accessibility inspection", stdout.getvalue())
        self.assertIn("--include-json-details", stdout.getvalue())

    def test_frame_click_cli_dry_run_invokes_verifier_without_confirmation(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": True,
            "status": "dry_run_ready",
            "confirm_frame_click": False,
            "app_name": "ChatGPT",
            "kind": "project",
            "title": "PTG Assistant",
            "pid_present": True,
            "process_resolution_method": "fake",
            "target": {
                "title_ax_path": "W.1.3.1",
                "computed_row_ax_path": "W.1.3",
                "source_frame_path": "W.1.3",
                "source_frame_relation": "computed_row_node",
            },
            "frame_safety": {
                "source_frame": {
                    "x": 10,
                    "y": 112,
                    "width": 250,
                    "height": 32,
                    "valid": True,
                    "fully_inside_window": True,
                    "inside_sidebar_or_list": True,
                    "large_enough_for_safe_interior_click": True,
                },
                "safety_checks_passed": True,
                "why_click_point_avoids_overflow_region": "safe point is left of the excluded overflow/menu zone.",
            },
            "click_point": {
                "x": 83.34,
                "y": 128.0,
                "ok": True,
                "policy": "left/center-left row interior",
                "overflow_exclusion_zone": {"x": 216, "y": 112, "width": 44, "height": 32},
            },
            "post_click_evidence": {},
            "actions_performed": [],
            "error": "",
        }

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "verify-chatgpt-sidebar-frame-click",
                    "--kind",
                    "project",
                    "--title",
                    "PTG Assistant",
                ],
            ),
            mock.patch.object(cli, "verify_chatgpt_sidebar_frame_click", return_value=result) as verify,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        verify.assert_called_once()
        self.assertFalse(verify.call_args.kwargs["confirm_frame_click"])
        output = stdout.getvalue()
        self.assertIn("dry_run: true", output)
        self.assertIn("actions_performed: []", output)
        self.assertNotIn("Explicit frame-click verification authorized", output)

    def test_frame_click_cli_confirmation_prints_authorization_notice(self) -> None:
        stdout = io.StringIO()
        result = {
            "ok": False,
            "status": "click_performed_no_observable_change",
            "confirm_frame_click": True,
            "app_name": "ChatGPT",
            "kind": "chat",
            "title": "Markdown Formatting Guide",
            "pid_present": True,
            "process_resolution_method": "fake",
            "target": {"title_ax_path": "W.1.7", "computed_row_ax_path": "W.1.7", "source_frame_path": "W.1.7"},
            "frame_safety": {"source_frame": {}, "safety_checks_passed": True},
            "click_point": {"x": 83.34, "y": 264.0, "ok": True},
            "post_click_evidence": {"status": "click_performed_no_observable_change"},
            "actions_performed": [{"event": "left_mouse_down"}, {"event": "left_mouse_up"}],
            "error": "",
        }

        def fake_frame_verify(**kwargs: object) -> dict:
            callback = kwargs["before_click_callback"]
            callback(kwargs["kind"], kwargs["title"])
            return result

        with (
            mock.patch.object(
                cli.sys,
                "argv",
                [
                    "agent-loop",
                    "verify-chatgpt-sidebar-frame-click",
                    "--kind",
                    "chat",
                    "--title",
                    "Markdown Formatting Guide",
                    "--confirm-frame-click",
                ],
            ),
            mock.patch.object(cli, "verify_chatgpt_sidebar_frame_click", side_effect=fake_frame_verify) as verify,
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 1)
        verify.assert_called_once()
        self.assertTrue(verify.call_args.kwargs["confirm_frame_click"])
        output = stdout.getvalue()
        self.assertIn('Explicit frame-click verification authorized for: chat "Markdown Formatting Guide".', output)
        self.assertIn("status: click_performed_no_observable_change", output)

    def test_frame_click_cli_rejects_empty_title_before_calling_verifier(self) -> None:
        with (
            mock.patch.object(
                cli.sys,
                "argv",
                ["agent-loop", "verify-chatgpt-sidebar-frame-click", "--kind", "chat", "--title", "   "],
            ),
            mock.patch.object(cli, "verify_chatgpt_sidebar_frame_click") as verify,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 2)
        verify.assert_not_called()

    def test_frame_click_help_mentions_dry_run_and_confirmation_gate(self) -> None:
        stdout = io.StringIO()

        with (
            mock.patch.object(cli.sys, "argv", ["agent-loop", "verify-chatgpt-sidebar-frame-click", "--help"]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("Dry-run by default", help_text)
        self.assertIn("--confirm-frame-click", help_text)


if __name__ == "__main__":
    unittest.main()
