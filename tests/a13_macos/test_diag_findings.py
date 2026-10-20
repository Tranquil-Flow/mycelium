# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: automatic findings, in plain English, each with a fix. Pure function of the diagnostics document."""
from __future__ import annotations

from mycelium_a13_macos.diagnostics.findings import derive_findings

SNTP = "sudo sntp -sS time.apple.com"


def peer(**kw):
    base = {"node_id": "nodeaaaa1111", "name": "The joined Mac",
            "clock": {"available": False, "reason": "no signed message from this peer yet"},
            "liveness": {"available": False, "reason": "no route is open"},
            "membership": {"liveness_status": "idle_fresh"},
            "mailbox": {"available": True, "counts": {}, "total": 0,
                        "short_ttl": {"total": 0, "completed": 0, "expired": 0, "reconciliation_required": 0},
                        "median_claim_latency_s": None}}
    base.update(kw)
    return base


def diag(**kw):
    base = {"role": "owner", "peers": [], "chats": {"recent": [], "error_codes": {}},
            "relay": {"state": "relay_process_running", "provider": "cloudflare", "expires_in_seconds": 1500.0},
            "host": {}, "member_view": None,
            "run": {"state": "serving"}}
    base.update(kw)
    return base


def ids(findings):
    return [f["id"] for f in findings]


def by_id(findings, ident):
    return next(f for f in findings if f["id"] == ident)


def test_a_healthy_swarm_has_no_findings():
    clock = {"available": True, "offset_s": 0.04, "uncertainty_s": 0.05, "direction": "in_sync", "samples": 30,
             "rtt_corrected": True}
    assert derive_findings(diag(peers=[peer(clock=clock)])) == []


def test_clock_9_seconds_ahead_names_the_peer_and_the_fix():
    clock = {"available": True, "offset_s": 9.19, "uncertainty_s": 0.2, "direction": "ahead", "samples": 12,
             "rtt_corrected": True}
    found = derive_findings(diag(peers=[peer(clock=clock)]))
    item = by_id(found, "peer_clock_skew")
    assert item["severity"] == "critical" and item["peer"] == "nodeaaaa1111"
    assert "The joined Mac's clock is 9.2 s ahead" in item["title"]
    assert SNTP in item["fix"] and "both Macs" in item["fix"]
    assert "expire" in item["detail"]  # says WHY it matters: short-lived commands run out of time


def test_a_behind_clock_and_a_small_skew():
    behind = {"available": True, "offset_s": -5.0, "uncertainty_s": 0.1, "direction": "behind", "samples": 9,
              "rtt_corrected": True}
    assert "5.0 s behind" in by_id(derive_findings(diag(peers=[peer(clock=behind)])), "peer_clock_skew")["title"]
    mild = {"available": True, "offset_s": 1.6, "uncertainty_s": 0.1, "direction": "ahead", "samples": 9,
            "rtt_corrected": True}
    item = by_id(derive_findings(diag(peers=[peer(clock=mild)])), "peer_clock_skew")
    assert item["severity"] == "warning"


def test_a_skew_inside_its_own_uncertainty_is_not_reported():
    noisy = {"available": True, "offset_s": 1.2, "uncertainty_s": 1.5, "direction": "ahead", "samples": 2,
             "rtt_corrected": True}
    assert "peer_clock_skew" not in ids(derive_findings(diag(peers=[peer(clock=noisy)])))


def test_most_commands_expiring_before_they_are_claimed():
    counts = {"expired": 18, "completed": 2, "reconciliation_required": 0}
    mailbox = {"available": True, "counts": counts, "total": 20, "median_claim_latency_s": 0.4,
               "short_ttl": {"total": 20, "completed": 0, "expired": 18, "reconciliation_required": 0}}
    item = by_id(derive_findings(diag(peers=[peer(mailbox=mailbox)])), "peer_commands_expire")
    assert "Most commands to The joined Mac expire before it claims them" in item["title"]
    assert "18 of 20" in item["detail"]
    assert item["fix"]


def test_commands_claimed_but_never_confirmed():
    counts = {"expired": 0, "completed": 3, "reconciliation_required": 17}
    mailbox = {"available": True, "counts": counts, "total": 20, "median_claim_latency_s": 0.4,
               "short_ttl": {"total": 20, "completed": 0, "expired": 0, "reconciliation_required": 17}}
    found = derive_findings(diag(peers=[peer(mailbox=mailbox)]))
    assert "peer_commands_unconfirmed" in ids(found)


def test_a_handful_of_commands_is_not_a_trend():
    mailbox = {"available": True, "counts": {"expired": 3}, "total": 3, "median_claim_latency_s": None,
               "short_ttl": {"total": 3, "completed": 0, "expired": 3, "reconciliation_required": 0}}
    assert derive_findings(diag(peers=[peer(mailbox=mailbox)])) == []


def test_slow_claims():
    mailbox = {"available": True, "counts": {"completed": 12}, "total": 12, "median_claim_latency_s": 14.0,
               "short_ttl": {"total": 0, "completed": 0, "expired": 0, "reconciliation_required": 0}}
    item = by_id(derive_findings(diag(peers=[peer(mailbox=mailbox)])), "peer_slow_claims")
    assert "14.0 s" in item["title"]


def test_quarantined_peer_explains_why_chats_are_refused():
    live = {"available": True, "label": "quarantined", "state": "quarantined", "last_seen_seconds_ago": 95.0,
            "consecutive_misses": 6, "recovery_observations": 0}
    chats = {"recent": [{"terminal": "failed", "error_code": "backend_failed", "age_s": 5.0}] * 3,
             "error_codes": {"backend_failed": 3}}
    found = derive_findings(diag(peers=[peer(liveness=live)], chats=chats))
    item = by_id(found, "peer_quarantined")
    assert item["severity"] == "critical"
    assert "Chats are refused because The joined Mac is quarantined" in item["title"]
    assert "95" in item["detail"] and item["fix"]
    generic = by_id(found, "chats_generic_failure")
    assert "backend_failed" in generic["title"]


def test_a_router_code_naming_quarantine_is_explained_even_without_liveness():
    chats = {"recent": [{"terminal": "failed", "error_code": "affected_peer_quarantined", "age_s": 5.0}],
             "error_codes": {"affected_peer_quarantined": 1}}
    found = derive_findings(diag(peers=[peer()], chats=chats))
    assert "Chats are refused because a joined Mac is quarantined" in by_id(found, "chats_refused_quarantine")["title"]


def test_stale_peer_is_a_warning():
    live = {"available": True, "label": "stale", "state": "suspect", "last_seen_seconds_ago": 12.0,
            "consecutive_misses": 2, "recovery_observations": 0}
    item = by_id(derive_findings(diag(peers=[peer(liveness=live)])), "peer_stale")
    assert item["severity"] == "warning"


def test_membership_says_the_peer_is_dead():
    item = by_id(derive_findings(diag(peers=[peer(membership={"liveness_status": "dead"})])), "peer_not_heard_from")
    assert item["severity"] == "critical"


def test_relay_problems():
    gone = diag(peers=[peer()], relay={"state": "stopped", "provider": "cloudflare", "error_code": None})
    assert "relay_down" in ids(derive_findings(gone))
    failed = diag(peers=[peer()], relay={"state": "stopped", "provider": "cloudflare", "error_code": "relay_origin_changed"})
    assert "relay_origin_changed" in by_id(derive_findings(failed), "relay_down")["detail"]
    expiring = diag(peers=[peer()], relay={"state": "relay_process_running", "provider": "cloudflare",
                                           "expires_in_seconds": 120.0})
    item = by_id(derive_findings(expiring), "relay_expiring")
    assert "2 min" in item["title"]
    fine = diag(peers=[peer()], relay={"state": "relay_process_running", "provider": "cloudflare",
                                       "expires_in_seconds": 1500.0})
    assert derive_findings(fine) == []


def test_no_relay_finding_when_nobody_has_joined():
    assert derive_findings(diag(peers=[], relay={"state": "stopped", "provider": None})) == []


def test_this_macs_own_node_does_not_need_a_relay():
    own = peer(is_this_mac=True, name="This Mac")
    assert derive_findings(diag(peers=[own], relay={"state": "private", "provider": None})) == []


def test_host_pressure():
    host = {"memory_pressure": "critical", "spotlight_mds_cpu_percent": 85.0, "disk_free_bytes": 2_000_000_000,
            "load_average": [20.0, 10.0, 8.0], "cpu_count": 8}
    found = derive_findings(diag(host=host))
    assert {"host_memory_pressure", "host_spotlight_busy", "host_disk_low", "host_load_high"} <= set(ids(found))
    assert "mds" in by_id(found, "host_spotlight_busy")["title"]
    quiet = {"memory_pressure": "normal", "spotlight_mds_cpu_percent": 1.0, "disk_free_bytes": 200_000_000_000,
             "load_average": [1.0, 1.0, 1.0], "cpu_count": 8}
    assert derive_findings(diag(host=quiet)) == []


def test_unreadable_host_values_never_produce_findings():
    host = {"memory_pressure": "unknown", "spotlight_mds_cpu_percent": None, "disk_free_bytes": None,
            "load_average": None, "cpu_count": None}
    assert derive_findings(diag(host=host)) == []


def test_member_view_owner_clock_and_slow_polls():
    view = {"owner_clock": {"available": True, "offset_s": -9.19, "uncertainty_s": 0.2, "direction": "behind",
                            "samples": 10, "rtt_corrected": True},
            "claim_loop": {"polls": 40, "errors": {"managed_control_unreachable": 9}, "median_poll_seconds": 4.2,
                           "median_ack_seconds": 0.5, "last_command": {"ttl_seconds": 0.4}}}
    found = derive_findings(diag(role="target", member_view=view))
    assert "The owner's clock is 9.2 s behind this Mac" in by_id(found, "owner_clock_skew")["title"]
    assert SNTP in by_id(found, "owner_clock_skew")["fix"]
    assert "claim_loop_errors" in ids(found)
    assert "claim_loop_slow" in ids(found)


def test_critical_findings_come_first():
    live = {"available": True, "label": "stale", "state": "suspect", "last_seen_seconds_ago": 12.0,
            "consecutive_misses": 2, "recovery_observations": 0}
    clock = {"available": True, "offset_s": 9.19, "uncertainty_s": 0.2, "direction": "ahead", "samples": 12,
             "rtt_corrected": True}
    found = derive_findings(diag(peers=[peer(liveness=live, clock=clock)]))
    severities = [f["severity"] for f in found]
    assert severities == sorted(severities, key={"critical": 0, "warning": 1, "info": 2}.get)
