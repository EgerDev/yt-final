"""Instructions about the HOST machine must not be derived from the VIEWER's browser.

The Auth panel tells the user to fully quit the browser whose cookies are being imported. That browser
runs on the HOST. The wording was derived from `navigator.platform` — the machine looking at the page —
and this console is routinely opened from another device on the LAN, a configuration the codebase
explicitly supports and reasons about elsewhere (see hostDeviceLabel and applyHostOs).

So a Windows host viewed from a Mac or iPad rendered, in the same card: "Browsers on this PC: chrome,
edge" (correct, server-derived) and directly beneath it "Fully quit the browser — Cmd+Q" (wrong; there
is no Cmd+Q on the host). Worse, the wording it displaced is the one that matters on Windows — close
every window AND any tray/background instance — which is precisely what breaks a cookie import there.
The mis-targeted hint steered the user into the exact failure the Windows wording exists to prevent.

Also here: two JS-supplied smooth scrolls. `behavior: 'smooth'` from JS overrides CSS entirely, so the
stylesheet's prefers-reduced-motion reset did not apply to them.
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


def _html_without_comments() -> str:
    return re.sub(r"<!--.*?-->", "", HTML, flags=re.S)


def test_the_viewers_user_agent_no_longer_decides_host_instructions():
    assert "IS_MAC_UI" not in JS, "the UA-derived platform flag is back"
    assert "QUIT_HINT" not in JS, "the UA-derived quit constants are back"
    # navigator may still be used for genuinely client-side things (clipboard), but not for deciding
    # what the HOST's browser needs.
    for fn in ("quitHint", "quitHintQualified"):
        body = JS.split("function %s(" % fn, 1)[1].split("\n}", 1)[0]
        assert "navigator" not in body, "%s reads the viewer's user agent" % fn
        assert "HOST_OS" not in body, \
            "%s should take the os as an argument so it stays pure and testable" % fn
    # every consumer must pass the server-reported host OS
    assert JS.count("quitHint(HOST_OS)") >= 3, "not all quit-hint consumers use the host OS"
    assert "quitHintQualified(HOST_OS)" in JS


def test_the_markup_ships_neutral_and_is_sharpened_after_health():
    """HOST_OS only exists once /api/health answers, so the static labels must be neutral rather than
    guessing — never WRONG, just vaguer, the same discipline hostDeviceLabel() uses."""
    visible = _html_without_comments()
    assert "Cmd+Q" not in visible, \
        "the markup hardcodes Cmd+Q again — wrong on a Windows host (comments are exempt)"
    assert visible.count('class="quit-hint"') >= 2, "the stampable labels are gone"
    # ...and the stamping must live inside applyHostOs, not at module level where HOST_OS is unknown
    apply_fn = JS.split("function applyHostOs(", 1)[1].split("\nfunction ", 1)[0]
    assert ".quit-hint" in apply_fn, "the quit-hint labels are no longer stamped from the host OS"
    tail = JS.split("tryExistingSession();", 1)[-1]
    assert "querySelectorAll('.quit-hint')" not in tail, \
        "stamping at module level bakes in the neutral fallback and never corrects it"


def test_quit_hint_logic_executes_correctly():
    """EXECUTE both helpers. The whole defect was a wrong branch, and a source assertion cannot tell
    'branches on the host' from 'branches on the viewer'."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fns = ""
    for fn in ("quitHint", "quitHintQualified"):
        fns += "function %s(" % fn + JS.split("function %s(" % fn, 1)[1].split("\n}", 1)[0] + "\n}\n"
    harness = fns + r"""
console.log(JSON.stringify({
  mac:      quitHint('macOS'),
  darwin:   quitHint('Darwin'),
  windows:  quitHint('Windows'),
  win11:    quitHint('Windows 11 Pro'),
  linux:    quitHint('Linux'),
  unknown:  quitHint(''),
  nullish:  quitHint(null),
  q_mac:    quitHintQualified('macOS'),
  q_win:    quitHintQualified('Windows'),
  q_null:   quitHintQualified(null)
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # a Mac host gets the Mac shortcut
    assert g["mac"] == "Cmd+Q" and g["darwin"] == "Cmd+Q", g
    # a Windows host gets the wording that actually matters there — the tray/background instance is
    # what holds the cookie DB open and breaks the import
    for k in ("windows", "win11", "linux"):
        assert "tray" in g[k] and "Cmd+Q" not in g[k], (k, g[k])
    # unknown host: neutral, never a guess, and never empty
    for k in ("unknown", "nullish"):
        assert g[k] and "Cmd+Q" not in g[k] and "tray" not in g[k], (k, g[k])
        assert "quit" in g[k].lower(), g[k]
    # the qualified form names the platform only when it knows it
    assert g["q_mac"] == "macOS: Cmd+Q", g["q_mac"]
    assert "Cmd+Q" not in g["q_win"] and "Cmd+Q" not in g["q_null"], g


def test_programmatic_scrolls_honour_reduced_motion():
    """A JS `behavior: 'smooth'` overrides CSS, so the stylesheet's prefers-reduced-motion reset did
    not reach these two scrolls."""
    assert "function reduceMotion(" in JS
    # Comment lines must be excluded, and not only `//` ones: the JSDoc above reduceMotion quotes
    # `behavior: 'smooth'` while explaining the bug, and a `//`-only filter matched that and failed
    # against correct code. This is the same comment-vs-code trap that produced several toothless or
    # falsely-failing assertions across this batch.
    smooth = [ln.strip() for ln in JS.splitlines()
              if "behavior:" in ln and "smooth" in ln
              and not ln.strip().startswith(("//", "*", "/*"))]
    assert smooth, "no programmatic scrolls found — did they change shape?"
    for ln in smooth:
        assert "reduceMotion()" in ln, "this scroll ignores the motion preference: " + ln


def test_reduce_motion_logic_executes_correctly():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = "function reduceMotion(" + JS.split("function reduceMotion(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = fn + r"""
var out = {};
global.window = { matchMedia: () => ({ matches: true }) };
global.matchMedia = global.window.matchMedia;
out.reduce = reduceMotion();
global.window = { matchMedia: () => ({ matches: false }) };
global.matchMedia = global.window.matchMedia;
out.allow = reduceMotion();
// some embedded webviews have no matchMedia at all, and a throw here during boot would kill the page
global.window = {};
global.matchMedia = undefined;
out.absent = reduceMotion();
global.window = { matchMedia: () => { throw new Error('nope'); } };
global.matchMedia = global.window.matchMedia;
out.throws = reduceMotion();
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert g["reduce"] is True and g["allow"] is False, g
    # absence or a throw must degrade to "animate", never to an exception
    assert g["absent"] is False and g["throws"] is False, g
