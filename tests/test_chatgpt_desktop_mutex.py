from __future__ import annotations

import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from agent.chatgpt_desktop_mutex import (
    ChatGPTDesktopMutex,
    controller_process_is_live,
    capture_mutex_identity,
)


def _hold_mutex_until_release(lock_path: str, ready_path: str, release_path: str) -> None:
    mutex = ChatGPTDesktopMutex(lock_path)
    hold = mutex.acquire("holder-run", controller_instance_id="holder")
    Path(ready_path).write_text("ready", encoding="utf-8")
    while not Path(release_path).exists():
        time.sleep(0.05)
    hold.release()


class ChatGPTDesktopMutexTests(unittest.TestCase):
    def test_live_owner_cannot_be_stolen_by_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "chatgpt-desktop.lock"
            mutex = ChatGPTDesktopMutex(lock_path)
            first = mutex.acquire("run-a", controller_instance_id="controller-a")
            self.assertTrue(first.ok)
            first.identity["acquired_at"] = "1999-01-01T00:00:00+00:00"
            second = ChatGPTDesktopMutex(lock_path).acquire(
                "run-b",
                controller_instance_id="controller-b",
            )
            self.assertFalse(second.ok)
            self.assertEqual(second.reason_code, "chatgpt_desktop_mutex_already_held")
            self.assertTrue(second.owner_is_live)
            first.release()

    def test_dead_owner_file_does_not_block_a_new_acquirer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "chatgpt-desktop.lock"
            lock_path.write_text(
                '{"pid": 1, "boot_id": "stale", "process_start_identity": "old",'
                ' "owning_run_id": "dead-run", "controller_instance_id": "dead"}',
                encoding="utf-8",
            )
            hold = ChatGPTDesktopMutex(lock_path).acquire(
                "live-run",
                controller_instance_id="live",
            )
            self.assertTrue(hold.ok)
            self.assertEqual(hold.identity["owning_run_id"], "live-run")
            hold.release()

    def test_pid_reuse_without_matching_start_identity_is_not_live(self) -> None:
        identity = capture_mutex_identity(
            owning_run_id="run-a",
            controller_instance_id="controller-a",
        )
        identity["process_start_identity"] = "not-this-process-start"
        self.assertFalse(controller_process_is_live(identity))
        live = capture_mutex_identity(
            owning_run_id="run-a",
            controller_instance_id="controller-a",
        )
        self.assertTrue(controller_process_is_live(live))

    def test_cross_process_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = str(Path(tmpdir) / "chatgpt-desktop.lock")
            ready_path = str(Path(tmpdir) / "ready")
            release_path = str(Path(tmpdir) / "release")
            worker = multiprocessing.get_context("spawn").Process(
                target=_hold_mutex_until_release,
                args=(lock_path, ready_path, release_path),
            )
            worker.start()
            deadline = time.time() + 10
            while not Path(ready_path).exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(Path(ready_path).exists())
            contested = ChatGPTDesktopMutex(lock_path).acquire(
                "challenger",
                controller_instance_id="challenger",
            )
            self.assertFalse(contested.ok)
            self.assertEqual(contested.reason_code, "chatgpt_desktop_mutex_already_held")
            Path(release_path).write_text("done", encoding="utf-8")
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)
            after = ChatGPTDesktopMutex(lock_path).acquire(
                "after",
                controller_instance_id="after",
            )
            self.assertTrue(after.ok)
            after.release()
