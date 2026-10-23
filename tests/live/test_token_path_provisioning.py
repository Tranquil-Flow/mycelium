# SPDX-License-Identifier: AGPL-3.0-only
"""Signed route-policy bootstrap; real authority, no model or device execution."""
from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest

from mycelium_qualification.signing import generate_ed25519_signer
from tests.live.test_token_path_admission import world, payload
from tests.live.test_token_path_participants import entry, bind


def provisioned(w):
    api = importlib.import_module('mycelium_node.token_provisioning')
    n = entry(w)
    tail = generate_ed25519_signer(endpoint_id='tail-endpoint')
    members = {p['node_id']: {**p, 'control_endpoint_id': p['endpoint_id'],
        'verification_key': (w.entry if p['node_id']=='entry' else tail).public_key_record()}
        for p in w.pins['participants']}
    peers = {p['node_id']: deepcopy(p) for p in w.pins['participants']}
    trust = api.RoutePolicyTrust(run_id='desk', deployment_id='deployment', node_id='entry',
        owner_endpoint_id=w.owner.endpoint_id, owner_key=w.owner.public_key_record(),
        member=lambda: deepcopy(members['entry']), current=lambda: True,
        process_deadline_monotonic_s=30., lease_deadline_monotonic_s=20.,
        clock_uncertainty_ms=100, clock=lambda: w.now, unix_ms=lambda:100000)
    envelope = api.sign_route_policy(signer=w.owner, run_id='desk', deployment_id='deployment',
        model_digest=n.graph.manifest_digest, requester_id='requester', members=members,
        max_new_tokens=6, stop_token_ids=(99,), expires_at_unix_ms=110000)
    return SimpleNamespace(api=api,n=n,members=members,peers=peers,trust=trust,envelope=envelope)


def test_signed_bootstrap_installs_participant_policy_and_pinned_cursor(world):
    s=provisioned(world)
    admitted=s.trust.admit(s.envelope, graph=s.n.graph, peers=lambda:s.peers)
    s.n.provision_token_path(admitted.participant_policy())
    s.n._bind_request_control(bind(world))
    cursor=admitted.cursor(payload(world))
    assert cursor.grant.deadline_monotonic_s == pytest.approx(14.9)
    assert s.n._infer_start(payload(world))['observation']['details']['status']=='COMPLETED'
    assert tuple(t for r in world.receipts for t in cursor.accept(r))==(1,2,3,4,5,6)
    assert world.prefill==1 and world.rounds==5


@pytest.mark.parametrize('change', ['signature','owner','run','deployment','model','member',
    'peer','expired','budget_bool','stops_bool','unknown','extra_member_field','duplicate_endpoint',
    'key_digest','self_key','missing_self'])
def test_signed_bootstrap_refuses_before_policy_install(world, change):
    s=provisioned(world)
    body=s.envelope['statement']
    if change=='signature':
        other=generate_ed25519_signer(endpoint_id=world.owner.endpoint_id)
        s.envelope['signature']=other.sign(body)
    else:
        if change=='owner': body['owner_endpoint_id']='other'
        elif change=='run': body['run_id']='other'
        elif change=='deployment': body['deployment_id']='other'
        elif change=='model': body['model_digest']='sha256:'+'a'*64
        elif change=='member': body['members']['entry']['generation']+=1
        elif change=='peer': s.peers['tail']['generation']+=1
        elif change=='expired': body['expires_at_unix_ms']=100000
        elif change=='budget_bool': body['max_new_tokens']=True
        elif change=='stops_bool': body['stop_token_ids']=[True]
        elif change=='unknown': body['surprise']=True
        elif change=='extra_member_field': body['members']['entry']['surprise']=True
        elif change=='duplicate_endpoint': body['members']['tail']['endpoint_id']=body['members']['entry']['endpoint_id']
        elif change=='key_digest': body['members']['tail']['verification_key']['verification_key_digest']='sha256:'+'a'*64
        elif change=='self_key': body['members']['entry']['verification_key']=generate_ed25519_signer(endpoint_id='entry-endpoint').public_key_record()
        elif change=='missing_self': del body['members']['entry']
        s.envelope['signature']=world.owner.sign(body)
    with pytest.raises((ValueError,RuntimeError)):
        s.trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers)
    assert getattr(s.n,'_token_participant_policy',None) is None
    assert world.prefill==world.rounds==0


def test_bootstrap_is_single_use_and_cannot_reset_replay_fences(world):
    s=provisioned(world)
    s.trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers)
    with pytest.raises((RuntimeError,ValueError),match='token_path_policy_already_installed'):
        s.trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers)


@pytest.mark.parametrize('change',['member','peer','expiry'])
def test_installed_policy_observes_live_authority_before_bind(world,change):
    s=provisioned(world)
    admitted=s.trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers)
    s.n.provision_token_path(admitted.participant_policy())
    if change=='member':s.members['entry']['generation']+=1
    elif change=='peer':s.peers['tail']['generation']+=1
    else:world.now=20.
    with pytest.raises((ValueError,RuntimeError)):
        s.n._bind_request_control(bind(world))
    assert world.prefill==world.rounds==0


def test_cursor_scope_is_derived_from_policy_graph_and_manifest_not_grant(world):
    s=provisioned(world)
    admitted=s.trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers)
    p=payload(world)
    p['token_grant']['statement']['scope']['participants'][1]['generation']+=1
    p['token_grant']['signature']=world.owner.sign(p['token_grant']['statement'])
    with pytest.raises((ValueError,RuntimeError)):
        admitted.cursor(p)


@pytest.mark.parametrize('trusted',[False,True])
def test_native_install_command_requires_local_trust_and_signed_owner_policy(world,trusted):
    s=provisioned(world)
    if trusted:s.n._token_route_trust=s.trust
    s.n._token_peers=lambda:s.peers
    command=dict(protocol='mycelium.physical_node_control.v1',run_id='desk',
        deployment_id='deployment',command_id='install',command='install_token_path',
        payload={'policy':s.envelope})
    if not trusted:
        with pytest.raises(RuntimeError,match='token_path_disabled'):
            s.n.dispatch(command)
    else:
        result=s.n.dispatch(command)
        assert result['observation']['event']=='token_path_installed'
        s.n._bind_request_control(bind(world))
        assert s.n._infer_start(payload(world))['observation']['details']['status']=='COMPLETED'


def test_private_host_pin_file_loads_without_accepting_wire_trust(world,tmp_path):
    s=provisioned(world)
    pin=dict(protocol='mycelium.token_path_host_pin.v1',run_id='desk',deployment_id='deployment',
        node_id='entry',owner_endpoint_id=world.owner.endpoint_id,owner_key=world.owner.public_key_record(),
        member=s.members['entry'],process_deadline_monotonic_s=30.,lease_expires_at_unix_ms=110000,
        clock_uncertainty_ms=100)
    import json
    path=tmp_path/'host.private.json';path.write_text(json.dumps(pin));path.chmod(0o600)
    load=lambda:s.api.load_host_trust(path,run_id='desk',deployment_id='deployment',node_id='entry',
        current=lambda:True,clock=lambda:world.now,unix_ms=lambda:100000)
    trust=load()
    assert trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers).deadline_monotonic_s==pytest.approx(19.9)
    path.chmod(0o644)
    with pytest.raises((ValueError,RuntimeError)):load()
    path.chmod(0o600)
    original=tmp_path/'original';path.rename(original);path.symlink_to(original)
    with pytest.raises((ValueError,RuntimeError)):load()


def test_policy_deadline_clamps_to_original_process_and_lease(world):
    s=provisioned(world)
    body=s.envelope['statement'];body['expires_at_unix_ms']=200000
    s.envelope['signature']=world.owner.sign(body)
    admitted=s.trust.admit(s.envelope,graph=s.n.graph,peers=lambda:s.peers)
    assert admitted.deadline_monotonic_s==20.
    world.now=19.
    p=payload(world);p['token_grant']['statement']['expires_at_unix_ms']=200000
    p['token_grant']['signature']=world.owner.sign(p['token_grant']['statement'])
    assert admitted.cursor(p).grant.deadline_monotonic_s==20.
