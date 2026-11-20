from __future__ import annotations

import unittest

from agent.new_chatgpt_app import PreflightResult
from agent.new_chatgpt_navigation import (
    OUTCOME_ALREADY_OPEN,
    OUTCOME_DRY_RUN_NEEDS_PRESS,
    OUTCOME_DRY_RUN_WOULD_OPEN,
    OUTCOME_OPENED,
    REASON_CHAT_AMBIGUOUS,
    REASON_CHAT_NOT_FOUND,
    REASON_INVALID_BINDING,
    REASON_NOT_CONFIRMED,
    REASON_PRESS_FAILED,
    REASON_PROJECT_AMBIGUOUS,
    REASON_PROJECT_NOT_FOUND,
    REASON_PROJECTS_SECTION_NOT_FOUND,
    open_project_chat,
)
from tests.new_chatgpt_ax_fixtures import FakeBackend, FakeProject, SidebarModel, build_app_tree


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def ok_preflight(**_kwargs) -> PreflightResult:
    return PreflightResult(ok=True, pid=4242)


def setup(model: SidebarModel, **tree_kwargs):
    tree = build_app_tree(sidebar_model=model, **tree_kwargs)
    backend = tree.wire(FakeBackend(tree.app))
    return tree, backend


def navigate(backend, project="<project>", chat="<chat 2>", **kwargs):
    clock = FakeClock()
    kwargs.setdefault("preflight", ok_preflight)
    return open_project_chat(project, chat, backend=backend, sleep=clock.sleep, clock=clock.clock, **kwargs)


def pressed_labels(backend) -> list[str]:
    return [node.attrs.get("AXDescription") or node.attrs.get("AXTitle") for node, action in backend.actions if action == "AXPress"]


def two_projects(**model_kwargs) -> SidebarModel:
    return SidebarModel(
        [
            FakeProject("<project>", ["<chat 0>", "<chat 1>", "<chat 2>"]),
            FakeProject("<other project>", ["<chat 2>", "<chat 9>"]),
        ],
        **{"current": ("<project>", "<chat 0>"), "recents": ["<chat 2>", "<chat 0>"], "pinned_chats": ["<chat 2>"], **model_kwargs},
    )


class OpenProjectChatTests(unittest.TestCase):
    def test_opens_chat_from_its_project_list_and_confirms(self) -> None:
        tree, backend = setup(two_projects())

        result = navigate(backend)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.outcome, OUTCOME_OPENED)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.presses, ["chat"])
        self.assertEqual(tree.sidebar_model.current, ("<project>", "<chat 2>"))
        self.assertEqual(tree.web_area.attrs["AXTitle"], "<chat 2>")

    def test_only_the_row_inside_the_bound_project_is_pressed(self) -> None:
        # "<chat 2>" also exists under Pinned, Recents and another project.
        tree, backend = setup(two_projects())

        navigate(backend)

        pressed = [node for node, action in backend.actions if action == "AXPress"]
        self.assertEqual(len(pressed), 1)
        self.assertEqual(tree.sidebar_model.current[0], "<project>")

    def test_already_open_chat_is_not_pressed(self) -> None:
        _, backend = setup(two_projects())

        result = navigate(backend, chat="<chat 0>")

        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, OUTCOME_ALREADY_OPEN)
        self.assertEqual(backend.actions, [])

    def test_collapsed_project_is_expanded_first(self) -> None:
        model = two_projects()
        model.projects[0].expanded = False
        tree, backend = setup(model)

        result = navigate(backend)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.presses, ["project", "chat"])
        self.assertEqual(pressed_labels(backend), ["<project>", "<chat 2>"])

    def test_collapsed_projects_section_is_expanded_first(self) -> None:
        _, backend = setup(two_projects(section_expanded=False))

        result = navigate(backend)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.presses, ["projects_section", "chat"])

    def test_show_more_is_pressed_until_the_chat_appears(self) -> None:
        chats = [f"<chat {i}>" for i in range(12)]
        model = SidebarModel([FakeProject("<project>", chats, shown=5)], current=("<project>", "<chat 0>"))
        _, backend = setup(model)

        result = navigate(backend, chat="<chat 11>")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.show_more_presses, 2)
        self.assertEqual(result.presses, ["chats_show_more", "chats_show_more", "chat"])

    def test_show_more_presses_are_capped(self) -> None:
        chats = [f"<chat {i}>" for i in range(40)]
        model = SidebarModel([FakeProject("<project>", chats, shown=5)], current=("<project>", "<chat 0>"))
        _, backend = setup(model)

        result = navigate(backend, chat="<chat 39>", max_show_more_presses=2)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_CHAT_NOT_FOUND)
        self.assertEqual(result.show_more_presses, 2)

    def test_project_hidden_behind_projects_show_more(self) -> None:
        projects = [FakeProject(f"<project {i}>", ["<chat a>"], expanded=False) for i in range(8)]
        model = SidebarModel(projects, projects_shown=5)
        _, backend = setup(model)

        result = navigate(backend, project="<project 7>", chat="<chat a>")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.presses, ["projects_show_more", "project", "chat"])

    def test_pinned_project_copy_is_ignored(self) -> None:
        model = two_projects(pinned_projects=["<project>"])
        _, backend = setup(model)

        result = navigate(backend)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.project_row_count, 1)

    def test_missing_project_fails_closed(self) -> None:
        _, backend = setup(two_projects())

        result = navigate(backend, project="<no such project>")

        self.assertEqual(result.reason_code, REASON_PROJECT_NOT_FOUND)
        self.assertEqual(backend.actions, [])

    def test_missing_chat_fails_closed(self) -> None:
        _, backend = setup(two_projects())

        result = navigate(backend, chat="<chat 9>")  # exists, but only in the other project

        self.assertEqual(result.reason_code, REASON_CHAT_NOT_FOUND)
        self.assertEqual(backend.actions, [])

    def test_duplicate_chat_titles_in_one_project_fail_closed(self) -> None:
        model = SidebarModel([FakeProject("<project>", ["<dup>", "<dup>"])])
        _, backend = setup(model)

        result = navigate(backend, chat="<dup>")

        self.assertEqual(result.reason_code, REASON_CHAT_AMBIGUOUS)
        self.assertEqual(result.chat_row_count, 2)
        self.assertEqual(backend.actions, [])

    def test_duplicate_project_names_fail_closed(self) -> None:
        model = SidebarModel([FakeProject("<project>", ["<a>"]), FakeProject("<project>", ["<b>"])])
        _, backend = setup(model)

        result = navigate(backend, chat="<a>")

        self.assertEqual(result.reason_code, REASON_PROJECT_AMBIGUOUS)

    def test_no_projects_section_fails_closed(self) -> None:
        _, backend = setup(two_projects())
        nav = next(node for node in backend.app.walk() if node.attrs.get("AXDescription") == "Home")
        for node in nav.walk():
            if node.attrs.get("AXTitle") == "Projects":
                node.attrs["AXTitle"] = node.attrs["AXDescription"] = "Something else"

        result = navigate(backend)

        self.assertEqual(result.reason_code, REASON_PROJECTS_SECTION_NOT_FOUND)

    def test_press_without_effect_is_not_confirmed(self) -> None:
        # e.g. AXEnhancedUserInterface silently off: AXPress returns 0 but nothing happens.
        _, backend = setup(two_projects(rows_respond=False))

        result = navigate(backend, confirm_timeout_seconds=1.0)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_NOT_CONFIRMED)
        self.assertEqual(result.presses, ["chat"])
        self.assertGreaterEqual(result.confirm_wait_seconds, 1.0)

    def test_press_error_fails_closed(self) -> None:
        _, backend = setup(two_projects())
        backend.on_press = lambda node: -25200

        result = navigate(backend)

        self.assertEqual(result.reason_code, REASON_PRESS_FAILED)

    def test_dry_run_presses_nothing(self) -> None:
        tree, backend = setup(two_projects())

        result = navigate(backend, dry_run=True)

        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, OUTCOME_DRY_RUN_WOULD_OPEN)
        self.assertEqual(backend.actions, [])
        self.assertEqual(tree.sidebar_model.current, ("<project>", "<chat 0>"))

    def test_dry_run_reports_a_needed_reveal_press(self) -> None:
        model = two_projects()
        model.projects[0].expanded = False
        _, backend = setup(model)

        result = navigate(backend, dry_run=True)

        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, OUTCOME_DRY_RUN_NEEDS_PRESS)
        self.assertEqual(result.presses, ["would_press:project"])
        self.assertEqual(backend.actions, [])

    def test_failed_preflight_stops_before_reading(self) -> None:
        _, backend = setup(two_projects())

        result = navigate(backend, preflight=lambda **_: PreflightResult(ok=False, reason_code="new_chatgpt_wrong_mode", error="Codex mode"))

        self.assertEqual(result.reason_code, "new_chatgpt_wrong_mode")
        self.assertEqual(backend.read_nodes, [])
        self.assertEqual(backend.actions, [])

    def test_blank_binding_is_rejected(self) -> None:
        _, backend = setup(two_projects())

        result = navigate(backend, project="  ", chat="<chat 2>")

        self.assertEqual(result.reason_code, REASON_INVALID_BINDING)


if __name__ == "__main__":
    unittest.main()


class BringToFrontTests(unittest.TestCase):
    """Live finding 2026-11-20: presses work in the background, but the AX tree
    only updates while the window is on screen."""

    def _real_preflight(self, tree):
        import functools

        from agent.new_chatgpt_app import preflight_new_chatgpt

        tree.app.attrs["AXFrontmost"] = False
        return functools.partial(preflight_new_chatgpt, pid_resolver=lambda: 4242, activator=tree.bring_to_front)

    def test_background_press_is_unconfirmed_without_bringing_to_front(self) -> None:
        tree = build_app_tree(sidebar_model=two_projects())
        backend = tree.wire(FakeBackend(tree.app), stale_in_background=True)

        result = navigate(backend, preflight=self._real_preflight(tree), bring_to_front=False, confirm_timeout_seconds=1.0)

        self.assertEqual(result.reason_code, REASON_NOT_CONFIRMED)
        self.assertEqual(tree.sidebar_model.current, ("<project>", "<chat 2>"))  # the press did work

    def test_real_run_brings_app_to_front_and_confirms(self) -> None:
        tree = build_app_tree(sidebar_model=two_projects())
        backend = tree.wire(FakeBackend(tree.app), stale_in_background=True)

        result = navigate(backend, preflight=self._real_preflight(tree))

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.outcome, OUTCOME_OPENED)
        self.assertTrue(result.preflight["bring_to_front"])
        self.assertTrue(result.preflight["frontmost"])
        self.assertTrue(result.preflight["activated"])

    def test_dry_run_does_not_change_focus(self) -> None:
        tree = build_app_tree(sidebar_model=two_projects())
        backend = tree.wire(FakeBackend(tree.app), stale_in_background=True)

        result = navigate(backend, preflight=self._real_preflight(tree), dry_run=True)

        self.assertTrue(result.ok)
        self.assertFalse(result.preflight["bring_to_front"])
        self.assertFalse(result.preflight["activated"])
        self.assertFalse(tree.app.attrs["AXFrontmost"])
