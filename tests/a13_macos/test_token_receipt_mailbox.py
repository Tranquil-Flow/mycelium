# SPDX-License-Identifier: AGPL-3.0-only
"""Real pinned TLS mailbox receipt transport, no worker/model execution."""
from copy import deepcopy
import json

import pytest

from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptCursor, ReceiptJournal
from tests.live.test_token_path_admission import world, policy, payload
from test_managed_control import _world, _enqueue, RUN_ID, DEPLOYMENT_ID


def provision(w, authority, *, capacity=64, seal=None, native_endpoint=False):
    m = authority['member']
    w.owner, w.entry = authority['owner_signer'], authority['target_signer']
    if native_endpoint:
        from dataclasses import replace
        w.entry = replace(w.entry, endpoint_id=w.entry._public_key_bytes.hex())
    w.pins.update(run_id=RUN_ID, owner_endpoint_id=w.owner.endpoint_id,
        owner_key_digest=w.owner.verification_key_digest, entry_node_id=m['node_id'],
        entry_endpoint_id=w.entry.endpoint_id, entry_key_digest=w.entry.verification_key_digest,
        participants=[dict(node_id=m['node_id'], endpoint_id=w.entry.endpoint_id, generation=m['generation'])])
    w.control['deployment_id'] = DEPLOYMENT_ID
    w.grant['statement']['scope'] = {**deepcopy(w.pins), 'request_id': 'request',
        'control': deepcopy(w.control), 'request_digest': sha256_bytes(canonical_json_bytes(w.request))}
    w.grant['signature'] = w.owner.sign(w.grant['statement'])
    admitted = policy(w).admit(w.grant, request=w.request, bound_control=w.control)
    cursor = ReceiptCursor(grant=admitted, entry_key=w.entry.public_key_record(),
                           clock=lambda: w.now, current=lambda: not w.cancelled)
    journal = ReceiptJournal(grant=admitted, signer=w.entry,
                             clock=lambda: w.now, current=lambda: not w.cancelled)
    sealed = []
    stream = authority['mailbox'].open_token_progress(command_id='grant-start', cursor=cursor,
        seal=sealed.append if seal is None else seal, capacity=capacity,
        **({'target_endpoint_id':m['endpoint_id']} if native_endpoint else {}))
    _enqueue(authority, 'grant-start', operation='infer_start', payload=payload(w))
    claimed = authority['target_client'].poll(target=m, run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID, sequence=1, nonce='poll-nonce',
        request_id='poll-request', active_claims={})
    return stream, journal, sealed, claimed['claim_token']


def send(a, claim, receipt, *, sequence=2, **overrides):
    args = dict(target=a['member'], run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
        command_id='grant-start', claim_token=claim, receipt=receipt,
        sequence=sequence, nonce=f'progress-nonce-{sequence}', request_id=f'progress-{sequence}')
    args.update(overrides)
    return a['target_client'].token_progress(**args)


def test_token_receipt_obeys_mailbox_before_member_lock_order(tmp_path, world, monkeypatch):
    with _world(tmp_path) as a:
        stream,journal,sealed,claim=provision(world,a)
        box=a['mailbox']; lock=box._lock; member_lock=a['authority'].coordinator._lock
        class LockOrderAudit:
            def __enter__(self):
                assert lock._is_owned() or not member_lock._is_owned(), 'mailbox_after_member_lock_inversion'
                return lock.__enter__()
            def __exit__(self,*args):return lock.__exit__(*args)
        monkeypatch.setattr(box,'_lock',LockOrderAudit())
        # Real validators/claims/signatures, direct transport for a synchronous
        # assertion rather than deadlocking an HTTP thread during behavioral RED.
        monkeypatch.setattr(a['target_client'],'_request',lambda path,envelope:box.token_progress(envelope))
        first=journal.append(TokenBatch(0,0,(10,),'DECODING'))
        ack=send(a,claim,first)
        assert ack['receipt_sequence']==0 and stream.read(timeout=0).token_ids==(10,)
        assert len(sealed)==1


def test_tls_receipt_verified_sealed_and_acked_before_command_completion(tmp_path, world):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10, 11), 'DECODING'))
        ack = send(a, claim, receipt)
        assert len(sealed) == 1 and sealed[0] == canonical_json_bytes(receipt)
        verified = stream.read(timeout=0)
        assert verified.token_ids == (10, 11)
        assert verified.envelope_bytes == sealed[0]
        journal.acknowledge(ack['receipt_sequence'], ack['receipt_digest'])
        assert not journal.read(after_sequence=0)
        # Exact signed request retry and newly signed receipt retry never redeliver.
        assert send(a, claim, receipt) == ack
        send(a, claim, receipt, sequence=3)
        assert len(sealed) == 1
        with pytest.raises(TimeoutError):
            stream.read(timeout=0)
        terminal = journal.append(TokenBatch(1, 2, (), 'COMPLETED'))
        send(a, claim, terminal, sequence=4)
        assert stream.read(timeout=0).status == 'COMPLETED'
        assert a['mailbox'].command_receipt('grant-start')['state'] == 'claimed'
        a['mailbox'].close_token_progress('grant-start')


@pytest.mark.parametrize('mutation', ['claim', 'signature', 'grant', 'gap', 'cancel', 'expiry'])
def test_bad_progress_cannot_seal_ack_or_return_tokens(tmp_path, world, mutation):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        if mutation == 'claim':
            claim = 'wrong-claim'
        elif mutation == 'signature':
            receipt['statement']['token_ids'] = [55]
        elif mutation in {'grant', 'gap'}:
            receipt['statement']['grant_digest' if mutation == 'grant' else 'sequence'] = (
                'sha256:'+'a'*64 if mutation == 'grant' else 1)
            receipt['signature'] = world.entry.sign(receipt['statement'])
        elif mutation == 'cancel':
            world.cancelled = True
        else:
            world.now = 16.
        with pytest.raises(ManagedControlError):
            send(a, claim, receipt)
        assert not sealed
        a['mailbox'].close_token_progress('grant-start')


def test_seal_failure_and_queue_overflow_fail_closed(tmp_path, world):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a, capacity=1)
        first = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        send(a, claim, first)
        second = journal.append(TokenBatch(1, 1, (11,), 'DECODING'))
        with pytest.raises(ManagedControlError):
            send(a, claim, second, sequence=3)
        assert len(sealed) == 1
        with pytest.raises(RuntimeError, match='token_progress_overflow'):
            stream.read(timeout=0)


def test_seal_must_finish_before_ack_and_failure_poison_is_sticky(tmp_path, world):
    def fail(_):
        raise OSError('injected-seal-failure')
    with _world(tmp_path) as a:
        stream, journal, _, claim = provision(world, a, seal=fail)
        first = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        with pytest.raises(ManagedControlError):
            send(a, claim, first)
        with pytest.raises(RuntimeError, match='token_progress_seal_failed'):
            stream.read(timeout=0)


@pytest.mark.parametrize('race', ['membership', 'command'])
def test_sealing_race_cannot_expose_unacked_tokens(tmp_path, world, race):
    with _world(tmp_path) as a:
        def seal(_):
            if race == 'membership':
                a['authority'].coordinator.advance_member_generation(
                    node_id=a['member']['node_id'], expected_generation=a['member']['generation'],
                    lifecycle_state='STOPPED')
            else:
                # A concurrent terminal/reconciliation transaction overtakes sealing.
                import sqlite3
                with sqlite3.connect(a['database']) as db:
                    db.execute("UPDATE managed_commands SET state='reconciliation_required' WHERE command_id='grant-start'")
        stream, journal, _, claim = provision(world, a, seal=seal)
        first = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        with pytest.raises(ManagedControlError):
            send(a, claim, first)
        with pytest.raises(RuntimeError, match='token_progress_forward_failed'):
            stream.read(timeout=0)


def test_closed_stream_and_mailbox_restart_never_resurrect_authority(tmp_path, world):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a)
        first = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        a['mailbox'].close_token_progress('grant-start')
        with pytest.raises(ManagedControlError):
            send(a, claim, first)
        assert not sealed
        with pytest.raises(RuntimeError, match='token_progress_closed'):
            stream.read(timeout=0)
        from mycelium_a13_macos.managed_control import ManagedControlMailbox
        fresh = ManagedControlMailbox(a['database'], coordinator=a['authority'].coordinator)
        assert not fresh._token_progress_streams
        fresh.close()
