# SPDX-License-Identifier: AGPL-3.0-only
"""Same-installation target identity with explicit, pinned owner-local TLS.

This is one logical contributor on ONE physical Mac. It neither supplies a
second-host claim nor authorizes model work. The normal target membership,
staging, work-consent and resource gates remain authoritative.
"""
from pathlib import Path
from .peer import RemoteMacMember,RemoteMacMemberError
from .target_app import TargetApplication
from .authority import DemoAuthority,_private_directory
from .tls import pinned_seed_client
from .managed_control import ManagedControlClient
from mycelium_invite import verify_invite_bundle
from mycelium_node.durable_membership import load_membership_state


class LocalOwnerMember(RemoteMacMember):
    def __init__(self,root,*,owner):
        if not isinstance(owner,DemoAuthority):raise ValueError('local_owner_required')
        super().__init__(root);self.owner=owner

    def _make_client(self,*,bundle=None,membership=None):
        owner=self.owner;coordinator=owner.coordinator
        if coordinator is None or owner.seed_server is None:raise ValueError('local_owner_unavailable')
        if (bundle is None)==(membership is None):raise ValueError('peer_state_invalid')
        if bundle is not None:
            v=verify_invite_bundle(bundle,now=self._clock())
            membership={'seed_url':v['payload']['seed_url'],'swarm_id':v['payload']['swarm_id'],
                        'seed_key_digest':v['seed_key_digest'],'seed_key_records':v['seed_key_records']}
        if (membership['swarm_id']!=coordinator.swarm_id
            or membership['seed_key_digest']!=coordinator.signer.verification_key_digest):
            raise ValueError('local_owner_pin_mismatch')
        # A local owner restart may select another ephemeral loopback port;
        # authority comes from the same durable signer, not the old port number.
        return pinned_seed_client(seed_url=coordinator.seed_url,swarm_id=coordinator.swarm_id,
            seed_key_digest=membership['seed_key_digest'],seed_key_records=membership['seed_key_records'],
            transport_url=owner.seed_server.base_url,ca_certificate=owner.seed_server.material.ca_certificate,
            timeout=self._timeout)

    def start(self):
        # Retained local membership tracks an owner-local address change only
        # after comparing the same in-process owner's durable key and swarm.
        from mycelium_node.durable_membership import save_membership_state
        m=load_membership_state(self.state_root)
        c=self.owner.coordinator
        if m is not None and c is not None and m['seed_url']!=c.seed_url:
            if m['swarm_id']!=c.swarm_id or m['seed_key_digest']!=c.signer.verification_key_digest:
                raise ValueError('local_owner_pin_mismatch')
            save_membership_state(self.state_root,{**m,'seed_url':c.seed_url})
        return super().start()

    def _control_client(self,*,contribution_required,_test_ca_certificate=None):
        self._control_snapshot(contribution_required=contribution_required)
        m=load_membership_state(self.state_root);c=self.owner.coordinator
        if c is None or m['seed_key_digest']!=c.signer.verification_key_digest or m['swarm_id']!=c.swarm_id:
            raise ValueError('local_owner_pin_mismatch')
        return ManagedControlClient.for_local_tls(base_url=self.owner.seed_server.base_url,
            signer=self._signer,trusted_owner_key_record=c.signer.public_key_record(),
            # The same durable owner signer was authenticated against the
            # accepted membership above. Never derive native trust from work.
            trusted_owner_endpoint_id=c.signer.endpoint_id,
            swarm_id=c.swarm_id,owner_node_id=c.seed_node_id,timeout=self._timeout,clock=self._clock,
            ca_certificate=self.owner.seed_server.material.ca_certificate)


class LocalContributor(TargetApplication):
    HONOURS_TEST_CLOCK_SEAM=False  # the owner's own node: the A13 clock test seam is member-only
    def __init__(self,owner_app):
        super().__init__(owner_app.root/'local-contributor')
        self.peer=LocalOwnerMember(owner_app.root/'local-contributor',owner=owner_app.authority)
        self.owner_root=owner_app.root
        self.owner_app=owner_app

    def _preposition_cache_root(self,offer):
        if offer.get('preposition') is None:return None
        # The owner's CURRENT acquisition cache: a non-default run model keeps
        # its own model-source-<commit12> root (app._open_acquisition).
        acquisition=getattr(self.owner_app,'acquisition',None)
        root=(self.owner_root/'model-source') if acquisition is None else acquisition.state_root
        if root.resolve(strict=True)!=root:raise ValueError('local_owner_cache_invalid')
        return _private_directory(root,create=False)


def create_local_contributor(owner_app):
    from .app import read_resource_policy
    policy=read_resource_policy(owner_app.root)
    if (owner_app.installation_role!='owner' or policy is None or not owner_app.running
        or owner_app.authority.status().get('membership_state')!='current'):
        raise ValueError('local_contributor_owner_consent_required')
    actor=LocalContributor(owner_app)
    try:
        actor.start()
        if actor.member is None:
            handoff=owner_app.authority.approve_pairing(actor.pair_request(),ttl_seconds=120,
                quota={'storage_bytes':policy['storage_bytes'],'network_bytes_per_day':policy['storage_bytes'],
                       'background_allowed':False})
            actor.accept_pairing(handoff)
        return actor
    except BaseException:
        actor.stop();raise
