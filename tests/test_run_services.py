from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from agent import cli
from agent.run_services import HumanDecision, create_run_service, resolve_human_decision
from agent.run_state import RunStatus


class FakeCreateRunLedger:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.runs: list[dict] = []
        self.events: list[dict] = []
        self.create_run_calls: list[str] = []
        self._next_run_number = 1
        self._next_event_id = 200

    def create_run(self, user_instruction: str) -> str:
        self.create_run_calls.append(user_instruction)
        if self.fail_create:
            raise RuntimeError("create failed")

        run_id = f"run-{self._next_run_number}"
        self._next_run_number += 1
        self.runs.append(
            {
                "id": run_id,
                "status": RunStatus.CREATED.value,
                "user_instruction": user_instruction,
            }
        )
        return run_id

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> int:
        event_id = self._next_event_id
        self._next_event_id += 1
        self.events.append(
            {
                "id": event_id,
                "run_id": run_id,
                "event_type": event_type,
                "message": message,
                "metadata": metadata,
            }
        )
        return event_id


class FakeDecisionLedger:
    def __init__(self, status: str | None) -> None:
        self.run = None
        if status is not None:
            self.run = {
                "id": "run-1",
                "status": status,
                "final_summary": "kept summary",
                "error": "kept error",
            }
        self.events: list[dict] = []
        self.status_updates: list[dict] = []
        self._next_event_id = 100

    def get_run(self, run_id: str) -> dict | None:
        if self.run is None or run_id != self.run["id"]:
            return None
        return self.run

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        final_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        self.status_updates.append(
            {
                "run_id": run_id,
                "status": status,
                "final_summary": final_summary,
                "error": error,
            }
        )
        if self.run is not None:
            self.run["status"] = status.value
            self.run["final_summary"] = final_summary
            self.run["error"] = error

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> int:
        event_id = self._next_event_id
        self._next_event_id += 1
        self.events.append(
            {
                "id": event_id,
                "run_id": run_id,
                "event_type": event_type,
                "message": message,
                "metadata": metadata,
            }
        )
        return event_id


class CreateRunServiceTests(unittest.TestCase):
    def test_success_returns_structured_result_and_writes_one_run_and_event(self) -> None:
        ledger = FakeCreateRunLedger()

        result = create_run_service(" keep this instruction exactly ", ledger=ledger)

        self.assertTrue(result.ok)
        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(result.user_instruction, " keep this instruction exactly ")
        self.assertEqual(result.initial_status, RunStatus.CREATED.value)
        self.assertEqual(result.event_type, "run_created")
        self.assertEqual(result.event_id, 200)
        self.assertEqual(result.message, "Run created.")
        self.assertIsNone(result.metadata)
        self.assertIsNone(result.reason_code)
        self.assertIsNone(result.error_message)
        self.assertEqual(ledger.create_run_calls, [" keep this instruction exactly "])
        self.assertEqual(
            ledger.runs,
            [
                {
                    "id": "run-1",
                    "status": RunStatus.CREATED.value,
                    "user_instruction": " keep this instruction exactly ",
                }
            ],
        )
        self.assertEqual(
            ledger.events,
            [
                {
                    "id": 200,
                    "run_id": "run-1",
                    "event_type": "run_created",
                    "message": "Run created.",
                    "metadata": None,
                }
            ],
        )

    def test_expected_create_failure_returns_structured_failure_without_system_exit(self) -> None:
        ledger = FakeCreateRunLedger(fail_create=True)

        result = create_run_service("cannot create", ledger=ledger)

        self.assertFalse(result.ok)
        self.assertIsNone(result.run_id)
        self.assertEqual(result.user_instruction, "cannot create")
        self.assertEqual(result.reason_code, "run_create_failed")
        self.assertEqual(result.error_message, "create failed")
        self.assertEqual(ledger.create_run_calls, ["cannot create"])
        self.assertEqual(ledger.runs, [])
        self.assertEqual(ledger.events, [])


class CreateRunCliCompatibilityTests(unittest.TestCase):
    def test_start_prints_only_run_id_and_writes_one_run_and_event(self) -> None:
        ledger = FakeCreateRunLedger()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(cli, "ledger", ledger),
            mock.patch("sys.argv", ["agent-loop", "start", "CLI instruction"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            cli.main()

        self.assertEqual(stdout.getvalue(), "run-1\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(ledger.runs), 1)
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(ledger.runs[0]["status"], RunStatus.CREATED.value)
        self.assertEqual(ledger.runs[0]["user_instruction"], "CLI instruction")
        self.assertEqual(
            ledger.events,
            [
                {
                    "id": 200,
                    "run_id": "run-1",
                    "event_type": "run_created",
                    "message": "Run created.",
                    "metadata": None,
                }
            ],
        )

    def test_start_matches_service_path_without_duplicate_run_or_event(self) -> None:
        service_ledger = FakeCreateRunLedger()
        cli_ledger = FakeCreateRunLedger()
        stdout = io.StringIO()

        service_result = create_run_service("same behavior", ledger=service_ledger)
        with (
            mock.patch.object(cli, "ledger", cli_ledger),
            mock.patch("sys.argv", ["agent-loop", "start", "same behavior"]),
            contextlib.redirect_stdout(stdout),
        ):
            cli.main()

        self.assertTrue(service_result.ok)
        self.assertEqual(stdout.getvalue(), "run-1\n")
        self.assertEqual(cli_ledger.runs, service_ledger.runs)
        self.assertEqual(cli_ledger.events, service_ledger.events)
        self.assertEqual(len(cli_ledger.runs), 1)
        self.assertEqual(len(cli_ledger.events), 1)


class HumanDecisionServiceTests(unittest.TestCase):
    def test_approve_succeeds_from_every_allowed_status(self) -> None:
        for status in (
            RunStatus.WAITING_FOR_APPROVAL.value,
            RunStatus.NEEDS_REVIEW.value,
        ):
            with self.subTest(status=status):
                ledger = FakeDecisionLedger(status)
                result = resolve_human_decision(
                    "run-1",
                    HumanDecision.APPROVE,
                    note="ship it",
                    ledger=ledger,
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.previous_status, status)
                self.assertEqual(result.next_status, RunStatus.APPROVED.value)
                self.assertEqual(result.event_type, "human_approval")
                self.assertEqual(result.event_id, 100)
                self.assertEqual(ledger.run["status"], RunStatus.APPROVED.value)
                self.assertEqual(
                    ledger.status_updates,
                    [
                        {
                            "run_id": "run-1",
                            "status": RunStatus.APPROVED,
                            "final_summary": "kept summary",
                            "error": "kept error",
                        }
                    ],
                )
                self.assertEqual(
                    ledger.events,
                    [
                        {
                            "id": 100,
                            "run_id": "run-1",
                            "event_type": "human_approval",
                            "message": "Run approved by user.",
                            "metadata": {
                                "previous_status": status,
                                "next_status": RunStatus.APPROVED.value,
                                "note": "ship it",
                            },
                        }
                    ],
                )

    def test_reject_succeeds_from_every_allowed_status(self) -> None:
        for status in (
            RunStatus.WAITING_FOR_APPROVAL.value,
            RunStatus.NEEDS_REVIEW.value,
        ):
            with self.subTest(status=status):
                ledger = FakeDecisionLedger(status)
                result = resolve_human_decision(
                    "run-1",
                    HumanDecision.REJECT,
                    note="do not continue",
                    ledger=ledger,
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.previous_status, status)
                self.assertEqual(result.next_status, RunStatus.REJECTED.value)
                self.assertEqual(result.event_type, "human_rejection")
                self.assertEqual(ledger.run["status"], RunStatus.REJECTED.value)
                self.assertEqual(
                    ledger.events[0],
                    {
                        "id": 100,
                        "run_id": "run-1",
                        "event_type": "human_rejection",
                        "message": "Run rejected by user.",
                        "metadata": {
                            "previous_status": status,
                            "next_status": RunStatus.REJECTED.value,
                            "note": "do not continue",
                        },
                    },
                )

    def test_complete_review_succeeds_from_every_allowed_status(self) -> None:
        ledger = FakeDecisionLedger(RunStatus.NEEDS_REVIEW.value)
        result = resolve_human_decision(
            "run-1",
            HumanDecision.COMPLETE_REVIEW,
            note="review complete",
            ledger=ledger,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.previous_status, RunStatus.NEEDS_REVIEW.value)
        self.assertEqual(result.next_status, RunStatus.COMPLETED.value)
        self.assertEqual(result.event_type, "human_review_completed")
        self.assertEqual(ledger.run["status"], RunStatus.COMPLETED.value)
        self.assertEqual(
            ledger.events,
            [
                {
                    "id": 100,
                    "run_id": "run-1",
                    "event_type": "human_review_completed",
                    "message": "Run review completed by user.",
                    "metadata": {
                        "previous_status": RunStatus.NEEDS_REVIEW.value,
                        "next_status": RunStatus.COMPLETED.value,
                        "note": "review complete",
                    },
                }
            ],
        )

    def test_invalid_decisions_do_not_change_status_and_write_rejected_events(self) -> None:
        cases = [
            (
                HumanDecision.APPROVE,
                RunStatus.CREATED.value,
                "human_approval_rejected_by_state",
                "Cannot approve run from current status 'created'. "
                "Allowed statuses: needs_review, waiting_for_approval.",
            ),
            (
                HumanDecision.REJECT,
                RunStatus.COMPLETED.value,
                "human_rejection_rejected_by_state",
                "Cannot reject run from current status 'completed'. "
                "Allowed statuses: needs_review, waiting_for_approval.",
            ),
            (
                HumanDecision.COMPLETE_REVIEW,
                RunStatus.WAITING_FOR_APPROVAL.value,
                "human_review_completion_rejected_by_state",
                "Cannot complete review for run from current status 'waiting_for_approval'. "
                "Allowed statuses: needs_review.",
            ),
        ]

        for decision, status, event_type, message in cases:
            with self.subTest(decision=decision, status=status):
                ledger = FakeDecisionLedger(status)
                result = resolve_human_decision(
                    "run-1",
                    decision,
                    note="same note",
                    ledger=ledger,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "invalid_status")
                self.assertEqual(result.error_message, message)
                self.assertEqual(result.previous_status, status)
                self.assertEqual(result.event_type, event_type)
                self.assertEqual(result.event_id, 100)
                self.assertEqual(ledger.run["status"], status)
                self.assertEqual(ledger.status_updates, [])
                self.assertEqual(
                    ledger.events,
                    [
                        {
                            "id": 100,
                            "run_id": "run-1",
                            "event_type": event_type,
                            "message": message,
                            "metadata": {
                                "current_status": status,
                                "note": "same note",
                            },
                        }
                    ],
                )

    def test_note_is_preserved_exactly(self) -> None:
        note = " keep  spacing\nand newline "
        ledger = FakeDecisionLedger(RunStatus.WAITING_FOR_APPROVAL.value)

        result = resolve_human_decision(
            "run-1",
            "approve",
            note=note,
            ledger=ledger,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.metadata["note"], note)
        self.assertEqual(ledger.events[0]["metadata"]["note"], note)

    def test_invalid_state_returns_structured_failure_without_system_exit(self) -> None:
        ledger = FakeDecisionLedger(RunStatus.CREATED.value)

        result = resolve_human_decision(
            "run-1",
            HumanDecision.APPROVE,
            ledger=ledger,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "invalid_status")
        self.assertEqual(ledger.run["status"], RunStatus.CREATED.value)

    def test_missing_run_returns_structured_failure_without_writing_event(self) -> None:
        ledger = FakeDecisionLedger(None)

        result = resolve_human_decision(
            "run-1",
            HumanDecision.APPROVE,
            ledger=ledger,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "run_not_found")
        self.assertEqual(result.error_message, "Run not found: run-1")
        self.assertEqual(ledger.events, [])
        self.assertEqual(ledger.status_updates, [])

    def test_complete_review_accepts_cli_style_decision_alias(self) -> None:
        ledger = FakeDecisionLedger(RunStatus.NEEDS_REVIEW.value)

        result = resolve_human_decision(
            "run-1",
            "complete-review",
            ledger=ledger,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.decision, HumanDecision.COMPLETE_REVIEW.value)
        self.assertEqual(ledger.run["status"], RunStatus.COMPLETED.value)


class HumanDecisionCliCompatibilityTests(unittest.TestCase):
    def test_resolve_flagged_run_wrapper_preserves_success_output(self) -> None:
        ledger = FakeDecisionLedger(RunStatus.NEEDS_REVIEW.value)
        stdout = io.StringIO()

        with mock.patch.object(cli, "ledger", ledger), contextlib.redirect_stdout(stdout):
            cli._resolve_flagged_run(
                "run-1",
                ledger.run,
                "ok",
                {RunStatus.WAITING_FOR_APPROVAL.value, RunStatus.NEEDS_REVIEW.value},
                RunStatus.APPROVED,
                "human_approval",
                "Run approved by user.",
                "human_approval_rejected_by_state",
                "approve",
            )

        self.assertEqual(
            stdout.getvalue(),
            "previous_status: needs_review\nnext_status: approved\nnote: ok\n",
        )
        self.assertEqual(ledger.events[0]["event_type"], "human_approval")

    def test_resolve_flagged_run_wrapper_preserves_invalid_state_exit(self) -> None:
        ledger = FakeDecisionLedger(RunStatus.CREATED.value)
        stderr = io.StringIO()

        with (
            mock.patch.object(cli, "ledger", ledger),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli._resolve_flagged_run(
                "run-1",
                ledger.run,
                "no",
                {RunStatus.WAITING_FOR_APPROVAL.value, RunStatus.NEEDS_REVIEW.value},
                RunStatus.APPROVED,
                "human_approval",
                "Run approved by user.",
                "human_approval_rejected_by_state",
                "approve",
            )

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "error: Cannot approve run from current status 'created'. "
            "Allowed statuses: needs_review, waiting_for_approval.\n",
        )
        self.assertEqual(ledger.status_updates, [])
        self.assertEqual(ledger.events[0]["event_type"], "human_approval_rejected_by_state")


if __name__ == "__main__":
    unittest.main()
