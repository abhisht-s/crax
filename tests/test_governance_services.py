from __future__ import annotations

import json
import unittest

from agent.governance_services import (
    PostCodexGovernanceCallbacks,
    apply_post_codex_governance_service,
)
from agent.workspace_write_policy import ExpectedScope, verify_workspace_write_post_run


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
        "prompt": "Update README.md.",
        "repo_path": "/tmp/repo",
        "sandbox": sandbox,
        "command": ["codex", "exec", "-C", "/tmp/repo", "-s", sandbox, "Update README.md."],
        "exit_code": exit_code,
        "stdout": "done\n",
        "stderr": "",
        "timed_out": timed_out,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "validation_error": validation_error,
    }


def _snapshot(status_short: str = "", validation_error: str | None = None) -> dict:
    return {
        "repo_path": "/tmp/repo",
        "is_git_repo": validation_error is None,
        "head": "abcdef1234567890",
        "branch": "main",
        "status_short": status_short,
        "diff_stat": "",
        "diff_name_only": "",
        "commands": {},
        "validation_error": validation_error,
        "captured_at": "2026-01-01T00:00:00+00:00",
    }


def _state(validation_error: str | None = None) -> dict:
    return {
        "repo_path": "/tmp/repo",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "status_porcelain": "",
        "paths": {},
        "commands": {},
        "validation_error": validation_error,
    }


def _delta(paths: list[str] | None = None, *, validation_error: str | None = None) -> dict:
    paths = paths or []
    return {
        "attribution_version": "invocation_delta_v1",
        "attributable_changed_files": paths,
        "attributable_added_files": [],
        "attributable_deleted_files": [],
        "attributable_renamed_files": [],
        "attributable_staged_paths": [],
        "attributable_worktree_paths": paths,
        "preexisting_changed_files": [],
        "preexisting_untracked_files": [],
        "path_delta_details": [
            {
                "path": path,
                "change_type": "modified",
                "diff_unified_zero": "+small local change\n",
            }
            for path in paths
        ],
        "not_evaluable_paths": [],
        "validation_error": validation_error,
    }


def _classification(paths: list[str]) -> dict:
    return {
        "total_files": len(paths),
        "files": [
            {
                "path": path,
                "category": "docs" if path.endswith(".md") else "python_source",
                "risk_level": "low" if path.endswith(".md") else "medium",
                "reason": "test classifier",
            }
            for path in paths
        ],
        "counts_by_category": {
            "docs": len([path for path in paths if path.endswith(".md")]),
            "python_source": len([path for path in paths if path.endswith(".py")]),
        },
        "counts_by_risk_level": {
            "low": len([path for path in paths if path.endswith(".md")]),
            "medium": len([path for path in paths if path.endswith(".py")]),
            "high": 0,
        },
        "high_risk_files": [],
    }


def _diagnostics(flags: list[str] | None = None) -> dict:
    flags = flags or []
    return {
        "outcome": "clean_expected_no_changes" if not flags else "clean_completed_with_changes",
        "attention_level": "ok" if not flags else "info",
        "flags": flags,
        "messages": [],
        "prompt_intents": [],
        "changed_files_count": 0,
        "high_risk_files_count": 0,
        "changed_files": [],
        "high_risk_files": [],
        "codex_exit_code": 0,
        "codex_timed_out": False,
        "before_dirty": False,
        "after_dirty": False,
    }


class FakeLedger:
    def __init__(self, run_status: str = "created", seed_codex: bool = True) -> None:
        self.events: list[dict] = []
        self.status_updates: list[tuple[str, object]] = []
        self.run = {"id": "run-1", "status": run_status}
        self._next_id = 1
        if seed_codex:
            self.add_event("run-1", "codex_exec_finished", "raw finished", _raw_result())

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

    def list_events(self, run_id: str) -> list[dict]:
        return self.events

    def update_run_status(self, run_id: str, status: object) -> None:
        self.status_updates.append((run_id, status))
        self.run = {**self.run, "status": getattr(status, "value", str(status))}


class RecordingCallable:
    def __init__(self, value=None, exception: Exception | None = None) -> None:
        self.value = value
        self.exception = exception
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exception is not None:
            raise self.exception
        return self.value


class GovernanceServiceTests(unittest.TestCase):
    def _apply(
        self,
        *,
        raw: dict | None = None,
        sandbox: str = "read-only",
        git_before: dict | None = None,
        git_after: dict | None = None,
        invocation_before: dict | None = None,
        invocation_after: dict | None = None,
        delta: dict | None = None,
        expected_scope: dict | None = None,
        ledger: FakeLedger | None = None,
        **overrides,
    ):
        raw = raw or _raw_result(sandbox=sandbox)
        ledger = ledger or FakeLedger()
        delta = delta if delta is not None else _delta([])
        classifier = overrides.pop("file_classifier_function", lambda paths: _classification(paths))
        return apply_post_codex_governance_service(
            "run-1",
            ledger.run,
            "Update README.md.",
            "/tmp/repo",
            sandbox,
            {
                "confidence": "low",
                "path_safety": {"valid": True, "invalid_paths": []},
                "read_only": {"explicit": False, "matched_phrases": []},
                "allowed_paths": [],
                "allowed_path_groups": [],
                "excluded_areas": [],
            },
            raw,
            git_before if git_before is not None else _snapshot(),
            invocation_before if invocation_before is not None else _state(),
            expected_scope=expected_scope,
            ledger=ledger,
            git_snapshot_function=overrides.pop("git_snapshot_function", RecordingCallable(git_after or _snapshot())),
            invocation_state_function=overrides.pop("invocation_state_function", RecordingCallable(invocation_after or _state())),
            delta_function=overrides.pop("delta_function", RecordingCallable(delta)),
            file_classifier_function=classifier,
            diagnostics_evaluator=overrides.pop("diagnostics_evaluator", lambda *args: _diagnostics()),
            workspace_write_post_run_evaluator=overrides.pop("workspace_write_post_run_evaluator", None)
            or verify_workspace_write_post_run,
            **overrides,
        )

    def test_read_only_success_writes_expected_post_codex_events(self) -> None:
        ledger = FakeLedger()
        result = self._apply(ledger=ledger)

        self.assertTrue(result.ok)
        self.assertEqual(result.next_status, "completed")
        self.assertEqual(result.reason_code, "supervision_decision_continue")
        self.assertEqual(
            [event["event_type"] for event in ledger.events[1:]],
            [
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
        self.assertNotIn("workspace_write_post_run_policy", [event["event_type"] for event in ledger.events])
        self.assertEqual(ledger.status_updates[0][1].value, "completed")
        self.assertFalse(any(event["event_type"].startswith("gpt_") for event in ledger.events))
        self.assertEqual([event["event_type"] for event in ledger.events if event["event_type"].startswith("codex_exec")], ["codex_exec_finished"])

    def test_workspace_write_allowed_writes_current_policy_metadata(self) -> None:
        ledger = FakeLedger()
        scope = ExpectedScope(
            explicit_files=["agent/foo.py"],
            allowed_categories=["python_source"],
        ).to_dict()
        result = self._apply(
            ledger=ledger,
            sandbox="workspace-write",
            raw=_raw_result(sandbox="workspace-write"),
            delta=_delta(["agent/foo.py"]),
            expected_scope=scope,
        )

        event_types = [event["event_type"] for event in ledger.events]
        self.assertIn("workspace_write_diff_metadata_captured", event_types)
        self.assertIn("workspace_write_post_run_policy", event_types)
        self.assertNotIn("human_required_after_write", event_types)
        self.assertEqual(result.workspace_write_post_run_result["reason_code"], "post_run_diff_within_expected_scope")
        self.assertEqual(result.next_status, "completed")
        self.assertFalse(any("reset" in str(event["metadata"]).lower() for event in ledger.events))

    def test_workspace_write_diff_metadata_unavailable_records_human_required_without_status_change(self) -> None:
        ledger = FakeLedger()
        result = self._apply(
            ledger=ledger,
            sandbox="workspace-write",
            raw=_raw_result(sandbox="workspace-write"),
            delta=_delta(["agent/foo.py"], validation_error="git status --porcelain failed or timed out"),
        )

        event_types = [event["event_type"] for event in ledger.events]
        self.assertIn("workspace_write_diff_metadata_captured", event_types)
        self.assertIn("workspace_write_post_run_policy", event_types)
        self.assertIn("human_required_after_write", event_types)
        self.assertEqual(result.workspace_write_post_run_result["reason_code"], "post_run_diff_metadata_unavailable")
        self.assertEqual(result.next_status, "completed")
        self.assertTrue(result.human_review_required)

    def test_objective_raw_outcomes_preserve_status_reasons(self) -> None:
        cases = [
            (_raw_result(exit_code=37), "codex_nonzero_exit"),
            (_raw_result(exit_code=None, timed_out=True), "codex_timed_out"),
            (_raw_result(found=False, exit_code=None), "codex_not_found"),
        ]
        for raw, reason in cases:
            with self.subTest(reason=reason):
                ledger = FakeLedger()
                result = self._apply(ledger=ledger, raw=raw)
                self.assertEqual(result.next_status, "needs_review")
                self.assertEqual(result.reason_code, reason)
                self.assertIn("git_snapshot_after_codex", [event["event_type"] for event in ledger.events])

    def test_validation_error_skips_post_git_delta_classification_governance(self) -> None:
        ledger = FakeLedger()
        git_after = RecordingCallable(_snapshot())
        invocation_after = RecordingCallable(_state())
        delta = RecordingCallable(_delta([]))

        result = self._apply(
            ledger=ledger,
            raw=_raw_result(exit_code=2, validation_error="invalid path"),
            git_snapshot_function=git_after,
            invocation_state_function=invocation_after,
            delta_function=delta,
        )

        self.assertEqual(result.next_status, "needs_review")
        self.assertEqual(result.reason_code, "codex_validation_failed_before_running")
        self.assertEqual(git_after.calls, [])
        self.assertEqual(invocation_after.calls, [])
        self.assertEqual(delta.calls, [])
        self.assertEqual(
            [event["event_type"] for event in ledger.events[1:]],
            ["prompt_repo_impact_diagnostics", "supervision_decision", "run_status_transition"],
        )

    def test_dirty_worktree_preexisting_paths_stay_separate_and_record_only(self) -> None:
        ledger = FakeLedger()
        delta = _delta(["README.md"])
        delta["preexisting_changed_files"] = ["agent/cli.py"]
        delta["preexisting_untracked_files"] = ["notes.txt"]

        result = self._apply(
            ledger=ledger,
            sandbox="workspace-write",
            raw=_raw_result(sandbox="workspace-write"),
            git_before=_snapshot(" M agent/cli.py\n?? notes.txt\n"),
            delta=delta,
            diagnostics_evaluator=lambda *args: _diagnostics(["repo_dirty_before_codex"]),
        )

        self.assertEqual(result.changed_file_classification["total_files"], 1)
        self.assertEqual(result.governance_observation["attributable_changed_files"], ["README.md"])
        self.assertEqual(result.governance_observation["preexisting_changed_files"], ["agent/cli.py"])
        self.assertEqual(result.governance_observation["preexisting_untracked_files"], ["notes.txt"])
        self.assertEqual(result.supervision_decision["decision"], "record_only")
        self.assertEqual(result.next_status, "completed")

    def test_validation_error_in_snapshot_and_delta_is_recorded_not_caught(self) -> None:
        ledger = FakeLedger()
        result = self._apply(
            ledger=ledger,
            git_after=_snapshot(validation_error="Repo path does not exist: /tmp/repo"),
            invocation_after=_state(validation_error="git status --porcelain failed or timed out"),
            delta=_delta([], validation_error="git status --porcelain failed or timed out"),
        )

        after_event = next(event for event in ledger.events if event["event_type"] == "git_snapshot_after_codex")
        delta_event = next(event for event in ledger.events if event["event_type"] == "invocation_delta_attributed")
        self.assertEqual(after_event["metadata"]["validation_error"], "Repo path does not exist: /tmp/repo")
        self.assertEqual(delta_event["metadata"]["validation_error"], "git status --porcelain failed or timed out")
        self.assertEqual(result.next_status, "completed")

    def test_diagnostics_exception_becomes_none_and_status_needs_review(self) -> None:
        ledger = FakeLedger()
        warnings = []
        result = self._apply(
            ledger=ledger,
            diagnostics_evaluator=RecordingCallable(exception=RuntimeError("diag boom")),
            callbacks=PostCodexGovernanceCallbacks(diagnostics_warning=lambda exc: warnings.append(str(exc))),
        )

        self.assertEqual(warnings, ["diag boom"])
        self.assertIsNone(result.diagnostics)
        self.assertEqual(result.supervision_decision["decision"], "needs_review")
        self.assertEqual(result.next_status, "needs_review")

    def test_unexpected_classifier_workspace_policy_and_status_failures_bubble(self) -> None:
        with self.subTest("classifier"):
            ledger = FakeLedger()
            with self.assertRaisesRegex(RuntimeError, "classifier boom"):
                self._apply(
                    ledger=ledger,
                    delta=_delta(["README.md"]),
                    file_classifier_function=RecordingCallable(exception=RuntimeError("classifier boom")),
                )
            self.assertNotIn("changed_file_classification", [event["event_type"] for event in ledger.events])

        with self.subTest("workspace policy"):
            ledger = FakeLedger()
            with self.assertRaisesRegex(RuntimeError, "policy boom"):
                self._apply(
                    ledger=ledger,
                    sandbox="workspace-write",
                    raw=_raw_result(sandbox="workspace-write"),
                    delta=_delta(["agent/foo.py"]),
                    workspace_write_post_run_evaluator=RecordingCallable(exception=RuntimeError("policy boom")),
                )
            self.assertIn("workspace_write_diff_metadata_captured", [event["event_type"] for event in ledger.events])
            self.assertNotIn("workspace_write_post_run_policy", [event["event_type"] for event in ledger.events])

        with self.subTest("status update"):
            ledger = FakeLedger()

            def fail_status(*args):
                raise RuntimeError("status boom")

            with self.assertRaisesRegex(RuntimeError, "status boom"):
                self._apply(ledger=ledger, status_update_function=fail_status)
            self.assertNotIn("run_status_transition", [event["event_type"] for event in ledger.events])


if __name__ == "__main__":
    unittest.main()
