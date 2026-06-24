from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath


CONTRACT_VERSION = "prompt_contract_v1"

READ_ONLY_PATTERNS = (
    re.compile(r"\bread[- ]only task\b", re.IGNORECASE),
    re.compile(r"\bdo not modify files\b", re.IGNORECASE),
    re.compile(r"\bdo not make any file changes\b", re.IGNORECASE),
    re.compile(r"\bno file changes\b", re.IGNORECASE),
    re.compile(r"\bdo not write files\b", re.IGNORECASE),
    re.compile(
        r"\bdo not make code, database, configuration, or generated-file changes\b",
        re.IGNORECASE,
    ),
)

FILE_RE = re.compile(
    r"(?<![\w./-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|swift|js|jsx|ts|tsx|css|scss|html|md|txt|json|yaml|yml|toml|sql|png|jpg|jpeg|svg|gif)"
)

ONLY_PATH_PATTERNS = (
    re.compile(r"\bonly\s+(?:edit|change|modify)\s+(?P<path>\S+\.[A-Za-z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(?:edit|change|modify)\s+only\s+(?P<path>\S+\.[A-Za-z0-9]+)\b", re.IGNORECASE),
)

RELATED_TESTS_PATTERN = re.compile(
    r"\bmodify\s+(?P<path>\S+\.[A-Za-z0-9]+)\s+and\s+related\s+focused\s+tests\s+only\b",
    re.IGNORECASE,
)

EXCLUDED_AREA_PATTERNS = (
    ("backend", re.compile(r"\bdo not modify backend\b|\bno backend changes\b|\bdo not change backend\b", re.IGNORECASE)),
    ("database", re.compile(r"\bno database changes\b|\bdo not change database\b|\bdo not modify database\b", re.IGNORECASE)),
    ("networking", re.compile(r"\bdo not change networking\b|\bno networking changes\b|\bdo not modify networking\b", re.IGNORECASE)),
    ("configuration", re.compile(r"\bdo not change configuration\b|\bno configuration changes\b|\bdo not modify configuration\b", re.IGNORECASE)),
    ("auth", re.compile(r"\bdo not change auth\b|\bno auth changes\b|\bdo not modify auth\b|\bno authentication changes\b", re.IGNORECASE)),
    ("infrastructure", re.compile(r"\bdo not change infrastructure\b|\bno infrastructure changes\b|\bdo not modify infrastructure\b|\bno deployment changes\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class ReadOnlyContract:
    explicit: bool = False
    matched_phrases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SandboxContract:
    selected: str
    source: str = "caller"


@dataclass(frozen=True)
class AllowedPath:
    path: str
    mode: str


@dataclass(frozen=True)
class AllowedPathGroup:
    kind: str
    anchor: str


@dataclass(frozen=True)
class ExcludedArea:
    area: str
    matched_text: str


@dataclass(frozen=True)
class PathSafety:
    valid: bool
    invalid_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromptContract:
    contract_version: str
    sandbox: SandboxContract
    read_only: ReadOnlyContract
    allowed_paths: list[AllowedPath] = field(default_factory=list)
    allowed_path_groups: list[AllowedPathGroup] = field(default_factory=list)
    excluded_areas: list[ExcludedArea] = field(default_factory=list)
    path_safety: PathSafety = field(default_factory=lambda: PathSafety(valid=True))
    confidence: str = "low"

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/").strip("`'\"")
    return cleaned.rstrip(".,;:)]}")


def _path_is_safe(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    parts = PurePosixPath(path).parts
    return ".." not in parts


def _match_read_only(prompt: str) -> ReadOnlyContract:
    matches: list[str] = []
    for pattern in READ_ONLY_PATTERNS:
        for match in pattern.finditer(prompt):
            text = match.group(0).strip()
            if text and text not in matches:
                matches.append(text)
    return ReadOnlyContract(explicit=bool(matches), matched_phrases=matches)


def _add_path(paths: list[AllowedPath], path: str, mode: str) -> None:
    if not any(item.path == path and item.mode == mode for item in paths):
        paths.append(AllowedPath(path=path, mode=mode))


def _extract_allowed_paths(prompt: str) -> tuple[list[AllowedPath], list[AllowedPathGroup], list[str]]:
    allowed_paths: list[AllowedPath] = []
    groups: list[AllowedPathGroup] = []
    invalid_paths: list[str] = []

    for pattern in ONLY_PATH_PATTERNS:
        for match in pattern.finditer(prompt):
            path = _normalize_path(match.group("path"))
            if not _path_is_safe(path):
                invalid_paths.append(path)
                continue
            _add_path(allowed_paths, path, "only")

    for match in RELATED_TESTS_PATTERN.finditer(prompt):
        path = _normalize_path(match.group("path"))
        if not _path_is_safe(path):
            invalid_paths.append(path)
            continue
        _add_path(allowed_paths, path, "only")
        if not any(group.kind == "related_focused_tests" and group.anchor == path for group in groups):
            groups.append(AllowedPathGroup(kind="related_focused_tests", anchor=path))

    return allowed_paths, groups, invalid_paths


def _extract_excluded_areas(prompt: str) -> list[ExcludedArea]:
    areas: list[ExcludedArea] = []
    for area, pattern in EXCLUDED_AREA_PATTERNS:
        for match in pattern.finditer(prompt):
            matched_text = match.group(0).strip()
            if not any(item.area == area and item.matched_text == matched_text for item in areas):
                areas.append(ExcludedArea(area=area, matched_text=matched_text))
    return areas


def parse_prompt_contract(prompt: str, sandbox: str) -> PromptContract:
    read_only = _match_read_only(prompt)
    allowed_paths, allowed_path_groups, invalid_paths = _extract_allowed_paths(prompt)
    excluded_areas = _extract_excluded_areas(prompt)
    explicit_parts = bool(read_only.explicit or allowed_paths or allowed_path_groups or excluded_areas)
    confidence = "high" if explicit_parts and not invalid_paths else "low"
    if invalid_paths:
        confidence = "low"

    return PromptContract(
        contract_version=CONTRACT_VERSION,
        sandbox=SandboxContract(selected=sandbox),
        read_only=read_only,
        allowed_paths=allowed_paths,
        allowed_path_groups=allowed_path_groups,
        excluded_areas=excluded_areas,
        path_safety=PathSafety(valid=not invalid_paths, invalid_paths=invalid_paths),
        confidence=confidence,
    )
