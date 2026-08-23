"""Deterministic redaction for normal operational logs."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def _home_prefix_variants() -> tuple[str, ...]:
    """Every spelling the user's home directory realistically appears as in a log line.

    The labelled rules below only fire on known phrasings (cookies:, output:, saving to, ...). Real
    log lines — and, more importantly, TRACEBACK frames, which are routed through the same filter via
    exc_text — use none of those, so an absolute path in unlabelled prose leaked the account name
    verbatim into final_yt.log. On a Windows install the home directory contains the operator's
    username, which is the actual PII here.

    Only the PREFIX is replaced, so "<home>/Videos/clip.mp4" keeps everything needed to debug while
    dropping the identity. Variants cover the native separator, the forward-slash form Python and
    ffmpeg both emit, and the doubled-backslash form that appears inside repr() and JSON.
    """
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):        # no HOME/USERPROFILE — nothing to redact
        return ()
    stripped = home.rstrip("\\/")
    if len(stripped) < 4:
        # Degenerate roots ("/", "C:\") would rewrite every path in the log into <home>.
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for variant in (stripped, stripped.replace("\\", "/"), stripped.replace("\\", "\\\\")):
        if variant and variant not in seen:
            seen.add(variant)
            out.append(variant)
    # Longest first so the doubled-backslash spelling wins over its own shorter prefix.
    return tuple(sorted(out, key=len, reverse=True))


_HOME_PREFIXES = _home_prefix_variants()
# Case-insensitive on Windows only: POSIX paths are case-sensitive, and folding there could rewrite a
# genuinely different directory that merely differs in case.
_HOME_RE = (
    re.compile(
        "|".join(re.escape(prefix) for prefix in _HOME_PREFIXES),
        re.IGNORECASE if os.name == "nt" else 0,
    )
    if _HOME_PREFIXES
    else None
)


_URL_RE = re.compile(
    r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"<>]+)"
)
_COOKIE_LABEL = (
    r"(?:\b(?:cookiefile|cookie[_ -]?path|cookies(?:_from_browser)?)"
    r"\s*[:=]\s*"
    r"|\busing\s+cookie\s+file\s*:\s*"
    r"|\bauto-discovered\s+cookies\.txt\s+at\s*)"
)
_OUTPUT_LABEL = (
    r"(?:\boutput\s*[:=]\s*"
    r"|\boutput[_ -](?:dir|path)\s*(?:[:=]\s*|\s+)"
    r"|\bsaving\s+to\s+"
    r"|\bOutput\s+#\d+.*?\bto\s+)"
)
_COOKIE_QUOTED_START_RE = re.compile(
    rf"(?P<label>{_COOKIE_LABEL})(?P<quote>['\"])",
    re.IGNORECASE,
)
_COOKIE_UNQUOTED_RE = re.compile(
    rf"(?P<label>{_COOKIE_LABEL})(?P<value>.*?)(?=\s+\b[\w-]+\s*=|$)",
    re.IGNORECASE,
)
_OUTPUT_QUOTED_START_RE = re.compile(
    rf"(?P<label>{_OUTPUT_LABEL})(?P<quote>['\"])",
    re.IGNORECASE,
)
_OUTPUT_UNQUOTED_RE = re.compile(
    rf"(?P<label>{_OUTPUT_LABEL})(?P<value>.*?)(?=\s+\b[\w-]+\s*=|$)",
    re.IGNORECASE,
)
_QUOTED_COMPLETION_START_RE = re.compile(
    r"(?P<label>\b(?:on[-_ ]?complete|completion(?:[_ -]?command)?)"
    r"\s*[:=]\s*)(?P<quote>['\"])",
    re.IGNORECASE,
)
_LOG_COMPLETION_RE = re.compile(
    r"(?P<label>\[on[-_ ]?complete\]\s*).*$",
    re.IGNORECASE,
)
_PLAIN_COMPLETION_RE = re.compile(
    r"(?P<label>\b(?:on[-_ ]?complete|completion(?:[_ -]?command)?)"
    r"\s*[:=]\s*)(?P<value>.*?)"
    r"(?=\s+(?:output(?:[_ -]?(?:dir|path))?|cookie(?:file|[_ -]?path)?)"
    r"\s*[:=]|$)",
    re.IGNORECASE,
)


def _split_context_suffix(raw: str) -> tuple[str, str]:
    suffix = ""
    while raw and raw[-1] in ",;:!?":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    changed = True
    while raw and changed:
        changed = False
        for opening, closing in pairs:
            if raw.endswith(closing) and raw.count(closing) > raw.count(opening):
                suffix = closing + suffix
                raw = raw[:-1]
                changed = True
    return raw, suffix


def _find_quoted_value_end(text: str, start: int, quote: str) -> int | None:
    """Find a real quote boundary without stopping at an apostrophe in a value."""
    candidate = text.find(quote, start)
    while candidate >= 0:
        following = candidate + 1
        if following == len(text):
            return candidate
        next_character = text[following]
        if next_character in ",;:!?)]}":
            return candidate
        if next_character.isspace():
            remainder = text[following:].lstrip()
            if (
                not remainder
                or remainder[0] in "([{,;:!?"
                or re.match(r"[\w-]+\s*(?:=|:)", remainder)
                or text.find(quote, following) < 0
            ):
                return candidate
        candidate = text.find(quote, candidate + 1)
    return None


def _redact_quoted_values(
        text: str,
        start_pattern: re.Pattern[str],
        replacement: str,
        *,
        require_absolute: bool = False) -> str:
    cursor = 0
    while match := start_pattern.search(text, cursor):
        closing = _find_quoted_value_end(
            text, match.end(), match.group("quote"))
        if closing is None:
            cursor = match.end()
            continue
        value = text[match.end():closing]
        if require_absolute and not _is_absolute_path(value):
            cursor = closing + 1
            continue
        substituted = match.group("label") + replacement
        text = text[:match.start()] + substituted + text[closing + 1:]
        cursor = match.start() + len(substituted)
    return text


def _redact_url(match: re.Match[str]) -> str:
    raw, suffix = _split_context_suffix(match.group("url"))
    if (
            match.start() > 0
            and match.string[match.start() - 1] == "'"
            and raw.endswith("'")):
        raw = raw[:-1]
        suffix = "'" + suffix
        raw, inner_suffix = _split_context_suffix(raw)
        suffix = inner_suffix + suffix
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "<redacted-url>" + suffix
    host = parsed.netloc.rsplit("@", 1)[-1]
    if not parsed.scheme or not host:
        return "<redacted-url>" + suffix
    base = f"{parsed.scheme}://{host}"
    if "@" in parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return base + "/<redacted>" + suffix
    return base + ("/" if parsed.path == "/" else "") + suffix


def _is_absolute_path(value: str) -> bool:
    value = value.strip().strip("'\"")
    return (
        value.startswith("/")
        or value.startswith("\\\\")
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _redact_unquoted_path(
        match: re.Match[str], *, require_absolute: bool) -> str:
    value, suffix = _split_context_suffix(match.group("value").rstrip())
    if require_absolute and not _is_absolute_path(value):
        return match.group(0)
    return match.group("label") + "<redacted-path>" + suffix


def redact_sensitive_text(text: str) -> str:
    """Remove credential material and private labeled paths while retaining context."""
    clean = str(text)
    clean = _redact_quoted_values(
        clean, _QUOTED_COMPLETION_START_RE, "<redacted-command>"
    )
    clean = _LOG_COMPLETION_RE.sub(
        lambda match: match.group("label") + "<redacted-command>", clean
    )
    clean = _PLAIN_COMPLETION_RE.sub(
        lambda match: match.group("label") + "<redacted-command>", clean
    )
    clean = _redact_quoted_values(
        clean, _COOKIE_QUOTED_START_RE, "<redacted-path>"
    )
    clean = _COOKIE_UNQUOTED_RE.sub(
        lambda match: _redact_unquoted_path(match, require_absolute=False), clean
    )
    clean = _redact_quoted_values(
        clean,
        _OUTPUT_QUOTED_START_RE,
        "<redacted-path>",
        require_absolute=True,
    )
    clean = _OUTPUT_UNQUOTED_RE.sub(
        lambda match: _redact_unquoted_path(match, require_absolute=True), clean
    )
    clean = _URL_RE.sub(_redact_url, clean)
    # LAST, and deliberately so: the labelled rules above already collapsed their matches to
    # <redacted-path>, so this only sees what they did not recognise — unlabelled prose and traceback
    # frames. It is a backstop for phrasings nobody thought to enumerate, not a replacement for them.
    if _HOME_RE is not None:
        clean = _HOME_RE.sub("<home>", clean)
    return clean


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_info:
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        return True
