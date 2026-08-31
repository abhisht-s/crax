from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent import cli
from agent import codex_terminal
from agent import ledger as ledger_module
from agent.codex_services import (
    CodexDirectExecutionResult,
    execute_codex_direct_service,
)
from agent.run_services import (
    CODEX_DEFAULT_SELECTION,
    RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
    RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_result(
    *,
    prompt: str = "Say exactly: hello",
    repo_path: str = "/tmp/repo",
    sandbox: str = "read-only",
    exit_code: int | None = 0,
    stdout: str = "hello\n",
    stderr: str = "",
    timed_out: bool = False,
    found: bool = True,
    codex_path: str | None = "/usr/local/bin/codex",
    validation_error: str | None = None,
    final_message: str = "Final assistant summary.\n",
    final_message_status: str = "valid",
    final_message_path: str | None = None,
    final_message_error: str | None = None,
) -> dict:
    final_path = final_message_path or f"{repo_path}/.codex-final-message.md"
    return {
        "mode": "exec",
        "found": found,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "validation_error": validation_error,
        "command": [
            "codex",
            "exec",
            "-C",
            repo_path,
            "-s",
            sandbox,
            "--output-last-message",
            final_path,
            prompt,
        ],
        "cwd": repo_path,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "final_message_path": final_path,
        "final_message": final_message,
        "final_message_length": len(final_message),
        "final_message_status": final_message_status,
        "final_message_error": final_message_error,
    }


def _execution_profile_event(
    *,
    sandbox: str = "read-only",
    model: str = CODEX_DEFAULT_SELECTION,
    event_id: int = 1,
) -> dict:
    metadata = {
        "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
        "sandbox": sandbox,
        "model": model,
        "reasoning_effort": CODEX_DEFAULT_SELECTION,
        "approval_policy": CODEX_DEFAULT_SELECTION,
        "profile_source": "explicit_user_selection",
    }
    return {
        "id": event_id,
        "run_id": "run-1",
        "event_type": RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
        "message": "Run execution profile selected.",
        "metadata": metadata,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


def _controller_started_event(*, event_id: int = 2, sandbox: str = "read-only") -> dict:
    metadata = {
        "metadata_version": "local_controller_v1",
        "repository_path": "/tmp/repo",
        "sandbox": sandbox,
        "source": "local_controller",
        "controller_mode": "browser_v1",
        "browser_safe_sandbox": True,
    }
    return {
        "id": event_id,
        "run_id": "run-1",
        "event_type": "local_controller_run_started",
        "message": "Local controller run initialized.",
        "metadata": metadata,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


class FakeLedger:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events: list[dict] = list(events or [])
        self.status_updates: list[tuple[str, object]] = []
        self._next_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1
        self.run = {"id": "run-1", "status": "created"}

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        event = {
            "id": self._next_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        self._next_id += 1
        self.events.append(event)
        return event

    def update_run_status(self, run_id: str, status: object) -> None:
        self.status_updates.append((run_id, status))

    def list_events(self, run_id: str) -> list[dict]:
        return self.events

    def get_run(self, run_id: str) -> dict:
        return self.run


class StepClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        return self.values.pop(0)


class RecordingRunner:
    def __init__(self, result: dict | None = None, exception: Exception | None = None) -> None:
        self.result = result
        self.exception = exception
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        if self.exception is not None:
            raise self.exception
        if self.result is None:
            raise AssertionError("runner result was not configured")
        return self.result


class FakeStreamingStdout:
    def __init__(self, lines: list[str], checkpoints: list[int], progress_events: list[dict]) -> None:
        self.lines = lines
        self.checkpoints = checkpoints
        self.progress_events = progress_events

    def __iter__(self):
        for index, line in enumerate(self.lines):
            yield line
            if index == 0:
                self.checkpoints.append(len(self.progress_events))


class FakeStreamingPopen:
    instances: list["FakeStreamingPopen"] = []
    lines: list[str] = []
    checkpoints: list[int] = []
    progress_events: list[dict] = []
    returncode: int = 0

    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242
        self.stdout = FakeStreamingStdout(
            self.lines,
            self.checkpoints,
            self.progress_events,
        )
        stderr_file = kwargs["stderr"]
        stderr_file.write("raw stderr body that must not appear in progress")
        self.instances.append(self)

    def wait(self) -> int:
        final_path = Path(self.command[self.command.index("--output-last-message") + 1])
        final_path.write_text("Authoritative final message from file.\n", encoding="utf-8")
        return self.returncode


def _temporary_real_ledger():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "ledger.db"
    patcher = mock.patch.object(ledger_module, "DB_PATH", db_path)

    class _Context:
        def __enter__(self):
            patcher.__enter__()
            return db_path

        def __exit__(self, exc_type, exc, tb):
            patcher.__exit__(exc_type, exc, tb)
            tmpdir.cleanup()

    return _Context()


class CodexTerminalTimeoutTests(unittest.TestCase):
    def test_todo_list_event_preserves_bounded_plan_items(self) -> None:
        event = codex_terminal.normalize_codex_jsonl_event(
            json.dumps(
                {
                    "type": "item.updated",
                    "item": {
                        "id": "item-1",
                        "type": "todo_list",
                        "items": [
                            {"text": "Inspect the narrow seam", "completed": True},
                            {"text": "Run focused tests", "completed": False},
                        ],
                    },
                }
            )
        )

        summary = event["metadata"]["value_summary"]
        self.assertEqual(summary["item_type"], "todo_list")
        self.assertEqual(
            summary["plan_items"],
            [
                {"label": "Inspect the narrow seam", "completed": True},
                {"label": "Run focused tests", "completed": False},
            ],
        )
        self.assertFalse(summary["plan_items_truncated"])
        self.assertNotIn('"text"', json.dumps(event["metadata"], sort_keys=True))

    def test_thread_started_event_preserves_codex_session_id(self) -> None:
        session_id = "01a05837-1cb2-76b0-852f-6a104eb1f07c"
        event = codex_terminal.normalize_codex_jsonl_event(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": session_id,
                }
            )
        )

        self.assertEqual(event["kind"], "codex_json_event")
        self.assertEqual(
            event["metadata"]["value_summary"]["codex_session_id"],
            session_id,
        )

    def test_agent_message_json_event_becomes_bounded_assistant_commentary(self) -> None:
        event = codex_terminal.normalize_codex_jsonl_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "agent_message",
                        "text": "I found the narrow seam and am preserving the existing behavior.",
                    },
                }
            )
        )

        self.assertEqual(event["kind"], "assistant_commentary")
        self.assertEqual(event["title"], "Codex update")
        self.assertEqual(
            event["summary"],
            "I found the narrow seam and am preserving the existing behavior.",
        )
        self.assertNotIn("text", json.dumps(event["metadata"], sort_keys=True))

    def test_non_message_json_event_does_not_become_assistant_commentary(self) -> None:
        event = codex_terminal.normalize_codex_jsonl_event(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-2",
                        "type": "command_execution",
                        "command": "zsh -lc pwd",
                        "aggregated_output": "private command output",
                        "exit_code": 0,
                    },
                }
            )
        )

        self.assertEqual(event["kind"], "command_finished")
        self.assertNotIn("private command output", json.dumps(event, sort_keys=True))

    def test_full_access_uses_explicit_approval_and_sandbox_bypass(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="done\n",
            stderr="",
        )

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                sandbox="danger-full-access",
            )

        command = run.call_args.args[0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("-s", command)

    def test_run_codex_exec_requests_run_scoped_final_message_artifact(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="raw stdout that must not be submitted\n",
            stderr="raw stderr that must not be submitted\n",
        )

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("Clean final assistant message.\n", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                timeout_seconds=None,
                sandbox="read-only",
                run_id="run-abc/123",
            )

        command = run.call_args.args[0]
        self.assertIn("--output-last-message", command)
        final_message_path = Path(command[command.index("--output-last-message") + 1])
        self.assertIn("run-abc_123", str(final_message_path))
        self.assertEqual(result["final_message_status"], "valid")
        self.assertEqual(result["final_message"], "Clean final assistant message.")
        self.assertEqual(result["stdout"], "raw stdout that must not be submitted\n")
        self.assertEqual(result["stderr"], "raw stderr that must not be submitted\n")

    def test_run_codex_exec_omits_subprocess_timeout_when_timeout_is_none(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="done\n",
            stderr="",
        )

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                timeout_seconds=None,
                sandbox="read-only",
            )

        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        _args, kwargs = run.call_args
        self.assertNotIn("timeout", kwargs)
        self.assertEqual(kwargs["cwd"], str(Path(repo).resolve(strict=False)))

    def test_run_codex_exec_ignores_numeric_timeout_for_subprocess(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="done\n",
            stderr="",
        )

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                timeout_seconds=123,
                sandbox="workspace-write",
            )

        self.assertFalse(result["timed_out"])
        _args, kwargs = run.call_args
        self.assertNotIn("timeout", kwargs)

    def test_run_codex_exec_includes_optional_model_and_no_reasoning_or_approval_args(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="done\n",
            stderr="",
        )

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                timeout_seconds=123,
                sandbox="workspace-write",
                model="gpt-5-codex",
            )

        self.assertTrue(result["found"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-m") + 1], "gpt-5-codex")
        self.assertIn("--output-last-message", command)
        self.assertNotIn("--reasoning-effort", command)
        self.assertNotIn("--approval-policy", command)

    def test_run_codex_exec_json_stream_parses_incrementally_and_uses_final_file(self) -> None:
        progress_events: list[dict] = []
        checkpoints: list[int] = []
        FakeStreamingPopen.instances = []
        FakeStreamingPopen.lines = [
            json.dumps(
                {
                    "type": "command_started",
                    "command": ["npm", "test", "--token", "secret-value"],
                    "stdout": "raw stdout body that must not appear in progress",
                }
            )
            + "\n",
            "{malformed json line with secret output\n",
            json.dumps(
                {
                    "type": "command_finished",
                    "command": ["npm", "test"],
                    "exit_code": 0,
                    "stderr": "raw stderr body that must not appear in progress",
                }
            )
            + "\n",
        ]
        FakeStreamingPopen.checkpoints = checkpoints
        FakeStreamingPopen.progress_events = progress_events
        FakeStreamingPopen.returncode = 0

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "Popen", FakeStreamingPopen),
        ):
            result = codex_terminal.run_codex_exec(
                "Prompt text must stay out of progress",
                repo_path=repo,
                sandbox="read-only",
                run_id="run-stream",
                json_stream=True,
                codex_invocation_id="inv-1",
                progress_callback=progress_events.append,
            )

        command = FakeStreamingPopen.instances[0].command
        self.assertIn("--json", command)
        self.assertEqual(result["final_message"], "Authoritative final message from file.")
        self.assertIn("command_started", result["stdout"])
        self.assertGreaterEqual(checkpoints[0], 2)
        progress_text = json.dumps(progress_events, sort_keys=True)
        self.assertIn("Malformed Codex JSONL event", progress_text)
        self.assertIn("final_message_available", progress_text)
        self.assertIn("process_exited", progress_text)
        self.assertNotIn("raw stdout body", progress_text)
        self.assertNotIn("raw stderr body", progress_text)
        self.assertNotIn("secret-value", progress_text)
        self.assertNotIn("Prompt text must stay out of progress", progress_text)

    def test_run_codex_exec_non_streaming_keeps_existing_command_shape(self) -> None:
        completed = subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="done\n", stderr="")

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            result = codex_terminal.run_codex_exec("Prompt", repo_path=repo, sandbox="read-only")

        self.assertEqual(result["final_message"], "done")
        self.assertNotIn("--json", run.call_args.args[0])

    def test_run_codex_exec_resume_session_id_inserts_resume_and_keeps_default_argv(self) -> None:
        completed = subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="done\n", stderr="")
        thread_id = "01a05837-1cb2-76b0-852f-6a104eb1f07c"

        def run_side_effect(command, **_kwargs):
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(codex_terminal.subprocess, "run", side_effect=run_side_effect) as run,
        ):
            default_result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                sandbox="read-only",
            )
            resume_result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                sandbox="read-only",
                resume_session_id=thread_id,
            )

        default_command = run.call_args_list[0].args[0]
        resume_command = run.call_args_list[1].args[0]
        self.assertEqual(default_command[:2], ["codex", "exec"])
        self.assertNotIn("resume", default_command)
        self.assertNotIn("--last", default_command)
        self.assertNotIn("--last", resume_command)
        resume_index = resume_command.index("resume")
        output_index = resume_command.index("--output-last-message")
        self.assertLess(resume_index, output_index)
        self.assertEqual(resume_command[output_index + 2], thread_id)
        self.assertEqual(resume_command[-1], "Prompt")
        self.assertEqual(default_result["exit_code"], 0)
        self.assertEqual(resume_result["resume_session_id"], thread_id)

    def test_subprocess_timeout_exception_keeps_historical_timed_out_result_shape(self) -> None:
        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(codex_terminal.shutil, "which", return_value="/opt/homebrew/bin/codex"),
            mock.patch.object(
                codex_terminal.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["codex"],
                    timeout=123,
                    output="partial stdout",
                    stderr="partial stderr",
                ),
            ) as run,
        ):
            result = codex_terminal.run_codex_exec(
                "Prompt",
                repo_path=repo,
                timeout_seconds=123,
                sandbox="read-only",
            )

        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout"], "partial stdout")
        self.assertEqual(result["stderr"], "partial stderr")
        _args, kwargs = run.call_args
        self.assertNotIn("timeout", kwargs)


class CodexDirectExecutionServiceTests(unittest.TestCase):
    def test_success_writes_only_raw_events_and_preserves_output(self) -> None:
        ledger = FakeLedger()
        prompt = "Say exactly: hello"
        repo_path = "/tmp/repo"
        stdout = "line 1\nstdout-without-final-newline"
        stderr = "stderr line\n"
        raw = _raw_result(prompt=prompt, repo_path=repo_path, stdout=stdout, stderr=stderr)
        runner = RecordingRunner(raw)

        result = execute_codex_direct_service(
            "run-1",
            prompt,
            repo_path,
            "read-only",
            300,
            {"confidence": "low"},
            ledger=ledger,
            codex_runner=runner,
            monotonic_clock=StepClock([10.0, 12.5]),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.reason_code, "codex_exec_completed")
        self.assertEqual(result.started_event_id, 1)
        self.assertEqual(result.finished_event_id, 2)
        self.assertEqual(result.duration_seconds, 2.5)
        self.assertEqual(result.stdout, stdout)
        self.assertEqual(result.stderr, stderr)
        self.assertEqual(result.final_message, "Final assistant summary.\n")
        self.assertEqual(result.final_message_status, "valid")
        self.assertEqual(result.final_message_length, len("Final assistant summary.\n"))
        self.assertEqual(result.stdout_sha256, _sha(stdout))
        self.assertEqual(result.stderr_sha256, _sha(stderr))
        self.assertEqual(result.stdout_length, len(stdout))
        self.assertEqual(result.stderr_length, len(stderr))
        self.assertIs(result.raw_process_result, raw)
        self.assertNotIn("resume_session_id", runner.calls[0][1])

        self.assertEqual([event["event_type"] for event in ledger.events], ["codex_exec_started", "codex_exec_finished"])
        self.assertEqual(ledger.events[0]["message"], "Running Codex exec.")
        self.assertEqual(
            ledger.events[1]["message"],
            "found=True exit_code=0 timed_out=False repo_path=/tmp/repo sandbox=read-only",
        )
        self.assertEqual(ledger.events[0]["metadata"], {
            "prompt": prompt,
            "repo_path": repo_path,
            "timeout": None,
            "sandbox": "read-only",
            "prompt_contract": {"confidence": "low"},
        })
        self.assertIs(ledger.events[1]["metadata"], raw)
        self.assertNotIn("stdout_sha256", ledger.events[1]["metadata"])
        self.assertNotIn("stdout_length", ledger.events[1]["metadata"])
        self.assertNotIn("duration_seconds", ledger.events[1]["metadata"])
        self.assertEqual(ledger.status_updates, [])

    def test_resume_session_id_is_passed_only_when_set(self) -> None:
        ledger = FakeLedger()
        raw = _raw_result()
        runner = RecordingRunner(raw)

        result = execute_codex_direct_service(
            "run-1",
            "Say exactly: hello",
            "/tmp/repo",
            "read-only",
            300,
            {"confidence": "low"},
            ledger=ledger,
            codex_runner=runner,
            monotonic_clock=StepClock([10.0, 12.5]),
            resume_session_id="01a05837-1cb2-76b0-852f-6a104eb1f07c",
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            runner.calls[0][1]["resume_session_id"],
            "01a05837-1cb2-76b0-852f-6a104eb1f07c",
        )
        self.assertEqual(
            ledger.events[0]["metadata"]["resume_session_id"],
            "01a05837-1cb2-76b0-852f-6a104eb1f07c",
        )

    def test_runner_call_and_command_metadata_match_existing_contract(self) -> None:
        ledger = FakeLedger()
        prompt = "Fix the focused test"
        repo_path = "/tmp/focused-repo"
        raw = _raw_result(prompt=prompt, repo_path=repo_path, sandbox="workspace-write")
        runner = RecordingRunner(raw)

        result = execute_codex_direct_service(
            "run-1",
            prompt,
            repo_path,
            "workspace-write",
            123,
            {},
            ledger=ledger,
            codex_runner=runner,
            monotonic_clock=StepClock([1.0, 1.25]),
        )

        self.assertEqual(runner.calls, [
            (
                (prompt,),
                {
                    "repo_path": repo_path,
                    "timeout_seconds": None,
                    "sandbox": "workspace-write",
                    "run_id": "run-1",
                },
            )
        ])
        self.assertEqual(
            result.command,
            [
                "codex",
                "exec",
                "-C",
                repo_path,
                "-s",
                "workspace-write",
                "--output-last-message",
                "/tmp/focused-repo/.codex-final-message.md",
                prompt,
            ],
        )
        self.assertEqual(ledger.events[1]["metadata"]["command"], result.command)

    def test_real_ledger_progress_events_get_run_invocation_and_sequence(self) -> None:
        def runner(prompt: str, **kwargs) -> dict:
            kwargs["progress_callback"](
                {
                    "source": "test",
                    "kind": "command_started",
                    "status": "running",
                    "title": "Command started",
                    "summary": "bounded summary",
                    "metadata": {
                        "stdout": "raw body must be summarized",
                        "command": ["npm", "test"],
                    },
                }
            )
            return _raw_result(
                prompt=prompt,
                repo_path="/tmp/repo",
                stdout="raw process output remains on legacy result",
            )

        with _temporary_real_ledger():
            run_id = ledger_module.create_run("Task")
            result = execute_codex_direct_service(
                run_id,
                "Task prompt",
                "/tmp/repo",
                "read-only",
                None,
                {},
                ledger=ledger_module,
                codex_runner=runner,
                monotonic_clock=StepClock([1.0, 2.0]),
            )
            events = ledger_module.list_codex_progress_events(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["run_id"], run_id)
        self.assertEqual(event["codex_invocation_id"], result.raw_process_result["codex_invocation_id"])
        self.assertIsInstance(event["sequence"], int)
        self.assertEqual(event["kind"], "command_started")
        encoded = json.dumps(event, sort_keys=True)
        self.assertIn("stdout_length", encoded)
        self.assertNotIn("raw body must be summarized", encoded)

    def test_real_ledger_preserves_assistant_commentary_kind_and_text(self) -> None:
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("Task")
            stored = ledger_module.add_codex_progress_event(
                run_id,
                "inv-commentary",
                {
                    "source": "codex_cli_jsonl",
                    "kind": "assistant_commentary",
                    "status": "completed",
                    "title": "Codex update",
                    "summary": "I found a stable seam and am checking it now.",
                    "metadata": {"event_type": "item.completed"},
                },
            )
            events = ledger_module.list_codex_progress_events(run_id)

        self.assertEqual(stored["kind"], "assistant_commentary")
        self.assertEqual(events[0]["kind"], "assistant_commentary")
        self.assertEqual(events[0]["summary"], "I found a stable seam and am checking it now.")

    def test_real_ledger_preserves_codex_session_id(self) -> None:
        session_id = "01a05837-1cb2-76b0-852f-6a104eb1f07c"
        progress_event = codex_terminal.normalize_codex_jsonl_event(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": session_id,
                }
            )
        )
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("Task")
            ledger_module.add_codex_progress_event(
                run_id,
                "inv-session",
                progress_event,
            )
            events = ledger_module.list_codex_progress_events(run_id)

        self.assertEqual(
            events[0]["metadata"]["value_summary"]["codex_session_id"],
            session_id,
        )

    def test_real_ledger_preserves_codex_plan_items(self) -> None:
        progress_event = codex_terminal.normalize_codex_jsonl_event(
            json.dumps(
                {
                    "type": "item.updated",
                    "item": {
                        "id": "item-1",
                        "type": "todo_list",
                        "items": [
                            {"text": "Inspect the narrow seam", "completed": True},
                            {"text": "Run focused tests", "completed": False},
                        ],
                    },
                }
            )
        )
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("Task")
            ledger_module.add_codex_progress_event(
                run_id,
                "inv-plan",
                progress_event,
            )
            events = ledger_module.list_codex_progress_events(run_id)

        self.assertEqual(
            events[0]["metadata"]["value_summary"]["plan_items"],
            [
                {"label": "Inspect the narrow seam", "completed": True},
                {"label": "Run focused tests", "completed": False},
            ],
        )

    def test_controller_profile_default_model_launches_without_model_arg(self) -> None:
        ledger = FakeLedger(
            [
                _execution_profile_event(sandbox="workspace-write"),
                _controller_started_event(sandbox="workspace-write"),
            ]
        )
        prompt = "Fix the focused test"
        repo_path = "/tmp/focused-repo"
        raw = _raw_result(prompt=prompt, repo_path=repo_path, sandbox="workspace-write")
        runner = RecordingRunner(raw)

        result = execute_codex_direct_service(
            "run-1",
            prompt,
            repo_path,
            "workspace-write",
            123,
            {},
            ledger=ledger,
            codex_runner=runner,
            monotonic_clock=StepClock([1.0, 1.25]),
        )

        self.assertTrue(result.ok)
        self.assertEqual(runner.calls[0][1]["sandbox"], "workspace-write")
        self.assertNotIn("model", runner.calls[0][1])
        self.assertNotIn("-m", result.command)

    def test_controller_profile_explicit_model_launches_with_model_arg(self) -> None:
        ledger = FakeLedger(
            [
                _execution_profile_event(model="gpt-5-codex"),
                _controller_started_event(),
            ]
        )
        prompt = "Fix the focused test"
        repo_path = "/tmp/focused-repo"
        raw = _raw_result(prompt=prompt, repo_path=repo_path)
        raw["command"] = [
            "codex",
            "exec",
            "-C",
            repo_path,
            "-s",
            "read-only",
            "-m",
            "gpt-5-codex",
            "--output-last-message",
            "/tmp/focused-repo/.codex-final-message.md",
            prompt,
        ]
        runner = RecordingRunner(raw)

        result = execute_codex_direct_service(
            "run-1",
            prompt,
            repo_path,
            "read-only",
            123,
            {},
            ledger=ledger,
            codex_runner=runner,
            monotonic_clock=StepClock([1.0, 1.25]),
        )

        self.assertTrue(result.ok)
        self.assertEqual(runner.calls[0][1]["model"], "gpt-5-codex")
        self.assertNotIn("reasoning_effort", runner.calls[0][1])
        self.assertNotIn("approval_policy", runner.calls[0][1])
        self.assertEqual(result.command[result.command.index("-m") + 1], "gpt-5-codex")

    def test_controller_profile_missing_or_mismatched_fails_before_codex_started(self) -> None:
        cases = [
            (
                "missing",
                [_controller_started_event()],
                "read-only",
                "execution_profile_missing",
            ),
            (
                "mismatch",
                [
                    _execution_profile_event(sandbox="read-only"),
                    _controller_started_event(sandbox="workspace-write"),
                ],
                "workspace-write",
                "execution_profile_sandbox_mismatch",
            ),
            (
                "malformed",
                [
                    {
                        **_execution_profile_event(),
                        "metadata": {"schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION},
                        "metadata_json": json.dumps({"schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION}),
                    },
                    _controller_started_event(),
                ],
                "read-only",
                "malformed_execution_profile_event",
            ),
        ]

        for name, events, sandbox, reason in cases:
            with self.subTest(name=name):
                ledger = FakeLedger(events)
                runner = RecordingRunner(_raw_result(sandbox=sandbox))

                result = execute_codex_direct_service(
                    "run-1",
                    "Prompt",
                    "/tmp/repo",
                    sandbox,
                    123,
                    {},
                    ledger=ledger,
                    codex_runner=runner,
                    monotonic_clock=StepClock([1.0, 1.25]),
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason)
                self.assertEqual(runner.calls, [])
                self.assertNotIn(
                    "codex_exec_started",
                    [event["event_type"] for event in ledger.events],
                )

    def test_no_timeout_execution_records_none_and_completes_without_timed_out_result(self) -> None:
        ledger = FakeLedger()
        prompt = "Let Codex run naturally"
        repo_path = "/tmp/repo"
        raw = _raw_result(prompt=prompt, repo_path=repo_path, timed_out=False)
        runner = RecordingRunner(raw)

        result = execute_codex_direct_service(
            "run-1",
            prompt,
            repo_path,
            "read-only",
            None,
            {"confidence": "low"},
            ledger=ledger,
            codex_runner=runner,
            monotonic_clock=StepClock([10.0, 11.0]),
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.reason_code, "codex_exec_completed")
        self.assertEqual(runner.calls[0][1]["timeout_seconds"], None)
        self.assertIsNone(ledger.events[0]["metadata"]["timeout"])
        self.assertFalse(ledger.events[1]["metadata"]["timed_out"])

    def test_result_shaped_failures_write_started_then_finished(self) -> None:
        cases = [
            (
                "nonzero",
                _raw_result(exit_code=37, stderr="failed\n"),
                "codex_nonzero_exit",
            ),
            (
                "timeout",
                _raw_result(exit_code=None, timed_out=True, stdout="partial", stderr=""),
                "codex_timed_out",
            ),
            (
                "missing_codex",
                _raw_result(found=False, codex_path=None, exit_code=None, stdout="", stderr="Codex CLI not found on PATH.\n"),
                "codex_not_found",
            ),
            (
                "invalid_repo",
                _raw_result(exit_code=2, stdout="", stderr="Repo path does not exist: /tmp/repo\n", validation_error="Repo path does not exist: /tmp/repo"),
                "codex_validation_error",
            ),
            (
                "invalid_sandbox",
                _raw_result(exit_code=2, sandbox="bad", stdout="", stderr="Invalid Codex sandbox. Allowed values: read-only, workspace-write, danger-full-access.\n", validation_error="Invalid Codex sandbox. Allowed values: read-only, workspace-write, danger-full-access."),
                "codex_validation_error",
            ),
            (
                "caught_file_not_found",
                _raw_result(exit_code=127, stdout="", stderr="[Errno 2] No such file or directory: 'codex'\n"),
                "codex_nonzero_exit",
            ),
        ]

        for name, raw, reason in cases:
            with self.subTest(name=name):
                ledger = FakeLedger()
                result = execute_codex_direct_service(
                    "run-1",
                    raw["prompt"],
                    raw["repo_path"],
                    raw["sandbox"],
                    300,
                    {},
                    ledger=ledger,
                    codex_runner=RecordingRunner(raw),
                    monotonic_clock=StepClock([1.0, 2.0]),
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason)
                self.assertEqual([event["event_type"] for event in ledger.events], ["codex_exec_started", "codex_exec_finished"])
                self.assertIs(ledger.events[1]["metadata"], raw)
                self.assertEqual(result.stdout, raw["stdout"])
                self.assertEqual(result.stderr, raw["stderr"])
                self.assertEqual(ledger.status_updates, [])

    def test_preflight_validation_builder_result_is_persisted(self) -> None:
        ledger = FakeLedger()
        built = _raw_result(
            exit_code=2,
            stdout="",
            stderr="Codex sandbox danger-full-access requires --confirm-full-access.\n",
            validation_error="Codex sandbox danger-full-access requires --confirm-full-access.",
        )
        builder_calls = []

        def build(prompt: str, repo_path: str, sandbox: str, validation_error: str) -> dict:
            builder_calls.append((prompt, repo_path, sandbox, validation_error))
            return built

        runner = RecordingRunner(_raw_result())
        result = execute_codex_direct_service(
            "run-1",
            "Say exactly: hello",
            "/tmp/repo",
            "danger-full-access",
            300,
            {},
            preflight_validation_error="Codex sandbox danger-full-access requires --confirm-full-access.",
            ledger=ledger,
            codex_runner=runner,
            validation_result_builder=build,
            monotonic_clock=StepClock([1.0, 2.0]),
        )

        self.assertEqual(runner.calls, [])
        self.assertEqual(builder_calls, [
            (
                "Say exactly: hello",
                "/tmp/repo",
                "danger-full-access",
                "Codex sandbox danger-full-access requires --confirm-full-access.",
            )
        ])
        self.assertEqual(result.reason_code, "codex_validation_error")
        self.assertIs(ledger.events[1]["metadata"], built)

    def test_raw_output_integrity_empty_and_large_values(self) -> None:
        large_stdout = "alpha\n" * 20_000
        large_stderr = "beta\n" * 15_000
        cases = [
            ("empty_stdout", "", "stderr only\n"),
            ("large_output", large_stdout, large_stderr),
        ]

        for name, stdout, stderr in cases:
            with self.subTest(name=name):
                ledger = FakeLedger()
                raw = _raw_result(stdout=stdout, stderr=stderr)
                result = execute_codex_direct_service(
                    "run-1",
                    raw["prompt"],
                    raw["repo_path"],
                    raw["sandbox"],
                    300,
                    {},
                    ledger=ledger,
                    codex_runner=RecordingRunner(raw),
                    monotonic_clock=StepClock([1.0, 1.5]),
                )

                self.assertEqual(result.stdout, stdout)
                self.assertEqual(result.stderr, stderr)
                self.assertEqual(ledger.events[1]["metadata"]["stdout"], stdout)
                self.assertEqual(ledger.events[1]["metadata"]["stderr"], stderr)
                self.assertEqual(result.stdout_length, len(stdout))
                self.assertEqual(result.stderr_length, len(stderr))

    def test_unexpected_runner_exception_bubbles_without_finished_event(self) -> None:
        ledger = FakeLedger()
        runner = RecordingRunner(exception=RuntimeError("boom"))

        with self.assertRaisesRegex(RuntimeError, "boom"):
            execute_codex_direct_service(
                "run-1",
                "Say exactly: hello",
                "/tmp/repo",
                "read-only",
                300,
                {},
                ledger=ledger,
                codex_runner=runner,
                monotonic_clock=StepClock([1.0]),
            )

        self.assertEqual([event["event_type"] for event in ledger.events], ["codex_exec_started"])
        self.assertEqual(ledger.status_updates, [])


class CodexExecFlowCoordinatorCompatibilityTests(unittest.TestCase):
    def test_coordinator_preserves_raw_execution_boundary_order(self) -> None:
        fake_ledger = FakeLedger()
        snapshot = {
            "repo_path": "/tmp/repo",
            "is_git_repo": True,
            "head": "abcdef1234567890",
            "branch": "main",
            "status_short": "",
            "diff_stat": "",
            "diff_name_only": "",
            "validation_error": None,
        }
        invocation_state = {"validation_error": None}
        invocation_delta = {
            "attributable_changed_files": [],
            "attributable_added_files": [],
            "attributable_deleted_files": [],
            "attributable_renamed_files": [],
            "preexisting_changed_files": [],
            "preexisting_untracked_files": [],
            "path_delta_details": [],
            "validation_error": None,
        }
        classification = {
            "total_files": 0,
            "files": [],
            "counts_by_category": {},
            "counts_by_risk_level": {},
            "high_risk_files": [],
        }
        diagnostics = {
            "outcome": "no_changes",
            "attention_level": "ok",
            "prompt_intents": [],
            "flags": [],
            "messages": [],
        }
        supervision = {
            "decision": "continue",
            "attention_level": "ok",
            "approval_required": False,
            "needs_review": False,
            "reasons": [],
            "messages": [],
        }
        transition = {
            "next_status": "completed",
            "reason": "supervision_decision_continue",
            "decision": "continue",
            "approval_required": False,
            "needs_review": False,
            "should_auto_complete": True,
        }
        raw = _raw_result(repo_path="/tmp/repo")

        def service_side_effect(
            run_id,
            prompt,
            repo_path,
            sandbox,
            timeout_seconds,
            prompt_contract,
            **kwargs,
        ) -> CodexDirectExecutionResult:
            event_ledger = kwargs["ledger"]
            started = event_ledger.add_event(
                run_id,
                "codex_exec_started",
                "Running Codex exec.",
                {
                    "prompt": prompt,
                    "repo_path": repo_path,
                    "timeout": timeout_seconds,
                    "sandbox": sandbox,
                    "prompt_contract": prompt_contract,
                },
            )
            finished = event_ledger.add_event(
                run_id,
                "codex_exec_finished",
                "found=True exit_code=0 timed_out=False repo_path=/tmp/repo sandbox=read-only",
                raw,
            )
            return CodexDirectExecutionResult(
                ok=True,
                run_id=run_id,
                reason_code="codex_exec_completed",
                error_message=None,
                repo_path=repo_path,
                prompt=prompt,
                sandbox=sandbox,
                command=raw["command"],
                started_event_id=started["id"],
                finished_event_id=finished["id"],
                exit_code=0,
                timed_out=False,
                duration_seconds=0.1,
                stdout=raw["stdout"],
                stderr=raw["stderr"],
                stdout_sha256=_sha(raw["stdout"]),
                stderr_sha256=_sha(raw["stderr"]),
                stdout_length=len(raw["stdout"]),
                stderr_length=len(raw["stderr"]),
                raw_process_result=raw,
                metadata={},
                persisted=True,
            )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "capture_git_snapshot", side_effect=[snapshot, snapshot]),
            mock.patch.object(cli, "capture_invocation_git_state", side_effect=[invocation_state, invocation_state]),
            mock.patch.object(cli, "compute_invocation_delta", return_value=invocation_delta),
            mock.patch.object(cli, "classify_changed_files", return_value=classification),
            mock.patch.object(cli, "analyze_prompt_repo_impact", return_value=diagnostics),
            mock.patch.object(cli, "evaluate_supervision_decision", return_value=supervision),
            mock.patch.object(cli, "status_from_supervision_decision", return_value=transition),
            mock.patch.object(cli, "execute_codex_direct_service", side_effect=service_side_effect) as service,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            flow = cli._run_codex_exec_flow(
                "run-1",
                {"id": "run-1", "status": "created"},
                "Say exactly: hello",
                "/tmp/repo",
                "read-only",
                300,
                confirm_full_access=False,
            )

        self.assertIs(flow["result"], raw)
        service.assert_called_once()
        self.assertEqual(
            [event["event_type"] for event in fake_ledger.events],
            [
                "git_snapshot_before_codex",
                "prompt_contract_parsed",
                "invocation_git_state_before",
                "codex_exec_started",
                "codex_exec_finished",
                "git_snapshot_after_codex",
                "invocation_git_state_after",
                "invocation_delta_attributed",
                "changed_file_classification",
                "prompt_repo_impact_diagnostics",
                "supervision_decision",
                "run_governance_observation",
                "run_status_transition",
            ],
        )
        self.assertEqual(fake_ledger.events[3]["message"], "Running Codex exec.")
        self.assertEqual(
            fake_ledger.events[4]["message"],
            "found=True exit_code=0 timed_out=False repo_path=/tmp/repo sandbox=read-only",
        )

    def test_direct_cli_exit_mapping_remains_outside_service(self) -> None:
        cases = [
            ("validation", {"validation_error": "Invalid sandbox.", "found": True, "timed_out": False, "exit_code": 2}, 2),
            ("missing_codex", {"validation_error": None, "found": False, "timed_out": False, "exit_code": None}, 1),
            ("timeout", {"validation_error": None, "found": True, "timed_out": True, "exit_code": None}, 124),
            ("nonzero", {"validation_error": None, "found": True, "timed_out": False, "exit_code": 37}, 37),
        ]

        for name, result, expected_exit in cases:
            with self.subTest(name=name):
                fake_ledger = FakeLedger()
                argv = [
                    "agent-loop",
                    "codex-run",
                    "run-1",
                    "--repo",
                    "/tmp/repo",
                    "--sandbox",
                    "read-only",
                    "--prompt",
                    "Say exactly: hello",
                ]
                with (
                    mock.patch.object(cli, "ledger", fake_ledger),
                    mock.patch.object(cli, "_run_codex_exec_flow", return_value={"result": result}),
                    mock.patch.object(cli.sys, "argv", argv),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()

                self.assertEqual(raised.exception.code, expected_exit)


if __name__ == "__main__":
    unittest.main()
