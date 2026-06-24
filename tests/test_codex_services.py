from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest
from unittest import mock

from agent import cli
from agent.codex_services import (
    CodexDirectExecutionResult,
    execute_codex_direct_service,
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
) -> dict:
    return {
        "mode": "exec",
        "found": found,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "validation_error": validation_error,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, prompt],
        "cwd": repo_path,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
    }


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.status_updates: list[tuple[str, object]] = []
        self._next_id = 1
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
        self.assertEqual(result.stdout_sha256, _sha(stdout))
        self.assertEqual(result.stderr_sha256, _sha(stderr))
        self.assertEqual(result.stdout_length, len(stdout))
        self.assertEqual(result.stderr_length, len(stderr))
        self.assertIs(result.raw_process_result, raw)

        self.assertEqual([event["event_type"] for event in ledger.events], ["codex_exec_started", "codex_exec_finished"])
        self.assertEqual(ledger.events[0]["message"], "Running Codex exec.")
        self.assertEqual(
            ledger.events[1]["message"],
            "found=True exit_code=0 timed_out=False repo_path=/tmp/repo sandbox=read-only",
        )
        self.assertEqual(ledger.events[0]["metadata"], {
            "prompt": prompt,
            "repo_path": repo_path,
            "timeout": 300,
            "sandbox": "read-only",
            "prompt_contract": {"confidence": "low"},
        })
        self.assertIs(ledger.events[1]["metadata"], raw)
        self.assertNotIn("stdout_sha256", ledger.events[1]["metadata"])
        self.assertNotIn("stdout_length", ledger.events[1]["metadata"])
        self.assertNotIn("duration_seconds", ledger.events[1]["metadata"])
        self.assertEqual(ledger.status_updates, [])

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
                {"repo_path": repo_path, "timeout_seconds": 123, "sandbox": "workspace-write"},
            )
        ])
        self.assertEqual(
            result.command,
            ["codex", "exec", "-C", repo_path, "-s", "workspace-write", prompt],
        )
        self.assertEqual(ledger.events[1]["metadata"]["command"], result.command)

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
