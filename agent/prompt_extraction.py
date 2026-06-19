from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BEGIN_SENTINEL = "BEGIN_NEXT_CODEX_PROMPT"
END_SENTINEL = "END_NEXT_CODEX_PROMPT"
SAFETY_STATUS_REQUIRES_REVIEW = "requires_human_review"

_CODEX_PROMPT_INTRODUCER_RE = re.compile(
    r"^\s*(codex prompt|next codex prompt|prompt for codex|use this codex prompt)\s*:\s*$",
    re.IGNORECASE,
)
_FENCED_BLOCK_RE = re.compile(r"(?m)^```[^\n]*\n(?P<body>.*?)(?:\n)?^```\s*$", re.DOTALL)


@dataclass(frozen=True)
class CapturedResponseSelection:
    ok: bool
    error: str | None
    warnings: tuple[str, ...]
    source_event: dict | None
    source_metadata: dict
    submitted_event: dict | None
    response_text: str
    response_sha256: str


@dataclass(frozen=True)
class PromptExtractionResult:
    ok: bool
    error: str | None
    warnings: tuple[str, ...]
    extraction_method: str | None
    prompt_text: str
    prompt_length: int
    prompt_sha256: str
    prompt_count_detected: int
    selected_prompt_index: int | None
    safety_status: str


@dataclass(frozen=True)
class ExtractedCodexPromptSelection:
    ok: bool
    error: str | None
    warnings: tuple[str, ...]
    event: dict | None
    metadata: dict
    prompt_text: str
    prompt_sha256: str
    prompt_length: int
    prompt_path: str | None
    source_event_id: Any
    matched_submission_event_id: Any


def parse_metadata_json(event: dict) -> dict:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        return metadata

    metadata_json = event.get("metadata_json")
    if not metadata_json:
        return {}

    try:
        decoded = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}

    return decoded if isinstance(decoded, dict) else {}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _latest_successful_submission(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "gpt_feedback_submitted":
            continue
        metadata = parse_metadata_json(event)
        submit_result = metadata.get("submit_result")
        if isinstance(submit_result, dict):
            if submit_result.get("submitted") is True:
                return event
            continue
        return event
    return None


def _same_event_id(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return left == right


def _validate_captured_response(event: dict) -> tuple[dict | None, str | None]:
    metadata = parse_metadata_json(event)
    response_text = metadata.get("response_text")
    response_sha256 = metadata.get("response_sha256")

    if not isinstance(response_text, str) or not response_text.strip():
        return None, "captured response_text is missing or empty"
    if not isinstance(response_sha256, str) or not response_sha256.strip():
        return None, "captured response_sha256 is missing"
    if sha256_text(response_text) != response_sha256:
        return None, "captured response SHA mismatch"

    return metadata, None


def find_latest_valid_captured_response(events: list[dict]) -> CapturedResponseSelection:
    submitted_event = _latest_successful_submission(events)
    if submitted_event is None:
        return CapturedResponseSelection(
            ok=False,
            error="No successful gpt_feedback_submitted event was found for this run.",
            warnings=(),
            source_event=None,
            source_metadata={},
            submitted_event=None,
            response_text="",
            response_sha256="",
        )

    submitted_event_id = submitted_event.get("id")
    matching_captures = []
    warnings: list[str] = []
    for event in events:
        if event.get("event_type") != "gpt_response_captured":
            continue
        metadata = parse_metadata_json(event)
        if _same_event_id(metadata.get("matched_submission_event_id"), submitted_event_id):
            matching_captures.append(event)

    if not matching_captures:
        return CapturedResponseSelection(
            ok=False,
            error=(
                "No gpt_response_captured event matched the latest successful "
                "gpt_feedback_submitted event."
            ),
            warnings=(),
            source_event=None,
            source_metadata={},
            submitted_event=submitted_event,
            response_text="",
            response_sha256="",
        )

    for event in reversed(matching_captures):
        metadata, error = _validate_captured_response(event)
        if metadata is None:
            warnings.append(f"Skipped capture event {event.get('id')}: {error}.")
            continue
        response_text = metadata["response_text"]
        response_sha256 = metadata["response_sha256"]
        return CapturedResponseSelection(
            ok=True,
            error=None,
            warnings=tuple(warnings),
            source_event=event,
            source_metadata=metadata,
            submitted_event=submitted_event,
            response_text=response_text,
            response_sha256=response_sha256,
        )

    return CapturedResponseSelection(
        ok=False,
        error="No valid gpt_response_captured event matched the latest successful submission.",
        warnings=tuple(warnings),
        source_event=None,
        source_metadata={},
        submitted_event=submitted_event,
        response_text="",
        response_sha256="",
    )


def _trim_outer_blank_lines(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _sentinel_result(text: str) -> PromptExtractionResult | None:
    begin_matches = list(re.finditer(re.escape(BEGIN_SENTINEL), text))
    end_matches = list(re.finditer(re.escape(END_SENTINEL), text))

    if not begin_matches and not end_matches:
        return None

    if len(begin_matches) != len(end_matches):
        return PromptExtractionResult(
            ok=False,
            error="Malformed sentinel markers.",
            warnings=(),
            extraction_method="sentinel_block",
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=min(len(begin_matches), len(end_matches)),
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    if len(begin_matches) > 1:
        return PromptExtractionResult(
            ok=False,
            error="Multiple sentinel prompt blocks were found.",
            warnings=(),
            extraction_method="sentinel_block",
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=len(begin_matches),
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    begin = begin_matches[0]
    end = end_matches[0]
    if begin.end() > end.start():
        return PromptExtractionResult(
            ok=False,
            error="Malformed sentinel markers.",
            warnings=(),
            extraction_method="sentinel_block",
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=1,
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    prompt_text = _trim_outer_blank_lines(text[begin.end() : end.start()])
    if not prompt_text.strip():
        return PromptExtractionResult(
            ok=False,
            error="Extracted sentinel prompt is empty.",
            warnings=(),
            extraction_method="sentinel_block",
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=1,
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    return PromptExtractionResult(
        ok=True,
        error=None,
        warnings=(),
        extraction_method="sentinel_block",
        prompt_text=prompt_text,
        prompt_length=len(prompt_text),
        prompt_sha256=sha256_text(prompt_text),
        prompt_count_detected=1,
        selected_prompt_index=0,
        safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
    )


def _nearest_previous_nonblank_line(text: str, position: int) -> str:
    previous_text = text[:position]
    for line in reversed(previous_text.splitlines()):
        if line.strip():
            return line
    return ""


def _labeled_fenced_result(text: str) -> PromptExtractionResult:
    accepted: list[str] = []
    total_fences = 0
    for match in _FENCED_BLOCK_RE.finditer(text):
        total_fences += 1
        introducer = _nearest_previous_nonblank_line(text, match.start())
        if not _CODEX_PROMPT_INTRODUCER_RE.match(introducer):
            continue
        accepted.append(_trim_outer_blank_lines(match.group("body")))

    if len(accepted) > 1:
        return PromptExtractionResult(
            ok=False,
            error="Multiple labeled fenced Codex prompt blocks were found.",
            warnings=(),
            extraction_method="labeled_fenced_code_block",
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=len(accepted),
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    if not accepted:
        warning = "Only unlabeled fenced code blocks were found." if total_fences else ""
        return PromptExtractionResult(
            ok=False,
            error="No next Codex prompt candidate was found.",
            warnings=(warning,) if warning else (),
            extraction_method=None,
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=0,
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    prompt_text = accepted[0]
    if not prompt_text.strip():
        return PromptExtractionResult(
            ok=False,
            error="Extracted fenced Codex prompt is empty.",
            warnings=(),
            extraction_method="labeled_fenced_code_block",
            prompt_text="",
            prompt_length=0,
            prompt_sha256="",
            prompt_count_detected=1,
            selected_prompt_index=None,
            safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
        )

    return PromptExtractionResult(
        ok=True,
        error=None,
        warnings=(),
        extraction_method="labeled_fenced_code_block",
        prompt_text=prompt_text,
        prompt_length=len(prompt_text),
        prompt_sha256=sha256_text(prompt_text),
        prompt_count_detected=1,
        selected_prompt_index=0,
        safety_status=SAFETY_STATUS_REQUIRES_REVIEW,
    )


def extract_next_codex_prompt_from_text(text: str) -> PromptExtractionResult:
    sentinel_result = _sentinel_result(text)
    if sentinel_result is not None:
        return sentinel_result
    return _labeled_fenced_result(text)


def _metadata_text(metadata: dict, key: str) -> tuple[str | None, str | None]:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        return None, f"extraction metadata {key} is missing or empty"
    return value, None


def _metadata_required_value(metadata: dict, key: str) -> tuple[Any, str | None]:
    value = metadata.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, f"extraction metadata {key} is missing"
    return value, None


def _metadata_length(metadata: dict) -> tuple[int | None, str | None]:
    value = metadata.get("prompt_length")
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "extraction metadata prompt_length is missing or invalid"
    if value <= 0:
        return None, "extraction metadata prompt_length must be positive"
    return value, None


def _latest_extraction_event(events: list[dict]) -> dict | None:
    candidates = [
        event
        for event in events
        if event.get("event_type") == "next_codex_prompt_extracted"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda event: int(event.get("id") or -1))


def select_latest_valid_extracted_codex_prompt(
    events: list[dict],
    expect_prompt_sha256: str | None = None,
) -> ExtractedCodexPromptSelection:
    event = _latest_extraction_event(events)
    if event is None:
        return ExtractedCodexPromptSelection(
            ok=False,
            error="No next_codex_prompt_extracted event was found for this run.",
            warnings=(),
            event=None,
            metadata={},
            prompt_text="",
            prompt_sha256="",
            prompt_length=0,
            prompt_path=None,
            source_event_id=None,
            matched_submission_event_id=None,
        )

    metadata = parse_metadata_json(event)
    prompt_text, error = _metadata_text(metadata, "prompt_text")
    if error:
        return _invalid_extracted_prompt(event, metadata, error)

    prompt_sha256, error = _metadata_text(metadata, "prompt_sha256")
    if error:
        return _invalid_extracted_prompt(event, metadata, error)

    prompt_length, error = _metadata_length(metadata)
    if error:
        return _invalid_extracted_prompt(event, metadata, error)

    source_event_id, error = _metadata_required_value(metadata, "source_event_id")
    if error:
        return _invalid_extracted_prompt(event, metadata, error)

    matched_submission_event_id, error = _metadata_required_value(
        metadata,
        "matched_submission_event_id",
    )
    if error:
        return _invalid_extracted_prompt(event, metadata, error)

    safety_status = metadata.get("safety_status")
    if safety_status != SAFETY_STATUS_REQUIRES_REVIEW:
        return _invalid_extracted_prompt(
            event,
            metadata,
            "extraction safety_status must be requires_human_review",
        )

    if len(prompt_text) != prompt_length:
        return _invalid_extracted_prompt(
            event,
            metadata,
            "extraction prompt_length does not match prompt_text length",
        )

    computed_sha256 = sha256_text(prompt_text)
    if computed_sha256 != prompt_sha256:
        return _invalid_extracted_prompt(
            event,
            metadata,
            "extraction prompt_text SHA does not match prompt_sha256",
        )

    if expect_prompt_sha256 is not None and expect_prompt_sha256 != prompt_sha256:
        return _invalid_extracted_prompt(
            event,
            metadata,
            "expected prompt SHA does not match selected extracted prompt",
        )

    freshness = find_latest_valid_captured_response(events)
    if not freshness.ok:
        return _invalid_extracted_prompt(
            event,
            metadata,
            f"could not verify extraction freshness: {freshness.error}",
            warnings=freshness.warnings,
        )

    if not _same_event_id(freshness.submitted_event.get("id"), matched_submission_event_id):
        return _invalid_extracted_prompt(
            event,
            metadata,
            "extracted prompt is stale: matched submission is not the latest successful submission",
            warnings=freshness.warnings,
        )

    if not _same_event_id(freshness.source_event.get("id"), source_event_id):
        return _invalid_extracted_prompt(
            event,
            metadata,
            "extracted prompt is stale: source capture is not the latest valid captured response",
            warnings=freshness.warnings,
        )

    source_response_sha256 = metadata.get("source_response_sha256")
    if (
        isinstance(source_response_sha256, str)
        and source_response_sha256.strip()
        and source_response_sha256 != freshness.response_sha256
    ):
        return _invalid_extracted_prompt(
            event,
            metadata,
            "extracted prompt source response SHA does not match latest valid capture",
            warnings=freshness.warnings,
        )

    prompt_path = metadata.get("prompt_path")
    prompt_path_text = prompt_path if isinstance(prompt_path, str) and prompt_path.strip() else None
    warnings = list(freshness.warnings)
    if prompt_path_text is not None:
        path = Path(prompt_path_text).expanduser()
        if path.exists():
            if not path.is_file():
                return _invalid_extracted_prompt(
                    event,
                    metadata,
                    "extracted prompt path exists but is not a file",
                    warnings=tuple(warnings),
                )
            try:
                file_text = path.read_text(encoding="utf-8")
            except OSError as exc:
                return _invalid_extracted_prompt(
                    event,
                    metadata,
                    f"failed to read extracted prompt artifact: {exc}",
                    warnings=tuple(warnings),
                )
            if sha256_text(file_text) != prompt_sha256:
                return _invalid_extracted_prompt(
                    event,
                    metadata,
                    "extracted prompt artifact SHA does not match prompt_sha256",
                    warnings=tuple(warnings),
                )
        else:
            warnings.append(
                "extracted prompt artifact is missing; using validated prompt_text metadata."
            )

    return ExtractedCodexPromptSelection(
        ok=True,
        error=None,
        warnings=tuple(warnings),
        event=event,
        metadata=metadata,
        prompt_text=prompt_text,
        prompt_sha256=prompt_sha256,
        prompt_length=prompt_length,
        prompt_path=prompt_path_text,
        source_event_id=source_event_id,
        matched_submission_event_id=matched_submission_event_id,
    )


def _invalid_extracted_prompt(
    event: dict,
    metadata: dict,
    error: str,
    warnings: tuple[str, ...] = (),
) -> ExtractedCodexPromptSelection:
    prompt_text = metadata.get("prompt_text")
    prompt_sha256 = metadata.get("prompt_sha256")
    prompt_length = metadata.get("prompt_length")
    prompt_path = metadata.get("prompt_path")
    return ExtractedCodexPromptSelection(
        ok=False,
        error=error,
        warnings=warnings,
        event=event,
        metadata=metadata,
        prompt_text=prompt_text if isinstance(prompt_text, str) else "",
        prompt_sha256=prompt_sha256 if isinstance(prompt_sha256, str) else "",
        prompt_length=prompt_length if isinstance(prompt_length, int) else 0,
        prompt_path=prompt_path if isinstance(prompt_path, str) and prompt_path.strip() else None,
        source_event_id=metadata.get("source_event_id"),
        matched_submission_event_id=metadata.get("matched_submission_event_id"),
    )
