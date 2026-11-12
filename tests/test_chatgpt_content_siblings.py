"""Exercise native-reader traversal with an offline Accessibility graph."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from agent import chatgpt_ax_capture as capture, cli, mac_ui_inspect as ui
from agent.chatgpt_services import _submission_verification_status, _verify_submission_marker
from tests.test_chatgpt_ax_capture import MARKER, SENTINEL_RESPONSE, _FakeClock


class Call:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class Graph:
    def __init__(self, *, visible=False, draft=False, duplicate=False, foreign=False, cycle=False):
        self.attributes = {
            99: {"AXFocusedWindow": 1},
            1: {"AXRole": "AXWindow", "AXChildren": [20]},
            20: {"AXRole": "AXGroup", "AXSubrole": "AXHostingView", "AXChildren": [2, 9, 10]},
            2: {"AXRole": "AXList", "AXSubrole": "AXSectionList", "AXChildren": [3, 4, 5] if visible else [3, 5]},
            3: {"AXRole": "AXGroup", "AXParent": 2},
            4: {"AXRole": "AXGroup", "AXSubrole": "AXHostingView", "AXParent": 12 if foreign else 2,
                "AXChildren": [7], "AXPreviousContentSibling": 104 if cycle else 3},
            5: {"AXRole": "AXGroup", "AXSubrole": "AXHostingView", "AXParent": 2,
                "AXChildren": [8], "AXPreviousContentSibling": 11 if duplicate else 104},
            7: {"AXRole": "AXStaticText", "AXDescription": MARKER},
            8: {"AXRole": "AXStaticText", "AXDescription": SENTINEL_RESPONSE},
            9: {"AXRole": "AXTextArea", "AXFocused": True, "AXValue": MARKER if draft else ""},
            10: {"AXRole": "AXButton", "AXDescription": "Send", "AXEnabled": True},
            11: {"AXRole": "AXGroup", "AXSubrole": "AXHostingView", "AXParent": 2,
                 "AXChildren": [13], "AXPreviousContentSibling": 4},
            13: {"AXRole": "AXStaticText", "AXDescription": MARKER},
        }
        # Two native references to the same AX element must compare equal.
        self.identity = lambda value: 4 if value == 104 else value
        self.values = {}
        self.sibling_reads = 0
        self.cf = SimpleNamespace(
            CFGetTypeID=lambda ref: 2 if isinstance(self.values.get(ref.value), list) else 1,
            CFArrayGetCount=lambda ref: len(self.values[ref.value]),
            CFArrayGetValueAtIndex=lambda ref, index: self.values[ref.value][index],
            CFEqual=Call(lambda left, right: self.identity(left.value) == self.identity(right.value)),
        )

    def copy(self, element, name):
        if name == "AXPreviousContentSibling":
            self.sibling_reads += 1
        value = self.attributes.get(self.identity(element), {}).get(name)
        if value is None:
            return None
        if isinstance(value, (list, str, bool)):
            reference = 1000 + len(self.values)
            self.values[reference] = value
            return reference
        return value

    def reader(self, cls=ui._AXSubmissionReader, *, max_nodes=1200, max_depth=18):
        reader = cls.__new__(cls)
        reader.app_name, reader.max_nodes, reader.max_depth = "ChatGPT", max_nodes, max_depth
        reader._cf, reader._array_type = self.cf, 2
        reader._ax = SimpleNamespace(AXUIElementCreateApplication=lambda pid: 99)
        reader._get_pid = lambda: 123
        reader._copy_attribute = self.copy
        reader._cf_string = lambda value: self.values.get(value, "") if isinstance(self.values.get(value), str) else ""
        reader._cf_bool = lambda value: self.values.get(value) if isinstance(self.values.get(value), bool) else None
        reader._copy_actions = lambda element: None
        reader._array_strings = lambda value: ()
        return reader

    def inspect(self, *, recover_missing_marker=True, **limits):
        reader = self.reader(**limits)
        with mock.patch.object(ui, "_AXSubmissionReader", return_value=reader):
            return ui.inspect_chatgpt_submission_ui(
                marker_text=MARKER, recover_missing_marker=recover_missing_marker, **limits,
            )


class ContentSiblingTests(unittest.TestCase):
    def test_preparation_inspection_does_not_scan_content_siblings(self):
        graph = Graph()
        observation = graph.inspect(recover_missing_marker=False)
        self.assertFalse(_submission_verification_status(observation, MARKER)["verified"])
        self.assertEqual(graph.sibling_reads, 0)

    def test_native_submission_verifier_enables_missing_marker_recovery(self):
        clock = _FakeClock()
        with mock.patch.object(ui, "_AXSubmissionReader", return_value=Graph().reader()):
            result = _verify_submission_marker(
                "ChatGPT", MARKER, monotonic_function=clock.monotonic, sleep_function=clock.sleep,
            )
        self.assertTrue(result["ok"])

    def test_cli_verifier_enables_missing_marker_recovery(self):
        with (
            mock.patch.object(ui, "_AXSubmissionReader", return_value=Graph().reader()),
            mock.patch.object(cli.time, "sleep", side_effect=AssertionError("Marker should verify on the first read")),
        ):
            result = cli._verify_submission_marker("ChatGPT", MARKER)
        self.assertTrue(result["ok"])

    def test_hidden_submission_is_verified_using_exact_marker(self):
        observation = Graph().inspect()
        self.assertTrue(_submission_verification_status(observation, MARKER)["verified"])
        self.assertEqual(observation["marker_text_candidate_count"], 1)

    def test_normal_visible_message_keeps_existing_paths_and_skips_fallback(self):
        graph = Graph(visible=True)
        observation = graph.inspect()
        self.assertTrue(_submission_verification_status(observation, MARKER)["verified"])
        self.assertEqual(observation["send_button"]["path"], "FW.1.3")
        self.assertEqual(graph.sibling_reads, 0)

    def test_marker_in_composer_does_not_trigger_recovery_or_verify_submission(self):
        graph = Graph(draft=True)
        observation = graph.inspect()
        self.assertFalse(_submission_verification_status(observation, MARKER)["verified"])
        self.assertTrue(observation["marker_text_present_in_composer"])
        self.assertEqual(graph.sibling_reads, 0)

    def test_draft_marker_appearing_during_recovery_cannot_verify_submission(self):
        graph = Graph()
        original = graph.copy

        def copy(element, name):
            if element == 9 and name == "AXValue" and graph.sibling_reads:
                graph.attributes[9]["AXValue"] = MARKER
            return original(element, name)

        graph.copy = copy
        self.assertFalse(_submission_verification_status(graph.inspect(), MARKER)["verified"])

    def test_distinct_duplicate_messages_remain_ambiguous(self):
        observation = Graph(duplicate=True).inspect()
        self.assertTrue(_submission_verification_status(observation, MARKER)["ambiguous"])

    def test_sibling_cycle_is_bounded_and_alias_is_not_a_duplicate_submission(self):
        graph = Graph(cycle=True)
        observation = graph.inspect()
        self.assertTrue(_submission_verification_status(observation, MARKER)["verified"])
        self.assertEqual(observation["marker_text_candidate_count"], 1)
        self.assertLess(graph.sibling_reads, 10)

    def test_sibling_from_another_list_is_rejected(self):
        self.assertFalse(_submission_verification_status(Graph(foreign=True).inspect(), MARKER)["verified"])

    def test_recent_hidden_marker_is_not_starved_by_many_earlier_visible_rows(self):
        graph = Graph()
        earlier = list(range(100, 132))
        for element in earlier:
            graph.attributes[element] = {"AXRole": "AXGroup", "AXParent": 2}
        # Avoid the deliberately aliased reference 104 in this fixture.
        earlier.remove(104)
        graph.attributes[2]["AXChildren"] = earlier + [3, 5]
        self.assertTrue(_submission_verification_status(graph.inspect(), MARKER)["verified"])

    def test_large_older_linked_reply_does_not_starve_recent_anchor_and_response(self):
        graph = Graph()
        graph.attributes[4]["AXPreviousContentSibling"] = 30
        graph.attributes[30] = {"AXRole": "AXGroup", "AXParent": 2, "AXChildren": [31]}
        graph.attributes[31] = {"AXRole": "AXGroup", "AXChildren": list(range(200, 280))}
        for element in range(200, 280):
            graph.attributes[element] = {"AXRole": "AXStaticText", "AXDescription": "Old reply"}
        graph.attributes[1]["AXChildren"] = [40, 20]
        graph.attributes[40] = {"AXRole": "AXGroup", "AXChildren": list(range(300, 330))}
        for element in range(300, 330):
            graph.attributes[element] = {"AXRole": "AXGroup"}
        reader = graph.reader(capture._AXReader, max_nodes=100)
        reader._submission_marker_text = MARKER
        candidates, stats = reader.collect_text_candidates()
        result = capture.find_response_candidate_after_marker(candidates, MARKER)
        self.assertTrue(result["ok"])
        self.assertFalse(stats["response_controls_complete"])

    def test_unfinished_scan_cannot_hide_an_older_duplicate_marker(self):
        graph = Graph()
        graph.attributes[4]["AXPreviousContentSibling"] = 30
        graph.attributes[30] = {"AXRole": "AXGroup", "AXParent": 2, "AXChildren": list(range(200, 240))}
        for element in range(200, 240):
            graph.attributes[element] = {"AXRole": "AXStaticText", "AXDescription": "Old reply"}
        graph.attributes[239]["AXDescription"] = MARKER
        self.assertFalse(_submission_verification_status(graph.inspect(max_nodes=20), MARKER)["verified"])

    def test_recovered_marker_does_not_verify_when_the_recovered_walk_is_truncated(self):
        graph = Graph()
        graph.attributes[4]["AXChildren"] = [7] + list(range(200, 240))
        for element in range(200, 240):
            graph.attributes[element] = {"AXRole": "AXStaticText", "AXDescription": "More text"}
        self.assertFalse(_submission_verification_status(graph.inspect(max_nodes=20), MARKER)["verified"])

    def test_aliases_in_visible_rows_do_not_duplicate_the_recovered_reply(self):
        graph = Graph()
        graph.identity = lambda value: {104: 4, 105: 5}.get(value, value)
        graph.attributes[2]["AXChildren"] = [3, 5, 105]
        reader = graph.reader(capture._AXReader)
        reader._submission_marker_text = MARKER
        candidates, _ = reader.collect_text_candidates()
        self.assertTrue(capture.find_response_candidate_after_marker(candidates, MARKER)["ok"])

    def test_traversal_limits_cannot_turn_partial_message_into_verified_submission(self):
        for limits in ({"max_nodes": 3}, {"max_depth": 2}):
            with self.subTest(limits=limits):
                self.assertFalse(_submission_verification_status(Graph().inspect(**limits), MARKER)["verified"])

    def test_capture_recovers_anchor_before_reply_and_returns_stable_prompt(self):
        graph = Graph()
        reader = graph.reader(capture._AXReader)
        clock = _FakeClock()
        calls = {"count": 0}
        collect = reader.collect_text_candidates

        def poll():
            calls["count"] += 1
            return collect()

        with (
            mock.patch.object(capture, "_AXReader", return_value=reader),
            mock.patch.object(reader, "collect_text_candidates", side_effect=poll),
            mock.patch.object(capture.time, "monotonic", clock.monotonic),
            mock.patch.object(capture.time, "sleep", clock.sleep),
        ):
            result = capture.capture_response_after_feedback(
                "", submission_marker_text=MARKER, require_sentinel_response=True,
                stable_seconds=0.5, poll_interval_seconds=0,
                should_stop=lambda: calls["count"] >= 8,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["response_text"], SENTINEL_RESPONSE)
        self.assertLess(result["matched_candidate_index"], result["response_candidate_index"])

    def test_reply_before_hidden_anchor_cannot_be_captured_as_its_response(self):
        graph = Graph()
        graph.attributes[4]["AXChildren"] = [8]
        graph.attributes[5]["AXChildren"] = [7]
        reader = graph.reader(capture._AXReader)
        reader._submission_marker_text = MARKER
        candidates, _ = reader.collect_text_candidates()
        result = capture.find_response_candidate_after_marker(candidates, MARKER)
        self.assertFalse(result["ok"])

