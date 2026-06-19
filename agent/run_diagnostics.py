from __future__ import annotations


ATTENTION_LEVELS = ("ok", "info", "needs_review", "high_attention")

AUDIT_ONLY_PHRASES = (
    "audit only",
    "read only",
    "read-only",
    "no changes",
    "do not modify",
    "do not edit",
    "no files should be changed",
    "do not write files",
    "do not change files",
    "without making changes",
)

FRONTEND_SCOPE_PHRASES = (
    "frontend only",
    "ui only",
    "client only",
    "ios only",
    "swift only",
    "website ui",
    "scoped frontend",
    "frontend change",
    "ui change",
    "no backend changes",
)

BACKEND_SCOPE_PHRASES = (
    "backend only",
    "database only",
    "db only",
    "migration only",
    "sql only",
    "server only",
    "rpc only",
    "supabase only",
    "no frontend changes",
    "no ui changes",
)

IMPLEMENTATION_INTENT_PHRASES = (
    "implement",
    "fix",
    "change",
    "patch",
    "add",
    "update",
    "modify",
    "create",
    "write migration",
    "make the change",
    "make this change",
)

HIGH_RISK_INTENT_PHRASES = (
    "migration",
    "database",
    "sql",
    "supabase",
    "env",
    "secret",
    "credential",
    "key",
)

BACKEND_PATH_MARKERS = (
    "supabase/",
    "migrations/",
    "server/",
    "api/",
    "functions/",
    "workers/",
    "backend/",
    "database/",
    "schema",
)

FRONTEND_PATH_MARKERS = (
    "components/",
    "views/",
    "screens/",
    "app/",
    "pages/",
    "ui/",
)

FRONTEND_SUFFIXES = (".swift", ".jsx", ".tsx", ".css", ".scss", ".html")
SECRET_PATH_MARKERS = (".env", "secret", "secrets", "credential", "credentials", ".pem", ".key")


def _contains_any(value: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in value for phrase in phrases)


def _prompt_intents(prompt: str) -> list[str]:
    normalized_prompt = prompt.lower()
    intents = []

    audit_only = _contains_any(normalized_prompt, AUDIT_ONLY_PHRASES)
    if audit_only:
        intents.append("audit_only")
    if _contains_any(normalized_prompt, FRONTEND_SCOPE_PHRASES):
        intents.append("frontend_scope")
    if _contains_any(normalized_prompt, BACKEND_SCOPE_PHRASES):
        intents.append("backend_scope")
    if not audit_only and _contains_any(normalized_prompt, IMPLEMENTATION_INTENT_PHRASES):
        intents.append("implementation_intent")

    return intents


def _snapshot_dirty(snapshot: dict | None) -> bool | None:
    if snapshot is None:
        return None
    return bool((snapshot.get("status_short") or "").strip())


def _snapshot_changed_files(snapshot: dict | None) -> list[str]:
    if snapshot is None:
        return []
    diff_name_only = snapshot.get("diff_name_only") or ""
    return [line.strip() for line in diff_name_only.splitlines() if line.strip()]


def _classification_files(classification: dict | None) -> list[dict]:
    if not classification:
        return []
    files = classification.get("files")
    if not isinstance(files, list):
        return []
    return [file for file in files if isinstance(file, dict)]


def _changed_files(
    after_snapshot: dict | None,
    classification: dict | None,
) -> tuple[list[str], dict[str, str | None]]:
    raw_files = classification.get("files") if classification else None
    if isinstance(raw_files, list):
        files = [file for file in raw_files if isinstance(file, dict)]
        changed_files = [
            str(file.get("path") or "").strip()
            for file in files
            if str(file.get("path") or "").strip()
        ]
        categories = {
            str(file.get("path") or "").strip(): file.get("category")
            for file in files
            if str(file.get("path") or "").strip()
        }
        return changed_files, categories

    changed_files = _snapshot_changed_files(after_snapshot)
    return changed_files, {path: None for path in changed_files}


def _high_risk_files(classification: dict | None) -> list[str]:
    if not classification:
        return []
    high_risk_files = classification.get("high_risk_files")
    if not isinstance(high_risk_files, list):
        return []
    return [str(path).strip() for path in high_risk_files if str(path).strip()]


def _is_backend_file(path: str, category: str | None) -> bool:
    lower_path = path.lower()
    return (
        category == "database_migration"
        or lower_path.endswith(".sql")
        or any(marker in lower_path for marker in BACKEND_PATH_MARKERS)
    )


def _is_frontend_file(path: str) -> bool:
    lower_path = path.lower()
    return lower_path.endswith(FRONTEND_SUFFIXES) or any(
        marker in lower_path for marker in FRONTEND_PATH_MARKERS
    )


def _is_database_or_migration_file(path: str, category: str | None) -> bool:
    lower_path = path.lower()
    return category == "database_migration" or lower_path.endswith(".sql") or "migrations" in lower_path


def _is_secret_or_env_file(path: str, category: str | None) -> bool:
    lower_path = path.lower()
    return category == "secrets_or_env" or any(marker in lower_path for marker in SECRET_PATH_MARKERS)


def _attention_at_least(current: str, minimum: str) -> str:
    if ATTENTION_LEVELS.index(minimum) > ATTENTION_LEVELS.index(current):
        return minimum
    return current


def analyze_prompt_repo_impact(
    prompt: str,
    codex_result: dict,
    before_snapshot: dict | None,
    after_snapshot: dict | None,
    classification: dict | None,
) -> dict:
    prompt_intents = _prompt_intents(prompt)
    changed_files, categories_by_path = _changed_files(after_snapshot, classification)
    high_risk_files = _high_risk_files(classification)

    flags: list[str] = []
    messages: list[str] = []
    attention_level = "ok"

    def add_flag(flag: str, message: str, level: str) -> None:
        nonlocal attention_level
        flags.append(flag)
        messages.append(message)
        attention_level = _attention_at_least(attention_level, level)

    changed_files_count = len(changed_files)
    high_risk_files_count = len(high_risk_files)
    codex_exit_code = codex_result.get("exit_code")
    codex_timed_out = bool(codex_result.get("timed_out"))
    before_dirty = _snapshot_dirty(before_snapshot)
    after_dirty = _snapshot_dirty(after_snapshot)
    validation_error = bool(codex_result.get("validation_error"))

    touched_backend = any(
        _is_backend_file(path, categories_by_path.get(path))
        for path in changed_files
    )
    touched_frontend = any(_is_frontend_file(path) for path in changed_files)
    touched_database_or_migration = any(
        _is_database_or_migration_file(path, categories_by_path.get(path))
        for path in changed_files
    )
    touched_secret_or_env = any(
        _is_secret_or_env_file(path, categories_by_path.get(path))
        for path in changed_files
    )
    docs_only = (
        changed_files_count > 0
        and all(categories_by_path.get(path) == "docs" for path in changed_files)
    )

    if "audit_only" in prompt_intents and changed_files_count > 0:
        add_flag("audit_only_modified_files", "audit/read-only prompt changed files.", "needs_review")

    if "frontend_scope" in prompt_intents and touched_backend:
        add_flag(
            "frontend_scope_touched_backend",
            "frontend-scoped prompt touched backend/database files.",
            "needs_review",
        )

    if "backend_scope" in prompt_intents and touched_frontend:
        add_flag(
            "backend_scope_touched_frontend",
            "backend-scoped prompt touched frontend/UI files.",
            "needs_review",
        )

    if touched_database_or_migration:
        add_flag(
            "database_or_migration_touched",
            "database/migration files touched; review manually.",
            "needs_review",
        )

    if touched_secret_or_env:
        add_flag(
            "secret_or_env_touched",
            "secret/env/credential-like file touched.",
            "high_attention",
        )

    if codex_timed_out and changed_files_count == 0:
        add_flag(
            "timed_out_with_no_changes",
            "Codex timed out and no files changed.",
            "needs_review",
        )

    if (codex_timed_out or (codex_exit_code is not None and codex_exit_code != 0)) and changed_files_count > 0:
        add_flag(
            "codex_ended_uncleanly_with_changes",
            "Codex may have completed code changes but ended uncleanly while summarizing/reporting.",
            "needs_review",
        )

    if "implementation_intent" in prompt_intents and changed_files_count == 0:
        add_flag(
            "implementation_intent_no_files_changed",
            "implementation-like prompt changed no files.",
            "needs_review",
        )

    if "implementation_intent" in prompt_intents and docs_only:
        add_flag(
            "implementation_intent_docs_only",
            "implementation-like prompt changed only docs.",
            "needs_review",
        )

    prompt_mentions_high_risk_intent = _contains_any(prompt.lower(), HIGH_RISK_INTENT_PHRASES)
    if high_risk_files_count > 0 and not prompt_mentions_high_risk_intent:
        add_flag(
            "unexpected_high_risk_files",
            "high-risk files touched without explicit prompt intent.",
            "high_attention",
        )

    if before_dirty:
        add_flag("repo_dirty_before_codex", "repo was dirty before Codex ran.", "info")

    if any(flag in {"secret_or_env_touched", "unexpected_high_risk_files"} for flag in flags):
        outcome = "high_attention"
    elif any(
        flag
        in {
            "audit_only_modified_files",
            "frontend_scope_touched_backend",
            "backend_scope_touched_frontend",
            "database_or_migration_touched",
            "timed_out_with_no_changes",
            "codex_ended_uncleanly_with_changes",
            "implementation_intent_no_files_changed",
            "implementation_intent_docs_only",
        }
        for flag in flags
    ):
        outcome = "needs_review"
    elif validation_error:
        outcome = "validation_failed"
        attention_level = _attention_at_least(attention_level, "needs_review")
    elif "audit_only" in prompt_intents and changed_files_count == 0:
        outcome = "clean_expected_no_changes"
    elif changed_files_count > 0:
        outcome = "clean_completed_with_changes"
    else:
        outcome = "clean_expected_no_changes"

    return {
        "outcome": outcome,
        "attention_level": attention_level,
        "flags": flags,
        "messages": messages,
        "prompt_intents": prompt_intents,
        "changed_files_count": changed_files_count,
        "high_risk_files_count": high_risk_files_count,
        "changed_files": changed_files,
        "high_risk_files": high_risk_files,
        "codex_exit_code": codex_exit_code,
        "codex_timed_out": codex_timed_out,
        "before_dirty": before_dirty,
        "after_dirty": after_dirty,
    }
