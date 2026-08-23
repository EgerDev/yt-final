#!/usr/bin/env python3
"""
Empirical runtime verification for yt_final.py
Tests all requested scenarios by actually running ffmpeg.
"""

import sys
import os
import subprocess
import json
import tempfile
import shutil
from pathlib import Path

# Add the Final directory (this script's own location) to path so we can import the modules.
# Resolve it relative to __file__ rather than hardcoding a developer-specific absolute path, which
# crashed os.chdir() with FileNotFoundError on any other checkout/machine.
FINAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(FINAL_DIR))
os.chdir(str(FINAL_DIR))

# Import the modules
from capability import CapabilityDetector
from yt_final import FFmpegProcessor, FinalYouTubeGPUEngine

SAMPLE = FINAL_DIR / "downloads" / "Bruno Mars - Grenade (Lyrics) [4YrzJ9RZ9qY].mp4"
WORK_DIR = FINAL_DIR / "test_outputs"
WORK_DIR.mkdir(exist_ok=True)

detector = CapabilityDetector()
proc = FFmpegProcessor(detector)
ffmpeg = proc.ffmpeg
ffprobe = proc.ffprobe

print(f"ffmpeg: {ffmpeg}")
print(f"ffprobe: {ffprobe}")
print(f"sample: {SAMPLE}")
print(f"sample exists: {SAMPLE.exists()}")
print()

results = {}


def run_cmd(cmd, label, capture=True):
    """Run a command and return (returncode, stdout, stderr)."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"CMD: {' '.join(str(c) for c in cmd)}")
    print(f"{'='*60}")
    try:
        r = subprocess.run(
            [str(c) for c in cmd],
            capture_output=capture,
            text=True,
            timeout=120
        )
        print(f"returncode: {r.returncode}")
        if r.stderr:
            # Print last 30 lines of stderr
            lines = r.stderr.strip().splitlines()
            tail = lines[-30:] if len(lines) > 30 else lines
            print("--- stderr tail ---")
            print('\n'.join(tail))
        return r.returncode, r.stdout or '', r.stderr or ''
    except subprocess.TimeoutExpired:
        print("TIMEOUT!")
        return -999, '', 'TIMEOUT'
    except Exception as e:
        print(f"EXCEPTION: {e}")
        return -1, '', str(e)


def ffprobe_tags(path):
    """Probe a file for title and chapter count."""
    cmd = [ffprobe, '-v', 'quiet', '-print_format', 'json',
           '-show_format', '-show_chapters', str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    try:
        data = json.loads(r.stdout)
        title = data.get('format', {}).get('tags', {}).get('title', '(none)')
        chapters = data.get('chapters', [])
        return title, chapters
    except Exception as e:
        return f'(parse error: {e})', []


# ============================================================
# TEST 1: encode_existing / _do_encode copy with ffmetadata
# ============================================================
print("\n\n### TEST 1: copy with ffmetadata (metadata+chapters) ###")
ffmeta_path = WORK_DIR / "test.ffmeta"
tags = {'title': 'Test Title For Verify', 'artist': 'Test Artist'}
chapters = [
    {'start_time': 0.0, 'end_time': 5.0, 'title': 'Intro'},
    {'start_time': 5.0, 'end_time': 10.0, 'title': 'Main Part'},
]
written = proc.write_ffmetadata(ffmeta_path, tags=tags, chapters=chapters)
print(f"ffmetadata written to: {written}")
if written:
    print("ffmetadata contents:")
    print(Path(written).read_text(encoding='utf-8'))

t1_out = WORK_DIR / "test1_copy_meta.mp4"
# Use the real method via _do_encode on a short clip first (-t 5 via direct ffmpeg)
# We need a short clip for speed:
clip_5s = WORK_DIR / "clip_5s.mp4"
if not clip_5s.exists():
    rc, _, _ = run_cmd(
        [ffmpeg, '-hide_banner', '-y', '-i', str(SAMPLE), '-t', '5', '-c', 'copy', str(clip_5s)],
        "create 5s test clip"
    )
    print(f"clip creation rc={rc}")

# Now test copy with ffmetadata file
if clip_5s.exists():
    cmd_copy = proc.build_ffmpeg_command(
        clip_5s, t1_out, 'copy_fast',
        ffmetadata_file=ffmeta_path
    )
    rc1, _, stderr1 = run_cmd(cmd_copy, "TEST1: copy with ffmetadata")
    results['test1_copy_ffmeta'] = {'rc': rc1, 'cmd': ' '.join(str(c) for c in cmd_copy)}
    if rc1 == 0 and t1_out.exists():
        title, chaps = ffprobe_tags(t1_out)
        print(f"\nffprobe back: title='{title}', chapters={len(chaps)}")
        results['test1_copy_ffmeta']['title'] = title
        results['test1_copy_ffmeta']['chapter_count'] = len(chaps)
        for ch in chaps:
            print(f"  Chapter: {ch.get('tags', {}).get('title')} @ {ch.get('start_time')}-{ch.get('end_time')}")
    else:
        results['test1_copy_ffmeta']['error'] = stderr1[-500:]
else:
    results['test1_copy_ffmeta'] = {'rc': -1, 'error': 'clip_5s creation failed'}


# ============================================================
# TEST 2: prores_4444 (source has no alpha)
# ============================================================
print("\n\n### TEST 2: prores_4444 (no alpha source) ###")
t2_out = WORK_DIR / "test2_prores4444.mov"
cmd_p4444 = proc.build_ffmpeg_command(
    clip_5s if clip_5s.exists() else SAMPLE,
    t2_out, 'prores_4444'
)
rc2, _, stderr2 = run_cmd(cmd_p4444, "TEST2: prores_4444")
results['test2_prores_4444'] = {
    'rc': rc2,
    'cmd': ' '.join(str(c) for c in cmd_p4444),
    'success': rc2 == 0 and t2_out.exists()
}
if rc2 != 0:
    results['test2_prores_4444']['error'] = (stderr2.splitlines()[-10:] if stderr2 else [])
    print(f"prores_4444 FAILED (rc={rc2})")
    print("Note: yuva444p10le requires alpha plane; may fail on non-alpha source.")
else:
    print(f"prores_4444 succeeded, output size={t2_out.stat().st_size}")


# ============================================================
# TEST 3: dnxhd_hq_1080p25
# ============================================================
print("\n\n### TEST 3: dnxhd_hq_1080p25 ###")
t3_out = WORK_DIR / "test3_dnxhd_1080p25.mov"
cmd_dnxhd = proc.build_ffmpeg_command(
    clip_5s if clip_5s.exists() else SAMPLE,
    t3_out, 'dnxhd_hq_1080p25'
)
rc3, _, stderr3 = run_cmd(cmd_dnxhd, "TEST3: dnxhd_hq_1080p25")
results['test3_dnxhd_hq_1080p25'] = {
    'rc': rc3,
    'cmd': ' '.join(str(c) for c in cmd_dnxhd),
    'success': rc3 == 0 and t3_out.exists()
}
if rc3 != 0:
    results['test3_dnxhd_hq_1080p25']['error'] = (stderr3.splitlines()[-15:] if stderr3 else [])
    print(f"dnxhd_hq_1080p25 FAILED (rc={rc3})")
else:
    print(f"dnxhd_hq_1080p25 succeeded, output size={t3_out.stat().st_size}")


# ============================================================
# TEST 4: cpu_av1_svt_filmgrain (-t 1)
# ============================================================
print("\n\n### TEST 4: cpu_av1_svt_filmgrain (1s clip) ###")
clip_1s = WORK_DIR / "clip_1s.mp4"
if not clip_1s.exists():
    run_cmd([ffmpeg, '-hide_banner', '-y', '-i', str(SAMPLE), '-t', '1', '-c', 'copy', str(clip_1s)],
            "create 1s clip")

t4_out = WORK_DIR / "test4_svt_filmgrain.mkv"
cmd_svt = proc.build_ffmpeg_command(
    clip_1s if clip_1s.exists() else SAMPLE,
    t4_out, 'cpu_av1_svt_filmgrain'
)
rc4, _, stderr4 = run_cmd(cmd_svt, "TEST4: cpu_av1_svt_filmgrain")
results['test4_cpu_av1_svt_filmgrain'] = {
    'rc': rc4,
    'cmd': ' '.join(str(c) for c in cmd_svt),
    'success': rc4 == 0 and t4_out.exists()
}
if rc4 != 0:
    err_lines = stderr4.splitlines()[-15:] if stderr4 else []
    results['test4_cpu_av1_svt_filmgrain']['error'] = err_lines
    print(f"cpu_av1_svt_filmgrain FAILED (rc={rc4})")
    print("Error lines:", '\n'.join(err_lines))
else:
    print(f"cpu_av1_svt_filmgrain succeeded, output size={t4_out.stat().st_size}")


# ============================================================
# TEST 5: nvenc_av1_vbr (-t 1)
# ============================================================
print("\n\n### TEST 5: nvenc_av1_vbr (1s clip) ###")
t5_out = WORK_DIR / "test5_nvenc_av1_vbr.mp4"

# Check if av1_nvenc is available
av1_nvenc_available = detector.encoder_usable('av1_nvenc')
print(f"av1_nvenc available: {av1_nvenc_available}")

cmd_nv_av1 = proc.build_ffmpeg_command(
    clip_1s if clip_1s.exists() else SAMPLE,
    t5_out, 'nvenc_av1_vbr'
)
rc5, _, stderr5 = run_cmd(cmd_nv_av1, "TEST5: nvenc_av1_vbr")
results['test5_nvenc_av1_vbr'] = {
    'rc': rc5,
    'cmd': ' '.join(str(c) for c in cmd_nv_av1),
    'success': rc5 == 0 and t5_out.exists(),
    'av1_nvenc_detected': av1_nvenc_available
}
if rc5 != 0:
    err_lines = stderr5.splitlines()[-15:] if stderr5 else []
    results['test5_nvenc_av1_vbr']['error'] = err_lines
    print(f"nvenc_av1_vbr FAILED (rc={rc5})")
else:
    print(f"nvenc_av1_vbr succeeded, output size={t5_out.stat().st_size}")


# ============================================================
# TEST 6: normalize_audio (3s clip)
# ============================================================
print("\n\n### TEST 6: normalize_audio (3s clip) ###")
clip_3s = WORK_DIR / "clip_3s.mp4"
if not clip_3s.exists():
    run_cmd([ffmpeg, '-hide_banner', '-y', '-i', str(SAMPLE), '-t', '3', '-c', 'copy', str(clip_3s)],
            "create 3s clip")

t6_out = WORK_DIR / "test6_normalized.mp4"
try:
    ok6 = proc.normalize_audio(
        clip_3s if clip_3s.exists() else SAMPLE,
        t6_out, target_lufs=-14.0
    )
    rc6 = 0 if ok6 else 1
    results['test6_normalize_audio'] = {
        'rc': rc6,
        'success': ok6 and t6_out.exists()
    }
    if ok6:
        print(f"normalize_audio succeeded, output size={t6_out.stat().st_size}")
    else:
        print("normalize_audio returned False")
except Exception as e:
    results['test6_normalize_audio'] = {'rc': -1, 'error': str(e)}
    print(f"normalize_audio EXCEPTION: {e}")


# ============================================================
# TEST 7: mux_sidecars with .en.srt into mp4 (mov_text)
# ============================================================
print("\n\n### TEST 7: mux_sidecars with .en.srt into mp4 (mov_text) ###")
srt_path = WORK_DIR / "test_sidecar.en.srt"
srt_path.write_text(
    "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n"
    "2\n00:00:02,000 --> 00:00:04,000\nThis is a test subtitle\n",
    encoding='utf-8'
)

t7_in = clip_5s if clip_5s.exists() else SAMPLE
t7_out = WORK_DIR / "test7_with_subs.mp4"
try:
    ok7 = proc.mux_sidecars(t7_in, t7_out, sub_files=[srt_path], container='mp4')
    rc7 = 0 if ok7 else 1
    results['test7_mux_sidecars'] = {
        'rc': rc7,
        'success': ok7 and t7_out.exists()
    }
    if ok7:
        print(f"mux_sidecars succeeded, output size={t7_out.stat().st_size}")
        # Probe for subtitle streams
        probe_cmd = [ffprobe, '-v', 'quiet', '-print_format', 'json', '-show_streams', str(t7_out)]
        r = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        streams = json.loads(r.stdout).get('streams', [])
        sub_streams = [s for s in streams if s.get('codec_type') == 'subtitle']
        print(f"Subtitle streams in output: {len(sub_streams)}")
        for s in sub_streams:
            print(f"  codec={s.get('codec_name')}, lang={s.get('tags', {}).get('language', '?')}")
        results['test7_mux_sidecars']['sub_stream_count'] = len(sub_streams)
    else:
        print("mux_sidecars returned False")
except Exception as e:
    results['test7_mux_sidecars'] = {'rc': -1, 'error': str(e)}
    print(f"mux_sidecars EXCEPTION: {e}")


# ============================================================
# SUMMARY
# ============================================================
print("\n\n" + "="*60)
print("SUMMARY")
print("="*60)
for test, r in results.items():
    success = r.get('success', r.get('rc') == 0)
    status = "PASS" if success else "FAIL"
    print(f"  {status}  {test}  (rc={r.get('rc', '?')})")
    if not success and r.get('error'):
        err = r['error']
        if isinstance(err, list):
            for line in err[-5:]:
                print(f"       {line}")
        else:
            print(f"       {str(err)[:300]}")

print("\nDone. Output files in:", WORK_DIR)
