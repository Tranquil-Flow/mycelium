# SPDX-License-Identifier: AGPL-3.0-only
"""Installed owner bootstrap using admitted membership and verified native keys.

A flag selects this path but supplies no authority. Startup qualification remains
separate. Signed installation must succeed on every participant before a request
grant exists; uncertain installation is never retried as legacy inference.
"""
import base64
import json
import math
import os
from pathlib import Path
import threading
import time

from mycelium_live.token_path_activation import token_path_enabled
from mycelium_live.token_path_owner import OwnerTokenPath
from mycelium_node.token_provisioning import sign_route_policy
from mycelium_qualification.evidence import canonical_json_bytes,sha256_bytes
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_router.serialization import path_manifest_from_dict
from mycelium_router.validation import validate_manifest
from .authority import _private_directory
from .managed_control import MAX_CLOCK_SKEW_SECONDS


class ManagedRouteTokenOwner:
    def __init__(self, *, signer, current_member, mailbox, evidence_root,
                 process_deadline_monotonic_s, clock_uncertainty_ms=None):
        self.enabled = token_path_enabled()
        self.signer,self.current_member,self.mailbox = signer,current_member,mailbox
        self.root = Path(evidence_root)
        self.deadline = process_deadline_monotonic_s
        self.uncertainty = int(MAX_CLOCK_SKEW_SECONDS*1000) if clock_uncertainty_ms is None else clock_uncertainty_ms
        self.route = None
        self._lock = threading.RLock()
        self._policy_members = None
        self._lease_deadline = None
        self._member_leases = {}
        self._stops = None
        self._install_started = False
        self._owners = {}
        self._sealed_bytes = 0

    @property
    def required(self):
        return self.enabled

    def attach_route(self, route):
        with self._lock:
            if self.route is not None:
                raise RuntimeError('token_path_owner_already_attached')
            self.route = route

    def _members(self):
        r = self.route
        if r is None or r._closed:
            raise RuntimeError('token_path_owner_route_unavailable')
        nodes = {p.node_id for stage in r._graph.stages for p in stage.placements}
        members, leases = {}, []
        for node in sorted(nodes):
            member = self.current_member(node)
            key = r._verification_keys[node]
            build_ed25519_verifier([key])
            endpoint = r._endpoints[node]
            if (member['node_id'] != node or type(member['generation']) is not int
                    or member['generation'] != endpoint['membership_generation']
                    or member['verification_key_digest'] != key['verification_key_digest']
                    or base64.b64decode(key['verification_key'],validate=True).hex() != endpoint['endpoint_id']
                    or type(member['lease_expires_at']) not in (int,float)
                    or not math.isfinite(member['lease_expires_at'])):
                raise RuntimeError('token_path_owner_membership_mismatch')
            members[node] = dict(node_id=node,endpoint_id=endpoint['endpoint_id'],generation=member['generation'],
                control_endpoint_id=member['endpoint_id'],verification_key=key)
            # Translate each authenticated lease only when it changes. A wall
            # clock rollback cannot extend an unchanged lease.
            now = time.monotonic()
            lease = member['lease_expires_at']
            previous = self._member_leases.get(node)
            if previous is None or previous[0] != lease:
                previous = (lease, now + lease - time.time())
                self._member_leases[node] = previous
            if now >= previous[1] - self.uncertainty/1000:
                raise RuntimeError('token_path_policy_expired')
            leases.append(previous[1])
        return json.loads(canonical_json_bytes(members)), min(leases)

    def _refresh_lease(self):
        with self._lock:
            if time.monotonic() >= self.deadline:
                raise RuntimeError('token_path_process_expired')
            members, lease = self._members()
            if canonical_json_bytes(members) != self._policy_members:
                raise RuntimeError('token_path_grant_stale')
            self._lease_deadline = min(self.deadline, lease)
            return self._lease_deadline

    def current(self):
        try:
            return time.monotonic() < self._refresh_lease()
        except Exception:
            return False

    def seal(self, raw):
        # Exact bytes are private evidence. Never put receipt tokens in logs.
        from mycelium_router.token_path_authority import MAX_RECEIPT_BYTES
        if not isinstance(raw,bytes) or len(raw)>MAX_RECEIPT_BYTES:
            raise RuntimeError('token_path_evidence_invalid')
        root = _private_directory(self.root,create=True)
        path = root/(sha256_bytes(raw).split(':',1)[1]+'.receipt')
        with self._lock:
            if path.exists():
                if path.is_symlink() or path.read_bytes()!=raw:
                    raise RuntimeError('token_path_evidence_conflict')
                return
            if self._sealed_bytes+len(raw)>256*1024*1024:
                raise RuntimeError('token_path_evidence_capacity')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                with os.fdopen(fd,'wb') as output:
                    output.write(raw);output.flush();os.fsync(output.fileno())
            except BaseException:
                # Preserve incomplete evidence; it cannot become an ACK on retry.
                raise
            self._sealed_bytes+=len(raw)

    def install(self, *, stop_token_ids):
        """Qualify signed participant installation without issuing any grant."""
        if not self.enabled:return
        with self._lock:
            r=self.route
            if r is None:raise RuntimeError('token_path_owner_route_unavailable')
            if time.monotonic() >= self.deadline:
                raise RuntimeError('token_path_process_expired')
            members,lease=self._members()
            stops=tuple(stop_token_ids)
            if not self._install_started:
                now=time.monotonic()
                self._lease_deadline=min(self.deadline,lease)
                # Install once for the fixed process lifetime; every request is
                # still clamped to current authenticated membership and its TTL.
                remaining=self.deadline-now
                if remaining<=0:raise RuntimeError('token_path_grant_expired')
                policy=sign_route_policy(signer=self.signer,run_id=r._plan['run_id'],
                    deployment_id=r._plan['deployment_id'],model_digest=r._graph.manifest_digest,
                    requester_id='managed-owner',members=members,max_new_tokens=65536,
                    stop_token_ids=stops,expires_at_unix_ms=int(time.time()*1000+remaining*1000))
                self._install_started=True  # Partial/uncertain installs must not be replayed.
                for node in sorted(members):
                    response=r._sessions[node].send(command_id=r._command_id(node,'install-token-path'),
                        command='install_token_path',payload={'policy':policy})
                    observation=r._verify_observation(node,response,event='token_path_installed')
                    if observation['details'].get('policy_digest')!=sha256_bytes(canonical_json_bytes(policy['statement'])):
                        raise RuntimeError('token_path_policy_install_mismatch')
                self._policy_members=canonical_json_bytes(members)
                self._stops=stops
            self._refresh_lease()
            if stops!=self._stops:
                raise RuntimeError('token_path_grant_stale')

    def prepare(self, *, command_id, request, control, manifest, participating_node_ids, stop_token_ids):
        if not self.enabled:return None
        with self._lock:
            r=self.route
            if r is None:raise RuntimeError('token_path_owner_route_unavailable')
            path=path_manifest_from_dict(manifest)
            validate_manifest(path,r._graph)
            placements={p.placement_id:p.node_id for stage in r._graph.stages for p in stage.placements}
            nodes=list(dict.fromkeys(placements[h.placement_id] for h in path.ordered_hops))
            if set(nodes)!=set(participating_node_ids):raise RuntimeError('token_path_owner_scope_mismatch')
            self.install(stop_token_ids=stop_token_ids)
            members,_=self._members()
            stops=tuple(stop_token_ids)
            entry=nodes[0]
            owner_key=(entry,tuple(nodes))
            if owner_key not in self._owners:
                pins=dict(run_id=r._plan['run_id'],requester_id='managed-owner',owner_endpoint_id=self.signer.endpoint_id,
                    owner_key_digest=self.signer.verification_key_digest,entry_node_id=entry,
                    entry_endpoint_id=members[entry]['endpoint_id'],
                    entry_key_digest=members[entry]['verification_key']['verification_key_digest'],
                    participants=[{k:members[n][k] for k in ('node_id','endpoint_id','generation')} for n in nodes],
                    model_digest=r._graph.manifest_digest)
                self._owners[owner_key]=OwnerTokenPath(signer=self.signer,scope_pins=pins,
                    entry_key=members[entry]['verification_key'],mailbox=self.mailbox,seal=self.seal,
                    current=self.current,capable=lambda node:node in members,max_new_tokens=65536,
                    stop_token_ids=stops,process_deadline_monotonic_s=self.deadline,
                    lease_deadline_monotonic_s=self._lease_deadline,clock_uncertainty_ms=self.uncertainty,
                    control_endpoint_id=members[entry]['control_endpoint_id'],refresh_lease=self._refresh_lease)
            return self._owners[owner_key].prepare(command_id=command_id,request=request,control=control,
                manifest=manifest,participating_node_ids=participating_node_ids,stop_token_ids=stops)
