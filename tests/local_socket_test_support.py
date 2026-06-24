from __future__ import annotations

import errno
import functools
import socket
import unittest
from dataclasses import dataclass


LOCAL_SOCKET_BIND_SKIP_REASON = "Local socket binding is prohibited by this execution environment."

_LOCAL_SOCKET_BIND_DENIED_ERRNOS = {
    errno.EACCES,
    errno.EPERM,
}


@dataclass(frozen=True)
class LocalSocketBindProbeResult:
    supported: bool
    skip_reason: str | None = None


def is_environment_denied_bind_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and exc.errno in _LOCAL_SOCKET_BIND_DENIED_ERRNOS


def probe_localhost_ephemeral_bind() -> LocalSocketBindProbeResult:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except OSError as exc:
        if is_environment_denied_bind_error(exc):
            return LocalSocketBindProbeResult(
                supported=False,
                skip_reason=LOCAL_SOCKET_BIND_SKIP_REASON,
            )
        raise
    return LocalSocketBindProbeResult(supported=True)


def require_localhost_ephemeral_bind() -> None:
    result = probe_localhost_ephemeral_bind()
    if not result.supported:
        raise unittest.SkipTest(result.skip_reason or LOCAL_SOCKET_BIND_SKIP_REASON)


def requires_localhost_ephemeral_bind(test_method):
    @functools.wraps(test_method)
    def wrapper(*args, **kwargs):
        require_localhost_ephemeral_bind()
        return test_method(*args, **kwargs)

    return wrapper


class LocalSocketBindingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        require_localhost_ephemeral_bind()
