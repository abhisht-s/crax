from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from agent.codex_quota_resume_services import execute_codex_quota_resume_service
from agent.codex_quota_wait import (
    CODEX_QUOTA_RESUME_PROMPT,
    CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
)


class FakeResumeLedger:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = list(events or [])
        self.run = {"id": "run-1", "status": "running"}
        self._next_id = max((int(event.get("id") or 0) for event in self.events), default=0) + 1

    def list_events(self, run_id: str) -> list[dict]:
        del run_id
        return self.events

    def get_run(self, run_id: str) -> dict:
        del run_id
        return self.run

    def add_event(self, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> dict:
        event = {
            "id": self._next_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        }
        self._next_id += 1
        self.events.append(event)
        return event


class RecordingDirect:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, run_id, prompt, repo_path, sandbox, timeout_seconds, prompt_contract, **kwargs):
        self.calls.append(
            {
                "run_id": run_id,
                "prompt": prompt,
                "repo_path": repo_path,
                "sandbox": sandbox,
                "timeout_seconds": timeout_seconds,
                "prompt_contract": prompt_contract,
                **kwargs,
            }
        )
        return self.result


class RecordingGovernance:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(next_status="completed", ok=True)


def _wait_event(*, resume_at: str, thread_id: str = "thread-1") -> dict:
    return {
        "id": 1,
        "event_type": CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE,
        "metadata": {
            "thread_id": thread_id,
            "resume_at": resume_at,
            "resets_at": resume_at,
            "repository_path": "/tmp/repo",
            "sandbox": "read-only",
        },
    }


class CodexQuotaResumeServiceTests(unittest.TestCase):
    def test_resume_uses_continue_prompt_thread_id_and_existing_governance(self) -> None:
        now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
        resume_at = (now - timedelta(minutes=1)).isoformat()
        ledger = FakeResumeLedger([_wait_event(resume_at=resume_at)])
        raw = RecordingDirect(
            SimpleNamespace(
                ok=True,
                reason_code="codex_exec_completed",
                error_message=None,
                exit_code=0,
                raw_process_result={"exit_code": 0},
            )
        )
        governance = RecordingGovernance()

        result = execute_codex_quota_resume_service(
            "run-1",
            ledger=ledger,
            now=now,
            raw_execution_service=raw,
            governance_service=governance,
            git_snapshot_function=lambda path: {"repo_path": path},
            invocation_state_function=lambda path: {"repo_path": path},
        )

        self.assertTrue(result.ok)
        self.assertEqual(raw.calls[0]["prompt"], CODEX_QUOTA_RESUME_PROMPT)
        self.assertEqual(raw.calls[0]["resume_session_id"], "thread-1")
        self.assertEqual(raw.calls[0]["sandbox"], "read-only")
        self.assertEqual(len(governance.calls), 1)
        self.assertEqual(governance.calls[0][0][2], CODEX_QUOTA_RESUME_PROMPT)
        self.assertEqual(
            [event["event_type"] for event in ledger.events if event["id"] > 1],
            ["codex_quota_resume_started", "codex_quota_resume_finished"],
        )

    def test_resume_before_due_does_not_call_codex(self) -> None:
        now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
        resume_at = (now + timedelta(hours=1)).isoformat()
        raw = RecordingDirect(SimpleNamespace(ok=True))
        result = execute_codex_quota_resume_service(
            "run-1",
            ledger=FakeResumeLedger([_wait_event(resume_at=resume_at)]),
            now=now,
            raw_execution_service=raw,
            governance_service=RecordingGovernance(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "quota_resume_not_due")
        self.assertEqual(raw.calls, [])

    def test_force_resume_before_due_calls_codex_with_continue_prompt(self) -> None:
        now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
        resume_at = (now + timedelta(hours=1)).isoformat()
        ledger = FakeResumeLedger([_wait_event(resume_at=resume_at)])
        raw = RecordingDirect(
            SimpleNamespace(
                ok=True,
                reason_code="codex_exec_completed",
                error_message=None,
                exit_code=0,
                raw_process_result={"exit_code": 0},
            )
        )
        governance = RecordingGovernance()

        result = execute_codex_quota_resume_service(
            "run-1",
            ledger=ledger,
            now=now,
            allow_before_due=True,
            raw_execution_service=raw,
            governance_service=governance,
            git_snapshot_function=lambda path: {"repo_path": path},
            invocation_state_function=lambda path: {"repo_path": path},
        )

        self.assertTrue(result.ok)
        self.assertEqual(raw.calls[0]["prompt"], CODEX_QUOTA_RESUME_PROMPT)
        self.assertEqual(raw.calls[0]["resume_session_id"], "thread-1")
        started = next(
            event for event in ledger.events if event["event_type"] == "codex_quota_resume_started"
        )
        self.assertTrue(started["metadata"]["forced"])
        self.assertEqual(started["metadata"]["prompt"], CODEX_QUOTA_RESUME_PROMPT)

    def test_missing_wait_does_not_start_a_fresh_exec(self) -> None:
        raw = RecordingDirect(SimpleNamespace(ok=True))
        result = execute_codex_quota_resume_service(
            "run-1",
            ledger=FakeResumeLedger([]),
            now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
            raw_execution_service=raw,
            governance_service=RecordingGovernance(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "quota_wait_not_active")
        self.assertEqual(raw.calls, [])
