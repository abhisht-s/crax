from __future__ import annotations

import ctypes
import hashlib
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from ctypes import POINTER, byref, c_bool, c_char_p, c_int, c_long, c_ulong, c_void_p, create_string_buffer
from dataclasses import dataclass


AX_CAPTURE_SOURCE = "chatgpt_desktop_ax"
AX_CAPTURE_FORMAT = "rendered_ax_text"
DEFAULT_CAPTURE_TIMEOUT_SECONDS = 60.0
DEFAULT_STABLE_SECONDS = 2.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_DEPTH = 18
DEFAULT_MAX_NODES = 1200
MATCH_THRESHOLD = 0.90
MIN_MATCH_TEXT_LENGTH = 80
BEGIN_SENTINEL = "BEGIN_NEXT_CODEX_PROMPT"
END_SENTINEL = "END_NEXT_CODEX_PROMPT"
POST_FEEDBACK_SUMMARY_LIMIT = 20
TEXT_PREVIEW_LIMIT = 80
SENTINEL_STATE_ANCHOR_PENDING = "anchor_pending"
SENTINEL_STATE_PENDING = "sentinel_pending"
SENTINEL_STATE_STREAMING_INCOMPLETE = "streaming_incomplete_sentinel"
SENTINEL_STATE_COMPLETE_UNSTABLE = "complete_sentinel_unstable"
SENTINEL_STATE_COMPLETE_STABLE = "complete_sentinel_stable"
SENTINEL_STATE_MALFORMED_UNSTABLE = "malformed_sentinel_unstable"
SENTINEL_STATE_MALFORMED_STABLE = "stable_malformed_sentinel"
SENTINEL_STATE_MULTIPLE_COMPLETE = "multiple_complete_sentinels"

TEXT_ROLES = {
    "AXStaticText",
    "AXTextArea",
    "AXTextField",
    "AXHeading",
}
TEXT_ATTRIBUTES = (
    "AXDescription",
    "AXValue",
    "AXTitle",
)
CONTAINER_ROLE = "AXGroup"
CONTAINER_SUBROLE = "AXHostingView"


class AXCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class AXNode:
    path: str
    depth: int
    role: str
    subrole: str
    text_parts: tuple[str, ...]
    ancestor_containers: tuple[str, ...]


@dataclass(frozen=True)
class TextCandidate:
    index: int
    path: str
    text: str
    text_node_paths: tuple[str, ...]


class _AXReader:
    def __init__(self, app_name: str, max_depth: int, max_nodes: int) -> None:
        if sys.platform != "darwin":
            raise AXCaptureError("ChatGPT desktop AX capture is only supported on macOS.")

        self.app_name = app_name
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._attr_cache: dict[str, int] = {}
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._cf.CFGetTypeID.argtypes = [c_void_p]
        self._cf.CFGetTypeID.restype = c_ulong
        self._cf.CFStringGetTypeID.restype = c_ulong
        self._cf.CFArrayGetTypeID.restype = c_ulong
        self._cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_ulong]
        self._cf.CFStringCreateWithCString.restype = c_void_p
        self._cf.CFStringGetLength.argtypes = [c_void_p]
        self._cf.CFStringGetLength.restype = c_long
        self._cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_ulong]
        self._cf.CFStringGetCString.restype = c_bool
        self._cf.CFArrayGetCount.argtypes = [c_void_p]
        self._cf.CFArrayGetCount.restype = c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        self._cf.CFArrayGetValueAtIndex.restype = c_void_p

        self._ax.AXUIElementCreateApplication.argtypes = [c_int]
        self._ax.AXUIElementCreateApplication.restype = c_void_p
        self._ax.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        self._ax.AXUIElementCopyAttributeValue.restype = c_int

        self._string_type = self._cf.CFStringGetTypeID()
        self._array_type = self._cf.CFArrayGetTypeID()

    def _attribute_ref(self, name: str) -> int:
        if name not in self._attr_cache:
            self._attr_cache[name] = self._cf.CFStringCreateWithCString(
                None,
                name.encode("utf-8"),
                0x08000100,
            )
        return self._attr_cache[name]

    def _copy_attribute(self, element: int, name: str) -> int | None:
        output = c_void_p()
        error = self._ax.AXUIElementCopyAttributeValue(
            c_void_p(element),
            c_void_p(self._attribute_ref(name)),
            byref(output),
        )
        if error != 0 or not output.value:
            return None
        return output.value

    def _cf_string(self, value: int | None) -> str:
        if not value:
            return ""
        try:
            if self._cf.CFGetTypeID(c_void_p(value)) != self._string_type:
                return ""
            length = self._cf.CFStringGetLength(c_void_p(value))
            buffer = create_string_buffer(max(16, length * 4 + 1))
            ok = self._cf.CFStringGetCString(c_void_p(value), buffer, len(buffer), 0x08000100)
            if not ok:
                return ""
            return buffer.value.decode("utf-8", "replace")
        except Exception:
            return ""

    def _children(self, element: int) -> list[int]:
        for name in ("AXChildren", "AXVisibleChildren"):
            value = self._copy_attribute(element, name)
            if not value:
                continue
            try:
                if self._cf.CFGetTypeID(c_void_p(value)) != self._array_type:
                    continue
                count = self._cf.CFArrayGetCount(c_void_p(value))
                if count > 0:
                    return [
                        self._cf.CFArrayGetValueAtIndex(c_void_p(value), index)
                        for index in range(count)
                    ]
            except Exception:
                continue
        return []

    def _get_pid(self) -> int:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                (
                    'tell application "System Events" to get unix id of first '
                    f'application process whose name is "{_applescript_string_inner(self.app_name)}"'
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            raise AXCaptureError(error or f"Could not find process for app {self.app_name!r}.")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise AXCaptureError(f"Could not parse process id for app {self.app_name!r}.") from exc

    def collect_text_candidates(self) -> tuple[list[TextCandidate], dict]:
        pid = self._get_pid()
        app_element = self._ax.AXUIElementCreateApplication(pid)
        if not app_element:
            raise AXCaptureError("Could not create AX application element.")

        focused_window = self._copy_attribute(app_element, "AXFocusedWindow")
        if not focused_window:
            raise AXCaptureError("Could not read AXFocusedWindow from ChatGPT.")

        nodes: list[AXNode] = []
        visited_count = 0

        def walk(element: int, path: str, depth: int, containers: tuple[str, ...]) -> None:
            nonlocal visited_count
            if visited_count >= self.max_nodes:
                return
            visited_count += 1

            role = self._cf_string(self._copy_attribute(element, "AXRole"))
            subrole = self._cf_string(self._copy_attribute(element, "AXSubrole"))
            next_containers = containers
            if role == CONTAINER_ROLE and subrole == CONTAINER_SUBROLE:
                next_containers = (*containers, path)

            text_parts: list[str] = []
            if role in TEXT_ROLES:
                for attribute in TEXT_ATTRIBUTES:
                    text = self._cf_string(self._copy_attribute(element, attribute)).strip()
                    if text and text not in text_parts:
                        text_parts.append(text)

            if text_parts:
                nodes.append(
                    AXNode(
                        path=path,
                        depth=depth,
                        role=role,
                        subrole=subrole,
                        text_parts=tuple(text_parts),
                        ancestor_containers=next_containers,
                    )
                )

            if depth >= self.max_depth:
                return

            for index, child in enumerate(self._children(element), start=1):
                if visited_count >= self.max_nodes:
                    break
                walk(child, f"{path}.{index}", depth + 1, next_containers)

        walk(focused_window, "FW", 0, ())

        candidates = _group_text_nodes(nodes)
        stats = {
            "pid": pid,
            "visited_nodes": visited_count,
            "text_node_count": len(nodes),
            "candidate_count": len(candidates),
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
        }
        return candidates, stats


def _applescript_string_inner(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _group_text_nodes(nodes: list[AXNode]) -> list[TextCandidate]:
    grouped: dict[str, dict] = {}

    for node in nodes:
        group_path = node.ancestor_containers[-1] if node.ancestor_containers else node.path
        if group_path not in grouped:
            grouped[group_path] = {
                "first_depth": node.depth,
                "parts": [],
                "node_paths": [],
            }
        for text in node.text_parts:
            if text:
                grouped[group_path]["parts"].append(text)
        grouped[group_path]["node_paths"].append(node.path)

    candidates: list[TextCandidate] = []
    for group_path, group in grouped.items():
        text = _join_visible_text(group["parts"])
        if not text:
            continue
        candidates.append(
            TextCandidate(
                index=len(candidates),
                path=group_path,
                text=text,
                text_node_paths=tuple(group["node_paths"]),
            )
        )

    return candidates


def _join_visible_text(parts: list[str]) -> str:
    result: list[str] = []
    previous = ""
    for part in parts:
        cleaned = part.strip()
        if not cleaned or cleaned == previous:
            continue
        result.append(cleaned)
        previous = cleaned
    return "\n\n".join(result).strip()


def normalize_for_match(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def _token_counter(text: str) -> Counter[str]:
    return Counter(re.findall(r"[a-z0-9_]+", normalize_for_match(text)))


def _token_coverage(needle: str, haystack: str) -> float:
    needle_counter = _token_counter(needle)
    if not needle_counter:
        return 0.0
    haystack_counter = _token_counter(haystack)
    overlap = sum((needle_counter & haystack_counter).values())
    return overlap / sum(needle_counter.values())


def _match_score(feedback_text: str, candidate_text: str) -> float:
    feedback_normalized = normalize_for_match(feedback_text)
    candidate_normalized = normalize_for_match(candidate_text)

    if len(candidate_normalized) < MIN_MATCH_TEXT_LENGTH:
        return 0.0
    if feedback_normalized and feedback_normalized in candidate_normalized:
        return 1.0
    if candidate_normalized and candidate_normalized in feedback_normalized:
        length_ratio = len(candidate_normalized) / max(len(feedback_normalized), 1)
        return 0.98 if length_ratio >= 0.80 else 0.0

    return _token_coverage(feedback_text, candidate_text)


def find_response_candidate(
    candidates: list[TextCandidate],
    feedback_text: str,
    threshold: float = MATCH_THRESHOLD,
    require_sentinel_response: bool = False,
) -> dict:
    matches = []
    for candidate in candidates:
        score = _match_score(feedback_text, candidate.text)
        if score >= threshold:
            matches.append((candidate, score))

    if not matches:
        return {
            "ok": False,
            "error": "Could not match the submitted GPT feedback in the visible ChatGPT AX text.",
            "matched_feedback": False,
            "match_count": 0,
        }
    if len(matches) > 1:
        return {
            "ok": False,
            "error": "Multiple visible ChatGPT AX text groups matched the submitted GPT feedback.",
            "matched_feedback": True,
            "match_count": len(matches),
        }

    matched_candidate, score = matches[0]
    post_feedback_candidates = candidates[matched_candidate.index + 1 :]
    post_feedback_summaries = _post_feedback_candidate_summaries(post_feedback_candidates)

    if require_sentinel_response:
        sentinel_candidates = [
            candidate
            for candidate in post_feedback_candidates
            if _sentinel_marker_status(candidate.text) == "valid_complete_sentinel"
        ]
        if len(sentinel_candidates) > 1:
            return {
                "ok": False,
                "error": (
                    "Multiple complete sentinel-wrapped assistant responses were found "
                    "after the matched feedback."
                ),
                "matched_feedback": True,
                "match_count": 1,
                "match_score": score,
                "matched_candidate": matched_candidate,
                "post_feedback_candidate_summaries": post_feedback_summaries,
                "sentinel_required": True,
                "fatal": True,
            }
        if not sentinel_candidates:
            return {
                "ok": False,
                "error": (
                    "No complete sentinel-wrapped assistant response was found after "
                    "the matched feedback."
                ),
                "matched_feedback": True,
                "match_count": 1,
                "match_score": score,
                "matched_candidate": matched_candidate,
                "post_feedback_candidate_summaries": post_feedback_summaries,
                "sentinel_required": True,
            }
        response_candidate = sentinel_candidates[0]
    else:
        response_candidate = None
        for candidate in post_feedback_candidates:
            if normalize_for_match(candidate.text):
                response_candidate = candidate
                break

    if response_candidate is None:
        return {
            "ok": False,
            "error": "Could not identify a following assistant response after the matched feedback.",
            "matched_feedback": True,
            "match_count": 1,
            "match_score": score,
            "matched_candidate_index": matched_candidate.index,
            "matched_candidate_path": matched_candidate.path,
            "post_feedback_candidate_summaries": post_feedback_summaries,
        }

    return {
        "ok": True,
        "matched_feedback": True,
        "match_count": 1,
        "match_score": score,
        "matched_candidate": matched_candidate,
        "response_candidate": response_candidate,
        "post_feedback_candidate_summaries": post_feedback_summaries,
        "sentinel_required": require_sentinel_response,
    }


def find_response_candidate_after_marker(
    candidates: list[TextCandidate],
    submission_marker_text: str,
    require_sentinel_response: bool = True,
) -> dict:
    marker_matches = [
        candidate for candidate in candidates if submission_marker_text in candidate.text
    ]
    if not marker_matches:
        return {
            "ok": False,
            "error": "Could not find the verified submission marker in the visible ChatGPT AX text.",
            "matched_feedback": False,
            "matched_submission_marker": False,
            "match_count": 0,
            "sentinel_required": require_sentinel_response,
            "sentinel_state": SENTINEL_STATE_ANCHOR_PENDING if require_sentinel_response else None,
            "reason_code": "anchor_pending",
            "provisional": True,
            "fatal": False,
        }
    if len(marker_matches) > 1:
        return {
            "ok": False,
            "error": "Multiple visible ChatGPT AX text groups contained the verified submission marker.",
            "matched_feedback": True,
            "matched_submission_marker": True,
            "match_count": len(marker_matches),
            "sentinel_required": require_sentinel_response,
            "reason_code": "multiple_submission_marker_candidates",
            "fatal": True,
        }

    anchor_candidate = marker_matches[0]
    post_anchor_candidates = candidates[anchor_candidate.index + 1 :]
    post_anchor_summaries = _post_feedback_candidate_summaries(post_anchor_candidates)

    if require_sentinel_response:
        sentinel_decision = _classify_post_anchor_sentinel_window(post_anchor_candidates)
        base = {
            "matched_feedback": True,
            "matched_submission_marker": True,
            "match_count": 1,
            "matched_candidate": anchor_candidate,
            "post_feedback_candidate_summaries": post_anchor_summaries,
            "sentinel_required": True,
            **sentinel_decision,
        }
        if sentinel_decision["sentinel_state"] == SENTINEL_STATE_MULTIPLE_COMPLETE:
            return {
                "ok": False,
                "error": (
                    "Multiple complete sentinel-wrapped assistant responses were found "
                    "after the verified submission marker."
                ),
                **base,
            }
        if sentinel_decision["sentinel_state"] == SENTINEL_STATE_MALFORMED_UNSTABLE:
            return {
                "ok": False,
                "error": "Malformed sentinel markers were found after the verified submission marker.",
                **base,
            }
        if sentinel_decision["sentinel_state"] == SENTINEL_STATE_STREAMING_INCOMPLETE:
            return {
                "ok": False,
                "error": (
                    "A sentinel-wrapped assistant response is still streaming after "
                    "the verified submission marker."
                ),
                **base,
            }
        if sentinel_decision["sentinel_state"] == SENTINEL_STATE_PENDING:
            return {
                "ok": False,
                "error": (
                    "No complete sentinel-wrapped assistant response was found after "
                    "the verified submission marker."
                ),
                **base,
            }
        response_candidate = sentinel_decision["response_candidate"]
    else:
        response_candidate = None
        for candidate in post_anchor_candidates:
            if normalize_for_match(candidate.text):
                response_candidate = candidate
                break

    if response_candidate is None:
        return {
            "ok": False,
            "error": "Could not identify a following assistant response after the verified submission marker.",
            "matched_feedback": True,
            "matched_submission_marker": True,
            "match_count": 1,
            "matched_candidate": anchor_candidate,
            "post_feedback_candidate_summaries": post_anchor_summaries,
            "sentinel_required": require_sentinel_response,
            "reason_code": "response_not_found",
        }

    return {
        "ok": True,
        "matched_feedback": True,
        "matched_submission_marker": True,
        "match_count": 1,
        "match_score": 1.0,
        "matched_candidate": anchor_candidate,
        "response_candidate": response_candidate,
        "post_feedback_candidate_summaries": post_anchor_summaries,
        "sentinel_required": require_sentinel_response,
        "sentinel_state": (
            SENTINEL_STATE_COMPLETE_UNSTABLE
            if require_sentinel_response
            else None
        ),
        "reason_code": (
            "complete_sentinel_observed"
            if require_sentinel_response
            else "response_candidate_observed"
        ),
        "provisional": False,
        "fatal": False,
    }


def capture_response_after_feedback(
    feedback_text: str,
    app_name: str = "ChatGPT",
    timeout_seconds: float = DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    stable_seconds: float = DEFAULT_STABLE_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    require_sentinel_response: bool = False,
    submission_marker_text: str | None = None,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    reader = _AXReader(app_name=app_name, max_depth=max_depth, max_nodes=max_nodes)
    last_stability_fingerprint: str | None = None
    last_response_text = ""
    stable_since: float | None = None
    successful_polls = 0
    last_error: str | None = None
    last_stats: dict = {}
    last_match: dict = {}
    last_observed_sentinel_state: str | None = None

    while time.monotonic() <= deadline:
        try:
            candidates, stats = reader.collect_text_candidates()
            last_stats = stats
            if submission_marker_text:
                match = find_response_candidate_after_marker(
                    candidates,
                    submission_marker_text,
                    require_sentinel_response=require_sentinel_response,
                )
            else:
                match = find_response_candidate(
                    candidates,
                    feedback_text,
                    require_sentinel_response=require_sentinel_response,
                )
            last_match = _match_summary(match)
            last_observed_sentinel_state = last_match.get("sentinel_state") or last_observed_sentinel_state
            if not match["ok"]:
                last_error = match["error"]
                if match.get("fatal"):
                    return {
                        "ok": False,
                        "source": AX_CAPTURE_SOURCE,
                        "capture_format": AX_CAPTURE_FORMAT,
                        "error": last_error,
                        "reason_code": match.get("reason_code") or "fatal_capture_error",
                        "matched_feedback": bool(last_match.get("matched_feedback", False)),
                        "candidate_count": stats["candidate_count"],
                        "stable": False,
                        "stable_seconds": stable_seconds,
                        "successful_polls": successful_polls,
                        "poll_interval_seconds": poll_interval_seconds,
                        "timeout_seconds": timeout_seconds,
                        "ax_stats": stats,
                        "sentinel_required": require_sentinel_response,
                        **last_match,
                    }
                if match.get("sentinel_state") == SENTINEL_STATE_MALFORMED_UNSTABLE:
                    malformed_fingerprint = _match_stability_fingerprint(match)
                    if malformed_fingerprint == last_stability_fingerprint:
                        successful_polls += 1
                        if stable_since is None:
                            stable_since = time.monotonic()
                    else:
                        successful_polls = 1
                        stable_since = time.monotonic()
                        last_stability_fingerprint = malformed_fingerprint
                    elapsed_stable_seconds = time.monotonic() - (stable_since or time.monotonic())
                    if successful_polls >= 2 and elapsed_stable_seconds >= stable_seconds:
                        return {
                            "ok": False,
                            "source": AX_CAPTURE_SOURCE,
                            "capture_format": AX_CAPTURE_FORMAT,
                            "error": last_error,
                            "reason_code": "sentinel_malformed_stable",
                            "matched_feedback": bool(last_match.get("matched_feedback", False)),
                            "candidate_count": stats["candidate_count"],
                            "stable": False,
                            "stable_seconds": stable_seconds,
                            "successful_polls": successful_polls,
                            "poll_interval_seconds": poll_interval_seconds,
                            "timeout_seconds": timeout_seconds,
                            "ax_stats": stats,
                            "sentinel_required": require_sentinel_response,
                            **last_match,
                            "sentinel_state": SENTINEL_STATE_MALFORMED_STABLE,
                            "reason_code": "sentinel_malformed_stable",
                        }
                else:
                    stable_since = None
                    last_stability_fingerprint = None
                    successful_polls = 0
            else:
                response_candidate: TextCandidate = match["response_candidate"]
                response_fingerprint = _match_stability_fingerprint(match)
                if response_fingerprint == last_stability_fingerprint:
                    successful_polls += 1
                    if stable_since is None:
                        stable_since = time.monotonic()
                else:
                    successful_polls = 1
                    stable_since = time.monotonic()
                    last_stability_fingerprint = response_fingerprint
                    last_response_text = response_candidate.text

                elapsed_stable_seconds = time.monotonic() - (stable_since or time.monotonic())
                if successful_polls >= 2 and elapsed_stable_seconds >= stable_seconds:
                    response_sha256 = hashlib.sha256(last_response_text.encode("utf-8")).hexdigest()
                    return {
                        "ok": True,
                        "source": AX_CAPTURE_SOURCE,
                        "capture_format": AX_CAPTURE_FORMAT,
                        "response_text": last_response_text,
                        "response_length": len(last_response_text),
                        "response_sha256": response_sha256,
                        "matched_feedback": True,
                        "matched_submission_marker": bool(submission_marker_text),
                        "matched_candidate_index": match["matched_candidate"].index,
                        "matched_candidate_path": match["matched_candidate"].path,
                        "response_candidate_index": response_candidate.index,
                        "response_candidate_path": response_candidate.path,
                        "candidate_count": stats["candidate_count"],
                        "stable": True,
                        "stable_seconds": stable_seconds,
                        "successful_polls": successful_polls,
                        "poll_interval_seconds": poll_interval_seconds,
                        "timeout_seconds": timeout_seconds,
                        "match_score": match["match_score"],
                        "ax_stats": stats,
                        "sentinel_required": require_sentinel_response,
                        "sentinel_state": (
                            SENTINEL_STATE_COMPLETE_STABLE
                            if require_sentinel_response
                            else match.get("sentinel_state")
                        ),
                        "reason_code": (
                            "complete_sentinel_stable"
                            if require_sentinel_response
                            else match.get("reason_code", "response_stable")
                        ),
                        "post_feedback_candidate_summaries": match.get(
                            "post_feedback_candidate_summaries",
                            [],
                        ),
                        "format_warning": (
                            "Captured text is rendered macOS Accessibility text; "
                            "Markdown and code formatting may be lossy."
                        ),
                    }
        except AXCaptureError as exc:
            last_error = str(exc)
            last_match = {}
        except Exception as exc:
            last_error = f"Unexpected AX capture error: {exc}"
            last_match = {}

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_seconds, remaining))

    timeout_reason_code = _timeout_reason_code(last_observed_sentinel_state, require_sentinel_response)
    return {
        "ok": False,
        "source": AX_CAPTURE_SOURCE,
        "capture_format": AX_CAPTURE_FORMAT,
        "error": last_error or "Response did not become stable before timeout.",
        "matched_feedback": bool(last_match.get("matched_feedback", False)),
        "candidate_count": last_stats.get("candidate_count", 0),
        "stable": False,
        "stable_seconds": stable_seconds,
        "successful_polls": successful_polls,
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_seconds": timeout_seconds,
        "ax_stats": last_stats,
        "sentinel_required": require_sentinel_response,
        **last_match,
        "reason_code": timeout_reason_code,
    }


def _match_summary(match: dict) -> dict:
    summary = {
        "matched_feedback": bool(match.get("matched_feedback", False)),
        "matched_submission_marker": bool(match.get("matched_submission_marker", False)),
        "match_count": match.get("match_count", 0),
    }
    if "matched_candidate" in match:
        summary["matched_candidate_index"] = match["matched_candidate"].index
        summary["matched_candidate_path"] = match["matched_candidate"].path
    if "response_candidate" in match:
        summary["response_candidate_index"] = match["response_candidate"].index
        summary["response_candidate_path"] = match["response_candidate"].path
    if "match_score" in match:
        summary["match_score"] = match["match_score"]
    if "post_feedback_candidate_summaries" in match:
        summary["post_feedback_candidate_summaries"] = match["post_feedback_candidate_summaries"]
    if "sentinel_required" in match:
        summary["sentinel_required"] = match["sentinel_required"]
    for key in (
        "sentinel_state",
        "reason_code",
        "provisional",
        "fatal",
        "sentinel_candidate_count",
        "malformed_sentinel_candidate_count",
    ):
        if key in match:
            summary[key] = match[key]
    return summary


def _classify_post_anchor_sentinel_window(candidates: list[TextCandidate]) -> dict:
    valid_sentinel_candidates = []
    malformed_sentinel_candidates = []
    streaming_incomplete_candidates = []
    marker_candidate_count = 0

    for candidate in candidates:
        status = _sentinel_marker_status(candidate.text)
        if status == "no_markers":
            continue
        marker_candidate_count += 1
        if status == "valid_complete_sentinel":
            valid_sentinel_candidates.append(candidate)
        elif status == "missing_end_marker":
            streaming_incomplete_candidates.append(candidate)
        else:
            malformed_sentinel_candidates.append(candidate)

    if len(valid_sentinel_candidates) > 1:
        return {
            "sentinel_state": SENTINEL_STATE_MULTIPLE_COMPLETE,
            "reason_code": "multiple_complete_sentinels",
            "provisional": False,
            "fatal": True,
            "sentinel_candidate_count": marker_candidate_count,
        }

    if malformed_sentinel_candidates:
        return {
            "sentinel_state": SENTINEL_STATE_MALFORMED_UNSTABLE,
            "reason_code": "sentinel_malformed_observed",
            "provisional": True,
            "fatal": False,
            "sentinel_candidate_count": marker_candidate_count,
            "malformed_sentinel_candidate_count": len(malformed_sentinel_candidates),
        }

    if len(valid_sentinel_candidates) == 1:
        return {
            "sentinel_state": SENTINEL_STATE_COMPLETE_UNSTABLE,
            "reason_code": "complete_sentinel_observed",
            "provisional": False,
            "fatal": False,
            "response_candidate": valid_sentinel_candidates[0],
            "sentinel_candidate_count": marker_candidate_count,
        }

    if streaming_incomplete_candidates:
        return {
            "sentinel_state": SENTINEL_STATE_STREAMING_INCOMPLETE,
            "reason_code": "streaming_incomplete_sentinel",
            "provisional": True,
            "fatal": False,
            "sentinel_candidate_count": marker_candidate_count,
        }

    return {
        "sentinel_state": SENTINEL_STATE_PENDING,
        "reason_code": "sentinel_pending",
        "provisional": True,
        "fatal": False,
        "sentinel_candidate_count": 0,
    }


def _match_stability_fingerprint(match: dict) -> str:
    parts = [
        str(match.get("sentinel_state") or ""),
        str(match.get("reason_code") or ""),
    ]
    response_candidate = match.get("response_candidate")
    if isinstance(response_candidate, TextCandidate):
        parts.extend(_candidate_fingerprint_parts(response_candidate))
    for summary in match.get("post_feedback_candidate_summaries", ()):
        if summary.get("omitted_count") is not None:
            parts.append(f"omitted:{summary.get('omitted_count')}")
            continue
        sentinel_status = summary.get("sentinel_status")
        if sentinel_status and sentinel_status != "no_markers":
            parts.extend(
                [
                    f"candidate:{summary.get('index')}",
                    str(summary.get("path") or ""),
                    str(summary.get("sha256") or ""),
                    str(sentinel_status),
                ]
            )
    return "\n".join(parts)


def _candidate_fingerprint_parts(candidate: TextCandidate) -> list[str]:
    return [
        f"candidate:{candidate.index}",
        candidate.path,
        hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
        _sentinel_marker_status(candidate.text),
    ]


def _timeout_reason_code(sentinel_state: str | None, require_sentinel_response: bool) -> str:
    if not require_sentinel_response:
        return "response_not_stable_timeout"
    if sentinel_state == SENTINEL_STATE_STREAMING_INCOMPLETE:
        return "sentinel_incomplete_timeout"
    if sentinel_state == SENTINEL_STATE_COMPLETE_UNSTABLE:
        return "response_not_stable_timeout"
    if sentinel_state == SENTINEL_STATE_MALFORMED_UNSTABLE:
        return "response_not_stable_timeout"
    return "sentinel_not_found_timeout"


def _sentinel_marker_status(text: str) -> str:
    begin_matches = list(re.finditer(re.escape(BEGIN_SENTINEL), text))
    end_matches = list(re.finditer(re.escape(END_SENTINEL), text))

    if not begin_matches and not end_matches:
        return "no_markers"
    if len(begin_matches) > 1 or len(end_matches) > 1:
        return "multiple_sentinel_pairs"
    if begin_matches and not end_matches:
        return "missing_end_marker"
    if end_matches and not begin_matches:
        return "missing_begin_marker"

    begin = begin_matches[0]
    end = end_matches[0]
    if begin.end() > end.start():
        return "end_before_begin"
    if not text[begin.end() : end.start()].strip():
        return "empty_sentinel_prompt"
    return "valid_complete_sentinel"


def _post_feedback_candidate_summaries(candidates: list[TextCandidate]) -> list[dict]:
    summaries = [_candidate_summary(candidate) for candidate in candidates[:POST_FEEDBACK_SUMMARY_LIMIT]]
    omitted_count = len(candidates) - len(summaries)
    if omitted_count > 0:
        summaries.append(
            {
                "omitted_count": omitted_count,
                "sentinel_status": "omitted",
            }
        )
    return summaries


def _candidate_summary(candidate: TextCandidate) -> dict:
    classification, reason = _candidate_classification(candidate.text)
    return {
        "index": candidate.index,
        "path": candidate.path,
        "length": len(candidate.text),
        "sha256": hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
        "text_preview_repr": _short_escaped_repr(candidate.text),
        "sentinel_status": _sentinel_marker_status(candidate.text),
        "candidate_classification": classification,
        "classification_reason": reason,
    }


def _candidate_classification(text: str) -> tuple[str, str]:
    normalized = normalize_for_match(text)
    if re.fullmatch(r"thought for \d+s", normalized):
        return "ui_status", "thought_duration"
    if normalized == "search the web":
        return "ui_chrome", "search_the_web"
    return "content", "not_known_ui_chrome"


def _short_escaped_repr(text: str, limit: int = TEXT_PREVIEW_LIMIT) -> str:
    preview = text[:limit]
    escaped = preview.encode("unicode_escape", "backslashreplace").decode("ascii")
    if len(text) > limit:
        escaped += "..."
    return f"'{escaped}'"
