# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 item e: the external-provider relay renews before it expires while the swarm is serving.

Live demo (2026-10-20/21): a relay on ``demo.mycelium.now`` lasted ``max_seconds`` (at most 1800 s) and the public demo
went dark until somebody restarted it by hand (a cron-like keepalive script).  The external relay now extends itself
shortly before expiry **while a run is serving**, bounded by a total cap (24 h by default), and an owner who runs a named
tunnel can choose ``long_lived`` explicitly.  A relay that is not serving is never extended: the bounded budget is unchanged.
"""
import socket

import pytest

from mycelium_a13_macos import public_relay
from mycelium_a13_macos.app import DemoApp, DemoError
from mycelium_a13_macos.public_relay import TemporaryHttpsRelay

ORIGIN = 'https://demo.mycelium.now'


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError('external provider must not spawn a process')
    monkeypatch.setattr(public_relay.subprocess, 'Popen', boom)


class Clock:
    def __init__(self, now=1000.0): self.now = now
    def __call__(self): return self.now


def relay(tmp_path, clock, serving, **extra):
    value = TemporaryHttpsRelay(tmp_path / 'relay', provider='external', public_origin=ORIGIN, proxy_port=free_port(),
                                _clock=clock, _lifetime_poll=None, **extra)
    value.set_serving_probe(lambda: serving[0])
    return value


def test_a_serving_relay_is_extended_shortly_before_it_expires(tmp_path):
    clock, serving = Clock(), [True]
    value = relay(tmp_path, clock, serving).start(acknowledged=True, max_seconds=60)
    try:
        clock.now = 1030; value._lifetime_step()           # 30 s left: not yet
        assert value.status()['renewals'] == 0 and value.status()['expires_in_seconds'] == 30
        clock.now = 1041; value._lifetime_step()           # 19 s left: renew for another 60 s
        status = value.status()
        assert status['renewals'] == 1 and status['expires_in_seconds'] == 60 and status['state'] == 'relay_process_running'
        clock.now = 1100; value._lifetime_step()
        assert value.status()['state'] == 'relay_process_running'  # it outlived the original 60 s
    finally:
        value.close()


def test_a_relay_that_is_not_serving_is_not_extended_and_closes_at_its_budget(tmp_path):
    clock, serving = Clock(), [False]
    value = relay(tmp_path, clock, serving).start(acknowledged=True, max_seconds=60)
    clock.now = 1045; value._lifetime_step()
    assert value.status()['renewals'] == 0
    serving[0] = True                                       # serving started after the margin but before expiry
    clock.now = 1050; value._lifetime_step()
    assert value.status()['renewals'] == 1
    serving[0] = False
    clock.now = 1111; value._lifetime_step()
    assert value.status()['state'] == 'stopped'


def test_renewal_stops_at_the_total_cap_and_says_so(tmp_path):
    clock, serving = Clock(), [True]
    value = relay(tmp_path, clock, serving, max_renewed_seconds=150).start(acknowledged=True, max_seconds=60)
    clock.now = 1041; value._lifetime_step()               # window 2 (total 120 <= 150)
    assert value.status()['renewals'] == 1
    clock.now = 1082; value._lifetime_step()               # a third window would be 180 > 150
    status = value.status()
    assert status['renewals'] == 1 and status['renewal_stopped'] == 'relay_renewal_cap_reached'
    clock.now = 1101; value._lifetime_step()
    assert value.status()['state'] == 'stopped'


def test_auto_renew_can_be_turned_off(tmp_path):
    clock, serving = Clock(), [True]
    value = relay(tmp_path, clock, serving, auto_renew=False).start(acknowledged=True, max_seconds=60)
    clock.now = 1045; value._lifetime_step()
    assert value.status()['renewals'] == 0 and value.status()['auto_renew'] is False
    clock.now = 1061; value._lifetime_step()
    assert value.status()['state'] == 'stopped'


def test_long_lived_is_an_explicit_owner_setting_without_a_deadline(tmp_path):
    clock, serving = Clock(), [False]
    value = relay(tmp_path, clock, serving, long_lived=True).start(acknowledged=True, max_seconds=60)
    try:
        clock.now += 10 * 86400; value._lifetime_step()
        status = value.status()
        assert status['state'] == 'relay_process_running' and status['long_lived'] is True
        assert status['expires_in_seconds'] is None
    finally:
        value.close()


def test_the_default_is_bounded_not_long_lived(tmp_path):
    clock, serving = Clock(), [True]
    value = relay(tmp_path, clock, serving).start(acknowledged=True, max_seconds=60)
    try:
        status = value.status()
        assert status['long_lived'] is False and status['auto_renew'] is True and status['renewal_cap_seconds'] == 86400
    finally:
        value.close()


def test_closing_stops_renewal_and_a_closed_relay_is_never_revived(tmp_path):
    clock, serving = Clock(), [True]
    value = relay(tmp_path, clock, serving).start(acknowledged=True, max_seconds=60)
    value.close()
    clock.now = 1045; value._lifetime_step()
    assert value.status()['state'] == 'stopped' and value.status()['renewals'] == 0


@pytest.mark.parametrize('extra', [{'auto_renew': 'yes'}, {'long_lived': 1}, {'max_renewed_seconds': -1},
                                   {'max_renewed_seconds': True}])
def test_malformed_renewal_settings_are_refused(tmp_path, extra):
    with pytest.raises(ValueError, match='relay_renewal_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='external', public_origin=ORIGIN, proxy_port=free_port(), **extra)


def test_the_quick_tunnel_has_no_renewal_settings(tmp_path):
    with pytest.raises(ValueError, match='relay_provider_invalid'):
        TemporaryHttpsRelay(tmp_path / 'r', provider='cloudflare', long_lived=True)


@pytest.fixture
def app(tmp_path):
    instance = DemoApp(tmp_path / 'app', check_platform=False).start()
    yield instance
    instance.close()


def body(**extra):
    return {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external', 'public_origin': ORIGIN,
            'proxy_port': free_port(), **extra}


def test_the_app_starts_a_long_lived_external_relay_when_the_owner_says_so(app):
    value = app.action('/api/network/start', body(long_lived=True))
    assert value['long_lived'] is True and value['expires_in_seconds'] is None


def test_the_app_relay_is_bounded_and_renewing_by_default(app):
    value = app.action('/api/network/start', body())
    assert value['long_lived'] is False and value['auto_renew'] is True and 0 < value['expires_in_seconds'] <= 1800


@pytest.mark.parametrize('extra', [{'long_lived': 'yes'}, {'auto_renew': 2}])
def test_the_app_refuses_malformed_renewal_settings(app, extra):
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', body(**extra))
    assert str(caught.value) == 'relay_renewal_invalid'
    assert app.public_relay is None


def test_the_quick_tunnel_start_cannot_carry_renewal_settings(app):
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', {'relay_acknowledged': True, 'max_seconds': 1800, 'long_lived': True})
    assert str(caught.value) == 'network_consent_required'


def test_the_relay_counts_a_run_as_serving_only_while_the_owner_run_is_active(app):
    app.action('/api/network/start', body())
    probe = app.public_relay._serving_probe
    assert probe() is False
    app.owner_run = type('Run', (), {'active': lambda self: True})()
    assert probe() is True
    app.owner_run = type('Run', (), {'active': lambda self: False})()
    assert probe() is False


def test_restart_passes_the_renewal_settings_through(app):
    app.action('/api/network/start', body())
    app.action('/api/create', {'consent': {'peer_visibility': True, 'power': 'battery_allowed', 'storage_bytes': 2_000_000_000,
                                           'network': 'unmetered_only', 'background': False, 'diagnostics': False,
                                           'thermal': 'stop_on_serious'}})
    value = app.action('/api/network/restart', {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external',
                                                'long_lived': True})
    assert value['network']['long_lived'] is True
