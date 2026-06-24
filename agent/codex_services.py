from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import shutil
import time
from datetime import UTC, datetime
from typing import Any, Callable

from agent import ledger as ledger_module
from agent.codex_terminal import run_codex_exec


@dataclass(frozen=True)
class CodexDirectExecutionResult:
    ok: bool
    run_id: str
    reason_code: str | None
    error_message: str | None
    repo_path: str
    prompt: str
    sandbox: str
    command: list[str] | None
    started_event_id: int | None
    finished_event_id: int | None
    exit_code: int | None
    timed_out: bool
    duration_seconds: float | None
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_length: int
    stderr_length: int
    raw_process_result: dict[str, Any] | None
    metadata: dict[str, Any] = field(default_factory=dict)
    persisted: bool = False


def _default_hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _default_validation_result_builder(
    prompt: str,
    repo_path: str,
    sandbox: str,
    validation_error: str,
) -> dict[str, Any]:
    now = _utc_now()
    codex_path = shutil.which("codex")
    return {
        "mode": "exec",
        "found": codex_path is not None,
        "codex_path": codex_path,
        "prompt": prompt,
        "repo_path": repo_path,
        "sandbox": sandbox,
        "command": ["codex", "exec", "-C", repo_path, "-s", sandbox, prompt],
        "exit_code": 2,
        "stdout": "",
        "stderr": f"{validation_error}\n",
        "timed_out": False,
        "started_at": now,
        "finished_at": now,
        "validation_error": validation_error,
    }


def _event_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _reason_code(result: dict[str, Any]) -> str:
    if result.get("validation_error"):
        return "codex_validation_error"
    if result.get("found") is False:
        return "codex_not_found"
    if bool(result.get("timed_out")):
        return "codex_timed_out"
    exit_code = result.get("exit_code")
    if exit_code is None:
        return "codex_exit_missing"
    if isinstance(exit_code, int) and exit_code != 0:
        return "codex_nonzero_exit"
    return "codex_exec_completed"


def _error_message(result: dict[str, Any], reason_code: str) -> str | None:
    if reason_code == "codex_exec_completed":
        return None
    validation_error = result.get("validation_error")
    if validation_error:
        return str(validation_error)
    stderr = str(result.get("stderr") or "").strip()
    return stderr or None


def execute_codex_direct_service(
    run_id: str,
    prompt: str,
    repo_path: str,
    sandbox: str,
    timeout_seconds: int,
    prompt_contract: dict[str, Any],
    *,
    confirm_full_access: bool = False,
    preflight_validation_error: str | None = None,
    ledger: Any | None = None,
    codex_runner: Callable[..., dict[str, Any]] = run_codex_exec,
    validation_result_builder: Callable[[str, str, str, str], dict[str, Any]] = _default_validation_result_builder,
    monotonic_clock: Callable[[], float] = time.monotonic,
    hash_function: Callable[[str], str] = _default_hash_text,
) -> CodexDirectExecutionResult:
    del confirm_full_access
    event_ledger = ledger if ledger is not None else ledger_module
    started_metadata = {
        "prompt": prompt,
        "repo_path": repo_path,
        "timeout": timeout_seconds,
        "sandbox": sandbox,
        "prompt_contract": prompt_contract,
    }
    started_event_id = _event_id(
        event_ledger.add_event(
            run_id,
            "codex_exec_started",
            "Running Codex exec.",
            started_metadata,
        )
    )

    start_time = monotonic_clock()
    if preflight_validation_error is None:
        result = codex_runner(
            prompt,
            repo_path=repo_path,
            timeout_seconds=timeout_seconds,
            sandbox=sandbox,
        )
    else:
        result = validation_result_builder(
            prompt,
            repo_path,
            sandbox,
            preflight_validation_error,
        )
    end_time = monotonic_clock()
    duration_seconds = end_time - start_time

    validation_message = (
        f" validation_error={result['validation_error']}"
        if result["validation_error"]
        else ""
    )
    finished_message = (
        f"found={result['found']} exit_code={result['exit_code']} "
        f"timed_out={result['timed_out']} repo_path={result['repo_path']} "
        f"sandbox={result['sandbox']}{validation_message}"
    )
    finished_event_id = _event_id(
        event_ledger.add_event(
            run_id,
            "codex_exec_finished",
            finished_message,
            result,
        )
    )

    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    reason = _reason_code(result)
    return CodexDirectExecutionResult(
        ok=reason == "codex_exec_completed",
        run_id=run_id,
        reason_code=reason,
        error_message=_error_message(result, reason),
        repo_path=str(result.get("repo_path") or repo_path),
        prompt=str(result.get("prompt") or prompt),
        sandbox=str(result.get("sandbox") or sandbox),
        command=result.get("command") if isinstance(result.get("command"), list) else None,
        started_event_id=started_event_id,
        finished_event_id=finished_event_id,
        exit_code=result.get("exit_code") if isinstance(result.get("exit_code"), int) else None,
        timed_out=bool(result.get("timed_out")),
        duration_seconds=duration_seconds,
        stdout=stdout,
        stderr=stderr,
        stdout_sha256=hash_function(stdout),
        stderr_sha256=hash_function(stderr),
        stdout_length=len(stdout),
        stderr_length=len(stderr),
        raw_process_result=result,
        metadata={
            "started_event_metadata": started_metadata,
            "finished_event_message": finished_message,
            "reason_code": reason,
        },
        persisted=True,
    )
