from __future__ import annotations

import argparse
import hmac
import json
import threading
import time
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent.local_controller import LocalController, LocalControllerSession
from agent.repository_picker import RepositoryPickerResult, choose_repository_directory
from agent.run_services import (
    ALLOWED_CODEX_MODEL_SELECTIONS,
    CODEX_DEFAULT_SELECTION,
    RunDestinationBinding,
    execution_profile_options,
)


LOCAL_SERVER_BIND_HOST = "127.0.0.1"
LOCAL_SERVER_DEFAULT_PORT = 0
LOCAL_SERVER_TOKEN_HEADER = "X-Controller-Token"
LOCAL_SERVER_START_BODY_LIMIT = 64 * 1024
LOCAL_SERVER_APPROVAL_BODY_LIMIT = 4 * 1024
LOCAL_SERVER_GENERIC_BODY_LIMIT = 8 * 1024
LOCAL_SERVER_PROGRESS_LIMIT = 100
LOCAL_SERVER_SSE_HEARTBEAT_SECONDS = 2.0
LOCAL_SERVER_PERMISSION_PRESET_VALUES = (
    "read-only",
    "workspace-write",
    "danger-full-access",
)

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"
WEB_STATIC_DIR = Path(__file__).with_name("web_static")
DEFAULT_GREETING_PATH = Path(__file__).resolve().parent.parent / "kick_off_prompt_gpt.md"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class LocalServerError(Exception):
    def __init__(self, status: int, reason_code: str, error_message: str) -> None:
        super().__init__(error_message)
        self.status = status
        self.reason_code = reason_code
        self.error_message = error_message


class LocalControllerServer:
    def __init__(
        self,
        *,
        host: str = LOCAL_SERVER_BIND_HOST,
        port: int = LOCAL_SERVER_DEFAULT_PORT,
        controller: LocalController | None = None,
        session: LocalControllerSession | None = None,
    ) -> None:
        if host != LOCAL_SERVER_BIND_HOST:
            raise ValueError("Local controller server must bind to 127.0.0.1.")
        self.host = host
        self.configured_port = port
        self.session = session or (controller.session if controller is not None else LocalControllerSession())
        self.controller = controller or LocalController(session=self.session)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    @property
    def port(self) -> int:
        if self.httpd is not None:
            return int(self.httpd.server_address[1])
        return int(self.configured_port)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.httpd is not None:
                return
            handler_class = _make_handler(self)
            self.httpd = ThreadingHTTPServer((self.host, self.configured_port), handler_class)
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()

    def serve_foreground(self) -> None:
        with self._lifecycle_lock:
            if self.httpd is not None:
                raise RuntimeError("Server is already started.")
            handler_class = _make_handler(self)
            self.httpd = ThreadingHTTPServer((self.host, self.configured_port), handler_class)
        try:
            self.httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            httpd = self.httpd
            thread = self.thread
            self.httpd = None
            self.thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def bootstrap_url(self) -> str:
        return f"http://{self.host}:{self.port}/#token={self.session.token}"

    def token_matches(self, value: str | None) -> bool:
        if value is None:
            return False
        return hmac.compare_digest(value, self.session.token)


def _make_handler(server_runtime: LocalControllerServer):
    class LocalControllerRequestHandler(BaseHTTPRequestHandler):
        server_version = "LocalControllerHTTP/1"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            self._handle_request("GET")

        def do_POST(self) -> None:
            self._handle_request("POST")

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def _handle_request(self, method: str) -> None:
            try:
                parsed_url = urlsplit(self.path)
                path = parsed_url.path
                if not path.startswith("/api/"):
                    self._validate_host()
                    self._handle_static(method, path)
                    return
                self._validate_host()
                self._authenticate()
                if method == "GET":
                    self._handle_get(path, parsed_url.query)
                elif method == "POST":
                    self._handle_post(path)
                else:
                    self._method_not_allowed()
            except LocalServerError as exc:
                self._write_error(exc.status, exc.reason_code, exc.error_message)
            except Exception:
                self._write_error(500, "internal_controller_error", "Internal controller error.")

        def _handle_static(self, method: str, path: str) -> None:
            if method != "GET":
                self._method_not_allowed()
                return
            route = STATIC_ROUTES.get(path)
            if route is None:
                self._write_error(404, "route_not_found", "Static asset was not found.")
                return
            filename, content_type = route
            asset_path = WEB_STATIC_DIR / filename
            try:
                data = asset_path.read_bytes()
            except OSError:
                self._write_error(404, "route_not_found", "Static asset was not found.")
                return
            self._write_bytes(200, data, content_type)

        def _handle_get(self, path: str, query: str = "") -> None:
            if path == "/api/health":
                self._write_json(200, _health_payload(server_runtime))
                return
            if path == "/api/session":
                self._write_json(200, _session_payload(server_runtime))
                return
            if path == "/api/execution-profile/options":
                self._write_json(200, _execution_profile_options_payload())
                return
            if path == "/api/default-greeting":
                self._write_json(200, _default_greeting_payload())
                return
            if path == "/api/runs/current":
                self._write_json(200, _state_payload(server_runtime.controller.get_current_state()))
                return
            if path == "/api/runs/current/progress":
                after_sequence, limit = _progress_query(query)
                self._write_json(
                    200,
                    _progress_payload(
                        server_runtime,
                        after_sequence=after_sequence,
                        limit=limit,
                    ),
                )
                return
            if path == "/api/runs/current/events":
                after_sequence, limit = _progress_query(query)
                self._handle_progress_sse(after_sequence=after_sequence, limit=limit)
                return
            if path == "/api/chatgpt-ui-lease":
                self._write_json(200, _operation_payload(server_runtime.controller.get_chatgpt_ui_lease_status()))
                return
            self._write_error(404, "route_not_found", "API route was not found.")

        def _handle_post(self, path: str) -> None:
            self._validate_origin()
            if path == "/api/repository/pick":
                payload = self._read_json_body(
                    LOCAL_SERVER_GENERIC_BODY_LIMIT,
                    require_object=True,
                    allow_empty=True,
                )
                if payload:
                    raise LocalServerError(400, "unexpected_request_fields", "Unexpected request fields.")
                picker_result = choose_repository_directory()
                self._write_repository_picker_result(picker_result)
                return
            if path == "/api/runs/start":
                payload = self._read_json_body(LOCAL_SERVER_START_BODY_LIMIT, require_object=True, allow_empty=False)
                result = server_runtime.controller.start_run(**_start_run_kwargs(payload))
                self._write_operation_result(result, success_status=202)
                return
            if path == "/api/approval":
                payload = self._read_json_body(LOCAL_SERVER_APPROVAL_BODY_LIMIT, require_object=True, allow_empty=False)
                _require_exact_fields(payload, {"decision"})
                if payload["decision"] not in {"approved", "rejected"}:
                    raise LocalServerError(
                        400,
                        "invalid_approval_decision",
                        "Invalid approval decision. Expected approved or rejected.",
                    )
                result = server_runtime.controller.submit_approval_decision(payload["decision"])
                self._write_operation_result(result, success_status=202)
                return
            if path == "/api/tick":
                payload = self._read_json_body(LOCAL_SERVER_GENERIC_BODY_LIMIT, require_object=True, allow_empty=True)
                if payload:
                    raise LocalServerError(400, "unexpected_request_fields", "Unexpected request fields.")
                result = server_runtime.controller.request_automatic_progress()
                status = 202 if result.ok else 200
                self._write_operation_result(result, success_status=status, default_failure_status=200)
                return
            if path == "/api/chatgpt-ui-lease/release-stale":
                payload = self._read_json_body(LOCAL_SERVER_GENERIC_BODY_LIMIT, require_object=True, allow_empty=False)
                result = server_runtime.controller.release_stale_chatgpt_ui_lease(
                    **_stale_lease_release_kwargs(payload)
                )
                self._write_operation_result(result, success_status=200)
                return
            self._write_error(404, "route_not_found", "API route was not found.")

        def _write_repository_picker_result(self, result: RepositoryPickerResult) -> None:
            payload = {
                "ok": result.ok,
                "selected": result.selected,
                "repository_path": result.repository_path,
                "reason_code": result.reason_code,
                "error_message": result.error_message,
            }
            if result.ok:
                self._write_json(200, payload)
                return
            status = 409 if result.reason_code == "repository_picker_in_progress" else 500
            if result.reason_code == "repository_picker_unavailable":
                status = 501
            self._write_json(status, payload)

        def _handle_progress_sse(self, *, after_sequence: int, limit: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", SSE_CONTENT_TYPE)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            latest_sequence = after_sequence
            while True:
                payload = _progress_payload(
                    server_runtime,
                    after_sequence=latest_sequence,
                    limit=limit,
                )
                progress = payload.get("metadata", {}).get("progress", {})
                events = progress.get("events") if isinstance(progress, dict) else []
                if not isinstance(events, list):
                    events = []
                for event in events:
                    if not self._write_sse_event("progress", event):
                        return
                    sequence = event.get("sequence") if isinstance(event, dict) else None
                    if isinstance(sequence, int):
                        latest_sequence = max(latest_sequence, sequence)

                if not isinstance(progress, dict) or not progress.get("run_id"):
                    self._write_sse_event("progress_state", payload)
                    return
                if not self._write_sse_comment("heartbeat"):
                    return
                time.sleep(LOCAL_SERVER_SSE_HEARTBEAT_SECONDS)

        def _write_sse_event(self, event_name: str, payload: dict[str, Any]) -> bool:
            event_id = payload.get("sequence") if isinstance(payload, dict) else None
            lines = []
            if isinstance(event_id, int):
                lines.append(f"id: {event_id}")
            lines.append(f"event: {event_name}")
            data = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"))
            for line in data.splitlines() or [""]:
                lines.append(f"data: {line}")
            lines.append("")
            lines.append("")
            return self._write_sse_chunk("\n".join(lines))

        def _write_sse_comment(self, comment: str) -> bool:
            return self._write_sse_chunk(f": {comment}\n\n")

        def _write_sse_chunk(self, chunk: str) -> bool:
            try:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False
            return True

        def _write_operation_result(
            self,
            result: Any,
            *,
            success_status: int,
            default_failure_status: int | None = None,
        ) -> None:
            payload = _operation_payload(result)
            if getattr(result, "ok", False):
                self._write_json(success_status, payload)
                return
            reason = getattr(result, "reason_code", None) or "internal_controller_error"
            status = _status_for_reason(reason, default_failure_status=default_failure_status)
            self._write_json(status, payload)

        def _read_json_body(self, limit: int, *, require_object: bool, allow_empty: bool) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "")
            length_text = self.headers.get("Content-Length")
            if length_text is None:
                if allow_empty:
                    return {}
                raise LocalServerError(400, "invalid_request_shape", "Request body is required.")
            try:
                length = int(length_text)
            except ValueError:
                raise LocalServerError(400, "invalid_request_shape", "Invalid Content-Length.")
            if length > limit:
                raise LocalServerError(413, "request_body_too_large", "Request body is too large.")
            if length == 0:
                if allow_empty:
                    return {}
                if not _is_json_content_type(content_type):
                    raise LocalServerError(415, "unsupported_content_type", "Request body must use application/json.")
                return self._invalid_json()
            if not _is_json_content_type(content_type):
                raise LocalServerError(415, "unsupported_content_type", "Request body must use application/json.")
            body = self.rfile.read(length)
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                raise LocalServerError(400, "invalid_json", "Request body must be valid UTF-8 JSON.")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                raise LocalServerError(400, "invalid_json", "Request body must be valid JSON.")
            if require_object and not isinstance(payload, dict):
                raise LocalServerError(400, "invalid_request_shape", "Request body must be a JSON object.")
            return payload

        def _invalid_json(self) -> dict[str, Any]:
            raise LocalServerError(400, "invalid_json", "Request body must be valid JSON.")

        def _validate_host(self) -> None:
            host = self.headers.get("Host")
            allowed = {
                f"{LOCAL_SERVER_BIND_HOST}:{server_runtime.port}",
                f"localhost:{server_runtime.port}",
            }
            if host not in allowed:
                raise LocalServerError(400, "invalid_host", "Invalid Host header.")

        def _validate_origin(self) -> None:
            origin = self.headers.get("Origin")
            if not origin:
                return
            allowed = {
                f"http://{LOCAL_SERVER_BIND_HOST}:{server_runtime.port}",
                f"http://localhost:{server_runtime.port}",
            }
            if origin not in allowed:
                raise LocalServerError(403, "invalid_origin", "Invalid Origin header.")

        def _authenticate(self) -> None:
            token = self.headers.get(LOCAL_SERVER_TOKEN_HEADER)
            if token is None:
                raise LocalServerError(401, "authentication_required", "Controller token is required.")
            if not server_runtime.token_matches(token):
                raise LocalServerError(401, "authentication_failed", "Controller token is invalid.")

        def _method_not_allowed(self) -> None:
            self._write_error(405, "method_not_allowed", "HTTP method is not allowed for this route.")

        def _write_error(self, status: int, reason_code: str, error_message: str) -> None:
            self._write_json(
                status,
                {
                    "ok": False,
                    "reason_code": reason_code,
                    "error_message": error_message,
                },
            )

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
            self._write_bytes(status, data, JSON_CONTENT_TYPE)

        def _write_bytes(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return LocalControllerRequestHandler


def _is_json_content_type(value: str) -> bool:
    if not value:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def _require_exact_fields(payload: dict[str, Any], expected: set[str]) -> None:
    keys = set(payload)
    if keys != expected:
        reason = "unexpected_request_fields" if keys - expected else "invalid_request_shape"
        raise LocalServerError(400, reason, "Request fields did not match the expected schema.")


def _start_run_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "repository_path",
        "initial_instruction",
        "sandbox",
        "project_title",
        "chat_title",
    }
    optional = {"model", "allow_destination_navigation"}
    keys = set(payload)
    if missing := required - keys:
        raise LocalServerError(
            400,
            "invalid_request_shape",
            "Request fields did not match the expected schema.",
        )
    if keys - required - optional:
        raise LocalServerError(
            400,
            "unexpected_request_fields",
            "Request fields did not match the expected schema.",
        )

    kwargs = {
        "repository_path": payload["repository_path"],
        "initial_instruction": payload["initial_instruction"],
    }
    try:
        destination = RunDestinationBinding(payload["project_title"], payload["chat_title"])
    except (TypeError, ValueError) as exc:
        raise LocalServerError(400, "invalid_destination", str(exc))
    kwargs["project_title"] = destination.project_title
    kwargs["chat_title"] = destination.chat_title
    sandbox = payload["sandbox"]
    if not isinstance(sandbox, str) or sandbox not in LOCAL_SERVER_PERMISSION_PRESET_VALUES:
        allowed_text = ", ".join(LOCAL_SERVER_PERMISSION_PRESET_VALUES)
        raise LocalServerError(
            400,
            "invalid_browser_sandbox",
            f"Invalid browser sandbox. Allowed values: {allowed_text}.",
        )
    kwargs["sandbox"] = sandbox
    if "model" in payload:
        model = payload["model"]
        if not isinstance(model, str) or model not in ALLOWED_CODEX_MODEL_SELECTIONS:
            allowed_text = ", ".join(ALLOWED_CODEX_MODEL_SELECTIONS)
            raise LocalServerError(
                400,
                "invalid_codex_model",
                f"Invalid Codex model. Allowed values: {allowed_text}.",
            )
        kwargs["model"] = model
    if "allow_destination_navigation" in payload:
        allow_navigation = payload["allow_destination_navigation"]
        if not isinstance(allow_navigation, bool):
            raise LocalServerError(
                400,
                "invalid_request_shape",
                "allow_destination_navigation must be a boolean.",
            )
        kwargs["allow_destination_navigation"] = allow_navigation
    return kwargs


def _stale_lease_release_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "owning_run_id",
        "owner_pid",
        "acquired_at",
        "active_event_id",
        "expected_lease_token_sha256",
        "expected_run_status",
        "confirm_stale",
        "reason",
        "allow_owner_pid_alive",
    }
    _require_exact_fields(payload, expected)

    owning_run_id = payload["owning_run_id"]
    acquired_at = payload["acquired_at"]
    expected_fingerprint = payload["expected_lease_token_sha256"]
    reason = payload["reason"]
    expected_run_status = payload["expected_run_status"]
    if not isinstance(owning_run_id, str) or owning_run_id.strip() == "":
        raise LocalServerError(400, "invalid_request_shape", "owning_run_id must be a non-empty string.")
    if not isinstance(payload["owner_pid"], int) or isinstance(payload["owner_pid"], bool) or payload["owner_pid"] <= 0:
        raise LocalServerError(400, "invalid_request_shape", "owner_pid must be a positive integer.")
    if not isinstance(acquired_at, str) or acquired_at.strip() == "":
        raise LocalServerError(400, "invalid_request_shape", "acquired_at must be a non-empty string.")
    if (
        not isinstance(payload["active_event_id"], int)
        or isinstance(payload["active_event_id"], bool)
        or payload["active_event_id"] <= 0
    ):
        raise LocalServerError(400, "invalid_request_shape", "active_event_id must be a positive integer.")
    if not isinstance(expected_fingerprint, str) or not _valid_sha256(expected_fingerprint):
        raise LocalServerError(
            400,
            "invalid_request_shape",
            "expected_lease_token_sha256 must be a lowercase SHA-256 hex digest.",
        )
    if expected_run_status is not None and (
        not isinstance(expected_run_status, str) or expected_run_status.strip() == ""
    ):
        raise LocalServerError(400, "invalid_request_shape", "expected_run_status must be a string or null.")
    if payload["confirm_stale"] is not True:
        raise LocalServerError(
            400,
            "manual_stale_lease_confirmation_required",
            "Manual stale ChatGPT UI lease release requires operator confirmation.",
        )
    if not isinstance(reason, str) or reason.strip() == "":
        raise LocalServerError(
            400,
            "manual_stale_lease_reason_required",
            "Manual stale ChatGPT UI lease release requires a human-readable reason.",
        )
    if not isinstance(payload["allow_owner_pid_alive"], bool):
        raise LocalServerError(400, "invalid_request_shape", "allow_owner_pid_alive must be a boolean.")

    return {
        "owning_run_id": owning_run_id.strip(),
        "owner_pid": payload["owner_pid"],
        "acquired_at": acquired_at.strip(),
        "active_event_id": payload["active_event_id"],
        "expected_lease_token_sha256": expected_fingerprint,
        "expected_run_status": expected_run_status.strip() if isinstance(expected_run_status, str) else None,
        "confirm_stale": True,
        "reason": reason.strip(),
        "allow_owner_pid_alive": payload["allow_owner_pid_alive"],
    }


def _progress_query(query: str) -> tuple[int, int]:
    params = parse_qs(query, keep_blank_values=True)
    after_sequence = _single_nonnegative_int_query_value(
        params,
        "after_sequence",
        default=0,
        reason_code="invalid_progress_cursor",
    )
    limit = _single_nonnegative_int_query_value(
        params,
        "limit",
        default=LOCAL_SERVER_PROGRESS_LIMIT,
        reason_code="invalid_progress_limit",
    )
    limit = max(1, min(limit, LOCAL_SERVER_PROGRESS_LIMIT))
    return after_sequence, limit


def _single_nonnegative_int_query_value(
    params: dict[str, list[str]],
    name: str,
    *,
    default: int,
    reason_code: str,
) -> int:
    values = params.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise LocalServerError(400, reason_code, "Invalid progress query parameter.")
    try:
        value = int(values[0])
    except ValueError:
        raise LocalServerError(400, reason_code, "Invalid progress query parameter.")
    if value < 0:
        raise LocalServerError(400, reason_code, "Invalid progress query parameter.")
    return value


def _progress_payload(
    server_runtime: LocalControllerServer,
    *,
    after_sequence: int,
    limit: int,
) -> dict[str, Any]:
    progress_reader = getattr(server_runtime.controller, "get_current_progress", None)
    if callable(progress_reader):
        return _operation_payload(
            progress_reader(after_sequence=after_sequence, limit=limit)
        )

    active_run_id = server_runtime.session.active_run_id
    return {
        "ok": True,
        "reason_code": "progress_loaded" if active_run_id else "no_active_run",
        "error_message": None,
        "run_id": active_run_id,
        "controller_state": server_runtime.session.controller_state,
        "metadata": {
            "progress": {
                "run_id": active_run_id,
                "after_sequence": after_sequence,
                "latest_sequence": after_sequence,
                "events": [],
            }
        },
        "state": None,
    }


def _valid_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _status_for_reason(reason: str, *, default_failure_status: int | None = None) -> int:
    if reason in {
        "active_run_exists",
        "no_pending_approval",
        "action_already_running",
        "pending_approval_exists",
        "controller_state_terminal",
        "human_approval_required",
        "chatgpt_ui_lease_owner_pid_alive",
        "chatgpt_ui_lease_owner_pid_unknown",
        "chatgpt_ui_lease_owner_run_not_terminal",
        "active_chatgpt_ui_lease_mismatch",
        "chatgpt_ui_lease_not_active",
        "manual_stale_lease_run_status_mismatch",
    }:
        return 409 if default_failure_status is None else default_failure_status
    if reason in {
        "repository_path_required",
        "repository_path_not_found",
        "repository_path_not_directory",
        "initial_instruction_required",
        "invalid_browser_sandbox",
        "danger_full_access_not_available_in_local_controller",
        "invalid_codex_model",
        "invalid_approval_decision",
        "destination_required",
        "invalid_destination",
        "no_active_run",
        "no_routine_action_available",
        "manual_stale_lease_confirmation_required",
        "manual_stale_lease_reason_required",
        "invalid_manual_stale_lease_release_request",
        "invalid_expected_lease_token_sha256",
    }:
        return 400 if default_failure_status is None else default_failure_status
    return default_failure_status or 500


def _health_payload(server_runtime: LocalControllerServer) -> dict[str, Any]:
    runtime = _runtime(server_runtime.controller)
    return {
        "ok": True,
        "bind_host": server_runtime.host,
        "port": server_runtime.port,
        "controller_state": server_runtime.session.controller_state,
        "active_run_id": server_runtime.session.active_run_id,
        "action_running": bool(runtime.get("action_running", False)),
    }


def _default_greeting_payload() -> dict[str, Any]:
    try:
        initial_instruction = DEFAULT_GREETING_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise LocalServerError(
            500,
            "default_greeting_unavailable",
            "The default greeting could not be loaded.",
        )
    if not initial_instruction.strip():
        raise LocalServerError(
            500,
            "default_greeting_unavailable",
            "The default greeting is empty.",
        )
    return {
        "ok": True,
        "initial_instruction": initial_instruction,
        "source": DEFAULT_GREETING_PATH.name,
    }


def _session_payload(server_runtime: LocalControllerServer) -> dict[str, Any]:
    runtime = _runtime(server_runtime.controller)
    return {
        "ok": True,
        "session_id": server_runtime.session.session_id,
        "controller_state": server_runtime.session.controller_state,
        "active_run_id": server_runtime.session.active_run_id,
        "action_running": bool(runtime.get("action_running", False)),
        "pending_approval_available": bool(runtime.get("pending_approval_available", False)),
    }


def _execution_profile_options_payload() -> dict[str, Any]:
    options = execution_profile_options()
    sandbox_options = [
        _option_payload(value, _sandbox_label(value), _sandbox_description(value))
        for value in options["sandbox_options"]
        if value in LOCAL_SERVER_PERMISSION_PRESET_VALUES
    ]
    model_options = [
        _option_payload(value, "Codex default" if value == CODEX_DEFAULT_SELECTION else value)
        for value in options["model_options"]
    ]
    return {
        "ok": True,
        "sandbox_options": sandbox_options,
        "model_options": model_options,
        "locked": {
            "reasoning_effort": _option_payload(options["reasoning_effort"], "Codex default"),
            "approval_policy": _option_payload(
                options["approval_policy"],
                "Codex default — Full Access bypasses approvals",
            ),
        },
    }


def _option_payload(value: str, label: str, description: str | None = None) -> dict[str, str]:
    payload = {"value": value, "label": label}
    if description is not None:
        payload["description"] = description
    return payload


def _sandbox_label(value: str) -> str:
    return {
        "read-only": "Read Only",
        "workspace-write": "Workspace Write",
        "danger-full-access": "Full Access (Autonomous)",
    }.get(value, value)


def _sandbox_description(value: str) -> str:
    return {
        "read-only": (
            "Codex can inspect the workspace. Edits are not allowed for this dashboard run."
        ),
        "workspace-write": (
            "Codex can edit files in this repository. Outside-workspace and dangerous "
            "access remain blocked by this dashboard run."
        ),
        "danger-full-access": (
            "Codex runs autonomously without filesystem, network, or prompt-policy "
            "limits. The loop does not request per-run approval."
        ),
    }.get(value, "")


def _runtime(controller: LocalController) -> dict[str, Any]:
    with controller._lock:  # noqa: SLF001 - local server needs a safe session summary without executing reads.
        return controller._runtime_snapshot_locked()  # noqa: SLF001


def _state_payload(result: Any) -> dict[str, Any]:
    return _operation_payload(result)


def _operation_payload(result: Any) -> dict[str, Any]:
    read_model = getattr(result, "read_model", None)
    payload = {
        "ok": bool(getattr(result, "ok", False)),
        "reason_code": getattr(result, "reason_code", None),
        "error_message": getattr(result, "error_message", None),
        "run_id": getattr(result, "run_id", None),
        "controller_state": getattr(result, "controller_state", None),
        "metadata": _json_safe(getattr(result, "metadata", {})),
        "state": _json_safe(read_model) if read_model is not None else None,
    }
    return payload


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key) not in {"token", "lease_token"}
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local controller JSON API server.")
    parser.add_argument("--host", default=LOCAL_SERVER_BIND_HOST, help="Bind host. Must be 127.0.0.1.")
    parser.add_argument("--port", type=int, default=LOCAL_SERVER_DEFAULT_PORT, help="Bind port. Use 0 for an OS-assigned port.")
    args = parser.parse_args(argv)
    if args.host != LOCAL_SERVER_BIND_HOST:
        parser.exit(2, "error: local server host must be 127.0.0.1\n")

    server = LocalControllerServer(host=args.host, port=args.port)
    try:
        server.start()
        print(f"Local controller server listening on http://{server.host}:{server.port}")
        print(f"Bootstrap URL: {server.bootstrap_url()}")
        if server.thread is not None:
            while server.thread.is_alive():
                server.thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
