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

    def test_html_ui_contract_and_sandbox_options(self) -> None:
        for required in (
            'id="startup-panel"',
            'id="repository-path"',
            'id="initial-task"',
            'id="sandbox-select"',
            'id="start-button"',
            'id="active-run-panel"',
            'id="approval-panel"',
            'id="approve-button"',
            'id="reject-button"',
            'id="progress-panel"',
            'id="tick-button"',
            'id="event-timeline"',
            'id="connection-status"',
        ):
            self.assertIn(required, self.html)

        options = re.findall(r'<option value="([^"]+)">', self.html)
        self.assertEqual(options, ["read-only", "workspace-write"])
        self.assertIsNone(re.search(r"\son\w+=", self.html))

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
        self.assertNotIn("danger-full-access", self.all_static)
        self.assertNotIn("WebSocket", self.js)
        self.assertNotIn("EventSource", self.js)
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
        self.assertIn('requestJson("POST", "/api/approval", { decision })', self.js)
        self.assertIn('requestJson("POST", "/api/tick", {})', self.js)
        self.assertIn("pollingStopped = true", self.js)
        self.assertNotRegex(self.js, r"set(?:Timeout|Interval)\([^)]*requestTick")
        self.assertNotRegex(self.js, r"set(?:Timeout|Interval)\([^)]*startRun")
        self.assertNotRegex(self.js, r"set(?:Timeout|Interval)\([^)]*submitApproval")


if __name__ == "__main__":
    unittest.main()
