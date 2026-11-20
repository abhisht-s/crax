"""Step 3: choosing the coding agent for a new run (server, controller, CLI, dashboard).

No agent runs here except a fake `claude` CLI; the real CLIs are never on PATH.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent import cli, ledger
from agent.local_controller import (
    LocalControllerSession,
    start_local_controller_run,
    validate_local_controller_start_request,
)
from agent.local_server import LocalServerError, _execution_profile_options_payload, _start_run_kwargs
from agent.run_services import get_run_execution_profile
from tests.test_claude_code_provider import FAKE_CLAUDE_SOURCE

WEB = Path(__file__).resolve().parent.parent / "agent" / "web_static"


def _payload(**extra):
    return {
        "repository_path": "/tmp/repo",
        "initial_instruction": "Task",
        "project_title": "Project",
        "chat_title": "Chat",
        "sandbox": "read-only",
        **extra,
    }


class ServerStartRequestTests(unittest.TestCase):
    def test_coding_agent_and_its_model_pass_through(self):
        kwargs = _start_run_kwargs(_payload(coding_agent="claude_code", model="claude-fable-5-2", reasoning_effort="high"))
        self.assertEqual((kwargs["coding_agent"], kwargs["model"], kwargs["reasoning_effort"]),
                         ("claude_code", "claude-fable-5-2", "high"))
        kwargs = _start_run_kwargs(_payload(coding_agent="codex", model="gpt-5"))
        self.assertEqual((kwargs["coding_agent"], kwargs["model"]), ("codex", "gpt-5"))

    def test_omitted_agent_is_unchanged_codex_request(self):
        kwargs = _start_run_kwargs(_payload(model="gpt-5-codex"))
        self.assertNotIn("coding_agent", kwargs)
        self.assertEqual(kwargs["model"], "gpt-5-codex")

    def test_bad_agent_or_wrong_agent_model_is_refused(self):
        cases = [
            (_payload(coding_agent="cursor"), "invalid_coding_agent"),
            (_payload(coding_agent="Claude"), "invalid_coding_agent"),
            (_payload(coding_agent=None), "invalid_coding_agent"),
            (_payload(coding_agent="claude_code", model="gpt-5"), "invalid_claude_code_model"),
            (_payload(coding_agent="claude_code", model="opus"), "invalid_claude_code_model"),
            (_payload(coding_agent="claude_code", model="claude-haiku-4-5", reasoning_effort="low"), "invalid_reasoning_effort"),
            (_payload(coding_agent="claude_code", model="claude-opus-4-6", reasoning_effort="xhigh"), "invalid_reasoning_effort"),
            (_payload(coding_agent="claude_code", reasoning_effort="ultra"), "invalid_reasoning_effort"),
            (_payload(coding_agent="codex", reasoning_effort="high"), "invalid_reasoning_effort"),
            (_payload(coding_agent="codex", model="opus"), "invalid_codex_model"),
            (_payload(model="opus"), "invalid_codex_model"),
        ]
        for payload, reason in cases:
            with self.subTest(payload=payload), self.assertRaises(LocalServerError) as raised:
                _start_run_kwargs(payload)
            self.assertEqual(raised.exception.reason_code, reason)

    def test_options_list_agents_availability_and_models(self):
        installed = {"codex": "/fake/codex"}
        with mock.patch("agent.local_server.shutil.which", side_effect=lambda name: installed.get(name)):
            payload = _execution_profile_options_payload()
        agents = {option["value"]: option for option in payload["coding_agent_options"]}
        self.assertEqual(list(agents), ["codex", "claude_code"])
        self.assertEqual(agents["claude_code"]["label"], "Claude Code")
        self.assertTrue(agents["codex"]["available"])
        self.assertFalse(agents["claude_code"]["available"])
        self.assertIn("claude", agents["claude_code"]["description"])
        claude_models = payload["model_options_by_agent"]["claude_code"]
        from agent.claude_code_models import CLAUDE_MODEL_IDS

        self.assertEqual([m["value"] for m in claude_models], ["codex_default", *CLAUDE_MODEL_IDS])
        fable = next(m for m in claude_models if m["value"] == "claude-fable-5-2")
        self.assertEqual((fable["label"], fable["group"]), ("Fable 5.2", "main"))
        self.assertEqual(next(m for m in claude_models if m["value"] == "claude-haiku-4-5")["efforts"], [])
        self.assertEqual([m["value"] for m in claude_models if m.get("group") == "main"],
                         ["codex_default", "claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5"])
        efforts = [e["value"] for e in payload["reasoning_effort_options_by_agent"]["claude_code"]]
        self.assertEqual(efforts, ["codex_default", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual([e["value"] for e in payload["reasoning_effort_options_by_agent"]["codex"]], ["codex_default"])
        self.assertEqual(claude_models[0]["value"], "codex_default")
        self.assertEqual([m["value"] for m in payload["model_options"]],
                         [m["value"] for m in payload["model_options_by_agent"]["codex"]])
        self.assertEqual(payload["model_options"][0]["label"], "Codex default")


class DefaultModelLabelTests(unittest.TestCase):
    def test_claude_default_label_names_the_users_model(self):
        with tempfile.TemporaryDirectory() as config, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config}):
            payload = _execution_profile_options_payload()
            self.assertEqual(payload["model_options_by_agent"]["claude_code"][0]["label"], "Default (Claude Code built-in)")
            (Path(config) / "settings.json").write_text(json.dumps({"model": "opus", "effortLevel": "xhigh"}))
            payload = _execution_profile_options_payload()
            self.assertEqual(payload["model_options_by_agent"]["claude_code"][0]["label"],
                             "Default (your Claude setting: opus)")
            self.assertEqual(payload["reasoning_effort_options_by_agent"]["claude_code"][0]["label"],
                             "Default (your Claude setting: xhigh)")
            self.assertEqual(payload["model_options_by_agent"]["codex"][0]["label"], "Codex default")


class AutoModePresetTests(unittest.TestCase):
    """Claude Code's autonomous preset (Codex: Full Access) is auto mode."""

    def test_claude_presets_are_labelled_for_auto_mode(self):
        payload = _execution_profile_options_payload()
        by_agent = payload["sandbox_options_by_agent"]
        self.assertEqual(by_agent["codex"], payload["sandbox_options"])  # Codex unchanged
        claude = {option["value"]: option for option in by_agent["claude_code"]}
        self.assertEqual(claude["danger-full-access"]["label"], "Auto Mode (Autonomous)")
        self.assertIn("auto mode", claude["danger-full-access"]["description"])
        self.assertNotIn("without filesystem", claude["danger-full-access"]["description"])
        presentation = payload["agent_presentation"]
        self.assertEqual(presentation["codex"]["autonomous_button_label"], "Use Full Access")
        self.assertEqual(presentation["claude_code"]["autonomous_button_label"], "Use Auto Mode")
        self.assertEqual(presentation["codex"]["approval_policy_label"], payload["locked"]["approval_policy"]["label"])
        self.assertIn("Auto Mode", presentation["claude_code"]["approval_policy_label"])

    def test_remote_device_without_permission_sees_no_autonomous_preset_for_any_agent(self):
        from agent.remote_access import RemotePrincipal

        principal = RemotePrincipal(kind="remote_device", scope="operator", device_id="d1")
        payload = _execution_profile_options_payload(principal=principal)
        for agent, options in payload["sandbox_options_by_agent"].items():
            with self.subTest(agent=agent):
                self.assertNotIn("danger-full-access", [option["value"] for option in options])

    def test_claude_autonomous_run_starts_in_auto_mode(self):
        from agent.claude_code_provider import build_claude_command

        command = build_claude_command("p", sandbox="danger-full-access", session_id="11111111-2222-4333-8444-555555555555")
        self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", command)


class ControllerStartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = str(Path(temp.name).resolve())
        self.enterContext(mock.patch.object(ledger, "DB_PATH", Path(temp.name) / "ledger.db"))

    def validate(self, chat_title="Chat", **kwargs):
        return validate_local_controller_start_request(
            self.repo, "Task", kwargs.pop("sandbox", None), project_title="Project", chat_title=chat_title, **kwargs)

    def test_validation(self):
        ok = self.validate(sandbox="workspace-write", model="claude-sonnet-5-5", coding_agent="claude_code",
                           reasoning_effort="max")
        self.assertTrue(ok.ok, ok.error_message)
        self.assertEqual((ok.coding_agent, ok.model, ok.reasoning_effort, ok.profile_source),
                         ("claude_code", "claude-sonnet-5-5", "max", "explicit_user_selection"))
        self.assertEqual(self.validate(coding_agent="claude_code", model="claude-haiku-4-5",
                                       reasoning_effort="low").reason_code, "invalid_reasoning_effort")
        self.assertEqual(self.validate(reasoning_effort="high").reason_code, "invalid_reasoning_effort")

        default = self.validate()
        self.assertEqual((default.coding_agent, default.profile_source), ("codex", "system_default"))
        self.assertEqual(self.validate(coding_agent="claude_code").profile_source, "explicit_user_selection")

        for kwargs, reason in (
            ({"coding_agent": "cursor"}, "invalid_coding_agent"),
            ({"coding_agent": "claude_code", "model": "gpt-5"}, "invalid_claude_code_model"),
            ({"model": "opus"}, "invalid_codex_model"),
        ):
            with self.subTest(kwargs=kwargs):
                result = self.validate(**kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason)
        self.assertEqual(self.validate(model="opus").error_message,
                         "Invalid Codex model. Allowed values: codex_default, gpt-5, gpt-5-codex.")

    def test_started_run_records_the_chosen_agent(self):
        for agent, model in (("claude_code", "claude-opus-5-5"), ("codex", "gpt-5"), (None, None)):
            with self.subTest(agent=agent):
                # One live run per ChatGPT chat, so each start uses its own chat.
                request = self.validate(chat_title=f"Chat {agent}", sandbox="read-only", model=model, coding_agent=agent)
                result = start_local_controller_run(LocalControllerSession(), request, ledger=ledger)
                self.assertTrue(result.ok, result.error_message)
                profile = get_run_execution_profile(result.run_id, ledger=ledger).profile
                self.assertEqual(profile.coding_agent, agent or "codex")
                self.assertEqual(profile.model, model or "codex_default")


class CliTests(unittest.TestCase):
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
        env = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_CLAUDE_")}
        env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
        # Never read the user's real ~/.claude/settings.json.
        env["CLAUDE_CONFIG_DIR"] = str(self.root / "claude-config")
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.assertEqual(shutil.which("claude"), str(fake))  # guard: never the real CLI
        self.assertIsNone(shutil.which("codex"))
        self.enterContext(mock.patch.object(ledger, "DB_PATH", self.root / "ledger.db"))
        self.enterContext(mock.patch("agent.codex_invocation.default_runs_root", return_value=self.root / "runs"))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(cli.sys, "argv", ["agent", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_plain_start_records_no_profile(self):
        code, out, _ = self.cli("start", "Task")
        self.assertEqual(code, 0)
        self.assertEqual(self.run_count(), 1)  # proves run_count sees runs
        run_id = out.strip()
        self.assertEqual(get_run_execution_profile(run_id, ledger=ledger).profile, None)

    def test_start_with_agent_records_profile(self):
        code, out, err = self.cli("start", "Task", "--coding-agent", "claude_code", "--model", "claude-haiku-5-5",
                                  "--effort", "low",
                                  "--sandbox", "workspace-write")
        self.assertEqual(code, 0, err)
        profile = get_run_execution_profile(out.strip(), ledger=ledger).profile
        self.assertEqual((profile.coding_agent, profile.model, profile.sandbox, profile.profile_source),
                         ("claude_code", "claude-haiku-5-5", "workspace-write", "explicit_user_selection"))
        self.assertEqual(profile.reasoning_effort, "low")

    def test_invalid_combination_creates_no_run(self):
        code, out, err = self.cli("start", "Task", "--coding-agent", "claude_code", "--model", "gpt-5")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("invalid execution profile", err)
        self.assertEqual(self.run_count(), 0)

    def run_count(self):
        import sqlite3

        if not ledger.DB_PATH.exists():
            return 0
        with contextlib.closing(sqlite3.connect(ledger.DB_PATH)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] if "runs" in tables else 0

    def test_codex_run_executes_a_claude_code_run(self):
        code, out, err = self.cli("start", "Say exactly: complete", "--coding-agent", "claude_code")
        self.assertEqual(code, 0, err)
        run_id = out.strip()
        with mock.patch.object(cli, "_codex_run_auto_supervise_exit_code", return_value=None) as supervise:
            code, out, err = self.cli("codex-run", run_id, "--prompt", "Say exactly: complete",
                                      "--repo", str(self.repo), "--no-supervise")
        self.assertEqual(code, 0, err)
        self.assertIn("coding_agent: claude_code", out)
        self.assertIn("claude_path:", out)
        supervise.assert_called_once()
        finished = next(e for e in ledger.list_events(run_id) if e["event_type"] == "codex_exec_finished")
        metadata = json.loads(finished["metadata_json"])
        self.assertEqual(metadata["coding_agent"], "claude_code")
        self.assertEqual(metadata["final_message"], "Claude final answer for: Say exactly: complete")

    def test_codex_run_sandbox_must_match_the_profile(self):
        _, out, _ = self.cli("start", "Task", "--coding-agent", "claude_code", "--sandbox", "workspace-write")
        with mock.patch.object(cli, "_codex_run_auto_supervise_exit_code", return_value=None):
            code, _, err = self.cli("codex-run", out.strip(), "--prompt", "Task", "--repo", str(self.repo),
                                    "--no-supervise")
        self.assertNotEqual(code, 0)
        self.assertIn("sandbox", err.lower())


class DashboardContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (WEB / "index.html").read_text()
        self.js = (WEB / "app.js").read_text()

    def test_every_collected_element_exists(self):
        block = self.js[self.js.index("function collectElements()"):]
        block = block[:block.index("]) {")]
        ids = re.findall(r'"([a-z0-9-]+)"', block)
        self.assertIn("coding-agent-select", ids)
        missing = [element_id for element_id in ids if f'id="{element_id}"' not in self.html]
        self.assertEqual(missing, [])

    def test_agent_picker_contract(self):
        for required in ('id="coding-agent-select"', 'name="coding_agent"', 'id="run-coding-agent"',
                         'id="effort-select"', 'id="model-description"',
                         'id="sandbox-label"', 'id="model-label"', "Codex Permission Preset"):
            self.assertIn(required, self.html)
        for required in ("coding_agent: codingAgent", "model_options_by_agent", "(CLI not found)",
                         "reasoning_effort: reasoningEffort", "reasoning_effort_options_by_agent", "More models",
                         "sandbox_options_by_agent", "autonomous_button_label", "approval_policy_label",
                         "applyCodingAgentSelection", "agentOption.available !== false"):
            self.assertIn(required, self.js)
        for required in ('id="copy-run-id-button"', 'id="copy-session-id-button"', 'aria-label="Copy session ID"',
                         'id="codex-live-session-label"'):
            self.assertIn(required, self.html)
        for required in ("copyIdFrom(", "navigator.clipboard", 'document.execCommand("copy")',
                         'metadata.event_type === "system.init"', "summary.claude_session_id"):
            self.assertIn(required, self.js)
        mutation = self.js[self.js.index("function setMutationDisabled"):]
        self.assertIn('"coding-agent-select"', mutation[:mutation.index("]) {")])


if __name__ == "__main__":
    unittest.main()
