"""Direct unit tests for the local DPI-desync proxy's pure, byte-level logic.
No network: synthetic TLS ClientHellos and a fake socket. These are the smoke alarm for the
tool's highest-novelty code (SNI parsing + SOCKS5/HTTP-CONNECT parsing + desync framing)."""
import ssl
import socket

import pytest

import desync_proxy as dp


def _client_hello(host="rr7---sn-8xgp1vo-a5ml.googlevideo.com"):
    """Generate a real TLS ClientHello for `host` via a memory BIO (no socket)."""
    ctx = ssl.create_default_context()
    inb, outb = ssl.MemoryBIO(), ssl.MemoryBIO()
    s = ctx.wrap_bio(inb, outb, server_hostname=host)
    try:
        s.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outb.read(), host


class _FakeSock:
    """Minimal blocking-socket stand-in: recv() consumes an in-memory buffer, sends accumulate."""
    def __init__(self, data=b""):
        self.buf = bytearray(data)
        self.sent = bytearray()

    def recv(self, n, flags=0):
        if not self.buf:
            return b""
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def sendall(self, b):
        self.sent += b

    def send(self, b, flags=0):
        self.sent += b
        return len(b)

    def setsockopt(self, *a, **k):
        pass


def test_sni_range_finds_hostname():
    data, host = _client_hello()
    rng = dp.sni_range(data)
    assert rng is not None
    hs, he = rng
    assert data[hs:he] == host.encode()


def test_sni_range_none_on_non_tls_or_short():
    assert dp.sni_range(b"GET / HTTP/1.1\r\n\r\n") is None
    assert dp.sni_range(b"\x16\x03") is None                 # too short to be a ClientHello
    assert dp.sni_range(b"") is None


def test_http_connect_parse():
    fs = _FakeSock(b"CONNECT rr7---sn.googlevideo.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
    host, port, ok, _pipelined = dp.http_connect(fs)
    assert host == "rr7---sn.googlevideo.com" and port == 443
    assert b"200" in ok


def test_http_connect_strips_ipv6_brackets():
    """A spec-valid IPv6-literal CONNECT target ([addr]:port) must yield the BARE address so it reaches
    inet_pton / create_connection; leaving the brackets makes the host unroutable (DoH name query fails
    and getaddrinfo rejects '[...]')."""
    fs = _FakeSock(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\nHost: x\r\n\r\n")
    host, port, ok, _pipelined = dp.http_connect(fs)
    assert host == "2001:db8::1" and port == 443, (host, port)
    assert b"200" in ok


def test_http_connect_rejects_malformed_target_as_oserror():
    """CONFIRMED: a CONNECT target with no colon (or a non-numeric port) made int(port) raise
    ValueError — which handle()'s `except OSError` does NOT catch, so every such request dumped an
    unhandled-thread traceback and the client got no reply at all. Malformed input must fail the
    same way every other bad-request path here does: an OSError, after telling the client."""
    for target in (b"example.com", b"example.com:notaport", b"example.com:99999", b"example.com:-1"):
        fs = _FakeSock(b"CONNECT " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n")
        with pytest.raises(OSError):
            dp.http_connect(fs)
        assert b"400" in bytes(fs.sent), (target, bytes(fs.sent))


def test_http_connect_returns_bytes_pipelined_behind_the_header():
    """A client may pipeline its TLS ClientHello with CONNECT (saving an RTT). http_connect read
    past the header terminator into its own buffer and DISCARDED the surplus, while handle() then
    waited in recv() for a write that had already happened — the tunnel hung until timeout."""
    hello = b"\x16\x03\x01\x00\x05hello"
    fs = _FakeSock(b"CONNECT example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n" + hello)
    host, port, ok, surplus = dp.http_connect(fs)
    assert (host, port) == ("example.com", 443)
    assert surplus == hello, surplus


def test_socks5_connect_parse():
    host = b"example.com"
    greeting = b"\x05\x01\x00"                               # ver, nmethods=1, method=none
    request = b"\x05\x01\x00\x03" + bytes([len(host)]) + host + b"\x01\xbb"  # CONNECT domain :443
    fs = _FakeSock(greeting + request)
    h, p, ok = dp.socks5_connect(fs)
    assert h == "example.com" and p == 443
    assert ok[:2] == b"\x05\x00" and fs.sent[:2] == b"\x05\x00"


def test_socks5_rejects_non_connect():
    # ver, nmethods=1, method=0 ; then ver, cmd=BIND(2) ...
    fs = _FakeSock(b"\x05\x01\x00" + b"\x05\x02\x00\x01" + b"\x00\x00\x00\x00\x00\x00")
    try:
        dp.socks5_connect(fs)
        assert False, "should reject non-CONNECT"
    except OSError:
        pass


def test_desync_send_passthrough_on_non_tls():
    fs = _FakeSock()
    dp.desync_send(fs, b"POST /hook HTTP/1.1\r\n", "oob")
    assert bytes(fs.sent) == b"POST /hook HTTP/1.1\r\n"       # untouched


def test_desync_frag_preserves_all_bytes_in_order():
    data, _ = _client_hello()
    fs = _FakeSock()
    dp.desync_send(fs, data, "frag")
    assert bytes(fs.sent) == data                            # fragmentation splits sends, not bytes


def test_desync_oob_inserts_exactly_one_poison_byte():
    data, _ = _client_hello()
    fs = _FakeSock()
    dp.desync_send(fs, data, "oob")
    # oob = data[:mid] + one poison byte (urgent, discarded by server) + data[mid:]
    assert len(fs.sent) == len(data) + 1
    assert bytes(fs.sent).count(bytes([dp.POISON])) >= bytes(data).count(bytes([dp.POISON])) + 1


def test_desync_oob_short_prefix_send_aborts_without_misaligned_remainder():
    data, _ = _client_hello()
    hs, he = dp.sni_range(data)
    mid = (hs + he) // 2

    class _ShortSock(_FakeSock):
        def __init__(self):
            super().__init__()
            self.sendall_calls = 0

        def send(self, b, flags=0):
            sent = mid - 1
            self.sent += b[:sent]
            return sent

        def sendall(self, b):
            self.sendall_calls += 1
            super().sendall(b)

    sock = _ShortSock()
    dp.desync_send(sock, data, "oob")
    assert sock.sendall_calls == 0
    assert bytes(sock.sent) == data[:mid - 1]


class _FlakySock:
    """Fake socket that fails on chosen send/sendall call indices (1-based), else accumulates."""
    def __init__(self, fail_calls=()):
        self.sent = bytearray()
        self.calls = 0
        self.fail_calls = set(fail_calls)

    def _tick(self):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise OSError("simulated socket failure")

    def sendall(self, b):
        self._tick()
        self.sent += b

    def send(self, b, flags=0):
        self._tick()
        self.sent += b
        return len(b)

    def setsockopt(self, *a, **k):
        pass


def test_desync_oob_does_not_duplicate_bytes_on_late_failure():
    """If a write fails AFTER the poisoned prefix is committed, we must NOT resend the whole
    ClientHello (that duplicates bytes into the server's stream and corrupts the TLS handshake).
    The error must propagate so handle() closes the connection instead."""
    data, _ = _client_hello()
    hs, he = dp.sni_range(data)
    mid = (hs + he) // 2
    # oob mode: call 1 = MSG_OOB prefix send (commits data[:mid]+POISON), call 2 = sendall(rest) fails
    fs = _FlakySock(fail_calls={2})
    raised = False
    try:
        dp.desync_send(fs, data, "oob")
    except OSError:
        raised = True
    assert raised, "a post-commit write failure must propagate, not be swallowed with a full resend"
    # Only the committed prefix is on the wire — never a duplicated full copy of `data`.
    assert len(fs.sent) < len(data), "the whole ClientHello was re-sent -> byte duplication"
    assert bytes(fs.sent) == data[:mid] + bytes([dp.POISON])


def test_desync_oob_falls_back_cleanly_when_oob_unsupported():
    """If the very first (MSG_OOB) send fails with nothing on the wire (e.g. platform lacks MSG_OOB),
    fall back to one clean, un-fragmented send so the connection still works."""
    data, _ = _client_hello()
    fs = _FlakySock(fail_calls={1})       # first write (the OOB send) fails atomically
    dp.desync_send(fs, data, "oob")       # must NOT raise
    assert bytes(fs.sent) == data          # exactly one clean copy — no fragmentation, no duplication


def test_desync_frag_first_write_failure_aborts_without_resend():
    """frag mode has no OOB primitive, so a first-write failure may have committed bytes -> we abort
    (no clean resend) rather than risk duplicating them. The OLD code did sock.sendall(data) here,
    which duplicated the (possibly partial) prefix; assert we do NOT re-send a full copy."""
    data, _ = _client_hello()
    fs = _FlakySock(fail_calls={1})        # first write (sendall(data[:hs])) fails
    dp.desync_send(fs, data, "frag")       # must NOT raise (handle() would just close the conn)
    assert len(fs.sent) < len(data)        # old code re-sent the whole ClientHello -> == data


def test_desync_frag_late_failure_propagates_no_resend():
    """frag mode, failure AFTER the prefix is committed: the error must propagate (handle() closes the
    connection) and we must NOT resend the whole ClientHello (that duplicates the committed prefix)."""
    data, _ = _client_hello()
    hs, he = dp.sni_range(data)
    fs = _FlakySock(fail_calls={2})        # call 1 = sendall(data[:hs]) ok, call 2 = first frag chunk fails
    raised = False
    try:
        dp.desync_send(fs, data, "frag")
    except OSError:
        raised = True
    assert raised, "a post-commit frag failure must propagate, not be swallowed with a full resend"
    assert bytes(fs.sent) == data[:hs]     # only the committed prefix — never a duplicated full copy
    assert len(fs.sent) < len(data)


def test_desync_frag_oob_late_failure_propagates_no_resend():
    """frag_oob mode: after the MSG_OOB prefix is committed, a failure in the fragmentation loop must
    propagate and never resend — otherwise the already-sent data[:mid]+POISON prefix is duplicated."""
    data, _ = _client_hello()
    hs, he = dp.sni_range(data)
    mid = (hs + he) // 2
    fs = _FlakySock(fail_calls={2})        # call 1 = MSG_OOB prefix commit, call 2 = first frag-loop chunk fails
    raised = False
    try:
        dp.desync_send(fs, data, "frag_oob")
    except OSError:
        raised = True
    assert raised, "a post-commit frag_oob failure must propagate, not be swallowed with a full resend"
    assert bytes(fs.sent) == data[:mid] + bytes([dp.POISON])
    assert len(fs.sent) < len(data)


def test_desync_tlsrec_splits_into_two_valid_records():
    """tlsrec rewrites the ClientHello into TWO valid TLS records whose payloads concatenate to the
    original handshake (only the 5-byte record headers change). Verifies record framing + byte-exact
    reassembly so the real server always accepts it."""
    data, _ = _client_hello()
    fs = _FakeSock()
    dp.desync_send(fs, data, "tlsrec")
    sent = bytes(fs.sent)
    reclen = int.from_bytes(data[3:5], 'big')
    payload = data[5:5 + reclen]
    # record 1
    assert sent[0] == 0x16
    l1 = int.from_bytes(sent[3:5], 'big')
    r1p = sent[5:5 + l1]
    off = 5 + l1
    # record 2
    assert sent[off] == 0x16
    l2 = int.from_bytes(sent[off + 3:off + 5], 'big')
    r2p = sent[off + 5:off + 5 + l2]
    assert sent[1:3] == data[1:3] == sent[off + 1:off + 3]   # legacy version preserved in both headers
    assert l1 + l2 == reclen                                 # lengths partition the original payload
    assert r1p + r2p == payload                              # payloads concatenate to the original handshake
    assert 0 < l1 < reclen                                   # a genuine split — both records non-empty


def test_desync_tlsrec_first_write_failure_aborts_without_resend():
    """First-record write failure -> abort (bytes may be committed); never resend the whole CH."""
    data, _ = _client_hello()
    fs = _FlakySock(fail_calls={1})
    dp.desync_send(fs, data, "tlsrec")       # must NOT raise
    assert len(fs.sent) < len(data)          # never re-sent a full copy


def test_desync_tlsrec_late_failure_propagates_no_resend():
    """After record 1 is committed, a record-2 failure must propagate (handle() closes) — no resend."""
    data, _ = _client_hello()
    fs = _FlakySock(fail_calls={2})
    raised = False
    try:
        dp.desync_send(fs, data, "tlsrec")
    except OSError:
        raised = True
    assert raised, "a post-commit tlsrec failure must propagate, not resend"
    assert len(fs.sent) < len(data)          # only the committed first record, not a duplicated full copy


def test_desync_tlsrec_passthrough_on_non_tls():
    """Non-ClientHello first write -> passthrough, untouched (same as the other modes)."""
    fs = _FakeSock()
    dp.desync_send(fs, b"GET / HTTP/1.1\r\n\r\n", "tlsrec")
    assert bytes(fs.sent) == b"GET / HTTP/1.1\r\n\r\n"


def test_doh_resolve_never_raises_on_ctx_failure():
    """A TLS-context build failure (e.g. a bad SSLKEYLOGFILE path -> OSError) must NOT escape
    doh_resolve — it returns [], and connect_upstream falls back to system DNS rather than failing
    every connection silently while DoH is on."""
    import ssl as _ssl
    dp._doh_ctx = None
    dp._doh_cache.clear()
    saved = _ssl.create_default_context
    _ssl.create_default_context = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError('bad keylog'))
    try:
        assert dp.doh_resolve('rr3---sn-x.googlevideo.com') == []      # never raises, no network attempted
        raised = None
        try:
            dp.connect_upstream('nonexistent.invalid.example', 443, True, timeout=3)
        except OSError as e:
            raised = e
        assert raised is not None      # fell back to system DNS (getaddrinfo fails) — did NOT propagate ctx error
    finally:
        _ssl.create_default_context = saved
        dp._doh_ctx = None
        dp._doh_cache.clear()
