from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent import cli
from agent import ledger as ledger_module
from agent.run_services import (
    CODEX_DEFAULT_SELECTION,
    RUN_DESTINATION_BOUND_EVENT_TYPE,
    RUN_DESTINATION_BOUND_MESSAGE,
    RUN_DESTINATION_BOUND_SCHEMA_VERSION,
    RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
    RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
    RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
    DestinationBindingLookupStatus,
    ExecutionProfileLookupStatus,
    HumanDecision,
    RunDestinationBinding,
    RunExecutionProfile,
    bind_run_destination,
    create_run_service,
    get_run_destination_binding,
    get_run_execution_profile,
    resolve_human_decision,
    select_run_execution_profile,
)
from agent.run_state import RunStatus


class FakeCreateRunLedger:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.runs: list[dict] = []
        self.events: list[dict] = []
        self.create_run_calls: list[str] = []
        self.get_run_calls: list[str] = []
        self.list_events_calls: list[str] = []
        self.destination_bind_calls: list[dict] = []
        self.execution_profile_bind_calls: list[dict] = []
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

    def get_run(self, run_id: str) -> dict | None:
        self.get_run_calls.append(run_id)
        for run in self.runs:
            if run["id"] == run_id:
                return run
        return None

    def list_events(self, run_id: str) -> list[dict]:
        self.list_events_calls.append(run_id)
        return [event for event in self.events if event["run_id"] == run_id]

    def bind_run_destination(
        self,
        run_id: str,
        project_title: str,
        chat_title: str,
    ) -> ledger_module.AtomicDestinationBindingResult:
        self.destination_bind_calls.append(
            {
                "run_id": run_id,
                "project_title": project_title,
                "chat_title": chat_title,
            }
        )
        if self.get_run(run_id) is None:
            return ledger_module.AtomicDestinationBindingResult(
                status=ledger_module.AtomicDestinationBindingStatus.RUN_NOT_FOUND,
                run_id=run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
            )

        existing = get_run_destination_binding(run_id, ledger=self)
        if existing.status == DestinationBindingLookupStatus.INVALID:
            return ledger_module.AtomicDestinationBindingResult(
                status=ledger_module.AtomicDestinationBindingStatus.INVALID,
                run_id=run_id,
                reason_code=existing.reason_code,
                error_message=existing.error_message,
                event_ids=existing.event_ids,
            )
        if existing.status == DestinationBindingLookupStatus.PRESENT:
            if existing.binding == RunDestinationBinding(project_title, chat_title):
                return ledger_module.AtomicDestinationBindingResult(
                    status=ledger_module.AtomicDestinationBindingStatus.IDEMPOTENT,
                    run_id=run_id,
                    project_title=existing.binding.project_title,
                    chat_title=existing.binding.chat_title,
                    event_ids=existing.event_ids,
                )
            return ledger_module.AtomicDestinationBindingResult(
                status=ledger_module.AtomicDestinationBindingStatus.DIFFERENT_DESTINATION,
                run_id=run_id,
                project_title=existing.binding.project_title,
                chat_title=existing.binding.chat_title,
                reason_code="destination_already_bound_to_different_destination",
                error_message="Run is already bound to a different destination.",
                event_ids=existing.event_ids,
            )

        event_id = self.add_event(
            run_id,
            RUN_DESTINATION_BOUND_EVENT_TYPE,
            RUN_DESTINATION_BOUND_MESSAGE,
            metadata={
                "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                "project_title": project_title,
                "chat_title": chat_title,
            },
        )
        return ledger_module.AtomicDestinationBindingResult(
            status=ledger_module.AtomicDestinationBindingStatus.BOUND,
            run_id=run_id,
            project_title=project_title,
            chat_title=chat_title,
            event_id=event_id,
            event_written=True,
        )

    def bind_run_execution_profile(
        self,
        run_id: str,
        sandbox: str,
        model: str,
        reasoning_effort: str,
        approval_policy: str,
        profile_source: str,
    ) -> ledger_module.AtomicExecutionProfileResult:
        self.execution_profile_bind_calls.append(
            {
                "run_id": run_id,
                "sandbox": sandbox,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "approval_policy": approval_policy,
                "profile_source": profile_source,
            }
        )
        if self.get_run(run_id) is None:
            return ledger_module.AtomicExecutionProfileResult(
                status=ledger_module.AtomicExecutionProfileStatus.RUN_NOT_FOUND,
                run_id=run_id,
                reason_code="run_not_found",
                error_message=f"Run not found: {run_id}",
            )

        existing = get_run_execution_profile(run_id, ledger=self)
        if existing.status == ExecutionProfileLookupStatus.INVALID:
            return ledger_module.AtomicExecutionProfileResult(
                status=ledger_module.AtomicExecutionProfileStatus.INVALID,
                run_id=run_id,
                reason_code=existing.reason_code,
                error_message=existing.error_message,
                event_ids=existing.event_ids,
            )

        existing_profile = existing.profile
        if any(
            event["run_id"] == run_id and event["event_type"] == "codex_exec_started"
            for event in self.events
        ):
            return ledger_module.AtomicExecutionProfileResult(
                status=ledger_module.AtomicExecutionProfileStatus.EXECUTION_STARTED,
                run_id=run_id,
                sandbox=existing_profile.sandbox if existing_profile else None,
                model=existing_profile.model if existing_profile else None,
                reasoning_effort=(
                    existing_profile.reasoning_effort if existing_profile else None
                ),
                approval_policy=(
                    existing_profile.approval_policy if existing_profile else None
                ),
                profile_source=(
                    existing_profile.profile_source if existing_profile else None
                ),
                reason_code="execution_profile_immutable_after_codex_exec_started",
                error_message=(
                    "Run execution profile cannot be selected after Codex execution "
                    "has started."
                ),
                event_ids=existing.event_ids,
            )

        requested = RunExecutionProfile(
            sandbox,
            model,
            reasoning_effort,
            approval_policy,
            profile_source,
        )
        if existing.status == ExecutionProfileLookupStatus.PRESENT:
            if existing_profile == requested:
                return ledger_module.AtomicExecutionProfileResult(
                    status=ledger_module.AtomicExecutionProfileStatus.IDEMPOTENT,
                    run_id=run_id,
                    sandbox=existing_profile.sandbox,
                    model=existing_profile.model,
                    reasoning_effort=existing_profile.reasoning_effort,
                    approval_policy=existing_profile.approval_policy,
                    profile_source=existing_profile.profile_source,
                    event_ids=existing.event_ids,
                )
            return ledger_module.AtomicExecutionProfileResult(
                status=ledger_module.AtomicExecutionProfileStatus.DIFFERENT_PROFILE,
                run_id=run_id,
                sandbox=existing_profile.sandbox,
                model=existing_profile.model,
                reasoning_effort=existing_profile.reasoning_effort,
                approval_policy=existing_profile.approval_policy,
                profile_source=existing_profile.profile_source,
                reason_code="execution_profile_already_selected_different_profile",
                error_message="Run already has a different execution profile.",
                event_ids=existing.event_ids,
            )

        event_id = self.add_event(
            run_id,
            RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
            RUN_EXECUTION_PROFILE_SELECTED_MESSAGE,
            metadata={
                "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
                "sandbox": sandbox,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "approval_policy": approval_policy,
                "profile_source": profile_source,
            },
        )
        return ledger_module.AtomicExecutionProfileResult(
            status=ledger_module.AtomicExecutionProfileStatus.SELECTED,
            run_id=run_id,
            sandbox=sandbox,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            profile_source=profile_source,
            event_id=event_id,
            event_written=True,
        )


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

    def test_create_without_destination_preserves_existing_behavior(self) -> None:
        ledger = FakeCreateRunLedger()

        result = create_run_service("no destination yet", ledger=ledger)

        self.assertTrue(result.ok)
        self.assertEqual(result.event_type, "run_created")
        self.assertIsNone(result.destination_binding)
        self.assertIsNone(result.destination_event_id)
        self.assertEqual(len(ledger.runs), 1)
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

    def test_create_with_one_destination_field_rejects_without_creating_run(self) -> None:
        cases = [
            {"project_title": "Project", "chat_title": None},
            {"project_title": None, "chat_title": "Chat"},
        ]

        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                ledger = FakeCreateRunLedger()

                result = create_run_service("partial", ledger=ledger, **kwargs)

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "partial_destination")
                self.assertEqual(ledger.runs, [])
                self.assertEqual(ledger.events, [])

    def test_create_with_destination_persists_valid_pair(self) -> None:
        ledger = FakeCreateRunLedger()

        result = create_run_service(
            "with destination",
            project_title=" Project ",
            chat_title=" Chat ",
            ledger=ledger,
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            result.destination_binding,
            RunDestinationBinding("Project", "Chat"),
        )
        self.assertEqual(result.destination_event_id, 201)
        self.assertEqual(
            ledger.events,
            [
                {
                    "id": 200,
                    "run_id": "run-1",
                    "event_type": "run_created",
                    "message": "Run created.",
                    "metadata": None,
                },
                {
                    "id": 201,
                    "run_id": "run-1",
                    "event_type": RUN_DESTINATION_BOUND_EVENT_TYPE,
                    "message": RUN_DESTINATION_BOUND_MESSAGE,
                    "metadata": {
                        "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                        "project_title": "Project",
                        "chat_title": "Chat",
                    },
                },
            ],
        )


class RunDestinationBindingServiceTests(unittest.TestCase):
    def _ledger_with_run(self) -> FakeCreateRunLedger:
        ledger = FakeCreateRunLedger()
        ledger.create_run("existing run")
        return ledger

    def test_valid_normalization_preserves_interior_whitespace_and_trims_edges(self) -> None:
        binding = RunDestinationBinding(
            "  Project   With\tInterior  Space  ",
            "\nChat   With\tInterior  Space\n",
        )

        self.assertEqual(binding.project_title, "Project   With\tInterior  Space")
        self.assertEqual(binding.chat_title, "Chat   With\tInterior  Space")

    def test_empty_or_whitespace_only_destination_values_are_rejected(self) -> None:
        cases = [
            ("", "Chat", "project_title must not be empty"),
            ("   ", "Chat", "project_title must not be empty"),
            ("Project", "", "chat_title must not be empty"),
            ("Project", "\n\t ", "chat_title must not be empty"),
        ]

        for project_title, chat_title, message in cases:
            with self.subTest(project_title=project_title, chat_title=chat_title):
                ledger = FakeCreateRunLedger()

                result = bind_run_destination(
                    "run-1",
                    project_title,
                    chat_title,
                    ledger=ledger,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "invalid_destination")
                self.assertEqual(result.error_message, message)
                self.assertEqual(ledger.destination_bind_calls, [])
                self.assertEqual(ledger.events, [])

    def test_first_bind_writes_exactly_one_durable_binding_event(self) -> None:
        ledger = self._ledger_with_run()

        result = bind_run_destination(
            "run-1",
            " Project   Alpha ",
            " Chat   One ",
            ledger=ledger,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.event_written)
        self.assertEqual(result.event_id, 200)
        self.assertEqual(
            result.binding,
            RunDestinationBinding("Project   Alpha", "Chat   One"),
        )
        self.assertEqual(
            result.metadata,
            {
                "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                "project_title": "Project   Alpha",
                "chat_title": "Chat   One",
            },
        )
        self.assertEqual(
            ledger.events,
            [
                {
                    "id": 200,
                    "run_id": "run-1",
                    "event_type": RUN_DESTINATION_BOUND_EVENT_TYPE,
                    "message": RUN_DESTINATION_BOUND_MESSAGE,
                    "metadata": {
                        "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                        "project_title": "Project   Alpha",
                        "chat_title": "Chat   One",
                    },
                }
            ],
        )

    def test_bind_to_nonexistent_run_fails_without_reading_history_or_writing_event(self) -> None:
        ledger = FakeCreateRunLedger()

        result = bind_run_destination("missing-run", "Project", "Chat", ledger=ledger)

        self.assertFalse(result.ok)
        self.assertEqual(result.run_id, "missing-run")
        self.assertEqual(result.reason_code, "run_not_found")
        self.assertEqual(result.error_message, "Run not found: missing-run")
        self.assertEqual(
            ledger.destination_bind_calls,
            [
                {
                    "run_id": "missing-run",
                    "project_title": "Project",
                    "chat_title": "Chat",
                }
            ],
        )
        self.assertEqual(ledger.get_run_calls, ["missing-run"])
        self.assertEqual(ledger.list_events_calls, [])
        self.assertEqual(ledger.events, [])

    def test_binding_can_be_reconstructed_from_ledger_history(self) -> None:
        ledger = FakeCreateRunLedger()
        ledger.events.append(
            {
                "id": 200,
                "run_id": "run-1",
                "event_type": RUN_DESTINATION_BOUND_EVENT_TYPE,
                "message": RUN_DESTINATION_BOUND_MESSAGE,
                "metadata_json": json.dumps(
                    {
                        "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                        "project_title": "Project",
                        "chat_title": "Chat",
                    },
                    sort_keys=True,
                ),
            }
        )

        lookup = get_run_destination_binding("run-1", ledger=ledger)

        self.assertEqual(lookup.status, DestinationBindingLookupStatus.PRESENT)
        self.assertEqual(lookup.binding, RunDestinationBinding("Project", "Chat"))
        self.assertEqual(lookup.event_ids, (200,))
        self.assertIsNone(lookup.reason_code)

    def test_same_normalized_destination_bind_is_idempotent_without_duplicate_event(self) -> None:
        ledger = self._ledger_with_run()
        first = bind_run_destination("run-1", " Project ", " Chat ", ledger=ledger)

        second = bind_run_destination("run-1", "Project", "Chat", ledger=ledger)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(second.event_written)
        self.assertIsNone(second.event_id)
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(ledger.events[0]["event_type"], RUN_DESTINATION_BOUND_EVENT_TYPE)

    def test_different_destination_bind_fails_closed_and_leaves_original_intact(self) -> None:
        ledger = self._ledger_with_run()
        bind_run_destination("run-1", "Project", "Chat", ledger=ledger)

        result = bind_run_destination("run-1", "Project", "Different Chat", ledger=ledger)

        self.assertFalse(result.ok)
        self.assertEqual(
            result.reason_code,
            "destination_already_bound_to_different_destination",
        )
        self.assertEqual(result.binding, RunDestinationBinding("Project", "Chat"))
        self.assertEqual(len(ledger.events), 1)
        lookup = get_run_destination_binding("run-1", ledger=ledger)
        self.assertEqual(lookup.status, DestinationBindingLookupStatus.PRESENT)
        self.assertEqual(lookup.binding, RunDestinationBinding("Project", "Chat"))

    def test_missing_binding_is_distinguished_from_invalid_history(self) -> None:
        missing_ledger = FakeCreateRunLedger()
        invalid_ledger = FakeCreateRunLedger()
        invalid_ledger.runs.append(
            {
                "id": "run-1",
                "status": RunStatus.CREATED.value,
                "user_instruction": "existing run",
            }
        )
        invalid_ledger.add_event(
            "run-1",
            RUN_DESTINATION_BOUND_EVENT_TYPE,
            RUN_DESTINATION_BOUND_MESSAGE,
            metadata={"schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION},
        )

        missing = get_run_destination_binding("run-1", ledger=missing_ledger)
        invalid = get_run_destination_binding("run-1", ledger=invalid_ledger)
        invalid_bind = bind_run_destination(
            "run-1",
            "Project",
            "Chat",
            ledger=invalid_ledger,
        )

        self.assertEqual(missing.status, DestinationBindingLookupStatus.MISSING)
        self.assertIsNone(missing.reason_code)
        self.assertEqual(invalid.status, DestinationBindingLookupStatus.INVALID)
        self.assertEqual(invalid.reason_code, "malformed_destination_binding_event")
        self.assertFalse(invalid_bind.ok)
        self.assertEqual(invalid_bind.reason_code, "destination_binding_invalid")
        self.assertEqual(len(invalid_ledger.events), 1)

    def test_contradictory_binding_events_fail_closed(self) -> None:
        ledger = self._ledger_with_run()
        ledger.add_event(
            "run-1",
            RUN_DESTINATION_BOUND_EVENT_TYPE,
            RUN_DESTINATION_BOUND_MESSAGE,
            metadata={
                "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                "project_title": "Project",
                "chat_title": "Chat",
            },
        )
        ledger.add_event(
            "run-1",
            RUN_DESTINATION_BOUND_EVENT_TYPE,
            RUN_DESTINATION_BOUND_MESSAGE,
            metadata={
                "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                "project_title": "Project",
                "chat_title": "Other Chat",
            },
        )

        lookup = get_run_destination_binding("run-1", ledger=ledger)
        result = bind_run_destination("run-1", "Project", "Chat", ledger=ledger)

        self.assertEqual(lookup.status, DestinationBindingLookupStatus.INVALID)
        self.assertEqual(lookup.reason_code, "contradictory_destination_binding_events")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_binding_invalid")
        self.assertEqual(len(ledger.events), 2)

    def test_operational_binding_failure_returns_structured_failure(self) -> None:
        class OperationalFailureLedger:
            def list_events(self, run_id: str) -> list[dict]:
                raise AssertionError("history lookup should not be used by service")

            def bind_run_destination(
                self,
                run_id: str,
                project_title: str,
                chat_title: str,
            ) -> ledger_module.AtomicDestinationBindingResult:
                return ledger_module.AtomicDestinationBindingResult(
                    status=ledger_module.AtomicDestinationBindingStatus.OPERATIONAL_FAILURE,
                    run_id=run_id,
                    reason_code="destination_binding_transaction_failed",
                    error_message="Failed to bind run destination: database is locked",
                )

        result = bind_run_destination(
            "run-1",
            "Project",
            "Chat",
            ledger=OperationalFailureLedger(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_binding_transaction_failed")
        self.assertEqual(
            result.error_message,
            "Failed to bind run destination: database is locked",
        )
        self.assertFalse(result.event_written)


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
