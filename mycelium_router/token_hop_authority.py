# SPDX-License-Identifier: AGPL-3.0-only
"""Default-absent relay fences for independently admitted token-path grants.

Installation is a trusted native-control operation, never inferred from a hop.
This gate does not authorize successor/recovery or speculative VERIFY frames.
Those need separately committed continuation authority, not a local grant edit.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import threading
import time
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from .serialization import path_manifest_to_dict
from .token_path_authority import AdmittedGrant, TokenPathAuthorityError
from .validation import validate_manifest


@dataclass
class _Route:
    grant: AdmittedGrant
    body: dict[str, Any]
    hops: tuple[tuple[str, str], ...]
    prompt_tokens: int
    current: Callable[[], bool]
    # Per-local-stage state: prefill chunk index, covered prompt, next decode.
    positions: dict[int, tuple[int, int, int]] = field(default_factory=dict)


class GrantedHopGate:
    def __init__(self, *, node_id: str,
                 peers: Callable[[], Mapping[str, Mapping[str, Any]]],
                 clock: Callable[[], float] = time.monotonic, capacity: int = 4096):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise TokenPathAuthorityError('token_path_grant_capacity')
        self.node_id = node_id
        self._peers = peers
        self._clock = clock
        self._capacity = capacity
        self._routes: dict[str, _Route] = {}
        self._lock = threading.Lock()

    def install(self, *, grant: AdmittedGrant, request: Mapping[str, Any],
                manifest: Any, graph: Any, current: Callable[[], bool]) -> None:
        if not isinstance(grant, AdmittedGrant) or not callable(current):
            raise TokenPathAuthorityError('token_path_hop_mismatch')
        body = grant.statement()
        scope, control = body['scope'], body['scope']['control']
        try:
            validate_manifest(manifest, graph)
            placements = {p.placement_id: p.node_id for s in graph.stages for p in s.placements}
            hops = tuple((h.placement_id, placements[h.placement_id]) for h in manifest.ordered_hops)
            matches = (manifest.request_id == scope['request_id'] == request['request_id']
                and manifest.path_id == control['path_id']
                and manifest.path_attempt == control['path_attempt']
                and graph.deployment_id == control['deployment_id']
                and graph.deployment_epoch == control['deployment_epoch']
                and graph.topology_version == control['topology_generation']
                and graph.manifest_digest == scope['model_digest']
                and sha256_bytes(canonical_json_bytes(path_manifest_to_dict(manifest))) == control['path_digest']
                and sha256_bytes(canonical_json_bytes(dict(request))) == scope['request_digest']
                and request['max_new_tokens'] == body['max_new_tokens']
                and hops[0][1] == scope['entry_node_id']
                and self.node_id in {n for _, n in hops}
                and {n for _, n in hops} == {p['node_id'] for p in scope['participants']})
        except (KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            raise TokenPathAuthorityError('token_path_hop_mismatch')
        route = _Route(grant, body, hops, len(request['prompt_token_ids']), current)
        reason = self._fence(route)
        if reason:
            raise TokenPathAuthorityError(reason)
        with self._lock:
            if scope['request_id'] in self._routes:
                raise TokenPathAuthorityError('token_path_grant_replay')
            if len(self._routes) >= self._capacity:
                raise TokenPathAuthorityError('token_path_grant_capacity')
            self._routes[scope['request_id']] = route

    def _fence(self, route: _Route) -> str | None:
        # Authority/transport callbacks never execute under the gate's lock.
        try:
            if route.current() is not True:
                return 'token_path_grant_stale'
            peers = self._peers()
            for expected in route.body['scope']['participants']:
                actual = peers.get(expected['node_id'])
                if actual is None or canonical_json_bytes(dict(actual)) != canonical_json_bytes(expected):
                    return 'token_path_peer_stale'
            if self._clock() >= route.grant.deadline_monotonic_s:
                return 'token_path_grant_expired'
        except Exception:
            return 'token_path_grant_stale'
        return None

    def _lookup(self, value: Any) -> tuple[_Route | None, str | None]:
        with self._lock:
            route = self._routes.get(value.request_id)
        if route is None:
            return None, 'token_path_grant_missing'
        reason = self._fence(route)
        if reason:
            return route, reason
        control = route.body['scope']['control']
        if (value.path_id != control['path_id'] or type(value.path_attempt) is not int
                or value.path_attempt != control['path_attempt']
                or type(value.hop_index) is not int or not 0 <= value.hop_index < len(route.hops)):
            return route, 'token_path_hop_mismatch'
        placement, node = route.hops[value.hop_index]
        actual_placement = getattr(value, 'destination_placement_id', getattr(value, 'placement_id', None))
        if node != self.node_id or actual_placement != placement:
            return route, 'token_path_hop_mismatch'
        if hasattr(value, 'topology_version') and (type(value.topology_version) is not int
                or value.topology_version != control['topology_generation']):
            return route, 'token_path_hop_mismatch'
        key = getattr(value, 'batch_key', None)
        if key is not None and (key.deployment_id != control['deployment_id']
                or key.deployment_epoch != control['deployment_epoch']
                or key.manifest_digest != route.body['scope']['model_digest']):
            return route, 'token_path_hop_mismatch'
        return route, None

    def _order(self, route: _Route, value: Any, *, claim: bool) -> str | None:
        if value.phase not in {'PREFILL', 'PREFILL_CHUNK', 'DECODE'}:
            return 'token_path_hop_phase'
        if type(value.token_index) is not int:
            return 'token_path_hop_order'
        if value.phase == 'DECODE' and not 1 <= value.token_index < route.body['max_new_tokens']:
            return 'token_path_hop_budget'
        with self._lock:
            chunk, covered, decode = route.positions.get(value.hop_index, (0, 0, 1))
            if value.phase == 'PREFILL':
                if value.token_index != -1 or covered != 0 or chunk != 0:
                    return 'token_path_hop_order'
                next_state = (1, route.prompt_tokens, 1)
            elif value.phase == 'PREFILL_CHUNK':
                count = value.prefill_chunk_token_count
                if (value.token_index != chunk or covered >= route.prompt_tokens
                        or type(count) is not int or count <= 0 or covered + count > route.prompt_tokens):
                    return 'token_path_hop_order'
                next_state = (chunk + 1, covered + count, 1)
            else:
                if covered != route.prompt_tokens or value.token_index != decode:
                    return 'token_path_hop_order'
                next_state = (chunk, covered, decode + 1)
            if claim:
                route.positions[value.hop_index] = next_state
        return None

    def ingress(self, header: Any, source_node_id: str | None) -> str | None:
        route, reason = self._lookup(header)
        if reason:
            return reason
        assert route is not None
        expected_source = route.hops[max(0, header.hop_index - 1)][1]
        if source_node_id != expected_source:
            return 'token_path_peer_stale'
        # Ingress does not claim compute: retransmitted cached frames stay
        # idempotent. Sequence is claimed exactly once at the runtime boundary.
        if header.phase not in {'PREFILL', 'PREFILL_CHUNK', 'DECODE'}:
            return 'token_path_hop_phase'
        if header.phase == 'DECODE' and (type(header.token_index) is not int
                or not 1 <= header.token_index < route.body['max_new_tokens']):
            return 'token_path_hop_budget'
        return None

    def claim(self, item: Any) -> str | None:
        route, reason = self._lookup(item)
        if reason:
            return reason
        assert route is not None
        return self._order(route, item, claim=True)

    def fence(self, item: Any) -> str | None:
        return self._lookup(item)[1]
