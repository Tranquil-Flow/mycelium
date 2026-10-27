# SPDX-License-Identifier: AGPL-3.0-only
"""Signed cancellation retirement is not a receipt ACK or cleanup authority."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.token_progress_pump import PumpTokenProgress
from mycelium_router.token_path import TokenBatch
from test_managed_control import _world
from test_token_receipt_mailbox import provision, send
from tests.live.test_token_path_admission import world

CANCELLED = 'mycelium.managed_control.token_progress_cancelled.v1'


@pytest.mark.parametrize('cancelled', [False, True])
def test_owner_marks_only_its_authorized_cancellation(world, monkeypatch, cancelled):
    from tests.live.test_token_path_owner import owner, prepare
    path, box, _ = owner(world)
    attempt = prepare(path, world)
    closed = []
    def close(command_id, **kwargs):
        closed.append(kwargs.get('cancelled', False))
        box.stream.close()
    monkeypatch.setattr(box, 'close_token_progress', close)
    def refused(_):
        raise RuntimeError('native_start_refused')
    with pytest.raises(RuntimeError, match='request_cancelled' if cancelled else 'native_start_refused'):
        attempt.run(send=refused, consume=lambda _: None, cancelled=lambda: cancelled)
    assert closed == [cancelled]


@pytest.mark.parametrize('authority', ['unproven', 'already-authorized'])
def test_owner_finalizer_closes_on_authority_failure_without_rechecking_authorized_cancel(world, monkeypatch, authority):
    from tests.live.test_token_path_owner import owner, prepare
    path, box, _ = owner(world)
    attempt = prepare(path, world)
    closed, checks = [], []
    def close(command_id, **kwargs):
        closed.append(kwargs.get('cancelled', False))
        box.stream.close()
    def cancelled():
        checks.append(True)
        if authority == 'already-authorized' and len(checks) == 1:
            return True
        raise RuntimeError('cancellation_authority_unavailable')
    def forbidden(_):
        pytest.fail('unadmitted start must not execute')
    monkeypatch.setattr(box, 'close_token_progress', close)
    code = 'request_cancelled' if authority == 'already-authorized' else 'cancellation_authority_unavailable'
    with pytest.raises(RuntimeError, match=code):
        attempt.run(send=forbidden, consume=lambda _: None, cancelled=cancelled)
    assert closed == [authority == 'already-authorized']
    if authority == 'already-authorized':
        assert len(checks) == 1


def test_unsigned_disabled_error_never_authorizes_progress_retirement(tmp_path, monkeypatch):
    from mycelium_a13_macos import control_pump
    from mycelium_a13_macos.control_pump import TargetControlPump, ControlPumpError
    pump = object.__new__(TargetControlPump)
    packet = {'operation': 'token_progress'}
    pump._state = {'outbound': packet}
    pump.client = object()
    def rejected(*args, **kwargs):
        raise ManagedControlError('token_progress_disabled')
    monkeypatch.setattr(control_pump, 'replay', rejected)
    with pytest.raises(ControlPumpError, match='control_token_progress_reconciliation_required') as error:
        pump._replay({})
    assert error.value.cause_code == 'token_progress_disabled'
    assert pump._state['outbound'] is packet



@pytest.mark.parametrize('ordering', ['before-lookup', 'before-accept'])
def test_cancelled_stream_returns_bound_signed_retirement_not_receipt_ack(tmp_path, world, monkeypatch, ordering):
    with _world(tmp_path/'swarm') as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        if ordering == 'before-lookup':
            a['mailbox'].close_token_progress('grant-start', cancelled=True)
        else:
            accept = stream.accept
            def cancelled_accept(receipt):
                a['mailbox'].close_token_progress('grant-start', cancelled=True)
                return accept(receipt)
            monkeypatch.setattr(stream, 'accept', cancelled_accept)
        result = send(a, claim, receipt, return_envelope=True)
        assert result['statement']['protocol'] == CANCELLED
        assert result['statement']['command_id'] == 'grant-start'
        assert result['statement']['grant_digest'] == journal.grant.digest
        assert result['statement']['receipt_digest'] == receipt['signature']['signed_statement_digest']
        assert send(a, claim, receipt, return_envelope=True) == result
        assert not sealed
        assert a['mailbox'].command_receipt('grant-start')['state'] == 'claimed'
        from mycelium_node.token_ack import NativeReceiptJournals
        from mycelium_router.token_path_authority import TokenPathAuthorityError
        native_journals = NativeReceiptJournals()
        native_journals.register(command_id='grant-start', journal=journal,
                                owner_key=a['owner_signer'].public_key_record())
        with pytest.raises(TokenPathAuthorityError, match='token_path_receipt_ack'):
            native_journals.acknowledge(result)
        assert native_journals.active_count == 1
        with pytest.raises(RuntimeError, match='token_progress_closed'):
            stream.read(timeout=0)
        # No authority reconstructed from the command DB after owner restart.
        from mycelium_a13_macos.managed_control import ManagedControlMailbox
        fresh = ManagedControlMailbox(a['database'], coordinator=a['authority'].coordinator)
        try:
            client = a['target_client']
            old = client._request
            client._request = lambda path, envelope: fresh.token_progress(envelope)
            with pytest.raises(ManagedControlError, match='token_progress_disabled'):
                send(a, claim, receipt, sequence=3)
            client._request = old
        finally:
            fresh.close()


@pytest.mark.parametrize('field', ['statement', 'signature'])
def test_cancelled_progress_rejects_malformed_receipt_with_typed_refusal(tmp_path, world, field):
    with _world(tmp_path) as a:
        _, journal, _, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        a['mailbox'].close_token_progress('grant-start', cancelled=True)
        receipt[field] = []
        with pytest.raises(ManagedControlError, match='token_progress_subject_mismatch'):
            send(a, claim, receipt)


@pytest.mark.parametrize('fault', ['claim', 'grant', 'command', 'generation'])
def test_cancelled_stream_still_requires_exact_live_claim(tmp_path, world, fault):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        a['mailbox'].close_token_progress('grant-start', cancelled=True)
        overrides = {}
        if fault == 'claim':
            claim = 'wrong-claim'
        elif fault == 'grant':
            receipt['statement']['grant_digest'] = 'sha256:'+'a'*64
            receipt['signature'] = world.entry.sign(receipt['statement'])
        elif fault == 'command':
            overrides['command_id'] = 'other-start'
        else:
            a['authority'].coordinator.advance_member_generation(
                node_id=a['member']['node_id'], expected_generation=a['member']['generation'],
                lifecycle_state='STOPPED')
        with pytest.raises(ManagedControlError):
            send(a, claim, receipt, **overrides)
        assert not sealed


@pytest.mark.parametrize('fault', ['signature', 'request_id', 'command_id', 'grant_digest', 'receipt_digest', 'receipt_sequence'])
def test_client_rejects_forged_or_transplanted_retirement(tmp_path, world, monkeypatch, fault):
    with _world(tmp_path) as a:
        _, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        a['mailbox'].close_token_progress('grant-start', cancelled=True)
        request = a['target_client']._request
        def corrupt(path, envelope):
            response = deepcopy(request(path, envelope))
            field = 'command_id' if fault == 'signature' else fault
            response['statement'][field] = 99 if field == 'receipt_sequence' else 'wrong'
            if fault != 'signature':
                response['signature'] = a['owner_signer'].sign(response['statement'])
            return response
        monkeypatch.setattr(a['target_client'], '_request', corrupt)
        with pytest.raises(ManagedControlError):
            send(a, claim, receipt)
        assert not sealed


def test_bridge_durably_retires_progress_without_native_ack_or_terminal(tmp_path, world):
    with _world(tmp_path/'swarm') as a:
        _, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        a['mailbox'].close_token_progress('grant-start', cancelled=True)
        result = send(a, claim, receipt, return_envelope=True)
        root = tmp_path/'pump'; root.mkdir()
        calls = []
        def forbidden(**kwargs):
            pytest.fail('retirement must never evict native receipt evidence')
        pump = SimpleNamespace(root=root, worker=SimpleNamespace(send_before=forbidden,
            close_progress=calls.append), _state={'outbound': 'pending'}, _save=lambda: None,
            _claims_state={'claims': {'grant-start': {'phase': 'executing', 'native_response': None}}})
        bridge = PumpTokenProgress(pump, None)
        bridge.state['rows']['grant-start'] = {'pending': receipt, 'owner_ack': None,
                                              'terminal': False, 'error': None}
        packet = {'envelope': {'statement': {'command_id': 'grant-start', 'receipt': receipt}}}
        assert bridge.accepted(packet, result)['state'] == 'token_progress_cancelled'
        assert pump._state['outbound'] is None
        assert calls == ['grant-start']
        for current in (bridge, PumpTokenProgress(pump, None)):
            assert current.next({}) is None
            assert current.state['rows']['grant-start']['terminal'] is False
            assert not current.ready_to_ack('grant-start')
            pump._claims_state['claims']['grant-start'].update(
                phase='executed', native_response={'ok': False, 'error': {'code': 'stale_infer_decode_generation'}})
            assert current.ready_to_ack('grant-start')
            pump._claims_state['claims']['grant-start'].update(phase='executing', native_response=None)
        assert not sealed
        assert a['mailbox'].command_receipt('grant-start')['state'] == 'claimed'
