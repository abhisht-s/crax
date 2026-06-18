from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


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


def run_codex_exec(
    prompt: str,
    repo_path: str | Path | None = None,
    timeout_seconds: int = 300,
    cwd: str | Path | None = None,
) -> dict:
    codex_path = shutil.which("codex")
    if repo_path is None:
        repo_path = cwd if cwd is not None else Path.cwd()

    resolved_repo_path = Path(repo_path).expanduser().resolve(strict=False)
    resolved_repo_path_text = str(resolved_repo_path)
    command = ["codex", "exec", "-C", resolved_repo_path_text, prompt]

    validation_error = None
    if not resolved_repo_path.exists():
        validation_error = f"Repo path does not exist: {resolved_repo_path_text}"
    elif not resolved_repo_path.is_dir():
        validation_error = f"Repo path is not a directory: {resolved_repo_path_text}"

    if validation_error is not None:
        now = _utc_now()
        return {
            "mode": "exec",
            "found": codex_path is not None,
            "codex_path": codex_path,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "command": command,
            "exit_code": 2,
            "stdout": "",
            "stderr": f"{validation_error}\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": validation_error,
        }

    if codex_path is None:
        now = _utc_now()
        return {
            "mode": "exec",
            "found": False,
            "codex_path": None,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": "Codex CLI not found on PATH.\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": None,
        }

    result = run_command(command, cwd=resolved_repo_path_text, timeout_seconds=timeout_seconds)
    return {
        "mode": "exec",
        "found": True,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": resolved_repo_path_text,
        "validation_error": None,
        **result,
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
