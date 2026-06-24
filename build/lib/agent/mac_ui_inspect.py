from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from ctypes import POINTER, byref, c_bool, c_char_p, c_int, c_long, c_ulong, c_void_p, create_string_buffer
from dataclasses import dataclass

from agent.mac_app_control import activate_chatgpt


INSPECT_METHOD = "osascript_system_events_shallow_ui_inspect"
SUBMISSION_UI_INSPECT_METHOD = "macos_accessibility_submission_ui_inspect"
SEND_BUTTON_AXPRESS_METHOD = "macos_accessibility_axpress_send_button"
DEFAULT_MAX_DEPTH = 18
DEFAULT_MAX_NODES = 1200

TEXT_INPUT_ROLES = {"AXTextArea", "AXTextField"}
TEXT_ROLES = {"AXStaticText", "AXTextArea", "AXTextField", "AXHeading"}
BUTTON_ROLES = {"AXButton"}
TEXT_ATTRIBUTES = ("AXValue", "AXDescription", "AXTitle")
META_ATTRIBUTES = ("AXIdentifier", "AXTitle", "AXDescription", "AXValue")


class AXInspectError(RuntimeError):
    pass


@dataclass(frozen=True)
class _AXElementSnapshot:
    path: str
    depth: int
    role: str
    subrole: str
    title: str
    description: str
    identifier: str
    value: str
    enabled: bool | None
    focused: bool | None
    actions: tuple[str, ...]


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


class _AXSubmissionReader:
    def __init__(self, app_name: str, max_depth: int, max_nodes: int) -> None:
        if sys.platform != "darwin":
            raise AXInspectError("ChatGPT desktop AX inspection is only supported on macOS.")
        self.app_name = app_name
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._attr_cache: dict[str, int] = {}
        self._action_cache: dict[str, int] = {}
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._cf.CFGetTypeID.argtypes = [c_void_p]
        self._cf.CFGetTypeID.restype = c_ulong
        self._cf.CFStringGetTypeID.restype = c_ulong
        self._cf.CFArrayGetTypeID.restype = c_ulong
        self._cf.CFBooleanGetTypeID.restype = c_ulong
        self._cf.CFBooleanGetValue.argtypes = [c_void_p]
        self._cf.CFBooleanGetValue.restype = c_bool
        self._cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_ulong]
        self._cf.CFStringCreateWithCString.restype = c_void_p
        self._cf.CFStringGetLength.argtypes = [c_void_p]
        self._cf.CFStringGetLength.restype = c_long
        self._cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_ulong]
        self._cf.CFStringGetCString.restype = c_bool
        self._cf.CFArrayGetCount.argtypes = [c_void_p]
        self._cf.CFArrayGetCount.restype = c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        self._cf.CFArrayGetValueAtIndex.restype = c_void_p

        self._ax.AXUIElementCreateApplication.argtypes = [c_int]
        self._ax.AXUIElementCreateApplication.restype = c_void_p
        self._ax.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyAttributeValue.restype = c_int
        self._ax.AXUIElementCopyActionNames.argtypes = [c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyActionNames.restype = c_int
        self._ax.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
        self._ax.AXUIElementPerformAction.restype = c_int

        self._string_type = self._cf.CFStringGetTypeID()
        self._array_type = self._cf.CFArrayGetTypeID()
        self._boolean_type = self._cf.CFBooleanGetTypeID()

    def _attribute_ref(self, name: str) -> int:
        if name not in self._attr_cache:
            self._attr_cache[name] = self._cf.CFStringCreateWithCString(
                None,
                name.encode("utf-8"),
                0x08000100,
            )
        return self._attr_cache[name]

    def _action_ref(self, name: str) -> int:
        if name not in self._action_cache:
            self._action_cache[name] = self._cf.CFStringCreateWithCString(
                None,
                name.encode("utf-8"),
                0x08000100,
            )
        return self._action_cache[name]

    def _copy_attribute(self, element: int, name: str) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyAttributeValue(
            c_void_p(element),
            c_void_p(self._attribute_ref(name)),
            byref(output),
        )
        if error != 0 or not output.value:
            return None
        return output.value

    def _cf_string(self, value: int | None) -> str:
        if not value:
            return ""
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._string_type:
                return ""
            length = self._cf.CFStringGetLength(c_void_p(value))
            buffer = create_string_buffer(max(16, length * 4 + 1))
            ok = self._cf.CFStringGetCString(c_void_p(value), buffer, len(buffer), 0x08000100)
            if not ok:
                return ""
            return buffer.value.decode("utf-8", "replace")
        except Exception:
            return ""

    def _cf_bool(self, value: int | None) -> bool | None:
        if not value:
            return None
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._boolean_type:
                return None
            return bool(self._cf.CFBooleanGetValue(c_void_p(value)))
        except Exception:
            return None

    def _array_strings(self, value: int | None) -> tuple[str, ...]:
        if not value:
            return ()
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._array_type:
                return ()
            return tuple(
                text
                for text in (
                    self._cf_string(self._cf.CFArrayGetValueAtIndex(c_void_p(value), index))
                    for index in range(self._cf.CFArrayGetCount(c_void_p(value)))
                )
                if text
            )
        except Exception:
            return ()

    def _children(self, element: int) -> list[int]:
        for name in ("AXChildren", "AXVisibleChildren"):
            value = self._copy_attribute(element, name)
            if not value:
                continue
            try:
                if self._cf.CFGetTypeID(c_void_p(value)) != self._array_type:
                    continue
                count = self._cf.CFArrayGetCount(c_void_p(value))
                if count > 0:
                    return [
                        self._cf.CFArrayGetValueAtIndex(c_void_p(value), index)
                        for index in range(count)
                    ]
            except Exception:
                continue
        return []

    def _get_pid(self) -> int:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                (
                    'tell application "System Events" to get unix id of first '
                    f'application process whose name is "{_applescript_string_inner(self.app_name)}"'
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            raise AXInspectError(error or f"Could not find process for app {self.app_name!r}.")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise AXInspectError(f"Could not parse process id for app {self.app_name!r}.") from exc

    def _focused_window(self) -> int:
        pid = self._get_pid()
        app_element = self._ax.AXUIElementCreateApplication(pid)
        if not app_element:
            raise AXInspectError("Could not create AX application element.")
        focused_window = self._copy_attribute(app_element, "AXFocusedWindow")
        if not focused_window:
            raise AXInspectError("Could not read AXFocusedWindow from ChatGPT.")
        return focused_window

    def collect(self) -> tuple[list[_AXElementSnapshot], dict]:
        focused_window = self._focused_window()
        snapshots: list[_AXElementSnapshot] = []
        visited_count = 0

        def walk(element: int, path: str, depth: int) -> None:
            nonlocal visited_count
            if visited_count >= self.max_nodes:
                return
            visited_count += 1
            role = self._cf_string(self._copy_attribute(element, "AXRole"))
            subrole = self._cf_string(self._copy_attribute(element, "AXSubrole"))
            actions = self._array_strings(self._copy_actions(element))
            snapshot = _AXElementSnapshot(
                path=path,
                depth=depth,
                role=role,
                subrole=subrole,
                title=self._cf_string(self._copy_attribute(element, "AXTitle")),
                description=self._cf_string(self._copy_attribute(element, "AXDescription")),
                identifier=self._cf_string(self._copy_attribute(element, "AXIdentifier")),
                value=self._cf_string(self._copy_attribute(element, "AXValue")),
                enabled=self._cf_bool(self._copy_attribute(element, "AXEnabled")),
                focused=self._cf_bool(self._copy_attribute(element, "AXFocused")),
                actions=actions,
            )
            snapshots.append(snapshot)
            if depth >= self.max_depth:
                return
            for index, child in enumerate(self._children(element), start=1):
                if visited_count >= self.max_nodes:
                    break
                walk(child, f"{path}.{index}", depth + 1)

        walk(focused_window, "FW", 0)
        return snapshots, {
            "visited_nodes": visited_count,
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
        }

    def _copy_actions(self, element: int) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyActionNames(c_void_p(element), byref(output))
        if error != 0 or not output.value:
            return None
        return output.value

    def _element_at_path(self, path: str) -> int:
        if path == "FW":
            return self._focused_window()
        if not path.startswith("FW."):
            raise AXInspectError(f"Unsupported AX path: {path}")
        element = self._focused_window()
        for raw_index in path.removeprefix("FW.").split("."):
            try:
                child_index = int(raw_index) - 1
            except ValueError as exc:
                raise AXInspectError(f"Unsupported AX path: {path}") from exc
            children = self._children(element)
            if child_index < 0 or child_index >= len(children):
                raise AXInspectError(f"AX path no longer exists: {path}")
            element = children[child_index]
        return element

    def press(self, path: str) -> dict:
        element = self._element_at_path(path)
        error_code = self._ax.AXUIElementPerformAction(
            c_void_p(element),
            c_void_p(self._action_ref("AXPress")),
        )
        return {
            "pressed": error_code == 0,
            "method": SEND_BUTTON_AXPRESS_METHOD,
            "path": path,
            "error": None if error_code == 0 else f"AXPress failed with error code {error_code}.",
            "exit_code": error_code,
        }


def _applescript_string_inner(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _element_text(snapshot: _AXElementSnapshot) -> str:
    parts = []
    for text in (snapshot.value, snapshot.description, snapshot.title):
        stripped = text.strip()
        if stripped and stripped not in parts:
            parts.append(stripped)
    return "\n\n".join(parts)


def _snapshot_dict(snapshot: _AXElementSnapshot, include_text: bool = True) -> dict:
    data = {
        "path": snapshot.path,
        "role": snapshot.role,
        "subrole": snapshot.subrole,
        "title": snapshot.title,
        "description": snapshot.description,
        "identifier": snapshot.identifier,
        "enabled": snapshot.enabled,
        "focused": snapshot.focused,
        "actions": list(snapshot.actions),
    }
    if include_text:
        data["text"] = _element_text(snapshot)
        data["value"] = snapshot.value
    return data


def _looks_like_send_button(snapshot: _AXElementSnapshot) -> bool:
    if snapshot.role not in BUTTON_ROLES:
        return False
    if snapshot.enabled is False:
        return False
    text = " ".join((snapshot.title, snapshot.description, snapshot.identifier)).lower()
    if "send" in text:
        return True
    return "AXPress" in snapshot.actions and any(token in text for token in ("submit", "arrow", "up"))


def _message_candidates(snapshots: list[_AXElementSnapshot]) -> list[dict]:
    candidates = []
    previous_text = ""
    for snapshot in snapshots:
        if snapshot.role not in TEXT_ROLES:
            continue
        text = _element_text(snapshot).strip()
        if not text or text == previous_text:
            continue
        previous_text = text
        candidates.append(
            {
                "index": len(candidates),
                "path": snapshot.path,
                "role": snapshot.role,
                "subrole": snapshot.subrole,
                "text": text,
                "text_length": len(text),
                "focused": snapshot.focused,
            }
        )
    return candidates


def inspect_chatgpt_submission_ui(
    app_name: str = "ChatGPT",
    marker_text: str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict:
    result = {
        "ok": False,
        "method": SUBMISSION_UI_INSPECT_METHOD,
        "app_name": app_name,
        "focused_element": None,
        "focused_composer": None,
        "text_input_candidates": [],
        "button_candidates": [],
        "send_button": None,
        "message_candidates": [],
        "marker_text_present_in_composer": False,
        "marker_text_candidate_count": 0,
        "ax_stats": {},
        "error": None,
    }
    try:
        snapshots, stats = _AXSubmissionReader(app_name, max_depth, max_nodes).collect()
    except (AXInspectError, OSError) as exc:
        result["error"] = str(exc)
        return result

    focused = next((item for item in snapshots if item.focused is True), None)
    text_inputs = [_snapshot_dict(item) for item in snapshots if item.role in TEXT_INPUT_ROLES]
    focused_composer = focused if focused is not None and focused.role in TEXT_INPUT_ROLES else None
    buttons = [_snapshot_dict(item, include_text=False) for item in snapshots if item.role in BUTTON_ROLES]
    send_button = next((item for item in snapshots if _looks_like_send_button(item)), None)
    messages = _message_candidates(snapshots)

    if marker_text:
        marker_candidate_count = sum(
            1 for candidate in messages if marker_text in candidate.get("text", "")
        )
    else:
        marker_candidate_count = 0

    result.update(
        {
            "ok": True,
            "focused_element": _snapshot_dict(focused) if focused is not None else None,
            "focused_composer": _snapshot_dict(focused_composer) if focused_composer is not None else None,
            "text_input_candidates": text_inputs,
            "button_candidates": buttons,
            "send_button": _snapshot_dict(send_button, include_text=False) if send_button is not None else None,
            "message_candidates": messages,
            "marker_text_present_in_composer": bool(
                marker_text
                and focused_composer is not None
                and marker_text in _element_text(focused_composer)
            ),
            "marker_text_candidate_count": marker_candidate_count,
            "ax_stats": stats,
        }
    )
    return result


def press_chatgpt_send_button(app_name: str, button_path: str) -> dict:
    try:
        return _AXSubmissionReader(
            app_name,
            DEFAULT_MAX_DEPTH,
            DEFAULT_MAX_NODES,
        ).press(button_path)
    except (AXInspectError, OSError) as exc:
        return {
            "pressed": False,
            "method": SEND_BUTTON_AXPRESS_METHOD,
            "path": button_path,
            "error": str(exc),
            "exit_code": None,
        }


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
