"""Provider contract; durable scheduling and process ownership remain CRAX concerns."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class NativeSessionIdentity:
    coding_agent: str
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.coding_agent, str) or not self.coding_agent.strip():
            raise ValueError("Native session requires a coding-agent identity.")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("Native session requires a nonempty provider session ID.")


@dataclass(frozen=True)
class CodingAgentRequest:
    run_id: str
    prompt: str
    repo_path: str
    sandbox: str
    model: str
    invocation_id: str | None = None
    artifact_dir: str | None = None
    progress_callback: Callable[[dict[str, object]], None] | None = None
    extraction_event_id: int | None = None
    extraction_prompt_sha256: str | None = None
    native_session: NativeSessionIdentity | None = None


@dataclass(frozen=True)
class ExecutionOutcome:
    """Independent evidence dimensions, never inferred from arbitrary stdout."""
    process: str = "not_started"
    provider: str = "not_started"
    final_answer: str = "unavailable"
    cancelled: bool = False
    interrupted: bool = False
    uncertain: bool = False
    native_session: NativeSessionIdentity | None = None


class CodingAgentProvider(Protocol):
    identity: str

    def execute(self, request: CodingAgentRequest) -> dict[str, Any]: ...

    def observe(self, invocation: Any, *, progress_callback: Callable | None) -> dict[str, Any]: ...

    def normalize_progress(self, line: str) -> dict[str, object]: ...

    def session_details(self, run_id: str, events: list[dict]) -> dict[str, Any]: ...

    def classify(self, result: dict[str, Any]) -> ExecutionOutcome: ...

    def reason_code(self, result: dict[str, Any]) -> str: ...

    def error_message(self, result: dict[str, Any], reason: str) -> str | None: ...

    def validation_result(self, prompt: str, repo_path: str, sandbox: str, error: str) -> dict[str, Any]: ...

    def cancel(self, run_id: str, **kwargs: Any) -> dict[str, object]: ...
