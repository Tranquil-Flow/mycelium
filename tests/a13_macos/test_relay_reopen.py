# SPDX-License-Identifier: AGPL-3.0-only
"""V1.2 D4: a closed or expired relay can be reopened with the same Start button, and a published swarm keeps it up.

A13 app-flow gate (V1.1 candidate): the external relay renewed only while the owner run served; once its window closed,
``/api/network/start`` answered ``network_requires_fresh_owner`` (the closed relay object was still attached) and only
local-contributor/stop + network/restart worked.  Reopening on the owner's unchanged address needs neither: the retained
authority already answers on it.  A changed address still needs the local contributor stopped (named code, unchanged).
"""
import json
import socket
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from mycelium_a13_macos import public_relay
from mycelium_a13_macos.app import DemoApp, DemoError

ORIGIN = 'https://demo.mycelium.now'
CONSENT = {'peer_visibility': True, 'power': 'battery_allowed', 'storage_bytes': 2_000_000_000,
           'network': 'unmetered_only', 'background': False, 'diagnostics': False, 'thermal': 'stop_on_serious'}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError('external provider must not spawn a process')
    monkeypatch.setattr(public_relay.subprocess, 'Popen', boom)


@pytest.fixture
def app(tmp_path):
    instance = DemoApp(tmp_path / 'app', check_platform=False).start()
    yield instance
    instance.close()


def body(port, **extra):
    return {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external', 'public_origin': ORIGIN,
            'proxy_port': port, **extra}


def expire(app):
    """The bounded window ran out: the relay closed itself; the app still holds the closed object."""
    app.public_relay.close()
    assert app.public_relay.status()['state'] == 'stopped'


def test_start_reopens_an_expired_external_relay_on_the_same_address(app):
    port = free_port()
    app.action('/api/network/start', body(port))
    app.action('/api/create', {'consent': CONSENT})
    swarm = app.authority.coordinator.swarm_id
    expire(app)
    value = app.action('/api/network/start', body(port))
    assert value['state'] == 'relay_process_running' and value['public_origin'] == ORIGIN
    assert app.public_relay.status()['state'] == 'relay_process_running'
    assert app.authority.coordinator.swarm_id == swarm and app.authority.coordinator.seed_url == ORIGIN
    with urlopen(app.public_relay.proxy.origin + '/seed/identity', timeout=3) as response:
        assert json.load(response)['statement']['seed_url'] == ORIGIN


def test_start_reopens_an_expired_relay_with_the_local_contributor_still_running(app):
    port = free_port()
    app.action('/api/network/start', body(port))
    app.action('/api/create', {'consent': CONSENT})
    app.local_contributor = SimpleNamespace(member=SimpleNamespace(node_id='n'), stop=lambda: None)
    expire(app)
    value = app.action('/api/network/start', body(port))
    assert value['state'] == 'relay_process_running'
    assert app.local_contributor is not None    # the demo keeps serving; nothing was stopped


def test_restart_on_the_unchanged_address_does_not_need_the_local_contributor_stopped(app):
    port = free_port()
    app.action('/api/network/start', body(port))
    app.action('/api/create', {'consent': CONSENT})
    app.local_contributor = SimpleNamespace(member=SimpleNamespace(node_id='n'), stop=lambda: None)
    expire(app)
    value = app.action('/api/network/restart', {'relay_acknowledged': True, 'max_seconds': 1800, 'provider': 'external'})
    assert value['network']['state'] == 'relay_process_running' and value['recovery_card'] is None


def test_a_changed_address_still_needs_the_local_contributor_stopped_and_names_it(app):
    port = free_port()
    app.action('/api/network/start', body(port))
    app.action('/api/create', {'consent': CONSENT})
    app.local_contributor = SimpleNamespace(member=SimpleNamespace(node_id='n'), stop=lambda: None)
    expire(app)
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', body(port, public_origin='https://other.mycelium.now'))
    assert str(caught.value) == 'network_stop_local_contributor_required'


def test_a_running_relay_is_still_not_started_twice(app):
    port = free_port()
    app.action('/api/network/start', body(port))
    with pytest.raises(DemoError) as caught:
        app.action('/api/network/start', body(port))
    assert str(caught.value) == 'network_requires_fresh_owner'


def test_the_reopened_relay_keeps_the_1800_second_window_cap(app):
    port = free_port()
    app.action('/api/network/start', body(port))
    expire(app)
    value = app.action('/api/network/start', body(port))
    assert 0 < value['expires_in_seconds'] <= 1800
    with pytest.raises(DemoError) as caught:
        expire(app); app.action('/api/network/start', body(port, max_seconds=3600))
    assert str(caught.value) == 'network_consent_required'


# -- renewal while a swarm is published but no run is active ----------------------------------------------

def test_a_published_swarm_counts_as_serving_for_renewal_while_the_run_is_idle(app):
    app.action('/api/network/start', body(free_port()))
    probe = app.public_relay._serving_probe
    assert probe() is False
    app.swarm_publisher = SimpleNamespace(state={'enabled': True, 'swarm_id': 's'})
    assert probe() is True                                  # published: the listing points at this relay
    app.swarm_publisher = SimpleNamespace(state={'enabled': False})
    assert probe() is False                                 # unpublished: bounded as before
    app.swarm_publisher = SimpleNamespace(state={})
    assert probe() is False


def test_a_published_idle_swarm_renews_the_window_up_to_the_cap_and_no_further(app, tmp_path):
    clock = [1000.0]
    relay = public_relay.TemporaryHttpsRelay(tmp_path / 'r', provider='external', public_origin=ORIGIN,
                                             proxy_port=free_port(), _clock=lambda: clock[0], _lifetime_poll=None,
                                             max_renewed_seconds=150)
    app.public_relay = relay
    relay.set_serving_probe(app._relay_is_serving)
    app.swarm_publisher = SimpleNamespace(state={'enabled': True})
    relay.start(acknowledged=True, max_seconds=60)
    try:
        clock[0] = 1041; relay._lifetime_step()
        assert relay.status()['renewals'] == 1
        clock[0] = 1082; relay._lifetime_step()
        assert relay.status()['renewal_stopped'] == 'relay_renewal_cap_reached'
    finally:
        relay.close()
