from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import repository_picker


class RepositoryPickerTests(unittest.TestCase):
    def test_selected_directory_is_resolved_and_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.CompletedProcess(
                ["/usr/bin/osascript"],
                0,
                stdout=f"{directory}/\n",
                stderr="",
            )
            with (
                mock.patch.object(repository_picker.shutil, "which", return_value="/usr/bin/osascript"),
                mock.patch.object(repository_picker.subprocess, "run", return_value=completed) as run,
            ):
                result = repository_picker.choose_repository_directory()

        self.assertTrue(result.ok)
        self.assertTrue(result.selected)
        self.assertEqual(result.repository_path, str(Path(directory).resolve()))
        self.assertEqual(result.reason_code, "repository_picker_selected")
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")
        self.assertNotIn(directory, run.call_args.args[0])

    def test_closing_picker_is_a_successful_non_selection(self) -> None:
        completed = subprocess.CompletedProcess(
            ["/usr/bin/osascript"],
            0,
            stdout=f"{repository_picker._NO_SELECTION_SENTINEL}\n",
            stderr="",
        )
        with (
            mock.patch.object(repository_picker.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(repository_picker.subprocess, "run", return_value=completed),
        ):
            result = repository_picker.choose_repository_directory()

        self.assertTrue(result.ok)
        self.assertFalse(result.selected)
        self.assertIsNone(result.repository_path)
        self.assertEqual(result.reason_code, "repository_picker_closed")

    def test_unavailable_and_failed_picker_are_safe(self) -> None:
        with mock.patch.object(repository_picker.shutil, "which", return_value=None):
            unavailable = repository_picker.choose_repository_directory()
        self.assertFalse(unavailable.ok)
        self.assertEqual(unavailable.reason_code, "repository_picker_unavailable")

        completed = subprocess.CompletedProcess(
            ["/usr/bin/osascript"],
            1,
            stdout="",
            stderr="private detail",
        )
        with (
            mock.patch.object(repository_picker.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(repository_picker.subprocess, "run", return_value=completed),
        ):
            failed = repository_picker.choose_repository_directory()
        self.assertFalse(failed.ok)
        self.assertEqual(failed.reason_code, "repository_picker_failed")
        self.assertNotIn("private detail", failed.error_message or "")
