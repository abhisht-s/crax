from __future__ import annotations

from dataclasses import replace
import unittest
from unittest import mock

from agent import chatgpt_navigation_diagnostic as nav
from tests.test_chatgpt_navigation_diagnostic import (
    _ActionFactory,
    _ActionReader,
    _ClickFactory,
    _ClickService,
    _DisplayFactory,
    _DisplayProbe,
    _SleepRecorder,
    _WindowServerFactory,
    _WindowServerProbe,
    _detailed_sidebar_snapshots,
    _project_visible_chats_snapshots,
)


def _classic_project_snapshots():
    snapshots = [
        replace(node, title="PTG Assistant") if node.path == "W" else node
        for node in _project_visible_chats_snapshots(partial_last_row=False)
        if node.path != "W.2.1"
    ]
    return snapshots + [
        nav.AXElementSnapshot(path="W.3", depth=1, role="AXGroup", frame=(0, 0, 1200, 52)),
        nav.AXElementSnapshot(path="W.3.1", depth=2, role="AXToolbar", frame=(0, 0, 1200, 52)),
        nav.AXElementSnapshot(path="W.3.1.1", depth=3, role="AXButton",
                              description="PTG Assistant", frame=(300, 0, 200, 52)),
    ]


class ClassicNavigationTests(unittest.TestCase):
    def test_window_and_toolbar_titles_identify_one_open_project(self):
        snapshots = _classic_project_snapshots()
        result = nav.resolve_open_project_content_and_visible_chats(
            "PTG Assistant", snapshots, (0, 0, 1200, 900),
        )
        self.assertEqual(result["status"], "visible_chats_found")
        self.assertEqual(result["visible_chat_count"], 3)
        self.assertEqual(result["chat_list_container"]["path"], "W.2.5")

    def test_menu_only_project_can_be_clicked_after_window_bounds_appear(self):
        sidebar = [replace(n, actions=("AXShowMenu",)) if n.path == "W.1.3.3" else n
                   for n in _detailed_sidebar_snapshots()]
        reader = _ActionReader([sidebar])
        reader.hit_test_at_position = lambda pid, point, title: {
            "available": True, "path": "W.1.3.1", "role": "AXStaticText",
        }

        class Clicker(_ClickService):
            def left_click(self, x, y):
                result = super().left_click(x, y)
                reader.snapshots_by_collect = [_classic_project_snapshots()]
                return result

        class AppearingWindows(_WindowServerProbe):
            calls = 0

            def visible_windows_for_pid(self, pid):
                self.calls += 1
                return [] if self.calls == 1 else super().visible_windows_for_pid(pid)

        clicker = Clicker()
        windows = AppearingWindows([{"window_id": 9, "bounds": (0, 0, 1200, 900)}])
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project", title="PTG Assistant", confirm_open_destination=True,
                activation_function=lambda name: {"activated": True, "is_frontmost": True},
                process_resolver=lambda name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                click_service_factory=_ClickFactory(clicker),
                display_probe_factory=_DisplayFactory(_DisplayProbe()),
                windowserver_probe_factory=_WindowServerFactory(windows),
                sleep_function=_SleepRecorder(),
            )
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["outcome"], "destination_opened_and_visible_chats_resolved")
        self.assertEqual(result["chosen_method"], "validated_geometry_click")
        self.assertEqual(len(clicker.clicks), 1)
        self.assertEqual(reader.actions, [])

    def test_transient_ax_read_failure_does_not_discard_usable_window_geometry(self):
        snapshots = _detailed_sidebar_snapshots()
        reader = _ActionReader([snapshots])
        collect = reader.collect
        failed = False

        def first_read_fails(pid):
            nonlocal failed
            if not failed:
                failed = True
                raise nav.AXDiagnosticError("Temporary AX read failure")
            return collect(pid)

        reader.collect = first_read_fails
        result = nav._stable_chatgpt_geometry_sample(
            reader, 123,
            _WindowServerFactory(_WindowServerProbe([{"bounds": (0, 0, 1200, 900)}])),
            sample_count=3, sleep_function=_SleepRecorder(),
        )
        self.assertEqual(result["status"], "stable")
        self.assertEqual(result["windowserver_bounds"], (0, 0, 1200, 900))

    def test_missing_window_is_retried_for_at_least_five_seconds(self):
        for geometry_required in (True, False):
            with self.subTest(geometry_required=geometry_required):
                sleeper = _SleepRecorder()
                result = nav._stable_chatgpt_geometry_sample(
                    _ActionReader([[]]), 123,
                    _WindowServerFactory(_WindowServerProbe([])),
                    sample_count=3, sleep_function=sleeper,
                    require_window_geometry=geometry_required,
                )
                self.assertEqual(result["status"], "unstable")
                self.assertGreaterEqual(sum(sleeper.calls), 5.0)
                self.assertLess(sum(sleeper.calls), 6.0)

    def test_window_that_appears_after_four_seconds_still_opens(self):
        sleeper = _SleepRecorder()

        class SlowWindow(_WindowServerProbe):
            def visible_windows_for_pid(self, pid):
                if sum(sleeper.calls) < 4.5:
                    return []
                return super().visible_windows_for_pid(pid)

        result = nav._stable_chatgpt_geometry_sample(
            _ActionReader([_detailed_sidebar_snapshots()]), 123,
            _WindowServerFactory(SlowWindow([{"bounds": (0, 0, 1200, 900)}])),
            sample_count=3, sleep_function=sleeper,
        )
        self.assertEqual(result["status"], "stable")
        self.assertGreaterEqual(sum(sleeper.calls), 4.5)

    def test_moving_window_is_retried_for_at_least_five_seconds(self):
        sleeper = _SleepRecorder()

        class MovingWindow(_WindowServerProbe):
            calls = 0

            def visible_windows_for_pid(self, pid):
                self.calls += 1
                return [{"bounds": (self.calls * 50, 0, 1200, 900)}]

        result = nav._stable_chatgpt_geometry_sample(
            _ActionReader([_detailed_sidebar_snapshots()]), 123,
            _WindowServerFactory(MovingWindow([])),
            sample_count=3, sleep_function=sleeper,
        )
        self.assertEqual(result["status"], "unstable")
        self.assertGreaterEqual(sum(sleeper.calls), 5.0)

    def test_ready_window_proceeds_without_waiting_five_seconds(self):
        sleeper = _SleepRecorder()
        result = nav._stable_chatgpt_geometry_sample(
            _ActionReader([_detailed_sidebar_snapshots()]), 123,
            _WindowServerFactory(_WindowServerProbe([{"bounds": (0, 0, 1200, 900)}])),
            sample_count=3, sleep_function=sleeper,
        )
        self.assertEqual(result["status"], "stable")
        self.assertLess(sum(sleeper.calls), 0.5)

    def test_open_chat_accepts_classic_combined_toolbar_identity(self):
        result = self._inspect_open_chat("Profile Photo Verification Change, PTG Assistant")
        self.assertTrue(result["confirmed"], result["reason"])

    def test_open_chat_rejects_combined_identity_for_another_project(self):
        result = self._inspect_open_chat("Profile Photo Verification Change, Different Project")
        self.assertFalse(result["confirmed"])

    def test_open_chat_rejects_combined_title_in_transcript_content(self):
        result = self._inspect_open_chat(
            "Profile Photo Verification Change, PTG Assistant", title_path="W.1.2.1.1.1.1.1.1",
        )
        self.assertFalse(result["confirmed"])

    def _inspect_open_chat(self, label, *, title_path="W.2.1.3"):
        # Classic replaces the project list with the conversation and exposes
        # its identity in the toolbar, without a separately selected chat row.
        snapshots = [
            nav.AXElementSnapshot(path="W", depth=0, role="AXWindow", title="PTG Assistant",
                                  frame=(0, 0, 1200, 900)),
            nav.AXElementSnapshot(path="W.1", depth=1, role="AXGroup", frame=(280, 52, 920, 848)),
            nav.AXElementSnapshot(path="W.1.1", depth=2, role="AXTextArea", title="Message ChatGPT",
                                  enabled=True, frame=(320, 820, 620, 44)),
            nav.AXElementSnapshot(path=title_path, depth=title_path.count("."), role="AXButton",
                                  description=label,
                                  frame=(300, 0, 400, 52) if title_path == "W.2.1.3" else (300, 200, 400, 52)),
        ]
        return nav._project_chat_post_action_inspection(
            _ActionReader([snapshots]), 123, "PTG Assistant", "Profile Photo Verification Change",
            {"matched_chat_row": {"row_path": "W.2.5.2", "title_path": "W.2.5.2.1"},
             "project_chat_resolution": {"chat_list_container": {"path": "W.2.5"}}},
            windowserver_probe_factory=_WindowServerFactory(
                _WindowServerProbe([{"bounds": (0, 0, 1200, 900)}])),
            sleep_function=_SleepRecorder(),
        )
