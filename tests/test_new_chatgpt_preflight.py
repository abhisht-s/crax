from __future__ import annotations

import unittest
from unittest import mock

from agent import mac_app_control, new_chatgpt_app
from agent.new_chatgpt_app import (
    AX_ERROR_NOT_IMPLEMENTED,
    NEW_CHATGPT_BUNDLE_ID,
    REASON_ACCESSIBILITY_NOT_TRUSTED,
    REASON_AX_ERROR,
    REASON_AX_NOT_ENABLED,
    REASON_MODE_UNKNOWN,
    REASON_NOT_RUNNING,
    REASON_OVERLAY_OPEN,
    REASON_WINDOW_NOT_ATTACHED,
    REASON_WRONG_MODE,
    preflight_new_chatgpt,
)
from tests.new_chatgpt_ax_fixtures import FakeBackend, Node, build_app, radix_menu


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def run(backend: FakeBackend, *, pid: int | None = 4242, activator=None, clock: FakeClock | None = None, **kwargs):
    clock = clock or FakeClock()
    activator = activator or mock.Mock(return_value={"activated": True})
    result = preflight_new_chatgpt(
        backend=backend,
        pid_resolver=lambda: pid,
        activator=activator,
        sleep=clock.sleep,
        clock=clock.clock,
        **kwargs,
    )
    return result, activator, clock


class NewChatGPTPreflightTests(unittest.TestCase):
    def test_bundle_id_constant_is_the_new_app(self) -> None:
        self.assertEqual(mac_app_control.NEW_CHATGPT_BUNDLE_ID, "com.openai.codex")
        self.assertEqual(NEW_CHATGPT_BUNDLE_ID, "com.openai.codex")
        self.assertNotEqual(NEW_CHATGPT_BUNDLE_ID, mac_app_control.CLASSIC_CHATGPT_BUNDLE_ID)

    def test_pid_is_resolved_by_new_bundle_id_only(self) -> None:
        with mock.patch("agent.chatgpt_ax_destination_snapshot._pid_for_bundle_id", return_value=77) as resolve:
            self.assertEqual(new_chatgpt_app.resolve_new_chatgpt_pid(), 77)
        resolve.assert_called_once_with("com.openai.codex")

    def test_happy_path_enables_enhanced_ui_despite_not_implemented_error(self) -> None:
        app, *_ = build_app()
        backend = FakeBackend(app)

        result, activator, _ = run(backend)

        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.reason_code)
        self.assertEqual(result.pid, 4242)
        self.assertEqual(result.enhanced_ui_set_error, AX_ERROR_NOT_IMPLEMENTED)
        self.assertTrue(result.enhanced_ui_enabled)
        self.assertTrue(result.web_content_attached)
        self.assertEqual(result.product_mode, "ChatGPT")
        self.assertFalse(result.activated)
        activator.assert_not_called()
        self.assertIn((app, "AXEnhancedUserInterface", True), backend.set_calls)

    def test_enhanced_ui_is_set_on_every_run(self) -> None:
        app, *_ = build_app()
        backend = FakeBackend(app)

        run(backend)
        run(backend)

        eui_sets = [call for call in backend.set_calls if call[1] == "AXEnhancedUserInterface"]
        self.assertEqual(len(eui_sets), 2)

    def test_live_regions_are_not_treated_as_overlays(self) -> None:
        app, *_ = build_app()
        backend = FakeBackend(app)

        result, _, _ = run(backend)

        self.assertTrue(result.ok)
        self.assertEqual(result.blocking_overlay_count, 0)
        self.assertEqual(result.nonblocking_overlay_count, 0)
        self.assertFalse(result.overlay_dismiss_attempted)

    def test_not_trusted_fails_closed(self) -> None:
        app, *_ = build_app()
        resolver = mock.Mock(return_value=1)

        result = preflight_new_chatgpt(backend=FakeBackend(app, trusted=False), pid_resolver=resolver, activator=mock.Mock())

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_ACCESSIBILITY_NOT_TRUSTED)
        resolver.assert_not_called()

    def test_not_running_fails_closed(self) -> None:
        app, *_ = build_app()

        result, activator, _ = run(FakeBackend(app), pid=None)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_NOT_RUNNING)
        activator.assert_not_called()

    def test_enhanced_ui_that_does_not_read_back_fails_closed(self) -> None:
        app, *_ = build_app()
        backend = FakeBackend(app, eui_set_error=-25205, eui_sticks=False)

        result, activator, _ = run(backend)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_AX_NOT_ENABLED)
        self.assertEqual(result.enhanced_ui_set_error, -25205)
        self.assertFalse(result.enhanced_ui_enabled)
        activator.assert_not_called()

    def test_detached_window_is_activated_by_bundle_id_then_attaches(self) -> None:
        app, web_area, *_ = build_app()
        attached_children = list(web_area.children)
        web_area.children = []
        clock = FakeClock()

        def attach_after_activation(bundle_id: str) -> dict:
            clock.now += 0.5
            web_area.children = attached_children
            return {"activated": True, "bundle_id": bundle_id}

        activator = mock.Mock(side_effect=attach_after_activation)
        result, _, _ = run(FakeBackend(app), activator=activator, clock=clock)

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.activated)
        activator.assert_called_once_with("com.openai.codex")
        self.assertEqual(result.activation, {"activated": True, "bundle_id": "com.openai.codex"})

    def test_window_that_never_attaches_times_out(self) -> None:
        app, *_ = build_app(attached=False)

        result, activator, clock = run(FakeBackend(app), attach_timeout_seconds=2.0, poll_interval_seconds=0.5)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_WINDOW_NOT_ATTACHED)
        activator.assert_called_once_with("com.openai.codex")
        self.assertEqual(result.attach_wait_seconds, 2.0)
        self.assertEqual(clock.sleeps, [0.5, 0.5, 0.5, 0.5])

    def test_web_area_still_loading_counts_as_not_attached(self) -> None:
        app, web_area, *_ = build_app()
        web_area.attrs["AXLoaded"] = False

        result, _, _ = run(FakeBackend(app), attach_timeout_seconds=0.5)

        self.assertEqual(result.reason_code, REASON_WINDOW_NOT_ATTACHED)

    def test_codex_mode_fails_closed(self) -> None:
        app, *_ = build_app(mode="Codex")

        result, _, _ = run(FakeBackend(app))

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_WRONG_MODE)
        self.assertEqual(result.product_mode, "Codex")

    def test_missing_mode_switcher_fails_closed(self) -> None:
        app, *_ = build_app(mode=None)

        result, _, _ = run(FakeBackend(app))

        self.assertEqual(result.reason_code, REASON_MODE_UNKNOWN)
        self.assertIsNone(result.product_mode)

    def test_missing_sidebar_fails_closed(self) -> None:
        app, *_ = build_app(with_sidebar=False)

        result, _, _ = run(FakeBackend(app))

        self.assertEqual(result.reason_code, REASON_MODE_UNKNOWN)
        self.assertFalse(result.details["sidebar_found"])

    def test_open_menu_is_dismissed_by_focusing_the_composer(self) -> None:
        menu = radix_menu()
        app, _, top_group, composer = build_app(overlays=[menu])
        backend = FakeBackend(app)
        backend.on_focus_composer = lambda: top_group.children.remove(menu)

        result, _, clock = run(backend)

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.overlay_dismiss_attempted)
        self.assertEqual(result.blocking_overlay_count, 0)
        self.assertIn((composer, "AXFocused", True), backend.set_calls)
        focused = [call[0] for call in backend.set_calls if call[1] == "AXFocused"]
        self.assertEqual(focused, [composer])  # never the search box or anything else
        self.assertTrue(clock.sleeps)

    def test_menu_that_stays_open_fails_closed(self) -> None:
        app, *_ = build_app(overlays=[radix_menu()])

        result, _, _ = run(FakeBackend(app))

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_OVERLAY_OPEN)
        self.assertEqual(result.blocking_overlay_count, 1)

    def test_open_dialog_is_blocking(self) -> None:
        dialog = Node("AXGroup", [Node("AXButton", AXTitle="Keep thinking")], AXSubrole="AXApplicationDialog")
        app, *_ = build_app(overlays=[dialog])

        result, _, _ = run(FakeBackend(app))

        self.assertEqual(result.reason_code, REASON_OVERLAY_OPEN)

    def test_empty_layout_layers_seen_live_do_not_block(self) -> None:
        # Shapes observed on the live app (2026-11-20): three AXEmptyGroup siblings of #root.
        layer = Node("AXGroup", [Node("AXGroup")], AXSubrole="AXEmptyGroup", AXDOMClassList=["pointer-events-none", "fixed", "inset-0", "overflow-visible"])
        wrapper = Node("AXGroup", [Node("AXGroup")], AXSubrole="AXEmptyGroup", AXDOMClassList=["contents"])
        empty = Node("AXGroup", AXSubrole="AXEmptyGroup", AXDOMClassList=["contents"])
        app, *_ = build_app(overlays=[layer, wrapper, empty])
        backend = FakeBackend(app)

        result, _, _ = run(backend)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.nonblocking_overlay_count, 3)
        self.assertFalse(result.overlay_dismiss_attempted)

    def test_menu_nested_deep_inside_a_layout_layer_is_blocking(self) -> None:
        menu = Node("AXMenu", [Node("AXMenuItem", AXTitle="Codex Build, debug, and ship")])
        nested = menu
        for _ in range(8):
            nested = Node("AXGroup", [nested])
        layer = Node("AXGroup", [nested], AXSubrole="AXEmptyGroup", AXDOMClassList=["pointer-events-none", "fixed", "inset-0"])
        app, *_ = build_app(overlays=[layer])

        result, _, _ = run(FakeBackend(app))

        self.assertEqual(result.reason_code, REASON_OVERLAY_OPEN)

    def test_unknown_non_menu_portal_does_not_block(self) -> None:
        toast = Node("AXGroup", [Node("AXStaticText", AXValue="Copied")])
        app, *_ = build_app(overlays=[toast])
        backend = FakeBackend(app)

        result, _, _ = run(backend)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.nonblocking_overlay_count, 1)
        summary = result.details["nonblocking_overlays"][0]
        self.assertEqual(summary["role"], "AXGroup")
        self.assertEqual(summary["child_roles"], ["AXStaticText"])
        self.assertNotIn("Copied", repr(result.to_dict()))  # structure only, never text
        self.assertFalse(result.overlay_dismiss_attempted)
        self.assertFalse([call for call in backend.set_calls if call[1] == "AXFocused"])

    def test_backend_exception_fails_closed(self) -> None:
        app, *_ = build_app()
        backend = FakeBackend(app)
        backend.attribute = mock.Mock(side_effect=OSError("AX messaging timeout"))

        result, _, _ = run(backend)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_AX_ERROR)
        self.assertIn("AX messaging timeout", result.error)

    def test_result_serializes_without_handles(self) -> None:
        app, *_ = build_app()

        result, _, _ = run(FakeBackend(app))
        data = result.to_dict()

        self.assertEqual(data["method"], "ax_preflight_new_chatgpt")
        self.assertEqual(data["bundle_id"], "com.openai.codex")
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()


class BringToFrontPreflightTests(unittest.TestCase):
    def test_brings_attached_background_app_to_front(self) -> None:
        app, *_ = build_app()
        app.attrs["AXFrontmost"] = False

        def activate(bundle_id: str) -> dict:
            app.attrs["AXFrontmost"] = True
            return {"activated": True, "bundle_id": bundle_id}

        result, activator, _ = run(FakeBackend(app), activator=mock.Mock(side_effect=activate), bring_to_front=True)

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.activated)
        self.assertTrue(result.frontmost)
        activator.assert_called_once_with("com.openai.codex")

    def test_already_frontmost_app_is_not_reactivated(self) -> None:
        app, *_ = build_app()
        app.attrs["AXFrontmost"] = True

        result, activator, _ = run(FakeBackend(app), bring_to_front=True)

        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.activated)
        activator.assert_not_called()

    def test_app_that_never_comes_to_front_fails_closed(self) -> None:
        app, *_ = build_app()
        app.attrs["AXFrontmost"] = False

        result, activator, _ = run(FakeBackend(app), bring_to_front=True, attach_timeout_seconds=1.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "new_chatgpt_not_frontmost")
        activator.assert_called_once()

    def test_default_does_not_change_focus(self) -> None:
        app, *_ = build_app()
        app.attrs["AXFrontmost"] = False

        result, activator, _ = run(FakeBackend(app))

        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.frontmost)
        activator.assert_not_called()
