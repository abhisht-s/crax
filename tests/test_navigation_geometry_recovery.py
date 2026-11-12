from __future__ import annotations

from dataclasses import replace
import unittest
from unittest import mock

from agent import chatgpt_navigation_diagnostic as nav
from agent.supervision_services import _navigation_attempt_from_open_result
from tests.test_chatgpt_navigation_diagnostic import (
    _ActionFactory, _ClickFactory, _ClickService, _DisplayFactory, _DisplayProbe,
    _SleepRecorder, _WindowServerFactory, _WindowServerProbe,
    _detailed_sidebar_snapshots, _project_visible_chats_snapshots,
    _project_chat_opened_snapshots,
)


class RecoveryReader:
    def __init__(self, *, snapshots=None, fail_press=False, unchanged=False, first_missing=False):
        self.snapshots = snapshots or _detailed_sidebar_snapshots()
        self.fail_press = fail_press
        self.unchanged = unchanged
        self.first_missing = first_missing
        self.collect_calls = 0
        self.actions = []

    def collect(self, pid):
        self.collect_calls += 1
        snapshots = [] if self.first_missing and self.collect_calls == 1 else self.snapshots
        return snapshots, {"visited_nodes": len(snapshots)}, {"window": snapshots[0] if snapshots else None}

    def perform_action(self, path, action):
        self.actions.append((path, action))
        if self.fail_press:
            return False
        if not self.unchanged:
            self.snapshots = (_project_chat_opened_snapshots() if path == "W.2.5.2"
                              else _project_visible_chats_snapshots())
        return True


class NavigationGeometryRecoveryTests(unittest.TestCase):
    def open(self, reader, *, project_chat=False, windows=None):
        self.clicker = _ClickService()
        kwargs = dict(
            process_resolver=lambda name: nav.ProcessResolution(pid=123, method="fake"),
            activation_function=lambda name: {"activated": True, "is_frontmost": True},
            reader_factory=_ActionFactory(reader),
            windowserver_probe_factory=windows or _WindowServerFactory(_WindowServerProbe([])),
            display_probe_factory=_DisplayFactory(_DisplayProbe()),
            click_service_factory=_ClickFactory(self.clicker),
            sleep_function=_SleepRecorder(),
        )
        with mock.patch.object(nav.sys, "platform", "darwin"):
            if project_chat:
                return nav.open_chatgpt_project_chat(project_title="PTG Assistant",
                    chat_title="Profile Photo Verification Change", confirm_open_chat=True, **kwargs)
            return nav.open_chatgpt_sidebar_destination(kind="project", title="PTG Assistant",
                                                       confirm_open_destination=True, **kwargs)

    def test_missing_windowserver_bounds_still_opens_exact_project_and_chat(self):
        reader = RecoveryReader()
        result = self.open(reader, project_chat=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["post_action_evidence"]["confirmed"])
        self.assertTrue(result["chat_open_action_posted"])
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress"), ("W.2.5.2", "AXPress")])
        self.assertEqual(self.clicker.clicks, [])
        project = result["project_open_result"]
        self.assertEqual(project["activation_stability_status"], "unstable")
        self.assertTrue(project["axpress_geometry_fallback"])
        self.assertIn("unavailable", project["activation_stability_error"])
        diagnostics = _navigation_attempt_from_open_result(result).navigation_action_diagnostics
        self.assertTrue(diagnostics["project_open_axpress_geometry_fallback"])
        self.assertIn("unavailable", diagnostics["project_open_stability_error"])

    def test_missing_ax_window_frame_does_not_prevent_project_press(self):
        snapshots = [replace(s, frame=None) if s.path == "W" else s for s in _detailed_sidebar_snapshots()]
        reader = RecoveryReader(snapshots=snapshots)
        result = self.open(reader)
        self.assertTrue(result["ok"])
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(self.clicker.clicks, [])

    def test_first_missing_tree_is_read_again_before_press(self):
        reader = RecoveryReader(first_missing=True)
        self.assertTrue(self.open(reader)["ok"])
        self.assertGreater(reader.collect_calls, 1)
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])

    def test_transient_read_error_during_ax_fallback_recovers(self):
        reader = RecoveryReader()
        collect = reader.collect
        def temporary_failure(pid):
            if reader.collect_calls == 1:
                reader.collect_calls += 1
                raise RuntimeError("Temporary Accessibility read failure")
            return collect(pid)
        reader.collect = temporary_failure
        result = self.open(reader)
        self.assertTrue(result["ok"])
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(self.clicker.clicks, [])

    def test_windowserver_exception_does_not_prevent_axpress(self):
        def unavailable():
            raise RuntimeError("WindowServer probe unavailable")
        reader = RecoveryReader()
        result = self.open(reader, windows=unavailable)
        self.assertTrue(result["ok"])
        self.assertIn("probe unavailable", result["activation_stability"]["error"])
        self.assertEqual(self.clicker.clicks, [])

    def test_changed_windowserver_bounds_do_not_prevent_axpress(self):
        class MovingWindows:
            calls = 0
            def visible_windows_for_pid(self, pid):
                self.calls += 1
                return [{"bounds": (self.calls * 50, 0, 1200, 900)}]
        reader = RecoveryReader()
        probe = MovingWindows()
        result = self.open(reader, windows=lambda: probe)
        self.assertTrue(result["ok"])
        self.assertIn("changed", result["activation_stability"]["error"])
        self.assertEqual(self.clicker.clicks, [])

    def test_failed_press_never_falls_back_to_unverified_coordinates(self):
        reader = RecoveryReader(fail_press=True)
        result = self.open(reader)
        self.assertFalse(result["ok"])
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(self.clicker.clicks, [])

    def test_successful_press_without_destination_evidence_is_not_success(self):
        reader = RecoveryReader(unchanged=True)
        result = self.open(reader)
        self.assertFalse(result["ok"])
        self.assertFalse(result["post_action_evidence"]["confirmed"])
        self.assertEqual(self.clicker.clicks, [])

    def test_missing_disabled_ambiguous_or_menu_only_controls_are_never_pressed(self):
        base = _detailed_sidebar_snapshots()
        cases = {
            "missing": [replace(s, value="Different project") if s.path == "W.1.3.1" else s for s in base],
            "disabled": [replace(s, enabled=False) if s.path == "W.1.3.3" else s for s in base],
            "menu_only": [replace(s, actions=("AXShowMenu",)) if s.path == "W.1.3.3" else s for s in base],
            "ambiguous": base + [nav.AXElementSnapshot(path="W.1.4", depth=2, role="AXButton",
                title="PTG Assistant", actions=("AXPress",), enabled=True, frame=(10, 150, 250, 32))],
        }
        for name, snapshots in cases.items():
            with self.subTest(name=name):
                reader = RecoveryReader(snapshots=snapshots)
                result = self.open(reader)
                self.assertFalse(result["ok"])
                self.assertEqual(reader.actions, [])
                self.assertEqual(self.clicker.clicks, [])

    def test_unreadable_tree_never_posts_an_action(self):
        reader = RecoveryReader()
        reader.collect = mock.Mock(return_value=([], {}, {}))
        result = self.open(reader)
        self.assertFalse(result["ok"])
        self.assertEqual(reader.actions, [])
        self.assertEqual(self.clicker.clicks, [])

    def test_unstable_geometry_dry_run_never_activates_or_presses(self):
        reader = RecoveryReader()
        activator = mock.Mock()
        with mock.patch.object(nav.sys, "platform", "darwin"):
            result = nav.open_chatgpt_sidebar_destination(
                kind="project", title="PTG Assistant", confirm_open_destination=False,
                activation_function=activator,
                process_resolver=lambda name: nav.ProcessResolution(pid=123, method="fake"),
                reader_factory=_ActionFactory(reader),
                windowserver_probe_factory=_WindowServerFactory(_WindowServerProbe([])),
                sleep_function=_SleepRecorder(),
            )
        self.assertEqual(result["outcome"], "dry_run_ready")
        activator.assert_not_called()
        self.assertEqual(reader.actions, [])

    def test_truncated_tree_cannot_prove_unique_press_target(self):
        reader = RecoveryReader()
        collect = reader.collect
        def truncated(pid):
            snaps, stats, metadata = collect(pid)
            return snaps, {**stats, "truncated_by_node_limit": True}, metadata
        reader.collect = truncated
        result = self.open(reader)
        self.assertFalse(result["ok"])
        self.assertEqual(reader.actions, [])
        self.assertIn("truncated", result["error"])

    def test_missing_bounds_do_not_allow_mouse_click_for_chat_without_press(self):
        reader = RecoveryReader()
        original = reader.perform_action
        def press(path, action):
            ok = original(path, action)
            reader.snapshots = [replace(s, actions=()) if s.path == "W.2.5.2" else s
                                for s in reader.snapshots]
            return ok
        reader.perform_action = press
        result = self.open(reader, project_chat=True)
        self.assertFalse(result["chat_open_action_posted"])
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress")])
        self.assertEqual(self.clicker.clicks, [])

    def test_failed_chat_press_does_not_fall_back_to_coordinates(self):
        reader = RecoveryReader()
        original = reader.perform_action
        def press(path, action):
            if path == "W.2.5.2":
                reader.actions.append((path, action))
                return False
            return original(path, action)
        reader.perform_action = press
        result = self.open(reader, project_chat=True)
        self.assertFalse(result["chat_open_action_posted"])
        self.assertEqual(reader.actions, [("W.1.3.3", "AXPress"), ("W.2.5.2", "AXPress")])
        self.assertEqual(self.clicker.clicks, [])


if __name__ == "__main__":
    unittest.main()
