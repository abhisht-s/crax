from __future__ import annotations

import shutil
import subprocess


ACTIVATE_APP_METHOD = "osascript_activate_app"
ACTIVATE_BUNDLE_METHOD = "osascript_activate_bundle"
FRONTMOST_APP_METHOD = "osascript_system_events_frontmost_app"
CLASSIC_CHATGPT_BUNDLE_ID = "com.openai.chat"


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


def activate_bundle_identifier(bundle_id: str) -> dict:
    if shutil.which("osascript") is None:
        return {
            "activated": False,
            "bundle_id": bundle_id,
            "method": ACTIVATE_BUNDLE_METHOD,
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
                f"tell application id {_applescript_string(bundle_id)} to activate",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "activated": False,
            "bundle_id": bundle_id,
            "method": ACTIVATE_BUNDLE_METHOD,
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
        "bundle_id": bundle_id,
        "method": ACTIVATE_BUNDLE_METHOD,
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
                    'tell application "System Events" to tell first application '
                    'process whose frontmost is true to return (name as text) & linefeed & '
                    '((bundle identifier) as text)'
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

    frontmost_app = None
    frontmost_bundle_id = None
    if result.returncode == 0:
        parts = result.stdout.strip().splitlines()
        frontmost_app = parts[0].strip() if parts else None
        frontmost_bundle_id = parts[1].strip() if len(parts) > 1 else None
    error = None
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        if not error:
            error = f"osascript exited with status {result.returncode}."

    return {
        "frontmost_app": frontmost_app or None,
        "frontmost_bundle_id": frontmost_bundle_id or None,
        "method": FRONTMOST_APP_METHOD,
        "error": error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def activate_chatgpt(app_name: str = "ChatGPT") -> dict:
    use_classic_bundle = app_name.casefold() == "chatgpt"
    activation_result = (
        activate_bundle_identifier(CLASSIC_CHATGPT_BUNDLE_ID)
        if use_classic_bundle
        else activate_app(app_name)
    )
    frontmost_result = get_frontmost_app()

    activated = bool(activation_result["activated"])
    frontmost_app = frontmost_result["frontmost_app"]
    frontmost_bundle_id = frontmost_result.get("frontmost_bundle_id")
    is_frontmost = activated and (
        frontmost_bundle_id == CLASSIC_CHATGPT_BUNDLE_ID
        if use_classic_bundle
        else frontmost_app == app_name
    )

    error = None
    if not activated:
        error = activation_result["error"] or f"Failed to activate app: {app_name}"
    elif frontmost_result["error"]:
        error = frontmost_result["error"]
    elif not is_frontmost:
        if use_classic_bundle:
            error = (
                f"Expected frontmost Classic ChatGPT bundle {CLASSIC_CHATGPT_BUNDLE_ID!r}, "
                f"got {frontmost_bundle_id!r}."
            )
        else:
            error = f"Expected frontmost app {app_name!r}, got {frontmost_app!r}."

    return {
        "activated": activated,
        "app_name": app_name,
        "bundle_id": CLASSIC_CHATGPT_BUNDLE_ID if use_classic_bundle else None,
        "frontmost_app": frontmost_app,
        "frontmost_bundle_id": frontmost_bundle_id,
        "is_frontmost": is_frontmost,
        "activation_result": activation_result,
        "frontmost_result": frontmost_result,
        "error": error,
    }
