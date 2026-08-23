"""Unit tests for the pure Speed Test decision engine (no network)."""
import speedtest as st


def _ids(res):
    return [r["id"] for r in res["recommendations"]]


def _rec(res, rid):
    return next(r for r in res["recommendations"] if r["id"] == rid)


def test_classify_egress():
    assert st.classify_egress("AS6167 Verizon Business") == "carrier"
    assert st.classify_egress("Amazon.com, Inc.") == "datacenter"
    assert st.classify_egress("Mullvad VPN") == "datacenter"
    assert st.classify_egress("Comcast Cable") == "carrier"
    assert st.classify_egress("Andres Home Fiber Co") == "residential"


def test_pick_control_trust_rule():
    assert st.pick_control(12.0, 13.0) == 12.0      # trustworthy same-owner control
    assert st.pick_control(2.0, 13.0) == 13.0       # poisoned control (< half ceiling) -> ceiling
    assert st.pick_control(None, 13.0) == 13.0
    assert st.pick_control(12.0, None) == 12.0
    assert st.pick_control(None, None) is None
    assert st.pick_control(0.0, 0.0) is None


def test_no_network_only_when_everything_failed():
    r = st.analyze({"ceiling_mibs": None, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": None})
    assert r["verdict"]["state"] == "no_network"
    assert "retry" in _ids(r)


def test_ceiling_failure_alone_is_not_no_network():
    # BUG C2: one Cloudflare hiccup must NOT print "No internet"; the Google control carries the test.
    r = st.analyze({"ceiling_mibs": None, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 8.0,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_fixable"


def test_clean_connection_no_bypass_card():
    r = st.analyze({"ceiling_mibs": 100, "google_nonvideo_mibs": 95,
                    "youtube_direct_mibs": 88, "engine": {"js_runtime": True},
                    "egress": {"org": "Home Fiber"}})
    assert r["verdict"]["state"] == "clean"
    assert "enable_bypass" not in _ids(r)


def test_sni_throttle_bypass_fixes_it():
    r = st.analyze({"ceiling_mibs": 13.2, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.2, "youtube_bypass_mibs": 8.1,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_fixable"
    assert r["verdict"]["recommended"] == "enable_bypass"
    assert _rec(r, "enable_bypass")["action"]["payload"] == {"desync": True}
    assert any(b["kind"] == "bypass" and b["mibs"] == 8.1 for b in r["bars"])


def test_sni_throttle_bypass_does_not_help():
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 1.1,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_hard"
    assert r["verdict"]["recommended"] == "try_proxy"


def test_po_provider_guidance_does_not_claim_a_failed_bypass_helped():
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 1.0,
                    "engine": {"js_runtime": True, "po_provider": "down"},
                    "egress": {"org": "Verizon"}})

    assert r["verdict"]["state"] == "throttled_hard"
    detail = _rec(r, "po_provider")["detail"].lower()
    assert "bypass already gets you most" not in detail
    assert "may not change the bottleneck measured above" in detail


def test_analyze_surfaces_direct_speed_range_when_variable():
    # A jittery line (samples 14..62, median 20) with low confidence must SHOW the spread and a
    # plain-language note, and carry lo/hi on the direct bar — so a single median can't mislead
    # (the Mac-25-vs-Windows-17 confusion). state stays 'clean' (fast, not throttled).
    r = st.analyze({"ceiling_mibs": 110, "google_nonvideo_mibs": 100,
                    "youtube_direct_mibs": 20.0, "youtube_direct_lo": 14.0, "youtube_direct_hi": 62.0,
                    "confidence": "low", "engine": {"js_runtime": True},
                    "egress": {"org": "Home Fiber"}})
    var = r["variability"]
    assert var is not None
    assert var["lo"] == 14.0 and var["hi"] == 62.0 and var["median"] == 20.0
    assert "14" in var["note"] and "62" in var["note"]
    assert any(w in var["note"].lower() for w in ("vary", "swing", "bounc", "normal"))
    b = next(b for b in r["bars"] if b["kind"] == "direct")
    assert b["lo"] == 14.0 and b["hi"] == 62.0


def test_analyze_no_variability_block_when_stable():
    # High confidence + tight spread -> no variance note (nothing to warn about), no lo/hi on the bar.
    r = st.analyze({"ceiling_mibs": 100, "google_nonvideo_mibs": 95,
                    "youtube_direct_mibs": 88.0, "youtube_direct_lo": 86.0, "youtube_direct_hi": 90.0,
                    "confidence": "high", "engine": {"js_runtime": True},
                    "egress": {"org": "Home Fiber"}})
    assert r.get("variability") is None
    b = next(b for b in r["bars"] if b["kind"] == "direct")
    assert "lo" not in b and "hi" not in b


def test_analyze_no_variability_block_without_range_samples():
    # Low confidence but only a single sample (lo==hi absent) -> nothing to show as a range.
    r = st.analyze({"ceiling_mibs": 110, "google_nonvideo_mibs": 100, "youtube_direct_mibs": 20.0,
                    "confidence": "low", "engine": {"js_runtime": True},
                    "egress": {"org": "Home Fiber"}})
    assert r.get("variability") is None


def test_user_proxy_wins_saves_proxy_and_clears_desync():
    # BUG #3: saving the winning proxy must also turn the bypass OFF (desync precedence would shadow it)
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 1.1,
                    "youtube_proxy_mibs": 9.0, "proxy_label": "socks5://10.0.0.9:1080",
                    "proxy_url": "socks5://10.0.0.9:1080", "desync_enabled": True,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["recommended"] == "use_proxy"
    pl = _rec(r, "use_proxy")["action"]["payload"]
    assert pl == {"enabled": True, "proxy": "socks5://10.0.0.9:1080", "desync": False}


def test_missing_js_runtime_is_top_priority():
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 8.0,
                    "engine": {"js_runtime": False}, "egress": {"org": "Verizon"}})
    assert r["recommendations"][0]["id"] == "install_js"
    assert r["recommendations"][0]["severity"] == "critical"


def test_slow_link_not_throttling():
    r = st.analyze({"ceiling_mibs": 2.0, "google_nonvideo_mibs": 1.8,
                    "youtube_direct_mibs": 1.6, "engine": {"js_runtime": True},
                    "egress": {"org": "Rural WISP"}})
    assert r["verdict"]["state"] == "slow_link"


def test_middle_band_is_partial_not_slow_link():
    # BUG #2: direct in the band between throttle and clean, on a fast link, must NOT be "slow_link"
    # with a self-contradicting "~13 slow" headline — it's a distinct "partial" state.
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 6.0, "youtube_bypass_mibs": 6.2,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "partial"
    assert "slow" not in r["verdict"]["headline"].lower() or "capacity" in r["verdict"]["headline"].lower()


def test_winning_bypass_asserts_throttle_even_if_control_ratio_borderline():
    # BUG #2: a clearly winning bypass A/B (6x) asserts a throttle on its own.
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 8.0,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_fixable"


def test_bypass_already_on_says_leave_it_not_enable():
    # BUG #8: when desync is already enabled and winning, don't emit a no-op "Enable Bypass".
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.2, "youtube_bypass_mibs": 8.0,
                    "desync_enabled": True, "engine": {"js_runtime": True},
                    "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "bypass_active"
    assert "enable_bypass" not in _ids(r)
    assert "bypass_active" in _ids(r)


def test_bypass_active_offers_faster_mode():
    # Already on oob (~2.0); the sweep found frag (~8.0) is much better -> offer to switch, and the
    # headline reflects what oob actually yields (2.0), not the swept best.
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.1, "youtube_bypass_mibs": 8.0,
                    "saved_bypass_mibs": 2.0, "desync_mode": "oob", "bypass_mode": "frag",
                    "desync_enabled": True, "engine": {"js_runtime": True},
                    "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "bypass_active"
    assert r["verdict"]["recommended"] == "switch_mode"
    assert _rec(r, "switch_mode")["action"]["payload"] == {"desync": True, "desync_mode": "frag"}
    assert "2.0" in r["verdict"]["headline"] and "8.0" not in r["verdict"]["headline"]


def test_bypass_active_no_nudge_when_running_mode_is_best():
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.1, "youtube_bypass_mibs": 8.0,
                    "saved_bypass_mibs": 8.0, "desync_mode": "oob", "bypass_mode": "oob",
                    "desync_enabled": True, "engine": {"js_runtime": True},
                    "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "bypass_active"
    assert "switch_mode" not in _ids(r)


def test_vpn_egress_never_blames_isp():
    # BUG C3: on a datacenter/VPN exit, don't say "your ISP is throttling" or emit enable_bypass.
    r = st.analyze({"ceiling_mibs": 7.0, "google_nonvideo_mibs": 7.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 1.1,
                    "engine": {"js_runtime": True}, "egress": {"org": "Amazon.com, Inc."}})
    assert r["verdict"]["state"] in ("vpn_ok", "vpn_slow")
    assert "enable_bypass" not in _ids(r)
    assert "isp" not in r["verdict"]["headline"].lower()


def test_vpn_egress_fast_is_ok():
    r = st.analyze({"ceiling_mibs": 20.0, "google_nonvideo_mibs": 19.0,
                    "youtube_direct_mibs": 18.0, "engine": {"js_runtime": True},
                    "egress": {"org": "Mullvad VPN"}})
    assert r["verdict"]["state"] == "vpn_ok"


def test_failed_direct_fetch_degrades_not_throttled():
    # BUG #1: a failed YouTube fetch is None (not 0.0) and must route to 'degraded', never 'throttled'.
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": None, "youtube_bypass_mibs": 8.0,
                    "extraction_ok": True, "engine": {"js_runtime": True},
                    "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "degraded"
    assert "enable_bypass" not in _ids(r)
    assert "fix_extract" in _ids(r)
    assert _rec(r, "fix_extract")["action"]["kind"] == "open_auth"


def test_extraction_failed_degrades_gracefully():
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": None, "extraction_ok": False,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "degraded"
    assert "fix_extract" in _ids(r)


def test_throttle_detected_without_nonvideo_control():
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 8.0,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_fixable"
    assert r["verdict"]["recommended"] == "enable_bypass"


def test_desync_unreachable_surfaced():
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "desync_health": "unreachable",
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert "bypass_broken" in _ids(r)


def test_bypass_mode_carried_into_enable_payload_and_bar():
    r = st.analyze({"ceiling_mibs": 13.2, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.2, "youtube_bypass_mibs": 8.1,
                    "bypass_mode": "frag", "engine": {"js_runtime": True},
                    "egress": {"org": "Verizon"}})
    assert _rec(r, "enable_bypass")["action"]["payload"] == {"desync": True, "desync_mode": "frag"}
    assert any("frag" in b["label"] for b in r["bars"] if b["kind"] == "bypass")


def test_fast_link_single_flow_youtube_is_not_throttled_via_ceiling_fallback():
    # The real Comcast case: link (Cloudflare) 60, a single YouTube flow 14 (healthy), the same-owner
    # control is down (None), bypass barely differs. This USED to read 'throttled_hard, try a proxy'
    # because 14 < 0.4*60 — but 14 MiB/s single-flow is fine; it's single-flow-limited, not throttled.
    r = st.analyze({"ceiling_mibs": 60.0, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": 14.3, "youtube_bypass_mibs": 17.0,
                    "engine": {"js_runtime": True}, "egress": {"org": "Comcast"}})
    assert r["verdict"]["state"] == "clean"
    assert "throttl" not in r["verdict"]["headline"].lower()


def test_fast_link_with_working_same_owner_control_is_clean():
    # Same connection, control now up (~15, a single Google flow ≈ the single YouTube flow) -> clean.
    r = st.analyze({"ceiling_mibs": 60.0, "google_nonvideo_mibs": 15.0,
                    "youtube_direct_mibs": 14.3, "engine": {"js_runtime": True},
                    "egress": {"org": "Comcast"}})
    assert r["verdict"]["state"] == "clean"


def test_ceiling_fallback_still_catches_a_real_throttle_via_winning_bypass():
    # Verizon with the control down: ceiling 13, direct 1.2, bypass 8 (6.7x). alt_wins carries it.
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": 1.2, "youtube_bypass_mibs": 8.0,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_fixable"


def test_ceiling_fallback_catches_hard_throttle_via_low_absolute_direct():
    # Verizon, control down, bypass didn't help, but direct is slow in ABSOLUTE terms -> throttled.
    r = st.analyze({"ceiling_mibs": 13.0, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": 1.1,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_hard"


def test_throttled_hard_does_not_claim_bypass_ran_when_it_didnt():
    # bypass=None (helper never started) -> don't say "the bypass didn't clear it".
    r = st.analyze({"ceiling_mibs": 11.5, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.0, "youtube_bypass_mibs": None,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "throttled_hard"
    assert "didn't clear it" not in r["verdict"]["headline"]
    assert "couldn't be tested" in r["verdict"]["headline"]


def test_control_none_gives_measured_direct_only_not_dash_partial():
    # both link probes failed, YouTube measured, not throttled by a winning alt -> no "~—" partial.
    r = st.analyze({"ceiling_mibs": None, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": 5.0, "youtube_bypass_mibs": 5.2,
                    "engine": {"js_runtime": True}, "egress": {"org": "Verizon"}})
    assert r["verdict"]["state"] == "measured_direct_only"
    assert "~—" not in r["verdict"]["headline"]        # the _fmt(None) bug pattern


def test_vpn_slow_without_control_stays_neutral():
    r = st.analyze({"ceiling_mibs": None, "google_nonvideo_mibs": None,
                    "youtube_direct_mibs": 3.0, "engine": {"js_runtime": True},
                    "egress": {"org": "Amazon.com, Inc."}})
    assert r["verdict"]["state"] == "vpn_ok"          # neutral, not a bottleneck assertion
    assert "bottleneck" not in r["verdict"]["headline"]


def test_low_confidence_hedges_headline():
    r = st.analyze({"ceiling_mibs": 13.2, "google_nonvideo_mibs": 12.0,
                    "youtube_direct_mibs": 1.2, "youtube_bypass_mibs": 8.1,
                    "confidence": "low", "engine": {"js_runtime": True},
                    "egress": {"org": "Verizon"}})
    assert "re-test" in r["verdict"]["headline"].lower()
