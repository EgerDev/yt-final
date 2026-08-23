"""EXECUTE the job-poll lifecycle instead of grepping for its guards.

Three shipped regressions live behind these functions, and each was pinned only by a source-text
assertion in test_webui_structure.py:

  * refresh() aborted the previous request unconditionally on a fixed 1.2s setInterval, so ANY
    /api/jobs slower than one tick was killed by the next one — forever, with the AbortError
    swallowed. The pipeline froze on stale progress showing no error.
  * handlePollError returned silently for anything that was not a 403, so killing the server left a
    climbing elapsed timer, a pulsing bar and a busy LED all asserting the download was progressing.
  * refreshWatches' catch left the static "No channels watched yet" empty state standing, telling a
    user who HAS watches that they have none.

A grep cannot tell "the guard runs" from "the guard is present but never fires". Mutation proved it:
each of these could be neutralised with the whole 202-test structure suite still green. So the real
functions are extracted and run against stubbed collaborators, on a fake clock, with no browser.

beginAuthenticatedAction is extracted for real rather than stubbed, because refresh()'s in-flight
flag is cleared through action.effect() — a hand-written stub would be testing the stub.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
JS = (HERE / "webui" / "app.js").read_text(encoding="utf-8")


def _fn(name, *, prefix="function "):
    """Slice one top-level function out of app.js by its declaration."""
    head = prefix + name + "("
    assert JS.count(head) == 1, f"{name}: expected exactly one declaration, found {JS.count(head)}"
    body = JS.split(head, 1)[1].split("\n}\n", 1)[0]
    return head + body + "\n}"


def _run(harness):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI")
    result = subprocess.run(
        [node, "-e", harness], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, "node failed: " + result.stderr[-600:]
    return json.loads(result.stdout.strip().splitlines()[-1])


# Shared preamble: the real functions plus the smallest believable world around them.
def _preamble():
    return (
        _fn("refresh", prefix="async function ")
        + "\n" + _fn("handlePollError")
        + "\n" + _fn("beginAuthenticatedAction")
        + "\n" + r"""
// ---- module state the real functions read/write -------------------------------------------
var jobsInFlight = false, jobsAbort = null, jobsEtag = null;
var _pollAuthLost = false, _pollFailStreak = 0;
var calls = { fetch: 0, render: 0, stale: [], sessionLost: 0, aborted: 0 };

// session context: always current, so isSilentError/effect exercise their real branches
function currentSessionContext() { return { id: 1 }; }
function sessionContextIsCurrent(_c) { return true; }
function assertCurrentSession(_c) {}
function ownAuthenticatedControl() { return null; }

function setStaleContact(on) { calls.stale.push(!!on); }
function sessionLost(_m) { calls.sessionLost += 1; }
function renderJobs(_b) { calls.render += 1; }
function sessionJson(res) { return Promise.resolve(res._body || {}); }

// A controllable AbortController so "was the in-flight request aborted?" is observable.
global.AbortController = function () {
  var self = this;
  this.signal = { aborted: false, _onabort: null };
  this.abort = function () { self.signal.aborted = true; calls.aborted += 1;
                             if (self.signal._onabort) self.signal._onabort(); };
};
function abortError() { var e = new Error('aborted'); e.name = 'AbortError'; return e; }
"""
    )


def test_a_slow_poll_is_not_killed_by_the_next_tick():
    """THE REGRESSION. Tick 2 must skip while tick 1 is still in flight — and must not abort it."""
    got = _run(_preamble() + r"""
var release;
function sessionFetch(_u, opts) {
  calls.fetch += 1;
  return new Promise(function (resolve, reject) {
    if (opts.signal) opts.signal._onabort = function () { reject(abortError()); };
    release = function () { resolve({ ok: true, status: 200, headers: { get: function () { return 'e1'; } }, _body: {} }); };
  });
}
(async () => {
  var first = refresh();                 // tick 1 — hangs
  await refresh();                       // tick 2 — must be a no-op
  var midway = { fetches: calls.fetch, aborted: calls.aborted, inFlight: jobsInFlight };
  release(); await first;
  console.log(JSON.stringify({ midway: midway, after: { fetches: calls.fetch,
    aborted: calls.aborted, inFlight: jobsInFlight, rendered: calls.render } }));
})();
""")
    assert got["midway"]["fetches"] == 1, "the second tick started a second request: " + str(got)
    assert got["midway"]["aborted"] == 0, "the second tick ABORTED the slow poll — the shipped bug"
    assert got["midway"]["inFlight"] is True
    # the slow poll still completes and renders
    assert got["after"]["rendered"] == 1 and got["after"]["inFlight"] is False, got


def test_a_user_initiated_refresh_still_pre_empts_the_slow_poll():
    """The guard must not make the UI unresponsive: force bypasses it AND aborts the in-flight one."""
    got = _run(_preamble() + r"""
function sessionFetch(_u, opts) {
  calls.fetch += 1;
  return new Promise(function (resolve, reject) {
    if (opts.signal) opts.signal._onabort = function () { reject(abortError()); };
    if (calls.fetch > 1) resolve({ ok: true, status: 200, headers: { get: function () { return 'e'; } }, _body: {} });
  });
}
(async () => {
  refresh();                             // hangs
  await refresh({ force: true });
  console.log(JSON.stringify({ fetches: calls.fetch, aborted: calls.aborted,
    rendered: calls.render, inFlight: jobsInFlight }));
})();
""")
    assert got["fetches"] == 2, "force did not pre-empt the in-flight poll: " + str(got)
    assert got["aborted"] == 1, "force must abort the request it is replacing"
    assert got["rendered"] == 1 and got["inFlight"] is False, got


def test_the_inflight_flag_is_cleared_even_when_the_request_throws():
    """If the flag leaks true the poller stops forever — a worse freeze than the bug it fixes."""
    got = _run(_preamble() + r"""
function sessionFetch() { calls.fetch += 1; return Promise.reject(new Error('ECONNREFUSED')); }
(async () => {
  await refresh();
  var afterThrow = jobsInFlight;
  await refresh();                       // must be able to poll again
  console.log(JSON.stringify({ afterThrow: afterThrow, fetches: calls.fetch, streak: _pollFailStreak }));
})();
""")
    assert got["afterThrow"] is False, "jobsInFlight leaked true — polling would stop forever"
    assert got["fetches"] == 2, "the poller did not recover after a failure"
    assert got["streak"] == 2, "each failure must count toward the stale-contact streak"


def test_a_dead_server_is_announced_after_a_short_streak_not_on_the_first_blip():
    """One dropped poll is normal; three in a row means the server is gone. The elapsed timer runs off
    an independent interval, so without this the UI keeps asserting progress that is not happening."""
    got = _run(_preamble() + r"""
handlePollError(new Error('ECONNREFUSED'));
var afterOne = calls.stale.slice();
handlePollError(new Error('ECONNREFUSED'));
handlePollError(new Error('ECONNREFUSED'));
var afterThree = calls.stale.slice();
handlePollError(abortError());           // our own abort is not a server failure
handlePollError(null);
console.log(JSON.stringify({ afterOne: afterOne, afterThree: afterThree,
  streak: _pollFailStreak, sessionLost: calls.sessionLost }));
""")
    assert got["afterOne"] == [], "a single blip must not claim the server is gone"
    assert True in got["afterThree"], "three consecutive failures must surface the lost server"
    assert got["streak"] == 3, "an AbortError/None must not increment the streak: " + str(got)
    assert got["sessionLost"] == 0


def test_a_401_logs_out_once_and_does_not_count_as_a_transport_failure():
    got = _run(_preamble() + r"""
handlePollError(new Error('HTTP 401'));
handlePollError(new Error('session expired'));
console.log(JSON.stringify({ sessionLost: calls.sessionLost, streak: _pollFailStreak,
  stale: calls.stale }));
""")
    assert got["sessionLost"] == 1, "an expired session must sign out exactly once, not per poll"
    assert got["streak"] == 0, "an auth failure is not a transport failure"
    assert got["stale"] == []


def test_a_recovered_poll_clears_the_stale_state_on_both_the_200_and_304_paths():
    """Two reset sites, and the 304 one is easy to forget: an idle server answers 304 forever, so a
    UI that only cleared on 200 would stay stuck showing 'lost contact' after recovery."""
    got = _run(_preamble() + r"""
var mode = '304';
function sessionFetch() {
  calls.fetch += 1;
  if (mode === '304') return Promise.resolve({ ok: false, status: 304, headers: { get: function () { return null; } } });
  return Promise.resolve({ ok: true, status: 200, headers: { get: function () { return 'e'; } }, _body: {} });
}
(async () => {
  _pollFailStreak = 5;
  await refresh();
  var after304 = { streak: _pollFailStreak, stale: calls.stale.slice(), rendered: calls.render };
  _pollFailStreak = 5; mode = '200';
  await refresh();
  console.log(JSON.stringify({ after304: after304,
    after200: { streak: _pollFailStreak, stale: calls.stale.slice(), rendered: calls.render } }));
})();
""")
    assert got["after304"]["streak"] == 0, "a 304 must clear the failure streak"
    assert got["after304"]["stale"][-1] is False, "a 304 must clear the stale-contact banner"
    assert got["after304"]["rendered"] == 0, "a 304 must skip the re-render — that is its whole point"
    assert got["after200"]["streak"] == 0 and got["after200"]["stale"][-1] is False, got
    assert got["after200"]["rendered"] == 1, got


def test_a_failed_watch_load_says_so_instead_of_claiming_there_are_none():
    """refreshWatches' catch. With no rows rendered there is nothing to look stale, so the static
    'No channels watched yet' empty state stood — telling a user who HAS watches that they have none.
    Our own aborts (a newer refresh superseding this one) must stay silent."""
    got = _run(
        _fn("refreshWatches", prefix="async function ")
        + "\n" + _fn("handlePollError")
        + "\n" + _fn("beginAuthenticatedAction")
        + "\n" + r"""
var watchesAbort = null, watchesInFlight = false, _pollAuthLost = false, _pollFailStreak = 0;
var hint = { className: '', textContent: '' };
var rowsPresent = false;
function currentSessionContext() { return { id: 1 }; }
function sessionContextIsCurrent() { return true; }
function assertCurrentSession() {}
function ownAuthenticatedControl() { return null; }
function setStaleContact() {}
function sessionLost() {}
function renderWatches() {}
function $(id) {
  if (id === 'watch_hint') return hint;
  if (id === 'watches') return { querySelector: function () { return rowsPresent ? {} : null; } };
  return null;
}
global.AbortController = function () { this.signal = {}; this.abort = function () {}; };
var failure = new Error('ECONNREFUSED');
function api() { return Promise.reject(failure); }
(async () => {
  await refreshWatches();
  var onFailure = { cls: hint.className, txt: hint.textContent };

  hint.className = ''; hint.textContent = '';
  var ab = new Error('superseded'); ab.name = 'AbortError'; failure = ab;
  await refreshWatches();
  var onAbort = { cls: hint.className, txt: hint.textContent };

  // TimeoutError is the LIVE half of that guard: action.isSilentError() already returns early for
  // AbortError, so the inner AbortError clause is belt-and-braces, but TimeoutError reaches it.
  hint.className = ''; hint.textContent = '';
  var to = new Error('deadline'); to.name = 'TimeoutError'; failure = to;
  await refreshWatches();
  var onTimeout = { cls: hint.className, txt: hint.textContent };

  hint.className = ''; hint.textContent = ''; rowsPresent = true;
  failure = new Error('ECONNREFUSED');
  await refreshWatches();
  var withRows = { cls: hint.className, txt: hint.textContent };
  console.log(JSON.stringify({ onFailure: onFailure, onAbort: onAbort,
    onTimeout: onTimeout, withRows: withRows }));
})();
""")
    assert "could not load" in got["onFailure"]["txt"], \
        "a failed load still reads as an empty watch list: " + str(got["onFailure"])
    assert got["onFailure"]["cls"] == "hint bad", got["onFailure"]
    assert got["onAbort"]["txt"] == "", \
        "our own superseding abort was reported to the user as a failure"
    assert got["onTimeout"]["txt"] == "", \
        "a request deadline is our own bound, not evidence the watch list is unloadable"
    assert got["withRows"]["txt"] == "", \
        "rows are on screen, so the list did load — do not claim it could not"
