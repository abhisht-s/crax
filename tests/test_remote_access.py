from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import ledger
from agent.local_controller import LocalController
from agent.local_server import LocalControllerServer
from agent.remote_access import (
    REMOTE_FULL_ACCESS_CONFIRMATION,
    REMOTE_PAIRING_ATTEMPT_LIMIT,
    RemoteAccessConfig,
    RemoteAccessManager,
)
from agent.run_state import RunStatus
from tests.test_local_server import FakeController


class RemoteAccessManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "ledger.db"
        self.ledger_patch = mock.patch.object(ledger, "DB_PATH", self.database_path)
        self.ledger_patch.start()
        self.repository = self.root / "repos" / "project"
        (self.repository / ".git").mkdir(parents=True)
        self.config = RemoteAccessConfig(
            public_base_url="https://mac.example.ts.net/",
            repository_roots=(self.root / "repos",),
        )
        self.manager = RemoteAccessManager(self.config)

    def tearDown(self) -> None:
        self.ledger_patch.stop()
        self.temporary_directory.cleanup()

    def test_pairing_is_one_time_authenticates_and_can_be_revoked(self) -> None:
        code, _ = self.manager.create_pairing_code()
        paired = self.manager.pair_device(code, "Pixel")
        self.assertTrue(paired.ok)
        self.assertIsNotNone(paired.token)

        reused = self.manager.pair_device(code, "Other")
        self.assertFalse(reused.ok)
        self.assertEqual(reused.reason_code, "pairing_code_used")

        principal = self.manager.authenticate_cookie(paired.token)
        self.assertIsNotNone(principal)
        self.assertEqual(principal.scope, "admin")
        self.assertTrue(principal.permits("control"))

        self.assertTrue(self.manager.revoke_device(str(paired.device_id)))
        self.assertIsNone(self.manager.authenticate_cookie(paired.token))

    def test_repository_catalog_only_returns_git_repositories_under_roots(self) -> None:
        outside = self.root / "outside"
        (outside / ".git").mkdir(parents=True)

        repositories = self.manager.list_repositories()

        self.assertEqual(
            [item["path"] for item in repositories],
            [str(self.repository.resolve())],
        )
        self.assertTrue(self.manager.repository_allowed(self.repository))
        self.assertFalse(self.manager.repository_allowed(outside))

    def test_pairing_attempts_are_rate_limited(self) -> None:
        for _ in range(REMOTE_PAIRING_ATTEMPT_LIMIT):
            result = self.manager.pair_device("invalid", "Phone")
            self.assertEqual(result.reason_code, "pairing_code_invalid")

        limited = self.manager.pair_device("invalid", "Phone")

        self.assertEqual(limited.reason_code, "pairing_rate_limited")

    def test_rotation_invalidates_previous_credential(self) -> None:
        code, _ = self.manager.create_pairing_code()
        paired = self.manager.pair_device(code, "Pixel")

        rotated = self.manager.rotate_device(str(paired.device_id))

        self.assertTrue(rotated.ok)
        self.assertIsNone(self.manager.authenticate_cookie(paired.token))
        self.assertIsNotNone(self.manager.authenticate_cookie(rotated.token))


class RemoteHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "ledger.db"
        self.ledger_patch = mock.patch.object(ledger, "DB_PATH", self.database_path)
        self.ledger_patch.start()
        self.repository = self.root / "repos" / "project"
        (self.repository / ".git").mkdir(parents=True)
        self.config = RemoteAccessConfig(
            public_base_url="https://mac.example.ts.net",
            repository_roots=(self.root / "repos",),
            allow_full_access=True,
        )
        self.manager = RemoteAccessManager(self.config)
        self.controller = FakeController()
        self.server = LocalControllerServer(
            controller=self.controller,
            port=0,
            remote_access=self.manager,
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.ledger_patch.stop()
        self.temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        cookie: str | None = None,
        host: str = "mac.example.ts.net",
        origin: str | None = "https://mac.example.ts.net",
    ) -> tuple[int, dict[str, str], dict]:
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        if cookie is not None:
            headers["Cookie"] = cookie
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        return (
            response.status,
            dict(response.getheaders()),
            json.loads(raw.decode("utf-8")),
        )

    def pair(self) -> str:
        status, headers, payload = self.request(
            "POST",
            "/api/remote/pair",
            {
                "code": self.server.remote_pairing_code,
                "device_label": "Pixel",
            },
        )
        self.assertEqual(status, 200, payload)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_remote_pairing_cookie_can_read_session(self) -> None:
        cookie = self.pair()

        status, _, payload = self.request(
            "GET",
            "/api/session",
            cookie=cookie,
            origin=None,
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["remote_mode"])
        self.assertEqual(payload["principal"]["label"], "Pixel")
        self.assertNotIn("token", json.dumps(payload))

    def test_remote_start_requires_authorized_repository_and_full_access_phrase(self) -> None:
        cookie = self.pair()
        base = {
            "repository_path": str(self.repository),
            "initial_instruction": "Task",
            "project_title": "Project",
            "chat_title": "Chat",
            "sandbox": "danger-full-access",
        }
        status, _, payload = self.request("POST", "/api/runs/start", base, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "remote_full_access_confirmation_required")

        status, _, payload = self.request(
            "POST",
            "/api/runs/start",
            {
                **base,
                "full_access_confirmation": REMOTE_FULL_ACCESS_CONFIRMATION,
            },
            cookie=cookie,
        )
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.controller.start_calls[-1]["repository_path"], str(self.repository))

    def test_untrusted_host_and_origin_fail_closed(self) -> None:
        status, _, payload = self.request(
            "POST",
            "/api/remote/pair",
            {"code": "x", "device_label": "Phone"},
            host="public.example.com",
            origin="https://public.example.com",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["reason_code"], "invalid_host")

    def test_remote_control_can_cancel_current_run(self) -> None:
        cookie = self.pair()

        status, _, payload = self.request(
            "POST",
            "/api/runs/current/cancel",
            {},
            cookie=cookie,
        )

        self.assertEqual(status, 202, payload)
        self.assertEqual(self.controller.cancel_calls, 1)

    def test_remote_control_can_force_quota_resume(self) -> None:
        cookie = self.pair()

        status, _, payload = self.request(
            "POST",
            "/api/runs/current/quota-resume",
            {},
            cookie=cookie,
        )

        self.assertEqual(status, 202, payload)
        self.assertEqual(self.controller.quota_resume_calls, 1)


class ControllerSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "ledger.db"
        self.ledger_patch = mock.patch.object(ledger, "DB_PATH", self.database_path)
        self.ledger_patch.start()

    def tearDown(self) -> None:
        self.ledger_patch.stop()
        self.temporary_directory.cleanup()

    def test_snapshot_round_trip(self) -> None:
        ledger.save_local_controller_snapshot(
            {
                "active_run_id": "run-1",
                "controller_state": "waiting_for_approval",
                "pending_approval": None,
            }
        )
        self.assertEqual(
            ledger.load_local_controller_snapshot(),
            {
                "active_run_id": "run-1",
                "controller_state": "waiting_for_approval",
                "pending_approval": None,
            },
        )

    def test_new_controller_restores_active_run_from_snapshot(self) -> None:
        run_id = ledger.create_run("Task")
        ledger.save_local_controller_snapshot(
            {
                "active_run_id": run_id,
                "controller_state": "idle",
                "pending_approval": None,
            }
        )

        controller = LocalController(ledger=ledger)

        self.assertEqual(controller.session.active_run_id, run_id)
        self.assertEqual(controller.session.controller_state, "idle")

    def test_new_controller_does_not_restore_terminal_run(self) -> None:
        run_id = ledger.create_run("Task")
        ledger.update_run_status(run_id, RunStatus.NEEDS_REVIEW)
        ledger.save_local_controller_snapshot(
            {
                "active_run_id": run_id,
                "controller_state": "blocked",
                "pending_approval": None,
            }
        )

        controller = LocalController(ledger=ledger)

        self.assertIsNone(controller.session.active_run_id)
        self.assertEqual(controller.session.controller_state, "idle")
        self.assertIsNone(
            ledger.load_local_controller_snapshot()["active_run_id"]
        )
