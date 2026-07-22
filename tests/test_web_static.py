from __future__ import annotations

import http.client
import re
import unittest
from pathlib import Path

from agent.local_controller import LocalControllerSession
from agent.local_server import LocalControllerServer
from tests.local_socket_test_support import LocalSocketBindingTestCase


STATIC_DIR = Path(__file__).resolve().parents[1] / "agent" / "web_static"
INDEX = STATIC_DIR / "index.html"
STYLE = STATIC_DIR / "style.css"
APP = STATIC_DIR / "app.js"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


class StaticServerTests(LocalSocketBindingTestCase):
    def setUp(self) -> None:
        self.server = LocalControllerServer(port=0)
        self.server.start()

    def tearDown(self) -> None:
        self.server.shutdown()

    def request_raw(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        headers = {"Host": f"127.0.0.1:{self.server.port}"}
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        return response.status, dict(response.getheaders()), raw.decode("utf-8", errors="replace")

    def test_static_assets_are_served_with_safe_headers(self) -> None:
        cases = [
            ("/", "text/html; charset=utf-8", "<main"),
            ("/assets/style.css", "text/css; charset=utf-8", ".panel"),
            ("/assets/app.js", "application/javascript; charset=utf-8", "getCurrentState"),
        ]
        for path, content_type, marker in cases:
            with self.subTest(path=path):
                status, headers, body = self.request_raw(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], content_type)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn(marker, body)

    def test_unknown_static_and_path_traversal_are_safe_404(self) -> None:
        for path in ("/assets/missing.js", "/assets/../local_server.py", "/agent/web_static/app.js"):
            with self.subTest(path=path):
                status, headers, body = self.request_raw(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
                self.assertIn("route_not_found", body)

    def test_api_authentication_remains_required(self) -> None:
        status, headers, body = self.request_raw("/api/health")
        self.assertEqual(status, 401)
        self.assertIn("authentication_required", body)

    def test_bootstrap_url_uses_fragment_only(self) -> None:
        url = self.server.bootstrap_url()
        self.assertIn("/#token=", url)
        self.assertNotIn("?token=", url)


class StaticSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.js = APP.read_text(encoding="utf-8")
        cls.all_static = "\n".join([cls.html, cls.css, cls.js])

    def test_packaging_includes_static_assets(self) -> None:
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        self.assertIn("[tool.setuptools.package-data]", pyproject)
        self.assertIn('agent = ["web_static/*"]', pyproject)

    def test_html_ui_contract_and_execution_profile_controls(self) -> None:
        for required in (
            'id="startup-panel"',
            'id="repository-path"',
            'id="repository-browse-button"',
            'id="repository-picker-status"',
            'id="initial-task"',
            'id="default-greeting-button"',
            'id="default-greeting-status"',
            'id="project-title"',
            'id="chat-title"',
            'id="sandbox-select"',
            'id="permission-preset-description"',
            'id="model-select"',
            'id="reasoning-lock"',
            'id="approval-lock"',
            'id="start-button"',
            'id="active-run-panel"',
            'id="run-project-title"',
            'id="run-chat-title"',
            'id="run-destination-state"',
            'id="run-sandbox"',
            'id="run-model"',
            'id="run-reasoning"',
            'id="run-approval"',
            'id="run-profile-source"',
            'id="chatgpt-ui-lease-panel"',
            'id="lease-state"',
            'id="lease-owner-run"',
            'id="lease-owner-pid"',
            'id="lease-acquired-at"',
            'id="lease-active-event-id"',
            'id="lease-token-sha256"',
            'id="lease-owner-run-status"',
            'id="lease-owner-pid-state"',
            'id="lease-release-allowed"',
            'id="lease-confirm-stale"',
            'id="lease-release-reason"',
            'id="lease-allow-owner-pid-alive"',
            'id="lease-release-button"',
            'id="codex-live-panel"',
            'id="codex-live-state"',
            'id="codex-live-final"',
            'id="codex-live-error"',
            'id="codex-live-events"',
            'id="event-timeline-details"',
            'id="approval-panel"',
            'id="approve-button"',
            'id="reject-button"',
            'id="progress-panel"',
            'id="tick-button"',
            'id="event-timeline"',
            'id="connection-status"',
        ):
            self.assertIn(required, self.html)

        self.assertIn("Codex Permission Preset", self.html)
        self.assertIn(
            "Codex can inspect the workspace. Edits are not allowed for this dashboard run.",
            self.html,
        )
        self.assertIn(
            "Codex can edit files in this repository. Outside-workspace and dangerous access remain blocked by this dashboard run.",
            self.js,
        )
        self.assertIn("Permission preset / sandbox", self.html)
        self.assertIn("Codex default — Full Access bypasses approvals", self.all_static)
        options = re.findall(r'<option value="([^"]*)">', self.html)
        self.assertEqual(options, ["", ""])
        self.assertIn("Loading...", self.html)
        self.assertIn("currently open ChatGPT Desktop destination", self.html)
        self.assertIn("exact titles", self.html)
        self.assertIn('requestJson("GET", "/api/execution-profile/options")', self.js)
        self.assertIn("populateProfileOptions(result)", self.js)
        self.assertIn("project_title: projectTitle", self.js)
        self.assertIn("chat_title: chatTitle", self.js)
        self.assertIn('!elements["project-title"].value.trim()', self.js)
        self.assertIn('!elements["chat-title"].value.trim()', self.js)
        self.assertIn("sandbox,", self.js)
        self.assertIn("model,", self.js)
        self.assertIn('ALLOWED_PERMISSION_PRESET_VALUES = new Set(["read-only", "workspace-write", "danger-full-access"])', self.js)
        self.assertIn("safePermissionPresetOptions(profileOptions.sandbox_options)", self.js)
        self.assertNotIn("gpt-5-codex", self.all_static)
        self.assertNotIn("gpt-5", self.all_static)
        self.assertIsNone(re.search(r"\son\w+=", self.html))

    def test_dashboard_renders_locked_and_run_profile_states_truthfully(self) -> None:
        for required in (
            'setText(elements["reasoning-lock"]',
            'elements["approval-lock"]',
            "optionLabel(locked.approval_policy",
            'profile.status === "invalid"',
            "Invalid profile history",
            'profile.status === "legacy_compatibility"',
            "Legacy/default compatibility",
            'profile.model === "codex_default" ? "Codex default"',
            'profile.reasoning_effort === "codex_default"',
            'profile.approval_policy === "codex_default"',
            "permissionPresetSummary(profile.sandbox)",
            'setText(elements["run-sandbox"], sandbox)',
            'setText(elements["run-model"], model)',
            'setText(elements["run-profile-source"], source)',
        ):
            self.assertIn(required, self.js)

        self.assertNotIn("reasoning_effort:", self.js)
        self.assertNotIn("approval_policy:", self.js)

    def test_dashboard_renders_chatgpt_ui_lease_state_and_guarded_controls(self) -> None:
        for required in (
            "renderChatGPTUILease(result)",
            "No active lease",
            "Active lease",
            "Invalid lease history",
            'setText(elements["lease-owner-run"], details.ownerRun)',
            'setText(elements["lease-owner-pid"], details.ownerPid)',
            'setText(elements["lease-acquired-at"], details.acquiredAt)',
            'setText(elements["lease-active-event-id"], details.eventId)',
            'setText(elements["lease-token-sha256"], details.tokenSha)',
            'setText(elements["lease-owner-run-status"], details.runStatus)',
            'setText(elements["lease-owner-pid-state"], details.pidState)',
            'setText(elements["lease-release-allowed"], details.releaseAllowed)',
            'elements["lease-confirm-stale"].checked',
            'elements["lease-release-reason"].value.trim()',
            'elements["lease-allow-owner-pid-alive"].checked',
            "PID-reuse override",
            "expected_lease_token_sha256: lease.lease_token_sha256",
            "expected_run_status: lease.owning_run_status || null",
            "confirm_stale: confirmStale",
            "allow_owner_pid_alive: allowOwnerPidAlive",
            "STALE_LEASE_RECOVERABLE_RUN_STATUSES",
        ):
            self.assertIn(required, self.all_static)

        self.assertIn("lease_token_sha256", self.js)
        self.assertNotIn("raw_lease_token", self.all_static)
        self.assertNotIn("lease_token:", self.js)

    def test_dashboard_renders_destination_binding_states_truthfully(self) -> None:
        for required in (
            "renderDestinationBinding(model)",
            'binding.status === "present"',
            "Bound and valid",
            'binding.status === "missing"',
            "No autonomous destination binding",
            "Invalid / contradictory",
            'setText(elements["run-project-title"], projectTitle)',
            'setText(elements["run-chat-title"], chatTitle)',
            'setText(elements["run-destination-state"], state)',
        ):
            self.assertIn(required, self.js)

    def test_operator_navigation_toggle_and_phase_visibility(self) -> None:
        # The toggle exists, is a checkbox, and is unchecked (disabled) by default.
        self.assertIn(
            '<input id="allow-destination-navigation" name="allow_destination_navigation" type="checkbox">',
            self.html,
        )
        self.assertIn('id="run-navigation-approved"', self.html)
        self.assertIn('id="run-handoff-phase"', self.html)

        # The start payload carries the explicit, defaulted-false operator flag.
        self.assertIn(
            'const allowDestinationNavigation = Boolean(elements["allow-destination-navigation"].checked);',
            self.js,
        )
        self.assertIn("allow_destination_navigation: allowDestinationNavigation", self.js)

        # The bounded phase state and navigation approval are surfaced read-only.
        self.assertIn("renderNavigationApproval(model)", self.js)
        self.assertIn("renderHandoffPhase(model)", self.js)
        self.assertIn("model.allow_destination_navigation", self.js)
        self.assertIn("model.latest_handoff_phase", self.js)
        self.assertIn("phase.navigation_outcome", self.js)

    def test_token_safety_contract(self) -> None:
        session = LocalControllerSession()
        self.assertNotIn(session.token, self.all_static)
        self.assertNotIn("localStorage", self.js)
        self.assertNotIn("sessionStorage", self.js)
        self.assertNotIn("document.cookie", self.js)
        self.assertNotIn("?token=", self.js)
        self.assertIn('"X-Controller-Token"', self.js)
        self.assertIn("history.replaceState(null, \"\", window.location.pathname + window.location.search)", self.js)
        self.assertNotIn("console.log", self.js)
        self.assertNotIn("console.error", self.js)

    def test_no_unsafe_rendering_external_assets_or_frameworks(self) -> None:
        self.assertNotIn("innerHTML", self.js)
        self.assertIn("danger-full-access", self.all_static)
        self.assertNotIn("WebSocket", self.js)
        self.assertNotIn("EventSource", self.js)
        self.assertNotIn("clipboard", self.all_static.lower())
        self.assertNotIn("navigator.", self.js)
        self.assertNotIn("accessibility", self.all_static.lower())
        self.assertNotIn("AXUIElement", self.all_static)
        self.assertNotIn("verify now", self.all_static.lower())
        self.assertNotIn("React", self.all_static)
        self.assertNotIn("Vue", self.all_static)
        self.assertNotIn("Svelte", self.all_static)
        self.assertNotIn("https://", self.all_static)
        self.assertNotIn("http://", self.all_static)
        self.assertNotIn("cancel", self.all_static.lower())
        self.assertNotIn("stdout", self.js.lower())
        self.assertNotIn("stderr", self.js.lower())
        self.assertNotIn("prompt_text", self.js)

    def test_polling_and_api_contract(self) -> None:
        self.assertIn("stateRequestInFlight", self.js)
        self.assertIn("POLL_RUNNING_MS = 1000", self.js)
        self.assertIn("POLL_ACTIVE_MS = 2000", self.js)
        self.assertIn("POLL_IDLE_MS = 5000", self.js)
        self.assertIn("POLL_FAILURE_MAX_MS = 10000", self.js)
        self.assertIn('requestJson("GET", "/api/runs/current")', self.js)
        self.assertIn('requestJson("POST", "/api/runs/start", payload)', self.js)
        self.assertIn('requestJson("POST", "/api/repository/pick", {})', self.js)
        self.assertIn('requestJson("GET", "/api/default-greeting")', self.js)
        self.assertIn('requestJson("POST", "/api/approval", { decision })', self.js)
        self.assertIn('requestJson("POST", "/api/tick", {})', self.js)
        self.assertIn('requestJson("GET", "/api/chatgpt-ui-lease")', self.js)
        self.assertIn('requestJson("POST", "/api/chatgpt-ui-lease/release-stale", payload)', self.js)
        self.assertIn('requestJson("GET", `/api/runs/current/progress?after_sequence=${cursor}`)', self.js)
        self.assertIn('fetch(`/api/runs/current/events?after_sequence=${cursor}`', self.js)
        self.assertIn("renderCodexLiveProgress(runtime)", self.js)
        self.assertIn('event.kind === "assistant_commentary"', self.js)
        self.assertIn(".slice(-PROGRESS_EVENT_RENDER_LIMIT)", self.js)
        self.assertIn("Codex Working Notes", self.html)
        self.assertIn("Raw event timeline", self.html)
        self.assertNotIn('<details id="event-timeline-details" open>', self.html)
        self.assertIn("final_message_available", self.js)
        self.assertIn("pollingStopped = true", self.js)
        self.assertNotRegex(self.js, r"set(?:Timeout|Interval)\([^)]*requestTick")
        self.assertNotRegex(self.js, r"set(?:Timeout|Interval)\([^)]*startRun")
        self.assertNotRegex(self.js, r"set(?:Timeout|Interval)\([^)]*submitApproval")


if __name__ == "__main__":
    unittest.main()
