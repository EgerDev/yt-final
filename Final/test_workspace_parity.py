"""Batch C — engine capabilities that had no way in from the Workspace.

C1 (channel / playlist): download_archive and organize_by both exist in JobSpec, are honoured by the
engine and are on the CLI, but had ZERO references anywhere in webui/ — so the repeat-pull workflow
("only grab what's new") was CLI-only. No server change was needed: _web_jobspec_from_payload does
dict(payload) -> JobSpec.from_dict, which keeps any known field.

The load-bearing part is not the controls, it is telling the truth about when the archive does
NOTHING. Three verified engine facts:
  * applied only when there is no clip — `if download_archive and not section` (yt_final.py:3294),
    because a clip must not mark the whole video id as done;
  * turbo refuses it outright — 'download_archive requires built-in' (yt_final.py:4195);
  * the public-mode path guard validates download_archive with expand_vars=True but output_dir with
    expand_vars=False (webserver.py:620 vs 613), so a "$VAR" folder must NOT be used to derive an
    archive path — the derived path could resolve somewhere the folder itself would not.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HTML = (HERE / "webui" / "index.html").read_text(encoding="utf-8")
JS = (HERE / "webui" / "app.js").read_text(encoding="utf-8")
CSS = (HERE / "webui" / "style.css").read_text(encoding="utf-8")


def test_channel_workflow_controls_are_reachable():
    for cid in ("adv_batch", "download_archive_on", "download_archive",
                "download_archive_browse", "organize_by", "archive_note", "batch_summary_hint"):
        assert 'id="' + cid + '"' in HTML, "missing control/element #" + cid
    spec = JS.split("function buildSpec(", 1)[1].split("\n}", 1)[0]
    # colon form: a comment mentioning the field must not satisfy this
    assert "download_archive:" in spec, "download_archive not wired into buildSpec"
    assert "organize_by:" in spec, "organize_by not wired into buildSpec"
    assert "archivePlanFromForm()" in spec, "the archive path must come from the planner"
    assert ".archive-note" in CSS, "the archive state line needs styling"


def test_archive_plan_logic_executes_correctly():
    """EXECUTE the planner. Its whole job is branching truth ("on but doing nothing"), which a source
    assertion cannot verify — the same class of gap that let two regressions ship earlier today."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = "function archivePlan(" + JS.split("function archivePlan(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = fn + r"""
var P = archivePlan;
console.log(JSON.stringify({
  off:                P(false, '', '/dl', '', 'auto'),
  off_wins:           P(false, '/a/b.txt', '/dl', '', 'auto'),
  derive_blank:       P(true, '', '', '', 'auto'),
  derive_posix:       P(true, '', '/Volumes/Media/dl', '', 'auto'),
  derive_posix_slash: P(true, '', '/Volumes/Media/dl/', '', 'auto'),
  derive_windows:     P(true, '', 'D:\\Footage', '', 'auto'),
  explicit:           P(true, '/keep/mine.txt', '/dl', '', 'auto'),
  dollar_folder:      P(true, '', '$HOME/dl', '', 'auto'),
  dollar_explicit:    P(true, '/keep/mine.txt', '$HOME/dl', '', 'auto'),
  clip:               P(true, '', '/dl', '0:00-1:00', 'auto'),
  turbo:              P(true, '', '/dl', '', 'turbo'),
  clip_and_turbo:     P(true, '', '/dl', '0:00-1:00', 'turbo')
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # OFF means no archive at all, and the checkbox is the switch even with a path typed
    assert g["off"]["path"] is None and g["off"]["level"] == "idle", g["off"]
    assert g["off_wins"]["path"] is None, g["off_wins"]

    # derivation keeps the folder's own separator so a Windows path never comes back half-POSIX
    assert g["derive_blank"]["path"] == "./downloads/yt-final-archive.txt", g["derive_blank"]
    assert g["derive_posix"]["path"] == "/Volumes/Media/dl/yt-final-archive.txt", g["derive_posix"]
    assert g["derive_posix_slash"]["path"] == "/Volumes/Media/dl/yt-final-archive.txt", g["derive_posix_slash"]
    assert g["derive_windows"]["path"] == "D:\\Footage\\yt-final-archive.txt", g["derive_windows"]
    assert g["explicit"]["path"] == "/keep/mine.txt", g["explicit"]

    # SECURITY: never derive from a $VAR folder (the expand_vars asymmetry in the confinement guard)
    assert g["dollar_folder"]["path"] is None, g["dollar_folder"]
    assert g["dollar_folder"]["level"] == "bad", g["dollar_folder"]
    assert g["dollar_explicit"]["level"] == "ok", g["dollar_explicit"]

    # TRUTH-TELLING: on, but the engine will not apply it
    assert g["clip"]["level"] == "warn", g["clip"]
    assert "clip" in g["clip"]["note"].lower(), g["clip"]
    assert g["turbo"]["level"] == "warn", g["turbo"]
    assert "turbo" in g["turbo"]["note"].lower(), g["turbo"]
    assert g["clip_and_turbo"]["level"] == "warn", g["clip_and_turbo"]
    # ...and a healthy job says so plainly
    assert g["derive_posix"]["level"] == "ok", g["derive_posix"]


def test_every_input_the_archive_note_reads_also_repaints_it():
    """archivePlanFromForm() reads five inputs. If one of them is not registered on the repaint, the
    note keeps asserting a STALE truth — e.g. still saying "already-downloaded videos are skipped"
    after the user switched to Turbo, which cannot use the archive at all. Nothing else fails: the
    planner is correct, the controls exist, buildSpec is wired, and every other test stays green.

    Found while live-testing: writing #section directly left the note stale, which is harmless (it is
    a hidden field written only by syncClipRange, which runs immediately before the note repaint) —
    but it showed this invariant was resting on nothing but the four ids happening to be in a list."""
    reads = JS.split("function archivePlanFromForm(", 1)[1].split("\n}", 1)[0]
    read_ids = set(re.findall(r"\$\('([a-z_]+)'\)", reads))
    assert read_ids == {"download_archive_on", "download_archive", "output_dir",
                        "download_engine", "section"}, read_ids

    # the repaint registration list: the array literal that ends in the forEach binding the listeners
    reg = JS.split("].forEach((id) => {", 1)[0]
    reg = reg[reg.rindex("['quality'"):]
    registered = set(re.findall(r"'([a-z_]+)'", reg))
    missing = (read_ids - registered) - {"section"}      # #section is covered via section_start/end
    assert not missing, "the archive note reads these but is never repainted when they change: " + str(sorted(missing))
    # ...and #section's own coverage really is via the clip fields, so keep them registered too
    assert {"section_start", "section_end"} <= registered, registered
    # the ordering the note depends on: #section is populated by syncClipRange, so the note must be
    # repainted AFTER that call, not before (otherwise it reads the previous clip on every change)
    summary = JS.split("function updateComposeSummary(", 1)[1].split("\nfunction ", 1)[0]
    assert summary.index("syncClipRange(") < summary.index("syncArchiveNote("), \
        "syncArchiveNote must run after syncClipRange — it reads the #section that call writes"


def test_turbo_really_falls_back_so_the_ui_note_is_not_a_lie():
    """The archive note tells the user turbo "falls back to the built-in downloader". That is a claim
    about the ENGINE, so pin the engine: _turbo_compatible returns (False, 'download_archive requires
    built-in') and decide_download_engine turns a not-ok turbo into builtin rather than an error. If
    that ever becomes a hard refusal, this fails and the wording must change with it."""
    from jobspec import decide_download_engine

    d = decide_download_engine('turbo', 'fragment_playlist', turbo_path='/x/N_m3u8DL-RE',
                               turbo_ok=False, turbo_reason='download_archive requires built-in')
    assert d.path == 'builtin', d                        # falls back, does not raise
    assert 'download_archive' in d.reason, d.reason       # ...and says why
    # sanity: with nothing objecting, turbo is still chosen (so the fallback above is meaningful)
    assert decide_download_engine('turbo', 'fragment_playlist',
                                  turbo_path='/x/N_m3u8DL-RE').path == 'turbo'


# ---- C2: verify_strict ------------------------------------------------------------------------
# format_id is deliberately NOT exposed. It exists in JobSpec (jobspec.py:279) and on the CLI, but a
# raw itag box is a footgun four independent ways, each verified in the engine: it silently beats both
# the Max-resolution and Preferred-stream dropdowns (yt_final.py:3290); an id YouTube will not serve
# does NOT fail the job — the retry planner swaps in 'bestvideo*+bestaudio/best' (yt_final.py:5133) so
# a typo quietly pulls the biggest stream; a video-only id yields a file with no audio; and combined
# with verify_strict it ALWAYS fails, because an audio stream is required unconditionally
# (yt_final.py:6292). The probe pop-out already applies heights and codecs in one click, so the
# escape hatch stays CLI-only (--format-id). Do not "complete the set" without re-reading those.


def test_verify_strict_is_reachable_and_the_help_states_both_traps():
    assert 'id="verify_strict"' in HTML, "the strict-verify checkbox is missing"
    assert 'type="checkbox" id="verify_strict"' in HTML, "must be a checkbox (JobSpec coerces a bool)"
    spec = JS.split("function buildSpec(", 1)[1].split("\n}", 1)[0]
    assert "verify_strict:" in spec, "verify_strict not wired into buildSpec"

    # The help paragraph is the only place a user learns the two things they cannot guess. Both are
    # engine behaviour pinned by the tests below, so the wording must not drift away from them.
    help_txt = HTML.split('id="verify_strict"', 1)[1].split("</details>", 1)[0].lower()
    assert "ffprobe is missing" in help_txt, "must say a missing ffprobe FAILS the job (fails closed)"
    assert "download only" in help_txt, \
        "must say Delivery='Download only' skips the check entirely — omitting this ships a lie"
    assert "skips verification" in help_txt, "point at the Will… line that shows it"


def test_download_only_reports_verification_as_skipped():
    """EXECUTE extrasSkippedNote. Under Delivery='Download only' the checkbox is a SILENT no-op — not
    a lenient warning, no check at all — so the compose receipt has to name it alongside the other
    extras that mode drops. A source assertion cannot tell 'listed' from 'listed in the wrong mode'."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function extrasSkippedNote("
          + JS.split("function extrasSkippedNote(", 1)[1].split("\n}", 1)[0] + "\n}")
    harness = fn + r"""
var CHECKED = {};
function $(id) { return { type: 'checkbox', get checked() { return !!CHECKED[id]; } }; }
function run(ids, mode) { CHECKED = {}; ids.forEach(function (i) { CHECKED[i] = true; }); return extrasSkippedNote(mode); }
console.log(JSON.stringify({
  none_verify_only:  run(['verify_strict'], 'none'),
  none_with_others:  run(['verify_strict', 'chapters'], 'none'),
  none_unchecked:    run([], 'none'),
  none_others_only:  run(['chapters'], 'none'),
  copy_verify:       run(['verify_strict'], 'copy'),
  reencode_verify:   run(['verify_strict'], 'reencode')
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    assert "verification" in g["none_verify_only"], g["none_verify_only"]
    assert "verification" in g["none_with_others"] and "chapters" in g["none_with_others"], g["none_with_others"]
    # ...and ONLY when it is actually on, and ONLY in the mode that drops it
    assert "verification" not in g["none_unchecked"], g["none_unchecked"]
    assert "verification" not in g["none_others_only"], g["none_others_only"]
    assert g["copy_verify"] == "" and g["reencode_verify"] == "", g


def test_strict_verification_fails_closed_and_requires_an_audio_stream():
    """EXECUTE the verifier. Two claims the help text makes about the ENGINE:
      * a missing/failed ffprobe FAILS a strict job (it must not quietly pass a file it never read);
      * an audio stream is required, which is the reason format_id stays unexposed — a video-only id
        plus this checkbox can never succeed.
    probe_integrity is the only ffprobe touch point, so injecting it exercises the real branching."""
    import yt_final

    def verifier(probe_result, **spec_attrs):
        v = yt_final.FinalYouTubeGPUEngine.__new__(yt_final.FinalYouTubeGPUEngine)
        v.processor = type("P", (), {"probe_integrity": staticmethod(lambda _p: probe_result)})()
        v.downloader = type("D", (), {"last_info": {}})()
        spec = type("S", (), dict({"verify_strict": False, "section": None,
                                   "quality": "1080p"}, **spec_attrs))()
        return yt_final.FinalYouTubeGPUEngine._verify_output(v, "out.mp4", "raw.mp4", spec, info={})

    GOOD = {"size": 5_000_000, "has_video": True, "has_audio": True}

    # ffprobe unavailable -> {} : strict FAILS CLOSED, non-strict degrades to a pass
    ok, why = verifier({}, verify_strict=True)
    assert ok is False and "strict verification cannot pass" in why, (ok, why)
    assert verifier({}, verify_strict=False)[0] is True, "non-strict must still degrade gracefully"

    # an audio stream is required even for a strict-off job (this is why format_id is a trap)
    assert verifier(dict(GOOD, has_audio=False))[0] is False
    assert "no audio stream" in verifier(dict(GOOD, has_audio=False))[1]

    # video is required unless the job is audio-only, and the size floor is 1 KiB
    assert verifier(dict(GOOD, has_video=False))[0] is False
    assert verifier(dict(GOOD, has_video=False), quality="audio")[0] is True
    assert verifier(dict(GOOD, size=1023))[0] is False
    # a healthy file passes
    assert verifier(GOOD, verify_strict=True)[0] is True


def test_download_only_never_reaches_the_verifier():
    """The help text claims Delivery='Download only' skips the check ENTIRELY. That rests on two
    structural facts: _run_body returns before the _finalize loop when encode_mode is 'none', and
    _verify_output is reachable from nowhere else. If a second call site ever appears, or the early
    return moves below the loop, the UI text becomes a lie — so fail here instead."""
    engine = (HERE / "yt_final.py").read_text(encoding="utf-8")
    calls = [ln for ln in engine.splitlines() if "self._verify_output(" in ln]
    assert len(calls) == 1, "verify is reachable from more than one place now: " + str(calls)
    body = engine.split("def _finalize(", 1)[1].split("\n    def ", 1)[0]
    assert "self._verify_output(" in body, "_verify_output must live inside _finalize"
    # the early return for 'none' must come BEFORE the loop that calls _finalize
    run_body = engine.split('if spec.encode_mode == "none":', 1)
    assert len(run_body) == 2, "the download-only early return is gone — revisit the help text"
    assert "return downloaded" in run_body[1].split("for src in downloaded", 1)[0], \
        "encode_mode 'none' must return before the _finalize loop"


def test_ui_derived_archive_passes_the_public_mode_path_guard(tmp_path, monkeypatch):
    """The archive is validated with expand_vars=True and base=None (CWD-relative), while output_dir is
    expand_vars=False. Deriving the archive INSIDE Save-to-folder is what makes it pass the public
    guard exactly when the folder does — this pins that, and pins that a bare relative name (which
    yt-dlp would open next to the process, outside the root) is still rejected."""
    import webserver
    root = tmp_path / "dl"
    root.mkdir()
    monkeypatch.chdir(tmp_path)                 # CWD outside the root, like a real public server

    ok = webserver._web_jobspec_from_payload(
        {"urls": ["https://youtu.be/x"], "output_dir": str(root),
         "download_archive": str(root / "yt-final-archive.txt"), "organize_by": "uploader"},
        confine_root=str(root))
    assert ok.download_archive == str(root / "yt-final-archive.txt")
    assert ok.organize_by == "uploader"

    with pytest.raises(webserver.PathPolicyError):   # bare relative -> opened outside the root
        webserver._web_jobspec_from_payload(
            {"urls": ["https://youtu.be/x"], "output_dir": str(root),
             "download_archive": "yt-final-archive.txt"},
            confine_root=str(root))


def test_verify_strict_and_rip_bitrate_controls_are_reachable():
    """C2/C3 reduced: JobSpec.verify_strict (jobspec.py:340) and JobSpec.audio_quality
    (jobspec.py:307) are honoured by the engine and are on the CLI, but had ZERO references in
    webui/. No server change was needed: _web_jobspec_from_payload goes through JobSpec.from_dict,
    which keeps verify_strict (jobspec.py:453) and coerces audio_quality to str (jobspec.py:413)."""
    for cid in ("verify_strict", "audio_quality", "audio_note"):
        assert 'id="' + cid + '"' in HTML, "missing control/element #" + cid
    spec = JS.split("function buildSpec(", 1)[1].split("\n}", 1)[0]
    assert "verify_strict:" in spec, "verify_strict not wired into buildSpec"
    assert "audio_quality:" in spec, "audio_quality not wired into buildSpec"
    assert ".audio-note" in CSS, "the audio state line needs styling"

    # CALL SITES, not just the helper: a helper tested in isolation while the shipped code bypassed
    # it entirely has already happened in this UI once.
    sync = JS.split("function syncAudioNote(", 1)[1].split("\n}", 1)[0]
    assert "ripBitratePlanFromForm(" in sync, "syncAudioNote must go through the form reader"
    form = JS.split("function ripBitratePlanFromForm(", 1)[1].split("\n}", 1)[0]
    assert "ripBitrateNote(" in form, "the form reader must go through the pure planner"
    summary = JS.split("function updateComposeSummary(", 1)[1].split("\nfunction ", 1)[0]
    assert "syncAudioNote(" in summary, "the receipt pass must repaint the audio note"
    # ...and the two shipped lines this batch must NOT disturb (Batch A/B)
    assert "extrasSkippedNote(mode)" in summary, "the receipt line lost extrasSkippedNote"
    assert "h264CapWarning(q, codecVal)" in summary, "the #compose_warn cap line was disturbed"


def test_every_input_the_audio_note_reads_also_repaints_it():
    """Same invariant test_every_input_the_archive_note_reads_also_repaints_it pins for the archive
    ids: an input the note READS but that never triggers a repaint leaves the note asserting a stale
    truth -- e.g. still promising 320 kbps after File format was switched to flac, which ignores it."""
    form = JS.split("function ripBitratePlanFromForm(", 1)[1].split("\n}", 1)[0]
    read_ids = set(re.findall(r"\$\('([a-z_]+)'\)", form))
    assert read_ids == {"quality", "audio_format", "audio_quality"}, read_ids

    reg = JS.split("].forEach((id) => {", 1)[0]
    reg = reg[reg.rindex("['quality'"):]
    registered = set(re.findall(r"'([a-z_]+)'", reg))
    missing = read_ids - registered
    assert not missing, "the audio note reads these but is never repainted: " + str(sorted(missing))
    # the C1 archive ids and the clip fields must survive this edit to the same array
    assert {"download_archive_on", "download_archive", "output_dir",
            "download_engine"} <= registered, registered
    assert {"section_start", "section_end"} <= registered, registered
    # ordering: both notes are repainted after syncClipRange, archive first (it reads #section)
    summary = JS.split("function updateComposeSummary(", 1)[1].split("\nfunction ", 1)[0]
    assert summary.index("syncClipRange(") < summary.index("syncArchiveNote(") \
        < summary.index("syncAudioNote("), "repaint order changed"


def test_rip_bitrate_note_logic_executes_correctly():
    """The note's whole job is branching truth -- WHICH combinations the engine ignores outright.
    A source assertion cannot verify that, so EXECUTE it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function ripBitrateNote("
          + JS.split("function ripBitrateNote(", 1)[1].split("\n}", 1)[0] + "\n}")
    harness = fn + """
var P = ripBitrateNote;
console.log(JSON.stringify({
  video_blank: P('1080p','',''),      video_kbps: P('1080p','','320'),
  video_kbps2: P('4k','mp3','192'),   best_kbps:  P('audio','best','320'),
  best_blank:  P('audio','best',''),  flac_kbps:  P('audio','flac','320'),
  wav_kbps:    P('audio','wav','256'),flac_blank: P('audio','flac',''),
  mp3_kbps:    P('audio','mp3','320'),blank_kbps: P('audio','','320'),
  blank_blank: P('audio','',''),      opus_kbps:  P('audio','opus','320'),
  m4a_kbps:    P('audio','m4a','256'),mp3_blank:  P('audio','mp3',''),
  ws:          P('audio','mp3','   '),upper:      P('audio','FLAC','320')
}));
"""
    # encoding='utf-8' explicitly: text=True alone decodes with the Windows ANSI code page and dies
    # on the curly quotes and em dashes this helper is full of.
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-400:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # IGNORED, and it says which control decides instead. audio_format/audio_quality are read only
    # under `if audio_only:` (yt_final.py:3262) where audio_only == (quality == 'audio') (4532/4581).
    for k in ("video_kbps", "video_kbps2", "best_kbps", "flac_kbps", "wav_kbps", "upper"):
        assert g[k]["level"] == "warn" and "ignored" in g[k]["note"], (k, g[k])
    assert "Max resolution" in g["video_kbps"]["note"], g["video_kbps"]
    # 'best' installs NO FFmpegExtractAudio postprocessor at all (yt_final.py:3265)
    assert "best" in g["best_kbps"]["note"], g["best_kbps"]
    # flac/wav are lossless, so the -b:a yt-dlp derives has no effect (a claim about the CODEC)
    assert "lossless" in g["flac_kbps"]["note"] and "flac" in g["flac_kbps"]["note"], g["flac_kbps"]
    assert "lossless" in g["wav_kbps"]["note"] and "wav" in g["wav_kbps"]["note"], g["wav_kbps"]

    # nothing chosen == nothing claimed
    assert g["video_blank"]["level"] == "idle", g["video_blank"]
    assert g["best_blank"]["level"] == "idle", g["best_blank"]
    # ...and a lossless rip with no bitrate is not scolded
    assert g["flac_blank"]["level"] == "ok", g["flac_blank"]
    assert "ignored" not in g["flac_blank"]["note"], g["flac_blank"]

    # HONEST where it works
    assert g["mp3_kbps"] == {"level": "ok", "note": "Ripping mp3 at 320 kbps"}, g["mp3_kbps"]
    # a BLANK File format means mp3, it does not mean "no conversion" (yt_final.py:3264)
    assert "blank File format means mp3" in g["blank_kbps"]["note"], g["blank_kbps"]
    # the unset bitrate default is 192 (yt_final.py:3270)
    assert "192" in g["blank_blank"]["note"], g["blank_blank"]
    assert "192" in g["mp3_blank"]["note"], g["mp3_blank"]
    assert "192" in g["ws"]["note"], "whitespace is not a bitrate"
    # yt-dlp copies instead of re-encoding when the source already IS the target codec, so the
    # bitrate is skipped -- the browser cannot know the source codec, so state the CONDITION
    for k in ("opus_kbps", "m4a_kbps"):
        assert g[k]["level"] == "ok" and "kept as-is" in g[k]["note"], (k, g[k])


def test_rip_bitrate_control_offers_only_values_the_engine_can_use():
    """yt-dlp's FFmpegExtractAudioPP._quality_args: `elif self._preferredquality > 10: return
    ['-b:a', ...]`; anything <= 10 takes the VBR branch, which `return []`s -- silently NOTHING --
    for libopus/flac/wav/alac. A <select> of kbps values makes that trap unreachable; a free-text
    box would ship it. Also pins the engine literals the note quotes verbatim."""
    m = re.search(r'<select id="audio_quality".*?</select>', HTML, re.S)
    assert m, "the rip-bitrate control must be a <select>, not a free-text box"
    vals = re.findall(r'<option value="([^"]*)"', m.group(0))
    assert "" in vals, "there must be a blank 'engine default' option"
    nums = [int(v) for v in vals if v]
    assert nums, vals
    assert all(n > 10 for n in nums), ("a value <= 10 is a VBR quality yt-dlp silently drops for "
                                       "opus/flac/wav: " + str(nums))
    src = (HERE / "yt_final.py").read_text(encoding="utf-8")
    assert "afmt = (getattr(spec, 'audio_format', None) or 'mp3')" in src, \
        "blank File format no longer means mp3 -- the note must change"
    assert "str(getattr(spec, 'audio_quality', None) or '192')" in src, \
        "the unset rip bitrate is no longer 192 -- the note must change"
    assert "if afmt and afmt != 'best':" in src, \
        "'best' now installs an extractor -- the 'ignored' note must change"
    assert "audio_only = (quality == 'audio')" in src


def test_ytdlp_still_treats_the_offered_bitrates_as_kbps_not_vbr():
    """The whole reason the control is a fixed <select> is a yt-dlp behaviour, and this app UPGRADES
    yt-dlp itself — so pin it against the INSTALLED library rather than trusting a comment. Every
    offered value must take the '-b:a Nk' branch, and a VBR-range value must be shown to produce
    NOTHING for a codec outside yt-dlp's limits table (libopus), which is the trap being avoided."""
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP

    pp = FFmpegExtractAudioPP.__new__(FFmpegExtractAudioPP)
    m = re.search(r'<select id="audio_quality".*?</select>', HTML, re.S)
    offered = [int(v) for v in re.findall(r'<option value="([^"]*)"', m.group(0)) if v]
    assert offered, "no bitrate options to check"

    for kbps in offered + [192]:                     # 192 = the engine's default for a blank box
        pp._preferredquality = kbps
        for codec in ("libmp3lame", "aac", "libopus", "flac", "alac"):
            assert pp._quality_args(codec) == ["-b:a", f"{kbps}k"], \
                f"yt-dlp no longer maps {kbps} to a bitrate for {codec} — revisit the control"

    # ...and the trap itself: a VBR-range value silently emits nothing for a codec yt-dlp has no
    # limits entry for, which is exactly what a free-text box would let a user ship.
    pp._preferredquality = 5
    assert pp._quality_args("libopus") == [], "the VBR trap is gone; the <select> may be relaxed"
    assert pp._quality_args("libmp3lame") != [], "sanity: mp3 IS in yt-dlp's limits table"
    pp._preferredquality = None
    assert pp._quality_args("libmp3lame") == []


def test_verify_strict_note_states_every_rule_and_the_engine_backs_them():
    """The checkbox note makes claims a user must be able to trust before enabling it: it FAILS the
    job, and it fails CLOSED without ffprobe. EXECUTE _verify_output for every rule the note names."""
    from types import SimpleNamespace

    from yt_final import FinalYouTubeGPUEngine

    def _stub(probe):
        return SimpleNamespace(processor=SimpleNamespace(probe_integrity=lambda p: probe),
                               downloader=SimpleNamespace(last_info={}))

    V = FinalYouTubeGPUEngine._verify_output
    f = Path("out.mp4")
    strict = SimpleNamespace(verify_strict=True, quality="1080p", section=None)
    loose = SimpleNamespace(verify_strict=False, quality="1080p", section=None)

    # fails CLOSED when ffprobe is unavailable/failed (yt_final.py:6279-6280)...
    ok, why = V(_stub({}), f, f, strict, info={})
    assert ok is False and "ffprobe" in why, why
    # ...and only when strict; non-strict still degrades gracefully to a warning
    assert V(_stub({}), f, f, loose, info={})[0] is True

    good = {"size": 5_000_000, "has_video": True, "has_audio": True, "duration": 100.0}
    assert V(_stub(good), f, f, strict, info={"duration": 100.0})[0] is True

    # an audio stream is required UNCONDITIONALLY (yt_final.py:6292-6293)
    ok, why = V(_stub(dict(good, has_audio=False)), f, f, strict, info={"duration": 100.0})
    assert ok is False and "audio" in why, why
    # a video stream too, unless quality == 'audio' (yt_final.py:6288-6290)
    ok, why = V(_stub(dict(good, has_video=False)), f, f, strict, info={"duration": 100.0})
    assert ok is False and "video" in why, why
    aud = SimpleNamespace(verify_strict=True, quality="audio", section=None)
    assert V(_stub(dict(good, has_video=False)), f, f, aud, info={"duration": 100.0})[0] is True

    # 1% tolerance with a 2s floor (yt_final.py:6299)
    ok, why = V(_stub(dict(good, duration=90.0)), f, f, strict, info={"duration": 100.0})
    assert ok is False and "duration" in why, why
    assert V(_stub(dict(good, duration=101.5)), f, f, strict, info={"duration": 100.0})[0] is True
    # a clip is exempt: its output is shorter than info['duration'] by design (yt_final.py:6297)
    clip = SimpleNamespace(verify_strict=True, quality="1080p", section="0:00-1:00")
    assert V(_stub(dict(good, duration=60.0)), f, f, clip, info={"duration": 100.0})[0] is True
    # the 1 KiB size floor (yt_final.py:6284)
    assert V(_stub(dict(good, size=10)), f, f, strict, info={"duration": 100.0})[0] is False

    # ...and the shipped note tells the user every one of those rules
    note = HTML.split('id="verify_strict"', 1)[1].split("</details>", 1)[0]
    for claim in ("ffprobe", "audio stream", "video stream", "1 KiB", "1%", "clips"):
        assert claim in note, "the verify_strict note must state: " + claim
    assert "fails" in note, note[:200]
    assert "<code>audio</code>" in note, "the note must name the Max resolution value that exempts video"


def test_verify_strict_and_audio_quality_survive_the_web_payload():
    """The premise the whole client design rests on: NO server change was needed. Both fields pass
    through _web_jobspec_from_payload untouched, and an unset control arrives as None/False."""
    import webserver

    spec = webserver._web_jobspec_from_payload(
        {"urls": ["https://youtu.be/x"], "verify_strict": True, "audio_quality": "320"})
    assert spec.verify_strict is True
    assert spec.audio_quality == "320", spec.audio_quality

    blank = webserver._web_jobspec_from_payload({"urls": ["https://youtu.be/x"]})
    assert blank.verify_strict is False
    assert blank.audio_quality is None, blank.audio_quality


def test_audio_map_and_bitrate_and_format_id_stay_unexposed_on_purpose():
    """These three JobSpec fields are engine/CLI-only BY DECISION, not by oversight. If a real user
    need appears, delete the relevant assertion in the SAME commit as the control.

    audio_bitrate: looks interchangeable with audio_quality but is not -- audio_quality shapes an
      audio-only RIP (yt_final.py:3262-3271), audio_bitrate overrides a VIDEO profile's audio
      (yt_final.py:6039-6040). Two controls that look the same and are not is worse than one. It is
      also DROPPED for lossless codecs (yt_final.py:1882-1884), i.e. on every ProRes/DNxHR profile an
      editor actually picks, since those pin pcm_s16le.
    audio_map: 'stereo' is a verified no-op and 'dual-mono' is the SAME engine branch as 'split', so
      the JobSpec's three values are one dead option and two duplicates. Worse, the channelsplit args
      include '-map 0:v' (yt_final.py:6613), so on an audio-only rip it is a GUARANTEED ffmpeg
      failure -- exposing it means also shipping a Queue-blocking gate for a state only the new
      control can reach.
    format_id: overrides the quality selector outright (yt_final.py:3290-3291) AND an id YouTube will
      not serve does not fail -- the retry planner swaps in 'bestvideo*+bestaudio/best'
      (yt_final.py:5133-5135), so a typo quietly downloads the biggest stream. The probe pop-out
      already applies heights and codecs one-click; a raw itag box is a CLI-grade escape hatch."""
    ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', HTML))
    for cid in ("audio_bitrate", "audio_map", "audio_split_mics", "format_id"):
        assert cid not in ids, "exposing #" + cid + "? delete this assertion and state the need"
    spec = JS.split("function buildSpec(", 1)[1].split("\n}", 1)[0]
    for key in ("audio_bitrate:", "audio_map:", "format_id:"):
        assert key not in spec, key + " reached buildSpec without a decision"

    src = (HERE / "yt_final.py").read_text(encoding="utf-8")
    # re-checked every run: the reasons above must still be true of the engine
    assert "lossless = str(ac).startswith('pcm') or ac in ('flac', 'alac')" in src
    assert "if p.get('audio_bitrate') and not lossless:" in src
    for pid in ("prores_hq", "dnxhr_hqx"):
        i = src.index('"' + pid + '": {')
        assert "pcm_s" in src[i:i + 400], pid + " no longer pins PCM -- re-check the skip"
    assert "vmap = ['-map', '0:v']" in src, "channelsplit no longer needs a video stream"
    assert "if spec.audio_map not in ('dual-mono', 'split'):" in src, \
        "'stereo' may no longer be a no-op -- re-check the audio_map skip"
    assert "new_opts['format'] = 'bestvideo*+bestaudio/best'" in src, \
        "a bad format id may no longer fall back silently -- re-check the format_id skip"

