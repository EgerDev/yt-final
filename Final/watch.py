#!/usr/bin/env python3
"""
Channel "subscribe & auto-archive" support via YouTube's public RSS feed.

YouTube exposes a lightweight, no-API-key, NOT-bot-walled RSS feed per channel:
    https://www.youtube.com/feeds/videos.xml?channel_id=UC...
This module resolves a channel URL/@handle/id to its channel_id, fetches the feed (the latest
~15 uploads), and exposes a simple seen-id archive so the `watch` command only acts on NEW videos.
Feed transport/parsing uses urllib + ElementTree; title matching uses the required ``regex``
runtime so each user-supplied pattern has a hard per-operation time budget.
"""

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from .executable_trust import explicit_path_executable
    from .watch_filters import WatchPatternError, title_matches
except ImportError:
    from executable_trust import explicit_path_executable
    from watch_filters import WatchPatternError, title_matches

_YT_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
             'gaming.youtube.com', 'youtu.be'}

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')
_NS = {'atom': 'http://www.w3.org/2005/Atom',
       'yt': 'http://www.youtube.com/xml/schemas/2015',
       'media': 'http://search.yahoo.com/mrss/'}
_UC = r'UC[0-9A-Za-z_-]{22}'


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': _UA, 'Accept-Language': 'en-US,en;q=0.9'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def resolve_channel_id(target):
    """Resolve a channel URL / @handle / bare name / channel_id to a UC… id (or None)."""
    t = (target or '').strip()
    if re.fullmatch(_UC, t):
        return t
    m = re.search(r'channel_id=(' + _UC + ')', t) or re.search(r'/channel/(' + _UC + ')', t)
    if m:
        return m.group(1)
    # Build a channel page URL to scrape the id from.
    if t.startswith('http'):
        # SSRF guard: only ever fetch YouTube hosts, never an arbitrary user-supplied URL.
        host = (urlparse(t).hostname or '').lower()
        if host not in _YT_HOSTS:
            return None
        url = t
    elif t.startswith('@'):
        url = 'https://www.youtube.com/' + t
    else:
        url = 'https://www.youtube.com/@' + t
    try:
        html = _get(url)
    except Exception:
        return None
    # Order matters: the canonical link + externalId are the PAGE OWNER's channel; a bare "channelId"
    # can be a recommended/featured channel elsewhere on the page (picked the wrong one otherwise).
    for pat in (r'rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(' + _UC + ')"',
                r'href="https://www\.youtube\.com/channel/(' + _UC + ')"[^>]+rel="canonical"',
                r'"externalId":"(' + _UC + ')"',
                r'"channelId":"(' + _UC + ')"',
                r'/channel/(' + _UC + ')'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def feed_url(channel_id):
    return f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'


def _entries(root):
    out = []
    for e in root.findall('atom:entry', _NS):
        vid = e.findtext('yt:videoId', namespaces=_NS)
        if not vid:
            continue
        out.append({
            'id': vid,
            'title': (e.findtext('atom:title', namespaces=_NS) or '').strip(),
            'published': e.findtext('atom:published', namespaces=_NS),
            'url': f'https://www.youtube.com/watch?v={vid}',
        })
    return out


def _published_yyyymmdd(published):
    """'2026-01-01T00:00:00+00:00' -> '20260101'; tolerant of None/short strings -> None."""
    if not published or len(published) < 10:
        return None
    return published[:4] + published[5:7] + published[8:10]


def match_filters(video, filters, duration=None):
    """True if `video` (dict with title/published) passes `filters`.

    filters keys (all optional): filter_title (bounded regex, case-insensitive),
    after (YYYYMMDD str),
    min_duration / max_duration (seconds int). `duration` (seconds) is supplied by the
    caller only when a duration filter is present (probed lazily); when a duration filter
    is set but duration is None (probe failed/unknown) -> reject (conservative).

    Pure: never raises, never touches the network. An invalid or timed-out regex is treated
    as a reject, a missing publish date is a reject when `after` is set, and an unknown
    duration is a reject when a duration bound is set. Empty/None filters are a no-op.
    """
    if not filters:
        return True
    pat = filters.get('filter_title')
    if pat:
        try:
            if not title_matches(pat, video.get('title') or ''):
                return False
        except WatchPatternError:
            return False
    after = filters.get('after')
    if after:
        # accept both YYYYMMDD and ISO YYYY-MM-DD: compare on digits only
        after_d = ''.join(c for c in str(after) if c.isdigit())[:8]
        pub = _published_yyyymmdd(video.get('published'))
        if not pub or (after_d and pub < after_d):
            return False
    mn = filters.get('min_duration')
    mx = filters.get('max_duration')
    if mn is not None or mx is not None:
        if duration is None:
            return False
        if mn is not None and duration < mn:
            return False
        if mx is not None and duration > mx:
            return False
    return True


# A video's duration never changes, so cache successful probes by video id. The watch poller
# re-sees the same ~15-entry RSS window every cycle; without this every duration-filtered watch
# would repeat the yt-dlp network probe for the SAME videos on every poll.
# Only successful probes are cached (a transient failure stays retryable). Bounded so it can't grow
# without limit; guarded by a lock since the poller probes several watches concurrently.
_DURATION_CACHE = {}
_DURATION_CACHE_LOCK = threading.Lock()
_DURATION_CACHE_MAX = 4096


def _duration_cache_key(url):
    """Stable key for a video url (its 11-char id when recognizable, else the url itself)."""
    u = url or ''
    m = re.search(r'[?&]v=([0-9A-Za-z_-]{11})', u) or re.search(r'youtu\.be/([0-9A-Za-z_-]{11})', u)
    return m.group(1) if m else u


def _duration_value(value):
    if value in (None, '', 'NA', 'None'):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _probe_duration_command(command, url, timeout):
    try:
        res = subprocess.run(
            [*map(str, command), '--ignore-config', '--no-warnings',
             '--skip-download', '--no-playlist', '--print', '%(duration)s', url],
            capture_output=True, text=True, timeout=timeout)
        out = (res.stdout or '').strip().splitlines()
        return _duration_value(out[0].strip() if out else None)
    except Exception:
        return None


def _probe_duration_isolated_python(url, timeout):
    """Run the installed yt_dlp module under the current interpreter without cwd imports."""
    executable = Path(sys.executable)
    if not executable.is_absolute() or not executable.is_file():
        return None
    return _probe_duration_command(
        (executable, '-I', '-m', 'yt_dlp'),
        url,
        timeout,
    )


def _probe_duration_explicit_path(url, timeout):
    """Fallback to a user-managed yt-dlp only when an absolute PATH entry names it."""
    names = ('yt-dlp.exe',) if os.name == 'nt' else ('yt-dlp',)
    executable = explicit_path_executable(names)
    if executable is None:
        return None
    return _probe_duration_command((executable,), url, timeout)


def probe_duration(url, timeout=30):
    """Lazily probe a video's duration (seconds) via yt-dlp. Returns int or None on any failure.
    Only the duration-filter callers invoke this, and only when a duration bound is set.
    Successful results are memoized by video id (durations are immutable) so re-polling the same
    RSS window doesn't repeat the network probe per video every cycle."""
    key = _duration_cache_key(url)
    with _DURATION_CACHE_LOCK:
        if key in _DURATION_CACHE:
            return _DURATION_CACHE[key]
    started = time.monotonic()
    dur = _probe_duration_isolated_python(url, timeout)
    remaining = timeout - (time.monotonic() - started)
    if dur is None and remaining > 0:
        dur = _probe_duration_explicit_path(url, remaining)
    if dur is not None:
        with _DURATION_CACHE_LOCK:
            if len(_DURATION_CACHE) >= _DURATION_CACHE_MAX:
                _DURATION_CACHE.clear()
            _DURATION_CACHE[key] = dur
    return dur


def parse_feed(xml_text):
    """Parse channel RSS XML -> list of {id, title, url, published} (newest first)."""
    try:
        return _entries(ET.fromstring(xml_text))
    except ET.ParseError:
        return []


def parse_feed_full(xml_text):
    """-> (channel_title, [videos])."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return (None, [])
    title = root.findtext('atom:title', namespaces=_NS)
    return ((title or '').strip() or None, _entries(root))


def fetch_feed(channel_id, timeout=15):
    """-> (channel_title, [videos]) in one request."""
    return parse_feed_full(_get(feed_url(channel_id), timeout))


def fetch_videos(channel_id, timeout=15):
    return fetch_feed(channel_id, timeout)[1]


def load_archive(path):
    p = Path(path)
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()}


def append_archive(path, video_id):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(video_id + '\n')
