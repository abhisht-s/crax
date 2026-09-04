from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent import ledger as default_ledger


REMOTE_SESSION_COOKIE = "crax_session"
REMOTE_PAIRING_CODE_BYTES = 9
REMOTE_TOKEN_BYTES = 32
REMOTE_DEFAULT_PAIRING_TTL_SECONDS = 10 * 60
REMOTE_DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
REMOTE_FULL_ACCESS_CONFIRMATION = "ENABLE FULL ACCESS"
REMOTE_PAIRING_ATTEMPT_LIMIT = 10
REMOTE_PAIRING_ATTEMPT_WINDOW_SECONDS = 5 * 60
REMOTE_SCOPES = ("read", "control", "admin")
_SCOPE_RANK = {scope: index for index, scope in enumerate(REMOTE_SCOPES)}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Remote base URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Remote base URL must not include credentials, query, or fragment.")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


@dataclass(frozen=True)
class RemoteAccessConfig:
    public_base_url: str
    repository_roots: tuple[Path, ...] = ()
    allow_full_access: bool = False
    pairing_ttl_seconds: int = REMOTE_DEFAULT_PAIRING_TTL_SECONDS
    session_ttl_seconds: int = REMOTE_DEFAULT_SESSION_TTL_SECONDS

    def __post_init__(self) -> None:
        origin = _normalized_origin(self.public_base_url)
        roots = tuple(
            Path(root).expanduser().resolve(strict=False)
            for root in self.repository_roots
        )
        if self.pairing_ttl_seconds <= 0 or self.session_ttl_seconds <= 0:
            raise ValueError("Remote pairing and session TTLs must be positive.")
        object.__setattr__(self, "public_base_url", origin)
        object.__setattr__(self, "repository_roots", roots)

    @property
    def trusted_host(self) -> str:
        parsed = urlsplit(self.public_base_url)
        if parsed.port is None:
            return str(parsed.hostname)
        return f"{parsed.hostname}:{parsed.port}"

    @property
    def trusted_origin(self) -> str:
        return self.public_base_url


@dataclass(frozen=True)
class RemotePrincipal:
    kind: str
    scope: str
    device_id: str | None = None
    label: str | None = None

    def permits(self, required_scope: str) -> bool:
        return _SCOPE_RANK.get(self.scope, -1) >= _SCOPE_RANK.get(required_scope, 99)


@dataclass(frozen=True)
class PairingResult:
    ok: bool
    token: str | None = None
    device_id: str | None = None
    expires_at: str | None = None
    reason_code: str | None = None
    error_message: str | None = None


class RemoteAccessManager:
    def __init__(
        self,
        config: RemoteAccessConfig,
        *,
        ledger: Any = default_ledger,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self._pairing_attempts: list[float] = []
        self._rate_limit_lock = threading.Lock()

    def create_pairing_code(self) -> tuple[str, str]:
        code = secrets.token_urlsafe(REMOTE_PAIRING_CODE_BYTES)
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.config.pairing_ttl_seconds)
        pairing_id = self.ledger.create_remote_pairing_code(
            _sha256(code),
            _iso(now),
            _iso(expires_at),
        )
        return code, str(pairing_id)

    def pairing_url(self, code: str) -> str:
        return f"{self.config.public_base_url}/#pair={code}"

    def pair_device(self, code: object, label: object) -> PairingResult:
        if not self._allow_pairing_attempt():
            return PairingResult(
                ok=False,
                reason_code="pairing_rate_limited",
                error_message="Too many pairing attempts. Wait before trying again.",
            )
        code_text = str(code or "").strip()
        label_text = str(label or "").strip()
        if not code_text:
            return PairingResult(
                ok=False,
                reason_code="pairing_code_required",
                error_message="A pairing code is required.",
            )
        if not label_text or len(label_text) > 80:
            return PairingResult(
                ok=False,
                reason_code="invalid_device_label",
                error_message="Device label must contain 1 to 80 characters.",
            )
        token = secrets.token_urlsafe(REMOTE_TOKEN_BYTES)
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.config.session_ttl_seconds)
        result = self.ledger.consume_remote_pairing_code(
            code_sha256=_sha256(code_text),
            consumed_at=_iso(now),
            device_label=label_text,
            token_sha256=_sha256(token),
            scopes_json=json.dumps(list(REMOTE_SCOPES), separators=(",", ":")),
            device_expires_at=_iso(expires_at),
        )
        if not result.get("ok"):
            return PairingResult(
                ok=False,
                reason_code=str(result.get("reason_code") or "pairing_failed"),
                error_message=str(result.get("error_message") or "Pairing failed."),
            )
        return PairingResult(
            ok=True,
            token=token,
            device_id=str(result["device_id"]),
            expires_at=_iso(expires_at),
        )

    def _allow_pairing_attempt(self) -> bool:
        now = time.monotonic()
        cutoff = now - REMOTE_PAIRING_ATTEMPT_WINDOW_SECONDS
        with self._rate_limit_lock:
            self._pairing_attempts = [
                attempted_at
                for attempted_at in self._pairing_attempts
                if attempted_at > cutoff
            ]
            if len(self._pairing_attempts) >= REMOTE_PAIRING_ATTEMPT_LIMIT:
                return False
            self._pairing_attempts.append(now)
        return True

    def authenticate_cookie(self, token: str | None) -> RemotePrincipal | None:
        if not token:
            return None
        row = self.ledger.authenticate_remote_device(_sha256(token), _iso(_utc_now()))
        if not row:
            return None
        scopes = _scopes(row.get("scopes_json"))
        scope = max(scopes, key=lambda item: _SCOPE_RANK[item], default="read")
        return RemotePrincipal(
            kind="remote_device",
            scope=scope,
            device_id=str(row.get("id") or ""),
            label=str(row.get("label") or ""),
        )

    def list_devices(self) -> list[dict[str, Any]]:
        return self.ledger.list_remote_devices(_iso(_utc_now()))

    def revoke_device(self, device_id: str) -> bool:
        return bool(self.ledger.revoke_remote_device(device_id, _iso(_utc_now())))

    def rotate_device(self, device_id: str) -> PairingResult:
        token = secrets.token_urlsafe(REMOTE_TOKEN_BYTES)
        now = _utc_now()
        expires_at = now + timedelta(seconds=self.config.session_ttl_seconds)
        rotated = self.ledger.rotate_remote_device_token(
            device_id,
            _sha256(token),
            _iso(now),
            _iso(expires_at),
        )
        if not rotated:
            return PairingResult(
                ok=False,
                reason_code="device_not_active",
                error_message="The device credential could not be rotated.",
            )
        return PairingResult(
            ok=True,
            token=token,
            device_id=device_id,
            expires_at=_iso(expires_at),
        )

    def audit(
        self,
        principal: RemotePrincipal,
        *,
        method: str,
        path: str,
        outcome: str,
        run_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        if principal.kind != "remote_device":
            return
        self.ledger.add_remote_audit_event(
            created_at=_iso(_utc_now()),
            device_id=principal.device_id,
            device_label=principal.label,
            method=method,
            path=path,
            outcome=outcome,
            run_id=run_id,
            reason_code=reason_code,
        )

    def repository_allowed(self, value: object) -> bool:
        try:
            path = Path(str(value or "")).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if not path.is_dir() or not (path / ".git").exists():
            return False
        return any(path == root or root in path.parents for root in self.config.repository_roots)

    def list_repositories(self, query: str = "") -> list[dict[str, str]]:
        query_text = query.strip().lower()
        repositories: dict[str, dict[str, str]] = {}
        for root in self.config.repository_roots:
            for candidate in _repository_candidates(root):
                path_text = str(candidate)
                if query_text and query_text not in path_text.lower() and query_text not in candidate.name.lower():
                    continue
                repositories[path_text] = {
                    "name": candidate.name,
                    "path": path_text,
                    "root": str(root),
                }
        return [repositories[key] for key in sorted(repositories, key=str.lower)]


def _scopes(value: object) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(scope for scope in decoded if scope in REMOTE_SCOPES)


def _repository_candidates(root: Path, *, max_depth: int = 4) -> list[Path]:
    try:
        root = root.resolve(strict=True)
    except OSError:
        return []
    if not root.is_dir():
        return []
    if (root / ".git").exists():
        return [root]

    skip_names = {
        ".git",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "__pycache__",
    }
    found: list[Path] = []
    frontier = [(root, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth >= max_depth:
            continue
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name in skip_names:
                continue
            if child.name.startswith("."):
                continue
            if (child / ".git").exists():
                found.append(child.resolve(strict=False))
                continue
            frontier.append((child, depth + 1))
    return found
