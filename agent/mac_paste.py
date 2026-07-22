from __future__ import annotations

import shutil
import subprocess

from agent.mac_app_control import CLASSIC_CHATGPT_BUNDLE_ID, get_frontmost_app


PASTE_METHOD = "osascript_system_events_classic_chatgpt_clear_then_cmd_v"
ENTER_METHOD = "osascript_system_events_classic_chatgpt_enter"


def _classic_chatgpt_frontmost_error() -> str | None:
    frontmost = get_frontmost_app()
    if frontmost.get("error"):
        return str(frontmost["error"])
    bundle_id = frontmost.get("frontmost_bundle_id")
    if bundle_id != CLASSIC_CHATGPT_BUNDLE_ID:
        return (
            f"Classic ChatGPT bundle {CLASSIC_CHATGPT_BUNDLE_ID!r} was not frontmost "
            f"immediately before keyboard input (got {bundle_id!r})."
        )
    return None


def _classic_chatgpt_input_script(*, clear_composer: bool, submit: bool) -> str:
    commands = [
        'tell application "System Events"',
        f'    set targetApp to first application process whose bundle identifier is "{CLASSIC_CHATGPT_BUNDLE_ID}"',
        "    if not (frontmost of targetApp) then",
        "        set frontmost of targetApp to true",
        "        delay 0.05",
        "    end if",
        "    if not (frontmost of targetApp) then error \"Classic ChatGPT was not frontmost.\"",
        "    tell targetApp",
    ]
    if clear_composer:
        # The caller has just proved that ChatGPT's composer is focused.  Clear
        # it immediately before paste so a partial/stale draft cannot combine
        # with the agent payload.
        commands.extend(("        keystroke \"a\" using command down", "        key code 51"))
    if submit:
        commands.append("        key code 36")
    else:
        commands.append('        keystroke "v" using command down')
    commands.extend(("    end tell", "end tell"))
    return "\n".join(commands)


def _keyboard_input_result(*, method: str, success_key: str, script: str, cleared_composer: bool) -> dict:
    if shutil.which("osascript") is None:
        return {
            success_key: False,
            "method": method,
            "cleared_composer": cleared_composer,
            "error": "osascript was not found on PATH.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    frontmost_error = _classic_chatgpt_frontmost_error()
    if frontmost_error is not None:
        return {
            success_key: False,
            "method": method,
            "cleared_composer": False,
            "error": frontmost_error,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            success_key: False,
            "method": method,
            "cleared_composer": False,
            "error": f"Failed to run osascript: {exc}",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    error = None
    success = result.returncode == 0
    if not success:
        error = (result.stderr or result.stdout or "").strip() or f"osascript exited with status {result.returncode}."

    return {
        success_key: success,
        "method": method,
        "cleared_composer": bool(cleared_composer and success),
        "error": error,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


def paste_clipboard_to_frontmost_app() -> dict:
    """Clear the focused Classic composer, then paste into that exact app.

    The public name is retained for compatibility with existing callers.  It
    no longer posts a global frontmost-app keystroke.
    """

    return _keyboard_input_result(
        method=PASTE_METHOD,
        success_key="pasted",
        script=_classic_chatgpt_input_script(clear_composer=True, submit=False),
        cleared_composer=True,
    )


def press_enter_in_frontmost_app() -> dict:
    """Submit only to the already-verified Classic ChatGPT process."""

    return _keyboard_input_result(
        method=ENTER_METHOD,
        success_key="submitted",
        script=_classic_chatgpt_input_script(clear_composer=False, submit=True),
        cleared_composer=False,
    )
