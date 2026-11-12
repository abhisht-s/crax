"""Provider composition tests. Only a temporary fake CLI is ever executable."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from agent import cli, ledger
from agent.codex_invocation import artifact_paths_for, build_intent_payload, write_intent
from agent.codex_resume_services import execute_coding_agent_resume_service
from agent.coding_agent_provider import NativeSessionIdentity
from agent.coding_agent_services import execute_coding_agent_service, reconcile_coding_agent_invocation
from agent.coding_agents import (
    ProviderResolutionError, cancel_coding_agent_run, coding_agent_session_details,
    provider_for_events, resolve_provider,
)
from agent.extracted_prompt_services import execute_extracted_codex_prompt_service
from agent.local_controller import (
    DurabilityGuardedLedger, LedgerDurabilityGuard, default_initial_run_executor,
)
from agent.run_services import RunExecutionProfile, get_run_execution_profile, select_run_execution_profile
from agent.run_state import RunStatus
from tests.test_codex_invocation import FAKE_CODEX_SOURCE
from tests.test_extracted_prompt_services import _submission, _capture, _extraction


class ProviderRegistryTests(unittest.TestCase):
    def test_known_identity_is_distinct_from_executable_provider(self):
        self.assertEqual(resolve_provider("codex").identity, "codex")
        for identity in ("claude_code", "cursor"):
            with self.subTest(identity=identity), self.assertRaises(ProviderResolutionError) as raised:
                resolve_provider(identity)
            self.assertEqual(raised.exception.reason_code, "coding_agent_unavailable")
        for identity in ("unknown", "Codex", "", None):
            with self.subTest(identity=identity), self.assertRaises(ProviderResolutionError) as raised:
                resolve_provider(identity)
            self.assertEqual(raised.exception.reason_code, "coding_agent_unknown")


class ProviderCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        source = FAKE_CODEX_SOURCE.replace("import json\n", "import json\nimport uuid\n")
        source = source.replace('payloads = [\n', '''payloads = [
        {"type": "thread.started", "thread_id": sys.argv[sys.argv.index("resume") + 1] if "resume" in sys.argv else str(uuid.uuid5(uuid.NAMESPACE_URL, sys.argv[-1]))},
''')
        executable = bin_dir / "codex"
        executable.write_text(source)
        executable.chmod(0o755)
        env = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_CODEX_")}
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.enterContext(mock.patch.object(ledger, "DB_PATH", self.root / "ledger.db"))
        self.enterContext(mock.patch("agent.codex_invocation.default_runs_root", return_value=self.root / "runs"))

    def run_id(self, identity="codex", sandbox="read-only", model="codex_default"):
        run_id = ledger.create_run("Say exactly: complete")
        selected = select_run_execution_profile(run_id, RunExecutionProfile(
            sandbox=sandbox, coding_agent=identity, model=model,
        ), ledger=ledger)
        self.assertTrue(selected.ok, selected.error_message)
        return run_id

    def execute(self, run_id, prompt="Say exactly: complete", sandbox="read-only", **kwargs):
        return execute_coding_agent_service(run_id, prompt, str(self.repo), sandbox, None, {}, ledger=ledger, **kwargs)

    def write_event(self, run_id, event):
        ledger.add_event(run_id, event["event_type"], "fixture", json.loads(event["metadata_json"]))
        return ledger.list_events(run_id)[-1]

    def test_v1_default_resolves_and_real_wrapper_preserves_native_evidence(self):
        run_id = self.run_id()
        profile = get_run_execution_profile(run_id, ledger=ledger)
        self.assertEqual(profile.profile.coding_agent, "codex")
        self.assertEqual(json.loads(ledger.list_events(run_id)[0]["metadata_json"])["schema_version"], 1)
        result = self.execute(run_id)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.coding_agent, "codex")
        self.assertEqual(result.outcome.process, "exited")
        self.assertEqual(result.outcome.provider, "completed")
        self.assertEqual(result.outcome.final_answer, "valid")
        self.assertEqual(result.outcome.native_session.coding_agent, "codex")
        raw = result.raw_process_result
        paths = artifact_paths_for(run_id, raw["codex_invocation_id"])
        self.assertEqual(json.loads(paths.intent_path.read_text())["coding_agent"], "codex")
        for path in (paths.identity_path, paths.exit_path, paths.stdout_path, paths.stderr_path):
            self.assertTrue(path.exists())
        self.assertEqual(result.final_message, paths.final_message_path.read_text().strip())
        finished = next(e for e in ledger.list_events(run_id) if e["event_type"] == "codex_exec_finished")
        self.assertEqual(json.loads(finished["metadata_json"])["execution_outcome"]["final_answer"], "valid")
        self.assertEqual(coding_agent_session_details(run_id, ledger.list_events(run_id))["session_id"], result.outcome.native_session.session_id)
        progress = [json.loads(e["metadata_json"]) for e in ledger.list_events(run_id) if e["event_type"] == "codex_progress_event"]
        self.assertTrue(any(e.get("metadata", {}).get("value_summary", {}).get("codex_session_id") for e in progress))

    def test_v2_bind_is_immutable_reconstructible_and_unavailable_never_executes_codex(self):
        for identity in ("claude_code", "cursor"):
            run_id = self.run_id(identity)
            self.assertEqual(get_run_execution_profile(run_id, ledger=ledger).profile.coding_agent, identity)
            self.assertTrue(select_run_execution_profile(run_id, RunExecutionProfile(sandbox="read-only", coding_agent=identity), ledger=ledger).ok)
            different = select_run_execution_profile(run_id, RunExecutionProfile(sandbox="read-only"), ledger=ledger)
            self.assertFalse(different.ok)
            with mock.patch("agent.codex_terminal.run_codex_exec", side_effect=AssertionError("No fallback")):
                result = self.execute(run_id)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_code, "coding_agent_unavailable")
            self.assertEqual(result.coding_agent, identity)
            self.assertEqual(result.outcome.provider, "unavailable")
            self.assertFalse(any(e["event_type"] == "codex_exec_started" for e in ledger.list_events(run_id)))

    def test_invalid_direct_binding_and_contradictory_events_fail_closed(self):
        run_id = self.run_id()
        bad = ledger.bind_run_execution_profile(run_id, "read-only", "codex_default", "codex_default", "codex_default", "system_default", coding_agent="unknown")
        self.assertEqual(bad.status, ledger.AtomicExecutionProfileStatus.INVALID)
        metadata = json.loads(ledger.list_events(run_id)[0]["metadata_json"])
        metadata.update(schema_version=2, coding_agent="cursor")
        ledger.add_event(run_id, "run_execution_profile_selected", "conflict", metadata)
        with mock.patch("agent.codex_terminal.run_codex_exec", side_effect=AssertionError("No fallback")):
            result = self.execute(run_id)
        self.assertEqual(result.reason_code, "contradictory_execution_profile_events")
        self.assertEqual(result.coding_agent, "unresolved")

    def test_atomic_bind_race_has_one_durable_owner(self):
        run_id = ledger.create_run("race")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda identity: select_run_execution_profile(run_id, RunExecutionProfile(sandbox="read-only", coding_agent=identity), ledger=ledger), ("codex", "cursor")))
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertIn(get_run_execution_profile(run_id, ledger=ledger).profile.coding_agent, ("codex", "cursor"))
        self.assertEqual(len(ledger.list_events(run_id)), 1)

    def test_dashboard_initial_executor_composes_with_provider_and_governance(self):
        run_id = self.run_id()
        result = default_initial_run_executor(run_id=run_id, run=ledger.get_run(run_id), initial_instruction="Say exactly: complete", repository_path=str(self.repo), sandbox="read-only", ledger=ledger)
        self.assertTrue(result.ok, result.error_message)
        self.assertTrue(result.raw_execution_result.ok)
        self.assertEqual(result.raw_execution_result.coding_agent, "codex")
        self.assertIsNotNone(result.governance_result)

    def test_unavailable_initial_executor_skips_governance_safely(self):
        run_id = self.run_id("cursor")
        result = default_initial_run_executor(run_id=run_id, run=ledger.get_run(run_id), initial_instruction="Say exactly: complete", repository_path=str(self.repo), sandbox="read-only", ledger=ledger)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "coding_agent_unavailable")
        self.assertIsNone(result.governance_result)

    def test_extracted_default_callback_uses_guarded_ledger_and_real_provider(self):
        run_id = self.run_id()
        ledger.update_run_status(run_id, RunStatus.COMPLETED)
        submission = self.write_event(run_id, _submission())
        capture = self.write_event(run_id, _capture(submission))
        self.write_event(run_id, _extraction(capture, submission))
        guarded = DurabilityGuardedLedger(ledger, LedgerDurabilityGuard())
        with mock.patch.object(cli, "ledger", mock.Mock(side_effect=AssertionError("Module ledger must not be used"))), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute_extracted_codex_prompt_service(run_id, ledger.get_run(run_id), str(self.repo), "read-only", None, confirm_full_access=False, allow_full_access=False, approval_mode="human", ledger=guarded)
        self.assertTrue(result.ok, result.error_message)
        finished = [e for e in ledger.list_events(run_id) if e["event_type"] == "codex_exec_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(json.loads(finished[0]["metadata_json"])["coding_agent"], "codex")
        self.assertFalse(guarded.durability_guard.status()["blocked"])

    def test_extracted_unavailable_provider_rejects_before_callback(self):
        run_id = self.run_id("claude_code")
        ledger.update_run_status(run_id, RunStatus.COMPLETED)
        callback = mock.Mock(side_effect=AssertionError("No fallback callback"))
        result = execute_extracted_codex_prompt_service(run_id, ledger.get_run(run_id), str(self.repo), "read-only", None, confirm_full_access=False, allow_full_access=False, approval_mode="human", ledger=ledger, codex_flow_coordinator=callback)
        self.assertEqual(result.reason_code, "coding_agent_unavailable")
        callback.assert_not_called()

    def test_full_access_and_model_are_preserved_by_adapter(self):
        run_id = self.run_id(sandbox="danger-full-access", model="gpt-5-codex")
        result = self.execute(run_id, sandbox="danger-full-access")
        self.assertTrue(result.ok)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", result.command)
        self.assertNotIn("-s", result.command)
        self.assertEqual(result.command[result.command.index("-m") + 1], "gpt-5-codex")
        self.assertEqual(result.command[result.command.index("-C") + 1], str(self.repo))

    def test_exit_zero_without_final_answer_is_not_success(self):
        run_id = self.run_id()
        with mock.patch.dict(os.environ, {"FAKE_CODEX_FINAL": ""}):
            result = self.execute(run_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.outcome.process, "exited")
        self.assertEqual(result.outcome.provider, "unverified")
        self.assertEqual(result.outcome.final_answer, "empty")
        self.assertEqual(result.final_message, "")
        self.assertTrue(result.stdout)

    def test_concurrent_codex_runs_have_independent_durable_ownership(self):
        run_ids = [self.run_id() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda item: self.execute(item[1], prompt=f"Say exactly: session {item[0]}"), enumerate(run_ids)))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(len({r.raw_process_result["codex_invocation_id"] for r in results}), 4)
        self.assertEqual(len({r.outcome.native_session.session_id for r in results}), 4)
        for run_id, result in zip(run_ids, results):
            self.assertEqual(result.run_id, run_id)
            self.assertEqual(provider_for_events(run_id, ledger.list_events(run_id)).identity, "codex")
            paths = artifact_paths_for(run_id, result.raw_process_result["codex_invocation_id"])
            self.assertEqual(json.loads(paths.intent_path.read_text())["run_id"], run_id)

    def test_native_continuation_resolves_original_provider_and_exact_session(self):
        run_id = self.run_id()
        initial = self.execute(run_id)
        session = initial.outcome.native_session
        result = execute_coding_agent_resume_service(run_id=run_id, run=ledger.get_run(run_id), repository_path=str(self.repo), sandbox="read-only", session_id=session.session_id, ledger=ledger)
        self.assertTrue(result.ok, result.error_message)
        raw = result.raw_execution_result
        self.assertEqual(raw.command[-3:-1], ["resume", session.session_id])
        self.assertEqual(raw.outcome.native_session, session)
        bad = self.execute(run_id, native_session=NativeSessionIdentity("cursor", session.session_id))
        self.assertEqual(bad.reason_code, "coding_agent_session_mismatch")
        bad = self.execute(run_id, native_session=NativeSessionIdentity("codex", "wrong-session"))
        self.assertEqual(bad.reason_code, "coding_agent_session_mismatch")

    def test_unavailable_native_continuation_never_dispatches_codex(self):
        run_id = self.run_id("claude_code")
        with mock.patch("agent.codex_terminal.run_codex_exec", side_effect=AssertionError("No fallback")):
            result = execute_coding_agent_resume_service(run_id=run_id, run=ledger.get_run(run_id), repository_path=str(self.repo), sandbox="read-only", session_id="other", ledger=ledger)
        self.assertEqual(result.reason_code, "coding_agent_unavailable")

    def test_cancel_through_boundary_preserves_durable_outcome(self):
        run_id = self.run_id()
        marker = self.root / "started"
        with mock.patch.dict(os.environ, {"FAKE_CODEX_STARTED_PATH": str(marker), "FAKE_CODEX_SLEEP": "30"}), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.execute, run_id)
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            stopped = cancel_coding_agent_run(run_id, ledger=ledger)
            self.assertTrue(stopped["terminated"])
            result = pending.result(timeout=5)
        self.assertFalse(result.ok)
        self.assertTrue(result.outcome.cancelled)
        self.assertTrue(result.outcome.interrupted)
        self.assertEqual(result.outcome.process, "cancelled")
        self.assertTrue(Path(result.raw_process_result["cancel_path"]).exists())

    def test_cancel_with_unavailable_ledger_still_uses_verified_process_stop(self):
        with mock.patch("agent.codex_terminal.terminate_codex_run", return_value={"terminated": True}) as stop:
            broken = mock.Mock()
            broken.list_events.side_effect = OSError("ledger unavailable")
            self.assertTrue(cancel_coding_agent_run("run-1", ledger=broken)["terminated"])
            stop.assert_called_once_with("run-1")

    def test_recovery_observes_existing_artifacts_without_reexecution(self):
        run_id = self.run_id()
        first = self.execute(run_id)
        # Simulate process completion followed by loss of only the finish write.
        with contextlib.closing(ledger._connect()) as connection, connection:
            connection.execute("DELETE FROM events WHERE run_id = ? AND event_type = 'codex_exec_finished'", (run_id,))
        paths = artifact_paths_for(run_id, first.raw_process_result["codex_invocation_id"])
        old_intent = json.loads(paths.intent_path.read_text())
        old_intent.pop("coding_agent")
        paths.intent_path.write_text(json.dumps(old_intent))
        with mock.patch("agent.codex_terminal.run_codex_exec", side_effect=AssertionError("Must not replay")):
            result = reconcile_coding_agent_invocation(run_id, ledger=ledger)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.raw_process_result["codex_invocation_id"], first.raw_process_result["codex_invocation_id"])
        self.assertEqual(result.outcome.native_session, first.outcome.native_session)
        self.assertIsNone(reconcile_coding_agent_invocation(run_id, ledger=ledger))

    def test_recovery_rejects_provider_mismatch_and_never_replays_uncertainty(self):
        run_id = self.run_id()
        paths = artifact_paths_for(run_id, "inv-test")
        intent = build_intent_payload(run_id=run_id, invocation_id="inv-test", prompt="original", repo_path=str(self.repo), cwd=str(self.repo), sandbox="read-only", model=None, command=["never-execute"], json_mode=True, paths=paths)
        write_intent(paths, intent)
        ledger.add_event(run_id, "codex_exec_started", "started", {"codex_invocation_id": "inv-test", "prompt": "original", "repo_path": str(self.repo), "sandbox": "read-only"})
        # Start evidence without process/exit proof must remain uncertain.
        with mock.patch("agent.codex_terminal.run_codex_exec", side_effect=AssertionError("No replay")):
            result = reconcile_coding_agent_invocation(run_id, ledger=ledger)
        self.assertEqual(result.reason_code, "codex_invocation_uncertain")
        self.assertTrue(result.outcome.uncertain)
        intent["coding_agent"] = "cursor"
        write_intent(paths, intent)
        with mock.patch("agent.codex_terminal.observe_existing_codex_invocation", side_effect=AssertionError("Wrong owner")):
            result = reconcile_coding_agent_invocation(run_id, ledger=ledger)
        self.assertEqual(result.reason_code, "coding_agent_invocation_mismatch")

    def test_unavailable_concurrent_selection_cannot_change_another_runs_provider(self):
        identities = ("codex", "claude_code", "codex", "cursor")
        run_ids = [self.run_id(identity) for identity in identities]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda pair: self.execute(pair[1], prompt=f"Say exactly: mixed {pair[0]}"), enumerate(run_ids)))
        for identity, result in zip(identities, results):
            self.assertEqual(result.coding_agent, identity)
            self.assertEqual(result.ok, identity == "codex")
            if identity != "codex":
                self.assertEqual(result.reason_code, "coding_agent_unavailable")

    def test_empty_native_session_cannot_turn_continuation_into_a_new_task(self):
        for session_id in (None, "", " "):
            with self.subTest(session_id=session_id), self.assertRaises(ValueError):
                NativeSessionIdentity("codex", session_id)
