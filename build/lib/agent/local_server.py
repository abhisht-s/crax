from __future__ import annotations

import argparse
import hmac
import json
import threading
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent.local_controller import LocalController, LocalControllerSession
from agent.run_services import (
    ALLOWED_CODEX_MODEL_SELECTIONS,
    ALLOWED_EXECUTION_PROFILE_SANDBOXES,
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

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
WEB_STATIC_DIR = Path(__file__).with_name("web_static")
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
                path = urlsplit(self.path).path
                if not path.startswith("/api/"):
                    self._validate_host()
                    self._handle_static(method, path)
                    return
                self._validate_host()
                self._authenticate()
                if method == "GET":
                    self._handle_get(path)
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

        def _handle_get(self, path: str) -> None:
            if path == "/api/health":
                self._write_json(200, _health_payload(server_runtime))
                return
            if path == "/api/session":
                self._write_json(200, _session_payload(server_runtime))
                return
            if path == "/api/execution-profile/options":
                self._write_json(200, _execution_profile_options_payload())
                return
            if path == "/api/runs/current":
                self._write_json(200, _state_payload(server_runtime.controller.get_current_state()))
                return
            self._write_error(404, "route_not_found", "API route was not found.")

        def _handle_post(self, path: str) -> None:
            self._validate_origin()
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
            self._write_error(404, "route_not_found", "API route was not found.")

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
    optional = {"model"}
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
    if sandbox == "danger-full-access":
        raise LocalServerError(
            400,
            "danger_full_access_not_available_in_local_controller",
            "danger-full-access is not available through the local controller.",
        )
    if not isinstance(sandbox, str) or sandbox not in ALLOWED_EXECUTION_PROFILE_SANDBOXES:
        allowed_text = ", ".join(ALLOWED_EXECUTION_PROFILE_SANDBOXES)
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
    return kwargs


def _status_for_reason(reason: str, *, default_failure_status: int | None = None) -> int:
    if reason in {
        "active_run_exists",
        "no_pending_approval",
        "action_already_running",
        "pending_approval_exists",
        "controller_state_terminal",
        "human_approval_required",
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
        _option_payload(value, _sandbox_label(value))
        for value in options["sandbox_options"]
        if value != "danger-full-access"
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
            "approval_policy": _option_payload(options["approval_policy"], "Codex default"),
        },
    }


def _option_payload(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _sandbox_label(value: str) -> str:
    return {
        "read-only": "Read-only",
        "workspace-write": "Workspace write",
    }.get(value, value)


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
        "state": _json_safe(read_model) if read_model is not None else None,
    }
    return payload


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if str(key) != "token"}
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
