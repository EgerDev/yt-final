"""Speed Test decision engine — PURE logic, no network/sockets.

Takes a measurement vector (gathered by the /api/speedtest endpoint, which injects the real
fetchers) and returns a plain-language verdict + honest comparison bars + ranked, actionable
recommendations. Kept import-light and side-effect-free so the whole decision tree is unit-testable
with fake measurements.

Measurement contract (post-hardening): every probe value is EITHER a positive float (measured) OR
None (failed / not run). A failed probe is NEVER 0.0 — that distinction is load-bearing: a transient
fetch error must degrade to "couldn't test", not masquerade as an extreme throttle.

Priority: P0 JS-runtime gate → P1 egress-aware throttle+bypass A/B → P2 PO provider → link/other.
Throttle detection is anchored to the same-owner Google control (not the Cloudflare ceiling, which
can be better-peered) and a clearly winning bypass/proxy A/B can assert a throttle on its own.
"""
from __future__ import annotations

# Ratio thresholds (calibrated to the real diagnostic: link ~11-13, throttled ~1, bypass ~7-9).
_CLEAN_RATIO = 0.60        # youtube_direct >= this * control  -> keeping up, not throttled
_THROTTLE_RATIO = 0.40     # youtube_direct <  this * control  -> throttle suspected
_CONTROL_TRUST = 0.50      # non-video control >= this * ceiling -> trust it as the anchor
_BYPASS_WIN = 2.5          # (bypass or proxy) / direct above this -> that path clears the throttle
_HEALTHY_ABS_MIBS = 3.5    # link below this reads as a genuinely slow link, not throttling
_THROTTLE_FLOOR = 3.5      # a single YouTube flow below this on a FAST link reads as shaped
_CLEAN_ABS_MIBS = 8.0      # a single YouTube flow at/above this is a healthy speed (not throttled)
_CARRIER_HINTS = ("verizon", "t-mobile", "at&t", "comcast", "charter", "spectrum", "cox",
                  "cellular", "wireless", "mobile", "lte", "5g")
_DC_HINTS = ("amazon", "aws", "google llc", "microsoft", "digitalocean", "ovh", "hetzner",
             "linode", "vultr", "cloudflare", "mullvad", "datacamp", "m247", "leaseweb")


def classify_egress(org: str) -> str:
    """residential | carrier | datacenter (VPN/proxy exit) — best-effort from the ASN org string."""
    o = (org or "").lower()
    if any(h in o for h in _DC_HINTS):
        return "datacenter"
    if any(h in o for h in _CARRIER_HINTS):
        return "carrier"
    return "residential"


def _fmt(mibs) -> str:
    return f"{mibs:.1f} MiB/s" if isinstance(mibs, (int, float)) and mibs > 0 else "—"


def _meas(v):
    """A probe value that was actually measured (positive float), else None."""
    return v if isinstance(v, (int, float)) and v > 0 else None


def pick_control(nonvideo, ceiling):
    """The healthy-SNI reference to judge YouTube against: prefer the same-owner Google control, but
    fall back to the Cloudflare ceiling when the control is missing or itself looks shaped
    (< _CONTROL_TRUST * ceiling). Returns a positive float or None. Shared by analyze() AND the
    /api/speedtest mode-sweep so the two can never disagree about whether the line is throttled."""
    nonvideo, ceiling = _meas(nonvideo), _meas(ceiling)
    if nonvideo and (ceiling is None or nonvideo >= _CONTROL_TRUST * ceiling):
        return nonvideo
    return ceiling or nonvideo


def _rec(rid, severity, icon, title, detail, expected_gain=None, action=None):
    return {"id": rid, "severity": severity, "icon": icon, "title": title, "detail": detail,
            "expected_gain": expected_gain, "action": action or {"kind": "none"}}


def analyze(m: dict) -> dict:
    """m keys (any may be None if a probe was skipped/failed):
        ceiling_mibs, google_nonvideo_mibs, youtube_direct_mibs, youtube_bypass_mibs,
        youtube_proxy_mibs, proxy_label, proxy_url, bypass_mode, egress{org}, desync_enabled,
        engine{js_runtime,po_provider,ffmpeg}, extraction_ok(bool),
        desync_health('off'|'running'|'unreachable'), confidence('high'|'low').
    Returns {verdict{headline,state,recommended}, bars[], recommendations[]}.
    """
    ceiling = _meas(m.get("ceiling_mibs"))
    nonvideo = _meas(m.get("google_nonvideo_mibs"))
    direct = _meas(m.get("youtube_direct_mibs"))
    bypass = _meas(m.get("youtube_bypass_mibs"))
    prox = _meas(m.get("youtube_proxy_mibs"))
    prox_label = m.get("proxy_label") or "your proxy"
    prox_url = m.get("proxy_url") or m.get("proxy_label")
    bypass_mode = m.get("bypass_mode")
    engine = m.get("engine") or {}
    egress_org = (m.get("egress") or {}).get("org") or ""
    egress_type = classify_egress(egress_org)
    on_vpn = egress_type == "datacenter"
    desync_enabled = bool(m.get("desync_enabled"))
    extraction_ok = m.get("extraction_ok", m.get("youtube_direct_mibs") is not None)
    low_conf = m.get("confidence") == "low"
    hedge = " (borderline — re-test to confirm)" if low_conf else ""

    # Sample spread of the direct YouTube flow (min..max across probes). Surfaced so a single median
    # can't mislead when the line is jittery: googlevideo pacing bursts then stalls, so back-to-back
    # runs on the SAME box legitimately read 14 vs 62 MiB/s. Only shown when the samples actually
    # disagreed (low confidence) AND we have a real range — otherwise there's nothing to warn about.
    direct_lo = _meas(m.get("youtube_direct_lo"))
    direct_hi = _meas(m.get("youtube_direct_hi"))
    variability = None
    if direct and direct_lo and direct_hi and direct_hi > direct_lo and low_conf:
        variability = {
            "lo": direct_lo, "hi": direct_hi, "median": direct,
            "note": (f"YouTube's speed is bouncing around this run (~{direct_lo:.0f}–{direct_hi:.0f} "
                     "MiB/s). That's normal CDN pacing, not a fault — read the number as a rough "
                     "middle, and don't compare a single reading against another run or machine."),
        }

    recs: list = []
    bars = []
    if ceiling:
        bars.append({"label": "Your link", "mibs": ceiling, "kind": "ceiling"})
    if direct:
        _db = {"label": "YouTube direct", "mibs": direct, "kind": "direct"}
        if variability:                       # show the spread on the bar only when it's meaningful
            _db["lo"], _db["hi"] = variability["lo"], variability["hi"]
        bars.append(_db)
    if bypass:
        label = f"YouTube + Bypass ({bypass_mode})" if bypass_mode else "YouTube + Bypass"
        bars.append({"label": label, "mibs": bypass, "kind": "bypass"})
    if prox:
        bars.append({"label": f"YouTube via {prox_label}", "mibs": prox, "kind": "proxy"})

    # --- truly no connectivity: only when NOTHING measured anywhere (a single endpoint failing must
    #     not print "no internet") ---
    if not any([ceiling, nonvideo, direct, bypass, prox]):
        return {"verdict": {"headline": "Couldn't reach the internet to run the test.",
                            "state": "no_network", "recommended": None},
                "bars": bars, "variability": variability, "recommendations": [
                    _rec("retry", "high", "⚠", "Check your connection",
                         "None of the test servers responded. Check you're online and try again.",
                         action={"kind": "retest", "label": "Run again"})]}

    # --- P0: JS runtime gate (a missing node/deno pins YouTube speed low regardless of bypass) ---
    if engine.get("js_runtime") is False:
        recs.append(_rec("install_js", "critical", "⛔",
                         "Install a JavaScript runtime (node)",
                         "Without node or deno, YouTube's signature can't be solved and every download "
                         "stays throttled to ~1 MiB/s — fix this first, before anything else.",
                         expected_gain="unlocks the fast path",
                         action={"kind": "install_tool", "label": "Install node",
                                 "endpoint": "/api/updates/tools", "payload": {"tool": "node"}}))

    # --- reference speed: how fast a single flow to a well-peered host goes (proves the LINK is
    #     fast, so we can tell a throttled-YouTube-on-a-fast-link from a genuinely slow link). It is
    #     NOT a fair yardstick for YouTube itself: googlevideo is per-connection-shaped to a moderate
    #     rate on EVERY connection (measured ~14 MiB/s single-flow on a link where GCS/Cloudflare give
    #     58-60), which is why parallel fetching exists. Comparing YouTube's single flow to another
    #     host's single flow therefore cries "throttled" on a perfectly healthy fast connection. ---
    control = pick_control(nonvideo, ceiling)
    link_speed = max([v for v in (ceiling, nonvideo) if v] or [0]) or None

    # best alternative path measured (bypass or a user proxy), and whether it clearly wins
    alt_mibs, alt_kind, alt_label = None, None, None
    for cand, kind, label in ((bypass, "bypass", "the Bypass toggle"), (prox, "proxy", prox_label)):
        if cand and (alt_mibs is None or cand > alt_mibs):
            alt_mibs, alt_kind, alt_label = cand, kind, label
    alt_wins = bool(alt_mibs and direct and alt_mibs > _BYPASS_WIN * direct)

    state = "unknown"
    headline = "Test complete."
    recommended = None

    if not extraction_ok or direct is None:
        # YouTube leg couldn't run (sign-in / bot-check / no PO token / transient fetch error).
        state = "degraded"
        if control:
            headline = f"Couldn't test YouTube itself — but your connection looks healthy (~{_fmt(control)})."
        else:
            headline = "Couldn't complete the YouTube part of the test."
        recs.append(_rec("fix_extract", "medium", "🍪",
                         "YouTube test couldn't run",
                         "Fetching a YouTube video failed (sign-in / bot-check / no PO token). Set up "
                         "cookies in the Auth tab, then re-test.",
                         action={"kind": "open_auth", "label": "Open Auth"}))
    else:
        # Reliable throttle signals (NOT a ratio to the reference — see above):
        #   (a) the bypass/proxy A/B clearly wins  -> a real, defeatable throttle;
        #   (b) YouTube is slow in ABSOLUTE terms AND the link is demonstrably fast -> shaped, since
        #       a single googlevideo flow does not drop near-zero on a healthy link without shaping.
        link_fast = bool(control) and control >= _HEALTHY_ABS_MIBS
        throttled = alt_wins or (bool(direct) and direct < _THROTTLE_FLOOR and link_fast)
        # Clean = not throttled AND YouTube is healthy in absolute terms (a good single-flow rate),
        # or is essentially matching the reference (covers a maxed-out slower link).
        clean = (not throttled and bool(direct)
                 and (direct >= _CLEAN_ABS_MIBS
                      or (bool(control) and direct >= _CLEAN_RATIO * control)))

        if on_vpn:
            # Traffic is tunneled — never blame "your ISP"; talk about the exit instead.
            if clean:
                state = "vpn_ok"
                headline = (f"You're on a VPN/proxy exit and YouTube runs fine through it "
                            f"(~{_fmt(direct)}). The local Bypass isn't needed while the VPN is on.")
            elif control:
                # Only assert the exit is the bottleneck when we have a reference to compare against.
                state = "vpn_slow"
                headline = (f"You're on a VPN/proxy exit and YouTube is ~{_fmt(direct)} through it — "
                            "the exit is likely the bottleneck.")
                recs.insert(0, _rec("vpn_exit", "high", "🛡",
                                    "Try a different exit, or test your real ISP",
                                    "This speed reflects the VPN/proxy exit, not your home line. Switch "
                                    "the VPN to a closer/faster location, or turn it off and re-test to "
                                    "see (and fix) what your actual ISP does.",
                                    action={"kind": "retest", "label": "Re-test"}))
            else:
                state = "vpn_ok"
                headline = (f"You're on a VPN/proxy exit; YouTube runs ~{_fmt(direct)} through it. Turn the "
                            "VPN off and re-test to see your real ISP.")
        elif desync_enabled and alt_kind == "bypass" and alt_wins:
            # Bypass is already ON and doing its job — the un-bypassed "direct" probe is artificial.
            # "you're getting" reflects the mode you actually run (saved_bypass), not the swept best.
            state = "bypass_active"
            recommended = None
            saved_mode = m.get("desync_mode")
            saved_b = _meas(m.get("saved_bypass_mibs")) or alt_mibs
            headline = (f"Your bypass is on and clearing the throttle — YouTube would be ~{_fmt(direct)} "
                        f"without it, you're getting ~{_fmt(saved_b)}. Leave it on.")
            recs.insert(0, _rec("bypass_active", "info", "✅",
                                "Bypass is working — nothing to do",
                                f"With the local bypass, YouTube runs ~{_fmt(saved_b)} instead of the "
                                f"~{_fmt(direct)} your ISP would otherwise throttle it to.",
                                action={"kind": "guidance", "label": "OK"}))
            # A different swept mode measured meaningfully faster than the one you're running — offer it.
            if (bypass_mode and saved_mode and bypass_mode != saved_mode
                    and alt_mibs > 1.25 * saved_b):
                recommended = "switch_mode"
                recs.insert(0, _rec("switch_mode", "medium", "⚡",
                                    f"A faster bypass mode ({bypass_mode}) is available",
                                    f"You're on “{saved_mode}” (~{_fmt(saved_b)}); “{bypass_mode}” measured "
                                    f"~{_fmt(alt_mibs)} here. Switch to it?",
                                    expected_gain=f"~{_fmt(alt_mibs)}",
                                    action={"kind": "enable_bypass", "label": f"Switch to {bypass_mode}",
                                            "endpoint": "/api/settings",
                                            "payload": {"desync": True, "desync_mode": bypass_mode}}))
        elif throttled and alt_wins:
            state = "throttled_fixable"
            headline = (f"Your ISP is throttling YouTube (~{_fmt(direct)}) — but {alt_label} fixes it "
                        f"(~{_fmt(alt_mibs)}){hedge}.")
            if alt_kind == "bypass":
                recommended = "enable_bypass"
                payload = {"desync": True}
                if bypass_mode:
                    payload["desync_mode"] = bypass_mode
                mode_note = f" (mode: {bypass_mode})" if bypass_mode else ""
                recs.insert(0, _rec("enable_bypass", "high", "⚡",
                                    "Turn on “Bypass ISP throttling”",
                                    f"Your ISP inspects the video hostname to throttle YouTube. The local "
                                    f"bypass{mode_note} hides it — no VPN needed. Measured: {_fmt(direct)} "
                                    f"→ {_fmt(alt_mibs)}.",
                                    expected_gain=f"~{_fmt(alt_mibs)}",
                                    action={"kind": "enable_bypass", "label": "Enable Bypass",
                                            "endpoint": "/api/settings", "payload": payload}))
            else:  # a user-supplied proxy won — save THAT proxy and turn the bypass off so it applies
                recommended = "use_proxy"
                recs.insert(0, _rec("use_proxy", "high", "⚡",
                                    f"Use {alt_label} for downloads",
                                    f"{alt_label} cleared the throttle: {_fmt(direct)} → {_fmt(alt_mibs)}. "
                                    "Save it as your download proxy.",
                                    expected_gain=f"~{_fmt(alt_mibs)}",
                                    action={"kind": "set_proxy", "label": "Save this proxy",
                                            "endpoint": "/api/settings",
                                            "payload": {"enabled": True, "proxy": prox_url,
                                                        "desync": False}}))
        elif throttled:
            state = "throttled_hard"
            recommended = "try_proxy"
            if bypass is None:
                # The bypass never actually ran (helper couldn't start) — don't claim it "didn't clear it".
                headline = (f"YouTube is throttled (~{_fmt(direct)}) and the local bypass couldn't be "
                            f"tested here{hedge}.")
                detail = ("The local bypass didn't run — its helper may not have started (see Settings). "
                          "Enable it and re-test, or try a remote proxy / VPN.")
            else:
                headline = f"YouTube is slow (~{_fmt(direct)}) and the local bypass didn't clear it{hedge}."
                detail = ("The local bypass mode tested didn't beat the shaping here. A remote proxy "
                          "(SOCKS5) or a VPN on a good exit likely will — add one under “Also test a "
                          "proxy or VPN” and re-run.")
            recs.insert(0, _rec("try_proxy", "high", "🛡", "Try a remote proxy or a VPN", detail,
                                action={"kind": "test_proxy", "label": "Test a proxy / VPN"}))
        elif control is None:
            # No reference server measured — we can report YouTube's number but can't say if it's
            # throttled or the link is just slow, so never assert either (avoids a "~—" comparison).
            state = "measured_direct_only"
            headline = (f"YouTube runs at ~{_fmt(direct)}. Couldn't measure a reference server this run, "
                        "so I can't tell throttling from a slow link — re-test to compare.")
            recs.append(_rec("retest_ref", "low", "🔁", "Re-test for a full comparison",
                             "The link/reference probes didn't complete this run.",
                             action={"kind": "retest", "label": "Re-test"}))
        elif link_speed and link_speed < _HEALTHY_ABS_MIBS:
            # A genuinely slow link is not throttling — check this BEFORE "clean" so a 1.6-on-a-2.0
            # line isn't told "clean, no fix needed" when the real problem is the link itself.
            state = "slow_link"
            headline = f"Your connection itself is slow (~{_fmt(link_speed)}) — this isn't YouTube throttling."
            recs.append(_rec("slow_link", "medium", "📶",
                             "Improve the connection, not the app",
                             "Both YouTube and other Google servers are slow, so it's the link, not "
                             "throttling. On 5G home internet, window placement / an external antenna / a "
                             "better band can help a lot.",
                             action={"kind": "guidance", "label": "OK"}))
        elif clean:
            state = "clean"
            headline = f"Your connection is clean — YouTube runs at ~{_fmt(direct)}. No fix needed."
            if desync_enabled:
                recs.append(_rec("bypass_redundant", "info", "ℹ",
                                 "Bypass is on but not needed here",
                                 "Your line isn't being throttled, so the local bypass isn't doing "
                                 "anything. It's harmless to leave on, or you can turn it off in Settings.",
                                 action={"kind": "guidance", "label": "OK"}))
        else:
            # Middle band: below the link's capacity but not hard-throttled.
            state = "partial"
            headline = (f"YouTube (~{_fmt(direct)}) is running below your link's capacity (~{_fmt(control)}), "
                        "but it's not a hard throttle.")
            if alt_kind == "bypass" and not alt_wins:
                recs.insert(0, _rec("partial_proxy", "medium", "🛡",
                                    "Try a remote proxy or a VPN",
                                    "The local bypass helped only a little. A remote proxy or VPN on a good "
                                    "exit may do better — add one under “Also test a proxy or VPN”.",
                                    action={"kind": "test_proxy", "label": "Test a proxy / VPN"}))
            else:
                recs.insert(0, _rec("partial_bypass", "medium", "⚡",
                                    "Try turning on the Bypass and re-testing",
                                    "YouTube is under-performing your link. The local bypass may clear it — "
                                    "enable it and re-run this test to compare.",
                                    action={"kind": "enable_bypass", "label": "Enable Bypass & re-test",
                                            "endpoint": "/api/settings", "payload": {"desync": True}}))

    # --- P2: PO-token provider (optional speed helper) — only when not already fast/clean ---
    if engine.get("po_provider") in ("down", "not_running", None) and state not in (
            "clean", "vpn_ok", "bypass_active", "no_network"):
        recs.append(_rec("po_provider", "low", "🔑",
                         "Optional: start the PO-token helper",
                         "The bgutil PO-token provider can unlock an additional YouTube client path. "
                         "It is optional and may not change the bottleneck measured above.",
                         action={"kind": "install_tool", "label": "Start it",
                                 "endpoint": "/api/updates/tools", "payload": {"tool": "bgutil-server"}}))

    # The bypass is enabled but its child can't run — surface it.
    if m.get("desync_health") == "unreachable":
        recs.insert(0, _rec("bypass_broken", "high", "⚠",
                            "The bypass helper isn't running",
                            "Bypass is enabled but its local proxy couldn't start (port in use?). Toggle it "
                            "off and on in Settings, or restart the app.",
                            action={"kind": "guidance", "label": "OK"}))

    # de-dupe by id, stable order (criticals already inserted at front)
    seen, ordered = set(), []
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    for r in sorted(recs, key=lambda r: sev_rank.get(r["severity"], 5)):
        if r["id"] not in seen:
            seen.add(r["id"]); ordered.append(r)

    return {"verdict": {"headline": headline, "state": state, "recommended": recommended},
            "bars": bars, "variability": variability, "recommendations": ordered}
