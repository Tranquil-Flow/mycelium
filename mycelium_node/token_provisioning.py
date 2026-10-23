# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-signed route bootstrap, independent of per-request token grants.

The trust root is supplied by the local authenticated host, never by this wire
statement. One installation per native process retains replay registries. This
module alone neither installs a managed worker nor enables operator selection.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_router.token_path_authority import ReceiptCursor, TokenPathAuthorityError
from mycelium_router.serialization import path_manifest_from_dict
from .token_participants import ParticipantAdmissionPolicy

PROTOCOL = 'mycelium.token_path_route_policy.v1'
_FIELDS = {'protocol', 'run_id', 'deployment_id', 'model_digest', 'requester_id',
           'owner_endpoint_id', 'members', 'max_new_tokens', 'stop_token_ids',
           'expires_at_unix_ms'}
_MEMBER_FIELDS = {'node_id', 'endpoint_id', 'generation', 'control_endpoint_id', 'verification_key'}


def _require(condition, code='token_path_policy_invalid'):
    if not condition:
        raise TokenPathAuthorityError(code)


def _copy(value):
    return json.loads(canonical_json_bytes(value))


def _name(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 256 and value == value.strip()


def _validate(body):
    _require(isinstance(body, dict) and set(body) == _FIELDS and body['protocol'] == PROTOCOL)
    _require(all(_name(body[k]) for k in ('run_id', 'deployment_id', 'requester_id', 'owner_endpoint_id')))
    _require(isinstance(body['model_digest'], str)
             and re.fullmatch(r'sha256:[0-9a-f]{64}', body['model_digest']) is not None)
    _require(type(body['max_new_tokens']) is int and 1 <= body['max_new_tokens'] <= 65536)
    stops = body['stop_token_ids']
    _require(isinstance(stops, list) and len(stops) <= 128
             and all(type(t) is int and 0 <= t <= 2**31-1 for t in stops)
             and len(set(stops)) == len(stops))
    _require(type(body['expires_at_unix_ms']) is int and body['expires_at_unix_ms'] > 0)
    members = body['members']
    _require(isinstance(members, dict) and 1 <= len(members) <= 64)
    for node, member in members.items():
        _require(_name(node) and isinstance(member, dict) and set(member) == _MEMBER_FIELDS
                 and member['node_id'] == node and _name(member['endpoint_id'])
                 and _name(member['control_endpoint_id']) and type(member['generation']) is int
                 and 1 <= member['generation'] <= 2**64-1)
        build_ed25519_verifier([member['verification_key']])
    _require(len({m['endpoint_id'] for m in members.values()}) == len(members)
             and len({m['control_endpoint_id'] for m in members.values()}) == len(members))


def valid_policy_payload(payload):
    """Closed bounded wire grammar; signature authority is checked at install."""
    try:
        _require(isinstance(payload, dict) and set(payload) == {'policy'})
        envelope = payload['policy']
        _require(isinstance(envelope, dict) and set(envelope) == {'statement', 'signature'}
                 and len(canonical_json_bytes(envelope)) <= 65536)
        _validate(envelope['statement'])
        from mycelium_qualification.signing import SIGNATURE_FIELDS
        _require(isinstance(envelope['signature'], dict) and set(envelope['signature']) == SIGNATURE_FIELDS
                 and all(isinstance(v, str) for v in envelope['signature'].values()))
        return True
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def sign_route_policy(*, signer, run_id, deployment_id, model_digest, requester_id,
                      members, max_new_tokens, stop_token_ids, expires_at_unix_ms):
    """Sign independently derived route/membership pins, not an incoming grant."""
    body = _copy(dict(protocol=PROTOCOL, run_id=run_id, deployment_id=deployment_id,
        model_digest=model_digest, requester_id=requester_id, owner_endpoint_id=signer.endpoint_id,
        members=members, max_new_tokens=max_new_tokens, stop_token_ids=list(stop_token_ids),
        expires_at_unix_ms=expires_at_unix_ms))
    _validate(body)
    envelope = {'statement': body, 'signature': signer.sign(body)}
    _require(len(canonical_json_bytes(envelope)) <= 65536)
    return envelope


HOST_PIN_PROTOCOL = 'mycelium.token_path_host_pin.v1'


def load_host_trust(path, *, run_id, deployment_id, node_id, current, member=None,
                    clock=time.monotonic, unix_ms=lambda: int(time.time()*1000)):
    """Read a local private host pin; this path is never a managed payload.

    The local parent's monotonic ceiling survives native process replacement.
    The lease is translated once, before reading a potentially slow wall clock.
    """
    import os
    import stat
    from pathlib import Path
    path = Path(path)
    _require(not path.is_symlink() and path.resolve(strict=True) == path,
             'token_path_host_pin_invalid')
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 16384,
                 'token_path_host_pin_invalid')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(16385)
        _require(len(raw) <= 16384, 'token_path_host_pin_invalid')
        pin = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise TokenPathAuthorityError('token_path_host_pin_invalid') from exc
    finally:
        if fd is not None:os.close(fd)
    _require(isinstance(pin, dict) and set(pin) == {'protocol','run_id','deployment_id','node_id',
        'owner_endpoint_id','owner_key','member','process_deadline_monotonic_s',
        'lease_expires_at_unix_ms','clock_uncertainty_ms'}
        and pin['protocol'] == HOST_PIN_PROTOCOL and pin['run_id'] == run_id
        and pin['deployment_id'] == deployment_id and pin['node_id'] == node_id,
        'token_path_host_pin_invalid')
    _require(isinstance(pin['member'], dict) and set(pin['member']) == _MEMBER_FIELDS
             and pin['member']['node_id'] == node_id, 'token_path_host_pin_invalid')
    build_ed25519_verifier([pin['member']['verification_key']])
    _require(type(pin['lease_expires_at_unix_ms']) is int
             and type(pin['clock_uncertainty_ms']) is int and 0 <= pin['clock_uncertainty_ms'] <= 60000
             and type(pin['process_deadline_monotonic_s']) in (float,int)
             and math.isfinite(pin['process_deadline_monotonic_s']), 'token_path_host_pin_invalid')
    now = clock()
    lease = now + (pin['lease_expires_at_unix_ms'] - unix_ms() - pin['clock_uncertainty_ms']) / 1000
    ceiling = min(pin['process_deadline_monotonic_s'], now+1800)
    # Expired optional pins do not break legacy startup; admit still refuses
    # before any policy installation or autonomous work.
    local_member = canonical_json_bytes(pin['member'])
    return RoutePolicyTrust(run_id=run_id, deployment_id=deployment_id, node_id=node_id,
        owner_endpoint_id=pin['owner_endpoint_id'], owner_key=pin['owner_key'],
        member=(lambda: json.loads(local_member)) if member is None else member, current=current,
        process_deadline_monotonic_s=ceiling, lease_deadline_monotonic_s=lease,
        clock_uncertainty_ms=pin['clock_uncertainty_ms'], clock=clock, unix_ms=unix_ms)


class RoutePolicyTrust:
    """One process-lifetime host pin; absence is the default-OFF boundary."""
    def __init__(self, *, run_id, deployment_id, node_id, owner_endpoint_id, owner_key,
                 member, current, process_deadline_monotonic_s, lease_deadline_monotonic_s,
                 clock_uncertainty_ms, clock=time.monotonic, unix_ms=lambda: int(time.time()*1000)):
        _require(all(_name(v) for v in (run_id, deployment_id, node_id, owner_endpoint_id)))
        _require(all(callable(f) for f in (member, current, clock, unix_ms)))
        _require(all(type(v) in (int, float) and math.isfinite(v)
                     for v in (process_deadline_monotonic_s, lease_deadline_monotonic_s)))
        _require(type(clock_uncertainty_ms) is int and 0 <= clock_uncertainty_ms <= 60000)
        self.run_id, self.deployment_id, self.node_id = run_id, deployment_id, node_id
        self.owner_endpoint_id = owner_endpoint_id
        self._key_bytes = canonical_json_bytes(owner_key)
        self._verify = build_ed25519_verifier([json.loads(self._key_bytes)])
        self.member, self.current, self.clock, self.unix_ms = member, current, clock, unix_ms
        self.ceiling = min(process_deadline_monotonic_s, lease_deadline_monotonic_s)
        self.uncertainty = clock_uncertainty_ms
        self._lock = threading.Lock()
        self._installed = False

    def admit(self, envelope, *, graph, peers):
        with self._lock:
            _require(not self._installed, 'token_path_policy_already_installed')
            _require(callable(peers) and isinstance(envelope, dict)
                     and set(envelope) == {'statement', 'signature'})
            encoded = canonical_json_bytes(envelope)
            _require(len(encoded) <= 65536)
            envelope = json.loads(encoded)
            body, signature = envelope['statement'], envelope['signature']
            _require(isinstance(signature, dict)
                     and signature.get('signer_endpoint_id') == self.owner_endpoint_id
                     and self._verify(canonical_json_bytes(body), signature), 'token_path_policy_signature')
            _validate(body)
            _require(body['owner_endpoint_id'] == self.owner_endpoint_id
                     and body['run_id'] == self.run_id and body['deployment_id'] == self.deployment_id
                     and graph.deployment_id == self.deployment_id
                     and graph.manifest_digest == body['model_digest'], 'token_path_policy_mismatch')
            graph_nodes = {p.node_id for s in graph.stages for p in s.placements}
            _require(self.node_id in body['members'] and set(body['members']) == graph_nodes,
                     'token_path_policy_mismatch')
            now = self.clock()  # Before potentially slow wall-clock/current-member sampling.
            deadline = min(self.ceiling, now + (body['expires_at_unix_ms']-self.unix_ms()-self.uncertainty)/1000)
            _require(deadline > self.clock(), 'token_path_policy_expired')
            admitted = InstalledRoutePolicy(trust=self, body=body, graph=graph, peers=peers, deadline=deadline)
            _require(admitted.current(), 'token_path_policy_stale')
            admitted.check_peers()
            self._installed = True
            return admitted


class InstalledRoutePolicy:
    def __init__(self, *, trust, body, graph, peers, deadline):
        self._trust, self._body, self._graph, self._peers = trust, canonical_json_bytes(body), graph, peers
        self.deadline_monotonic_s = deadline
        self._participant = self._make_policy()
        # The target reader and native participant each need a distinct registry;
        # neither may create a fresh registry on each request.
        self._cursor_policy = self._make_policy()

    def document(self):
        return json.loads(self._body)

    def current(self):
        return self._trust.clock() < self.deadline_monotonic_s and self.authority_current()

    def authority_current(self):
        try:
            t = self._trust
            return (t.current() is True
                    and canonical_json_bytes(t.member()) == canonical_json_bytes(self.document()['members'][t.node_id]))
        except Exception:
            return False

    def check_peers(self):
        expected = {n: {k: m[k] for k in ('node_id', 'endpoint_id', 'generation')}
                    for n, m in self.document()['members'].items()}
        actual = self._peers()
        _require(all(canonical_json_bytes(actual.get(n)) == canonical_json_bytes(p)
                     for n, p in expected.items()), 'token_path_peer_stale')

    def _members(self):
        _require(self.current(), 'token_path_policy_stale')
        return {n: {k: v for k, v in m.items() if k != 'verification_key'} |
                {'verification_key_digest': m['verification_key']['verification_key_digest']}
                for n, m in self.document()['members'].items()}

    def _make_policy(self):
        b, t = self.document(), self._trust
        return ParticipantAdmissionPolicy(run_id=t.run_id, requester_id=b['requester_id'],
            owner_endpoint_id=t.owner_endpoint_id, owner_key=json.loads(t._key_bytes),
            members=self._members, peers=self._peers, max_new_tokens=b['max_new_tokens'],
            stop_token_ids=tuple(b['stop_token_ids']), process_deadline_monotonic_s=t.ceiling,
            lease_deadline_monotonic_s=self.deadline_monotonic_s,
            clock_uncertainty_ms=t.uncertainty, clock=t.clock, unix_ms=t.unix_ms)

    def participant_policy(self):
        return self._participant

    def cursor(self, payload):
        _require(isinstance(payload, dict) and set(payload) == {'request', 'control', 'path_manifest', 'token_grant'})
        manifest = path_manifest_from_dict(payload['path_manifest'])
        placements = {p.placement_id: p.node_id for s in self._graph.stages for p in s.placements}
        entry = placements[manifest.ordered_hops[0].placement_id]
        _require(entry == self._trust.node_id, 'token_path_policy_mismatch')
        grant, current = self._cursor_policy.admit(envelope=payload['token_grant'], request=payload['request'],
            control=payload['control'], manifest=manifest, graph=self._graph,
            node_id=self._trust.node_id, current=self.current)
        return ReceiptCursor(grant=grant, entry_key=self.document()['members'][entry]['verification_key'],
                             clock=self._trust.clock, current=current)
