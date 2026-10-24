# SPDX-License-Identifier: AGPL-3.0-only
"""Packaged-app identity -> target runtime -> native token-policy composition.

Fresh real TLS membership, durable identities and host pins. The tiny graph and
configured router are CPU fixtures; no model, device or public authority.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
import time

import pytest

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.local_contributor import create_local_contributor
from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite
from mycelium_a13_macos.token_policy import member_pin
from mycelium_node.token_provisioning import sign_route_policy
from physical_inference_node import PhysicalNodeService
from test_app_authority import CONSENT
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from tests.live.test_token_path_admission import world, real_prefill_node


@contextmanager
def enrolled_apps(root):
    owner = DemoApp(root/'owner', check_platform=False).start()
    local = remote = None
    try:
        owner.action('/api/create', {'consent': CONSENT})
        local = create_local_contributor(owner)
        ca = owner.authority.seed_server.material.ca_certificate
        def client(*, bundle=None, membership=None, now, timeout):
            if bundle is not None:
                return pinned_seed_client_from_invite(bundle, now=now, ca_certificate=ca, timeout=timeout)
            return pinned_seed_client(seed_url=membership['seed_url'], swarm_id=membership['swarm_id'],
                seed_key_digest=membership['seed_key_digest'], seed_key_records=membership['seed_key_records'],
                ca_certificate=ca, timeout=timeout)
        remote = TargetApplication(root/'member', _test_peer_factory=lambda path:
            RemoteMacMember(path, _test_seed_client_factory=client))
        remote.start()
        remote.accept_pairing(owner.authority.approve_pairing(remote.pair_request(), ttl_seconds=120,
            quota={'storage_bytes': CONSENT['storage_bytes'],
                   'network_bytes_per_day': CONSENT['storage_bytes'], 'background_allowed': False}))
        for app in (local, remote):
            app.peer.set_contribution_enabled(enabled=True, expected_generation=app.member.generation)
        yield owner, local, remote
    finally:
        if remote is not None: remote.stop()
        if local is not None: local.stop()
        owner.close()


def prepare_runtime(app, owner, root, *, deployment_id=DEPLOYMENT_ID, seconds=60, authorize_load=None):
    from mycelium_a13_macos.managed_staging import TargetStaging
    from mycelium_a13_macos.worker import WorkerLimits
    from test_managed_staging import bundle
    from test_worker import POLICY, SAFE
    data, digest = bundle()
    stage = TargetStaging(root/'mycelium-staging', node_id=app.member.node_id,
        max_archive_bytes=100_000, max_disk_bytes=2_000_000,
        current_member=lambda: True, authorize=lambda intent: True)
    stage.prepare(archive=data, archive_digest=digest)
    return app.prepare_runtime(run_id=RUN_ID, deployment_id=deployment_id,
        staging=stage, archive=data, archive_digest=digest, resource_policy=POLICY,
        limits=WorkerLimits(max_rss_bytes=2_000_000_000, max_disk_bytes=2_000_000,
                           max_wall_seconds=seconds, max_commands=100),
        sidecar_binary=root/'never-started', authorize_load=authorize_load,
        _test_observer=lambda: dict(SAFE),
        _test_ca_certificate=owner.authority.seed_server.material.ca_certificate)


@contextmanager
def app_control_world(root):
    """Use packaged owner/local/member identity constructors in the CPU child proof."""
    from mycelium_a13_macos.managed_control import ManagedControlClient
    with enrolled_apps(root) as (owner, local, remote):
        authority = owner.authority
        coordinator = authority.coordinator
        ca = authority.seed_server.material.ca_certificate
        runtimes = {}
        try:
            yield dict(app=owner, authority=authority, window=180,
                apps={a.member.node_id:a for a in (local, remote)}, runtimes=runtimes,
                member=coordinator.member(local.member.node_id),
                tail_node_id=remote.member.node_id, tail_signer=remote.member.signer,
                target_signer=local.member.signer, owner_signer=coordinator.signer,
                target_client=local.peer.managed_control_client(),
                owner_client=ManagedControlClient.for_local_tls(base_url=authority.seed_server.base_url,
                    signer=coordinator.signer, trusted_owner_key_record=coordinator.signer.public_key_record(),
                    swarm_id=coordinator.swarm_id, owner_node_id=coordinator.seed_node_id, ca_certificate=ca),
                mailbox=authority.control_mailbox)
        finally:
            for runtime in runtimes.values(): runtime.close()


def test_app_identity_provisioned_two_native_processes_complete_chat(tmp_path, world, monkeypatch):
    from test_token_two_process_join import test_owner_native_two_process_route
    test_owner_native_two_process_route(tmp_path, world, monkeypatch, True, 'signed', 'chat',
                                       startup=True, app_pair=True)


@pytest.mark.parametrize('role', ['owner', 'member'])
def test_app_provisioned_native_accepts_only_the_authenticated_owner(tmp_path, world, role):
    with enrolled_apps(tmp_path/'apps') as (owner, local, remote):
        apps = (local, remote)
        clients = [a.peer.managed_control_client(
            _test_ca_certificate=owner.authority.seed_server.material.ca_certificate) for a in apps]
        members = {a.member.node_id: member_pin(c, a.peer.managed_control_snapshot())
                   for a, c in zip(apps, clients)}
        base = real_prefill_node(world)
        names = {'entry': local.member.node_id, 'tail': remote.member.node_id}
        graph = replace(base.graph, deployment_id=DEPLOYMENT_ID,
            stages=tuple(replace(s, placements=tuple(replace(p, node_id=names[p.node_id])
                for p in s.placements)) for s in base.graph.stages))
        app = local if role == 'owner' else remote
        runtime = prepare_runtime(app, owner, tmp_path/'runtime', authorize_load=lambda intent: False)
        try:
            w = runtime.worker
            # Use the actual native constructor, not a trust object built from
            # the command being tested. Missing app provisioning reaches exactly
            # the production token_path_disabled guard on the old source.
            node = PhysicalNodeService(run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
                node_id=app.member.node_id, artifact_root=w.artifact_root,
                socket_root=w.socket_root, sidecar_binary=w.sidecar_binary,
                sidecar_local_only=True, command_timeout=10,
                token_policy_trust_file=w.token_policy_trust_file)
            node.graph = graph
            node.state = 'RUNNING'
            node.router = SimpleNamespace(relay=SimpleNamespace(token_path_gate=None))
            node.signer = app.member.signer
            node.endpoint_id = members[node.node_id]['endpoint_id']
            node._token_peers = lambda: {n: {k: m[k] for k in ('node_id', 'endpoint_id', 'generation')}
                                        for n, m in members.items()}
            policy = sign_route_policy(signer=owner.authority.coordinator.signer,
                run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, model_digest=graph.manifest_digest,
                requester_id='managed-owner', members=members, max_new_tokens=6,
                stop_token_ids=(), expires_at_unix_ms=int(time.time()*1000)+90000)
            from copy import deepcopy
            from mycelium_qualification.signing import generate_ed25519_signer
            foreign = deepcopy(policy)
            foreign['signature'] = generate_ed25519_signer(
                endpoint_id=owner.authority.coordinator.signer.endpoint_id).sign(foreign['statement'])
            if w.token_policy_trust_file is not None:
                with pytest.raises(RuntimeError, match='token_path_policy_signature'):
                    node.dispatch(dict(protocol='mycelium.physical_node_control.v1',
                        run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, command_id='foreign',
                        command='install_token_path', payload={'policy': foreign}))
                assert getattr(node, '_token_participant_policy', None) is None
            reply = node.dispatch(dict(protocol='mycelium.physical_node_control.v1',
                run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, command_id='install',
                command='install_token_path', payload={'policy': policy}))
            assert reply['observation']['event'] == 'token_path_installed'
            pin = json.loads(w.token_policy_trust_file.read_bytes())
            assert pin['owner_endpoint_id'] == owner.authority.coordinator.signer.endpoint_id
            assert pin['owner_key'] == owner.authority.coordinator.signer.public_key_record()
            assert pin['member'] == members[node.node_id]
            assert w.token_policy_trust_file.stat().st_mode & 0o777 == 0o600
            assert callable(runtime.pump._token_progress.factory)
        finally:
            runtime.close()


def test_owner_client_refuses_foreign_swarm_before_provisioning(tmp_path):
    from mycelium_node.durable_membership import load_membership_state, save_membership_state
    with enrolled_apps(tmp_path/'apps') as (owner, local, remote):
        state = load_membership_state(local.peer.state_root)
        save_membership_state(local.peer.state_root, {**state, 'swarm_id': 'foreign-swarm'})
        with pytest.raises((ValueError, RuntimeError), match='peer_member_binding_mismatch|local_owner_pin_mismatch'):
            local.peer.managed_control_client()
        assert not tuple(local.peer.state_root.rglob('token-host.private.json'))
