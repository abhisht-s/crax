from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from agent.ledger import ALLOWED_CODEX_MODEL_SELECTIONS, CODEX_DEFAULT_SELECTION

ALLOWED_CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _safe_run_id(value: str | None) -> str:
    text = str(value or "unscoped-run").strip() or "unscoped-run"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)
    return safe[:80] or "unscoped-run"


def _default_final_message_path(run_id: str | None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"{timestamp}-{uuid.uuid4().hex}.md"
    return (
        Path(tempfile.gettempdir())
        / "agent-gpt-codex-loop"
        / "codex-final-messages"
        / _safe_run_id(run_id)
        / filename
    )


def _final_message_artifact_result(path: Path) -> dict:
    base = {
        "final_message_path": str(path),
        "final_message": "",
        "final_message_length": 0,
        "final_message_status": "missing",
        "final_message_error": None,
    }
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            **base,
            "final_message_error": "Codex final-message artifact was not written.",
        }
    except UnicodeDecodeError as error:
        return {
            **base,
            "final_message_status": "invalid_utf8",
            "final_message_error": f"Codex final-message artifact is not valid UTF-8: {error}",
        }
    except OSError as error:
        return {
            **base,
            "final_message_status": "unreadable",
            "final_message_error": f"Unable to read Codex final-message artifact: {error}",
        }

    clean_text = text.strip()
    if not clean_text:
        return {
            **base,
            "final_message_status": "empty",
            "final_message_error": "Codex final-message artifact was empty.",
        }

    return {
        **base,
        "final_message": clean_text,
        "final_message_length": len(clean_text),
        "final_message_status": "valid",
    }


def run_command(
    command: list[str],
    cwd: str | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    started_at = _utc_now()

    try:
        run_kwargs = {
            "cwd": cwd,
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if timeout_seconds is not None:
            run_kwargs["timeout"] = timeout_seconds
        completed = subprocess.run(
            command,
            **run_kwargs,
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
    timeout_seconds: float | None = None,
    cwd: str | Path | None = None,
    sandbox: str = "read-only",
    model: str | None = None,
    run_id: str | None = None,
    final_message_path: str | Path | None = None,
) -> dict:
    codex_path = shutil.which("codex")
    if repo_path is None:
        repo_path = cwd if cwd is not None else Path.cwd()

    resolved_repo_path = Path(repo_path).expanduser().resolve(strict=False)
    resolved_repo_path_text = str(resolved_repo_path)
    resolved_final_message_path = (
        Path(final_message_path).expanduser().resolve(strict=False)
        if final_message_path is not None
        else _default_final_message_path(run_id)
    )
    model_text = CODEX_DEFAULT_SELECTION if model is None else str(model).strip()
    command = [
        "codex",
        "exec",
        "-C",
        resolved_repo_path_text,
        "-s",
        sandbox,
    ]
    if model_text != CODEX_DEFAULT_SELECTION:
        command.extend(["-m", model_text])
    command.extend(
        [
            "--output-last-message",
            str(resolved_final_message_path),
            prompt,
        ]
    )

    validation_error = None
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        validation_error = (
            "Invalid Codex sandbox. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_SANDBOXES)}."
        )
    elif model_text not in ALLOWED_CODEX_MODEL_SELECTIONS:
        validation_error = (
            "Invalid Codex model. Allowed values: "
            f"{', '.join(ALLOWED_CODEX_MODEL_SELECTIONS)}."
        )
    elif not resolved_repo_path.exists():
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
            "sandbox": sandbox,
            "command": command,
            "exit_code": 2,
            "stdout": "",
            "stderr": f"{validation_error}\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": validation_error,
            "final_message_path": str(resolved_final_message_path),
            "final_message": "",
            "final_message_length": 0,
            "final_message_status": "not_requested",
            "final_message_error": validation_error,
        }

    if codex_path is None:
        now = _utc_now()
        return {
            "mode": "exec",
            "found": False,
            "codex_path": None,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "sandbox": sandbox,
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": "Codex CLI not found on PATH.\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": None,
            "final_message_path": str(resolved_final_message_path),
            "final_message": "",
            "final_message_length": 0,
            "final_message_status": "not_requested",
            "final_message_error": "Codex CLI not found on PATH.",
        }

    try:
        resolved_final_message_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        now = _utc_now()
        return {
            "mode": "exec",
            "found": True,
            "codex_path": codex_path,
            "prompt": prompt,
            "repo_path": resolved_repo_path_text,
            "sandbox": sandbox,
            "command": command,
            "exit_code": 2,
            "stdout": "",
            "stderr": f"Unable to prepare Codex final-message artifact path: {error}\n",
            "timed_out": False,
            "started_at": now,
            "finished_at": now,
            "validation_error": None,
            "final_message_path": str(resolved_final_message_path),
            "final_message": "",
            "final_message_length": 0,
            "final_message_status": "path_prepare_failed",
            "final_message_error": str(error),
        }

    result = run_command(command, cwd=resolved_repo_path_text, timeout_seconds=None)
    final_message = _final_message_artifact_result(resolved_final_message_path)
    return {
        "mode": "exec",
        "found": True,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": resolved_repo_path_text,
        "sandbox": sandbox,
        "validation_error": None,
        **result,
        **final_message,
    }


def check_codex_environment(timeout_seconds: float | None = None) -> dict:
    del timeout_seconds
    codex_path = shutil.which("codex")
    result = {
        "codex_path": codex_path,
        "found": codex_path is not None,
        "help": None,
        "doctor": None,
        "timeout_seconds": None,
    }

    if codex_path is None:
        return result

    result["help"] = run_command(
        [codex_path, "--help"],
        timeout_seconds=None,
    )
    result["doctor"] = run_command(
        [codex_path, "doctor"],
        timeout_seconds=None,
    )
    return result
