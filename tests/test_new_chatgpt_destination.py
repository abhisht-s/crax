from __future__ import annotations

import unittest

from agent.chatgpt_destination_gate import (
    DestinationLeaseContext,
    verify_chatgpt_destination_snapshot,
)
from agent.new_chatgpt_app import PreflightResult
from agent.new_chatgpt_destination import NewChatGPTDestinationSnapshotAdapter
from agent.run_services import RunDestinationBinding
from tests.new_chatgpt_ax_fixtures import (
    FakeBackend,
    FakeProject,
    Node,
    SidebarModel,
    build_app_tree,
)

RUN_ID = "run-1"
LEASE = DestinationLeaseContext(owning_run_id=RUN_ID, lease_token="token", owner_pid=1, acquired_at="2026-11-20T00:00:00Z")


def model(**kwargs) -> SidebarModel:
    defaults = {
        "current": ("<project>", "<chat 1>"),
        "recents": ["<chat 1>", "<chat 9>"],
        "pinned_chats": ["<chat 1>"],
        "pinned_projects": ["<project>"],
    }
    defaults.update(kwargs)
    return SidebarModel(
        [
            FakeProject("<project>", ["<chat 0>", "<chat 1>", "<chat 2>"]),
            FakeProject("<other project>", ["<chat 1>", "<chat 9>"]),
        ],
        **defaults,
    )


class Harness:
    def __init__(self, sidebar: SidebarModel | None = None, **tree_kwargs) -> None:
        self.tree = build_app_tree(sidebar_model=sidebar or model(), **tree_kwargs)
        self.backend = self.tree.wire(FakeBackend(self.tree.app))
        self.preflight_calls: list[dict] = []
        self.preflight_result = PreflightResult(ok=True, pid=4242)
        self.between_reads = None

    def preflight(self, **kwargs) -> PreflightResult:
        self.preflight_calls.append(kwargs)
        return self.preflight_result

    def sleep(self, _seconds: float) -> None:
        if self.between_reads is not None:
            self.between_reads()
            self.between_reads = None

    def adapter(self, **kwargs) -> NewChatGPTDestinationSnapshotAdapter:
        return NewChatGPTDestinationSnapshotAdapter(backend=self.backend, preflight=self.preflight, sleep=self.sleep, **kwargs)

    def gate(self, project: str = "<project>", chat: str = "<chat 1>", **adapter_kwargs):
        adapter = self.adapter(**adapter_kwargs)
        result = verify_chatgpt_destination_snapshot(
            run_id=RUN_ID,
            binding=RunDestinationBinding(project_title=project, chat_title=chat),
            lease_context=LEASE,
            adapter=adapter,
        )
        return result, adapter


class GateWithNewAppAdapterTests(unittest.TestCase):
    def test_bound_chat_open_passes_the_unchanged_gate(self) -> None:
        h = Harness()

        result, adapter = h.gate()

        self.assertTrue(result.ok, result.reason_code)
        self.assertEqual(result.state, "destination_verified_exact")
        self.assertEqual(result.evidence_summary.matching_active_project_candidate_count, 1)
        self.assertEqual(result.evidence_summary.matching_selected_chat_row_candidate_count, 1)
        self.assertTrue(adapter.last_evidence["stable"])

    def test_adapter_never_uses_the_authoritative_fast_path(self) -> None:
        h = Harness()

        snapshot = h.adapter().read_destination_snapshot()

        self.assertEqual(snapshot.active_conversation_chat_title, "")
        self.assertEqual(snapshot.active_conversation_project_title, "")

    def test_adapter_presses_nothing_and_brings_app_to_front(self) -> None:
        h = Harness()

        h.gate()

        self.assertEqual(h.backend.actions, [])
        self.assertEqual(h.preflight_calls[0]["bring_to_front"], True)

    def test_other_chat_in_the_same_project_fails(self) -> None:
        h = Harness()

        result, _ = h.gate(chat="<chat 2>")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_not_active")

    def test_same_chat_title_in_another_project_fails(self) -> None:
        # "<chat 1>" exists in both projects; the open one is in "<project>".
        h = Harness()

        result, _ = h.gate(project="<other project>")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "project_not_active")

    def test_open_chat_outside_any_project_fails(self) -> None:
        h = Harness(model(current=None))
        h.tree.web_area.attrs["AXTitle"] = h.tree.window.attrs["AXTitle"] = "<chat 9>"

        result, _ = h.gate(chat="<chat 9>")

        # The bound project is visible but holds no current row: the gate's
        # existing reason for "visible but not the active destination".
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "visible_title_not_actionable_destination")

    def test_duplicate_titles_in_the_bound_project_fail(self) -> None:
        sidebar = SidebarModel([FakeProject("<project>", ["<dup>", "<dup>"])], current=("<project>", "<dup>"))
        h = Harness(sidebar)

        result, _ = h.gate(chat="<dup>")

        self.assertFalse(result.ok)
        self.assertIn(result.reason_code, ("chat_title_ambiguous", "ax_tree_truncated_or_unstable"))

    def test_recents_copy_marked_current_does_not_matter(self) -> None:
        h = Harness(model(recents_current_too=True))

        result, _ = h.gate()

        self.assertTrue(result.ok, result.reason_code)

    def test_title_bar_showing_another_chat_fails(self) -> None:
        h = Harness()
        for node in h.tree.banner.walk():
            if node.attrs.get("AXRole") == "AXPopUpButton":
                node.attrs["AXTitle"] = node.attrs["AXDescription"] = "<chat 2>"

        result, _ = h.gate()

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_not_active")

    def test_window_title_disagreeing_is_uncertain(self) -> None:
        h = Harness()
        h.tree.window.attrs["AXTitle"] = "<something else>"

        result, adapter = h.gate()

        self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")
        self.assertIn("window_title_disagrees_with_web_area", adapter.last_evidence["uncertainty"])

    def test_chat_switching_between_reads_is_unstable(self) -> None:
        h = Harness()

        def switch() -> None:
            h.tree.sidebar_model.current = ("<project>", "<chat 2>")
            h.tree.sidebar_model.rerender()

        h.between_reads = switch

        result, adapter = h.gate()

        self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")
        self.assertFalse(adapter.last_evidence["stable"])

    def test_missing_composer_fails(self) -> None:
        h = Harness(composer_count=0)

        result, _ = h.gate()

        self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_empty_chat_without_transcript_fails(self) -> None:
        h = Harness(turns=[])

        result, _ = h.gate()

        self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_legacy_banner_project_label_must_agree(self) -> None:
        agree = Harness()
        agree.tree.banner.children.append(Node("AXImage", AXDescription="Project: <project>"))
        disagree = Harness()
        disagree.tree.banner.children.append(Node("AXImage", AXDescription="Project: <other project>"))

        self.assertTrue(agree.gate()[0].ok)
        result, adapter = disagree.gate()
        self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")
        self.assertIn("banner_project_disagrees_with_sidebar", adapter.last_evidence["uncertainty"])

    def test_collapsed_bound_project_fails(self) -> None:
        sidebar = model()
        sidebar.projects[0].expanded = False
        h = Harness(sidebar)

        result, _ = h.gate()

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "visible_title_not_actionable_destination")


class PreflightFailureMappingTests(unittest.TestCase):
    def check(self, reason_code: str, expected_gate_reason: str) -> None:
        h = Harness()
        h.preflight_result = PreflightResult(ok=False, reason_code=reason_code, error="x")

        result, _ = h.gate()

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, expected_gate_reason)
        self.assertEqual(h.backend.read_nodes, [])

    def test_not_running(self) -> None:
        self.check("new_chatgpt_not_running", "chatgpt_not_running")

    def test_not_trusted(self) -> None:
        self.check("accessibility_not_trusted", "accessibility_unavailable")

    def test_enhanced_ui_off(self) -> None:
        self.check("new_chatgpt_ax_not_enabled", "accessibility_unavailable")

    def test_window_not_attached(self) -> None:
        self.check("new_chatgpt_window_not_attached", "chatgpt_window_unavailable")

    def test_not_frontmost(self) -> None:
        self.check("new_chatgpt_not_frontmost", "chatgpt_window_unavailable")

    def test_wrong_mode(self) -> None:
        self.check("new_chatgpt_wrong_mode", "ax_tree_truncated_or_unstable")

    def test_menu_left_open(self) -> None:
        self.check("new_chatgpt_overlay_open", "ax_tree_truncated_or_unstable")


if __name__ == "__main__":
    unittest.main()
