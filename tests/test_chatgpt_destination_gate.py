from __future__ import annotations

import inspect
import unittest

from agent.chatgpt_destination_gate import (
    DESTINATION_VERIFIED_EXACT,
    READ_ONLY_ADAPTER_METHODS,
    ChatGPTDestinationReadOnlyAdapter,
    ChatGPTDestinationSnapshot,
    DestinationEvidenceCandidate,
    DestinationLeaseContext,
)
from agent.run_services import (
    CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
    CHATGPT_UI_LEASE_SCHEMA_VERSION,
    RUN_DESTINATION_BOUND_EVENT_TYPE,
    RUN_DESTINATION_BOUND_MESSAGE,
    RUN_DESTINATION_BOUND_SCHEMA_VERSION,
    verify_chatgpt_destination_for_run,
)


RUN_ID = "run-1"
PROJECT_TITLE = "Bound Project"
CHAT_TITLE = "Bound Chat"
LEASE_TOKEN = "lease-token"
OWNER_PID = 12345
ACQUIRED_AT = "2026-01-01T00:00:00+00:00"


class ChatGPTDestinationGateTests(unittest.TestCase):
    def test_exact_project_chat_success_requires_all_active_chat_evidence(self) -> None:
        result = verify_chatgpt_destination_for_run(
            RUN_ID,
            _lease_context(),
            _FakeReadOnlyAdapter(_snapshot()),
            ledger=_FakeGateLedger.with_binding_and_lease(),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.state, DESTINATION_VERIFIED_EXACT)
        self.assertIsNone(result.reason_code)
        self.assertEqual(result.binding_project_title, PROJECT_TITLE)
        self.assertEqual(result.binding_chat_title, CHAT_TITLE)
        self.assertTrue(result.lease_token_present)
        self.assertEqual(
            result.evidence_summary.matching_selected_chat_row_candidate_count,
            1,
        )
        self.assertEqual(
            result.evidence_summary.matching_conversation_header_candidate_count,
            1,
        )
        self.assertTrue(result.evidence_summary.composer_available)
        self.assertTrue(result.evidence_summary.transcript_available)

    def test_matching_title_without_selected_row_evidence_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(selected_chat_row_candidates=()),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_not_active")

    def test_matching_title_without_conversation_header_evidence_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(conversation_header_candidates=()),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_matching_title_without_conversation_structure_fails(self) -> None:
        for override in (
            {"composer_available": False},
            {"transcript_available": False},
            {"conversation_surface_available": False},
        ):
            with self.subTest(override=override):
                result = _verify_snapshot(_snapshot(**override))

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_toolbar_only_chat_title_evidence_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(
                selected_chat_row_candidates=(),
                conversation_header_candidates=(
                    DestinationEvidenceCandidate(
                        CHAT_TITLE,
                        active=False,
                        identity_confirmed=False,
                        actionable_destination_evidence=True,
                    ),
                ),
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_not_active")

    def test_ambiguous_composer_or_conversation_surface_counts_fail(self) -> None:
        for override in (
            {"composer_candidate_count": 2},
            {"conversation_surface_candidate_count": 2},
        ):
            with self.subTest(override=override):
                result = _verify_snapshot(_snapshot(**override))

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_wrong_active_project_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(active_project_candidates=(_project_candidate("Wrong Project"),)),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "project_not_active")
        self.assertTrue(result.evidence_summary.contradiction_present)

    def test_wrong_active_chat_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(selected_chat_row_candidates=(_chat_candidate("Wrong Chat"),)),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_not_active")
        self.assertTrue(result.evidence_summary.contradiction_present)

    def test_duplicate_project_title_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(
                active_project_candidates=(
                    _project_candidate(PROJECT_TITLE),
                    _project_candidate(PROJECT_TITLE),
                ),
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "project_title_ambiguous")

    def test_duplicate_chat_title_fails(self) -> None:
        result = _verify_snapshot(
            _snapshot(
                selected_chat_row_candidates=(
                    _chat_candidate(CHAT_TITLE),
                    _chat_candidate(CHAT_TITLE),
                ),
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat_title_ambiguous")

    def test_truncated_or_unstable_ax_evidence_fails(self) -> None:
        for override in (
            {"ax_tree_truncated": True},
            {"snapshot_stable": False},
            {"snapshot_complete": False},
            {"uncertainty_present": True},
        ):
            with self.subTest(override=override):
                result = _verify_snapshot(_snapshot(**override))

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "ax_tree_truncated_or_unstable")

    def test_inaccessible_process_window_or_accessibility_fails(self) -> None:
        cases = (
            ({"process_running": False}, "chatgpt_not_running"),
            ({"window_available": False}, "chatgpt_window_unavailable"),
            ({"accessibility_available": False}, "accessibility_unavailable"),
        )
        for override, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = _verify_snapshot(_snapshot(**override))

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason_code)

    def test_visible_but_non_actionable_labels_fail(self) -> None:
        result = _verify_snapshot(
            _snapshot(
                active_project_candidates=(
                    DestinationEvidenceCandidate(
                        PROJECT_TITLE,
                        active=False,
                        identity_confirmed=False,
                        actionable_destination_evidence=False,
                    ),
                ),
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "visible_title_not_actionable_destination")
        self.assertTrue(result.evidence_summary.visible_matching_title_not_actionable)

    def test_active_conversation_identity_verifies_despite_missing_heuristic_evidence(self) -> None:
        # Mirrors the real conversation-open state (run f85f7126): the window
        # title item proves "<chat>, <project>" while the chats-list heuristics
        # (selected row, matching header) are absent and surfaces are ambiguous.
        result = _verify_snapshot(
            _snapshot(
                active_conversation_chat_title=CHAT_TITLE,
                active_conversation_project_title=PROJECT_TITLE,
                active_project_candidates=(
                    DestinationEvidenceCandidate(
                        PROJECT_TITLE,
                        active=False,
                        identity_confirmed=False,
                        actionable_destination_evidence=False,
                    ),
                ),
                selected_chat_row_candidates=(),
                conversation_header_candidates=(),
                composer_available=True,
                composer_candidate_count=1,
                transcript_available=False,
                conversation_surface_available=False,
                conversation_surface_candidate_count=4,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.state, DESTINATION_VERIFIED_EXACT)
        self.assertIsNone(result.reason_code)
        self.assertTrue(result.evidence_summary.active_conversation_identity_present)
        self.assertTrue(result.evidence_summary.active_conversation_identity_matches)

    def test_active_conversation_identity_requires_single_composer(self) -> None:
        for override in (
            {"composer_available": False, "composer_candidate_count": 0},
            {"composer_available": True, "composer_candidate_count": 2},
        ):
            with self.subTest(override=override):
                result = _verify_snapshot(
                    _snapshot(
                        active_conversation_chat_title=CHAT_TITLE,
                        active_conversation_project_title=PROJECT_TITLE,
                        selected_chat_row_candidates=(),
                        conversation_header_candidates=(),
                        **override,
                    )
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, "chat_identity_unconfirmed")

    def test_active_conversation_identity_mismatch_falls_back_to_heuristics(self) -> None:
        # A non-matching identity must not short-circuit; the strict heuristic
        # path still governs and (here, with full evidence) verifies.
        result = _verify_snapshot(
            _snapshot(
                active_conversation_chat_title="Other Chat",
                active_conversation_project_title="Other Project",
            )
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.evidence_summary.active_conversation_identity_matches)

    def test_active_conversation_identity_partial_match_is_insufficient(self) -> None:
        # Chat matches but project does not -> not an authoritative match; with
        # the heuristic evidence absent this stays fail-closed.
        result = _verify_snapshot(
            _snapshot(
                active_conversation_chat_title=CHAT_TITLE,
                active_conversation_project_title="Other Project",
                active_project_candidates=(
                    DestinationEvidenceCandidate(
                        PROJECT_TITLE,
                        active=False,
                        identity_confirmed=False,
                        actionable_destination_evidence=False,
                    ),
                ),
                selected_chat_row_candidates=(),
                conversation_header_candidates=(),
                composer_candidate_count=1,
            )
        )

        self.assertFalse(result.ok)
        self.assertFalse(result.evidence_summary.active_conversation_identity_matches)

    def test_missing_destination_binding_fails_without_adapter_access(self) -> None:
        adapter = _FakeReadOnlyAdapter(_snapshot())

        result = verify_chatgpt_destination_for_run(
            RUN_ID,
            _lease_context(),
            adapter,
            ledger=_FakeGateLedger.with_lease_only(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_binding_missing")
        self.assertEqual(adapter.read_count, 0)

    def test_invalid_destination_binding_fails_without_adapter_access(self) -> None:
        adapter = _FakeReadOnlyAdapter(_snapshot())
        fake_ledger = _FakeGateLedger.with_binding_and_lease()
        fake_ledger.events[0]["metadata"] = {
            "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
            "project_title": PROJECT_TITLE,
        }

        result = verify_chatgpt_destination_for_run(
            RUN_ID,
            _lease_context(),
            adapter,
            ledger=fake_ledger,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination_binding_invalid")
        self.assertEqual(adapter.read_count, 0)

    def test_lease_run_or_token_mismatch_fails_before_adapter_access(self) -> None:
        cases = (
            _lease_context(run_id="other-run"),
            _lease_context(token="wrong-token"),
        )
        for lease_context in cases:
            with self.subTest(lease_context=lease_context):
                adapter = _FakeReadOnlyAdapter(_snapshot())

                result = verify_chatgpt_destination_for_run(
                    RUN_ID,
                    lease_context,
                    adapter,
                    ledger=_FakeGateLedger.with_binding_and_lease(),
                )

                self.assertFalse(result.ok)
                self.assertEqual(
                    result.reason_code,
                    "destination_lease_invalid_or_mismatched",
                )
                self.assertEqual(adapter.read_count, 0)

    def test_read_only_adapter_contract_has_no_mutation_methods(self) -> None:
        self.assertEqual(READ_ONLY_ADAPTER_METHODS, ("read_destination_snapshot",))
        signature = inspect.signature(
            ChatGPTDestinationReadOnlyAdapter.read_destination_snapshot,
        )
        self.assertEqual(tuple(signature.parameters), ("self",))

        forbidden = (
            "activate",
            "click",
            "scroll",
            "press",
            "focus",
            "clipboard",
            "type",
            "paste",
            "submit",
            "capture",
        )
        for method_name in READ_ONLY_ADAPTER_METHODS:
            self.assertFalse(any(fragment in method_name for fragment in forbidden))

    def test_no_timeout_or_deadline_behavior_is_introduced(self) -> None:
        from agent.chatgpt_destination_gate import verify_chatgpt_destination_snapshot

        functions = (
            verify_chatgpt_destination_for_run,
            verify_chatgpt_destination_snapshot,
        )
        forbidden = ("timeout", "deadline", "elapsed", "expires")
        for function in functions:
            names = inspect.signature(function).parameters
            self.assertFalse(
                any(fragment in name for name in names for fragment in forbidden),
                function.__name__,
            )


def _verify_snapshot(snapshot: ChatGPTDestinationSnapshot):
    return verify_chatgpt_destination_for_run(
        RUN_ID,
        _lease_context(),
        _FakeReadOnlyAdapter(snapshot),
        ledger=_FakeGateLedger.with_binding_and_lease(),
    )


def _snapshot(**overrides) -> ChatGPTDestinationSnapshot:
    values = {
        "process_running": True,
        "window_available": True,
        "accessibility_available": True,
        "snapshot_stable": True,
        "snapshot_complete": True,
        "ax_tree_truncated": False,
        "uncertainty_present": False,
        "active_project_candidates": (_project_candidate(PROJECT_TITLE),),
        "selected_chat_row_candidates": (_chat_candidate(CHAT_TITLE),),
        "conversation_header_candidates": (_header_candidate(CHAT_TITLE),),
        "composer_available": True,
        "transcript_available": True,
        "conversation_surface_available": True,
    }
    values.update(overrides)
    return ChatGPTDestinationSnapshot(**values)


def _project_candidate(title: str) -> DestinationEvidenceCandidate:
    return DestinationEvidenceCandidate(
        title,
        active=True,
        identity_confirmed=True,
        actionable_destination_evidence=True,
        project_chats_list_confirmed=True,
    )


def _chat_candidate(title: str) -> DestinationEvidenceCandidate:
    return DestinationEvidenceCandidate(
        title,
        active=True,
        selected=True,
        identity_confirmed=True,
        actionable_destination_evidence=True,
    )


def _header_candidate(title: str) -> DestinationEvidenceCandidate:
    return DestinationEvidenceCandidate(
        title,
        active=True,
        identity_confirmed=True,
        actionable_destination_evidence=True,
    )


def _lease_context(
    *,
    run_id: str = RUN_ID,
    token: str = LEASE_TOKEN,
) -> DestinationLeaseContext:
    return DestinationLeaseContext(
        owning_run_id=run_id,
        lease_token=token,
        owner_pid=OWNER_PID,
        acquired_at=ACQUIRED_AT,
    )


class _FakeReadOnlyAdapter:
    def __init__(self, snapshot: ChatGPTDestinationSnapshot) -> None:
        self.snapshot = snapshot
        self.read_count = 0

    def read_destination_snapshot(self) -> ChatGPTDestinationSnapshot:
        self.read_count += 1
        return self.snapshot


class _FakeGateLedger:
    def __init__(
        self,
        *,
        events: list[dict] | None = None,
        lease_events: list[dict] | None = None,
    ) -> None:
        self.events = events or []
        self.lease_events = lease_events or []

    @classmethod
    def with_binding_and_lease(cls) -> "_FakeGateLedger":
        return cls(
            events=[_binding_event()],
            lease_events=[_lease_event()],
        )

    @classmethod
    def with_lease_only(cls) -> "_FakeGateLedger":
        return cls(lease_events=[_lease_event()])

    def list_events(self, run_id: str) -> list[dict]:
        return [event for event in self.events if event["run_id"] == run_id]

    def list_chatgpt_ui_lease_events(self) -> list[dict]:
        return list(self.lease_events)


def _binding_event() -> dict:
    return {
        "id": 1,
        "run_id": RUN_ID,
        "event_type": RUN_DESTINATION_BOUND_EVENT_TYPE,
        "message": RUN_DESTINATION_BOUND_MESSAGE,
        "metadata": {
            "schema_version": RUN_DESTINATION_BOUND_SCHEMA_VERSION,
            "project_title": PROJECT_TITLE,
            "chat_title": CHAT_TITLE,
        },
    }


def _lease_event() -> dict:
    return {
        "id": 2,
        "run_id": RUN_ID,
        "event_type": CHATGPT_UI_LEASE_ACQUIRED_EVENT_TYPE,
        "metadata": {
            "schema_version": CHATGPT_UI_LEASE_SCHEMA_VERSION,
            "lease_token": LEASE_TOKEN,
            "owner_pid": OWNER_PID,
            "owning_run_id": RUN_ID,
            "acquired_at": ACQUIRED_AT,
        },
    }


if __name__ == "__main__":
    unittest.main()
