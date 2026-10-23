# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned native entry admission, provisioned only by a trusted host.

This is not a bearer policy and has no wire installer. Production activation must
supply membership/route pins and process-origin/lease deadlines independently of
the request grant, and enforce the same authority on every remote participant.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import time
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.token_path_authority import (
    AdmittedGrant, GrantRegistry, TokenPathAuthorityError, _validate_scope,
)


def valid_start_grant_shape(payload: Mapping[str, Any]) -> bool:
    """Closed bounded transport shape only; never signature authorization."""
    if set(payload) != {'request', 'control', 'path_manifest', 'token_grant'}:
        return False
    if not all(isinstance(payload[k], dict) for k in payload):
        return False
    envelope = payload['token_grant']
    try:
        return (set(envelope) == {'statement', 'signature'}
                and isinstance(envelope['statement'], dict)
                and isinstance(envelope['signature'], dict)
                and len(canonical_json_bytes(envelope)) <= 16384)
    except (TypeError, ValueError, RecursionError):
        return False


class EntryAdmissionPolicy:
    """A process-lifetime replay registry plus immutable host-provisioned pins.

    Request/control identity is derived from the actual request and the already
    bound native control, never copied from the grant. Clocks are immutable
    process-origin/lease ceilings; retrying start cannot restart either ceiling.
    No managed caller may construct this policy from a grant's own scope/key.
    """

    def __init__(self, *, scope_pins: Mapping[str, Any], owner_key: Mapping[str, Any],
                 max_new_tokens: int, stop_token_ids: tuple[int, ...],
                 process_deadline_monotonic_s: float, lease_deadline_monotonic_s: float,
                 clock_uncertainty_ms: int,
                 clock: Callable[[], float] = time.monotonic,
                 unix_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._pins = canonical_json_bytes(dict(scope_pins))
        self._owner_key = canonical_json_bytes(dict(owner_key))
        self._max_new_tokens = max_new_tokens
        self._stops = tuple(stop_token_ids)
        self._process_deadline = process_deadline_monotonic_s
        self._lease_deadline = lease_deadline_monotonic_s
        self._uncertainty = clock_uncertainty_ms
        self.clock = clock
        self._unix_ms = unix_ms
        self._registry = GrantRegistry()

    def owner_key(self):
        return json.loads(self._owner_key)

    def admit(self, envelope: Mapping[str, Any], *, request: Mapping[str, Any],
              bound_control: Mapping[str, Any]) -> AdmittedGrant:
        pins = json.loads(self._pins)
        # Pins must not contain dynamic fields which could shadow these values.
        if set(pins).intersection({'request_id', 'control', 'request_digest'}):
            raise TokenPathAuthorityError('token_path_grant_invalid')
        scope = {**pins, 'request_id': request.get('request_id'),
                 'control': dict(bound_control),
                 'request_digest': sha256_bytes(canonical_json_bytes(dict(request)))}
        _validate_scope(scope)
        budget = request.get('max_new_tokens')
        if type(budget) is not int or not 1 <= budget <= self._max_new_tokens:
            raise TokenPathAuthorityError('token_path_grant_over_budget')
        # Exact request budget: accepting a narrower grant would let prefill
        # execute against a different budget before the entry driver clamps it.
        body = envelope.get('statement') if isinstance(envelope, Mapping) else None
        if not isinstance(body, Mapping) or type(body.get('max_new_tokens')) is not int or body['max_new_tokens'] != budget:
            raise TokenPathAuthorityError('token_path_grant_mismatch')
        # Anchor before a potentially delayed wall-clock provider returns.
        now_monotonic_s = self.clock()
        now_unix_ms = self._unix_ms()
        return self._registry.admit(
            envelope, expected_scope=scope, owner_key=json.loads(self._owner_key),
            max_new_tokens=budget, stop_token_ids=self._stops,
            now_unix_ms=now_unix_ms, now_monotonic_s=now_monotonic_s,
            process_deadline_monotonic_s=self._process_deadline,
            lease_deadline_monotonic_s=self._lease_deadline,
            clock_uncertainty_ms=self._uncertainty)
