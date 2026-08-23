"""Structural checks for the web console: primary surfaces + must-keep control IDs.

Drives the real shipped HTML/JS under Final/webui/ (no mocks of markup).
"""
from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parent / "webui"
HTML = (WEBUI / "index.html").read_text(encoding="utf-8")
JS = (WEBUI / "app.js").read_text(encoding="utf-8")
CSS = (WEBUI / "style.css").read_text(encoding="utf-8")


def _ids_in_html(html: str) -> set[str]:
    return set(re.findall(r'\bid=["\']([^"\']+)["\']', html))


def test_static_assets_exist_and_nonempty():
    assert (WEBUI / "index.html").stat().st_size > 500
    assert (WEBUI / "style.css").stat().st_size > 500
    assert (WEBUI / "app.js").stat().st_size > 500


def test_compose_and_pipeline_are_primary_workspace():
    # Primary work surfaces live under view-workspace and are not hidden by default.
    assert 'id="view-workspace"' in HTML
    assert 'id="compose"' in HTML
    assert 'id="pipeline"' in HTML or 'class="panel queue"' in HTML
    # Secondary views start hidden (not dumped into the always-visible primary page).
    for vid in ("view-watch", "view-auth", "view-guide"):
        assert re.search(rf'id="{vid}"[^>]*\bhidden\b', HTML) or re.search(
            rf'id="{vid}"[^>]*hidden', HTML
        ), f"{vid} should be hidden until selected"
    # Workspace is the active default view.
    assert re.search(r'id="view-workspace"[^>]*is-active', HTML) or 'view-workspace is-active' in HTML


def test_compose_must_keep_control_ids_present():
    ids = _ids_in_html(HTML)
    required = {
        "urls", "quality", "codec", "section", "section_start", "section_end", "section_precise",
        "encode_mode", "profile", "profile_wrap",
        "output_dir", "browse", "advToggle", "adv",
        "audio_codec", "loudnorm", "sponsorblock", "sub_langs", "sub_mode", "sub_format",
        "audio_format", "cookiefile", "notify_url", "notify_template", "notify_test", "export_markers",
        "no_shorts", "chapters", "embed_metadata", "embed_thumbnail", "write_sidecar",
        "write_sidecar_text", "write_nfo", "proxy_pair", "preserve_hdr", "parallel",
        "submit", "hint", "download_engine",
        # C1 channel/playlist parity: engine-only for the whole life of the web UI. Keep them in
        # the must-keep registry so a refactor cannot drop them back into unreachability.
        "download_archive_on", "download_archive", "download_archive_browse", "organize_by",
        # C2: verify_strict (jobspec.py:340) was engine+CLI only. format_id, audio_map and
        # audio_bitrate are deliberately NOT exposed — see test_workspace_parity.py for why.
        "verify_strict", "audio_quality", "audio_note",
        # Machine capability strip (GPU/encoders live in Compose, not header)
        "gpu", "encoders", "machine_status",
    }
    missing = sorted(required - ids)
    assert not missing, f"Compose controls missing from shipped HTML: {missing}"
    # Section is advanced (optional clip), not primary Quality row
    assert "Clip range" in HTML or "Time range" in HTML
    assert "compose-group" in HTML or "compose_summary" in HTML
    assert "toggleProfile" in JS and "hidden" in JS
    assert "machine-status" in HTML
    assert "loadGpuStatus" in JS
    assert 'id="qcount"' in HTML


def test_pipeline_must_keep_controls_present():
    ids = _ids_in_html(HTML)
    required = {"pause", "retry", "clear", "jobs", "qcount", "led", "pipe_filters", "pipe_banner"}
    missing = sorted(required - ids)
    assert not missing, f"Pipeline controls missing: {missing}"
    # Job card template still carries progress / age-gate / actions hooks.
    for needle in (".job-agegate", ".job-acts", ".fill", "agegate-banner", "Retry"):
        assert needle in JS, f"Pipeline card JS missing {needle!r}"
    # QoL: filters + copy helpers + short labels
    for needle in ("setPipeFilter", "shortJobLabel", "copyText", "pipe_filters", "jobFilter"):
        assert needle in JS, f"Pipeline QoL missing {needle!r}"


def test_watch_and_auth_controls_still_present():
    ids = _ids_in_html(HTML)
    watch = {
        "watch_channel", "watch_quality", "watch_profile", "watch_markers", "watch_organize",
        "watch_nfo", "watch_loudnorm", "watch_noshorts", "watch_autoqueue",
        "watch_filter_title", "watch_min_dur", "watch_max_dur", "watch_after",
        "watch_add", "watch_hint", "watches",
    }
    auth = {"auth_refresh", "auth_test", "auth_clear", "auth_status", "auth_hint"}
    missing = sorted((watch | auth) - ids)
    assert not missing, f"Watch/Auth controls missing: {missing}"


def test_field_guide_substance_preserved():
    # Key guide topics still shipped (labels match Workspace Compose / Advanced).
    for heading in (
        "YouTube URL(s)", "Max resolution", "Preferred stream", "Delivery",
        "Encoder profile", "Save to folder", "Check available formats", "Queue job",
        "Time range (clip)", "Audio &amp; subtitles",
        "For editors", "Performance", "Audio codec", "Loudness target (LUFS)",
        "SponsorBlock", "Timeline markers", "skip YouTube Shorts",
        "Cookies file", "Download engine", "Notify", "Pipeline (job list)",
        "Main compose", "Advanced drawer",
    ):
        assert heading in HTML, f"Field guide heading missing: {heading}"


def test_main_tab_nav_and_activate_view_wired_in_js():
    assert "function activateView" in JS
    assert "function handleTablistKeydown" in JS
    assert "data-view" in HTML
    for name in ("workspace", "watch", "auth", "speedtest", "updates", "guide"):
        assert f'id="nav-{name}"' in HTML
        assert f'id="view-{name}"' in HTML
    assert "/api/updates" in JS
    assert "refreshUpdates" in JS


def test_main_tab_transitions_that_hide_the_invoker_request_focus():
    """Clicks, keys, and cross-view calls hide their source control, so each must explicitly move
    focus; hash and initial activation intentionally rely on activateView's false default."""
    assert "activateView(btn.dataset.view, { focus: true })" in JS
    assert "activateView(next.dataset.view, { focus: true })" in JS
    assert "activateView('updates', { focus: true })" in JS
    assert JS.count("activateView('auth', { focus: true })") >= 2
    assert "hashchange', () => activateView(viewFromHash(), { syncHash: false })" in JS
    assert "activateView(viewFromHash(), { syncHash: true })" in JS


def test_api_contracts_unchanged_in_client():
    # Same REST paths the server already exposes — reorg must not invent new contracts.
    for path in (
        "/api/jobs", "/api/retry-failed", "/api/clear", "/api/pause", "/api/resume",
        "/api/remove",
        "/api/pick-folder", "/api/pick-file", "/api/notify-test", "/api/open-folder", "/api/profiles", "/api/gpu", "/api/health",
        "/api/watches", "/api/auth", "/api/jobs/rerun",
    ):
        assert path in JS, f"Client no longer calls {path}"
    assert "open folder" in JS
    # Password login gate (no token URLs for normal users)
    assert "yt_final_web_session_v2" in JS or "SESSION_KEY" in JS
    assert "/api/login" in JS
    assert 'id="login-form"' in HTML or 'id="login-gate"' in HTML
    assert "login-password" in HTML


def test_css_linked_and_has_workspace_grid():
    assert 'href="/static/style.css"' in HTML
    assert 'src="/static/app.js"' in HTML
    assert ".workspace-grid" in CSS
    assert ".nav-tab" in CSS
    assert ".app-nav" in CSS


def test_host_os_detection_surface_in_header_and_health():
    """Global host OS badge + health fields so Windows/macOS is not Updates-only."""
    assert 'id="host_os_badge"' in HTML
    assert 'id="host_os_gauge"' in HTML
    assert 'host os' in HTML.lower() or 'host_os' in HTML
    assert 'function applyHostOs' in JS
    assert 'HOST_OS' in JS
    assert "h.host_os" in JS or 'applyHostOs(h)' in JS
    assert 'gauge-host' in CSS


def test_auth_wording_is_not_hardcoded_to_macos():
    """The Auth tab said "Browsers on this Mac" / "This Mac:" verbatim, which is simply wrong on a
    Windows host (the app is cross-platform and HOST_OS is already server-reported). Wording must be
    derived from HOST_OS, never hardcoded to one OS."""
    import re
    # The verbatim user-facing forms must be gone (the helper's own 'this Mac' BRANCH is correct and
    # is what produces that wording on a real Mac — so only the hardcoded prose is banned).
    assert 'Browsers on this Mac' not in JS, 'hardcoded "Browsers on this Mac" still in app.js'
    assert not re.search(r'["\'`]This Mac:', JS), 'hardcoded "This Mac:" prose still in app.js'
    assert 'hostDeviceLabel' in JS, 'expected a HOST_OS-derived device label helper'
    # the helper must branch on the server-reported host, not the browser UA
    m = re.search(r'function hostDeviceLabel[\s\S]{0,400}', JS)
    assert m and 'HOST_OS' in m.group(0), 'hostDeviceLabel must derive from HOST_OS'
    assert 'navigator' not in m.group(0), 'device label must not come from the browser UA'
    # and it must actually be used for the Auth-tab prose
    assert re.search(r'Browsers on \$\{hostDeviceLabel\(\)\}', JS), 'Auth note not using the helper'
    assert JS.count('hostDeviceLabel(true)') >= 4, 'reqLive lines not using the helper'


def test_narrow_header_does_not_let_branding_overflow_into_gauges():
    """At phone widths the header row is `id meta` with .rack-id justify-self:start, which sizes it to
    MAX-CONTENT rather than its grid track — so min-width:0 never bites, .sub's ellipsis never
    triggers, and the tagline rendered on top of the status gauges (measured 77px overlap at 390px).
    The narrow breakpoint must stop .rack-id being max-content sized."""
    import re
    # NOTE: style.css has MORE THAN ONE max-width:640px block — check them all, not just the first.
    blocks = re.findall(r'@media\(max-width:640px\)\{(.*?)\n\}', CSS, re.S)
    assert blocks, 'expected a max-width:640px responsive block'
    joined = '\n'.join(blocks)
    assert re.search(r'\.rack-id\{[^}]*justify-self:\s*stretch', joined), \
        '.rack-id must stretch (not start) at phone widths so its own track clips the tagline'


def test_app_js_handles_hashchange_and_clears_elapsed():
    # hash deep-links / browser back must switch views after load
    assert "hashchange" in JS
    assert "viewFromHash" in JS or "location.hash" in JS
    # non-running jobs must clear the running elapsed timer
    assert "elapsed" in JS and "textContent = ''" in JS


def test_field_guide_matches_workspace_labels():
    """Field guide form-map: same labels as Compose / Advanced + layout markers."""
    from pathlib import Path
    html = (Path(__file__).parent / "webui" / "index.html").read_text(encoding="utf-8")
    js = (Path(__file__).parent / "webui" / "app.js").read_text(encoding="utf-8")
    css = (Path(__file__).parent / "webui" / "style.css").read_text(encoding="utf-8")
    assert 'id="view-guide"' in html
    # Exact Compose labels (must appear in both form and guide)
    for label in (
        "Max resolution", "Preferred stream", "Delivery", "Encoder profile",
        "Save to folder", "Check available formats", "Queue job",
        "Cookies file", "Time range", "Loudness target (LUFS)",
        "Timeline markers (chapters → editor)", "Download engine",
        "Webhook URL",
    ):
        assert label in html, f"Workspace/guide label missing: {label}"
    # Form-map structure (not card gallery)
    assert "guide-layout" in html
    assert "guide-toc" in html
    assert "guide-row" in html
    assert "guide-panel" in html
    assert "guide-opts" in html
    assert "guide-path" in html
    assert "Main compose" in html
    assert "Advanced drawer" in html
    assert "Pipeline (job list)" in html
    assert 'id="g-compose"' in html
    assert 'id="g-access"' in html
    assert "playGuideEnter" in js
    assert "scrollGuideTo" in js
    assert "guide-reveal-in" in css
    assert "guide-layout" in css


def test_auth_browser_import_controls_present():
    from pathlib import Path
    html = (Path(__file__).parent / "webui" / "index.html").read_text(encoding="utf-8")
    js = (Path(__file__).parent / "webui" / "app.js").read_text(encoding="utf-8")
    assert 'id="auth-from-browser-btn"' in html
    assert 'id="auth-login-btn"' in html
    assert 'id="auth-browser"' in html
    assert 'id="auth-help-panel"' in html
    assert 'id="auth_help_btn"' in html
    assert 'id="auth-hero"' in html
    assert 'auth-btn-primary' in html or 'auth-btn-primary' in (Path(__file__).parent / 'webui' / 'style.css').read_text(encoding='utf-8')
    assert 'Test cookies' in html
    assert 'Login with Chrome' in html


def test_compose_access_linked_to_auth():
    """Advanced → Access is Auth-first; browser pull removed; path override collapsed."""
    from pathlib import Path
    base = Path(__file__).parent / "webui"
    html = (base / "index.html").read_text(encoding="utf-8")
    js = (base / "app.js").read_text(encoding="utf-8")
    css = (base / "style.css").read_text(encoding="utf-8")
    assert 'id="access_auth"' in html
    assert 'id="access_auth_lead"' in html
    assert 'id="access_open_auth"' in html
    assert 'id="access_summary_hint"' in html
    assert 'id="access_override"' in html
    assert 'id="cookiefile"' in html
    assert 'id="cookiefile_browse"' in html
    assert 'id="cookiefile_upload"' in html
    assert 'id="cookiefile_clear"' in html
    # Per-job Yes/No for Auth cookies
    assert 'id="use_auth_yes"' in html
    assert 'id="use_auth_no"' in html
    assert 'Use cookies for this job' in html
    # B: no Compose browser-pull control (Auth tab owns that)
    assert 'id="cookies_from_browser"' not in html
    assert "Override for this job only" in html
    assert "syncComposeAccessFromAuth" in js
    assert "jobWantsAuth" in js
    assert "use_auth:" in js
    assert "applyAuthStatus" in js
    assert "access_open_auth" in js
    assert "setCookiefilePath" in js
    assert "/api/pick-file" in js
    assert "/api/upload-cookiefile" in js
    assert "cookies_from_browser: null" in js
    assert ".access-auth" in css
    assert ".access-override" in css
    assert ".access-mode" in css
    assert 'auth-help-clip' in html  # animated collapse wrapper
    assert 'is-open' in (Path(__file__).parent / 'webui' / 'style.css').read_text(encoding='utf-8')
    assert "How to sign in" in html
    assert "setAuthHelpOpen" in js
    assert "classList.contains('is-open')" in js or 'classList.contains("is-open")' in js
    assert "panel.hidden" not in js.split("auth_help_btn")[1][:400]  # toggle must not use hidden
    assert "authFromBrowser" in js
    assert "from_browser" in js
    assert "prefers-reduced-motion" in (Path(__file__).parent / 'webui' / 'style.css').read_text(encoding='utf-8')


def test_auth_import_controls_present():
    ids = _ids_in_html(HTML)
    for need in ("auth-file", "auth-file-name", "auth-paste", "auth-import-btn"):
        assert need in ids, f"auth import control missing: {need}"
    assert "action: 'import'" in JS or 'action: "import"' in JS or "action: 'import'" in JS.replace('"', "'")


def test_upsert_card_preserves_content_across_unchanged_polls():
    # Regression: upsertCard() must not unconditionally tear down and rebuild the
    # label/output/error/agegate/actions subtrees on every poll. It should compute a
    # content signature and skip the rebuild (and thus preserve DOM state such as the
    # failed-job error 'more/less' toggle) when nothing relevant has changed.
    assert "_lastContentSig" in JS
    assert "contentUnchanged" in JS
    # The err subtree rebuild must be reachable only through the unchanged-skip guard,
    # not run unconditionally on every call.
    upsert_start = JS.index("function upsertCard(j)")
    upsert_body = JS[upsert_start:JS.index("\nfunction ", upsert_start + 10)]
    assert "if (contentUnchanged) return el;" in upsert_body
    err_idx = upsert_body.index("job-err")
    guard_idx = upsert_body.index("if (contentUnchanged) return el;")
    assert guard_idx < err_idx, "content-unchanged guard must precede the error subtree rebuild"


def test_job_card_shows_engine_speed_and_running_cancel():
    """Running cards must expose cancel + live engine; done cards show avg/peak from result."""
    upsert_start = JS.index("function upsertCard(j)")
    upsert_body = JS[upsert_start:JS.index("\nfunction ", upsert_start + 10)]
    assert "cancel_requested" in upsert_body
    assert "j.status === 'running'" in upsert_body
    assert "via " in upsert_body or "via ' + " in upsert_body
    assert "avg_speed" in upsert_body or "avg " in upsert_body
    assert "peak_speed" in upsert_body or "peak " in upsert_body
    assert "cancelling" in upsert_body
    # Cancel action for running jobs (not only queued/failed)
    assert "j.status === 'running'" in upsert_body
    assert "postAction('/api/cancel'" in upsert_body or 'postAction("/api/cancel"' in upsert_body
    # Dedicated always-visible result row for finalized jobs
    assert "job-result" in JS
    assert "engine " in upsert_body
    # Corner dismiss control on every card
    assert "job-dismiss" in JS
    assert "dismissJob" in JS
    assert "/api/remove" in JS


def test_upsert_card_skips_hidden_hidden_unchanged_cards():
    # Regression: cards hidden by the active pipe filter, with no status change, should
    # return early before doing any subtree work at all.
    upsert_start = JS.index("function upsertCard(j)")
    upsert_body = JS[upsert_start:JS.index("\nfunction ", upsert_start + 10)]
    assert "wasHidden && newHidden && prevStatus === j.status" in upsert_body


def test_settings_panel_present():
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    for ident in ('id="network_enabled"', 'id="network_proxy_scheme"',
                  'id="network_proxy_hostport"',
                  'id="network_save"', 'id="network_test"', 'id="network_status"'):
        assert ident in html, f"missing {ident}"


def test_per_job_proxy_relabeled_as_override():
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    assert "overrides the default" in html


def test_logout_clears_clock_timer():
    # Regression: clockTimer must be torn down on logout, mirroring jobsTimer/watchTimer,
    # so the footer clock/elapsed tick doesn't keep firing forever after sign-out.
    assert "function stopClock" in JS
    lost_body = JS.split("function sessionLost(", 1)[1].split("\n}", 1)[0]
    assert "stopClock()" in lost_body
    logout_idx = JS.index("logoutBtn.onclick")
    logout_body = JS[logout_idx:JS.index("};", logout_idx)]
    assert "finally" in logout_body and "sessionLost(" in logout_body


def test_desync_toggle_present():
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    assert 'id="network_desync"' in html
    assert 'Bypass ISP throttling' in html


def test_preset_controls_present():
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    for ident in ('id="preset_select"', 'id="preset_save"', 'id="preset_del"'):
        assert ident in html, f"missing {ident}"


def test_speedtest_tab_present_and_wired():
    # Nav tab + secondary view start hidden until selected.
    assert 'data-view="speedtest"' in HTML and 'id="nav-speedtest"' in HTML
    assert re.search(r'id="view-speedtest"[^>]*hidden', HTML), "speedtest view must be hidden by default"
    # Core controls: run button, optional proxy/VPN tester, live phases, results host.
    for ident in ('id="st_run"', 'id="st_proxy_scheme"', 'id="st_proxy_hostport"',
                  'id="st_phases"', 'id="st_results"'):
        assert ident in HTML, f"speedtest control missing: {ident}"
    # Client calls the endpoint and renders verdict/bars/recommendations.
    assert "/api/speedtest" in JS
    for needle in ("runSpeedTest", "renderSpeedTest", "stRenderBars", "stDoAction", "stBuildProxy"):
        assert needle in JS, f"speedtest JS missing {needle!r}"
    # One-click actions route to existing endpoints (no invented contracts).
    assert "enable_bypass" in JS and "install_tool" in JS
    # Actions are payload-driven (server carries desync_mode / measured proxy / desync:false).
    assert "a.payload" in JS
    assert "open_auth" in JS and "stProxyError" in JS   # cookie fix opens Auth; proxy typo validated
    assert 'id="st_proxy_err"' in HTML                   # inline proxy-validation error slot
    # v2 UX: run history / VPN-compare, copy result, live elapsed, a11y live regions.
    assert "stPushHistory" in JS and "stCompareLine" in JS and "yt_speedtest_history" in JS
    assert "stResultText" in JS and "copyText" in JS     # copy-result affordance
    assert re.search(r'id="st_phases"[^>]*aria-live', HTML)
    assert re.search(r'id="st_results"[^>]*aria-live', HTML)
    # Styling shipped.
    assert ".st-verdict" in CSS and ".st-bars" in CSS and ".st-rec" in CSS and ".st-compare" in CSS


def test_speedtest_mentions_vpn_and_proxy_option():
    # The user-requested "turn on a VPN / add a proxy for testing" affordance is present.
    assert "VPN" in HTML
    assert "proxy" in HTML.lower()


# --------------------------------------------------------------------------------------------
# Download-Workspace hardening: five confirmed defects (platform copy, 403 dead end, poll
# self-abort, invisible dead server, non-live #hint). Source assertions because the web UI has
# no JS test runner; each is paired with a reproduction noted in its docstring.
# --------------------------------------------------------------------------------------------

def test_no_macos_only_copy_in_markup():
    """The earlier "this Mac" cleanup only touched app.js — and the test that guarded it asserted
    against JS only, so the MARKUP kept shipping macOS-only strings to Windows users:
    "Reveal in Finder" (the Workspace probe pop-out) and title="Pick cookies.txt on this Mac".
    Neither is rewritten at runtime, so the invariant must cover HTML too."""
    assert "this Mac" not in HTML, 'hardcoded "this Mac" still in index.html'
    assert "Finder" not in HTML, 'hardcoded "Finder" still in index.html (name it neutrally)'
    # the JS helper stays the single source of truth for device wording
    assert "hostDeviceLabel" in JS


def test_open_folder_recovery_hint_is_not_windows_only():
    """The stale-route recovery hint told every user to run `python yt_final.py serve` — wrong on
    macOS (system python3) and it bypasses the launcher on both. Name the launchers instead."""
    assert "python yt_final.py serve" not in JS, "recovery hint hardcodes a Windows-only command"


def test_session_loss_resets_app_so_relogin_works():
    """A mid-flight 401 called showLogin() but never reset appStarted, and startApp() early-returns
    on `if (appStarted) return;` — so re-login stored the token and then never showed the app. The
    user typed the right password, saw no error, and nothing happened (reload was the only escape).
    A single sessionLost() helper must clear that state, and sessionFetch() must use it."""
    assert "function sessionLost(" in JS, "expected a shared sessionLost() helper"
    body = JS.split("function sessionLost(", 1)[1].split("\n}", 1)[0]
    assert "appStarted = false" in body, "sessionLost must clear appStarted or re-login stays stuck"
    assert "stopPolling()" in body, "sessionLost must stop the pollers"
    assert "showLogin(" in body
    session_fetch = JS.split("async function sessionFetch(", 1)[1].split("\n}", 1)[0]
    assert "res.status === 401" in session_fetch, \
        "sessionFetch() must treat 401 as session loss"
    assert "sessionLost(" in session_fetch
    assert "res.status === 403" not in session_fetch, \
        "403 is an action/policy error and must remain available to the caller"


def test_session_restore_catch_uses_session_ownership_for_every_stale_failure():
    """A stale generic fetch rejection is just as stale as an AbortError and must stay silent."""
    restore = JS.split("async function tryExistingSession()", 1)[1].split(
        "\n}\n\nconst loginForm", 1
    )[0]
    assert "action.isSilentError(e)" in restore, (
        "restore catch must classify staleness through its authenticated action; "
        "an AbortError-only check can relock a newer successful login"
    )


def test_job_poll_does_not_abort_its_own_inflight_request():
    """refresh() aborted the previous request unconditionally while running on a fixed 1.2s
    setInterval, so any /api/jobs slower than 1.2s was killed by the next tick — forever, with
    AbortError swallowed. The pipeline froze on stale progress with no error. The timer must be
    self-scheduling and must skip a tick while a poll is still in flight."""
    # NB the comma matters: "setInterval(refresh" is also a prefix of setInterval(refreshWatches, …)
    assert "setInterval(refresh," not in JS, "the job poll must self-schedule, not use setInterval"
    assert "JOBS_POLL_MS" in JS, "expected a named poll interval"
    ref = JS.split("async function refresh(", 1)[1].split("\n}\n", 1)[0]
    # assert the GUARD EXPRESSION, not merely that the flag is mentioned — the flag is also
    # assigned true/false in the body, so a token check survives gutting the early return
    assert "jobsInFlight && !opts.force" in ref, \
        "refresh() must skip a timer tick while a poll is still in flight"
    assert "jobsInFlight = false" in ref, "the in-flight flag must be cleared in a finally"
    # an explicit user-initiated refresh may still pre-empt the in-flight poll
    assert "opts.force && jobsAbort" in ref, "keep a force path for user-initiated refreshes"


def test_lost_server_is_surfaced_not_swallowed():
    """handlePollError returned silently for anything that was not a 403, while the elapsed-time
    clock runs off an independent timer — so killing the server (closing the launcher console) left
    a climbing timer, a pulsing bar and a busy LED asserting the download was still progressing."""
    h = JS.split("function handlePollError(", 1)[1].split("\n}", 1)[0]
    assert "_pollFailStreak" in h, "handlePollError must count consecutive failures"
    assert "setStaleContact(" in h or "stale" in h.lower(), "a lost server must be surfaced in the UI"
    assert "function setStaleContact(" in JS
    # and the streak must be cleared on the success AND 304 paths
    ref = JS.split("async function refresh(", 1)[1].split("\n}\n", 1)[0]
    assert ref.count("_pollFailStreak = 0") >= 2, "reset the streak on both 304 and success"
    assert "is-stale" in CSS, "expected a stale-contact style"


def test_hint_strip_is_a_live_region():
    """#hint is the ONLY feedback channel for submit validation, queue confirmations and every
    pipeline action, yet it was the one status strip without a live region — its three siblings
    (#probe_result, #compose_summary, #clip_status) all have aria-live. Screen-reader users pressing
    Queue job with an empty URL box got silence."""
    import re
    m = re.search(r'<p class="hint" id="hint"[^>]*>', HTML)
    assert m, '#hint element not found'
    tag = m.group(0)
    assert 'role="status"' in tag or 'aria-live' in tag, f'#hint is not a live region: {tag}'


# --------------------------------------------------------------------------------------------
# Workspace truth-telling + a11y round 2: the compose summary must not assert things the engine
# will not do, the advertised subtitle feature must be reachable, focus must be visible, and a
# "detect" button must not silently rewrite the user's own choices.
# --------------------------------------------------------------------------------------------

def test_summary_warns_when_h264_cannot_reach_the_requested_height():
    """quality=4k/1440p/best + Preferred stream=H.264 silently yields 1080p: the selector's first
    clause `bestvideo[height<=2160][vcodec^=avc1]` matches a 1080p avc1 stream and never falls
    through to the 4K VP9/AV1 clause (yt_final.py:3076 says so outright: "avc1 tops out at 1080p on
    YouTube"). The summary asserted "4k" anyway — the sentence directly above Queue job."""
    assert "h264CapWarning" in JS, "expected an explicit H.264-height-cap warning helper"
    body = JS.split("function h264CapWarning(", 1)[1].split("\n}", 1)[0]
    # require the real comparisons, not just the tokens (a token check survives gutting the guard).
    # NB a source assertion still cannot prove the LOGIC — that is verified by executing the helper
    # in a browser against the full truth table (both quality and codec axes).
    for q in ("4k", "1440p", "best"):
        assert f"quality === '{q}'" in body, f"the cap warning must test quality === {q!r}"
    assert "codec !== 'h264'" in body or "codec === 'h264'" in body, \
        "the cap warning must gate on the H.264 source codec"
    assert "return ''" in body, "it must stay silent for honest combinations"
    summ = JS.split("function updateComposeSummary(", 1)[1].split("\n}", 1)[0]
    assert "h264CapWarning(" in summ, "updateComposeSummary must surface the cap warning"


def test_summary_says_download_only_skips_editor_extras():
    """encode_mode='none' never calls _finalize (yt_final.py:5665 states it), so every editor extra
    is silently dropped — including chapters and embed_metadata, which are CHECKED BY DEFAULT. The
    summary must say so instead of implying the settings apply."""
    summ = JS.split("function updateComposeSummary(", 1)[1].split("\n}", 1)[0]
    assert "extrasSkippedNote(" in summ, "summary must note skipped extras for Download only"
    body = JS.split("function extrasSkippedNote(", 1)[1].split("\n}", 1)[0]
    assert "'none'" in body or '"none"' in body, "the note must trigger on encode_mode none"
    # it should name the extras that are actually set, not just say "some settings"
    for probe in ("export_markers", "write_nfo", "chapters"):
        assert probe in body, f"the note should consider {probe}"


def test_sub_auto_is_reachable_and_defaults_on():
    """sub_auto had ZERO references in the web UI while the engine gates writeautomaticsub on it
    (yt_final.py:3338) — so a user typing a subtitle language got no file at all on the majority of
    YouTube videos, which carry only ASR captions. Exposed, and default-on because "give me captions
    if any exist" is the intent for an ingest tool."""
    import re
    m = re.search(r'<input[^>]*id="sub_auto"[^>]*>', HTML)
    assert m, "no #sub_auto control in the markup"
    assert 'type="checkbox"' in m.group(0), "sub_auto should be a checkbox"
    assert "checked" in m.group(0), "sub_auto must default ON (see docstring)"
    assert "sub_auto" in JS, "buildSpec must send sub_auto"
    spec = JS.split("function buildSpec(", 1)[1].split("\n}", 1)[0]
    # the KEY assignment (with colon): prose/comments mentioning sub_auto must not satisfy this
    assert "sub_auto:" in spec, "sub_auto missing from the buildSpec payload"


def test_focus_ring_has_an_opaque_layer():
    """--ring was a single 22%-alpha shadow (style.css:58) while 11 rules set outline:none, so the
    focus indicator measured ~1.6:1 — under the 3:1 of WCAG SC 1.4.11 — on every button-like control
    in the workspace, including the cancel/retry pair on a job card. Give the ring an OPAQUE inner
    layer so keyboard focus is actually visible."""
    import re
    m = re.search(r'--ring:([^;]+);', CSS)
    assert m, "--ring token not found"
    ring = m.group(1)
    # an opaque colour layer (var/hex/rgb without alpha), not only an rgba() wash
    assert "var(--acc" in ring or re.search(r'#[0-9a-fA-F]{3,6}', ring), \
        f"--ring has no opaque layer, focus stays invisible: {ring}"


def test_probe_does_not_auto_apply_over_user_choices():
    """"Detect max resolution" called applyProbeSuggested() unconditionally (app.js:768), so a user
    who deliberately chose 4k + AV1 and clicked it just to inspect the source had their selection
    silently reset to 1080p H.264. It must be a read-only query — the popover already has two
    explicit apply buttons — and the label must say so."""
    # Assert no call STATEMENT (trailing semicolon). A plain count is wrong: the function definition
    # line and any prose mention also contain "applyProbeSuggested()".
    assert "applyProbeSuggested();" not in JS, \
        "applyProbeSuggested() must not be auto-invoked by the detect handler"
    assert "probe_apply_suggested" in JS, "the explicit apply button must remain wired"
    assert "Detect max resolution" not in HTML, "rename the button so it reads as a query"
    assert "Check available formats" in HTML


def test_compose_warning_logic_executes_correctly():
    """EXECUTE the two summary helpers instead of pattern-matching them.

    Source assertions cannot catch a gutted guard (prefixing `false &&` leaves every token in
    place), so the truth table is verified by running the real functions in node — which this app
    already requires for yt-dlp's nsig solver. Skipped only if node is unavailable; the structural
    tests above still pin the wiring in that case.
    """
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")

    src = JS
    cap = "function h264CapWarning(" + src.split("function h264CapWarning(", 1)[1].split("\n}", 1)[0] + "\n}"
    note = "function extrasSkippedNote(" + src.split("function extrasSkippedNote(", 1)[1].split("\n}", 1)[0] + "\n}"

    harness = cap + "\n" + note + r"""
// minimal $() stub: only the ids named in ON are "set"
var ON = new Set(["export_markers", "write_nfo"]);
function $(id) { return ON.has(id) ? { type: 'checkbox', checked: true, value: 'x' } : null; }
var out = {
  // h264CapWarning: warn iff a >1080p request is paired with the H.264 source ladder
  cap_4k_h264:     h264CapWarning('4k', 'h264')     !== '',
  cap_1440_h264:   h264CapWarning('1440p', 'h264')  !== '',
  cap_best_h264:   h264CapWarning('best', 'h264')   !== '',
  cap_1080_h264:   h264CapWarning('1080p', 'h264')  !== '',
  cap_720_h264:    h264CapWarning('720p', 'h264')   !== '',
  cap_4k_av1:      h264CapWarning('4k', 'av1')      !== '',
  cap_4k_vp9:      h264CapWarning('4k', 'vp9')      !== '',
  cap_4k_any:      h264CapWarning('4k', 'any')      !== '',
  cap_audio_h264:  h264CapWarning('audio', 'h264')  !== '',
  // extrasSkippedNote: only for Download only, and it must NAME what is set
  note_copy:       extrasSkippedNote('copy')     !== '',
  note_reencode:   extrasSkippedNote('reencode') !== '',
  note_none:       extrasSkippedNote('none')     !== '',
  note_none_text:  extrasSkippedNote('none'),
};
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-400:]}"
    got = json.loads(r.stdout.strip().splitlines()[-1])

    # warns exactly for the unreachable combinations
    assert got["cap_4k_h264"] and got["cap_1440_h264"] and got["cap_best_h264"], got
    # ...and stays silent everywhere else (this is what a gutted guard breaks)
    for k in ("cap_1080_h264", "cap_720_h264", "cap_4k_av1", "cap_4k_vp9", "cap_4k_any",
              "cap_audio_h264"):
        assert not got[k], f"{k} must NOT warn: {got}"

    # the skipped-extras note fires only for Download only, and names the extras actually set
    assert got["note_none"] and not got["note_copy"] and not got["note_reencode"], got
    assert "markers" in got["note_none_text"] and ".nfo" in got["note_none_text"], got["note_none_text"]


def test_rerun_idempotency_ui_executes_double_click_retry_reuse_and_rotation():
    """Execute the real rerun state machine with both controls for one source."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the rerun UI state machine")

    start = JS.index("const rerunsInFlight")
    end = JS.index("async function bumpJob", start)
    unit = JS[start:end]
    harness = r"""
var generated = 0;
var crypto = {
  randomUUID() {
    generated += 1;
    return `00000000-0000-4000-8000-${String(generated).padStart(12, '0')}`;
  },
  getRandomValues() { throw new Error('randomUUID path expected in harness'); },
};
var controls = [
  { dataset: { rerunSource: 'source-1' }, disabled: false },
  { dataset: { rerunSource: 'source-1' }, disabled: false },
];
var document = {
  querySelectorAll() { return controls; },
};
var calls = [];
var pending = [];
function api(path, options) {
  var payload = JSON.parse(options.body);
  calls.push({ path, payload });
  return new Promise((resolve, reject) => pending.push({ resolve, reject, payload }));
}
function beginAuthenticatedAction() {
  return {
    guard() {},
    isCurrent() { return true; },
    isSilentError() { return false; },
  };
}
var refreshes = 0;
function refresh() { refreshes += 1; }
function pipelineHint() {}

// __RERUN_UNIT__

async function settleFailure(message, doubleClick) {
  var before = calls.length;
  var first = rerunJob('source-1');
  var second = doubleClick ? rerunJob('source-1') : Promise.resolve();
  var disabled = controls.every((control) => control.disabled);
  var issued = calls.length - before;
  pending.shift().reject(new Error(message));
  await Promise.all([first, second]);
  return {
    issued,
    disabled,
    reenabled: controls.every((control) => !control.disabled),
    requestId: calls[calls.length - 1].payload.request_id,
  };
}

(async () => {
  var failures = [];
  failures.push(await settleFailure('fetch failed', true));
  failures.push(await settleFailure('AbortError', false));
  failures.push(await settleFailure('timeout', false));
  failures.push(await settleFailure('body parse failed', false));

  // No malformed 2xx body is authoritative, even when it echoes the right key.
  var malformedFactories = [
    () => null,
    () => [],
    () => ({}),
    (id) => ({ request_id: id }),
    (id) => ({ id: null, request_id: id }),
    (id) => ({ id: 7, request_id: id }),
    (id) => ({ id: '', request_id: id }),
    (id) => ({ id: '   ', request_id: id }),
    () => ({ id: 'j_wrong_key', request_id: 'different-key' }),
  ];
  var malformed = [];
  for (var makeBody of malformedFactories) {
    var malformedPromise = rerunJob('source-1');
    var malformedCall = calls[calls.length - 1];
    var malformedId = malformedCall.payload.request_id;
    pending.shift().resolve(makeBody(malformedId));
    await malformedPromise;
    malformed.push({
      retained: rerunRequestIds.get('source-1') === malformedId,
      reenabled: controls.every((control) => !control.disabled),
      requestId: malformedId,
    });
  }

  var successPromise = rerunJob('source-1');
  var successCall = calls[calls.length - 1];
  pending.shift().resolve({ id: 'j_first', request_id: successCall.payload.request_id });
  await successPromise;
  var clearedAfterSuccess = !rerunRequestIds.has('source-1');

  // Settle an old pre-reset request after a replacement operation has taken ownership.
  var stalePromise = rerunJob('source-1');
  var staleCall = calls[calls.length - 1];
  var stalePending = pending.shift();
  var rotated = staleCall.payload.request_id !== successCall.payload.request_id;
  var bothDisabledAgain = controls.every((control) => control.disabled);
  resetRerunState();
  var resetCleared = rerunsInFlight.size === 0 && rerunRequestIds.size === 0
    && controls.every((control) => !control.disabled);
  var currentPromise = rerunJob('source-1');
  var currentCall = calls[calls.length - 1];
  var currentPending = pending.shift();
  var beforeGuardedThird = calls.length;
  await rerunJob('source-1');
  var guardedBeforeStale = calls.length === beforeGuardedThird;

  stalePending.resolve({ id: 'j_stale', request_id: staleCall.payload.request_id });
  await stalePromise;
  var stalePreservedOwner = rerunsInFlight.has('source-1')
    && rerunRequestIds.get('source-1') === currentCall.payload.request_id
    && controls.every((control) => control.disabled);
  var beforeThirdAfterStale = calls.length;
  rerunJob('source-1');
  var guardedAfterStale = calls.length === beforeThirdAfterStale;

  currentPending.resolve({ id: 'j_current', request_id: currentCall.payload.request_id });
  await currentPromise;
  var currentCleared = !rerunsInFlight.has('source-1')
    && !rerunRequestIds.has('source-1')
    && controls.every((control) => !control.disabled);

  console.log(JSON.stringify({
    failures,
    malformed,
    clearedAfterSuccess,
    rotated,
    bothDisabledAgain,
    resetCleared,
    guardedBeforeStale,
    stalePreservedOwner,
    guardedAfterStale,
    currentCleared,
    refreshes,
    generated,
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""
    result = subprocess.run(
        [node, "-e", harness.replace("// __RERUN_UNIT__", unit)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, f"node failed: {result.stderr[-1000:]}"
    got = json.loads(result.stdout.strip().splitlines()[-1])

    assert got["failures"][0]["issued"] == 1
    assert all(item["issued"] == 1 for item in got["failures"])
    assert all(item["disabled"] and item["reenabled"] for item in got["failures"])
    assert len({item["requestId"] for item in got["failures"]}) == 1
    assert all(item["retained"] and item["reenabled"] for item in got["malformed"])
    assert len({item["requestId"] for item in got["malformed"]}) == 1
    assert got["malformed"][0]["requestId"] == got["failures"][0]["requestId"]
    assert got["clearedAfterSuccess"]
    assert got["rotated"] and got["bothDisabledAgain"]
    assert got["resetCleared"]
    assert got["guardedBeforeStale"]
    assert got["stalePreservedOwner"]
    assert got["guardedAfterStale"]
    assert got["currentCleared"]
    assert got["refreshes"] == 2
    assert got["generated"] == 3
    assert "Math.random" not in unit
    assert "resetRerunState();" in JS.split("function resetAuthenticatedUiState()", 1)[1]


def test_every_guide_panel_is_reachable_from_the_jump_to_list():
    """The Jump-to chips are the only navigation in a very long page. #g-batch — the panel documenting
    the archive and folder-layout controls — was missing, so the newest section was unreachable."""
    import re
    panels = set(re.findall(r'<details class="guide-panel" id="([^"]+)"', HTML))
    assert panels, "no guide panels found — did the markup change shape?"
    chips = set(re.findall(r'<a class="guide-toc-link" href="#([^"]+)"', HTML))
    missing = sorted(panels - chips)
    assert not missing, "guide panels with no Jump-to chip: %s" % missing


def test_every_view_completion_hint_is_a_live_region():
    """Each view's hint strip is its ONLY completion signal. The pipeline announces its outcomes via
    #jobs_live; without role=status these views announce nothing at all to a screen reader."""
    import re
    for hid in ('watch_hint', 'auth_hint', 'updates_hint', 'hint'):
        m = re.search(r'<p[^>]*id="%s"[^>]*>' % hid, HTML)
        assert m, "missing #%s" % hid
        assert 'role="status"' in m.group(0), "#%s is not a live region: %s" % (hid, m.group(0))


def test_the_login_gate_says_where_the_password_comes_from():
    """A first-run user is shown a password box and nothing else. The password is printed by the
    launcher and saved to a file; neither was named anywhere on the gate."""
    gate = HTML.split('id="login-gate"', 1)[1].split('</form>', 1)[0] if 'id="login-gate"' in HTML         else HTML.split('id="login-form"', 1)[1].split('</form>', 1)[0]
    assert 'webui.txt' in gate, "the gate must name the file the password is saved to"
    assert 'launcher' in gate.lower() or 'starts' in gate.lower(),         "the gate must say the password is printed at startup"
    # it must NOT live in #login-msg: app.js updates that live region's text
    hint = HTML.split('id="login-msg"', 1)[1].split('</p>', 1)[0]
    assert 'webui.txt' not in hint, "put it in its own element — #login-msg gets updated by JS"
    # and the gate must not advertise the env-var override to whoever is looking at a locked console
    assert 'YT_FINAL_WEBUI_PASSWORD' not in gate,         "do not name the override env var on the lock screen"


def test_the_dark_console_declares_its_color_scheme():
    """Without color-scheme, UA-drawn widgets (select popups, scrollbars, form controls) render in the
    light scheme inside a deliberately dark UI."""
    root = CSS.split(':root{', 1)[1].split('}', 1)[0]
    assert 'color-scheme' in root, "no color-scheme in :root"
    assert 'dark' in root.split('color-scheme', 1)[1].split(';', 1)[0]
    assert 'name="color-scheme"' in HTML, "no color-scheme meta (UA chrome before CSS loads)"


def test_lost_contact_is_visible_on_every_view_not_just_the_workspace():
    """setStaleContact wrote only into #jobs, #led and #pipe_banner. #jobs and #pipe_banner both live
    inside #view-workspace, which is hidden on the other five views — so a server that died while the
    user sat on Watch, Auth, Speed test, Updates or the Guide produced no visible signal at all.

    #pipe_banner deliberately stays where it is (three call sites depend on it); the fix is an
    additional strip in the header, outside every view."""
    assert 'id="conn_banner"' in HTML, "no shell-level lost-contact strip"
    # POSITION is the whole point: it must come before the first view in the document, i.e. not be
    # nested inside one. A banner inside a hidden view is exactly the bug being fixed.
    assert HTML.index('id="conn_banner"') < HTML.index('id="view-workspace"'),         "#conn_banner is inside a view — it will be hidden exactly when it is needed"
    m = __import__("re").search(r'<p[^>]*id="conn_banner"[^>]*>', HTML)
    assert 'role="status"' in m.group(0), "the notice must be announced: " + m.group(0)
    assert ".conn-banner" in CSS, "the strip is unstyled"

    raw = JS.split("function setStaleContact(", 1)[1].split("\n}", 1)[0]
    # CODE ONLY. The comments in here legitimately name #clock while explaining why it is left alone,
    # and a raw substring check matched the comment and failed against correct code — the same
    # comment-vs-code trap that produced several bad assertions across this batch.
    fn = "\n".join(ln for ln in raw.splitlines()
                   if not ln.strip().startswith(("//", "*", "/*")))
    assert "conn_banner" in fn, "setStaleContact does not drive the shell strip"
    # the server-derived queue count must not keep displaying a frozen number as fact
    # Require the WRITE, not just the lookup: deleting the assignment left `const qc = $('qcount')`
    # behind and a bare "qcount" check still passed (proven toothless by mutation).
    assert "qcount" in fn, "the queue gauge is not touched at all"
    qc_write = [ln for ln in fn.splitlines() if "qc" in ln and "textContent" in ln]
    assert qc_write, "the queue gauge still shows a stale count as if it were current fact"
    # ...but the clock is local time and must be left alone
    assert "clock" not in fn, "the clock shows local time and stays true — do not blank it"
    # the LED should say WHY, not just change colour
    assert "led.title" in fn, "the always-visible LED gives no reason for the state"
    # this runs on every healthy poll, so the write must be guarded rather than unconditional
    assert "textContent !== stxt" in fn,         "unguarded writes replay the entrance animation on every 1.2s poll"
