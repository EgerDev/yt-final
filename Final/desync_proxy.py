#!/usr/bin/env python3
"""Local DPI-desync proxy — a no-root, no-infrastructure ISP-throttle bypass.

Speaks BOTH HTTP CONNECT and SOCKS5. On the first outbound write of each
connection it corrupts the TLS ClientHello's server-name from the ISP DPI's
point of view (default mode 'oob': a TCP out-of-band poison byte at the SNI
midpoint that the real server discards but a DPI ingests inline), so the ISP
can't read the hostname and can't throttle the flow.

MEASURED (Verizon 5G Home, 2026-07): full bypass. Downloads shaped to ~1 MiB/s
reach ~6.7-7 MiB/s end-to-end through yt_final (and ~9 MiB/s via curl), near the
~11.5 link ceiling. No VPN, no remote server, no root. Modes: 'oob' (default,
fast + effective), 'frag' (whole-ClientHello 2-byte split — effective via curl
but adds per-connection latency), 'frag_oob' (both).

Use with yt_final: run this, then set the Proxy to  http://127.0.0.1:1080
(the managed yt-dlp lacks PySocks, so use http:// not socks5:// for the app;
socks5:// works for clients that have PySocks, e.g. curl --socks5-hostname).

Usage:  python3 desync_proxy.py [--host 127.0.0.1] [--port 1080]
                                 [--mode frag|oob|frag_oob] [--verbose]
No root required. Ctrl-C to stop.

Technique credit: this is a minimal, self-contained reimplementation of the
no-root ClientHello/SNI desync methods from byedpi (https://github.com/hufrea/byedpi)
— specifically its 'oob' (URG poison byte) and 'split' primitives — tuned and
measured against a real ISP SNI throttle. byedpi and zapret are the full upstream
tools if you want more modes; this file exists so yt_final has a zero-dependency,
auditable, macOS-no-root bypass built in.
"""
import argparse
import socket
import struct
import sys
import threading
import time

POISON = ord('a')


def sni_range(data):
    """If data is a TLS ClientHello, return (host_start, host_end) of the SNI value, else None."""
    try:
        if len(data) < 6 or data[0] != 0x16 or data[5] != 0x01:
            return None
        p = 5 + 4 + 2 + 32
        p += 1 + data[p]                                    # session_id
        p += 2 + int.from_bytes(data[p:p+2], 'big')         # cipher_suites
        p += 1 + data[p]                                    # compression
        end = p + 2 + int.from_bytes(data[p:p+2], 'big'); p += 2
        while p + 4 <= end:
            et = int.from_bytes(data[p:p+2], 'big')
            el = int.from_bytes(data[p+2:p+4], 'big'); p += 4
            if et == 0x0000:                                # server_name
                sp = p + 3
                nlen = int.from_bytes(data[sp:sp+2], 'big'); sp += 2
                return sp, sp + nlen
            p += el
    except Exception:
        pass
    return None


VERBOSE = False
DOH = False                     # opt-in: resolve upstream hosts via DNS-over-HTTPS at the 1.1.1.1 literal

_DOH_IP = "1.1.1.1"             # connect by IP literal -> no bootstrap DNS, no ISP DNS hijack
_DOH_SNI = "cloudflare-dns.com"  # cert SNI (the 1.1.1.1 leaf cert carries this DNS SAN)
_doh_ctx = None                  # lazy ssl context (system CA store, verifies _DOH_SNI)
_doh_cache = {}                  # host -> (expiry_monotonic, [ip, ...])


def _is_ip(h):
    """True if h is already a numeric IPv4/IPv6 literal (skip DoH — nothing to resolve)."""
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, h)
            return True
        except OSError:
            pass
    return False


def doh_resolve(host, timeout=5.0):
    """Resolve host -> [ip] via Cloudflare DoH JSON at the 1.1.1.1 literal. Connects by IP (no bootstrap
    DNS) and never touches the system resolver, so ISP DNS hijacking/blocking is bypassed. Returns []
    on any failure, so the caller falls back to system DNS — the proxy never hard-fails on DoH."""
    import http.client
    import json
    import ssl
    global _doh_ctx
    if _is_ip(host):
        return [host]
    now = time.monotonic()
    hit = _doh_cache.get(host)
    if hit and hit[0] > now:
        return hit[1]
    ips = []
    for qtype, want in (("A", 1), ("AAAA", 28)):          # v4 first, then v6
        try:
            if _doh_ctx is None:
                # Inside the try: a context-build failure (e.g. a bad SSLKEYLOGFILE path -> OSError)
                # must degrade to [] like every other failure, never escape doh_resolve.
                _doh_ctx = ssl.create_default_context()   # system CAs, check_hostname=True
            raw = socket.create_connection((_DOH_IP, 443), timeout=timeout)
            tls = _doh_ctx.wrap_socket(raw, server_hostname=_DOH_SNI)   # cert verified vs cloudflare-dns.com
            conn = http.client.HTTPConnection(_DOH_SNI, timeout=timeout)
            conn.sock = tls                               # pre-set the (already-TLS) socket -> skips connect()/DNS
            conn.request("GET", f"/dns-query?name={host}&type={qtype}",
                         headers={"accept": "application/dns-json"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status != 200:
                continue
            j = json.loads(body)
            if j.get("Status") != 0:                      # NXDOMAIN / SERVFAIL etc.
                continue
            ips += [a["data"] for a in j.get("Answer", [])
                    if a.get("type") == want and a.get("data")]
            if qtype == "A" and ips:                       # prefer v4; skip AAAA if we have A records
                break
        except Exception:
            continue
    if ips:
        _doh_cache[host] = (now + 30.0, ips)               # short TTL; conns to one rr*.googlevideo host repeat
    return ips


def connect_upstream(host, port, use_doh, timeout=15):
    """Connect to (host, port). With DoH on, resolve via the 1.1.1.1 literal and connect to the numeric
    IP so the real hostname never hits system DNS; on any DoH failure fall back to system DNS. `host`
    is preserved by the caller for SNI + the desync — only the TCP endpoint changes."""
    if use_doh and not _is_ip(host):
        try:
            resolved = doh_resolve(host, timeout=min(timeout, 5.0))
        except Exception:
            resolved = []                                                      # DoH must never break the connect
        for ip in resolved:
            try:
                return socket.create_connection((ip, port), timeout=timeout)   # numeric IP -> no DNS leak
            except OSError:
                continue
    return socket.create_connection((host, port), timeout=timeout)             # system-DNS fallback


def _tlsrec_split(data):
    """Split a single TLS ClientHello record into TWO valid records, cut inside the SNI value.
    Returns (r1, r2, trailer) or None if `data` isn't a splittable ClientHello record.

    Only the 5-byte record headers change; the handshake-length field inside the ClientHello is left
    intact (RFC 8446 §5.1 permits one handshake message to span multiple records), so the payloads
    concatenate byte-for-byte to the original handshake. This is the 'tlsrec' primitive: it fragments
    one layer ABOVE the TCP segment that 'frag' splits, so a DPI that reassembles TCP must ALSO
    reassemble across TLS record boundaries before it can find the SNI — many fast-path DPIs won't."""
    rng = sni_range(data)
    if rng is None or len(data) < 6 or data[0] != 0x16:
        return None
    reclen = int.from_bytes(data[3:5], 'big')
    if len(data) < 5 + reclen:                          # record header claims more than we hold
        return None
    ver, payload = data[1:3], data[5:5 + reclen]
    trailer = data[5 + reclen:]                         # extra records (normally empty for a lone CH)
    hs, he = rng
    split = ((hs + he) // 2) - 5                        # SNI midpoint as an offset INTO the payload
    if not (0 < split < reclen):
        return None
    r1 = b'\x16' + ver + split.to_bytes(2, 'big') + payload[:split]
    r2 = b'\x16' + ver + (reclen - split).to_bytes(2, 'big') + payload[split:]
    return r1, r2, trailer


def desync_send(sock, data, mode, host=''):
    """Fragment ONLY the SNI hostname window of the first (ClientHello) write, so the ISP's
    DPI can't parse the server name. Cheap: ~a few small sends around the hostname, not the
    whole handshake — keeps per-connection cost near zero for chunk-heavy clients."""
    rng = sni_range(data)
    if VERBOSE:
        print(f"[conn] {host} firstwrite={len(data)}B "
              f"desync={'sni'+str(rng) if rng else 'passthrough'}", file=sys.stderr, flush=True)
    if rng is None:
        sock.sendall(data)
        return
    if mode == 'tlsrec':
        # TLS-record-layer split: two valid records whose payloads concatenate to the original
        # handshake. Obeys the same commit/abort discipline as the frag path below.
        parts = _tlsrec_split(data)
        if parts is None:
            sock.sendall(data)                          # not a splittable ClientHello -> passthrough
            return
        r1, r2, trailer = parts
        try:
            sock.sendall(r1)                            # first write: may commit bytes on partial failure
        except OSError:
            return                                      # -> abort, never resend (matches frag semantics)
        sock.sendall(r2)                                # committed: let any failure propagate to handle()
        if trailer:
            sock.sendall(trailer)
        return
    hs, he = rng
    mid = (hs + he) // 2
    use_oob = mode in ('oob', 'frag_oob') and 6 < mid < len(data) - 1
    # First write. The OOB primitive raises atomically (nothing on the wire) when the platform
    # lacks MSG_OOB, so on that failure we can still fall back to one clean, un-fragmented send and
    # keep the connection working. After this point the poisoned/fragmented prefix is COMMITTED to
    # the stream, so any later failure must abort — a full resend would duplicate those bytes into
    # the server's stream and corrupt the real TLS handshake.
    try:
        if use_oob:
            # OOB poison byte at the SNI midpoint (urgent byte; discarded by the server, ingested
            # inline by the DPI so its view of the hostname is corrupted). send() MAY return a short
            # count (FI4-09): if fewer than `mid` REAL prefix bytes went out, those bytes are already
            # committed to the stream, so we can't safely resend — abort and let handle() close the
            # connection. (`< mid` not `< mid+1`: some platforms don't count the urgent POISON byte,
            # which is fine — the real prefix still landed.)
            if sock.send(data[:mid] + bytes([POISON]), socket.MSG_OOB) < mid:
                return
            frag_lo = mid
        else:
            sock.sendall(data[:hs])                         # bulk up to the hostname
            frag_lo = hs
    except OSError:
        if use_oob:                                         # OOB unsupported -> nothing sent: clean fallback
            try:
                sock.sendall(data)
            except OSError:
                pass
        return                                              # plain-send failure may have committed bytes -> abort
    # Prefix committed: let any failure below propagate so handle() closes the connection. Never
    # resend `data` here (that duplicates the already-sent prefix).
    if mode in ('frag', 'frag_oob'):
        for i in range(frag_lo, he, 2):                     # 2-byte segments across the hostname
            sock.sendall(data[i:min(i + 2, he)])            # cap at he so the last chunk can't grab data[he]
        sock.sendall(data[he:])                             # bulk the remainder (no duplicated byte)
    else:                                                   # oob: just send the rest whole
        sock.sendall(data[frag_lo:])


def relay(src, dst):
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def recvn(sock, n):
    """Read exactly n bytes (SOCKS5 fields can arrive across multiple segments)."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("short read")
        buf += chunk
    return buf


def socks5_connect(client):
    """Parse a SOCKS5 CONNECT (first byte already known to be 0x05). Returns (host, port)."""
    head = recvn(client, 2)
    recvn(client, head[1])                                  # methods
    client.sendall(b'\x05\x00')                             # no auth
    req = recvn(client, 4)
    if req[1] != 0x01:                                      # only CONNECT
        client.sendall(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
        raise OSError("socks: not CONNECT")
    atyp = req[3]
    if atyp == 0x01:
        host = socket.inet_ntoa(recvn(client, 4))
    elif atyp == 0x03:
        host = recvn(client, recvn(client, 1)[0]).decode('ascii', 'ignore')
    elif atyp == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, recvn(client, 16))
    else:
        client.sendall(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
        raise OSError("socks: bad atyp")
    port = struct.unpack('>H', recvn(client, 2))[0]
    return host, port, b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00'


def http_connect(client):
    """Parse an HTTP CONNECT request. Returns (host, port, ok_reply, pipelined_bytes).

    pipelined_bytes is whatever arrived after the header terminator: a client may send its TLS
    ClientHello together with CONNECT (without waiting for the 200, saving an RTT). Those bytes
    used to be read into this buffer and DISCARDED, while handle() waited in recv() for a write
    that had already happened — the tunnel hung until something timed out."""
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = client.recv(4096)
        if not chunk:
            raise OSError("http: eof")
        buf += chunk
        if len(buf) > 65536:
            raise OSError("http: header too big")
    head, _, rest = buf.partition(b'\r\n\r\n')
    line = head.split(b'\r\n', 1)[0].decode('latin1')
    parts = line.split()
    if len(parts) < 2 or parts[0].upper() != 'CONNECT':
        client.sendall(b'HTTP/1.1 405 Method Not Allowed\r\n\r\n')
        raise OSError("http: not CONNECT")
    target = parts[1]
    host, sep, port = target.rpartition(':')
    # Spec-valid IPv6 literal target is bracketed ("[2001:db8::1]:443"); rpartition splits on the
    # last colon and leaves the brackets on host. Strip them so the bare address reaches inet_pton /
    # create_connection (a bracketed host fails DoH name lookup AND getaddrinfo).
    if not sep:
        # No colon at all: rpartition puts the WHOLE target in `port`, so int() raised ValueError —
        # which handle()'s `except OSError` does not catch (unhandled thread traceback, no reply).
        # CONNECT's authority-form requires the port (RFC 7231 §4.3.6), so this is a 400, not :443.
        host, port = target, ''
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    try:
        port_num = int(port) if port else (443 if sep else -1)
    except ValueError:
        port_num = -1
    if not host or not (0 < port_num < 65536):
        client.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
        raise OSError("http: bad CONNECT target")
    return host, port_num, b'HTTP/1.1 200 Connection established\r\n\r\n', rest


def handle(client, mode):
    upstream = None
    try:
        b0 = client.recv(1, socket.MSG_PEEK)                # dispatch: SOCKS5 vs HTTP CONNECT
        if not b0:
            return
        pipelined = b''
        if b0[0] == 0x05:
            host, port, ok = socks5_connect(client)
        else:
            host, port, ok, pipelined = http_connect(client)
        upstream = connect_upstream(host, port, DOH, timeout=15)
        upstream.settimeout(None)   # clear the connect-phase timeout so a slow/idle relay isn't torn down at 15s
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.sendall(ok)
        # --- first client->upstream write gets the desync ---
        # Bytes pipelined behind CONNECT ARE that first write; only wait on the socket when the
        # client did the usual thing and held its ClientHello until the 200.
        first = pipelined or client.recv(65536)
        if first:
            desync_send(upstream, first, mode, host)
        threading.Thread(target=relay, args=(upstream, client), daemon=True).start()
        relay(client, upstream)
    except OSError:
        pass
    finally:
        for s in (client, upstream):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=1080)
    ap.add_argument('--mode', default='oob', choices=['frag', 'oob', 'frag_oob', 'tlsrec'])
    ap.add_argument('--doh', action='store_true',
                    help='Resolve upstream hosts via DNS-over-HTTPS at the 1.1.1.1 literal (bypass ISP DNS hijack)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()
    global VERBOSE; VERBOSE = args.verbose
    global DOH; DOH = args.doh
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Plain SO_REUSEADDR (as upstream). SO_EXCLUSIVEADDRUSE was tried here to stop an orphaned
    # proxy hijacking the port, but on Windows it also refuses a bind while any socket on this
    # endpoint is in TIME_WAIT — and relay() actively closes, so a completed download leaves one.
    # That made the Speed Test's rapid mode-sweep restarts fail for ~120s (silently, since the
    # child's stderr is DEVNULL). Residual risk, stated honestly: the parent's readiness check is an
    # ENDPOINT probe (connect to 127.0.0.1:port), so an orphaned proxy already listening would also
    # satisfy it — the sweep's own throughput measurement is what would expose a wrong-mode listener.
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(128)
    print(f"[desync-proxy] listening on {args.host}:{args.port} (HTTP CONNECT + SOCKS5)  mode={args.mode}")
    print(f"[desync-proxy] in yt_final set Proxy to  http://{args.host}:{args.port}  "
          f"(bypasses ISP SNI throttling; no VPN needed)")
    try:
        while True:
            c, _ = srv.accept()
            threading.Thread(target=handle, args=(c, args.mode), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[desync-proxy] stopped")
    finally:
        srv.close()


if __name__ == '__main__':
    main()
