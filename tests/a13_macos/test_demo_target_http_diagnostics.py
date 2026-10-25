# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded HTTP identity on local demo status, never transport material."""
import json
import threading
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.app import _demo_status
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.run_exchange import DemoExchangeError
from test_demo_target import world, announcement, node_plan


def test_slice_worker_retains_bounded_http_identity_until_recovery(world, monkeypatch):
    app, agent, gate, state, calls = world
    def fail(*args, **kwargs):
        raise ManagedControlError('managed_control_remote_error', status=504,
                                  request_path='/managed-control/model-source/read')
    monkeypatch.setattr('mycelium_a13_macos.slice_acquisition.fetch_node_slice', fail)
    agent._slice_transport = lambda: object()  # no real byte-source access
    state['a'] = announcement('collect')
    agent.tick()
    state['a'] = announcement('work', node_slice_plan=node_plan())
    agent.tick()
    agent.join_slice(5)
    for _ in range(2):
        result = agent.tick()
        assert result['state'] == 'error'
        assert result['error_http'] == {'status': 504, 'path': '/managed-control/model-source/read',
                                        'code': 'managed_control_remote_error'}
    state['a'] = None
    assert agent.tick()['error_http'] is None


@pytest.mark.parametrize('where', ['source', 'step'])
@pytest.mark.parametrize('local', [False, True])
@pytest.mark.parametrize('error_type', [ManagedControlError, DemoExchangeError])
def test_http_error_identity_survives_status_and_clears(world, monkeypatch, where, local, error_type):
    app, agent, gate, state, calls = world
    agent.local = local
    app.installation_role = 'owner' if local else 'target'
    if local:
        app.owner_run = SimpleNamespace(chain=SimpleNamespace(agent=agent))
    else:
        app.demo_target = agent
    error = error_type('managed_control_remote_error', status=503,
                      request_path='/managed-control/staging/read')
    def fail(*args):
        raise error
    monkeypatch.setattr(agent, 'source' if where == 'source' else '_step', fail)
    status = agent.tick()
    expected = {'status': 503, 'path': '/managed-control/staging/read',
                'code': 'managed_control_remote_error'}
    assert status['state'] == 'error'
    assert status['detail'] == expected['code']
    assert status['error_http'] == expected
    assert _demo_status(app, 'target')['error_http'] == expected
    status['error_http']['status'] = 599
    assert agent.status()['error_http'] == expected
    monkeypatch.setattr(agent, 'source', lambda: None)
    monkeypatch.setattr(agent, '_step', lambda a: agent._set('idle'))
    assert agent.tick()['error_http'] is None


@pytest.mark.parametrize('status,path,expected_status,expected_path', [
    (530, None, 530, None),
    (None, '/managed-control/staging/cleanup/read', None, '/managed-control/staging/cleanup/read'),
    (True, '/managed-control/staging/ack', None, '/managed-control/staging/ack'),
    ('503', '/managed-control/staging/cleanup/ack', None, '/managed-control/staging/cleanup/ack'),
    (999, '/managed-control/demo-run/read', None, '/managed-control/demo-run/read'),
    (503, 'https://user:PRIVATE@host/managed-control/poll', 503, None),
    (503, '/managed-control/poll?token=PRIVATE', 503, None),
    (503, '/managed-control/PRIVATE', 503, None),
    (503, {'body': 'PRIVATE'}, 503, None),
])
def test_http_identity_is_typed_allowlisted_and_bounded(world, monkeypatch, status, path,
                                                       expected_status, expected_path):
    app, agent, *_ = world
    def fail():
        raise ManagedControlError('PRIVATE response body or token', status=status, request_path=path)
    monkeypatch.setattr(agent, 'source', fail)
    result = agent.tick()
    assert result['error_http'] == {'status': expected_status, 'path': expected_path,
                                    'code': 'demo_announcement_unavailable'}
    assert 'PRIVATE' not in json.dumps(result)


def test_owner_status_does_not_wait_for_local_agent_io(world, monkeypatch):
    app, agent, *_ = world
    app.installation_role = 'owner'
    app.owner_run = SimpleNamespace(chain=SimpleNamespace(agent=agent))
    entered, release = threading.Event(), threading.Event()
    def held_source():
        entered.set()
        assert release.wait(5)
        return None
    monkeypatch.setattr(agent, 'source', held_source)
    worker = threading.Thread(target=agent.tick)
    answers = []
    reader = threading.Thread(target=lambda: answers.append(_demo_status(app, 'target')))
    worker.start()
    try:
        assert entered.wait(2)
        reader.start()
        reader.join(.2)
        assert answers and answers[0]['state'] == 'idle'
    finally:
        release.set()
        worker.join(5)
        if reader.ident is not None:
            reader.join(5)
    assert not worker.is_alive() and not reader.is_alive()


def test_non_http_error_replaces_previous_http_identity(world, monkeypatch):
    app, agent, *_ = world
    errors = iter([ManagedControlError('managed_control_remote_error', status=502),
                   ValueError('demo_step_failed'), ManagedControlError('managed_control_unreachable')])
    def fail():
        raise next(errors)
    monkeypatch.setattr(agent, 'source', fail)
    assert agent.tick()['error_http']['status'] == 502
    assert agent.tick()['error_http'] is None
    assert agent.tick()['error_http'] is None
