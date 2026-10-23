# SPDX-License-Identifier: AGPL-3.0-only
"""Exact, caller-persisted signed requests for bounded mailbox recovery.

These documents are PRIVATE state, not evidence exports. Preparing never makes
an HTTP request. Replay restores the original signed bytes and uses the existing
client/server validators; it does not refresh expiry or allocate a new attempt.
"""
from collections.abc import Mapping

from .managed_control import (
    OWNER_COMMAND_PROTOCOL, TARGET_POLL_PROTOCOL, TARGET_POLL_PROTOCOL_V2, TARGET_ACK_PROTOCOL,
    _OWNER_FIELDS, _POLL_FIELDS, _POLL_V2_FIELDS, _ACK_FIELDS, _BINDING_FIELDS,
    _binding_from_member, _verify_signed, _require, _detached, _diagnostic_fields, _target_diagnostic,
)

from .token_receipt_mailbox import PROGRESS

_PROTOCOLS = {'enqueue': OWNER_COMMAND_PROTOCOL, 'poll': TARGET_POLL_PROTOCOL, 'ack': TARGET_ACK_PROTOCOL,
              'token_progress': PROGRESS}
_FIELDS = {'enqueue': _OWNER_FIELDS, 'poll': _POLL_FIELDS, 'ack': _ACK_FIELDS,
           'token_progress': _POLL_FIELDS | {'command_id', 'claim_token', 'receipt'}}


def prepare(client, operation, *, target, **kwargs):
    _require(operation in _PROTOCOLS, 'managed_control_operation_invalid')
    if operation == 'enqueue':
        statement = client.owner_command_statement(target=target, **kwargs)
    else:
        kwargs.setdefault('ttl_seconds', 60.0)
        additions = None
        active = kwargs.pop('active_claims', None) if operation == 'poll' else None
        if active is not None:
            additions = {'active_claims': dict(active)}
        if operation == 'ack':
            additions = {field: kwargs.pop(field) for field in (
                'command_id', 'claim_token', 'disposition', 'native_response')}
            if 'target_error_code' in kwargs:
                additions['target_error_code'] = kwargs.pop('target_error_code')
                _target_diagnostic(additions)
        if operation == 'token_progress':
            additions = {field: kwargs.pop(field) for field in ('command_id', 'claim_token', 'receipt')}
            from mycelium_node.token_progress import _snapshot
            import json
            additions['receipt'] = json.loads(_snapshot(additions['receipt']))
        statement = client._target_statement(
            protocol=TARGET_POLL_PROTOCOL_V2 if active is not None else _PROTOCOLS[operation],
            target=target, additions=additions, **kwargs)
    return {'operation': operation, 'envelope': _detached(client._signed_outbound(statement))}


def replay(client, prepared, *, target):
    _require(isinstance(prepared, Mapping) and set(prepared) == {'operation', 'envelope'},
        'managed_control_prepared_request_invalid')
    operation = prepared['operation']
    _require(isinstance(operation, str) and operation in _PROTOCOLS, 'managed_control_operation_invalid')
    envelope = prepared['envelope']
    concurrent = operation == 'poll' and envelope.get('statement', {}).get('protocol') == TARGET_POLL_PROTOCOL_V2
    statement = _verify_signed(envelope, expected_protocol=TARGET_POLL_PROTOCOL_V2 if concurrent else _PROTOCOLS[operation],
        expected_key_record=client.signer.public_key_record(),
        expected_endpoint_id=client.signer.endpoint_id, now=client._clock())
    fields = _POLL_V2_FIELDS if concurrent else _FIELDS[operation]
    if operation == 'ack':
        fields = _diagnostic_fields(envelope, fields)
        _target_diagnostic(statement)
    _require(set(statement) == fields and statement['swarm_id'] == client.swarm_id,
        'managed_control_prepared_request_invalid')
    binding = _binding_from_member(target, statement['run_id'], statement['deployment_id'])
    _require(all(statement[field] == binding[field] for field in _BINDING_FIELDS),
        'managed_control_member_stale')
    if operation == 'enqueue':
        _require(statement['owner_node_id'] == client.owner_node_id,
            'managed_control_authority_binding_invalid')
    intent = {key: value for key, value in statement.items() if key not in {'issued_at', 'expires_at'}}
    cached = client._prepared_requests.get(statement['request_id'])
    _require(cached is None or cached[1] == envelope, 'managed_control_conflicting_retry')
    # Re-seed the bounded in-memory cache so a subsequent same-process retry of
    # this exact persisted envelope reuses its bytes; capacity accounting still
    # applies (oldest entry forgotten first).  Never refreshes signed freshness.
    client._remember_prepared(statement['request_id'], _detached(intent), _detached(envelope))
    kwargs = {field: statement[field] for field in ('sequence', 'nonce', 'request_id')}
    if operation == 'enqueue':
        kwargs['native_command'] = statement['native_command']
    else:
        kwargs.update(run_id=statement['run_id'], deployment_id=statement['deployment_id'])
        if concurrent:
            kwargs['active_claims'] = statement['active_claims']
        if operation == 'ack':
            kwargs.update({field: statement[field] for field in (
                'command_id', 'claim_token', 'disposition', 'native_response')})
            kwargs.update(_target_diagnostic(statement))
        if operation == 'token_progress':
            kwargs.update({field: statement[field] for field in ('command_id', 'claim_token', 'receipt')})
            kwargs['return_envelope'] = True
    return getattr(client, operation)(target=target, **kwargs)


def forget(client, prepared):
    """Explicitly forget a durably persisted request's cache entry.

    Safe only after the caller has terminally resolved the persisted envelope
    (confirmed enqueue, terminal ack): the exact bytes remain in the caller's
    own durable state, so dropping the in-memory copy loses nothing.
    """
    _require(isinstance(prepared, Mapping) and set(prepared) == {'operation', 'envelope'},
        'managed_control_prepared_request_invalid')
    envelope = prepared['envelope']
    statement = envelope.get('statement') if isinstance(envelope, Mapping) else None
    request_id = statement.get('request_id') if isinstance(statement, Mapping) else None
    _require(isinstance(request_id, str), 'managed_control_prepared_request_invalid')
    client.forget_prepared_request(request_id)
