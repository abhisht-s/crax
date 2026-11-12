"""Deterministic provider resolution. Recognition does not imply availability."""
from __future__ import annotations

from typing import Any, Callable

from agent import ledger as default_ledger
from agent.codex_invocation import event_metadata
from agent.coding_agent_provider import CodingAgentProvider
from agent.codex_provider import CodexProvider
from agent.run_services import ExecutionProfileLookupStatus, get_run_execution_profile


class ProviderResolutionError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def resolve_provider(identity: str, *, codex_runner: Callable | None = None) -> CodingAgentProvider:
    if identity not in default_ledger.ALLOWED_CODING_AGENTS:
        raise ProviderResolutionError("coding_agent_unknown", f"Unknown coding agent: {identity!r}.")
    if identity != "codex":
        raise ProviderResolutionError("coding_agent_unavailable", f"Coding agent {identity!r} is recognized but its execution adapter is unavailable.")
    return CodexProvider(runner=codex_runner)


def validate_invocation_provider(identity: str, events: list[dict], invocation: Any = None) -> None:
    """Absent tags are historical Codex evidence, never the selected default."""
    evidence = [event_metadata(e) for e in events if e.get("event_type") in {
        "codex_exec_started", "codex_exec_finished",
    }]
    if invocation is not None:
        if invocation.started_event is not None:
            evidence.append(event_metadata(invocation.started_event))
        if invocation.intent is not None:
            evidence.append(invocation.intent)
    for metadata in evidence:
        recorded = metadata.get("coding_agent", "codex")
        if recorded != identity:
            raise ProviderResolutionError("coding_agent_invocation_mismatch", "Invocation provider evidence disagrees with the run execution profile.")


def provider_identity_for_events(run_id: str, events: list[dict]) -> str:
    class EventReader:
        def list_events(self, _run_id: str) -> list[dict]:
            return events

    lookup = get_run_execution_profile(run_id, ledger=EventReader())
    if lookup.status == ExecutionProfileLookupStatus.INVALID:
        raise ProviderResolutionError(lookup.reason_code or "execution_profile_invalid", lookup.error_message or "Invalid execution profile.")
    if lookup.profile is None and any(e.get("event_type") == "local_controller_run_started" for e in events):
        raise ProviderResolutionError("execution_profile_missing", "Controller-created run is missing a durable execution profile.")
    identity = lookup.profile.coding_agent if lookup.profile is not None else "codex"
    validate_invocation_provider(identity, events)
    return identity


def provider_for_events(run_id: str, events: list[dict]) -> CodingAgentProvider:
    return resolve_provider(provider_identity_for_events(run_id, events))


def coding_agent_session_details(run_id: str, events: list[dict]) -> dict[str, Any]:
    """Keep the historical dashboard shape while failing closed on bad ownership."""
    try:
        return provider_for_events(run_id, events).session_details(run_id, events)
    except ProviderResolutionError:
        return {"session_id": None, "invocation_id": None}


def cancel_coding_agent_run(run_id: str, *, ledger: Any = default_ledger, **kwargs: Any) -> dict[str, object]:
    # Process-group cancellation is CRAX-owned and must work even if SQLite is
    # unavailable. It uses verified run/invocation process identity, not login,
    # native-session strings, or a provider's "current session" facility.
    try:
        provider = provider_for_events(run_id, ledger.list_events(run_id))
    except Exception:
        from agent.codex_terminal import terminate_coding_agent_processes
        return terminate_coding_agent_processes(run_id, **kwargs)
    return provider.cancel(run_id, **kwargs)
