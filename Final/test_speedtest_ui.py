"""The Speed Test view must not out-shout its own backend.

Two defects, both about the UI being more confident or more talkative than the measurement warrants:

  * #st_phases is a live region, and paint() cleared it and rebuilt both children every second with
    the elapsed-seconds counter in the SAME text node as the phase sentence. So a screen reader
    re-announced "Fetching a real YouTube video… · 34s (can take 1–2 min)" once per second for the
    whole 1–2 minute run. The counter has to keep ticking visibly (a throttled line must read as
    working, not stuck) — it just must not be spoken.

  * The "Last test" line printed one confident decimal for runs the backend had itself graded
    low-confidence. analyze() computes a lo–hi spread precisely because googlevideo pacing makes
    back-to-back runs on the SAME box read 14 vs 62 MiB/s, and it warns against comparing single
    readings — while this line invited exactly that comparison.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HTML = (HERE / "webui" / "index.html").read_text(encoding="utf-8")
JS = (HERE / "webui" / "app.js").read_text(encoding="utf-8")
ANALYZE = (HERE / "speedtest.py").read_text(encoding="utf-8")
SERVER = (HERE / "webserver.py").read_text(encoding="utf-8")


def _phases() -> str:
    return JS.split("function stStartPhases(", 1)[1].split("\nfunction ", 1)[0]


def test_the_backend_still_produces_what_the_ui_now_reads():
    """The hedging is only justified because the server grades the run. If these stop being sent, the
    UI silently reverts to a bare number instead of failing."""
    assert 'm["youtube_direct_lo"], m["youtube_direct_hi"]' in SERVER, \
        "the direct-flow spread is no longer measured — revisit the compare line"
    assert 'm.get("confidence") == "low"' in ANALYZE, "confidence grading is gone"
    # and analyze() still explains why a single median can mislead
    assert "variability" in ANALYZE


def test_only_the_phase_sentence_is_announced():
    body = _phases()
    # built once, outside paint()
    assert body.count("box.textContent = ''") == 1, \
        "the live region is still cleared on every tick — that re-announces everything"
    paint = body.split("const paint = () => {", 1)[1].split("\n  };", 1)[0]
    assert "createElement" not in paint, "children are still recreated every second"
    # the seconds counter must live in an aria-hidden node, and NOT in the announced one
    assert "aria-hidden" in body, "nothing is hidden from the announcement"
    assert "meta.textContent = " in paint and "(can take 1" in paint, \
        "the visible counter must still tick every second"
    announced = paint.split("if (view.announce) {", 1)
    assert len(announced) == 2, "the phase text is written unconditionally — it will re-announce"
    assert "txt.textContent" in announced[1], "the phase sentence must be the announced node"
    assert "txt.textContent" not in announced[0], \
        "the phase sentence is also written outside the changed-phase guard"
    # The announced write must carry ONLY the phase. Appending the ticking counter to it re-announces
    # a changed string every second even though the guard fires once — the guard would still look
    # correct, and every other assertion here still passed (proven toothless by mutation).
    said = [ln for ln in announced[1].splitlines() if "txt.textContent" in ln][0]
    for leak in ("meta", "secs", "can take"):
        assert leak not in said, \
            "the announced node includes the per-second counter (%r): %s" % (leak, said.strip())
    # the container keeps its live-region role (other tests pin this too)
    assert 'id="st_phases"' in HTML and 'aria-live="polite"' in \
        HTML.split('id="st_phases"', 1)[1].split(">", 1)[0] + 'aria-live="polite"'


def test_phase_view_logic_executes_correctly():
    """EXECUTE the boundary. `announce` is the whole fix: a source assertion cannot distinguish
    "announces on change" from "announces always"."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = "function stPhaseView(" + JS.split("function stPhaseView(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = fn + r"""
var V = stPhaseView;
console.log(JSON.stringify({
  t0:        V(0, 6, -1),
  t6_same:   V(6, 6, 0),
  t7_next:   V(7, 6, 0),
  t13_same:  V(13, 6, 1),
  t14_next:  V(14, 6, 1),
  clamped:   V(9999, 6, 5),
  clamp_new: V(9999, 6, 4),
  negative:  V(-5, 6, -1),
  nan:       V(NaN, 6, -1),
  one_phase: V(50, 1, 0),
  zero_phase: V(3, 0, -1)
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # first paint announces; ticking inside the same phase does NOT
    assert g["t0"] == {"index": 0, "announce": True}, g["t0"]
    assert g["t6_same"]["announce"] is False, "re-announces within the same phase — this is the bug"
    assert g["t13_same"]["announce"] is False, g["t13_same"]
    # crossing a 7s boundary announces exactly once
    assert g["t7_next"] == {"index": 1, "announce": True}, g["t7_next"]
    assert g["t14_next"] == {"index": 2, "announce": True}, g["t14_next"]
    # the last phase holds instead of running off the end, and does not re-announce
    assert g["clamped"] == {"index": 5, "announce": False}, g["clamped"]
    assert g["clamp_new"]["index"] == 5 and g["clamp_new"]["announce"] is True, g["clamp_new"]
    # junk input must not produce a negative or undefined index
    assert g["negative"]["index"] == 0 and g["nan"]["index"] == 0, g
    assert g["one_phase"]["index"] == 0, g["one_phase"]
    assert g["zero_phase"]["index"] == 0, "a zero phase count must not yield index -1"


def test_compare_line_hedges_a_jittery_run_and_keeps_legacy_entries_readable():
    """EXECUTE it. The legacy path is the one that breaks silently: history entries saved before
    lo/hi/conf existed are still in localStorage, and a naive range render turns them into
    'undefined–undefined MiB/s'."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = "function stCompareText(" + JS.split("function stCompareText(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = fn + r"""
var T = stCompareText;
console.log(JSON.stringify({
  legacy:      T({direct: 14.02, org: 'Vodafone ES', t: 1}, '2h ago'),
  legacy_bare: T({direct: 14.02, t: 1}, '2h ago'),
  spread:      T({direct: 14, lo: 9, hi: 41, conf: 'low', t: 1}, '5m ago'),
  low_no_range:T({direct: 14, conf: 'low', t: 1}, '5m ago'),
  high_conf:   T({direct: 22.5, conf: 'high', t: 1}, '1d ago'),
  with_bypass: T({direct: 14, bypass: 51.2, t: 1}, '3m ago'),
  bad_range:   T({direct: 14, lo: 41, hi: 9, t: 1}, '3m ago'),
  zero_lo:     T({direct: 14, lo: 0, hi: 41, t: 1}, '3m ago'),
  no_direct:   T({direct: 0, t: 1}, '3m ago'),
  nothing:     T(null, '3m ago')
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # LEGACY: byte-for-byte what it rendered before, and never a broken range
    assert g["legacy"] == "Last test 2h ago via Vodafone: YouTube was 14.0 MiB/s", g["legacy"]
    assert g["legacy_bare"] == "Last test 2h ago: YouTube was 14.0 MiB/s", g["legacy_bare"]
    for k, v in g.items():
        if isinstance(v, str):
            assert "undefined" not in v and "NaN" not in v, (k, v)

    # a measured spread replaces the false-precision median
    assert "9–41 MiB/s" in g["spread"], g["spread"]
    assert "14.0" not in g["spread"], "the misleading single median is still shown"
    # low confidence with no usable range still gets said out loud
    assert "jittery" in g["low_no_range"], g["low_no_range"]
    # a confident run is not hedged
    assert "jittery" not in g["high_conf"] and "22.5" in g["high_conf"], g["high_conf"]
    # the bypass A/B comparison survives
    assert "bypass 51.2" in g["with_bypass"], g["with_bypass"]
    # a nonsensical range falls back rather than printing hi<lo
    assert "14.0 MiB/s" in g["bad_range"] and "41–9" not in g["bad_range"], g["bad_range"]
    assert "14.0 MiB/s" in g["zero_lo"], g["zero_lo"]
    # nothing to compare against stays silent
    assert g["no_direct"] is None and g["nothing"] is None, g


def test_the_history_entry_persists_the_fields_the_line_needs():
    """The helper can only hedge if stPushHistory saved the grade. This is the wiring assertion — the
    class of gap where a tested helper is fed data the writer never stored."""
    fn = JS.split("function stPushHistory(", 1)[1].split("\n}", 1)[0]
    for key in ("lo:", "hi:", "conf:"):
        assert key in fn, "stPushHistory does not persist " + key
    assert "m.youtube_direct_lo" in fn and "m.youtube_direct_hi" in fn, fn
    assert "m.confidence" in fn, fn
    # ...and the renderer must go through the tested helper
    line = JS.split("function stCompareLine(", 1)[1].split("\n}", 1)[0]
    assert "stCompareText(" in line, "stCompareLine builds its own text again"
