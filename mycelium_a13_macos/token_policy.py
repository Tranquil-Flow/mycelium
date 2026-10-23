# SPDX-License-Identifier: AGPL-3.0-only
"""Target-side policy/cursor state tied to successful native configure/start.

Local host pins are not managed payloads. Installation is single-use even when
its response is lost; uncertain work requires ordinary run reconciliation.
"""
import threading

from mycelium_node.token_provisioning import load_host_trust
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.token_path_authority import TokenPathAuthorityError


def member_pin(client, member):
    import base64
    key = client.signer.public_key_record()
    if (member.get('endpoint_id') != client.signer.endpoint_id
            or member.get('verification_key_digest') != key['verification_key_digest']
            or type(member.get('generation')) is not int or member['generation'] < 1):
        raise TokenPathAuthorityError('token_path_policy_stale')
    return dict(node_id=member['node_id'], endpoint_id=base64.b64decode(key['verification_key'], validate=True).hex(),
        generation=member['generation'], control_endpoint_id=member['endpoint_id'], verification_key=key)


def prepare_host_pin(*, root, client, current_member, run_id, deployment_id, max_wall_seconds):
    """Capture current authenticated membership, never extend the process budget.

    Called again immediately before install, after potentially slow compilation.
    The callback is the host's authenticated membership reader, not wire input.
    Renewal may refresh ONLY the lease; identity and process origin stay pinned.
    """
    import math
    import os
    import time
    from .authority import _atomic_private_json, _load_private_json
    from .managed_control import MAX_CLOCK_SKEW_SECONDS
    if os.name != 'posix' or getattr(client, 'trusted_owner_endpoint_id', None) is None:
        return None
    member = current_member()
    local = member_pin(client, member)
    path = root/'token-host.private.json'
    stable = dict(protocol='mycelium.token_path_host_pin.v1',run_id=run_id,deployment_id=deployment_id,
        node_id=local['node_id'], owner_endpoint_id=client.trusted_owner_endpoint_id,
        owner_key=client.trusted_owner_key_record,member=local,
        clock_uncertainty_ms=int(MAX_CLOCK_SKEW_SECONDS*1000))
    lease = member.get('lease_expires_at')
    if type(lease) not in (int,float) or not math.isfinite(lease) or lease <= time.time():
        raise TokenPathAuthorityError('token_path_policy_expired')
    previous = _load_private_json(path)
    if previous is not None:
        if any(canonical_json_bytes(previous.get(k)) != canonical_json_bytes(v) for k,v in stable.items()):
            raise TokenPathAuthorityError('token_path_host_pin_stale')
        if previous.get('lease_expires_at_unix_ms') != int(lease*1000):
            _atomic_private_json(path, {**previous, 'lease_expires_at_unix_ms':int(lease*1000)})
        return path  # The original process ceiling never moves, even on reopen.
    _atomic_private_json(path, {**stable, 'lease_expires_at_unix_ms':int(lease*1000),
        'process_deadline_monotonic_s':time.monotonic()+min(1800,max_wall_seconds)})
    return path


class TargetTokenPolicy:
    def __init__(self, *, path, run_id, deployment_id, node_id, current, member=None, refresh=None):
        self.trust = load_host_trust(path, run_id=run_id, deployment_id=deployment_id,
                                    node_id=node_id, current=current, member=member)
        self._refresh = refresh
        self.graph = None
        self._peers = {}
        self.installed = None
        self._pending = None
        self._lock = threading.RLock()

    def configured(self, graph_document):
        from physical_inference_node import execution_graph_from_document
        graph = execution_graph_from_document(graph_document)
        with self._lock:
            if self.installed is not None or self._pending is not None:
                raise TokenPathAuthorityError('token_path_policy_already_installed')
            self.graph = graph

    def peers(self):
        with self._lock:
            return {n:dict(p) for n,p in self._peers.items()}

    def before(self, command, payload):
        if command != 'install_token_path':return
        with self._lock:
            if self.graph is None or not self._peers:
                raise TokenPathAuthorityError('token_path_policy_unconfigured')
            if self._refresh is not None:
                self._refresh()
            admitted = self.trust.admit(payload['policy'], graph=self.graph, peers=self.peers)
            self._pending = (admitted, sha256_bytes(canonical_json_bytes(payload['policy']['statement'])))

    def after(self, command, payload, response):
        if response.get('ok') is not True:return
        with self._lock:
            if command == 'configure':
                self.configured(payload['graph'])
            elif command == 'start':
                member = self.trust.member()
                self._peers = {p['node_id']:{k:p[k] for k in ('node_id','endpoint_id','generation')}
                               for p in (payload['peer'], *payload.get('peers', []))}
                self._peers[self.trust.node_id] = dict(node_id=self.trust.node_id,
                    endpoint_id=member['endpoint_id'], generation=payload['local_generation'])
            elif command == 'rotate':
                p = payload['peer']
                self._peers[p['node_id']] = {k:p[k] for k in ('node_id','endpoint_id','generation')}
            elif command == 'install_token_path':
                observation = response.get('result', {}).get('observation', {})
                if (self._pending is None or observation.get('event') != 'token_path_installed'
                        or observation.get('details', {}).get('policy_digest') != self._pending[1]):
                    raise TokenPathAuthorityError('token_path_policy_install_mismatch')
                self.installed = self._pending[0]
                self._pending = None

    def cursor(self, command):
        with self._lock:
            if self.installed is None:
                raise TokenPathAuthorityError('token_path_activation_unprovisioned')
            if (command.get('command') != 'infer_start' or command.get('run_id') != self.trust.run_id
                    or command.get('deployment_id') != self.trust.deployment_id):
                raise TokenPathAuthorityError('token_path_policy_mismatch')
            return self.installed.cursor(command['payload'])

    def current(self):
        with self._lock:
            return self.installed is None or self.installed.authority_current()
