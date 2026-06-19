from __future__ import annotations

import shutil
import subprocess


ACTIVATE_APP_METHOD = "osascript_activate_app"
FRONTMOST_APP_METHOD = "osascript_system_events_frontmost_app"


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def activate_app(app_name: str) -> dict:
    if shutil.which("osascript") is None:
        return {
            "activated": False,
            "app_name": app_name,
            "method": ACTIVATE_APP_METHOD,
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
                f"tell application {_applescript_string(app_name)} to activate",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "activated": False,
            "app_name": app_name,
            "method": ACTIVATE_APP_METHOD,
            "error": f"Failed to run osascript: {exc}",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    activated = result.returncode == 0
    error = None
    if not activated:
        error = (result.stderr or result.stdout or "").strip()
        if not error:
            error = f"osascript exited with status {result.returncode}."

    return {
        "activated": activated,
        "app_name": app_name,
        "method": ACTIVATE_APP_METHOD,
        "error": error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def get_frontmost_app() -> dict:
    if shutil.which("osascript") is None:
        return {
            "frontmost_app": None,
            "method": FRONTMOST_APP_METHOD,
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
                (
                    'tell application "System Events" to get name of first '
                    "application process whose frontmost is true"
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "frontmost_app": None,
            "method": FRONTMOST_APP_METHOD,
            "error": f"Failed to run osascript: {exc}",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    frontmost_app = result.stdout.strip() if result.returncode == 0 else None
    error = None
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        if not error:
            error = f"osascript exited with status {result.returncode}."

    return {
        "frontmost_app": frontmost_app or None,
        "method": FRONTMOST_APP_METHOD,
        "error": error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def activate_chatgpt(app_name: str = "ChatGPT") -> dict:
    activation_result = activate_app(app_name)
    frontmost_result = get_frontmost_app()

    activated = bool(activation_result["activated"])
    frontmost_app = frontmost_result["frontmost_app"]
    is_frontmost = activated and frontmost_app == app_name

    error = None
    if not activated:
        error = activation_result["error"] or f"Failed to activate app: {app_name}"
    elif frontmost_result["error"]:
        error = frontmost_result["error"]
    elif not is_frontmost:
        error = f"Expected frontmost app {app_name!r}, got {frontmost_app!r}."

    return {
        "activated": activated,
        "app_name": app_name,
        "frontmost_app": frontmost_app,
        "is_frontmost": is_frontmost,
        "activation_result": activation_result,
        "frontmost_result": frontmost_result,
        "error": error,
    }
