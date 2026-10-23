# SPDX-License-Identifier: AGPL-3.0-only
"""Closed, signed target diagnostic contract; no native dispatch or model."""
from copy import deepcopy

import pytest

from mycelium_a13_macos.durable_requests import prepare, replay
from mycelium_a13_macos.managed_control import ManagedControlError, sign_managed_statement
from tests.live.test_token_path_admission import world
from tests.live.test_token_path_provisioning import provisioned
from test_control_pump import _world
from test_managed_control import _enqueue, _command, RUN_ID, DEPLOYMENT_ID


def claimed(swarm, envelope, operation='install_token_path'):
    payload = {'policy':envelope} if operation == 'install_token_path' else {}
    _enqueue(swarm, 'diagnostic', operation=operation, payload=payload)
    reply = swarm['target_client'].poll(target=swarm['member'], run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID, sequence=1, nonce='poll-nonce', request_id='poll-request')
    prepared = prepare(swarm['target_client'], 'ack', target=swarm['member'], run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID, command_id='diagnostic', claim_token=reply['claim_token'],
        disposition='reconciliation_required', native_response=None, target_error_code='token_path_policy_expired',
        sequence=2, nonce='ack-nonce', request_id='ack-request')
    return prepared, _command('diagnostic', operation, payload)


@pytest.mark.parametrize('change', ['unknown', 'large', 'object', 'completed', 'native', 'signature', 'command'])
def test_target_diagnostic_cannot_smuggle_data_or_change_authority(tmp_path, world, change):
    with _world(tmp_path/'swarm') as swarm:
        packet, command = claimed(swarm, provisioned(world).envelope,
                                  operation='hello' if change == 'command' else 'install_token_path')
        envelope = deepcopy(packet['envelope'])
        body = envelope['statement']
        if change == 'unknown': body['target_error_code'] = 'private_arbitrary_string'
        if change == 'large': body['target_error_code'] = 'x'*4096
        if change == 'object': body['target_error_code'] = {'private':'payload'}
        if change == 'completed': body['disposition'] = 'completed'
        if change == 'native': body['native_response'] = {'ok':True}
        if change == 'signature': body['target_error_code'] = 'token_path_policy_stale'
        else: envelope = sign_managed_statement(swarm['target_signer'], body)
        with pytest.raises(ManagedControlError) as refused:
            swarm['authority'].control_mailbox.ack(envelope)
        assert refused.value.code == ('managed_control_signature_invalid' if change == 'signature'
                                      else 'managed_control_target_diagnostic_invalid')
        assert swarm['authority'].control_mailbox.command_receipt('diagnostic')['state'] == 'claimed'


def test_diagnostic_survives_exact_lost_ack_replay_and_rejects_conflicting_retry(tmp_path, world):
    with _world(tmp_path/'swarm') as swarm:
        packet, command = claimed(swarm, provisioned(world).envelope)
        first = replay(swarm['target_client'], packet, target=swarm['member'])
        swarm['target_client']._prepared_requests.clear()
        assert replay(swarm['target_client'], packet, target=swarm['member']) == first
        assert first['target_error_code'] == 'token_path_policy_expired'
        result = swarm['owner_client'].result(target=swarm['member'], native_command=command,
            sequence=2, nonce='result-nonce', request_id='result-request')
        assert result['state'] == 'reconciliation_required'
        assert result['target_error_code'] == 'token_path_policy_expired'
        assert result['native_response'] is None and result['native_response_digest'] is None
        conflicting = deepcopy(packet['envelope']['statement'])
        conflicting.update(sequence=3, nonce='changed-nonce', request_id='changed-request',
                           target_error_code='token_path_policy_stale')
        with pytest.raises(ManagedControlError, match='managed_control_conflicting_retry'):
            swarm['authority'].control_mailbox.ack(sign_managed_statement(swarm['target_signer'], conflicting))
        # Retained outcome cannot be rewritten by a different authenticated ack.
        with swarm['authority'].control_mailbox._connect() as connection:
            row = connection.execute('SELECT terminal_receipt_json FROM managed_commands WHERE command_id=?',
                                     ('diagnostic',)).fetchone()
        import json
        assert json.loads(row[0])['statement']['target_error_code'] == 'token_path_policy_expired'
