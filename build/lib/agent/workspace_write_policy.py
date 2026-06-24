from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from agent.file_classifier import classify_changed_files
from agent.prompt_contract import parse_prompt_contract


POLICY_VERSION = "workspace_write_auto_policy_v1"

TIER_READ_ONLY_ROUTINE_AUTO = "read_only_routine_auto"
TIER_WORKSPACE_WRITE_SCOPED_AUTO = "workspace_write_scoped_auto"
TIER_WORKSPACE_WRITE_HUMAN_REQUIRED = "workspace_write_human_required"
TIER_EXTERNAL_OR_IRREVERSIBLE_HUMAN_REQUIRED = "external_or_irreversible_human_required"
TIER_POST_RUN_HUMAN_REQUIRED = "post_run_human_required"

DEFAULT_MAX_CHANGED_FILES = 4
FOCUSED_MULTI_FILE_MAX_CHANGED_FILES = 8

AUTO_ALLOWED_CATEGORIES = {
    "python_source",
    "tests",
    "docs",
    "local_assets",
}
POST_RUN_DENIED_CATEGORIES = {
    "auth_security",
    "build_or_ci",
    "config",
    "database_migration",
    "dependency_manifest",
    "generated_or_cache",
    "infrastructure",
    "scripts",
    "secrets_or_env",
    "unknown",
}

IMPLEMENTATION_TERMS = (
    "fix",
    "add",
    "update",
    "implement",
    "refactor",
    "test",
    "tests",
    "docs",
    "documentation",
    "comment",
    "comments",
    "example",
    "examples",
    "changelog",
    "ui",
    "layout",
    "copy",
    "component",
    "helper",
    "service",
    "model",
    "fixture",
)
BOUNDED_TERMS = (
    "focused",
    "existing",
    "this",
    "specific",
    "bounded",
    "preserve behavior",
    "without changing public behavior",
    "do not change database",
    "do not change networking",
    "do not change auth",
)
BROAD_TERMS = (
    "clean up the repo",
    "clean up repo",
    "entire repo",
    "whole repo",
    "all files",
    "fix everything",
    "improve everything",
    "refactor the repo",
    "large refactor",
    "across the codebase",
    "global refactor",
)
AMBIGUOUS_TERMS = (
    "clean up",
    "improve",
    "make better",
    "polish",
    "modernize",
    "optimize",
)
DESTRUCTIVE_TERMS = (
    "delete",
    "remove files",
    "remove the files",
    "wipe",
    "drop",
    "truncate",
    "rename",
    "move files",
    "large rename",
)
DESTRUCTIVE_COMMAND_RE = re.compile(r"\brm\s+-")
DATABASE_TERMS = (
    "migration",
    "migrations",
    "schema",
    "sql",
    "trigger",
    "triggers",
    "rls",
    "supabase",
    "backfill",
    "production data",
    "repair script",
)
DEPENDENCY_TERMS = (
    "dependency",
    "dependencies",
    "lockfile",
    "package.json",
    "package-lock",
    "pnpm-lock",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "upgrade package",
    "install package",
    "npm install",
    "pip install",
    "build toolchain",
    "build settings",
    "package resolution",
    "package manager",
    "configuration",
    "config file",
)
AUTH_SECURITY_TERMS = (
    "auth",
    "authentication",
    "authorization",
    "session",
    "sessions",
    "verification",
    "permissions",
    "role",
    "roles",
    "privacy",
    "billing",
    "payment",
    "payments",
    "account deletion",
)
SECRET_TERMS = (
    ".env",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "token",
    "tokens",
    "api key",
    "certificate",
    "certificates",
    "private key",
    "signing",
)
INFRA_TERMS = (
    "deploy",
    "deployment",
    "ci/cd",
    "github actions",
    "docker",
    "terraform",
    "cloudflare",
    "aws",
    "vercel",
    "provider config",
    "infrastructure",
)
EXTERNAL_EFFECT_TERMS = (
    "send email",
    "send message",
    "publish",
    "release",
    "git push",
    "git commit",
    "git tag",
    "external api",
    "production",
    "curl -x",
    "curl --request",
    "post request",
)
GENERATED_UNCLEAR_TERMS = (
    "generate code",
    "generated",
    "regenerate",
    "codegen",
)

FILE_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|swift|js|jsx|ts|tsx|css|scss|html|md|txt|json|yaml|yml|toml|sql|png|jpg|jpeg|svg|gif)"
)
DIR_RE = re.compile(r"\b(?:agent|tests|docs|src|app|components|views|screens|pages|ui|assets|examples)/[A-Za-z0-9_./-]*")
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"(?i)\bAWS_SECRET_ACCESS_KEY\s*=\s*['\"]?[A-Za-z0-9/+=]{32,}['\"]?"),
    re.compile(r"(?i)\b(?:private|secret|access|api)[_-]?token\s*=\s*['\"][A-Za-z0-9_./+=-]{24,}['\"]"),
)
LOW_CONFIDENCE_SECRET_LIKE_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|credential|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"
)
EXTERNAL_OR_DESTRUCTIVE_DIFF_RE = re.compile(
    r"(?i)\b(rm\s+-rf|git\s+push|git\s+commit|git\s+tag|npm\s+publish|deploy|release|curl\s+(-X|--request)\s+(POST|PUT|PATCH|DELETE)|send(email|message))\b"
)


@dataclass(frozen=True)
class ExpectedScope:
    explicit_files: list[str] = field(default_factory=list)
    allowed_dirs: list[str] = field(default_factory=list)
    allowed_categories: list[str] = field(default_factory=list)
    denied_categories: list[str] = field(default_factory=lambda: sorted(POST_RUN_DENIED_CATEGORIES))
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES
    allow_deletions: bool = False
    allow_renames: bool = False
    confidence: str = "bounded_generic"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PolicyResult:
    tier: str
    allowed: bool
    reason_code: str
    policy_version: str = POLICY_VERSION
    expected_scope: ExpectedScope | None = None
    matched_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["expected_scope"] = self.expected_scope.to_dict() if self.expected_scope else None
        return data


@dataclass(frozen=True)
class PostRunPolicyResult:
    tier: str
    allowed: bool
    reason_code: str
    policy_version: str = POLICY_VERSION
    expected_scope: ExpectedScope | None = None
    matched_rules: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)
    prohibited_files: list[str] = field(default_factory=list)
    name_status_summary: list[dict] = field(default_factory=list)
    diff_content_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["expected_scope"] = self.expected_scope.to_dict() if self.expected_scope else None
        return data


def _normalized_text(prompt: str) -> str:
    return " ".join(prompt.lower().split())


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _path_is_safe(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return ".." not in parts


def _normalize_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/").strip("`'\"")
    return cleaned.rstrip(".,;:)]}")


def _extract_named_files(prompt: str) -> tuple[list[str], bool]:
    files: list[str] = []
    invalid = False
    for match in FILE_RE.finditer(prompt):
        path = _normalize_path(match.group(0))
        if not _path_is_safe(path):
            invalid = True
            continue
        if path not in files:
            files.append(path)
    return files, invalid


def _extract_named_dirs(prompt: str) -> tuple[list[str], bool]:
    dirs: list[str] = []
    invalid = False
    for match in DIR_RE.finditer(prompt):
        path = _normalize_path(match.group(0))
        if "." in PurePosixPath(path).name:
            continue
        if not path.endswith("/"):
            path = f"{path}/"
        if not _path_is_safe(path):
            invalid = True
            continue
        if path not in dirs:
            dirs.append(path)
    return dirs, invalid


def _categories_from_prompt(text: str) -> list[str]:
    categories: set[str] = set()
    if any(term in text for term in ("test", "tests", "fixture")):
        categories.add("tests")
    if any(term in text for term in ("docs", "documentation", "readme", "changelog", "example", "comment")):
        categories.add("docs")
    if any(term in text for term in ("ui", "layout", "copy", "component", "view", "screen", "asset")):
        categories.add("local_assets")
        categories.add("python_source")
    if any(term in text for term in ("fix", "add", "update", "implement", "refactor", "helper", "service", "model")):
        categories.add("python_source")
    return sorted(categories & AUTO_ALLOWED_CATEGORIES)


def infer_expected_scope(prompt: str) -> tuple[ExpectedScope | None, str, list[str]]:
    text = _normalized_text(prompt)
    files, invalid_file = _extract_named_files(prompt)
    dirs, invalid_dir = _extract_named_dirs(prompt)
    matched_rules: list[str] = []
    if invalid_file or invalid_dir:
        return None, "workspace_write_scope_not_inferred", ["invalid_path_reference"]

    categories = _categories_from_prompt(text)
    if files:
        matched_rules.append("explicit_files")
        max_files = FOCUSED_MULTI_FILE_MAX_CHANGED_FILES if len(files) > 1 or "focused test" in text else DEFAULT_MAX_CHANGED_FILES
        allowed_categories = sorted(set(categories) | {"python_source", "tests", "docs", "local_assets"})
        return (
            ExpectedScope(
                explicit_files=files,
                allowed_dirs=dirs,
                allowed_categories=allowed_categories,
                max_changed_files=max_files,
                confidence="explicit",
            ),
            "",
            matched_rules,
        )
    if dirs:
        matched_rules.append("explicit_dirs")
        return (
            ExpectedScope(
                allowed_dirs=dirs,
                allowed_categories=categories or ["python_source", "tests", "docs", "local_assets"],
                max_changed_files=DEFAULT_MAX_CHANGED_FILES,
                confidence="explicit",
            ),
            "",
            matched_rules,
        )
    if categories and _contains_any(text, BOUNDED_TERMS):
        matched_rules.append("bounded_generic_categories")
        return (
            ExpectedScope(
                allowed_categories=categories,
                max_changed_files=DEFAULT_MAX_CHANGED_FILES,
                confidence="bounded_generic",
            ),
            "",
            matched_rules,
        )
    return None, "workspace_write_scope_not_inferred", matched_rules


def classify_workspace_write_prompt(prompt: str, sandbox: str) -> PolicyResult:
    text = _normalized_text(prompt)
    matched_rules: list[str] = []
    if sandbox != "workspace-write":
        return PolicyResult(
            tier=TIER_WORKSPACE_WRITE_HUMAN_REQUIRED,
            allowed=False,
            reason_code="workspace_write_requires_workspace_write_sandbox",
            matched_rules=["sandbox_not_workspace_write"],
        )
    if not text:
        return PolicyResult(
            tier=TIER_WORKSPACE_WRITE_HUMAN_REQUIRED,
            allowed=False,
            reason_code="workspace_write_prompt_ambiguous",
            matched_rules=["empty_prompt"],
        )
    contract = parse_prompt_contract(prompt, sandbox)
    if not contract.path_safety.valid:
        return PolicyResult(
            tier=TIER_WORKSPACE_WRITE_HUMAN_REQUIRED,
            allowed=False,
            reason_code="workspace_write_scope_not_inferred",
            matched_rules=["unsafe_path_reference"],
        )
    if DESTRUCTIVE_COMMAND_RE.search(text):
        return PolicyResult(
            tier=TIER_EXTERNAL_OR_IRREVERSIBLE_HUMAN_REQUIRED,
            allowed=False,
            reason_code="workspace_write_prompt_dangerous_command",
            matched_rules=["dangerous_shell_command"],
        )
    if "../" in prompt or "..\\" in prompt or re.search(r"(^|\s)/[A-Za-z0-9_.-]+", prompt):
        return PolicyResult(
            tier=TIER_WORKSPACE_WRITE_HUMAN_REQUIRED,
            allowed=False,
            reason_code="workspace_write_scope_not_inferred",
            matched_rules=["unsafe_path_reference"],
        )

    scope = None
    explicit_files = [item["path"] for item in contract.to_dict().get("allowed_paths", [])]
    if explicit_files:
        scope = ExpectedScope(
            explicit_files=explicit_files,
            allowed_categories=sorted(AUTO_ALLOWED_CATEGORIES),
            confidence=contract.confidence,
        )
        matched_rules.append("explicit_contract_paths")
    else:
        scope = ExpectedScope(
            allowed_categories=sorted(AUTO_ALLOWED_CATEGORIES),
            max_changed_files=10_000,
            confidence=contract.confidence,
        )
        matched_rules.append("permissive_workspace_write")

    return PolicyResult(
        tier=TIER_WORKSPACE_WRITE_SCOPED_AUTO,
        allowed=True,
        reason_code="workspace_write_scoped_auto",
        expected_scope=scope,
        matched_rules=matched_rules,
    )


def _parse_name_status_line(line: str) -> list[dict]:
    parts = line.strip().split("\t")
    if not parts or not parts[0]:
        return []
    status = parts[0]
    if status.startswith("R") and len(parts) >= 3:
        return [{"status": "R", "path": _normalize_path(parts[2]), "old_path": _normalize_path(parts[1])}]
    if status.startswith("D") and len(parts) >= 2:
        return [{"status": "D", "path": _normalize_path(parts[1])}]
    if len(parts) >= 2:
        return [{"status": status[:1], "path": _normalize_path(parts[1])}]
    return []


def parse_name_status(name_status: str) -> list[dict]:
    entries: list[dict] = []
    for line in name_status.splitlines():
        entries.extend(_parse_name_status_line(line))
    return entries


def _path_matches_scope(path: str, scope: ExpectedScope, category: str) -> bool:
    if path in scope.explicit_files:
        return True
    path_name = PurePosixPath(path).name
    for explicit in scope.explicit_files:
        if "/" not in explicit and path_name == explicit:
            return True
    if any(path.startswith(directory) for directory in scope.allowed_dirs):
        return category in scope.allowed_categories
    if not scope.explicit_files and not scope.allowed_dirs:
        return category in scope.allowed_categories
    return False


def _diff_content_flags(diff_text: str) -> list[str]:
    flags: list[str] = []
    added_lines = "\n".join(
        line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    if any(pattern.search(added_lines) for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS):
        flags.append("high_confidence_secret_literal")
    elif LOW_CONFIDENCE_SECRET_LIKE_RE.search(added_lines):
        flags.append("secret_like_content")
    if EXTERNAL_OR_DESTRUCTIVE_DIFF_RE.search(added_lines):
        flags.append("external_or_destructive_command")
    return flags


def diff_content_flags(diff_text: str) -> list[str]:
    return _diff_content_flags(diff_text)


def verify_workspace_write_post_run(
    expected_scope: ExpectedScope | dict | None,
    changed_files: list[str],
    name_status: str,
    diff_text: str,
    classification: dict | None = None,
) -> PostRunPolicyResult:
    if expected_scope is None:
        expected_scope = ExpectedScope(allowed_categories=sorted(AUTO_ALLOWED_CATEGORIES), max_changed_files=10_000)
    if isinstance(expected_scope, dict):
        scope = ExpectedScope(**expected_scope)
    else:
        scope = expected_scope

    name_status_summary = parse_name_status(name_status)
    if not name_status_summary and changed_files:
        name_status_summary = [{"status": "M", "path": _normalize_path(path)} for path in changed_files]
    if changed_files and not name_status_summary:
        return PostRunPolicyResult(
            tier=TIER_POST_RUN_HUMAN_REQUIRED,
            allowed=False,
            reason_code="post_run_diff_metadata_unavailable",
            expected_scope=scope,
            changed_files=changed_files,
        )

    paths = [_normalize_path(entry["path"]) for entry in name_status_summary]
    if not paths:
        return PostRunPolicyResult(
            tier=TIER_WORKSPACE_WRITE_SCOPED_AUTO,
            allowed=True,
            reason_code="post_run_no_attributable_changes",
            expected_scope=scope,
            name_status_summary=name_status_summary,
        )
    invalid_paths = [path for path in paths if not _path_is_safe(path)]
    if invalid_paths:
        return PostRunPolicyResult(
            tier=TIER_POST_RUN_HUMAN_REQUIRED,
            allowed=False,
            reason_code="post_run_path_escape",
            expected_scope=scope,
            changed_files=paths,
            unexpected_files=invalid_paths,
            name_status_summary=name_status_summary,
        )
    classification = classification or classify_changed_files(paths)
    files = classification.get("files") if isinstance(classification, dict) else None
    if not isinstance(files, list):
        return PostRunPolicyResult(
            tier=TIER_POST_RUN_HUMAN_REQUIRED,
            allowed=False,
            reason_code="post_run_diff_metadata_unavailable",
            expected_scope=scope,
            changed_files=paths,
            name_status_summary=name_status_summary,
        )

    categories_by_path = {str(file.get("path")): str(file.get("category")) for file in files if isinstance(file, dict)}
    prohibited_files = [
        path for path in paths
        if categories_by_path.get(path) in POST_RUN_DENIED_CATEGORIES or path.lower().endswith(".sql")
    ]
    unexpected_files = [
        path for path in paths
        if not _path_matches_scope(path, scope, categories_by_path.get(path, "unknown"))
    ]

    content_flags = _diff_content_flags(diff_text)
    if "high_confidence_secret_literal" in content_flags:
        return PostRunPolicyResult(
            tier=TIER_POST_RUN_HUMAN_REQUIRED,
            allowed=False,
            reason_code="post_run_high_confidence_secret_literal",
            expected_scope=scope,
            changed_files=paths,
            prohibited_files=prohibited_files,
            unexpected_files=unexpected_files,
            name_status_summary=name_status_summary,
            diff_content_flags=content_flags,
        )

    return PostRunPolicyResult(
        tier=TIER_WORKSPACE_WRITE_SCOPED_AUTO,
        allowed=True,
        reason_code=(
            "post_run_observations_recorded"
            if unexpected_files or prohibited_files or content_flags
            else "post_run_diff_within_expected_scope"
        ),
        expected_scope=scope,
        matched_rules=["changed_files_within_expected_scope"],
        changed_files=paths,
        unexpected_files=unexpected_files,
        prohibited_files=prohibited_files,
        name_status_summary=name_status_summary,
        diff_content_flags=content_flags,
    )
