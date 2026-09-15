from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.codex_quota_wait import (
    CODEX_FORCE_CONTINUE_PROMPT,
    CODEX_QUOTA_RESUME_FINISHED_EVENT_TYPE,
    CODEX_QUOTA_RESUME_PROMPT,
    CODEX_QUOTA_RESUME_STARTED_EVENT_TYPE,
    active_quota_wait,
    latest_codex_thread_id,
    quota_wait_fields,
)
from agent.codex_services import execute_codex_direct_service
from agent.git_snapshot import capture_git_snapshot, capture_invocation_git_state
from agent.governance_services import apply_post_codex_governance_service
from agent.prompt_contract import parse_prompt_contract


@dataclass(frozen=True)
class CodexQuotaResumeResult:
    ok: bool
    reason_code: str
    error_message: str | None = None
    run_status: str | None = None
    events_written: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_execution_result: Any = None
    governance_result: Any = None


def execute_codex_quota_resume_service(
    run_id: str,
    *,
    ledger: Any = default_ledger,
    now: datetime | None = None,
    allow_before_due: bool = False,
    raw_execution_service: Callable[..., Any] = execute_codex_direct_service,
    governance_service: Callable[..., Any] = apply_post_codex_governance_service,
    git_snapshot_function: Callable[[str], dict[str, Any]] = capture_git_snapshot,
    invocation_state_function: Callable[[str], dict[str, Any]] = capture_invocation_git_state,
    prompt_contract_parser: Callable[..., Any] = parse_prompt_contract,
) -> CodexQuotaResumeResult:
    events = ledger.list_events(run_id)
    target = _resume_target(events, now=now or datetime.now(UTC), allow_before_due=allow_before_due)
    if isinstance(target, CodexQuotaResumeResult):
        return target

    thread_id = target["thread_id"]
    repo_path = target["repository_path"]
    sandbox = target["sandbox"]
    resume_at = target["resume_at"]
    forced = target["forced"]
    prompt = target["prompt"]

    run = ledger.get_run(run_id)
    if not isinstance(run, dict):
        return CodexQuotaResumeResult(
            ok=False,
            reason_code="run_missing",
            error_message=f"Run not found: {run_id}",
        )

    prompt_contract_object = prompt_contract_parser(prompt, sandbox)
    prompt_contract = (
        prompt_contract_object.to_dict()
        if hasattr(prompt_contract_object, "to_dict")
        else dict(prompt_contract_object)
    )
    git_before = git_snapshot_function(repo_path)
    invocation_state_before = invocation_state_function(repo_path)

    started = ledger.add_event(
        run_id,
        CODEX_QUOTA_RESUME_STARTED_EVENT_TYPE,
        (
            "Resuming Codex session after operator force continue."
            if forced
            else "Resuming Codex session after usage-limit reset."
        ),
        {
            "thread_id": thread_id,
            "resume_at": resume_at,
            "repository_path": repo_path,
            "sandbox": sandbox,
            "prompt": prompt,
            "forced": bool(forced),
        },
    )

    raw_execution = raw_execution_service(
        run_id,
        prompt,
        repo_path,
        sandbox,
        None,
        prompt_contract,
        confirm_full_access=sandbox == "danger-full-access",
        ledger=ledger,
        resume_session_id=thread_id,
    )
    raw_result = getattr(raw_execution, "raw_process_result", None) or {}
    governance_result = governance_service(
        run_id,
        run,
        prompt,
        repo_path,
        sandbox,
        prompt_contract,
        raw_result,
        git_before,
        invocation_state_before,
        ledger=ledger,
    )

    finished = ledger.add_event(
        run_id,
        CODEX_QUOTA_RESUME_FINISHED_EVENT_TYPE,
        (
            "Codex quota resume finished "
            f"ok={bool(getattr(raw_execution, 'ok', False))} "
            f"reason={getattr(raw_execution, 'reason_code', None)}."
        ),
        {
            "thread_id": thread_id,
            "reason_code": getattr(raw_execution, "reason_code", None),
            "exit_code": getattr(raw_execution, "exit_code", None),
            "run_status": getattr(governance_result, "next_status", None),
        },
    )
    ok = bool(getattr(raw_execution, "ok", False))
    return CodexQuotaResumeResult(
        ok=ok,
        reason_code=str(
            getattr(raw_execution, "reason_code", None) or "codex_quota_resume_finished"
        ),
        error_message=getattr(raw_execution, "error_message", None),
        run_status=getattr(governance_result, "next_status", None),
        events_written=[started, finished],
        metadata={
            "thread_id": thread_id,
            "resume_at": resume_at,
        },
        raw_execution_result=raw_execution,
        governance_result=governance_result,
    )


def _resume_target(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    allow_before_due: bool,
) -> dict[str, Any] | CodexQuotaResumeResult:
    wait_event = active_quota_wait(events)
    fields = quota_wait_fields(wait_event)
    if fields is not None:
        resume_at = _parse_iso(str(fields["resume_at"]))
        if not allow_before_due and (resume_at is None or now < resume_at):
            return CodexQuotaResumeResult(
                ok=False,
                reason_code="quota_resume_not_due",
                error_message="Codex quota resume is not due yet.",
                metadata={"resume_at": fields["resume_at"]},
            )
        repo_path = fields.get("repository_path")
        if not repo_path:
            return CodexQuotaResumeResult(
                ok=False,
                reason_code="quota_resume_missing_repo",
                error_message="Quota wait is missing the repository path.",
            )
        return {
            "thread_id": fields["thread_id"],
            "repository_path": repo_path,
            "sandbox": fields.get("sandbox") or "read-only",
            "resume_at": fields["resume_at"],
            "forced": bool(allow_before_due),
            "prompt": CODEX_QUOTA_RESUME_PROMPT,
        }

    if not allow_before_due:
        return CodexQuotaResumeResult(
            ok=False,
            reason_code="quota_wait_not_active",
            error_message="No active Codex quota wait is available to resume.",
        )

    thread_id = latest_codex_thread_id(events)
    if not thread_id:
        return CodexQuotaResumeResult(
            ok=False,
            reason_code="no_codex_session",
            error_message="No Codex session is available to continue.",
        )
    repo_path, sandbox = _latest_resume_repository(events)
    if not repo_path:
        return CodexQuotaResumeResult(
            ok=False,
            reason_code="quota_resume_missing_repo",
            error_message="Codex resume is missing the repository path.",
        )
    return {
        "thread_id": thread_id,
        "repository_path": repo_path,
        "sandbox": sandbox or "read-only",
        "resume_at": None,
        "forced": True,
        "prompt": CODEX_FORCE_CONTINUE_PROMPT,
    }


def _latest_resume_repository(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    fallback_repo: str | None = None
    fallback_sandbox: str | None = None
    for event in reversed(events):
        metadata = _event_metadata(event)
        repo_path = str(
            metadata.get("repo_path") or metadata.get("repository_path") or ""
        ).strip()
        sandbox = str(metadata.get("sandbox") or "").strip() or "read-only"
        if not repo_path:
            continue
        event_type = str(event.get("event_type") or "")
        if event_type == "codex_exec_started":
            return repo_path, sandbox
        if fallback_repo is None and event_type == "local_controller_run_started":
            fallback_repo = repo_path
            fallback_sandbox = sandbox
    return fallback_repo, fallback_sandbox


def _event_metadata(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("metadata")
    if isinstance(value, dict):
        return value
    raw = event.get("metadata_json")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
