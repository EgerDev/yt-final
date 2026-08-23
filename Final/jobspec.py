#!/usr/bin/env python3
"""
JobSpec — the typed job contract for yt_final (keystone K1).

A single, declarative description of "download these URL(s) and turn them into
edit-ready media this way". It is the *one* object that flows through the engine,
the batch manifest, the persistent queue and the HTTP server, so that adding a new
knob is "add a field + consume it" instead of widening a 13-argument method.

Design rules:
- stdlib only (no third-party imports) so it is import-cheap on the CLI fast-path.
- tolerant `from_dict` (ignores unknown keys, coerces strings from JSON/TOML/forms).
- `to_dict` round-trips cleanly for the JSON job journal and the web UI.
- defaults reproduce today's behaviour exactly (smart 1080p H.264 copy/remux), so a
  bare JobSpec(urls=[...]) is byte-for-byte the legacy `download_then_process` path.
"""

import functools
import math
from dataclasses import dataclass, field, fields, asdict
from typing import Optional, List


def _as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ('1', 'true', 'yes', 'y', 'on'):
        return True
    if s in ('0', 'false', 'no', 'n', 'off', ''):
        return False
    return default


def _as_int(v, default=None):
    if v is None or v == '':
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default=None):
    if v is None or v == '':
        return default
    try:
        converted = float(v)
    except OverflowError as error:
        raise ValueError("number must be within the finite float domain") from error
    except (TypeError, ValueError):
        return default
    if not math.isfinite(converted):
        raise ValueError("number must be within the finite float domain")
    return converted


def _as_list(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    # comma/space separated string
    parts = [p.strip() for p in str(v).replace(';', ',').split(',')]
    out = [p for p in parts if p]
    return out or None


def _as_url_list(v):
    """Like _as_list but for URLs: NEVER split on comma/semicolon — a single YouTube URL can
    contain them (consent/playlist params) and would otherwise be shredded into bogus entries.
    A string is split only on whitespace/newlines (how the UI already separates multiple URLs)."""
    if v is None:
        return None
    items = [str(x).strip() for x in v] if isinstance(v, (list, tuple)) else str(v).split()
    out = [x for x in items if x]
    return out or None


VALID_DOWNLOAD_ENGINES = frozenset({'auto', 'hybrid', 'builtin', 'aria2', 'turbo'})


def normalize_download_engine(v) -> str:
    """Map UI/CLI aliases to a canonical download_engine value."""
    s = str(v or 'auto').strip().lower().replace('_', '-').replace(' ', '-')
    aliases = {
        'auto': 'auto',
        'default': 'auto',
        'smart': 'auto',
        'hybrid': 'hybrid',
        'builtin': 'builtin',
        'built-in': 'builtin',
        'native': 'builtin',
        'yt-dlp': 'builtin',
        'ytdlp': 'builtin',
        'aria2': 'aria2',
        'aria2c': 'aria2',
        'turbo': 'turbo',
        'n-m3u8dl-re': 'turbo',
        'n-m3u8dl': 'turbo',
        'nm3u8': 'turbo',
        'm3u8dl': 'turbo',
    }
    eng = aliases.get(s, s)
    return eng if eng in VALID_DOWNLOAD_ENGINES else 'auto'


def pick_download_engine(requested, *, aria2_available=False, turbo_available=False):
    """Choose the effective engine policy and a short human reason (for logs / UI).

    Returns (effective_engine, reason).
    - auto → builtin (safe default; does not auto-enable hybrid/turbo/aria2)
    - hybrid → smart path after format extract (opt-in only)
    - Forced engines fall back to builtin if the binary is missing
    """
    eng = normalize_download_engine(requested)
    if eng == 'auto':
        return 'builtin', 'auto → built-in (native parallel fragments)'
    if eng == 'hybrid':
        bits = ['hybrid: built-in dual-DASH']
        if turbo_available:
            bits.append('turbo if single playlist')
        if aria2_available:
            bits.append('aria2 if progressive HTTP')
        return 'hybrid', ' · '.join(bits)
    if eng == 'aria2':
        if aria2_available:
            return 'aria2', 'aria2 multi-connection (progressive HTTP; DASH may stay native)'
        return 'builtin', 'aria2 requested but aria2c not found → built-in'
    if eng == 'turbo':
        if turbo_available:
            return 'turbo', 'turbo (N_m3u8DL-RE multi-thread HLS)'
        return 'builtin', 'turbo requested but N_m3u8DL-RE not found → built-in'
    return 'builtin', 'built-in (native parallel fragments)'


@dataclass(frozen=True)
class DownloadEngineDecision:
    """One immutable post-extraction fetch decision.

    Selection is intentionally separate from option mutation/execution so Hybrid can
    be exhaustively tested and parallel jobs cannot influence each other's routing.
    """

    path: str
    stream_kind: str
    reason: str
    executable: Optional[str] = None


def decide_download_engine(policy, stream_kind, *, aria_path=None, turbo_path=None,
                           aria2_ok=True, aria2_reason='', turbo_ok=True,
                           turbo_reason='', builtin_pinned=False):
    """Return the safe fetch decision for an already-classified resolved stream."""
    policy = normalize_download_engine(policy)
    kind = str(stream_kind or 'unknown')

    def builtin(reason):
        return DownloadEngineDecision('builtin', kind, reason)

    if builtin_pinned:
        return builtin('built-in pinned after optional-engine failure')
    if policy in ('auto', 'builtin'):
        return builtin('built-in native parallel fragments')

    if policy == 'aria2':
        if kind != 'progressive':
            return builtin(f'aria2 incompatible with {kind}')
        if not aria_path:
            return builtin('aria2c not installed')
        if not aria2_ok:
            return builtin(f'aria2 skipped: {aria2_reason or "job is incompatible"}')
        return DownloadEngineDecision(
            'aria2', kind, 'aria2 progressive HTTP(S)', str(aria_path))

    if policy == 'turbo':
        if kind != 'fragment_playlist':
            return builtin(f'turbo incompatible with {kind}')
        if not turbo_path:
            return builtin('N_m3u8DL-RE not installed')
        if not turbo_ok:
            return builtin(f'turbo skipped: {turbo_reason or "job is incompatible"}')
        return DownloadEngineDecision(
            'turbo', kind, 'turbo single HLS playlist', str(turbo_path))

    # Hybrid preserves the selected quality. High-quality YouTube DASH remains
    # native; it is never downgraded to a progressive format just to activate aria2.
    if kind in ('multi_dash', 'single_dash'):
        return builtin(f'hybrid uses native yt-dlp for {kind}')
    if kind == 'progressive':
        return decide_download_engine(
            'aria2', kind, aria_path=aria_path,
            aria2_ok=aria2_ok, aria2_reason=aria2_reason)
    if kind == 'fragment_playlist':
        return decide_download_engine(
            'turbo', kind, turbo_path=turbo_path,
            turbo_ok=turbo_ok, turbo_reason=turbo_reason)
    return builtin(f'hybrid uses built-in for {kind}')


def classify_stream_kind(info) -> str:
    """Classify resolved yt-dlp info into a download path kind.

    Returns one of:
      multi_dash        — separate video+audio (typical YouTube 1080p+) → built-in only
      single_dash       — one video- or audio-only HTTPS component (itag 137-style)
                          → built-in (native -N / range requests beat aria2 here)
      fragment_playlist — HLS playlist → turbo; DASH/ambiguous manifest → built-in
      progressive       — single progressive file with BOTH audio+video → aria2 or built-in
      unknown           — fall back to built-in

    Important: YouTube DASH video-only streams often have protocol='https' with no
    fragments list. Those must NOT be classified as progressive (aria2 would steal
    the download and underperform native -N).
    """
    if not info:
        return 'unknown'
    reqs = info.get('requested_formats') or []
    if len(reqs) > 1:
        return 'multi_dash'

    f = reqs[0] if reqs else info
    if not isinstance(f, dict):
        f = info if isinstance(info, dict) else {}
    proto = str(f.get('protocol') or '').lower()
    url = str(f.get('url') or (info.get('url') if isinstance(info, dict) else '') or '').lower()
    has_frags = bool(f.get('fragments') or f.get('fragment_base_url'))
    # Prefer format-level codecs; only fall back to top-level info when there is no
    # requested_formats entry (older / single-format info dicts).
    if reqs:
        vcodec = str(f.get('vcodec') or '')
        acodec = str(f.get('acodec') or '')
    else:
        vcodec = str(f.get('vcodec') or info.get('vcodec') or '')
        acodec = str(f.get('acodec') or info.get('acodec') or '')
    has_v = bool(vcodec and vcodec != 'none')
    has_a = bool(acodec and acodec != 'none')

    # Explicit playlist / segment manifests (before progressive check)
    if (
        has_frags
        or 'm3u8' in proto
        or 'http_dash_segments' in proto
        or ('dash' in proto and 'https' not in proto)  # mpd/dash protocol names
        or 'm3u8' in url
        or '.mpd' in url
        or '/manifest' in url
        or 'manifest.' in url
    ):
        return 'fragment_playlist'

    # Classic progressive: one file carries video AND audio (e.g. itag 18/22)
    if has_v and has_a and (proto in ('http', 'https', '') or url.startswith('http')):
        return 'progressive'

    # Single-media-component HTTPS URL (typical DASH itag) → native range downloader
    if has_v or has_a:
        return 'single_dash'

    # A bare HTTP(S) URL without codec evidence is ambiguous. It may be a media
    # component, redirect, or generic file, so Hybrid must keep it on built-in
    # rather than assuming it contains both video and audio.
    return 'unknown'


@dataclass
class JobSpec:
    # --- source ---
    urls: List[str] = field(default_factory=list)

    # --- destination ---
    output_dir: Optional[str] = None        # where to save (None == the engine/server default)

    # --- selection (existing behaviour) ---
    quality: str = "1080p"                 # best|4k|1440p|1080p|720p|480p|audio
    codec: Optional[str] = None            # h264|vp9|av1|any  (None == h264 default)
    encode_mode: str = "copy"              # none|copy|reencode|proxy_pair
    profile: str = "auto_h264"             # semantic default: fastest working H.264 for this machine (resolved at encode time)
    section: Optional[str] = None          # "0:00-2:00" or "90-180"
    section_precise: bool = False          # force_keyframes_at_cuts (slower, frame-accurate)
    format_id: Optional[str] = None        # exact yt-dlp format id (e.g. "137+140"); overrides quality/codec selection

    # --- batch / network plumbing (existing) ---
    parallel: bool = False
    workers: Optional[int] = None
    speed_priority: bool = False
    cookies_from_browser: Optional[str] = None
    cookiefile: Optional[str] = None
    # When True (default), blank cookiefile → inject Auth-tab session cookies automatically.
    # When False, this job stays anonymous unless cookiefile is set explicitly (override).
    use_auth: bool = True
    slow_requests: bool = False
    source_address: Optional[str] = None
    proxy: Optional[str] = None                     # R4: --proxy
    concurrent_fragments: Optional[int] = None      # static -N override (DO-NOT-BUILD said no live AIMD)
    # How to pull bytes after yt-dlp picks formats:
    #   auto     — default: same as builtin (native -N)
    #   hybrid   — opt-in smart path: dual-DASH→builtin, playlist→turbo, progressive→aria2
    #   builtin  — yt-dlp concurrent fragments only
    #   aria2    — force aria2c when available
    #   turbo    — force N_m3u8DL-RE when available (single playlist only)
    download_engine: str = "auto"

    # --- archive / de-dup (C3) ---
    download_archive: Optional[str] = None

    # --- audio (A1) ---
    audio_format: Optional[str] = None      # audio-only target: mp3|aac|opus|flac|wav|m4a|alac|best
    audio_quality: Optional[str] = None     # bitrate "192" / "320" or "0".."10" VBR
    audio_codec: Optional[str] = None       # override the audio codec of a *video* profile (copy|aac|pcm_s24le|flac|libopus)
    audio_bitrate: Optional[str] = None     # override the audio bitrate of a video profile
    audio_map: Optional[str] = None         # None|stereo|dual-mono|split: split hard-panned L/R mics into two mono tracks (MOV/MKV)

    # --- timeline metadata (B1/B2/B3/B6) ---
    chapters: bool = True                   # YouTube chapters -> container chapter markers
    embed_metadata: bool = True             # title/uploader/date/id/description -> container tags
    embed_thumbnail: bool = False           # cover art
    sub_langs: Optional[List[str]] = None   # e.g. ["en", "en-orig"]; None == off
    sub_mode: Optional[str] = None          # sidecar|embed|burn
    sub_auto: bool = False                  # also fetch auto-generated captions
    sub_format: str = 'srt'                 # srt|vtt|both: subtitle sidecar format(s)
    write_sidecar: bool = False             # write the full info.json next to the media (MAM provenance)
    write_sidecar_text: bool = False        # write <stem>.description.txt + <stem>.chapters.vtt
    export_markers: Optional[str] = None    # YouTube chapters -> NLE markers: edl|csv|fcpxml|all (comma list)
    sponsorblock_markers: Optional[str] = None  # SponsorBlock categories -> NLE markers: edl|csv|fcpxml|all
    no_shorts: bool = False                 # skip YouTube Shorts (<=60s or /shorts/ URLs)
    write_nfo: bool = False                 # write a Kodi/Jellyfin/Plex .nfo sidecar
    burn_in: bool = False                   # burn a drawtext slate (title/uploader/date/URL) + source timecode (forces re-encode)
    nfo_kind: str = 'auto'                  # auto|movie|episode: NFO schema (auto picks episode for playlist videos)
    write_artwork: bool = False             # write sibling poster/fanart/folder.jpg from the thumbnail
    proxy_pair: bool = False                # also produce a half-res H.264 editing proxy
    notify_url: Optional[str] = None        # POST a JSON job-complete payload to this URL (safe webhook)
    notify_template: str = 'raw'            # discord|slack|ntfy|raw — shape of the notify_url POST

    # --- sponsor / loudness / color (C8 / A4 / A2) ---
    sponsorblock: Optional[str] = None      # None|mark|remove  (categories handled by engine)
    loudnorm: Optional[float] = None        # target integrated LUFS, e.g. -14.0
    preserve_hdr: bool = True               # carry HDR10/HLG color metadata through copy/reencode
    color_tag: str = 'auto'                 # 'auto'|'off': stamp Rec.709 on untagged SDR (reencode only)

    # --- integrity (post-download verification) ---
    verify_strict: bool = False             # ffprobe the output; FAIL the job if duration/streams/size look wrong

    # --- rate control (A7) ---
    two_pass: bool = False
    bitrate: Optional[str] = None           # target for two-pass / constrained VBR, e.g. "12M"

    # --- output organisation (B7) ---
    output_template: Optional[str] = None   # yt-dlp outtmpl override
    organize_by: Optional[str] = None       # "uploader" | "date" -> subfolder

    # --- automation (R4) ---
    on_complete: Optional[str] = None       # shell command run per finished file ({} -> path)

    # ------------------------------------------------------------------ #
    @classmethod
    @functools.lru_cache(maxsize=None)
    def _field_names(cls):
        return frozenset(f.name for f in fields(cls))

    @classmethod
    def from_dict(cls, d):
        """Build a JobSpec from an arbitrary dict (manifest/queue/web form/CLI).
        Unknown keys are ignored; strings are coerced to the right type."""
        if d is None:
            return cls()
        if isinstance(d, cls):
            return d
        known = cls._field_names()
        # accept a couple of friendly aliases
        alias = {'url': 'urls', 'mode': 'encode_mode', 'quality_level': 'quality'}
        clean = {}
        for k, v in dict(d).items():
            key = alias.get(k, k)
            if key in known:
                clean[key] = v

        spec = cls()
        spec.urls = _as_url_list(clean.get('urls')) or []

        spec.output_dir = clean.get('output_dir', spec.output_dir) or None
        spec.quality = str(clean.get('quality', spec.quality) or spec.quality)
        spec.codec = clean.get('codec', spec.codec) or None
        spec.encode_mode = str(clean.get('encode_mode', spec.encode_mode) or spec.encode_mode)
        spec.profile = str(clean.get('profile', spec.profile) or spec.profile)
        raw_section = clean.get('section', spec.section) or None
        if isinstance(raw_section, str):
            raw_section = raw_section.strip() or None
        spec.section = raw_section
        spec.section_precise = _as_bool(clean.get('section_precise'), spec.section_precise)
        spec.format_id = clean.get('format_id', spec.format_id) or None

        spec.parallel = _as_bool(clean.get('parallel'), spec.parallel)
        spec.workers = _as_int(clean.get('workers'), spec.workers)
        spec.speed_priority = _as_bool(clean.get('speed_priority'), spec.speed_priority)
        spec.cookies_from_browser = clean.get('cookies_from_browser', spec.cookies_from_browser) or None
        spec.cookiefile = clean.get('cookiefile', spec.cookiefile) or None
        # Default True when key omitted (back-compat for older queue rows / CLI).
        if 'use_auth' in clean:
            spec.use_auth = _as_bool(clean.get('use_auth'), True)
        else:
            spec.use_auth = True
        spec.slow_requests = _as_bool(clean.get('slow_requests'), spec.slow_requests)
        spec.source_address = clean.get('source_address', spec.source_address) or None
        spec.proxy = clean.get('proxy', spec.proxy) or None
        spec.concurrent_fragments = _as_int(clean.get('concurrent_fragments'), spec.concurrent_fragments)
        if 'download_engine' in clean:
            spec.download_engine = normalize_download_engine(clean.get('download_engine'))
        else:
            spec.download_engine = 'auto'

        spec.download_archive = clean.get('download_archive', spec.download_archive) or None

        spec.audio_format = clean.get('audio_format', spec.audio_format) or None
        spec.audio_quality = clean.get('audio_quality', spec.audio_quality)
        if spec.audio_quality is not None:
            spec.audio_quality = str(spec.audio_quality)
        spec.audio_codec = clean.get('audio_codec', spec.audio_codec) or None
        spec.audio_bitrate = clean.get('audio_bitrate', spec.audio_bitrate) or None
        am = clean.get('audio_map', spec.audio_map)
        spec.audio_map = am if am in ('stereo', 'dual-mono', 'split') else None

        spec.chapters = _as_bool(clean.get('chapters'), spec.chapters)
        spec.embed_metadata = _as_bool(clean.get('embed_metadata'), spec.embed_metadata)
        spec.embed_thumbnail = _as_bool(clean.get('embed_thumbnail'), spec.embed_thumbnail)
        spec.sub_langs = _as_list(clean.get('sub_langs')) if 'sub_langs' in clean else spec.sub_langs
        spec.sub_mode = clean.get('sub_mode', spec.sub_mode) or None
        spec.sub_auto = _as_bool(clean.get('sub_auto'), spec.sub_auto)
        spec.sub_format = str(clean.get('sub_format', spec.sub_format) or 'srt').lower()
        if spec.sub_format not in ('srt', 'vtt', 'both'):
            spec.sub_format = 'srt'
        spec.write_sidecar = _as_bool(clean.get('write_sidecar'), spec.write_sidecar)
        spec.write_sidecar_text = _as_bool(clean.get('write_sidecar_text'), spec.write_sidecar_text)
        spec.export_markers = clean.get('export_markers', spec.export_markers) or None
        spec.sponsorblock_markers = clean.get('sponsorblock_markers', spec.sponsorblock_markers) or None
        spec.no_shorts = _as_bool(clean.get('no_shorts'), spec.no_shorts)
        spec.write_nfo = _as_bool(clean.get('write_nfo'), spec.write_nfo)
        spec.burn_in = _as_bool(clean.get('burn_in'), spec.burn_in)
        spec.nfo_kind = clean.get('nfo_kind', spec.nfo_kind) or 'auto'
        if spec.nfo_kind not in ('auto', 'movie', 'episode'):
            spec.nfo_kind = 'auto'
        spec.write_artwork = _as_bool(clean.get('write_artwork'), spec.write_artwork)
        spec.proxy_pair = _as_bool(clean.get('proxy_pair'), spec.proxy_pair)
        spec.notify_url = clean.get('notify_url', spec.notify_url) or None
        spec.notify_template = clean.get('notify_template', spec.notify_template) or 'raw'
        if spec.notify_template not in ('discord', 'slack', 'ntfy', 'raw'):
            spec.notify_template = 'raw'

        spec.sponsorblock = clean.get('sponsorblock', spec.sponsorblock) or None
        spec.loudnorm = _as_float(clean.get('loudnorm'), spec.loudnorm)
        spec.preserve_hdr = _as_bool(clean.get('preserve_hdr'), spec.preserve_hdr)
        spec.color_tag = clean.get('color_tag', spec.color_tag) or 'auto'
        if spec.color_tag not in ('auto', 'off'):
            spec.color_tag = 'auto'
        spec.verify_strict = _as_bool(clean.get('verify_strict'), spec.verify_strict)

        spec.two_pass = _as_bool(clean.get('two_pass'), spec.two_pass)
        spec.bitrate = clean.get('bitrate', spec.bitrate) or None

        spec.output_template = clean.get('output_template', spec.output_template) or None
        spec.organize_by = clean.get('organize_by', spec.organize_by) or None
        spec.on_complete = clean.get('on_complete', spec.on_complete) or None
        return spec

    def to_dict(self):
        """Plain JSON-serialisable dict (for the queue journal + web UI)."""
        return asdict(self)

    def merged(self, **overrides):
        """Return a copy with selected fields overridden (used by config/preset layering)."""
        d = self.to_dict()
        for k, v in overrides.items():
            if k in d and v is not None:
                d[k] = v
        return JobSpec.from_dict(d)

    @classmethod
    def from_args(cls, args):
        """Adapter from an argparse Namespace (the `download` subcommand)."""
        get = lambda n, default=None: getattr(args, n, default)
        return cls.from_dict({
            'urls': get('urls', []),
            'quality': get('quality', '1080p'),
            'codec': get('codec'),
            'encode_mode': get('encode_mode', 'copy'),
            'profile': get('profile', 'auto_h264'),
            'section': get('section'),
            'section_precise': get('section_precise', False),
            'format_id': get('format_id'),
            'parallel': get('parallel', False),
            'workers': get('workers'),
            'speed_priority': get('speed_priority', False),
            'cookies_from_browser': get('cookies_from_browser'),
            'cookiefile': get('cookiefile'),
            'use_auth': get('use_auth', True),
            'slow_requests': get('slow_requests', False),
            'source_address': get('source_address'),
            'proxy': get('proxy'),
            'concurrent_fragments': get('concurrent_fragments'),
            'download_engine': get('download_engine', 'auto'),
            'download_archive': get('download_archive'),
            'audio_format': get('audio_format'),
            'audio_quality': get('audio_quality'),
            'audio_codec': get('audio_codec'),
            'audio_bitrate': get('audio_bitrate'),
            'audio_map': get('audio_map'),
            'chapters': get('chapters', True),
            'embed_metadata': get('embed_metadata', True),
            'embed_thumbnail': get('embed_thumbnail', False),
            'sub_langs': get('sub_langs'),
            'sub_mode': get('sub_mode'),
            'sub_auto': get('sub_auto', False),
            'sub_format': get('sub_format', 'srt'),
            'write_sidecar': get('write_sidecar', False),
            'write_sidecar_text': get('write_sidecar_text', False),
            'export_markers': get('export_markers'),
            'sponsorblock_markers': get('sponsorblock_markers'),
            'no_shorts': get('no_shorts', False),
            'write_nfo': get('write_nfo', False),
            'burn_in': get('burn_in', False),
            'nfo_kind': get('nfo_kind', 'auto'),
            'write_artwork': get('write_artwork', False),
            'proxy_pair': get('proxy_pair', False),
            'notify_url': get('notify_url'),
            'notify_template': get('notify_template', 'raw'),
            'sponsorblock': get('sponsorblock'),
            'loudnorm': get('loudnorm'),
            'preserve_hdr': get('preserve_hdr', True),
            'color_tag': get('color_tag', 'auto'),
            'verify_strict': get('verify_strict', False),
            'two_pass': get('two_pass', False),
            'bitrate': get('bitrate'),
            'output_template': get('output_template'),
            'organize_by': get('organize_by'),
            'on_complete': get('on_complete'),
        })

    def summary(self):
        """Short one-line human description for logs/queue listings."""
        n = len(self.urls)
        extras = []
        if self.section:
            extras.append(f"section={self.section}")
        if self.format_id:
            extras.append(f"fmt={self.format_id}")
        if self.encode_mode != 'copy':
            extras.append(self.encode_mode)
        if self.audio_format:
            extras.append(f"audio={self.audio_format}")
        if self.loudnorm is not None:
            extras.append(f"{self.loudnorm}LUFS")
        if self.sub_mode:
            extras.append(f"subs:{self.sub_mode}")
        if self.on_complete:
            # Deliberately LAST and deliberately verbatim. `--dry-run` is advertised as a preview
            # (Final/examples/jobs.example.json), and this field runs a program when the job
            # finishes -- so a preview that omitted it was the one omission that mattered. Shown in
            # full rather than truncated: a reader cannot judge `curl … | sh` from its first token.
            extras.append(f"on-complete={self.on_complete!r}")
        tail = (" [" + ", ".join(extras) + "]") if extras else ""
        head = self.urls[0] if n == 1 else f"{n} urls"
        return f"{head} @ {self.quality}/{self.profile if self.encode_mode=='reencode' else self.codec or 'h264'}{tail}"

    def config_warnings(self):
        """Human-readable warnings for knobs that are ACCEPTED by the contract but not yet honored by
        the encoder, so a request never silently no-ops. The reserved A7 rate-control fields
        (`two_pass`, video `bitrate`) are parsed and persisted but no encoder consumes them yet — the
        CPU profiles are CRF/constant-quality (where two-pass VBR does not apply) and NVENC already
        does hardware multipass via its profile. Pure + side-effect-free so it's directly testable;
        callers (engine run(), the HTTP /api/jobs response) surface the list."""
        warnings = []
        if self.two_pass:
            warnings.append(
                "two_pass is not implemented yet — this job will encode single-pass. The CPU profiles "
                "are CRF/constant-quality (two-pass VBR targets a bitrate, so it doesn't apply); NVENC "
                "already multipasses via its profile.")
        if self.bitrate:
            warnings.append(
                f"bitrate override ('{self.bitrate}') is not applied yet — the encoder uses the "
                "selected profile's rate control.")
        return warnings
