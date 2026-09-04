from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent.codex_invocation import (
    STATUS_ALREADY_FINISHED,
    STATUS_COMPLETE,
    STATUS_LIVE,
    STATUS_UNCERTAIN,
    artifact_paths_for,
    boot_identity,
    build_intent_payload,
    classify_invocation,
    identity_matches_live_process,
    pid_is_alive,
    process_start_identity,
    spawn_invocation_wrapper,
    terminate_verified_identity,
    write_intent,
)
from agent.codex_services import execute_codex_direct_service, reconcile_codex_invocation
from agent.codex_terminal import (
    observe_existing_codex_invocation,
    run_codex_exec,
    terminate_all_active_codex_invocations,
    terminate_codex_run,
)


FAKE_CODEX_SOURCE = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

def _arg_after(flag):
    args = sys.argv
    if flag in args:
        return args[args.index(flag) + 1]
    return None

final_path = _arg_after("--output-last-message")
hold_path = os.environ.get("FAKE_CODEX_HOLD_PATH")
sleep_s = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
exit_code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
stderr_text = os.environ.get(
    "FAKE_CODEX_STDERR",
    "raw stderr body that must not appear in progress\\n",
)
final_text = os.environ.get(
    "FAKE_CODEX_FINAL",
    "Authoritative final message from file.\\n",
)
started_path = os.environ.get("FAKE_CODEX_STARTED_PATH")
lines = os.environ.get("FAKE_CODEX_LINES")

sys.stderr.write(stderr_text)
sys.stderr.flush()
if lines:
    payloads = json.loads(lines)
else:
    payloads = [
        {"type": "command_started", "command": ["npm", "test", "--token", "secret-value"]},
        {"type": "command_finished", "command": ["npm", "test"], "exit_code": 0},
    ]
for index, payload in enumerate(payloads):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()
    if index == 0 and started_path:
        Path(started_path).write_text("started\\n")
    if index == 0 and hold_path:
        deadline = time.time() + 30
        while not Path(hold_path).exists() and time.time() < deadline:
            time.sleep(0.05)
    if sleep_s and index == 0:
        time.sleep(sleep_s)
if final_path:
    Path(final_path).parent.mkdir(parents=True, exist_ok=True)
    Path(final_path).write_text(final_text)
sys.exit(exit_code)
"""


class ProgressLedger:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events: list[dict] = list(events or [])
        self.progress_events: list[dict] = []
        self._next_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1
        self.progress_error: Exception | None = None

    def list_events(self, run_id: str) -> list[dict]:
        return self.events

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        event = {
            "id": self._next_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        self._next_id += 1
        self.events.append(event)
        return event

    def add_codex_progress_event(
        self,
        run_id: str,
        invocation_id: str,
        progress_event: dict,
    ) -> dict:
        if self.progress_error is not None:
            raise self.progress_error
        stored = {
            "run_id": run_id,
            "codex_invocation_id": invocation_id,
            **progress_event,
        }
        self.progress_events.append(stored)
        return stored


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        raise AssertionError("Codex runner should not have been called")


def _event(event_id: int, event_type: str, metadata: dict) -> dict:
    return {
        "id": event_id,
        "run_id": "run-durable",
        "event_type": event_type,
        "message": event_type,
        "metadata": metadata,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


class DurableCodexInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text(FAKE_CODEX_SOURCE, encoding="utf-8")
        fake.chmod(0o755)
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"
        self.runs_root = self.root / "runs"
        self._runs_root_patcher = mock.patch(
            "agent.codex_invocation.default_runs_root",
            return_value=self.runs_root,
        )
        self._runs_root_patcher.start()
        for key in (
            "FAKE_CODEX_SLEEP",
            "FAKE_CODEX_HOLD_PATH",
            "FAKE_CODEX_STARTED_PATH",
            "FAKE_CODEX_LINES",
        ):
            os.environ.pop(key, None)
        os.environ["FAKE_CODEX_EXIT"] = "0"

    def tearDown(self) -> None:
        terminate_all_active_codex_invocations(source="test_teardown")
        self._runs_root_patcher.stop()
        os.environ["PATH"] = self.old_path
        self.tmpdir.cleanup()

    def _paths(self, invocation_id: str = "inv-1"):
        return artifact_paths_for("run-durable", invocation_id)

    def _run_stream(self, **kwargs):
        return run_codex_exec(
            "Prompt text must stay out of progress",
            repo_path=self.repo,
            sandbox="read-only",
            run_id="run-durable",
            json_stream=True,
            codex_invocation_id=kwargs.pop("codex_invocation_id", "inv-1"),
            **kwargs,
        )

    def _codex_command(self, paths) -> list[str]:
        return [
            "codex",
            "exec",
            "--json",
            "-C",
            str(self.repo.resolve()),
            "-s",
            "read-only",
            "--output-last-message",
            str(paths.final_message_path),
            "Prompt",
        ]

    def test_normal_success_writes_durable_artifacts_and_progress(self) -> None:
        progress: list[dict] = []
        result = self._run_stream(progress_callback=progress.append)
        paths = self._paths()
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(paths.intent_path.is_file())
        self.assertTrue(paths.identity_path.is_file())
        self.assertTrue(paths.exit_path.is_file())
        self.assertTrue(paths.stdout_path.is_file())
        self.assertTrue(paths.stderr_path.is_file())
        self.assertEqual(result["final_message"], "Authoritative final message from file.")
        self.assertIn("raw stderr body", result["stderr"])
        kinds = [event["kind"] for event in progress]
        self.assertIn("process_started", kinds)
        self.assertIn("command_started", kinds)
        self.assertIn("final_message_available", kinds)
        self.assertIn("process_exited", kinds)
        encoded = json.dumps(progress)
        self.assertNotIn("secret-value", encoded)
        self.assertNotIn("raw stderr body", encoded)
        self.assertNotIn("Prompt text must stay out of progress", encoded)
        intent = json.loads(paths.intent_path.read_text(encoding="utf-8"))
        self.assertEqual(intent["cwd"], str(self.repo.resolve()))
        self.assertIn("--json", intent["command"])
        self.assertEqual(intent["sandbox"], "read-only")

    def test_nonzero_exit_persists_exit_json(self) -> None:
        os.environ["FAKE_CODEX_EXIT"] = "37"
        result = self._run_stream()
        self.assertEqual(result["exit_code"], 37)
        exit_payload = json.loads(self._paths().exit_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_payload["exit_code"], 37)
        classified = classify_invocation("run-durable", "inv-1")
        self.assertEqual(classified.status, STATUS_COMPLETE)

    def test_live_jsonl_progress_arrives_before_exit(self) -> None:
        hold = self.root / "continue"
        started = self.root / "started"
        os.environ["FAKE_CODEX_HOLD_PATH"] = str(hold)
        os.environ["FAKE_CODEX_STARTED_PATH"] = str(started)
        progress: list[dict] = []
        result_holder: dict[str, object] = {}

        def worker() -> None:
            result_holder["result"] = self._run_stream(progress_callback=progress.append)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not started.exists():
            time.sleep(0.05)
        self.assertTrue(started.exists())
        deadline = time.time() + 5
        while time.time() < deadline and not any(
            event.get("kind") == "command_started" for event in progress
        ):
            time.sleep(0.05)
        self.assertTrue(any(event.get("kind") == "command_started" for event in progress))
        self.assertFalse(any(event.get("kind") == "process_exited" for event in progress))
        hold.write_text("go\n")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder["result"]["exit_code"], 0)

    def test_cancellation_signals_wrapper_group_and_keeps_cancel_evidence(self) -> None:
        os.environ["FAKE_CODEX_SLEEP"] = "30"
        result_holder: dict[str, object] = {}

        def worker() -> None:
            result_holder["result"] = self._run_stream()

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self._paths().identity_path.is_file():
            time.sleep(0.05)
        termination = terminate_codex_run("run-durable")
        self.assertTrue(termination["terminated"])
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        paths = self._paths()
        self.assertTrue(paths.cancel_path.is_file())
        result = result_holder["result"]
        self.assertEqual(result["termination_reason"], "operator_cancelled")
        self.assertTrue(result["cancel_requested"])

    def test_exact_live_recovery_does_not_duplicate_wrapper(self) -> None:
        os.environ["FAKE_CODEX_SLEEP"] = "8"
        paths = self._paths()
        write_intent(
            paths,
            build_intent_payload(
                run_id="run-durable",
                invocation_id="inv-1",
                prompt="Prompt",
                repo_path=str(self.repo.resolve()),
                cwd=str(self.repo.resolve()),
                sandbox="read-only",
                model=None,
                command=self._codex_command(paths),
                json_mode=True,
                paths=paths,
            ),
        )
        wrapper = spawn_invocation_wrapper(paths)
        deadline = time.time() + 5
        while time.time() < deadline and not paths.identity_path.is_file():
            time.sleep(0.05)
        classified = classify_invocation("run-durable", "inv-1")
        self.assertEqual(classified.status, STATUS_LIVE)
        live_before = set(classified.live_pids)
        result = observe_existing_codex_invocation(classified, wrapper=wrapper)
        self.assertEqual(result["exit_code"], 0)
        after = classify_invocation("run-durable", "inv-1")
        self.assertEqual(after.status, STATUS_COMPLETE)
        self.assertTrue(live_before)

    def test_child_completion_before_controller_finalization_is_idempotent(self) -> None:
        paths = self._paths()
        write_intent(
            paths,
            build_intent_payload(
                run_id="run-durable",
                invocation_id="inv-1",
                prompt="Prompt",
                repo_path=str(self.repo.resolve()),
                cwd=str(self.repo.resolve()),
                sandbox="read-only",
                model=None,
                command=self._codex_command(paths),
                json_mode=True,
                paths=paths,
            ),
        )
        wrapper = spawn_invocation_wrapper(paths)
        wrapper.wait(timeout=10)
        classified = classify_invocation("run-durable", "inv-1")
        self.assertEqual(classified.status, STATUS_COMPLETE)
        ledger = ProgressLedger(
            [
                _event(
                    1,
                    "codex_exec_started",
                    {
                        "codex_invocation_id": "inv-1",
                        "prompt": "Prompt",
                        "repo_path": str(self.repo.resolve()),
                        "sandbox": "read-only",
                    },
                )
            ]
        )
        first = reconcile_codex_invocation("run-durable", ledger=ledger)
        self.assertIsNotNone(first)
        self.assertEqual(first.exit_code, 0)
        self.assertEqual(first.final_message, "Authoritative final message from file.")
        finished = [
            event for event in ledger.events if event["event_type"] == "codex_exec_finished"
        ]
        self.assertEqual(len(finished), 1)
        second = reconcile_codex_invocation("run-durable", ledger=ledger)
        self.assertIsNone(second)
        self.assertEqual(
            len([event for event in ledger.events if event["event_type"] == "codex_exec_finished"]),
            1,
        )
        again = classify_invocation("run-durable", "inv-1", events=ledger.events)
        self.assertEqual(again.status, STATUS_ALREADY_FINISHED)

    def test_incomplete_and_truncated_artifacts_are_uncertain(self) -> None:
        paths = self._paths()
        write_intent(
            paths,
            build_intent_payload(
                run_id="run-durable",
                invocation_id="inv-1",
                prompt="Prompt",
                repo_path=str(self.repo),
                cwd=str(self.repo),
                sandbox="read-only",
                model=None,
                command=["codex", "exec", "Prompt"],
                json_mode=True,
                paths=paths,
            ),
        )
        classified = classify_invocation(
            "run-durable",
            "inv-1",
        )
        self.assertEqual(classified.status, STATUS_UNCERTAIN)
        paths.exit_path.write_text("{truncated", encoding="utf-8")
        truncated = classify_invocation(
            "run-durable",
            "inv-1",
        )
        self.assertEqual(truncated.status, STATUS_UNCERTAIN)
        paths.exit_path.write_text(
            json.dumps({"codex_invocation_id": "inv-1", "exit_code": 0}),
            encoding="utf-8",
        )
        paths.stdout_path.unlink(missing_ok=True)
        missing_stdout = classify_invocation("run-durable", "inv-1")
        self.assertEqual(missing_stdout.status, STATUS_UNCERTAIN)
        paths.stdout_path.write_text("", encoding="utf-8")
        paths.exit_path.write_text(
            json.dumps({"codex_invocation_id": "inv-1"}),
            encoding="utf-8",
        )
        missing_code = classify_invocation("run-durable", "inv-1")
        self.assertEqual(missing_code.status, STATUS_UNCERTAIN)

    def test_pid_reuse_mismatch_is_not_adopted_or_signalled(self) -> None:
        identity = {
            "codex_invocation_id": "inv-1",
            "boot_id": boot_identity(),
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "start_identity": "not-the-real-start-identity",
        }
        self.assertFalse(identity_matches_live_process(identity))
        result = terminate_verified_identity(identity)
        self.assertEqual(result["reason_code"], "identity_mismatch")
        self.assertFalse(result["terminated"])
        self.assertEqual(process_start_identity(os.getpid()), process_start_identity(os.getpid()))

    def test_progress_persistence_failure_is_observable(self) -> None:
        ledger = ProgressLedger()
        ledger.progress_error = RuntimeError("ledger unavailable")
        result = execute_codex_direct_service(
            "run-durable",
            "Prompt",
            str(self.repo),
            "read-only",
            None,
            {},
            ledger=ledger,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "codex_progress_persistence_failed")
        self.assertTrue(result.raw_process_result["progress_persistence_failed"])
        self.assertTrue(
            any(event["event_type"] == "codex_progress_write_failed" for event in ledger.events)
        )
        self.assertEqual(result.exit_code, 0)

    def test_spawn_pending_without_process_never_reruns(self) -> None:
        ledger = ProgressLedger(
            [
                _event(
                    1,
                    "codex_exec_started",
                    {
                        "codex_invocation_id": "inv-pending",
                        "prompt": "Prompt",
                        "repo_path": str(self.repo),
                        "sandbox": "read-only",
                    },
                )
            ]
        )
        runner = RecordingRunner()
        result = execute_codex_direct_service(
            "run-durable",
            "Prompt",
            str(self.repo),
            "read-only",
            None,
            {},
            ledger=ledger,
            codex_runner=runner,
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(result.reason_code, "codex_invocation_uncertain")
        self.assertTrue(
            any(event["event_type"] == "codex_invocation_uncertain" for event in ledger.events)
        )

    def test_codex_exec_started_failure_prevents_spawn(self) -> None:
        class FailStartedLedger(ProgressLedger):
            def add_event(self, run_id, event_type, message, metadata=None):
                if event_type == "codex_exec_started":
                    raise RuntimeError("started persist failed")
                return super().add_event(run_id, event_type, message, metadata)

        runner = RecordingRunner()
        with self.assertRaisesRegex(RuntimeError, "started persist failed"):
            execute_codex_direct_service(
                "run-durable",
                "Prompt",
                str(self.repo),
                "read-only",
                None,
                {},
                ledger=FailStartedLedger(),
                codex_runner=runner,
            )
        self.assertEqual(runner.calls, [])

    def test_controller_death_during_output_recovers_without_duplicate_spawn(self) -> None:
        hold = self.root / "continue"
        started = self.root / "started"
        os.environ["FAKE_CODEX_HOLD_PATH"] = str(hold)
        os.environ["FAKE_CODEX_STARTED_PATH"] = str(started)
        paths = self._paths()
        write_intent(
            paths,
            build_intent_payload(
                run_id="run-durable",
                invocation_id="inv-1",
                prompt="Prompt",
                repo_path=str(self.repo.resolve()),
                cwd=str(self.repo.resolve()),
                sandbox="read-only",
                model=None,
                command=self._codex_command(paths),
                json_mode=True,
                paths=paths,
            ),
        )
        wrapper = spawn_invocation_wrapper(paths)
        deadline = time.time() + 5
        while time.time() < deadline and not started.exists():
            time.sleep(0.05)
        self.assertTrue(started.exists())
        classified = classify_invocation("run-durable", "inv-1")
        self.assertEqual(classified.status, STATUS_LIVE)
        live_before = set(classified.live_pids or [wrapper.pid])
        holder: dict[str, object] = {}

        def recover() -> None:
            holder["result"] = observe_existing_codex_invocation(classified, wrapper=wrapper)

        thread = threading.Thread(target=recover)
        thread.start()
        time.sleep(0.2)
        self.assertTrue(any(pid_is_alive(pid) for pid in live_before) or wrapper.poll() is None)
        hold.write_text("go\n")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["result"]["exit_code"], 0)
        self.assertEqual(classify_invocation("run-durable", "inv-1").status, STATUS_COMPLETE)

    def test_sigterm_parent_does_not_leave_live_codex(self) -> None:
        os.environ["FAKE_CODEX_SLEEP"] = "30"
        paths = self._paths()
        helper = r"""
import os
import threading
import time
from pathlib import Path

from agent.codex_terminal import install_codex_shutdown_handlers, run_codex_exec

install_codex_shutdown_handlers()
identity = Path(os.environ["HELPER_IDENTITY"])
ready = Path(os.environ["HELPER_READY"])
repo = Path(os.environ["HELPER_REPO"])
artifact_dir = Path(os.environ["HELPER_ARTIFACT_DIR"])

def worker():
    run_codex_exec(
        "Prompt",
        repo_path=repo,
        sandbox="read-only",
        run_id="run-durable",
        json_stream=True,
        codex_invocation_id="inv-1",
        artifact_dir=artifact_dir,
    )

thread = threading.Thread(target=worker, daemon=True)
thread.start()
deadline = time.time() + 10
while time.time() < deadline and not identity.is_file():
    time.sleep(0.05)
ready.write_text("ready\n")
while True:
    time.sleep(0.2)
"""
        ready = self.root / "helper-ready"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        env["HELPER_IDENTITY"] = str(paths.identity_path)
        env["HELPER_READY"] = str(ready)
        env["HELPER_REPO"] = str(self.repo)
        env["HELPER_ARTIFACT_DIR"] = str(paths.artifact_dir)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-c", helper],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not ready.is_file():
                time.sleep(0.05)
            self.assertTrue(ready.is_file())
            identity = json.loads(paths.identity_path.read_text(encoding="utf-8"))
            wrapper_pid = identity["pid"]
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15)
            self.assertFalse(identity_matches_live_process(identity))
            with self.assertRaises(OSError):
                os.kill(wrapper_pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_intentional_shutdown_does_not_leave_live_codex(self) -> None:
        os.environ["FAKE_CODEX_SLEEP"] = "30"
        holder: dict[str, object] = {}

        def worker() -> None:
            holder["result"] = self._run_stream()

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self._paths().identity_path.is_file():
            time.sleep(0.05)
        identity = json.loads(self._paths().identity_path.read_text(encoding="utf-8"))
        pid = identity["pid"]
        terminate_all_active_codex_invocations(source="signal")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(identity_matches_live_process(identity))
        with self.assertRaises(OSError):
            os.kill(pid, 0)
