"""Governed native continuation with provider-scoped session ownership.

Historical names remain for persisted dashboard/controller contracts.
"""
from __future__ import annotations

from typing import Any

from agent.codex_invocation import event_metadata
from agent.codex_services import execute_codex_direct_service, reconcile_codex_invocation
from agent.initial_codex_run_services import execute_initial_direct_codex_run_service

# Historical dashboard/public name; identity and native parsing resolve per run.
from agent.coding_agents import (
    ProviderResolutionError, coding_agent_session_details as codex_session_details,
    provider_identity_for_events, resolve_provider,
)
from agent.coding_agent_provider import NativeSessionIdentity


CONTINUE_PROMPT = (
    "Continue the task in this same session from where you stopped. "
    "Preserve the original scope, instructions, and restrictions. "
    "Check what has already completed before taking further actions."
)




def execute_codex_resume_service(
    *, run_id: str, run: dict, repository_path: str, sandbox: str,
    session_id: str, ledger: Any,
):
    from agent.coding_agent_services import _failure_result
    events = ledger.list_events(run_id)
    identity = "unresolved"
    try:
        identity = provider_identity_for_events(run_id, events)
        provider = resolve_provider(identity)
        recovered = provider.session_details(run_id, events)
        if recovered.get("session_id") != session_id:
            raise ProviderResolutionError("coding_agent_session_mismatch", "Continuation does not match this run's native session.")
    except ProviderResolutionError as exc:
        return _failure_result(run_id=run_id, prompt=CONTINUE_PROMPT,
                               repo_path=repository_path, sandbox=sandbox,
                               reason_code=exc.reason_code, error_message=str(exc),
                               coding_agent=identity)
    # A dead wrapper with a durable exit can lack its ledger finish event.
    # Reconcile it before starting a new invocation; never replay its prompt.
    reconciled = reconcile_codex_invocation(run_id, ledger=ledger)
    if reconciled is not None and not reconciled.persisted:
        return reconciled
    events = ledger.list_events(run_id)
    started = next((e for e in reversed(events) if e.get("event_type") == "codex_exec_started" and not event_metadata(e).get("resume_session_id")), None)
    original_prompt = str(event_metadata(started or {}).get("prompt") or run.get("user_instruction") or "")

    def resume_raw(*args, **kwargs):
        # Governance still evaluates the original explicit contract. The worker
        # receives a short follow-up in its existing session, not a new task.
        return execute_codex_direct_service(
            args[0], CONTINUE_PROMPT, *args[2:],
            native_session=NativeSessionIdentity(provider.identity, session_id), **kwargs,
        )

    result = execute_initial_direct_codex_run_service(
        run_id, run, original_prompt, repository_path, sandbox, None,
        confirm_full_access=sandbox == "danger-full-access",
        ledger=ledger, raw_execution_service=resume_raw,
    )
    # Preserve the extracted-prompt wrapper pairing expected by the planner.
    # Only close the latest outstanding wrapper with this continuation's result.
    latest_started = next((e for e in reversed(events) if e.get("event_type") == "extracted_codex_prompt_run_started"), None)
    if latest_started is not None:
        selection = event_metadata(latest_started)
        paired = any(
            e.get("event_type") == "extracted_codex_prompt_run_finished"
            and int(e.get("id") or 0) > int(latest_started.get("id") or 0)
            and event_metadata(e).get("extraction_event_id") == selection.get("extraction_event_id")
            and event_metadata(e).get("prompt_sha256") == selection.get("prompt_sha256")
            for e in events
        )
        raw = result.raw_execution_result.raw_process_result or {}
        if not paired and result.raw_execution_result.persisted:
            ledger.add_event(run_id, "extracted_codex_prompt_run_finished", "Finished extracted Codex prompt after explicit continuation.", {
                **selection, "exit_code": raw.get("exit_code"), "timed_out": raw.get("timed_out"),
                "status": result.run_status, "continued": True,
                "codex_invocation_id": raw.get("codex_invocation_id"),
            })
    return result


# Neutral name for new consumers; the legacy entry point above remains callable.
execute_coding_agent_resume_service = execute_codex_resume_service
