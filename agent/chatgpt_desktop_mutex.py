from __future__ import annotations

import json
import os
import secrets
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent.codex_invocation import (
    boot_identity,
    pid_is_alive,
    process_group_id,
    process_start_identity,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - production is macOS
    fcntl = None  # type: ignore[assignment]


DEFAULT_CHATGPT_DESKTOP_MUTEX_PATH = (
    Path.home() / "Library" / "Application Support" / "crax" / "chatgpt-desktop.lock"
)


def controller_process_is_live(identity: dict[str, Any] | None) -> bool:
    """Return True only when boot, pid, start identity, and pgid all match a live process."""

    if not isinstance(identity, dict):
        return False
    pid = identity.get("pid")
    if not isinstance(pid, int) or not pid_is_alive(pid):
        return False
    expected_boot = identity.get("boot_id")
    if expected_boot != boot_identity():
        return False
    expected_start = identity.get("process_start_identity") or identity.get("start_identity")
    live_start = process_start_identity(pid)
    if not expected_start or live_start != expected_start:
        return False
    expected_pgid = identity.get("pgid")
    if isinstance(expected_pgid, int):
        live_pgid = process_group_id(pid)
        if live_pgid != expected_pgid:
            return False
    return True


def handoff_claim_owner_identifier(
    run_id: str,
    *,
    controller_instance_id: str | None = None,
) -> str:
    pid = os.getpid()
    instance = controller_instance_id or f"process-{pid}"
    start = process_start_identity(pid) or "start-unknown"
    return (
        f"controller:{instance}|boot:{boot_identity()}|pid:{pid}"
        f"|start:{start}|run:{run_id}"
    )


def capture_mutex_identity(
    *,
    owning_run_id: str,
    controller_instance_id: str | None = None,
    mutex_token: str | None = None,
) -> dict[str, Any]:
    pid = os.getpid()
    pgid = process_group_id(pid)
    return {
        "controller_instance_id": controller_instance_id or f"process-{pid}",
        "boot_id": boot_identity(),
        "pid": pid,
        "pgid": pgid if pgid is not None else pid,
        "process_start_identity": process_start_identity(pid),
        "owning_run_id": owning_run_id,
        "mutex_token": mutex_token or secrets.token_urlsafe(16),
    }


@dataclass
class ChatGPTDesktopMutexHold:
    ok: bool
    identity: dict[str, Any] = field(default_factory=dict)
    reason_code: str | None = None
    error_message: str | None = None
    active_owner: dict[str, Any] | None = None
    owner_is_live: bool | None = None
    _release: Callable[[], None] | None = field(default=None, repr=False)

    def release(self) -> None:
        releaser = self._release
        self._release = None
        if releaser is not None:
            releaser()


class ChatGPTDesktopMutex:
    """Machine-scoped exclusive lock for Classic ChatGPT Desktop."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_CHATGPT_DESKTOP_MUTEX_PATH

    def acquire(
        self,
        owning_run_id: str,
        *,
        controller_instance_id: str | None = None,
    ) -> ChatGPTDesktopMutexHold:
        if fcntl is None:
            return ChatGPTDesktopMutexHold(
                ok=False,
                reason_code="chatgpt_desktop_mutex_unavailable",
                error_message="fcntl is required for the ChatGPT Desktop machine mutex.",
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            active_owner = _read_mutex_identity(handle)
            handle.close()
            return ChatGPTDesktopMutexHold(
                ok=False,
                reason_code="chatgpt_desktop_mutex_already_held",
                error_message="ChatGPT Desktop is owned by another live process.",
                active_owner=active_owner,
                owner_is_live=controller_process_is_live(active_owner),
            )
        except OSError as exc:
            handle.close()
            return ChatGPTDesktopMutexHold(
                ok=False,
                reason_code="chatgpt_desktop_mutex_acquire_failed",
                error_message=str(exc),
            )

        identity = capture_mutex_identity(
            owning_run_id=owning_run_id,
            controller_instance_id=controller_instance_id,
        )
        try:
            _write_mutex_identity(handle, identity)
        except OSError as exc:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            return ChatGPTDesktopMutexHold(
                ok=False,
                reason_code="chatgpt_desktop_mutex_acquire_failed",
                error_message=str(exc),
            )

        def _release() -> None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass

        return ChatGPTDesktopMutexHold(
            ok=True,
            identity=identity,
            owner_is_live=True,
            _release=_release,
        )


class NullChatGPTDesktopMutex:
    """Test double that never excludes another holder."""

    def acquire(
        self,
        owning_run_id: str,
        *,
        controller_instance_id: str | None = None,
    ) -> ChatGPTDesktopMutexHold:
        del controller_instance_id
        return ChatGPTDesktopMutexHold(
            ok=True,
            identity={"owning_run_id": owning_run_id, "mutex_token": str(uuid.uuid4())},
            owner_is_live=True,
        )


def _read_mutex_identity(handle: Any) -> dict[str, Any] | None:
    try:
        handle.seek(0)
        raw = handle.read()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _write_mutex_identity(handle: Any, identity: dict[str, Any]) -> None:
    encoded = json.dumps(identity, indent=2, sort_keys=True)
    encoded = encoded if encoded.endswith("\n") else f"{encoded}\n"
    handle.seek(0)
    handle.truncate()
    handle.write(encoded)
    handle.flush()
    os.fsync(handle.fileno())
