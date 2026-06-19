from __future__ import annotations

import shutil
import subprocess

from agent.mac_app_control import activate_chatgpt


INSPECT_METHOD = "osascript_system_events_shallow_ui_inspect"


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _window_inspection_script(app_name: str) -> str:
    return f"""
on cleanText(rawValue)
    try
        if rawValue is missing value then return ""
        set textValue to rawValue as text
        set AppleScript's text item delimiters to tab
        set textParts to every text item of textValue
        set AppleScript's text item delimiters to " "
        set textValue to textParts as text
        set AppleScript's text item delimiters to ""
        if (length of textValue) > 160 then set textValue to (text 1 thru 160 of textValue) & "..."
        return textValue
    on error
        return ""
    end try
end cleanText

on appendError(errorScope, errorMessage, errorNumber)
    set outputText to outputText & "ERROR" & tab & errorScope & tab & my cleanText(errorMessage & " (" & (errorNumber as text) & ")") & linefeed
end appendError

set appName to {_applescript_string(app_name)}
set outputText to ""

tell application "System Events"
    try
        set frontmostName to name of first application process whose frontmost is true
        set outputText to outputText & "FRONTMOST" & tab & my cleanText(frontmostName) & linefeed
    on error errMsg number errNum
        my appendError("frontmost_app", errMsg, errNum)
    end try

    try
        set targetProcess to first application process whose name is appName
        set winItems to windows of targetProcess
        set winCount to count of winItems
        repeat with winIndex from 1 to winCount
            set winItem to item winIndex of winItems
            set winName to ""
            set winRole to ""
            set winSubrole to ""
            try
                set winName to my cleanText(name of winItem)
            end try
            try
                set winRole to my cleanText(role of winItem)
            end try
            try
                set winSubrole to my cleanText(subrole of winItem)
            end try
            set outputText to outputText & "WINDOW" & tab & (winIndex as text) & tab & winRole & tab & winSubrole & tab & winName & linefeed
        end repeat
        if winCount is 0 then my appendError("windows", "No windows reported by System Events", 0)
    on error errMsg number errNum
        my appendError("windows", errMsg, errNum)
    end try
end tell

return outputText
"""


def _run_window_inspection(app_name: str) -> dict:
    if shutil.which("osascript") is None:
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "errors": ["osascript was not found on PATH."],
        }

    try:
        result = subprocess.run(
            ["osascript"],
            input=_window_inspection_script(app_name),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "errors": [f"Failed to run osascript: {exc}"],
        }

    errors = []
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "").strip()
        if not error_text:
            error_text = f"osascript exited with status {result.returncode}."
        errors.append(error_text)

    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
        "errors": errors,
    }


def _parse_window_stdout(stdout: str) -> dict:
    parsed = {
        "frontmost_app": None,
        "windows": [],
        "errors": [],
    }

    for raw_line in stdout.splitlines():
        parts = raw_line.split("\t")
        if not parts:
            continue
        if parts[0] == "FRONTMOST":
            if len(parts) > 1 and parts[1]:
                parsed["frontmost_app"] = parts[1]
        elif parts[0] == "WINDOW":
            parsed["windows"].append(
                {
                    "window_index": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                    "role": parts[2] if len(parts) > 2 else "",
                    "subrole": parts[3] if len(parts) > 3 else "",
                    "name": parts[4] if len(parts) > 4 else "",
                }
            )
        elif parts[0] == "ERROR":
            if len(parts) >= 3:
                parsed["errors"].append(f"{parts[1]}: {parts[2]}")
            elif len(parts) >= 2:
                parsed["errors"].append(parts[1])

    return parsed


def inspect_chatgpt_ui(app_name: str = "ChatGPT") -> dict:
    activation_result = activate_chatgpt(app_name)
    errors: list[str] = []
    if activation_result["error"]:
        errors.append(activation_result["error"])

    result = {
        "activated": bool(activation_result["activated"]),
        "frontmost_app": activation_result["frontmost_app"],
        "windows": [],
        "focused_element": None,
        "text_input_candidates": [],
        "button_candidates": [],
        "errors": errors,
        "raw_stdout": "",
        "raw_stderr": "",
        "exit_code": None,
        "activation_result": activation_result,
        "method": INSPECT_METHOD,
    }

    if not activation_result["is_frontmost"]:
        return result

    inspection = _run_window_inspection(app_name)
    parsed = _parse_window_stdout(inspection["stdout"])

    warnings = inspection["errors"] + parsed["errors"]
    if not parsed["windows"] and not warnings:
        warnings.append("No shallow window information was reported by System Events.")
    warnings.append(
        "Focused element and text input candidate inspection are intentionally unavailable in Stage 5.6A."
    )

    result.update(
        {
            "frontmost_app": parsed["frontmost_app"] or activation_result["frontmost_app"],
            "windows": parsed["windows"],
            "errors": errors + warnings,
            "raw_stdout": inspection["stdout"],
            "raw_stderr": inspection["stderr"],
            "exit_code": inspection["exit_code"],
        }
    )
    return result
