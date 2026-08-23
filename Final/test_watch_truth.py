"""The Watch view must not stay silent about work that did not happen.

Every /api/watches/* route already returns the truth — {'queued': n}, {'dismissed': n},
{'removed': bool}, {'new': n} or {'new': 0, 'timeout': True} — and the client discarded all of it,
inside a try whose catch was literally empty. So:

  * "Download all" that queued NOTHING re-rendered the same rows and said nothing. The user clicks
    again, and again.
  * "Download all" that queued 3 of 5 removed the three and left two behind with no explanation.
  * A 500 from a watches.json permission error, or a dropped connection, produced no UI whatsoever.
  * "Check for new" hitting the 25 s poll deadline was indistinguishable from "nothing new".

This is the same class the pipeline fixed with pipelineActionMsg() and partial_failures — the Watch
view simply never got that pass.
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
SERVER = (HERE / "webserver.py").read_text(encoding="utf-8")


def _handler() -> str:
    return JS.split("$('watches').addEventListener('click'", 1)[1].split("\n});", 1)[0]


def test_the_routes_really_return_the_counts_the_ui_now_reports():
    """The messages claim server behaviour, so pin the server. If a route stops returning its count,
    the planner silently starts reporting the fallback wording instead — fail here first."""
    for needle in ("'queued': len(queued_ids)", "'dismissed': len(taken)",
                   "'removed': ok", "'timeout': True"):
        assert needle in SERVER, "route no longer returns " + needle + " — revisit watchActionMsg"
    # poll returns a count of new videos under 'new'
    assert "'new': n, 'mode': mode" in SERVER, "poll no longer reports a count"


def test_every_watch_action_binds_its_response_and_reports_it():
    body = _handler()
    # the bug: `await post(...)` with the result discarded. Every branch must bind it.
    assert "await post(" in body
    unbound = [ln.strip() for ln in body.splitlines()
               if "await post(" in ln and "res = await post(" not in ln]
    assert not unbound, "these actions still throw their response away: " + str(unbound)
    assert "watchActionMsg(act, res," in body, "the planner must be called with the real response"
    # ...and the outcome must actually be written somewhere the user sees
    assert "say(m.level, m.note)" in body, "the planner's verdict is computed but never displayed"


def test_the_empty_catch_is_gone():
    """A silent failure is worse than a loud one: the user re-clicks a button that cannot work."""
    body = _handler()
    catch = body.split("} catch (err) {", 1)[1].split("} finally {", 1)[0]
    stripped = "\n".join(ln for ln in catch.splitlines()
                         if ln.strip() and not ln.strip().startswith("//"))
    assert stripped.strip(), "the catch block is empty again — errors vanish with no UI"
    assert "say('bad'" in catch, "an error must be shown to the user, not just swallowed"


def test_the_outcome_strip_is_announced():
    """#watch_hint is the only completion signal this view has; the pipeline's equivalent is a live
    region, so a screen-reader user gets the pipeline's outcomes but none of Watch's."""
    import re
    m = re.search(r'<p class="hint" id="watch_hint"[^>]*>', HTML)
    assert m, "#watch_hint is missing"
    assert 'role="status"' in m.group(0), m.group(0)
    assert 'aria-live' in m.group(0), m.group(0)


def test_watch_action_messages_execute_correctly():
    """EXECUTE the planner. Every rule here is a BRANCH — zero vs partial vs complete, timeout vs
    no-news — and a source assertion cannot tell those apart. This project has learned that the hard
    way more than once today."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function watchActionMsg("
          + JS.split("function watchActionMsg(", 1)[1].split("\n}\n", 1)[0] + "\n}")
    harness = fn + r"""
var M = watchActionMsg;
console.log(JSON.stringify({
  poll_none:     M('poll', {new: 0, mode: 'rss'}, null),
  poll_some:     M('poll', {new: 3, mode: 'rss'}, null),
  poll_one:      M('poll', {new: 1, mode: 'rss'}, null),
  poll_timeout:  M('poll', {new: 0, mode: null, timeout: true}, null),
  poll_missing:  M('poll', {}, null),
  q_all_none:    M('queueall', {queued: 0}, 5),
  q_all_partial: M('queueall', {queued: 3}, 5),
  q_all_full:    M('queueall', {queued: 5}, 5),
  q_all_one:     M('queueall', {queued: 1}, 1),
  q_all_nodenom: M('queueall', {queued: 2}, null),
  q_one_ok:      M('queue1', {queued: 1}, 1),
  q_one_fail:    M('queue1', {queued: 0}, 1),
  dis_none:      M('dismiss', {dismissed: 0}, 4),
  dis_some:      M('dismiss', {dismissed: 4}, 4),
  rm_ok:         M('remove', {removed: true}, null),
  rm_gone:       M('remove', {removed: false}, null),
  tog_on:        M('toggle', {}, true),
  tog_off:       M('toggle', {}, false),
  no_body:       M('queueall', null, 5)
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-400:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # THE BUG: a no-op must never read as success
    assert g["q_all_none"]["level"] == "warn", g["q_all_none"]
    assert "nothing could be queued" in g["q_all_none"]["note"], g["q_all_none"]
    assert g["dis_none"]["level"] == "warn" and "nothing to skip" in g["dis_none"]["note"], g["dis_none"]
    assert g["q_one_fail"]["level"] == "warn", g["q_one_fail"]
    assert g["rm_gone"]["level"] == "warn", g["rm_gone"]

    # THE PARTIAL CASE: the one that used to vanish entirely
    assert g["q_all_partial"]["level"] == "warn", g["q_all_partial"]
    assert "3 of 5" in g["q_all_partial"]["note"], g["q_all_partial"]
    assert "2 could not" in g["q_all_partial"]["note"], g["q_all_partial"]

    # a complete run is plainly successful, and is NOT scolded
    assert g["q_all_full"] == {"level": "ok", "note": "queued 5 videos"}, g["q_all_full"]
    assert g["q_all_one"]["note"] == "queued 1 video", g["q_all_one"]        # singular
    assert g["q_all_nodenom"]["level"] == "ok", g["q_all_nodenom"]           # no denominator -> no false partial

    # a slow feed is not "no new videos" — those were indistinguishable before
    assert g["poll_timeout"]["level"] == "warn", g["poll_timeout"]
    assert g["poll_none"]["level"] == "ok" and "no new videos" in g["poll_none"]["note"], g["poll_none"]
    assert g["poll_timeout"]["note"] != g["poll_none"]["note"], "timeout still reads as 'nothing new'"
    assert "3 new videos" in g["poll_some"]["note"], g["poll_some"]
    assert "1 new video" == g["poll_one"]["note"].split("found ")[1], g["poll_one"]

    # toggle reports the state actually requested
    assert "on" in g["tog_on"]["note"] and "off" in g["tog_off"]["note"], g
    # a route that returns nothing useful must not crash or invent a count
    assert g["no_body"]["level"] in ("ok", "warn") and g["no_body"]["note"], g["no_body"]
    assert g["poll_missing"]["level"] == "ok", g["poll_missing"]


def test_removing_a_watch_is_confirmed_before_anything_is_disabled():
    """Stop-watching is the one irreversible action here: it deletes the watch's download settings
    AND its record of already-seen videos, so re-adding the channel is not an undo — detection starts
    over and previously-seen videos can be re-downloaded. The equivalently destructive Auth "Clear"
    already confirms; this did not.

    The POSITION is load-bearing. The handler adds the id to watchActionsInFlight and disables every
    button in the row before awaiting. Confirming after that would leave a cancelled row's buttons
    dead until a later poll happened to re-render them."""
    body = _handler()
    assert "confirm(" in body, "removing a watch is still unconfirmed"
    # Positions must be computed over CODE only. The comments here legitimately NAME
    # watchActionsInFlight.add() while explaining the ordering, and a raw index() matches the comment
    # instead of the call — which made an earlier version of this assertion fail against correct code.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    gate = code.index("confirm(")
    assert gate < code.index("watchActionsInFlight.add("), \
        "the confirm runs after the in-flight marker — cancelling would strand the row"
    assert gate < code.index("action.ownControl(button)"), \
        "the confirm runs after control ownership — cancelling would leave dead buttons"
    # it must be scoped to remove only: confirming a poll or a queue would be noise
    cond = code[:gate]
    assert "act === 'remove'" in cond, "the confirm is not scoped to the remove action"
    # ...and the dialog must name the channel, not just say "are you sure"
    dialog = code[gate:gate + 700]
    # The label must actually FEED the dialog. Asserting only that '.watch-label' appears somewhere
    # nearby is toothless: replacing the interpolated name with a constant left the lookup in place
    # and the assertion still passed (proven by mutation).
    who = [ln for ln in cond.splitlines() if "const who" in ln]
    assert who, "no channel-name variable is built for the dialog"
    assert "textContent" in who[0], \
        "the dialog name is a constant — it must come from the row's .watch-label: " + who[0].strip()
    assert "who" in dialog, "the dialog does not interpolate the channel name"

    # The confirm must BE the gate, not merely be present. `if (false && !confirm(...))` keeps every
    # token this test looks for while never blocking anything — proven toothless by mutation — so pin
    # the exact gate shape and require the early return it guards.
    lines = code.splitlines()
    gate_i = next(i for i, ln in enumerate(lines) if "confirm(" in ln)
    assert lines[gate_i].strip().startswith("if (!confirm("), \
        "the confirm is not the sole condition of the gate: " + lines[gate_i].strip()
    assert any("return;" in ln for ln in lines[gate_i:gate_i + 5]), \
        "declining the confirm must return before anything is mutated"
    assert "cannot be undone" in dialog.lower(), "say plainly that it cannot be undone"


def test_a_failed_watch_load_does_not_read_as_an_empty_list():
    """refreshWatches' catch only called handlePollError, which marks the shared stale-contact state.
    With no rows rendered there is nothing to look stale, so the static "No channels watched yet"
    empty state stood — telling a user who HAS watches that they have none."""
    fn = JS.split("async function refreshWatches(", 1)[1].split("\n}", 1)[0]
    assert "handlePollError" in fn, "keep the shared stale-contact signalling"
    assert "watch_hint" in fn, "a failed load must say so, not fall back to the empty state"
    # our own aborts (a newer refresh superseded this one) must stay silent
    assert "AbortError" in fn, "an aborted refresh is not a failure and must not show an error"
    # and it must only speak when there is genuinely nothing on screen
    assert ".watch-row" in fn, "only claim a load failure when no rows are rendered"


def _watch_row() -> str:
    return JS.split("function upsertWatchRow(", 1)[1].split("\nfunction ", 1)[0]


def test_watch_rows_only_rebuild_when_their_content_changed():
    """Every 5s poll destroyed and recreated every button in every watch row: the auto note, the
    pending block, and one Download button per waiting video. Keyboard focus fell to <body> mid-tab and
    any in-progress selection of a video title was cancelled.

    This is the risky half of the change, because a wrong signature does not throw — it silently
    freezes a row, and a newly-pending video would never appear. So the placement is asserted here and
    the signature's contents are EXECUTED in the next test, with mutations proving both directions."""
    fn = _watch_row()
    assert "watchRowSig(w)" in fn, "no content signature — rows still rebuild every poll"
    assert "el._lastWatchSig" in fn, "the signature is computed but never remembered"
    gi = fn.index("if (!sigUnchanged) {")
    ge = fn.index("}  // end of the signature guard")
    inside, outside = fn[gi:ge], fn[:gi] + fn[ge:]

    # INSIDE: everything derived from auto / label / pending, including all three body branches
    assert inside.count("body.innerHTML") == 3,         "expected all three body branches behind the guard, found %d" % inside.count("body.innerHTML")
    for needle in (".watch-label", ".wp-auto", "btnToggle.textContent", "btnRemove.title"):
        assert needle in inside, needle + " still runs on every poll"

    # OUTSIDE: anything the signature does NOT cover. Each of these would freeze if guarded.
    assert ".watch-meta" in outside,         "the ageing 'Last checked' text is guarded — it would stop ticking, and it is the row's only heartbeat"
    assert ".watch-err" in outside, "last_error is not in the signature, so guarding it freezes errors"
    assert "watchActionsInFlight" in outside, \
        "the in-flight disable is runtime state, not server data — guarding it strands buttons"
    # ...and the SWEEP itself must be outside AND unconditional. Checking only for
    # 'watchActionsInFlight' matched the `const locked = ...` line while the sweep below it had been
    # wrapped in the guard — proven toothless by mutation.
    sweep = [ln.strip() for ln in fn.splitlines() if "b.disabled = locked" in ln]
    assert sweep, "the in-flight disable sweep is gone"
    assert sweep[0] in outside, "the disable sweep is inside the guard — buttons stay stranded"
    assert not sweep[0].startswith("if "), "the disable sweep is conditional: " + sweep[0]


def test_watch_row_signature_covers_exactly_the_right_fields():
    """EXECUTE the signature. Both directions matter and neither is visible from source:
      * a field missing from it => the row freezes (a new pending video never appears);
      * a field wrongly IN it => the row rebuilds every poll and the fix does nothing."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function watchRowSig("
          + JS.split("function watchRowSig(", 1)[1].split("\n}", 1)[0] + "\n}")
    harness = fn + r"""
var S = watchRowSig;
var base = {id: 'w1', label: 'Some Channel', auto_queue: false, last_checked: 1000,
            last_error: null, pending: [{id: 'v1', title: 'First'}, {id: 'v2', title: 'Second'}]};
function like(over) { return Object.assign({}, base, over); }
console.log(JSON.stringify({
  same:          S(base) === S(like({})),
  auto_flip:     S(base) !== S(like({auto_queue: true})),
  label_change:  S(base) !== S(like({label: 'Renamed'})),
  video_added:   S(base) !== S(like({pending: base.pending.concat([{id: 'v3', title: 'Third'}])})),
  video_removed: S(base) !== S(like({pending: [base.pending[0]]})),
  title_change:  S(base) !== S(like({pending: [{id: 'v1', title: 'Edited'}, base.pending[1]]})),
  reordered:     S(base) !== S(like({pending: [base.pending[1], base.pending[0]]})),
  last_checked:  S(base) === S(like({last_checked: 999999})),
  last_error:    S(base) === S(like({last_error: 'boom'})),
  id_change:     S(base) === S(like({id: 'w2'})),
  empty_ok:      S({}) === S({pending: []}),
  null_safe:     typeof S(null) === 'string'
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # MUST re-render: anything the markup is built from
    assert g["same"] is True, "an unchanged row still re-renders — the fix does nothing"
    assert g["auto_flip"] is True, "toggling auto-download would not repaint the pill or the body"
    assert g["label_change"] is True, "a renamed channel would keep the old title and button labels"
    assert g["video_added"] is True, "A NEWLY PENDING VIDEO WOULD NEVER APPEAR — the worst failure here"
    assert g["video_removed"] is True, "a queued/skipped video would stay listed"
    assert g["title_change"] is True, "a retitled video would keep the stale title"
    assert g["reordered"] is True, "reordering would not repaint"

    # MUST NOT re-render: written outside the guard, or invariant
    assert g["last_checked"] is True,         "last_checked is in the signature, so every 5s poll rebuilds the row again"
    assert g["last_error"] is True, "last_error is in the signature (it is handled outside the guard)"
    assert g["id_change"] is True, "the row id is the Map key and cannot change for one element"

    # degenerate input must not throw during a poll
    assert g["empty_ok"] is True and g["null_safe"] is True, g
