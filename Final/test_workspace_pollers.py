"""Poller/state defects in the workspace client that source-shaped hardening passes missed.

Each test here corresponds to a defect CONFIRMED by reading app.js against the server routes:
polling that re-arms itself while the tab is hidden, a compose-summary field whose listener was
never registered, a shared in-flight boolean with no ownership, a banner that disappears instead of
reverting, a probe chip that sets the OPPOSITE of what it says, and a watch poller still using the
abort-by-next-tick pattern the jobs poller was rewritten to avoid.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent / "webui"
HTML = (WEBUI / "index.html").read_text(encoding="utf-8")
JS = (WEBUI / "app.js").read_text(encoding="utf-8")


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    return node


def _fn(name):
    """Extract a top-level `function name(...) { ... }` body by brace matching.

    Brace matching starts after the parameter list's closing paren — a default parameter such as
    `opts = {}` would otherwise be mistaken for the whole body."""
    start = JS.index(f"function {name}(")
    # keep a leading `async` — dropping it turns an async function into a sync one and every
    # `await` in the extracted body becomes a SyntaxError under node.
    if JS[max(0, start - 6):start] == "async ":
        start -= 6
    depth, j = 0, JS.index("(", start)
    while True:                                  # find the paren that closes the parameter list
        if JS[j] == "(":
            depth += 1
        elif JS[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    depth, i, seen = 0, JS.index("{", j), False
    while i < len(JS):
        if JS[i] == "{":
            depth += 1
            seen = True
        elif JS[i] == "}":
            depth -= 1
            if seen and depth == 0:
                return JS[start:i + 1]
        i += 1
    raise AssertionError(f"could not extract {name}")


def _run(harness):
    r = subprocess.run([_node(), "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-400:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _watch_harness():
    """Real refreshGrid + stopPolling + the session machinery, wired to controllable fakes.

    Everything under test is the SHIPPED source (extracted by _fn); only the environment is faked,
    so a re-break in production code fails these tests."""
    return (
        _fn("refreshWatches") + "\n"
        + _fn("stopPolling") + "\n"
        + r"""
var watchesAbort = null, watchesInFlight = false;
var jobsAbort = null, jobsInFlight = false, jobsTimer = null, watchTimer = null;
var stale = false;                       // flips true when the session is lost
var calls = { api: 0, aborted: 0, render: 0 };
var pending = [];
function release() { pending.splice(0).forEach(function (r) { r(); }); }
function settle() { return new Promise(function (r) { setTimeout(r, 0); }); }

// main's session machinery: action.effect is a NO-OP once the session generation changes,
// which is precisely why the in-flight flag needs an explicit reset in teardown.
function beginAuthenticatedAction() {
  var bornStale = stale;
  return {
    guard: function () { if (stale !== bornStale) { var e = new Error('session'); e.name = 'SessionChanged'; throw e; } },
    isCurrent: function () { return stale === bornStale; },
    isSilentError: function (e) { return !!e && (e.name === 'AbortError' || e.name === 'SessionChanged'); },
    effect: function (fn) { if (stale === bornStale) fn(); },
  };
}
function clearAuthenticatedTimeout() {}
function clearAuthenticatedInterval() {}
function handlePollError() {}
function renderWatches() { calls.render += 1; }
function $(id) {
  if (id === 'watch_hint') return { className: '', textContent: '' };
  if (id === 'watches') return { querySelector: function () { return {}; } };
  return null;
}
global.AbortController = function () {
  var self = this;
  this.signal = {};
  this.abort = function () { calls.aborted += 1; self._aborted = true; };
};
function api() {
  calls.api += 1;
  return new Promise(function (resolve, reject) {
    pending.push(function () { resolve({ watches: [] }); });
  });
}
"""
    )


_WATCH_HARNESS = _watch_harness()


def test_jobs_poll_does_not_rearm_while_the_tab_is_hidden():
    """The self-scheduling tick re-armed on `appStarted` alone. Hide the tab while a poll is in
    flight (jobsTimer already null, so stopPolling clears nothing) and the settle re-arms the
    chain — the poller then runs for the whole hidden period, exactly what the visibilitychange
    handler exists to prevent."""
    harness = _fn("scheduleJobsPoll") + r"""
var JOBS_POLL_MS = 0;
var jobsTimer = null, appStarted = true, hidden = false, polls = 0, armed = 0;
var document = { get hidden() { return hidden; } };
var queued = [];
function beginAuthenticatedAction() { return { isCurrent: function () { return true; } }; }
function authenticatedTimeout(fn, ms) { armed++; queued.push(fn); return {}; }
function refresh() { polls++; return Promise.resolve(); }
scheduleJobsPoll();                         // arm the first tick
(async function () {
  var tick = queued.shift();
  hidden = true;                            // tab hidden while the tick's refresh is in flight
  await tick();                             // settles -> the finally decides whether to re-arm
  console.log(JSON.stringify({ armed: armed, queued: queued.length, polls: polls }));
})();
"""
    got = _run(harness)
    assert got["queued"] == 0, "the poll chain re-armed itself while the tab was hidden"
    assert got["armed"] == 1, got


def test_verify_strict_updates_the_compose_summary():
    """extrasSkippedNote() reads #verify_strict, but the field was missing from the listener list,
    so the 'skips …' line kept naming a stale set until an unrelated field fired an event."""
    block = JS.split("].forEach((id) =>", 1)[0]
    listener_list = block[block.rindex("['quality'"):]
    note = _fn("extrasSkippedNote")
    read_ids = {tok.split("'")[0] for tok in note.split("if (on('")[1:]}
    assert "verify_strict" in read_ids, "the note no longer reads verify_strict — update this test"
    missing = sorted(i for i in read_ids if f"'{i}'" not in listener_list)
    assert not missing, f"fields read by extrasSkippedNote with no change listener: {missing}"


def test_probe_chip_never_uncaps_resolution():
    """In the probe popover each available-height chip maps through heightToQualityOption for its
    click action, so a sub-480 height returning 'best' made a '360p' chip set Max resolution to the
    MAXIMUM — the opposite of the click. Every chip must cap at or below the height it names."""
    harness = _fn("heightToQualityOption") + r"""
var caps = {'4k':2160,'1440p':1440,'1080p':1080,'720p':720,'480p':480};
var out = {};
[2160,1440,1080,720,480,360,240,144].forEach(function (h) {
  var q = heightToQualityOption(h);
  out[h] = { q: q, uncapped: q === 'best', cap: caps[q] === undefined ? null : caps[q] };
});
console.log(JSON.stringify(out));
"""
    got = _run(harness)
    for h, info in got.items():
        assert not info["uncapped"], f"a {h}p chip sets Max resolution to 'best' (uncapped)"
        # 480p is the lowest cap the select offers, so sub-480 heights land there; nothing may map
        # ABOVE the height it names (that is the wrong-direction bug).
        assert info["cap"] is not None and info["cap"] <= max(int(h), 480), (h, info)


def test_stale_contact_restores_the_paused_banner_instead_of_hiding_it():
    """setStaleContact(true) overwrites the pipe banner; the restore path hid it outright, and a
    paused idle queue keeps a stable ETag (304), so renderJobs never repaints — the paused banner
    stayed gone with only the button label left to say so."""
    body = _fn("setStaleContact")
    assert "_bannerBeforeStale" in body, (
        "setStaleContact must remember the banner it overwrote so it can put it back")
    assert "banner.hidden = true" not in body.split("else")[-1] or "_bannerBeforeStale" in body.split("else")[-1], body


def test_watch_poller_tick_does_not_abort_an_in_flight_poll():
    """refreshWatches used to abort its predecessor on EVERY 5s tick — the pattern refresh()
    documents and abandoned ("any /api/jobs slower than the fixed 1.2s interval was killed by the
    next tick before it could render — forever, with the AbortError swallowed"). A /api/watches
    slower than 5s therefore never rendered. main keeps its session-scoped authenticatedInterval
    by design, so the fix is the guard, not the timer: only a forced (user-initiated) call may
    pre-empt.

    EXECUTE the guard rather than grepping for it — a substring check cannot tell "the guard runs"
    from "the guard is present but never fires", which is exactly the lesson of the preceding
    commit (7dd9cd8, "execute the job-poll lifecycle instead of grepping for its guards")."""
    harness = _WATCH_HARNESS + r"""
(async function () {
  // 1. a timer tick lands while a poll is in flight: it must NOT abort the predecessor...
  var slow = refreshWatches();              // in flight (never resolves until we release it)
  refreshWatches();                         // the 5s tick
  var afterTick = { calls: calls.api, aborted: calls.aborted };
  // 2. ...but a user-initiated (forced) call still pre-empts it
  refreshWatches({ force: true });
  var afterForce = { calls: calls.api, aborted: calls.aborted };
  release();
  await slow.catch(function () {});
  await settle();
  // 3. after everything settles a later tick must still be able to poll
  var before = calls.api;
  refreshWatches();
  await settle();
  console.log(JSON.stringify({
    tickCalls: afterTick.calls, tickAborted: afterTick.aborted,
    forceCalls: afterForce.calls, forceAborted: afterForce.aborted,
    revived: calls.api > before,
  }));
})();
"""
    got = _run(harness)
    assert got["tickCalls"] == 1, "the 5s tick started a second concurrent /api/watches"
    assert got["tickAborted"] == 0, "a non-forced tick aborted the in-flight poll"
    assert got["forceCalls"] == 2, "a user-initiated refresh must still pre-empt and re-request"
    assert got["forceAborted"] == 1, "the forced refresh did not pre-empt the in-flight poll"
    assert got["revived"], "the poller never recovered — watchesInFlight latched true"


def test_watch_poller_survives_teardown_and_session_loss():
    """REGRESSION (found by audit, introduced with the guard above): watchesInFlight was modelled on
    jobsInFlight but never got jobsInFlight's two resets — stopPolling() and
    resetAuthenticatedUiState(). stopPolling also NULLS watchesAbort, so the guard's only clear
    (`if (watchesAbort === myAbort)`) can never match afterwards, and on the sessionLost path
    action.effect is a no-op anyway because the session generation changed. The flag latched true
    and every non-forced revival path — startApp's refreshWatches(), the view switch, and the 5s
    interval — early-returned, so the Watch panel silently stopped refreshing for the life of the
    page."""
    harness = _WATCH_HARNESS + r"""
(async function () {
  var slow = refreshWatches();              // poll in flight on the Watch view
  stale = true;                             // session expires (generation bumps)
  stopPolling();                            // teardown: aborts + nulls watchesAbort
  release();
  await slow.catch(function () {});
  await settle();
  // measure the flag BEFORE starting anything new — a fresh poll would set it true legitimately
  var latched = watchesInFlight;
  var before = calls.api;
  refreshWatches();                         // the post-re-login revival path (NOT forced)
  await settle();
  console.log(JSON.stringify({ latched: latched, revived: calls.api > before }));
})();
"""
    got = _run(harness)
    assert got["latched"] is False, "watchesInFlight stayed true after teardown — poller is dead"
    assert got["revived"], "a non-forced refresh after re-login never issued a request"


def test_forced_refresh_keeps_ownership_of_the_in_flight_flag():
    """jobsInFlight was a bare boolean: a pre-empted poll's `finally` cleared the flag its forced
    successor had just set, so the next timer tick could start a duplicate concurrent /api/jobs and
    the two responses could render out of order."""
    body = _fn("refresh")
    assert "const myAbort = jobsAbort" in body, (
        "refresh() needs a poll-ownership token, not a shared boolean")
    # the flag may only be cleared by the poll that still OWNS the current controller
    tail = body.rsplit("finally", 1)[1]
    assert "jobsAbort === myAbort" in tail, (
        f"the finally clears the guard without an ownership check: {tail}")
