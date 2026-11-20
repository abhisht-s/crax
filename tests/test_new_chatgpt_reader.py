from __future__ import annotations

import dataclasses
import unittest

from agent import new_chatgpt_labels as labels
from agent.new_chatgpt_reader import (
    ANCHOR_NAMES,
    AXNodeData,
    locate_anchors,
    snapshot_region,
)
from tests.new_chatgpt_ax_fixtures import (
    FakeBackend,
    Node,
    assistant_turn,
    build_app_tree,
    radix_menu,
    text,
    user_turn,
)


class LocateAnchorsTests(unittest.TestCase):
    def test_finds_every_anchor_in_a_normal_chat(self) -> None:
        tree = build_app_tree()
        backend = FakeBackend(tree.app)

        anchors = locate_anchors(backend, tree.app)

        self.assertEqual(anchors.missing(), [])
        self.assertIs(anchors.web_area, tree.web_area)
        self.assertIs(anchors.root, tree.root)
        self.assertIs(anchors.sidebar, tree.sidebar)
        self.assertIs(anchors.banner, tree.banner)
        self.assertIs(anchors.main, tree.main)
        self.assertIs(anchors.chat_panel, tree.chat_panel)
        self.assertIs(anchors.turn_list, tree.turn_list)
        self.assertIs(anchors.composer, tree.composer)
        self.assertEqual(anchors.web_area_title, "<chat 0>")
        self.assertTrue(anchors.search_complete)
        self.assertEqual(anchors.turn_heading_count, 2)
        self.assertEqual(anchors.composer_candidate_count, 1)

    def test_current_build_without_chat_panel_id_uses_thread_layout(self) -> None:
        # 2026-11-20 build: no DOM id ending "-chat-panel" anywhere.
        tree = build_app_tree()
        self.assertFalse([n for n in tree.app.walk() if str(n.attrs.get("AXDOMIdentifier", "")).endswith("-chat-panel")])

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertIs(anchors.chat_panel, tree.chat_panel)
        self.assertIn("group/thread-scroll-layout", tree.chat_panel.attrs["AXDOMClassList"])
        self.assertIs(anchors.composer, tree.composer)

    def test_older_build_with_chat_panel_id_uses_the_outer_panel(self) -> None:
        # 2026-11-18 build: outer element with id "*-chat-panel" around the thread layout.
        tree = build_app_tree(legacy_chat_panel_id=True)

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertEqual(anchors.missing(), [])
        self.assertTrue(tree.chat_panel.attrs["AXDOMIdentifier"].endswith("-chat-panel"))
        self.assertIs(anchors.chat_panel, tree.chat_panel)
        self.assertIs(anchors.composer, tree.composer)

    def test_summary_is_structural_only(self) -> None:
        tree = build_app_tree()

        summary = locate_anchors(FakeBackend(tree.app), tree.app).summary()

        self.assertEqual(set(summary["found"]), set(ANCHOR_NAMES))
        self.assertTrue(all(summary["found"].values()))
        self.assertNotIn("<chat 0>", repr(summary))
        self.assertNotIn("<prompt>", repr(summary))

    def test_does_not_walk_into_sidebar_banner_or_turn_bodies(self) -> None:
        tree = build_app_tree()
        backend = FakeBackend(tree.app)

        locate_anchors(backend, tree.app)

        read = {id(node) for node in backend.read_nodes}
        skipped = [node for node in tree.sidebar.walk() if node is not tree.sidebar]
        skipped += [node for node in tree.banner.walk() if node is not tree.banner]
        for part in tree.turn_list.children:
            skipped += [node for node in part.walk() if node is not part]
        self.assertTrue(skipped)
        self.assertFalse([node for node in skipped if id(node) in read])

    def test_search_cost_does_not_grow_with_reply_length(self) -> None:
        short = build_app_tree(turns=user_turn("<p>") + assistant_turn(["<r>"]))
        long = build_app_tree(turns=user_turn("<p>") + assistant_turn([f"<r{i}>" for i in range(500)]))

        short_anchors = locate_anchors(FakeBackend(short.app), short.app)
        long_anchors = locate_anchors(FakeBackend(long.app), long.app)

        self.assertEqual(short_anchors.nodes_visited, long_anchors.nodes_visited)

    def test_ignores_text_areas_that_are_not_the_composer(self) -> None:
        tree = build_app_tree()

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertIsNot(anchors.composer, tree.extra["decoy_text_area"])
        self.assertIs(anchors.composer, tree.composer)

    def test_composer_lookalike_inside_a_turn_is_ignored(self) -> None:
        edit_box = Node("AXTextArea", AXTitle="Ask ChatGPT", AXDescription="Ask ChatGPT", AXDOMClassList=["ProseMirror"])
        turns = user_turn("<p>") + [Node("AXGroup", [edit_box])] + assistant_turn(["<r>"])
        tree = build_app_tree(turns=turns)

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertIs(anchors.composer, tree.composer)
        self.assertEqual(anchors.composer_candidate_count, 1)

    def test_two_composers_is_ambiguous(self) -> None:
        tree = build_app_tree(composer_count=2)

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertIsNone(anchors.composer)
        self.assertEqual(anchors.composer_candidate_count, 2)
        self.assertEqual(anchors.missing("composer"), ["composer"])

    def test_empty_chat_has_no_turn_list(self) -> None:
        tree = build_app_tree(turns=[])

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertEqual(anchors.missing(), ["turn_list"])
        self.assertEqual(anchors.turn_heading_count, 0)

    def test_detached_web_content_finds_nothing(self) -> None:
        tree = build_app_tree(attached=False)

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertEqual(anchors.missing(), list(ANCHOR_NAMES))
        self.assertFalse(anchors.search_complete)

    def test_node_budget_marks_search_incomplete(self) -> None:
        tree = build_app_tree()

        anchors = locate_anchors(FakeBackend(tree.app), tree.app, max_nodes=5)

        self.assertFalse(anchors.search_complete)
        self.assertEqual(anchors.nodes_visited, 5)

    def test_anchors_survive_portals_shifting_sibling_indexes(self) -> None:
        tree = build_app_tree(overlays=[radix_menu(), radix_menu()])
        tree.top_group.children.remove(tree.root)
        tree.top_group.children.append(tree.root)  # #root is no longer the first child

        anchors = locate_anchors(FakeBackend(tree.app), tree.app)

        self.assertEqual(anchors.missing(), [])

    def test_works_without_bulk_attribute_reads(self) -> None:
        tree = build_app_tree()

        anchors = locate_anchors(FakeBackend(tree.app, bulk=False), tree.app)

        self.assertEqual(anchors.missing(), [])


class SnapshotRegionTests(unittest.TestCase):
    def test_turn_list_snapshot_is_in_document_order(self) -> None:
        tree = build_app_tree()
        backend = FakeBackend(tree.app)
        anchors = locate_anchors(backend, tree.app)

        snap = snapshot_region(backend, anchors.turn_list)

        self.assertTrue(snap.complete)
        headings = [node.label for node in snap.root.children if node.role == "AXHeading"]
        self.assertEqual(headings, [labels.USER_TURN_HEADING.text, labels.ASSISTANT_TURN_HEADING.text])
        texts = [node.text_value for node in snap.root.iter() if node.role == "AXStaticText"]
        self.assertEqual(texts, ["<prompt>", "<reply paragraph 1>", "<reply paragraph 2>"])
        buttons = [node.label for node in snap.root.iter() if node.role in ("AXButton", "AXPopUpButton", "AXCheckBox")]
        self.assertEqual(buttons, ["Copy message", "Edit message", "Copy", "Read aloud", "Regenerate response", "Rate response"])

    def test_nodes_keep_element_handles_but_no_paths_or_frames(self) -> None:
        tree = build_app_tree()
        backend = FakeBackend(tree.app)

        snap = snapshot_region(backend, tree.composer)

        self.assertIs(snap.root.element, tree.composer)
        field_names = {f.name for f in dataclasses.fields(AXNodeData)}
        self.assertFalse({"path", "frame", "position", "size"} & field_names)

    def test_attribute_types_are_normalized(self) -> None:
        heading = Node("AXHeading", AXTitle="You said:", AXValue=4)
        checkbox = Node("AXCheckBox", AXDescription="Read aloud", AXValue=True, AXEnabled=True)
        group = Node("AXGroup", [heading, checkbox], AXDOMClassList=["a", 7, "b"])
        backend = FakeBackend(group)

        snap = snapshot_region(backend, group)

        self.assertEqual(snap.root.dom_classes, ("a", "b"))
        self.assertEqual(snap.root.children[0].value, 4)
        self.assertIsNone(snap.root.children[1].value)  # booleans are not values
        self.assertTrue(snap.root.children[1].enabled)
        self.assertIsNone(snap.root.children[0].enabled)
        self.assertTrue(snap.root.children[1].has_label("Read aloud"))

    def test_depth_limit_marks_snapshot_incomplete(self) -> None:
        tree = build_app_tree()
        backend = FakeBackend(tree.app)

        snap = snapshot_region(backend, tree.turn_list, max_depth=1)

        self.assertFalse(snap.complete)
        self.assertTrue(snap.truncated_by_depth)
        self.assertTrue(any(node.children_truncated for node in snap.root.iter()))

    def test_node_limit_marks_snapshot_incomplete(self) -> None:
        tree = build_app_tree()

        snap = snapshot_region(FakeBackend(tree.app), tree.turn_list, max_nodes=4)

        self.assertFalse(snap.complete)
        self.assertTrue(snap.truncated_by_nodes)
        self.assertEqual(snap.node_count, 4)

    def test_missing_region_is_incomplete(self) -> None:
        snap = snapshot_region(FakeBackend(Node("AXApplication")), None)

        self.assertIsNone(snap.root)
        self.assertFalse(snap.complete)

    def test_find_helpers(self) -> None:
        tree = build_app_tree()
        snap = snapshot_region(FakeBackend(tree.app), tree.chat_panel)

        send = snap.root.find_first(lambda node: node.role == "AXButton" and node.has_label(labels.SEND.text))
        rows = snap.root.find_all(lambda node: node.role == "AXStaticText")

        self.assertIs(send.element, tree.send)
        self.assertFalse(send.enabled)
        self.assertIn("ChatGPT can make mistakes. Check important info.", [row.text_value for row in rows])


class LabelTests(unittest.TestCase):
    def test_template_labels_parse_their_argument(self) -> None:
        self.assertEqual(labels.MODE_TRIGGER.parse("Switch mode, current mode: Codex"), "Codex")
        self.assertEqual(labels.BANNER_PROJECT.parse("Project: Example Project"), "Example Project")
        self.assertEqual(labels.PROJECT_CHATS_LIST.parse("Chats in demo"), "demo")
        self.assertIsNone(labels.BANNER_PROJECT.parse("Projects"))
        self.assertIsNone(labels.BANNER_PROJECT.parse("Project: "))
        self.assertIsNone(labels.SEND.parse("Send"))

    def test_preflight_uses_the_label_table(self) -> None:
        from agent import new_chatgpt_app

        self.assertEqual(new_chatgpt_app.LABEL_MODE_TRIGGER_PREFIX, "Switch mode, current mode: ")
        self.assertEqual(new_chatgpt_app.LABEL_COMPOSER, labels.COMPOSER.text)
        self.assertEqual(new_chatgpt_app.REQUIRED_PRODUCT_MODE, "ChatGPT")


if __name__ == "__main__":
    unittest.main()
