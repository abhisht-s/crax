from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from agent.codex_quota_wait import (
    CODEX_QUOTA_RESUME_DELAY_SECONDS,
    CODEX_QUOTA_RESUME_STARTED_EVENT_TYPE,
    CODEX_QUOTA_WAIT_CANCELLED_EVENT_TYPE,
    CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
    CODEX_QUOTA_WAIT_STALE_EVENT_TYPE,
    active_quota_wait,
    decide_quota_wait,
    quota_wait_client_message,
    parse_try_again_at,
)
from agent.ledger import CODEX_PROGRESS_EVENT_TYPE


USAGE_LIMIT_ERROR = (
    "You've hit your usage limit. Upgrade to Pro, visit "
    "chatgpt.com/codex/settings/usage to purchase more credits or try again at 7:52 AM."
)
TURN_FAILED_DICT_REPR_ERROR = str({"message": USAGE_LIMIT_ERROR})
SESSION_ID = "01a05837-1cb2-76b0-852f-6a104eb1f07c"
INVOCATION_ID = "codex-invocation-quota"


def _progress_event(
    *,
    kind: str,
    invocation_id: str = INVOCATION_ID,
    error: object | None = None,
    session_id: str | None = None,
    event_id: int = 1,
) -> dict:
    value_summary: dict[str, object] = {"event_type": "error" if kind == "error" else kind}
    if error is not None:
        value_summary["error"] = error
    if session_id is not None:
        value_summary["codex_session_id"] = session_id
    metadata = {
        "schema_version": 1,
        "codex_invocation_id": invocation_id,
        "kind": kind,
        "status": "failed" if kind == "error" else "observed",
        "title": "Codex error" if kind == "error" else "Codex JSON event",
        "summary": f"Codex emitted {kind}.",
        "metadata": {"value_summary": value_summary},
    }
    return {
        "id": event_id,
        "event_type": CODEX_PROGRESS_EVENT_TYPE,
        "metadata": metadata,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


class CodexQuotaWaitDecisionTests(unittest.TestCase):
    def test_generic_nonzero_exit_is_not_scheduled(self) -> None:
        events = [
            _progress_event(kind="error", error="failed", event_id=1),
            {
                "id": 2,
                "event_type": "codex_exec_finished",
                "metadata_json": json.dumps({"exit_code": 1, "found": True}),
            },
        ]
        decision = decide_quota_wait(events, now=datetime(2026, 8, 30, 1, 9, tzinfo=timezone.utc))
        self.assertFalse(decision.scheduled)
        self.assertEqual(decision.reason_code, "not_usage_limit")

    def test_usage_limit_without_reset_time_is_not_scheduled(self) -> None:
        events = [
            _progress_event(kind="thread.started", session_id=SESSION_ID, event_id=1),
            _progress_event(
                kind="error",
                error="You've hit your usage limit.",
                event_id=2,
            ),
        ]
        decision = decide_quota_wait(events, now=datetime(2026, 8, 30, 1, 9, tzinfo=timezone.utc))
        self.assertFalse(decision.scheduled)
        self.assertEqual(decision.reason_code, "usage_limit_without_reset_time")
        self.assertEqual(decision.thread_id, SESSION_ID)

    def test_usage_limit_without_thread_id_is_not_scheduled(self) -> None:
        events = [_progress_event(kind="error", error=USAGE_LIMIT_ERROR, event_id=1)]
        decision = decide_quota_wait(events, now=datetime(2026, 8, 30, 1, 9, tzinfo=timezone.utc))
        self.assertFalse(decision.scheduled)
        self.assertEqual(decision.reason_code, "usage_limit_without_thread_id")

    def test_past_try_again_clock_is_not_scheduled(self) -> None:
        local_now = datetime(2026, 8, 30, 8, 0, tzinfo=datetime.now().astimezone().tzinfo)
        events = [
            _progress_event(kind="error", error=USAGE_LIMIT_ERROR, session_id=SESSION_ID, event_id=1),
        ]
        # Force parse against a now after 7:52 AM local by using a now whose local time is 8:00.
        decision = decide_quota_wait(events, now=local_now)
        self.assertFalse(decision.scheduled)
        self.assertEqual(decision.reason_code, "usage_limit_without_reset_time")

    def test_complete_signal_schedules_resume_one_minute_after_reset(self) -> None:
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 8, 30, 6, 39, tzinfo=tz)
        events = [
            _progress_event(
                kind="codex_json_event",
                session_id=SESSION_ID,
                event_id=1,
            ),
            _progress_event(kind="error", error=USAGE_LIMIT_ERROR, event_id=2),
        ]
        decision = decide_quota_wait(events, now=now)
        self.assertTrue(decision.scheduled)
        self.assertEqual(decision.reason_code, "quota_wait_scheduled")
        self.assertEqual(decision.thread_id, SESSION_ID)
        self.assertEqual(decision.source, "error_text")
        resets_at = parse_try_again_at(USAGE_LIMIT_ERROR, now=now)
        self.assertIsNotNone(resets_at)
        self.assertEqual(decision.resets_at, resets_at.isoformat())
        self.assertEqual(
            decision.resume_at,
            (resets_at + timedelta(seconds=CODEX_QUOTA_RESUME_DELAY_SECONDS)).isoformat(),
        )

    def test_rate_limits_rpc_future_time_is_preferred_over_error_text(self) -> None:
        now = datetime(2026, 8, 30, 6, 39, tzinfo=timezone.utc)
        rpc_reset = now + timedelta(hours=5)
        events = [
            _progress_event(kind="codex_json_event", session_id=SESSION_ID, event_id=1),
            _progress_event(kind="error", error=USAGE_LIMIT_ERROR, event_id=2),
        ]
        decision = decide_quota_wait(events, now=now, rate_limits_resets_at=rpc_reset)
        self.assertTrue(decision.scheduled)
        self.assertEqual(decision.source, "rate_limits_rpc")
        self.assertEqual(decision.resets_at, rpc_reset.isoformat())
        self.assertEqual(
            decision.resume_at,
            (rpc_reset + timedelta(seconds=CODEX_QUOTA_RESUME_DELAY_SECONDS)).isoformat(),
        )

    def test_fourth_new_invocation_usage_limit_is_still_scheduled(self) -> None:
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 8, 30, 6, 39, tzinfo=tz)
        events: list[dict] = [
            _progress_event(kind="codex_json_event", session_id=SESSION_ID, event_id=1),
        ]
        event_id = 2
        for index in range(1, 4):
            invocation_id = f"codex-invocation-{index}"
            events.append(
                _progress_event(
                    kind="error",
                    error=USAGE_LIMIT_ERROR,
                    invocation_id=invocation_id,
                    event_id=event_id,
                )
            )
            event_id += 1
            events.append(
                {
                    "id": event_id,
                    "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
                    "metadata": {
                        "thread_id": SESSION_ID,
                        "resume_at": "2026-08-30T08:00:00+00:00",
                        "invocation_id": invocation_id,
                    },
                }
            )
            event_id += 1
        events.append(
            _progress_event(
                kind="error",
                error=USAGE_LIMIT_ERROR,
                invocation_id="codex-invocation-4",
                session_id=SESSION_ID,
                event_id=event_id,
            )
        )
        decision = decide_quota_wait(events, now=now)
        self.assertTrue(decision.scheduled)
        self.assertEqual(decision.invocation_id, "codex-invocation-4")

    def test_later_generic_error_does_not_reuse_older_usage_limit(self) -> None:
        events = [
            _progress_event(kind="error", error=USAGE_LIMIT_ERROR, session_id=SESSION_ID, event_id=1),
            _progress_event(kind="error", error="command failed", event_id=2),
        ]
        decision = decide_quota_wait(
            events,
            now=datetime(2026, 8, 30, 6, 39, tzinfo=timezone.utc),
        )
        self.assertFalse(decision.scheduled)
        self.assertEqual(decision.reason_code, "not_usage_limit")

    def test_turn_failed_dict_repr_error_still_schedules(self) -> None:
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 8, 31, 1, 39, tzinfo=tz)
        events = [
            _progress_event(kind="codex_json_event", session_id=SESSION_ID, event_id=1),
            _progress_event(kind="error", error=TURN_FAILED_DICT_REPR_ERROR, event_id=2),
        ]
        decision = decide_quota_wait(events, now=now)
        self.assertTrue(decision.scheduled)
        self.assertEqual(decision.thread_id, SESSION_ID)
        self.assertEqual(decision.source, "error_text")

    def test_nested_error_message_object_still_schedules(self) -> None:
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 8, 30, 6, 39, tzinfo=tz)
        events = [
            _progress_event(kind="codex_json_event", session_id=SESSION_ID, event_id=1),
            _progress_event(kind="error", error={"message": USAGE_LIMIT_ERROR}, event_id=2),
        ]
        decision = decide_quota_wait(events, now=now)
        self.assertTrue(decision.scheduled)
        self.assertEqual(decision.thread_id, SESSION_ID)

    def test_already_waited_invocation_is_not_scheduled_again(self) -> None:
        events = [
            _progress_event(kind="codex_json_event", session_id=SESSION_ID, event_id=1),
            _progress_event(kind="error", error=USAGE_LIMIT_ERROR, event_id=2),
            {
                "id": 3,
                "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
                "metadata": {
                    "thread_id": SESSION_ID,
                    "resume_at": "2026-08-30T08:00:00+00:00",
                    "invocation_id": INVOCATION_ID,
                },
            },
        ]
        decision = decide_quota_wait(
            events,
            now=datetime(2026, 8, 30, 6, 39, tzinfo=timezone.utc),
        )
        self.assertFalse(decision.scheduled)
        self.assertEqual(decision.reason_code, "not_usage_limit")

    def test_new_invocation_usage_limit_after_wait_is_scheduled(self) -> None:
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 8, 30, 6, 39, tzinfo=tz)
        second_invocation = "codex-invocation-resume-2"
        events = [
            _progress_event(
                kind="codex_json_event",
                session_id=SESSION_ID,
                invocation_id=INVOCATION_ID,
                event_id=1,
            ),
            _progress_event(
                kind="error",
                error=USAGE_LIMIT_ERROR,
                invocation_id=INVOCATION_ID,
                event_id=2,
            ),
            {
                "id": 3,
                "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
                "metadata": {
                    "thread_id": SESSION_ID,
                    "resume_at": "2026-08-30T08:00:00+00:00",
                    "invocation_id": INVOCATION_ID,
                },
            },
            {"id": 4, "event_type": CODEX_QUOTA_RESUME_STARTED_EVENT_TYPE, "metadata": {}},
            _progress_event(
                kind="codex_json_event",
                session_id=SESSION_ID,
                invocation_id=second_invocation,
                event_id=5,
            ),
            _progress_event(
                kind="error",
                error=USAGE_LIMIT_ERROR,
                invocation_id=second_invocation,
                event_id=6,
            ),
        ]
        decision = decide_quota_wait(events, now=now)
        self.assertTrue(decision.scheduled)
        self.assertEqual(decision.invocation_id, second_invocation)


class CodexQuotaWaitMessageTests(unittest.TestCase):
    def test_client_message_uses_remaining_duration_not_a_clock(self) -> None:
        now = datetime(2026, 8, 30, 6, 39, tzinfo=timezone.utc)
        resume_at = (now + timedelta(hours=2, minutes=4, seconds=1)).isoformat()
        self.assertEqual(
            quota_wait_client_message(resume_at, now=now),
            "Codex limits ran out. Reset in 02:05 hours",
        )

    def test_client_message_when_resume_is_due(self) -> None:
        now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            quota_wait_client_message(now.isoformat(), now=now),
            "Codex limits ran out. Resuming shortly.",
        )


class CodexQuotaWaitHelpersTests(unittest.TestCase):
    def test_active_wait_clears_after_stale(self) -> None:
        events = [
            {"id": 1, "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE, "metadata": {}},
            {"id": 2, "event_type": CODEX_QUOTA_WAIT_STALE_EVENT_TYPE, "metadata": {}},
        ]
        self.assertIsNone(active_quota_wait(events))

    def test_active_wait_clears_after_cancel(self) -> None:
        events = [
            {"id": 1, "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE, "metadata": {}},
            {"id": 2, "event_type": CODEX_QUOTA_WAIT_CANCELLED_EVENT_TYPE, "metadata": {}},
        ]
        self.assertIsNone(active_quota_wait(events))

    def test_latest_scheduled_wait_is_active(self) -> None:
        events = [
            {"id": 1, "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE, "metadata": {"n": 1}},
            {"id": 2, "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE, "metadata": {"n": 2}},
        ]
        active = active_quota_wait(events)
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], 2)

    def test_active_wait_clears_when_resume_starts(self) -> None:
        events = [
            {"id": 1, "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE, "metadata": {}},
            {"id": 2, "event_type": CODEX_QUOTA_RESUME_STARTED_EVENT_TYPE, "metadata": {}},
        ]
        self.assertIsNone(active_quota_wait(events))


if __name__ == "__main__":
    unittest.main()
