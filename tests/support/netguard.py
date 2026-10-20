"""Socket guard used by the test suite and inherited by child Pythons.

Only AF_UNIX sockets and literal loopback addresses (``127.0.0.1`` and
``::1``) are allowed. DNS resolution of any other host is refused, so a test
cannot accidentally reach the network even through a library that resolves
names before connecting.
"""

from __future__ import annotations

import socket
from typing import Any

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1"})
_INSTALL_MARKER = "_deepsearch_axi_netguard_installed"


class NetworkBlocked(OSError):
    """Raised when code under test attempts non-loopback network access."""


def _host_of(address: Any) -> Any:
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            return host.decode("ascii", "replace")
        return host
    return None


def _blocked(address: Any) -> NetworkBlocked:
    return NetworkBlocked(f"network access blocked by the test guard: {address!r}")


def install() -> None:
    """Patch socket entry points once per process."""
    if getattr(socket, _INSTALL_MARKER, False):
        return
    setattr(socket, _INSTALL_MARKER, True)

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_getaddrinfo = socket.getaddrinfo
    original_create_connection = socket.create_connection

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family == getattr(socket, "AF_UNIX", None):
            return original_connect(self, address)
        if _host_of(address) in _ALLOWED_HOSTS:
            return original_connect(self, address)
        raise _blocked(address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family == getattr(socket, "AF_UNIX", None):
            return original_connect_ex(self, address)
        if _host_of(address) in _ALLOWED_HOSTS:
            return original_connect_ex(self, address)
        raise _blocked(address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> Any:
        if args:
            address = args[-1]
            if not isinstance(address, (str, bytes)) and _host_of(address) not in _ALLOWED_HOSTS:
                raise _blocked(address)
        return original_sendto(self, data, *args)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is None:
            return original_getaddrinfo(host, port, *args, **kwargs)
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if name in _ALLOWED_HOSTS:
            return original_getaddrinfo(host, port, *args, **kwargs)
        raise _blocked((host, port))

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if _host_of(address) not in _ALLOWED_HOSTS:
            raise _blocked(address)
        return original_create_connection(address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    socket.create_connection = create_connection  # type: ignore[assignment]
