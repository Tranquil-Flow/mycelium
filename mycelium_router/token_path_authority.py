# SPDX-License-Identifier: AGPL-3.0-only
"""Desk-qualified token-path authority and receipt primitives (not a transport).

Trusted callers supply the pinned owner key and independently derived scope.
Never derive either from the envelope. Registry lifetime must equal the native
process lifetime; capacity refuses new grants rather than evicting replay fences.
Receipt acknowledgements must arrive over an authenticated owner channel. These
objects do not install participant authority, prove stage execution, or authorize
cleanup. Managed commands intentionally do not expose this protocol yet.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import math
import re
import threading
import time
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_qualification.signing import Ed25519EvidenceSigner, build_ed25519_verifier
from .token_path import TokenBatch

GRANT_PROTOCOL = 'mycelium.token_path_grant.v1'
RECEIPT_PROTOCOL = 'mycelium.token_path_receipt.v1'
MAX_RECEIPT_BYTES = 1024 * 1024  # 65,536 bounded int32 tokens plus signed metadata.
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_CONTROL_FIELDS = frozenset({
    'deployment_id', 'deployment_epoch', 'qualification_digest', 'command_id',
    'publisher_generation', 'absolute_deadline_ms', 'request_attempt', 'path_id',
    'path_attempt', 'path_digest', 'topology_generation', 'cancellation_generation',
})
_SCOPE_FIELDS = frozenset({
    'run_id', 'request_id', 'requester_id', 'owner_endpoint_id', 'owner_key_digest',
    'entry_node_id', 'entry_endpoint_id', 'entry_key_digest', 'participants',
    'control', 'model_digest', 'request_digest',
})
_GRANT_FIELDS = frozenset({'protocol', 'scope', 'max_new_tokens', 'stop_token_ids',
                           'expires_at_unix_ms'})
_RECEIPT_FIELDS = frozenset({'protocol', 'grant_digest', 'sequence', 'start_index',
    'end_index', 'token_ids', 'status', 'error_code', 'stage_receipt_refs',
    'previous_receipt_digest'})


class TokenPathAuthorityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise TokenPathAuthorityError(code)


def _integer(value: Any, minimum: int = 0, maximum: int = 2**53 - 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 256


def _digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _canonical(value: Any, code: str) -> bytes:
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise TokenPathAuthorityError(code) from exc
    _require(len(encoded) <= (MAX_RECEIPT_BYTES if code.startswith('token_path_receipt_') else 16384), code)
    return encoded


def _verify(envelope: Any, *, key: Mapping[str, Any], endpoint: str,
            key_digest: str, code: str) -> dict[str, Any]:
    _require(isinstance(envelope, Mapping) and set(envelope) == {'statement', 'signature'}, code)
    # Snapshot once before signature verification; never retain mutable input.
    copy = json.loads(_canonical(dict(envelope), code))
    signature = copy['signature']
    _require(isinstance(signature, dict) and isinstance(copy['statement'], dict), code)
    _require(signature.get('signer_endpoint_id') == endpoint
             and signature.get('verification_key_digest') == key_digest, code)
    try:
        verified = build_ed25519_verifier([key])(
            _canonical(copy['statement'], code), signature)
    except (TypeError, ValueError) as exc:
        raise TokenPathAuthorityError(code) from exc
    _require(verified is True, code)
    return copy['statement']


def _validate_scope(scope: Any) -> None:
    code = 'token_path_grant_invalid'
    _require(isinstance(scope, dict) and set(scope) == _SCOPE_FIELDS, code)
    for key, value in scope.items():
        if key.endswith('_digest'):
            _require(_digest(value), code)
        elif key not in {'participants', 'control'}:
            _require(_text(value), code)
    control = scope['control']
    _require(isinstance(control, dict) and set(control) == _CONTROL_FIELDS, code)
    for key, value in control.items():
        if key.endswith('_digest'):
            _require(_digest(value), code)
        elif key in {'deployment_id', 'command_id', 'path_id'}:
            _require(_text(value), code)
        else:
            _require(_integer(value, 0 if key in {'path_attempt', 'cancellation_generation'} else 1), code)
    _require(control['cancellation_generation'] == 0, 'token_path_grant_stale')
    peers = scope['participants']
    _require(isinstance(peers, list) and 1 <= len(peers) <= 64, code)
    for peer in peers:
        _require(isinstance(peer, dict) and set(peer) == {'node_id', 'endpoint_id', 'generation'}, code)
        _require(_text(peer['node_id']) and _text(peer['endpoint_id'])
                 and _integer(peer['generation'], 1), code)
    _require(len({p['node_id'] for p in peers}) == len(peers)
             and len({p['endpoint_id'] for p in peers}) == len(peers), code)
    _require(any(p['node_id'] == scope['entry_node_id']
                 and p['endpoint_id'] == scope['entry_endpoint_id'] for p in peers), code)


@dataclass(frozen=True)
class AdmittedGrant:
    """Process-local verified snapshot; not a serializable bearer capability."""
    _statement: bytes = field(repr=False)
    digest: str
    deadline_monotonic_s: float
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    _claimed: bool = field(default=False, repr=False, compare=False)

    def statement(self) -> dict[str, Any]:
        return json.loads(self._statement)

    def claim_execution(self) -> None:
        with self._lock:
            _require(not self._claimed, 'token_path_grant_replay')
            object.__setattr__(self, '_claimed', True)


class GrantRegistry:
    def __init__(self, *, capacity: int = 4096):
        _require(_integer(capacity, 1, 4096), 'token_path_grant_capacity')
        self._capacity = capacity
        self._seen: set[tuple[str, str, int]] = set()
        self._lock = threading.Lock()

    def admit(self, envelope: Any, *, expected_scope: Mapping[str, Any],
              owner_key: Mapping[str, Any], max_new_tokens: int,
              stop_token_ids: tuple[int, ...], now_unix_ms: int,
              now_monotonic_s: float, process_deadline_monotonic_s: float,
              lease_deadline_monotonic_s: float, clock_uncertainty_ms: int) -> AdmittedGrant:
        code = 'token_path_grant_invalid'
        scope = json.loads(_canonical(dict(expected_scope), code))
        _validate_scope(scope)
        statement = _verify(envelope, key=owner_key, endpoint=scope['owner_endpoint_id'],
                            key_digest=scope['owner_key_digest'], code='token_path_grant_signature')
        _require(set(statement) == _GRANT_FIELDS and statement['protocol'] == GRANT_PROTOCOL, code)
        _require(_integer(statement['max_new_tokens'], 1, 65536)
                 and _integer(statement['expires_at_unix_ms'], 1), code)
        stops = statement['stop_token_ids']
        _require(isinstance(stops, list) and len(stops) <= 128
                 and all(_integer(t, maximum=2**31 - 1) for t in stops), code)
        _require(len(set(stops)) == len(stops), code)
        _require(_integer(max_new_tokens, 1, 65536) and isinstance(stop_token_ids, tuple)
                 and len(stop_token_ids) <= 128
                 and all(_integer(t, maximum=2**31 - 1) for t in stop_token_ids), code)
        _require(statement['max_new_tokens'] <= max_new_tokens, 'token_path_grant_over_budget')
        # Canonical comparison avoids Python's True == 1 authority equivalence.
        _require(_canonical(statement['scope'], code) == _canonical(scope, code)
                 and tuple(stops) == stop_token_ids, 'token_path_grant_mismatch')
        _require(_integer(now_unix_ms) and _integer(clock_uncertainty_ms)
                 and all(_finite(v) for v in (now_monotonic_s,
                     process_deadline_monotonic_s, lease_deadline_monotonic_s)), code)
        remaining_ms = statement['expires_at_unix_ms'] - now_unix_ms - clock_uncertainty_ms
        deadline = min(now_monotonic_s + remaining_ms / 1000,
                       process_deadline_monotonic_s, lease_deadline_monotonic_s)
        _require(deadline > now_monotonic_s, 'token_path_grant_expired')
        encoded = _canonical(statement, code)
        subject = (scope['run_id'], scope['request_id'], scope['control']['request_attempt'])
        with self._lock:
            _require(subject not in self._seen, 'token_path_grant_replay')
            _require(len(self._seen) < self._capacity, 'token_path_grant_capacity')
            self._seen.add(subject)
        return AdmittedGrant(encoded, sha256_bytes(encoded), deadline)


class _ReceiptState:
    def __init__(self, grant: AdmittedGrant, clock: Callable[[], float],
                 current: Callable[[], bool]):
        self.grant = grant
        self._body = grant.statement()
        self._clock = clock
        self._current = current
        self._sequence = 0
        self._previous_digest = grant.digest
        self._index = 0
        self.terminal: str | None = None
        self._lock = threading.RLock()

    def _fence(self) -> None:
        _require(self._current() is True, 'token_path_grant_stale')
        now = self._clock()
        _require(_finite(now) and now < self.grant.deadline_monotonic_s, 'token_path_grant_expired')

    def _validate(self, body: dict[str, Any]) -> None:
        self._fence()
        _require(self.terminal is None, 'token_path_receipt_terminal')
        code = 'token_path_receipt_invalid'
        _require(set(body) == _RECEIPT_FIELDS and body['protocol'] == RECEIPT_PROTOCOL, code)
        _require(body['grant_digest'] == self.grant.digest, 'token_path_receipt_subject')
        _require(_integer(body['sequence']) and _integer(body['start_index'])
                 and _integer(body['end_index']), code)
        _require(body['sequence'] == self._sequence and body['start_index'] == self._index,
                 'token_path_receipt_order')
        _require(body['previous_receipt_digest'] == self._previous_digest,
                 'token_path_receipt_chain')
        tokens = body['token_ids']
        _require(isinstance(tokens, list) and len(tokens) <= self._body['max_new_tokens']
                 and all(_integer(t, maximum=2**31 - 1) for t in tokens), code)
        _require(body['end_index'] == self._index + len(tokens)
                 and body['end_index'] <= self._body['max_new_tokens'], code)
        _require(not set(tokens).intersection(self._body['stop_token_ids']), code)
        status = body['status']
        _require(type(status) is str and status in {'DECODING', 'COMPLETED', 'CANCELLED', 'FAILED'}, code)
        _require((status != 'DECODING' or bool(tokens))
                 and (status not in {'CANCELLED', 'FAILED'} or not tokens), code)
        _require((_text(body['error_code']) if status == 'FAILED' else body['error_code'] is None), code)
        refs = body['stage_receipt_refs']
        _require(isinstance(refs, list) and len(refs) <= 64, code)
        participants = {p['node_id'] for p in self._body['scope']['participants']}
        for ref in refs:
            _require(isinstance(ref, dict) and set(ref) == {'node_id', 'receipt_digest'}, code)
            _require(type(ref['node_id']) is str and ref['node_id'] in participants
                     and _digest(ref['receipt_digest']), code)
        _require(len({r['node_id'] for r in refs}) == len(refs), code)

    def _advance(self, body: dict[str, Any]) -> None:
        self._previous_digest = sha256_bytes(_canonical(body, 'token_path_receipt_invalid'))
        self._sequence += 1
        self._index = body['end_index']
        if body['status'] != 'DECODING':
            self.terminal = body['status']


class ReceiptJournal(_ReceiptState):
    """Bounded replay, never silent eviction. Overflow must stop the producer.

    Read/ack do not re-run compute. Retention is private; sealing full execution
    evidence and authenticating the ACK caller are transport responsibilities.
    """
    def __init__(self, *, grant: AdmittedGrant, signer: Ed25519EvidenceSigner,
                 capacity: int = 64, clock: Callable[[], float] = time.monotonic,
                 current: Callable[[], bool] = lambda: True):
        super().__init__(grant, clock, current)
        scope = self._body['scope']
        _require(signer.endpoint_id == scope['entry_endpoint_id']
                 and signer.verification_key_digest == scope['entry_key_digest'], 'token_path_receipt_signer')
        _require(_integer(capacity, 1, 64), 'token_path_receipt_capacity')
        self._signer = signer
        self._capacity = capacity
        self._pending: dict[int, bytes] = {}
        self._acked = -1
        self._ack_digest: str | None = None
        self._delivery_slot = threading.Event()
        self._delivery_slot.set()

    def delivery_ready(self) -> bool:
        """Only authenticated acknowledgement opens the next delivery slot."""
        return self._delivery_slot.is_set()

    def wait_for_delivery_slot(self, timeout: float) -> None:
        self._delivery_slot.wait(timeout)

    def append(self, batch: TokenBatch, *, error_code: str | None = None,
               stage_receipt_refs: tuple[Mapping[str, Any], ...] = ()) -> dict[str, Any]:
        body = dict(protocol=RECEIPT_PROTOCOL, grant_digest=self.grant.digest,
                    previous_receipt_digest=self._previous_digest,
                    sequence=batch.sequence, start_index=batch.start_index,
                    end_index=batch.start_index + len(batch.token_ids), token_ids=list(batch.token_ids),
                    status=batch.status, error_code=error_code,
                    stage_receipt_refs=[dict(ref) for ref in stage_receipt_refs])
        with self._lock:
            self._validate(body)
            _require(len(self._pending) < self._capacity, 'token_path_receipt_overflow')
            envelope = {'statement': body, 'signature': self._signer.sign(body)}
            encoded = _canonical(envelope, 'token_path_receipt_invalid')
            self._fence()  # signing must not make an expired/stale receipt visible
            self._pending[self._sequence] = encoded
            self._delivery_slot.clear()
            self._advance(body)
            return json.loads(encoded)

    def read(self, *, after_sequence: int) -> tuple[dict[str, Any], ...]:
        with self._lock:
            self._fence()
            _require(_integer(after_sequence, -1) and after_sequence < self._sequence,
                     'token_path_receipt_order')
            _require(after_sequence >= self._acked, 'token_path_receipt_prefix_lost')
            return tuple(json.loads(value) for seq, value in self._pending.items() if seq > after_sequence)

    def acknowledge(self, sequence: int, receipt_digest: str) -> None:
        with self._lock:
            _require(_integer(sequence), 'token_path_receipt_ack')
            if sequence == self._acked:
                _require(receipt_digest == self._ack_digest, 'token_path_receipt_ack')
                return
            _require(sequence in self._pending, 'token_path_receipt_ack')
            receipt = json.loads(self._pending[sequence])
            _require(receipt_digest == receipt['signature']['signed_statement_digest'], 'token_path_receipt_ack')
            self._pending = {seq: value for seq, value in self._pending.items() if seq > sequence}
            self._acked = sequence
            self._ack_digest = receipt_digest
            if not self._pending:
                self._delivery_slot.set()


class ReceiptCursor(_ReceiptState):
    """Owner-side verify-before-publish cursor. Stage references are NOT liveness.

    A caller must validate referenced execution receipts independently before
    crediting PV34. Terminal status is NOT a cleanup receipt. This cursor returns
    token IDs; it does not install a chat sink or run a successor transition.
    """
    def __init__(self, *, grant: AdmittedGrant, entry_key: Mapping[str, Any],
                 clock: Callable[[], float] = time.monotonic,
                 current: Callable[[], bool] = lambda: True):
        super().__init__(grant, clock, current)
        self._entry_key = dict(entry_key)

    def accept(self, envelope: Any) -> tuple[int, ...]:
        with self._lock:
            scope = self._body['scope']
            body = _verify(envelope, key=self._entry_key, endpoint=scope['entry_endpoint_id'],
                           key_digest=scope['entry_key_digest'], code='token_path_receipt_signature')
            self._validate(body)
            self._advance(body)
            return tuple(body['token_ids'])
