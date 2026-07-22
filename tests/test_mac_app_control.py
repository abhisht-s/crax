from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from agent import mac_app_control


class MacAppControlTests(unittest.TestCase):
    def test_activate_chatgpt_targets_and_verifies_classic_bundle(self) -> None:
        activation = subprocess.CompletedProcess(["osascript"], 0, stdout="", stderr="")
        frontmost = subprocess.CompletedProcess(
            ["osascript"], 0, stdout="ChatGPT\ncom.openai.chat\n", stderr=""
        )

        with mock.patch.object(mac_app_control.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(
            mac_app_control.subprocess, "run", side_effect=[activation, frontmost]
        ) as run:
            result = mac_app_control.activate_chatgpt()

        self.assertTrue(result["activated"])
        self.assertTrue(result["is_frontmost"])
        self.assertEqual(result["bundle_id"], "com.openai.chat")
        self.assertEqual(result["frontmost_bundle_id"], "com.openai.chat")
        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "osascript",
                "-e",
                'tell application id "com.openai.chat" to activate',
            ],
        )

    def test_activate_chatgpt_rejects_work_codex_despite_matching_name(self) -> None:
        activation = subprocess.CompletedProcess(["osascript"], 0, stdout="", stderr="")
        frontmost = subprocess.CompletedProcess(
            ["osascript"], 0, stdout="ChatGPT\ncom.openai.codex\n", stderr=""
        )

        with mock.patch.object(mac_app_control.shutil, "which", return_value="/usr/bin/osascript"), mock.patch.object(
            mac_app_control.subprocess, "run", side_effect=[activation, frontmost]
        ):
            result = mac_app_control.activate_chatgpt()

        self.assertTrue(result["activated"])
        self.assertFalse(result["is_frontmost"])
        self.assertIn("Classic ChatGPT bundle", result["error"])

