# SPDX-License-Identifier: AGPL-3.0-only
"""Owner pairing window: single-use, expiring, revocable, code-locked.

Pure state-machine tests with a controlled clock; no sockets.  The relay-path
integration lives in ``test_join_window_flow.py``.
"""
from __future__ import annotations

import copy
import json

import pytest

from mycelium_a13_macos.capability import signed_capability_link_report
from mycelium_a13_macos.pairing import (
    JOIN_LINK_SCHEME,
    PairingError,
    PairingWindows,
    identity_fingerprint,
    new_pairing_request,
    parse_join_text,
    request_digest,
)
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
ORIGIN = "https://quiet-river-apple-stone.trycloudflare.com"
SEED_PIN = "sha256:" + "ab" * 32
QUOTA = {"storage_bytes": 2_000_000_000, "network_bytes_per_day": 2_000_000_000,
         "background_allowed": False}
DEVICE = {"chip": "Apple M3 Pro", "memory_bytes": 36 * 1024**3,
          "available_storage_bytes": 200 * 1024**3, "os_version": "26.5.1",
          "mlx": {"available": True, "device": "Device(gpu, 0)"},
          "power": "external", "thermal": "nominal", "network": "unmetered"}
LINK = {"path_kind": "public_https_relay", "rounds": 3, "rtt_ms_median": 41.0,
        "rtt_ms_samples": [40.0, 41.0, 44.0], "upload_bytes": 262144,
        "upload_bytes_per_second": 2_000_000.0, "download_bytes": 262144,
        "download_bytes_per_second": 4_000_000.0}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def _target(name="target-endpoint", node="remote-mac-aaaa", now=NOW, ttl=300):
    signer = generate_ed25519_signer(endpoint_id=name)
    request, _private = new_pairing_request(signer=signer, node_id=node,
        peer_class="mac_mlx_iroh", now=now, ttl_seconds=ttl)
    report = signed_capability_link_report(signer=signer, node_id=node,
        purpose="join", generation=None, device=DEVICE, link=LINK, now=now)
    return signer, request, report


def _submission(code, request, report, quota=QUOTA):
    return {"protocol": "mycelium.a13_pairing_submission.v1", "code": code,
            "request": request, "report": report, "quota": dict(quota)}


def _open(clock, ttl=600):
    windows = PairingWindows(clock=clock)
    opened = windows.open(origin=ORIGIN, seed_key_digest=SEED_PIN, ttl_seconds=ttl)
    return windows, opened


def test_open_window_publishes_link_and_short_code_that_round_trip():
    clock = Clock()
    _windows, opened = _open(clock)
    assert opened["state"] == "open" and opened["expires_at"] == NOW + 600
    assert opened["join_link"].startswith(JOIN_LINK_SCHEME)
    full = parse_join_text(opened["join_link"], now=NOW)
    assert full == {"origin": ORIGIN, "window_id": opened["window_id"],
                    "code": full["code"], "seed_key_digest": SEED_PIN,
                    "expires_at": NOW + 600}
    # Short typed form: tunnel name + grouped code; no pin, same window.
    short = parse_join_text(f"quiet-river-apple-stone {opened['short_code'].lower()}", now=NOW)
    assert short == {**full, "seed_key_digest": None, "expires_at": None}
    assert len(opened["short_code"].replace("-", "")) == 12
    with pytest.raises(PairingError, match="join_link_expired"):
        parse_join_text(opened["join_link"], now=NOW + 601)
    for bad in ("", "mycelium-join:!!", "http://x.trycloudflare.com ABCD-EFGH-JKLM",
                "example.com ABCD-EFGH-JKLM", "quiet-river ABCD-EFGH"):
        with pytest.raises(PairingError):
            parse_join_text(bad, now=NOW)
    with pytest.raises(PairingError, match="pairing_window_ttl_invalid"):
        PairingWindows(clock=clock).open(origin=ORIGIN, seed_key_digest=SEED_PIN, ttl_seconds=5)
    with pytest.raises(PairingError, match="pairing_window_origin_invalid"):
        PairingWindows(clock=clock).open(origin="http://127.0.0.1:1", seed_key_digest=SEED_PIN,
                                         ttl_seconds=60)


def test_submission_is_verified_and_owner_sees_device_summary_before_approval():
    clock = Clock()
    windows, opened = _open(clock)
    code = parse_join_text(opened["join_link"], now=NOW)["code"]
    signer, request, report = _target()
    result = windows.submit(opened["window_id"], _submission(code, request, report))
    assert result == {"state": "waiting_for_owner",
                      "fingerprint": identity_fingerprint(request["identity_key_digest"]),
                      "request_digest": request_digest(request)}
    [listed] = windows.list()
    assert listed["state"] == "claimed"
    assert listed["pending"]["node_id"] == request["node_id"]
    assert listed["pending"]["fingerprint"] == result["fingerprint"]
    assert listed["pending"]["device"] == DEVICE and listed["pending"]["link"] == LINK
    assert listed["pending"]["quota"] == QUOTA
    assert "code" not in json.dumps(listed["pending"])
    # A report signed by a different identity is refused before claiming.
    windows2, opened2 = _open(clock)
    code2 = parse_join_text(opened2["join_link"], now=NOW)["code"]
    _other, _req, foreign_report = _target(name="other-endpoint", node="remote-mac-aaaa")
    with pytest.raises(PairingError, match="capability_report_signature_invalid"):
        windows2.submit(opened2["window_id"], _submission(code2, request, foreign_report))
    tampered = copy.deepcopy(request)
    tampered["node_id"] = "remote-mac-bbbb"
    with pytest.raises(PairingError, match="pairing_request_signature_invalid"):
        windows2.submit(opened2["window_id"], _submission(code2, tampered, report))
    with pytest.raises(PairingError, match="pairing_quota_invalid"):
        windows2.submit(opened2["window_id"], _submission(code2, request, report,
            {**QUOTA, "background_allowed": True}))
    assert windows2.list()[0]["state"] == "open"


def test_window_is_single_use_and_handoff_reaches_only_the_claimant():
    clock = Clock()
    windows, opened = _open(clock)
    wid = opened["window_id"]
    code = parse_join_text(opened["join_link"], now=NOW)["code"]
    _s, request, report = _target()
    digest = request_digest(request)
    windows.submit(wid, _submission(code, request, report))
    # Exact retry is idempotent; a second device cannot claim the window.
    assert windows.submit(wid, _submission(code, request, report))["state"] == "waiting_for_owner"
    _s2, other, other_report = _target(name="second-endpoint", node="remote-mac-bbbb")
    with pytest.raises(PairingError, match="pairing_window_claimed"):
        windows.submit(wid, _submission(code, other, other_report))
    poll = {"protocol": "mycelium.a13_pairing_poll.v1", "code": code, "request_digest": digest}
    assert windows.poll(wid, poll) == {"state": "waiting_for_owner"}
    with pytest.raises(PairingError, match="pairing_window_request_mismatch"):
        windows.poll(wid, {**poll, "request_digest": request_digest(other)})
    issued = []

    def issue(verified, quota):
        issued.append((verified["node_id"], quota))
        return {"protocol": "fixture-handoff", "for": verified["node_id"]}

    with pytest.raises(PairingError, match="pairing_window_request_mismatch"):
        windows.approve(wid, request_digest(other), issue)
    approved = windows.approve(wid, digest, issue)
    assert approved["state"] == "approved" and issued == [(request["node_id"], QUOTA)]
    # Approval is one click and idempotent: no second invite is minted.
    windows.approve(wid, digest, issue)
    assert len(issued) == 1
    delivered = windows.poll(wid, poll)
    assert delivered == {"state": "approved", "handoff": {"protocol": "fixture-handoff",
                                                          "for": request["node_id"]}}
    # Response loss: the same claimant may fetch the same ciphertext again.
    assert windows.poll(wid, poll) == delivered
    assert windows.list()[0]["state"] == "delivered"
    with pytest.raises(PairingError, match="pairing_window_claimed"):
        windows.submit(wid, _submission(code, other, other_report))


def test_window_expires_and_expired_windows_refuse_every_operation():
    clock = Clock()
    windows, opened = _open(clock, ttl=60)
    wid = opened["window_id"]
    code = parse_join_text(opened["join_link"], now=NOW)["code"]
    _s, request, report = _target()
    clock.now = NOW + 61
    with pytest.raises(PairingError, match="pairing_window_expired"):
        windows.submit(wid, _submission(code, request, report))
    windows, opened = _open(clock, ttl=60)
    wid = opened["window_id"]
    code = parse_join_text(opened["join_link"], now=clock.now)["code"]
    _s, request, report = _target(now=clock.now)
    windows.submit(wid, _submission(code, request, report))
    clock.now += 61
    with pytest.raises(PairingError, match="pairing_window_expired"):
        windows.approve(wid, request_digest(request), lambda *_: {"x": 1})
    with pytest.raises(PairingError, match="pairing_window_expired"):
        windows.poll(wid, {"protocol": "mycelium.a13_pairing_poll.v1", "code": code,
                           "request_digest": request_digest(request)})
    assert windows.list()[0]["state"] == "expired"
    assert "pending" not in windows.list()[0]


def test_owner_revocation_closes_window_even_after_approval():
    clock = Clock()
    windows, opened = _open(clock)
    wid = opened["window_id"]
    code = parse_join_text(opened["join_link"], now=NOW)["code"]
    _s, request, report = _target()
    windows.submit(wid, _submission(code, request, report))
    windows.approve(wid, request_digest(request), lambda *_: {"sealed": True})
    assert windows.revoke(wid)["state"] == "revoked"
    poll = {"protocol": "mycelium.a13_pairing_poll.v1", "code": code,
            "request_digest": request_digest(request)}
    with pytest.raises(PairingError, match="pairing_window_revoked"):
        windows.poll(wid, poll)
    with pytest.raises(PairingError, match="pairing_window_revoked"):
        windows.submit(wid, _submission(code, request, report))
    with pytest.raises(PairingError, match="pairing_window_unknown"):
        windows.revoke("ZZZZ")


def test_wrong_codes_lock_the_window_and_unknown_windows_leak_nothing():
    clock = Clock()
    windows, opened = _open(clock)
    wid = opened["window_id"]
    code = parse_join_text(opened["join_link"], now=NOW)["code"]
    _s, request, report = _target()
    wrong = ("2" if code[0] != "2" else "3") + code[1:]
    for _ in range(4):
        with pytest.raises(PairingError, match="pairing_window_code_invalid"):
            windows.submit(wid, _submission(wrong, request, report))
    with pytest.raises(PairingError, match="pairing_window_locked"):
        windows.submit(wid, _submission(wrong, request, report))
    # Even the right code no longer opens a locked window.
    with pytest.raises(PairingError, match="pairing_window_locked"):
        windows.submit(wid, _submission(code, request, report))
    with pytest.raises(PairingError, match="pairing_window_unknown"):
        windows.submit("ZZZZ", _submission(code, request, report))


def test_http_surface_maps_codes_to_bounded_statuses():
    clock = Clock()
    windows, opened = _open(clock)
    wid = opened["window_id"]
    code = parse_join_text(opened["join_link"], now=NOW)["code"]
    _s, request, report = _target()
    body = json.dumps(_submission(code, request, report)).encode()
    status, raw = windows.handle("POST", f"/pairing-window/v1/{wid}/request", body)
    assert status == 200 and json.loads(raw)["state"] == "waiting_for_owner"
    assert windows.handle("GET", f"/pairing-window/v1/{wid}/request", b"")[0] == 405
    assert windows.handle("POST", "/pairing-window/v1/ZZZZ/request", body) == (
        404, b'{"error":"pairing_window_unknown"}')
    assert windows.handle("POST", f"/pairing-window/v1/{wid}/request", b"{nope")[0] == 400
    assert windows.handle("POST", f"/pairing-window/v1/{wid}/other", body)[0] == 404
    windows.revoke(wid)
    assert windows.handle("POST", f"/pairing-window/v1/{wid}/request", body) == (
        410, b'{"error":"pairing_window_revoked"}')
