"""Which ChatGPT desktop app a run hands off to: Classic or the new app.

Step 0 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md`` (groundwork). This module:

- resolves the target from an explicit value (CLI flag), the
  ``CRAX_CHATGPT_APP`` environment variable, or the default (Classic);
- records the target on a run as a ledger event, once. A run never switches
  apps: recording a different target later is refused. Runs with no such
  event are Classic, which is what every run before this change used;
- names the loop services each target uses (:class:`ChatGPTTargetServices`).

Step 8 wires it in: the local controller and the CLI ``supervise`` command look
up each run's target and, for new-app runs only, pass the new-app services to
``run_supervision_step``. Classic runs keep using its defaults, unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

CHATGPT_TARGET_CLASSIC = "classic"
CHATGPT_TARGET_NEW = "new"
CHATGPT_TARGETS = (CHATGPT_TARGET_CLASSIC, CHATGPT_TARGET_NEW)
DEFAULT_CHATGPT_TARGET = CHATGPT_TARGET_CLASSIC
CHATGPT_TARGET_ENV_VAR = "CRAX_CHATGPT_APP"

CHATGPT_TARGET_EVENT_TYPE = "chatgpt_target_selected"

SOURCE_EXPLICIT = "explicit"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"
SOURCE_LEGACY_NO_EVENT = "legacy_no_event"

# Bundle ids, for evidence only (the modules that act on each app own their own constants).
TARGET_BUNDLE_IDS = {
    CHATGPT_TARGET_CLASSIC: "com.openai.chat",
    CHATGPT_TARGET_NEW: "com.openai.codex",
}


class ChatGPTTargetError(ValueError):
    """An invalid, contradictory or not-yet-supported ChatGPT target."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ChatGPTTargetSelection:
    target: str
    source: str

    @property
    def bundle_id(self) -> str:
        return TARGET_BUNDLE_IDS[self.target]


def normalize_chatgpt_target(value: Any) -> str:
    """Return ``"classic"`` or ``"new"``; raise ``ChatGPTTargetError`` for anything else."""

    if not isinstance(value, str) or value.strip().lower() not in CHATGPT_TARGETS:
        raise ChatGPTTargetError(
            "chatgpt_target_invalid",
            f"Unknown ChatGPT app {value!r}; expected one of {', '.join(CHATGPT_TARGETS)}.",
        )
    return value.strip().lower()


def resolve_chatgpt_target(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ChatGPTTargetSelection:
    """Pick the target: explicit value, else ``CRAX_CHATGPT_APP``, else Classic.

    An explicit or environment value that is set but invalid is an error, never
    a silent fallback to the default.
    """

    if explicit is not None:
        return ChatGPTTargetSelection(normalize_chatgpt_target(explicit), SOURCE_EXPLICIT)
    env = os.environ if environ is None else environ
    raw = env.get(CHATGPT_TARGET_ENV_VAR)
    if raw is not None and raw.strip() != "":
        return ChatGPTTargetSelection(normalize_chatgpt_target(raw), SOURCE_ENV)
    return ChatGPTTargetSelection(DEFAULT_CHATGPT_TARGET, SOURCE_DEFAULT)


# --- per-run record ---------------------------------------------------------------------


def _event_metadata(event: dict) -> dict:
    from agent.prompt_extraction import parse_metadata_json

    return parse_metadata_json(event)


def run_chatgpt_target_from_events(events: list[dict]) -> ChatGPTTargetSelection:
    """The run's recorded target. No event means Classic (every pre-existing run).

    Raises ``ChatGPTTargetError`` if the events are malformed or disagree.
    """

    recorded: list[str] = []
    for event in events:
        if event.get("event_type") != CHATGPT_TARGET_EVENT_TYPE:
            continue
        metadata = _event_metadata(event)
        try:
            recorded.append(normalize_chatgpt_target(metadata.get("chatgpt_target")))
        except ChatGPTTargetError as exc:
            raise ChatGPTTargetError("chatgpt_target_event_malformed", "A recorded ChatGPT target event is malformed.") from exc
    if not recorded:
        return ChatGPTTargetSelection(CHATGPT_TARGET_CLASSIC, SOURCE_LEGACY_NO_EVENT)
    if len(set(recorded)) > 1:
        raise ChatGPTTargetError(
            "chatgpt_target_contradictory",
            f"The run has contradictory ChatGPT target events: {sorted(set(recorded))}.",
        )
    return ChatGPTTargetSelection(recorded[0], "recorded")


def get_run_chatgpt_target(run_id: str, *, ledger: Any) -> ChatGPTTargetSelection:
    return run_chatgpt_target_from_events(ledger.list_events(run_id))


def record_run_chatgpt_target(run_id: str, selection: ChatGPTTargetSelection, *, ledger: Any) -> bool:
    """Record the run's target once. Returns True if an event was written.

    Re-recording the same target is a no-op. A different target than the one
    already recorded is refused: a run never switches apps.
    """

    events = ledger.list_events(run_id)
    has_event = any(event.get("event_type") == CHATGPT_TARGET_EVENT_TYPE for event in events)
    if has_event:
        existing = run_chatgpt_target_from_events(events)
        if existing.target != selection.target:
            raise ChatGPTTargetError(
                "chatgpt_target_change_refused",
                f"Run {run_id} already hands off to {existing.target!r}; it cannot switch to {selection.target!r}.",
            )
        return False
    ledger.add_event(
        run_id,
        CHATGPT_TARGET_EVENT_TYPE,
        f"ChatGPT handoff target: {selection.target}.",
        {
            "chatgpt_target": selection.target,
            "chatgpt_target_source": selection.source,
            "chatgpt_bundle_id": selection.bundle_id,
        },
    )
    return True


# --- services --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatGPTTargetServices:
    """The four ChatGPT-facing services ``run_supervision_step`` accepts.

    Signatures are those of the Classic defaults in ``agent/supervision_services.py``.
    """

    target: str
    submit_service: Callable[..., Any]
    capture_service: Callable[..., Any]
    destination_adapter_factory: Callable[[], Any]
    destination_navigation_service: Callable[..., Any]

    def as_step_kwargs(self) -> dict[str, Callable[..., Any]]:
        return {
            "submit_service": self.submit_service,
            "capture_service": self.capture_service,
            "destination_adapter_factory": self.destination_adapter_factory,
            "destination_navigation_service": self.destination_navigation_service,
        }


def classic_target_services() -> ChatGPTTargetServices:
    """Exactly the services the loop uses today (the Classic defaults)."""

    from agent import supervision_services as services

    return ChatGPTTargetServices(
        target=CHATGPT_TARGET_CLASSIC,
        submit_service=services._default_submit_service,
        capture_service=services._default_capture_service,
        destination_adapter_factory=services._default_destination_adapter_factory,
        destination_navigation_service=services._default_destination_navigation_service,
    )


def services_for_target(target: str) -> ChatGPTTargetServices:
    """Services for a target: today's Classic defaults, or the new-app services (Step 8)."""

    normalized = normalize_chatgpt_target(target)
    if normalized == CHATGPT_TARGET_CLASSIC:
        return classic_target_services()
    from agent.new_chatgpt_loop import new_target_services

    return new_target_services()


CHATGPT_HANDOFF_EVENT_PREFIXES = ("gpt_feedback_", "gpt_response_", "chatgpt_destination_", "chatgpt_navigation_")


def ensure_run_chatgpt_target(run_id: str, selection: ChatGPTTargetSelection, *, ledger: Any) -> ChatGPTTargetSelection:
    """Make the run's target agree with ``selection``, or refuse. Returns the run's target.

    - A recorded target must match ``selection``.
    - A run with no record is Classic. Selecting Classic writes nothing, so
      Classic runs' ledgers look exactly as they always have.
    - Selecting the new app for an unrecorded run records it, unless the run
      has already handed off to ChatGPT, which means it already used Classic.
      That is refused: a run never switches apps.
    """

    events = ledger.list_events(run_id)
    current = run_chatgpt_target_from_events(events)
    if current.source != SOURCE_LEGACY_NO_EVENT:
        if current.target != selection.target:
            raise ChatGPTTargetError(
                "chatgpt_target_change_refused",
                f"Run {run_id} hands off to {current.target!r}; it cannot switch to {selection.target!r}.",
            )
        return current
    if selection.target == CHATGPT_TARGET_CLASSIC:
        return current
    if any(str(event.get("event_type") or "").startswith(CHATGPT_HANDOFF_EVENT_PREFIXES) for event in events):
        raise ChatGPTTargetError(
            "chatgpt_target_change_refused",
            f"Run {run_id} already handed off to Classic ChatGPT; it cannot switch to the new app.",
        )
    record_run_chatgpt_target(run_id, selection, ledger=ledger)
    return run_chatgpt_target_from_events(ledger.list_events(run_id))
