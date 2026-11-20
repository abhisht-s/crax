"""Wire the new ChatGPT desktop app (``com.openai.codex``) into the supervision loop.

Step 8 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``.

Design: **reuse the Classic services, swap only the UI operations.**
``submit_feedback_to_chatgpt_service`` and ``capture_chatgpt_response_service``
already take every on-screen action as an injected function. This module
supplies new-app versions of those functions, so everything else stays the
Classic code path, unchanged:

- feedback building and the submission marker;
- every ledger event type and its metadata;
- approval modes, cancel handling and verification rules;
- the planner's view of the run.

Classic step -> new-app operation:

=====================  ====================================================
clipboard copy         no clipboard: the message is staged in memory
activate               preflight with ``bring_to_front=True`` (Step 1)
inspect                reader (Step 2), returned in the Classic dict format
paste                  set the composer ``AXValue`` and read it back (Step 5)
press Send             ``AXPress`` Send; one retry only if
                       ``AXEnhancedUserInterface`` was found off (Step 5)
press Enter            never: always reports "not supported"
capture                ``capture_reply_new_chatgpt`` (Step 6), returned in
                       the Classic result format and reason codes
destination adapter    ``NewChatGPTDestinationSnapshotAdapter`` (Step 4)
navigation             ``open_project_chat`` (Step 3)
=====================  ====================================================

The Classic Enter fallback after an AXPress only fires for Classic's
``macos_accessibility_axpress_send_button`` method, so it can never trigger here.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from agent.chatgpt_ax_capture import (
    COMPLETED_WITHOUT_PROMPT_REASON_CODE,
    OPERATOR_CANCELLED_REASON_CODE,
    SENTINEL_STATE_COMPLETE_STABLE,
    SENTINEL_STATE_MALFORMED_STABLE,
)
from agent.new_chatgpt_app import (
    ENHANCED_UI_ATTRIBUTE,
    NEW_CHATGPT_BUNDLE_ID,
    AXBackend,
    CtypesAXBackend,
    PreflightResult,
    preflight_new_chatgpt,
)
from agent.new_chatgpt_capture import (
    OUTCOME_COMPLETE_WITHOUT_PROMPT,
    REASON_ANCHOR_AMBIGUOUS,
    REASON_CANCELLED,
    REASON_COMPLETED_WITHOUT_PROMPT,
    REASON_PROMPT_MALFORMED,
    CaptureResult,
    capture_reply_new_chatgpt,
)
from agent.new_chatgpt_submit import (
    PRESS_SEND_METHOD,
    SET_VALUE_METHOD,
    _read_state,
    _value_matches,
    composer_is_empty,
)

INSPECT_METHOD = "ax_inspect_new_chatgpt"
STAGE_METHOD = "new_chatgpt_no_clipboard_staged"
ENTER_NOT_SUPPORTED_METHOD = "new_chatgpt_enter_not_supported"
CAPTURE_SOURCE = "new_chatgpt_desktop_ax"
CAPTURE_FORMAT = "new_chatgpt_rendered_ax_text"
COMPOSER_PATH = "new_chatgpt:composer"
SEND_PATH = "new_chatgpt:send"
USER_TURN_PATH = "new_chatgpt:user_turn:{index}"

DEFAULT_SEND_ENABLE_TIMEOUT_SECONDS = 2.0
DEFAULT_PRESS_EFFECT_TIMEOUT_SECONDS = 3.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_CAPTURE_POLL_INTERVAL_SECONDS = 1.0


class NewChatGPTUIOperations:
    """New-app implementations of the UI functions the Classic services accept.

    One instance per service call: it holds the staged message between the
    "copy" and "paste" steps (there is no clipboard).
    """

    def __init__(
        self,
        *,
        backend: AXBackend | None = None,
        preflight: Callable[..., PreflightResult] = preflight_new_chatgpt,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        send_enable_timeout_seconds: float = DEFAULT_SEND_ENABLE_TIMEOUT_SECONDS,
        press_effect_timeout_seconds: float = DEFAULT_PRESS_EFFECT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        capture_poll_interval_seconds: float = DEFAULT_CAPTURE_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._backend = backend
        self._preflight = preflight
        self._sleep = sleep
        self._clock = clock
        self._send_enable_timeout = send_enable_timeout_seconds
        self._press_effect_timeout = press_effect_timeout_seconds
        self._poll = poll_interval_seconds
        self._capture_poll = capture_poll_interval_seconds
        self._app: Any = None
        self._staged: str | None = None

    # -- plumbing ------------------------------------------------------------------
    def _get_backend(self) -> AXBackend:
        if self._backend is None:
            self._backend = CtypesAXBackend()
        return self._backend

    def _run_preflight(self) -> PreflightResult:
        backend = self._get_backend()
        pre = self._preflight(backend=backend, sleep=self._sleep, clock=self._clock, bring_to_front=True)
        if pre.ok:
            self._app = backend.application(pre.pid)
        return pre

    def submit_service_kwargs(self) -> dict[str, Callable[..., Any]]:
        """Keyword arguments for ``submit_feedback_to_chatgpt_service``."""

        return {
            "clipboard_copy_function": self.stage_message,
            "activation_function": self.activate,
            "submission_ui_inspection_function": self.inspect,
            "paste_function": self.paste,
            "ax_send_button_function": self.press_send,
            "enter_function": self.enter_not_supported,
        }

    # -- submit operations ---------------------------------------------------------
    def stage_message(self, text: str) -> dict:
        self._staged = text
        return {"copied": True, "method": STAGE_METHOD, "error": None, "clipboard_used": False}

    def activate(self, app_name: str) -> dict:
        pre = self._run_preflight()
        frontmost = bool(pre.ok and pre.frontmost)
        return {
            "activated": bool(pre.ok),
            "app_name": app_name,
            "bundle_id": NEW_CHATGPT_BUNDLE_ID,
            "frontmost_app": "ChatGPT" if frontmost else None,
            "frontmost_bundle_id": NEW_CHATGPT_BUNDLE_ID if frontmost else None,
            "is_frontmost": frontmost,
            "method": pre.method,
            "error": None if frontmost else (pre.error or pre.reason_code or "The new ChatGPT app is not ready."),
            "preflight_reason_code": pre.reason_code,
            "product_mode": pre.product_mode,
            "enhanced_ui_enabled": pre.enhanced_ui_enabled,
        }

    def inspect(self, app_name: str, marker_text: str | None = None, **_unused: Any) -> dict:
        result: dict[str, Any] = {
            "ok": False,
            "method": INSPECT_METHOD,
            "app_name": app_name,
            "error": None,
            "focused_element": None,
            "focused_composer": None,
            "text_input_candidates": [],
            "button_candidates": [],
            "send_button": None,
            "message_candidates": [],
            "marker_text_present_in_composer": False,
            "marker_text_candidate_count": 0,
            "ax_stats": {},
        }
        if self._app is None:
            result["error"] = "The new ChatGPT app was not activated before inspection."
            return result
        state = _read_state(self._get_backend(), self._app)
        result["ax_stats"] = {"composer_candidate_count": state.composer_count, "user_turn_count": len(state.user_turn_texts)}
        if state.composer is None:
            result["error"] = f"Expected one composer, found {state.composer_count}."
            return result

        value = state.composer.text_value
        text = "" if composer_is_empty(value) else value
        composer = {
            "path": COMPOSER_PATH,
            "role": "AXTextArea",
            "subrole": "",
            "title": state.composer.title,
            "description": state.composer.description,
            "identifier": "",
            "enabled": True,
            "focused": state.composer.focused,
            "actions": ["AXPress"],
            "text": text,
            "value": text,
        }
        controls = []
        send = None
        for node, path in ((state.send, SEND_PATH), (state.stop, "new_chatgpt:stop")):
            if node is None:
                continue
            control = {
                "path": path,
                "role": "AXButton",
                "subrole": "",
                "title": node.label,
                "description": node.label,
                "identifier": "",
                "enabled": node.enabled,
                "focused": node.focused,
                "actions": ["AXPress"],
            }
            controls.append(control)
            if node is state.send and node.enabled is True:
                send = control
        messages = [
            {
                "index": index,
                "path": USER_TURN_PATH.format(index=index),
                "role": "AXStaticText",
                "subrole": "",
                "text": turn_text,
                "text_length": len(turn_text),
                "focused": False,
            }
            for index, turn_text in enumerate(state.user_turn_texts)
        ]
        result.update(
            {
                "ok": True,
                "focused_element": composer,
                "focused_composer": composer,
                "text_input_candidates": [composer],
                "button_candidates": controls,
                "send_button": send,
                "message_candidates": messages,
                "marker_text_present_in_composer": bool(marker_text and marker_text in text),
                "marker_text_candidate_count": sum(1 for item in messages if marker_text and marker_text in item["text"]),
            }
        )
        return result

    def paste(self) -> dict:
        result: dict[str, Any] = {
            "pasted": False,
            "method": SET_VALUE_METHOD,
            "error": None,
            "readback_matches": False,
            "set_value_errors": [],
            "draft_overwritten": False,
            "draft_length": 0,
            "send_enabled": False,
            "clipboard_used": False,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        if self._staged is None or self._app is None:
            result["error"] = "Nothing staged, or the app was not activated."
            return result
        backend = self._get_backend()
        state = _read_state(backend, self._app)
        if state.composer is None:
            result["error"] = f"Expected one composer, found {state.composer_count}."
            return result
        if not composer_is_empty(state.composer.text_value):
            result["draft_overwritten"] = True
            result["draft_length"] = len(state.composer.text_value)
        for _ in range(2):
            result["set_value_errors"].append(backend.set_string(state.composer.element, "AXValue", self._staged))
            state = _read_state(backend, self._app)
            if state.composer is not None and _value_matches(state.composer.text_value, self._staged):
                result["readback_matches"] = True
                break
        if not result["readback_matches"]:
            result["error"] = "composer_value_mismatch: the composer did not read back the exact message."
            return result
        # Send enables a moment after the value lands; wait so send selection sees it.
        started = self._clock()
        while not (state.send is not None and state.send.enabled is True) and self._clock() - started < self._send_enable_timeout:
            self._sleep(self._poll)
            state = _read_state(backend, self._app)
        result["send_enabled"] = bool(state.send is not None and state.send.enabled is True)
        result["pasted"] = True
        return result

    def press_send(self, app_name: str, button_path: str) -> dict:
        result: dict[str, Any] = {
            "pressed": False,
            "method": PRESS_SEND_METHOD,
            "path": button_path,
            "error": None,
            "press_count": 0,
            "press_errors": [],
            "retry_reason": None,
            "effect_seen": False,
            "exit_code": None,
        }
        if button_path != SEND_PATH or self._app is None:
            result["error"] = "Unknown button or app not activated."
            return result
        backend = self._get_backend()
        for attempt in range(2):
            state = _read_state(backend, self._app)
            if state.send is None or state.send.enabled is not True:
                result["error"] = "The Send button is not available."
                return result
            error = backend.perform(state.send.element, "AXPress")
            result["press_count"] += 1
            result["press_errors"].append(error)
            if error != 0:
                result["error"] = f"AXPress on Send returned AXError {error}."
                return result
            result["pressed"] = True
            state = self._wait_for_press_effect(backend)
            if self._press_had_effect(state):
                result["effect_seen"] = True
                return result
            still_staged = state.composer is not None and self._staged is not None and _value_matches(state.composer.text_value, self._staged)
            enhanced_ui_off = backend.attribute(self._app, ENHANCED_UI_ATTRIBUTE) is not True
            if attempt == 0 and still_staged and enhanced_ui_off:
                # The one known cause of a silently ignored press. Re-enable and press once more.
                result["retry_reason"] = "enhanced_ui_was_off"
                if not self._run_preflight().ok:
                    result["error"] = "Preflight failed before the retry press."
                    return result
                continue
            # Never press again otherwise: the submission verification decides.
            return result
        return result

    def _wait_for_press_effect(self, backend: AXBackend):
        started = self._clock()
        while True:
            state = _read_state(backend, self._app)
            if self._press_had_effect(state) or self._clock() - started >= self._press_effect_timeout:
                return state
            self._sleep(self._poll)

    @staticmethod
    def _press_had_effect(state) -> bool:
        composer_cleared = state.composer is not None and composer_is_empty(state.composer.text_value)
        return composer_cleared or state.stop is not None

    def enter_not_supported(self) -> dict:
        return {
            "submitted": False,
            "method": ENTER_NOT_SUPPORTED_METHOD,
            "error": "The new ChatGPT app is driven by Accessibility only; Enter is never sent.",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    # -- capture -------------------------------------------------------------------
    def capture(
        self,
        feedback_text: str,
        *,
        app_name: str = "ChatGPT",
        timeout_seconds: float | None = None,
        stable_seconds: float = 0.0,
        require_sentinel_response: bool = False,
        submission_marker_text: str | None = None,
        should_stop: Callable[[], bool] | None = None,
        **_unused: Any,
    ) -> dict:
        """``capture_response_after_feedback`` replacement with the same result shape."""

        del feedback_text, app_name, timeout_seconds  # Classic capture has no deadline either.
        stable_polls = max(2, math.ceil(max(stable_seconds, 0.0) / self._capture_poll) + 1)
        captured = capture_reply_new_chatgpt(
            submission_marker_text,
            require_sentinel=require_sentinel_response,
            backend=self._get_backend(),
            preflight=self._preflight,
            sleep=self._sleep,
            clock=self._clock,
            timeout_seconds=None,
            poll_interval_seconds=self._capture_poll,
            stable_polls=stable_polls,
            should_stop=should_stop,
        )
        return classic_capture_result(
            captured,
            require_sentinel=require_sentinel_response,
            stable_seconds=stable_seconds,
            poll_interval_seconds=self._capture_poll,
            marker_used=bool(submission_marker_text),
        )


def classic_capture_result(
    captured: CaptureResult,
    *,
    require_sentinel: bool,
    stable_seconds: float,
    poll_interval_seconds: float,
    marker_used: bool,
) -> dict:
    """Translate a new-app capture into ``capture_response_after_feedback``'s result dict."""

    base = {
        "source": CAPTURE_SOURCE,
        "capture_format": CAPTURE_FORMAT,
        "matched_feedback": captured.last_state.get("anchor_count") == 1,
        "matched_submission_marker": marker_used and captured.last_state.get("anchor_count") == 1,
        "matched_candidate_index": None,
        "matched_candidate_path": "new_chatgpt:marker_user_turn",
        "response_candidate_index": None,
        "response_candidate_path": "new_chatgpt:assistant_turn_after_marker",
        "candidate_count": 1 if captured.last_state.get("reply_present") else 0,
        "stable_seconds": stable_seconds,
        "successful_polls": captured.polls,
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_seconds": None,
        "match_score": 1.0,
        "ax_stats": {"new_chatgpt_capture": captured.last_state, "method": captured.method},
        "sentinel_required": require_sentinel,
        "post_feedback_candidate_summaries": [],
        "format_warning": (
            "Captured text is rendered macOS Accessibility text from the new ChatGPT app; "
            "code blocks are exact, other Markdown formatting may be lossy."
        ),
    }
    text_fields = {
        "response_text": captured.reply_text,
        "response_length": captured.reply_length,
        "response_sha256": captured.reply_sha256,
    }
    if captured.ok:
        return {
            **base,
            **text_fields,
            "ok": True,
            "stable": True,
            "sentinel_state": SENTINEL_STATE_COMPLETE_STABLE if captured.prompt_text else "sentinel_pending",
            "reason_code": "complete_sentinel_stable" if require_sentinel else "response_stable",
        }
    if captured.reason_code == REASON_COMPLETED_WITHOUT_PROMPT and captured.outcome == OUTCOME_COMPLETE_WITHOUT_PROMPT:
        return {
            **base,
            **text_fields,
            "ok": False,
            "stable": True,
            "provisional": False,
            "error": "ChatGPT finished without a usable prompt. This session needs attention; other sessions can continue.",
            "reason_code": COMPLETED_WITHOUT_PROMPT_REASON_CODE,
            "sentinel_state": "sentinel_pending",
            "completion_evidence": {"source": CAPTURE_SOURCE, "response_text": captured.reply_text},
        }
    reason = {
        REASON_PROMPT_MALFORMED: "sentinel_malformed_stable",
        REASON_CANCELLED: OPERATOR_CANCELLED_REASON_CODE,
        REASON_ANCHOR_AMBIGUOUS: "multiple_submission_marker_candidates",
    }.get(captured.reason_code or "", captured.reason_code or "fatal_capture_error")
    failure = {**base, "ok": False, "stable": False, "error": captured.error, "reason_code": reason}
    if reason == "sentinel_malformed_stable":
        failure["sentinel_state"] = SENTINEL_STATE_MALFORMED_STABLE
    return failure


# --- loop services (same signatures as the Classic defaults in supervision_services) ------


def new_submit_service(
    run_id: str,
    run: dict,
    app_name: str,
    *,
    approval_mode: str,
    ledger: Any,
    should_stop: Callable[[], bool] | None = None,
    operations: NewChatGPTUIOperations | None = None,
) -> Any:
    from agent.chatgpt_services import submit_feedback_to_chatgpt_service

    ops = operations or NewChatGPTUIOperations()
    return submit_feedback_to_chatgpt_service(
        run_id,
        run,
        app_name=app_name,
        approval_mode=approval_mode,
        ledger=ledger,
        should_stop=should_stop,
        **ops.submit_service_kwargs(),
    )


def new_capture_service(
    run_id: str,
    run: dict,
    app_name: str,
    timeout_seconds: float | None,
    stable_seconds: float,
    *,
    require_sentinel_response: bool,
    ledger: Any,
    should_stop: Callable[[], bool] | None = None,
    operations: NewChatGPTUIOperations | None = None,
) -> Any:
    from agent.chatgpt_services import capture_chatgpt_response_service

    del run, timeout_seconds  # same as the Classic default
    ops = operations or NewChatGPTUIOperations()
    return capture_chatgpt_response_service(
        run_id,
        app_name=app_name,
        timeout_seconds=None,
        stable_seconds=stable_seconds,
        require_sentinel_response=require_sentinel_response,
        ledger=ledger,
        should_stop=should_stop,
        activation_function=ops.activate,
        capture_function=ops.capture,
    )


def new_destination_adapter_factory() -> Any:
    from agent.new_chatgpt_destination import NewChatGPTDestinationSnapshotAdapter

    return NewChatGPTDestinationSnapshotAdapter()


def new_destination_navigation_service(
    run_id: str,
    binding: Any,
    lease_context: Any,
    *,
    app_name: str,
    ledger: Any,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """Open the bound chat; the read-only destination gate still decides correctness."""

    from agent.new_chatgpt_navigation import (
        OUTCOME_ALREADY_OPEN,
        OUTCOME_OPENED,
        REASON_NOT_CONFIRMED,
        open_project_chat,
    )
    from agent.supervision_services import _NAVIGATION_ACTION_DIAGNOSTIC_DEFAULTS, NavigationAttemptResult

    del run_id, lease_context, ledger, app_name
    result = (opener or open_project_chat)(binding.project_title, binding.chat_title)
    pressed = "chat" in result.presses
    diagnostics = dict(_NAVIGATION_ACTION_DIAGNOSTIC_DEFAULTS)
    diagnostics.update(
        {
            "target_detected": result.chat_row_count == 1,
            "target_candidate_count": result.chat_row_count,
            "actionable_element_resolved": result.chat_row_count == 1,
            "chosen_method": "AXPress" if pressed else "",
            "axpress_attempted": pressed,
            "axpress_result": ("ok" if result.reason_code != "new_chatgpt_nav_press_failed" else "failed") if pressed else "not_attempted",
            "ui_changed_after_action": result.confirmed,
            "destination_confirmed": result.confirmed,
            "final_reresolution_status": "confirmed" if result.confirmed else "unconfirmed",
            "project_open_outcome": result.outcome,
            "project_open_target_match_count": result.project_row_count,
        }
    )
    if result.ok and result.outcome == OUTCOME_ALREADY_OPEN:
        return NavigationAttemptResult(
            ok=True, outcome="new_chatgpt_already_open", action_posted=False,
            navigator_confirmed=True, navigation_action_diagnostics=diagnostics,
        )
    if result.ok and result.outcome == OUTCOME_OPENED:
        return NavigationAttemptResult(
            ok=True, outcome="new_chatgpt_chat_opened", action_posted=True,
            navigator_confirmed=True, navigation_action_diagnostics=diagnostics,
        )
    if pressed and result.reason_code == REASON_NOT_CONFIRMED:
        # Same rule as Classic: the action was performed against the exact
        # target, so let the authoritative gate decide.
        return NavigationAttemptResult(
            ok=True, outcome="new_chatgpt_chat_pressed_unconfirmed", action_posted=True,
            navigator_confirmed=False, navigation_action_diagnostics=diagnostics,
        )
    return NavigationAttemptResult(
        ok=False,
        outcome="new_chatgpt_navigation_failed",
        reason_code="destination_navigation_action_not_performed",
        error_message=((result.reason_code or "") + ": " + (result.error or ""))[:800],
        action_posted=False,
        navigator_confirmed=False,
        navigation_action_diagnostics=diagnostics,
    )


def new_target_services():
    from agent.chatgpt_target import CHATGPT_TARGET_NEW, ChatGPTTargetServices

    return ChatGPTTargetServices(
        target=CHATGPT_TARGET_NEW,
        submit_service=new_submit_service,
        capture_service=new_capture_service,
        destination_adapter_factory=new_destination_adapter_factory,
        destination_navigation_service=new_destination_navigation_service,
    )

