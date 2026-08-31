from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any, Callable


ACCOUNT_RATE_LIMITS_TIMEOUT_SECONDS = 15


def fetch_account_rate_limits_resets_at(
    *,
    runner: Callable[[], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
) -> datetime | None:
    payload = (runner or _default_rate_limits_runner)()
    if not isinstance(payload, dict):
        return None
    clock = now or datetime.now(UTC)
    resets_at = exhausted_window_resets_at(payload)
    if resets_at is None or resets_at <= clock:
        return None
    return resets_at


def exhausted_window_resets_at(payload: dict[str, Any]) -> datetime | None:
    rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict):
        rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        rate_limits = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if isinstance(rate_limits, dict) and isinstance(rate_limits.get("rateLimits"), dict):
            rate_limits = rate_limits["rateLimits"]
    if not isinstance(rate_limits, dict):
        return None

    candidates: list[datetime] = []
    reached = str(rate_limits.get("rateLimitReachedType") or rate_limits.get("rate_limit_reached_type") or "")
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if used is None:
            used = window.get("used_percent")
        try:
            used_percent = int(used)
        except (TypeError, ValueError):
            used_percent = 0
        exhausted = used_percent >= 100 or bool(reached)
        if not exhausted:
            continue
        parsed = _unix_seconds_to_datetime(
            window.get("resetsAt") if "resetsAt" in window else window.get("resets_at")
        )
        if parsed is not None:
            candidates.append(parsed)
    if not candidates:
        return None
    return max(candidates)


def _unix_seconds_to_datetime(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _default_rate_limits_runner() -> dict[str, Any] | None:
    codex_path = shutil.which("codex")
    if codex_path is None:
        return None
    try:
        process = subprocess.Popen(
            [codex_path, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    requests = (
        json.dumps({"method": "initialize", "id": 1, "params": {"experimentalApi": True}})
        + "\n"
        + json.dumps({"method": "initialized", "params": {}})
        + "\n"
        + json.dumps({"method": "account/rateLimits/read", "id": 7})
        + "\n"
    )
    try:
        stdout, _stderr = process.communicate(
            requests,
            timeout=ACCOUNT_RATE_LIMITS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    except OSError:
        return None
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("id") != 7:
            continue
        result = parsed.get("result")
        if isinstance(result, dict):
            return result
    return None
