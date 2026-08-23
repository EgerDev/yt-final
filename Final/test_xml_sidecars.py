"""XML sidecar well-formedness: control bytes in titles/descriptions must not corrupt .nfo/.fcpxml.

Meta-audit findings (fmt-nfo, fmt-markers): ElementTree / saxutils emit XML-1.0-illegal C0 control
bytes verbatim, so a YouTube title carrying e.g. \\x1b produced a sidecar that Kodi/Jellyfin/Plex/FCP
reject as not-well-formed — while write_*() reported success. The fix strips the illegal class but
keeps tab/LF/CR so multi-line descriptions survive.
"""
import xml.etree.ElementTree as ET

import nfo
import markers


def _parse(xml_str):
    # Encode to bytes so the <?xml ... encoding=...?> declaration is accepted by fromstring.
    return ET.fromstring(xml_str.encode("utf-8"))


def test_nfo_strips_illegal_control_chars_but_keeps_whitespace():
    info = {
        "title": "My\x1bVideo\x08!",                      # ESC + BACKSPACE — illegal in XML 1.0
        "description": "line one\nline two\twith tab\x00nul",
        "id": "abcdEFGH123",
    }
    xml = nfo.to_nfo(info, kind="movie")
    root = _parse(xml)                                    # must NOT raise (was unparseable before)
    assert "\x1b" not in xml and "\x08" not in xml and "\x00" not in xml
    title = root.findtext("title")
    assert title == "MyVideo!"                            # control bytes gone, real chars kept
    plot = root.findtext("plot")
    assert "\n" in plot and "\t" in plot                 # multi-line/whitespace preserved
    assert "nul" in plot and "\x00" not in plot
    print("test_nfo_strips_illegal_control_chars_but_keeps_whitespace passed")


def test_fcpxml_strips_illegal_control_chars_in_titles():
    chapters = [{"start": 0.0, "title": "Intro\x1b"}, {"start": 10.0, "title": "Part \x0c2"}]
    xml = markers.to_fcpxml(chapters, fps=25.0, duration=60.0, title="Time\x1bline")
    root = _parse(xml)                                    # must NOT raise
    assert "\x1b" not in xml and "\x0c" not in xml
    print("test_fcpxml_strips_illegal_control_chars_in_titles passed")


def test_nfo_writers_are_byte_faithful_no_crlf_doubling(tmp_path):
    """write_nfo / write_tvshow_nfo must be byte-faithful (newline='') so a description carrying a
    literal CR (YouTube CRLF, which _xml_safe legally keeps) is written whole, not doubled to
    '\\r\\r\\n' on Windows's translating text mode. That doubling is well-formed but XML EOL
    normalization (spec 2.11) then collapses #xD#xD#xA to two newlines, so Kodi/Jellyfin/Plex show a
    doubled blank line in the plot. markers.write_markers already guards this with newline='' and has
    its own regression test; the .nfo writers must match. Fails on Windows without the fix; a no-op
    pass on POSIX (linesep is '\\n', so no translation happens there either way)."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"")
    info = {
        "title": "Ep",
        "description": "line one\r\nline two",
        "id": "abcdEFGH123",
        "playlist": "Show",
        "playlist_description": "show a\r\nshow b",
        "playlist_id": "PL_abcdEFGH",
    }
    p = nfo.write_nfo(media, info)
    raw = p.read_bytes()
    assert b"\r\r\n" not in raw                                  # not doubled
    assert ET.fromstring(raw).findtext("plot") == "line one\nline two"   # single blank line

    t = nfo.write_tvshow_nfo(tmp_path, info)
    assert t is not None
    assert b"\r\r\n" not in t.read_bytes()
    print("test_nfo_writers_are_byte_faithful_no_crlf_doubling passed")


def test_description_txt_is_byte_faithful_no_crlf_doubling(tmp_path):
    """write_text_sidecars's .description.txt must not double CRLF to '\\r\\r\\n' on Windows (same
    translating-text-mode trap as the CSV markers)."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"")
    written = markers.write_text_sidecars(media, description="alpha\r\nbeta")
    desc = next(pp for pp in written if pp.name.endswith(".description.txt"))
    assert b"\r\r\n" not in desc.read_bytes()
    print("test_description_txt_is_byte_faithful_no_crlf_doubling passed")


if __name__ == "__main__":
    test_nfo_strips_illegal_control_chars_but_keeps_whitespace()
    test_fcpxml_strips_illegal_control_chars_in_titles()


def test_chapters_vtt_is_byte_faithful_no_platform_drift(tmp_path):
    """CONFIRMED: .chapters.vtt was the ONE sidecar writer of six still lacking newline='', so it came
    out all-CRLF on Windows and all-LF on macOS — a pointless byte difference for the same input, and
    inconsistent with its siblings (markers + .description.txt both pass newline=''). WebVTT content is
    generated with '\n', so byte-faithful means LF everywhere."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"")
    chapters = [{"start": 0.0, "title": "Intro"}, {"start": 10.0, "title": "Part 2"}]
    written = markers.write_text_sidecars(media, description="d", chapters=chapters)
    vtt = next(p for p in written if p.name.endswith(".chapters.vtt"))
    raw = vtt.read_bytes()
    assert raw.startswith(b"WEBVTT"), raw[:20]
    assert b"\r\n" not in raw, "must be byte-faithful LF, not CRLF-translated"
    print("test_chapters_vtt_is_byte_faithful_no_platform_drift passed")


def test_ffmetadata_preserves_escaped_newline_continuation(tmp_path):
    """CONFIRMED: write_ffmetadata omitted newline='', so a value containing an escaped newline was
    emitted as 'title=line one\\\r\nline two' — the backslash escapes the CR, not the LF, and ffmpeg
    then TRUNCATES the tag to 'line one\r'. Latent today (tags are whitespace-collapsed first) but it
    is real data loss the moment a multi-line value reaches it, e.g. a raw chapter title."""
    import yt_final
    proc = yt_final.FFmpegProcessor.__new__(yt_final.FFmpegProcessor)
    dst = tmp_path / "m.ffmeta"
    out = proc.write_ffmetadata(dst, tags={"title": "line one\nline two"}, chapters=None)
    assert out, "writer returned None"
    raw = dst.read_bytes()
    assert b"\r" not in raw, f"CR must not appear (breaks escaped-newline continuation): {raw!r}"
    # the continuation must be BACKSLASH immediately followed by LF — byte-explicit so this assertion
    # cannot itself be confused by escaping
    assert b"title=line one" + b"\\" + b"\n" + b"line two" in raw, raw
    print("test_ffmetadata_preserves_escaped_newline_continuation passed")


def test_uploaded_cookies_txt_is_not_crlf_doubled(monkeypatch, tmp_path):
    """CONFIRMED: an uploaded cookies.txt (CRLF, as every Windows browser extension produces) was
    written back with DOUBLED carriage returns (\r\r\n) because write_text used default newline
    translation — same bug class as the .nfo one, on the auth store's active.txt."""
    import webserver
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    content = ("# Netscape HTTP Cookie File\r\n"
               ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tabc123\r\n"
               ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tHSID\tdef456\r\n")
    res = webserver.auth_import_cookies(content)
    assert res.get("ok"), res
    import cookies_auth
    dest = cookies_auth.cookie_store_dir() / "active.txt"
    raw = dest.read_bytes()
    assert b"\r\r\n" not in raw, f"CRLF doubled in the stored cookie jar: {raw[:80]!r}"
    print("test_uploaded_cookies_txt_is_not_crlf_doubled passed")


def test_write_tvshow_nfo_retries_windows_sharing_violation(tmp_path, monkeypatch):
    """CONFIRMED: this was the only os.replace site in the sidecar/store modules WITHOUT the
    codebase's 8-attempt PermissionError retry (jobstore, watchstore, settings and cookies_auth all
    have it) — despite its own docstring naming concurrent episode threads on one shared file, and
    despite media-server scanners (Jellyfin/Kodi) watching for exactly this filename. One transient
    sharing violation silently dropped tvshow.nfo (except OSError -> warn -> None)."""
    nfo_mod = nfo

    attempts = {'n': 0}
    real_replace = nfo_mod.os.replace

    def flaky_replace(src, dst):
        attempts['n'] += 1
        if attempts['n'] < 3:                       # transient AV/scanner hold, then success
            raise PermissionError(32, 'The process cannot access the file')
        return real_replace(src, dst)

    monkeypatch.setattr(nfo_mod.os, 'replace', flaky_replace)
    monkeypatch.setattr(nfo_mod.time, 'sleep', lambda *_a: None)
    out = nfo_mod.write_tvshow_nfo(tmp_path, {'playlist_title': 'Show', 'uploader': 'Chan'})
    assert out is not None and out.exists(), 'a transient sharing violation must be retried'
    assert attempts['n'] == 3, attempts

    # a PERSISTENT violation still gives up honestly and leaves no temp file behind
    attempts['n'] = 0
    monkeypatch.setattr(nfo_mod.os, 'replace',
                        lambda s, d: (_ for _ in ()).throw(PermissionError(32, 'held')))
    assert nfo_mod.write_tvshow_nfo(tmp_path, {'playlist_title': 'Show'}) is None
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('.tvshow.')], list(tmp_path.iterdir())
