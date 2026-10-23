# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticated owner ACKs for native journals; never compute authority.

The private pipe carries the original owner-signed mailbox envelope. ACKs may
retire an exact retained prefix after compute ended/expired; they cannot publish
anything, restart compute, extend a grant, or certify cleanup/stage liveness.
"""
from collections.abc import Mapping
import json
import threading

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_router.token_path_authority import ReceiptJournal, _verify, _require

_ACK_FIELDS = frozenset({'protocol', 'request_id', 'swarm_id', 'owner_node_id',
    'owner_endpoint_id', 'issued_at', 'expires_at', 'target_node_id',
    'target_endpoint_id', 'target_key_digest', 'membership_generation',
    'member_incarnation', 'run_id', 'deployment_id', 'command_id',
    'grant_digest', 'receipt_sequence', 'receipt_digest'})


class NativeReceiptJournals:
    def __init__(self):
        self._lock = threading.RLock()
        self._records = {}
        self._active = {}

    @property
    def active_count(self):
        with self._lock:
            return len(self._active)

    def register(self, *, command_id, journal, owner_key, target_endpoint_id=None):
        _require(isinstance(journal, ReceiptJournal), 'token_path_receipt_ack')
        scope = journal.grant.statement()['scope']
        record = {k: scope[k] for k in ('run_id', 'entry_node_id', 'entry_endpoint_id',
            'entry_key_digest', 'owner_endpoint_id', 'owner_key_digest')}
        control_endpoint = scope['entry_endpoint_id'] if target_endpoint_id is None else target_endpoint_id
        _require(isinstance(control_endpoint,str) and bool(control_endpoint), 'token_path_receipt_ack')
        record['control_endpoint_id'] = control_endpoint
        record.update(command_id=command_id, deployment_id=scope['control']['deployment_id'],
            generation=next(p['generation'] for p in scope['participants'] if p['node_id'] == scope['entry_node_id']),
            owner_key=json.loads(canonical_json_bytes(owner_key)), last=None)
        _require(record['owner_key']['verification_key_digest'] == scope['owner_key_digest'],
                 'token_path_receipt_ack')
        with self._lock:
            _require(journal.grant.digest not in self._records, 'token_path_grant_replay')
            _require(len(self._records) < 4096 and len(self._active) < 8,
                     'token_path_receipt_capacity')
            self._records[journal.grant.digest] = record
            self._active[journal.grant.digest] = journal

    def acknowledge(self, envelope):
        code = 'token_path_receipt_ack'
        _require(isinstance(envelope, Mapping) and set(envelope) ==
                 {'protocol', 'statement', 'signature', 'verification_key'}, code)
        # Snapshot before any callback or lock handoff.
        encoded = canonical_json_bytes(dict(envelope))
        _require(len(encoded) <= 16384, code)
        value = json.loads(encoded)
        _require(value['protocol'] == 'mycelium.managed_control.signed_envelope.v1', code)
        body = value['statement']
        _require(isinstance(body, dict) and set(body) == _ACK_FIELDS, code)
        digest = body['grant_digest']
        _require(isinstance(digest, str), code)
        with self._lock:
            record = self._records.get(digest)
        _require(record is not None, code)
        _require(value['verification_key'] == record['owner_key'], code)
        verified = _verify({'statement': body, 'signature': value['signature']},
            key=record['owner_key'], endpoint=record['owner_endpoint_id'],
            key_digest=record['owner_key_digest'], code=code)
        _require(verified['protocol'] == 'mycelium.managed_control.token_progress_ack.v1'
            and verified['owner_endpoint_id'] == record['owner_endpoint_id']
            and verified['run_id'] == record['run_id']
            and verified['deployment_id'] == record['deployment_id']
            and verified['command_id'] == record['command_id']
            and verified['target_node_id'] == record['entry_node_id']
            and verified['target_endpoint_id'] == record['control_endpoint_id']
            and verified['target_key_digest'] == record['entry_key_digest']
            and type(verified['membership_generation']) is int
            and verified['membership_generation'] == record['generation']
            and type(verified['receipt_sequence']) is int, code)
        sequence, receipt_digest = verified['receipt_sequence'], verified['receipt_digest']
        with self._lock:
            journal = self._active.get(digest)
            if journal is None:
                _require(record['last'] == (sequence, receipt_digest), code)
            else:
                with journal._lock:
                    journal.acknowledge(sequence, receipt_digest)
                    record['last'] = (sequence, receipt_digest)
                    # ACK of an earlier delta cannot erase a later terminal.
                    if journal.terminal is not None and not journal._pending:
                        del self._active[digest]
        return {'grant_digest': digest, 'receipt_sequence': sequence,
                'receipt_digest': receipt_digest}
