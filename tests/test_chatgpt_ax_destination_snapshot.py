from __future__ import annotations

import inspect
import pathlib
import unittest
from dataclasses import dataclass

import agent.chatgpt_ax_destination_snapshot as ax_snapshot_module
from agent.chatgpt_ax_destination_snapshot import (
    AXDestinationNode,
    AXDestinationObservation,
    ChatGPTAXDestinationSnapshotAdapter,
)
from agent.chatgpt_destination_gate import (
    DestinationLeaseContext,
    verify_chatgpt_destination_snapshot,
)


PROJECT_TITLE = "Bound Project"
CHAT_TITLE = "Bound Chat"


class ChatGPTAXDestinationSnapshotAdapterTests(unittest.TestCase):
    def test_public_adapter_api_exposes_only_read_snapshot_method(self) -> None:
        public_methods = _public_methods(ChatGPTAXDestinationSnapshotAdapter)

        self.assertEqual(public_methods, ("read_destination_snapshot",))

    def test_adapter_source_has_no_mutating_api_dependencies_or_timing_terms(self) -> None:
        source = pathlib.Path("agent/chatgpt_ax_destination_snapshot.py").read_text()

        forbidden = (
            "AXUIElementPerformAction",
            "AXSetFocus",
            "CGEvent",
            "activate_chatgpt",
            "activateApplication",
            "paste_clipboard",
            "copy_to_clipboard",
            "press_enter",
            "scroll_down",
            "set_frontmost",
            "pyperclip",
            "clipboard",
            "deadline",
            "elapsed",
            "time.sleep",
            "import time",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_behavior_path_uses_only_injected_read_query(self) -> None:
        query = _FakeQuery([_complete_observation()])

        result = ChatGPTAXDestinationSnapshotAdapter(
            query,
            stability_reads=1,
        ).read_destination_snapshot()

        self.assertTrue(result.process_running)
        self.assertEqual(query.calls, [("ChatGPT", 16, 900)])
        self.assertEqual(query.mutation_calls, [])

    def test_active_conversation_identity_read_from_window_title_item(self) -> None:
        # ChatGPT Desktop exposes the open conversation as a window toolbar item
        # labelled "<chat>, <project>". The adapter must surface both titles.
        nodes = (
            AXDestinationNode(path="W", depth=0, role="AXWindow", title="ChatGPT"),
            AXDestinationNode(
                path="W.2.1.3",
                depth=3,
                role="AXButton",
                description=f"{CHAT_TITLE}, {PROJECT_TITLE}",
                actions=("AXPress", "Name:Remove from toolbar", "Name:Move next"),
                frame=(20.0, 10.0, 320.0, 30.0),
            ),
        )

        snap = ax_snapshot_module._snapshot_from_observation(
            _complete_observation(nodes=nodes)
        )

        self.assertEqual(snap.active_conversation_chat_title, CHAT_TITLE)
        self.assertEqual(snap.active_conversation_project_title, PROJECT_TITLE)

    def test_active_conversation_identity_ignores_non_titlebar_comma_labels(self) -> None:
        # A comma-bearing label that is not a window toolbar/title item (e.g. a
        # mid-content static text) must not be mistaken for the active identity.
        nodes = (
            AXDestinationNode(path="W", depth=0, role="AXWindow", title="ChatGPT"),
            AXDestinationNode(
                path="W.1.5",
                depth=2,
                role="AXStaticText",
                description=f"{CHAT_TITLE}, {PROJECT_TITLE}",
                frame=(20.0, 300.0, 200.0, 20.0),
            ),
        )

        snap = ax_snapshot_module._snapshot_from_observation(
            _complete_observation(nodes=nodes)
        )

        self.assertEqual(snap.active_conversation_chat_title, "")
        self.assertEqual(snap.active_conversation_project_title, "")

    def test_active_conversation_identity_ignores_offscreen_transcript_before_toolbar_title(self) -> None:
        nodes = (
            AXDestinationNode(path="W", depth=0, role="AXWindow", title="ChatGPT"),
            AXDestinationNode(
                path="W.1.1.3.1.1.1.2.1.1",
                depth=9,
                role="AXStaticText",
                description="Old transcript text, not a project identity",
                frame=(291.0, -1985.0, 586.0, 95.0),
            ),
            AXDestinationNode(
                path="W.2.1.3",
                depth=3,
                role="AXButton",
                description=f"{CHAT_TITLE}, {PROJECT_TITLE}",
                actions=("AXPress", "Name:Remove from toolbar", "Name:Move next"),
                frame=(262.0, -52.0, 215.0, 52.0),
            ),
        )

        snap = ax_snapshot_module._snapshot_from_observation(
            _complete_observation(nodes=nodes)
        )

        self.assertEqual(snap.active_conversation_chat_title, CHAT_TITLE)
        self.assertEqual(snap.active_conversation_project_title, PROJECT_TITLE)

    def test_focused_window_is_usable_when_windows_list_is_empty(self) -> None:
        reader = object.__new__(ax_snapshot_module._ReadOnlyAXTreeReader)
        reader._copy_attribute = lambda _app, name: 42 if name == "AXFocusedWindow" else ()
        reader._array_values = lambda value: tuple(value or ())

        self.assertEqual(reader._window(1), 42)

    def test_process_window_and_accessibility_unavailable_map_conservatively(self) -> None:
        cases = (
            (
                AXDestinationObservation(
                    process_running=False,
                    window_available=False,
                    accessibility_available=True,
                ),
                "chatgpt_not_running",
            ),
            (
                AXDestinationObservation(
                    process_running=True,
                    window_available=False,
                    accessibility_available=True,
                ),
                "chatgpt_window_unavailable",
            ),
            (
                AXDestinationObservation(
                    process_running=True,
                    window_available=False,
                    accessibility_available=False,
                ),
                "accessibility_unavailable",
            ),
            (
                AXDestinationObservation(
                    process_running=True,
                    window_available=True,
                    accessibility_available=False,
                ),
                "accessibility_unavailable",
            ),
        )
        for observation, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                snapshot = _adapter_snapshot(observation)
                result = _verify(snapshot)

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason_code)

    def test_traversal_truncation_maps_to_gate_truncation(self) -> None:
        snapshot = _adapter_snapshot(
            _complete_observation(truncated_by_node_limit=True),
        )

        result = _verify(snapshot)

        self.assertTrue(snapshot.ax_tree_truncated)
        self.assertFalse(snapshot.snapshot_complete)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")

    def test_ambiguous_project_and_chat_evidence_stays_ambiguous(self) -> None:
        snapshot = _adapter_snapshot(
            _complete_observation(
                nodes=(
                    *_base_structure(),
                    _project_node(PROJECT_TITLE, path="W.1.4"),
                    _project_node(PROJECT_TITLE, path="W.1.5"),
                    _chat_row(CHAT_TITLE, path="W.2.10"),
                    _chat_row(CHAT_TITLE, path="W.2.11"),
                    _header(CHAT_TITLE),
                )
            )
        )

        project_result = _verify(snapshot)
        self.assertEqual(project_result.reason_code, "project_title_ambiguous")
        self.assertEqual(len(snapshot.active_project_candidates), 2)
        self.assertEqual(len(snapshot.selected_chat_row_candidates), 2)

    def test_missing_actionable_active_evidence_is_not_upgraded_to_success(self) -> None:
        snapshot = _adapter_snapshot(
            _complete_observation(
                nodes=(
                    *_base_structure(),
                    AXDestinationNode(
                        path="W.1.4",
                        depth=2,
                        role="AXStaticText",
                        title=PROJECT_TITLE,
                        description="project",
                    ),
                    AXDestinationNode(
                        path="W.2.10",
                        depth=3,
                        role="AXStaticText",
                        title=CHAT_TITLE,
                        description="chat row",
                    ),
                    _header(CHAT_TITLE),
                )
            )
        )

        result = _verify(snapshot)

        self.assertFalse(result.ok)
        self.assertIn(
            result.reason_code,
            {"visible_title_not_actionable_destination", "chat_not_active"},
        )

    def test_complete_fake_snapshot_maps_to_gate_success(self) -> None:
        snapshot = _adapter_snapshot(_complete_observation())

        result = _verify(snapshot)

        self.assertTrue(result.ok)
        self.assertTrue(snapshot.composer_available)
        self.assertTrue(snapshot.transcript_available)
        self.assertTrue(snapshot.conversation_surface_available)
        self.assertEqual(
            tuple(candidate.title for candidate in snapshot.active_project_candidates),
            (PROJECT_TITLE,),
        )
        self.assertEqual(
            tuple(candidate.title for candidate in snapshot.selected_chat_row_candidates),
            (CHAT_TITLE,),
        )
        self.assertEqual(
            tuple(candidate.title for candidate in snapshot.conversation_header_candidates),
            (CHAT_TITLE,),
        )

    def test_unstable_fake_observations_map_to_unstable_evidence(self) -> None:
        first = _complete_observation()
        second = _complete_observation(nodes=(*_complete_nodes(chat_title="Other Chat"),))
        query = _FakeQuery([first, second])

        snapshot = ChatGPTAXDestinationSnapshotAdapter(
            query,
            stability_reads=2,
        ).read_destination_snapshot()
        result = _verify(snapshot)

        self.assertEqual(len(query.calls), 2)
        self.assertFalse(snapshot.snapshot_stable)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")

    def test_live_shaped_project_mapping_does_not_upgrade_toolbar_chat(self) -> None:
        snapshot = _adapter_snapshot(
            _complete_observation(nodes=_observed_live_nodes()),
        )
        result = _verify(snapshot)

        self.assertEqual(
            tuple(candidate.title for candidate in snapshot.active_project_candidates),
            (PROJECT_TITLE,),
        )
        self.assertEqual(snapshot.selected_chat_row_candidates, ())
        self.assertEqual(snapshot.conversation_header_candidates, ())
        self.assertEqual(snapshot.composer_candidate_count, 2)
        self.assertEqual(snapshot.conversation_surface_candidate_count, 4)
        self.assertFalse(snapshot.composer_available)
        self.assertFalse(snapshot.conversation_surface_available)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_not_active")

    def test_harmless_volatile_metadata_and_ordering_do_not_break_stability(self) -> None:
        first = _complete_observation(
            nodes=(
                *_complete_nodes(),
                AXDestinationNode(
                    path="W.9",
                    depth=1,
                    role="AXImage",
                    title="volatile counter 1",
                    identifier="noise-1",
                ),
            )
        )
        second = _complete_observation(
            nodes=(
                AXDestinationNode(
                    path="W.9",
                    depth=1,
                    role="AXImage",
                    title="volatile counter 2",
                    identifier="noise-2",
                ),
                *_complete_nodes(),
            )
        )

        snapshot = ChatGPTAXDestinationSnapshotAdapter(
            _FakeQuery([first, second]),
            stability_reads=2,
        ).read_destination_snapshot()

        self.assertTrue(snapshot.snapshot_stable)

    def test_meaningful_identity_change_still_breaks_stability(self) -> None:
        first = _complete_observation(nodes=_observed_live_nodes())
        second = _complete_observation(
            nodes=_observed_live_nodes(project_title="Other Project")
        )

        snapshot = ChatGPTAXDestinationSnapshotAdapter(
            _FakeQuery([first, second]),
            stability_reads=2,
        ).read_destination_snapshot()
        result = _verify(snapshot)

        self.assertFalse(snapshot.snapshot_stable)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")

    def test_ambiguous_structural_composer_and_surface_candidates_fail_closed(self) -> None:
        snapshot = _adapter_snapshot(
            _complete_observation(
                nodes=(
                    *_complete_nodes(),
                    AXDestinationNode(
                        path="W.4.2",
                        depth=2,
                        role="AXTextField",
                        enabled=True,
                        frame=(760, 760, 320, 44),
                    ),
                    AXDestinationNode(
                        path="W.3.2",
                        depth=2,
                        role="AXGroup",
                        direct_child_count=3,
                        frame=(620, 220, 620, 420),
                    ),
                )
            )
        )
        result = _verify(snapshot)

        self.assertEqual(snapshot.composer_candidate_count, 2)
        self.assertEqual(snapshot.conversation_surface_candidate_count, 2)
        self.assertFalse(snapshot.composer_available)
        self.assertFalse(snapshot.conversation_surface_available)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_no_elapsed_time_deadline_is_introduced(self) -> None:
        signature = inspect.signature(ChatGPTAXDestinationSnapshotAdapter)
        parameter_text = " ".join(signature.parameters)

        for token in ("timeout", "deadline", "elapsed"):
            self.assertNotIn(token, parameter_text)


def _adapter_snapshot(observation: AXDestinationObservation):
    return ChatGPTAXDestinationSnapshotAdapter(
        _FakeQuery([observation]),
        stability_reads=1,
    ).read_destination_snapshot()


def _verify(snapshot):
    return verify_chatgpt_destination_snapshot(
        run_id="run-1",
        binding=_Binding(PROJECT_TITLE, CHAT_TITLE),
        lease_context=DestinationLeaseContext(
            owning_run_id="run-1",
            lease_token="lease-token",
            owner_pid=123,
            acquired_at="2026-01-01T00:00:00+00:00",
        ),
        adapter=_SnapshotAdapter(snapshot),
    )


def _complete_observation(**overrides) -> AXDestinationObservation:
    values = {
        "process_running": True,
        "window_available": True,
        "accessibility_available": True,
        "nodes": _complete_nodes(),
        "traversal_failed": False,
        "truncated_by_node_limit": False,
        "truncated_by_depth_limit": False,
    }
    values.update(overrides)
    return AXDestinationObservation(**values)


def _complete_nodes(
    *,
    project_title: str = PROJECT_TITLE,
    chat_title: str = CHAT_TITLE,
) -> tuple[AXDestinationNode, ...]:
    return (
        *_base_structure(),
        _project_node(project_title),
        _chat_row(chat_title),
        _header(chat_title),
    )


def _base_structure() -> tuple[AXDestinationNode, ...]:
    return (
        AXDestinationNode(path="W", depth=0, role="AXWindow", title="ChatGPT"),
        AXDestinationNode(
            path="W.2.1",
            depth=2,
            role="AXButton",
            title="Chats",
            actions=("default",),
        ),
        AXDestinationNode(
            path="W.2.2",
            depth=2,
            role="AXButton",
            title="Sources",
            actions=("default",),
        ),
        AXDestinationNode(
            path="W.2.3",
            depth=2,
            role="AXList",
            description="project chat conversation list",
            direct_child_count=1,
        ),
        AXDestinationNode(
            path="W.3",
            depth=1,
            role="AXScrollArea",
            description="conversation messages transcript",
        ),
        AXDestinationNode(
            path="W.4",
            depth=1,
            role="AXTextArea",
            title="Message ChatGPT",
        ),
    )


def _project_node(title: str, *, path: str = "W.1.4") -> AXDestinationNode:
    return AXDestinationNode(
        path=path,
        depth=2,
        role="AXButton",
        title=title,
        description="selected project",
        selected=True,
        enabled=True,
        actions=("default",),
    )


def _chat_row(title: str, *, path: str = "W.2.10") -> AXDestinationNode:
    return AXDestinationNode(
        path=path,
        depth=3,
        role="AXButton",
        title=title,
        description="selected chat row",
        selected=True,
        enabled=True,
        actions=("default",),
    )


def _header(title: str) -> AXDestinationNode:
    return AXDestinationNode(
        path="W.3.1",
        depth=2,
        role="AXHeading",
        title=title,
        description="current conversation title",
    )


def _observed_live_nodes(
    *,
    project_title: str = PROJECT_TITLE,
    chat_title: str = CHAT_TITLE,
) -> tuple[AXDestinationNode, ...]:
    return (
        AXDestinationNode(
            path="W",
            depth=0,
            role="AXWindow",
            title="ChatGPT",
            frame=(0, 0, 1280, 860),
        ),
        AXDestinationNode(
            path="W.1",
            depth=1,
            role="AXGroup",
            identifier="sidebar",
            frame=(0, 0, 250, 860),
        ),
        AXDestinationNode(
            path="W.1.1",
            depth=2,
            role="AXButton",
            title="Chats",
            actions=("default",),
            frame=(20, 82, 86, 32),
        ),
        AXDestinationNode(
            path="W.1.2",
            depth=2,
            role="AXButton",
            title="Sources",
            actions=("default",),
            frame=(112, 82, 96, 32),
        ),
        AXDestinationNode(
            path="W.1.3",
            depth=2,
            role="AXList",
            description="project chat conversation list",
            direct_child_count=1,
            frame=(12, 130, 228, 600),
        ),
        AXDestinationNode(
            path="W.1.4",
            depth=2,
            role="AXButton",
            description=project_title,
            enabled=True,
            actions=("default",),
            frame=(18, 190, 210, 36),
        ),
        AXDestinationNode(
            path="W.2",
            depth=1,
            role="AXGroup",
            identifier="toolbar",
            frame=(260, 0, 1020, 92),
        ),
        AXDestinationNode(
            path="W.2.1",
            depth=2,
            role="AXButton",
            description=chat_title,
            role_description="button",
            enabled=True,
            actions=("default",),
            frame=(520, 24, 280, 44),
        ),
        AXDestinationNode(
            path="W.3.1",
            depth=2,
            role="AXStaticText",
            value=project_title,
            frame=(540, 126, 320, 32),
        ),
        AXDestinationNode(
            path="W.3.2",
            depth=2,
            role="AXScrollArea",
            direct_child_count=4,
            frame=(300, 160, 920, 380),
        ),
        AXDestinationNode(
            path="W.3.3",
            depth=2,
            role="AXGroup",
            direct_child_count=3,
            frame=(315, 180, 890, 330),
        ),
        AXDestinationNode(
            path="W.3.4",
            depth=2,
            role="AXWebArea",
            direct_child_count=2,
            frame=(300, 170, 910, 350),
        ),
        AXDestinationNode(
            path="W.3.5",
            depth=2,
            role="AXGroup",
            identifier="content-region",
            direct_child_count=5,
            frame=(300, 155, 920, 410),
        ),
        AXDestinationNode(
            path="W.4.1",
            depth=2,
            role="AXTextArea",
            enabled=True,
            frame=(390, 720, 620, 50),
        ),
        AXDestinationNode(
            path="W.4.2",
            depth=2,
            role="AXTextField",
            enabled=True,
            frame=(390, 778, 620, 44),
        ),
    )


def _public_methods(cls: type) -> tuple[str, ...]:
    return tuple(
        name
        for name, value in cls.__dict__.items()
        if not name.startswith("_") and callable(value)
    )


@dataclass(frozen=True)
class _Binding:
    project_title: str
    chat_title: str


class _SnapshotAdapter:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def read_destination_snapshot(self):
        return self.snapshot


class _FakeQuery:
    def __init__(self, observations: list[AXDestinationObservation]) -> None:
        self.observations = list(observations)
        self.calls: list[tuple[str, int, int]] = []
        self.mutation_calls: list[str] = []

    def read_observation(self, *, app_name: str, max_depth: int, max_nodes: int):
        self.calls.append((app_name, max_depth, max_nodes))
        if len(self.calls) <= len(self.observations):
            return self.observations[len(self.calls) - 1]
        return self.observations[-1]

    def activate(self) -> None:
        self.mutation_calls.append("activate")

    def click(self) -> None:
        self.mutation_calls.append("click")

    def scroll(self) -> None:
        self.mutation_calls.append("scroll")

    def paste(self) -> None:
        self.mutation_calls.append("paste")


if __name__ == "__main__":
    unittest.main()
