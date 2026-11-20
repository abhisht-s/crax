"""Capture GPT's reply in the new ChatGPT desktop app (``com.openai.codex``).

Step 6 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``. Standalone; not wired into
the supervision loop yet. Read-only, except that preflight brings the app to the
front (the AX tree is stale off screen) and may press "Scroll to bottom" once if
the anchor turn is not in the tree.

1. Split the turn list into turns at the "You said:" / "ChatGPT said:" headings.
2. Anchor on the one user turn that contains the submission marker. With no
   marker (diagnostic use), anchor on the last user turn.
3. The reply is the assistant turn right after it. Its parts split into a
   **body** (has text) and a **footer** (only reply controls: Copy, Read aloud,
   Regenerate response, Rate response, ...).
4. Rebuild the body text:
   - code blocks (``AXCodeStyleGroup``): every text node joined exactly, so
     ``"\\n"`` nodes keep the lines;
   - paragraphs, headings, list items (with their ``AXListMarker``) and table
     rows: one line each;
   - text inside buttons ("Worked for 1m 37s", "Copy") is skipped.
   Nothing is trimmed or de-duplicated inside a block.
5. Finished = no "Stop" next to the composer, the reply footer has "Copy" and
   "Rate response", and the reply text was identical for N polls in a row.
6. The text then goes to the existing ``extract_next_codex_prompt_from_text``.
   A finished reply without a prompt ends the capture
   (``completed_without_prompt``); the loop no longer waits forever.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent import new_chatgpt_labels as labels
from agent.new_chatgpt_app import (
    COMPOSER_DOM_CLASS,
    AXBackend,
    CtypesAXBackend,
    PreflightResult,
    preflight_new_chatgpt,
)
from agent.new_chatgpt_reader import (
    ASSISTANT_ROLE,
    USER_ROLE,
    AXNodeData,
    Turn,
    locate_anchors,
    snapshot_region,
    split_turns,
)
from agent.new_chatgpt_submit import composer_controls
from agent.prompt_extraction import BEGIN_SENTINEL, END_SENTINEL, extract_next_codex_prompt_from_text

CAPTURE_METHOD = "ax_capture_reply_new_chatgpt"

OUTCOME_COMPLETE_WITH_PROMPT = "complete_with_prompt"
OUTCOME_COMPLETE_WITHOUT_PROMPT = "complete_without_prompt"
OUTCOME_FAILED = "failed"

REASON_ANCHOR_NOT_FOUND = "marker_turn_not_found"
REASON_ANCHOR_AMBIGUOUS = "marker_turn_ambiguous"
REASON_COMPLETED_WITHOUT_PROMPT = "completed_without_prompt"
REASON_PROMPT_MALFORMED = "sentinel_prompt_malformed"
REASON_TIMEOUT = "capture_timeout"
REASON_CANCELLED = "operator_cancelled"
REASON_UNREADABLE = "new_chatgpt_capture_tree_unreadable"

CONTROL_ROLES = frozenset({"AXButton", "AXPopUpButton", "AXCheckBox", "AXMenuButton", "AXRadioButton"})
FOOTER_LABELS = frozenset(
    {
        labels.COPY_RESPONSE.text,
        "Share",
        "Add to project sources",
        labels.READ_ALOUD.text,
        labels.REGENERATE.text,
        labels.RATE_RESPONSE.text,
        "More actions",
    }
)
CODE_SUBROLE = "AXCodeStyleGroup"

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_STABLE_POLLS = 2
DEFAULT_ANCHOR_MISSING_POLLS = 3


# --- text ---------------------------------------------------------------------------


def _is_code(node: AXNodeData) -> bool:
    return node.subrole == CODE_SUBROLE or node.role_description.casefold() == "code"


def _code_text(node: AXNodeData) -> str:
    return "".join(item.text_value for item in node.iter() if item.role == "AXStaticText")


def render_text(parts: tuple[AXNodeData, ...] | list[AXNodeData]) -> str:
    """Rebuild readable text from reply body parts, one line per block."""

    lines: list[str] = []
    buffer: list[str] = []
    prefix: list[str] = []  # pending list marker, kept until the item's text arrives

    def flush() -> None:
        text = "".join(buffer)
        if text.strip():
            lines.append("".join(prefix) + text)
            prefix.clear()
        buffer.clear()

    def emit(node: AXNodeData) -> None:
        if _is_code(node):
            flush()
            lines.append(_code_text(node).rstrip("\n"))
            return
        if node.role in CONTROL_ROLES:
            return
        if node.role == "AXStaticText":
            buffer.append(node.text_value)
            return
        if node.role == "AXListMarker":
            flush()
            prefix[:] = [node.text_value or node.label]
            return
        if node.role == "AXLink":
            for child in node.children:
                emit(child)
            return
        if node.role == "AXRow":
            flush()
            cells = [render_text([cell]).replace("\n", " ") for cell in node.children]
            lines.append(" | ".join(cell for cell in cells))
            return
        flush()
        for child in node.children:
            emit(child)
        flush()

    for part in parts:
        emit(part)
    flush()
    return "\n".join(lines)


# --- reading -------------------------------------------------------------------------


def _part_has_text(part: AXNodeData) -> bool:
    def walk(node: AXNodeData) -> bool:
        if node.role in CONTROL_ROLES:
            return False
        if node.role == "AXStaticText" and node.text_value.strip():
            return True
        return any(walk(child) for child in node.children)

    return walk(part)


def _controls_in(part: AXNodeData) -> list[str]:
    return [node.label for node in part.iter() if node.role in CONTROL_ROLES and node.label]


@dataclass(frozen=True)
class ReplyObservation:
    readable: bool
    anchor_count: int = 0
    reply_present: bool = False
    reply_text: str = ""
    streaming: bool = False
    footer_labels: tuple[str, ...] = ()
    later_user_turn: bool = False
    scroll_to_bottom: Any = None

    @property
    def footer_complete(self) -> bool:
        return labels.COPY_RESPONSE.text in self.footer_labels and labels.RATE_RESPONSE.text in self.footer_labels


def split_reply(turn: Turn) -> tuple[tuple[AXNodeData, ...], tuple[str, ...]]:
    """(body parts, footer control labels) for an assistant turn."""

    body: list[AXNodeData] = []
    footer: list[str] = []
    for part in turn.parts:
        if _part_has_text(part):
            body.append(part)
            continue
        controls = _controls_in(part)
        if controls and all(label in FOOTER_LABELS for label in controls):
            footer.extend(controls)
        elif controls:
            body.append(part)  # unknown controls stay with the body (their text is skipped)
    return tuple(body), tuple(footer)


def _user_text(turn: Turn) -> str:
    texts = turn.static_texts()
    return "\n".join(texts) if len(texts) > 1 else "".join(texts)


def read_reply(backend: AXBackend, app: Any, marker_text: str | None) -> ReplyObservation:
    anchors = locate_anchors(backend, app)
    panel = snapshot_region(backend, anchors.chat_panel)
    if panel.root is None or not panel.complete:
        return ReplyObservation(readable=False)

    composers = [
        node
        for node in panel.root.iter()
        if node.role == "AXTextArea" and COMPOSER_DOM_CLASS in node.dom_classes and node.has_label(labels.COMPOSER.text)
    ]
    streaming = False
    if len(composers) == 1:
        streaming = any(node.has_label(labels.STOP.text) for node in composer_controls(panel.root, composers[0]))
    scroll = panel.root.find_first(lambda node: node.role == "AXButton" and node.has_label(labels.SCROLL_TO_BOTTOM.text))

    turn_list = panel.root.find_first(
        lambda node: any(child.role == "AXHeading" and child.has_label(*labels.TURN_HEADINGS) for child in node.children)
    )
    if turn_list is None:
        return ReplyObservation(readable=True, streaming=streaming, scroll_to_bottom=scroll)
    turns = split_turns(turn_list)

    user_indexes = [
        index
        for index, turn in enumerate(turns)
        if turn.role == USER_ROLE and (marker_text is None or marker_text in _user_text(turn))
    ]
    if marker_text is None:
        user_indexes = user_indexes[-1:]
    if len(user_indexes) != 1:
        return ReplyObservation(readable=True, anchor_count=len(user_indexes), streaming=streaming, scroll_to_bottom=scroll)

    anchor = user_indexes[0]
    after = turns[anchor + 1 :]
    later_user = any(turn.role == USER_ROLE for turn in after)
    reply = after[0] if after and after[0].role == ASSISTANT_ROLE else None
    if reply is None:
        return ReplyObservation(readable=True, anchor_count=1, streaming=streaming, later_user_turn=later_user, scroll_to_bottom=scroll)
    body, footer = split_reply(reply)
    return ReplyObservation(
        readable=True,
        anchor_count=1,
        reply_present=True,
        reply_text=render_text(body),
        streaming=streaming,
        footer_labels=footer,
        later_user_turn=later_user,
        scroll_to_bottom=scroll,
    )


# --- capture loop ----------------------------------------------------------------------


@dataclass
class CaptureResult:
    ok: bool
    outcome: str = OUTCOME_FAILED
    reason_code: str | None = None
    error: str | None = None
    method: str = CAPTURE_METHOD
    preflight: dict | None = None
    reply_text: str = ""
    reply_length: int = 0
    reply_sha256: str = ""
    sentinel_status: str = "no_markers"
    prompt_text: str = ""
    prompt_length: int = 0
    prompt_sha256: str = ""
    extraction_error: str | None = None
    polls: int = 0
    wait_seconds: float = 0.0
    scrolled_to_bottom: bool = False
    last_state: dict = field(default_factory=dict)

    def to_dict(self, *, include_text: bool = True) -> dict:
        data = asdict(self)
        if not include_text:
            data.pop("reply_text")
            data.pop("prompt_text")
        return data


def capture_reply_new_chatgpt(
    marker_text: str | None = None,
    *,
    require_sentinel: bool = True,
    backend: AXBackend | None = None,
    preflight: Callable[..., PreflightResult] = preflight_new_chatgpt,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stable_polls: int = DEFAULT_STABLE_POLLS,
    anchor_missing_polls: int = DEFAULT_ANCHOR_MISSING_POLLS,
    should_stop: Callable[[], bool] | None = None,
) -> CaptureResult:
    """Wait for the reply to the marker turn to finish, then extract the next Codex prompt.

    ``timeout_seconds=None`` waits until the reply finishes or ``should_stop``
    returns True, like Classic capture (which has no elapsed-time deadline).
    """

    result = CaptureResult(ok=False)
    started = clock()
    try:
        if backend is None:
            backend = CtypesAXBackend()
        pre = preflight(backend=backend, sleep=sleep, clock=clock, bring_to_front=True)
        result.preflight = pre.to_dict()
        if not pre.ok:
            return _fail(result, pre.reason_code or "new_chatgpt_preflight_failed", pre.error or "Preflight failed.")
        app = backend.application(pre.pid)

        last_text: str | None = None
        same_count = 0
        missing_anchor = 0
        while True:
            if should_stop is not None and should_stop():
                return _fail(result, REASON_CANCELLED, "Capture was stopped by the operator.")
            if backend.attribute(app, "AXFrontmost") is False:
                pre = preflight(backend=backend, sleep=sleep, clock=clock, bring_to_front=True)
                if not pre.ok:
                    return _fail(result, pre.reason_code or "new_chatgpt_preflight_failed", pre.error or "Preflight failed.")

            observation = read_reply(backend, app, marker_text)
            result.polls += 1
            result.last_state = {
                "readable": observation.readable,
                "anchor_count": observation.anchor_count,
                "reply_present": observation.reply_present,
                "streaming": observation.streaming,
                "footer_complete": observation.footer_complete,
                "reply_length": len(observation.reply_text),
                "later_user_turn": observation.later_user_turn,
            }

            if observation.readable and observation.anchor_count > 1:
                return _fail(result, REASON_ANCHOR_AMBIGUOUS, f"{observation.anchor_count} user turns contain the marker.")
            if observation.readable and observation.anchor_count == 0:
                missing_anchor += 1
                if observation.scroll_to_bottom is not None and not result.scrolled_to_bottom:
                    backend.perform(observation.scroll_to_bottom.element, "AXPress")
                    result.scrolled_to_bottom = True
                elif missing_anchor >= anchor_missing_polls:
                    return _fail(result, REASON_ANCHOR_NOT_FOUND, "No user turn with the marker is in the conversation.")
            else:
                missing_anchor = 0

            finished = (
                observation.reply_present
                and not observation.streaming
                and observation.footer_complete
                and observation.reply_text.strip() != ""
            )
            if finished and observation.reply_text == last_text:
                same_count += 1
            elif finished:
                same_count = 1
            else:
                same_count = 0
            last_text = observation.reply_text if finished else None

            if same_count >= stable_polls:
                return _finish(result, observation.reply_text, require_sentinel, clock() - started)
            if timeout_seconds is not None and clock() - started >= timeout_seconds:
                result.wait_seconds = round(clock() - started, 3)
                return _fail(result, REASON_TIMEOUT, "The reply did not finish within the timeout.")
            sleep(poll_interval_seconds)
    except Exception as exc:  # noqa: BLE001 - fail closed with evidence
        return _fail(result, "new_chatgpt_ax_error", f"{type(exc).__name__}: {exc}")


def _finish(result: CaptureResult, text: str, require_sentinel: bool, waited: float) -> CaptureResult:
    result.wait_seconds = round(max(waited, 0.0), 3)
    result.reply_text = text
    result.reply_length = len(text)
    result.reply_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    has_markers = BEGIN_SENTINEL in text or END_SENTINEL in text
    extraction = extract_next_codex_prompt_from_text(text)
    if extraction.ok and extraction.extraction_method == "sentinel_block":
        result.sentinel_status = "valid_complete_sentinel"
        result.prompt_text = extraction.prompt_text
        result.prompt_length = extraction.prompt_length
        result.prompt_sha256 = extraction.prompt_sha256
        result.ok, result.outcome = True, OUTCOME_COMPLETE_WITH_PROMPT
        return result
    if has_markers:
        result.sentinel_status = "malformed"
        result.extraction_error = extraction.error
        return _fail(result, REASON_PROMPT_MALFORMED, extraction.error or "The prompt markers are malformed.")
    result.sentinel_status = "no_markers"
    result.outcome = OUTCOME_COMPLETE_WITHOUT_PROMPT  # finished, just no prompt
    if require_sentinel:
        return _fail(result, REASON_COMPLETED_WITHOUT_PROMPT, "The reply finished without a BEGIN/END_NEXT_CODEX_PROMPT block.")
    result.ok, result.outcome = True, OUTCOME_COMPLETE_WITHOUT_PROMPT
    return result


def _fail(result: CaptureResult, reason_code: str, error: str) -> CaptureResult:
    result.ok = False
    if result.outcome != OUTCOME_COMPLETE_WITHOUT_PROMPT:
        result.outcome = OUTCOME_FAILED
    result.reason_code = reason_code
    result.error = error
    return result


if __name__ == "__main__":  # pragma: no cover - live capture, read-only, run only when asked
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Capture the reply after a user turn in the open new-ChatGPT chat.")
    parser.add_argument("--marker", default=None, help="Text in the user turn to anchor on (default: the last user turn).")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--print-prompt", action="store_true", help="Also print the extracted prompt text.")
    args = parser.parse_args()
    captured = capture_reply_new_chatgpt(args.marker, timeout_seconds=args.timeout)
    out = captured.to_dict(include_text=False)
    out.pop("preflight", None)
    print(json.dumps(out, indent=2))
    if args.print_prompt and captured.prompt_text:
        print("----- extracted prompt -----")
        print(captured.prompt_text)
