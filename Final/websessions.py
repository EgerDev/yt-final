"""Thread-safe, in-memory sessions for the loopback web interface."""

import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path


def retire_legacy_token_file(path: Path) -> bool:
    """Remove an obsolete permanent-token file without ever opening or reading it."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


class SessionStore:
    def __init__(self, ttl_seconds=43_200, max_sessions=32, clock=time.time):
        if ttl_seconds <= 0 or max_sessions <= 0:
            raise ValueError("session limits must be positive")
        self._ttl_seconds = int(ttl_seconds)
        self._max_sessions = int(max_sessions)
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[bytes, float] = {}
        self._revoked: dict[bytes, float] = {}

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    def _prune(self, now: float) -> None:
        for records in (self._sessions, self._revoked):
            for digest, expiry in list(records.items()):
                if expiry <= now:
                    del records[digest]

    def _matching_digest(self, token: str, records: dict[bytes, float]) -> bytes | None:
        candidate = self._digest(token)
        for digest in records:
            if hmac.compare_digest(digest, candidate):
                return digest
        return None

    def create(self) -> tuple[str, float]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            while True:
                token = secrets.token_urlsafe(32)
                digest = self._digest(token)
                if digest not in self._sessions and digest not in self._revoked:
                    break
            if len(self._sessions) >= self._max_sessions:
                soonest = min(self._sessions, key=self._sessions.__getitem__)
                del self._sessions[soonest]
            expiry = now + self._ttl_seconds
            self._sessions[digest] = expiry
        return token, expiry

    def expiry_for(self, token: str) -> float | None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            digest = self._matching_digest(token, self._sessions)
            return self._sessions.get(digest) if digest is not None else None

    def revoke(self, token: str) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            digest = self._matching_digest(token, self._sessions)
            if digest is None:
                return self._matching_digest(token, self._revoked) is not None
            expiry = self._sessions.pop(digest)
            if len(self._revoked) >= self._max_sessions:
                soonest = min(self._revoked, key=self._revoked.__getitem__)
                del self._revoked[soonest]
            self._revoked[digest] = expiry
            return True

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._revoked.clear()
