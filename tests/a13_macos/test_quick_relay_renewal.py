# SPDX-License-Identifier: AGPL-3.0-only
"""Quick tunnels share the bounded serving lifetime, without replacing URL/process.

No cloudflared or internet: the real relay startup/log reader/proxy are exercised
with a fake child and a clock that also fires the old hard Timer on the RED path.
"""
import io
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import public_relay
from mycelium_a13_macos.app import DemoApp, DemoError

ORIGIN = 'https://quiet-relay-test.trycloudflare.com'


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.timers = []

    def __call__(self):
        return self.now

    def advance(self, seconds, relay):
        self.now += seconds
        for timer in self.timers:
            if not timer.cancelled and self.now >= timer.deadline:
                timer.cancelled = True
                timer.callback()
        relay._lifetime_step()

    def timer(self, seconds, callback):
        clock = self

        class Timer:
            cancelled = False

            def start(self):
                self.deadline = clock.now + seconds
                self.callback = callback
                clock.timers.append(self)

            def cancel(self):
                self.cancelled = True

        return Timer()


class FakeProcess:
    def __init__(self):
        self.stdout = io.BytesIO((ORIGIN + '\n').encode())
        self.returncode = None
        self.terminations = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminations += 1
        self.returncode = 0

    def wait(self, timeout):
        assert self.returncode == 0
        return self.returncode


@pytest.fixture
def quick(tmp_path, monkeypatch):
    clock = Clock()
    children, relays = [], []
    real_relay = public_relay.TemporaryHttpsRelay

    def spawn(*args, **kwargs):
        child = FakeProcess()
        children.append(child)
        return child

    def construct(root, **kwargs):
        relay = real_relay(root, _clock=clock, _lifetime_poll=None, **kwargs)
        relays.append(relay)
        return relay

    monkeypatch.setattr(public_relay, 'installed_relay_binary', lambda: '/fake/cloudflared')
    monkeypatch.setattr(public_relay.subprocess, 'Popen', spawn)
    monkeypatch.setattr(public_relay.threading, 'Timer', clock.timer)
    monkeypatch.setattr(public_relay, 'TemporaryHttpsRelay', construct)
    value = construct(tmp_path / 'relay')
    yield SimpleNamespace(relay=value, clock=clock, children=children)
    for relay in relays:
        relay.close()


def test_serving_quick_relay_survives_window_with_same_url_process_and_proxy(quick):
    relay, clock = quick.relay, quick.clock
    relay.set_serving_probe(lambda: True)
    relay.start(acknowledged=True, max_seconds=1800)
    process, proxy, origin = relay.process, relay.proxy, relay.public_origin
    clock.advance(1740, relay)  # same 60-second margin as external relays
    clock.advance(61, relay)    # beyond the original 30-minute cutoff
    assert relay.status()['state'] == 'relay_process_running'
    assert relay.status()['renewals'] == 1
    assert relay.status()['expires_in_seconds'] == 1739
    assert relay.public_origin == origin == ORIGIN
    assert relay.process is process and relay.proxy is proxy
    assert quick.children == [process] and process.terminations == 0


def test_nonserving_quick_relay_closes_at_exact_window_deadline(quick):
    relay, clock = quick.relay, quick.clock
    relay.start(acknowledged=True, max_seconds=60)
    clock.advance(59, relay)
    assert relay.status()['state'] == 'relay_process_running'
    clock.advance(1, relay)
    assert relay.status()['state'] == 'stopped'
    assert relay.process.terminations == 1 and relay.proxy._closed
    assert relay.status()['renewals'] == 0


def test_quick_relay_stops_renewing_when_serving_stops(quick):
    relay, clock, serving = quick.relay, quick.clock, [True]
    relay.set_serving_probe(lambda: serving[0])
    relay.start(acknowledged=True, max_seconds=60)
    clock.advance(39, relay)
    assert relay.status()['renewals'] == 0
    clock.advance(1, relay)
    assert relay.status()['renewals'] == 1
    serving[0] = False
    clock.advance(59, relay)
    assert relay.status()['state'] == 'relay_process_running'
    clock.advance(1, relay)
    assert relay.status()['state'] == 'stopped'
    assert relay.status()['renewals'] == 1


@pytest.mark.parametrize('window', [30, 1800])
def test_quick_relay_never_grants_more_than_24_hours(window, quick):
    relay, clock = quick.relay, quick.clock
    relay.set_serving_probe(lambda: True)
    relay.start(acknowledged=True, max_seconds=window)
    started = clock.now
    cap = public_relay.RENEWAL_CAP_SECONDS
    assert cap == 86400
    margin = min(public_relay.RENEWAL_MARGIN_SECONDS, window / 3)
    # Count the same granted-window budget as external relays, not 24 h of
    # extra wall time: early renewal means actual lifetime is slightly shorter.
    for renewal in range(1, cap // window):
        clock.advance(window - margin, relay)
        assert relay.status()['renewals'] == renewal
        assert relay.status()['state'] == 'relay_process_running'
    clock.advance(window - margin, relay)
    status = relay.status()
    assert status['renewal_stopped'] == 'relay_renewal_cap_reached'
    assert status['renewals'] == cap // window - 1
    assert status['renewal_cap_seconds'] == cap
    assert status['expires_in_seconds'] == margin
    clock.advance(margin, relay)
    assert relay.status()['state'] == 'stopped'
    assert clock.now - started <= cap
    clock.advance(cap, relay)
    assert relay.status()['state'] == 'stopped'
    assert relay.status()['renewals'] == cap // window - 1
    assert len(quick.children) == 1


def test_quick_relay_status_has_bounded_renewal_fields(quick):
    relay = quick.relay.start(acknowledged=True, max_seconds=60)
    status = relay.status()
    assert status['auto_renew'] is True
    assert status['long_lived'] is False
    assert status['renewals'] == 0
    assert status['expires_in_seconds'] == 60
    assert status['renewal_cap_seconds'] == 86400
    assert status['renewal_stopped'] is None


def test_quick_relay_probe_failure_does_not_extend_deadline(quick):
    def broken_probe():
        raise RuntimeError('run state unavailable')

    relay, clock = quick.relay, quick.clock
    relay.set_serving_probe(broken_probe)
    relay.start(acknowledged=True, max_seconds=60)
    clock.advance(40, relay)
    clock.advance(20, relay)
    assert relay.status()['state'] == 'stopped'
    assert relay.status()['renewals'] == 0


def test_closed_quick_relay_cannot_be_revived_by_serving(quick):
    relay, clock = quick.relay, quick.clock
    relay.set_serving_probe(lambda: True)
    relay.start(acknowledged=True, max_seconds=60)
    relay.close()
    clock.advance(40, relay)
    assert relay.status()['state'] == 'stopped'
    assert relay.status()['renewals'] == 0
    assert relay.process.terminations == 1


def test_app_quick_relay_uses_the_existing_run_and_published_swarm_signal(tmp_path, quick):
    app = DemoApp(tmp_path / 'app', check_platform=False)
    relay = app._new_relay(tmp_path / 'app-relay', {})
    assert relay._serving_probe() is False
    app.owner_run = SimpleNamespace(active=lambda: True)
    assert relay._serving_probe() is True
    relay.start(acknowledged=True, max_seconds=60)
    quick.clock.advance(40, relay)
    assert relay.status()['renewals'] == 1
    app.owner_run = SimpleNamespace(active=lambda: False)
    assert relay._serving_probe() is False
    app.swarm_publisher = SimpleNamespace(state={'enabled': True})
    assert relay._serving_probe() is True
    quick.clock.advance(40, relay)
    assert relay.status()['renewals'] == 2
    app.swarm_publisher.state['enabled'] = False
    assert relay._serving_probe() is False
    quick.clock.advance(60, relay)
    assert relay.status()['state'] == 'stopped'


@pytest.mark.parametrize('extra', [
    {'long_lived': True}, {'auto_renew': False}, {'max_renewed_seconds': 86401},
    {'public_origin': 'https://example.com'}, {'proxy_port': 8788}, {'cloudflare_front': True},
])
def test_quick_relay_refuses_external_only_settings(tmp_path, quick, extra):
    with pytest.raises(ValueError, match='relay_provider_invalid'):
        public_relay.TemporaryHttpsRelay(tmp_path / 'bad', **extra)
    assert quick.children == []


@pytest.mark.parametrize('extra', [
    {'relay_acknowledged': False}, {'max_seconds': 29}, {'max_seconds': 1801},
    {'long_lived': True}, {'auto_renew': True}, {'max_renewed_seconds': 86400},
])
def test_quick_app_consent_shape_and_limits_stay_unchanged(tmp_path, quick, extra):
    app = DemoApp(tmp_path / 'app', check_platform=False)
    with pytest.raises(DemoError, match='network_consent_required'):
        app._start_public_relay({'relay_acknowledged': True, 'max_seconds': 1800, **extra})
    assert quick.children == []
