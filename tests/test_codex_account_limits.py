from __future__ import annotations

from datetime import datetime, timezone
import unittest

from agent.codex_account_limits import (
    exhausted_window_resets_at,
    fetch_account_rate_limits_resets_at,
)


PRIMARY_RESET = 1_788_000_000
SECONDARY_RESET = 1_788_100_000


class CodexAccountLimitsTests(unittest.TestCase):
    def test_exhausted_window_uses_max_resets_at(self) -> None:
        payload = {
            "rateLimits": {
                "primary": {"usedPercent": 100, "resetsAt": PRIMARY_RESET},
                "secondary": {"usedPercent": 100, "resetsAt": SECONDARY_RESET},
            }
        }
        self.assertEqual(
            exhausted_window_resets_at(payload),
            datetime.fromtimestamp(SECONDARY_RESET, tz=timezone.utc),
        )

    def test_non_exhausted_window_is_ignored(self) -> None:
        payload = {
            "rateLimits": {
                "primary": {"usedPercent": 100, "resetsAt": PRIMARY_RESET},
                "secondary": {"usedPercent": 40, "resetsAt": SECONDARY_RESET},
            }
        }
        self.assertEqual(
            exhausted_window_resets_at(payload),
            datetime.fromtimestamp(PRIMARY_RESET, tz=timezone.utc),
        )

    def test_rate_limit_reached_type_counts_windows_even_below_100(self) -> None:
        payload = {
            "rateLimits": {
                "rateLimitReachedType": "primary",
                "primary": {"usedPercent": 99, "resetsAt": PRIMARY_RESET},
            }
        }
        self.assertEqual(
            exhausted_window_resets_at(payload),
            datetime.fromtimestamp(PRIMARY_RESET, tz=timezone.utc),
        )

    def test_fetch_uses_injected_runner_and_rejects_past_reset(self) -> None:
        now = datetime.fromtimestamp(PRIMARY_RESET + 10, tz=timezone.utc)
        result = fetch_account_rate_limits_resets_at(
            runner=lambda: {
                "rateLimits": {
                    "primary": {"usedPercent": 100, "resetsAt": PRIMARY_RESET},
                }
            },
            now=now,
        )
        self.assertIsNone(result)

    def test_fetch_returns_future_reset_from_injected_runner(self) -> None:
        now = datetime.fromtimestamp(PRIMARY_RESET - 60, tz=timezone.utc)
        result = fetch_account_rate_limits_resets_at(
            runner=lambda: {
                "rateLimits": {
                    "primary": {"usedPercent": 100, "resetsAt": PRIMARY_RESET},
                }
            },
            now=now,
        )
        self.assertEqual(result, datetime.fromtimestamp(PRIMARY_RESET, tz=timezone.utc))

    def test_runner_failure_returns_none(self) -> None:
        self.assertIsNone(fetch_account_rate_limits_resets_at(runner=lambda: None))
