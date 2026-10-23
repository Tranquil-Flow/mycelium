# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only authority/receipt tests; no mailbox or device qualification."""
from copy import deepcopy
import importlib

import pytest

from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.token_path import TokenBatch
from tests.live.test_entry_token_path import DeskNode, CONTROL


def api():
    # Call-time import keeps the RED at an executed test, not collection.
    return importlib.import_module('mycelium_router.token_path_authority')


@pytest.fixture
def setup():
    owner = generate_ed25519_signer(endpoint_id='owner-endpoint')
    entry = generate_ed25519_signer(endpoint_id='entry-endpoint')
    scope = {
        'run_id': 'desk', 'request_id': 'request', 'requester_id': 'requester',
        'owner_endpoint_id': owner.endpoint_id,
        'owner_key_digest': owner.verification_key_digest,
        'entry_node_id': 'entry', 'entry_endpoint_id': entry.endpoint_id,
        'entry_key_digest': entry.verification_key_digest,
        'participants': [
            {'node_id': 'entry', 'endpoint_id': entry.endpoint_id, 'generation': 1},
            {'node_id': 'tail', 'endpoint_id': 'tail-endpoint', 'generation': 2},
        ],
        'control': dict(CONTROL), 'model_digest': 'sha256:' + 'c' * 64,
        'request_digest': 'sha256:' + 'd' * 64,
    }
    return owner, entry, scope


def issue(setup, **changes):
    owner, _, scope = setup
    body = dict(protocol='mycelium.token_path_grant.v1', scope=deepcopy(scope),
                max_new_tokens=6, stop_token_ids=[99], expires_at_unix_ms=105000)
    body.update(changes)
    # Use the real signer even for malformed-but-authentic negative controls.
    return {'statement': body, 'signature': owner.sign(body)}


def admit(setup, envelope=None, **kwargs):
    owner, _, scope = setup
    args = dict(expected_scope=scope, owner_key=owner.public_key_record(),
                max_new_tokens=6, stop_token_ids=(99,), now_unix_ms=100000,
                now_monotonic_s=10.0, process_deadline_monotonic_s=1800.0,
                lease_deadline_monotonic_s=100.0, clock_uncertainty_ms=100)
    args.update(kwargs)
    return api().GrantRegistry().admit(issue(setup) if envelope is None else envelope,
                                      **args)


def test_initial_path_attempt_zero_matches_native_control_contract(setup):
    # PhysicalNodeService admits initial path_attempt=0. Grant authority must
    # bind that same value rather than making the first real route impossible.
    setup[2]['control']['path_attempt'] = 0
    assert admit(setup).statement()['scope']['control']['path_attempt'] == 0


def test_grant_is_frozen_and_deadline_translated_once(setup):
    envelope = issue(setup)
    grant = admit(setup, envelope)
    assert grant.deadline_monotonic_s == pytest.approx(14.9)
    envelope['statement']['scope']['control']['publisher_generation'] = 5
    setup[2]['control']['publisher_generation'] = 9
    assert grant.statement()['scope']['control']['publisher_generation'] == 1
    copy = grant.statement()
    copy['max_new_tokens'] = 100
    assert grant.statement()['max_new_tokens'] == 6


@pytest.mark.parametrize('field,value,code', [
    ('expires_at_unix_ms', 100100, 'token_path_grant_expired'),
    ('expires_at_unix_ms', True, 'token_path_grant_invalid'),
    ('max_new_tokens', 7, 'token_path_grant_over_budget'),
    ('max_new_tokens', True, 'token_path_grant_invalid'),
    ('stop_token_ids', [], 'token_path_grant_mismatch'),
    ('stop_token_ids', [99, 99], 'token_path_grant_invalid'),
    ('protocol', 'future', 'token_path_grant_invalid'),
    ('unknown', 1, 'token_path_grant_invalid'),
])
def test_grant_refusals(setup, field, value, code):
    with pytest.raises(ValueError, match=code):
        admit(setup, issue(setup, **{field: value}))


@pytest.mark.parametrize('field', ['requester_id', 'request_id', 'run_id',
                                  'model_digest', 'request_digest', 'entry_key_digest'])
def test_grant_exact_subject_binding(setup, field):
    envelope = issue(setup)
    envelope['statement']['scope'][field] += 'x'
    envelope['signature'] = setup[0].sign(envelope['statement'])
    with pytest.raises(ValueError, match='token_path_grant_mismatch'):
        admit(setup, envelope)


@pytest.mark.parametrize('field', list(CONTROL))
def test_every_route_generation_and_control_field_is_bound(setup, field):
    envelope = issue(setup)
    value = envelope['statement']['scope']['control'][field]
    envelope['statement']['scope']['control'][field] = value + 1 if type(value) is int else value + 'x'
    envelope['signature'] = setup[0].sign(envelope['statement'])
    with pytest.raises(ValueError, match='token_path_grant_mismatch'):
        admit(setup, envelope)


def test_tamper_and_self_signed_owner_are_rejected(setup):
    envelope = issue(setup)
    envelope['statement']['max_new_tokens'] = 5
    with pytest.raises(ValueError, match='token_path_grant_signature'):
        admit(setup, envelope)
    envelope = issue(setup)
    impostor = generate_ed25519_signer(endpoint_id=setup[0].endpoint_id)
    envelope['signature'] = impostor.sign(envelope['statement'])
    with pytest.raises(ValueError, match='token_path_grant_signature'):
        admit(setup, envelope)


def test_process_and_lease_ceiling_do_not_restart(setup):
    assert admit(setup, process_deadline_monotonic_s=12).deadline_monotonic_s == 12
    assert admit(setup, lease_deadline_monotonic_s=11).deadline_monotonic_s == 11
    with pytest.raises(ValueError, match='token_path_grant_expired'):
        admit(setup, process_deadline_monotonic_s=10)


def test_replay_and_capacity_do_not_evict_authority(setup):
    registry = api().GrantRegistry(capacity=1)
    owner, _, scope = setup
    args = dict(expected_scope=scope, owner_key=owner.public_key_record(),
                max_new_tokens=6, stop_token_ids=(99,), now_unix_ms=100000,
                now_monotonic_s=10., process_deadline_monotonic_s=1800.,
                lease_deadline_monotonic_s=100., clock_uncertainty_ms=0)
    registry.admit(issue(setup), **args)
    with pytest.raises(ValueError, match='token_path_grant_replay'):
        registry.admit(issue(setup), **args)
    scope['request_id'] = 'second'
    with pytest.raises(ValueError, match='token_path_grant_capacity'):
        registry.admit(issue(setup), **args)


def stream(setup, **kwargs):
    grant = admit(setup)
    return api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11., **kwargs)


def receiver(setup, **kwargs):
    return api().ReceiptCursor(grant=admit(setup), entry_key=setup[1].public_key_record(),
                               clock=lambda: 11., **kwargs)


def test_signed_batches_resume_ack_and_exactly_once_owner_tokens(setup):
    journal, cursor = stream(setup), receiver(setup)
    first = journal.append(TokenBatch(0, 0, (1, 2), 'DECODING'))
    last = journal.append(TokenBatch(1, 2, (3,), 'COMPLETED'))
    assert journal.read(after_sequence=-1) == (first, last)
    assert cursor.accept(first) == (1, 2)
    with pytest.raises(ValueError, match='token_path_receipt_order'):
        cursor.accept(first)
    assert cursor.accept(last) == (3,)
    journal.acknowledge(0, first['signature']['signed_statement_digest'])
    assert journal.read(after_sequence=0) == (last,)
    with pytest.raises(ValueError, match='token_path_receipt_prefix_lost'):
        journal.read(after_sequence=-1)
    assert cursor.terminal == 'COMPLETED'


def test_bounded_retention_refuses_without_advancing_and_bad_ack_cannot_evict(setup):
    journal = stream(setup, capacity=1)
    first = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    with pytest.raises(ValueError, match='token_path_receipt_overflow'):
        journal.append(TokenBatch(1, 1, (2,), 'COMPLETED'))
    with pytest.raises(ValueError, match='token_path_receipt_ack'):
        journal.acknowledge(0, 'sha256:' + '0' * 64)
    assert journal.read(after_sequence=-1) == (first,)
    journal.acknowledge(0, first['signature']['signed_statement_digest'])
    assert journal.append(TokenBatch(1, 1, (2,), 'COMPLETED'))['statement']['sequence'] == 1


@pytest.mark.parametrize('batch', [TokenBatch(1, 0, (1,), 'DECODING'),
    TokenBatch(0, 1, (1,), 'DECODING'), TokenBatch(0, 0, (True,), 'DECODING'),
    TokenBatch(0, 0, tuple(range(7)), 'COMPLETED'), TokenBatch(0, 0, (99,), 'COMPLETED'),
    TokenBatch(0, 0, (), 'DECODING'), TokenBatch(0, 0, (), 'UNKNOWN')])
def test_invalid_receipt_has_no_cursor_effect(setup, batch):
    journal = stream(setup)
    with pytest.raises(ValueError):
        journal.append(batch)
    assert journal.read(after_sequence=-1) == ()


def test_wrong_grant_signature_generation_expiry_and_terminal_are_refused(setup):
    journal = stream(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'COMPLETED'))
    cursor = receiver(setup)
    tampered = deepcopy(receipt)
    tampered['statement']['token_ids'] = [2]
    with pytest.raises(ValueError, match='token_path_receipt_signature'):
        cursor.accept(tampered)
    mismatch = deepcopy(receipt)
    mismatch['statement']['grant_digest'] = 'sha256:' + '0' * 64
    mismatch['signature'] = setup[1].sign(mismatch['statement'])
    with pytest.raises(ValueError, match='token_path_receipt_subject'):
        cursor.accept(mismatch)
    stale = receiver(setup, current=lambda: False)
    with pytest.raises(ValueError, match='token_path_grant_stale'):
        stale.accept(receipt)
    cursor.accept(receipt)
    with pytest.raises(ValueError, match='token_path_receipt_terminal'):
        journal.append(TokenBatch(1, 1, (), 'COMPLETED'))


def test_node_composition_validates_grant_and_publishes_signed_batches(setup):
    node = DeskNode()
    node.node.signer = setup[1]
    grant = admit(setup)
    journal = api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11.)
    cursor = receiver(setup)
    delivered = []
    result = node.node.drive_granted_entry_token_path(
        grant=grant, journal=journal, publish=lambda receipt: delivered.extend(cursor.accept(receipt)),
        cancelled=lambda: False, clock=lambda: 11., enabled=True, batch_tokens=2)
    assert delivered == [1, 2, 3, 4, 5, 6]
    assert result.status == cursor.terminal == 'COMPLETED'
    assert node.rounds == 6


def test_node_default_off_and_mismatched_control_never_compute(setup):
    node = DeskNode()
    node.node.signer = setup[1]
    grant = admit(setup)
    journal = api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11.)
    args = dict(grant=grant, journal=journal, publish=lambda _: None,
                cancelled=lambda: False, clock=lambda: 11.)
    with pytest.raises(RuntimeError, match='token_path_disabled'):
        node.node.drive_granted_entry_token_path(**args)
    node.node._request_controls['request']['publisher_generation'] = 2
    with pytest.raises(RuntimeError, match='stale_infer_decode_generation'):
        node.node.drive_granted_entry_token_path(**args, enabled=True)
    assert node.rounds == 0


def test_lost_ack_response_allows_exact_retry_but_not_conflicting_ack(setup):
    journal = stream(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    digest = receipt['signature']['signed_statement_digest']
    journal.acknowledge(0, digest)
    journal.acknowledge(0, digest)  # retry after lost transport response
    with pytest.raises(ValueError, match='token_path_receipt_ack'):
        journal.acknowledge(0, 'sha256:' + 'f' * 64)


def test_expired_or_stale_receipts_never_advance_owner_cursor(setup):
    journal = stream(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    now = [20.]
    cursor = api().ReceiptCursor(grant=admit(setup), entry_key=setup[1].public_key_record(),
                                 clock=lambda: now[0])
    with pytest.raises(ValueError, match='token_path_grant_expired'):
        cursor.accept(receipt)
    now[0] = 11.
    assert cursor.accept(receipt) == (1,)


def test_admitted_object_allows_only_one_execution_claim(setup):
    grant = admit(setup)
    grant.claim_execution()
    with pytest.raises(ValueError, match='token_path_grant_replay'):
        grant.claim_execution()


@pytest.mark.parametrize('mutation', ['extra_scope', 'extra_control', 'extra_peer', 'duplicate_peer', 'bool_generation'])
def test_malformed_even_independently_matching_scope_is_rejected(setup, mutation):
    scope = setup[2]
    if mutation == 'extra_scope':
        scope['unknown'] = 1
    elif mutation == 'extra_control':
        scope['control']['unknown'] = 1
    elif mutation == 'extra_peer':
        scope['participants'][1]['unknown'] = 1
    elif mutation == 'duplicate_peer':
        scope['participants'].append(deepcopy(scope['participants'][1]))
    else:
        scope['participants'][1]['generation'] = True
    with pytest.raises(ValueError, match='token_path_grant_invalid'):
        admit(setup)


@pytest.mark.parametrize('status,code', [('FAILED', 'delivery_deadline_exceeded'), ('CANCELLED', None)])
def test_zero_token_terminal_is_signed_without_invented_progress(setup, status, code):
    journal, cursor = stream(setup), receiver(setup)
    receipt = journal.append(TokenBatch(0, 0, (), status), error_code=code)
    assert cursor.accept(receipt) == ()
    assert cursor.terminal == status
    assert receipt['statement']['error_code'] == code
    assert receipt['statement']['stage_receipt_refs'] == []


def test_retained_receipt_is_not_mutable_by_reader(setup):
    journal = stream(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    receipt['statement']['token_ids'][0] = 999
    read = journal.read(after_sequence=-1)
    assert read[0]['statement']['token_ids'] == [1]
    read[0]['statement']['token_ids'].append(3)
    assert journal.read(after_sequence=-1)[0]['statement']['token_ids'] == [1]


def test_receipt_overflow_stops_native_compute_and_cannot_restart_grant(setup):
    node = DeskNode()
    node.node.signer = setup[1]
    grant = admit(setup)
    journal = api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11., capacity=1)
    args = dict(grant=grant, journal=journal, publish=lambda _: None,
                cancelled=lambda: False, clock=lambda: 11., enabled=True, batch_tokens=2)
    with pytest.raises(ValueError, match='token_path_receipt_overflow'):
        node.node.drive_granted_entry_token_path(**args)
    assert node.rounds == 4
    with pytest.raises(ValueError, match='token_path_grant_replay'):
        node.node.drive_granted_entry_token_path(**args)
    assert node.rounds == 4


def test_native_stage_loss_preserves_code_and_never_signs_completion(setup):
    from mycelium_router.transports.iroh import IrohTransportError
    node = DeskNode(fail_at=3)
    node.node.signer = setup[1]
    grant = admit(setup)
    journal = api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11.)
    delivered = []
    with pytest.raises(IrohTransportError, match='delivery_deadline_exceeded'):
        node.node.drive_granted_entry_token_path(grant=grant, journal=journal,
            publish=delivered.append, cancelled=lambda: False, clock=lambda: 11.,
            enabled=True, batch_tokens=2)
    assert [r['statement']['status'] for r in delivered] == ['DECODING']
    assert delivered[0]['statement']['token_ids'] == [1, 2]


def test_native_cancel_discards_unpublished_token(setup):
    node = DeskNode(cancel_at=3)
    node.node.signer = setup[1]
    grant = admit(setup)
    journal = api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11.)
    delivered = []
    result = node.node.drive_granted_entry_token_path(grant=grant, journal=journal,
        publish=delivered.append, cancelled=lambda: node.cancelled, clock=lambda: 11.,
        enabled=True, batch_tokens=2)
    assert result.status == 'CANCELLED'
    assert [t for r in delivered for t in r['statement']['token_ids']] == [1, 2]
    assert delivered[-1]['statement']['status'] == 'CANCELLED'


@pytest.mark.parametrize('mutation', [
    {'sequence': 2}, {'start_index': 1}, {'end_index': 4}, {'unknown': 1},
    {'sequence': False}, {'token_ids': [True]}, {'status': 'COMPLETED', 'error_code': 'bad'},
    {'status': 'FAILED', 'error_code': 'failure'},
    {'stage_receipt_refs': [{'node_id': 'outsider', 'receipt_digest': 'sha256:' + 'e' * 64}]},
    {'token_ids': list(range(7)), 'end_index': 7},
])
def test_authentic_but_invalid_receipt_does_not_poison_owner_cursor(setup, mutation):
    journal, cursor = stream(setup), receiver(setup)
    first = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    bad = deepcopy(first)
    bad['statement'].update(mutation)
    bad['signature'] = setup[1].sign(bad['statement'])
    with pytest.raises(ValueError):
        cursor.accept(bad)
    assert cursor.accept(first) == (1,)


def test_grant_registry_rejects_widened_reissue_of_same_attempt(setup):
    registry = api().GrantRegistry()
    owner, _, scope = setup
    args = dict(expected_scope=scope, owner_key=owner.public_key_record(),
                max_new_tokens=6, stop_token_ids=(99,), now_unix_ms=100000,
                now_monotonic_s=10., process_deadline_monotonic_s=1800.,
                lease_deadline_monotonic_s=100., clock_uncertainty_ms=0)
    registry.admit(issue(setup, max_new_tokens=4), **args)
    with pytest.raises(ValueError, match='token_path_grant_replay'):
        registry.admit(issue(setup, max_new_tokens=6, expires_at_unix_ms=110000), **args)


def test_deadline_during_signing_does_not_commit_receipt(setup, monkeypatch):
    now = [11.]
    journal = api().ReceiptJournal(grant=admit(setup), signer=setup[1], clock=lambda: now[0])
    signer_type = type(setup[1])
    real_sign = signer_type.sign
    def slow_sign(self, body):
        result = real_sign(self, body)
        now[0] = 20.
        return result
    monkeypatch.setattr(signer_type, 'sign', slow_sign)
    with pytest.raises(ValueError, match='token_path_grant_expired'):
        journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    now[0] = 11.
    assert journal.read(after_sequence=-1) == ()


def test_bool_cannot_replace_a_bound_integer_generation(setup):
    envelope = issue(setup)
    envelope['statement']['scope']['control']['publisher_generation'] = True
    envelope['signature'] = setup[0].sign(envelope['statement'])
    with pytest.raises(ValueError, match='token_path_grant_mismatch'):
        admit(setup, envelope)


def test_native_grant_for_another_entry_refuses_before_compute(setup):
    node = DeskNode()
    # The admitted grant is for setup's entry, not this node's real signer.
    grant = admit(setup)
    journal = api().ReceiptJournal(grant=grant, signer=setup[1], clock=lambda: 11.)
    with pytest.raises(RuntimeError, match='token_path_grant_mismatch'):
        node.node.drive_granted_entry_token_path(grant=grant, journal=journal,
            publish=lambda _: None, cancelled=lambda: False, clock=lambda: 11., enabled=True)
    assert node.rounds == 0
