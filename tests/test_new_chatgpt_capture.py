from __future__ import annotations

import unittest

from agent.new_chatgpt_app import PreflightResult
from agent.new_chatgpt_capture import (
    OUTCOME_COMPLETE_WITH_PROMPT,
    OUTCOME_COMPLETE_WITHOUT_PROMPT,
    REASON_ANCHOR_AMBIGUOUS,
    REASON_ANCHOR_NOT_FOUND,
    REASON_CANCELLED,
    REASON_COMPLETED_WITHOUT_PROMPT,
    REASON_PROMPT_MALFORMED,
    REASON_TIMEOUT,
    capture_reply_new_chatgpt,
    render_text,
)
from agent.new_chatgpt_reader import snapshot_region
from tests.new_chatgpt_ax_fixtures import FakeBackend, Node, build_app_tree, labelled, text, user_turn

MARKER = "SUBMISSION_MARKER_BEGIN\nnonce=abc\nSUBMISSION_MARKER_END"

PROMPT_LINES = [
    "BEGIN_NEXT_CODEX_PROMPT",
    "",
    "TASK: Do the thing.",
    "",
    "    indented line kept",
    "- step",
    "- step",  # identical consecutive lines must both survive
    "",
    "END_NEXT_CODEX_PROMPT",
]


def code_block(lines: list[str]) -> Node:
    """Live shape (findings section 17): AXCodeStyleGroup with one text node per line and "\\n" nodes."""

    children: list[Node] = []
    for line in lines:
        if line:
            children.append(text(line))
        children.append(text("\n"))
    return Node("AXGroup", children, AXSubrole="AXCodeStyleGroup", AXRoleDescription="code")


def paragraph(*inline: str) -> Node:
    return Node("AXGroup", [text(piece) for piece in inline])


def reply_parts(body: list[Node], *, finished: bool = True) -> list[Node]:
    parts = [
        Node("AXHeading", AXTitle="ChatGPT said:", AXValue=4),
        Node("AXGroup", [labelled("AXButton", "Worked for 1m 37s", [text("Worked for 1m 37s")])]),
        Node("AXGroup", body, AXDOMClassList=["relative", "PortalBoundary-x", "Renderer-y"]),
    ]
    if finished:
        parts += [
            Node("AXGroup", [labelled("AXButton", "Copy")], AXDOMClassList=["contents"]),
            Node("AXGroup", [Node("AXCheckBox", AXDescription="Read aloud", AXSubrole="AXToggleButton")]),
            Node("AXGroup", [labelled("AXButton", "Rate response")]),
            labelled("AXPopUpButton", "More actions"),
        ]
    return parts


def prompt_body() -> list[Node]:
    return [
        paragraph("Stage 1 looks ready."),
        paragraph("What we're doing:", " Checking it."),
        Node("AXGroup", [labelled("AXButton", "Copy")]),  # code-block toolbar Copy inside the body
        Node("AXGroup", [text("Plain text"), code_block(PROMPT_LINES)]),
    ]


class Harness:
    def __init__(self, turns: list[Node], *, streaming: bool = False) -> None:
        self.tree = build_app_tree(turns=turns)
        self.backend = FakeBackend(self.tree.app)
        self.now = 0.0
        self.script: list = []  # callables applied one per sleep
        self.set_streaming(streaming)

    def set_streaming(self, on: bool) -> None:
        label = "Stop" if on else "Send"
        self.tree.send.attrs["AXDescription"] = self.tree.send.attrs["AXTitle"] = label

    def preflight(self, **kwargs) -> PreflightResult:
        self.tree.app.attrs["AXFrontmost"] = True
        return PreflightResult(ok=True, pid=4242)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.script:
            self.script.pop(0)()

    def capture(self, marker: str | None = MARKER, **kwargs):
        kwargs.setdefault("timeout_seconds", 30.0)
        return capture_reply_new_chatgpt(
            marker, backend=self.backend, preflight=self.preflight, sleep=self.sleep, clock=lambda: self.now, **kwargs
        )


def as_data(nodes: list[Node]):
    """Snapshot fake nodes the way the capture code sees them."""

    return [snapshot_region(FakeBackend(node), node).root for node in nodes]


class RenderTextTests(unittest.TestCase):
    def test_code_block_is_exact(self) -> None:
        rendered = render_text(as_data([code_block(PROMPT_LINES)]))

        self.assertEqual(rendered, "\n".join(PROMPT_LINES))

    def test_inline_pieces_join_and_buttons_are_skipped(self) -> None:
        rendered = render_text(
            as_data(
                [
                    Node("AXGroup", [labelled("AXButton", "Worked for 1m", [text("Worked for 1m")])]),
                    paragraph("What we're doing:", " Checking it."),
                    paragraph("See ", "x"),
                ]
            )
        )

        self.assertEqual(rendered, "What we're doing: Checking it.\nSee x")

    def test_list_items_get_their_markers(self) -> None:
        items = [
            Node("AXGroup", [Node("AXListMarker", AXValue="1. "), Node("AXGroup", [text("Apple")])]),
            Node("AXGroup", [Node("AXListMarker", AXValue="2. "), Node("AXGroup", [text("Banana")])]),
        ]

        rendered = render_text(as_data([Node("AXList", items, AXSubrole="AXContentList")]))

        self.assertEqual(rendered, "1. Apple\n2. Banana")  # not "AppleBanana"

    def test_links_stay_inline_and_rows_become_lines(self) -> None:
        link_paragraph = Node("AXGroup", [text("Read "), Node("AXLink", [text("the docs")]), text(" first.")])
        table = Node(
            "AXTable",
            [
                Node("AXRow", [Node("AXCell", [text("Name")]), Node("AXCell", [text("Value")])]),
                Node("AXRow", [Node("AXCell", [text("a")]), Node("AXCell", [text("1")])]),
            ],
        )

        self.assertEqual(render_text(as_data([link_paragraph, table])), "Read the docs first.\nName | Value\na | 1")


class CaptureTests(unittest.TestCase):
    def test_finished_reply_with_prompt_is_extracted_exactly(self) -> None:
        h = Harness(user_turn("feedback\n" + MARKER) + reply_parts(prompt_body()))

        result = h.capture()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.outcome, OUTCOME_COMPLETE_WITH_PROMPT)
        self.assertEqual(result.prompt_text, "TASK: Do the thing.\n\n    indented line kept\n- step\n- step")
        self.assertNotIn("Worked for", result.reply_text)
        self.assertEqual(result.polls, 2)  # needs two identical finished reads

    def test_waits_while_streaming_then_captures(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts([paragraph("Thinking")], finished=False), streaming=True)
        turn_list = h.tree.turn_list

        def finish() -> None:
            turn_list.children[4:] = reply_parts(prompt_body())
            h.set_streaming(False)

        h.script = [lambda: None, lambda: None, finish]

        result = h.capture()

        self.assertTrue(result.ok, result.error)
        self.assertGreaterEqual(result.polls, 5)

    def test_text_still_changing_is_not_finished(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts([paragraph("v1")]))
        body = h.tree.turn_list.children[6]

        h.script = [lambda: body.children.__setitem__(0, paragraph("v2")), lambda: body.children.__setitem__(0, paragraph("v3"))]

        result = h.capture(require_sentinel=False)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.reply_text, "v3")

    def test_reply_without_footer_is_not_finished(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body(), finished=False))

        result = h.capture(timeout_seconds=5.0)

        self.assertEqual(result.reason_code, REASON_TIMEOUT)
        self.assertFalse(result.last_state["footer_complete"])

    def test_streaming_reply_times_out(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body()), streaming=True)

        result = h.capture(timeout_seconds=5.0)

        self.assertEqual(result.reason_code, REASON_TIMEOUT)
        self.assertTrue(result.last_state["streaming"])

    def test_finished_reply_without_prompt_ends_instead_of_waiting_forever(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts([paragraph("Could you clarify which stage?")]))

        result = h.capture(timeout_seconds=600.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, OUTCOME_COMPLETE_WITHOUT_PROMPT)
        self.assertEqual(result.reason_code, REASON_COMPLETED_WITHOUT_PROMPT)
        self.assertLess(result.wait_seconds, 10.0)

    def test_reply_without_prompt_is_ok_when_not_required(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts([paragraph("Here is the answer.")]))

        result = h.capture(require_sentinel=False)

        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, OUTCOME_COMPLETE_WITHOUT_PROMPT)

    def test_malformed_markers_fail(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts([code_block(["BEGIN_NEXT_CODEX_PROMPT", "no end"])]))

        result = h.capture()

        self.assertEqual(result.reason_code, REASON_PROMPT_MALFORMED)

    def test_anchors_on_the_marker_turn_not_the_latest(self) -> None:
        turns = (
            user_turn("first " + MARKER)
            + reply_parts([code_block(PROMPT_LINES)])
            + user_turn("an unrelated later question")
            + reply_parts([paragraph("unrelated answer")])
        )
        h = Harness(turns)

        result = h.capture()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.outcome, OUTCOME_COMPLETE_WITH_PROMPT)

    def test_marker_in_two_turns_is_ambiguous(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body()) + user_turn(MARKER) + reply_parts(prompt_body()))

        result = h.capture()

        self.assertEqual(result.reason_code, REASON_ANCHOR_AMBIGUOUS)

    def test_missing_marker_turn_scrolls_once_then_fails(self) -> None:
        h = Harness(user_turn("something else") + reply_parts(prompt_body()))

        result = h.capture()

        self.assertEqual(result.reason_code, REASON_ANCHOR_NOT_FOUND)
        self.assertTrue(result.scrolled_to_bottom)
        presses = [node.attrs.get("AXDescription") for node, action in h.backend.actions if action == "AXPress"]
        self.assertEqual(presses, ["Scroll to bottom"])

    def test_no_marker_anchors_on_last_user_turn(self) -> None:
        h = Harness(user_turn("old") + reply_parts([paragraph("old answer")]) + user_turn("Here it is") + reply_parts(prompt_body()))

        result = h.capture(marker=None)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.outcome, OUTCOME_COMPLETE_WITH_PROMPT)

    def test_operator_stop(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body()), streaming=True)

        result = h.capture(should_stop=lambda: True)

        self.assertEqual(result.reason_code, REASON_CANCELLED)

    def test_failed_preflight_reads_nothing(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body()))
        h.preflight = lambda **_: PreflightResult(ok=False, reason_code="new_chatgpt_not_frontmost", error="x")

        result = h.capture()

        self.assertEqual(result.reason_code, "new_chatgpt_not_frontmost")
        self.assertEqual(h.backend.read_nodes, [])

    def test_capture_presses_nothing_when_the_turn_is_present(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body()))

        h.capture()

        self.assertEqual(h.backend.actions, [])

    def test_result_can_omit_text_for_evidence(self) -> None:
        h = Harness(user_turn(MARKER) + reply_parts(prompt_body()))

        data = h.capture().to_dict(include_text=False)

        self.assertNotIn("reply_text", data)
        self.assertNotIn("prompt_text", data)
        self.assertEqual(len(data["prompt_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
