# SPDX-License-Identifier: AGPL-3.0-only
"""The CONCURRENT pump is the one the app actually runs.

Rounds 39 and 40 regression: ``ConcurrentTargetControlPump`` overrides ``step()``
and reaches ``_replay`` only through ``_finish_outbound``.  The expired-envelope
sentinel added to the base class was therefore subscripted as a mapping and raised
``TypeError: 'object' is not subscriptable``, which killed the control loop and
wedged the node -- every later owner command expired unanswered.

The base-pump tests passed throughout, because they exercised ``TargetControlPump``.
These tests exercise the subclass, so the same class of gap cannot recur silently.
"""
from contextlib import contextmanager

import pytest

import mycelium_a13_macos.control_pump as pump_module
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.control_pump import ControlPumpError
from mycelium_a13_macos.managed_control import ManagedControlError
from test_managed_control import (
    RUN_ID,
    DEPLOYMENT_ID,
    _enqueue,
    _world as _separate_world,
)
from test_worker import worker


@contextmanager
def _world(path):
    with _separate_world(path) as world:
        authority = world['authority']
        common = dict(
            base_url=authority.seed_server.base_url,
            trusted_owner_key_record=world['owner_signer'].public_key_record(),
            swarm_id=authority.coordinator.swarm_id,
            owner_node_id=authority.coordinator.seed_node_id,
            ca_certificate=authority.seed_server.material.ca_certificate,
        )
        world['owner_client'] = pump_module.ManagedControlClient.for_local_tls(
            signer=world['owner_signer'], **common
        )
        world['target_client'] = pump_module.ManagedControlClient.for_local_tls(
            signer=world['target_signer'], **common
        )
        yield world


def _pump(world, tmp_path):
    root = tmp_path / 'native'
    root.mkdir()
    w = worker(root, node_id=world['member']['node_id'], run_id=RUN_ID,
               deployment_id=DEPLOYMENT_ID)
    pump = ConcurrentTargetControlPump(
        tmp_path / 'pump', client=world['target_client'], worker=w,
        current_member=lambda: world['authority'].coordinator.member(
            world['member']['node_id']
        ),
    )
    return pump, w


def test_concurrent_pump_survives_an_expired_envelope(tmp_path, monkeypatch):
    """The regression itself: no TypeError, no wedged loop, and real recovery."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            real_replay = pump_module.replay
            seen = {}

            def expire_once(client, prepared, *, target):
                if not seen:
                    seen['first'] = prepared['envelope']['statement']['nonce']
                    raise ManagedControlError('managed_control_message_expired')
                seen['second'] = prepared['envelope']['statement']['nonce']
                return real_replay(client, prepared, target=target)

            monkeypatch.setattr(pump_module, 'replay', expire_once)
            result = pump.step()

            # Recoverable status -- not a TypeError, and not a raise.
            assert result['state'] == 'outbound_expired_requeued'
            assert pump._state['outbound'] is None

            # A fresh envelope is signed and the node keeps working.
            _enqueue(world, 'native-hello')
            assert pump.step() == {'state': 'dispatched', 'command_id': 'native-hello'}
            assert seen['second'] != seen['first']
            # step() is nonblocking: a poll count is not a native-completion
            # witness. Observe the actual dispatch, then collect and ack it.
            native = pump._futures['native-hello'].result(timeout=2)
            completed = pump.step()
            assert completed['state'] == 'completed'
            assert completed['command_id'] == 'native-hello'
            assert completed['native_response'] == native
            assert native['ok'] is True
            assert w.status()['command_count'] == 1
            assert world['authority'].control_mailbox.command_receipt('native-hello')['state'] == 'completed'
        finally:
            pump.close()
            w.close()


def test_concurrent_pump_still_fails_closed_on_other_errors(tmp_path, monkeypatch):
    """The guard must not weaken fail-closed handling of genuine uncertainty."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            calls = {'n': 0}

            def flaky(client, prepared, *, target):
                calls['n'] += 1
                raise RuntimeError('connection reset')

            monkeypatch.setattr(pump_module, 'replay', flaky)
            with pytest.raises(ControlPumpError):
                pump.step()
            assert pump._state['outbound'] is not None
            assert calls['n'] == 1
        finally:
            pump.close()
            w.close()
