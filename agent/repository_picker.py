from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


_NO_SELECTION_SENTINEL = "__AGENT_REPOSITORY_PICKER_NO_SELECTION__"
_PICKER_LOCK = threading.Lock()
_PICKER_SCRIPT = f'''
try
    set selectedFolder to choose folder with prompt "Choose a repository folder"
    return POSIX path of selectedFolder
on error number -128
    return "{_NO_SELECTION_SENTINEL}"
end try
'''.strip()


@dataclass(frozen=True)
class RepositoryPickerResult:
    ok: bool
    selected: bool = False
    repository_path: str | None = None
    reason_code: str = "repository_picker_failed"
    error_message: str | None = None


def choose_repository_directory() -> RepositoryPickerResult:
    osascript_path = shutil.which("osascript")
    if osascript_path is None:
        return RepositoryPickerResult(
            ok=False,
            reason_code="repository_picker_unavailable",
            error_message="The native macOS folder picker is unavailable.",
        )
    if not _PICKER_LOCK.acquire(blocking=False):
        return RepositoryPickerResult(
            ok=False,
            reason_code="repository_picker_in_progress",
            error_message="A repository folder picker is already open.",
        )

    try:
        try:
            result = subprocess.run(
                [osascript_path, "-e", _PICKER_SCRIPT],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return RepositoryPickerResult(
                ok=False,
                reason_code="repository_picker_failed",
                error_message="The native macOS folder picker could not be opened.",
            )

        output = result.stdout.strip()
        if result.returncode != 0:
            return RepositoryPickerResult(
                ok=False,
                reason_code="repository_picker_failed",
                error_message="The native macOS folder picker did not complete successfully.",
            )
        if not output:
            return RepositoryPickerResult(
                ok=False,
                reason_code="repository_picker_failed",
                error_message="The native macOS folder picker returned no folder.",
            )
        if output == _NO_SELECTION_SENTINEL:
            return RepositoryPickerResult(
                ok=True,
                reason_code="repository_picker_closed",
            )

        selected_path = Path(output).expanduser().resolve(strict=False)
        if not selected_path.exists() or not selected_path.is_dir():
            return RepositoryPickerResult(
                ok=False,
                reason_code="repository_picker_invalid_selection",
                error_message="The selected repository folder is not an available directory.",
            )
        return RepositoryPickerResult(
            ok=True,
            selected=True,
            repository_path=str(selected_path),
            reason_code="repository_picker_selected",
        )
    finally:
        _PICKER_LOCK.release()
