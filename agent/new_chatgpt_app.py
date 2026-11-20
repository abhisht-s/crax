"""Preflight for the new ChatGPT desktop app (bundle ``com.openai.codex``).

Step 1 of ``docs/new-chatgpt-app/MIGRATION_PLAN.md``: find the new app and make it
automatable before any loop step touches it. This module is standalone and is
not wired into the supervision loop yet; the Classic ChatGPT path is unchanged.

Observed facts this relies on (``docs/new-chatgpt-app/NEW_CHATGPT_APP_AX_FINDINGS.md``):

- Both desktop apps present themselves as "ChatGPT", so the process is resolved
  by bundle id only.
- Web content accepts ``AXPress`` only while ``AXEnhancedUserInterface`` is true
  on the application element. Setting it reports ``-25208`` even though the value
  takes effect, so success is decided by reading the value back. The app resets
  it on restart, so it is set on every preflight.
- The web content (``AXWebArea`` -> top group -> ``#root``) is only attached while
  the window's fullscreen Space is shown; activating the app attaches it.
- Once attached, the tree stays readable in the background but **stops updating**
  while the window is not on screen (seen 2026-11-20: a sidebar ``AXPress`` in the
  background switched the chat, yet the AX tree kept showing the old chat until
  the app was brought forward). ``bring_to_front=True`` activates the app and
  waits for ``AXFrontmost`` so later reads see current state.
- The sidebar trigger "Switch mode, current mode: <mode>" says which product mode
  is active. The handoff must be in ChatGPT mode.
- Radix menus, popovers and dialogs mount as siblings of ``#root``. Focusing the
  composer dismisses them. Escape must never be used: while a reply is streaming
  it opens a "Stop this response?" dialog.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Protocol

from agent import new_chatgpt_labels as labels
from agent.mac_app_control import NEW_CHATGPT_BUNDLE_ID, activate_bundle_identifier

PREFLIGHT_METHOD = "ax_preflight_new_chatgpt"

ENHANCED_UI_ATTRIBUTE = "AXEnhancedUserInterface"
AX_ERROR_SUCCESS = 0
# Electron applies AXEnhancedUserInterface but reports kAXErrorNotImplemented.
AX_ERROR_NOT_IMPLEMENTED = -25208

# Accessible names live in agent/new_chatgpt_labels.py.
LABEL_MODE_TRIGGER_PREFIX = labels.MODE_TRIGGER.prefix()
LABEL_COMPOSER = labels.COMPOSER.text
REQUIRED_PRODUCT_MODE = labels.PRODUCT_MODE_CHATGPT

ROOT_DOM_ID = "root"
SIDEBAR_DOM_ID = "app-shell-sidebar"
COMPOSER_DOM_CLASS = "ProseMirror"
LIVE_REGION_SUBROLE = "AXApplicationStatus"

BLOCKING_OVERLAY_ROLES = frozenset({"AXMenu", "AXMenuItem", "AXSheet"})
BLOCKING_OVERLAY_SUBROLES = frozenset({"AXDialog", "AXApplicationDialog", "AXApplicationAlertDialog"})
BLOCKING_OVERLAY_ROLE_DESCRIPTIONS = frozenset({"menu", "dialog", "alert dialog"})

REASON_ACCESSIBILITY_NOT_TRUSTED = "accessibility_not_trusted"
REASON_NOT_RUNNING = "new_chatgpt_not_running"
REASON_NO_APP_ELEMENT = "new_chatgpt_no_app_element"
REASON_AX_NOT_ENABLED = "new_chatgpt_ax_not_enabled"
REASON_WINDOW_NOT_ATTACHED = "new_chatgpt_window_not_attached"
REASON_NOT_FRONTMOST = "new_chatgpt_not_frontmost"
REASON_OVERLAY_OPEN = "new_chatgpt_overlay_open"
REASON_MODE_UNKNOWN = "new_chatgpt_mode_unknown"
REASON_WRONG_MODE = "new_chatgpt_wrong_mode"
REASON_AX_ERROR = "new_chatgpt_ax_error"

DEFAULT_ATTACH_TIMEOUT_SECONDS = 5.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_OVERLAY_SETTLE_SECONDS = 0.3

_WEB_AREA_SEARCH_DEPTH = 12
_SHELL_SEARCH_DEPTH = 60
_SHELL_SEARCH_NODES = 20000
_OVERLAY_SCAN_DEPTH = 15
_OVERLAY_SCAN_NODES = 1000


class AXBackend(Protocol):
    """Minimal accessibility access. Element handles are opaque to callers.

    ``attribute`` returns Python values: ``str``, ``bool``, ``int``/``float``,
    an element handle, a ``list`` of those, or ``None`` when absent.
    ``set_bool``, ``set_string`` and ``perform`` return the raw AXError code.
    """

    def is_trusted(self) -> bool: ...

    def application(self, pid: int) -> Any: ...

    def attribute(self, element: Any, name: str) -> Any: ...

    def set_bool(self, element: Any, name: str, value: bool) -> int: ...

    def perform(self, element: Any, action: str) -> int: ...

    def set_string(self, element: Any, name: str, value: str) -> int: ...


@dataclass
class PreflightResult:
    ok: bool
    reason_code: str | None = None
    error: str | None = None
    method: str = PREFLIGHT_METHOD
    bundle_id: str = NEW_CHATGPT_BUNDLE_ID
    pid: int | None = None
    enhanced_ui_set_error: int | None = None
    enhanced_ui_enabled: bool | None = None
    bring_to_front: bool = False
    frontmost: bool | None = None
    activated: bool = False
    activation: dict | None = None
    attach_wait_seconds: float = 0.0
    web_content_attached: bool = False
    product_mode: str | None = None
    blocking_overlay_count: int = 0
    overlay_dismiss_attempted: bool = False
    nonblocking_overlay_count: int = 0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_new_chatgpt_pid() -> int | None:
    """Return the new ChatGPT app's pid, resolved by bundle id only."""

    from agent.chatgpt_ax_destination_snapshot import _pid_for_bundle_id

    return _pid_for_bundle_id(NEW_CHATGPT_BUNDLE_ID)


def preflight_new_chatgpt(
    *,
    backend: AXBackend | None = None,
    pid_resolver: Callable[[], int | None] = resolve_new_chatgpt_pid,
    activator: Callable[[str], dict] = activate_bundle_identifier,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    attach_timeout_seconds: float = DEFAULT_ATTACH_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    overlay_settle_seconds: float = DEFAULT_OVERLAY_SETTLE_SECONDS,
    bring_to_front: bool = False,
) -> PreflightResult:
    """Make the new ChatGPT app automatable, or fail closed with a reason code.

    Order: accessibility trust -> process by bundle id -> enhanced UI on (read
    back) -> web content attached (activating once if needed) -> stray menus
    and dialogs dismissed via composer focus -> product mode is ChatGPT.

    ``bring_to_front=True`` also activates the app when it is not frontmost and
    waits until it is, because the AX tree does not update while the window is
    off screen. Use it before any step that must observe the app reacting.
    """

    result = PreflightResult(ok=False, bring_to_front=bring_to_front)
    try:
        if backend is None:
            backend = CtypesAXBackend()
        _run_preflight(
            result,
            backend=backend,
            pid_resolver=pid_resolver,
            activator=activator,
            sleep=sleep,
            clock=clock,
            attach_timeout_seconds=attach_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            overlay_settle_seconds=overlay_settle_seconds,
            bring_to_front=bring_to_front,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed with evidence
        result.ok = False
        result.reason_code = REASON_AX_ERROR
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _run_preflight(
    result: PreflightResult,
    *,
    backend: AXBackend,
    pid_resolver: Callable[[], int | None],
    activator: Callable[[str], dict],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    attach_timeout_seconds: float,
    poll_interval_seconds: float,
    overlay_settle_seconds: float,
    bring_to_front: bool = False,
) -> None:
    if not backend.is_trusted():
        _fail(result, REASON_ACCESSIBILITY_NOT_TRUSTED, "This process is not trusted for macOS Accessibility.")
        return

    pid = pid_resolver()
    if pid is None or pid <= 0:
        _fail(result, REASON_NOT_RUNNING, f"No running process with bundle id {NEW_CHATGPT_BUNDLE_ID!r}.")
        return
    result.pid = pid

    app = backend.application(pid)
    if not app:
        _fail(result, REASON_NO_APP_ELEMENT, "Could not create the accessibility element for the app.")
        return

    result.enhanced_ui_set_error = backend.set_bool(app, ENHANCED_UI_ATTRIBUTE, True)
    result.enhanced_ui_enabled = backend.attribute(app, ENHANCED_UI_ATTRIBUTE) is True
    if not result.enhanced_ui_enabled:
        _fail(
            result,
            REASON_AX_NOT_ENABLED,
            f"{ENHANCED_UI_ATTRIBUTE} did not read back true "
            f"(set returned AXError {result.enhanced_ui_set_error}).",
        )
        return

    def frontmost() -> bool:
        return backend.attribute(app, "AXFrontmost") is True

    content = _web_content(backend, app)
    if content is None or (bring_to_front and not frontmost()):
        result.activated = True
        result.activation = activator(NEW_CHATGPT_BUNDLE_ID)
        started = clock()
        while True:
            content = _web_content(backend, app)
            elapsed = clock() - started
            ready = content is not None and (not bring_to_front or frontmost())
            if ready or elapsed >= attach_timeout_seconds:
                result.attach_wait_seconds = round(max(elapsed, 0.0), 3)
                break
            sleep(poll_interval_seconds)
    if content is None:
        _fail(
            result,
            REASON_WINDOW_NOT_ATTACHED,
            "The app window's web content (#root) did not attach to the accessibility tree.",
        )
        return
    result.web_content_attached = True
    if bring_to_front:
        result.frontmost = frontmost()
        if not result.frontmost:
            _fail(result, REASON_NOT_FRONTMOST, "The app did not become frontmost; its accessibility tree would be stale.")
            return
    _web_area, top_group, root = content

    blocking, nonblocking = _classify_overlays(backend, top_group)
    result.blocking_overlay_count = len(blocking)
    result.nonblocking_overlay_count = len(nonblocking)
    result.details["blocking_overlays"] = [_overlay_summary(backend, item) for item in blocking]
    result.details["nonblocking_overlays"] = [_overlay_summary(backend, item) for item in nonblocking]
    if blocking:
        result.overlay_dismiss_attempted = True
        composer = _find_composer(backend, root)
        if composer is None:
            _fail(result, REASON_OVERLAY_OPEN, "A menu or dialog is open and the composer was not found to dismiss it.")
            return
        result.details["composer_focus_set_error"] = backend.set_bool(composer, "AXFocused", True)
        sleep(overlay_settle_seconds)
        blocking, nonblocking = _classify_overlays(backend, top_group)
        result.blocking_overlay_count = len(blocking)
        result.nonblocking_overlay_count = len(nonblocking)
        if blocking:
            _fail(result, REASON_OVERLAY_OPEN, "A menu or dialog stayed open after focusing the composer.")
            return

    sidebar = _find_first(backend, root, lambda el: _dom_id(backend, el) == SIDEBAR_DOM_ID)
    if sidebar is None:
        _fail(result, REASON_MODE_UNKNOWN, "The app sidebar (#app-shell-sidebar) was not found.")
        result.details["sidebar_found"] = False
        return
    mode = _product_mode(backend, sidebar)
    result.product_mode = mode
    if mode is None:
        _fail(result, REASON_MODE_UNKNOWN, "The product mode switcher was not found in the sidebar.")
        return
    if mode != REQUIRED_PRODUCT_MODE:
        _fail(result, REASON_WRONG_MODE, f"The app is in {mode!r} mode; the handoff needs {REQUIRED_PRODUCT_MODE!r} mode.")
        return

    result.ok = True
    result.reason_code = None
    result.error = None


def _fail(result: PreflightResult, reason_code: str, error: str) -> None:
    result.ok = False
    result.reason_code = reason_code
    result.error = error


# --- tree helpers (backend-agnostic) -------------------------------------------------


def _children(backend: AXBackend, element: Any) -> list:
    children = backend.attribute(element, "AXChildren")
    return list(children) if isinstance(children, list) else []


def _string(backend: AXBackend, element: Any, name: str) -> str:
    value = backend.attribute(element, name)
    return value if isinstance(value, str) else ""


def _dom_id(backend: AXBackend, element: Any) -> str:
    return _string(backend, element, "AXDOMIdentifier")


def _dom_classes(backend: AXBackend, element: Any) -> list[str]:
    value = backend.attribute(element, "AXDOMClassList")
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _find_first(
    backend: AXBackend,
    root: Any,
    predicate: Callable[[Any], bool],
    *,
    max_depth: int = _SHELL_SEARCH_DEPTH,
    max_nodes: int = _SHELL_SEARCH_NODES,
) -> Any | None:
    queue: deque[tuple[Any, int]] = deque([(root, 0)])
    visited = 0
    while queue and visited < max_nodes:
        element, depth = queue.popleft()
        visited += 1
        if predicate(element):
            return element
        if depth < max_depth:
            queue.extend((child, depth + 1) for child in _children(backend, element))
    return None


def _iter_subtree(backend: AXBackend, root: Any, *, max_depth: int, max_nodes: int) -> Iterable[Any]:
    queue: deque[tuple[Any, int]] = deque([(root, 0)])
    visited = 0
    while queue and visited < max_nodes:
        element, depth = queue.popleft()
        visited += 1
        yield element
        if depth < max_depth:
            queue.extend((child, depth + 1) for child in _children(backend, element))


def _window(backend: AXBackend, app: Any) -> Any | None:
    for name in ("AXFocusedWindow", "AXMainWindow"):
        window = backend.attribute(app, name)
        if window and not isinstance(window, (str, bool, int, float, list)):
            return window
    windows = backend.attribute(app, "AXWindows")
    if isinstance(windows, list) and windows:
        return windows[0]
    return None


def _web_content(backend: AXBackend, app: Any) -> tuple[Any, Any, Any] | None:
    """Return ``(web_area, top_group, #root)`` when the web content is attached, else None."""

    window = _window(backend, app)
    if window is None:
        return None
    web_area = _find_first(
        backend,
        window,
        lambda el: _string(backend, el, "AXRole") == "AXWebArea",
        max_depth=_WEB_AREA_SEARCH_DEPTH,
        max_nodes=500,
    )
    if web_area is None:
        return None
    if backend.attribute(web_area, "AXLoaded") is False:
        return None
    web_children = _children(backend, web_area)
    if not web_children:
        return None
    top_group = web_children[0]
    for child in _children(backend, top_group):
        if _dom_id(backend, child) == ROOT_DOM_ID:
            return web_area, top_group, child
    return None


def _classify_overlays(backend: AXBackend, top_group: Any) -> tuple[list, list]:
    """Split ``#root``'s siblings into blocking (menus/dialogs) and other overlays.

    Live regions are ignored. Anything else that is not a menu or dialog is
    reported but does not block, so an unknown benign portal cannot wedge the loop.
    """

    blocking: list = []
    nonblocking: list = []
    for child in _children(backend, top_group):
        if _dom_id(backend, child) == ROOT_DOM_ID:
            continue
        if _string(backend, child, "AXSubrole") == LIVE_REGION_SUBROLE:
            continue
        if _is_blocking_overlay(backend, child):
            blocking.append(child)
        else:
            nonblocking.append(child)
    return blocking, nonblocking


def _overlay_summary(backend: AXBackend, overlay: Any) -> dict:
    """Structural description of an overlay for evidence. Never includes text content."""

    children = _children(backend, overlay)
    return {
        "role": _string(backend, overlay, "AXRole"),
        "subrole": _string(backend, overlay, "AXSubrole"),
        "role_description": _string(backend, overlay, "AXRoleDescription"),
        "dom_id": _dom_id(backend, overlay),
        "dom_classes": _dom_classes(backend, overlay)[:6],
        "child_count": len(children),
        "child_roles": [_string(backend, child, "AXRole") for child in children[:6]],
    }


def _is_blocking_overlay(backend: AXBackend, overlay: Any) -> bool:
    for element in _iter_subtree(backend, overlay, max_depth=_OVERLAY_SCAN_DEPTH, max_nodes=_OVERLAY_SCAN_NODES):
        if _string(backend, element, "AXRole") in BLOCKING_OVERLAY_ROLES:
            return True
        if _string(backend, element, "AXSubrole") in BLOCKING_OVERLAY_SUBROLES:
            return True
        if _string(backend, element, "AXRoleDescription").casefold() in BLOCKING_OVERLAY_ROLE_DESCRIPTIONS:
            return True
    return False


def _find_composer(backend: AXBackend, root: Any) -> Any | None:
    def is_composer(element: Any) -> bool:
        if _string(backend, element, "AXRole") != "AXTextArea":
            return False
        if COMPOSER_DOM_CLASS not in _dom_classes(backend, element):
            return False
        return LABEL_COMPOSER in (_string(backend, element, "AXTitle"), _string(backend, element, "AXDescription"))

    return _find_first(backend, root, is_composer)


def _product_mode(backend: AXBackend, sidebar: Any) -> str | None:
    for element in _iter_subtree(backend, sidebar, max_depth=_SHELL_SEARCH_DEPTH, max_nodes=_SHELL_SEARCH_NODES):
        if _string(backend, element, "AXRole") != "AXPopUpButton":
            continue
        for name in ("AXDescription", "AXTitle"):
            label = _string(backend, element, name)
            if label.startswith(LABEL_MODE_TRIGGER_PREFIX):
                mode = label[len(LABEL_MODE_TRIGGER_PREFIX):].strip()
                return mode or None
    return None


# --- real backend ----------------------------------------------------------------------


class AXElementRef:
    """Opaque handle for an ``AXUIElementRef`` owned by :class:`CtypesAXBackend`."""

    __slots__ = ("ref",)

    def __init__(self, ref: int) -> None:
        self.ref = ref

    def __repr__(self) -> str:
        return f"AXElementRef(0x{self.ref:x})"


class CtypesAXBackend:
    """macOS Accessibility via ctypes (no third-party dependencies)."""

    _UTF8 = 0x08000100
    _NUMBER_DOUBLE = 13
    _MESSAGING_TIMEOUT_SECONDS = 2.0

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("macOS Accessibility is only available on macOS.")
        import ctypes
        from ctypes import POINTER, c_bool, c_char_p, c_double, c_float, c_int, c_long, c_ulong, c_void_p

        self._ctypes = ctypes
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._cf, self._ax = cf, ax

        cf.CFGetTypeID.argtypes = [c_void_p]
        cf.CFGetTypeID.restype = c_ulong
        for name in ("CFStringGetTypeID", "CFBooleanGetTypeID", "CFNumberGetTypeID", "CFArrayGetTypeID"):
            getattr(cf, name).restype = c_ulong
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_ulong]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringGetLength.argtypes = [c_void_p]
        cf.CFStringGetLength.restype = c_long
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [c_long, c_ulong]
        cf.CFStringGetMaximumSizeForEncoding.restype = c_long
        cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_ulong]
        cf.CFStringGetCString.restype = c_bool
        cf.CFBooleanGetValue.argtypes = [c_void_p]
        cf.CFBooleanGetValue.restype = c_bool
        cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
        cf.CFNumberGetValue.restype = c_bool
        cf.CFArrayGetCount.argtypes = [c_void_p]
        cf.CFArrayGetCount.restype = c_long
        cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        cf.CFArrayGetValueAtIndex.restype = c_void_p
        cf.CFRetain.argtypes = [c_void_p]
        cf.CFRetain.restype = c_void_p
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None
        cf.CFArrayCreate.argtypes = [c_void_p, c_void_p, c_long, c_void_p]
        cf.CFArrayCreate.restype = c_void_p

        ax.AXIsProcessTrusted.restype = c_bool
        ax.AXUIElementGetTypeID.restype = c_ulong
        ax.AXUIElementCreateApplication.argtypes = [c_int]
        ax.AXUIElementCreateApplication.restype = c_void_p
        ax.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        ax.AXUIElementCopyAttributeValue.restype = c_int
        ax.AXUIElementSetAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]
        ax.AXUIElementSetAttributeValue.restype = c_int
        ax.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
        ax.AXUIElementPerformAction.restype = c_int
        ax.AXUIElementSetMessagingTimeout.argtypes = [c_void_p, c_float]
        ax.AXUIElementSetMessagingTimeout.restype = c_int
        ax.AXUIElementCreateSystemWide.restype = c_void_p
        ax.AXUIElementCopyMultipleAttributeValues.argtypes = [c_void_p, c_void_p, ctypes.c_uint32, POINTER(c_void_p)]
        ax.AXUIElementCopyMultipleAttributeValues.restype = c_int

        self._c_void_p = c_void_p
        self._c_double = c_double
        self._true = c_void_p.in_dll(cf, "kCFBooleanTrue").value
        self._false = c_void_p.in_dll(cf, "kCFBooleanFalse").value
        self._string_type = cf.CFStringGetTypeID()
        self._boolean_type = cf.CFBooleanGetTypeID()
        self._number_type = cf.CFNumberGetTypeID()
        self._array_type = cf.CFArrayGetTypeID()
        self._element_type = ax.AXUIElementGetTypeID()
        self._names: dict[str, int] = {}
        self._name_arrays: dict[tuple[str, ...], int] = {}
        self._array_callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeArrayCallBacks"))
        # Bound every AX call made by this process so a hung app cannot hang the loop.
        system_wide = ax.AXUIElementCreateSystemWide()
        if system_wide:
            ax.AXUIElementSetMessagingTimeout(system_wide, self._MESSAGING_TIMEOUT_SECONDS)

    def is_trusted(self) -> bool:
        return bool(self._ax.AXIsProcessTrusted())

    def application(self, pid: int) -> AXElementRef | None:
        app = self._ax.AXUIElementCreateApplication(pid)
        if not app:
            return None
        self._ax.AXUIElementSetMessagingTimeout(app, self._MESSAGING_TIMEOUT_SECONDS)
        return AXElementRef(app)

    def attribute(self, element: AXElementRef, name: str) -> Any:
        out = self._c_void_p()
        err = self._ax.AXUIElementCopyAttributeValue(element.ref, self._name(name), self._ctypes.byref(out))
        if err != AX_ERROR_SUCCESS or not out.value:
            return None
        return self._convert(out.value, owned=True)

    def attributes(self, element: AXElementRef, names: tuple[str, ...]) -> dict[str, Any]:
        """Read several attributes in one AX round trip. Missing ones map to None."""

        names = tuple(names)
        array = self._name_arrays.get(names)
        if array is None:
            refs = (self._c_void_p * len(names))(*(self._name(name) for name in names))
            array = self._cf.CFArrayCreate(None, refs, len(names), self._array_callbacks)
            self._name_arrays[names] = array
        out = self._c_void_p()
        err = self._ax.AXUIElementCopyMultipleAttributeValues(element.ref, array, 0, self._ctypes.byref(out))
        if err != AX_ERROR_SUCCESS or not out.value:
            return {name: None for name in names}
        try:
            values = []
            for index in range(self._cf.CFArrayGetCount(out.value)):
                item = self._cf.CFArrayGetValueAtIndex(out.value, index)
                values.append(self._convert(item, owned=False) if item else None)
        finally:
            self._cf.CFRelease(out.value)
        values += [None] * (len(names) - len(values))
        return dict(zip(names, values))

    def perform(self, element: AXElementRef, action: str) -> int:
        return int(self._ax.AXUIElementPerformAction(element.ref, self._name(action)))

    def set_string(self, element: AXElementRef, name: str, value: str) -> int:
        ref = self._cf.CFStringCreateWithCString(None, value.encode("utf-8"), self._UTF8)
        if not ref:
            return -25200  # kAXErrorFailure
        try:
            return int(self._ax.AXUIElementSetAttributeValue(element.ref, self._name(name), ref))
        finally:
            self._cf.CFRelease(ref)

    def set_bool(self, element: AXElementRef, name: str, value: bool) -> int:
        return int(
            self._ax.AXUIElementSetAttributeValue(element.ref, self._name(name), self._true if value else self._false)
        )

    def _name(self, name: str) -> int:
        ref = self._names.get(name)
        if ref is None:
            ref = self._cf.CFStringCreateWithCString(None, name.encode("utf-8"), self._UTF8)
            self._names[name] = ref
        return ref

    def _convert(self, ref: int, *, owned: bool) -> Any:
        type_id = self._cf.CFGetTypeID(ref)
        if type_id == self._element_type:
            if not owned:
                self._cf.CFRetain(ref)
            return AXElementRef(ref)  # kept alive for the caller
        try:
            if type_id == self._string_type:
                return self._string(ref)
            if type_id == self._boolean_type:
                return bool(self._cf.CFBooleanGetValue(ref))
            if type_id == self._number_type:
                number = self._c_double()
                if self._cf.CFNumberGetValue(ref, self._NUMBER_DOUBLE, self._ctypes.byref(number)):
                    value = number.value
                    return int(value) if value.is_integer() else value
                return None
            if type_id == self._array_type:
                count = self._cf.CFArrayGetCount(ref)
                items = []
                for index in range(count):
                    item = self._cf.CFArrayGetValueAtIndex(ref, index)
                    if item:
                        items.append(self._convert(item, owned=False))
                return items
            return None
        finally:
            if owned:
                self._cf.CFRelease(ref)

    def _string(self, ref: int) -> str:
        length = self._cf.CFStringGetLength(ref)
        size = self._cf.CFStringGetMaximumSizeForEncoding(length, self._UTF8) + 1
        buffer = self._ctypes.create_string_buffer(size)
        if not self._cf.CFStringGetCString(ref, buffer, size, self._UTF8):
            return ""
        return buffer.value.decode("utf-8", errors="replace")


if __name__ == "__main__":  # pragma: no cover - live check, run only when asked
    import json

    print(json.dumps(preflight_new_chatgpt().to_dict(), indent=2))
