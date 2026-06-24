from __future__ import annotations

import errno
import unittest
from unittest import mock

from tests import local_socket_test_support


class LocalSocketBindProbeTests(unittest.TestCase):
    def test_successful_bind_probe_returns_supported(self) -> None:
        socket_instance = mock.MagicMock()

        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value = socket_instance

            result = local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertTrue(result.supported)
        self.assertIsNone(result.skip_reason)
        socket_factory.assert_called_once_with(
            local_socket_test_support.socket.AF_INET,
            local_socket_test_support.socket.SOCK_STREAM,
        )
        socket_instance.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_permission_error_becomes_skippable(self) -> None:
        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = PermissionError(
                errno.EPERM,
                "Operation not permitted",
            )

            result = local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertFalse(result.supported)
        self.assertEqual(
            result.skip_reason,
            local_socket_test_support.LOCAL_SOCKET_BIND_SKIP_REASON,
        )

    def test_operation_not_permitted_errno_becomes_skippable(self) -> None:
        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(
                errno.EPERM,
                "Operation not permitted",
            )

            result = local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertFalse(result.supported)
        self.assertEqual(
            result.skip_reason,
            local_socket_test_support.LOCAL_SOCKET_BIND_SKIP_REASON,
        )

    def test_permission_denied_errno_becomes_skippable(self) -> None:
        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(
                errno.EACCES,
                "Permission denied",
            )

            result = local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertFalse(result.supported)
        self.assertEqual(
            result.skip_reason,
            local_socket_test_support.LOCAL_SOCKET_BIND_SKIP_REASON,
        )

    def test_address_in_use_is_not_silently_skipped(self) -> None:
        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(
                errno.EADDRINUSE,
                "Address already in use",
            )

            with self.assertRaises(OSError) as raised:
                local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)

    def test_invalid_address_is_not_silently_skipped(self) -> None:
        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(
                errno.EADDRNOTAVAIL,
                "Cannot assign requested address",
            )

            with self.assertRaises(OSError) as raised:
                local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertEqual(raised.exception.errno, errno.EADDRNOTAVAIL)

    def test_arbitrary_oserror_is_not_silently_skipped(self) -> None:
        with mock.patch("tests.local_socket_test_support.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(
                errno.EIO,
                "I/O error",
            )

            with self.assertRaises(OSError) as raised:
                local_socket_test_support.probe_localhost_ephemeral_bind()

        self.assertEqual(raised.exception.errno, errno.EIO)


if __name__ == "__main__":
    unittest.main()
