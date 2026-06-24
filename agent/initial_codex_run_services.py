from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agent import ledger as default_ledger
from agent.codex_services import (
    CodexDirectExecutionResult,
    execute_codex_direct_service,
)
from agent.git_snapshot import (
    capture_git_snapshot,
    capture_invocation_git_state,
)
from agent.governance_services import (
    PostCodexGovernanceResult,
    apply_post_codex_governance_service,
)
from agent.prompt_contract import parse_prompt_contract


@dataclass(frozen=True)
class InitialDirectCodexRunResult:
    ok: bool
    run_id: str
    reason_code: str | None
    error_message: str | None
    prompt: str
    repo_path: str
    sandbox: str
    prompt_contract: dict[str, Any]
    git_before: dict[str, Any] | None
    invocation_state_before: dict[str, Any] | None
    raw_execution_result: CodexDirectExecutionResult | None
    governance_result: PostCodexGovernanceResult | None
    run_status: str | None
    supervision_decision: dict[str, Any] | None
    validation_error: str | None
    events_written: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    persisted: bool = False


def _event_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _working_tree_dirty(snapshot: dict[str, Any]) -> bool:
    return bool(str(snapshot.get("status_short") or "").strip())


def _short_hash(value: str | None) -> str | None:
    if not value:
        return None
    return value[:12]


def _snapshot_message(snapshot: dict[str, Any]) -> str:
    branch = snapshot["branch"] or "None"
    head = _short_hash(snapshot["head"]) or "None"
    dirty = str(_working_tree_dirty(snapshot)).lower()
    return (
        f"repo_path={snapshot['repo_path']} branch={branch} "
        f"head={head} dirty={dirty}"
    )


def _record_pre_state_event(
    event_ledger: Any,
    events_written: list[dict[str, Any]],
    run_id: str,
    event_type: str,
    message: str,
    metadata: dict[str, Any],
) -> int | None:
    event_id = _event_id(event_ledger.add_event(run_id, event_type, message, metadata))
    events_written.append(
        {
            "event_type": event_type,
            "event_id": event_id,
            "message": message,
            "metadata": metadata,
        }
    )
    return event_id


def _preflight_validation_error(
    prompt_contract: dict[str, Any],
    sandbox: str,
    *,
    confirm_full_access: bool,
) -> str | None:
    path_safety = prompt_contract.get("path_safety")
    if isinstance(path_safety, dict) and not path_safety.get("valid"):
        invalid_paths = ", ".join(str(path) for path in path_safety.get("invalid_paths", []))
        return f"Prompt contract contains invalid path references: {invalid_paths}"
    if sandbox == "danger-full-access" and not confirm_full_access:
        return "Codex sandbox danger-full-access requires --confirm-full-access."
    return None


def _raw_event_summaries(result: CodexDirectExecutionResult | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    return [
        {
            "event_type": "codex_exec_started",
            "event_id": result.started_event_id,
        },
        {
            "event_type": "codex_exec_finished",
            "event_id": result.finished_event_id,
        },
    ]


def execute_initial_direct_codex_run_service(
    run_id: str,
    run: dict[str, Any],
    prompt: str,
    repo_path: str,
    sandbox: str,
    timeout_seconds: int,
    *,
    confirm_full_access: bool = False,
    expected_scope: dict[str, Any] | None = None,
    workspace_write_pre_run_result: dict[str, Any] | None = None,
    ledger: Any = default_ledger,
    prompt_contract_parser: Callable[..., Any] = parse_prompt_contract,
    git_snapshot_function: Callable[[str], dict[str, Any]] = capture_git_snapshot,
    invocation_state_function: Callable[[str], dict[str, Any]] = capture_invocation_git_state,
    raw_execution_service: Callable[..., CodexDirectExecutionResult] = execute_codex_direct_service,
    governance_service: Callable[..., PostCodexGovernanceResult] = apply_post_codex_governance_service,
) -> InitialDirectCodexRunResult:
    prompt_contract_object = prompt_contract_parser(prompt, sandbox)
    prompt_contract = (
        prompt_contract_object.to_dict()
        if hasattr(prompt_contract_object, "to_dict")
        else dict(prompt_contract_object)
    )
    git_before = git_snapshot_function(repo_path)
    invocation_state_before = invocation_state_function(repo_path)

    events_written: list[dict[str, Any]] = []
    _record_pre_state_event(
        ledger,
        events_written,
        run_id,
        "git_snapshot_before_codex",
        _snapshot_message(git_before),
        git_before,
    )
    _record_pre_state_event(
        ledger,
        events_written,
        run_id,
        "prompt_contract_parsed",
        f"Parsed prompt contract confidence={prompt_contract['confidence']}.",
        prompt_contract,
    )
    _record_pre_state_event(
        ledger,
        events_written,
        run_id,
        "invocation_git_state_before",
        "Captured pre-Codex invocation git state.",
        invocation_state_before,
    )

    validation_error = _preflight_validation_error(
        prompt_contract,
        sandbox,
        confirm_full_access=confirm_full_access,
    )
    raw_execution = raw_execution_service(
        run_id,
        prompt,
        repo_path,
        sandbox,
        timeout_seconds,
        prompt_contract,
        confirm_full_access=confirm_full_access,
        preflight_validation_error=validation_error,
        ledger=ledger,
    )
    raw_result = raw_execution.raw_process_result or {}
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
        expected_scope=expected_scope or {},
        workspace_write_pre_run_result=workspace_write_pre_run_result,
        ledger=ledger,
        git_snapshot_function=git_snapshot_function,
        invocation_state_function=invocation_state_function,
    )

    transition = governance_result.metadata.get("transition", {})
    events_written.extend(_raw_event_summaries(raw_execution))
    events_written.extend(governance_result.events_written)
    return InitialDirectCodexRunResult(
        ok=bool(governance_result.ok and raw_execution.persisted and governance_result.persisted),
        run_id=run_id,
        reason_code=governance_result.reason_code or raw_execution.reason_code,
        error_message=governance_result.error_message or raw_execution.error_message,
        prompt=prompt,
        repo_path=repo_path,
        sandbox=sandbox,
        prompt_contract=prompt_contract,
        git_before=git_before,
        invocation_state_before=invocation_state_before,
        raw_execution_result=raw_execution,
        governance_result=governance_result,
        run_status=governance_result.next_status,
        supervision_decision=governance_result.supervision_decision,
        validation_error=raw_result.get("validation_error"),
        events_written=events_written,
        metadata={
            "transition": transition,
            "expected_scope": expected_scope or {},
            "workspace_write_pre_run_result": workspace_write_pre_run_result,
        },
        persisted=True,
    )
