"""Compatibility entry points for historical callers and injected Codex runners.

Execution, recovery and provider ownership are implemented by the common service.
"""
from __future__ import annotations

from typing import Any, Callable

from agent.codex_terminal import run_codex_exec
from agent.coding_agent_provider import NativeSessionIdentity
from agent.coding_agents import resolve_provider
from agent.codex_provider import _default_validation_result_builder
from agent.coding_agent_services import (
    CodingAgentExecutionResult as CodexDirectExecutionResult,
    execute_coding_agent_service,
    reconcile_coding_agent_invocation,
)


def execute_codex_direct_service(*args: Any, codex_runner: Callable | None = None, **kwargs: Any) -> CodexDirectExecutionResult:
    resume_session_id = kwargs.pop("resume_session_id", None)
    if resume_session_id is not None:
        native = NativeSessionIdentity("codex", resume_session_id)
        if kwargs.get("native_session") not in (None, native):
            raise ValueError("Contradictory native continuation identities.")
        kwargs["native_session"] = native
    if codex_runner is not None:
        kwargs["_provider_resolver"] = lambda identity: resolve_provider(identity, codex_runner=codex_runner)
    return execute_coding_agent_service(*args, **kwargs)


def reconcile_codex_invocation(run_id: str, *, ledger: Any = None) -> CodexDirectExecutionResult | None:
    return reconcile_coding_agent_invocation(run_id, ledger=ledger)
