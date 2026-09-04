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
    _execution_profile_options_payload,
    _start_run_kwargs,
)
from agent.repository_picker import RepositoryPickerResult
from agent.run_services import CODEX_DEFAULT_SELECTION
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
        self.retry_calls: list[int] = []
        self.cancel_calls = 0
        self.quota_resume_calls = 0
        self.lease_status_calls = 0
        self.lease_release_calls: list[dict] = []
        self.progress_calls: list[dict] = []
        self.start_result = self._result(ok=True, reason="started", run_id="run-1")
        self.state_result = self._result(ok=False, reason="no_active_run")
        self.approval_result = self._result(ok=True, reason="approval_worker_started", run_id="run-1")
        self.tick_result = self._result(ok=True, reason="routine_worker_started", run_id="run-1")
        self.retry_result = self._result(ok=True, reason="retry_worker_started", run_id="run-1")
        self.cancel_result = self._result(ok=True, reason="cancel_requested", run_id="run-1")
        self.quota_resume_result = self._result(
            ok=True,
            reason="quota_resume_worker_started",
            run_id="run-1",
        )
        self.progress_result = self._result(
            ok=True,
            reason="progress_loaded",
            run_id="run-1",
            metadata={
                "progress": {
                    "run_id": "run-1",
                    "after_sequence": 0,
                    "latest_sequence": 0,
                    "events": [],
                }
            },
        )
        self.lease_status_result = self._result(
            ok=True,
            reason="chatgpt_ui_lease_status_loaded",
            metadata={"chatgpt_ui_lease": {"status": "missing", "active": False}},
        )
        self.lease_release_result = self._result(
            ok=True,
            reason="chatgpt_ui_lease_released",
            metadata={
                "chatgpt_ui_lease": {"status": "missing", "active": False},
                "release": {"event_id": 42, "event_written": True},
            },
        )

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

    def _result(
        self,
        *,
        ok: bool,
        reason: str,
        run_id: str | None = None,
        error: str | None = None,
        metadata: dict | None = None,
    ):
        return LocalControllerOperationResult(
            ok=ok,
            reason_code=reason,
            error_message=error,
            run_id=run_id,
            controller_state=self.session.controller_state,
            metadata=metadata or {},
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

    def retry_failed_action(self, failure_event_id: int):
        self.retry_calls.append(failure_event_id)
        return self.retry_result

    def request_cancel(self):
        self.cancel_calls += 1
        return self.cancel_result

    def request_force_quota_resume(self):
        self.quota_resume_calls += 1
        return self.quota_resume_result

    def get_current_progress(self, *, after_sequence: int = 0, limit: int = 100):
        self.progress_calls.append({"after_sequence": after_sequence, "limit": limit})
        return self.progress_result

    def get_chatgpt_ui_lease_status(self):
        self.lease_status_calls += 1
        return self.lease_status_result

    def release_stale_chatgpt_ui_lease(self, **kwargs):
        self.lease_release_calls.append(kwargs)
        return self.lease_release_result


def _start_body(**overrides):
    body = {
        "repository_path": "/tmp/repo",
        "initial_instruction": "Task",
        "project_title": "Project",
        "chat_title": "Chat",
        "sandbox": "read-only",
    }
    body.update(overrides)
    return body


def _lease_release_body(**overrides):
    body = {
        "owning_run_id": "run-lease",
        "owner_pid": 12345,
        "acquired_at": "2026-07-04T09:27:15.687058+00:00",
        "active_event_id": 77,
        "expected_lease_token_sha256": "a" * 64,
        "expected_run_status": "completed",
        "confirm_stale": True,
        "reason": "operator verified stale owner",
        "allow_owner_pid_alive": False,
    }
    body.update(overrides)
    return body


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
            ("GET", "/api/default-greeting"),
            ("GET", "/api/runs/current"),
            ("GET", "/api/runs/current/progress"),
            ("GET", "/api/runs/current/events"),
            ("GET", "/api/chatgpt-ui-lease"),
            ("POST", "/api/runs/start"),
            ("POST", "/api/repository/pick"),
            ("POST", "/api/approval"),
            ("POST", "/api/tick"),
            ("POST", "/api/runs/current/retry"),
            ("POST", "/api/chatgpt-ui-lease/release-stale"),
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
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])


class LocalServerEndpointTests(LocalServerHTTPTestCase):
    def test_default_greeting_route_returns_exact_kickoff_prompt(self) -> None:
        import agent.local_server as local_server

        status, _headers, payload = self.request(
            "GET",
            "/api/default-greeting",
            token=self.token,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "kick_off_prompt_gpt.md")
        self.assertEqual(
            payload["initial_instruction"],
            local_server.DEFAULT_GREETING_PATH.read_text(encoding="utf-8"),
        )

    def test_default_greeting_route_fails_safely_when_prompt_is_missing(self) -> None:
        import agent.local_server as local_server

        missing_prompt = local_server.DEFAULT_GREETING_PATH.parent / "__missing_default_greeting__.md"
        with mock.patch.object(local_server, "DEFAULT_GREETING_PATH", missing_prompt):
            status, _headers, payload = self.request(
                "GET",
                "/api/default-greeting",
                token=self.token,
            )

        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason_code"], "default_greeting_unavailable")
        self.assertNotIn("/missing/greeting.md", json.dumps(payload))

    def test_repository_picker_route_returns_selected_absolute_path(self) -> None:
        picker_result = RepositoryPickerResult(
            ok=True,
            selected=True,
            repository_path="/tmp/selected-repo",
            reason_code="repository_picker_selected",
        )
        with mock.patch("agent.local_server.choose_repository_directory", return_value=picker_result) as picker:
            status, _headers, payload = self.request(
                "POST",
                "/api/repository/pick",
                body={},
                token=self.token,
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["selected"])
        self.assertEqual(payload["repository_path"], "/tmp/selected-repo")
        picker.assert_called_once_with()

    def test_repository_picker_route_rejects_fields_and_maps_unavailable_picker(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/repository/pick",
            body={"path": "/tmp"},
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "unexpected_request_fields")

        picker_result = RepositoryPickerResult(
            ok=False,
            reason_code="repository_picker_unavailable",
            error_message="The native macOS folder picker is unavailable.",
        )
        with mock.patch("agent.local_server.choose_repository_directory", return_value=picker_result):
            status, _headers, payload = self.request(
                "POST",
                "/api/repository/pick",
                body={},
                token=self.token,
            )
        self.assertEqual(status, 501)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason_code"], "repository_picker_unavailable")

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

    def test_progress_polling_route_returns_events_after_cursor(self) -> None:
        self.controller.progress_result = self.controller._result(
            ok=True,
            reason="progress_loaded",
            run_id="run-1",
            metadata={
                "progress": {
                    "run_id": "run-1",
                    "after_sequence": 7,
                    "latest_sequence": 8,
                    "events": [
                        {
                            "run_id": "run-1",
                            "codex_invocation_id": "inv-1",
                            "sequence": 8,
                            "created_at": "2026-01-01T00:00:08+00:00",
                            "source": "test",
                            "kind": "process_started",
                            "status": "running",
                            "title": "Codex process started",
                            "summary": None,
                            "metadata": {},
                        }
                    ],
                }
            },
        )

        status, _headers, payload = self.request(
            "GET",
            "/api/runs/current/progress?after_sequence=7&limit=2",
            token=self.token,
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.controller.progress_calls[-1], {"after_sequence": 7, "limit": 2})
        progress = payload["metadata"]["progress"]
        self.assertEqual(progress["latest_sequence"], 8)
        self.assertEqual(progress["events"][0]["codex_invocation_id"], "inv-1")

    def test_progress_polling_route_rejects_bad_cursor(self) -> None:
        status, _headers, payload = self.request(
            "GET",
            "/api/runs/current/progress?after_sequence=-1",
            token=self.token,
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "invalid_progress_cursor")

    def test_progress_sse_disconnect_is_read_only(self) -> None:
        self.controller.progress_result = self.controller._result(
            ok=True,
            reason="progress_loaded",
            run_id="run-1",
            metadata={
                "progress": {
                    "run_id": "run-1",
                    "after_sequence": 0,
                    "latest_sequence": 1,
                    "events": [
                        {
                            "run_id": "run-1",
                            "codex_invocation_id": "inv-1",
                            "sequence": 1,
                            "created_at": "2026-01-01T00:00:01+00:00",
                            "source": "test",
                            "kind": "process_started",
                            "status": "running",
                            "title": "Codex process started",
                            "summary": None,
                            "metadata": {},
                        }
                    ],
                }
            },
        )
        headers = {
            "Host": f"127.0.0.1:{self.server.port}",
            LOCAL_SERVER_TOKEN_HEADER: self.token,
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        try:
            conn.request("GET", "/api/runs/current/events?after_sequence=0", headers=headers)
            response = conn.getresponse()
            first = response.readline().decode("utf-8", errors="replace")
            second = response.readline().decode("utf-8", errors="replace")
        finally:
            conn.close()
        time.sleep(0.05)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertTrue(first.startswith("id: 1") or second.startswith("event: progress"))
        self.assertGreaterEqual(len(self.controller.progress_calls), 1)
        self.assertEqual(self.controller.start_calls, [])
        self.assertEqual(self.controller.tick_calls, 0)

    def test_start_route_success_and_conflicts(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/start",
            body=_start_body(),
            token=self.token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(len(self.controller.start_calls), 1)
        self.assertEqual(self.controller.start_calls[0]["sandbox"], "read-only")
        self.assertEqual(self.controller.start_calls[0]["project_title"], "Project")
        self.assertEqual(self.controller.start_calls[0]["chat_title"], "Chat")

        self.controller.start_result = self.controller._result(
            ok=False,
            reason="active_run_exists",
            run_id="run-1",
            error="active",
        )
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/start",
            body=_start_body(),
            token=self.token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason_code"], "active_run_exists")

    def test_start_request_validation_rejections(self) -> None:
        cases = [
            (b"{", "invalid_json", 400, "application/json"),
            ([], "invalid_request_shape", 400, "application/json"),
            (_start_body(extra=True), "unexpected_request_fields", 400, "application/json"),
            (_start_body(), "unsupported_content_type", 415, "text/plain"),
            (
                {
                    "repository_path": "/tmp/repo",
                    "initial_instruction": "Task",
                    "sandbox": "read-only",
                    "project_title": "Project",
                },
                "invalid_request_shape",
                400,
                "application/json",
            ),
            (_start_body(chat_title=" "), "invalid_destination", 400, "application/json"),
            (_start_body(project_title=[]), "invalid_destination", 400, "application/json"),
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
        self.assertEqual(self.controller.start_calls, [])

        large = _start_body(initial_instruction="x" * (70 * 1024))
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
            body=_start_body(sandbox="danger-full-access"),
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
            body=_start_body(),
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

    def test_force_quota_resume_route(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/quota-resume",
            body={},
            token=self.token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["reason_code"], "quota_resume_worker_started")
        self.assertEqual(self.controller.quota_resume_calls, 1)

        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/quota-resume",
            body=None,
            token=self.token,
            content_type=None,
        )
        self.assertEqual(status, 202)

        self.controller.quota_resume_result = self.controller._result(
            ok=False,
            reason="quota_wait_not_active",
            error="no wait",
            run_id="run-1",
        )
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/quota-resume",
            body={},
            token=self.token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason_code"], "quota_wait_not_active")

        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/quota-resume",
            body={"extra": True},
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "unexpected_request_fields")

    def test_manual_retry_route(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/retry",
            body={"failure_event_id": 42},
            token=self.token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["reason_code"], "retry_worker_started")
        self.assertEqual(self.controller.retry_calls, [42])

        for invalid in (0, -1, True, "42", None):
            with self.subTest(invalid=invalid):
                status, _headers, payload = self.request(
                    "POST",
                    "/api/runs/current/retry",
                    body={"failure_event_id": invalid},
                    token=self.token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["reason_code"], "invalid_failure_event_id")

        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/retry",
            body={"failure_event_id": 42, "extra": True},
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "unexpected_request_fields")

        self.controller.retry_result = self.controller._result(
            ok=False,
            reason="failure_requires_reconciliation",
            error="review first",
        )
        status, _headers, payload = self.request(
            "POST",
            "/api/runs/current/retry",
            body={"failure_event_id": 42},
            token=self.token,
        )
        self.assertEqual(status, 409)

    def test_chatgpt_ui_lease_status_route_returns_sanitized_metadata(self) -> None:
        self.controller.lease_status_result = self.controller._result(
            ok=True,
            reason="chatgpt_ui_lease_status_loaded",
            metadata={
                "chatgpt_ui_lease": {
                    "status": "active",
                    "active": True,
                    "owning_run_id": "run-lease",
                    "owner_pid": 12345,
                    "acquired_at": "2026-07-04T09:27:15.687058+00:00",
                    "active_event_id": 77,
                    "lease_token_sha256": "b" * 64,
                    "token": "raw-secret",
                    "lease_token": "raw-lease-secret",
                }
            },
        )

        status, _headers, payload = self.request("GET", "/api/chatgpt-ui-lease", token=self.token)

        self.assertEqual(status, 200)
        self.assertEqual(self.controller.lease_status_calls, 1)
        lease = payload["metadata"]["chatgpt_ui_lease"]
        self.assertEqual(lease["status"], "active")
        self.assertEqual(lease["lease_token_sha256"], "b" * 64)
        encoded = json.dumps(payload, sort_keys=True)
        self.assertIn("lease_token_sha256", encoded)
        self.assertNotIn('"token"', encoded)
        self.assertNotIn('"lease_token"', encoded)
        self.assertNotIn("raw-secret", encoded)
        self.assertNotIn("lease_token", lease)
        self.assertNotIn("raw-lease-secret", encoded)

    def test_chatgpt_ui_lease_release_route_rejects_missing_confirmation_before_controller(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/chatgpt-ui-lease/release-stale",
            body=_lease_release_body(confirm_stale=False),
            token=self.token,
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "manual_stale_lease_confirmation_required")
        self.assertEqual(self.controller.lease_release_calls, [])

    def test_chatgpt_ui_lease_release_route_rejects_bad_shape_before_controller(self) -> None:
        for body, reason in (
            (_lease_release_body(extra=True), "unexpected_request_fields"),
            (_lease_release_body(expected_lease_token_sha256="A" * 64), "invalid_request_shape"),
            (_lease_release_body(owner_pid=True), "invalid_request_shape"),
            (_lease_release_body(reason=" "), "manual_stale_lease_reason_required"),
        ):
            with self.subTest(reason=reason):
                status, _headers, payload = self.request(
                    "POST",
                    "/api/chatgpt-ui-lease/release-stale",
                    body=body,
                    token=self.token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["reason_code"], reason)
        self.assertEqual(self.controller.lease_release_calls, [])

    def test_chatgpt_ui_lease_release_route_passes_exact_expected_fields(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/api/chatgpt-ui-lease/release-stale",
            body=_lease_release_body(expected_run_status=None, allow_owner_pid_alive=True),
            token=self.token,
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(self.controller.lease_release_calls), 1)
        self.assertEqual(
            self.controller.lease_release_calls[0],
            {
                "owning_run_id": "run-lease",
                "owner_pid": 12345,
                "acquired_at": "2026-07-04T09:27:15.687058+00:00",
                "active_event_id": 77,
                "expected_lease_token_sha256": "a" * 64,
                "expected_run_status": None,
                "confirm_stale": True,
                "reason": "operator verified stale owner",
                "allow_owner_pid_alive": True,
            },
        )
        self.assertEqual(payload["metadata"]["release"]["event_id"], 42)

    def test_chatgpt_ui_lease_release_route_maps_fail_closed_controller_results(self) -> None:
        for reason in (
            "active_chatgpt_ui_lease_mismatch",
            "chatgpt_ui_lease_owner_pid_alive",
        ):
            with self.subTest(reason=reason):
                self.controller.lease_release_result = self.controller._result(
                    ok=False,
                    reason=reason,
                    error="blocked",
                    metadata={"chatgpt_ui_lease": {"status": "active", "active": True}},
                )
                status, _headers, payload = self.request(
                    "POST",
                    "/api/chatgpt-ui-lease/release-stale",
                    body=_lease_release_body(),
                    token=self.token,
                )
                self.assertEqual(status, 409)
                self.assertEqual(payload["reason_code"], reason)

    def test_routing_methods_static_root_and_no_default_log_leaks(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            status, headers, raw = self.request_raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertNotIn(self.token, stderr.getvalue())

        status, headers, raw = self.request_raw("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")

        status, headers, raw = self.request_raw("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/manifest+json; charset=utf-8")

        status, headers, raw = self.request_raw("GET", "/service-worker.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")

        status, headers, raw = self.request_raw("GET", "/assets/icon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")

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
            self.request("GET", "/api/runs/current/progress", token=self.token)
            self.request("POST", "/api/runs/start", body=_start_body(), token=self.token)
            self.request("POST", "/api/approval", body={"decision": "approved"}, token=self.token)
            self.request("POST", "/api/tick", body={}, token=self.token)
            self.request(
                "POST",
                "/api/runs/current/retry",
                body={"failure_event_id": 42},
                token=self.token,
            )
            self.request("GET", "/api/chatgpt-ui-lease", token=self.token)
            self.request(
                "POST",
                "/api/chatgpt-ui-lease/release-stale",
                body=_lease_release_body(),
                token=self.token,
            )

        subprocess_run.assert_not_called()
        codex.assert_not_called()
        submit.assert_not_called()
        capture.assert_not_called()
        supervise.assert_not_called()
        self.assertFalse(hasattr(local_server, "Access_Control_Allow_Origin"))
        self.assertNotIn("agent.cli", local_server.__dict__)


class LocalServerExecutionProfileContractTests(unittest.TestCase):
    def test_profile_options_include_explicit_autonomous_full_access(self) -> None:
        import agent.local_server as local_server

        with mock.patch.object(
            local_server,
            "execution_profile_options",
            return_value={
                "sandbox_options": ["read-only", "danger-full-access", "custom", "workspace-write"],
                "model_options": [CODEX_DEFAULT_SELECTION, "test-central-model"],
                "reasoning_effort": CODEX_DEFAULT_SELECTION,
                "approval_policy": CODEX_DEFAULT_SELECTION,
            },
        ):
            payload = _execution_profile_options_payload()

        self.assertTrue(payload["ok"])
        self.assertEqual(
            [option["value"] for option in payload["sandbox_options"]],
            ["read-only", "danger-full-access", "workspace-write"],
        )
        self.assertEqual(
            [option["label"] for option in payload["sandbox_options"]],
            ["Read Only", "Full Access (Autonomous)", "Workspace Write"],
        )
        self.assertEqual(
            [option["description"] for option in payload["sandbox_options"]],
            [
                "Codex can inspect the workspace. Edits are not allowed for this dashboard run.",
                (
                    "Codex runs autonomously without filesystem, network, or prompt-policy "
                    "limits. The loop does not request per-run approval."
                ),
                (
                    "Codex can edit files in this repository. Outside-workspace and dangerous "
                    "access remain blocked by this dashboard run."
                ),
            ],
        )
        self.assertIn("danger-full-access", json.dumps(payload))
        self.assertEqual(
            [option["value"] for option in payload["model_options"]],
            [CODEX_DEFAULT_SELECTION, "test-central-model"],
        )
        self.assertEqual(payload["locked"]["reasoning_effort"]["value"], CODEX_DEFAULT_SELECTION)
        self.assertEqual(payload["locked"]["approval_policy"]["value"], CODEX_DEFAULT_SELECTION)
        self.assertEqual(
            payload["locked"]["approval_policy"]["label"],
            "Codex default — Full Access bypasses approvals",
        )

    def test_start_kwargs_preserve_explicit_model_and_reject_bad_profile_before_controller(self) -> None:
        import agent.local_server as local_server

        payload = {
            "repository_path": "/tmp/repo",
            "initial_instruction": "Task",
            "project_title": " Project ",
            "chat_title": " Chat ",
            "sandbox": "workspace-write",
            "model": "gpt-5-codex",
        }

        self.assertEqual(
            _start_run_kwargs(payload),
            {
                "repository_path": "/tmp/repo",
                "initial_instruction": "Task",
                "project_title": "Project",
                "chat_title": "Chat",
                "sandbox": "workspace-write",
                "model": "gpt-5-codex",
            },
        )

        for body, reason in (
            (
                _start_body(sandbox="bad", model=CODEX_DEFAULT_SELECTION),
                "invalid_browser_sandbox",
            ),
            (
                _start_body(model="not-a-model"),
                "invalid_codex_model",
            ),
            (
                {
                    "initial_instruction": "Task",
                    "sandbox": "read-only",
                    "project_title": "Project",
                    "chat_title": "Chat",
                    "model": CODEX_DEFAULT_SELECTION,
                },
                "invalid_request_shape",
            ),
            (
                {
                    "repository_path": "/tmp/repo",
                    "initial_instruction": "Task",
                    "project_title": "Project",
                    "chat_title": "Chat",
                    "model": CODEX_DEFAULT_SELECTION,
                },
                "invalid_request_shape",
            ),
            (_start_body(project_title=" "), "invalid_destination"),
            (_start_body(chat_title=[]), "invalid_destination"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(local_server.LocalServerError) as raised:
                    _start_run_kwargs(body)
                self.assertEqual(raised.exception.reason_code, reason)

    def test_start_kwargs_omitted_model_remains_legacy_compatible(self) -> None:
        kwargs = _start_run_kwargs(
            {
                "repository_path": "/tmp/repo",
                "initial_instruction": "Task",
                "project_title": "Project",
                "chat_title": "Chat",
                "sandbox": "read-only",
            }
        )

        self.assertEqual(kwargs["sandbox"], "read-only")
        self.assertEqual(kwargs["project_title"], "Project")
        self.assertEqual(kwargs["chat_title"], "Chat")
        self.assertNotIn("model", kwargs)
        self.assertNotIn("allow_destination_navigation", kwargs)

    def test_start_kwargs_navigation_flag_default_and_explicit_and_type_checked(self) -> None:
        import agent.local_server as local_server

        base = {
            "repository_path": "/tmp/repo",
            "initial_instruction": "Task",
            "project_title": "Project",
            "chat_title": "Chat",
            "sandbox": "read-only",
        }

        # Omitted: the controller default (disabled) is preserved by absence.
        self.assertNotIn(
            "allow_destination_navigation", _start_run_kwargs(dict(base))
        )

        enabled = _start_run_kwargs({**base, "allow_destination_navigation": True})
        self.assertIs(enabled["allow_destination_navigation"], True)

        disabled = _start_run_kwargs({**base, "allow_destination_navigation": False})
        self.assertIs(disabled["allow_destination_navigation"], False)

        with self.assertRaises(local_server.LocalServerError) as raised:
            _start_run_kwargs({**base, "allow_destination_navigation": "yes"})
        self.assertEqual(raised.exception.reason_code, "invalid_request_shape")


if __name__ == "__main__":
    unittest.main()
