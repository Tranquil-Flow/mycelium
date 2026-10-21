# SPDX-License-Identifier: AGPL-3.0-only
"""Owner consumes actual native output over canonical TLS, not a DB shortcut."""
import copy
import pytest

from test_control_pump import _world
from test_managed_control import _command, _enqueue, RUN_ID, DEPLOYMENT_ID
from test_worker import worker
from mycelium_a13_macos.control_pump import TargetControlPump
from mycelium_a13_macos.managed_control import ManagedControlError


def result(world, command_id, sequence=2, *, client=None, **command_changes):
    command = _command(command_id)
    command.update(command_changes)
    return (client or world['owner_client']).result(
        target=world['member'], native_command=command, sequence=sequence,
        nonce=f'result-nonce-{sequence}', request_id=f'result-request-{sequence}')


def test_owner_gets_actual_native_response_and_cached_request_is_a_snapshot(tmp_path):
    with _world(tmp_path/'swarm') as world:
        root = tmp_path/'native'; root.mkdir()
        w = worker(root, node_id=world['member']['node_id'], run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
        pump = TargetControlPump(tmp_path/'pump', client=world['target_client'], worker=w,
            current_member=lambda: world['authority'].coordinator.member(world['member']['node_id']))
        try:
            w.start(); _enqueue(world, 'actual-hello')
            pending = result(world, 'actual-hello')
            assert pending['state'] == 'pending'
            assert pending['native_response'] is None
            actual = pump.step()
            received = result(world, 'actual-hello', 3)
            assert received['state'] == 'completed'
            assert received['native_response'] == actual['native_response']
            assert received['native_response']['route_ready'] is False
            assert received['native_response']['result']
            assert w.status()['command_count'] == 1
            # Identical request replay is a snapshot, never a new observation.
            assert result(world, 'actual-hello', 2) == pending
            assert 'claim_token' not in received
        finally:
            pump.close(); w.close()
        assert not w.process_group_present()


def test_result_is_owner_only_and_binds_exact_command_and_membership(tmp_path):
    with _world(tmp_path/'swarm') as world:
        _enqueue(world, 'bound')
        with pytest.raises(ManagedControlError):
            result(world, 'bound', client=world['target_client'])
        with pytest.raises(ManagedControlError, match='command_binding_mismatch'):
            result(world, 'bound', command='health')
        with pytest.raises(ManagedControlError, match='command_unknown'):
            result(world, 'absent', 3)
        assert result(world, 'bound', 4)['state'] == 'pending'
        world['authority'].revoke(world['member']['node_id'], world['member']['generation'])
        with pytest.raises(ManagedControlError, match='member_stale'):
            result(world, 'bound', 5)


def test_result_body_tamper_is_rejected(tmp_path, monkeypatch):
    with _world(tmp_path/'swarm') as world:
        _enqueue(world, 'bound')
        client = world['owner_client']; original = client._request
        def tamper(path, envelope):
            reply = copy.deepcopy(original(path, envelope))
            reply['statement']['native_response'] = {'ok': True}
            return reply
        monkeypatch.setattr(client, '_request', tamper)
        with pytest.raises(ManagedControlError, match='signature_invalid'):
            result(world, 'bound')
