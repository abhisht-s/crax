from __future__ import annotations

import shutil
import subprocess


PASTE_METHOD = "osascript_system_events_cmd_v"
ENTER_METHOD = "osascript_system_events_enter"


def paste_clipboard_to_frontmost_app() -> dict:
    if shutil.which("osascript") is None:
        return {
            "pasted": False,
            "method": PASTE_METHOD,
            "error": "osascript was not found on PATH.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to keystroke "v" using command down',
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "pasted": False,
            "method": PASTE_METHOD,
            "error": f"Failed to run osascript: {exc}",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    error = None
    pasted = result.returncode == 0
    if not pasted:
        error = (result.stderr or result.stdout or "").strip()
        if not error:
            error = f"osascript exited with status {result.returncode}."

    return {
        "pasted": pasted,
        "method": PASTE_METHOD,
        "error": error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def press_enter_in_frontmost_app() -> dict:
    if shutil.which("osascript") is None:
        return {
            "submitted": False,
            "method": ENTER_METHOD,
            "error": "osascript was not found on PATH.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to key code 36',
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "submitted": False,
            "method": ENTER_METHOD,
            "error": f"Failed to run osascript: {exc}",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    error = None
    submitted = result.returncode == 0
    if not submitted:
        error = (result.stderr or result.stdout or "").strip()
        if not error:
            error = f"osascript exited with status {result.returncode}."

    return {
        "submitted": submitted,
        "method": ENTER_METHOD,
        "error": error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }
