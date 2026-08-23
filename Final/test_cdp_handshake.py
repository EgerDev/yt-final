"""The CDP WebSocket handshake must accept what real browsers actually send.

_ws_handshake required the status line to equal the literal "HTTP/1.1 101 Switching Protocols".
No shipping Chromium sends that. Captured from the DevTools endpoint of three browsers installed on
the development host:

    Chrome/150.0.7871.187  -> HTTP/1.1 101 WebSocket Protocol Handshake
    Edg/151.0.4129.59      -> HTTP/1.1 101 WebSocket Protocol Handshake
    Chrome/151.0.7922.71   -> HTTP/1.1 101 WebSocket Protocol Handshake   (Brave)

So the entire CDP cookie tier failed closed on every machine that has a browser installed — and,
because the failure happens after the browser has already been launched on a snapshot of the real
profile, each attempt also exercised the temp-profile cleanup path for nothing.

RFC 6455 4.1 requires the client to validate the 101 STATUS CODE. The reason phrase is arbitrary
text and carries no security meaning; the anti-hijack gate is the Sec-WebSocket-Accept HMAC, which
these tests also pin so relaxing the phrase check cannot be mistaken for relaxing the real one.

These tests speak the protocol against a local socket rather than launching a browser, so they run
everywhere — including hosts with no Chromium, where a browser-dependent test would silently skip
and leave this regression uncovered.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time

import pytest

try:
    from . import _cdp_client
except ImportError:
    import _cdp_client


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _serve_one_handshake(status_line, *, accept_override=None):
    """A one-shot listener that answers a websocket upgrade with `status_line`.

    Returns (host, port, thread). Computes a genuine Sec-WebSocket-Accept from the client's key
    unless accept_override is given, so the ONLY variable under test is the status line.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def run():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            request = b""
            while b"\r\n\r\n" not in request and len(request) < 8192:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                request += chunk
            key = ""
            for line in request.decode("latin-1").split("\r\n"):
                name, sep, value = line.partition(":")
                if sep and name.strip().lower() == "sec-websocket-key":
                    key = value.strip()
            accept = accept_override or base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            conn.sendall(
                (
                    f"{status_line}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            time.sleep(0.2)          # let the client finish reading before the socket closes
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return host, port, thread


def _handshake(status_line, *, accept_override=None):
    host, port, thread = _serve_one_handshake(status_line, accept_override=accept_override)
    try:
        sock = _cdp_client._ws_handshake(
            host, port, "/devtools/browser/probe", deadline=time.monotonic() + 10
        )
        sock.close()
        return True
    finally:
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "status_line",
    [
        # what every shipping Chromium actually sends
        "HTTP/1.1 101 WebSocket Protocol Handshake",
        # the RFC's own example phrasing, which the old check demanded
        "HTTP/1.1 101 Switching Protocols",
        # an unusual but still-valid phrase: the code is what matters
        "HTTP/1.1 101 Upgrade",
    ],
)
def test_any_101_reason_phrase_is_accepted(status_line):
    assert _handshake(status_line) is True, (
        "a valid 101 was rejected over its reason phrase: " + status_line
    )


@pytest.mark.parametrize(
    "status_line",
    [
        "HTTP/1.1 200 OK",              # not an upgrade at all
        "HTTP/1.1 400 Bad Request",
        "HTTP/1.1 1010 Switching Protocols",   # prefix of 101 must not pass
        "HTTP/1.0 101 Switching Protocols",    # version stays pinned
        "GARBAGE",
    ],
)
def test_non_101_status_lines_are_still_rejected(status_line):
    """Relaxing the phrase must not relax the code, the version, or the parse."""
    with pytest.raises(ValueError):
        _handshake(status_line)


def test_a_wrong_sec_websocket_accept_is_still_rejected():
    """The real anti-hijack gate. If this ever stops biting, the phrase relaxation above would have
    turned a security check into nothing — pinned here so the two can never be confused."""
    with pytest.raises(ValueError):
        _handshake(
            "HTTP/1.1 101 WebSocket Protocol Handshake",
            accept_override=base64.b64encode(b"x" * 20).decode("ascii"),
        )
