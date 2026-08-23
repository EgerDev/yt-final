#!/usr/bin/env python3
"""
Kodi/Jellyfin/Plex .nfo sidecar generation from a yt-dlp info dict.

Writes a `<video>.nfo` next to the media so self-hosted media servers (Jellyfin, Kodi, and Plex
with a local-metadata/NFO agent) show the real title, description, uploader, date and thumbnail
instead of the raw filename. Uses the widely-read Kodi <movie> schema. stdlib only.
"""

import os
import re
import time
import logging
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# C0 control bytes ILLEGAL in XML 1.0 (below 0x20 except tab/LF/CR). ElementTree emits Element.text
# verbatim, so a YouTube title/description carrying e.g. \x1b produces a .nfo that Kodi/Jellyfin/Plex
# reject as not-well-formed — while write_nfo() reports success. Strip them; keep tab/LF/CR so
# multi-line descriptions survive.
_XML_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _xml_safe(value):
    """Coerce to str and strip XML-1.0-illegal control bytes so element text can't corrupt the file."""
    return _XML_ILLEGAL.sub('', str(value))


def _fmt_date(upload_date):
    s = str(upload_date or '')
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _show_title(info):
    """Resolve the series/show title from the playlist or channel metadata."""
    return (info.get('playlist') or info.get('playlist_title')
            or info.get('channel') or info.get('uploader'))


def _common_fields(root, info, add):
    """Append the fields shared by <movie> and <episodedetails> (kept in one place so the two
    schemas never diverge): plot/outline, studio/credits, dates, runtime, genres/tags, thumb,
    uniqueid, source link."""
    add('plot', info.get('description'))
    # Strip first: a whitespace-only description ('   ') is truthy and would otherwise yield an
    # empty-looking <outline>   </outline>. After .strip() it becomes '' which add() drops.
    desc = (info.get('description') or '').strip()
    add('outline', desc[:300] or None)
    uploader = info.get('uploader')
    add('studio', uploader)
    add('director', uploader)
    if uploader:
        ET.SubElement(root, 'credits').text = _xml_safe(uploader)

    premiered = _fmt_date(info.get('upload_date'))
    if premiered:
        add('premiered', premiered)
        add('aired', premiered)
        add('year', premiered[:4])

    dur = info.get('duration')
    if isinstance(dur, (int, float)) and dur > 0:
        add('runtime', int(round(dur / 60)))   # Kodi runtime is in minutes

    for g in (info.get('categories') or []):
        ET.SubElement(root, 'genre').text = _xml_safe(g)
    for t in (info.get('tags') or [])[:25]:
        ET.SubElement(root, 'tag').text = _xml_safe(t)

    thumb = info.get('thumbnail')
    if thumb:
        ET.SubElement(root, 'thumb').text = _xml_safe(thumb)

    vid = info.get('id')
    if vid:
        uid = ET.SubElement(root, 'uniqueid', {'type': 'youtube', 'default': 'true'})
        uid.text = _xml_safe(vid)
    if info.get('webpage_url'):
        add('trailer', info['webpage_url'])   # source link; harmless, surfaces the origin URL
    add('source', 'YouTube')


def _serialize(root):
    # pretty-print (stdlib indent is 3.9+)
    try:
        ET.indent(root, space='  ')
    except Exception:
        pass
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
        ET.tostring(root, encoding='unicode') + '\n'


def _to_movie_nfo(info):
    """Build a Kodi <movie> NFO XML string from a (distilled) yt-dlp info dict."""
    root = ET.Element('movie')

    def add(tag, value):
        if value not in (None, '', []):
            el = ET.SubElement(root, tag)
            el.text = _xml_safe(value)

    add('title', info.get('title'))
    add('originaltitle', info.get('title'))
    _common_fields(root, info, add)
    return _serialize(root)


def _to_episode_nfo(info):
    """Build a Kodi/Jellyfin <episodedetails> NFO string (playlist video as a series episode)."""
    root = ET.Element('episodedetails')

    def add(tag, value):
        if value not in (None, '', []):
            el = ET.SubElement(root, tag)
            el.text = _xml_safe(value)

    add('title', info.get('title'))
    add('showtitle', _show_title(info))
    add('season', 1)   # YouTube has no seasons; Kodi/Jellyfin require an int -> fixed at 1
    ep = info.get('playlist_index')
    if isinstance(ep, str) and ep.isdigit():
        ep = int(ep)
    if isinstance(ep, int):
        add('episode', ep)   # omit when absent rather than fabricate a wrong number
    _common_fields(root, info, add)
    return _serialize(root)


def _nfo_kind(info, kind):
    """Resolve the effective schema. auto -> episode when the video is part of a playlist/series."""
    info = info or {}
    if kind == 'movie':
        return 'movie'
    if kind == 'episode':
        return 'episode'
    # auto
    if info.get('playlist') or info.get('playlist_index') or info.get('playlist_title'):
        return 'episode'
    return 'movie'


def to_nfo(info, kind='auto'):
    """Build an NFO XML string. `kind` is auto|movie|episode (auto picks episode for playlist videos).
    Default 'auto' on a non-playlist video stays byte-identical to the legacy <movie> output."""
    info = info or {}
    resolved = _nfo_kind(info, kind)
    if resolved == 'episode':
        return _to_episode_nfo(info)
    return _to_movie_nfo(info)


def to_tvshow_nfo(info):
    """Build a Kodi/Jellyfin <tvshow> NFO string for a playlist/channel treated as a series.
    Returns None when no show title can be resolved."""
    info = info or {}
    title = _show_title(info)
    if not title:
        return None
    root = ET.Element('tvshow')

    def add(tag, value):
        if value not in (None, '', []):
            el = ET.SubElement(root, tag)
            el.text = _xml_safe(value)

    add('title', title)
    add('showtitle', title)
    # Show-level metadata must come from playlist/channel fields, NOT the per-video (episode) info
    # dict. write_tvshow_nfo() writes a single shared tvshow.nfo once per downloaded item, so under
    # concurrent playlist downloads every episode's thread targets the same file; deriving plot/date
    # from an episode's own description/upload_date would make the show's plot and premiered date
    # nondeterministic (whichever episode wrote last wins) and semantically wrong (an episode
    # description shown as the show plot). Use the playlist-level description when yt-dlp provides it,
    # and omit plot/premiered/year entirely rather than falling back to per-video values.
    add('plot', info.get('playlist_description'))
    studio = info.get('channel') or info.get('uploader')
    add('studio', studio)
    sid = info.get('playlist_id') or info.get('channel_id')
    if sid:
        uid = ET.SubElement(root, 'uniqueid', {'type': 'youtube', 'default': 'true'})
        uid.text = _xml_safe(sid)
    add('source', 'YouTube')
    return _serialize(root)


def write_nfo(media_path, info, kind='auto'):
    """Write `<media stem>.nfo` next to the media file. Returns the Path, or None on failure/no-data.
    `kind` (auto|movie|episode) selects the <movie> or <episodedetails> schema."""
    if not info or not info.get('title'):
        return None
    media_path = Path(media_path)
    dst = media_path.with_name(media_path.stem + '.nfo')
    try:
        # newline='' is load-bearing, not style (mirrors markers.write_markers): the XML text mode
        # would otherwise translate every '\n' to '\r\n' on Windows, so a description carrying a
        # literal CR (YouTube CRLF, legally kept by _xml_safe) becomes '\r\r\n' and the plot shows a
        # doubled blank line in Kodi/Jellyfin/Plex. Byte-faithful keeps the serializer's endings.
        dst.write_text(to_nfo(info, kind=kind), encoding='utf-8', newline='')
        return dst
    except OSError as e:
        # Surface the real cause (long-path/permission/disk-full) instead of silently dropping it,
        # so callers don't mistake a write failure for 'no NFO data to write'.
        logger.warning("[NFO] failed to write %s: %s", dst, e)
        return None


def write_tvshow_nfo(dir_path, info):
    """Write `tvshow.nfo` into the media's directory (idempotent overwrite). Returns Path or None.

    The write is atomic (temp file + os.replace): a single shared tvshow.nfo is written once per
    downloaded playlist item, so concurrent episode threads can otherwise race and leave a torn file.
    os.replace publishes a fully-written copy in one step; combined with show-level (playlist) metadata
    the content is identical across episodes, so the last atomic replace is deterministic and valid."""
    xml = to_tvshow_nfo(info)
    if not xml:
        return None
    dst = Path(dir_path) / 'tvshow.nfo'
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix='.tvshow.', suffix='.nfo.tmp')
        # newline='' — see write_nfo: avoid Windows CRLF-doubling of a CR-bearing plot/description.
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
            fh.write(xml)
        # Retry the publish like every other os.replace in this codebase: concurrent episode
        # threads all target this ONE shared file, and a media-server scanner (Jellyfin/Kodi)
        # watching the folder can hold it open for a moment — a single attempt silently dropped
        # the tvshow.nfo where a 50 ms retry succeeds.
        for attempt in range(8):
            try:
                os.replace(tmp, dst)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05)
        return dst
    except OSError as e:
        logger.warning("[NFO] failed to write %s: %s", dst, e)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return None
