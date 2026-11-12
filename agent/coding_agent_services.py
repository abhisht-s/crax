"""CRAX durable coding execution coordinator.

Legacy codex_* ledger keys and artifact paths are a persisted compatibility
format, not a provider dispatch mechanism. Native semantics live in adapters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import time
import uuid
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
from agent.coding_agent_provider import CodingAgentRequest, ExecutionOutcome, NativeSessionIdentity
from agent.coding_agents import ProviderResolutionError, resolve_provider, validate_invocation_provider
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
class CodingAgentExecutionResult:
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
    coding_agent: str = "codex"
    outcome: ExecutionOutcome = field(default_factory=ExecutionOutcome)


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


def _failure_result(
    *,
    run_id: str,
    prompt: str,
    repo_path: str,
    sandbox: str,
    reason_code: str,
    error_message: str,
    command: list[str] | None = None,
    coding_agent: str = "unresolved",
) -> CodingAgentExecutionResult:
    uncertain = reason_code.endswith("uncertain")
    provider_outcome = "uncertain" if uncertain else "rejected"
    if reason_code == "coding_agent_unavailable":
        provider_outcome = "unavailable"
    return CodingAgentExecutionResult(
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
        coding_agent=coding_agent,
        outcome=ExecutionOutcome(
            process="uncertain" if uncertain else "not_started",
            provider=provider_outcome,
            uncertain=uncertain,
        ),
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


def reconcile_coding_agent_invocation(
    run_id: str,
    *,
    ledger: Any | None = None,
) -> CodingAgentExecutionResult | None:
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
    return execute_coding_agent_service(
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


def execute_coding_agent_service(
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
    _provider_resolver: Callable | None = None,
    validation_result_builder: Callable[[str, str, str, str], dict[str, Any]] | None = None,
    monotonic_clock: Callable[[], float] = time.monotonic,
    hash_function: Callable[[str], str] = _default_hash_text,
    native_session: NativeSessionIdentity | None = None,
) -> CodingAgentExecutionResult:
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
            coding_agent=resolved_profile.profile.coding_agent if resolved_profile.profile else "unresolved",
            reason_code=resolved_profile.reason_code or "execution_profile_invalid",
            error_message=(
                resolved_profile.error_message
                or "Run execution profile is invalid."
            ),
        )

    profile = resolved_profile.profile
    identity = profile.coding_agent if profile is not None else "codex"
    try:
        provider = (_provider_resolver or resolve_provider)(identity)
        if native_session is not None and native_session.coding_agent != identity:
            raise ProviderResolutionError("coding_agent_session_mismatch", "Native session belongs to another coding agent.")
    except ProviderResolutionError as exc:
        return _failure_result(run_id=run_id, prompt=prompt, repo_path=repo_path,
                               sandbox=sandbox, reason_code=exc.reason_code,
                               error_message=str(exc), coding_agent=identity)
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
    started_metadata["coding_agent"] = identity
    if native_session is not None:
        started_metadata["resume_session_id"] = native_session.session_id

    try:
        validate_invocation_provider(identity, events, open_item)
        if native_session is not None:
            session = provider.session_details(run_id, events)
            if session.get("session_id") != native_session.session_id:
                raise ProviderResolutionError("coding_agent_session_mismatch", "Continuation does not match this run's native session.")
    except ProviderResolutionError as exc:
        return _failure_result(run_id=run_id, prompt=prompt, repo_path=repo_path,
                               sandbox=sandbox, reason_code=exc.reason_code,
                               error_message=str(exc), coding_agent=identity)

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
            coding_agent=identity,
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
        result = provider.observe(
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
                coding_agent=identity,
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
            callback = None
            if codex_invocation_id is not None:
                callback = _progress_callback(
                    event_ledger, run_id=run_id, codex_invocation_id=codex_invocation_id,
                    state=progress_state, artifact_dir=str(started_metadata.get("artifact_dir") or ""),
                )
            result = provider.execute(CodingAgentRequest(
                run_id=run_id, prompt=prompt, repo_path=repo_path, sandbox=sandbox,
                model=model, invocation_id=codex_invocation_id,
                progress_callback=callback, native_session=native_session,
                artifact_dir=started_metadata.get("artifact_dir"),
                extraction_event_id=started_metadata.get("extraction_event_id"),
                extraction_prompt_sha256=started_metadata.get("extraction_prompt_sha256"),
            ))
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
            result = (validation_result_builder or provider.validation_result)(
                prompt,
                repo_path,
                sandbox,
                preflight_validation_error,
            )
            if codex_invocation_id is not None:
                result["codex_invocation_id"] = codex_invocation_id
                result["json_stream"] = False

    result["coding_agent"] = identity
    if progress_state.failed:
        result["progress_persistence_failed"] = True
    outcome = provider.classify(result)
    if outcome.native_session is None and native_session is not None:
        outcome = replace(outcome, native_session=native_session)
    result["execution_outcome"] = asdict(outcome)
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
    reason = provider.reason_code(result)
    # Older finish events predate normalized evidence; reconstruct without
    # changing their durable raw record.
    outcome = provider.classify(result)
    if outcome.native_session is None and native_session is not None:
        outcome = replace(outcome, native_session=native_session)
    return CodingAgentExecutionResult(
        ok=outcome.provider == "completed" and not result.get("progress_persistence_failed"),
        run_id=run_id,
        reason_code=reason,
        error_message=provider.error_message(result, reason),
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
        coding_agent=identity,
        outcome=outcome,
    )
