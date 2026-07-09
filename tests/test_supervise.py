from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent import cli, ledger as default_ledger
from agent import supervision_services as supervision_services_module
from agent.chatgpt_destination_gate import (
    ChatGPTDestinationSnapshot,
    DestinationEvidenceCandidate,
)
from agent.codex_terminal import run_command
from agent.file_classifier import classify_changed_files
from agent.git_snapshot import (
    attributable_paths,
    capture_invocation_git_state,
    compute_invocation_delta,
)
from agent.prompt_contract import parse_prompt_contract
from agent.supervise import SuperviseAction, SupervisePlan, detect_next_supervise_action
from agent.workspace_write_policy import (
    ExpectedScope,
    classify_workspace_write_prompt,
    verify_workspace_write_post_run,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo: str, *args: str) -> dict:
    result = run_command(["git", *args], cwd=repo, timeout_seconds=30)
    if result["exit_code"] != 0 or result["timed_out"]:
        raise AssertionError(result)
    return result


class PromptContractTests(unittest.TestCase):
    def test_generic_words_do_not_create_read_only_contract(self) -> None:
        prompt = "Perform an audit and temporary local test. Inspect README.md."
        contract = parse_prompt_contract(prompt, "workspace-write").to_dict()
        self.assertFalse(contract["read_only"]["explicit"])
        self.assertEqual(contract["allowed_paths"], [])

    def test_explicit_read_only_and_allowed_path_contracts(self) -> None:
        contract = parse_prompt_contract("READ-ONLY TASK. Do not modify files.", "read-only").to_dict()
        self.assertTrue(contract["read_only"]["explicit"])

        contract = parse_prompt_contract("Only edit README.md", "workspace-write").to_dict()
        self.assertEqual(contract["allowed_paths"], [{"path": "README.md", "mode": "only"}])
        self.assertEqual(contract["confidence"], "high")

    def test_explicit_excluded_areas_and_bad_paths(self) -> None:
        contract = parse_prompt_contract(
            "Only edit ../README.md. Do not modify backend. No database changes.",
            "workspace-write",
        ).to_dict()
        self.assertFalse(contract["path_safety"]["valid"])
        self.assertIn("../README.md", contract["path_safety"]["invalid_paths"])
        self.assertEqual(
            [item["area"] for item in contract["excluded_areas"]],
            ["backend", "database"],
        )


class InvocationDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_path = self._tmp.name
        _git(self.repo_path, "init")
        _git(self.repo_path, "config", "user.email", "test@example.com")
        _git(self.repo_path, "config", "user.name", "Test User")
        Path(self.repo_path, "README.md").write_text("base\n", encoding="utf-8")
        Path(self.repo_path, "agent.py").write_text("print('base')\n", encoding="utf-8")
        _git(self.repo_path, "add", "README.md", "agent.py")
        _git(self.repo_path, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dirty_tracked_and_untracked_unchanged_are_preexisting_only(self) -> None:
        Path(self.repo_path, "agent.py").write_text("print('dirty')\n", encoding="utf-8")
        Path(self.repo_path, "notes.txt").write_text("preexisting\n", encoding="utf-8")
        before = capture_invocation_git_state(self.repo_path)
        Path(self.repo_path, "README.md").write_text("base\nnew section\n", encoding="utf-8")
        after = capture_invocation_git_state(self.repo_path)
        delta = compute_invocation_delta(before, after)

        self.assertEqual(attributable_paths(delta), ["README.md"])
        self.assertEqual(delta["preexisting_changed_files"], ["agent.py"])
        self.assertEqual(delta["preexisting_untracked_files"], ["notes.txt"])

    def test_preexisting_dirty_file_new_hunk_is_attributable(self) -> None:
        Path(self.repo_path, "README.md").write_text("base\npre-run dirty\n", encoding="utf-8")
        before = capture_invocation_git_state(self.repo_path)
        Path(self.repo_path, "README.md").write_text("base\npre-run dirty\ncodex line\n", encoding="utf-8")
        after = capture_invocation_git_state(self.repo_path)
        delta = compute_invocation_delta(before, after)

        self.assertEqual(attributable_paths(delta), ["README.md"])
        detail = delta["path_delta_details"][0]
        self.assertTrue(detail["preexisting_tracked_change"])
        self.assertEqual(detail["added_lines"], ["codex line"])


class GovernancePolicyTests(unittest.TestCase):
    def test_audit_only_modified_files_is_record_only_not_approval(self) -> None:
        decision = cli.evaluate_supervision_decision(
            {
                "flags": ["audit_only_modified_files"],
                "messages": [],
                "attention_level": "needs_review",
            }
        )
        self.assertEqual(decision["decision"], "record_only")
        self.assertFalse(decision["approval_required"])
        self.assertFalse(decision["needs_review"])
        transition = cli.status_from_supervision_decision(
            decision,
            {"validation_error": None, "found": True, "timed_out": False, "exit_code": 0},
        )
        self.assertEqual(transition["next_status"], "completed")

    def test_read_only_sandbox_attributable_write_is_objective_failure(self) -> None:
        contract = parse_prompt_contract("READ-ONLY TASK. Do not modify files.", "read-only").to_dict()
        delta = {
            "attributable_changed_files": ["README.md"],
            "attributable_added_files": [],
            "attributable_deleted_files": [],
            "attributable_renamed_files": [],
            "preexisting_changed_files": [],
            "preexisting_untracked_files": [],
            "path_delta_details": [
                {
                    "path": "README.md",
                    "change_type": "modified",
                    "diff_unified_zero": "+new line\n",
                }
            ],
        }
        observation = cli._build_governance_observation(
            contract,
            delta,
            classify_changed_files(["README.md"]),
            "read-only",
            {"status_short": ""},
        )
        self.assertIn("read_only_sandbox_attributable_write", observation["objective_failures"])
        transition = cli._governance_transition_if_blocking(
            observation,
            {
                "next_status": "completed",
                "reason": "supervision_decision_continue",
                "decision": "continue",
                "approval_required": False,
                "needs_review": False,
                "should_auto_complete": True,
            },
        )
        self.assertEqual(transition["next_status"], "needs_review")

    def test_workspace_write_explicit_read_only_mismatch_is_observation(self) -> None:
        contract = parse_prompt_contract("READ-ONLY TASK. Do not modify files.", "workspace-write").to_dict()
        delta = {
            "attributable_changed_files": ["README.md"],
            "attributable_added_files": [],
            "attributable_deleted_files": [],
            "attributable_renamed_files": [],
            "preexisting_changed_files": ["agent/cli.py"],
            "preexisting_untracked_files": ["tests/local.txt"],
            "path_delta_details": [
                {
                    "path": "README.md",
                    "change_type": "modified",
                    "diff_unified_zero": "+new line\n",
                }
            ],
        }
        observation = cli._build_governance_observation(
            contract,
            delta,
            classify_changed_files(["README.md"]),
            "workspace-write",
            {"status_short": " M agent/cli.py\n?? tests/local.txt\n"},
        )
        self.assertEqual(observation["objective_failures"], [])
        self.assertEqual(observation["preexisting_changed_files"], ["agent/cli.py"])
        self.assertEqual(observation["preexisting_untracked_files"], ["tests/local.txt"])
        self.assertEqual(observation["contract_mismatches"][0]["type"], "explicit_read_only_changed_files")


class SupervisePlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_path = self._tmp.name
        self._next_id = 1

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def event(self, event_type: str, metadata: dict | None = None) -> dict:
        event = {
            "id": self._next_id,
            "event_type": event_type,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        self._next_id += 1
        return event

    def run_record(self, status: str = "completed") -> dict:
        return {"id": "run-1", "status": status}

    def codex_finished(self, exit_code: int | None = 0, timed_out: bool = False) -> dict:
        return self.event(
            "codex_exec_finished",
            {
                "found": True,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "validation_error": None,
                "sandbox": "read-only",
            },
        )

    def supervision(self, decision: str = "continue", approval_required: bool = False, needs_review: bool = False) -> dict:
        return self.event(
            "supervision_decision",
            {
                "decision": decision,
                "approval_required": approval_required,
                "needs_review": needs_review,
            },
        )

    def diagnostics(self) -> dict:
        return self.event(
            "prompt_repo_impact_diagnostics",
            {
                "flags": [],
                "messages": [],
                "attention_level": "ok",
            },
        )

    def successful_submission(self) -> dict:
        return self.event(
            "gpt_feedback_submission_verified",
            {
                "reason_code": "chatgpt_submission_verified",
                "submission_marker_text": "AGENT_SUBMISSION\nrun_id=run-1\nnonce=n\npayload_sha256=p\nEND_AGENT_SUBMISSION",
                "submission_marker_sha256": _sha(
                    "AGENT_SUBMISSION\nrun_id=run-1\nnonce=n\npayload_sha256=p\nEND_AGENT_SUBMISSION"
                ),
            },
        )

    def capture(self, submission_event: dict, text: str | None = None) -> dict:
        response = text or (
            "BEGIN_NEXT_CODEX_PROMPT\n"
            "Say exactly: next step\n"
            "END_NEXT_CODEX_PROMPT"
        )
        return self.event(
            "gpt_response_captured",
            {
                "matched_submission_event_id": submission_event["id"],
                "response_text": response,
                "response_sha256": _sha(response),
            },
        )

    def extraction(
        self,
        capture_event: dict,
        submission_event: dict,
        method: str = "sentinel_block",
        prompt: str = "Say exactly: next step",
        source_event_id: int | None = None,
    ) -> dict:
        return self.event(
            "next_codex_prompt_extracted",
            {
                "source_event_id": capture_event["id"] if source_event_id is None else source_event_id,
                "source_event_type": "gpt_response_captured",
                "source_response_sha256": json.loads(capture_event["metadata_json"])["response_sha256"],
                "matched_submission_event_id": submission_event["id"],
                "extraction_method": method,
                "prompt_text": prompt,
                "prompt_length": len(prompt),
                "prompt_sha256": _sha(prompt),
                "prompt_count_detected": 1,
                "selected_prompt_index": 0,
                "safety_status": "requires_human_review",
                "warnings": [],
            },
        )

    def base_completed_events(self) -> list[dict]:
        return [self.codex_finished(), self.diagnostics(), self.supervision()]

    def test_completed_codex_without_submission_asks_send(self) -> None:
        plan = detect_next_supervise_action(
            self.run_record(),
            self.base_completed_events(),
            self.repo_path,
        )
        self.assertEqual(plan.action, SuperviseAction.ASK_SEND_TO_GPT)

    def test_successful_submission_without_capture_captures(self) -> None:
        base = self.base_completed_events()
        events = [*base, self.successful_submission()]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.CAPTURE_GPT_RESPONSE)

    def test_valid_capture_without_extracted_prompt_extracts(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        events = [*base, submission, self.capture(submission)]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.EXTRACT_NEXT_PROMPT)

    def test_valid_fresh_sentinel_extraction_asks_run(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        extraction = self.extraction(capture, submission)
        events = [*base, submission, capture, extraction]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.ASK_RUN_PROMPT)
        self.assertEqual(plan.prompt_sha, _sha("Say exactly: next step"))

    def test_non_sentinel_extraction_stops(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        extraction = self.extraction(capture, submission, method="labeled_fenced_code_block")
        events = [*base, submission, capture, extraction]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.STOP)
        self.assertEqual(plan.reason, "non_sentinel_prompt")

    def test_failed_timed_out_and_nonzero_codex_stop(self) -> None:
        cases = [
            ([self.codex_finished(exit_code=1), self.supervision()], "codex_nonzero_exit"),
            ([self.codex_finished(exit_code=0, timed_out=True), self.supervision()], "codex_timed_out"),
            ([self.codex_finished(exit_code=None), self.supervision()], "codex_exit_missing"),
        ]
        for events, reason in cases:
            with self.subTest(reason=reason):
                plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
                self.assertEqual(plan.action, SuperviseAction.STOP)
                self.assertEqual(plan.reason, reason)

    def test_needs_review_and_approval_required_stop(self) -> None:
        cases = [
            (self.run_record("needs_review"), self.base_completed_events(), "needs_review"),
            (self.run_record("waiting_for_approval"), self.base_completed_events(), "waiting_for_approval"),
            (
                self.run_record(),
                [
                    self.codex_finished(),
                    self.diagnostics(),
                    self.supervision("approval_required", approval_required=True, needs_review=True),
                ],
                "approval_required",
            ),
        ]
        for run, events, reason in cases:
            with self.subTest(reason=reason):
                plan = detect_next_supervise_action(run, events, self.repo_path)
                self.assertEqual(plan.action, SuperviseAction.STOP)
                self.assertEqual(plan.reason, reason)

    def test_completed_extracted_prompt_run_then_new_codex_output_asks_send(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        extraction = self.extraction(capture, submission)
        extraction_metadata = json.loads(extraction["metadata_json"])
        events = [
            *base,
            submission,
            capture,
            extraction,
            self.event(
                "extracted_codex_prompt_run_started",
                {
                    "extraction_event_id": extraction["id"],
                    "prompt_sha256": extraction_metadata["prompt_sha256"],
                },
            ),
            self.codex_finished(),
            self.diagnostics(),
            self.supervision(),
            self.event(
                "extracted_codex_prompt_run_finished",
                {
                    "extraction_event_id": extraction["id"],
                    "prompt_sha256": extraction_metadata["prompt_sha256"],
                    "exit_code": 0,
                    "timed_out": False,
                },
            ),
        ]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.ASK_SEND_TO_GPT)

    def test_stale_extraction_validation_stops(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        extraction = self.extraction(capture, submission, source_event_id=999)
        events = [*base, submission, capture, extraction]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.STOP)
        self.assertIn(plan.reason, {"invalid_extracted_prompt", "ambiguous_extracted_prompt"})

    def test_capture_sha_mismatch_stops_before_extraction(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        metadata = json.loads(capture["metadata_json"])
        metadata["response_sha256"] = "not-the-real-sha"
        capture["metadata_json"] = json.dumps(metadata, sort_keys=True)
        events = [*base, submission, capture]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.STOP)
        self.assertEqual(plan.reason, "captured_response_integrity_failed")

    def test_new_capture_after_old_extraction_extracts_again(self) -> None:
        base = self.base_completed_events()
        first_submission = self.successful_submission()
        first_capture = self.capture(first_submission)
        first_extraction = self.extraction(first_capture, first_submission)
        first_extraction_metadata = json.loads(first_extraction["metadata_json"])
        second_codex = self.codex_finished()
        second_diagnostics = self.diagnostics()
        second_supervision = self.supervision()
        second_submission = self.successful_submission()
        second_capture = self.capture(second_submission)
        events = [
            *base,
            first_submission,
            first_capture,
            first_extraction,
            self.event(
                "extracted_codex_prompt_run_started",
                {
                    "extraction_event_id": first_extraction["id"],
                    "prompt_sha256": first_extraction_metadata["prompt_sha256"],
                },
            ),
            second_codex,
            second_diagnostics,
            second_supervision,
            self.event(
                "extracted_codex_prompt_run_finished",
                {
                    "extraction_event_id": first_extraction["id"],
                    "prompt_sha256": first_extraction_metadata["prompt_sha256"],
                    "exit_code": 0,
                    "timed_out": False,
                },
            ),
            second_submission,
            second_capture,
        ]
        plan = detect_next_supervise_action(self.run_record(), events, self.repo_path)
        self.assertEqual(plan.action, SuperviseAction.EXTRACT_NEXT_PROMPT)

    def test_danger_full_access_is_blocked(self) -> None:
        plan = detect_next_supervise_action(
            self.run_record(),
            self.base_completed_events(),
            self.repo_path,
            sandbox="danger-full-access",
        )
        self.assertEqual(plan.action, SuperviseAction.STOP)
        self.assertEqual(plan.reason, "danger_full_access_blocked")

    def test_workspace_write_is_allowed_when_explicit(self) -> None:
        plan = detect_next_supervise_action(
            self.run_record(),
            self.base_completed_events(),
            self.repo_path,
            sandbox="workspace-write",
        )
        self.assertEqual(plan.action, SuperviseAction.ASK_SEND_TO_GPT)
        self.assertEqual(plan.sandbox, "workspace-write")

    def test_confirmation_eof_and_keyboard_interrupt_are_no(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(cli._confirm_yes_no("Question?"))
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertFalse(cli._confirm_yes_no("Question?"))

    def test_execution_refuses_if_newer_extraction_appears_after_approval(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        first = self.extraction(capture, submission, prompt="Prompt A")
        second = self.extraction(capture, submission, prompt="Prompt B")
        first_metadata = json.loads(first["metadata_json"])
        events = [*base, submission, capture, first, second]
        fake_ledger = _FakeLedger(self.run_record(), events)

        with mock.patch.object(cli, "ledger", fake_ledger), mock.patch.object(cli, "_run_codex_exec_flow") as run_flow:
            result = cli._run_extracted_codex_prompt_flow(
                "run-1",
                self.run_record(),
                self.repo_path,
                "read-only",
                300,
                expected_extraction_event_id=first["id"],
                expected_prompt_sha256=first_metadata["prompt_sha256"],
                expected_prompt_text=first_metadata["prompt_text"],
                expected_extraction_method="sentinel_block",
            )

        self.assertEqual(result, 1)
        run_flow.assert_not_called()

    def test_execution_refuses_if_expected_sha_matches_but_event_id_differs(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        extraction = self.extraction(capture, submission, prompt="Same prompt")
        metadata = json.loads(extraction["metadata_json"])
        events = [*base, submission, capture, extraction]
        fake_ledger = _FakeLedger(self.run_record(), events)

        with mock.patch.object(cli, "ledger", fake_ledger), mock.patch.object(cli, "_run_codex_exec_flow") as run_flow:
            result = cli._run_extracted_codex_prompt_flow(
                "run-1",
                self.run_record(),
                self.repo_path,
                "read-only",
                300,
                expected_extraction_event_id=extraction["id"] + 100,
                expected_prompt_sha256=metadata["prompt_sha256"],
                expected_prompt_text=metadata["prompt_text"],
                expected_extraction_method="sentinel_block",
            )

        self.assertEqual(result, 1)
        run_flow.assert_not_called()

    def test_execution_refuses_non_sentinel_expected_method(self) -> None:
        base = self.base_completed_events()
        submission = self.successful_submission()
        capture = self.capture(submission)
        extraction = self.extraction(
            capture,
            submission,
            method="labeled_fenced_code_block",
            prompt="Prompt A",
        )
        metadata = json.loads(extraction["metadata_json"])
        events = [*base, submission, capture, extraction]
        fake_ledger = _FakeLedger(self.run_record(), events)

        with mock.patch.object(cli, "ledger", fake_ledger), mock.patch.object(cli, "_run_codex_exec_flow") as run_flow:
            result = cli._run_extracted_codex_prompt_flow(
                "run-1",
                self.run_record(),
                self.repo_path,
                "read-only",
                300,
                expected_extraction_event_id=extraction["id"],
                expected_prompt_sha256=metadata["prompt_sha256"],
                expected_prompt_text=metadata["prompt_text"],
                expected_extraction_method="sentinel_block",
            )

        self.assertEqual(result, 1)
        run_flow.assert_not_called()


class CodexRunAutoSuperviseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_path = self._tmp.name
        self.run = {"id": "run-1", "status": "completed"}
        self.events: list[dict] = []
        self._destination_adapter_patcher = mock.patch.object(
            supervision_services_module,
            "ChatGPTAXDestinationSnapshotAdapter",
            _FakeDestinationAdapter,
        )
        self._destination_adapter_patcher.start()

    def tearDown(self) -> None:
        self._destination_adapter_patcher.stop()
        self._tmp.cleanup()

    def args(
        self,
        no_supervise: bool = False,
        sandbox: str = "read-only",
        interactive: bool = False,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            command="codex-run",
            run_id="run-1",
            prompt="Say exactly: top-level",
            repo=self.repo_path,
            cwd=None,
            sandbox=sandbox,
            confirm_full_access=False,
            timeout=None,
            no_supervise=no_supervise,
            interactive=interactive,
        )

    def flow(
        self,
        *,
        validation_error: str | None = None,
        found: bool = True,
        timed_out: bool = False,
        exit_code: int | None = 0,
        next_status: str = "completed",
        decision: str = "continue",
        needs_review: bool = False,
        approval_required: bool = False,
    ) -> dict:
        return {
            "result": {
                "validation_error": validation_error,
                "found": found,
                "timed_out": timed_out,
                "exit_code": exit_code,
            },
            "transition": {"next_status": next_status},
            "supervision_decision": {
                "decision": decision,
                "needs_review": needs_review,
                "approval_required": approval_required,
            },
        }

    def send_plan(self, codex_event_id: int = 1) -> object:
        return SupervisePlan(
            action=SuperviseAction.ASK_SEND_TO_GPT,
            reason="codex_result_ready",
            event_ids={"codex_exec_finished": codex_event_id},
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
            codex_exit_code=0,
            codex_timed_out=False,
            codex_sandbox="read-only",
            changed_files_count=0,
            supervision_decision="continue",
        )

    def capture_plan(self, codex_event_id: int = 1, submission_event_id: int = 2) -> object:
        return SupervisePlan(
            action=SuperviseAction.CAPTURE_GPT_RESPONSE,
            reason="feedback_submitted_capture_needed",
            event_ids={"codex_exec_finished": codex_event_id, "gpt_feedback_submission_verified": submission_event_id},
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
        )

    def run_plan(
        self,
        codex_event_id: int = 1,
        extraction_event_id: int = 4,
        prompt: str = "Say exactly: extracted",
    ) -> object:
        return SupervisePlan(
            action=SuperviseAction.ASK_RUN_PROMPT,
            reason="fresh_sentinel_prompt_ready",
            event_ids={
                "codex_exec_finished": codex_event_id,
                "next_codex_prompt_extracted": extraction_event_id,
            },
            prompt_preview=prompt,
            prompt_text=prompt,
            prompt_sha=_sha(prompt),
            extraction_method="sentinel_block",
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
            prompt_auto_run_safe=True,
            prompt_auto_run_reason="caller_selected_read_only_sandbox",
        )

    def extract_plan(
        self,
        codex_event_id: int = 1,
        submission_event_id: int = 2,
        capture_event_id: int = 3,
    ) -> object:
        return SupervisePlan(
            action=SuperviseAction.EXTRACT_NEXT_PROMPT,
            reason="gpt_response_captured_extract_needed",
            event_ids={
                "codex_exec_finished": codex_event_id,
                "gpt_feedback_submission_verified": submission_event_id,
                "gpt_response_captured": capture_event_id,
            },
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
        )

    def stop_plan(self, reason: str = "needs_review") -> object:
        return SupervisePlan(
            action=SuperviseAction.STOP,
            reason=reason,
            stop_message="Stopped by planner.",
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
        )

    def _append_codex_result(
        self,
        fake_ledger: "_FakeLedger",
        *,
        decision: str = "continue",
        needs_review: bool = False,
        approval_required: bool = False,
    ) -> dict:
        codex = fake_ledger.append_event(
            "codex_exec_finished",
            {
                "found": True,
                "exit_code": 0,
                "timed_out": False,
                "validation_error": None,
                "sandbox": "read-only",
            },
        )
        fake_ledger.append_event(
            "prompt_repo_impact_diagnostics",
            {
                "flags": [],
                "messages": [],
                "attention_level": "ok",
            },
        )
        fake_ledger.append_event(
            "changed_file_classification",
            {
                "total_files": 0,
                "files": [],
                "changed_files": [],
            },
        )
        fake_ledger.append_event(
            "supervision_decision",
            {
                "decision": decision,
                "needs_review": needs_review,
                "approval_required": approval_required,
            },
        )
        return codex

    def _sentinel_response(self, prompt: str) -> str:
        return f"BEGIN_NEXT_CODEX_PROMPT\n{prompt}\nEND_NEXT_CODEX_PROMPT"

    def _latest_event(self, fake_ledger: "_FakeLedger", event_type: str) -> dict:
        for event in reversed(fake_ledger.list_events("run-1")):
            if event.get("event_type") == event_type:
                return event
        raise AssertionError(f"Missing event: {event_type}")

    def _mock_successful_iteration_flows(
        self,
        fake_ledger: "_FakeLedger",
        prompts: list[str],
        *,
        second_codex_needs_review: bool = False,
    ) -> tuple[mock.Mock, mock.Mock, mock.Mock, mock.Mock, list[int], list[int]]:
        submission_codex_ids: list[int] = []
        extraction_ids: list[int] = []

        def submit_side_effect(*args, **kwargs) -> bool:
            latest_codex_id = cli._latest_event_id(fake_ledger.list_events("run-1"), "codex_exec_finished")
            submission_codex_ids.append(latest_codex_id)
            fake_ledger.append_event(
                "gpt_feedback_submission_verified",
                {"reason_code": "chatgpt_submission_verified", "codex_event_id": latest_codex_id},
            )
            return True

        def capture_side_effect(*args, **kwargs) -> bool:
            submission = self._latest_event(fake_ledger, "gpt_feedback_submission_verified")
            prompt = prompts[len(extraction_ids)]
            response = self._sentinel_response(prompt)
            fake_ledger.append_event(
                "gpt_response_captured",
                {
                    "matched_submission_event_id": submission["id"],
                    "response_text": response,
                    "response_sha256": _sha(response),
                },
            )
            return True

        def extract_side_effect(*args, **kwargs) -> bool:
            submission = self._latest_event(fake_ledger, "gpt_feedback_submission_verified")
            capture = self._latest_event(fake_ledger, "gpt_response_captured")
            capture_metadata = json.loads(capture["metadata_json"])
            response = capture_metadata["response_text"]
            prompt = prompts[len(extraction_ids)]
            extraction = fake_ledger.append_event(
                "next_codex_prompt_extracted",
                {
                    "source_event_id": capture["id"],
                    "source_event_type": "gpt_response_captured",
                    "source_response_sha256": capture_metadata["response_sha256"],
                    "matched_submission_event_id": submission["id"],
                    "extraction_method": "sentinel_block",
                    "prompt_text": prompt,
                    "prompt_length": len(prompt),
                    "prompt_sha256": _sha(prompt),
                    "prompt_count_detected": 1,
                    "selected_prompt_index": 0,
                    "safety_status": "requires_human_review",
                    "warnings": [],
                },
            )
            self.assertIn(prompt, response)
            extraction_ids.append(extraction["id"])
            return True

        def run_prompt_side_effect(*args, **kwargs) -> int:
            run_number = len([event for event in fake_ledger.list_events("run-1") if event["event_type"] == "codex_exec_finished"])
            self._append_codex_result(
                fake_ledger,
                decision="needs_review" if second_codex_needs_review and run_number == 2 else "continue",
                needs_review=second_codex_needs_review and run_number == 2,
            )
            return 0

        return (
            mock.Mock(side_effect=submit_side_effect),
            mock.Mock(side_effect=capture_side_effect),
            mock.Mock(side_effect=extract_side_effect),
            mock.Mock(side_effect=run_prompt_side_effect),
            submission_codex_ids,
            extraction_ids,
        )

    def test_top_level_codex_run_auto_enters_supervise_flow(self) -> None:
        args = self.args()
        fake_ledger = _FakeLedger(self.run, self.events)

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=self.send_plan()) as planner,
            mock.patch.object(cli, "_run_supervise_command", return_value=0) as supervise,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._codex_run_auto_supervise_exit_code(args, self.repo_path, self.flow())

        self.assertEqual(result, 0)
        planner.assert_called_once()
        supervise.assert_called_once()
        supervise_args = supervise.call_args.args[0]
        self.assertEqual(supervise_args.run_id, "run-1")
        self.assertEqual(supervise_args.repo, self.repo_path)
        self.assertEqual(supervise_args.sandbox, "read-only")

    def test_codex_run_main_uses_auto_supervise_by_default(self) -> None:
        fake_ledger = _FakeLedger(self.run, self.events)
        argv = [
            "agent-loop",
            "codex-run",
            "run-1",
            "--repo",
            self.repo_path,
            "--sandbox",
            "read-only",
            "--prompt",
            "Say exactly: top-level",
        ]

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_codex_exec_flow", return_value=self.flow()),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=self.send_plan()),
            mock.patch.object(cli, "_run_supervise_command", return_value=0) as supervise,
            mock.patch.object(cli.sys, "argv", argv),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main()

        self.assertEqual(raised.exception.code, 0)
        supervise.assert_called_once()

    def test_no_supervise_prevents_handoff(self) -> None:
        args = self.args(no_supervise=True)

        with (
            mock.patch.object(cli, "detect_next_supervise_action") as planner,
            mock.patch.object(cli, "_run_supervise_command") as supervise,
        ):
            result = cli._codex_run_auto_supervise_exit_code(args, self.repo_path, self.flow())

        self.assertIsNone(result)
        planner.assert_not_called()
        supervise.assert_not_called()

    def test_failure_results_do_not_invoke_supervision(self) -> None:
        cases = [
            self.flow(validation_error="Invalid sandbox."),
            self.flow(found=False, exit_code=None),
            self.flow(timed_out=True, exit_code=None),
            self.flow(exit_code=1),
        ]

        for flow in cases:
            with self.subTest(flow=flow):
                with (
                    mock.patch.object(cli, "detect_next_supervise_action") as planner,
                    mock.patch.object(cli, "_run_supervise_command") as supervise,
                ):
                    result = cli._codex_run_auto_supervise_exit_code(
                        self.args(),
                        self.repo_path,
                        flow,
                    )
                self.assertIsNone(result)
                planner.assert_not_called()
                supervise.assert_not_called()

    def test_review_outcomes_do_not_invoke_supervision(self) -> None:
        cases = [
            self.flow(next_status="needs_review", decision="needs_review", needs_review=True),
            self.flow(next_status="waiting_for_approval", decision="approval_required", needs_review=True, approval_required=True),
            self.flow(decision="needs_review", needs_review=True),
            self.flow(decision="approval_required", approval_required=True),
        ]

        for flow in cases:
            with self.subTest(flow=flow):
                with (
                    mock.patch.object(cli, "detect_next_supervise_action") as planner,
                    mock.patch.object(cli, "_run_supervise_command") as supervise,
                ):
                    result = cli._codex_run_auto_supervise_exit_code(
                        self.args(),
                        self.repo_path,
                        flow,
                    )
                self.assertIsNone(result)
                planner.assert_not_called()
                supervise.assert_not_called()

    def test_non_send_planner_result_does_not_invoke_supervision(self) -> None:
        fake_ledger = _FakeLedger(self.run, self.events)
        stop_plan = SupervisePlan(
            action=SuperviseAction.STOP,
            reason="extracted_prompt_already_run",
            stop_message="Already run.",
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=stop_plan) as planner,
            mock.patch.object(cli, "_run_supervise_command") as supervise,
        ):
            result = cli._codex_run_auto_supervise_exit_code(
                self.args(),
                self.repo_path,
                self.flow(),
            )

        self.assertIsNone(result)
        planner.assert_called_once()
        supervise.assert_not_called()

    def test_declining_send_performs_no_followup_actions(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(interactive=True), self.repo_path)
        fake_ledger = _FakeLedger(self.run, self.events)

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=self.send_plan()),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow") as submit,
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow") as capture,
            mock.patch.object(cli, "_extract_next_codex_prompt_flow") as extract,
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow") as run_prompt,
            mock.patch("builtins.input", return_value="n"),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 0)
        submit.assert_not_called()
        capture.assert_not_called()
        extract.assert_not_called()
        run_prompt.assert_not_called()

    def test_declining_run_does_not_execute_prompt(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(interactive=True), self.repo_path)
        fake_ledger = _FakeLedger(self.run, self.events)

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=self.run_plan()),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow") as run_prompt,
            mock.patch("builtins.input", return_value="n"),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 0)
        run_prompt.assert_not_called()

    def test_two_successful_iterations_continue_through_planner(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, submission_codex_ids, extraction_ids = (
            self._mock_successful_iteration_flows(
                fake_ledger,
                ["Say exactly: first extracted", "Say exactly: second extracted"],
                second_codex_needs_review=True,
            )
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch.object(cli, "_confirm_yes_no") as confirm,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        confirm.assert_not_called()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(run_prompt.call_count, 2)
        self.assertEqual(submit.call_args_list[0].kwargs["approval_mode"], "auto")
        self.assertEqual(run_prompt.call_args_list[0].kwargs["approval_mode"], "auto")
        self.assertEqual(submission_codex_ids.count(submission_codex_ids[0]), 1)
        self.assertEqual(len(extraction_ids), 2)
        run_extraction_ids = [
            call.kwargs["expected_extraction_event_id"]
            for call in run_prompt.call_args_list
        ]
        self.assertEqual(run_extraction_ids, extraction_ids)
        self.assertEqual(run_extraction_ids.count(extraction_ids[0]), 1)

    def test_generic_implementation_wording_does_not_block_read_only_auto_run(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        plan = self.run_plan(prompt="Implement a fix in the workspace files")

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=plan),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", return_value=37) as run_prompt,
            mock.patch.object(cli, "_confirm_yes_no") as confirm,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 37)
        confirm.assert_not_called()
        run_prompt.assert_called_once()

    def test_changed_file_result_does_not_auto_submit(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        changed_plan = SupervisePlan(
            action=SuperviseAction.ASK_SEND_TO_GPT,
            reason="codex_result_ready",
            event_ids={"codex_exec_finished": 1},
            repo_path=self.repo_path,
            sandbox="read-only",
            status="completed",
            codex_exit_code=0,
            codex_timed_out=False,
            codex_sandbox="read-only",
            changed_files_count=1,
            supervision_decision="continue",
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", return_value=changed_plan),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow") as submit,
            mock.patch.object(cli, "_confirm_yes_no") as confirm,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        confirm.assert_not_called()
        submit.assert_not_called()
        auto_stop = self._latest_event(fake_ledger, "supervise_auto_stopped")
        metadata = json.loads(auto_stop["metadata_json"])
        self.assertEqual(metadata["automatic_stop_reason"], "codex_result_changed_files")

    def test_verified_workspace_write_result_auto_submits(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(sandbox="workspace-write"), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        fake_ledger.append_event("codex_exec_finished", {"exit_code": 0, "timed_out": False})
        fake_ledger.append_event(
            "workspace_write_post_run_policy",
            {
                "codex_exec_finished_event_id": 1,
                "post_run_policy": {
                    "allowed": True,
                    "reason_code": "post_run_diff_within_expected_scope",
                },
            },
        )
        send_plan = SupervisePlan(
            action=SuperviseAction.ASK_SEND_TO_GPT,
            reason="codex_result_ready",
            event_ids={"codex_exec_finished": 1},
            repo_path=self.repo_path,
            sandbox="workspace-write",
            status="completed",
            codex_exit_code=0,
            codex_timed_out=False,
            codex_sandbox="workspace-write",
            changed_files_count=1,
            supervision_decision="continue",
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "detect_next_supervise_action", side_effect=[send_plan, self.stop_plan()]),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", return_value=True) as submit,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["approval_mode"], "auto")

    def test_workspace_write_objective_governance_failure_stops_extracted_run(self) -> None:
        prompt = "Fix agent/foo.py and preserve behavior."
        response = self._sentinel_response(prompt)
        events = [
            {
                "id": 1,
                "event_type": "gpt_feedback_submission_verified",
                "metadata_json": json.dumps({"reason_code": "chatgpt_submission_verified"}, sort_keys=True),
            },
            {
                "id": 2,
                "event_type": "gpt_response_captured",
                "metadata_json": json.dumps(
                    {
                        "matched_submission_event_id": 1,
                        "response_text": response,
                        "response_sha256": _sha(response),
                    },
                    sort_keys=True,
                ),
            },
            {
                "id": 3,
                "event_type": "next_codex_prompt_extracted",
                "metadata_json": json.dumps(
                    {
                        "source_event_id": 2,
                        "source_event_type": "gpt_response_captured",
                        "source_response_sha256": _sha(response),
                        "matched_submission_event_id": 1,
                        "extraction_method": "sentinel_block",
                        "prompt_text": prompt,
                        "prompt_length": len(prompt),
                        "prompt_sha256": _sha(prompt),
                        "prompt_count_detected": 1,
                        "selected_prompt_index": 0,
                        "safety_status": "requires_human_review",
                        "warnings": [],
                    },
                    sort_keys=True,
                ),
            },
        ]
        fake_ledger = _FakeLedger(self.run, events)
        flow = self.flow()
        flow["governance_observation"] = {"objective_failures": ["high_confidence_secret_literal"]}

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_codex_exec_flow", return_value=flow),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_extracted_codex_prompt_flow(
                "run-1",
                self.run,
                self.repo_path,
                "workspace-write",
                300,
                expected_extraction_event_id=3,
                expected_prompt_sha256=_sha(prompt),
                expected_prompt_text=prompt,
                expected_extraction_method="sentinel_block",
                approval_mode="auto",
                pre_run_policy={"allowed": True, "reason_code": "workspace_write_scoped_auto"},
                expected_scope={
                    "explicit_files": ["agent/foo.py"],
                    "allowed_dirs": [],
                    "allowed_categories": ["python_source"],
                    "denied_categories": [],
                    "max_changed_files": 4,
                    "allow_deletions": False,
                    "allow_renames": False,
                    "confidence": "explicit",
                },
            )

        self.assertEqual(result, 1)

    def test_declining_send_during_iteration_two_does_not_repeat_actions(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(interactive=True), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Prompt one", "Prompt two"])
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch("builtins.input", side_effect=["y", "y", "n"]),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(run_prompt.call_count, 1)

    def test_declining_run_during_iteration_two_does_not_execute_second_prompt(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(interactive=True), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Prompt one", "Prompt two"])
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch("builtins.input", side_effect=["y", "y", "y", "n"]),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(run_prompt.call_count, 1)
        self.assertEqual(
            run_prompt.call_args.kwargs["expected_extraction_event_id"],
            extraction_ids[0],
        )

    def test_capture_failure_during_iteration_two_returns_existing_failure(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Say exactly: one", "Say exactly: two"])
        )
        original_capture_side_effect = capture.side_effect

        def capture_failure_side_effect(*args, **kwargs) -> bool:
            if capture.call_count == 1:
                return original_capture_side_effect(*args, **kwargs)
            return False

        capture.side_effect = capture_failure_side_effect

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(run_prompt.call_count, 1)

    def test_nonzero_codex_exit_during_iteration_two_returns_exact_code(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Say exactly: one", "Say exactly: two"])
        )

        def run_side_effect(*args, **kwargs) -> int:
            if run_prompt.call_count == 1:
                self._append_codex_result(fake_ledger)
                return 0
            return 37

        run_prompt.side_effect = run_side_effect

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 37)
        self.assertEqual(run_prompt.call_count, 2)
        self.assertEqual(submit.call_count, 2)

    def test_timeout_during_iteration_two_returns_124(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Say exactly: one", "Say exactly: two"])
        )

        def run_side_effect(*args, **kwargs) -> int:
            if run_prompt.call_count == 1:
                self._append_codex_result(fake_ledger)
                return 0
            return 124

        run_prompt.side_effect = run_side_effect

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 124)
        self.assertEqual(run_prompt.call_count, 2)
        self.assertEqual(submit.call_count, 2)

    def test_eof_and_ctrl_c_are_safe_at_later_approvals(self) -> None:
        cases = [
            ("send", ["y", "y", EOFError], 1, 1, 1, 1),
            ("run", ["y", "y", "y", KeyboardInterrupt], 2, 2, 2, 1),
        ]
        for _gate, inputs, submit_count, capture_count, extract_count, run_count in cases:
            with self.subTest(gate=_gate):
                args = cli._supervise_args_for_codex_run(self.args(interactive=True), self.repo_path)
                fake_ledger = _FakeLedger(self.run, [])
                self._append_codex_result(fake_ledger)
                submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
                    self._mock_successful_iteration_flows(fake_ledger, ["Prompt one", "Prompt two"])
                )

                with (
                    mock.patch.object(cli, "ledger", fake_ledger),
                    mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
                    mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
                    mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
                    mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
                    mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
                    mock.patch("builtins.input", side_effect=inputs),
                    mock.patch("sys.stdout", new=io.StringIO()),
                ):
                    result = cli._run_supervise_command(args)

                self.assertEqual(result, 0)
                self.assertEqual(submit.call_count, submit_count)
                self.assertEqual(capture.call_count, capture_count)
                self.assertEqual(extract.call_count, extract_count)
                self.assertEqual(run_prompt.call_count, run_count)

    def test_success_without_newer_codex_completion_fails_closed(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Say exactly: one"])
        )
        run_prompt.side_effect = None
        run_prompt.return_value = 0

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch("sys.stdout", new=io.StringIO()),
            mock.patch("sys.stderr", new=io.StringIO()) as stderr,
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        self.assertIn("no newer codex_exec_finished event", stderr.getvalue())
        self.assertEqual(run_prompt.call_count, 1)

    def test_continuation_path_does_not_call_codex_run_auto_handoff(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(interactive=True), self.repo_path)
        fake_ledger = _FakeLedger(self.run, [])
        self._append_codex_result(fake_ledger)
        submit, capture, extract, run_prompt, _submission_codex_ids, _extraction_ids = (
            self._mock_successful_iteration_flows(fake_ledger, ["Prompt one", "Prompt two"])
        )

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", submit),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", capture),
            mock.patch.object(cli, "_extract_next_codex_prompt_flow", extract),
            mock.patch.object(cli, "capture_git_snapshot", return_value={"status_short": ""}),
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow", run_prompt),
            mock.patch.object(cli, "_codex_run_auto_supervise_exit_code") as auto_handoff,
            mock.patch("builtins.input", side_effect=["y", "y", "n"]),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 0)
        auto_handoff.assert_not_called()

    def test_stop_plan_still_returns_without_actions(self) -> None:
        args = cli._supervise_args_for_codex_run(self.args(), self.repo_path)
        stop_run = {"id": "run-1", "status": "needs_review"}
        fake_ledger = _FakeLedger(stop_run, [])
        self._append_codex_result(fake_ledger, decision="needs_review", needs_review=True)

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow") as submit,
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow") as capture,
            mock.patch.object(cli, "_extract_next_codex_prompt_flow") as extract,
            mock.patch.object(cli, "_run_extracted_codex_prompt_flow") as run_prompt,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_supervise_command(args)

        self.assertEqual(result, 1)
        submit.assert_not_called()
        capture.assert_not_called()
        extract.assert_not_called()
        run_prompt.assert_not_called()

    def test_auto_handoff_uses_sentinel_required_capture(self) -> None:
        args = self.args()
        fake_ledger = _FakeLedger(self.run, self.events)

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(
                cli,
                "detect_next_supervise_action",
                side_effect=[self.send_plan(), self.capture_plan()],
            ),
            mock.patch.object(cli, "_submit_feedback_to_chatgpt_flow", return_value=True),
            mock.patch.object(cli, "_capture_gpt_response_from_chatgpt_ax_flow", return_value=False) as capture,
            mock.patch("builtins.input", return_value="y"),
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._codex_run_auto_supervise_exit_code(args, self.repo_path, self.flow())

        self.assertEqual(result, 1)
        self.assertEqual(capture.call_args.kwargs["require_sentinel_response"], True)

    def test_extracted_prompt_execution_does_not_recurse_into_auto_handoff(self) -> None:
        submission = {
            "id": 1,
            "event_type": "gpt_feedback_submission_verified",
            "metadata_json": json.dumps({"reason_code": "chatgpt_submission_verified"}, sort_keys=True),
        }
        response = (
            "BEGIN_NEXT_CODEX_PROMPT\n"
            "Say exactly: extracted\n"
            "END_NEXT_CODEX_PROMPT"
        )
        capture = {
            "id": 2,
            "event_type": "gpt_response_captured",
            "metadata_json": json.dumps(
                {
                    "matched_submission_event_id": 1,
                    "response_text": response,
                    "response_sha256": _sha(response),
                },
                sort_keys=True,
            ),
        }
        prompt = "Say exactly: extracted"
        extraction = {
            "id": 3,
            "event_type": "next_codex_prompt_extracted",
            "metadata_json": json.dumps(
                {
                    "source_event_id": 2,
                    "source_event_type": "gpt_response_captured",
                    "source_response_sha256": _sha(response),
                    "matched_submission_event_id": 1,
                    "extraction_method": "sentinel_block",
                    "prompt_text": prompt,
                    "prompt_length": len(prompt),
                    "prompt_sha256": _sha(prompt),
                    "prompt_count_detected": 1,
                    "selected_prompt_index": 0,
                    "safety_status": "requires_human_review",
                    "warnings": [],
                },
                sort_keys=True,
            ),
        }
        fake_ledger = _FakeLedger(self.run, [submission, capture, extraction])
        flow = self.flow()

        with (
            mock.patch.object(cli, "ledger", fake_ledger),
            mock.patch.object(cli, "_run_codex_exec_flow", return_value=flow),
            mock.patch.object(cli, "_codex_run_auto_supervise_exit_code") as auto_handoff,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = cli._run_extracted_codex_prompt_flow(
                "run-1",
                self.run,
                self.repo_path,
                "read-only",
                300,
                expected_extraction_event_id=3,
                expected_prompt_sha256=_sha(prompt),
                expected_prompt_text=prompt,
                expected_extraction_method="sentinel_block",
            )

        self.assertEqual(result, 0)
        auto_handoff.assert_not_called()


class FeedbackPayloadTests(unittest.TestCase):
    def event(self, event_type: str, metadata: dict, event_id: int) -> dict:
        return {
            "id": event_id,
            "event_type": event_type,
            "metadata_json": json.dumps(metadata, sort_keys=True),
        }

    def test_feedback_payload_uses_clean_final_message_not_raw_stdout_or_stderr(self) -> None:
        stdout = "line 1\nraw stdout must not be submitted\nstdout-without-final-newline"
        stderr = "stderr line 1\nraw stderr must not be submitted\n"
        final_message = "Clean final assistant completion report."
        events = [
            self.event(
                "codex_exec_finished",
                {
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": 0,
                    "timed_out": False,
                    "final_message": final_message,
                    "final_message_path": "/tmp/run-1/final-message.md",
                    "final_message_status": "valid",
                    "final_message_error": None,
                    "final_message_length": len(final_message),
                },
                1,
            ),
            self.event(
                "changed_file_classification",
                {"files": [{"path": "agent/cli.py", "category": "source"}]},
                2,
            ),
            self.event("prompt_repo_impact_diagnostics", {"flags": []}, 3),
            self.event(
                "supervision_decision",
                {
                    "decision": "continue",
                    "needs_review": False,
                    "approval_required": False,
                    "policy_version": "risk_policy_v1",
                },
                4,
            ),
        ]

        feedback = cli.build_gpt_feedback_message({"id": "run-1", "status": "completed"}, events)
        message = feedback["message"]

        self.assertTrue(feedback["submittable"])
        self.assertIn("Codex completion report", message)
        self.assertIn("Final assistant message:\n" + final_message, message)
        self.assertNotIn(stdout, message)
        self.assertNotIn(stderr, message)
        self.assertNotIn("raw stdout must not be submitted", message)
        self.assertNotIn("raw stderr must not be submitted", message)
        self.assertIn("- changed_files: [\"agent/cli.py\"]", message)
        self.assertIn("- run_id: run-1", message)
        self.assertIn("- exit_code: 0", message)
        self.assertIn("- timed_out: false", message)
        self.assertNotIn("supervision_decision", message)
        self.assertNotIn("diagnostics_flags", message)
        self.assertNotIn("continuation_allowed", message)
        self.assertNotIn("policy_version", message)
        self.assertEqual(feedback["final_message_length"], len(final_message))
        self.assertEqual(feedback["feedback_payload_version"], "compact_wrapper_v2_submission_marker")
        self.assertEqual(feedback["feedback_payload_length"], len(message))
        self.assertEqual(feedback["feedback_payload_sha256"], _sha(message))
        self.assertTrue(message.startswith("AGENT_SUBMISSION\n"))
        self.assertIn("payload_sha256=" + feedback["payload_without_marker_sha256"], feedback["submission_marker_text"])
        self.assertEqual(feedback["submission_marker_sha256"], _sha(feedback["submission_marker_text"]))
        self.assertEqual(feedback["submission_marker_payload_sha256"], _sha(feedback["payload_without_marker"]))

    def test_feedback_payload_includes_review_signal_when_needed(self) -> None:
        events = [
            self.event(
                "codex_exec_finished",
                {
                    "stdout": "done\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "final_message": "Needs review summary.",
                    "final_message_path": "/tmp/run-1/final-message.md",
                    "final_message_status": "valid",
                    "final_message_error": None,
                    "final_message_length": len("Needs review summary."),
                },
                1,
            ),
            self.event("prompt_repo_impact_diagnostics", {"flags": ["implementation_intent_docs_only"]}, 2),
            self.event(
                "supervision_decision",
                {
                    "decision": "needs_review",
                    "needs_review": True,
                    "approval_required": False,
                },
                3,
            ),
        ]

        feedback = cli.build_gpt_feedback_message({"id": "run-1", "status": "needs_review"}, events)

        self.assertIn("- review_signal: needs_review", feedback["message"])
        self.assertIn("- stop_reason: needs_review", feedback["message"])
        self.assertNotIn("diagnostics_flags", feedback["message"])

    def test_feedback_payload_missing_or_empty_final_message_is_not_submittable(self) -> None:
        for name, metadata in [
            (
                "missing",
                {
                    "exit_code": 0,
                    "timed_out": False,
                    "final_message": "",
                    "final_message_path": "/tmp/run-1/final-message.md",
                    "final_message_status": "missing",
                    "final_message_error": "Codex final-message artifact was not written.",
                    "final_message_length": 0,
                },
            ),
            (
                "empty",
                {
                    "exit_code": 0,
                    "timed_out": False,
                    "final_message": "",
                    "final_message_path": "/tmp/run-1/final-message.md",
                    "final_message_status": "empty",
                    "final_message_error": "Codex final-message artifact was empty.",
                    "final_message_length": 0,
                },
            ),
        ]:
            with self.subTest(name=name):
                feedback = cli.build_gpt_feedback_message(
                    {"id": "run-1", "status": "completed"},
                    [self.event("codex_exec_finished", metadata, 1)],
                )

                self.assertFalse(feedback["submittable"])
                self.assertIsNone(feedback["message"])
                self.assertEqual(feedback["reason_code"], "codex_final_message_unavailable")
                self.assertEqual(feedback["feedback_payload_length"], 0)

    def test_feedback_payload_oversized_final_message_is_not_submittable(self) -> None:
        final_message = "x" * 12_001
        events = [
            self.event(
                "codex_exec_finished",
                {
                    "stdout": "raw stdout must not be submitted",
                    "stderr": "raw stderr must not be submitted",
                    "exit_code": 0,
                    "timed_out": False,
                    "final_message": final_message,
                    "final_message_path": "/tmp/run-1/final-message.md",
                    "final_message_status": "valid",
                    "final_message_error": None,
                    "final_message_length": len(final_message),
                },
                1,
            )
        ]

        feedback = cli.build_gpt_feedback_message({"id": "run-1", "status": "completed"}, events)

        self.assertFalse(feedback["submittable"])
        self.assertIsNone(feedback["message"])
        self.assertEqual(feedback["reason_code"], "codex_final_message_oversize")
        self.assertEqual(feedback["feedback_payload_length"], 0)
        self.assertEqual(
            feedback["transport_guard"]["final_message_sha256"],
            _sha(final_message),
        )
        self.assertNotIn(final_message, json.dumps(feedback))

    def test_feedback_payload_absolute_transport_limit_is_not_submittable(self) -> None:
        changed_files = [{"path": f"src/generated_{index:04d}.py"} for index in range(900)]
        final_message = "Clean final message under the final-message limit."
        events = [
            self.event(
                "codex_exec_finished",
                {
                    "exit_code": 0,
                    "timed_out": False,
                    "final_message": final_message,
                    "final_message_path": "/tmp/run-1/final-message.md",
                    "final_message_status": "valid",
                    "final_message_error": None,
                    "final_message_length": len(final_message),
                },
                1,
            ),
            self.event("changed_file_classification", {"files": changed_files}, 2),
        ]

        feedback = cli.build_gpt_feedback_message({"id": "run-1", "status": "completed"}, events)

        self.assertFalse(feedback["submittable"])
        self.assertIsNone(feedback["message"])
        self.assertEqual(feedback["reason_code"], "chatgpt_feedback_payload_oversize")
        self.assertGreater(feedback["transport_guard"]["attempted_payload_length"], 16_000)


class WorkspaceWritePolicyTests(unittest.TestCase):
    def assertAllowed(self, prompt: str) -> dict:
        result = classify_workspace_write_prompt(prompt, "workspace-write")
        self.assertTrue(result.allowed, result.to_dict())
        self.assertEqual(result.tier, "workspace_write_scoped_auto")
        self.assertIsNotNone(result.expected_scope)
        return result.to_dict()

    def assertDenied(self, prompt: str, reason: str) -> None:
        result = classify_workspace_write_prompt(prompt, "workspace-write")
        self.assertFalse(result.allowed, result.to_dict())
        self.assertEqual(result.reason_code, reason)

    def test_named_source_file_fix_auto_runs_in_workspace_write(self) -> None:
        data = self.assertAllowed("Only change ProfileView.swift.")
        self.assertIn("ProfileView.swift", data["expected_scope"]["explicit_files"])

    def test_named_source_and_focused_test_change_auto_runs(self) -> None:
        data = self.assertAllowed("Modify ProfileView.swift and related focused tests only.")
        self.assertIn("ProfileView.swift", data["expected_scope"]["explicit_files"])

    def test_docs_only_write_auto_runs(self) -> None:
        data = self.assertAllowed("Update docs/usage.md with focused documentation for this behavior.")
        self.assertIn("docs", data["expected_scope"]["allowed_categories"])

    def test_bounded_ui_layout_change_auto_runs(self) -> None:
        data = self.assertAllowed("Fix the empty-state layout in ProfileView.swift and preserve behavior.")
        self.assertEqual(data["reason_code"], "workspace_write_scoped_auto")

    def test_workspace_write_prompt_denials_are_disabled_observations(self) -> None:
        cases = [
            "Only edit ../README.md.",
            "Run rm -rf /tmp/example from the app.",
        ]
        for prompt in cases:
            with self.subTest(prompt=prompt):
                data = self.assertAllowed(prompt)
                self.assertIn("safety_classifiers_disabled", data["matched_rules"])

    def test_workspace_write_generic_and_category_terms_are_observation_only(self) -> None:
        cases = [
            "This is a temporary local test. Only edit README.md.",
            "Perform an audit and update README.md.",
            "Add a Supabase RLS migration for profiles.",
            "Upgrade package.json and package-lock.json.",
            "Fix auth session verification in AuthService.swift.",
            "Update Docker deployment metadata.",
        ]
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertAllowed(prompt)

    def test_post_run_records_prohibited_categories_as_observations(self) -> None:
        scope = ExpectedScope(explicit_files=["agent/foo.py"], allowed_categories=["python_source", "tests"])
        changed = ["agent/foo.py", "migrations/001.sql"]
        result = verify_workspace_write_post_run(
            scope,
            changed,
            "M\tagent/foo.py\nA\tmigrations/001.sql",
            "diff --git a/migrations/001.sql b/migrations/001.sql\n+select 1;\n",
            classify_changed_files(changed),
        )
        self.assertTrue(result.allowed, result.to_dict())
        self.assertEqual(result.reason_code, "post_run_observations_recorded")
        self.assertIn("migrations/001.sql", result.prohibited_files)

    def test_post_run_records_too_many_changed_files_as_observation(self) -> None:
        changed = [f"agent/file{i}.py" for i in range(5)]
        scope = ExpectedScope(allowed_categories=["python_source"], max_changed_files=4)
        result = verify_workspace_write_post_run(
            scope,
            changed,
            "\n".join(f"M\t{path}" for path in changed),
            "",
            classify_changed_files(changed),
        )
        self.assertTrue(result.allowed, result.to_dict())

    def test_post_run_records_delete_and_rename_as_observations(self) -> None:
        scope = ExpectedScope(explicit_files=["agent/foo.py"], allowed_categories=["python_source"])
        delete_result = verify_workspace_write_post_run(
            scope,
            ["agent/foo.py"],
            "D\tagent/foo.py",
            "",
            classify_changed_files(["agent/foo.py"]),
        )
        rename_result = verify_workspace_write_post_run(
            scope,
            ["agent/bar.py"],
            "R100\tagent/foo.py\tagent/bar.py",
            "",
            classify_changed_files(["agent/bar.py"]),
        )
        self.assertTrue(delete_result.allowed, delete_result.to_dict())
        self.assertTrue(rename_result.allowed, rename_result.to_dict())

    def test_post_run_secret_literals_are_observation_only(self) -> None:
        scope = ExpectedScope(explicit_files=["agent/foo.py"], allowed_categories=["python_source"])
        low_confidence_secret_result = verify_workspace_write_post_run(
            scope,
            ["agent/foo.py"],
            "M\tagent/foo.py",
            "+API_TOKEN = 'abc123456789abcdef'\n",
            classify_changed_files(["agent/foo.py"]),
        )
        destructive_result = verify_workspace_write_post_run(
            scope,
            ["agent/foo.py"],
            "M\tagent/foo.py",
            "+os.system('rm -rf /tmp/example')\n",
            classify_changed_files(["agent/foo.py"]),
        )
        high_confidence_secret_result = verify_workspace_write_post_run(
            scope,
            ["agent/foo.py"],
            "M\tagent/foo.py",
            "+AWS_SECRET_ACCESS_KEY='abcdefghijklmnopqrstuvwxyzABCDEF123456'\n",
            classify_changed_files(["agent/foo.py"]),
        )
        self.assertTrue(low_confidence_secret_result.allowed, low_confidence_secret_result.to_dict())
        self.assertIn("secret_like_content", low_confidence_secret_result.diff_content_flags)
        self.assertTrue(destructive_result.allowed, destructive_result.to_dict())
        self.assertIn("external_or_destructive_command", destructive_result.diff_content_flags)
        self.assertTrue(high_confidence_secret_result.allowed, high_confidence_secret_result.to_dict())
        self.assertEqual(high_confidence_secret_result.reason_code, "post_run_observations_recorded")
        self.assertIn("high_confidence_secret_literal", high_confidence_secret_result.diff_content_flags)

    def test_post_run_accepts_expected_scoped_source_test_docs_edits(self) -> None:
        changed = ["agent/foo.py", "tests/test_foo.py", "docs/usage.md"]
        scope = ExpectedScope(
            explicit_files=changed,
            allowed_categories=["python_source", "tests", "docs"],
            max_changed_files=4,
            confidence="explicit",
        )
        result = verify_workspace_write_post_run(
            scope,
            changed,
            "\n".join(f"M\t{path}" for path in changed),
            "+small local source change\n+test assertion\n+docs line\n",
            classify_changed_files(changed),
        )
        self.assertTrue(result.allowed, result.to_dict())
        self.assertEqual(result.reason_code, "post_run_diff_within_expected_scope")


class _FakeLedger:
    def __init__(self, run: dict, events: list[dict]) -> None:
        self._run = run
        self._events = events
        self.added_events: list[tuple] = []
        self._lease_counter = 0
        self._active_lease: dict | None = None
        self._next_id = max(
            [int(event.get("id") or 0) for event in events if str(event.get("id") or "").isdigit()],
            default=0,
        ) + 1
        self._ensure_destination_binding()

    def get_run(self, run_id: str) -> dict:
        return self._run

    def list_events(self, run_id: str) -> list[dict]:
        return self._events

    def add_event(self, *args, **kwargs) -> None:
        self.added_events.append((args, kwargs))
        if len(args) >= 4:
            self.append_event(str(args[1]), args[3])

    def update_run_status(self, run_id: str, status: object) -> None:
        self._run = {**self._run, "status": getattr(status, "value", str(status))}

    def acquire_chatgpt_ui_lease(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ):
        self._lease_counter += 1
        lease_token = f"fake-chatgpt-ui-lease-{self._lease_counter}"
        owner_pid = 222
        acquired_at = f"2026-01-01T00:00:{self._lease_counter:02d}+00:00"
        metadata = {
            "schema_version": default_ledger.CHATGPT_UI_LEASE_SCHEMA_VERSION,
            "lease_token_sha256": default_ledger.chatgpt_ui_lease_token_fingerprint(
                lease_token
            ),
            "owner_pid": owner_pid,
            "owning_run_id": run_id,
            "acquired_at": acquired_at,
            "reason": reason,
            "source": source,
        }
        event = self.append_event(
            default_ledger.CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
            metadata,
        )
        event["run_id"] = run_id
        self._active_lease = {
            "run_id": run_id,
            "lease_token": lease_token,
            "owner_pid": owner_pid,
            "acquired_at": acquired_at,
        }
        return default_ledger.AtomicChatGPTUILeaseResult(
            status=default_ledger.AtomicChatGPTUILeaseStatus.ACQUIRED,
            run_id=run_id,
            lease_token=lease_token,
            owner_pid=owner_pid,
            owning_run_id=run_id,
            acquired_at=acquired_at,
            event_id=event["id"],
            event_written=True,
            event_ids=(event["id"],),
        )

    def release_chatgpt_ui_lease(
        self,
        lease_token: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ):
        active = self._active_lease or {
            "run_id": self._run.get("id", "run-1"),
            "lease_token": lease_token,
            "owner_pid": 222,
            "acquired_at": "2026-01-01T00:00:00+00:00",
        }
        metadata = {
            "schema_version": default_ledger.CHATGPT_UI_LEASE_SCHEMA_VERSION,
            "lease_token_sha256": default_ledger.chatgpt_ui_lease_token_fingerprint(
                lease_token
            ),
            "owner_pid": active["owner_pid"],
            "owning_run_id": active["run_id"],
            "acquired_at": active["acquired_at"],
            "released_at": "2026-01-01T00:01:00+00:00",
            "reason": reason,
            "source": source,
        }
        event = self.append_event(
            default_ledger.CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
            metadata,
        )
        event["run_id"] = active["run_id"]
        self._active_lease = None
        return default_ledger.AtomicChatGPTUILeaseResult(
            status=default_ledger.AtomicChatGPTUILeaseStatus.RELEASED,
            lease_token=lease_token,
            owner_pid=active["owner_pid"],
            owning_run_id=active["run_id"],
            acquired_at=active["acquired_at"],
            released_at="2026-01-01T00:01:00+00:00",
            event_id=event["id"],
            event_written=True,
            event_ids=(event["id"],),
        )

    def list_chatgpt_ui_lease_events(self) -> list[dict]:
        return [
            event
            for event in self._events
            if event.get("event_type")
            in {
                default_ledger.CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
                default_ledger.CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
            }
        ]

    def append_event(self, event_type: str, metadata: dict | None = None) -> dict:
        event = {
            "id": self._next_id,
            "event_type": event_type,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        self._next_id += 1
        self._events.append(event)
        return event

    def _ensure_destination_binding(self) -> None:
        if any(
            event.get("event_type") == default_ledger.RUN_DESTINATION_BOUND_EVENT_TYPE
            for event in self._events
        ):
            return
        self._events.append(
            {
                "id": 0,
                "run_id": self._run.get("id", "run-1"),
                "event_type": default_ledger.RUN_DESTINATION_BOUND_EVENT_TYPE,
                "metadata_json": json.dumps(
                    {
                        "schema_version": default_ledger.RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                        "project_title": "Project Alpha",
                        "chat_title": "Main Chat",
                    },
                    sort_keys=True,
                ),
            }
        )


class _FakeDestinationAdapter:
    def read_destination_snapshot(self) -> ChatGPTDestinationSnapshot:
        return ChatGPTDestinationSnapshot(
            process_running=True,
            window_available=True,
            accessibility_available=True,
            snapshot_stable=True,
            snapshot_complete=True,
            active_project_candidates=(
                DestinationEvidenceCandidate(
                    "Project Alpha",
                    active=True,
                    identity_confirmed=True,
                    actionable_destination_evidence=True,
                    project_chats_list_confirmed=True,
                ),
            ),
            selected_chat_row_candidates=(
                DestinationEvidenceCandidate(
                    "Main Chat",
                    selected=True,
                    identity_confirmed=True,
                    actionable_destination_evidence=True,
                ),
            ),
            conversation_header_candidates=(
                DestinationEvidenceCandidate(
                    "Main Chat",
                    active=True,
                    identity_confirmed=True,
                    actionable_destination_evidence=True,
                ),
            ),
            composer_available=True,
            transcript_available=True,
            conversation_surface_available=True,
            composer_candidate_count=1,
            conversation_surface_candidate_count=1,
            active_conversation_chat_title="Main Chat",
            active_conversation_project_title="Project Alpha",
        )


if __name__ == "__main__":
    unittest.main()
