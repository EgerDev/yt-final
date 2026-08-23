#!/usr/bin/env python3
"""
Chapter -> NLE timeline-marker export (the differentiator).

Turns YouTube chapters (already captured in the downloader's `last_info` at zero network cost)
into editor-importable marker sidecars:
  - EDL   : CMX3600 with DaVinci Resolve `* LOC:` marker lines.
  - CSV   : Premiere Pro marker-import format (Marker Name, Description, In, Out, Duration, Type).
  - FCPXML: chapter markers for Final Cut Pro / Premiere / Resolve.

stdlib only. No tool in the competitive set exports NLE-native markers — every other downloader
only writes chapters *into the container*; this writes a file the editor imports onto the timeline.
"""

import io
import csv
import logging
import re
from pathlib import Path
from xml.sax.saxutils import escape as _xesc

logger = logging.getLogger(__name__)


# C0 control bytes that are ILLEGAL in XML 1.0 (everything below 0x20 except tab/LF/CR). saxutils
# does not strip these, so a chapter title carrying e.g. \x1b yields an unparseable .fcpxml.
_XML_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _xattr(s):
    """Escape for an XML ATTRIBUTE value: saxutils.escape() does NOT escape the double-quote that
    delimits the attribute (a title with a " breaks the FCPXML) NOR strip XML-illegal control bytes
    (a title with a \\x1b makes the whole file unparseable). Handle both. Tab/LF/CR are kept."""
    return _xesc(_XML_ILLEGAL.sub('', str(s)), {'"': '&quot;'})


# DaVinci Resolve marker colors, cycled per chapter.
_COLORS = ['Red', 'Green', 'Blue', 'Cyan', 'Magenta', 'Yellow', 'Fuchsia', 'Rose',
           'Lavender', 'Sky', 'Mint', 'Lemon', 'Sand', 'Cocoa', 'Cream']

# SponsorBlock category -> stable Resolve marker color (NOT the positional cycle).
# Categories follow yt-dlp's `sponsorblock_chapters[*]['category']` values.
_SB_COLORS = {
    'sponsor': 'Red',
    'selfpromo': 'Yellow',
    'intro': 'Cyan',
    'outro': 'Cyan',
    'interaction': 'Magenta',
    'preview': 'Lavender',
    'music_offtopic': 'Green',
    'filler': 'Sand',
}
_SB_DEFAULT_COLOR = 'Blue'


def _fps_int(fps):
    try:
        f = int(round(float(fps)))
    except (TypeError, ValueError):
        f = 0
    return f or 25


def _fps_rational(fps):
    """Resolve an fps scalar to a true rational rate (num, den, is_drop).

    Recovers the NTSC fractional rates that yt-dlp reports as rounded/approximate floats so
    timecodes don't drift: 23.976->24000/1001 (NDF), 29.97->30000/1001 (DROP), 59.94->60000/1001
    (DROP). Plain integer rates (24/25/30/50/60) map to (round(f), 1, False) so existing
    non-drop math stays byte-identical.
    """
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return (25, 1, False)
    if f <= 0:
        return (25, 1, False)
    for target, rat in ((23.976, (24000, 1001, False)),
                        (23.98, (24000, 1001, False)),
                        (29.97, (30000, 1001, True)),
                        (59.94, (60000, 1001, True))):
        if abs(f - target) < 0.01:
            return rat
    return (int(round(f)), 1, False)


def _timecode_df(seconds, num, den):
    """SMPTE drop-frame HH:MM:SS;FF (29.97 drops 2 frames/min, 59.94 drops 4; never on the 10th min)."""
    nominal = int(round(num / den))            # 30 or 60
    drop = nominal // 15                        # 2 for 30, 4 for 60
    # Count ACTUAL elapsed frames at the true rate (seconds * num/den), then apply the drop-frame
    # label renumbering. This keeps the code aligned to wall-clock across long spans (3600s ->
    # 01:00:00;00); counting nominal 30/60 slots drifts (-> 01:00:03;18) and defeats drop-frame.
    frame_number = int(round(max(0.0, float(seconds)) * num / den))
    frames_per_min = nominal * 60
    frames_per_10min = frames_per_min * 10 - drop * 9
    d, m = divmod(frame_number, frames_per_10min)
    if m > drop - 1:
        frame_number += drop * 9 * d + drop * ((m - drop) // (frames_per_min - drop))
    else:
        frame_number += drop * 9 * d
    ff = frame_number % nominal
    total = frame_number // nominal
    ss, total = total % 60, total // 60
    mm, hh = total % 60, total // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"


def _timecode(seconds, fps):
    """Frame-accurate timecode. Drop-frame (HH:MM:SS;FF) for 29.97/59.94, else NDF (HH:MM:SS:FF).

    Frame indices are laid at the true rational rate (so 23.976 lays at frame 24/s NDF without
    drifting), but the nominal integer rate governs the FF wraparound."""
    num, den, drop = _fps_rational(fps)
    if drop:
        return _timecode_df(seconds, num, den)
    nominal = int(round(num / den))
    total = int(round(max(0.0, float(seconds)) * num / den))
    ff = total % nominal
    total //= nominal
    ss, total = total % 60, total // 60
    mm, hh = total % 60, total // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _norm(chapters):
    out = []
    for i, ch in enumerate(chapters or [], 1):
        try:
            start = float(ch.get('start_time', ch.get('start', 0)) or 0)
        except (TypeError, ValueError):
            continue
        end = ch.get('end_time', ch.get('end'))
        try:
            end = float(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        # Collapse any embedded newlines: a multi-line chapter title would otherwise break the
        # single-line CMX3600 `* LOC:` record (and the CSV/FCPXML rows). Single chokepoint.
        title = ' '.join((ch.get('title') or ch.get('name') or f'Chapter {i}').splitlines()).strip() or f'Chapter {i}'
        # Prefer a pre-set color (e.g. SponsorBlock category color); else cycle by index.
        color = ch.get('color') or _COLORS[(i - 1) % len(_COLORS)]
        out.append({'i': i, 'start': start, 'end': end, 'title': title, 'color': color})
    return out


def to_edl(chapters, fps=25.0, title='Timeline'):
    """CMX3600 EDL with Resolve marker (`* LOC:`) lines."""
    num, den, drop = _fps_rational(fps)
    chs = _norm(chapters)
    fcm = "FCM: DROP FRAME" if drop else "FCM: NON-DROP FRAME"
    lines = [f"TITLE: {title}", fcm, ""]
    for ch in chs:
        tc_in = _timecode(ch['start'], fps)
        tc_out = _timecode(ch['start'] + 1.0 * den / num, fps)   # 1-frame event keeps the EDL valid
        color = ch['color'].upper()
        lines.append(f"{ch['i']:03d}  AX       V     C        {tc_in} {tc_out} {tc_in} {tc_out}")
        lines.append(f"* LOC: {tc_in} {color} {ch['title']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def to_csv(chapters, fps=25.0, source_url=None):
    """Premiere Pro / Resolve marker-import CSV.

    Columns 1-6 (Marker Name, Description, In, Out, Duration, Marker Type) keep their exact
    header text and order — Premiere's importer is header/position-driven, so we only APPEND
    'Marker Color' (7th column, read by Resolve) and never reorder. Description carries a note
    (source URL + 1-based chapter index) so the marker traces back to its origin."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Marker Name', 'Description', 'In', 'Out', 'Duration', 'Marker Type', 'Marker Color'])
    for ch in _norm(chapters):
        tc_in = _timecode(ch['start'], fps)
        end = ch['end'] if ch['end'] is not None else ch['start']
        tc_out = _timecode(end, fps)
        dur = _timecode(max(0.0, end - ch['start']), fps)
        note = f"{source_url} #{ch['i']}" if source_url else f"#{ch['i']}"
        w.writerow([ch['title'], note, tc_in, tc_out, dur, 'Comment', ch['color']])
    return buf.getvalue()


def to_fcpxml(chapters, fps=25.0, duration=None, title='Timeline'):
    """Minimal FCPXML 1.10 with chapter markers (FCP / Premiere / Resolve importable)."""
    num, den, drop = _fps_rational(fps)
    chs = _norm(chapters)

    def rt(sec):
        return f"{int(round(float(sec) * num / den)) * den}/{num}s"

    if duration is None:
        duration = max((c['end'] or c['start'] for c in chs), default=0) + 1
    markers = "\n".join(
        f'              <chapter-marker start="{rt(c["start"])}" value="{_xattr(c["title"])}"/>'
        for c in chs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.10">\n'
        '  <resources>\n'
        f'    <format id="r1" frameDuration="{den}/{num}s"/>\n'
        '  </resources>\n'
        '  <library>\n'
        f'    <event name="{_xattr(title)}">\n'
        f'      <project name="{_xattr(title)}">\n'
        '        <sequence format="r1">\n'
        '          <spine>\n'
        f'            <gap name="Gap" offset="0s" duration="{rt(duration)}" start="0s">\n'
        f'{markers}\n'
        '            </gap>\n'
        '          </spine>\n'
        '        </sequence>\n'
        '      </project>\n'
        '    </event>\n'
        '  </library>\n'
        '</fcpxml>\n'
    )


def _vtt_ts(seconds):
    """WEBVTT cue timestamp HH:MM:SS.mmm (millisecond precision, not frame-based)."""
    s = max(0.0, float(seconds))
    ms = int(round(s * 1000))
    hh, ms = divmod(ms, 3600000)
    mm, ms = divmod(ms, 60000)
    ss, ms = divmod(ms, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def to_vtt(chapters):
    """WEBVTT chapter cue file (Jellyfin/Plex/browser chapter sidecar)."""
    chs = _norm(chapters)
    lines = ["WEBVTT", ""]
    for idx, ch in enumerate(chs):
        start = ch['start']
        if ch['end'] is not None:
            end = ch['end']
        elif idx + 1 < len(chs):
            end = chs[idx + 1]['start']
        else:
            end = start + 1.0
        if end <= start:
            end = start + 1.0   # WEBVTT rejects zero-length cues
        lines.append(str(ch['i']))
        lines.append(f"{_vtt_ts(start)} --> {_vtt_ts(end)}")
        lines.append(ch['title'])
        lines.append("")
    return "\n".join(lines) + "\n"


# format key -> (filename suffix, generator). The generator takes (chs, fps, dur, title, url);
# only the CSV writer uses `url` (source-URL note), the others ignore it.
_FORMATS = {
    'edl': ('.edl', lambda chs, fps, dur, title, url: to_edl(chs, fps, title)),
    'csv': ('.markers.csv', lambda chs, fps, dur, title, url: to_csv(chs, fps, url)),
    'fcpxml': ('.fcpxml', lambda chs, fps, dur, title, url: to_fcpxml(chs, fps, dur, title)),
}

# SponsorBlock sidecars use distinct suffixes so they don't clobber the chapter marker files.
_SB_SUFFIX = {'edl': '.sponsorblock.edl', 'csv': '.sponsorblock.csv', 'fcpxml': '.sponsorblock.fcpxml'}


def parse_formats(spec):
    """'edl,csv' / 'all' / ['edl'] -> ordered list of valid format keys."""
    if not spec:
        return []
    if isinstance(spec, (list, tuple)):
        items = [str(x).strip().lower() for x in spec]
    else:
        items = [x.strip().lower() for x in str(spec).replace(';', ',').split(',')]
    if 'all' in items:
        return ['edl', 'csv', 'fcpxml']
    seen, out = set(), []
    for it in items:
        if it in _FORMATS and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def write_markers(media_path, chapters, fps=25.0, formats=('edl',), title=None, duration=None,
                  source_url=None, suffix_map=None):
    """Write marker sidecar(s) next to `media_path`. Returns the list of written Paths
    (empty if there are no chapters or no valid formats).

    `source_url` is woven into the CSV Description note. `suffix_map` overrides the per-format
    filename suffix (used by the SponsorBlock writer to avoid clobbering chapter sidecars)."""
    chs = _norm(chapters)
    if not chs:
        return []
    media_path = Path(media_path)
    title = title or media_path.stem
    written = []
    for fmt in parse_formats(formats):
        suffix, gen = _FORMATS[fmt]
        if suffix_map and fmt in suffix_map:
            suffix = suffix_map[fmt]
        dst = media_path.with_name(media_path.stem + suffix)
        try:
            # newline='' is load-bearing, not style: the CSV generator emits RFC-4180 CRLF line
            # endings, and Path.write_text() opens in translating text mode — so on Windows every
            # '\n' becomes '\r\n', turning the generator's '\r\n' into '\r\r\n' and producing
            # malformed CSV that Premiere/Resolve reject. Writing byte-faithfully keeps each
            # generator's intended endings (CSV=CRLF, EDL/FCPXML=LF) identical on every platform.
            with dst.open('w', encoding='utf-8', newline='') as fh:
                fh.write(gen(chs, fps, duration, title, source_url))
            written.append(dst)
        except OSError as e:
            # Log the real failure (long-path/permission/disk-full) so it isn't silently conflated
            # with 'no valid --export-markers format' by the caller's empty-list diagnostic.
            logger.warning("[Markers] failed to write %s: %s", dst, e)
    return written


def write_text_sidecars(media_path, description=None, chapters=None):
    """Write <stem>.description.txt and/or <stem>.chapters.vtt next to media_path.
    Returns the list of written Paths (skips empties; OSErrors are swallowed)."""
    media_path = Path(media_path)
    written = []
    desc = (description or '').strip()
    if desc:
        dst = media_path.with_name(media_path.stem + '.description.txt')
        try:
            # newline='' — see write_markers: a CR-bearing YouTube description would otherwise be
            # doubled to '\r\r\n' by Windows text-mode translation.
            dst.write_text(desc + '\n', encoding='utf-8', newline='')
            written.append(dst)
        except OSError as e:
            logger.warning("[Sidecar] failed to write %s: %s", dst, e)
    chs = _norm(chapters)
    if chs:
        dst = media_path.with_name(media_path.stem + '.chapters.vtt')
        try:
            # newline='' — see write_markers: byte-faithful, so identical chapters produce identical
            # bytes on Windows and macOS. to_vtt emits '\n'; translation made this the only all-CRLF
            # sidecar of the six.
            dst.write_text(to_vtt(chapters), encoding='utf-8', newline='')
            written.append(dst)
        except OSError as e:
            logger.warning("[Sidecar] failed to write %s: %s", dst, e)
    return written


def sponsorblock_to_chapters(sb_chapters):
    """Normalize yt-dlp `sponsorblock_chapters` into the chapter-dict shape `_norm` consumes.

    Each SB segment -> {'start_time','end_time','title','color'} where the title is a human label
    like '[Sponsor]' and the color is the stable per-category color (not the positional cycle).
    One marker per SB segment (no synthesized content spans). Returns [] for empty/missing input."""
    out = []
    for seg in (sb_chapters or []):
        if not seg:
            continue
        cat = (seg.get('category') or '').strip().lower()
        if seg.get('start_time') is None:
            continue
        label = '[' + (cat.replace('_', ' ').title() if cat else 'Segment') + ']'
        out.append({
            'start_time': seg.get('start_time'),
            'end_time': seg.get('end_time'),
            'title': label,
            'color': _SB_COLORS.get(cat, _SB_DEFAULT_COLOR),
        })
    return out


def write_sponsorblock_markers(media_path, sb_chapters, fps=25.0, formats=('csv',),
                               title=None, duration=None, source_url=None):
    """Emit color-coded SponsorBlock category markers as NLE sidecar(s) with `.sponsorblock.*`
    suffixes (distinct from the chapter marker files). Returns the list of written Paths
    (empty when there is no SponsorBlock data or no valid format)."""
    chs = sponsorblock_to_chapters(sb_chapters)
    if not chs:
        return []
    return write_markers(media_path, chs, fps=fps, formats=formats, title=title,
                         duration=duration, source_url=source_url, suffix_map=_SB_SUFFIX)
