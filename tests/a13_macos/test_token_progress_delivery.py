# SPDX-License-Identifier: AGPL-3.0-only
"""Production origin + exact durable request replay; no compute/model work."""
from copy import deepcopy

import pytest

from mycelium_a13_macos.durable_requests import prepare, replay
from mycelium_a13_macos.managed_control import ManagedControlClient, ManagedControlError
from mycelium_a13_macos.token_receipt_mailbox import PATH
from mycelium_router.token_path import TokenBatch
from tests.live.test_token_path_admission import world
from test_managed_control import _world, RUN_ID, DEPLOYMENT_ID
from test_token_receipt_mailbox import provision
from test_control_origin import _origin, _request


def prepared(a, claim, receipt, sequence=2):
    return prepare(a['target_client'], 'token_progress', target=a['member'],
        run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, command_id='grant-start',
        claim_token=claim, receipt=receipt, sequence=sequence,
        nonce=f'progress-{sequence}', request_id=f'progress-{sequence}')


def test_durable_progress_replay_preserves_bytes_and_owner_signature(tmp_path, world):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        packet = prepared(a, claim, receipt)
        saved = deepcopy(packet)
        first = replay(a['target_client'], packet, target=a['member'])
        assert first['signature']['signer_endpoint_id'] == world.owner.endpoint_id
        assert first['statement']['receipt_sequence'] == 0
        assert packet == saved
        # Simulate process-local signing-cache loss, not native restart/recompute.
        c = a['target_client']
        c._prepared_requests.clear()
        second = replay(c, packet, target=a['member'])
        assert second == first and packet == saved
        assert len(sealed) == 1 and stream.read(timeout=0).token_ids == (10,)
        with pytest.raises(TimeoutError):
            stream.read(timeout=0)


def test_production_origin_mounts_authenticated_receipt_path(tmp_path, world):
    with _world(tmp_path) as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        packet = prepared(a, claim, receipt)
        with _origin(a['mailbox']) as origin:
            status, _, response, _ = _request(origin, 'POST', PATH, packet['envelope'])
            assert status == 200
            assert response['statement']['receipt_digest'] == receipt['signature']['signed_statement_digest']
            status, _, _, _ = _request(origin, 'POST', PATH, {})
            assert status != 200
        assert len(sealed) == 1 and stream.read(timeout=0).token_ids == (10,)


@pytest.mark.parametrize('change', ['signature', 'member', 'receipt'])
def test_durable_progress_replay_never_resigns_mutated_authority(tmp_path, world, change):
    with _world(tmp_path) as a:
        _, journal, sealed, claim = provision(world, a)
        packet = prepared(a, claim, journal.append(TokenBatch(0, 0, (10,), 'DECODING')))
        target = deepcopy(a['member'])
        if change == 'signature':
            packet['envelope']['signature']['signature'] = 'invalid'
        elif change == 'member':
            target['generation'] += 1
        else:
            packet['envelope']['statement']['receipt']['statement']['token_ids'] = [11]
        with pytest.raises(ManagedControlError):
            replay(a['target_client'], packet, target=target)
        assert not sealed
