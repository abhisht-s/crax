from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent.extracted_prompt_services import execute_extracted_codex_prompt_service


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sentinel_response(prompt: str = "Say exactly: extracted") -> str:
    return f"BEGIN_NEXT_CODEX_PROMPT\n{prompt}\nEND_NEXT_CODEX_PROMPT"


class FakeLedger:
    def __init__(self, run: dict | None, events: list[dict] | None = None) -> None:
        self.run = run
        self.events = list(events or [])
        self.status_updates: list[tuple[str, object]] = []
        self._next_id = max(
            [int(event.get("id") or 0) for event in self.events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1

    def get_run(self, run_id: str) -> dict | None:
        return self.run

    def list_events(self, run_id: str) -> list[dict]:
        return self.events

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
        self.run = {**(self.run or {}), "status": getattr(status, "value", str(status))}


class RecordingCoordinator:
    def __init__(self, flow: dict | None = None, exception: Exception | None = None) -> None:
        self.flow = flow if flow is not None else _flow()
        self.exception = exception
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        if self.exception is not None:
            raise self.exception
        return self.flow


def _submission(event_id: int = 1) -> dict:
    return {
        "id": event_id,
        "event_type": "gpt_feedback_submission_verified",
        "metadata_json": json.dumps({"reason_code": "chatgpt_submission_verified"}, sort_keys=True),
    }


def _capture(submission: dict, event_id: int = 2, response: str | None = None) -> dict:
    response_text = response or _sentinel_response()
    return {
        "id": event_id,
        "event_type": "gpt_response_captured",
        "metadata_json": json.dumps(
            {
                "matched_submission_event_id": submission["id"],
                "response_text": response_text,
                "response_sha256": _sha(response_text),
            },
            sort_keys=True,
        ),
    }


def _extraction(
    capture: dict,
    submission: dict,
    event_id: int = 3,
    *,
    prompt: str = "Say exactly: extracted",
    method: str = "sentinel_block",
    source_event_id: int | None = None,
    source_response_sha256: str | None = None,
    prompt_sha256: str | None = None,
    prompt_path: str | None = None,
) -> dict:
    capture_metadata = json.loads(capture["metadata_json"])
    metadata = {
        "source_event_id": capture["id"] if source_event_id is None else source_event_id,
        "source_event_type": "gpt_response_captured",
        "source_response_sha256": (
            capture_metadata["response_sha256"]
            if source_response_sha256 is None
            else source_response_sha256
        ),
        "matched_submission_event_id": submission["id"],
        "extraction_method": method,
        "prompt_text": prompt,
        "prompt_length": len(prompt),
        "prompt_sha256": prompt_sha256 or _sha(prompt),
        "prompt_count_detected": 1,
        "selected_prompt_index": 0,
        "safety_status": "requires_human_review",
        "warnings": [],
    }
    if prompt_path is not None:
        metadata["prompt_path"] = prompt_path
    return {
        "id": event_id,
        "event_type": "next_codex_prompt_extracted",
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


def _events(prompt: str = "Say exactly: extracted", **extraction_kwargs) -> list[dict]:
    submission = _submission()
    response = _sentinel_response(prompt)
    capture = _capture(submission, response=response)
    extraction = _extraction(capture, submission, prompt=prompt, **extraction_kwargs)
    return [submission, capture, extraction]


def _flow(
    *,
    found: bool = True,
    exit_code: int | None = 0,
    timed_out: bool = False,
    validation_error: str | None = None,
    status: str = "completed",
    decision: str = "continue",
    objective_failures: list[str] | None = None,
) -> dict:
    return {
        "result": {
            "found": found,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "validation_error": validation_error,
        },
        "supervision_decision": {"decision": decision},
        "transition": {"next_status": status},
        "governance_observation": {"objective_failures": objective_failures or []},
    }


def _run(
    ledger: FakeLedger,
    coordinator: RecordingCoordinator | None = None,
    **kwargs,
):
    return execute_extracted_codex_prompt_service(
        "run-1",
        ledger.run,
        kwargs.pop("repo_path_text", tempfile.gettempdir()),
        kwargs.pop("sandbox", "read-only"),
        kwargs.pop("timeout", None),
        confirm_full_access=kwargs.pop("confirm_full_access", False),
        allow_full_access=kwargs.pop("allow_full_access", False),
        approval_mode=kwargs.pop("approval_mode", "human"),
        ledger=ledger,
        codex_flow_coordinator=coordinator or RecordingCoordinator(),
        **kwargs,
    )


class ExtractedPromptServiceTests(unittest.TestCase):
    def test_valid_read_only_prompt_writes_wrapper_order_and_calls_coordinator_once(self) -> None:
        ledger = FakeLedger({"id": "run-1", "status": "completed"}, _events())
        coordinator_calls = []

        def coordinator(*args, **kwargs) -> dict:
            coordinator_calls.append((args, kwargs))
            ledger.add_event("run-1", "codex_exec_started", "Running Codex exec.", {"prompt": args[2]})
            ledger.add_event(
                "run-1",
                "codex_exec_finished",
                "found=True exit_code=0 timed_out=False repo_path=/tmp sandbox=read-only",
                _flow()["result"],
            )
            return _flow()

        with mock.patch("agent.cli._codex_run_auto_supervise_exit_code") as auto_handoff:
            result = _run(
                ledger,
                coordinator,
                expected_extraction_event_id=3,
                expected_prompt_sha256=_sha("Say exactly: extracted"),
                expected_prompt_text="Say exactly: extracted",
                expected_extraction_method="sentinel_block",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(coordinator_calls), 1)
        auto_handoff.assert_not_called()
        self.assertEqual(
            [event["event_type"] for event in ledger.events[3:]],
            [
                "extracted_codex_prompt_selected",
                "extracted_codex_prompt_run_started",
                "codex_exec_started",
                "codex_exec_finished",
                "extracted_codex_prompt_run_finished",
            ],
        )
        self.assertEqual(
            [event["event_type"] for event in result.events_written],
            [
                "extracted_codex_prompt_selected",
                "extracted_codex_prompt_run_started",
                "extracted_codex_prompt_run_finished",
            ],
        )
        self.assertEqual(result.selected_event_id, 3)
        self.assertEqual(result.selected_prompt_sha256, _sha("Say exactly: extracted"))

    def test_valid_workspace_write_preserves_pre_run_policy_and_expected_scope(self) -> None:
        ledger = FakeLedger({"id": "run-1", "status": "completed"}, _events())
        coordinator = RecordingCoordinator(_flow())
        pre_run_policy = {
            "tier": "workspace_write_scoped_auto",
            "allowed": True,
            "reason_code": "workspace_write_scoped_auto",
        }
        expected_scope = {
            "explicit_files": ["agent/foo.py"],
            "allowed_dirs": [],
            "allowed_categories": ["python_source"],
            "denied_categories": [],
            "max_changed_files": 4,
            "allow_deletions": False,
            "allow_renames": False,
            "confidence": "explicit",
        }

        result = _run(
            ledger,
            coordinator,
            sandbox="workspace-write",
            approval_mode="auto",
            workspace_write_pre_run_policy=pre_run_policy,
            expected_scope=expected_scope,
            expected_extraction_event_id=3,
            expected_prompt_sha256=_sha("Say exactly: extracted"),
            expected_prompt_text="Say exactly: extracted",
            expected_extraction_method="sentinel_block",
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(coordinator.calls), 1)
        event_types = [event["event_type"] for event in ledger.events]
        self.assertNotIn("workspace_write_pre_run_policy", event_types)
        for event in result.events_written:
            metadata = event["metadata"]
            self.assertEqual(metadata["pre_run_policy"], pre_run_policy)
            self.assertEqual(metadata["expected_scope"], expected_scope)
            self.assertEqual(metadata["reason_code"], "workspace_write_scoped_auto")
            self.assertTrue(metadata["auto_executed"])

    def test_integrity_and_freshness_failures_block_before_wrapper_events(self) -> None:
        prompt = "Say exactly: extracted"
        submission = _submission()
        capture = _capture(submission, response=_sentinel_response(prompt))
        first = _extraction(capture, submission, event_id=3, prompt="Prompt A")
        second = _extraction(capture, submission, event_id=4, prompt="Prompt B")
        cases = [
            ("missing extraction", [submission, capture], {}, "invalid_extracted_prompt", 1),
            (
                "newer extraction",
                [submission, capture, first, second],
                {
                    "expected_extraction_event_id": 3,
                    "expected_prompt_sha256": _sha("Prompt A"),
                    "expected_prompt_text": "Prompt A",
                    "expected_extraction_method": "sentinel_block",
                },
                "extracted_prompt_changed_after_approval",
                1,
            ),
            (
                "source capture mismatch",
                [submission, capture, _extraction(capture, submission, source_event_id=999)],
                {"expected_extraction_event_id": 3},
                "invalid_extracted_prompt",
                1,
            ),
            (
                "source response sha mismatch",
                [submission, capture, _extraction(capture, submission, source_response_sha256="bad")],
                {"expected_extraction_event_id": 3},
                "invalid_extracted_prompt",
                1,
            ),
            (
                "prompt sha mismatch",
                [submission, capture, _extraction(capture, submission, prompt_sha256="bad")],
                {"expected_extraction_event_id": 3},
                "invalid_extracted_prompt",
                1,
            ),
            (
                "expected prompt text mismatch",
                [submission, capture, _extraction(capture, submission, prompt=prompt)],
                {"expected_extraction_event_id": 3, "expected_prompt_text": "changed"},
                "extracted_prompt_changed_after_approval",
                1,
            ),
            (
                "expected method mismatch",
                [submission, capture, _extraction(capture, submission, method="labeled_fenced_code_block")],
                {"expected_extraction_event_id": 3, "expected_extraction_method": "sentinel_block"},
                "extracted_prompt_changed_after_approval",
                1,
            ),
        ]

        for name, events, kwargs, reason, expected_exit in cases:
            with self.subTest(name=name):
                ledger = FakeLedger({"id": "run-1", "status": "completed"}, events)
                coordinator = RecordingCoordinator()
                result = _run(ledger, coordinator, **kwargs)

                self.assertEqual(result.reason_code, reason)
                self.assertEqual(result.exit_code, expected_exit)
                self.assertEqual(coordinator.calls, [])
                self.assertFalse(
                    any(event["event_type"].startswith("extracted_codex_prompt_") for event in ledger.events)
                )

    def test_artifact_missing_is_warning_but_mismatch_blocks(self) -> None:
        prompt = "Say exactly: artifact"
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.md")
            valid_events = _events(prompt, prompt_path=missing)
            valid_ledger = FakeLedger({"id": "run-1", "status": "completed"}, valid_events)
            valid_coordinator = RecordingCoordinator()

            allowed = _run(
                valid_ledger,
                valid_coordinator,
                expected_extraction_event_id=3,
                expected_prompt_sha256=_sha(prompt),
                expected_prompt_text=prompt,
                expected_extraction_method="sentinel_block",
            )

            self.assertEqual(allowed.exit_code, 0)
            self.assertEqual(allowed.artifact_path, missing)
            self.assertEqual(allowed.artifact_status, "missing_allowed")
            self.assertIn(
                "extracted prompt artifact is missing; using validated prompt_text metadata.",
                allowed.metadata["selection_warnings"],
            )
            self.assertEqual(len(valid_coordinator.calls), 1)

            mismatch_path = Path(tmp) / "next.md"
            mismatch_path.write_text("different", encoding="utf-8")
            invalid_ledger = FakeLedger(
                {"id": "run-1", "status": "completed"},
                _events(prompt, prompt_path=str(mismatch_path)),
            )
            invalid_coordinator = RecordingCoordinator()

            blocked = _run(
                invalid_ledger,
                invalid_coordinator,
                expected_extraction_event_id=3,
                expected_prompt_sha256=_sha(prompt),
                expected_prompt_text=prompt,
                expected_extraction_method="sentinel_block",
            )

            self.assertEqual(blocked.reason_code, "invalid_extracted_prompt")
            self.assertEqual(blocked.exit_code, 1)
            self.assertEqual(blocked.artifact_status, "sha_mismatch")
            self.assertEqual(invalid_coordinator.calls, [])
            self.assertFalse(
                any(event["event_type"].startswith("extracted_codex_prompt_") for event in invalid_ledger.events)
            )

    def test_sandbox_continuation_and_full_access_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "not-a-dir"
            file_path.write_text("x", encoding="utf-8")
            cases = [
                (
                    "missing run",
                    FakeLedger(None, _events()),
                    {},
                    "run_not_found",
                    1,
                ),
                (
                    "missing repo",
                    FakeLedger({"id": "run-1", "status": "completed"}, _events()),
                    {"repo_path_text": str(Path(tmp) / "missing")},
                    "repo_missing",
                    2,
                ),
                (
                    "repo not directory",
                    FakeLedger({"id": "run-1", "status": "completed"}, _events()),
                    {"repo_path_text": str(file_path)},
                    "repo_not_directory",
                    2,
                ),
                (
                    "invalid sandbox",
                    FakeLedger({"id": "run-1", "status": "completed"}, _events()),
                    {"sandbox": "bad"},
                    "invalid_sandbox",
                    2,
                ),
                (
                    "continuation denied",
                    FakeLedger({"id": "run-1", "status": "needs_review"}, _events()),
                    {},
                    "continuation_denied",
                    2,
                ),
                (
                    "full access disallowed",
                    FakeLedger({"id": "run-1", "status": "completed"}, _events()),
                    {"sandbox": "danger-full-access", "allow_full_access": False, "confirm_full_access": True},
                    "danger_full_access_blocked",
                    2,
                ),
                (
                    "full access unconfirmed",
                    FakeLedger({"id": "run-1", "status": "completed"}, _events()),
                    {"sandbox": "danger-full-access", "allow_full_access": True, "confirm_full_access": False},
                    "full_access_confirmation_required",
                    2,
                ),
            ]

            for name, ledger, kwargs, reason, expected_exit in cases:
                with self.subTest(name=name):
                    coordinator = RecordingCoordinator()
                    result = _run(ledger, coordinator, **kwargs)
                    self.assertEqual(result.reason_code, reason)
                    self.assertEqual(result.exit_code, expected_exit)
                    self.assertEqual(coordinator.calls, [])
                    self.assertFalse(
                        any(event["event_type"].startswith("extracted_codex_prompt_") for event in ledger.events)
                    )

        confirmed_ledger = FakeLedger({"id": "run-1", "status": "completed"}, _events())
        confirmed_coordinator = RecordingCoordinator(_flow())
        confirmed = _run(
            confirmed_ledger,
            confirmed_coordinator,
            sandbox="danger-full-access",
            allow_full_access=True,
            confirm_full_access=True,
        )
        self.assertEqual(confirmed.exit_code, 0)
        self.assertEqual(len(confirmed_coordinator.calls), 1)

    def test_downstream_outcomes_map_to_current_exit_behavior_and_finish_metadata(self) -> None:
        cases = [
            ("nonzero", _flow(exit_code=37), 37, 37, False, "completed", "continue", None),
            ("timeout", _flow(exit_code=None, timed_out=True), 124, None, True, "completed", "continue", None),
            ("missing codex", _flow(found=False, exit_code=None), 1, None, False, "completed", "continue", None),
            (
                "validation",
                _flow(exit_code=2, validation_error="invalid path", status="needs_review", decision="needs_review"),
                2,
                2,
                False,
                "needs_review",
                "needs_review",
                "invalid path",
            ),
        ]
        for name, flow, expected_exit, codex_exit, timed_out, status, decision, validation_error in cases:
            with self.subTest(name=name):
                ledger = FakeLedger({"id": "run-1", "status": "completed"}, _events())
                result = _run(ledger, RecordingCoordinator(flow))
                finished = ledger.events[-1]
                metadata = finished["metadata"]

                self.assertEqual(result.exit_code, expected_exit)
                self.assertEqual(finished["event_type"], "extracted_codex_prompt_run_finished")
                self.assertEqual(metadata["exit_code"], codex_exit)
                self.assertEqual(metadata["timed_out"], timed_out)
                self.assertEqual(metadata["status"], status)
                self.assertEqual(metadata["supervision_decision"], decision)
                self.assertEqual(metadata["validation_error"], validation_error)

    def test_unexpected_coordinator_exception_bubbles_without_finished_wrapper(self) -> None:
        ledger = FakeLedger({"id": "run-1", "status": "completed"}, _events())
        coordinator = RecordingCoordinator(exception=RuntimeError("boom"))

        with mock.patch("agent.cli._codex_run_auto_supervise_exit_code") as auto_handoff:
            with self.assertRaisesRegex(RuntimeError, "boom"):
                _run(
                    ledger,
                    coordinator,
                    expected_extraction_event_id=3,
                    expected_prompt_sha256=_sha("Say exactly: extracted"),
                    expected_prompt_text="Say exactly: extracted",
                    expected_extraction_method="sentinel_block",
                )

        auto_handoff.assert_not_called()
        self.assertEqual(
            [event["event_type"] for event in ledger.events[3:]],
            ["extracted_codex_prompt_selected", "extracted_codex_prompt_run_started"],
        )
        self.assertEqual(len(coordinator.calls), 1)

    def test_direct_path_still_allows_non_sentinel_when_no_expected_method_is_supplied(self) -> None:
        prompt = "Say exactly: fenced"
        events = _events(prompt, method="labeled_fenced_code_block")
        ledger = FakeLedger({"id": "run-1", "status": "completed"}, events)
        coordinator = RecordingCoordinator()

        result = _run(ledger, coordinator, expected_prompt_sha256=_sha(prompt))

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.selected_method, "labeled_fenced_code_block")
        self.assertEqual(len(coordinator.calls), 1)


if __name__ == "__main__":
    unittest.main()
