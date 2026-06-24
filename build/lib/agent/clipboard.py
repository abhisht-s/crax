from __future__ import annotations

import shutil
import subprocess
import sys


def copy_to_clipboard(text: str) -> dict:
    if sys.platform != "darwin":
        return {
            "copied": False,
            "method": None,
            "error": "Clipboard copy is only supported on macOS via pbcopy.",
        }

    if shutil.which("pbcopy") is None:
        return {
            "copied": False,
            "method": None,
            "error": "pbcopy was not found on PATH.",
        }

    try:
        result = subprocess.run(
            ["pbcopy"],
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {
            "copied": False,
            "method": "pbcopy",
            "error": f"Failed to run pbcopy: {exc}",
        }

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        if not error:
            error = f"pbcopy exited with status {result.returncode}."
        return {
            "copied": False,
            "method": "pbcopy",
            "error": error,
        }

    return {
        "copied": True,
        "method": "pbcopy",
        "error": None,
    }
