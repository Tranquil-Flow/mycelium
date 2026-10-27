# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in owner-local receipt inbox behind authenticated managed claims.

Registration is a trusted owner operation with an independently admitted cursor
and an exact-byte evidence sealer. It is not inferred from arriving receipts.
This transport does not install native authority, start compute, credit stage
liveness, or discharge cleanup. Restart loses registration and fails closed.
"""
from collections import deque
import json
import math
import sqlite3
import threading
import time

from mycelium_node.token_progress import VerifiedReceipt, _snapshot
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.token_path_authority import ReceiptCursor

PROGRESS = 'mycelium.managed_control.token_progress.v1'
PROGRESS_ACK = 'mycelium.managed_control.token_progress_ack.v1'
PROGRESS_CANCELLED = 'mycelium.managed_control.token_progress_cancelled.v1'
PATH = '/managed-control/token-progress'


class MailboxReceiptStream:
    def __init__(self, cursor, seal, capacity):
        if (not isinstance(cursor, ReceiptCursor) or not callable(seal)
                or (capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 64))):
            raise ValueError('token_progress_arguments_invalid')
        self.cursor = cursor
        self.target_endpoint_id = cursor.grant.statement()['scope']['entry_endpoint_id']
        self.seal = seal
        self.capacity = capacity
        self._token_capacity = cursor.grant.statement()['max_new_tokens']
        self._queued_tokens = 0
        self._condition = threading.Condition(threading.RLock())
        self._queue = deque()
        self._last = None
        self._last_bytes = None
        self._error = None
        self._terminal = False

    def check(self):
        if self._error:
            raise RuntimeError(self._error)
        # Same authority/deadline fence as signature verification, including retries.
        self.cursor._fence()

    def accept(self, receipt):
        encoded = _snapshot(receipt)
        with self._condition:
            self.check()
            if encoded == self._last_bytes:
                return self._last
            if self.capacity is not None and len(self._queue) >= self.capacity:
                self.close('token_progress_overflow')
                raise RuntimeError(self._error)
            value = json.loads(encoded)
            tokens = self.cursor.accept(value)
            if self._queued_tokens + len(tokens) > self._token_capacity:
                self.close('token_progress_overflow')
                raise RuntimeError(self._error)
            try:
                self.seal(encoded)
            except Exception:
                self.close('token_progress_seal_failed')
                raise RuntimeError(self._error) from None
            self.check()  # a slow seal cannot extend authority
            body = value['statement']
            verified = VerifiedReceipt(body['sequence'], tokens, body['status'], body['error_code'],
                value['signature']['signed_statement_digest'], encoded)
            self._last, self._last_bytes = verified, encoded
            self._queue.append(verified)
            self._queued_tokens += len(tokens)
            self._condition.notify_all()
            return verified

    def read(self, *, timeout=1.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('token_progress_arguments_invalid')
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self.check()
                if self._terminal:
                    raise RuntimeError('token_progress_terminal')
                if self._queue:
                    item = self._queue.popleft()
                    self._queued_tokens -= len(item.token_ids)
                    self._terminal = item.status != 'DECODING'
                    return item
                remaining = min(deadline - time.monotonic(),
                    self.cursor.grant.deadline_monotonic_s - self.cursor._clock())
                if remaining <= 0:
                    raise TimeoutError('token_progress_wait_timeout')
                # Recheck revocation even without a receipt or explicit close.
                self._condition.wait(min(remaining, .05))

    def close(self, code='token_progress_closed'):
        with self._condition:
            self._error = self._error or code
            self._queue.clear()
            self._queued_tokens = 0
            self._condition.notify_all()


class TokenProgressMailboxMixin:
    def _setup_token_progress(self):
        self._token_progress_lock = threading.RLock()
        self._token_progress_streams = {}
        self._token_progress_seen = set()
        # Small cancellation tombstones, bounded by the existing 4096 single-use
        # registrations. No tokens/cursors/compute authority survive closure.
        self._token_progress_cancelled = {}

    def open_token_progress(self, *, command_id, cursor, seal, capacity=None, target_endpoint_id=None):
        from .managed_control import _segment, _require
        _segment(command_id)
        stream = MailboxReceiptStream(cursor, seal, capacity)
        scope = cursor.grant.statement()['scope']
        # Managed and Iroh identities may name the same pinned key differently.
        # This control name is supplied independently by the trusted owner.
        stream.target_endpoint_id = scope['entry_endpoint_id'] if target_endpoint_id is None else target_endpoint_id
        _require(isinstance(stream.target_endpoint_id,str) and bool(stream.target_endpoint_id),
                 'token_progress_arguments_invalid')
        _require(scope['owner_endpoint_id'] == self.owner_signer.endpoint_id
                 and scope['owner_key_digest'] == self.owner_signer.verification_key_digest,
                 'token_progress_owner_mismatch')
        with self._token_progress_lock:
            _require(not self._closed, 'managed_control_closed')
            _require(command_id not in self._token_progress_seen, 'token_progress_stream_replayed')
            _require(len(self._token_progress_streams) < 8 and len(self._token_progress_seen) < 4096,
                     'token_progress_capacity')
            stream.check()
            self._token_progress_streams[command_id] = stream
            self._token_progress_seen.add(command_id)
        return stream

    def close_token_progress(self, command_id, *, cancelled=False):
        from .managed_control import _require
        _require(type(cancelled) is bool, 'token_progress_arguments_invalid')
        with self._token_progress_lock:
            stream = self._token_progress_streams.pop(command_id, None)
            if stream is not None and cancelled:
                self._token_progress_cancelled[command_id] = (
                    stream.cursor.grant.digest, stream.target_endpoint_id)
        if stream is not None:
            stream.close()

    def _close_token_progress(self):
        with self._token_progress_lock:
            streams = list(self._token_progress_streams.values())
            self._token_progress_streams.clear()
            self._token_progress_cancelled.clear()
        for stream in streams:
            stream.close()

    def _cancelled_token_progress(self, envelope, statement, member):
        """Authenticate retirement of forwarding, NOT receipt acceptance/cleanup.

        No grant deadline is renewed, no receipt is sealed/consumed/evicted and
        the command remains claimed. Only the ordinary native result and separate
        cleanup observations can discharge those obligations. Owner restart loses
        these explicit cancellation marks and still fails closed.
        """
        from .managed_control import _BINDING_FIELDS, _require, _document_digest
        command_id = statement['command_id']
        with self._token_progress_lock:
            cancelled = self._token_progress_cancelled.get(command_id)
        if cancelled is None:
            return None
        grant_digest, target_endpoint_id = cancelled
        with self._lock, self._member_guard(statement):
            _require(not self._closed, 'managed_control_closed')
            connection = self._connect()
            try:
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute('SELECT * FROM managed_commands WHERE command_id=?',
                                         (command_id,)).fetchone()
                _require(row is not None and row['state'] == 'claimed', 'token_progress_claim_stale')
                _require(all(row[k] == statement[k] for k in _BINDING_FIELDS),
                         'managed_control_command_binding_mismatch')
                _require(row['claim_token'] == statement['claim_token'],
                         'managed_control_claim_token_mismatch')
                command = json.loads(row['envelope_json'])['statement']['native_command']
                grant = command['payload'].get('token_grant', {}).get('statement', {})
                scope = grant.get('scope', {})
                receipt = json.loads(_snapshot(statement['receipt']))
                body, signature = receipt['statement'], receipt['signature']
                _require(isinstance(body, dict) and isinstance(signature, dict),
                         'token_progress_subject_mismatch')
                _require(command['command'] == 'infer_start'
                    and sha256_bytes(canonical_json_bytes(grant)) == grant_digest
                    and scope.get('entry_node_id') == statement['target_node_id']
                    and target_endpoint_id == statement['target_endpoint_id']
                    and scope.get('entry_key_digest') == statement['target_key_digest']
                    and scope.get('run_id') == statement['run_id']
                    and scope.get('control', {}).get('deployment_id') == statement['deployment_id']
                    and body.get('grant_digest') == grant_digest
                    and type(body.get('sequence')) is int
                    and signature.get('signed_statement_digest') == sha256_bytes(canonical_json_bytes(body)),
                    'token_progress_subject_mismatch')
                request_digest = _document_digest(envelope)
                signer_digest = member['verification_key_digest']
                cached = self._cached_or_guard(connection, signer_digest=signer_digest,
                    statement=statement, request_digest=request_digest)
                if cached is not None:
                    return cached
                response = self._response(PROGRESS_CANCELLED, statement['request_id'], {
                    **{k: statement[k] for k in _BINDING_FIELDS}, 'command_id': command_id,
                    'grant_digest': grant_digest, 'receipt_sequence': body['sequence'],
                    'receipt_digest': signature['signed_statement_digest']})
                self._record_auth(connection, signer_digest=signer_digest, statement=statement,
                                  request_digest=request_digest, response=response)
                connection.commit()
                return response
            finally:
                connection.close()

    def token_progress(self, envelope):
        from .managed_control import (_POLL_FIELDS, _BINDING_FIELDS, _require, _segment,
                                      _document_digest, ManagedControlError)
        fields = _POLL_FIELDS | {'command_id', 'claim_token', 'receipt'}
        statement, member = self._verify_target(envelope, protocol=PROGRESS, fields=fields)
        command_id = _segment(statement['command_id'])
        _segment(statement['claim_token'])
        with self._token_progress_lock:
            stream = self._token_progress_streams.get(command_id)
        cancelled = self._cancelled_token_progress(envelope, statement, member)
        if cancelled is not None:
            return cancelled
        _require(stream is not None, 'token_progress_disabled')
        # Serialize this stream only. Never hold the mailbox dispatch lock while
        # calling the trusted evidence sealer or receipt verifier.
        with stream._condition:
            accepted = False
            try:
                stream.check()
                # Match enqueue/poll/result: mailbox before member authority.
                # The inverse order deadlocks a receipt against owner result reads.
                with self._lock:
                    with self._member_guard(statement):
                        connection = self._connect()
                        try:
                            row = connection.execute('SELECT * FROM managed_commands WHERE command_id=?',
                                                     (command_id,)).fetchone()
                            _require(row is not None and row['state'] == 'claimed', 'token_progress_claim_stale')
                            _require(all(row[k] == statement[k] for k in _BINDING_FIELDS),
                                     'managed_control_command_binding_mismatch')
                            _require(row['claim_token'] == statement['claim_token'],
                                     'managed_control_claim_token_mismatch')
                            command = json.loads(row['envelope_json'])['statement']['native_command']
                            grant = command['payload'].get('token_grant', {})
                            scope = stream.cursor.grant.statement()['scope']
                            _require(command['command'] == 'infer_start'
                                and sha256_bytes(canonical_json_bytes(grant.get('statement'))) == stream.cursor.grant.digest
                                and scope['entry_node_id'] == statement['target_node_id']
                                and stream.target_endpoint_id == statement['target_endpoint_id']
                                and scope['entry_key_digest'] == statement['target_key_digest']
                                and scope['run_id'] == statement['run_id']
                                and scope['control']['deployment_id'] == statement['deployment_id'],
                                'token_progress_subject_mismatch')
                            request_digest = _document_digest(envelope)
                            signer_digest = member['verification_key_digest']
                            cached = self._cached_or_guard(connection, signer_digest=signer_digest,
                                statement=statement, request_digest=request_digest)
                        finally:
                            connection.close()
                if cached is not None:
                    stream.check()
                    return cached
                receipt = stream.accept(statement['receipt'])
                accepted = True
                # Reauthenticate current membership after sealing and before ACK.
                with self._lock, self._member_guard(statement):
                    stream.check()
                    connection = self._connect()
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        latest = connection.execute(
                            'SELECT state, claim_token FROM managed_commands WHERE command_id=?',
                            (command_id,)).fetchone()
                        _require(latest is not None and latest['state'] == 'claimed'
                                 and latest['claim_token'] == statement['claim_token'],
                                 'token_progress_claim_stale')
                        cached = self._cached_or_guard(connection, signer_digest=signer_digest,
                            statement=statement, request_digest=request_digest)
                        _require(cached is None, 'managed_control_conflicting_retry')
                        response = self._response(PROGRESS_ACK, statement['request_id'], {
                            **{k: statement[k] for k in _BINDING_FIELDS}, 'command_id': command_id,
                            'grant_digest': stream.cursor.grant.digest,
                            'receipt_sequence': receipt.sequence, 'receipt_digest': receipt.receipt_digest})
                        self._record_auth(connection, signer_digest=signer_digest, statement=statement,
                                          request_digest=request_digest, response=response)
                        connection.commit()
                        stream.check()
                        return response
                    finally:
                        connection.close()
            except ManagedControlError:
                if accepted:
                    stream.close('token_progress_forward_failed')
                raise
            except (ValueError, RuntimeError, sqlite3.Error, OSError):
                if accepted:
                    stream.close('token_progress_forward_failed')
                # Cancellation may have overtaken lookup/sealing. Reauthenticate
                # the exact claim; never infer authority from the error string.
                cancelled = self._cancelled_token_progress(envelope, statement, member)
                if cancelled is not None:
                    return cancelled
                raise ManagedControlError('token_progress_rejected') from None


def send_token_progress(client, *, target, run_id, deployment_id, command_id,
                        claim_token, receipt, sequence, nonce, request_id, ttl_seconds=60.,
                        return_envelope=False):
    from .managed_control import _RESPONSE_COMMON_FIELDS, _BINDING_FIELDS, _require
    encoded = _snapshot(receipt)
    receipt = json.loads(encoded)
    statement = client._target_statement(protocol=PROGRESS,
        additions=dict(command_id=command_id, claim_token=claim_token, receipt=receipt),
        target=target, run_id=run_id, deployment_id=deployment_id,
        sequence=sequence, nonce=nonce, request_id=request_id, ttl_seconds=ttl_seconds)
    response = client._request(PATH, client._signed_outbound(statement))
    fields = _RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | {
        'command_id', 'grant_digest', 'receipt_sequence', 'receipt_digest'}
    protocol = (PROGRESS_CANCELLED if isinstance(response, dict)
        and isinstance(response.get('statement'), dict)
        and response['statement'].get('protocol') == PROGRESS_CANCELLED else PROGRESS_ACK)
    ack = client._verified_response(response, protocol=protocol, fields=fields,
        request_id=request_id, target=target, run_id=run_id, deployment_id=deployment_id)
    _require(ack['command_id'] == command_id
        and ack['grant_digest'] == receipt['statement']['grant_digest']
        and type(ack['receipt_sequence']) is int
        and ack['receipt_sequence'] == receipt['statement']['sequence']
        and ack['receipt_digest'] == receipt['signature']['signed_statement_digest'],
        'managed_control_response_invalid')
    # Native journal eviction needs the original owner signature, not a
    # reconstructed statement. This option changes no verification predicate.
    return json.loads(canonical_json_bytes(response)) if return_envelope else ack
