# SPDX-License-Identifier: AGPL-3.0-only
"""Grant-bearing managed grammar; signed mailbox transport, not model execution."""
from copy import deepcopy

import pytest

from mycelium_a13_macos.managed_control import validate_native_command, ManagedControlError
from mycelium_a13_macos.worker import _OPERATION_PAYLOAD_FIELDS
from test_managed_control import _world, _enqueue, _command, RUN_ID, DEPLOYMENT_ID
from test_work_authorization import _consent, _envelope


def start_payload():
    return {'request': {'request_id': 'request', 'max_new_tokens': 6},
            'control': {}, 'path_manifest': {},
            'token_grant': {'statement': {'protocol': 'mycelium.token_path_grant.v1'},
                            'signature': {}}}


def bind_payload():
    p = start_payload()
    return {'request_id': 'request', 'control': p.pop('control'), 'token_path': p}


def test_grant_binding_crosses_signed_mailbox_without_changing_bytes(tmp_path):
    p = bind_payload()
    with _world(tmp_path) as w:
        _enqueue(w, 'grant-bind', operation='bind_request_control', payload=p)
        result = w['target_client'].poll(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, sequence=1, nonce='bind-poll-nonce',
            request_id='bind-poll', active_claims={})
        assert result['command_envelope']['statement']['native_command']['payload'] == p
    assert frozenset(p) in _OPERATION_PAYLOAD_FIELDS['bind_request_control']


@pytest.mark.parametrize('mutation', ['unknown', 'request', 'grant', 'extra-key', 'oversize'])
def test_grant_bind_closed_grammar(tmp_path, mutation):
    p = bind_payload()
    if mutation == 'unknown':
        p['token_path']['unknown'] = True
    elif mutation == 'request':
        p['request_id'] = 'other'
    elif mutation == 'grant':
        p['token_path']['token_grant'] = None
    elif mutation == 'extra-key':
        p['token_path']['token_grant']['owner_key'] = {}
    else:
        p['token_path']['token_grant']['statement']['padding'] = 'x' * 16384
    with pytest.raises(ManagedControlError):
        validate_native_command(_command('grant-bind', 'bind_request_control', p))


def test_grant_start_crosses_signed_mailbox_without_changing_bytes(tmp_path):
    p = start_payload()
    with _world(tmp_path) as w:
        _enqueue(w, 'grant-start', operation='infer_start', payload=p)
        result = w['target_client'].poll(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, sequence=1, nonce='grant-poll-nonce',
            request_id='grant-poll', active_claims={})
        assert result['command_envelope']['statement']['native_command']['payload'] == p


def test_worker_and_consent_share_closed_grant_start_shape(tmp_path):
    p = start_payload()
    auth, *_ = _consent(tmp_path)
    assert frozenset(p) in _OPERATION_PAYLOAD_FIELDS['infer_start']
    assert auth.authorize_load(_envelope('infer_start', p)) is True
    assert validate_native_command(_command('grant-start', 'infer_start', p))['payload'] == p


@pytest.mark.parametrize('mutation', ['no-control', 'no-manifest', 'unknown', 'excluded',
                                      'null', 'oversize', 'extra-envelope', 'no-signature'])
def test_grant_grammar_rejects_malformed_or_unlocked_start(tmp_path, mutation):
    p = start_payload()
    if mutation == 'no-control':
        del p['control']
    elif mutation == 'no-manifest':
        del p['path_manifest']
    elif mutation == 'unknown':
        p['surprise'] = 1
    elif mutation == 'excluded':
        p['excluded_placement_ids'] = []
    elif mutation == 'null':
        p['token_grant'] = None
    elif mutation == 'oversize':
        p['token_grant']['statement']['padding'] = 'x' * 16384
    elif mutation == 'extra-envelope':
        p['token_grant']['owner_key'] = {}
    else:
        del p['token_grant']['signature']
    with pytest.raises(ManagedControlError):
        validate_native_command(_command('grant-start', 'infer_start', deepcopy(p)))
    auth, *_ = _consent(tmp_path)
    assert auth.authorize_load(_envelope('infer_start', p)) is False
