from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import chatgpt_ax_capture
from agent import codex_terminal
from agent import ledger as ledger_module
from agent.chatgpt_ax_capture import BEGIN_SENTINEL as AX_BEGIN_SENTINEL
from agent.chatgpt_ax_capture import END_SENTINEL as AX_END_SENTINEL
from agent.codex_invocation import (
    artifact_paths_for,
    build_intent_payload,
    spawn_invocation_wrapper,
    write_intent,
)
from agent.codex_invocation_wrapper import run_wrapper
from agent.codex_services import execute_codex_direct_service
from agent.codex_terminal import (
    run_codex_exec,
    terminate_all_active_codex_invocations,
)
from agent.ledger import (
    AtomicExecutionProfileStatus,
    CODEX_EXEC_STARTED_EVENT_TYPE,
    _execution_profile_from_metadata_json,
    bind_run_execution_profile,
)
from agent.prompt_extraction import (
    BEGIN_SENTINEL,
    END_SENTINEL,
    extract_next_codex_prompt_from_text,
)
from agent.run_services import (
    CODEX_DEFAULT_SELECTION,
    DEFAULT_CODING_AGENT,
    ExecutionProfileLookupStatus,
    RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
    RUN_EXECUTION_PROFILE_SCHEMA_VERSION_V2,
    RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
    RunExecutionProfile,
    _execution_profile_from_metadata,
    get_run_execution_profile,
    select_run_execution_profile,
)


EXPECTED_V1_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "sandbox",
        "model",
        "reasoning_effort",
        "approval_policy",
        "profile_source",
    }
)
EXPECTED_V2_PROFILE_KEYS = EXPECTED_V1_PROFILE_KEYS | {"coding_agent"}

VALID_V1_METADATA = {
    "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
    "sandbox": "workspace-write",
    "model": CODEX_DEFAULT_SELECTION,
    "reasoning_effort": CODEX_DEFAULT_SELECTION,
    "approval_policy": CODEX_DEFAULT_SELECTION,
    "profile_source": "explicit_user_selection",
}

VALID_PROFILE = RunExecutionProfile(
    sandbox="workspace-write",
    model=CODEX_DEFAULT_SELECTION,
    reasoning_effort=CODEX_DEFAULT_SELECTION,
    approval_policy=CODEX_DEFAULT_SELECTION,
    profile_source="explicit_user_selection",
)


def _valid_v2_metadata(*, coding_agent: str = "claude_code") -> dict:
    return {
        "schema_version": RUN_EXECUTION_PROFILE_SCHEMA_VERSION_V2,
        "sandbox": "workspace-write",
        "model": CODEX_DEFAULT_SELECTION,
        "reasoning_effort": CODEX_DEFAULT_SELECTION,
        "approval_policy": CODEX_DEFAULT_SELECTION,
        "profile_source": "explicit_user_selection",
        "coding_agent": coding_agent,
    }

FAKE_CODEX_WITHOUT_FINAL_MESSAGE = """#!/usr/bin/env python3
import json
import sys

sys.stdout.write(
    json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "stdout pretending to be final",
            },
        }
    )
    + "\\n"
)
sys.stdout.write(
    json.dumps(
        {
            "type": "result",
            "result": "stdout pretending to be final",
            "subtype": "success",
        }
    )
    + "\\n"
)
sys.exit(0)
"""


def _temporary_real_ledger():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "ledger.db"
    patcher = mock.patch.object(ledger_module, "DB_PATH", db_path)

    class _Context:
        def __enter__(self):
            patcher.__enter__()
            return db_path

        def __exit__(self, exc_type, exc, tb):
            patcher.__exit__(exc_type, exc, tb)
            tmpdir.cleanup()

    return _Context()


def _raw_runner_result(
    *,
    prompt: str = "Prompt",
    repo_path: str = "/tmp/repo",
    sandbox: str = "read-only",
) -> dict:
    return {
        "mode": "exec",
        "found": True,
        "codex_path": "/usr/local/bin/codex",
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "validation_error": None,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, prompt],
        "cwd": repo_path,
        "exit_code": 0,
        "stdout": "hello\n",
        "stderr": "",
        "timed_out": False,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "final_message_path": f"{repo_path}/.codex-final-message.md",
        "final_message": "Final assistant summary.\n",
        "final_message_length": 26,
        "final_message_status": "valid",
        "final_message_error": None,
    }


class FakeLedger:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.events: list[dict] = list(events or [])
        self._next_id = max(
            [
                int(event.get("id") or 0)
                for event in self.events
                if str(event.get("id") or "").isdigit()
            ],
            default=0,
        ) + 1

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> dict:
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

    def get_run(self, run_id: str) -> dict:
        return {"id": run_id, "status": "created"}


class RecordingRunner:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        return self.result


def _controller_started_event(*, event_id: int = 1, sandbox: str = "read-only") -> dict:
    metadata = {
        "metadata_version": "local_controller_v1",
        "repository_path": "/tmp/repo",
        "sandbox": sandbox,
        "source": "local_controller",
        "controller_mode": "browser_v1",
        "browser_safe_sandbox": True,
    }
    return {
        "id": event_id,
        "run_id": "run-1",
        "event_type": "local_controller_run_started",
        "message": "Local controller run initialized.",
        "metadata": metadata,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


class ExecutionProfileExactKeyReconstructorTests(unittest.TestCase):
    def test_v1_exact_six_keys_reconstruct_on_both_readers(self) -> None:
        service_profile = _execution_profile_from_metadata(VALID_V1_METADATA)
        self.assertIsNotNone(service_profile)
        assert service_profile is not None
        self.assertEqual(service_profile.sandbox, "workspace-write")
        self.assertEqual(service_profile.model, CODEX_DEFAULT_SELECTION)
        self.assertEqual(service_profile.reasoning_effort, CODEX_DEFAULT_SELECTION)
        self.assertEqual(service_profile.approval_policy, CODEX_DEFAULT_SELECTION)
        self.assertEqual(service_profile.profile_source, "explicit_user_selection")
        self.assertEqual(service_profile.coding_agent, DEFAULT_CODING_AGENT)

        ledger_profile = _execution_profile_from_metadata_json(
            json.dumps(VALID_V1_METADATA, sort_keys=True)
        )
        self.assertEqual(
            ledger_profile,
            (
                "workspace-write",
                CODEX_DEFAULT_SELECTION,
                CODEX_DEFAULT_SELECTION,
                CODEX_DEFAULT_SELECTION,
                "explicit_user_selection",
                DEFAULT_CODING_AGENT,
            ),
        )
        self.assertEqual(set(VALID_V1_METADATA), EXPECTED_V1_PROFILE_KEYS)

    def test_extra_key_is_invalid_on_both_reconstructors(self) -> None:
        extra = dict(VALID_V1_METADATA)
        extra["coding_agent"] = "codex"
        self.assertIsNone(_execution_profile_from_metadata(extra))
        self.assertIsNone(
            _execution_profile_from_metadata_json(json.dumps(extra, sort_keys=True))
        )

    def test_unknown_schema_version_is_invalid_on_both_reconstructors(self) -> None:
        unknown = dict(VALID_V1_METADATA)
        unknown["schema_version"] = 2
        self.assertEqual(set(unknown), EXPECTED_V1_PROFILE_KEYS)
        self.assertIsNone(_execution_profile_from_metadata(unknown))
        self.assertIsNone(
            _execution_profile_from_metadata_json(json.dumps(unknown, sort_keys=True))
        )

    def test_missing_key_is_invalid_on_both_reconstructors(self) -> None:
        missing = {
            key: value
            for key, value in VALID_V1_METADATA.items()
            if key != "profile_source"
        }
        self.assertIsNone(_execution_profile_from_metadata(missing))
        self.assertIsNone(
            _execution_profile_from_metadata_json(json.dumps(missing, sort_keys=True))
        )


class ExecutionProfileDurableLockstepTests(unittest.TestCase):
    def test_selected_v1_profile_round_trips_through_both_reconstructors(self) -> None:
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("pin v1 execution profile")
            selected = select_run_execution_profile(
                run_id,
                VALID_PROFILE,
                ledger=ledger_module,
            )
            self.assertTrue(selected.ok)
            self.assertEqual(set(selected.metadata or {}), EXPECTED_V1_PROFILE_KEYS)
            self.assertNotIn("coding_agent", selected.metadata or {})

            lookup = get_run_execution_profile(run_id, ledger=ledger_module)
            self.assertEqual(lookup.status, ExecutionProfileLookupStatus.PRESENT)
            self.assertIsNotNone(lookup.profile)
            assert lookup.profile is not None
            self.assertEqual(lookup.profile, VALID_PROFILE)
            self.assertEqual(lookup.profile.coding_agent, DEFAULT_CODING_AGENT)

            events = [
                event
                for event in ledger_module.list_events(run_id)
                if event["event_type"] == RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE
            ]
            self.assertEqual(len(events), 1)
            stored = json.loads(events[0]["metadata_json"])
            self.assertEqual(set(stored), EXPECTED_V1_PROFILE_KEYS)
            self.assertEqual(
                _execution_profile_from_metadata(stored),
                VALID_PROFILE,
            )
            self.assertEqual(
                _execution_profile_from_metadata_json(events[0]["metadata_json"]),
                (
                    VALID_PROFILE.sandbox,
                    VALID_PROFILE.model,
                    VALID_PROFILE.reasoning_effort,
                    VALID_PROFILE.approval_policy,
                    VALID_PROFILE.profile_source,
                    DEFAULT_CODING_AGENT,
                ),
            )

            rebound = bind_run_execution_profile(
                run_id,
                VALID_PROFILE.sandbox,
                VALID_PROFILE.model,
                VALID_PROFILE.reasoning_effort,
                VALID_PROFILE.approval_policy,
                VALID_PROFILE.profile_source,
            )
            self.assertEqual(rebound.status, AtomicExecutionProfileStatus.IDEMPOTENT)
            self.assertEqual(rebound.coding_agent, DEFAULT_CODING_AGENT)

    def test_extra_key_durable_row_is_invalid_for_lookup_and_bind(self) -> None:
        extra = dict(VALID_V1_METADATA)
        extra["coding_agent"] = "codex"
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("malformed extra key")
            ledger_module.add_event(
                run_id,
                RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
                "Run execution profile selected.",
                extra,
            )
            lookup = get_run_execution_profile(run_id, ledger=ledger_module)
            self.assertEqual(lookup.status, ExecutionProfileLookupStatus.INVALID)
            self.assertEqual(lookup.reason_code, "malformed_execution_profile_event")

            rebound = bind_run_execution_profile(
                run_id,
                VALID_PROFILE.sandbox,
                VALID_PROFILE.model,
                VALID_PROFILE.reasoning_effort,
                VALID_PROFILE.approval_policy,
                VALID_PROFILE.profile_source,
            )
            self.assertEqual(rebound.status, AtomicExecutionProfileStatus.INVALID)
            self.assertEqual(rebound.reason_code, "malformed_execution_profile_event")

    def test_unknown_version_durable_row_is_invalid_for_lookup_and_bind(self) -> None:
        unknown = dict(VALID_V1_METADATA)
        unknown["schema_version"] = 2
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("unknown profile version")
            ledger_module.add_event(
                run_id,
                RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
                "Run execution profile selected.",
                unknown,
            )
            lookup = get_run_execution_profile(run_id, ledger=ledger_module)
            self.assertEqual(lookup.status, ExecutionProfileLookupStatus.INVALID)
            self.assertEqual(lookup.reason_code, "malformed_execution_profile_event")

            rebound = bind_run_execution_profile(
                run_id,
                VALID_PROFILE.sandbox,
                VALID_PROFILE.model,
                VALID_PROFILE.reasoning_effort,
                VALID_PROFILE.approval_policy,
                VALID_PROFILE.profile_source,
            )
            self.assertEqual(rebound.status, AtomicExecutionProfileStatus.INVALID)
            self.assertEqual(rebound.reason_code, "malformed_execution_profile_event")


class ExecutionProfileDualSchemaReaderTests(unittest.TestCase):
    def test_v1_reads_as_codex_on_both_reconstructors(self) -> None:
        service_profile = _execution_profile_from_metadata(VALID_V1_METADATA)
        self.assertIsNotNone(service_profile)
        assert service_profile is not None
        self.assertEqual(service_profile.coding_agent, DEFAULT_CODING_AGENT)

        ledger_profile = _execution_profile_from_metadata_json(
            json.dumps(VALID_V1_METADATA, sort_keys=True)
        )
        self.assertIsNotNone(ledger_profile)
        assert ledger_profile is not None
        self.assertEqual(ledger_profile[5], DEFAULT_CODING_AGENT)

    def test_v2_requires_coding_agent_and_reconstructs_worker(self) -> None:
        for worker in ("codex", "claude_code", "cursor"):
            with self.subTest(coding_agent=worker):
                metadata = _valid_v2_metadata(coding_agent=worker)
                self.assertEqual(set(metadata), EXPECTED_V2_PROFILE_KEYS)
                service_profile = _execution_profile_from_metadata(metadata)
                self.assertIsNotNone(service_profile)
                assert service_profile is not None
                self.assertEqual(service_profile.coding_agent, worker)
                self.assertEqual(service_profile.sandbox, "workspace-write")

                ledger_profile = _execution_profile_from_metadata_json(
                    json.dumps(metadata, sort_keys=True)
                )
                self.assertEqual(
                    ledger_profile,
                    (
                        "workspace-write",
                        CODEX_DEFAULT_SELECTION,
                        CODEX_DEFAULT_SELECTION,
                        CODEX_DEFAULT_SELECTION,
                        "explicit_user_selection",
                        worker,
                    ),
                )

    def test_malformed_v2_does_not_become_codex(self) -> None:
        cases = [
            ("missing_coding_agent", dict(VALID_V1_METADATA, schema_version=2)),
            ("unknown_worker", _valid_v2_metadata(coding_agent="gpt")),
            (
                "extra_key",
                {**_valid_v2_metadata(coding_agent="claude_code"), "extra": "nope"},
            ),
            (
                "schema_version_3",
                {**_valid_v2_metadata(coding_agent="claude_code"), "schema_version": 3},
            ),
        ]
        for name, metadata in cases:
            with self.subTest(name=name):
                self.assertIsNone(_execution_profile_from_metadata(metadata))
                self.assertIsNone(
                    _execution_profile_from_metadata_json(
                        json.dumps(metadata, sort_keys=True)
                    )
                )

    def test_codex_select_still_writes_v1_six_keys(self) -> None:
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("codex still writes v1")
            selected = select_run_execution_profile(
                run_id,
                VALID_PROFILE,
                ledger=ledger_module,
            )
            self.assertTrue(selected.ok)
            self.assertEqual(set(selected.metadata or {}), EXPECTED_V1_PROFILE_KEYS)
            self.assertEqual(
                (selected.metadata or {}).get("schema_version"),
                RUN_EXECUTION_PROFILE_SCHEMA_VERSION,
            )
            self.assertNotIn("coding_agent", selected.metadata or {})
            stored = json.loads(
                [
                    event
                    for event in ledger_module.list_events(run_id)
                    if event["event_type"]
                    == RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE
                ][0]["metadata_json"]
            )
            self.assertEqual(stored["schema_version"], 1)
            self.assertEqual(set(stored), EXPECTED_V1_PROFILE_KEYS)

            lookup = get_run_execution_profile(run_id, ledger=ledger_module)
            self.assertEqual(lookup.status, ExecutionProfileLookupStatus.PRESENT)
            assert lookup.profile is not None
            self.assertEqual(lookup.profile.coding_agent, DEFAULT_CODING_AGENT)

    def test_non_codex_select_writes_v2_identity_without_enabling_execution(self) -> None:
        claude_profile = RunExecutionProfile(
            sandbox="workspace-write",
            model=CODEX_DEFAULT_SELECTION,
            reasoning_effort=CODEX_DEFAULT_SELECTION,
            approval_policy=CODEX_DEFAULT_SELECTION,
            profile_source="explicit_user_selection",
            coding_agent="claude_code",
        )
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("no v2 writer yet")
            selected = select_run_execution_profile(
                run_id,
                claude_profile,
                ledger=ledger_module,
            )
            self.assertTrue(selected.ok)
            self.assertEqual(selected.profile.coding_agent, "claude_code")
            events = [
                event
                for event in ledger_module.list_events(run_id)
                if event["event_type"] == RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE
            ]
            self.assertEqual(len(events), 1)
            metadata = json.loads(events[0]["metadata_json"])
            self.assertEqual(metadata["schema_version"], 2)
            self.assertEqual(metadata["coding_agent"], "claude_code")

    def test_planted_v2_claude_row_is_not_treated_as_codex_idempotent(self) -> None:
        metadata = _valid_v2_metadata(coding_agent="claude_code")
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("planted v2 claude")
            ledger_module.add_event(
                run_id,
                RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
                "Run execution profile selected.",
                metadata,
            )
            lookup = get_run_execution_profile(run_id, ledger=ledger_module)
            self.assertEqual(lookup.status, ExecutionProfileLookupStatus.PRESENT)
            assert lookup.profile is not None
            self.assertEqual(lookup.profile.coding_agent, "claude_code")

            rebound = bind_run_execution_profile(
                run_id,
                VALID_PROFILE.sandbox,
                VALID_PROFILE.model,
                VALID_PROFILE.reasoning_effort,
                VALID_PROFILE.approval_policy,
                VALID_PROFILE.profile_source,
            )
            self.assertEqual(
                rebound.status,
                AtomicExecutionProfileStatus.DIFFERENT_PROFILE,
            )
            self.assertEqual(rebound.coding_agent, "claude_code")
            events = [
                event
                for event in ledger_module.list_events(run_id)
                if event["event_type"] == RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE
            ]
            self.assertEqual(len(events), 1)
            stored = json.loads(events[0]["metadata_json"])
            self.assertEqual(stored["coding_agent"], "claude_code")
            self.assertEqual(stored["schema_version"], 2)

    def test_durable_malformed_v2_stays_invalid(self) -> None:
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("malformed v2")
            ledger_module.add_event(
                run_id,
                RUN_EXECUTION_PROFILE_SELECTED_EVENT_TYPE,
                "Run execution profile selected.",
                _valid_v2_metadata(coding_agent="not-a-worker"),
            )
            lookup = get_run_execution_profile(run_id, ledger=ledger_module)
            self.assertEqual(lookup.status, ExecutionProfileLookupStatus.INVALID)
            self.assertEqual(lookup.reason_code, "malformed_execution_profile_event")

            rebound = bind_run_execution_profile(
                run_id,
                VALID_PROFILE.sandbox,
                VALID_PROFILE.model,
                VALID_PROFILE.reasoning_effort,
                VALID_PROFILE.approval_policy,
                VALID_PROFILE.profile_source,
            )
            self.assertEqual(rebound.status, AtomicExecutionProfileStatus.INVALID)


class ExecutionProfileImmutabilityTests(unittest.TestCase):
    def test_profile_cannot_be_rebound_after_codex_exec_started(self) -> None:
        with _temporary_real_ledger():
            run_id = ledger_module.create_run("immutable after start")
            selected = select_run_execution_profile(
                run_id,
                VALID_PROFILE,
                ledger=ledger_module,
            )
            self.assertTrue(selected.ok)
            ledger_module.add_event(
                run_id,
                CODEX_EXEC_STARTED_EVENT_TYPE,
                "Running Codex exec.",
                {"sandbox": VALID_PROFILE.sandbox},
            )

            retry = select_run_execution_profile(
                run_id,
                VALID_PROFILE,
                ledger=ledger_module,
            )
            self.assertFalse(retry.ok)
            self.assertEqual(
                retry.reason_code,
                "execution_profile_immutable_after_codex_exec_started",
            )

            rebound = bind_run_execution_profile(
                run_id,
                "read-only",
                VALID_PROFILE.model,
                VALID_PROFILE.reasoning_effort,
                VALID_PROFILE.approval_policy,
                VALID_PROFILE.profile_source,
            )
            self.assertEqual(
                rebound.status,
                AtomicExecutionProfileStatus.EXECUTION_STARTED,
            )
            self.assertEqual(
                rebound.reason_code,
                "execution_profile_immutable_after_codex_exec_started",
            )
            self.assertEqual(rebound.sandbox, VALID_PROFILE.sandbox)


class ExecutionProfileSpawnGateTests(unittest.TestCase):
    def test_cli_missing_profile_still_spawns_codex_default(self) -> None:
        ledger = FakeLedger()
        runner = RecordingRunner(_raw_runner_result())

        result = execute_codex_direct_service(
            "run-1",
            "Prompt",
            "/tmp/repo",
            "read-only",
            None,
            {},
            ledger=ledger,
            codex_runner=runner,
        )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["sandbox"], "read-only")
        self.assertNotIn("model", runner.calls[0][1])
        self.assertIn(
            "codex_exec_started",
            [event["event_type"] for event in ledger.events],
        )
        self.assertTrue(result.ok)

    def test_controller_missing_profile_does_not_spawn(self) -> None:
        ledger = FakeLedger([_controller_started_event()])
        runner = RecordingRunner(_raw_runner_result())

        result = execute_codex_direct_service(
            "run-1",
            "Prompt",
            "/tmp/repo",
            "read-only",
            None,
            {},
            ledger=ledger,
            codex_runner=runner,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "execution_profile_missing")
        self.assertEqual(runner.calls, [])
        self.assertNotIn(
            "codex_exec_started",
            [event["event_type"] for event in ledger.events],
        )


class CodexWrapperCurrentBehaviorTests(unittest.TestCase):
    def test_spawn_wrapper_starts_new_session_and_does_not_set_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "agent.codex_invocation.default_runs_root",
                return_value=Path(tmp),
            ):
                paths = artifact_paths_for("run-wrapper", "inv-1")
                with mock.patch("agent.codex_invocation.subprocess.Popen") as popen:
                    popen.return_value = mock.Mock(pid=4242)
                    spawn_invocation_wrapper(paths, cwd="/tmp/repo")

        kwargs = popen.call_args.kwargs
        self.assertNotIn("stdin", kwargs)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["cwd"], "/tmp/repo")

    def test_run_wrapper_child_popen_does_not_set_stdin(self) -> None:
        recorded: dict[str, object] = {}

        def fake_popen(command, **kwargs):
            recorded["kwargs"] = kwargs
            child = mock.Mock()
            child.pid = 99
            child.wait.return_value = 0
            child.poll.return_value = 0
            return child

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "agent.codex_invocation.default_runs_root",
                return_value=Path(tmp),
            ):
                paths = artifact_paths_for("run-wrapper", "inv-2")
                write_intent(
                    paths,
                    build_intent_payload(
                        run_id="run-wrapper",
                        invocation_id="inv-2",
                        prompt="Prompt",
                        repo_path="/tmp/repo",
                        cwd="/tmp/repo",
                        sandbox="read-only",
                        model=None,
                        command=["codex", "exec", "Prompt"],
                        json_mode=True,
                        paths=paths,
                    ),
                )
                with (
                    mock.patch(
                        "agent.codex_invocation_wrapper.capture_process_identity",
                        return_value={"codex_invocation_id": "inv-2", "pid": 99},
                    ),
                    mock.patch(
                        "agent.codex_invocation_wrapper.subprocess.Popen",
                        side_effect=fake_popen,
                    ),
                ):
                    exit_code = run_wrapper(paths.intent_path)

        self.assertEqual(exit_code, 0)
        kwargs = recorded["kwargs"]
        assert isinstance(kwargs, dict)
        self.assertNotIn("stdin", kwargs)
        self.assertNotIn("start_new_session", kwargs)

    def test_terminate_popen_group_signals_process_group(self) -> None:
        class _LiveProcess:
            def __init__(self) -> None:
                self.pid = 4242
                self.returncode = -signal.SIGTERM

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                return self.returncode

            def terminate(self) -> None:
                raise AssertionError("group terminate should use killpg")

            def kill(self) -> None:
                raise AssertionError("group kill should use killpg")

        with (
            mock.patch.object(codex_terminal.os, "getpgid", return_value=9999) as getpgid,
            mock.patch.object(codex_terminal.os, "killpg") as killpg,
            mock.patch.object(codex_terminal, "process_group_is_alive", return_value=False),
            mock.patch.object(codex_terminal, "wait_for_process_group_exit", return_value=True),
        ):
            result = codex_terminal._terminate_popen_group(
                _LiveProcess(),
                timeout_seconds=1.0,
            )

        getpgid.assert_called_once_with(4242)
        killpg.assert_called_once_with(9999, signal.SIGTERM)
        self.assertEqual(result["reason_code"], "codex_process_terminated")
        self.assertEqual(result["exit_code"], -signal.SIGTERM)


class CodexFinalMessageNotSynthesizedTests(unittest.TestCase):
    def test_missing_final_message_file_is_not_filled_from_subprocess_stdout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="stdout pretending to be final\n",
            stderr="",
        )

        def run_side_effect(command, **_kwargs):
            self.assertIn("--output-last-message", command)
            return completed

        with (
            tempfile.TemporaryDirectory() as repo,
            mock.patch.object(
                codex_terminal.shutil,
                "which",
                return_value="/opt/homebrew/bin/codex",
            ),
            mock.patch.object(
                codex_terminal.subprocess,
                "run",
                side_effect=run_side_effect,
            ),
        ):
            result = run_codex_exec(
                "Prompt",
                repo_path=repo,
                sandbox="read-only",
            )

        self.assertEqual(result["stdout"], "stdout pretending to be final\n")
        self.assertEqual(result["final_message_status"], "missing")
        self.assertEqual(result["final_message"], "")
        self.assertNotEqual(result["final_message"], result["stdout"].strip())
        self.assertIn("was not written", str(result.get("final_message_error") or ""))

    def test_json_stream_missing_final_message_is_not_filled_from_stdout(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        root = Path(tmpdir.name)
        repo = root / "repo"
        repo.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text(FAKE_CODEX_WITHOUT_FINAL_MESSAGE, encoding="utf-8")
        fake.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        runs_root = root / "runs"
        path_patch = mock.patch(
            "agent.codex_invocation.default_runs_root",
            return_value=runs_root,
        )
        path_patch.start()
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            result = run_codex_exec(
                "Prompt",
                repo_path=str(repo),
                sandbox="read-only",
                run_id="run-no-final",
                json_stream=True,
                codex_invocation_id="inv-no-final",
            )
            self.assertEqual(result["exit_code"], 0)
            self.assertIn("stdout pretending to be final", result["stdout"])
            self.assertEqual(result["final_message_status"], "missing")
            self.assertEqual(result["final_message"], "")
            self.assertNotIn("stdout pretending to be final", result["final_message"])
        finally:
            terminate_all_active_codex_invocations(source="test_teardown")
            path_patch.stop()
            os.environ["PATH"] = old_path
            tmpdir.cleanup()


class CodexSentinelExtractionTests(unittest.TestCase):
    def test_sentinel_constants_and_extraction_method_are_unchanged(self) -> None:
        self.assertEqual(BEGIN_SENTINEL, "BEGIN_NEXT_CODEX_PROMPT")
        self.assertEqual(END_SENTINEL, "END_NEXT_CODEX_PROMPT")
        self.assertEqual(AX_BEGIN_SENTINEL, BEGIN_SENTINEL)
        self.assertEqual(AX_END_SENTINEL, END_SENTINEL)
        self.assertEqual(chatgpt_ax_capture.BEGIN_SENTINEL, BEGIN_SENTINEL)
        self.assertEqual(chatgpt_ax_capture.END_SENTINEL, END_SENTINEL)

        extracted = extract_next_codex_prompt_from_text(
            "Planner preamble\n"
            "BEGIN_NEXT_CODEX_PROMPT\n"
            "Do the task\n"
            "END_NEXT_CODEX_PROMPT\n"
            "Planner closing"
        )
        self.assertTrue(extracted.ok)
        self.assertEqual(extracted.extraction_method, "sentinel_block")
        self.assertEqual(extracted.prompt_text, "Do the task")


if __name__ == "__main__":
    unittest.main()
