"""Send a message in the new ChatGPT desktop app (``com.openai.codex``).

Step 5 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``. Standalone; not wired into
the supervision loop yet.

Replaces Classic's clipboard + Cmd+V + Enter with Accessibility only:
no clipboard and no keystrokes.

1. Preflight with ``bring_to_front=True`` (the AX tree is stale off screen).
2. Find the single composer (``AXTextArea "Ask ChatGPT"``). An empty composer
   reads ``""`` or ``"Ask ChatGPT\\n"`` (the placeholder leaks into the value).
   A draft is overwritten, as Classic does, and recorded (length only).
3. Set the composer's ``AXValue`` to the message, read it back, retry once.
4. Press the enabled ``AXButton "Send"`` next to the composer.
5. Confirm: a new "You said:" turn contains the marker (or the whole message)
   **and** the composer emptied or "Stop" appeared.
6. If the press had no visible effect (no new turn, text still in the composer)
   **and** ``AXEnhancedUserInterface`` reads false (the one known cause of a
   silently ignored press), re-run preflight and press exactly once more.
   Otherwise never press again, only keep waiting: a slow app must not cause a
   double send. There is no Enter-key fallback.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent import new_chatgpt_labels as labels
from agent.new_chatgpt_app import (
    COMPOSER_DOM_CLASS,
    ENHANCED_UI_ATTRIBUTE,
    AXBackend,
    CtypesAXBackend,
    PreflightResult,
    preflight_new_chatgpt,
)
from agent.new_chatgpt_reader import (
    USER_ROLE,
    AXNodeData,
    locate_anchors,
    snapshot_region,
    split_turns,
)

SUBMIT_METHOD = "ax_set_value_press_send_new_chatgpt"
SET_VALUE_METHOD = "ax_set_value_new_chatgpt"
PRESS_SEND_METHOD = "ax_press_send_new_chatgpt"

OUTCOME_SENT = "sent"
OUTCOME_FAILED = "failed"

REASON_EMPTY_MESSAGE = "new_chatgpt_submit_empty_message"
REASON_COMPOSER_NOT_FOUND = "new_chatgpt_submit_composer_not_found"
REASON_SET_VALUE_FAILED = "new_chatgpt_submit_set_value_failed"
REASON_VALUE_MISMATCH = "composer_value_mismatch"
REASON_SEND_NOT_FOUND = "new_chatgpt_submit_send_button_not_found"
REASON_SEND_NOT_ENABLED = "new_chatgpt_submit_send_button_disabled"
REASON_PRESS_FAILED = "new_chatgpt_submit_press_failed"
REASON_NOT_REGISTERED = "send_not_registered"
REASON_NOT_CONFIRMED = "new_chatgpt_submit_not_confirmed"

DEFAULT_SEND_ENABLE_TIMEOUT_SECONDS = 2.0
DEFAULT_PRESS_EFFECT_TIMEOUT_SECONDS = 3.0
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 15.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
_CONTROL_SEARCH_ANCESTORS = 4


@dataclass
class SubmitResult:
    ok: bool
    outcome: str = OUTCOME_FAILED
    reason_code: str | None = None
    error: str | None = None
    method: str = SUBMIT_METHOD
    set_value_method: str = SET_VALUE_METHOD
    send_method: str = PRESS_SEND_METHOD
    preflight: dict | None = None
    message_length: int = 0
    draft_overwritten: bool = False
    draft_length: int = 0
    set_value_attempts: int = 0
    set_value_errors: list[int] = field(default_factory=list)
    value_readback_matches: bool = False
    send_press_count: int = 0
    send_press_errors: list[int] = field(default_factory=list)
    retry_reason: str | None = None
    user_turns_before: int = 0
    user_turns_after: int = 0
    marker_found_in_new_turn: bool = False
    stop_seen: bool = False
    composer_cleared: bool = False
    confirmed: bool = False
    confirm_wait_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _ChatState:
    composer: AXNodeData | None
    composer_count: int
    send: AXNodeData | None
    stop: AXNodeData | None
    control_count: int
    user_turn_texts: tuple[str, ...]


def composer_is_empty(value: str, label: str = labels.COMPOSER.text) -> bool:
    """The composer is empty when its value is blank or just the leaked placeholder."""

    return value.strip() in ("", label)


def submit_message_new_chatgpt(
    message: str,
    *,
    marker_text: str | None = None,
    backend: AXBackend | None = None,
    preflight: Callable[..., PreflightResult] = preflight_new_chatgpt,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    send_enable_timeout_seconds: float = DEFAULT_SEND_ENABLE_TIMEOUT_SECONDS,
    press_effect_timeout_seconds: float = DEFAULT_PRESS_EFFECT_TIMEOUT_SECONDS,
    confirm_timeout_seconds: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> SubmitResult:
    """Put ``message`` in the composer, press Send, and confirm it was sent.

    ``marker_text`` is what must appear in the new user turn (the loop's
    submission marker). It defaults to the whole message.
    """

    result = SubmitResult(ok=False)
    if not isinstance(message, str) or not message.strip():
        return _fail(result, REASON_EMPTY_MESSAGE, "The message is empty.")
    result.message_length = len(message)
    marker = marker_text if marker_text else message

    try:
        if backend is None:
            backend = CtypesAXBackend()
        pre = preflight(backend=backend, sleep=sleep, clock=clock, bring_to_front=True)
        result.preflight = pre.to_dict()
        if not pre.ok:
            return _fail(result, pre.reason_code or "new_chatgpt_preflight_failed", pre.error or "Preflight failed.")
        app = backend.application(pre.pid)

        state = _read_state(backend, app)
        if state.composer is None:
            return _fail(result, REASON_COMPOSER_NOT_FOUND, f"Expected one composer, found {state.composer_count}.")
        result.user_turns_before = len(state.user_turn_texts)
        # Count turns already containing the marker, so a new one is detected even
        # if the virtualized turn list drops older turns meanwhile.
        marker_turns_before = _marker_turn_count(state, marker)
        before_value = state.composer.text_value
        if not composer_is_empty(before_value):
            result.draft_overwritten = True
            result.draft_length = len(before_value)

        # 3. set the value and read it back (one retry)
        for _ in range(2):
            result.set_value_attempts += 1
            error = backend.set_string(state.composer.element, "AXValue", message)
            result.set_value_errors.append(error)
            state = _read_state(backend, app)
            if state.composer is not None and _value_matches(state.composer.text_value, message):
                result.value_readback_matches = True
                break
        if not result.value_readback_matches:
            if result.set_value_errors and all(code != 0 for code in result.set_value_errors):
                return _fail(result, REASON_SET_VALUE_FAILED, f"Setting the composer value failed: AXError {result.set_value_errors}.")
            return _fail(result, REASON_VALUE_MISMATCH, "The composer did not read back the exact message.")

        # 4. wait for Send to enable, then press it
        state = _wait_for(lambda s: s.send is not None and s.send.enabled is True, backend, app, sleep, clock, send_enable_timeout_seconds, poll_interval_seconds)
        if state.send is None:
            return _fail(result, REASON_SEND_NOT_FOUND, f"Expected one Send button next to the composer, found {state.control_count} controls.")
        if state.send.enabled is not True:
            return _fail(result, REASON_SEND_NOT_ENABLED, "The Send button stayed disabled after the message was set.")

        started = clock()
        sent = lambda s: _sent(s, marker_turns_before, marker)  # noqa: E731
        for attempt in range(2):
            error = backend.perform(state.send.element, "AXPress")
            result.send_press_count += 1
            result.send_press_errors.append(error)
            if error != 0:
                return _fail(result, REASON_PRESS_FAILED, f"AXPress on Send returned AXError {error}.")

            state = _wait_for(sent, backend, app, sleep, clock, press_effect_timeout_seconds, poll_interval_seconds)
            _record(result, state, marker, marker_turns_before)
            if sent(state):
                break
            press_had_no_effect = (
                _marker_turn_count(state, marker) == marker_turns_before
                and state.composer is not None
                and _value_matches(state.composer.text_value, message)
                and state.send is not None
                and state.send.enabled is True
            )
            if attempt == 0 and press_had_no_effect and backend.attribute(app, ENHANCED_UI_ATTRIBUTE) is not True:
                # 7. Nothing happened because AXEnhancedUserInterface was reset, so
                # the press was ignored for certain: re-enable and press once more.
                result.retry_reason = "enhanced_ui_was_off"
                pre = preflight(backend=backend, sleep=sleep, clock=clock, bring_to_front=True)
                if not pre.ok:
                    return _fail(result, pre.reason_code or "new_chatgpt_preflight_failed", pre.error or "Preflight failed.")
                state = _read_state(backend, app)
                if state.send is None or state.send.enabled is not True:
                    return _fail(result, REASON_SEND_NOT_ENABLED, "Send was not available for the retry press.")
                continue
            # The press did something (or this was the retry): wait out the rest
            # of the confirmation window. Never press again.
            remaining = max(confirm_timeout_seconds - (clock() - started), 0.0)
            state = _wait_for(sent, backend, app, sleep, clock, remaining, poll_interval_seconds)
            _record(result, state, marker, marker_turns_before)
            break
        result.confirm_wait_seconds = round(max(clock() - started, 0.0), 3)

        if result.marker_found_in_new_turn and (result.composer_cleared or result.stop_seen):
            result.ok, result.outcome, result.confirmed = True, OUTCOME_SENT, True
            result.reason_code = result.error = None
            return result
        if not result.marker_found_in_new_turn:
            return _fail(result, REASON_NOT_REGISTERED, "Send was pressed but no new message with the marker appeared.")
        return _fail(result, REASON_NOT_CONFIRMED, "A new message appeared but the send could not be fully confirmed.")
    except Exception as exc:  # noqa: BLE001 - fail closed with evidence
        return _fail(result, "new_chatgpt_ax_error", f"{type(exc).__name__}: {exc}")


def _value_matches(value: str, message: str) -> bool:
    """Exact match, ignoring only trailing newlines.

    The composer (ProseMirror) normalizes the end of the text: live on
    2026-11-20 it dropped the loop's final "\n" (a real run then failed the
    read-back 27 times). Everything else, including inner blank lines and
    spacing, must match exactly.
    """

    return value.rstrip("\n") == message.rstrip("\n")


def _marker_turn_count(state: _ChatState, marker: str) -> int:
    return sum(1 for text in state.user_turn_texts if marker in text)


def _sent(state: _ChatState, marker_turns_before: int, marker: str) -> bool:
    if _marker_turn_count(state, marker) <= marker_turns_before:
        return False
    composer_cleared = state.composer is not None and composer_is_empty(state.composer.text_value)
    return composer_cleared or state.stop is not None


def _record(result: SubmitResult, state: _ChatState, marker: str, marker_turns_before: int) -> None:
    result.user_turns_after = len(state.user_turn_texts)
    result.marker_found_in_new_turn = result.marker_found_in_new_turn or _marker_turn_count(state, marker) > marker_turns_before
    result.stop_seen = result.stop_seen or state.stop is not None
    if state.composer is not None and composer_is_empty(state.composer.text_value):
        result.composer_cleared = True


def _wait_for(
    predicate: Callable[[_ChatState], bool],
    backend: AXBackend,
    app: Any,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> _ChatState:
    started = clock()
    while True:
        state = _read_state(backend, app)
        if predicate(state) or clock() - started >= timeout_seconds:
            return state
        sleep(poll_interval_seconds)


def _read_state(backend: AXBackend, app: Any) -> _ChatState:
    anchors = locate_anchors(backend, app)
    panel = snapshot_region(backend, anchors.chat_panel)
    if panel.root is None or not panel.complete:
        return _ChatState(None, 0, None, None, 0, ())

    composers = [
        node
        for node in panel.root.iter()
        if node.role == "AXTextArea" and COMPOSER_DOM_CLASS in node.dom_classes and node.has_label(labels.COMPOSER.text)
    ]
    composer = composers[0] if len(composers) == 1 else None
    send = stop = None
    control_count = 0
    if composer is not None:
        controls = composer_controls(panel.root, composer)
        control_count = len(controls)
        sends = [node for node in controls if node.has_label(labels.SEND.text)]
        stops = [node for node in controls if node.has_label(labels.STOP.text)]
        send = sends[0] if len(sends) == 1 else None
        stop = stops[0] if len(stops) == 1 else None

    user_texts: list[str] = []
    turn_list = panel.root.find_first(
        lambda node: any(
            child.role == "AXHeading" and child.has_label(*labels.TURN_HEADINGS) for child in node.children
        )
    )
    if turn_list is not None:
        for turn in split_turns(turn_list):
            if turn.role == USER_ROLE:
                parts = turn.static_texts()
                # The user's message is one AXStaticText with newlines kept; join
                # defensively in case a build splits it per line.
                user_texts.append("\n".join(parts) if len(parts) > 1 else "".join(parts))
    return _ChatState(composer, len(composers), send, stop, control_count, tuple(user_texts))


def composer_controls(panel: AXNodeData, composer: AXNodeData) -> list[AXNodeData]:
    """Send/Stop buttons in the nearest composer ancestor that has any."""

    parents: dict[int, AXNodeData] = {}
    for node in panel.iter():
        for child in node.children:
            parents[id(child)] = node
    node = parents.get(id(composer))
    for _ in range(_CONTROL_SEARCH_ANCESTORS):
        if node is None:
            break
        controls = [
            item
            for item in node.iter()
            if item.role == "AXButton" and item.has_label(labels.SEND.text, labels.STOP.text)
        ]
        if controls:
            return controls
        node = parents.get(id(node))
    return []


def _fail(result: SubmitResult, reason_code: str, error: str) -> SubmitResult:
    result.ok = False
    result.outcome = OUTCOME_FAILED
    result.reason_code = reason_code
    result.error = error
    return result


if __name__ == "__main__":  # pragma: no cover - live send, run only when asked
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Send one message in the currently open new-ChatGPT chat.")
    parser.add_argument("--message", required=True)
    parser.add_argument("--marker", default=None, help="Text that must appear in the sent turn (default: the message).")
    args = parser.parse_args()
    outcome = submit_message_new_chatgpt(args.message, marker_text=args.marker).to_dict()
    outcome.pop("preflight", None)
    print(json.dumps(outcome, indent=2))
