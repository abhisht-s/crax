from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from agent import mac_paste


class MacPasteTests(unittest.TestCase):
    def test_paste_rechecks_classic_then_clears_and_pastes_in_that_process(self) -> None:
        completed = subprocess.CompletedProcess(["osascript"], 0, stdout="", stderr="")
        with mock.patch.object(mac_paste.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(
            mac_paste, "get_frontmost_app", return_value={"frontmost_bundle_id": "com.openai.chat", "error": None}
        ), mock.patch.object(mac_paste.subprocess, "run", return_value=completed) as run:
            result = mac_paste.paste_clipboard_to_frontmost_app()

        self.assertTrue(result["pasted"])
        self.assertTrue(result["cleared_composer"])
        self.assertEqual(result["method"], mac_paste.PASTE_METHOD)
        script = run.call_args.args[0][2]
        self.assertIn('bundle identifier is "com.openai.chat"', script)
        self.assertIn('keystroke "a" using command down', script)
        self.assertIn("key code 51", script)
        self.assertIn('keystroke "v" using command down', script)

    def test_paste_refuses_to_post_when_classic_is_not_frontmost(self) -> None:
        with mock.patch.object(mac_paste.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(
            mac_paste, "get_frontmost_app", return_value={"frontmost_bundle_id": "com.openai.codex", "error": None}
        ), mock.patch.object(mac_paste.subprocess, "run") as run:
            result = mac_paste.paste_clipboard_to_frontmost_app()

        self.assertFalse(result["pasted"])
        self.assertFalse(result["cleared_composer"])
        self.assertIn("was not frontmost", result["error"])
        run.assert_not_called()

    def test_enter_rechecks_and_targets_only_classic(self) -> None:
        completed = subprocess.CompletedProcess(["osascript"], 0, stdout="", stderr="")
        with mock.patch.object(mac_paste.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(
            mac_paste, "get_frontmost_app", return_value={"frontmost_bundle_id": "com.openai.chat", "error": None}
        ), mock.patch.object(mac_paste.subprocess, "run", return_value=completed) as run:
            result = mac_paste.press_enter_in_frontmost_app()

        self.assertTrue(result["submitted"])
        script = run.call_args.args[0][2]
        self.assertIn('bundle identifier is "com.openai.chat"', script)
        self.assertIn("key code 36", script)
        self.assertNotIn("key code 51", script)
