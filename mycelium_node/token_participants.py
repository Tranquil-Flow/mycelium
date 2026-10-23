# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted host policy for native participant installation.

Membership and peer callbacks come from the authenticated host and live transport,
not the incoming grant. This module is not a membership bootstrap or a public flag.
One policy/registry is retained for the lifetime of a native process.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import time
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.serialization import path_manifest_to_dict
from mycelium_router.token_path_authority import GrantRegistry, TokenPathAuthorityError
from mycelium_router.validation import validate_manifest


def _require(value, code='token_path_grant_mismatch'):
    if not value:
        raise TokenPathAuthorityError(code)


def valid_bind_token_shape(payload):
    try:
        token = payload['token_path']
        return (set(payload) == {'request_id', 'control', 'token_path'}
            and isinstance(payload['request_id'], str)
            and isinstance(payload['control'], dict)
            and isinstance(token, dict)
            and set(token) == {'request', 'path_manifest', 'token_grant'}
            and all(isinstance(v, dict) for v in token.values())
            and token['request'].get('request_id') == payload['request_id']
            and set(token['token_grant']) == {'statement', 'signature'}
            and len(canonical_json_bytes(token['token_grant'])) <= 16384)
    except (KeyError, TypeError, ValueError, RecursionError):
        return False


class ParticipantAdmissionPolicy:
    def __init__(self, *, run_id: str, requester_id: str, owner_endpoint_id: str,
                 owner_key: Mapping[str, Any], members: Callable, peers: Callable,
                 max_new_tokens: int, stop_token_ids: tuple[int, ...],
                 process_deadline_monotonic_s: float, lease_deadline_monotonic_s: float,
                 clock_uncertainty_ms: int, clock=time.monotonic,
                 unix_ms=lambda: int(time.time() * 1000)):
        _require(callable(members) and callable(peers), 'token_path_policy_invalid')
        self.run_id = run_id
        self.requester_id = requester_id
        self.owner_endpoint_id = owner_endpoint_id
        self._owner_key = canonical_json_bytes(dict(owner_key))
        self._members, self._peers = members, peers
        self._max_tokens, self._stops = max_new_tokens, tuple(stop_token_ids)
        self._process_deadline = process_deadline_monotonic_s
        self._lease_deadline = lease_deadline_monotonic_s
        self._uncertainty = clock_uncertainty_ms
        self.clock, self._unix_ms = clock, unix_ms
        self._registry = GrantRegistry()

    def owner_key(self):
        return json.loads(self._owner_key)

    def control_endpoint(self, node_id):
        # Independent host membership, never a value supplied by the grant.
        member = self._members()[node_id]
        endpoint = member.get('control_endpoint_id', member['endpoint_id'])
        if not isinstance(endpoint, str) or not endpoint:
            raise TokenPathAuthorityError('token_path_membership_invalid')
        return endpoint

    def peers(self):
        # Detach live mappings so mutation cannot change an admitted snapshot.
        return json.loads(canonical_json_bytes(dict(self._peers())))

    def admit(self, *, envelope, request, control, manifest, graph, node_id, current):
        validate_manifest(manifest, graph)
        placements = {p.placement_id: p.node_id for s in graph.stages for p in s.placements}
        nodes = list(dict.fromkeys(placements[h.placement_id] for h in manifest.ordered_hops))
        _require(node_id in nodes and self.run_id and callable(current))
        _require(manifest.request_id == request.get('request_id')
            and manifest.path_id == control['path_id']
            and manifest.path_attempt == control['path_attempt']
            and graph.deployment_id == control['deployment_id']
            and graph.deployment_epoch == control['deployment_epoch']
            and graph.topology_version == control['topology_generation']
            and sha256_bytes(canonical_json_bytes(path_manifest_to_dict(manifest))) == control['path_digest'])
        members = json.loads(canonical_json_bytes(dict(self._members())))
        try:
            selected = {n: members[n] for n in nodes}
            participants = [{k: selected[n][k] for k in ('node_id', 'endpoint_id', 'generation')}
                            for n in nodes]
            _require(all(selected[n]['node_id'] == n for n in nodes))
            entry = selected[nodes[0]]
            owner_key = json.loads(self._owner_key)
            scope = dict(run_id=self.run_id, request_id=request['request_id'],
                requester_id=self.requester_id, owner_endpoint_id=self.owner_endpoint_id,
                owner_key_digest=owner_key['verification_key_digest'],
                entry_node_id=nodes[0], entry_endpoint_id=entry['endpoint_id'],
                entry_key_digest=entry['verification_key_digest'], participants=participants,
                model_digest=graph.manifest_digest, control=dict(control),
                request_digest=sha256_bytes(canonical_json_bytes(request)))
        except (KeyError, TypeError):
            raise TokenPathAuthorityError('token_path_membership_invalid') from None
        membership_bytes = canonical_json_bytes(selected)
        def authority_current():
            try:
                latest = self._members()
                return (current() is True
                    and canonical_json_bytes({n: latest[n] for n in nodes}) == membership_bytes)
            except Exception:
                return False
        _require(authority_current(), 'token_path_grant_stale')
        peers = self.peers()
        _require(all(canonical_json_bytes(peers.get(p['node_id'])) == canonical_json_bytes(p)
                     for p in participants), 'token_path_peer_stale')
        budget = request.get('max_new_tokens')
        _require(type(budget) is int and 1 <= budget <= self._max_tokens,
                 'token_path_grant_over_budget')
        body = envelope.get('statement', {})
        _require(type(body.get('max_new_tokens')) is int and body['max_new_tokens'] == budget)
        # Sampling the wall clock must not restart a monotonic budget.
        now = self.clock()
        grant = self._registry.admit(envelope, expected_scope=scope, owner_key=owner_key,
            max_new_tokens=budget, stop_token_ids=self._stops,
            now_unix_ms=self._unix_ms(), now_monotonic_s=now,
            process_deadline_monotonic_s=self._process_deadline,
            lease_deadline_monotonic_s=self._lease_deadline,
            clock_uncertainty_ms=self._uncertainty)
        return grant, authority_current
