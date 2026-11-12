from __future__ import annotations

import http.client
import json
import signal
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import codex_terminal, codex_resume_services, local_controller
from agent.codex_invocation import STATUS_COMPLETE, STATUS_LIVE, STATUS_UNCERTAIN, artifact_paths_for
from agent.codex_services import execute_codex_direct_service
from agent.local_controller import LocalController, LocalControllerSession
from agent.local_controller import ControllerSessionRuntime
from agent.local_server import LocalControllerServer, LOCAL_SERVER_TOKEN_HEADER
from tests.local_socket_test_support import LocalSocketBindingTestCase
from tests.test_local_controller import FakeLedger, _model, _run, _event

SESSION_ID = "01a132c6-a2d1-76c2-8947-a8cdec3781a0"


def session_events(artifact_dir=None):
    metadata = {"codex_invocation_id": "inv-current", "prompt": "Original task"}
    if artifact_dir:
        metadata["artifact_dir"] = str(artifact_dir)
    return [
        _event(1, "codex_exec_started", metadata),
        _event(2, "codex_progress_event", {
            "codex_invocation_id": "inv-current",
            "metadata": {"event_type": "thread.started", "value_summary": {"codex_session_id": SESSION_ID}},
        }),
    ]


class CodexSessionRecoveryTests(unittest.TestCase):
    def test_thread_id_and_error_message_survive_normalization(self):
        event = codex_terminal.normalize_codex_jsonl_event(json.dumps({"type": "thread.started", "thread_id": SESSION_ID}))
        self.assertEqual(event["metadata"]["value_summary"]["codex_session_id"], SESSION_ID)
        event = codex_terminal.normalize_codex_jsonl_event(json.dumps({"type": "error", "message": "Reconnecting: DNS lookup failed"}))
        self.assertEqual(event["summary"], "Reconnecting: DNS lookup failed")
        self.assertEqual(event["metadata"]["value_summary"]["error"], event["summary"])

    def test_legacy_log_recovery_uses_current_invocation_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            events = session_events(path)
            events[1]["metadata"]["codex_invocation_id"] = "inv-old"
            events[1]["metadata_json"] = json.dumps(events[1]["metadata"])
            (path / "stdout.log").write_text(json.dumps({"type": "thread.started", "thread_id": SESSION_ID}) + "\n")
            recovered = codex_resume_services.codex_session_details("run-1", events)
            self.assertEqual(recovered, {"session_id": SESSION_ID, "invocation_id": "inv-current"})
            (path / "stdout.log").unlink()
            self.assertIsNone(codex_resume_services.codex_session_details("run-1", events)["session_id"])

    def test_failed_resume_before_thread_started_preserves_session_for_next_attempt(self):
        events = session_events()
        events.append(_event(3, "codex_exec_finished", {"codex_invocation_id": "inv-current", "exit_code": 1}))
        events.append(_event(4, "codex_exec_started", {"codex_invocation_id": "inv-resume", "resume_session_id": SESSION_ID}))
        events.append(_event(5, "codex_exec_finished", {"codex_invocation_id": "inv-resume", "exit_code": 1}))
        self.assertEqual(codex_resume_services.codex_session_details("run-1", events), {
            "session_id": SESSION_ID, "invocation_id": "inv-resume",
        })

    def test_resume_command_targets_exact_id_and_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            final_path = Path(directory) / "final.md"
            final_path.write_text("Finished")
            for sandbox in ["read-only", "workspace-write", "danger-full-access"]:
                with self.subTest(sandbox=sandbox), mock.patch.object(codex_terminal.shutil, "which", return_value="fake-codex"), mock.patch.object(codex_terminal, "run_json_streaming_command", return_value={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}) as runner:
                    result = codex_terminal.run_codex_exec("Continue", repo_path=directory, sandbox=sandbox, json_stream=True, resume_session_id=SESSION_ID, final_message_path=final_path)
                    argv = runner.call_args.args[0]
                    self.assertEqual(argv[-3:], ["resume", SESSION_ID, "Continue"])
                    self.assertIn("--json", argv)
                    self.assertNotIn("--last", argv)
                    if sandbox == "danger-full-access":
                        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
                    else:
                        self.assertEqual(argv[argv.index("-s") + 1], sandbox)
                    self.assertEqual(result["final_message"], "Finished")


class EmergencyTerminationTests(unittest.TestCase):
    def test_termination_is_not_reported_successful_while_group_still_exists(self):
        process = mock.Mock(pid=123456, returncode=-15)
        process.poll.return_value = None
        with mock.patch.object(codex_terminal.os, "getpgid", return_value=123456), mock.patch.object(codex_terminal.os, "killpg") as signal_group, mock.patch.object(codex_terminal, "process_group_is_alive", return_value=True), mock.patch.object(codex_terminal, "wait_for_process_group_exit", return_value=False):
            result = codex_terminal._terminate_popen_group(process, timeout_seconds=0.1)
        self.assertFalse(result["terminated"])
        self.assertEqual(result["reason_code"], "codex_process_termination_failed")
        self.assertEqual(signal_group.call_args.args, (123456, signal.SIGKILL))

    def test_stop_before_spawn_never_starts_process_and_has_exit_evidence(self):
        stopped = threading.Event()
        stopped.set()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(codex_terminal, "spawn_invocation_wrapper") as spawn:
            with codex_terminal.codex_cancellation_scope(stopped):
                result = codex_terminal.run_json_streaming_command(["fake-codex", "exec", "Task"], process_metadata={"run_id": "run-1", "codex_invocation_id": "inv-current", "artifact_dir": directory})
            spawn.assert_not_called()
            self.assertEqual(result["exit_code"], -signal.SIGTERM)
            self.assertTrue((Path(directory) / "cancel_requested.json").exists())
            self.assertTrue((Path(directory) / "exit.json").exists())

    def test_stop_between_spawn_and_registration_terminates_new_process(self):
        stopped = threading.Event()
        process = mock.Mock(pid=123456)
        def spawn(_paths):
            stopped.set()
            return process
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(codex_terminal, "spawn_invocation_wrapper", side_effect=spawn), mock.patch.object(codex_terminal, "terminate_codex_run", return_value={"terminated": True}) as terminate, mock.patch.object(codex_terminal, "tail_stdout_and_wait"), mock.patch.object(codex_terminal, "result_from_artifacts", return_value={"exit_code": -15}), mock.patch.object(codex_terminal, "_install_main_thread_shutdown_handlers"):
            with codex_terminal.codex_cancellation_scope(stopped):
                codex_terminal.run_json_streaming_command(["fake-codex", "exec", "Task"], process_metadata={"run_id": "run-1", "codex_invocation_id": "inv-current", "artifact_dir": directory})
            terminate.assert_called_once_with("run-1")
        self.assertNotIn("run-1", codex_terminal._ACTIVE_CODEX_PROCESSES)

    def test_marker_write_failure_does_not_prevent_process_termination(self):
        process = mock.Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            active = codex_terminal.ActiveCodexInvocation("run-stop", "inv", process, artifact_paths_for("run-stop", "inv", artifact_dir=directory))
            with mock.patch.dict(codex_terminal._ACTIVE_CODEX_PROCESSES, {"run-stop": active}), mock.patch.object(codex_terminal, "write_cancel_marker", side_effect=OSError("disk full")), mock.patch.object(codex_terminal, "_terminate_popen_group", return_value={"terminated": True}) as terminate:
                result = codex_terminal.terminate_codex_run("run-stop")
                terminate.assert_called_once()
                self.assertTrue(result["terminated"])
                self.assertEqual(result["persistence_error"], "disk full")

    def test_recovered_stop_checks_all_invocations_not_alphabetical_last(self):
        identity = {"codex_invocation_id": "inv-a-live"}
        with mock.patch.object(codex_terminal, "disk_invocation_ids", return_value=["inv-a-live", "inv-z-old"]), mock.patch.object(codex_terminal, "classify_invocation", side_effect=[SimpleNamespace(identity=identity, paths="paths"), SimpleNamespace(identity=None)]), mock.patch.object(codex_terminal, "identity_matches_live_process", return_value=True), mock.patch.object(codex_terminal, "_record_terminated_exit"), mock.patch.object(codex_terminal, "terminate_verified_identity", return_value={"terminated": True}) as terminate:
            result = codex_terminal.terminate_codex_run("run-stop")
            self.assertTrue(result["terminated"])
            self.assertEqual(terminate.call_args.args[0], identity)


class ControllerRecoveryTests(unittest.TestCase):
    def make_controller(self, *, events=None, executor=None):
        ledger = FakeLedger(_run("completed"), events or session_events())
        controller = LocalController(session=LocalControllerSession(active_run_id="run-1"), ledger=ledger, read_model_builder=lambda *a, **kw: _model(terminal=True, blocked=True), codex_resume_executor=executor or mock.Mock(return_value=SimpleNamespace(ok=True)))
        return controller, ledger

    def test_running_worker_is_not_reported_terminal(self):
        controller, _ = self.make_controller()
        controller._sessions["run-1"].action_running = True
        model = controller.get_current_state("run-1").read_model
        self.assertFalse(model.terminal)
        self.assertFalse(model.blocked)

    def test_stop_survives_ledger_failure_and_can_be_retried(self):
        controller, ledger = self.make_controller()
        ledger.save_local_controller_snapshot = mock.Mock(side_effect=sqlite3.OperationalError("disk full"))
        ledger.add_event = mock.Mock(side_effect=sqlite3.OperationalError("disk full"))
        with mock.patch.object(local_controller, "terminate_codex_run", side_effect=[{"terminated": False, "reason_code": "codex_process_termination_failed"}, {"terminated": True}]) as terminate:
            first = controller.request_cancel("run-1")
            self.assertFalse(first.ok)
            second = controller.request_cancel("run-1")
            self.assertTrue(second.ok)
            self.assertEqual(terminate.call_count, 2)
            self.assertIsNotNone(second.metadata["persistence_error"])

    def test_continue_is_not_quota_only_and_clears_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        def execute(**kwargs):
            self.assertEqual(kwargs["session_id"], SESSION_ID)
            self.assertFalse(controller._sessions["run-1"].cancel_requested.is_set())
            entered.set()
            release.wait(3)
            return SimpleNamespace(ok=True)
        controller, ledger = self.make_controller(executor=execute)
        controller._sessions["run-1"].cancel_requested.set()
        ledger.run["status"] = "failed"
        with mock.patch.object(local_controller, "latest_open_invocation", return_value=None), mock.patch.object(controller, "_automatic_progress_loop") as progress:
            try:
                result = controller.request_force_continue("run-1")
                self.assertTrue(result.ok)
                self.assertTrue(entered.wait(2))
                duplicate = controller.request_force_continue("run-1")
                self.assertEqual(duplicate.reason_code, "action_already_running")
            finally:
                release.set()
                controller._sessions["run-1"].current_worker.join(3)
            progress.assert_called_once()
        self.assertEqual(ledger.added_events[-1]["event_type"], "local_controller_continue_requested")

    def test_continue_rejects_live_uncertain_missing_session_and_unknown_run(self):
        controller, _ = self.make_controller()
        self.assertEqual(controller.request_force_continue("missing").reason_code, "no_active_run")
        for status, reason in [(STATUS_LIVE, "action_already_running"), (STATUS_UNCERTAIN, "codex_invocation_uncertain")]:
            with self.subTest(status=status), mock.patch.object(local_controller, "latest_open_invocation", return_value=SimpleNamespace(status=status)):
                self.assertEqual(controller.request_force_continue("run-1").reason_code, reason)
        controller, _ = self.make_controller(events=[_event(1, "unrelated", {})])
        self.assertEqual(controller.request_force_continue("run-1").reason_code, "codex_session_unavailable")

    def test_continue_cannot_reenable_a_worker_that_is_still_exiting(self):
        controller, _ = self.make_controller()
        controller._sessions["run-1"].current_worker = mock.Mock()
        controller._sessions["run-1"].current_worker.is_alive.return_value = True
        self.assertEqual(controller.request_force_continue("run-1").reason_code, "action_already_running")

    def test_continuation_intent_failure_preserves_stop_and_starts_no_worker(self):
        executor = mock.Mock()
        controller, ledger = self.make_controller(executor=executor)
        runtime = controller._sessions["run-1"]
        runtime.cancel_requested.set()
        ledger.add_event = mock.Mock(side_effect=sqlite3.OperationalError("disk full"))
        with mock.patch.object(local_controller, "latest_open_invocation", return_value=None), self.assertRaises(sqlite3.OperationalError):
            controller.request_force_continue("run-1")
        self.assertTrue(runtime.cancel_requested.is_set())
        self.assertFalse(runtime.action_running)
        self.assertIsNone(runtime.current_worker)
        executor.assert_not_called()

    def test_continuation_cannot_exceed_session_capacity(self):
        controller, _ = self.make_controller()
        controller.max_active_sessions = 1
        controller._sessions["run-1"].cancel_requested.set()
        controller._sessions["run-b"] = ControllerSessionRuntime("run-b", action_running=True)
        with mock.patch.object(local_controller, "latest_open_invocation", return_value=None):
            self.assertEqual(controller.request_force_continue("run-1").reason_code, "session_capacity_reached")
        self.assertTrue(controller._sessions["run-b"].action_running)


class ResumePipelineTests(unittest.TestCase):
    def test_real_governance_pipeline_with_fake_codex_resumes_and_repairs_failed_step(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            events = session_events()
            events.append(_event(3, "codex_exec_finished", {"codex_invocation_id": "inv-current", "exit_code": 1}))
            events.append(_event(4, "extracted_codex_prompt_run_started", {"extraction_event_id": 42, "prompt_sha256": "original-sha"}))
            ledger = FakeLedger(_run("needs_review"), events)
            def fake_codex(prompt, **kwargs):
                self.assertEqual(prompt, codex_resume_services.CONTINUE_PROMPT)
                self.assertEqual(kwargs["resume_session_id"], SESSION_ID)
                return {
                    "mode": "exec", "found": True, "codex_path": "fake-codex",
                    "prompt": prompt, "repo_path": str(repo), "sandbox": "read-only",
                    "validation_error": None, "command": ["fake-codex", "exec", "resume", SESSION_ID, prompt],
                    "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                    "final_message": "Work completed", "final_message_length": 14,
                    "final_message_status": "valid", "final_message_error": None,
                }
            def raw(*args, **kwargs):
                return execute_codex_direct_service(*args, codex_runner=fake_codex, **kwargs)
            with mock.patch("agent.codex_invocation.default_runs_root", return_value=Path(directory) / "runs"), mock.patch.object(codex_resume_services, "execute_codex_direct_service", side_effect=raw):
                result = codex_resume_services.execute_codex_resume_service(run_id="run-1", run=ledger.run, repository_path=str(repo), sandbox="read-only", session_id=SESSION_ID, ledger=ledger)
            self.assertTrue(result.ok, result.error_message)
            self.assertEqual(ledger.run["status"], "completed")
            types = [e["event_type"] for e in ledger.added_events]
            self.assertIn("codex_exec_started", types)
            self.assertIn("codex_exec_finished", types)
            self.assertIn("invocation_delta_attributed", types)
            self.assertIn("supervision_decision", types)
            self.assertIn("extracted_codex_prompt_run_finished", types)

    def test_resume_uses_original_governance_contract_and_closes_unfinished_wrapper(self):
        events = session_events()
        events.append(_event(3, "extracted_codex_prompt_run_started", {"extraction_event_id": 42, "prompt_sha256": "original-sha"}))
        ledger = FakeLedger(_run("completed"), events)
        raw_result = SimpleNamespace(persisted=True, raw_process_result={"exit_code": 0, "timed_out": False, "codex_invocation_id": "inv-resume"})
        continued = SimpleNamespace(ok=True, raw_execution_result=raw_result, run_status="completed")
        def governed(run_id, run, prompt, repo, sandbox, timeout, **kwargs):
            self.assertEqual(prompt, "Original task")
            self.assertEqual(sandbox, "read-only")
            kwargs["raw_execution_service"](run_id, prompt, repo, sandbox, timeout, {"read_only": True}, ledger=ledger)
            return continued
        with mock.patch.object(codex_resume_services, "reconcile_codex_invocation", return_value=None), mock.patch.object(codex_resume_services, "execute_initial_direct_codex_run_service", side_effect=governed), mock.patch.object(codex_resume_services, "execute_codex_direct_service", return_value=raw_result) as raw:
            result = codex_resume_services.execute_codex_resume_service(run_id="run-1", run=ledger.run, repository_path="/tmp/repo", sandbox="read-only", session_id=SESSION_ID, ledger=ledger)
        self.assertTrue(result.ok)
        self.assertEqual(raw.call_args.args[1], codex_resume_services.CONTINUE_PROMPT)
        self.assertEqual(raw.call_args.kwargs["native_session"].session_id, SESSION_ID)
        self.assertEqual(raw.call_args.kwargs["native_session"].coding_agent, "codex")
        self.assertEqual(raw.call_args.args[5], {"read_only": True})
        repaired = ledger.added_events[-1]
        self.assertEqual(repaired["event_type"], "extracted_codex_prompt_run_finished")
        self.assertEqual(repaired["metadata"]["extraction_event_id"], 42)
        self.assertEqual(repaired["metadata"]["prompt_sha256"], "original-sha")
        self.assertTrue(repaired["metadata"]["continued"])

    def test_unproven_reconciliation_never_launches_continuation(self):
        failed = SimpleNamespace(persisted=False, ok=False)
        ledger = FakeLedger(_run("completed"), session_events())
        with mock.patch.object(codex_resume_services, "reconcile_codex_invocation", return_value=failed), mock.patch.object(codex_resume_services, "execute_initial_direct_codex_run_service") as execute:
            result = codex_resume_services.execute_codex_resume_service(run_id="run-1", run=ledger.run, repository_path="/tmp", sandbox="read-only", session_id=SESSION_ID, ledger=ledger)
        self.assertIs(result, failed)
        execute.assert_not_called()


class ForceContinueDeliveryTests(LocalSocketBindingTestCase):
    def test_http_continue_delivers_prompt_to_exact_session_for_every_stopped_status(self):
        # Exercise the real HTTP/controller/resume/governance/CLI pipeline.
        # Only the subprocess boundary is replaced; no Codex or desktop runs.
        for status in ("needs_review", "failed", "completed", "waiting_for_approval", "rejected"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
                events = session_events()
                events.append(_event(3, "codex_exec_finished", {"codex_invocation_id": "inv-current", "exit_code": 1}))
                ledger = FakeLedger(_run(status), events)
                ledger.add_codex_progress_event = mock.Mock()
                controller = LocalController(
                    session=LocalControllerSession(active_run_id="run-1"), ledger=ledger,
                    read_model_builder=lambda *a, **kw: _model(repo_path=str(repo), terminal=True, blocked=True),
                )
                runtime = controller._sessions["run-1"]
                runtime.cancel_requested.set()
                if status in {"waiting_for_approval", "rejected"}:
                    runtime.pending_approval = mock.Mock()
                if status in {"failed", "completed"}:
                    # Restore omits absorbing/finished runtimes; manual continue
                    # must still recover the focused run from durable evidence.
                    controller._sessions.clear()
                delivered = threading.Event()
                commands = []

                def fake_transport(command, cwd=None, **kwargs):
                    commands.append(command)
                    self.assertEqual(command[-3:], ["resume", SESSION_ID, codex_resume_services.CONTINUE_PROMPT])
                    self.assertNotIn("--last", command)
                    self.assertEqual(command[command.index("-s") + 1], "read-only")
                    self.assertEqual(cwd, str(repo.resolve()))
                    self.assertFalse(controller._sessions["run-1"].cancel_requested.is_set())
                    Path(command[command.index("--output-last-message") + 1]).write_text("Continued task completed")
                    delivered.set()
                    return {"command": command, "cwd": cwd, "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

                server = LocalControllerServer(controller=controller, port=0)
                original_which = codex_terminal.shutil.which
                with mock.patch("agent.codex_invocation.default_runs_root", return_value=Path(directory) / "runs"), mock.patch.object(codex_terminal.shutil, "which", side_effect=lambda name: "fake-codex" if name == "codex" else original_which(name)), mock.patch.object(codex_terminal, "run_json_streaming_command", side_effect=fake_transport), mock.patch.object(codex_terminal, "run_command", side_effect=AssertionError("Unexpected non-streaming Codex launch")), mock.patch.object(controller, "_automatic_progress_loop") as progress:
                    try:
                        server.start()
                        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
                        try:
                            connection.request("POST", "/api/runs/run-1/continue", body="{}", headers={LOCAL_SERVER_TOKEN_HEADER: controller.session.token, "Content-Type": "application/json"})
                            response = connection.getresponse()
                            payload = json.loads(response.read())
                        finally:
                            connection.close()
                        self.assertEqual(response.status, 202, payload)
                        self.assertTrue(delivered.wait(3), (payload, [(e["event_type"], e["message"], e.get("metadata")) for e in ledger.added_events[-2:]]))
                        controller._sessions["run-1"].current_worker.join(5)
                        self.assertEqual(len(commands), 1)
                        self.assertEqual(ledger.run["status"], "completed")
                        self.assertIsNone(controller._sessions["run-1"].pending_approval)
                        progress.assert_called_once()
                        started = [e for e in ledger.added_events if e["event_type"] == "codex_exec_started"][-1]
                        self.assertEqual(started["metadata"]["resume_session_id"], SESSION_ID)
                        self.assertEqual(started["metadata"]["prompt"], codex_resume_services.CONTINUE_PROMPT)
                    finally:
                        server.shutdown()


if __name__ == "__main__":
    unittest.main()
