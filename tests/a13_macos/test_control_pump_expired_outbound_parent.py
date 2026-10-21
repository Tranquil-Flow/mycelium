# SPDX-License-Identifier: AGPL-3.0-only
"""An expired durable control envelope must recover, never wedge the target.

Round38 regression: one transient replay failure left a signed ``target_poll``
envelope in durable state past its 60 s validity.  ``_replay`` mapped *every*
failure to ``ControlPumpError``, ``step()`` raised, and ``managed_runtime._serve``
catches any exception and exits the control loop for good.  The node was then
permanently ``blocked``: it claimed nothing further, and 99 of 102 owner
``snapshot`` commands expired unanswered -- which is how the request gateway lost
all observation of the peer and no token event ever reached the SSE stream.

An envelope whose signed validity has passed can never be accepted, so replaying
it is provably useless and dropping it loses nothing.  These tests pin that
recovery, and pin the fail-closed behaviour that must NOT change.
"""
from contextlib import contextmanager

import pytest

import mycelium_a13_macos.control_pump as pump_module
from mycelium_a13_macos.control_pump import ControlPumpError, TargetControlPump
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
    pump = TargetControlPump(
        tmp_path / 'pump', client=world['target_client'], worker=w,
        current_member=lambda: world['authority'].coordinator.member(
            world['member']['node_id']
        ),
    )
    return pump, w


def test_expired_poll_envelope_recovers_instead_of_wedging(tmp_path, monkeypatch):
    """The round38 wedge: an expired poll must not raise out of step()."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            real_replay = pump_module.replay
            seen = {}

            def expire_once(client, prepared, *, target):
                if not seen:
                    seen['envelope'] = prepared
                    raise ManagedControlError('managed_control_message_expired')
                return real_replay(client, prepared, target=target)

            monkeypatch.setattr(pump_module, 'replay', expire_once)
            result = pump.step()

            # Recovered, not raised: raising here is what killed the loop.
            assert result['state'] == 'outbound_expired_requeued'
            assert pump._state['outbound'] is None

            # And the node makes real progress on the very next step.
            _enqueue(world, 'native-hello')
            second = pump.step()
            assert second['state'] == 'completed'
            assert second['native_response']['ok'] is True
            assert second['native_response']['result']
        finally:
            pump.close()
            w.close()


def test_reshipped_envelope_is_fresh_not_the_dead_one(tmp_path, monkeypatch):
    """Recovery must re-sign, never replay the envelope that cannot be accepted."""
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
            pump.step()
            pump.step()
            assert 'second' in seen
            assert seen['second'] != seen['first']
        finally:
            pump.close()
            w.close()


def test_non_expiry_replay_failure_still_fails_closed(tmp_path, monkeypatch):
    """Fail-closed on genuine transport uncertainty must NOT be weakened."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()

            def network_down(client, prepared, *, target):
                raise RuntimeError('connection reset')

            monkeypatch.setattr(pump_module, 'replay', network_down)
            with pytest.raises(ControlPumpError) as info:
                pump.step()
            assert str(info.value) == 'control_poll_reconciliation_required'
            # The exact durable outbound must survive for a later retry.
            assert pump._state['outbound'] is not None
        finally:
            pump.close()
            w.close()


def test_expired_ack_recovers_and_keeps_the_native_response(tmp_path, monkeypatch):
    """The ack path is the second _replay call site and must recover too."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            _enqueue(world, 'native-hello')
            real_replay = pump_module.replay
            state = {'expired': False}

            def expire_on_ack(client, prepared, *, target):
                if prepared['operation'] == 'ack' and not state['expired']:
                    state['expired'] = True
                    raise ManagedControlError('managed_control_message_expired')
                return real_replay(client, prepared, target=target)

            monkeypatch.setattr(pump_module, 'replay', expire_on_ack)
            result = pump.step()
            assert result['state'] == 'ack_expired_requeued'
            # The executed native response stays durable so a fresh ack can carry it.
            assert pump._state['pending'] is not None
            assert pump._state['pending']['native_response']['ok'] is True

            # A later step re-prepares the ack and finishes the exchange.
            monkeypatch.setattr(pump_module, 'replay', real_replay)
            done = pump.step()
            assert done['state'] == 'completed'
            assert done['native_response']['ok'] is True
        finally:
            pump.close()
            w.close()
