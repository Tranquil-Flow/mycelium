# SPDX-License-Identifier: AGPL-3.0-only
"""A13 order trap: the relay must precede the swarm, so creation can start it for you."""
from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a13_macos.app import DemoApp, DemoError, action_error_body
from mycelium_a13_macos.public_relay import SeedRelayProxy

CONSENT = {
    "peer_visibility": True, "power": "battery_allowed", "storage_bytes": 2_000_000_000,
    "network": "unmetered_only", "background": False, "diagnostics": False,
    "thermal": "stop_on_serious",
}
RELAY = {"relay_acknowledged": True, "max_seconds": 1800}
WEB = Path(__file__).resolve().parents[2] / "mycelium_a13_macos" / "web"


@pytest.fixture
def tunnels(monkeypatch):
    import mycelium_a13_macos.public_relay as module

    instances: list = []

    class Tunnel:
        def __init__(self, root, provider="cloudflare"):
            self.public_origin = "https://relay-fixture.invalid"
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
    yield instance
    instance.close()


def test_create_with_relay_starts_the_relay_first_automatically(app, tunnels) -> None:
    app.action("/api/create", {"consent": CONSENT, **RELAY})
    assert len(tunnels) == 1
    # only possible when the relay origin was configured before the swarm existed
    assert app.authority.coordinator.seed_url == "https://relay-fixture.invalid"
    assert app.authority.status()["membership_state"] == "current"
    assert app.public_relay is tunnels[0]


def test_create_with_relay_needs_the_explicit_acknowledgement(app, tunnels) -> None:
    with pytest.raises(DemoError):
        app.action("/api/create", {"consent": CONSENT, "relay_acknowledged": False, "max_seconds": 1800})
    with pytest.raises(DemoError):
        app.action("/api/create", {"consent": CONSENT, "relay_acknowledged": True})
    assert tunnels == []
    assert app.authority.status().get("swarm_created") is not True


def test_failed_creation_does_not_leave_a_relay_running(app, tunnels, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise DemoError("create_refused")

    monkeypatch.setattr(app.authority, "create_swarm", refuse)
    with pytest.raises(DemoError):
        app.action("/api/create", {"consent": CONSENT, **RELAY})
    assert tunnels[0].closed is True and app.public_relay is None


def test_plain_create_is_unchanged_and_private(app, tunnels) -> None:
    app.action("/api/create", {"consent": CONSENT})
    assert tunnels == [] and app.public_relay is None
    assert app.authority.status()["membership_state"] == "current"


def test_relay_after_creation_is_no_longer_a_dead_end(app, tunnels) -> None:
    app.action("/api/create", {"consent": CONSENT})
    value = app.action("/api/network/start", RELAY)  # R23: the relay can start after the swarm exists
    assert value["public_origin"] == "https://relay-fixture.invalid"
    assert app.authority.coordinator.seed_url == "https://relay-fixture.invalid"
    with pytest.raises(DemoError) as caught:  # a second one while one runs is still refused
        app.action("/api/network/start", RELAY)
    assert str(caught.value) == "network_requires_fresh_owner"
    assert len(tunnels) == 1


def test_plain_errors_keep_the_closed_code_only_body() -> None:
    assert action_error_body(DemoError("consent_required")) == {"error": "consent_required"}


def test_fresh_owner_can_still_start_the_relay_by_itself(app, tunnels) -> None:
    value = app.action("/api/network/start", RELAY)
    assert value["public_origin"] == "https://relay-fixture.invalid"


def test_the_page_offers_the_one_click_order_and_shows_the_message() -> None:
    # Static artifact contract only: no browser is driven here.
    html = (WEB / "index.html").read_text()
    script = (WEB / "app.js").read_text()
    assert 'id="create-with-relay"' in html
    assert "create-with-relay" in script and "relay_acknowledged" in script
    assert "value.message" in script and "value.fix" in script
