from __future__ import annotations

import concurrent.futures
import inspect
import io
import json
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from unittest import mock

from agent import cli, ledger
from agent.run_services import (
    CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
    CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE,
    CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
    CHATGPT_UI_LEASE_SCHEMA_VERSION,
    ChatGPTUILeaseLookupStatus,
    acquire_chatgpt_ui_lease,
    get_chatgpt_ui_lease,
    release_chatgpt_ui_lease,
)


def _lease_token_sha256(token: str) -> str:
    return ledger.chatgpt_ui_lease_token_fingerprint(token)


def _raw_token(label: str) -> str:
    return f"unit-{_lease_token_sha256(label)}"


class ChatGPTUILeaseTests(unittest.TestCase):
    def test_lookup_before_acquire_reports_none(self) -> None:
        with _temporary_ledger():
            self.assertEqual(
                get_chatgpt_ui_lease(ledger=ledger).status,
                ChatGPTUILeaseLookupStatus.MISSING,
            )

    def test_first_acquisition_succeeds(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")

            result = acquire_chatgpt_ui_lease(
                run_id,
                reason="submit",
                source="unit_test",
                ledger=ledger,
            )

            self.assertTrue(result.ok)
            self.assertTrue(result.event_written)
            self.assertEqual(result.event_type, CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE)
            self.assertIsInstance(result.lease_token, str)
            self.assertGreaterEqual(len(result.lease_token or ""), 32)
            self.assertEqual(result.owner.owning_run_id, run_id)
            self.assertNotIn("lease_token", result.metadata)
            self.assertEqual(
                result.metadata["lease_token_sha256"],
                _lease_token_sha256(result.lease_token),
            )
            self.assertNotIn("expires_at", result.metadata)
            self.assertNotIn(result.lease_token, json.dumps(result.metadata, sort_keys=True))

            lookup = get_chatgpt_ui_lease(ledger=ledger)
            self.assertEqual(lookup.status, ChatGPTUILeaseLookupStatus.ACTIVE)
            self.assertEqual(lookup.active_owner, result.owner)
            self.assertFalse(hasattr(lookup.active_owner, "lease_token"))

    def test_concurrent_distinct_acquisition_attempts_have_one_winner(self) -> None:
        with _temporary_ledger():
            run_a = ledger.create_run("run a")
            run_b = ledger.create_run("run b")
            barrier = threading.Barrier(2)

            def attempt(run_id: str):
                barrier.wait(timeout=5)
                return acquire_chatgpt_ui_lease(run_id, ledger=ledger)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(attempt, run_a),
                    executor.submit(attempt, run_b),
                ]
                results = [future.result(timeout=10) for future in futures]

            winners = [result for result in results if result.ok]
            denied = [
                result
                for result in results
                if result.reason_code == "chatgpt_ui_lease_already_held"
            ]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(denied), 1)
            self.assertTrue(winners[0].event_written)
            self.assertTrue(denied[0].event_written)
            self.assertEqual(denied[0].event_type, CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE)

            lifecycle_events = _lease_lifecycle_events()
            self.assertEqual(
                [event["event_type"] for event in lifecycle_events],
                [CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE],
            )
            denial_events = _events_by_type(CHATGPT_UI_LEASE_ACQUIRE_DENIED_EVENT_TYPE)
            self.assertEqual(len(denial_events), 1)

    def test_second_acquisition_while_active_fails_closed(self) -> None:
        with _temporary_ledger():
            owner_run_id = ledger.create_run("owner")
            second_run_id = ledger.create_run("second")
            acquired = acquire_chatgpt_ui_lease(owner_run_id, ledger=ledger)

            second = acquire_chatgpt_ui_lease(second_run_id, ledger=ledger)

            self.assertFalse(second.ok)
            self.assertEqual(second.reason_code, "chatgpt_ui_lease_already_held")
            self.assertEqual(second.active_owner, acquired.owner)
            self.assertEqual(get_chatgpt_ui_lease(ledger=ledger).active_owner, acquired.owner)
            self.assertEqual(len(_lease_lifecycle_events()), 1)

    def test_release_with_correct_token_succeeds(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = acquire_chatgpt_ui_lease(run_id, ledger=ledger)

            released = release_chatgpt_ui_lease(acquired.lease_token, ledger=ledger)

            self.assertTrue(released.ok)
            self.assertTrue(released.event_written)
            self.assertEqual(released.event_type, CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)
            self.assertEqual(released.owner, acquired.owner)
            self.assertNotIn("lease_token", released.metadata)
            self.assertEqual(
                released.metadata["lease_token_sha256"],
                _lease_token_sha256(acquired.lease_token),
            )
            self.assertNotIn(acquired.lease_token, json.dumps(released.metadata, sort_keys=True))
            self.assertEqual(
                get_chatgpt_ui_lease(ledger=ledger).status,
                ChatGPTUILeaseLookupStatus.MISSING,
            )

            repeated = release_chatgpt_ui_lease(acquired.lease_token, ledger=ledger)
            self.assertTrue(repeated.ok)
            self.assertFalse(repeated.event_written)
            self.assertEqual(len(_events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)), 1)

    def test_release_with_wrong_token_fails_and_leaves_active(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = acquire_chatgpt_ui_lease(run_id, ledger=ledger)

            released = release_chatgpt_ui_lease(_raw_token("wrong"), ledger=ledger)

            self.assertFalse(released.ok)
            self.assertEqual(released.reason_code, "chatgpt_ui_lease_token_mismatch")
            lookup = get_chatgpt_ui_lease(ledger=ledger)
            self.assertEqual(lookup.status, ChatGPTUILeaseLookupStatus.ACTIVE)
            self.assertEqual(lookup.active_owner, acquired.owner)
            self.assertEqual(len(_events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)), 0)

    def test_manual_stale_release_requires_confirmation_without_writing(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = ledger.acquire_chatgpt_ui_lease(run_id)
            active_event = _lease_lifecycle_events()[0]

            released = ledger.manual_release_stale_chatgpt_ui_lease(
                owning_run_id=run_id,
                owner_pid=acquired.owner_pid or -1,
                acquired_at=acquired.acquired_at or "",
                active_event_id=active_event["id"],
                reason="operator verified stale",
            )

            self.assertEqual(
                released.status,
                ledger.AtomicChatGPTUILeaseStatus.CONFIRMATION_REQUIRED,
            )
            self.assertFalse(released.event_written)
            self.assertEqual(len(_events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)), 0)

    def test_manual_stale_release_appends_matching_release_without_raw_token(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            ledger.update_run_status(run_id, ledger.RunStatus.COMPLETED)
            acquired = ledger.acquire_chatgpt_ui_lease(run_id)
            active_event = _lease_lifecycle_events()[0]
            token_sha = _metadata(active_event)["lease_token_sha256"]

            released = ledger.manual_release_stale_chatgpt_ui_lease(
                owning_run_id=run_id,
                owner_pid=acquired.owner_pid or -1,
                acquired_at=acquired.acquired_at or "",
                active_event_id=active_event["id"],
                expected_run_status=ledger.RunStatus.COMPLETED.value,
                expected_lease_token_sha256=token_sha,
                reason="operator verified stale",
                confirm_stale=True,
            )

            self.assertEqual(released.status, ledger.AtomicChatGPTUILeaseStatus.RELEASED)
            self.assertTrue(released.event_written)
            self.assertEqual(released.active_event_id, active_event["id"])
            self.assertEqual(
                get_chatgpt_ui_lease(ledger=ledger).status,
                ChatGPTUILeaseLookupStatus.MISSING,
            )
            release_events = _events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)
            self.assertEqual(len(release_events), 1)
            release_metadata = _metadata(release_events[0])
            self.assertEqual(release_metadata["lease_token_sha256"], token_sha)
            self.assertEqual(release_metadata["active_acquire_event_id"], active_event["id"])
            self.assertTrue(release_metadata["manual_release"])
            self.assertTrue(release_metadata["stale_confirmed"])
            self.assertEqual(release_metadata["verified_run_status"], "completed")
            self.assertNotIn("lease_token", release_metadata)

    def test_manual_stale_release_rejects_mismatched_active_owner(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = ledger.acquire_chatgpt_ui_lease(run_id)
            active_event = _lease_lifecycle_events()[0]

            released = ledger.manual_release_stale_chatgpt_ui_lease(
                owning_run_id=run_id,
                owner_pid=(acquired.owner_pid or 0) + 1,
                acquired_at=acquired.acquired_at or "",
                active_event_id=active_event["id"],
                reason="operator verified stale",
                confirm_stale=True,
            )

            self.assertEqual(
                released.status,
                ledger.AtomicChatGPTUILeaseStatus.ACTIVE_LEASE_MISMATCH,
            )
            self.assertFalse(released.event_written)
            self.assertEqual(
                get_chatgpt_ui_lease(ledger=ledger).status,
                ChatGPTUILeaseLookupStatus.ACTIVE,
            )
            self.assertEqual(len(_events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)), 0)

    def test_manual_stale_release_rejects_unexpected_run_status(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = ledger.acquire_chatgpt_ui_lease(run_id)
            active_event = _lease_lifecycle_events()[0]

            released = ledger.manual_release_stale_chatgpt_ui_lease(
                owning_run_id=run_id,
                owner_pid=acquired.owner_pid or -1,
                acquired_at=acquired.acquired_at or "",
                active_event_id=active_event["id"],
                expected_run_status=ledger.RunStatus.COMPLETED.value,
                reason="operator verified stale",
                confirm_stale=True,
            )

            self.assertEqual(
                released.reason_code,
                "manual_stale_lease_run_status_mismatch",
            )
            self.assertFalse(released.event_written)
            self.assertEqual(len(_events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)), 0)

    def test_cli_manual_stale_release_pid_guard_exits_before_writing(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = ledger.acquire_chatgpt_ui_lease(run_id)
            active_event = _lease_lifecycle_events()[0]

            argv = [
                "agent-loop",
                "release-stale-chatgpt-ui-lease",
                "--owning-run-id",
                run_id,
                "--owner-pid",
                str(acquired.owner_pid),
                "--acquired-at",
                acquired.acquired_at or "",
                "--active-event-id",
                str(active_event["id"]),
                "--reason",
                "operator verified stale",
                "--confirm-stale",
            ]
            stderr = io.StringIO()
            with (
                mock.patch.object(cli.sys, "argv", argv),
                mock.patch.object(cli, "_pid_exists", return_value=True),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main()

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(len(_events_by_type(CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE)), 0)

    def test_persisted_lease_events_use_fingerprint_only(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            acquired = acquire_chatgpt_ui_lease(run_id, ledger=ledger)
            release_chatgpt_ui_lease(acquired.lease_token, ledger=ledger)

            events = _lease_lifecycle_events()
            self.assertEqual(len(events), 2)
            for event in events:
                metadata = _metadata(event)
                self.assertNotIn("lease_token", metadata)
                self.assertNotIn(acquired.lease_token, json.dumps(metadata, sort_keys=True))
                self.assertEqual(
                    metadata["lease_token_sha256"],
                    _lease_token_sha256(acquired.lease_token),
                )

    def test_malformed_lease_history_blocks_acquisition(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            ledger.add_event(
                run_id,
                CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
                "bad acquire",
                metadata={"schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION},
            )

            lookup = get_chatgpt_ui_lease(ledger=ledger)
            acquired = acquire_chatgpt_ui_lease(run_id, ledger=ledger)

            self.assertEqual(lookup.status, ChatGPTUILeaseLookupStatus.INVALID)
            self.assertFalse(acquired.ok)
            self.assertEqual(
                acquired.reason_code,
                "malformed_chatgpt_ui_lease_acquire_event",
            )

    def test_contradictory_lease_history_blocks_acquisition(self) -> None:
        with _temporary_ledger():
            run_a = ledger.create_run("run a")
            run_b = ledger.create_run("run b")
            _write_acquire_event(run_a, token="token-a")
            _write_acquire_event(run_b, token="token-b")

            lookup = get_chatgpt_ui_lease(ledger=ledger)
            acquired = acquire_chatgpt_ui_lease(run_a, ledger=ledger)

            self.assertEqual(lookup.status, ChatGPTUILeaseLookupStatus.INVALID)
            self.assertFalse(acquired.ok)
            self.assertEqual(
                acquired.reason_code,
                "contradictory_active_chatgpt_ui_lease_events",
            )

    def test_lease_does_not_auto_expire_because_time_passes(self) -> None:
        with _temporary_ledger():
            owner_run_id = ledger.create_run("owner")
            second_run_id = ledger.create_run("second")
            _write_acquire_event(owner_run_id, token="old-token", acquired_at="2000-01-01T00:00:00+00:00")

            lookup = get_chatgpt_ui_lease(ledger=ledger)
            second = acquire_chatgpt_ui_lease(second_run_id, ledger=ledger)

            self.assertEqual(lookup.status, ChatGPTUILeaseLookupStatus.ACTIVE)
            self.assertEqual(lookup.active_owner.owning_run_id, owner_run_id)
            self.assertFalse(second.ok)
            self.assertEqual(second.reason_code, "chatgpt_ui_lease_already_held")

    def test_release_when_no_active_lease_is_deterministic(self) -> None:
        with _temporary_ledger():
            released = release_chatgpt_ui_lease("never-issued", ledger=ledger)

            self.assertFalse(released.ok)
            self.assertEqual(released.reason_code, "chatgpt_ui_lease_not_active")

    def test_historical_raw_token_events_can_be_redacted_idempotently(self) -> None:
        with _temporary_ledger():
            run_id = ledger.create_run("run")
            unrelated_run = ledger.create_run("unrelated")
            raw = _raw_token("historical")
            _write_acquire_event(run_id, token=raw, raw=True)
            ledger.add_event(
                unrelated_run,
                "unrelated_event",
                "unrelated",
                metadata={"lease_token": raw, "keep": "same"},
            )

            first = ledger.redact_chatgpt_ui_lease_tokens()
            second = ledger.redact_chatgpt_ui_lease_tokens()

            self.assertTrue(first.ok)
            self.assertEqual(first.events_redacted, 1)
            self.assertEqual(first.already_fingerprint_only, 0)
            self.assertTrue(second.ok)
            self.assertEqual(second.events_redacted, 0)
            self.assertEqual(second.already_fingerprint_only, 1)
            lease_metadata = _metadata(_lease_lifecycle_events()[0])
            self.assertNotIn("lease_token", lease_metadata)
            self.assertEqual(lease_metadata["lease_token_sha256"], _lease_token_sha256(raw))
            unrelated = _events_by_type("unrelated_event")[0]
            self.assertEqual(_metadata(unrelated), {"lease_token": raw, "keep": "same"})
            redaction_events = _events_by_type(ledger.CHATGPT_UI_LEASE_REDACTION_EVENT_TYPE)
            self.assertEqual(len(redaction_events), 2)
            for event in redaction_events:
                self.assertNotIn(raw, event["metadata_json"])

    def test_no_operational_timeout_or_deadline_api_is_introduced(self) -> None:
        functions = (
            acquire_chatgpt_ui_lease,
            release_chatgpt_ui_lease,
            get_chatgpt_ui_lease,
            ledger.acquire_chatgpt_ui_lease,
            ledger.release_chatgpt_ui_lease,
            ledger.list_chatgpt_ui_lease_events,
        )
        forbidden = ("timeout", "deadline", "elapsed", "expires")
        for function in functions:
            names = inspect.signature(function).parameters
            self.assertFalse(
                any(fragment in name for name in names for fragment in forbidden),
                function.__name__,
            )

    def test_service_api_accepts_fake_lease_ledger(self) -> None:
        fake = _FakeLeaseLedger()

        acquired = acquire_chatgpt_ui_lease(
            "run-fake",
            reason="test",
            source="fake",
            ledger=fake,
        )
        lookup = get_chatgpt_ui_lease(ledger=fake)
        fake_token = _raw_token("fake")
        released = release_chatgpt_ui_lease(fake_token, ledger=fake)

        self.assertTrue(acquired.ok)
        self.assertEqual(acquired.lease_token, fake_token)
        self.assertEqual(lookup.status, ChatGPTUILeaseLookupStatus.ACTIVE)
        self.assertTrue(released.ok)
        self.assertEqual(fake.acquire_calls, [{"run_id": "run-fake", "reason": "test", "source": "fake"}])
        self.assertEqual(fake.release_calls, [{"lease_token": fake_token, "reason": None, "source": None}])


def _temporary_ledger():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "ledger.db"
    patcher = mock.patch.object(ledger, "DB_PATH", db_path)

    class _Context:
        def __enter__(self):
            patcher.__enter__()
            return db_path

        def __exit__(self, exc_type, exc, tb):
            patcher.__exit__(exc_type, exc, tb)
            tmpdir.cleanup()

    return _Context()


class _FakeLeaseLedger:
    def __init__(self) -> None:
        self.acquire_calls: list[dict] = []
        self.release_calls: list[dict] = []

    def acquire_chatgpt_ui_lease(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ):
        self.acquire_calls.append(
            {"run_id": run_id, "reason": reason, "source": source}
        )
        return ledger.AtomicChatGPTUILeaseResult(
            status=ledger.AtomicChatGPTUILeaseStatus.ACQUIRED,
            run_id=run_id,
            lease_token=_raw_token("fake"),
            owner_pid=123,
            owning_run_id=run_id,
            acquired_at="2026-01-01T00:00:00+00:00",
            event_id=1,
            event_written=True,
        )

    def release_chatgpt_ui_lease(
        self,
        lease_token: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ):
        self.release_calls.append(
            {"lease_token": lease_token, "reason": reason, "source": source}
        )
        return ledger.AtomicChatGPTUILeaseResult(
            status=ledger.AtomicChatGPTUILeaseStatus.RELEASED,
            lease_token=lease_token,
            owner_pid=123,
            owning_run_id="run-fake",
            acquired_at="2026-01-01T00:00:00+00:00",
            released_at="2026-01-01T00:01:00+00:00",
            event_id=2,
            event_written=True,
        )

    def list_chatgpt_ui_lease_events(self) -> list[dict]:
        return [
            {
                "id": 1,
                "run_id": "run-fake",
                "event_type": CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
                "metadata": {
                    "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
                    "lease_token_sha256": _lease_token_sha256(_raw_token("fake")),
                    "owner_pid": 123,
                    "owning_run_id": "run-fake",
                    "acquired_at": "2026-01-01T00:00:00+00:00",
                },
            }
        ]


def _write_acquire_event(
    run_id: str,
    *,
    token: str,
    acquired_at: str = "2026-01-01T00:00:00+00:00",
    raw: bool = False,
) -> None:
    token_metadata = (
        {"lease_token": token}
        if raw
        else {"lease_token_sha256": _lease_token_sha256(token)}
    )
    ledger.add_event(
        run_id,
        CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
        "manual acquire",
        metadata={
            "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
            **token_metadata,
            "owner_pid": 12345,
            "owning_run_id": run_id,
            "acquired_at": acquired_at,
        },
    )


def _lease_lifecycle_events() -> list[dict]:
    return [
        event
        for event in ledger.list_chatgpt_ui_lease_events()
        if event["event_type"]
        in {
            CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
            CHATGPT_UI_LEASE_RELEASED_EVENT_TYPE,
        }
    ]


def _events_by_type(event_type: str) -> list[dict]:
    events = []
    for run_id in _all_run_ids():
        events.extend(
            event
            for event in ledger.list_events(run_id)
            if event["event_type"] == event_type
        )
    return events


def _all_run_ids() -> list[str]:
    run_ids = set()
    for event in ledger.list_chatgpt_ui_lease_events():
        run_ids.add(event["run_id"])
    for event in _raw_events():
        run_ids.add(event["run_id"])
    return sorted(run_ids)


def _raw_events() -> list[dict]:
    with closing(ledger._connect()) as connection:
        rows = connection.execute("SELECT run_id FROM events").fetchall()
    return [dict(row) for row in rows]


def _metadata(event: dict) -> dict:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return json.loads(event["metadata_json"])


if __name__ == "__main__":
    unittest.main()
