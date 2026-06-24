from __future__ import annotations

from pathlib import PurePosixPath


CATEGORIES = (
    "docs",
    "python_source",
    "tests",
    "auth_security",
    "config",
    "database_migration",
    "secrets_or_env",
    "dependency_manifest",
    "build_or_ci",
    "infrastructure",
    "local_assets",
    "scripts",
    "generated_or_cache",
    "unknown",
)

RISK_LEVELS = ("low", "medium", "high")

DEPENDENCY_MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

CONFIG_EXTENSIONS = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"}
DOC_EXTENSIONS = {".md", ".txt"}
PRIVATE_KEY_EXTENSIONS = {".pem", ".key"}
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
SCRIPT_EXTENSIONS = {".sh", ".bash", ".zsh", ".ps1"}
AUTH_SECURITY_MARKERS = {
    "auth",
    "authentication",
    "authorization",
    "session",
    "sessions",
    "verification",
    "permissions",
    "privacy",
    "billing",
    "payments",
}
INFRASTRUCTURE_MARKERS = {
    "terraform",
    "cloudflare",
    "aws",
    "vercel",
    "infrastructure",
    "deploy",
    "deployment",
}


def _normalize_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def _path_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _filename(path: str) -> str:
    return PurePosixPath(path).name


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _has_part(path: str, *parts: str) -> bool:
    path_parts = {part.lower() for part in _path_parts(path)}
    return any(part in path_parts for part in parts)


def _classify_path(path: str) -> tuple[str, str, str]:
    normalized = _normalize_path(path)
    lower_path = normalized.lower()
    lower_name = _filename(normalized).lower()
    suffix = _suffix(normalized)

    if (
        lower_name == ".env"
        or lower_name.startswith(".env.")
        or "secret" in lower_path
        or "credential" in lower_path
        or "token" in lower_path
        or "certificate" in lower_path
        or suffix in PRIVATE_KEY_EXTENSIONS
    ):
        return "secrets_or_env", "high", "Secrets, env, credentials, or private key path."

    if "migration" in lower_path or suffix == ".sql":
        return "database_migration", "high", "Database migration or SQL path."

    if any(marker in {part.lower() for part in _path_parts(normalized)} for marker in AUTH_SECURITY_MARKERS):
        return "auth_security", "high", "Authentication, authorization, session, privacy, or billing path."

    if any(marker in lower_path for marker in INFRASTRUCTURE_MARKERS):
        return "infrastructure", "high", "Infrastructure, provider, deployment, or cloud configuration path."

    if (
        _has_part(normalized, "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache")
        or lower_name.endswith(".pyc")
        or lower_name == ".ds_store"
        or lower_path.startswith("build/")
        or lower_path.startswith("dist/")
        or "/build/" in lower_path
        or "/dist/" in lower_path
    ):
        return "generated_or_cache", "low", "Generated artifact, cache file, or build/dist output."

    if (
        "/tests/" in f"/{lower_path}/"
        or lower_path.startswith("tests/")
        or lower_name.startswith("test_")
        or lower_name.endswith("_test.py")
    ):
        return "tests", "low", "Test path or test filename."

    if lower_name in DEPENDENCY_MANIFESTS:
        return "dependency_manifest", "medium", "Dependency manifest or lockfile."

    if (
        lower_path.startswith(".github/")
        or lower_name == "dockerfile"
        or lower_name == "docker-compose.yml"
        or lower_name == "makefile"
    ):
        return "build_or_ci", "medium", "Build, Docker, Makefile, or CI configuration."

    if suffix in SCRIPT_EXTENSIONS or _has_part(normalized, "scripts", "bin"):
        return "scripts", "medium", "Script path or executable script extension."

    if (
        suffix in DOC_EXTENSIONS
        or lower_name.startswith("readme")
        or lower_path.startswith("docs/")
    ):
        return "docs", "low", "Documentation path or extension."

    if suffix == ".py":
        return "python_source", "medium", "Python source file."

    if suffix in ASSET_EXTENSIONS or _has_part(normalized, "assets", "images", "public"):
        return "local_assets", "low", "Local UI asset path or extension."

    if suffix in CONFIG_EXTENSIONS:
        return "config", "medium", "Configuration file extension."

    return "unknown", "medium", "No deterministic classifier rule matched."


def classify_changed_files(paths: list[str]) -> dict:
    files = []
    counts_by_category = dict.fromkeys(CATEGORIES, 0)
    counts_by_risk_level = dict.fromkeys(RISK_LEVELS, 0)
    high_risk_files = []

    for path in paths:
        normalized = _normalize_path(path)
        category, risk_level, reason = _classify_path(normalized)
        files.append(
            {
                "path": normalized,
                "category": category,
                "risk_level": risk_level,
                "reason": reason,
            }
        )
        counts_by_category[category] += 1
        counts_by_risk_level[risk_level] += 1
        if risk_level == "high":
            high_risk_files.append(normalized)

    return {
        "total_files": len(files),
        "files": files,
        "counts_by_category": counts_by_category,
        "counts_by_risk_level": counts_by_risk_level,
        "high_risk_files": high_risk_files,
    }
