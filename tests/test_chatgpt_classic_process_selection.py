from __future__ import annotations

import types
import unittest
from unittest import mock

from agent import chatgpt_ax_capture as ax_capture
from agent import mac_ui_inspect


class ClassicChatGPTProcessSelectionTests(unittest.TestCase):
    def test_response_capture_uses_classic_bundle_pid_not_display_name_lookup(self) -> None:
        reader = types.SimpleNamespace(app_name="ChatGPT")

        with mock.patch.object(ax_capture, "resolve_classic_chatgpt_pid", return_value=1398) as resolve, mock.patch.object(
            ax_capture.subprocess, "run"
        ) as run:
            pid = ax_capture._AXReader._get_pid(reader)

        self.assertEqual(pid, 1398)
        resolve.assert_called_once_with()
        run.assert_not_called()

    def test_response_capture_fails_closed_when_classic_is_not_running(self) -> None:
        reader = types.SimpleNamespace(app_name="ChatGPT")

        with mock.patch.object(ax_capture, "resolve_classic_chatgpt_pid", return_value=None), mock.patch.object(
            ax_capture.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(ax_capture.AXCaptureError, "com.openai.chat"):
                ax_capture._AXReader._get_pid(reader)

        run.assert_not_called()

    def test_submission_inspector_uses_classic_bundle_pid_not_display_name_lookup(self) -> None:
        reader = types.SimpleNamespace(app_name="ChatGPT")

        with mock.patch.object(mac_ui_inspect, "resolve_classic_chatgpt_pid", return_value=1398) as resolve, mock.patch.object(
            mac_ui_inspect.subprocess, "run"
        ) as run:
            pid = mac_ui_inspect._AXSubmissionReader._get_pid(reader)

        self.assertEqual(pid, 1398)
        resolve.assert_called_once_with()
        run.assert_not_called()

    def test_submission_inspector_fails_closed_when_classic_is_not_running(self) -> None:
        reader = types.SimpleNamespace(app_name="ChatGPT")

        with mock.patch.object(mac_ui_inspect, "resolve_classic_chatgpt_pid", return_value=None), mock.patch.object(
            mac_ui_inspect.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(mac_ui_inspect.AXInspectError, "com.openai.chat"):
                mac_ui_inspect._AXSubmissionReader._get_pid(reader)

        run.assert_not_called()
