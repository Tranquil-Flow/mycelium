# SPDX-License-Identifier: AGPL-3.0-only
"""R23(6a)/R24(5): join codes any time; the relay can start after the swarm exists; serving only with the relay up."""
from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a13_macos.app import DemoApp, DemoError, action_error_body
from mycelium_a13_macos.public_relay import SeedRelayProxy

CONSENT = {"peer_visibility": True, "power": "battery_allowed", "storage_bytes": 2_000_000_000,
           "network": "unmetered_only", "background": False, "diagnostics": False, "thermal": "stop_on_serious"}
RELAY = {"relay_acknowledged": True, "max_seconds": 1800}
WEB = Path(__file__).resolve().parents[2] / "mycelium_a13_macos" / "web"


@pytest.fixture
def tunnels(monkeypatch):
    import mycelium_a13_macos.public_relay as module
    instances: list = []

    class Tunnel:
        def __init__(self, root, provider="cloudflare"):
            self.public_origin = f"https://relay-fixture-{len(instances)}.invalid"
            self.closed = False
            instances.append(self)

        def start(self, *, acknowledged, max_seconds):
            self.proxy = SeedRelayProxy().start()
            return self

        def status(self):
            return {"state": "fixture", "public_origin": self.public_origin}

        def close(self):
            self.proxy.close()
            self.closed = True

    monkeypatch.setattr(module, "TemporaryHttpsRelay", Tunnel)
    return instances


@pytest.fixture
def app(tmp_path):
    instance = DemoApp(tmp_path / "app", check_platform=False).start()
    instance.action("/api/create", {"consent": CONSENT})  # a private swarm: NO relay yet
    yield instance
    instance.close()


def test_relay_can_start_after_swarm_created(app, tunnels) -> None:
    before = app.authority.coordinator.swarm_id
    value = app.action("/api/network/start", RELAY)
    assert value["public_origin"] == tunnels[0].public_origin
    assert app.public_relay is tunnels[0]
    assert app.authority.coordinator.seed_url == tunnels[0].public_origin
    assert app.authority.coordinator.swarm_id == before  # same swarm, same identity
    assert app.authority.status()["membership_state"] == "current"
    assert app.status()["network"]["public_origin"] == tunnels[0].public_origin


def test_relay_failure_after_create_leaves_swarm_intact_and_closes_relay(app, tunnels, monkeypatch) -> None:
    import mycelium_a13_macos.endpoint_recovery as recovery

    def boom(*_a, **_k):
        raise RuntimeError("attach failed")

    monkeypatch.setattr(recovery, "attach_public_endpoint", boom)
    seed = app.authority.coordinator.seed_url
    with pytest.raises(RuntimeError):
        app.action("/api/network/start", RELAY)
    assert tunnels[0].closed is True and app.public_relay is None
    assert app.authority.coordinator.seed_url == seed
    assert app.authority.status()["membership_state"] == "current"


def test_attach_rolls_back_when_the_restart_fails(app, monkeypatch) -> None:
    from mycelium_a13_macos.endpoint_recovery import attach_public_endpoint
    authority = app.authority
    real_start, calls = authority.start, []
    seed = authority.coordinator.seed_url

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("restart failed")
        return real_start()

    monkeypatch.setattr(authority, "start", flaky)
    with pytest.raises(RuntimeError):
        attach_public_endpoint(authority, public_origin="https://x-fixture.invalid", port=50001, acknowledged=True)
    assert not (authority.state_root / "network.json").exists()
    assert authority.coordinator.seed_url.startswith("https://127.0.0.1:") and seed.startswith("https://127.0.0.1:")
    assert authority.status()["membership_state"] == "current"


def test_second_relay_start_is_still_refused_while_one_runs(app, tunnels) -> None:
    app.action("/api/network/start", RELAY)
    with pytest.raises(DemoError) as caught:
        app.action("/api/network/start", RELAY)
    assert str(caught.value) == "network_requires_fresh_owner"


def test_join_window_opens_without_relay_state_waiting_for_relay(app, tunnels) -> None:
    value = app.action("/api/pair/window/open", {"ttl_seconds": 600})
    assert value["state"] == "waiting_for_relay" and value["pending_id"]
    assert "once the relay starts" in value["message"] and value["fix"] == {"action": "start_relay"}
    flow = app.status()["join_flow"]
    assert [p["state"] for p in flow["pending_windows"]] == ["waiting_for_relay"]
    assert flow["pending_windows"][0]["note"] == "ready to share once the relay starts"
    assert flow["pairing_windows"] == [] and tunnels == []


def test_window_becomes_redeemable_when_relay_starts_and_link_renders(app, tunnels) -> None:
    app.action("/api/pair/window/open", {"ttl_seconds": 600})
    app.action("/api/pair/window/open", {"ttl_seconds": 300})
    app.action("/api/network/start", RELAY)
    flow = app.status()["join_flow"]
    assert flow["pending_windows"] == []
    windows = flow["pairing_windows"]
    assert len(windows) == 2 and {w["state"] for w in windows} == {"open"}
    for window in windows:
        assert window["short_code"] and window["join_link"].startswith("mycelium-join:") or window["join_link"]
        assert window["tunnel_name"] or window["join_link"]


def test_pending_codes_are_capped_and_can_be_cancelled(app, tunnels) -> None:
    from mycelium_a13_macos.pairing import _MAX_WINDOWS
    ids = [app.action("/api/pair/window/open", {"ttl_seconds": 600})["pending_id"] for _ in range(_MAX_WINDOWS)]
    with pytest.raises(DemoError) as caught:
        app.action("/api/pair/window/open", {"ttl_seconds": 600})
    assert str(caught.value) == "pairing_window_capacity"
    with pytest.raises(DemoError):
        app.action("/api/pair/window/open", {"ttl_seconds": 1})  # same TTL bounds as a live window
    value = app.action("/api/pair/window/revoke", {"window_id": ids[0]})
    assert value["state"] == "revoked"
    assert ids[0] not in [p["pending_id"] for p in app.status()["join_flow"]["pending_windows"]]


def _members(app, monkeypatch, joined):
    rows = tuple({"node_id": f"{i:02d}" * 10, "generation": 1, "lifecycle_state": "ACTIVE"} for i in range(joined + 1))
    monkeypatch.setattr(app.authority.coordinator, "members", lambda *a, **k: rows)


def test_status_members_joined_waiting_for_relay(app, monkeypatch) -> None:
    status = app.status()
    assert status["serving"]["state"] != "waiting_for_relay" and status["relay_wait"] is None
    _members(app, monkeypatch, 2)
    wait = app.status()["relay_wait"]
    assert wait == {"state": "waiting_for_relay", "members_joined": 2,
                    "line": "2 members joined, waiting for relay to serve"}
    assert app.status()["serving"]["state"] == "waiting_for_relay"
    _members(app, monkeypatch, 1)
    assert app.status()["relay_wait"]["line"] == "1 member joined, waiting for relay to serve"


def test_status_no_wait_line_once_relay_is_up(app, tunnels, monkeypatch) -> None:
    app.action("/api/network/start", RELAY)
    _members(app, monkeypatch, 2)
    assert app.status()["relay_wait"] is None


def test_route_start_without_relay_refuses_with_fix_hint(app, monkeypatch) -> None:
    _members(app, monkeypatch, 1)
    for path, body in (("/api/demo/start", {}),
                       ("/api/serving/start", {"execution_proposal": {}, "model_execution_acknowledged": True}),
                       ("/api/serving/activate", {"execution_proposal": {}, "activation_acknowledged": True})):
        with pytest.raises(DemoError) as caught:
            app.action(path, body)
        error = action_error_body(caught.value)
        assert error["error"] == "relay_required" and error["fix"] == {"action": "start_relay"}, path
        assert "relay" in error["message"]


def test_solo_owner_is_not_blocked_without_relay(app, monkeypatch) -> None:
    from mycelium_a13_macos.demo_app import relay_gate
    relay_gate(app)  # no other member joined: a private one-Mac swarm keeps working


def test_swarm_publish_without_relay_offers_start_relay_fix(app) -> None:
    with pytest.raises(DemoError) as caught:
        app.action("/api/swarm/publish", {"directory": "https://swarms.mycelium.now", "name": "x",
                                          "access_mode": "request"})
    error = action_error_body(caught.value)
    assert error["error"] == "swarm_relay_required" and error["fix"] == {"action": "start_relay"}


def test_target_cannot_start_a_relay(tmp_path, tunnels) -> None:
    target = DemoApp(tmp_path / "t", check_platform=False).start()
    try:
        target.action("/api/join/request", {"consent": CONSENT})
        with pytest.raises(DemoError):
            target.action("/api/network/start", RELAY)
        assert tunnels == []
    finally:
        target.close()


def test_page_has_a_start_relay_card_after_creation_and_no_start_over_copy() -> None:
    html, script = (WEB / "index.html").read_text(), (WEB / "app.js").read_text()
    assert "start over with a new owner" not in html and "start over with a new owner" not in script
    assert "waiting_for_relay" in script and "waiting for relay to serve" in script
    assert "pending_windows" in script and "start_relay" in script
    assert "once the relay starts" in script
