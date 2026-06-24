from __future__ import annotations

import inspect
import json
import unittest
from unittest import mock

from agent.codex_services import CodexDirectExecutionResult, execute_codex_direct_service
from agent.governance_services import PostCodexGovernanceResult
from agent.initial_codex_run_services import (
    execute_initial_direct_codex_run_service,
)
from agent.prompt_contract import parse_prompt_contract


def _snapshot(status_short: str = "") -> dict:
    return {
        "repo_path": "/tmp/repo",
        "is_git_repo": True,
        "head": "abcdef1234567890",
        "branch": "main",
        "status_short": status_short,
        "diff_stat": "",
        "diff_name_only": "",
        "commands": {},
        "validation_error": None,
        "captured_at": "2026-01-01T00:00:00+00:00",
    }


def _state() -> dict:
    return {
        "repo_path": "/tmp/repo",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "status_porcelain": "",
        "paths": {},
        "commands": {},
        "validation_error": None,
    }


def _raw_result(
    *,
    found: bool = True,
    exit_code: int | None = 0,
    timed_out: bool = False,
    validation_error: str | None = None,
    sandbox: str = "read-only",
) -> dict:
    return {
        "mode": "exec",
        "found": found,
        "codex_path": "/usr/local/bin/codex" if found else None,
        "prompt": "Say exactly: hello",
        "repo_path": "/tmp/repo",
        "sandbox": sandbox,
        "command": ["codex", "exec", "-C", "/tmp/repo", "-s", sandbox, "Say exactly: hello"],
        "cwd": "/tmp/repo",
        "exit_code": exit_code,
        "stdout": "hello\n",
        "stderr": "",
        "timed_out": timed_out,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "validation_error": validation_error,
    }


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.status_updates: list[tuple[str, object]] = []
        self._next_id = 1

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


class RecordingRunner:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else _raw_result()
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        return self.result


class RawService:
    def __init__(self, raw: dict | None = None, *, exception: Exception | None = None) -> None:
        self.raw = raw if raw is not None else _raw_result()
        self.exception = exception
        self.calls: list[tuple[tuple, dict]] = []
        self.runner = RecordingRunner(self.raw)

    def __call__(self, *args, **kwargs) -> CodexDirectExecutionResult:
        self.calls.append((args, kwargs))
        if self.exception is not None:
            raise self.exception
        service_kwargs = {
            **kwargs,
            "codex_runner": self.runner,
            "monotonic_clock": StepClock([10.0, 11.0]),
        }
        return execute_codex_direct_service(
            *args,
            **service_kwargs,
        )


class StepClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        return self.values.pop(0)


class GovernanceService:
    def __init__(
        self,
        *,
        status: str = "completed",
        sandbox: str = "read-only",
        validation_path: bool = False,
        exception: Exception | None = None,
        workspace_events: bool = False,
    ) -> None:
        self.status = status
        self.sandbox = sandbox
        self.validation_path = validation_path
        self.exception = exception
        self.workspace_events = workspace_events
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> PostCodexGovernanceResult:
        self.calls.append((args, kwargs))
        if self.exception is not None:
            raise self.exception
        run_id = args[0]
        raw = args[6]
        ledger = kwargs["ledger"]
        events_written = []
        if not self.validation_path:
            for event_type, message in (
                ("git_snapshot_after_codex", "repo_path=/tmp/repo branch=main head=abcdef123456 dirty=false"),
                ("invocation_git_state_after", "Captured post-Codex invocation git state."),
                ("invocation_delta_attributed", "Attributed invocation delta files=0."),
                ("changed_file_classification", "total_files=0 category_counts={} risk_counts={} high_risk_file_count=0"),
                ("prompt_repo_impact_diagnostics", "outcome=no_changes attention_level=ok flags=[]"),
                ("supervision_decision", "decision=continue attention_level=ok approval_required=False reasons=[]"),
                ("run_governance_observation", "Recorded explicit-contract and attributable-delta governance observation scope=matched."),
            ):
                event = ledger.add_event(run_id, event_type, message, {})
                events_written.append({"event_type": event_type, "event_id": event["id"], "message": message, "metadata": {}})
            if self.workspace_events:
                for event_type in ("workspace_write_diff_metadata_captured", "workspace_write_post_run_policy"):
                    event = ledger.add_event(run_id, event_type, event_type, {"owner": "governance"})
                    events_written.append({"event_type": event_type, "event_id": event["id"], "message": event_type, "metadata": {"owner": "governance"}})
        else:
            for event_type in ("prompt_repo_impact_diagnostics", "supervision_decision"):
                event = ledger.add_event(run_id, event_type, event_type, {})
                events_written.append({"event_type": event_type, "event_id": event["id"], "message": event_type, "metadata": {}})

        transition = {
            "previous_status": "created",
            "next_status": self.status,
            "reason": "supervision_decision_continue",
            "decision": "continue",
            "approval_required": False,
            "needs_review": False,
            "should_auto_complete": True,
        }
        status_event = ledger.add_event(run_id, "run_status_transition", "previous_status=created next_status=completed reason=supervision_decision_continue", transition)
        events_written.append({"event_type": "run_status_transition", "event_id": status_event["id"], "message": status_event["message"], "metadata": transition})
        return PostCodexGovernanceResult(
            ok=True,
            run_id=run_id,
            reason_code=transition["reason"],
            error_message=None,
            raw_execution_result=raw,
            git_before=args[7],
            git_after={} if not self.validation_path else None,
            invocation_state_before=args[8],
            invocation_state_after={} if not self.validation_path else None,
            invocation_delta={} if not self.validation_path else None,
            changed_file_classification={} if not self.validation_path else None,
            diagnostics={},
            supervision_decision={"decision": "continue"},
            workspace_write_pre_run_result=kwargs.get("workspace_write_pre_run_result"),
            workspace_write_post_run_result={"allowed": True} if self.workspace_events else None,
            governance_observation={} if not self.validation_path else None,
            previous_status="created",
            next_status=self.status,
            status_transition_event_id=status_event["id"],
            events_written=events_written,
            auto_supervision_allowed=True,
            human_review_required=False,
            metadata={"transition": transition, "expected_scope": kwargs.get("expected_scope") or {}},
            persisted=True,
        )


class InitialCodexRunCoordinatorTests(unittest.TestCase):
    def _run(
        self,
        *,
        sandbox: str = "read-only",
        raw_service: RawService | None = None,
        governance: GovernanceService | None = None,
        prompt: str = "Say exactly: hello",
        parser=parse_prompt_contract,
        confirm_full_access: bool = False,
        expected_scope: dict | None = None,
        pre_run_policy: dict | None = None,
    ):
        ledger = FakeLedger()
        raw_service = raw_service or RawService(_raw_result(sandbox=sandbox))
        governance = governance or GovernanceService(sandbox=sandbox)
        result = execute_initial_direct_codex_run_service(
            "run-1",
            {"id": "run-1", "status": "created"},
            prompt,
            "/tmp/repo",
            sandbox,
            300,
            confirm_full_access=confirm_full_access,
            expected_scope=expected_scope,
            workspace_write_pre_run_result=pre_run_policy,
            ledger=ledger,
            prompt_contract_parser=parser,
            git_snapshot_function=lambda path: _snapshot(),
            invocation_state_function=lambda path: _state(),
            raw_execution_service=raw_service,
            governance_service=governance,
        )
        return result, ledger, raw_service, governance

    def test_successful_read_only_preserves_event_order_and_boundaries(self) -> None:
        result, ledger, raw_service, governance = self._run()

        self.assertTrue(result.ok)
        self.assertEqual(result.run_status, "completed")
        self.assertEqual(result.prompt_contract["confidence"], "low")
        self.assertEqual(len(raw_service.calls), 1)
        self.assertEqual(len(governance.calls), 1)
        self.assertEqual(
            [event["event_type"] for event in ledger.events],
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
        self.assertEqual(ledger.events[0]["message"], "repo_path=/tmp/repo branch=main head=abcdef123456 dirty=false")
        self.assertEqual(ledger.events[1]["message"], "Parsed prompt contract confidence=low.")
        self.assertNotIn("exit_code", result.__dict__)

    def test_successful_workspace_write_forwards_scope_and_policy_to_governance(self) -> None:
        scope = {"explicit_files": ["agent/foo.py"]}
        pre_run_policy = {"allowed": True, "reason_code": "pre_run_ok"}
        result, ledger, _raw_service, governance = self._run(
            sandbox="workspace-write",
            raw_service=RawService(_raw_result(sandbox="workspace-write")),
            governance=GovernanceService(sandbox="workspace-write", workspace_events=True),
            expected_scope=scope,
            pre_run_policy=pre_run_policy,
        )

        self.assertTrue(result.ok)
        _, kwargs = governance.calls[0]
        self.assertIs(kwargs["expected_scope"], scope)
        self.assertIs(kwargs["workspace_write_pre_run_result"], pre_run_policy)
        workspace_events = [event for event in ledger.events if event["event_type"].startswith("workspace_write_")]
        self.assertEqual([event["metadata"]["owner"] for event in workspace_events], ["governance", "governance"])

    def test_invalid_prompt_contract_path_is_validation_shaped(self) -> None:
        def parser(prompt: str, sandbox: str) -> dict:
            return {
                "confidence": "low",
                "path_safety": {"valid": False, "invalid_paths": ["../secret.txt"]},
            }

        raw_service = RawService(_raw_result())
        result, ledger, raw_service, governance = self._run(
            parser=parser,
            raw_service=raw_service,
            governance=GovernanceService(validation_path=True),
        )

        self.assertTrue(result.ok)
        self.assertIn("../secret.txt", result.validation_error)
        self.assertEqual(raw_service.runner.calls, [])
        self.assertEqual(len(governance.calls), 1)
        self.assertEqual(
            [event["event_type"] for event in ledger.events],
            [
                "git_snapshot_before_codex",
                "prompt_contract_parsed",
                "invocation_git_state_before",
                "codex_exec_started",
                "codex_exec_finished",
                "prompt_repo_impact_diagnostics",
                "supervision_decision",
                "run_status_transition",
            ],
        )

    def test_danger_full_access_without_confirmation_is_validation_shaped(self) -> None:
        result, _ledger, raw_service, governance = self._run(
            sandbox="danger-full-access",
            raw_service=RawService(_raw_result(sandbox="danger-full-access")),
            governance=GovernanceService(validation_path=True),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.validation_error, "Codex sandbox danger-full-access requires --confirm-full-access.")
        self.assertEqual(raw_service.runner.calls, [])
        self.assertEqual(len(governance.calls), 1)

    def test_invalid_repo_or_sandbox_result_shape_still_runs_governance(self) -> None:
        raw = _raw_result(exit_code=2, validation_error="Invalid Codex sandbox.", sandbox="bad")
        result, ledger, _raw_service, governance = self._run(
            sandbox="bad",
            raw_service=RawService(raw),
            governance=GovernanceService(validation_path=True),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.validation_error, "Invalid Codex sandbox.")
        self.assertEqual(len(governance.calls), 1)
        self.assertIn("run_status_transition", [event["event_type"] for event in ledger.events])

    def test_raw_outcomes_remain_result_shaped_and_governed(self) -> None:
        cases = [
            _raw_result(found=False, exit_code=None),
            _raw_result(timed_out=True, exit_code=None),
            _raw_result(exit_code=37),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                result, _ledger, _raw_service, governance = self._run(raw_service=RawService(raw))
                self.assertTrue(result.ok)
                self.assertIs(result.raw_execution_result.raw_process_result, raw)
                self.assertEqual(len(governance.calls), 1)
                self.assertIsNotNone(result.governance_result)

    def test_unexpected_exceptions_bubble_without_invented_failure_events(self) -> None:
        with self.subTest("pre snapshot"):
            ledger = FakeLedger()
            with self.assertRaisesRegex(RuntimeError, "snapshot boom"):
                execute_initial_direct_codex_run_service(
                    "run-1",
                    {"id": "run-1", "status": "created"},
                    "Prompt",
                    "/tmp/repo",
                    "read-only",
                    300,
                    ledger=ledger,
                    git_snapshot_function=mock.Mock(side_effect=RuntimeError("snapshot boom")),
                )
            self.assertEqual(ledger.events, [])

        with self.subTest("raw service"):
            ledger = FakeLedger()
            with self.assertRaisesRegex(RuntimeError, "raw boom"):
                execute_initial_direct_codex_run_service(
                    "run-1",
                    {"id": "run-1", "status": "created"},
                    "Prompt",
                    "/tmp/repo",
                    "read-only",
                    300,
                    ledger=ledger,
                    git_snapshot_function=lambda path: _snapshot(),
                    invocation_state_function=lambda path: _state(),
                    raw_execution_service=RawService(exception=RuntimeError("raw boom")),
                )
            self.assertEqual([event["event_type"] for event in ledger.events], [
                "git_snapshot_before_codex",
                "prompt_contract_parsed",
                "invocation_git_state_before",
            ])

        with self.subTest("governance"):
            ledger = FakeLedger()
            with self.assertRaisesRegex(RuntimeError, "governance boom"):
                execute_initial_direct_codex_run_service(
                    "run-1",
                    {"id": "run-1", "status": "created"},
                    "Prompt",
                    "/tmp/repo",
                    "read-only",
                    300,
                    ledger=ledger,
                    git_snapshot_function=lambda path: _snapshot(),
                    invocation_state_function=lambda path: _state(),
                    raw_execution_service=RawService(),
                    governance_service=GovernanceService(exception=RuntimeError("governance boom")),
                )
            self.assertNotIn("controller_failed", [event["event_type"] for event in ledger.events])

    def test_no_private_cli_chatgpt_or_supervision_coupling(self) -> None:
        import agent.initial_codex_run_services as service_module
        import agent.local_controller as local_controller_module

        source = inspect.getsource(service_module)
        self.assertNotIn("agent.cli", source)
        self.assertNotIn("agent.cli", inspect.getsource(local_controller_module.default_initial_run_executor))
        with (
            mock.patch("agent.chatgpt_services.submit_feedback_to_chatgpt_service") as submit,
            mock.patch("agent.chatgpt_services.capture_chatgpt_response_service") as capture,
            mock.patch("agent.supervision_services.run_supervision_step") as supervise,
        ):
            self._run()
        submit.assert_not_called()
        capture.assert_not_called()
        supervise.assert_not_called()

    def test_generic_full_access_can_be_confirmed_independent_of_controller_layer(self) -> None:
        raw = _raw_result(sandbox="danger-full-access")
        result, _ledger, raw_service, _governance = self._run(
            sandbox="danger-full-access",
            raw_service=RawService(raw),
            confirm_full_access=True,
        )

        self.assertTrue(result.ok)
        self.assertIsNone(result.validation_error)
        self.assertEqual(len(raw_service.runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
