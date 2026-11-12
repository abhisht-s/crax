"""Focused progress snapshot coverage using a temporary ledger and no workers."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent import ledger
from agent.local_controller import ControllerSessionRuntime, LocalController, LocalControllerSession
from agent.local_server import LOCAL_SERVER_TOKEN_HEADER, LocalControllerServer, _make_handler
from tests.local_socket_test_support import requires_localhost_ephemeral_bind


class DashboardProgressSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = mock.patch.object(ledger, "DB_PATH", Path(self.directory.name) / "ledger.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_a = ledger.create_run("A")
        self.run_b = ledger.create_run("B")
        self.controller = LocalController(session=LocalControllerSession(), ledger=ledger)
        for run_id in (self.run_a, self.run_b):
            self.controller._sessions[run_id] = ControllerSessionRuntime(run_id=run_id)
        self.controller.session.active_run_id = self.run_a

    def add(self, run_id, kind="command_finished", metadata=None, invocation="inv-a"):
        return ledger.add_codex_progress_event(run_id, invocation, {
            "kind": kind, "summary": "working note" if kind == "assistant_commentary" else None,
            "metadata": metadata or {},
        })

    def test_snapshot_opens_at_tail_without_changing_forward_cursor_reads(self):
        first = self.add(self.run_a)
        for _ in range(120):
            last = self.add(self.run_a)
        other = self.add(self.run_b)
        snapshot = self.controller.get_run_progress(self.run_a, snapshot=True, limit=5, after_sequence=999999)
        progress = snapshot.metadata["progress"]
        self.assertEqual(len(progress["events"]), 5)
        self.assertEqual(progress["latest_sequence"], last["sequence"])
        self.assertEqual(progress["events"][-1], last)
        self.assertNotIn(first, progress["events"])
        self.assertNotIn(other, progress["events"])
        forward = self.controller.get_run_progress(self.run_a, limit=5)
        self.assertEqual(forward.metadata["progress"]["events"][0], first)
        next_event = self.add(self.run_a)
        delta = self.controller.get_run_progress(self.run_a, after_sequence=progress["latest_sequence"])
        self.assertEqual(delta.metadata["progress"]["events"], [next_event])

    def test_snapshot_includes_sparse_working_notes_session_and_plan_in_order(self):
        process = self.add(self.run_a, "process_started")
        thread = self.add(self.run_a, "codex_json_event", {
            "event_type": "thread.started", "value_summary": {"codex_session_id": "session-a"},
        })
        plan = self.add(self.run_a, "codex_json_event", {
            "value_summary": {"item_type": "todo_list", "plan_items": [{"label": "Current plan", "completed": False}]},
        })
        notes = [self.add(self.run_a, "assistant_commentary") for _ in range(12)]
        for _ in range(120):
            last = self.add(self.run_a)
        snapshot = ledger.get_codex_progress_snapshot(self.run_a, limit=5)
        self.assertEqual(snapshot[:3], [process, thread, plan])
        self.assertEqual([e for e in snapshot if e["kind"] == "assistant_commentary"], notes[-8:])
        self.assertEqual(snapshot[-1], last)
        sequences = [e["sequence"] for e in snapshot]
        self.assertEqual(sequences, sorted(set(sequences)))
        # A new process boundary follows old context, allowing the client to clear it.
        new_process = self.add(self.run_a, "process_started", invocation="inv-b")
        refreshed = ledger.get_codex_progress_snapshot(self.run_a, limit=5)
        self.assertEqual(refreshed[-1], new_process)
        self.assertNotIn(process, refreshed)

    def test_snapshot_deduplicates_context_and_handles_malformed_metadata(self):
        note = self.add(self.run_a, "assistant_commentary")
        ledger.add_event(self.run_a, ledger.CODEX_PROGRESS_EVENT_TYPE, "Malformed", {})
        with ledger._connect() as connection:
            connection.execute("UPDATE events SET metadata_json = 'invalid json' WHERE message = 'Malformed'")
        snapshot = ledger.get_codex_progress_snapshot(self.run_a)
        self.assertEqual(snapshot.count(note), 1)
        self.assertEqual(snapshot[-1]["status"], "malformed")

    def test_empty_and_unknown_sessions_and_current_snapshot(self):
        empty = self.controller.get_run_progress(self.run_a, snapshot=True)
        self.assertTrue(empty.ok)
        self.assertEqual(empty.metadata["progress"]["events"], [])
        self.assertEqual(empty.metadata["progress"]["latest_sequence"], 0)
        unknown = self.controller.get_run_progress("unknown", snapshot=True)
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.reason_code, "run_not_found")
        note = self.add(self.run_a, "assistant_commentary")
        current = self.controller.get_current_progress(snapshot=True)
        self.assertEqual(current.metadata["progress"]["events"], [note])

    def test_stream_drains_available_batches_without_heartbeat_delay(self):
        events = [self.add(self.run_a) for _ in range(5)]
        handler_class = _make_handler(LocalControllerServer(controller=self.controller, port=0))
        handler = object.__new__(handler_class)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._write_sse_event = mock.Mock(return_value=True)
        handler._write_sse_comment = mock.Mock(side_effect=[True, True, False])
        with mock.patch("agent.local_server.time.sleep") as sleep:
            handler._handle_progress_sse(run_id=self.run_a, after_sequence=0, limit=2)
        sleep.assert_not_called()
        self.assertEqual([call.args[1] for call in handler._write_sse_event.call_args_list], events)

    @requires_localhost_ephemeral_bind
    def test_http_snapshot_and_following_cursor_use_the_requested_session(self):
        for _ in range(105):
            last = self.add(self.run_b)
        self.add(self.run_a)
        server = LocalControllerServer(controller=self.controller, port=0)
        server.start()
        self.addCleanup(server.shutdown)

        def read(path):
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
            try:
                connection.request("GET", path, headers={LOCAL_SERVER_TOKEN_HEADER: self.controller.session.token})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        path = f"/api/runs/{self.run_b}/progress"
        status, response = read(path + "?snapshot=1&limit=5")
        self.assertEqual(status, 200)
        progress = response["metadata"]["progress"]
        self.assertEqual(progress["run_id"], self.run_b)
        self.assertEqual(progress["latest_sequence"], last["sequence"])
        self.assertEqual(len(progress["events"]), 5)
        self.assertTrue(all(e["run_id"] == self.run_b for e in progress["events"]))
        status, response = read(path + f"?after_sequence={last['sequence']}")
        self.assertEqual(status, 200)
        self.assertEqual(response["metadata"]["progress"]["events"], [])
        status, response = read("/api/runs/current/progress?snapshot=1")
        self.assertEqual(status, 200)
        self.assertEqual(response["metadata"]["progress"]["run_id"], self.run_a)
        for invalid in ("snapshot=yes", "snapshot=", "snapshot=1&snapshot=0"):
            with self.subTest(invalid=invalid):
                status, response = read(path + "?" + invalid)
                self.assertEqual(status, 400)
                self.assertEqual(response["reason_code"], "invalid_progress_snapshot")
        status, _ = read("/api/runs/unknown/progress?snapshot=1")
        self.assertEqual(status, 404)

