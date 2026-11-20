"""Step 0 of docs/new-chatgpt-app/MIGRATION_PLAN.md.

Two kinds of tests:

1. Characterization: pin how the loop reaches ChatGPT **today** (Classic), so
   Step 8's target dispatch can prove Classic is unchanged.
2. The target setting itself (agent/chatgpt_target.py).
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from agent import chatgpt_target, supervision_services
from agent.chatgpt_ax_destination_snapshot import ChatGPTAXDestinationSnapshotAdapter
from agent.chatgpt_desktop_mutex import NullChatGPTDesktopMutex, capture_mutex_identity
from agent.chatgpt_target import (
    CHATGPT_TARGET_CLASSIC,
    CHATGPT_TARGET_EVENT_TYPE,
    CHATGPT_TARGET_NEW,
    ChatGPTTargetError,
    ChatGPTTargetSelection,
    classic_target_services,
    ensure_run_chatgpt_target,
    get_run_chatgpt_target,
    record_run_chatgpt_target,
    resolve_chatgpt_target,
    services_for_target,
)
from agent.run_services import RunDestinationBinding


class ClassicLoopWiringCharacterizationTests(unittest.TestCase):
    """How run_supervision_step reaches ChatGPT before any target dispatch exists."""

    def test_run_supervision_step_defaults_are_the_classic_services(self) -> None:
        params = inspect.signature(supervision_services.run_supervision_step).parameters

        self.assertIs(params["submit_service"].default, supervision_services._default_submit_service)
        self.assertIs(params["capture_service"].default, supervision_services._default_capture_service)
        self.assertIs(params["destination_adapter_factory"].default, supervision_services._default_destination_adapter_factory)
        self.assertIs(params["destination_navigation_service"].default, supervision_services._default_destination_navigation_service)
        self.assertEqual(params["app_name"].default, "ChatGPT")
        self.assertIs(params["allow_destination_navigation"].default, False)

    def test_default_submit_service_forwards_to_the_classic_submit_flow(self) -> None:
        stop = lambda: False  # noqa: E731
        with mock.patch.object(supervision_services, "submit_feedback_to_chatgpt_service", return_value="sentinel") as submit:
            result = supervision_services._default_submit_service(
                "run-1", {"id": "run-1"}, "ChatGPT", approval_mode="auto", ledger="ledger", should_stop=stop
            )

        self.assertEqual(result, "sentinel")
        submit.assert_called_once_with(
            "run-1", {"id": "run-1"}, app_name="ChatGPT", approval_mode="auto", ledger="ledger", should_stop=stop
        )

    def test_default_capture_service_forwards_and_drops_the_timeout(self) -> None:
        # Current behavior: the capture timeout passed in is discarded (timeout_seconds=None).
        with mock.patch.object(supervision_services, "capture_chatgpt_response_service", return_value="sentinel") as capture:
            result = supervision_services._default_capture_service(
                "run-1", {"id": "run-1"}, "ChatGPT", 42.0, 3.0, require_sentinel_response=True, ledger="ledger"
            )

        self.assertEqual(result, "sentinel")
        capture.assert_called_once_with(
            "run-1",
            app_name="ChatGPT",
            timeout_seconds=None,
            stable_seconds=3.0,
            require_sentinel_response=True,
            ledger="ledger",
            should_stop=None,
        )

    def test_default_destination_adapter_is_the_classic_adapter(self) -> None:
        self.assertIsInstance(supervision_services._default_destination_adapter_factory(), ChatGPTAXDestinationSnapshotAdapter)

    def test_default_navigation_uses_the_classic_navigator(self) -> None:
        binding = RunDestinationBinding(project_title="<project>", chat_title="<chat>")
        with mock.patch("agent.chatgpt_navigation_diagnostic.open_chatgpt_project_chat", return_value={}) as opener:
            supervision_services._default_destination_navigation_service(
                "run-1", binding, None, app_name="ChatGPT", ledger="ledger"
            )

        opener.assert_called_once_with(
            project_title="<project>", chat_title="<chat>", confirm_open_chat=True, app_name="ChatGPT"
        )

    def test_local_controller_passes_no_chatgpt_services(self) -> None:
        # The dashboard loop relies on run_supervision_step's defaults for every ChatGPT service.
        from agent.local_controller import LocalController
        from tests.test_local_controller import FakeLedger, ReadModelSequence, _model

        controller = LocalController(
            ledger=FakeLedger(),
            read_model_builder=ReadModelSequence(_model(action="stop", reason="no_action", stage="idle")),
            desktop_mutex=NullChatGPTDesktopMutex(),
        )

        kwargs = controller._supervision_handoff_kwargs("run-1")

        self.assertEqual(set(kwargs), {"desktop_mutex", "controller_instance_id", "should_stop"})

    def test_classic_target_services_are_exactly_todays_defaults(self) -> None:
        services = classic_target_services()
        params = inspect.signature(supervision_services.run_supervision_step).parameters

        for name, service in services.as_step_kwargs().items():
            self.assertIs(service, params[name].default, name)
        self.assertEqual(services.target, "classic")


class ResolveTargetTests(unittest.TestCase):
    def test_default_is_classic(self) -> None:
        self.assertEqual(resolve_chatgpt_target(environ={}), ChatGPTTargetSelection("classic", "default"))

    def test_env_var(self) -> None:
        self.assertEqual(resolve_chatgpt_target(environ={"CRAX_CHATGPT_APP": "new"}), ChatGPTTargetSelection("new", "env"))
        self.assertEqual(resolve_chatgpt_target(environ={"CRAX_CHATGPT_APP": "  "}).source, "default")

    def test_explicit_wins_over_env(self) -> None:
        selection = resolve_chatgpt_target("Classic", environ={"CRAX_CHATGPT_APP": "new"})

        self.assertEqual(selection, ChatGPTTargetSelection("classic", "explicit"))

    def test_invalid_values_are_errors_not_fallbacks(self) -> None:
        for bad in ("codex", "chatgpt", ""):
            with self.subTest(bad=bad), self.assertRaises(ChatGPTTargetError) as ctx:
                resolve_chatgpt_target(bad, environ={})
            self.assertEqual(ctx.exception.reason_code, "chatgpt_target_invalid")
        with self.assertRaises(ChatGPTTargetError):
            resolve_chatgpt_target(environ={"CRAX_CHATGPT_APP": "codex"})

    def test_bundle_ids(self) -> None:
        self.assertEqual(ChatGPTTargetSelection("classic", "default").bundle_id, "com.openai.chat")
        self.assertEqual(ChatGPTTargetSelection("new", "env").bundle_id, "com.openai.codex")


class EventLedger:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = list(events or [])

    def list_events(self, run_id: str) -> list[dict]:
        return [event for event in self.events if event.get("run_id", run_id) == run_id]

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> None:
        self.events.append({"run_id": run_id, "event_type": event_type, "message": message, "metadata": metadata})


class RunTargetRecordTests(unittest.TestCase):
    def test_runs_without_a_target_event_are_classic(self) -> None:
        ledger = EventLedger([{"event_type": "run_started", "metadata": {}}])

        selection = get_run_chatgpt_target("run-1", ledger=ledger)

        self.assertEqual(selection.target, CHATGPT_TARGET_CLASSIC)
        self.assertEqual(selection.source, "legacy_no_event")

    def test_record_once_then_read_back(self) -> None:
        ledger = EventLedger()

        wrote = record_run_chatgpt_target("run-1", ChatGPTTargetSelection("new", "explicit"), ledger=ledger)

        self.assertTrue(wrote)
        self.assertEqual(get_run_chatgpt_target("run-1", ledger=ledger).target, CHATGPT_TARGET_NEW)
        event = ledger.events[0]
        self.assertEqual(event["event_type"], CHATGPT_TARGET_EVENT_TYPE)
        self.assertEqual(
            event["metadata"],
            {"chatgpt_target": "new", "chatgpt_target_source": "explicit", "chatgpt_bundle_id": "com.openai.codex"},
        )

    def test_recording_the_same_target_again_is_a_no_op(self) -> None:
        ledger = EventLedger()
        record_run_chatgpt_target("run-1", ChatGPTTargetSelection("classic", "default"), ledger=ledger)

        wrote = record_run_chatgpt_target("run-1", ChatGPTTargetSelection("classic", "env"), ledger=ledger)

        self.assertFalse(wrote)
        self.assertEqual(len(ledger.events), 1)

    def test_a_run_never_switches_apps(self) -> None:
        ledger = EventLedger()
        record_run_chatgpt_target("run-1", ChatGPTTargetSelection("classic", "default"), ledger=ledger)

        with self.assertRaises(ChatGPTTargetError) as ctx:
            record_run_chatgpt_target("run-1", ChatGPTTargetSelection("new", "explicit"), ledger=ledger)

        self.assertEqual(ctx.exception.reason_code, "chatgpt_target_change_refused")
        self.assertEqual(len(ledger.events), 1)

    def test_contradictory_or_malformed_events_fail_closed(self) -> None:
        contradictory = EventLedger(
            [
                {"event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "classic"}},
                {"event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "new"}},
            ]
        )
        malformed = EventLedger([{"event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata_json": '{"chatgpt_target": 7}'}])

        with self.assertRaises(ChatGPTTargetError) as first:
            get_run_chatgpt_target("run-1", ledger=contradictory)
        with self.assertRaises(ChatGPTTargetError) as second:
            get_run_chatgpt_target("run-1", ledger=malformed)

        self.assertEqual(first.exception.reason_code, "chatgpt_target_contradictory")
        self.assertEqual(second.exception.reason_code, "chatgpt_target_event_malformed")

    def test_metadata_json_events_from_the_real_ledger_shape(self) -> None:
        ledger = EventLedger([{"event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata_json": '{"chatgpt_target": "new"}'}])

        self.assertEqual(get_run_chatgpt_target("run-1", ledger=ledger).target, "new")


class ServicesForTargetTests(unittest.TestCase):
    def test_classic_services(self) -> None:
        self.assertEqual(services_for_target("classic"), classic_target_services())

    def test_new_app_services_are_wired(self) -> None:
        from agent.new_chatgpt_loop import new_target_services

        self.assertEqual(services_for_target("new"), new_target_services())
        self.assertNotEqual(services_for_target("new"), classic_target_services())


class EnsureRunTargetTests(unittest.TestCase):
    def test_classic_selection_writes_nothing(self) -> None:
        ledger = EventLedger([{"event_type": "run_started", "metadata": {}}])

        selection = ensure_run_chatgpt_target("run-1", ChatGPTTargetSelection("classic", "explicit"), ledger=ledger)

        self.assertEqual(selection.target, "classic")
        self.assertEqual(len(ledger.events), 1)

    def test_new_selection_is_recorded_for_a_fresh_run(self) -> None:
        ledger = EventLedger([{"event_type": "run_started", "metadata": {}}])

        selection = ensure_run_chatgpt_target("run-1", ChatGPTTargetSelection("new", "explicit"), ledger=ledger)

        self.assertEqual(selection.target, "new")
        self.assertEqual(ledger.events[-1]["event_type"], CHATGPT_TARGET_EVENT_TYPE)

    def test_run_that_already_used_classic_cannot_switch(self) -> None:
        ledger = EventLedger([{"event_type": "gpt_feedback_submission_verified", "metadata": {}}])

        with self.assertRaises(ChatGPTTargetError) as ctx:
            ensure_run_chatgpt_target("run-1", ChatGPTTargetSelection("new", "explicit"), ledger=ledger)

        self.assertEqual(ctx.exception.reason_code, "chatgpt_target_change_refused")
        self.assertEqual(len(ledger.events), 1)

    def test_recorded_new_run_cannot_go_back_to_classic(self) -> None:
        ledger = EventLedger([{"event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "new"}}])

        with self.assertRaises(ChatGPTTargetError):
            ensure_run_chatgpt_target("run-1", ChatGPTTargetSelection("classic", "explicit"), ledger=ledger)
        self.assertEqual(
            ensure_run_chatgpt_target("run-1", ChatGPTTargetSelection("new", "env"), ledger=ledger).target, "new"
        )


class LocalControllerTargetWiringTests(unittest.TestCase):
    def controller(self, ledger, **kwargs):
        from agent.local_controller import LocalController
        from tests.test_local_controller import ReadModelSequence, _model

        return LocalController(
            ledger=ledger,
            read_model_builder=ReadModelSequence(_model(action="stop", reason="no_action", stage="idle")),
            desktop_mutex=NullChatGPTDesktopMutex(),
            **kwargs,
        )

    def test_classic_runs_get_no_extra_services(self) -> None:
        from tests.test_local_controller import FakeLedger

        kwargs = self.controller(FakeLedger())._supervision_handoff_kwargs("run-1")

        self.assertEqual(set(kwargs), {"desktop_mutex", "controller_instance_id", "should_stop"})

    def test_new_app_runs_get_the_new_services(self) -> None:
        from agent.new_chatgpt_loop import new_target_services
        from tests.test_local_controller import FakeLedger

        ledger = FakeLedger(events=[{"id": 1, "event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "new"}}])

        kwargs = self.controller(ledger)._supervision_handoff_kwargs("run-1")

        for name, service in new_target_services().as_step_kwargs().items():
            self.assertIs(kwargs[name], service, name)

    def test_contradictory_record_raises_so_the_worker_pauses(self) -> None:
        from tests.test_local_controller import FakeLedger

        ledger = FakeLedger(
            events=[
                {"id": 1, "event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "new"}},
                {"id": 2, "event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "classic"}},
            ]
        )

        with self.assertRaises(ChatGPTTargetError):
            self.controller(ledger)._supervision_handoff_kwargs("run-1")

    def test_controller_setting_comes_from_flag_or_env(self) -> None:
        from tests.test_local_controller import FakeLedger

        with mock.patch.dict("os.environ", {"CRAX_CHATGPT_APP": "new"}):
            from_env = self.controller(FakeLedger())
            explicit = self.controller(FakeLedger(), chatgpt_target="classic")
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("CRAX_CHATGPT_APP", None)
            default = self.controller(FakeLedger())

        self.assertEqual(from_env.chatgpt_target_selection.target, "new")
        self.assertEqual(explicit.chatgpt_target_selection.target, "classic")
        self.assertEqual(default.chatgpt_target_selection.target, "classic")

    def test_invalid_setting_refuses_to_start_the_controller(self) -> None:
        from tests.test_local_controller import FakeLedger

        with self.assertRaises(ChatGPTTargetError):
            self.controller(FakeLedger(), chatgpt_target="codex")

    def _start(self, **controller_kwargs):
        import tempfile

        from agent.local_controller import LocalController
        from tests.test_local_controller import (
            DirectCodexInitialExecutor,
            FakeLedger,
            ReadModelSequence,
            RecordingCodexRunner,
            _model,
        )

        ledger = FakeLedger()
        with tempfile.TemporaryDirectory() as repo:
            controller = LocalController(
                ledger=ledger,
                read_model_builder=ReadModelSequence(_model(action="stop", reason="no_action", stage="idle")),
                initial_run_executor=DirectCodexInitialExecutor(ledger, RecordingCodexRunner()),
                desktop_mutex=NullChatGPTDesktopMutex(),
                **controller_kwargs,
            )
            result = controller.start_run(
                repository_path=repo, initial_instruction="Task", project_title="P", chat_title="C", sandbox="read-only"
            )
            controller.current_worker.join(2)
        return result, ledger

    def test_starting_a_new_app_run_records_its_target(self) -> None:
        result, ledger = self._start(chatgpt_target="new")

        self.assertTrue(result.ok)
        target_events = [e for e in ledger.events if e["event_type"] == CHATGPT_TARGET_EVENT_TYPE]
        self.assertEqual(len(target_events), 1)
        self.assertEqual(target_events[0]["metadata"]["chatgpt_target"], "new")

    def test_starting_a_classic_run_writes_no_target_event(self) -> None:
        result, ledger = self._start(chatgpt_target="classic")

        self.assertTrue(result.ok)
        self.assertFalse([e for e in ledger.events if e["event_type"] == CHATGPT_TARGET_EVENT_TYPE])


class MutexTargetTests(unittest.TestCase):
    def test_identity_unchanged_when_no_target_is_given(self) -> None:
        identity = capture_mutex_identity(owning_run_id="run-1", mutex_token="t")

        self.assertNotIn("chatgpt_target", identity)
        self.assertEqual(
            set(identity),
            {"controller_instance_id", "boot_id", "pid", "pgid", "process_start_identity", "owning_run_id", "mutex_token"},
        )

    def test_identity_records_the_target_when_given(self) -> None:
        identity = capture_mutex_identity(owning_run_id="run-1", chatgpt_target="new")
        hold = NullChatGPTDesktopMutex().acquire("run-1", chatgpt_target="new")

        self.assertEqual(identity["chatgpt_target"], "new")
        self.assertEqual(hold.identity["chatgpt_target"], "new")


if __name__ == "__main__":
    unittest.main()


class CliSuperviseTargetWiringTests(unittest.TestCase):
    def run_cli(self, *, flag: str | None, events: list[dict], env: dict | None = None):
        import argparse
        import io
        import os

        from agent import cli
        from agent.supervise import SuperviseAction, SupervisePlan
        from agent.supervision_services import SupervisionStepResult

        args = argparse.Namespace(
            run_id="run-1", repo="/tmp", sandbox="read-only", app_name="ChatGPT",
            capture_timeout_seconds=None, stable_seconds=2.0, timeout=None, chatgpt_app=flag,
        )
        stop_step = SupervisionStepResult(
            ok=False, run_id="run-1", planner_action=str(SuperviseAction.STOP), terminal=True,
            metadata={"plan": SupervisePlan(action=SuperviseAction.STOP, reason="test_stop")},
        )
        ledger = EventLedger(events)
        environ = {k: v for k, v in os.environ.items() if k != "CRAX_CHATGPT_APP"}
        environ.update(env or {})
        with (
            mock.patch.dict("os.environ", environ, clear=True),
            mock.patch.object(cli, "ledger", ledger),
            mock.patch.object(cli, "run_supervision_step", return_value=stop_step) as step,
            mock.patch("sys.stdout", new=io.StringIO()),
            mock.patch("sys.stderr", new=io.StringIO()) as stderr,
        ):
            code = cli._run_supervise_command(args)
        return code, step, ledger, stderr.getvalue()

    def test_classic_run_keeps_the_cli_wrappers(self) -> None:
        code, step, ledger, _ = self.run_cli(flag=None, events=[])

        kwargs = step.call_args.kwargs
        self.assertNotIn("destination_adapter_factory", kwargs)
        self.assertNotIn("destination_navigation_service", kwargs)
        self.assertEqual(kwargs["submit_service"].__name__, "submit_service")  # the CLI's own wrapper
        self.assertEqual(ledger.events, [])

    def test_new_flag_on_a_fresh_run_records_and_uses_new_services(self) -> None:
        from agent.new_chatgpt_loop import new_target_services

        _code, step, ledger, _ = self.run_cli(flag="new", events=[])

        kwargs = step.call_args.kwargs
        for name, service in new_target_services().as_step_kwargs().items():
            self.assertIs(kwargs[name], service, name)
        self.assertEqual(ledger.events[-1]["event_type"], CHATGPT_TARGET_EVENT_TYPE)

    def test_recorded_new_run_is_followed_without_a_flag(self) -> None:
        from agent.new_chatgpt_loop import new_submit_service

        _code, step, _, _ = self.run_cli(
            flag=None, events=[{"event_type": CHATGPT_TARGET_EVENT_TYPE, "metadata": {"chatgpt_target": "new"}}]
        )

        self.assertIs(step.call_args.kwargs["submit_service"], new_submit_service)

    def test_switching_a_classic_run_is_refused(self) -> None:
        code, step, ledger, stderr = self.run_cli(
            flag="new", events=[{"event_type": "gpt_feedback_submission_verified", "metadata": {}}]
        )

        self.assertEqual(code, 2)
        step.assert_not_called()
        self.assertIn("chatgpt_target_change_refused", stderr)

    def test_env_var_selects_the_new_app(self) -> None:
        from agent.new_chatgpt_loop import new_capture_service

        _code, step, _, _ = self.run_cli(flag=None, events=[], env={"CRAX_CHATGPT_APP": "new"})

        self.assertIs(step.call_args.kwargs["capture_service"], new_capture_service)
