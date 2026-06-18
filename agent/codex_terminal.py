from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def run_command(
    command: list[str],
    cwd: str | None = None,
    timeout_seconds: int = 30,
) -> dict:
    started_at = _utc_now()

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }
    except subprocess.TimeoutExpired as error:
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": None,
            "stdout": _decode_output(error.stdout),
            "stderr": _decode_output(error.stderr),
            "timed_out": True,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }
    except FileNotFoundError as error:
        return {
            "command": command,
            "cwd": cwd,
            "exit_code": 127,
            "stdout": "",
            "stderr": f"{error}\n",
            "timed_out": False,
            "started_at": started_at,
            "finished_at": _utc_now(),
        }


def check_codex_environment(timeout_seconds: int = 30) -> dict:
    codex_path = shutil.which("codex")
    result = {
        "codex_path": codex_path,
        "found": codex_path is not None,
        "help": None,
        "doctor": None,
        "timeout_seconds": timeout_seconds,
    }

    if codex_path is None:
        return result

    result["help"] = run_command(
        [codex_path, "--help"],
        timeout_seconds=timeout_seconds,
    )
    result["doctor"] = run_command(
        [codex_path, "doctor"],
        timeout_seconds=timeout_seconds,
    )
    return result
