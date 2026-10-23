# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned TLS owner ACK -> native dispatch -> retained receipt journal."""
from copy import deepcopy

import pytest

from mycelium_router.token_path import TokenBatch
from tests.live.test_token_path_admission import world, node
from test_managed_control import _world, RUN_ID, DEPLOYMENT_ID
from test_token_receipt_mailbox import provision
from test_token_progress_delivery import prepared
from mycelium_a13_macos.durable_requests import replay


def native(w, a, journal, *, target_endpoint_id=None):
    from mycelium_node.token_ack import NativeReceiptJournals
    n = node(w)
    n.run_id, n.deployment_id, n.node_id = RUN_ID, DEPLOYMENT_ID, a['member']['node_id']
    n._identity = lambda: {'run_id': RUN_ID, 'node_id': n.node_id}
    n._token_journals = NativeReceiptJournals()
    n._token_journals.register(command_id='grant-start', journal=journal,
        owner_key=w.owner.public_key_record(),
        **({'target_endpoint_id':target_endpoint_id} if target_endpoint_id is not None else {}))
    return n


def dispatch(n, ack):
    return n.dispatch(dict(protocol='mycelium.physical_node_control.v1',
        command_id='ack-native', run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
        command='ack_token_progress', payload={'ack': ack}))


def test_verified_owner_ack_reaches_native_journal_and_retries_without_compute(tmp_path, world):
    with _world(tmp_path) as a:
        stream, journal, _, claim = provision(world, a)
        n = native(world, a, journal)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        ack = replay(a['target_client'], prepared(a, claim, receipt), target=a['member'])
        assert stream.read(timeout=0).token_ids == (10,)
        assert dispatch(n, ack)['observation']['event'] == 'token_progress_acknowledged'
        assert journal.read(after_sequence=0) == ()
        dispatch(n, ack)
        assert world.prefill == world.rounds == 0
        terminal = journal.append(TokenBatch(1, 1, (), 'COMPLETED'))
        final = replay(a['target_client'], prepared(a, claim, terminal, sequence=3), target=a['member'])
        dispatch(n, final)
        dispatch(n, final)
        assert n._token_journals.active_count == 0
        assert journal.read(after_sequence=1) == ()


@pytest.mark.parametrize('change', ['signature', 'command', 'grant', 'node', 'endpoint',
    'key', 'run', 'deployment', 'sequence', 'digest', 'unknown'])
def test_invalid_ack_cannot_evict_native_receipts(tmp_path, world, change):
    with _world(tmp_path) as a:
        _, journal, _, claim = provision(world, a)
        n = native(world, a, journal)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        ack = replay(a['target_client'], prepared(a, claim, receipt), target=a['member'])
        ack = deepcopy(ack)
        body = ack['statement']
        field = {'command': 'command_id', 'grant': 'grant_digest', 'node': 'target_node_id',
                 'endpoint': 'target_endpoint_id', 'key': 'target_key_digest',
                 'run': 'run_id', 'deployment': 'deployment_id', 'sequence': 'receipt_sequence',
                 'digest': 'receipt_digest', 'unknown': 'unknown'}.get(change)
        if field is None:
            ack['signature']['signature'] = 'invalid'
        else:
            body[field] = 1 if change == 'sequence' else ('sha256:' + 'a' * 64 if change in {'key', 'grant', 'digest'} else 'wrong')
            ack['signature'] = world.owner.sign(body)
        with pytest.raises((ValueError, RuntimeError)):
            dispatch(n, ack)
        assert journal.read(after_sequence=-1) == (receipt,)


def test_separately_pinned_native_and_control_endpoints_share_exact_key_not_name(tmp_path, world):
    with _world(tmp_path) as a:
        stream,journal,_,claim=provision(world,a,native_endpoint=True)
        assert world.entry.endpoint_id!=a['member']['endpoint_id']
        n=native(world,a,journal,target_endpoint_id=a['member']['endpoint_id'])
        receipt=journal.append(TokenBatch(0,0,(10,),'COMPLETED'))
        ack=replay(a['target_client'],prepared(a,claim,receipt),target=a['member'])
        assert stream.read(timeout=0).token_ids==(10,)
        dispatch(n,ack)
        assert n._token_journals.active_count==0
        wrong=deepcopy(ack)
        wrong['statement']['target_endpoint_id']=world.entry.endpoint_id
        wrong['signature']=world.owner.sign(wrong['statement'])
        with pytest.raises((ValueError,RuntimeError),match='token_path_receipt_ack'):
            dispatch(n,wrong)


def test_ack_does_not_create_a_native_registration(tmp_path, world):
    with _world(tmp_path) as a:
        _, journal, _, claim = provision(world, a)
        n = native(world, a, journal)
        from mycelium_node.token_ack import NativeReceiptJournals
        n._token_journals = NativeReceiptJournals()
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        ack = replay(a['target_client'], prepared(a, claim, receipt), target=a['member'])
        with pytest.raises((ValueError, RuntimeError), match='token_path_receipt_ack'):
            dispatch(n, ack)
        assert journal.read(after_sequence=-1) == (receipt,)
