from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.local_controller import LocalControllerOperationResult, LocalControllerSession
from agent.local_server import (
    LOCAL_SERVER_BIND_HOST,
    LOCAL_SERVER_TOKEN_HEADER,
    LocalControllerServer,
)
from tests.local_socket_test_support import LocalSocketBindingTestCase, requires_localhost_ephemeral_bind


class FakeController:
    def __init__(self) -> None:
        self.session = LocalControllerSession()
        self._lock = threading.Lock()
        self.action_running = False
        self.pending_approval_available = False
        self.start_calls: list[dict] = []
        self.state_calls = 0
        self.approval_calls: list[str] = []
        self.tick_calls = 0
        self.start_result = self._result(ok=True, reason="started", run_id="run-1")
        self.state_result = self._result(ok=False, reason="no_active_run")
        self.approval_result = self._result(ok=True, reason="approval_worker_started", run_id="run-1")
        self.tick_result = self._result(ok=True, reason="routine_worker_started", run_id="run-1")

    def _runtime_snapshot_locked(self) -> dict:
        return {
            "controller_state": self.session.controller_state,
            "active_run_id": self.session.active_run_id,
            "pending_approval_available": self.pending_approval_available,
            "pending_approval_kind": None,
            "session_age_seconds": 0,
            "action_running": self.action_running,
            "current_action_kind": None,
            "current_action_started_at": None,
            "last_action_result_summary": None,
            "last_exception_summary": None,
            "automatic_burst_count": 0,
            "automatic_burst_reason": None,
        }

    def _result(self, *, ok: bool, reason: str, run_id: str | None = None, error: str | None = None):
        return LocalControllerOperationResult(
            ok=ok,
            reason_code=reason,
            error_message=error,
            run_id=run_id,
            controller_state=self.session.controller_state,
            read_model={
                "run_id": run_id,
                "controller_runtime": self._runtime_snapshot_locked(),
            } if run_id else None,
        )

    def start_run(self, **kwargs):
        self.start_calls.append(kwargs)
        return self.start_result

    def get_current_state(self):
        self.state_calls += 1
        return self.state_result

    def submit_approval_decision(self, decision: str):
        self.approval_calls.append(decision)
        return self.approval_result

    def request_automatic_progress(self):
        self.tick_calls += 1
        return self.tick_result


class LocalServerHTTPTestCase(LocalSocketBindingTestCase):
    def setUp(self) -> None:
        self.controller = FakeController()
        self.server = LocalControllerServer(controller=self.controller, port=0)
        self.server.start()
        self.token = self.controller.session.token

    def tearDown(self) -> None:
        self.server.shutdown()

    def request(
        self,
        method: str,
        path: str,
        body: object | bytes | None = None,
        *,
        token: str | None = None,
        host: str | None = None,
        origin: str | None = None,
        content_type: str | None = "application/json",
        cookie: str | None = None,
    ):
        headers = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if token is not None:
            headers[LOCAL_SERVER_TOKEN_HEADER] = token
        if origin is not None:
            headers["Origin"] = origin
        if cookie is not None:
            headers["Cookie"] = cookie
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            if content_type is not None:
                headers["Content-Type"] = content_type
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, dict(response.getheaders()), payload

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
    ):
        headers = {"Host": host or f"127.0.0.1:{self.server.port}"}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        return response.status, dict(response.getheaders()), raw


class LocalServerLifecycleTests(unittest.TestCase):
    @requires_localhost_ephemeral_bind
    def test_lifecycle_host_port_shutdown_and_bootstrap_url(self) -> None:
        server = LocalControllerServer(port=0)
        self.assertEqual(server.host, LOCAL_SERVER_BIND_HOST)
        server.start()
        try:
            self.assertGreater(server.port, 0)
            url = server.bootstrap_url()
            self.assertIn(f"http://127.0.0.1:{server.port}/#token=", url)
            self.assertNotIn("?token=", url)
        finally:
            server.shutdown()
            server.shutdown()

    def test_rejects_non_local_bind_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            LocalControllerServer(host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            LocalControllerServer(host="192.168.1.2")


class LocalServerAuthHostAndHeaderTests(LocalServerHTTPTestCase):
    def test_authentication_required_for_every_api_endpoint(self) -> None:
        for method, path in (
            ("GET", "/api/health"),
            ("GET", "/api/session"),
            ("GET", "/api/runs/current"),
            ("POST", "/api/runs/start"),
            ("POST", "/api/approval"),
            ("POST", "/api/tick"),
        ):
            with self.subTest(path=path):
                status, _headers, payload = self.request(method, path, body={} if method == "POST" else None)
                self.assertEqual(status, 401)
                self.assertEqual(payload["reason_code"], "authentication_required")

    def test_invalid_token_and_query_body_cookie_tokens_are_rejected(self) -> None:
        status, _headers, payload = self.request("GET", f"/api/health?token={self.token}", token="bad")
        self.assertEqual(status, 401)
        self.assertEqual(payload["reason_code"], "authentication_failed")

        status, _headers, payload = self.request("GET", "/api/health", cookie=f"token={self.token}")
        self.assertEqual(status, 401)
        self.assertEqual(payload["reason_code"], "authentication_required")

        status, _headers, payload = self.request("POST", "/api/tick", body={"token": self.token}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["reason_code"], "authentication_required")

    def test_valid_token_succeeds_and_token_is_not_in_response(self) -> None:
        status, _headers, payload = self.request("GET", "/api/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn(self.token, json.dumps(payload))

    def test_host_origin_and_no_cors_headers(self) -> None:
        status, _headers, payload = self.request("GET", "/api/health", token=self.token, host=f"localhost:{self.server.port}")
        self.assertEqual(status, 200)

        status, _headers, payload = self.request("GET", "/api/health", token=self.token, host="example.com")
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "invalid_host")

        status, _headers, payload = self.request(
            "POST",
            "/api/tick",
            body={},
            token=self.token,
            origin=f"http://127.0.0.1:{self.server.port}",
        )
        self.assertIn(status, {200, 202})

        status, _headers, payload = self.request("POST", "/api/tick", body={}, token=self.token, origin="https://evil.test")
        self.assertEqual(status, 403)
        self.assertEqual(payload["reason_code"], "invalid_origin")

        status, headers, _payload = self.request("GET", "/api/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_json_headers_are_set(self) -> None:
        status, headers, _payload = self.request("GET", "/api/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")


class LocalServerEndpointTests(LocalServerHTTPTestCase):
    def test_health_session_and_current_state_are_safe_and_read_only(self) -> None:
        status, _headers, health = self.request("GET", "/api/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(health["bind_host"], "127.0.0.1")
        self.assertEqual(health["port"], self.server.port)
        self.assertNotIn("token", health)

        status, _headers, session = self.request("GET", "/api/session", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(session["session_id"], self.controller.session.session_id)
        self.assertNotIn("token", session)
        self.assertNotIn("pending_approval", session)

        status, _headers, state = self.request("GET", "/api/runs/current", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.state_calls, 1)
        self.assertEqual(self.controller.start_calls, [])

    def test_start_route_success_and_conflicts(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/start",
            body={"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "read-only"},
            token=self.token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(len(self.controller.start_calls), 1)
        self.assertEqual(self.controller.start_calls[0]["sandbox"], "read-only")

        self.controller.start_result = self.controller._result(
            ok=False,
            reason="active_run_exists",
            run_id="run-1",
            error="active",
        )
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/start",
            body={"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "read-only"},
            token=self.token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason_code"], "active_run_exists")

    def test_start_request_validation_rejections(self) -> None:
        cases = [
            (b"{", "invalid_json", 400, "application/json"),
            ([], "invalid_request_shape", 400, "application/json"),
            ({"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "read-only", "extra": True}, "unexpected_request_fields", 400, "application/json"),
            ({"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "read-only"}, "unsupported_content_type", 415, "text/plain"),
        ]
        for body, reason, status_code, content_type in cases:
            with self.subTest(reason=reason):
                status, _headers, payload = self.request(
                    "POST",
                    "/api/runs/start",
                    body=body,
                    token=self.token,
                    content_type=content_type,
                )
                self.assertEqual(status, status_code)
                self.assertEqual(payload["reason_code"], reason)

        large = {"repository_path": "/tmp/repo", "initial_instruction": "x" * (70 * 1024), "sandbox": "read-only"}
        status, _headers, payload = self.request("POST", "/api/runs/start", body=large, token=self.token)
        self.assertEqual(status, 413)
        self.assertEqual(payload["reason_code"], "request_body_too_large")

        self.controller.start_result = self.controller._result(
            ok=False,
            reason="danger_full_access_not_available_in_local_controller",
            error="blocked",
        )
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/start",
            body={"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "danger-full-access"},
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "danger_full_access_not_available_in_local_controller")

    def test_start_handler_returns_before_worker_completion_when_controller_does(self) -> None:
        entered = threading.Event()
        released = threading.Event()

        def start_run(**kwargs):
            self.controller.start_calls.append(kwargs)
            entered.set()
            threading.Thread(target=lambda: released.wait(1), daemon=True).start()
            return self.controller._result(ok=True, reason="started", run_id="run-1")

        self.controller.start_run = start_run
        start = time.monotonic()
        status, _headers, _payload = self.request(
            "POST",
            "/api/runs/start",
            body={"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "read-only"},
            token=self.token,
        )
        elapsed = time.monotonic() - start
        released.set()
        self.assertEqual(status, 202)
        self.assertTrue(entered.is_set())
        self.assertLess(elapsed, 0.5)

    def test_approval_route(self) -> None:
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision):
                status, _headers, payload = self.request("POST", "/api/approval", body={"decision": decision}, token=self.token)
                self.assertEqual(status, 202)
                self.assertEqual(self.controller.approval_calls[-1], decision)

        status, _headers, payload = self.request("POST", "/api/approval", body={"decision": "yes"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "invalid_approval_decision")

        status, _headers, payload = self.request("POST", "/api/approval", body={"decision": "approved", "planner_action": "x"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "unexpected_request_fields")

        self.controller.approval_result = self.controller._result(ok=False, reason="no_pending_approval", error="none")
        status, _headers, payload = self.request("POST", "/api/approval", body={"decision": "approved"}, token=self.token)
        self.assertEqual(status, 409)

        self.controller.approval_result = self.controller._result(ok=False, reason="action_already_running", error="running")
        status, _headers, payload = self.request("POST", "/api/approval", body={"decision": "approved"}, token=self.token)
        self.assertEqual(status, 409)

    def test_tick_route(self) -> None:
        status, _headers, payload = self.request("POST", "/api/tick", body={}, token=self.token)
        self.assertEqual(status, 202)
        self.assertEqual(self.controller.tick_calls, 1)

        status, _headers, payload = self.request("POST", "/api/tick", body=None, token=self.token, content_type=None)
        self.assertEqual(status, 202)

        self.controller.tick_result = self.controller._result(ok=False, reason="no_routine_action_available", error="idle")
        status, _headers, payload = self.request("POST", "/api/tick", body={}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["reason_code"], "no_routine_action_available")

        status, _headers, payload = self.request("POST", "/api/tick", body={"extra": True}, token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "unexpected_request_fields")

    def test_routing_methods_static_root_and_no_default_log_leaks(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            status, headers, raw = self.request_raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertNotIn(self.token, stderr.getvalue())

        status, headers, raw = self.request_raw("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")

        status, _headers, payload = self.request("GET", "/api/missing", token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["reason_code"], "route_not_found")

        status, _headers, payload = self.request("PUT", "/api/health", token=self.token)
        self.assertEqual(status, 405)
        self.assertEqual(payload["reason_code"], "method_not_allowed")

    def test_explicit_non_actions(self) -> None:
        import agent.local_server as local_server

        with (
            mock.patch("subprocess.run") as subprocess_run,
            mock.patch("agent.codex_services.execute_codex_direct_service") as codex,
            mock.patch("agent.chatgpt_services.submit_feedback_to_chatgpt_service") as submit,
            mock.patch("agent.chatgpt_services.capture_chatgpt_response_service") as capture,
            mock.patch("agent.supervision_services.run_supervision_step") as supervise,
        ):
            self.request("GET", "/api/health", token=self.token)
            self.request("POST", "/api/runs/start", body={"repository_path": "/tmp/repo", "initial_instruction": "Task", "sandbox": "read-only"}, token=self.token)
            self.request("POST", "/api/approval", body={"decision": "approved"}, token=self.token)
            self.request("POST", "/api/tick", body={}, token=self.token)

        subprocess_run.assert_not_called()
        codex.assert_not_called()
        submit.assert_not_called()
        capture.assert_not_called()
        supervise.assert_not_called()
        self.assertFalse(hasattr(local_server, "Access_Control_Allow_Origin"))
        self.assertNotIn("agent.cli", local_server.__dict__)


if __name__ == "__main__":
    unittest.main()
