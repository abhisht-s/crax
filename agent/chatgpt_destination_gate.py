"""Pure read-only ChatGPT destination proof gate.

This module intentionally contains no live desktop automation.  It classifies a
normalized snapshot supplied by a narrow read-only adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


DESTINATION_VERIFIED_EXACT = "destination_verified_exact"
DESTINATION_VERIFICATION_FAILED = "destination_verification_failed"
READ_ONLY_ADAPTER_METHODS = ("read_destination_snapshot",)


@dataclass(frozen=True)
class DestinationLeaseContext:
    owning_run_id: str
    lease_token: str
    owner_pid: int
    acquired_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.owning_run_id, str):
            raise TypeError("owning_run_id must be a string")
        if not isinstance(self.lease_token, str):
            raise TypeError("lease_token must be a string")
        if not isinstance(self.owner_pid, int):
            raise TypeError("owner_pid must be an integer")
        if not isinstance(self.acquired_at, str):
            raise TypeError("acquired_at must be a string")

        owning_run_id = self.owning_run_id.strip()
        lease_token = self.lease_token.strip()
        acquired_at = self.acquired_at.strip()
        if owning_run_id == "":
            raise ValueError("owning_run_id must not be empty")
        if lease_token == "":
            raise ValueError("lease_token must not be empty")
        if acquired_at == "":
            raise ValueError("acquired_at must not be empty")

        object.__setattr__(self, "owning_run_id", owning_run_id)
        object.__setattr__(self, "lease_token", lease_token)
        object.__setattr__(self, "acquired_at", acquired_at)


@dataclass(frozen=True)
class DestinationEvidenceCandidate:
    title: str
    active: bool = False
    selected: bool = False
    identity_confirmed: bool = False
    actionable_destination_evidence: bool = False
    project_chats_list_confirmed: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str):
            raise TypeError("title must be a string")
        object.__setattr__(self, "title", self.title.strip())


@dataclass(frozen=True)
class ChatGPTDestinationSnapshot:
    process_running: bool
    window_available: bool
    accessibility_available: bool
    snapshot_stable: bool
    snapshot_complete: bool
    ax_tree_truncated: bool = False
    uncertainty_present: bool = False
    active_project_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    selected_chat_row_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    conversation_header_candidates: tuple[DestinationEvidenceCandidate, ...] = ()
    composer_available: bool = False
    transcript_available: bool = False
    conversation_surface_available: bool = False
    composer_candidate_count: int = 0
    conversation_surface_candidate_count: int = 0
    # Authoritative active-conversation identity read from the window title item
    # (ChatGPT Desktop exposes the open conversation as "<chat>, <project>").
    # Empty strings mean the title item was not present.
    active_conversation_chat_title: str = ""
    active_conversation_project_title: str = ""


@runtime_checkable
class ChatGPTDestinationReadOnlyAdapter(Protocol):
    def read_destination_snapshot(self) -> ChatGPTDestinationSnapshot: ...


@dataclass(frozen=True)
class DestinationEvidenceSummary:
    process_running: bool | None = None
    window_available: bool | None = None
    accessibility_available: bool | None = None
    snapshot_stable: bool | None = None
    snapshot_complete: bool | None = None
    ax_tree_truncated: bool | None = None
    uncertainty_present: bool | None = None
    active_project_candidate_count: int = 0
    matching_active_project_candidate_count: int = 0
    selected_chat_row_candidate_count: int = 0
    matching_selected_chat_row_candidate_count: int = 0
    conversation_header_candidate_count: int = 0
    matching_conversation_header_candidate_count: int = 0
    composer_available: bool | None = None
    transcript_available: bool | None = None
    conversation_surface_available: bool | None = None
    composer_candidate_count: int | None = None
    conversation_surface_candidate_count: int | None = None
    visible_matching_title_not_actionable: bool = False
    contradiction_present: bool = False
    active_conversation_identity_present: bool = False
    active_conversation_identity_matches: bool = False


@dataclass(frozen=True)
class DestinationGateResult:
    ok: bool
    state: str
    reason_code: str | None
    run_id: str
    binding_project_title: str | None
    binding_chat_title: str | None
    lease_owning_run_id: str | None
    lease_owner_pid: int | None
    lease_token_present: bool
    lease_context_matches_run: bool
    evidence_summary: DestinationEvidenceSummary


def verify_chatgpt_destination_snapshot(
    *,
    run_id: str,
    binding: Any,
    lease_context: DestinationLeaseContext | None,
    adapter: ChatGPTDestinationReadOnlyAdapter,
) -> DestinationGateResult:
    """Verify the active ChatGPT destination from injected read-only evidence."""

    normalized_binding = _normalize_binding(binding)
    if normalized_binding is None:
        return destination_gate_failure(
            run_id=run_id,
            reason_code="destination_binding_invalid",
            binding=None,
            lease_context=lease_context,
        )

    lease_failure = _lease_failure_reason(run_id, lease_context)
    if lease_failure is not None:
        return destination_gate_failure(
            run_id=run_id,
            reason_code=lease_failure,
            binding=normalized_binding,
            lease_context=lease_context,
        )

    try:
        snapshot = adapter.read_destination_snapshot()
    except Exception:
        return destination_gate_failure(
            run_id=run_id,
            reason_code="destination_verification_unavailable",
            binding=normalized_binding,
            lease_context=lease_context,
        )

    return _verify_snapshot(
        run_id=run_id,
        binding=normalized_binding,
        lease_context=lease_context,
        snapshot=snapshot,
    )


def destination_gate_failure(
    *,
    run_id: str,
    reason_code: str,
    binding: Any,
    lease_context: DestinationLeaseContext | None,
    evidence_summary: DestinationEvidenceSummary | None = None,
) -> DestinationGateResult:
    normalized_binding = _normalize_binding(binding)
    return DestinationGateResult(
        ok=False,
        state=DESTINATION_VERIFICATION_FAILED,
        reason_code=reason_code,
        run_id=run_id,
        binding_project_title=(
            normalized_binding.project_title if normalized_binding is not None else None
        ),
        binding_chat_title=(
            normalized_binding.chat_title if normalized_binding is not None else None
        ),
        lease_owning_run_id=(
            lease_context.owning_run_id if lease_context is not None else None
        ),
        lease_owner_pid=lease_context.owner_pid if lease_context is not None else None,
        lease_token_present=bool(
            lease_context is not None and lease_context.lease_token
        ),
        lease_context_matches_run=(
            lease_context is not None and lease_context.owning_run_id == run_id
        ),
        evidence_summary=evidence_summary or DestinationEvidenceSummary(),
    )


@dataclass(frozen=True)
class _NormalizedBinding:
    project_title: str
    chat_title: str


def _verify_snapshot(
    *,
    run_id: str,
    binding: _NormalizedBinding,
    lease_context: DestinationLeaseContext,
    snapshot: ChatGPTDestinationSnapshot,
) -> DestinationGateResult:
    summary = _summarize(snapshot, binding)
    if not snapshot.process_running:
        reason_code = "chatgpt_not_running"
    elif not snapshot.window_available:
        reason_code = "chatgpt_window_unavailable"
    elif not snapshot.accessibility_available:
        reason_code = "accessibility_unavailable"
    elif (
        not snapshot.snapshot_stable
        or not snapshot.snapshot_complete
        or snapshot.ax_tree_truncated
        or snapshot.uncertainty_present
    ):
        reason_code = "ax_tree_truncated_or_unstable"
    else:
        reason_code = _destination_evidence_failure_reason(snapshot, binding)

    if reason_code is not None:
        return destination_gate_failure(
            run_id=run_id,
            reason_code=reason_code,
            binding=binding,
            lease_context=lease_context,
            evidence_summary=summary,
        )

    return DestinationGateResult(
        ok=True,
        state=DESTINATION_VERIFIED_EXACT,
        reason_code=None,
        run_id=run_id,
        binding_project_title=binding.project_title,
        binding_chat_title=binding.chat_title,
        lease_owning_run_id=lease_context.owning_run_id,
        lease_owner_pid=lease_context.owner_pid,
        lease_token_present=True,
        lease_context_matches_run=True,
        evidence_summary=summary,
    )


def _active_conversation_identity_matches(
    snapshot: ChatGPTDestinationSnapshot,
    binding: _NormalizedBinding,
) -> bool:
    return bool(
        snapshot.active_conversation_chat_title
        and snapshot.active_conversation_project_title
        and snapshot.active_conversation_chat_title == binding.chat_title
        and snapshot.active_conversation_project_title == binding.project_title
    )


def _destination_evidence_failure_reason(
    snapshot: ChatGPTDestinationSnapshot,
    binding: _NormalizedBinding,
) -> str | None:
    # Authoritative fast-path: the window title item shows the single open
    # conversation as an exact "<chat>, <project>" identity. An exact match of
    # both bound titles, with a single composer present (i.e. a conversation
    # view with an input), is sufficient and unambiguous proof of the exact
    # destination. This cannot match the wrong chat because it is the window's
    # own title of the one open conversation. When it does not match, the strict
    # heuristic path below is unchanged and remains fail-closed.
    if _active_conversation_identity_matches(snapshot, binding):
        if snapshot.composer_available and snapshot.composer_candidate_count == 1:
            return None
        return "chat_identity_unconfirmed"

    project_reason = _project_failure_reason(
        snapshot.active_project_candidates,
        binding.project_title,
    )
    if project_reason is not None:
        return project_reason

    selected_reason = _chat_candidate_failure_reason(
        snapshot.selected_chat_row_candidates,
        binding.chat_title,
        missing_reason="chat_not_active",
    )
    if selected_reason is not None:
        return selected_reason

    header_reason = _chat_candidate_failure_reason(
        snapshot.conversation_header_candidates,
        binding.chat_title,
        missing_reason="chat_identity_unconfirmed",
    )
    if header_reason is not None:
        return header_reason

    if (
        not snapshot.composer_available
        or not snapshot.transcript_available
        or not snapshot.conversation_surface_available
        or snapshot.composer_candidate_count > 1
        or snapshot.conversation_surface_candidate_count > 1
    ):
        return "chat_identity_unconfirmed"

    return None


def _project_failure_reason(
    candidates: tuple[DestinationEvidenceCandidate, ...],
    target_title: str,
) -> str | None:
    matching = _matching_candidates(candidates, target_title)
    if len(matching) > 1:
        return "project_title_ambiguous"
    if _has_active_nonmatching_candidate(candidates, target_title):
        return "project_not_active"
    if not matching:
        return "project_not_active"

    candidate = matching[0]
    if not candidate.actionable_destination_evidence or not candidate.active:
        return "visible_title_not_actionable_destination"
    if not candidate.identity_confirmed:
        return "project_identity_unconfirmed"
    if candidate.project_chats_list_confirmed is not True:
        return "project_chats_list_unconfirmed"
    return None


def _chat_candidate_failure_reason(
    candidates: tuple[DestinationEvidenceCandidate, ...],
    target_title: str,
    *,
    missing_reason: str,
) -> str | None:
    matching = _matching_candidates(candidates, target_title)
    if len(matching) > 1:
        return "chat_title_ambiguous"
    if _has_active_nonmatching_candidate(candidates, target_title):
        return "chat_not_active"
    if not matching:
        return missing_reason

    candidate = matching[0]
    if not candidate.actionable_destination_evidence or not (
        candidate.active or candidate.selected
    ):
        return "visible_title_not_actionable_destination"
    if not candidate.identity_confirmed:
        return "chat_identity_unconfirmed"
    return None


def _summarize(
    snapshot: ChatGPTDestinationSnapshot,
    binding: _NormalizedBinding,
) -> DestinationEvidenceSummary:
    project_matches = _matching_candidates(
        snapshot.active_project_candidates,
        binding.project_title,
    )
    selected_matches = _matching_candidates(
        snapshot.selected_chat_row_candidates,
        binding.chat_title,
    )
    header_matches = _matching_candidates(
        snapshot.conversation_header_candidates,
        binding.chat_title,
    )
    return DestinationEvidenceSummary(
        process_running=snapshot.process_running,
        window_available=snapshot.window_available,
        accessibility_available=snapshot.accessibility_available,
        snapshot_stable=snapshot.snapshot_stable,
        snapshot_complete=snapshot.snapshot_complete,
        ax_tree_truncated=snapshot.ax_tree_truncated,
        uncertainty_present=snapshot.uncertainty_present,
        active_project_candidate_count=len(snapshot.active_project_candidates),
        matching_active_project_candidate_count=len(project_matches),
        selected_chat_row_candidate_count=len(snapshot.selected_chat_row_candidates),
        matching_selected_chat_row_candidate_count=len(selected_matches),
        conversation_header_candidate_count=len(snapshot.conversation_header_candidates),
        matching_conversation_header_candidate_count=len(header_matches),
        composer_available=snapshot.composer_available,
        transcript_available=snapshot.transcript_available,
        conversation_surface_available=snapshot.conversation_surface_available,
        composer_candidate_count=snapshot.composer_candidate_count,
        conversation_surface_candidate_count=snapshot.conversation_surface_candidate_count,
        visible_matching_title_not_actionable=(
            _has_nonactionable_match(project_matches)
            or _has_nonactionable_match(selected_matches)
            or _has_nonactionable_match(header_matches)
        ),
        contradiction_present=(
            _has_active_nonmatching_candidate(
                snapshot.active_project_candidates,
                binding.project_title,
            )
            or _has_active_nonmatching_candidate(
                snapshot.selected_chat_row_candidates,
                binding.chat_title,
            )
            or _has_active_nonmatching_candidate(
                snapshot.conversation_header_candidates,
                binding.chat_title,
            )
        ),
        active_conversation_identity_present=bool(
            snapshot.active_conversation_chat_title
            and snapshot.active_conversation_project_title
        ),
        active_conversation_identity_matches=_active_conversation_identity_matches(
            snapshot, binding
        ),
    )


def _has_nonactionable_match(
    candidates: tuple[DestinationEvidenceCandidate, ...],
) -> bool:
    return any(
        not candidate.actionable_destination_evidence
        or not (candidate.active or candidate.selected)
        for candidate in candidates
    )


def _matching_candidates(
    candidates: tuple[DestinationEvidenceCandidate, ...],
    title: str,
) -> tuple[DestinationEvidenceCandidate, ...]:
    return tuple(candidate for candidate in candidates if candidate.title == title)


def _has_active_nonmatching_candidate(
    candidates: tuple[DestinationEvidenceCandidate, ...],
    target_title: str,
) -> bool:
    return any(
        candidate.title != target_title
        and (candidate.active or candidate.selected)
        and candidate.actionable_destination_evidence
        for candidate in candidates
    )


def _lease_failure_reason(
    run_id: str,
    lease_context: DestinationLeaseContext | None,
) -> str | None:
    if lease_context is None:
        return "destination_lease_invalid_or_mismatched"
    if lease_context.owning_run_id != run_id:
        return "destination_lease_invalid_or_mismatched"
    if lease_context.lease_token.strip() == "":
        return "destination_lease_invalid_or_mismatched"
    return None


def _normalize_binding(binding: Any) -> _NormalizedBinding | None:
    if binding is None:
        return None
    try:
        project_title = binding.project_title
        chat_title = binding.chat_title
    except AttributeError:
        return None
    if not isinstance(project_title, str) or not isinstance(chat_title, str):
        return None
    project_title = project_title.strip()
    chat_title = chat_title.strip()
    if project_title == "" or chat_title == "":
        return None
    return _NormalizedBinding(project_title, chat_title)
