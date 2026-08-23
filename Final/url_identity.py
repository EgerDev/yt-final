"""Canonical download identities shared by every scheduling path."""

from __future__ import annotations

import _imp
import os
import re
import sys
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, cast
from urllib.parse import parse_qsl, urlsplit, urlunsplit


_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_ABSOLUTE_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_SCHEMELESS_HOST_PORT_RE = re.compile(
    r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|\[[^\]]+\]):[0-9]+(?=[/?#]|\Z)"
)

_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_WATCH_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "gaming.youtube.com",
    }
)
_VIDEO_PATH_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }
)
_EMBED_HOSTS = _VIDEO_PATH_HOSTS | frozenset(
    {"youtube-nocookie.com", "www.youtube-nocookie.com"}
)
_KNOWN_HOSTS = _SHORT_HOSTS | _WATCH_HOSTS | _EMBED_HOSTS


class _OutputLockEntry:
    __slots__ = ("lock", "references")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.references = 0


class _OutputLockState(ModuleType):
    guard: Any
    locks: dict[tuple[str, str], _OutputLockEntry]
    at_fork_registered: bool
    reset_after_fork_in_child: Callable[[], None]


_OUTPUT_LOCK_STATE_MODULE = "_yt_final_output_identity_state_v1"


def _get_output_lock_state() -> _OutputLockState:
    """Return one state module even while both import names execute concurrently."""
    _imp.acquire_lock()
    try:
        state = cast(
            _OutputLockState | None,
            sys.modules.get(_OUTPUT_LOCK_STATE_MODULE),
        )
        if state is None:
            state = _OutputLockState(_OUTPUT_LOCK_STATE_MODULE)
            state.guard = threading.RLock()
            state.locks = {}
            state.at_fork_registered = False
            sys.modules[_OUTPUT_LOCK_STATE_MODULE] = state

        if hasattr(os, "register_at_fork") and not state.at_fork_registered:
            def _reset_after_fork_in_child(
                shared_state: _OutputLockState = state,
            ) -> None:
                # Never acquire an inherited lock here: another parent thread may
                # have owned it at fork, leaving the child copy permanently locked.
                shared_state.guard = threading.RLock()
                shared_state.locks = {}
                for module_name in ("url_identity", "Final.url_identity"):
                    identity_module = sys.modules.get(module_name)
                    if (
                        identity_module is not None
                        and getattr(identity_module, "_OUTPUT_LOCK_STATE", None)
                        is shared_state
                    ):
                        setattr(
                            identity_module,
                            "_OUTPUT_LOCKS_GUARD",
                            shared_state.guard,
                        )
                        setattr(
                            identity_module,
                            "_OUTPUT_LOCKS",
                            shared_state.locks,
                        )

            state.reset_after_fork_in_child = _reset_after_fork_in_child
            os.register_at_fork(after_in_child=_reset_after_fork_in_child)
            state.at_fork_registered = True
        return state
    finally:
        _imp.release_lock()


_OUTPUT_LOCK_STATE = _get_output_lock_state()
# Compatibility/debug aliases. Runtime locking reads through the uniquely
# shared state module so simultaneous flat/package imports cannot diverge.
_OUTPUT_LOCKS_GUARD = _OUTPUT_LOCK_STATE.guard
_OUTPUT_LOCKS = _OUTPUT_LOCK_STATE.locks


def _url_with_default_scheme(url: str) -> str:
    value = str(url).strip()
    if value.startswith("//"):
        return "https:" + value
    # urlsplit treats ``youtube.com:443/...`` as a custom ``youtube.com``
    # URI scheme. A numeric port on a DNS/bracketed host is instead an
    # authority without a scheme and is normalized to HTTPS.
    if _SCHEMELESS_HOST_PORT_RE.match(value):
        return "https://" + value
    if not _ABSOLUTE_SCHEME_RE.match(value):
        return "https://" + value
    return value


def _canonical_host_text(host: str, *, bracketed: bool = False) -> str:
    """Lowercase a host while preserving IPv6/IPvFuture authority brackets."""
    if ":" not in host and not bracketed:
        return host.lower()
    address, scope_marker, scope = host.partition("%")
    canonical = address.lower()
    if scope_marker:
        canonical += scope_marker + scope
    return f"[{canonical}]"


def _normalized_url(url: str) -> str:
    """Normalize transport-only URL details without changing path/query meaning."""
    value = str(url).strip()
    candidate = _url_with_default_scheme(value)
    try:
        parts = urlsplit(candidate)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if scheme not in {"http", "https"} or not host:
            return value
        port = parts.port
    except (TypeError, ValueError):
        return value

    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
    authority = parts.netloc.rsplit("@", 1)[-1]
    host_text = _canonical_host_text(
        host,
        bracketed=authority.startswith("["),
    )
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host_text += f":{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, userinfo + host_text, path, parts.query, ""))


def _strict_youtube_parts(url: str):
    """Parse a YouTube authority without browser/request interpretation gaps."""
    try:
        parts = urlsplit(_url_with_default_scheme(url))
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        # Backslashes and userinfo are interpreted inconsistently by URL
        # consumers. They must never participate in a collapsed identity.
        if "\\" in parts.netloc or "@" in parts.netloc:
            return None
        host = (parts.hostname or "").lower()
        if host not in _KNOWN_HOSTS:
            return None
        port = parts.port
        default_port = 80 if scheme == "http" else 443
        if port is not None and port != default_port:
            return None
        return parts, host
    except (TypeError, ValueError):
        return None


def normalize_youtube_url(url: str) -> str | None:
    """Return an explicit, strict HTTP(S) YouTube URL for extractor handoff."""
    parsed = _strict_youtube_parts(url)
    if parsed is None:
        return None
    return _normalized_url(_url_with_default_scheme(url))


def youtube_video_id(url: str) -> str | None:
    """Return a confirmed single-video ID for exact supported YouTube URL forms."""
    parsed = _strict_youtube_parts(url)
    if parsed is None:
        return None
    parts, host = parsed
    query = parse_qsl(parts.query, keep_blank_values=True)

    if any(key == "list" for key, _value in query):
        return None
    query_ids = [value for key, value in query if key == "v"]

    if host in _SHORT_HOSTS:
        match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parts.path)
        if query_ids or not match:
            return None
        return match.group(1)

    if parts.path == "/watch" and host in _WATCH_HOSTS:
        if len(query_ids) != 1 or not _VIDEO_ID_RE.fullmatch(query_ids[0]):
            return None
        return query_ids[0]

    match = re.fullmatch(
        r"/(shorts|embed|live)/([A-Za-z0-9_-]{11})/?",
        parts.path,
    )
    if not match or query_ids:
        return None
    form, video_id = match.groups()
    if form == "embed":
        return video_id if host in _EMBED_HOSTS else None
    return video_id if host in _VIDEO_PATH_HOSTS else None


def download_identity(url: str) -> tuple[str, str]:
    """Return a canonical worker identity without guessing at unknown URLs."""
    video_id = youtube_video_id(url)
    if video_id is not None:
        return "youtube", video_id
    return "url", _normalized_url(url)


def dedupe_download_urls(urls: Iterable[str]) -> list[str]:
    """Drop duplicate download identities while preserving first spelling and order."""
    seen: set[tuple[str, str]] = set()
    result: list[str] = []
    for url in urls:
        identity = download_identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(url)
    return result


@contextmanager
def output_identity_lock(
    video_id: str | None,
    output_dir: Path,
) -> Iterator[None]:
    """Serialize one extracted video's output lifecycle within this process."""
    if not video_id:
        yield
        return

    resolved_dir = os.path.normcase(str(Path(output_dir).expanduser().resolve()))
    key = resolved_dir, str(video_id)
    state = _OUTPUT_LOCK_STATE
    guard = state.guard
    registry = state.locks
    with guard:
        entry = registry.get(key)
        if entry is None:
            entry = _OutputLockEntry()
            registry[key] = entry
        # References count both the current owner and threads blocked in acquire().
        entry.references += 1

    acquired = False
    try:
        entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with guard:
            entry.references -= 1
            if entry.references == 0 and registry.get(key) is entry:
                del registry[key]


def _register_module_aliases() -> None:
    """Keep process-wide identity state singular under package and direct imports."""
    module = sys.modules[__name__]
    sys.modules.setdefault("url_identity", module)
    sys.modules.setdefault("Final.url_identity", module)


_register_module_aliases()
