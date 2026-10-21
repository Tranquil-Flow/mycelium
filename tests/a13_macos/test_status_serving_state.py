# SPDX-License-Identifier: AGPL-3.0-only
"""PV30: ``/api/status`` ``serving.state`` follows the owner run and its route readiness (it was a constant).

The UI and the directory heartbeat must agree: a run that serves shows ``serving`` with ``route_ready`` true; a run still
being prepared shows ``starting``; a run on its way down ``stopping``; a failed run ``failed`` with its error code;
nothing running keeps the old ``not_yet_serving``.  ``waiting_for_relay`` (members joined, no relay) still wins.  The
owner run is asked once per status, not twice.
"""
from mycelium_a13_macos.app import DemoApp

import pytest


class Run:
    def __init__(self, **view):
        self.view = {'state': 'idle', 'route_ready': False, 'registry_origin': None, 'error_code': None,
                     'failed_step': None, 'step': None, 'participants': None, **view}
        self.calls = 0

    def status(self):
        self.calls += 1
        return dict(self.view)

    def active(self):
        return self.view['state'] in {'running', 'serving', 'stopping', 'failing'}


@pytest.fixture
def app(tmp_path):
    instance = DemoApp(tmp_path / 'app', check_platform=False).start()
    yield instance
    instance.close()


def serving(app):
    return app.status()['serving']


def test_nothing_running_keeps_not_yet_serving(app):
    assert serving(app)['state'] == 'not_yet_serving' and serving(app)['route_ready'] is False


@pytest.mark.parametrize('run_state,expected', [('running', 'starting'), ('stopping', 'stopping'),
                                                 ('failing', 'stopping'), ('stopped', 'not_yet_serving'),
                                                 ('idle', 'not_yet_serving')])
def test_the_run_state_is_reflected(app, run_state, expected):
    app.owner_run = Run(state=run_state)
    value = serving(app)
    assert value['state'] == expected and value['route_ready'] is False


def test_a_serving_run_with_a_ready_route_is_serving(app):
    app.owner_run = Run(state='serving', route_ready=True, registry_origin='http://127.0.0.1:1')
    value = serving(app)
    assert value['state'] == 'serving' and value['route_ready'] is True


def test_a_serving_run_without_a_registry_is_not_ready(app):
    app.owner_run = Run(state='serving', route_ready=True, registry_origin=None)
    value = serving(app)
    assert value['state'] == 'starting' and value['route_ready'] is False


def test_a_failed_run_names_its_error(app):
    app.owner_run = Run(state='failed', error_code='member_restarted', failed_step='serving')
    value = serving(app)
    assert (value['state'], value['route_ready'], value['error_code']) == ('failed', False, 'member_restarted')


def test_the_serving_block_adds_no_extra_owner_run_poll(app):
    # status() already polls the run for owner_run, the chat card and the swarm card (3); the serving block reuses
    # the owner_run view instead of polling (and ticking the monitor) a fourth time.
    run = Run(state='serving', route_ready=True, registry_origin='http://127.0.0.1:1')
    app.owner_run = run
    app.status()
    assert run.calls <= 3


def test_waiting_for_relay_still_wins(app, monkeypatch):
    monkeypatch.setattr(DemoApp, '_relay_wait_status',
                        lambda self: {'state': 'waiting_for_relay', 'members_joined': 2})
    app.owner_run = Run(state='running')
    assert serving(app)['state'] == 'waiting_for_relay'
