from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import shutil
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from agent import ledger as ledger_module
from agent.codex_invocation import (
    STATUS_COMPLETE,
    STATUS_LIVE,
    STATUS_UNCERTAIN,
    artifact_paths_for,
    event_metadata,
    latest_open_invocation,
    write_progress_persistence_failure,
)
from agent.codex_terminal import observe_existing_codex_invocation, run_codex_exec
from agent.run_services import (
    CODEX_DEFAULT_SELECTION,
    ExecutionProfileLookupStatus,
    RunExecutionProfile,
    get_run_execution_profile,
)


LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE = "local_controller_run_started"
CODEX_INVOCATION_UNCERTAIN_EVENT_TYPE = "codex_invocation_uncertain"
CODEX_PROGRESS_WRITE_FAILED_EVENT_TYPE = "codex_progress_write_failed"


class _ProgressPersistenceState:
    def __init__(self) -> None:
        self.failed = False
        self.error: str | None = None
        self.flagged = False


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
    final_message: str = ""
    final_message_path: str | None = None
    final_message_status: str | None = None
    final_message_error: str | None = None
    final_message_length: int = 0


@dataclass(frozen=True)
class _ResolvedExecutionProfile:
    ok: bool
    profile: RunExecutionProfile | None = None
    controller_run: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    event_ids: tuple[int, ...] = ()


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
        "final_message_path": None,
        "final_message": "",
        "final_message_length": 0,
        "final_message_status": "not_requested",
        "final_message_error": validation_error,
    }


def _failure_result(
    *,
    run_id: str,
    prompt: str,
    repo_path: str,
    sandbox: str,
    reason_code: str,
    error_message: str,
    command: list[str] | None = None,
) -> CodexDirectExecutionResult:
    return CodexDirectExecutionResult(
        ok=False,
        run_id=run_id,
        reason_code=reason_code,
        error_message=error_message,
        repo_path=repo_path,
        prompt=prompt,
        sandbox=sandbox,
        command=command,
        started_event_id=None,
        finished_event_id=None,
        exit_code=None,
        timed_out=False,
        duration_seconds=None,
        stdout="",
        stderr="",
        stdout_sha256=_default_hash_text(""),
        stderr_sha256=_default_hash_text(""),
        stdout_length=0,
        stderr_length=0,
        raw_process_result=None,
        metadata={
            "reason_code": reason_code,
            "error_message": error_message,
            "codex_invoked": False,
        },
        persisted=False,
    )


def _resolve_execution_profile(
    *,
    run_id: str,
    requested_sandbox: str,
    ledger: Any,
) -> _ResolvedExecutionProfile:
    try:
        events = ledger.list_events(run_id)
    except AttributeError:
        return _ResolvedExecutionProfile(ok=True)

    controller_run = any(
        event.get("event_type") == LOCAL_CONTROLLER_RUN_STARTED_EVENT_TYPE
        for event in events
    )
    lookup = get_run_execution_profile(run_id, ledger=ledger)

    if lookup.status == ExecutionProfileLookupStatus.INVALID:
        return _ResolvedExecutionProfile(
            ok=False,
            controller_run=controller_run,
            reason_code=lookup.reason_code or "execution_profile_invalid",
            error_message=lookup.error_message or "Run execution profile is invalid.",
            event_ids=tuple(lookup.event_ids),
        )

    if lookup.status == ExecutionProfileLookupStatus.MISSING:
        if controller_run:
            return _ResolvedExecutionProfile(
                ok=False,
                controller_run=True,
                reason_code="execution_profile_missing",
                error_message="Controller-created run is missing a durable execution profile.",
            )
        return _ResolvedExecutionProfile(ok=True, controller_run=False)

    profile = lookup.profile
    if profile is None:
        return _ResolvedExecutionProfile(
            ok=False,
            controller_run=controller_run,
            reason_code="execution_profile_missing",
            error_message="Run execution profile could not be reconstructed.",
            event_ids=tuple(lookup.event_ids),
        )

    if profile.sandbox != requested_sandbox:
        return _ResolvedExecutionProfile(
            ok=False,
            profile=profile,
            controller_run=controller_run,
            reason_code="execution_profile_sandbox_mismatch",
            error_message=(
                "Run execution profile sandbox does not match requested Codex sandbox."
            ),
            event_ids=tuple(lookup.event_ids),
        )

    return _ResolvedExecutionProfile(
        ok=True,
        profile=profile,
        controller_run=controller_run,
        event_ids=tuple(lookup.event_ids),
    )


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
    if bool(result.get("progress_persistence_failed")):
        return "codex_progress_persistence_failed"
    exit_code = result.get("exit_code")
    if exit_code is None:
        return "codex_exit_missing"
    if isinstance(exit_code, int) and exit_code != 0:
        return "codex_nonzero_exit"
    final_message_status = result.get("final_message_status")
    if final_message_status and final_message_status != "valid":
        return f"codex_final_message_{final_message_status}"
    if str(result.get("termination_reason") or "") == "uncertain":
        return "codex_invocation_uncertain"
    if result.get("invocation_complete") is False:
        return "codex_invocation_uncertain"
    return "codex_exec_completed"


def _new_codex_invocation_id() -> str:
    return f"codex-invocation-{uuid.uuid4().hex}"


def _supports_progress_events(event_ledger: Any) -> bool:
    return callable(getattr(event_ledger, "add_codex_progress_event", None))


def _progress_callback(
    event_ledger: Any,
    *,
    run_id: str,
    codex_invocation_id: str,
    state: _ProgressPersistenceState,
    artifact_dir: str | None = None,
) -> Callable[[dict[str, object]], None]:
    def record(progress_event: dict[str, object]) -> None:
        try:
            event_ledger.add_codex_progress_event(
                run_id,
                codex_invocation_id,
                progress_event,
            )
        except Exception as exc:
            state.failed = True
            state.error = str(exc)
            if artifact_dir:
                write_progress_persistence_failure(
                    artifact_paths_for(run_id, codex_invocation_id, artifact_dir=artifact_dir),
                    str(exc),
                )
            if state.flagged:
                return
            state.flagged = True
            try:
                event_ledger.add_event(
                    run_id,
                    CODEX_PROGRESS_WRITE_FAILED_EVENT_TYPE,
                    "Codex progress persistence failed.",
                    {
                        "codex_invocation_id": codex_invocation_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            except Exception:
                return

    return record


def _error_message(result: dict[str, Any], reason_code: str) -> str | None:
    if reason_code == "codex_exec_completed":
        return None
    if reason_code == "codex_invocation_uncertain":
        return str(result.get("error_message") or "Codex invocation is uncertain and will not be replayed.")
    if reason_code == "codex_progress_persistence_failed":
        return "Codex progress events could not be persisted."
    validation_error = result.get("validation_error")
    if validation_error:
        return str(validation_error)
    final_message_error = result.get("final_message_error")
    if final_message_error:
        return str(final_message_error)
    stderr = str(result.get("stderr") or "").strip()
    return stderr or None


def _list_events(event_ledger: Any, run_id: str) -> list[dict[str, Any]]:
    lister = getattr(event_ledger, "list_events", None)
    if not callable(lister):
        return []
    try:
        events = lister(run_id)
    except Exception:
        return []
    return list(events or [])


def _unpaired_extraction_identity(event_ledger: Any, run_id: str) -> dict[str, Any]:
    events = _list_events(event_ledger, run_id)
    started = None
    for event in reversed(events):
        if event.get("event_type") == "extracted_codex_prompt_run_started":
            started = event
            break
    if started is None:
        return {}
    started_id = _event_id(started)
    metadata = started.get("metadata") if isinstance(started.get("metadata"), dict) else {}
    if not metadata:
        metadata = event_metadata(started)
    extraction_event_id = metadata.get("extraction_event_id")
    prompt_sha256 = metadata.get("prompt_sha256")
    for event in events:
        if event.get("event_type") != "extracted_codex_prompt_run_finished":
            continue
        finished_id = _event_id(event)
        if started_id is None or finished_id is None or finished_id <= started_id:
            continue
        finished_metadata = event_metadata(event)
        if (
            finished_metadata.get("extraction_event_id") == extraction_event_id
            and finished_metadata.get("prompt_sha256") == prompt_sha256
        ):
            return {}
    identity: dict[str, Any] = {}
    if isinstance(extraction_event_id, int):
        identity["extraction_event_id"] = extraction_event_id
    elif str(extraction_event_id or "").isdigit():
        identity["extraction_event_id"] = int(extraction_event_id)
    if isinstance(prompt_sha256, str) and prompt_sha256:
        identity["extraction_prompt_sha256"] = prompt_sha256
    return identity


def _finished_event_for_invocation(
    events: list[dict[str, Any]],
    invocation_id: str | None,
) -> dict[str, Any] | None:
    if not invocation_id:
        return None
    for event in events:
        if event.get("event_type") != "codex_exec_finished":
            continue
        metadata = event_metadata(event)
        if metadata.get("codex_invocation_id") == invocation_id:
            return event
    return None


def _record_uncertain_invocation(
    event_ledger: Any,
    run_id: str,
    invocation_id: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    events = _list_events(event_ledger, run_id)
    for event in events:
        if event.get("event_type") != CODEX_INVOCATION_UNCERTAIN_EVENT_TYPE:
            continue
        metadata = event_metadata(event)
        if metadata.get("codex_invocation_id") == invocation_id:
            return
    event_ledger.add_event(
        run_id,
        CODEX_INVOCATION_UNCERTAIN_EVENT_TYPE,
        "Codex invocation is uncertain and will not be replayed.",
        {
            "codex_invocation_id": invocation_id,
            "reason": reason,
            **(extra or {}),
        },
    )


def reconcile_codex_invocation(
    run_id: str,
    *,
    ledger: Any | None = None,
) -> CodexDirectExecutionResult | None:
    event_ledger = ledger if ledger is not None else ledger_module
    events = _list_events(event_ledger, run_id)
    open_item = latest_open_invocation(run_id, events=events)
    if open_item is None:
        return None
    started_metadata: dict[str, Any] = {}
    if isinstance(open_item.started_event, dict):
        started_metadata = event_metadata(open_item.started_event)
    if not started_metadata and isinstance(open_item.intent, dict):
        started_metadata = open_item.intent
    return execute_codex_direct_service(
        run_id,
        str(started_metadata.get("prompt") or ""),
        str(started_metadata.get("repo_path") or ""),
        str(started_metadata.get("sandbox") or "read-only"),
        None,
        started_metadata.get("prompt_contract")
        if isinstance(started_metadata.get("prompt_contract"), dict)
        else {},
        ledger=event_ledger,
    )


def execute_codex_direct_service(
    run_id: str,
    prompt: str,
    repo_path: str,
    sandbox: str,
    timeout_seconds: float | None,
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
    resolved_profile = _resolve_execution_profile(
        run_id=run_id,
        requested_sandbox=sandbox,
        ledger=event_ledger,
    )
    if not resolved_profile.ok:
        return _failure_result(
            run_id=run_id,
            prompt=prompt,
            repo_path=repo_path,
            sandbox=sandbox,
            reason_code=resolved_profile.reason_code or "execution_profile_invalid",
            error_message=(
                resolved_profile.error_message
                or "Run execution profile is invalid."
            ),
        )

    profile = resolved_profile.profile
    model = profile.model if profile is not None else CODEX_DEFAULT_SELECTION
    progress_enabled = _supports_progress_events(event_ledger)
    progress_state = _ProgressPersistenceState()
    events = _list_events(event_ledger, run_id)
    open_item = latest_open_invocation(run_id, events=events)
    started_metadata = {
        "prompt": prompt,
        "repo_path": repo_path,
        "timeout": None,
        "sandbox": sandbox,
        "prompt_contract": prompt_contract,
    }
    if model != CODEX_DEFAULT_SELECTION:
        started_metadata["model"] = model

    if open_item is not None and open_item.status == STATUS_UNCERTAIN:
        _record_uncertain_invocation(
            event_ledger,
            run_id,
            open_item.invocation_id,
            open_item.reason,
        )
        return _failure_result(
            run_id=run_id,
            prompt=prompt,
            repo_path=repo_path,
            sandbox=sandbox,
            reason_code="codex_invocation_uncertain",
            error_message=(
                "An existing Codex invocation has start evidence without proven "
                "completion and will not be replayed."
            ),
        )

    start_time = monotonic_clock()
    started_event_id: int | None = None
    if open_item is not None and open_item.status in {STATUS_LIVE, STATUS_COMPLETE}:
        codex_invocation_id = open_item.invocation_id
        started_event_id = _event_id(open_item.started_event) if open_item.started_event else None
        started_metadata["codex_invocation_id"] = codex_invocation_id
        started_metadata["json_stream"] = True
        callback = None
        if progress_enabled:
            callback = _progress_callback(
                event_ledger,
                run_id=run_id,
                codex_invocation_id=codex_invocation_id,
                state=progress_state,
                artifact_dir=str(open_item.paths.artifact_dir),
            )
        result = observe_existing_codex_invocation(
            open_item,
            progress_callback=callback if open_item.status == STATUS_LIVE else None,
        )
        result["codex_invocation_id"] = codex_invocation_id
        result["json_stream"] = True
        if not result.get("invocation_complete", True):
            _record_uncertain_invocation(
                event_ledger,
                run_id,
                codex_invocation_id,
                "observation ended without durable exit evidence",
            )
            return _failure_result(
                run_id=run_id,
                prompt=prompt,
                repo_path=repo_path,
                sandbox=sandbox,
                reason_code="codex_invocation_uncertain",
                error_message=(
                    "An existing Codex invocation has start evidence without proven "
                    "completion and will not be replayed."
                ),
            )
    else:
        codex_invocation_id = _new_codex_invocation_id() if progress_enabled else None
        if codex_invocation_id is not None:
            started_metadata["codex_invocation_id"] = codex_invocation_id
            started_metadata["json_stream"] = True
            started_metadata["prompt_sha256"] = hash_function(prompt)
            started_metadata.update(
                artifact_paths_for(run_id, codex_invocation_id).as_dict()
            )
            extraction_identity = _unpaired_extraction_identity(event_ledger, run_id)
            started_metadata.update(extraction_identity)
        started_event_id = _event_id(
            event_ledger.add_event(
                run_id,
                "codex_exec_started",
                "Running Codex exec.",
                started_metadata,
            )
        )
        if preflight_validation_error is None:
            runner_kwargs: dict[str, Any] = {
                "repo_path": repo_path,
                "timeout_seconds": None,
                "sandbox": sandbox,
                "run_id": run_id,
            }
            if model != CODEX_DEFAULT_SELECTION:
                runner_kwargs["model"] = model
            if codex_invocation_id is not None:
                runner_kwargs["json_stream"] = True
                runner_kwargs["codex_invocation_id"] = codex_invocation_id
                runner_kwargs["progress_callback"] = _progress_callback(
                    event_ledger,
                    run_id=run_id,
                    codex_invocation_id=codex_invocation_id,
                    state=progress_state,
                    artifact_dir=str(started_metadata.get("artifact_dir") or ""),
                )
                if "extraction_event_id" in started_metadata:
                    runner_kwargs["extraction_event_id"] = started_metadata["extraction_event_id"]
                if "extraction_prompt_sha256" in started_metadata:
                    runner_kwargs["extraction_prompt_sha256"] = started_metadata[
                        "extraction_prompt_sha256"
                    ]
                if started_metadata.get("artifact_dir"):
                    runner_kwargs["artifact_dir"] = started_metadata["artifact_dir"]
            result = codex_runner(prompt, **runner_kwargs)
            if codex_invocation_id is not None:
                result["codex_invocation_id"] = codex_invocation_id
                result["json_stream"] = bool(result.get("json_stream", True))
        else:
            if codex_invocation_id is not None:
                _progress_callback(
                    event_ledger,
                    run_id=run_id,
                    codex_invocation_id=codex_invocation_id,
                    state=progress_state,
                )(
                    {
                        "source": "codex_service",
                        "kind": "error",
                        "status": "failed",
                        "title": "Codex preflight validation failed",
                        "summary": preflight_validation_error,
                        "metadata": {
                            "repo_path": repo_path,
                            "sandbox": sandbox,
                            "codex_invocation_id": codex_invocation_id,
                            "validation_error": preflight_validation_error,
                        },
                    }
                )
            result = validation_result_builder(
                prompt,
                repo_path,
                sandbox,
                preflight_validation_error,
            )
            if codex_invocation_id is not None:
                result["codex_invocation_id"] = codex_invocation_id
                result["json_stream"] = False

    if progress_state.failed:
        result["progress_persistence_failed"] = True
    end_time = monotonic_clock()
    duration_seconds = end_time - start_time

    validation_message = (
        f" validation_error={result['validation_error']}"
        if result.get("validation_error")
        else ""
    )
    finished_message = (
        f"found={result['found']} exit_code={result['exit_code']} "
        f"timed_out={result['timed_out']} repo_path={result['repo_path']} "
        f"sandbox={result['sandbox']}{validation_message}"
    )
    existing_finished = _finished_event_for_invocation(
        _list_events(event_ledger, run_id),
        str(result.get("codex_invocation_id") or codex_invocation_id or ""),
    )
    if existing_finished is not None:
        finished_event_id = _event_id(existing_finished)
        existing_metadata = event_metadata(existing_finished)
        if existing_metadata:
            result = existing_metadata
    else:
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
    final_message = str(result.get("final_message") or "")
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
        final_message=final_message,
        final_message_path=(
            str(result.get("final_message_path"))
            if result.get("final_message_path") is not None
            else None
        ),
        final_message_status=(
            str(result.get("final_message_status"))
            if result.get("final_message_status") is not None
            else None
        ),
        final_message_error=(
            str(result.get("final_message_error"))
            if result.get("final_message_error") is not None
            else None
        ),
        final_message_length=len(final_message),
    )
