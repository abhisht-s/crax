"""Claude Code provider (Step 1). Only a temporary fake ``claude`` CLI is ever executed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from agent import ledger
from agent.claude_code_provider import (
    CLAUDE_PROGRESS_SOURCE,
    ClaudeCodeProvider,
    build_claude_command,
    claude_session_details,
    normalize_claude_stream_event,
    validate_claude_request,
)
from agent.claude_code_stream import (
    EXTRACTION_EMPTY_RESULT,
    EXTRACTION_NO_RESULT_EVENT,
    EXTRACTION_RESULT_NOT_SUCCESS,
    EXTRACTION_WRITTEN,
    FINAL_MESSAGE_SOURCE_CLAUDE_STREAM,
    final_message_from_result,
    first_session_id,
    last_result_event,
    write_final_message_from_stream,
)
from agent.codex_invocation import artifact_paths_for, build_intent_payload, write_intent
from agent.codex_invocation_wrapper import run_wrapper
from agent.coding_agent_provider import NativeSessionIdentity
from agent.coding_agent_services import execute_coding_agent_service
from agent.coding_agents import (
    ProviderResolutionError, coding_agent_session_details, provider_for_events, resolve_provider,
)
from agent.run_services import RunExecutionProfile, get_run_execution_profile, select_run_execution_profile

SESSION = "11111111-2222-4333-8444-555555555555"

# Shapes follow `claude -p --output-format stream-json --verbose`.
def init_event(session=SESSION):
    return {"type": "system", "subtype": "init", "session_id": session, "model": "claude-x",
            "permissionMode": "dontAsk", "tools": ["Read"], "cwd": "/repo"}


def assistant(*blocks, session=SESSION):
    return {"type": "assistant", "session_id": session,
            "message": {"role": "assistant", "content": list(blocks)}}


def tool_result(is_error=False, session=SESSION):
    return {"type": "user", "session_id": session, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "output body", "is_error": is_error}]}}


def result_event(text="All done.", subtype="success", is_error=False, session=SESSION, **extra):
    return {"type": "result", "subtype": subtype, "is_error": is_error, "result": text,
            "session_id": session, "num_turns": 3, "duration_ms": 1200, **extra}


def lines(*events):
    return [json.dumps(event) for event in events]


FAKE_CLAUDE_SOURCE = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
record = os.environ.get("FAKE_CLAUDE_ARGV_PATH")
if record:
    with open(record, "a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
def after(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
session = after("--resume") or after("--session-id")
prompt = argv[argv.index("--") + 1]
subtype = os.environ.get("FAKE_CLAUDE_SUBTYPE", "success")
final = os.environ.get("FAKE_CLAUDE_FINAL", "Claude final answer for: " + prompt)
events = [
    {"type": "system", "subtype": "init", "session_id": session, "model": "claude-x"},
    {"type": "assistant", "session_id": session, "message": {"content": [{"type": "text", "text": "Working on it."}]}},
    {"type": "assistant", "session_id": session, "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "git status --token secret-value"}}]}},
    {"type": "user", "session_id": session, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "clean", "is_error": False}]}},
    {"type": "result", "subtype": subtype, "is_error": subtype != "success",
     "result": final if subtype == "success" else "", "session_id": session, "num_turns": 2,
     **({} if subtype == "success" else {"errors": ["max turns reached"]})},
]
sys.stderr.write("fake claude stderr\n")
for event in events:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0" if subtype == "success" else "1")))
'''


class StreamParsingTests(unittest.TestCase):
    def test_last_result_and_session_ignore_malformed_and_blank_lines(self):
        stream = ["", "not json", "[1]", *lines(init_event(), assistant({"type": "text", "text": "hi"}),
                                                result_event("first")), *lines(result_event("second"))]
        self.assertEqual(last_result_event(stream)["result"], "second")
        self.assertEqual(first_session_id(stream), SESSION)
        self.assertIsNone(last_result_event(lines(init_event())))

    def test_only_a_successful_nonempty_result_is_a_final_message(self):
        self.assertEqual(final_message_from_result(result_event("Done")), "Done")
        self.assertIsNone(final_message_from_result(result_event("Done", subtype="error_max_turns")))
        self.assertIsNone(final_message_from_result(result_event("Done", is_error=True)))
        self.assertIsNone(final_message_from_result(result_event("   ")))
        self.assertIsNone(final_message_from_result(None))

    def test_write_final_message_statuses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cases = {
                EXTRACTION_WRITTEN: lines(init_event(), result_event("Final\n\n  answer")),
                EXTRACTION_NO_RESULT_EVENT: lines(init_event()),
                EXTRACTION_RESULT_NOT_SUCCESS: lines(result_event("x", subtype="error_during_execution")),
                EXTRACTION_EMPTY_RESULT: lines(result_event("")),
            }
            for expected, stream in cases.items():
                with self.subTest(expected=expected):
                    stdout = root / f"{expected}.log"
                    stdout.write_text("\n".join(stream) + "\n")
                    final = root / f"{expected}.md"
                    self.assertEqual(write_final_message_from_stream(stdout, final), expected)
                    self.assertEqual(final.exists(), expected == EXTRACTION_WRITTEN)
            self.assertEqual((root / f"{EXTRACTION_WRITTEN}.md").read_text(), "Final\n\n  answer\n")
            self.assertEqual(write_final_message_from_stream(root / "missing.log", root / "m.md"), "read_failed")


class CommandTests(unittest.TestCase):
    def test_common_flags_and_prompt_after_double_dash(self):
        command = build_claude_command("--looks-like-a-flag", sandbox="read-only", session_id=SESSION)
        self.assertEqual(command[0], "claude")
        for flag in ("-p", "--verbose", "--strict-mcp-config"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
        self.assertEqual(command[command.index("--setting-sources") + 1], "project,local")
        self.assertEqual(command[-2:], ["--", "--looks-like-a-flag"])
        self.assertEqual(command[command.index("--session-id") + 1], SESSION)
        self.assertNotIn("--resume", command)
        self.assertNotIn("--model", command)

    def test_permission_profiles(self):
        def settings(command):
            return json.loads(command[command.index("--settings") + 1]) if "--settings" in command else None

        read_only = build_claude_command("p", sandbox="read-only", session_id=SESSION)
        self.assertEqual(read_only[read_only.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(settings(read_only)["permissions"]["allow"], ["Read", "Grep", "Glob"])
        self.assertIn("Bash", settings(read_only)["permissions"]["deny"])
        self.assertNotIn("--dangerously-skip-permissions", read_only)

        write = build_claude_command("p", sandbox="workspace-write", session_id=SESSION)
        self.assertEqual(write[write.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(settings(write)["sandbox"],
                         {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False})

        auto = build_claude_command("p", sandbox="danger-full-access", session_id=SESSION)
        self.assertEqual(auto[auto.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", auto)
        self.assertIsNone(settings(auto))  # no OS sandbox, like Codex Full Access
        self.assertEqual(auto[auto.index("--permission-prompts") + 1], "none")

    def test_model_and_resume(self):
        for default in ("codex_default", None):
            self.assertNotIn("--model", build_claude_command("p", sandbox="read-only", model=default, session_id=SESSION))
        command = build_claude_command("p", sandbox="read-only", model="opus", resume_session_id=SESSION)
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--resume") + 1], SESSION)
        self.assertNotIn("--session-id", command)
        with self.assertRaises(ValueError):
            build_claude_command("p", sandbox="read-only")
        with self.assertRaises(ValueError):
            build_claude_command("p", sandbox="read-only", session_id=SESSION, resume_session_id=SESSION)

    def test_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            ok = dict(repo_path=repo, sandbox="read-only", model="codex_default", session_id=SESSION, resume_session_id=None)
            self.assertIsNone(validate_claude_request(**ok))
            self.assertIn("sandbox", validate_claude_request(**{**ok, "sandbox": "full"}))
            self.assertIn("model", validate_claude_request(**{**ok, "model": "gpt-5"}))
            self.assertIn("session", validate_claude_request(**{**ok, "session_id": None, "resume_session_id": "--evil"}))
            self.assertIn("does not exist", validate_claude_request(**{**ok, "repo_path": repo / "nope"}))


class ProgressTests(unittest.TestCase):
    def test_events_map_to_dashboard_kinds(self):
        cases = [
            (init_event(), "codex_json_event", "running"),
            (assistant({"type": "text", "text": "Reading files"}), "assistant_commentary", "observed"),
            (assistant({"type": "tool_use", "name": "Bash", "input": {"command": "npm test --token secret-value"}}),
             "command_started", "running"),
            (assistant({"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/a.py"}}),
             "file_change_summary", "running"),
            (assistant({"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}), "tool_event", "running"),
            (tool_result(), "tool_event", "completed"),
            (tool_result(is_error=True), "tool_event", "failed"),
            (assistant({"type": "thinking", "thinking": "private"}), "codex_json_event", "observed"),
            (result_event(), "codex_json_event", "completed"),
            (result_event("", subtype="error_max_turns", errors=["max turns"]), "error", "failed"),
        ]
        for event, kind, status in cases:
            with self.subTest(event_type=event["type"], kind=kind):
                progress = normalize_claude_stream_event(json.dumps(event))
                self.assertIn(progress["kind"], ledger.CODEX_PROGRESS_ALLOWED_KINDS)
                self.assertEqual((progress["kind"], progress["status"]), (kind, status))
                self.assertEqual(progress["source"], CLAUDE_PROGRESS_SOURCE)

    def test_commands_thinking_and_tool_output_are_not_stored(self):
        for event in (
            assistant({"type": "tool_use", "name": "Bash", "input": {"command": "npm test --token secret-value"}}),
            assistant({"type": "thinking", "thinking": "private reasoning"}),
            tool_result(),
        ):
            dumped = json.dumps(normalize_claude_stream_event(json.dumps(event)))
            for secret in ("secret-value", "private reasoning", "output body"):
                self.assertNotIn(secret, dumped)
        bash = normalize_claude_stream_event(json.dumps(
            assistant({"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}})))
        self.assertEqual(bash["title"], "Command started: npm")

    def test_session_id_edit_path_and_malformed(self):
        init = normalize_claude_stream_event(json.dumps(init_event()))
        self.assertEqual(init["metadata"]["value_summary"]["claude_session_id"], SESSION)
        edit = normalize_claude_stream_event(json.dumps(
            assistant({"type": "tool_use", "name": "Write", "input": {"file_path": "/repo/b.md"}})))
        self.assertEqual(edit["metadata"]["value_summary"]["file_changes"]["paths"], ["/repo/b.md"])
        malformed = normalize_claude_stream_event("{nope")
        self.assertEqual((malformed["kind"], malformed["status"]), ("codex_json_event", "malformed"))
        error = normalize_claude_stream_event(json.dumps(result_event("", subtype="error_max_turns", errors=["max turns"])))
        self.assertIn("max turns", error["summary"])


def _result(**overrides):
    base = {
        "found": True, "validation_error": None, "timed_out": False, "exit_code": 0,
        "stdout": "\n".join(lines(init_event(), result_event("Done"))), "stderr": "",
        "final_message": "Done", "final_message_status": "valid",
        "invocation_complete": True, "termination_reason": "exited",
    }
    return {**base, **overrides}


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.provider = ClaudeCodeProvider()

    def test_success(self):
        result = _result()
        self.assertEqual(self.provider.reason_code(result), "claude_code_exec_completed")
        self.assertIsNone(self.provider.error_message(result, "claude_code_exec_completed"))
        outcome = self.provider.classify(result)
        self.assertEqual((outcome.process, outcome.provider, outcome.final_answer), ("exited", "completed", "valid"))
        self.assertEqual(outcome.native_session, NativeSessionIdentity("claude_code", SESSION))

    def test_failures(self):
        error_stream = "\n".join(lines(init_event(), result_event("", subtype="error_max_turns", errors=["max turns"])))
        cases = [
            (_result(stdout=error_stream, exit_code=1, final_message="", final_message_status="missing"),
             "claude_code_result_error_max_turns", "failed"),
            (_result(stdout="\n".join(lines(result_event("x", is_error=True)))), "claude_code_result_error", "failed"),
            (_result(exit_code=1), "claude_code_nonzero_exit", "failed"),
            (_result(final_message="", final_message_status="missing"), "claude_code_final_message_missing", "unverified"),
            (_result(found=False, exit_code=None), "claude_code_not_found", "failed"),
            (_result(validation_error="bad"), "claude_code_validation_error", "failed"),
            (_result(exit_code=None, invocation_complete=False), "claude_code_exit_missing", "uncertain"),
            (_result(invocation_complete=False, termination_reason="uncertain"), "codex_invocation_uncertain", "uncertain"),
            (_result(stdout="\n".join(lines(init_event()))), "claude_code_exec_completed", "unverified"),
        ]
        for result, reason, provider in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.provider.reason_code(result), reason)
                self.assertEqual(self.provider.classify(result).provider, provider)
        message = self.provider.error_message(cases[0][0], "claude_code_result_error_max_turns")
        self.assertIn("error_max_turns", message)
        self.assertIn("max turns", message)

    def test_session_falls_back_to_the_id_crax_passed(self):
        outcome = self.provider.classify(_result(stdout="", claude_session_id=SESSION, final_message_status="missing"))
        self.assertEqual(outcome.native_session.session_id, SESSION)
        self.assertIsNone(self.provider.classify(_result(validation_error="bad", claude_session_id=SESSION)).native_session)


class WrapperExtractionTests(unittest.TestCase):
    def _run(self, root: Path, stream: list[str], *, source: str | None) -> tuple[Path, dict]:
        paths = artifact_paths_for("run", f"inv-{uuid.uuid4().hex}", artifact_dir=root / uuid.uuid4().hex)
        script = root / "emit.py"
        script.write_text("import sys\nsys.stdout.write(open(sys.argv[1]).read())\n")
        data = root / f"{uuid.uuid4().hex}.jsonl"
        data.write_text("\n".join(stream) + "\n")
        intent = build_intent_payload(
            run_id="run", invocation_id=paths.artifact_dir.name, prompt="p", repo_path=str(root), cwd=str(root),
            sandbox="read-only", model=None, command=[sys.executable, str(script), str(data)], json_mode=True,
            paths=paths, final_message_source=source,
        )
        write_intent(paths, intent)
        self.assertEqual(run_wrapper(paths.intent_path), 0)
        return paths.final_message_path, json.loads(paths.exit_path.read_text())

    def test_claude_intent_writes_final_message_before_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            final, exit_payload = self._run(Path(temp), lines(init_event(), result_event("The answer")),
                                            source=FINAL_MESSAGE_SOURCE_CLAUDE_STREAM)
            self.assertEqual(final.read_text(), "The answer\n")
            self.assertEqual(exit_payload["final_message_extraction"], EXTRACTION_WRITTEN)
            self.assertEqual(exit_payload["exit_code"], 0)

    def test_codex_intent_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            final, exit_payload = self._run(Path(temp), lines(result_event("The answer")), source=None)
            self.assertFalse(final.exists())
            self.assertNotIn("final_message_extraction", exit_payload)
            intent_keys = json.loads(final.with_name("intent.json").read_text())
            self.assertNotIn("final_message_source", intent_keys)


class EndToEndTests(unittest.TestCase):
    """Real service, real ledger, real durable wrapper; fake `claude` on PATH."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(FAKE_CLAUDE_SOURCE.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        fake.chmod(0o755)
        self.argv_path = self.root / "argv.jsonl"
        env = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_CLAUDE_")}
        # Only the fake CLI and system tools: the user's real `codex`/`claude`
        # (e.g. ~/.local/bin) must never be reachable from these tests.
        env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
        # Never read the user's real ~/.claude/settings.json.
        env["CLAUDE_CONFIG_DIR"] = str(self.root / "claude-config")
        env["FAKE_CLAUDE_ARGV_PATH"] = str(self.argv_path)
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        import shutil
        self.assertEqual(shutil.which("claude"), str(fake))  # guard: never the real CLI
        self.assertIsNone(shutil.which("codex"))
        self.enterContext(mock.patch.object(ledger, "DB_PATH", self.root / "ledger.db"))
        self.enterContext(mock.patch("agent.codex_invocation.default_runs_root", return_value=self.root / "runs"))

    def run_id(self, sandbox="read-only", model="codex_default"):
        run_id = ledger.create_run("Say exactly: complete")
        selected = select_run_execution_profile(
            run_id, RunExecutionProfile(sandbox=sandbox, coding_agent="claude_code", model=model), ledger=ledger)
        self.assertTrue(selected.ok, selected.error_message)
        return run_id

    def execute(self, run_id, prompt="Say exactly: complete", sandbox="read-only", **kwargs):
        return execute_coding_agent_service(
            run_id, prompt, str(self.repo), sandbox, None, {}, ledger=ledger,
            _provider_resolver=lambda identity: ClaudeCodeProvider(), **kwargs)

    def calls(self):
        return [json.loads(line) for line in self.argv_path.read_text().splitlines()]

    def test_successful_run_records_claude_evidence(self):
        run_id = self.run_id()
        result = self.execute(run_id)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.coding_agent, "claude_code")
        self.assertEqual(result.reason_code, "claude_code_exec_completed")
        self.assertEqual(result.final_message, "Claude final answer for: Say exactly: complete")
        self.assertEqual((result.outcome.process, result.outcome.provider, result.outcome.final_answer),
                         ("exited", "completed", "valid"))

        call = self.calls()[0]
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo)
        session = call["argv"][call["argv"].index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(session)), session)
        self.assertEqual(result.outcome.native_session, NativeSessionIdentity("claude_code", session))

        raw = result.raw_process_result
        paths = artifact_paths_for(run_id, raw["codex_invocation_id"])
        intent = json.loads(paths.intent_path.read_text())
        self.assertEqual(intent["coding_agent"], "claude_code")
        self.assertEqual(intent["final_message_source"], FINAL_MESSAGE_SOURCE_CLAUDE_STREAM)
        self.assertEqual(json.loads(paths.exit_path.read_text())["final_message_extraction"], EXTRACTION_WRITTEN)
        self.assertEqual(paths.final_message_path.read_text().strip(), result.final_message)

        events = ledger.list_events(run_id)
        self.assertEqual(claude_session_details(run_id, events)["session_id"], session)
        started = next(e for e in events if e["event_type"] == "codex_exec_started")
        self.assertEqual(json.loads(started["metadata_json"])["coding_agent"], "claude_code")
        progress = [json.loads(e["metadata_json"]) for e in events if e["event_type"] == "codex_progress_event"]
        kinds = [p["kind"] for p in progress]
        for kind in ("process_started", "assistant_commentary", "command_started", "final_message_available", "process_exited"):
            self.assertIn(kind, kinds)
        self.assertEqual(next(p for p in progress if p["kind"] == "process_started")["title"], "Claude Code process started")
        self.assertNotIn("secret-value", json.dumps(progress))

    def test_resume_uses_the_same_session(self):
        run_id = self.run_id()
        first = self.execute(run_id)
        session = first.outcome.native_session.session_id
        second = self.execute(run_id, prompt="Continue", native_session=NativeSessionIdentity("claude_code", session))
        self.assertTrue(second.ok, second.error_message)
        argv = self.calls()[1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], session)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(second.outcome.native_session.session_id, session)

    def test_error_result_fails_without_a_final_message(self):
        os.environ["FAKE_CLAUDE_SUBTYPE"] = "error_max_turns"
        run_id = self.run_id()
        result = self.execute(run_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "claude_code_result_error_max_turns")
        self.assertEqual(result.outcome.provider, "failed")
        self.assertEqual(result.final_message, "")
        paths = artifact_paths_for(run_id, result.raw_process_result["codex_invocation_id"])
        self.assertFalse(paths.final_message_path.exists())
        self.assertEqual(json.loads(paths.exit_path.read_text())["final_message_extraction"], EXTRACTION_RESULT_NOT_SUCCESS)

    def test_workspace_write_passes_the_sandbox_settings(self):
        run_id = self.run_id(sandbox="workspace-write")
        self.assertTrue(self.execute(run_id, sandbox="workspace-write").ok)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertTrue(json.loads(argv[argv.index("--settings") + 1])["sandbox"]["enabled"])

    def test_autonomous_level_runs_in_auto_mode(self):
        run_id = self.run_id(sandbox="danger-full-access")
        self.assertTrue(self.execute(run_id, sandbox="danger-full-access").ok)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def write_user_settings(self, settings):
        config = Path(os.environ["CLAUDE_CONFIG_DIR"])
        config.mkdir(parents=True, exist_ok=True)
        (config / "settings.json").write_text(json.dumps(settings))

    def test_default_model_uses_the_users_own_model_and_effort(self):
        self.write_user_settings({"model": "opus", "effortLevel": "xhigh", "hooks": {"x": 1}})
        result = self.execute(self.run_id())
        self.assertTrue(result.ok, result.error_message)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")  # rest still skipped
        raw = result.raw_process_result
        self.assertEqual((raw["claude_model"], raw["claude_model_source"], raw["claude_effort"]),
                         ("opus", "user_settings", "xhigh"))

    def test_explicit_model_wins_over_user_settings(self):
        self.write_user_settings({"model": "opus", "effortLevel": "high"})
        result = self.execute(self.run_id(model="claude-sonnet-5-5"))
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(result.raw_process_result["claude_model_source"], "explicit")

    def test_chosen_effort_reaches_the_cli_and_wins_over_settings(self):
        self.write_user_settings({"model": "opus", "effortLevel": "xhigh"})
        run_id = ledger.create_run("Say exactly: complete")
        self.assertTrue(select_run_execution_profile(run_id, RunExecutionProfile(
            sandbox="read-only", coding_agent="claude_code", model="claude-fable-5-2", reasoning_effort="medium"),
            ledger=ledger).ok)
        result = self.execute(run_id)
        self.assertTrue(result.ok, result.error_message)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-2")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(result.raw_process_result["claude_effort_source"], "explicit")
        started = next(e for e in ledger.list_events(run_id) if e["event_type"] == "codex_exec_started")
        self.assertEqual(json.loads(started["metadata_json"])["reasoning_effort"], "medium")

    def test_user_effort_is_dropped_for_a_model_without_effort(self):
        self.write_user_settings({"effortLevel": "xhigh"})
        result = self.execute(self.run_id(model="claude-haiku-4-5"))
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-4-5")
        self.assertNotIn("--effort", argv)
        self.assertEqual(result.raw_process_result["claude_effort_source"], "model_has_no_such_effort")

    def test_no_or_bad_user_settings_fall_back_to_claude_default(self):
        for settings in (None, "not json", {"model": "--evil", "effortLevel": "ultra"}, {"model": ["opus"]}):
            with self.subTest(settings=settings):
                config = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
                if settings is None:
                    config.unlink(missing_ok=True)
                elif isinstance(settings, str):
                    config.parent.mkdir(parents=True, exist_ok=True)
                    config.write_text(settings)
                else:
                    self.write_user_settings(settings)
                result = self.execute(self.run_id())
                argv = self.calls()[-1]["argv"]
                self.assertNotIn("--model", argv)
                self.assertNotIn("--effort", argv)
                self.assertEqual(result.raw_process_result["claude_model_source"], "claude_code_default")

    def test_missing_cli_is_reported_not_run(self):
        os.environ["PATH"] = str(self.root / "empty-bin")
        run_id = self.run_id()
        result = self.execute(run_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "claude_code_not_found")
        self.assertFalse(self.argv_path.exists())




class ProductionWiringTests(EndToEndTests):
    """Step 2: the real resolver picks Claude Code from the run profile; nothing is injected."""

    def execute(self, run_id, prompt="Say exactly: complete", sandbox="read-only", **kwargs):
        return execute_coding_agent_service(run_id, prompt, str(self.repo), sandbox, None, {}, ledger=ledger, **kwargs)

    def test_profile_model_reaches_the_cli(self):
        run_id = self.run_id(model="claude-sonnet-5-5")
        result = self.execute(run_id)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.coding_agent, "claude_code")
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        events = ledger.list_events(run_id)
        self.assertEqual(provider_for_events(run_id, events).identity, "claude_code")
        self.assertEqual(coding_agent_session_details(run_id, events)["session_id"],
                         result.outcome.native_session.session_id)

    def test_codex_runs_never_reach_claude(self):
        run_id = ledger.create_run("Say exactly: complete")
        self.assertTrue(select_run_execution_profile(run_id, RunExecutionProfile(sandbox="read-only"), ledger=ledger).ok)
        self.assertEqual(provider_for_events(run_id, ledger.list_events(run_id)).identity, "codex")
        import shutil
        self.assertIsNone(shutil.which("codex"))  # guard: never the real Codex
        result = self.execute(run_id)
        self.assertEqual(result.coding_agent, "codex")
        self.assertEqual(result.reason_code, "codex_not_found")
        self.assertFalse(self.argv_path.exists())


class ModelSelectionTests(unittest.TestCase):
    def test_each_agent_has_its_own_models(self):
        from agent.claude_code_models import CLAUDE_MODEL_IDS

        for model in ("codex_default", *CLAUDE_MODEL_IDS):
            self.assertEqual(RunExecutionProfile(sandbox="read-only", coding_agent="claude_code", model=model).model, model)
        for model in ("gpt-5", "gpt-5-codex", "claude_default", "opus", "sonnet", "haiku", "claude-mythos-5-2"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                RunExecutionProfile(sandbox="read-only", coding_agent="claude_code", model=model)
        for model in ("codex_default", "gpt-5", "gpt-5-codex"):
            self.assertEqual(RunExecutionProfile(sandbox="read-only", model=model).model, model)
            self.assertEqual(RunExecutionProfile(sandbox="read-only", coding_agent="cursor", model=model).model, model)
        for agent in ("codex", "cursor"):
            with self.subTest(agent=agent), self.assertRaises(ValueError):
                RunExecutionProfile(sandbox="read-only", coding_agent=agent, model="claude-opus-5-5")
        with self.assertRaises(ValueError):
            RunExecutionProfile(sandbox="read-only", coding_agent="unknown")

    def test_effort_is_per_agent_and_per_model(self):
        ok = RunExecutionProfile(sandbox="read-only", coding_agent="claude_code", model="claude-fable-5-2",
                                 reasoning_effort="xhigh")
        self.assertEqual(ok.reasoning_effort, "xhigh")
        RunExecutionProfile(sandbox="read-only", coding_agent="claude_code", reasoning_effort="max")  # default model
        for model, effort in (("claude-haiku-4-5", "low"), ("claude-opus-4-6", "xhigh"), ("claude-sonnet-4-6", "xhigh"),
                              ("claude-opus-5-5", "ultra")):
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                RunExecutionProfile(sandbox="read-only", coding_agent="claude_code", model=model, reasoning_effort=effort)
        RunExecutionProfile(sandbox="read-only", coding_agent="claude_code", model="claude-opus-4-6", reasoning_effort="max")
        with self.assertRaises(ValueError):
            RunExecutionProfile(sandbox="read-only", reasoning_effort="high")  # Codex stays locked

    def test_ledger_stores_and_reads_back_claude_models(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(ledger, "DB_PATH", Path(temp) / "ledger.db"):
            run_id = ledger.create_run("x")
            self.assertTrue(select_run_execution_profile(
                run_id, RunExecutionProfile(sandbox="workspace-write", coding_agent="claude_code",
                                            model="claude-fable-5-1", reasoning_effort="high"),
                ledger=ledger).ok)
            profile = get_run_execution_profile(run_id, ledger=ledger).profile
            self.assertEqual((profile.coding_agent, profile.model, profile.reasoning_effort, profile.sandbox),
                             ("claude_code", "claude-fable-5-1", "high", "workspace-write"))

            codex_run = ledger.create_run("y")
            bad = ledger.bind_run_execution_profile(codex_run, "read-only", "claude-opus-5-5", "codex_default",
                                                    "codex_default", "system_default", coding_agent="codex")
            self.assertEqual(bad.status, ledger.AtomicExecutionProfileStatus.INVALID)
            bad = ledger.bind_run_execution_profile(codex_run, "read-only", "claude-haiku-4-5", "low",
                                                    "codex_default", "system_default", coding_agent="claude_code")
            self.assertEqual(bad.status, ledger.AtomicExecutionProfileStatus.INVALID)


class ResolverTests(unittest.TestCase):
    def test_resolver(self):
        self.assertIsInstance(resolve_provider("claude_code"), ClaudeCodeProvider)
        with self.assertRaises(ProviderResolutionError) as raised:
            resolve_provider("cursor")
        self.assertEqual(raised.exception.reason_code, "coding_agent_unavailable")


if __name__ == "__main__":
    unittest.main()
