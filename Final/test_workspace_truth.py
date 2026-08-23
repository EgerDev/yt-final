"""Batch A — the Workspace must not report a clean success for work that partly or wholly failed.

Four confirmed defects, each verified against the shipped code before this file existed:
  * the engine ALREADY records partial_failures for a partly-failed --parallel batch (and its own
    comment says the card should show "N of M downloaded"), but app.js never referenced it;
  * "retry failed" / "clear done" discarded the counts the server returns and always claimed success;
  * an unusable output_dir fell back to the previous folder with only a logger.warning, so the job
    reported done and the file was somewhere else;
  * a failed _finalize delivered the RAW download and still reported "Done", so choosing
    "Re-encode — editor profile" could hand back the untouched YouTube stream.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

WEBUI = Path(__file__).resolve().parent / "webui"
HTML = (WEBUI / "index.html").read_text(encoding="utf-8")
JS = (WEBUI / "app.js").read_text(encoding="utf-8")
CSS = (WEBUI / "style.css").read_text(encoding="utf-8")
SERVER = (Path(__file__).resolve().parent / "webserver.py").read_text(encoding="utf-8")
ENGINE = (Path(__file__).resolve().parent / "yt_final.py").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    marker = f"function {name}("
    assert marker in JS, f"missing required frontend function {name}()"
    body = JS.split(marker, 1)[1].split("\n}\n", 1)[0]
    return marker + body + "\n}"


def test_login_and_session_feedback_has_one_writer_and_truthful_severities():
    """Every login/session transition routes through the one stable announcement writer."""
    helper = _function_source("setLoginFeedback")
    assert "$('login-msg')" in helper
    assert ".className" not in helper, "severity classes must not replace base/transient classes"
    assert "textContent" in helper

    without_helper = JS.replace(helper, "", 1)
    assert "$('login-msg')" not in without_helper, "#login-msg must have exactly one direct accessor"
    assert "$('login-hint')" not in JS, "retired login feedback node is still being accessed"

    show_login = _function_source("showLogin")
    assert "function showLogin(message = '', severity = 'status')" in show_login
    assert "setLoginFeedback(message, severity);" in show_login
    assert show_login.index("setLoginFeedback(message, severity);") < show_login.index(
        "const pw = $('login-password')"
    ), "showLogin must always clear/update feedback before scheduling password focus"

    session_lost = _function_source("sessionLost")
    assert (
        "function sessionLost(message = 'Session expired — sign in again', severity = 'alert')"
        in session_lost
    )
    assert "showLogin(message, severity);" in session_lost

    restore = JS.split("async function tryExistingSession()", 1)[1].split(
        "const loginForm =", 1
    )[0]
    assert "if (!stored) { showLogin(); return false; }" in restore
    assert restore.count("showLogin(SESSION_STORAGE_RETRY_MESSAGE, 'alert');") == 1
    assert (
        restore.count(
            "showLogin('Session temporarily unavailable — try again', 'alert');"
        )
        == 2
    )
    http_failure = restore.split("if (res.status !== 200 || res.ok !== true)", 1)[1].split(
        "return false;", 1
    )[0]
    assert "'alert'," in http_failure
    assert "setLoginFeedback('', 'status');" in restore

    login = JS.split("const loginForm =", 1)[1].split("const logoutBtn =", 1)[0]
    for expected in (
        "setLoginFeedback('Enter the password', 'alert');",
        "setLoginFeedback('Signing in…', 'status');",
        "setLoginFeedback('', 'status');",
        "setLoginFeedback('Cannot reach the app — is it running?', 'alert');",
    ):
        assert expected in login
    assert "Invalid session response" in login and "Wrong password" in login
    assert "'alert'" in login.split("Invalid session response", 1)[1].split("return;", 1)[0]
    assert login.count("showLogin(SESSION_STORAGE_RETRY_MESSAGE, 'alert');") == 1
    login_request = login.split("const exchange = await runWithDeadline(", 1)[1].split(
        "const res = exchange.response;", 1
    )[0]
    assert "fetch('/api/login'" in login_request
    assert "{ signal: attemptController.signal }" in login_request
    assert "30_000" in login_request
    assert ".className" not in login and ".textContent" not in login

    revoke = _function_source("revokeIssuedLoginSession")
    revoke_request = revoke.split("const response = await fetchWithTimeout(", 1)[1].split(
        ");", 1
    )[0]
    assert "'/api/logout'" in revoke_request and "5_000" in revoke_request
    assert "'X-Token': token" in revoke

    logout = JS.split("const logoutBtn =", 1)[1].split("// Render-only:", 1)[0]
    assert "sessionLost('Signed out', 'status')" in logout

    flash = JS.split("(function wireHintFlash()", 1)[1]
    assert "'login-msg'" in flash
    assert "'login-hint'" not in flash


def test_card_renders_partial_failures_the_engine_already_sends():
    """result_meta() has emitted partial_failures since it was written — explicitly so the card could
    say "N of M downloaded" (yt_final.py comment at the emit site) — yet app.js never read it, so a
    six-URL job that lost two links showed a clean green done."""
    assert "partial_failures" in JS, "the card must read result.partial_failures"
    assert "job-partial" in JS and "job-partial" in CSS, "expected a distinct partial-failure row"


def test_pipeline_actions_report_the_server_counts():
    """/api/retry-failed returns {'reset': n} and /api/clear returns {'removed': n}; the client threw
    both away and said 'retried failed jobs' even when nothing was retried."""
    assert "'reset':" in SERVER or '"reset":' in SERVER
    for needle in ("res.reset", "res.removed"):
        assert needle in JS, f"the client must use the returned count ({needle})"
    assert "nothing to retry" in JS.lower() or "no failed jobs" in JS.lower(), \
        "a zero count must read as 'nothing to do', not success"


def test_output_dir_is_preflighted_before_queueing():
    """An unmounted drive silently landed the file in the previous folder. Preflight it: a check route
    on the server, called from the client before the job is queued."""
    assert "/api/check-folder" in SERVER, "expected a folder-check route"
    assert "/api/check-folder" in JS, "the client must call the folder check"
    assert "writable" in SERVER, "the check must report writability, not just existence"


def test_output_dir_fallback_is_recorded_in_result_meta():
    """When the fallback does happen the job must SAY so, not just log it."""
    assert "output_dir_fallback" in ENGINE, "the engine must record the fallback"
    assert "last_output_dir_fallback" in ENGINE, "record it on the downloader like last_partial_errors"
    # reset per job so one job cannot inherit another's fallback
    body = ENGINE.split("dl.last_partial_errors = None", 1)[1][:400]
    assert "last_output_dir_fallback = None" in body, "reset the fallback per job"
    assert "output_dir_fallback" in JS, "the card must surface the fallback"


def test_degraded_finalize_is_recorded_and_shown():
    """A non-cancel exception from _finalize logs and then delivers the raw download as 'Done'. Keep
    delivering it (better than nothing) but mark the job degraded so the file's nature is visible."""
    assert "last_degraded" in ENGINE, "the engine must record a degraded delivery"
    assert "degraded" in JS, "the card must surface the degraded delivery"
    assert "job-degraded" in CSS or "is-degraded" in CSS, "expected a degraded style"
    # it must be recorded in the same except-block that performs the raw-download fallback (bounded
    # to that block: the next `if out:` line ends it, so this cannot pass by matching distant code)
    seg = ENGINE.split('logger.error(f"[Finalize] {src}: {e}")', 1)[1].split("\n            if out:", 1)[0]
    assert "last_degraded" in seg, "record the degradation at the raw-download fallback site"
    assert "out is not None" in seg, "only record it when a file is actually delivered"


# ---- real behaviour (not source strings): result_meta must carry the two new truths ----------

def _meta_for(**attrs):
    """A YouTubeDownloader stub carrying only what result_meta() reads."""
    import yt_final
    dl = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    dl.speed_stats = lambda: {'avg_speed': None, 'peak_speed': None}
    dl._throttle_hint = lambda *a, **k: None
    for k, v in attrs.items():
        setattr(dl, k, v)
    return dl.result_meta()


def test_result_meta_reports_output_dir_fallback_only_when_it_happened():
    fb = {'requested': r'E:\Project\Footage', 'used': r'C:\dl', 'error': 'drive not available'}
    m = _meta_for(last_output_dir_fallback=fb)
    assert m.get('output_dir_fallback') == fb, m
    # a normal job must not carry it (the downloader is REUSED across jobs, so a leak would make an
    # unrelated later job claim someone else's fallback)
    assert 'output_dir_fallback' not in _meta_for(last_output_dir_fallback=None)
    assert 'output_dir_fallback' not in _meta_for()


def test_result_meta_reports_a_degraded_delivery_only_when_it_happened():
    deg = {'stage': 'finalize', 'detail': 'ffmpeg died', 'delivered': 'raw download (processing did not run)'}
    m = _meta_for(last_degraded=deg)
    assert m.get('degraded') == deg, m
    assert 'degraded' not in _meta_for(last_degraded=None)
    assert 'degraded' not in _meta_for()


def test_run_body_resets_both_flags_so_jobs_cannot_inherit_them():
    """The downloader is shared for the process lifetime, so these must be cleared at the start of
    every job's exclusive section — next to the existing last_partial_errors reset."""
    seg = ENGINE.split("dl.last_partial_errors = None", 1)[1][:600]
    assert "dl.last_output_dir_fallback = None" in seg
    assert "dl.last_degraded = None" in seg


def test_caveat_and_action_message_logic_executes_correctly():
    """EXECUTE the two pure client helpers. Source assertions cannot verify logic — gutting
    `if (pf && pf.length)` leaves every token in place and the string checks still passed — so the
    truth tables are run in node (already required for yt-dlp's nsig solver)."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")

    cav = "function jobCaveats(" + JS.split("function jobCaveats(", 1)[1].split("\n}", 1)[0] + "\n}"
    msg = "function pipelineActionMsg(" + JS.split("function pipelineActionMsg(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = cav + "\n" + msg + r"""
var J6 = { spec: { urls: ['a','b','c','d','e','f'] } };
var PF = [{url:'e',error:'HTTP 403'},{url:'f',error:'boom'}];
var out = {
  clean:        jobCaveats(J6, {engine:'builtin'}).lines.length,
  no_result:    jobCaveats(J6, null).lines.length,
  partial_n:    jobCaveats(J6, {partial_failures: PF}).lines.length,
  partial_text: jobCaveats(J6, {partial_failures: PF}).lines[0],
  partial_no_spec: jobCaveats({}, {partial_failures: PF}).lines[0],
  fallback:     jobCaveats(J6, {output_dir_fallback:{requested:'/vol/ext',used:'/home/dl',error:'gone'}}).lines[0],
  degraded:     jobCaveats(J6, {degraded:{delivered:'raw download',detail:'ffmpeg died'}}).lines[0],
  all_three:    jobCaveats(J6, {partial_failures: PF,
                                output_dir_fallback:{requested:'/vol/ext',used:'/home/dl'},
                                degraded:{delivered:'raw download'}}).lines.length,
  retry_0: pipelineActionMsg('retry', 0),
  retry_1: pipelineActionMsg('retry', 1),
  retry_3: pipelineActionMsg('retry', 3),
  clear_0: pipelineActionMsg('clear', 0),
  clear_2: pipelineActionMsg('clear', 2),
  retry_unknown: pipelineActionMsg('retry', null),
};
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-400:]}"
    got = json.loads(r.stdout.strip().splitlines()[-1])

    # a genuinely clean job must stay clean (this is what a gutted guard breaks)
    assert got["clean"] == 0 and got["no_result"] == 0, got
    # partial failures: counted against the spec's URL total when available
    assert got["partial_n"] == 1, got
    assert "4 of 6 downloaded" in got["partial_text"], got["partial_text"]
    assert "2 URL(s) failed" in got["partial_no_spec"], got["partial_no_spec"]
    # the other two truths, and all three at once
    assert "requested folder unavailable" in got["fallback"], got["fallback"]
    assert "processing failed" in got["degraded"] and "raw download" in got["degraded"], got["degraded"]
    assert got["all_three"] == 3, got
    # action messages must not claim success for a no-op
    assert "nothing to retry" in got["retry_0"] and "nothing to clear" in got["clear_0"], got
    assert got["retry_1"] == "re-queued 1 failed job", got          # singular
    assert got["retry_3"] == "re-queued 3 failed jobs", got         # plural
    assert got["clear_2"] == "cleared 2 finished jobs", got
    assert "retried failed jobs" == got["retry_unknown"], got       # old server, no count


def test_open_folder_timeout_cannot_leave_its_button_dead_for_the_session():
    """The "open folder" button disables itself and re-enables in a finally. That finally only runs if
    the request SETTLES — and the server handler ends in a blocking ShellExecuteW / os.startfile on the
    request thread, so a stuck file manager or a path on an unreachable drive never answers. api() sets
    no timeout of its own.

    This was harmless while the card was rebuilt every 1.2 s (a fresh, enabled button appeared, which
    also meant the disable was decorative and double-clicks could launch several windows). Now that the
    content guard keeps the row, the disable is real — so the request needs a deadline."""
    body = JS.split("folder.onclick = async () => {", 1)[1].split("row.append(", 1)[0]
    assert "/api/open-folder" in body
    assert "AbortSignal.timeout(" not in body, (
        "the open-folder handler directly depends on a native-only timeout API"
    )
    assert "fetchWithTimeout(" not in body, (
        "open-folder bypasses the authenticated session-aware API path"
    )
    # the deadline must be on THIS request, not somewhere else in the function
    call = body.split("/api/open-folder", 1)[1].split("});", 1)[0]
    assert "timeoutMs: 30000" in call, "the authenticated open-folder request has no 30s deadline"
    # The initiating session owns the re-enable. A replacement lifecycle/render owns its controls;
    # an old finally must not mutate controls now owned by that newer session.
    assert "const control = action.ownControl(folder);" in body
    assert "finally { control.release(); }" in body
    # A current fallback timeout is AbortError and must announce the truthful timeout note. A stale
    # logout/re-login action stays silent, and its old finally cannot mutate a newly owned control.
    catch = body.split("} catch (e) {", 1)[1].split("} finally", 1)[0]
    assert "if (!action.isCurrent()) return;" in catch
    assert "action.isSilentError(" not in catch
    assert "pipelineHint(false, openFolderErrorNote(e));" in catch


def test_open_folder_error_note_logic_executes_correctly():
    """EXECUTE the branches. A timeout, a missing route and a generic failure need different advice,
    and telling a user their file is missing when the file manager merely hung is a lie."""
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function openFolderErrorNote("
          + JS.split("function openFolderErrorNote(", 1)[1].split("\n}", 1)[0] + "\n}")
    harness = fn + r"""
var N = openFolderErrorNote;
function e(name, msg) { var x = new Error(msg || ''); x.name = name; return x; }
console.log(JSON.stringify({
  timeout:  N(e('TimeoutError', 'The operation was aborted due to timeout')),
  aborted:  N(e('AbortError', 'aborted')),
  old_app:  N(e('Error', 'HTTP 404: not found')),
  generic:  N(e('Error', 'permission denied')),
  empty:    N(e('Error', '')),
  nothing:  N(null)
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    # a stuck shell call must NOT read as a missing file, and must say the button recovered
    assert "30s" in g["timeout"] and "works again" in g["timeout"], g["timeout"]
    assert "file itself is fine" in g["timeout"], "do not imply the download is gone"
    assert g["aborted"] == g["timeout"], "an abort is the same story as a timeout"
    # an old server without the route keeps its restart advice, naming the launchers
    assert "restart" in g["old_app"] and "yt_final.bat" in g["old_app"], g["old_app"]
    assert "30s" not in g["old_app"], "a 404 is not a timeout"
    # everything else passes through, and never renders empty or 'undefined'
    assert g["generic"] == "permission denied", g["generic"]
    for k in ("empty", "nothing"):
        assert g[k] and "undefined" not in g[k], (k, g[k])


def test_the_content_gate_documents_what_it_skips():
    """Everything after the gate is skipped on unchanged polls, and block order puts the action buttons
    last — so code appended at the end of upsertCard runs on first paint and then silently never again.
    The comment is the only thing standing between a future reader and that trap."""
    body = JS.split("function upsertCard(", 1)[1].split("\nfunction ", 1)[0]
    pre = body.split("if (contentUnchanged) return el;", 1)[0]
    tail_comment = pre[-900:]
    assert "ABOVE this line" in tail_comment or "above this line" in tail_comment,         "the gate must say where always-run code belongs"
    assert "never again" in tail_comment, "the gate must name the silent failure mode"
