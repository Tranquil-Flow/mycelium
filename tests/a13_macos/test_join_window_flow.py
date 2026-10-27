# SPDX-License-Identifier: AGPL-3.0-only
"""Join through the owner's pairing window over the relay proxy (relay-only).

Two real local applications: owner authority + target member, real local TLS
seed, and the REAL ``SeedRelayProxy`` hop that cloudflared forwards to.  The
public quick tunnel itself is a fixture (``.invalid`` / ``trycloudflare``
origin names are never dialled): the target's window/probe/report requests
are sent to the proxy's loopback origin through the explicit test seam, and
its seed requests to the owner's loopback TLS seed.  NOT physical two-Mac or
public-network evidence.
"""
from __future__ import annotations

import json

import pytest

import mycelium_a13_macos.public_relay as relay_module
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.pairing import encode_join_link, parse_join_text
from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_macos.public_relay import SeedRelayProxy
from mycelium_a13_macos.standing_consent import DISCLOSURE_DIGEST
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite

CONSENT = {"peer_visibility": True, "power": "battery_allowed", "storage_bytes": 2_000_000_000,
           "network": "metered_allowed", "background": False, "diagnostics": False,
           "thermal": "stop_on_serious"}
STANDING = {"acknowledged": True, "disclosure_digest": DISCLOSURE_DIGEST,
            "caps": {"storage_bytes": 2_000_000_000, "max_memory_bytes": 8 * 1024**3,
                     "max_wall_seconds": 1800, "max_commands": 5000}}


def _owner(tmp_path, monkeypatch, origin):
    class Tunnel:
        def __init__(self, root, provider="cloudflare"):
            self.public_origin = origin

        def set_serving_probe(self, probe):
            self._serving_probe = probe

        def start(self, *, acknowledged, max_seconds):
            self.proxy = SeedRelayProxy().start()
            return self

        def status(self):
            return {"state": "fixture", "public_origin": self.public_origin}

        def close(self):
            self.proxy.close()

    monkeypatch.setattr(relay_module, "TemporaryHttpsRelay", Tunnel)
    owner = DemoApp(tmp_path / "owner", check_platform=False).start()
    owner.action("/api/network/start", {"relay_acknowledged": True, "max_seconds": 1800})
    owner.action("/api/create", {"consent": CONSENT})
    return owner


def _target(tmp_path, owner, name="target"):
    seed = owner.authority.seed_server

    def client(*, bundle=None, membership=None, now, timeout):
        if bundle is not None:
            return pinned_seed_client_from_invite(bundle, now=now, ca_certificate=seed.material.ca_certificate,
                                                  timeout=timeout, transport_url=seed.base_url)
        return pinned_seed_client(seed_url=membership["seed_url"], swarm_id=membership["swarm_id"],
            seed_key_digest=membership["seed_key_digest"],
            seed_key_records=list(membership["seed_key_records"]),
            ca_certificate=seed.material.ca_certificate, timeout=timeout, transport_url=seed.base_url)

    def factory(root):
        return RemoteMacMember(root, _test_seed_client_factory=client)

    return DemoApp(tmp_path / name, check_platform=False, _test_peer_factory=factory,
                   _test_join_base_url=owner.public_relay.proxy.origin).start()


def test_target_joins_through_window_with_one_owner_click_and_reports_automatically(tmp_path, monkeypatch):
    owner = _owner(tmp_path, monkeypatch, "https://relay-fixture.invalid")
    target = second = None
    try:
        opened = owner.action("/api/pair/window/open", {"ttl_seconds": 600})
        assert opened["state"] == "open" and opened["join_link"].startswith("mycelium-join:")
        target = _target(tmp_path, owner)
        waiting = target.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                                     "join": opened["join_link"]})
        assert waiting["state"] == "waiting_for_owner"
        assert target.installation_role == "target"
        [window] = owner.action("/api/pair/window/list", {})["windows"]
        pending = window["pending"]
        # Both screens show the same fingerprint; the owner sees the device
        # and the RTT/throughput measured over the relay path before approving.
        assert pending["fingerprint"] == waiting["fingerprint"]
        assert pending["device"]["memory_bytes"] > 0 and pending["device"]["chip"]
        assert pending["link"]["path_kind"] == "public_https_relay"
        assert pending["link"]["rtt_ms_median"] > 0 and pending["link"]["download_bytes_per_second"] > 0
        assert target.action("/api/join/window/poll", {})["state"] == "waiting_for_owner"
        approved = owner.action("/api/pair/window/approve", {"window_id": window["window_id"],
                                                             "request_digest": pending["request_digest"]})
        assert approved["state"] == "approved"
        joined = target.action("/api/join/window/poll", {})
        assert joined["state"] == "joined"
        status = target.status()
        assert status["authority"]["membership_state"] == "current"
        assert status["join_flow"]["standing_consent"]["state"] == "granted"
        assert status["join_flow"]["join_window"] is None
        # Automatic join report reached the owner over the relay and is
        # bound to the member's current generation; nothing qualified.
        node_id = target.authority.member.node_id
        reports = owner.status()["join_flow"]["member_reports"]
        assert reports[node_id]["purpose"] == "join"
        assert reports[node_id]["generation"] == target.authority.member.generation
        assert owner.member_capability_reports()[node_id]["link"]["rounds"] == 3
        assert status["join_flow"]["last_report"]["state"] == "delivered"
        assert not status["serving"]["route_ready"]
        # Single use: the same link cannot enrol a second Mac.
        second = _target(tmp_path, owner, name="second")
        with pytest.raises(Exception) as refused:
            second.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                               "join": opened["join_link"]})
        assert getattr(refused.value, "code", str(refused.value)) == "pairing_window_claimed"
        # Production composition of the stable auto-step interface: the real
        # DemoApp satisfies auto_poll_step and runs a registered step.
        from mycelium_a13_macos.standing_consent import auto_poll_step
        target.authority.pending_stage = lambda: None
        seen = []
        target.auto_steps.append(lambda app, ctx: seen.append(ctx["binding"]["node_id"]) or {"step": "fixture"})
        with target.guard:
            step = auto_poll_step(target)
        assert step["state"] == "ran" and step["offer"] is None and step["steps"] == [{"step": "fixture"}]
        assert seen[-1] == node_id
        # Revocable any time from the target: withdrawal is persisted.
        withdrawn = target.action("/api/consent/standing/withdraw", {"confirm": True})
        assert withdrawn["state"] == "withdrawn"
        assert target.status()["join_flow"]["standing_consent"]["state"] == "withdrawn"
        with target.guard:
            assert auto_poll_step(target)["state"] == "standing_consent_withdrawn"
    finally:
        for app in (second, target):
            if app is not None:
                app.close()
        owner.close()


def test_pinned_link_refuses_a_handoff_from_a_different_seed_key(tmp_path, monkeypatch):
    owner = _owner(tmp_path, monkeypatch, "https://relay-fixture.invalid")
    target = None
    try:
        opened = owner.action("/api/pair/window/open", {"ttl_seconds": 600})
        link = parse_join_text(opened["join_link"], now=opened["opened_at"])
        forged = encode_join_link({"protocol": "mycelium.a13_join_link.v1", "origin": link["origin"],
            "window_id": link["window_id"], "code": link["code"],
            "seed_key_digest": "sha256:" + "0" * 64, "expires_at": link["expires_at"]})
        target = _target(tmp_path, owner)
        target.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                           "join": forged})
        [window] = owner.action("/api/pair/window/list", {})["windows"]
        owner.action("/api/pair/window/approve", {"window_id": window["window_id"],
            "request_digest": window["pending"]["request_digest"]})
        with pytest.raises(Exception) as refused:
            target.action("/api/join/window/poll", {})
        assert str(refused.value) == "join_seed_pin_mismatch"
        assert target.status()["authority"]["membership_state"] != "current"
    finally:
        if target is not None:
            target.close()
        owner.close()


def test_short_code_join_is_refused_once_the_owner_revokes_the_window(tmp_path, monkeypatch):
    owner = _owner(tmp_path, monkeypatch, "https://quiet-river-apple-stone.trycloudflare.com")
    target = None
    try:
        opened = owner.action("/api/pair/window/open", {"ttl_seconds": 120})
        typed = f"{opened['tunnel_name']} {opened['short_code']}"
        target = _target(tmp_path, owner)
        assert target.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                                  "join": typed})["state"] == "waiting_for_owner"
        owner.action("/api/pair/window/revoke", {"window_id": opened["window_id"]})
        with pytest.raises(Exception) as refused:
            target.action("/api/join/window/poll", {})
        assert getattr(refused.value, "code", str(refused.value)) == "pairing_window_revoked"
        assert target.status()["join_flow"]["join_window"] is None
        assert target.status()["authority"]["membership_state"] != "current"
        # Owner-only and target-only routes stay separated.
        with pytest.raises(Exception, match="owner_role_required"):
            target.action("/api/pair/window/open", {"ttl_seconds": 120})
        with pytest.raises(Exception, match="owner_cannot_become_target"):
            owner.action("/api/join/window", {"consent": CONSENT, "standing_consent": STANDING,
                                              "join": typed})
    finally:
        if target is not None:
            target.close()
        owner.close()


def test_window_without_a_running_public_relay_waits_for_the_relay(tmp_path):
    owner = DemoApp(tmp_path / "owner", check_platform=False).start()
    try:
        owner.action("/api/create", {"consent": CONSENT})
        value = owner.action("/api/pair/window/open", {"ttl_seconds": 600})
        assert value["state"] == "waiting_for_relay"  # no redeemable window until an origin exists
        assert owner.pairing_windows.list() == []
    finally:
        owner.close()


def test_relay_proxy_serves_only_bound_local_join_routes(tmp_path):
    import http.client

    proxy = SeedRelayProxy().start()
    try:
        port = int(proxy.origin.rsplit(":", 1)[1])

        def post(path, body=b"{}"):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        assert post("/pairing-window/v1/ABCD/request")[0] == 503
        seen = []
        proxy.bind_local(lambda method, path, body: (seen.append((method, path, body)) or
                                                     (200, json.dumps({"ok": True}).encode())))
        assert post("/pairing-window/v1/ABCD/request", b'{"a":1}') == (200, b'{"ok": true}')
        assert post("/link-probe/v1/0", b"") [0] == 200
        assert post("/member-report/v1")[0] == 200
        assert post("/elsewhere/v1")[0] == 404
        assert post("/pairing-window/../seed/x")[0] == 404
        assert seen[0] == ("POST", "/pairing-window/v1/ABCD/request", b'{"a":1}')
    finally:
        proxy.close()
