"""Reusable ChatGPT-domain services shared by CLI and future local controllers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from agent import ledger as default_ledger
from agent.chatgpt_ax_capture import (
    DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    DEFAULT_STABLE_SECONDS,
    capture_response_after_feedback,
)
from agent.clipboard import copy_to_clipboard
from agent.gpt_feedback import build_gpt_feedback_message
from agent.mac_app_control import activate_chatgpt
from agent.mac_paste import (
    ENTER_METHOD,
    PASTE_METHOD,
    paste_clipboard_to_frontmost_app,
    press_enter_in_frontmost_app,
)
from agent.mac_ui_inspect import (
    inspect_chatgpt_submission_ui,
    press_chatgpt_send_button,
)
from agent.prompt_extraction import (
    extract_next_codex_prompt_from_text,
    find_latest_valid_captured_response,
    sha256_text,
)


NEXT_CODEX_PROMPT_EXTRACTED_EVENT_TYPE = "next_codex_prompt_extracted"
NEXT_CODEX_PROMPT_EXTRACTED_MESSAGE = "Extracted next Codex prompt from captured GPT response."
SENTINEL_EXTRACTION_METHOD = "sentinel_block"
GPT_RESPONSE_CAPTURE_STARTED_EVENT_TYPE = "gpt_response_capture_started"
GPT_RESPONSE_CAPTURE_STARTED_MESSAGE = "Assistant response capture started after verified ChatGPT submission."
GPT_RESPONSE_CAPTURED_EVENT_TYPE = "gpt_response_captured"
GPT_RESPONSE_CAPTURED_MESSAGE = "Captured GPT response from ChatGPT desktop accessibility tree."
GPT_RESPONSE_CAPTURE_FAILED_EVENT_TYPE = "gpt_response_capture_failed"
GPT_RESPONSE_CAPTURE_FAILED_MESSAGE = "Failed to capture GPT response from ChatGPT desktop accessibility tree."
CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS: float | None = None
CHATGPT_PASTE_VERIFY_POLL_SECONDS = 0.15
CHATGPT_POST_PASTE_SETTLE_SECONDS = 0.5
CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS: float | None = None
CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS = 0.35
# A submit is a near-instant UI change (the marker moves from the composer into
# the transcript). Submission confirmation is therefore bounded by a finite poll
# budget so that a submit which never registers fails closed (falls back, then
# releases the UI lease) instead of blocking the handoff forever. This bounds a
# discrete UI confirmation only; it does NOT cut off Codex execution or ChatGPT
# response generation, which remain deadline-free.
CHATGPT_SUBMISSION_VERIFY_MAX_POLLS = 40


class PromptExtractionLedger(Protocol):
    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


class ChatGPTResponseCaptureLedger(Protocol):
    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


class ChatGPTFeedbackSubmissionLedger(Protocol):
    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def add_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class ExtractNextCodexPromptServiceResult:
    ok: bool
    run_id: str
    reason_code: str | None = None
    error_message: str | None = None
    warnings: tuple[str, ...] = ()
    selection: Any = None
    extraction: Any = None
    prompt_text: str = ""
    prompt_sha256: str = ""
    prompt_length: int = 0
    extraction_method: str | None = None
    source_event_id: Any = None
    source_response_sha256: str = ""
    matched_submission_event_id: Any = None
    output_path: Path | None = None
    artifact_written: bool = False
    event_type: str | None = None
    event_id: int | None = None
    metadata: dict[str, Any] | None = None
    persisted: bool = False


@dataclass(frozen=True)
class CaptureChatGPTResponseServiceResult:
    ok: bool
    run_id: str
    reason_code: str | None = None
    error_message: str | None = None
    submission_event_id: int | None = None
    capture_started_event_id: int | None = None
    capture_event_id: int | None = None
    failure_event_id: int | None = None
    captured_response_text: str | None = None
    captured_response_sha256: str | None = None
    sentinel_state: str | None = None
    stable: bool | None = None
    candidate_summaries: list[dict[str, Any]] | None = None
    matched_submission_marker_details: dict[str, Any] | None = None
    activation_result: dict[str, Any] | None = None
    raw_capture_result: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    event_type: str | None = None
    persisted: bool = False


@dataclass(frozen=True)
class SubmitFeedbackToChatGPTServiceResult:
    ok: bool
    run_id: str
    reason_code: str | None = None
    error_message: str | None = None
    feedback_message: str | None = None
    feedback_payload_sha256: str | None = None
    feedback_payload_length: int | None = None
    submission_marker_text: str | None = None
    submission_marker_sha256: str | None = None
    submission_nonce: str | None = None
    generated_event_id: int | None = None
    copied_event_id: int | None = None
    pasted_event_id: int | None = None
    submit_input_event_id: int | None = None
    verified_event_id: int | None = None
    failure_event_id: int | None = None
    ambiguous_event_id: int | None = None
    copy_result: dict[str, Any] | None = None
    activation_result: dict[str, Any] | None = None
    composer_result: dict[str, Any] | None = None
    paste_result: dict[str, Any] | None = None
    send_result: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None
    event_type: str | None = None
    persisted: bool = False
    metadata: dict[str, Any] | None = None
    output_path: Path | None = None


def submit_feedback_to_chatgpt_service(
    run_id: str,
    run: dict[str, Any],
    *,
    app_name: str = "ChatGPT",
    output_path_text: str | None = None,
    approval_mode: str = "human",
    ledger: ChatGPTFeedbackSubmissionLedger = default_ledger,
    feedback_builder: Callable[..., dict[str, Any]] = build_gpt_feedback_message,
    clipboard_copy_function: Callable[[str], dict[str, Any]] = copy_to_clipboard,
    activation_function: Callable[[str], dict[str, Any]] = activate_chatgpt,
    submission_ui_inspection_function: Callable[..., dict[str, Any]] = inspect_chatgpt_submission_ui,
    paste_function: Callable[[], dict[str, Any]] = paste_clipboard_to_frontmost_app,
    ax_send_button_function: Callable[[str, str], dict[str, Any]] = press_chatgpt_send_button,
    enter_function: Callable[[], dict[str, Any]] = press_enter_in_frontmost_app,
    artifact_writer: Callable[[str, str], Path] | None = None,
    monotonic_function: Callable[[], float] = time.monotonic,
    sleep_function: Callable[[float], None] = time.sleep,
    paste_verify_timeout_seconds: float | None = CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS,
    paste_verify_poll_seconds: float = CHATGPT_PASTE_VERIFY_POLL_SECONDS,
    post_paste_settle_seconds: float = CHATGPT_POST_PASTE_SETTLE_SECONDS,
    submission_verify_timeout_seconds: float | None = CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS,
    submission_verify_poll_seconds: float = CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS,
    submission_verification_function: Callable[[str, str], dict[str, Any]] | None = None,
) -> SubmitFeedbackToChatGPTServiceResult:
    if artifact_writer is None:
        artifact_writer = _write_feedback_artifact

    events = ledger.list_events(run_id)
    feedback = feedback_builder(run, events)
    output_path = None
    feedback_message = feedback.get("message")
    if not feedback.get("submittable", True) or not isinstance(feedback_message, str) or not feedback_message:
        reason_code = str(feedback.get("reason_code") or "gpt_feedback_not_submittable")
        error_message = str(feedback.get("error_message") or reason_code)
        failure_metadata = {
            "run_id": feedback.get("run_id", run_id),
            "status": feedback.get("status", run.get("status")),
            "reason_code": reason_code,
            "error": error_message,
            "codex_exit_code": feedback.get("codex_exit_code"),
            "codex_timed_out": feedback.get("codex_timed_out"),
            "changed_files": feedback.get("changed_files", []),
            "feedback_payload_version": feedback.get("feedback_payload_version"),
            "feedback_payload_length": feedback.get("feedback_payload_length", 0),
            "transport_guard": feedback.get("transport_guard", {}),
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "clipboard_copy_skipped": True,
            "paste_skipped": True,
            "submit_input_skipped": True,
        }
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_generation_failed",
                "GPT feedback was not generated because the Codex final message was not transport-safe.",
                failure_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code=reason_code,
            error_message=error_message,
            failure_event_id=failure_event_id,
            event_type="gpt_feedback_generation_failed",
            metadata=failure_metadata,
        )

    if output_path_text:
        output_path = artifact_writer(output_path_text, feedback_message)

    marker_metadata = _feedback_marker_metadata(feedback)
    generation_metadata = {
        "run_id": feedback["run_id"],
        "status": feedback["status"],
        "codex_exit_code": feedback["codex_exit_code"],
        "codex_timed_out": feedback["codex_timed_out"],
        "changed_files": feedback["changed_files"],
        "message_length": len(feedback_message),
        "feedback_payload_version": feedback["feedback_payload_version"],
        "feedback_payload_sha256": feedback["feedback_payload_sha256"],
        "feedback_payload_length": feedback["feedback_payload_length"],
        **marker_metadata,
        "approval_mode": approval_mode,
        "target": "ChatGPT",
        "app_name": app_name,
        "targeted_chatgpt": True,
    }
    if output_path is not None:
        generation_metadata["output_path"] = str(output_path)
    generated_event_id = _event_id(
        ledger.add_event(
            run_id,
            "gpt_feedback_generated",
            "Generated GPT feedback message for ChatGPT-targeted submit.",
            generation_metadata,
        )
    )

    copy_result = clipboard_copy_function(feedback_message)
    copied_event_id = _event_id(
        ledger.add_event(
            run_id,
            "gpt_feedback_copied",
            (
                "Copied GPT feedback message to clipboard for ChatGPT-targeted submit."
                if copy_result["copied"]
                else f"Failed to copy GPT feedback message to clipboard: {copy_result['error']}"
            ),
            {
                "run_id": feedback["run_id"],
                "copied": copy_result["copied"],
                "method": copy_result["method"],
                "error": copy_result["error"],
                "message_length": len(feedback_message),
                "feedback_payload_version": feedback["feedback_payload_version"],
                "feedback_payload_sha256": feedback["feedback_payload_sha256"],
                "feedback_payload_length": feedback["feedback_payload_length"],
                **marker_metadata,
                "approval_mode": approval_mode,
                "target": "ChatGPT",
                "app_name": app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
            },
        )
    )

    if not copy_result["copied"]:
        activation_result = {
            "activated": False,
            "app_name": app_name,
            "frontmost_app": None,
            "is_frontmost": False,
            "activation_result": None,
            "frontmost_result": None,
            "error": "Skipped activation because copying GPT feedback to clipboard failed.",
        }
        paste_result = _skipped_paste_result("Skipped paste because copying GPT feedback to clipboard failed.")
        submit_result = _skipped_submit_result(
            "Skipped submit because copying GPT feedback to clipboard failed."
        )
        common_metadata = {
            "run_id": feedback["run_id"],
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "clipboard_copy_failed",
        }
        pasted_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_pasted",
                "Skipped ChatGPT-targeted paste because copying GPT feedback failed.",
                common_metadata,
            )
        )
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_failed",
                "ChatGPT feedback submission failed before submit input.",
                common_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code="clipboard_copy_failed",
            error_message=copy_result["error"],
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            pasted_event_id=pasted_event_id,
            failure_event_id=failure_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            paste_result=paste_result,
            send_result=submit_result,
            event_type="gpt_feedback_submission_failed",
            metadata=common_metadata,
        )

    activation_result = activation_function(app_name)
    if not activation_result["is_frontmost"]:
        paste_result = _skipped_paste_result("Skipped paste because ChatGPT was not frontmost.")
        submit_result = _skipped_submit_result("Skipped submit because ChatGPT was not frontmost.")
        common_metadata = {
            "run_id": feedback["run_id"],
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "approval_mode": approval_mode,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_not_frontmost",
        }
        pasted_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_pasted",
                "Skipped ChatGPT-targeted paste because ChatGPT was not frontmost.",
                common_metadata,
            )
        )
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_failed",
                "ChatGPT feedback submission failed before submit input.",
                common_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code="chatgpt_not_frontmost",
            error_message=activation_result["error"],
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            pasted_event_id=pasted_event_id,
            failure_event_id=failure_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            paste_result=paste_result,
            send_result=submit_result,
            event_type="gpt_feedback_submission_failed",
            metadata=common_metadata,
        )

    initial_observation = submission_ui_inspection_function(
        app_name,
        marker_text=feedback["submission_marker_text"],
    )
    focused_composer = _focused_composer_from_observation(initial_observation)
    composer_result = _submission_ui_observation_summary(
        initial_observation,
        feedback["submission_marker_text"],
    )
    composer_text_before_paste = str(
        (focused_composer or {}).get("text") or (focused_composer or {}).get("value") or ""
    )
    composer_result["focused_composer_was_empty_before_paste"] = not bool(
        composer_text_before_paste.strip()
    )
    composer_result["composer_clear_before_paste_required"] = bool(
        composer_text_before_paste.strip()
    )
    if not initial_observation.get("ok") or focused_composer is None:
        reason_code = (
            "chatgpt_composer_not_focused"
            if (initial_observation.get("text_input_candidates") or [])
            else "chatgpt_composer_not_found"
        )
        paste_result = _skipped_paste_result(
            "Skipped paste because focused ChatGPT composer could not be verified."
        )
        submit_result = _skipped_submit_result(
            "Skipped submit because focused ChatGPT composer could not be verified."
        )
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "composer_observation": composer_result,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": reason_code,
        }
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_failed",
                f"ChatGPT feedback submission failed: {reason_code}.",
                failure_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code=reason_code,
            error_message=reason_code,
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            failure_event_id=failure_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            composer_result=composer_result,
            paste_result=paste_result,
            send_result=submit_result,
            event_type="gpt_feedback_submission_failed",
            metadata=failure_metadata,
        )

    paste_result = paste_function()
    pasted_event_id = _event_id(
        ledger.add_event(
            run_id,
            "gpt_feedback_pasted",
            (
                "Pasted GPT feedback into ChatGPT for submit."
                if paste_result["pasted"]
                else "Failed to paste GPT feedback into ChatGPT for submit."
            ),
            {
                "run_id": feedback["run_id"],
                "activation_result": activation_result,
                "paste_result": paste_result,
                "composer_observation": composer_result,
                "message_length": len(feedback["message"]),
                "feedback_payload_version": feedback["feedback_payload_version"],
                "feedback_payload_sha256": feedback["feedback_payload_sha256"],
                "feedback_payload_length": feedback["feedback_payload_length"],
                **marker_metadata,
                "approval_mode": approval_mode,
                "target": "ChatGPT",
                "app_name": app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
            },
        )
    )

    if not paste_result["pasted"]:
        submit_result = _skipped_submit_result("Skipped submit because paste failed.")
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            "feedback_payload_version": feedback["feedback_payload_version"],
            "feedback_payload_sha256": feedback["feedback_payload_sha256"],
            "feedback_payload_length": feedback["feedback_payload_length"],
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_paste_input_failed",
        }
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_failed",
                "ChatGPT feedback submission failed before submit input.",
                failure_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code="chatgpt_paste_input_failed",
            error_message=paste_result["error"],
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            pasted_event_id=pasted_event_id,
            failure_event_id=failure_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            composer_result=composer_result,
            paste_result=paste_result,
            send_result=submit_result,
            event_type="gpt_feedback_submission_failed",
            metadata=failure_metadata,
        )

    paste_verification = _wait_for_pasted_marker(
        app_name,
        feedback["submission_marker_text"],
        inspection_function=submission_ui_inspection_function,
        monotonic_function=monotonic_function,
        sleep_function=sleep_function,
        timeout_seconds=paste_verify_timeout_seconds,
        poll_interval_seconds=paste_verify_poll_seconds,
    )
    if not paste_verification["ok"]:
        submit_result = _skipped_submit_result(
            "Skipped submit because pasted marker was not visible in the composer."
        )
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_paste_not_visible",
        }
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_failed",
                "ChatGPT feedback submission failed: pasted marker was not visible.",
                failure_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code="chatgpt_paste_not_visible",
            error_message="chatgpt_paste_not_visible",
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            pasted_event_id=pasted_event_id,
            failure_event_id=failure_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            composer_result=composer_result,
            paste_result=paste_result,
            send_result=submit_result,
            verification_result=paste_verification,
            event_type="gpt_feedback_submission_failed",
            metadata=failure_metadata,
        )

    sleep_function(post_paste_settle_seconds)
    send_result, send_observation = _select_send_input_method(
        app_name,
        feedback["submission_marker_text"],
        inspection_function=submission_ui_inspection_function,
        ax_send_button_function=ax_send_button_function,
        enter_function=enter_function,
    )
    submit_input_sent = _submit_input_sent_ok(send_result)
    submit_result = {
        **send_result,
        "submit_input_sent": submit_input_sent,
    }
    submit_input_event_id = _event_id(
        ledger.add_event(
            run_id,
            "gpt_feedback_submit_input_sent",
            (
                "Submit input sent; awaiting ChatGPT submission verification."
                if submit_input_sent
                else "Submit input was not confirmed."
            ),
            {
                "run_id": feedback["run_id"],
                "approval_mode": approval_mode,
                "human_confirmed": approval_mode == "human",
                "auto_executed": approval_mode == "auto",
                "activation_result": activation_result,
                "paste_result": paste_result,
                "paste_verification": paste_verification,
                "submit_input_result": submit_result,
                "send_observation": send_observation,
                "message_length": len(feedback["message"]),
                **marker_metadata,
                "target": "ChatGPT",
                "app_name": app_name,
                "targeted_chatgpt": True,
                "output_path": str(output_path) if output_path is not None else None,
                "reason_code": "submit_input_sent" if submit_input_sent else "chatgpt_submit_input_not_confirmed",
            },
        )
    )

    if not submit_input_sent:
        failure_metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "reason_code": "chatgpt_submit_input_not_confirmed",
        }
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_failed",
                "ChatGPT submit input was not confirmed.",
                failure_metadata,
            )
        )
        return _submission_result(
            False,
            run_id,
            feedback,
            output_path,
            reason_code="chatgpt_submit_input_not_confirmed",
            error_message="chatgpt_submit_input_not_confirmed",
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            pasted_event_id=pasted_event_id,
            submit_input_event_id=submit_input_event_id,
            failure_event_id=failure_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            composer_result=composer_result,
            paste_result=paste_result,
            send_result=submit_result,
            event_type="gpt_feedback_submission_failed",
            metadata=failure_metadata,
        )

    if submission_verification_function is None:
        verification = _verify_submission_marker(
            app_name,
            feedback["submission_marker_text"],
            inspection_function=submission_ui_inspection_function,
            monotonic_function=monotonic_function,
            sleep_function=sleep_function,
            timeout_seconds=submission_verify_timeout_seconds,
            poll_interval_seconds=submission_verify_poll_seconds,
        )
    else:
        verification = submission_verification_function(app_name, feedback["submission_marker_text"])

    fallback_attempt_count = 0
    if (
        not verification["ok"]
        and verification.get("reason_code") == "chatgpt_submission_not_verified"
        and verification.get("status", {}).get("composer_contains_marker") is True
        and verification.get("status", {}).get("submitted_candidate_count") == 0
        and _send_result_method(send_result) == "macos_accessibility_axpress_send_button"
    ):
        fallback_attempt_count = 1
        fallback_result = enter_function()
        fallback_submit_result = {**fallback_result, "submit_input_sent": _submit_input_sent_ok(fallback_result)}
        submit_input_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submit_input_sent",
                "Fallback submit input sent; awaiting ChatGPT submission verification.",
                {
                    "run_id": feedback["run_id"],
                    "approval_mode": approval_mode,
                    "activation_result": activation_result,
                    "paste_result": paste_result,
                    "paste_verification": paste_verification,
                    "submit_input_result": fallback_submit_result,
                    "previous_verification": verification,
                    "fallback_attempt_count": fallback_attempt_count,
                    "message_length": len(feedback["message"]),
                    **marker_metadata,
                    "target": "ChatGPT",
                    "app_name": app_name,
                    "targeted_chatgpt": True,
                    "reason_code": (
                        "submit_input_sent"
                        if fallback_submit_result["submit_input_sent"]
                        else "chatgpt_submit_input_not_confirmed"
                    ),
                },
            )
        )
        submit_result = fallback_submit_result
        if fallback_submit_result["submit_input_sent"]:
            if submission_verification_function is None:
                verification = _verify_submission_marker(
                    app_name,
                    feedback["submission_marker_text"],
                    inspection_function=submission_ui_inspection_function,
                    monotonic_function=monotonic_function,
                    sleep_function=sleep_function,
                    timeout_seconds=submission_verify_timeout_seconds,
                    poll_interval_seconds=submission_verify_poll_seconds,
                )
            else:
                verification = submission_verification_function(app_name, feedback["submission_marker_text"])

    terminal_error = paste_result["error"] or submit_result.get("error")
    if verification["ok"]:
        metadata = {
            "run_id": feedback["run_id"],
            "approval_mode": approval_mode,
            "human_confirmed": approval_mode == "human",
            "auto_executed": approval_mode == "auto",
            "activation_result": activation_result,
            "paste_result": paste_result,
            "paste_verification": paste_verification,
            "submit_input_result": submit_result,
            "submission_verification": verification,
            "fallback_attempt_count": fallback_attempt_count,
            "message_length": len(feedback["message"]),
            **marker_metadata,
            "target": "ChatGPT",
            "app_name": app_name,
            "targeted_chatgpt": True,
            "output_path": str(output_path) if output_path is not None else None,
            "reason_code": "chatgpt_submission_verified",
        }
        verified_event_id = _event_id(
            ledger.add_event(
                run_id,
                "gpt_feedback_submission_verified",
                "Feedback submission verified; waiting for ChatGPT response.",
                metadata,
            )
        )
        return _submission_result(
            True,
            run_id,
            feedback,
            output_path,
            reason_code="chatgpt_submission_verified",
            error_message=terminal_error,
            generated_event_id=generated_event_id,
            copied_event_id=copied_event_id,
            pasted_event_id=pasted_event_id,
            submit_input_event_id=submit_input_event_id,
            verified_event_id=verified_event_id,
            copy_result=copy_result,
            activation_result=activation_result,
            composer_result=composer_result,
            paste_result=paste_result,
            send_result=submit_result,
            verification_result=verification,
            event_type="gpt_feedback_submission_verified",
            metadata=metadata,
        )

    event_type = (
        "gpt_feedback_submission_ambiguous"
        if verification.get("reason_code") == "chatgpt_submission_ambiguous"
        else "gpt_feedback_submission_failed"
    )
    metadata = {
        "run_id": feedback["run_id"],
        "approval_mode": approval_mode,
        "human_confirmed": approval_mode == "human",
        "auto_executed": approval_mode == "auto",
        "activation_result": activation_result,
        "paste_result": paste_result,
        "paste_verification": paste_verification,
        "submit_input_result": submit_result,
        "submission_verification": verification,
        "fallback_attempt_count": fallback_attempt_count,
        "message_length": len(feedback["message"]),
        **marker_metadata,
        "target": "ChatGPT",
        "app_name": app_name,
        "targeted_chatgpt": True,
        "output_path": str(output_path) if output_path is not None else None,
        "reason_code": verification.get("reason_code") or "chatgpt_submission_not_verified",
    }
    terminal_event_id = _event_id(
        ledger.add_event(
            run_id,
            event_type,
            f"ChatGPT feedback submission was not verified: {verification.get('reason_code')}.",
            metadata,
        )
    )
    return _submission_result(
        False,
        run_id,
        feedback,
        output_path,
        reason_code=verification.get("reason_code") or "chatgpt_submission_not_verified",
        error_message=terminal_error or verification.get("reason_code"),
        generated_event_id=generated_event_id,
        copied_event_id=copied_event_id,
        pasted_event_id=pasted_event_id,
        submit_input_event_id=submit_input_event_id,
        failure_event_id=terminal_event_id if event_type == "gpt_feedback_submission_failed" else None,
        ambiguous_event_id=terminal_event_id if event_type == "gpt_feedback_submission_ambiguous" else None,
        copy_result=copy_result,
        activation_result=activation_result,
        composer_result=composer_result,
        paste_result=paste_result,
        send_result=submit_result,
        verification_result=verification,
        event_type=event_type,
        metadata=metadata,
    )


def capture_chatgpt_response_service(
    run_id: str,
    *,
    app_name: str = "ChatGPT",
    timeout_seconds: float | None = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    stable_seconds: float = DEFAULT_STABLE_SECONDS,
    require_sentinel_response: bool = False,
    ledger: ChatGPTResponseCaptureLedger = default_ledger,
    activation_function: Callable[[str], dict[str, Any]] = activate_chatgpt,
    capture_function: Callable[..., dict[str, Any]] = capture_response_after_feedback,
    hash_function: Callable[[str], str] = sha256_text,
) -> CaptureChatGPTResponseServiceResult:
    del timeout_seconds
    events = ledger.list_events(run_id)
    submitted_event = _latest_verified_gpt_feedback_submission(events)
    if submitted_event is None:
        return CaptureChatGPTResponseServiceResult(
            ok=False,
            run_id=run_id,
            reason_code="no_verified_submission",
            error_message="no verified ChatGPT submission was found for this run",
        )

    submitted_metadata = _event_metadata(submitted_event)
    submission_event_id = _event_id_from_event(submitted_event)
    marker_details = _submission_marker_details(submitted_metadata)
    submission_marker_text = marker_details.get("submission_marker_text")
    submission_marker_sha256 = marker_details.get("submission_marker_sha256")
    if not isinstance(submission_marker_text, str) or not submission_marker_text.strip():
        return CaptureChatGPTResponseServiceResult(
            ok=False,
            run_id=run_id,
            reason_code="missing_submission_marker",
            error_message="verified submission event did not include a submission marker",
            submission_event_id=submission_event_id,
            matched_submission_marker_details=marker_details,
        )
    if not isinstance(submission_marker_sha256, str) or hash_function(submission_marker_text) != submission_marker_sha256:
        return CaptureChatGPTResponseServiceResult(
            ok=False,
            run_id=run_id,
            reason_code="submission_marker_sha_mismatch",
            error_message="verified submission marker hash did not match marker text",
            submission_event_id=submission_event_id,
            matched_submission_marker_details=marker_details,
        )

    activation_result = activation_function(app_name)
    if not activation_result["is_frontmost"]:
        capture_result = {
            "matched_feedback": False,
            "candidate_count": 0,
            "response_length": 0,
            "response_sha256": "",
            "stable": False,
            "sentinel_required": require_sentinel_response,
            "error": activation_result["error"] or "ChatGPT was not frontmost.",
        }
        return CaptureChatGPTResponseServiceResult(
            ok=False,
            run_id=run_id,
            reason_code="chatgpt_not_frontmost",
            error_message=capture_result["error"],
            submission_event_id=submission_event_id,
            stable=False,
            candidate_summaries=[],
            matched_submission_marker_details=marker_details,
            activation_result=activation_result,
            raw_capture_result=capture_result,
        )

    capture_started_metadata = _capture_started_metadata(
        run_id,
        app_name,
        submitted_event,
        submitted_metadata,
        submission_marker_text,
        submission_marker_sha256,
        None,
        stable_seconds,
        require_sentinel_response,
        activation_result,
    )
    capture_started_event_id = _event_id(
        ledger.add_event(
            run_id,
            GPT_RESPONSE_CAPTURE_STARTED_EVENT_TYPE,
            GPT_RESPONSE_CAPTURE_STARTED_MESSAGE,
            capture_started_metadata,
        )
    )

    capture_result = capture_function(
        "",
        app_name=app_name,
        timeout_seconds=None,
        stable_seconds=stable_seconds,
        require_sentinel_response=require_sentinel_response,
        submission_marker_text=submission_marker_text,
    )
    if not capture_result["ok"]:
        metadata = _capture_failed_metadata(
            run_id,
            app_name,
            submitted_event,
            submitted_metadata,
            submission_marker_text,
            submission_marker_sha256,
            require_sentinel_response,
            capture_result,
        )
        failure_event_id = _event_id(
            ledger.add_event(
                run_id,
                GPT_RESPONSE_CAPTURE_FAILED_EVENT_TYPE,
                GPT_RESPONSE_CAPTURE_FAILED_MESSAGE,
                metadata,
            )
        )
        return CaptureChatGPTResponseServiceResult(
            ok=False,
            run_id=run_id,
            reason_code=capture_result.get("reason_code"),
            error_message=capture_result.get("error"),
            submission_event_id=submission_event_id,
            capture_started_event_id=capture_started_event_id,
            failure_event_id=failure_event_id,
            sentinel_state=capture_result.get("sentinel_state"),
            stable=bool(capture_result.get("stable", False)),
            candidate_summaries=capture_result.get("post_feedback_candidate_summaries", []),
            matched_submission_marker_details=marker_details,
            activation_result=activation_result,
            raw_capture_result=capture_result,
            metadata=metadata,
            event_type=GPT_RESPONSE_CAPTURE_FAILED_EVENT_TYPE,
            persisted=True,
        )

    metadata = _capture_success_metadata(
        run_id,
        app_name,
        submitted_event,
        submitted_metadata,
        submission_marker_text,
        submission_marker_sha256,
        capture_result,
    )
    capture_event_id = _event_id(
        ledger.add_event(
            run_id,
            GPT_RESPONSE_CAPTURED_EVENT_TYPE,
            GPT_RESPONSE_CAPTURED_MESSAGE,
            metadata,
        )
    )
    return CaptureChatGPTResponseServiceResult(
        ok=True,
        run_id=run_id,
        reason_code=capture_result.get("reason_code"),
        submission_event_id=submission_event_id,
        capture_started_event_id=capture_started_event_id,
        capture_event_id=capture_event_id,
        captured_response_text=capture_result["response_text"],
        captured_response_sha256=capture_result["response_sha256"],
        sentinel_state=capture_result.get("sentinel_state"),
        stable=bool(capture_result.get("stable", False)),
        candidate_summaries=capture_result.get("post_feedback_candidate_summaries", []),
        matched_submission_marker_details=marker_details,
        activation_result=activation_result,
        raw_capture_result=capture_result,
        metadata=metadata,
        event_type=GPT_RESPONSE_CAPTURED_EVENT_TYPE,
        persisted=True,
    )


def extract_next_codex_prompt_service(
    run_id: str,
    *,
    require_sentinel: bool = False,
    confirm_extract: bool = False,
    output_path_text: str | None = None,
    ledger: PromptExtractionLedger = default_ledger,
    selector: Callable[[list[dict]], Any] = find_latest_valid_captured_response,
    parser: Callable[[str], Any] = extract_next_codex_prompt_from_text,
    artifact_writer: Callable[[Path, str], Path] | None = None,
    hash_function: Callable[[str], str] = sha256_text,
) -> ExtractNextCodexPromptServiceResult:
    if artifact_writer is None:
        artifact_writer = _write_prompt_artifact

    events = ledger.list_events(run_id)
    selection = selector(events)
    if not selection.ok:
        return ExtractNextCodexPromptServiceResult(
            ok=False,
            run_id=run_id,
            reason_code="no_valid_captured_response",
            error_message=selection.error,
            warnings=tuple(selection.warnings),
            selection=selection,
        )

    extraction = parser(selection.response_text)
    warnings = (*selection.warnings, *extraction.warnings)
    if not extraction.ok:
        return _result_from_extraction(
            run_id,
            selection,
            extraction,
            reason_code="extraction_failed",
            error_message=extraction.error,
            warnings=warnings,
        )

    if require_sentinel and extraction.extraction_method != SENTINEL_EXTRACTION_METHOD:
        return _result_from_extraction(
            run_id,
            selection,
            extraction,
            reason_code="sentinel_required",
            error_message="ChatGPT did not provide a sentinel-wrapped next Codex prompt.",
            warnings=warnings,
        )

    output_path = _prompt_output_path(run_id, output_path_text) if confirm_extract else None
    if not confirm_extract:
        return _result_from_extraction(
            run_id,
            selection,
            extraction,
            reason_code=None,
            error_message=None,
            warnings=warnings,
            output_path=None,
            prompt_sha256=extraction.prompt_sha256 or hash_function(extraction.prompt_text),
        )

    try:
        written_path = artifact_writer(output_path, extraction.prompt_text)
    except OSError as exc:
        return _result_from_extraction(
            run_id,
            selection,
            extraction,
            reason_code="artifact_write_failed",
            error_message=str(exc),
            warnings=warnings,
            output_path=output_path,
            prompt_sha256=extraction.prompt_sha256 or hash_function(extraction.prompt_text),
        )

    prompt_sha256 = extraction.prompt_sha256 or hash_function(extraction.prompt_text)
    metadata = _extraction_event_metadata(
        selection,
        extraction,
        written_path,
        warnings,
        prompt_sha256,
    )
    event_id = _event_id(
        ledger.add_event(
            run_id,
            NEXT_CODEX_PROMPT_EXTRACTED_EVENT_TYPE,
            NEXT_CODEX_PROMPT_EXTRACTED_MESSAGE,
            metadata,
        )
    )
    return _result_from_extraction(
        run_id,
        selection,
        extraction,
        reason_code=None,
        error_message=None,
        warnings=warnings,
        output_path=written_path,
        artifact_written=True,
        event_type=NEXT_CODEX_PROMPT_EXTRACTED_EVENT_TYPE,
        event_id=event_id,
        metadata=metadata,
        persisted=True,
        prompt_sha256=prompt_sha256,
    )


def _feedback_marker_metadata(feedback: dict[str, Any]) -> dict[str, Any]:
    return {
        "submission_marker_text": feedback["submission_marker_text"],
        "submission_marker_sha256": feedback["submission_marker_sha256"],
        "submission_marker_nonce": feedback["submission_marker_nonce"],
        "submission_marker_payload_sha256": feedback["submission_marker_payload_sha256"],
        "payload_without_marker_sha256": feedback["payload_without_marker_sha256"],
        "feedback_payload_version": feedback["feedback_payload_version"],
        "feedback_payload_sha256": feedback["feedback_payload_sha256"],
        "feedback_payload_length": feedback["feedback_payload_length"],
    }


def _write_feedback_artifact(output_path_text: str, message: str) -> Path:
    output_path = Path(output_path_text).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(message, encoding="utf-8")
    return output_path


def _submission_result(
    ok: bool,
    run_id: str,
    feedback: dict[str, Any],
    output_path: Path | None,
    *,
    reason_code: str | None,
    error_message: str | None,
    generated_event_id: int | None = None,
    copied_event_id: int | None = None,
    pasted_event_id: int | None = None,
    submit_input_event_id: int | None = None,
    verified_event_id: int | None = None,
    failure_event_id: int | None = None,
    ambiguous_event_id: int | None = None,
    copy_result: dict[str, Any] | None = None,
    activation_result: dict[str, Any] | None = None,
    composer_result: dict[str, Any] | None = None,
    paste_result: dict[str, Any] | None = None,
    send_result: dict[str, Any] | None = None,
    verification_result: dict[str, Any] | None = None,
    event_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SubmitFeedbackToChatGPTServiceResult:
    return SubmitFeedbackToChatGPTServiceResult(
        ok=ok,
        run_id=run_id,
        reason_code=reason_code,
        error_message=error_message,
        feedback_message=feedback.get("message"),
        feedback_payload_sha256=feedback.get("feedback_payload_sha256"),
        feedback_payload_length=feedback.get("feedback_payload_length"),
        submission_marker_text=feedback.get("submission_marker_text"),
        submission_marker_sha256=feedback.get("submission_marker_sha256"),
        submission_nonce=feedback.get("submission_marker_nonce"),
        generated_event_id=generated_event_id,
        copied_event_id=copied_event_id,
        pasted_event_id=pasted_event_id,
        submit_input_event_id=submit_input_event_id,
        verified_event_id=verified_event_id,
        failure_event_id=failure_event_id,
        ambiguous_event_id=ambiguous_event_id,
        copy_result=copy_result,
        activation_result=activation_result,
        composer_result=composer_result,
        paste_result=paste_result,
        send_result=send_result,
        verification_result=verification_result,
        event_type=event_type,
        persisted=event_type is not None,
        metadata=metadata,
        output_path=output_path,
    )


def _skipped_paste_result(error: str) -> dict[str, Any]:
    return {
        "pasted": False,
        "method": PASTE_METHOD,
        "error": error,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
    }


def _skipped_submit_result(error: str) -> dict[str, Any]:
    return {
        "submit_input_sent": False,
        "method": ENTER_METHOD,
        "error": error,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
    }


def _submission_ui_observation_summary(observation: dict[str, Any], marker_text: str | None = None) -> dict[str, Any]:
    composer = observation.get("focused_composer")
    send_button = observation.get("send_button")
    message_candidates = observation.get("message_candidates") or []
    marker_candidates = []
    if marker_text:
        for candidate in message_candidates:
            text = candidate.get("text") if isinstance(candidate, dict) else ""
            if isinstance(text, str) and marker_text in text:
                marker_candidates.append(_candidate_observation_summary(candidate, marker_text))

    return {
        "ok": bool(observation.get("ok")),
        "method": observation.get("method"),
        "error": observation.get("error"),
        "focused_element": _element_observation_summary(observation.get("focused_element"), marker_text),
        "focused_composer": _element_observation_summary(composer, marker_text),
        "text_input_candidate_count": len(observation.get("text_input_candidates") or []),
        "button_candidate_count": len(observation.get("button_candidates") or []),
        "send_button": _element_observation_summary(send_button, marker_text, include_text=False),
        "message_candidate_count": len(message_candidates),
        "marker_text_present_in_composer": bool(observation.get("marker_text_present_in_composer")),
        "marker_text_candidate_count": len(marker_candidates),
        "marker_candidates": marker_candidates[:5],
        "ax_stats": observation.get("ax_stats") or {},
    }


def _element_observation_summary(
    element: object,
    marker_text: str | None = None,
    include_text: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(element, dict):
        return None
    text = str(element.get("text") or element.get("value") or "")
    summary = {
        "path": element.get("path"),
        "role": element.get("role"),
        "subrole": element.get("subrole"),
        "title": element.get("title"),
        "description": element.get("description"),
        "identifier": element.get("identifier"),
        "enabled": element.get("enabled"),
        "focused": element.get("focused"),
        "actions": element.get("actions") or [],
    }
    if include_text:
        summary.update(
            {
                "text_length": len(text),
                "text_sha256": sha256_text(text) if text else "",
                "contains_marker": bool(marker_text and marker_text in text),
            }
        )
    return summary


def _candidate_observation_summary(candidate: dict[str, Any], marker_text: str | None = None) -> dict[str, Any]:
    text = str(candidate.get("text") or "")
    return {
        "index": candidate.get("index"),
        "path": candidate.get("path"),
        "role": candidate.get("role"),
        "subrole": candidate.get("subrole"),
        "text_length": len(text),
        "text_sha256": sha256_text(text) if text else "",
        "contains_marker": bool(marker_text and marker_text in text),
    }


def _focused_composer_from_observation(observation: dict[str, Any]) -> dict[str, Any] | None:
    composer = observation.get("focused_composer")
    return composer if isinstance(composer, dict) else None


def _wait_for_pasted_marker(
    app_name: str,
    marker_text: str,
    *,
    inspection_function: Callable[..., dict[str, Any]] = inspect_chatgpt_submission_ui,
    monotonic_function: Callable[[], float] = time.monotonic,
    sleep_function: Callable[[float], None] = time.sleep,
    timeout_seconds: float | None = CHATGPT_PASTE_VERIFY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = CHATGPT_PASTE_VERIFY_POLL_SECONDS,
) -> dict[str, Any]:
    del timeout_seconds
    polls = 0
    last_observation: dict[str, Any] = {}
    while True:
        polls += 1
        observation = inspection_function(app_name, marker_text=marker_text)
        last_observation = observation
        composer = _focused_composer_from_observation(observation)
        if composer is not None and marker_text in str(composer.get("text") or composer.get("value") or ""):
            return {
                "ok": True,
                "reason_code": "chatgpt_draft_pasted",
                "poll_count": polls,
                "timeout_seconds": None,
                "poll_interval_seconds": poll_interval_seconds,
                "observation": _submission_ui_observation_summary(observation, marker_text),
            }
        if poll_interval_seconds > 0:
            sleep_function(poll_interval_seconds)


def _submission_verification_status(observation: dict[str, Any], marker_text: str) -> dict[str, Any]:
    composer = _focused_composer_from_observation(observation)
    composer_text = str((composer or {}).get("text") or (composer or {}).get("value") or "")
    composer_contains = marker_text in composer_text if composer is not None else False
    candidates = [
        candidate
        for candidate in observation.get("message_candidates") or []
        if isinstance(candidate, dict)
        and candidate.get("path") != (composer or {}).get("path")
        and marker_text in str(candidate.get("text") or "")
    ]

    if len(candidates) == 1 and not composer_contains:
        return {
            "verified": True,
            "ambiguous": False,
            "reason_code": "chatgpt_submission_verified",
            "composer_contains_marker": False,
            "submitted_candidate_count": 1,
            "submitted_candidate": _candidate_observation_summary(candidates[0], marker_text),
        }
    if len(candidates) > 1 or (len(candidates) >= 1 and composer_contains):
        return {
            "verified": False,
            "ambiguous": True,
            "reason_code": "chatgpt_submission_ambiguous",
            "composer_contains_marker": composer_contains,
            "submitted_candidate_count": len(candidates),
            "submitted_candidates": [_candidate_observation_summary(item, marker_text) for item in candidates[:5]],
        }
    return {
        "verified": False,
        "ambiguous": False,
        "reason_code": "chatgpt_submission_not_verified",
        "composer_contains_marker": composer_contains,
        "submitted_candidate_count": len(candidates),
        "submitted_candidates": [_candidate_observation_summary(item, marker_text) for item in candidates[:5]],
    }


def _verify_submission_marker(
    app_name: str,
    marker_text: str,
    *,
    inspection_function: Callable[..., dict[str, Any]] = inspect_chatgpt_submission_ui,
    monotonic_function: Callable[[], float] = time.monotonic,
    sleep_function: Callable[[float], None] = time.sleep,
    timeout_seconds: float | None = CHATGPT_SUBMISSION_VERIFY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = CHATGPT_SUBMISSION_VERIFY_POLL_SECONDS,
    max_polls: int = CHATGPT_SUBMISSION_VERIFY_MAX_POLLS,
) -> dict[str, Any]:
    start = monotonic_function()
    polls = 0
    last_observation: dict[str, Any] = {}
    last_status: dict[str, Any] = {}
    while True:
        polls += 1
        observation = inspection_function(app_name, marker_text=marker_text)
        last_observation = observation
        status = _submission_verification_status(observation, marker_text)
        last_status = status
        if status["verified"] or status["ambiguous"]:
            return {
                "ok": bool(status["verified"]),
                "reason_code": status["reason_code"],
                "poll_count": polls,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "status": status,
                "observation": _submission_ui_observation_summary(observation, marker_text),
            }
        timed_out = (
            timeout_seconds is not None
            and (monotonic_function() - start) >= timeout_seconds
        )
        exhausted = max_polls is not None and polls >= max_polls
        if timed_out or exhausted:
            return {
                "ok": False,
                "reason_code": "chatgpt_submission_not_verified",
                "poll_count": polls,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "status": last_status,
                "observation": _submission_ui_observation_summary(last_observation, marker_text),
            }
        if poll_interval_seconds > 0:
            sleep_function(poll_interval_seconds)


# Accessibility markers that identify transcript message-action / feedback
# controls (e.g. the reply toolbar with "Good response" / "Bad response" /
# "Read Aloud" / "Memory updated"). A composer Send control never carries these,
# so a candidate bearing any of them is not a send button and must not be
# pressed — we fall back to Return, which reliably submits the composer.
_NON_SEND_BUTTON_MARKERS = (
    "good response",
    "bad response",
    "read aloud",
    "memory",
    "regenerate",
    "copy",
    "share",
    "thumbs",
    "more actions",
    "edit message",
    "dictate",
    "voice mode",
)


def _is_send_button_safe_to_press(send_button: Any) -> bool:
    if not isinstance(send_button, dict) or not send_button.get("path"):
        return False
    blob = " ".join(
        str(send_button.get(key) or "")
        for key in ("title", "description", "identifier", "role", "subrole")
    )
    blob += " " + " ".join(str(action) for action in (send_button.get("actions") or []))
    blob = blob.casefold()
    return not any(marker in blob for marker in _NON_SEND_BUTTON_MARKERS)


def _select_send_input_method(
    app_name: str,
    marker_text: str,
    *,
    inspection_function: Callable[..., dict[str, Any]] = inspect_chatgpt_submission_ui,
    ax_send_button_function: Callable[[str, str], dict[str, Any]] = press_chatgpt_send_button,
    enter_function: Callable[[], dict[str, Any]] = press_enter_in_frontmost_app,
) -> tuple[dict[str, Any], dict[str, Any]]:
    observation = inspection_function(app_name, marker_text=marker_text)
    send_button = observation.get("send_button") if isinstance(observation, dict) else None
    if _is_send_button_safe_to_press(send_button):
        result = ax_send_button_function(app_name, str(send_button["path"]))
        return result, _submission_ui_observation_summary(observation, marker_text)
    return enter_function(), _submission_ui_observation_summary(observation, marker_text)


def _submit_input_sent_ok(send_result: dict[str, Any]) -> bool:
    if "pressed" in send_result:
        return bool(send_result.get("pressed"))
    return bool(send_result.get("submitted"))


def _send_result_method(send_result: dict[str, Any]) -> str | None:
    if send_result.get("method"):
        return str(send_result["method"])
    return None


def _event_metadata(event: dict) -> dict:
    metadata_json = event.get("metadata_json")
    if not metadata_json:
        return {}
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _latest_verified_gpt_feedback_submission(events: list[dict]) -> dict | None:
    for event in reversed(events):
        if event.get("event_type") != "gpt_feedback_submission_verified":
            continue
        metadata = _event_metadata(event)
        if metadata.get("reason_code") == "chatgpt_submission_verified":
            return event
    return None


def _event_id_from_event(event: dict) -> int | None:
    event_id = event.get("id")
    return event_id if isinstance(event_id, int) else None


def _submission_marker_details(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "submission_marker_text": metadata.get("submission_marker_text"),
        "submission_marker_sha256": metadata.get("submission_marker_sha256"),
        "submission_marker_nonce": metadata.get("submission_marker_nonce"),
        "submission_marker_payload_sha256": metadata.get("submission_marker_payload_sha256"),
        "feedback_payload_sha256": metadata.get("feedback_payload_sha256"),
        "feedback_payload_version": metadata.get("feedback_payload_version"),
        "feedback_payload_length": metadata.get("feedback_payload_length"),
    }


def _capture_started_metadata(
    run_id: str,
    app_name: str,
    submitted_event: dict,
    submitted_metadata: dict[str, Any],
    submission_marker_text: str,
    submission_marker_sha256: str,
    timeout_seconds: float | None,
    stable_seconds: float,
    require_sentinel_response: bool,
    activation_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "app_name": app_name,
        "matched_submission_event_id": submitted_event.get("id"),
        "source_event_type": "gpt_feedback_submission_verified",
        "submission_marker_text": submission_marker_text,
        "submission_marker_sha256": submission_marker_sha256,
        "submission_marker_nonce": submitted_metadata.get("submission_marker_nonce"),
        "submission_marker_payload_sha256": submitted_metadata.get("submission_marker_payload_sha256"),
        "feedback_payload_sha256": submitted_metadata.get("feedback_payload_sha256"),
        "feedback_payload_version": submitted_metadata.get("feedback_payload_version"),
        "feedback_payload_length": submitted_metadata.get("feedback_payload_length"),
        "timeout_seconds": timeout_seconds,
        "stable_seconds": stable_seconds,
        "sentinel_required": require_sentinel_response,
        "activation_result": activation_result,
    }


def _capture_failed_metadata(
    run_id: str,
    app_name: str,
    submitted_event: dict,
    submitted_metadata: dict[str, Any],
    submission_marker_text: str,
    submission_marker_sha256: str,
    require_sentinel_response: bool,
    capture_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source": capture_result.get("source"),
        "app_name": app_name,
        "reason_code": capture_result.get("reason_code"),
        "error": capture_result.get("error"),
        "matched_submission_event_id": submitted_event.get("id"),
        "matched_submission_event_type": "gpt_feedback_submission_verified",
        "submission_marker_text": submission_marker_text,
        "submission_marker_sha256": submission_marker_sha256,
        "submission_marker_nonce": submitted_metadata.get("submission_marker_nonce"),
        "submission_marker_payload_sha256": submitted_metadata.get("submission_marker_payload_sha256"),
        "feedback_payload_sha256": submitted_metadata.get("feedback_payload_sha256"),
        "feedback_payload_version": submitted_metadata.get("feedback_payload_version"),
        "feedback_payload_length": submitted_metadata.get("feedback_payload_length"),
        "matched_candidate_index": capture_result.get("matched_candidate_index"),
        "matched_candidate_path": capture_result.get("matched_candidate_path"),
        "response_candidate_index": capture_result.get("response_candidate_index"),
        "response_candidate_path": capture_result.get("response_candidate_path"),
        "candidate_count": capture_result.get("candidate_count", 0),
        "sentinel_state": capture_result.get("sentinel_state"),
        "sentinel_required": require_sentinel_response,
        "post_feedback_candidate_summaries": capture_result.get("post_feedback_candidate_summaries", []),
        "stability": {
            "stable": capture_result.get("stable", False),
            "stable_seconds": capture_result.get("stable_seconds"),
            "successful_polls": capture_result.get("successful_polls"),
            "poll_interval_seconds": capture_result.get("poll_interval_seconds"),
            "timeout_seconds": capture_result.get("timeout_seconds"),
        },
        "ax_stats": capture_result.get("ax_stats", {}),
    }


def _capture_success_metadata(
    run_id: str,
    app_name: str,
    submitted_event: dict,
    submitted_metadata: dict[str, Any],
    submission_marker_text: str,
    submission_marker_sha256: str,
    capture_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source": capture_result["source"],
        "app_name": app_name,
        "response_text": capture_result["response_text"],
        "response_length": capture_result["response_length"],
        "response_sha256": capture_result["response_sha256"],
        "matched_submission_event_id": submitted_event.get("id"),
        "matched_submission_event_type": "gpt_feedback_submission_verified",
        "submission_marker_text": submission_marker_text,
        "submission_marker_sha256": submission_marker_sha256,
        "submission_marker_nonce": submitted_metadata.get("submission_marker_nonce"),
        "submission_marker_payload_sha256": submitted_metadata.get("submission_marker_payload_sha256"),
        "matched_candidate_index": capture_result["matched_candidate_index"],
        "matched_candidate_path": capture_result["matched_candidate_path"],
        "response_candidate_index": capture_result["response_candidate_index"],
        "response_candidate_path": capture_result["response_candidate_path"],
        "candidate_count": capture_result["candidate_count"],
        "stability": {
            "stable": capture_result["stable"],
            "stable_seconds": capture_result["stable_seconds"],
            "successful_polls": capture_result["successful_polls"],
            "poll_interval_seconds": capture_result["poll_interval_seconds"],
            "timeout_seconds": capture_result["timeout_seconds"],
        },
        "match_score": capture_result["match_score"],
        "capture_format": capture_result["capture_format"],
        "format_warning": capture_result["format_warning"],
        "ax_stats": capture_result["ax_stats"],
        "sentinel_state": capture_result.get("sentinel_state"),
        "reason_code": capture_result.get("reason_code"),
    }


def _prompt_output_path(run_id: str, output_path_text: str | None) -> Path:
    if output_path_text:
        return Path(output_path_text).expanduser()
    return Path("data") / "runs" / run_id / "next_codex_prompt.md"


def _write_prompt_artifact(output_path: Path, text: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def _extraction_event_metadata(
    selection: Any,
    extraction: Any,
    output_path: Path,
    warnings: tuple[str, ...],
    prompt_sha256: str,
) -> dict[str, Any]:
    return {
        "source_event_id": selection.source_event["id"] if selection.source_event else None,
        "source_event_type": "gpt_response_captured",
        "source_response_sha256": selection.response_sha256,
        "matched_submission_event_id": (
            selection.submitted_event["id"] if selection.submitted_event else None
        ),
        "extraction_method": extraction.extraction_method,
        "prompt_text": extraction.prompt_text,
        "prompt_path": str(output_path),
        "prompt_length": extraction.prompt_length,
        "prompt_sha256": prompt_sha256,
        "prompt_count_detected": extraction.prompt_count_detected,
        "selected_prompt_index": extraction.selected_prompt_index,
        "safety_status": extraction.safety_status,
        "warnings": list(warnings),
    }


def _result_from_extraction(
    run_id: str,
    selection: Any,
    extraction: Any,
    *,
    reason_code: str | None,
    error_message: str | None,
    warnings: tuple[str, ...],
    output_path: Path | None = None,
    artifact_written: bool = False,
    event_type: str | None = None,
    event_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    persisted: bool = False,
    prompt_sha256: str | None = None,
) -> ExtractNextCodexPromptServiceResult:
    source_event = selection.source_event if selection is not None else None
    submitted_event = selection.submitted_event if selection is not None else None
    selected_prompt_sha256 = prompt_sha256
    if selected_prompt_sha256 is None and extraction is not None:
        selected_prompt_sha256 = extraction.prompt_sha256
    return ExtractNextCodexPromptServiceResult(
        ok=reason_code is None,
        run_id=run_id,
        reason_code=reason_code,
        error_message=error_message,
        warnings=tuple(warnings),
        selection=selection,
        extraction=extraction,
        prompt_text=extraction.prompt_text if extraction is not None else "",
        prompt_sha256=selected_prompt_sha256 or "",
        prompt_length=extraction.prompt_length if extraction is not None else 0,
        extraction_method=extraction.extraction_method if extraction is not None else None,
        source_event_id=source_event.get("id") if isinstance(source_event, dict) else None,
        source_response_sha256=selection.response_sha256 if selection is not None else "",
        matched_submission_event_id=(
            submitted_event.get("id") if isinstance(submitted_event, dict) else None
        ),
        output_path=output_path,
        artifact_written=artifact_written,
        event_type=event_type,
        event_id=event_id,
        metadata=metadata,
        persisted=persisted,
    )


def _event_id(add_event_result: Any) -> int | None:
    if isinstance(add_event_result, int):
        return add_event_result
    if isinstance(add_event_result, dict):
        event_id = add_event_result.get("id")
        if isinstance(event_id, int):
            return event_id
    return None
