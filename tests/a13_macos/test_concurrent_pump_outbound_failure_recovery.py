# SPDX-License-Identifier: AGPL-3.0-only
"""A failed outbound POLL/ACK must not be a control-loop death sentence.

Round 58 physical run `a13-qwen-20260915T231153Z` (preview.58) reached the first
successful terminal frame on the owner, but the peer's own record for the same run is

    target  runtime-control-failure.private.json =
        {"error_code": "control_poll_reconciliation_required",
         "exception_type": "ControlPumpError", "state": "blocked"}

and the registry reports 0 target operations for the streamed request, so C5's
per-placement evidence on both Macs is unmet.  That code has exactly one source:
`control_pump._replay`, which converts *any* non-expired `replay()` failure into
`control_<operation>_reconciliation_required` and (until now) discarded the cause with
`from None`.  `managed_runtime._serve` treats any exception from `step()` as terminal:
it closes the pump, records `state: blocked`, and the peer never polls again --
round 38's documented death class ("step() raised -> node went blocked -> 99/102 later
owner commands expired unanswered"), which rounds 39, 40, 41, 45, 47 and 58 each hit
through a different door.

Two of the three outcomes here are recoverable *without* relaxing anything:

* a transport/transient failure (`managed_control_unreachable`, a response timeout, a
  server 5xx, an unverifiable reply) leaves the exact signed bytes valid -- retrying the
  same envelope is the designed recovery, and a poll establishes no claim, so nothing
  native is replayed;
* a provably-unusable request (expired, or a signed-request conflict the mailbox can
  never accept: `member_stale` / `sequence_replayed` / `nonce_replayed` /
  `conflicting_retry` / `prepared_request_invalid`) must be DROPPED and re-signed with a
  fresh sequence/nonce -- the same treatment the expired case already gets.

What must NOT change: an uncoded failure still fails closed, the unknown-native-outcome
guard still raises, and no fix may raise the 2.0 s budget or publish a terminal early.
"""

from contextlib import contextmanager

import pytest

import mycelium_a13_macos.control_pump as pump_module
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.control_pump import ControlPumpError
from mycelium_a13_macos.managed_control import ManagedControlError
from test_managed_control import RUN_ID, DEPLOYMENT_ID, _enqueue, _world as _separate_world
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
        # Both clients, exactly as the sibling concurrent-pump tests build this world:
        # without the explicit owner client an enqueued command is never delivered to
        # this target's poll, so the recovery assertion below could not be exercised.
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


def _nonce(pump):
    return pump._state['outbound']['envelope']['statement']['nonce']


def test_a_transport_failed_poll_replay_retries_the_same_signed_bytes(tmp_path, monkeypatch):
    """A transport failure must be retryable, and must reuse the exact envelope.

    Re-signing here would burn a fresh sequence/nonce for a request that may still be
    in flight, and clearing the outbound would lose the only copy of the signed bytes.
    """

    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()

            def unreachable(client, prepared, *, target):
                raise ManagedControlError('managed_control_unreachable')

            monkeypatch.setattr(pump_module, 'replay', unreachable)
            result = pump.step()

            assert result['state'] == 'outbound_retryable'
            assert result['cause'] == 'managed_control_unreachable'
            assert pump._state['outbound'] is not None
            first = _nonce(pump)

            # A second attempt reuses the same signed bytes rather than minting new ones.
            again = pump.step()
            assert again['state'] == 'outbound_retryable'
            assert _nonce(pump) == first
        finally:
            pump.close()
            w.close()


def test_a_rejected_poll_request_is_dropped_and_resigned(tmp_path, monkeypatch):
    """A request the mailbox can never accept must be re-signed, not retried forever."""

    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            real_replay = pump_module.replay
            seen = {}

            def replayed_once(client, prepared, *, target):
                if not seen:
                    seen['first'] = prepared['envelope']['statement']['nonce']
                    raise ManagedControlError('managed_control_sequence_replayed')
                seen['second'] = prepared['envelope']['statement']['nonce']
                return real_replay(client, prepared, target=target)

            monkeypatch.setattr(pump_module, 'replay', replayed_once)
            result = pump.step()

            assert result['state'] == 'outbound_rejected_requeued'
            assert result['cause'] == 'managed_control_sequence_replayed'
            # The dead envelope is gone, and the node keeps working with a fresh
            # sequence/nonce instead of dying.
            assert pump._state['outbound'] is None

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


def test_an_uncoded_failure_still_fails_closed_with_its_cause_named(tmp_path, monkeypatch):
    """The terminal half, unchanged in behaviour and now self-describing.

    A corrupt own-envelope (`managed_control_signature_invalid`) is neither transient nor
    provably re-signable from here: it must keep the loop's fail-closed raise.  The cause
    code must survive, because the previous `from None` is what left round 58's peer
    blocked with no explanation of which rejection it hit.
    """

    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()

            def corrupt(client, prepared, *, target):
                raise ManagedControlError('managed_control_signature_invalid')

            monkeypatch.setattr(pump_module, 'replay', corrupt)
            with pytest.raises(ControlPumpError) as caught:
                pump.step()

            assert str(caught.value) == 'control_poll_reconciliation_required'
            assert caught.value.cause_code == 'managed_control_signature_invalid'
            # Not provably unusable, so the durable envelope is retained.
            assert pump._state['outbound'] is not None
        finally:
            pump.close()
            w.close()
