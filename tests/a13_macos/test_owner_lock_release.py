# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 items b + i: a finished or failed owner run releases ``authority/managed-control/owner/owner.lock``.

Live demo (2026-10-20/21): the route's close raised ``router_port_shutdown_cleanup_unproven``; the app process kept the
``ManagedControlOwner`` flock of the previous run, so ``/api/demo/stop`` then ``/api/demo/start`` answered
``managed_owner_already_running`` until the app was quit.  The unproven cleanup stays recorded (run error code, status
``cleanup_unproven``) and is never hidden; only the local flock is released.
"""
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import chain_compile
from mycelium_a13_macos.chain_observation import ObservationContext
from mycelium_a13_macos.control_session import ManagedControlOwner
from physical_inference_qualification import ControllerError
from test_demo_chain import Context, LOCAL, REMOTE, make, wait
from test_owner_quota_recovery import _world


def test_closing_the_observation_context_releases_the_owner_lock(tmp_path):
    _member, client, first = _world(tmp_path)
    context = ObservationContext('run', 'dep', (), {}, {}, SimpleNamespace(owner=first), None)
    with pytest.raises(ControllerError, match='managed_owner_already_running'):
        ManagedControlOwner(tmp_path, client=client, current_member=lambda _n: _member)
    context.close()
    second = ManagedControlOwner(tmp_path, client=client, current_member=lambda _n: _member)  # must not raise
    second.close()


def test_closing_twice_is_harmless(tmp_path):
    _member, _client, first = _world(tmp_path)
    context = ObservationContext('run', 'dep', (), {}, {}, SimpleNamespace(owner=first), None)
    context.close()
    context.close()


class FakeOwner:
    def __init__(self): self.closed = False
    def close(self): self.closed = True


def _with_owner_channels(ops):
    """Each prepared observation gets its own owner channel, as ``owner_channel`` makes one per run."""
    owners = []
    original = ops.prepare_observation

    def prepare(app, *, run_id, deployment_id, descriptors):
        original(app, run_id=run_id, deployment_id=deployment_id, descriptors=descriptors)
        owners.append(FakeOwner())
        app._observation_context.factory = SimpleNamespace(owner=owners[-1])
    ops.prepare_observation = prepare
    return owners


def test_stop_releases_the_owner_channel_so_start_works_again(tmp_path):
    app, ops, members, run = make(tmp_path)
    owners = _with_owner_channels(ops)
    run.start(); wait(run, lambda s: s['state'] == 'serving')
    run.stop(); wait(run, lambda s: s['state'] == 'stopped')
    assert [o.closed for o in owners] == [True]
    run.start(); wait(run, lambda s: s['state'] == 'serving')
    assert len(owners) == 2 and owners[1].closed is False


def test_a_route_whose_cleanup_is_unproven_fails_the_run_visibly_and_still_releases_the_lock(tmp_path):
    app, ops, members, run = make(tmp_path)
    owners = _with_owner_channels(ops)
    run.start(); wait(run, lambda s: s['state'] == 'serving')
    workflow = app._chain_workflow

    def stuck_stop(_app):
        raise ControllerError('router_port_shutdown_cleanup_unproven')
    ops.stop = stuck_stop
    workflow.status = lambda: {'state': 'failed', 'error_code': 'router_port_shutdown_cleanup_unproven', 'startup': None}
    status = wait(run, lambda s: s['state'] in {'failed', 'stopped'})
    assert status['state'] == 'failed' and status['error_code'] == 'router_port_shutdown_cleanup_unproven'
    assert status['cleanup_unproven'] == {'code': 'router_port_shutdown_cleanup_unproven',
                                         'owner_lock_released': True,
                                         'stop_error': 'router_port_shutdown_cleanup_unproven'}
    assert owners[0].closed is True
    ops.stop = lambda _app: {'state': 'stopped'}
    run.start(); status = wait(run, lambda s: s['state'] == 'serving')
    assert status['cleanup_unproven'] is None  # reported for the run it belongs to


def test_a_failed_run_releases_the_owner_channel_too(tmp_path):
    app, ops, members, run = make(tmp_path, compile_error='boom')
    owners = _with_owner_channels(ops)
    run.start(); wait(run, lambda s: s['state'] == 'failed')
    assert owners and all(o.closed for o in owners)


def test_rebuild_releases_the_previous_owner_channel_before_preparing_the_next(tmp_path, monkeypatch):
    old = FakeOwner()
    order = []
    app = SimpleNamespace(
        installation_role='owner', running=True, guard=__import__('threading').RLock(), _chain_rebuild=None,
        _observation_context=SimpleNamespace(run_id='older', factory=SimpleNamespace(owner=old)),
        authority=SimpleNamespace(coordinator=SimpleNamespace(
            swarm_id='s', member=lambda node: {'node_id': node, 'generation': 1, 'incarnation': 'i',
                                               'verification_key_digest': 'd', 'endpoint_id': 'e'}),
            control_mailbox=SimpleNamespace(pending_offer=lambda member: None)))
    monkeypatch.setattr(chain_compile, 'verify_installation_descriptor', lambda *a, **k: {})
    monkeypatch.setattr(chain_compile, 'stop_serving', lambda *a, **k: order.append('stop'))
    monkeypatch.setattr(chain_compile, 'prepare_observation',
                        lambda *a, **k: order.append('prepare-after-close' if old.closed else 'prepare-before-close'))
    cards = [{'statement': {'target_node_id': n}, 'verification_key': {}} for n in ('a', 'b')]
    chain_compile.rebuild_with_current_members(app, acknowledged=True, run_id='run-2', deployment_id='dep',
                                               descriptors=cards)
    assert order == ['stop', 'prepare-after-close']
