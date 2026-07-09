from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent import ledger
from agent.run_services import (
    DestinationBindingLookupStatus,
    RUN_DESTINATION_BOUND_EVENT_TYPE,
    RunDestinationBinding,
    bind_run_destination,
    get_run_destination_binding,
)


class LedgerDestinationBindingConcurrencyTests(unittest.TestCase):
    def test_different_destination_race_writes_one_binding_and_one_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            with mock.patch.object(ledger, "DB_PATH", db_path):
                run_id = ledger.create_run("existing run")
                barrier = threading.Barrier(2)

                def attempt(project_title: str, chat_title: str):
                    barrier.wait(timeout=5)
                    return bind_run_destination(
                        run_id,
                        project_title,
                        chat_title,
                        ledger=ledger,
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(attempt, "Project", "Chat A"),
                        executor.submit(attempt, "Project", "Chat B"),
                    ]
                    results = [future.result(timeout=10) for future in futures]

                successes = [result for result in results if result.ok]
                conflicts = [
                    result
                    for result in results
                    if result.reason_code
                    == "destination_already_bound_to_different_destination"
                ]
                self.assertEqual(len(successes), 1)
                self.assertTrue(successes[0].event_written)
                self.assertEqual(len(conflicts), 1)
                self.assertFalse(conflicts[0].event_written)

                events = _binding_events(run_id)
                self.assertEqual(len(events), 1)
                self.assertEqual(successes[0].event_id, events[0]["id"])
                metadata = json.loads(events[0]["metadata_json"])
                self.assertEqual(
                    metadata,
                    {
                        "schema_version": ledger.RUN_DESTINATION_BOUND_SCHEMA_VERSION,
                        "project_title": successes[0].binding.project_title,
                        "chat_title": successes[0].binding.chat_title,
                    },
                )
                self.assertEqual(conflicts[0].binding, successes[0].binding)
                final_lookup = get_run_destination_binding(run_id, ledger=ledger)
                self.assertEqual(
                    final_lookup.status,
                    DestinationBindingLookupStatus.PRESENT,
                )
                self.assertEqual(final_lookup.binding, successes[0].binding)

    def test_same_destination_race_writes_one_binding_and_one_idempotent_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            with mock.patch.object(ledger, "DB_PATH", db_path):
                run_id = ledger.create_run("existing run")
                barrier = threading.Barrier(2)

                def attempt():
                    barrier.wait(timeout=5)
                    return bind_run_destination(
                        run_id,
                        "Project",
                        "Chat",
                        ledger=ledger,
                    )

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(attempt), executor.submit(attempt)]
                    results = [future.result(timeout=10) for future in futures]

                self.assertTrue(all(result.ok for result in results))
                self.assertEqual(sum(result.event_written for result in results), 1)
                self.assertEqual(
                    [result.binding for result in results],
                    [
                        RunDestinationBinding("Project", "Chat"),
                        RunDestinationBinding("Project", "Chat"),
                    ],
                )

                events = _binding_events(run_id)
                self.assertEqual(len(events), 1)
                metadata = json.loads(events[0]["metadata_json"])
                self.assertEqual(metadata["project_title"], "Project")
                self.assertEqual(metadata["chat_title"], "Chat")
                final_lookup = get_run_destination_binding(run_id, ledger=ledger)
                self.assertEqual(
                    final_lookup.status,
                    DestinationBindingLookupStatus.PRESENT,
                )
                self.assertEqual(
                    final_lookup.binding,
                    RunDestinationBinding("Project", "Chat"),
                )

    def test_commit_failure_rolls_back_inserted_binding_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            with mock.patch.object(ledger, "DB_PATH", db_path):
                run_id = ledger.create_run("existing run")

                def connect_with_commit_failure():
                    connection = sqlite3.connect(db_path)
                    connection.row_factory = sqlite3.Row
                    return _SecondCommitFailsConnection(connection)

                with mock.patch.object(ledger, "_connect", connect_with_commit_failure):
                    result = bind_run_destination(
                        run_id,
                        "Project",
                        "Chat",
                        ledger=ledger,
                    )

                self.assertFalse(result.ok)
                self.assertEqual(
                    result.reason_code,
                    "destination_binding_transaction_failed",
                )
                self.assertEqual(_binding_events(run_id), [])


class _SecondCommitFailsConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commit_count = 0

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def commit(self) -> None:
        self._commit_count += 1
        if self._commit_count == 2:
            raise sqlite3.OperationalError("commit failed")
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def _binding_events(run_id: str) -> list[dict]:
    return [
        event
        for event in ledger.list_events(run_id)
        if event["event_type"] == RUN_DESTINATION_BOUND_EVENT_TYPE
    ]


if __name__ == "__main__":
    unittest.main()
