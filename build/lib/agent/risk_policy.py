from __future__ import annotations


POLICY_VERSION = "risk_policy_v2_observation_only"

APPROVAL_REQUIRED_FLAGS = {
    "audit_only_modified_files",
    "secret_or_env_touched",
    "unexpected_high_risk_files",
    "frontend_scope_touched_backend",
    "backend_scope_touched_frontend",
}

HIGH_ATTENTION_APPROVAL_FLAGS = {
    "secret_or_env_touched",
    "unexpected_high_risk_files",
}

NEEDS_REVIEW_FLAGS = {
    "database_or_migration_touched",
    "timed_out_with_no_changes",
    "codex_ended_uncleanly_with_changes",
    "implementation_intent_no_files_changed",
    "implementation_intent_docs_only",
}


def _diagnostic_flags(diagnostics: dict) -> list[str]:
    raw_flags = diagnostics.get("flags")
    if not isinstance(raw_flags, list):
        return []

    flags = []
    for flag in raw_flags:
        flag_text = str(flag).strip()
        if flag_text:
            flags.append(flag_text)
    return flags


def _matching_flags(flags: list[str], policy_flags: set[str]) -> list[str]:
    return [flag for flag in flags if flag in policy_flags]


def evaluate_supervision_decision(diagnostics: dict | None) -> dict:
    if diagnostics is None:
        return {
            "decision": "needs_review",
            "attention_level": "needs_review",
            "approval_required": False,
            "needs_review": True,
            "reasons": ["diagnostics_missing"],
            "messages": [
                "Prompt/repo impact diagnostics were unavailable; review this run before relying on the result."
            ],
            "policy_version": POLICY_VERSION,
            "source_flags": [],
        }

    source_flags = _diagnostic_flags(diagnostics)

    if source_flags:
        return {
            "decision": "record_only",
            "attention_level": "info",
            "approval_required": False,
            "needs_review": False,
            "reasons": source_flags,
            "messages": [
                "Diagnostics flags are context only; no review or approval is required by this policy."
            ],
            "policy_version": POLICY_VERSION,
            "source_flags": source_flags,
        }

    return {
        "decision": "continue",
        "attention_level": "ok",
        "approval_required": False,
        "needs_review": False,
        "reasons": [],
        "messages": ["No supervision concerns detected."],
        "policy_version": POLICY_VERSION,
        "source_flags": [],
    }
