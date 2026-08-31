from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any

from agent.ledger import CODEX_PROGRESS_ALLOWED_KINDS, CODEX_PROGRESS_EVENT_TYPE

CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE = "codex_quota_wait_scheduled"
CODEX_QUOTA_WAIT_CANCELLED_EVENT_TYPE = "codex_quota_wait_cancelled"
CODEX_QUOTA_RESUME_STARTED_EVENT_TYPE = "codex_quota_resume_started"
CODEX_QUOTA_RESUME_FINISHED_EVENT_TYPE = "codex_quota_resume_finished"
CODEX_QUOTA_RESUME_DELAY_SECONDS = 60
CODEX_QUOTA_WAIT_LIMIT = 3
CODEX_QUOTA_RESUME_PROMPT = (
    "The previous turn ended because the Codex usage limit reset. "
    "Continue from the last incomplete work. Do not restart from scratch."
)

_USAGE_LIMIT_MARKERS = (
    "you've hit your usage limit",
    "hit your usage limit",
    "usagelimitexceeded",
)
_TRY_AGAIN_AT_RE = re.compile(
    r"try again at\s+(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuotaWaitDecision:
    scheduled: bool
    reason_code: str
    thread_id: str | None = None
    resets_at: str | None = None
    resume_at: str | None = None
    error_text: str | None = None
    invocation_id: str | None = None
    source: str | None = None


def decide_quota_wait(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    rate_limits_resets_at: datetime | None = None,
) -> QuotaWaitDecision:
    """Return a wait plan only when usage-limit, thread id, and reset time are all present.

    Callers that do not schedule on ``scheduled is True`` keep current
    Codex-failure behavior.
    """
    clock = _aware_now(now)
    if quota_wait_count(events) >= CODEX_QUOTA_WAIT_LIMIT:
        return QuotaWaitDecision(
            scheduled=False,
            reason_code="quota_wait_limit_reached",
        )
    progress = [_progress_payload(event) for event in events]
    progress = [item for item in progress if item is not None]
    error_text, invocation_id = _latest_usage_limit_error(progress)
    if error_text is None:
        return QuotaWaitDecision(
            scheduled=False,
            reason_code="not_usage_limit",
        )
    thread_id = _session_id_for_invocation(progress, invocation_id)
    if thread_id is None:
        return QuotaWaitDecision(
            scheduled=False,
            reason_code="usage_limit_without_thread_id",
            error_text=error_text,
            invocation_id=invocation_id,
        )
    resets_at, source = _choose_resets_at(
        error_text,
        clock=clock,
        rate_limits_resets_at=rate_limits_resets_at,
    )
    if resets_at is None:
        return QuotaWaitDecision(
            scheduled=False,
            reason_code="usage_limit_without_reset_time",
            thread_id=thread_id,
            error_text=error_text,
            invocation_id=invocation_id,
        )
    resume_at = resets_at + timedelta(seconds=CODEX_QUOTA_RESUME_DELAY_SECONDS)
    return QuotaWaitDecision(
        scheduled=True,
        reason_code="quota_wait_scheduled",
        thread_id=thread_id,
        resets_at=resets_at.isoformat(),
        resume_at=resume_at.isoformat(),
        error_text=error_text,
        invocation_id=invocation_id,
        source=source,
    )


def quota_wait_count(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for event in events
        if event.get("event_type") == CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE
    )


def quota_wait_fields(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    metadata = _metadata(event)
    thread_id = str(metadata.get("thread_id") or "").strip()
    resume_at = str(metadata.get("resume_at") or "").strip()
    if not thread_id or not resume_at:
        return None
    return {
        "thread_id": thread_id,
        "resume_at": resume_at,
        "resets_at": str(metadata.get("resets_at") or "").strip() or None,
        "source": str(metadata.get("source") or "").strip() or None,
        "invocation_id": str(metadata.get("invocation_id") or "").strip() or None,
        "repository_path": str(metadata.get("repository_path") or "").strip() or None,
        "sandbox": str(metadata.get("sandbox") or "").strip() or None,
        "status": "waiting",
    }


def active_quota_wait(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    scheduled: dict[str, Any] | None = None
    for event in events:
        event_type = event.get("event_type")
        if event_type == CODEX_QUOTA_WAIT_SCHEDULED_EVENT_TYPE:
            scheduled = event
            continue
        if event_type in {
            CODEX_QUOTA_WAIT_CANCELLED_EVENT_TYPE,
            CODEX_QUOTA_RESUME_FINISHED_EVENT_TYPE,
        }:
            scheduled = None
    return scheduled


def _choose_resets_at(
    error_text: str,
    *,
    clock: datetime,
    rate_limits_resets_at: datetime | None,
) -> tuple[datetime | None, str | None]:
    if rate_limits_resets_at is not None:
        rpc_time = _aware_now(rate_limits_resets_at)
        if rpc_time > clock:
            return rpc_time, "rate_limits_rpc"
    parsed = parse_try_again_at(error_text, now=clock)
    if parsed is not None:
        return parsed, "error_text"
    return None, None


def parse_try_again_at(error_text: str, *, now: datetime) -> datetime | None:
    match = _TRY_AGAIN_AT_RE.search(error_text or "")
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = re.sub(r"[.\s]", "", match.group(3)).lower()
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if hour == 12:
        hour = 0
    if meridiem.startswith("p"):
        hour += 12
    local_now = _aware_now(now).astimezone()
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        return None
    return candidate.astimezone(UTC)


def _latest_usage_limit_error(
    progress: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    for payload in reversed(progress):
        if str(payload.get("kind") or "") != "error":
            continue
        error_text = _progress_error_text(payload)
        if not _looks_like_usage_limit(error_text):
            return None, None
        invocation_id = str(payload.get("codex_invocation_id") or "").strip() or None
        return error_text, invocation_id
    return None, None


def _session_id_for_invocation(
    progress: list[dict[str, Any]],
    invocation_id: str | None,
) -> str | None:
    for payload in reversed(progress):
        if invocation_id and str(payload.get("codex_invocation_id") or "") != invocation_id:
            continue
        session_id = (
            ((payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {})
            .get("value_summary", {})
        )
        if isinstance(session_id, dict):
            thread_id = str(session_id.get("codex_session_id") or "").strip()
            if thread_id:
                return thread_id
    return None


def _progress_error_text(payload: dict[str, Any]) -> str:
    nested = payload.get("metadata")
    if isinstance(nested, dict):
        summary = nested.get("value_summary")
        if isinstance(summary, dict):
            error = summary.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
    for key in ("summary", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def looks_like_usage_limit(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _USAGE_LIMIT_MARKERS)


def _looks_like_usage_limit(text: str) -> bool:
    return looks_like_usage_limit(text)


def _progress_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event_type") == CODEX_PROGRESS_EVENT_TYPE:
        metadata = _metadata(event)
        return metadata or None
    if (
        event.get("schema_version") == 1
        and event.get("kind") in CODEX_PROGRESS_ALLOWED_KINDS
        and "codex_invocation_id" in event
    ):
        return event
    metadata = _metadata(event)
    if (
        metadata.get("schema_version") == 1
        and metadata.get("kind") in CODEX_PROGRESS_ALLOWED_KINDS
        and "codex_invocation_id" in metadata
    ):
        return metadata
    return None


def _metadata(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("metadata")
    if isinstance(value, dict):
        return value
    raw = event.get("metadata_json")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _aware_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
