# SPDX-License-Identifier: AGPL-3.0-only
"""Owner bootstrap from admitted route/member state; scripted install transport."""
import importlib
from types import SimpleNamespace
import time
import pytest
from mycelium_qualification.evidence import canonical_json_bytes,sha256_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from tests.live.test_token_path_admission import world, real_prefill_node
from tests.live.test_token_path_owner import Mailbox


def configured(w,tmp_path,monkeypatch):
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on')
    api=importlib.import_module('mycelium_a13_macos.token_owner')
    n=real_prefill_node(w)
    signers={'entry':w.entry,'tail':generate_ed25519_signer(endpoint_id='tail-control')}
    members={node:dict(node_id=node,endpoint_id=s.endpoint_id,verification_key_digest=s.verification_key_digest,
        generation=i+1,lease_expires_at=time.time()+300) for i,(node,s) in enumerate(signers.items())}
    installs=[]
    class Session:
        def __init__(self,node):self.node=node
        def send(self,*,command_id,command,payload):
            assert command=='install_token_path'
            installs.append((self.node,payload['policy']))
            return dict(details=dict(policy_digest=sha256_bytes(canonical_json_bytes(payload['policy']['statement']))))
    route=SimpleNamespace(_graph=n.graph,_plan={'run_id':'desk','deployment_id':'deployment'},
        _endpoints={node:dict(endpoint_id=s._public_key_bytes.hex(),membership_generation=members[node]['generation'])
                    for node,s in signers.items()},
        _verification_keys={node:s.public_key_record() for node,s in signers.items()},
        _sessions={node:Session(node) for node in signers},_closed=False,
        _command_id=lambda node,operation:node+'-'+operation,
        _verify_observation=lambda node,response,**kw:response)
    class ManagedMailbox(Mailbox):
        def open_token_progress(self, *, target_endpoint_id, **kwargs):
            assert target_endpoint_id==w.entry.endpoint_id
            return super().open_token_progress(**kwargs)
    owner=api.ManagedRouteTokenOwner(signer=w.owner,current_member=lambda node:members[node],
        mailbox=ManagedMailbox(),evidence_root=tmp_path,process_deadline_monotonic_s=time.monotonic()+1800)
    owner.attach_route(route)
    w.control['absolute_deadline_ms']=int(time.monotonic()*1000)+120000
    return owner,route,members,installs


def test_owner_derives_and_installs_policy_before_minting_request(world,tmp_path,monkeypatch):
    owner,route,members,installs=configured(world,tmp_path,monkeypatch)
    attempt=owner.prepare(command_id='start',request=world.request,control=world.control,
        manifest=world.manifest,participating_node_ids={'entry','tail'},stop_token_ids=(99,))
    assert [n for n,_ in installs]==['entry','tail']
    assert installs[0][1]==installs[1][1]
    scope=attempt.payload()['token_grant']['statement']['scope']
    assert scope['entry_endpoint_id']==route._endpoints['entry']['endpoint_id']
    assert scope['entry_key_digest']==world.entry.verification_key_digest
    assert scope['owner_key_digest']==world.owner.verification_key_digest
    assert attempt.owner.required is True
    attempt.close()


@pytest.mark.parametrize('change',['member_generation','member_key','route_key','route_endpoint','expired'])
def test_owner_refuses_bad_independent_pins_before_install(world,tmp_path,monkeypatch,change):
    owner,route,members,installs=configured(world,tmp_path,monkeypatch)
    if change=='member_generation':members['tail']['generation']+=1
    elif change=='member_key':members['entry']['verification_key_digest']='sha256:'+'a'*64
    elif change=='route_key':route._verification_keys['entry']=generate_ed25519_signer(endpoint_id='entry').public_key_record()
    elif change=='route_endpoint':route._endpoints['entry']['endpoint_id']='foreign'
    else:members['tail']['lease_expires_at']=time.time()-1
    with pytest.raises((RuntimeError,ValueError)):
        owner.prepare(command_id='start',request=world.request,control=world.control,
            manifest=world.manifest,participating_node_ids={'entry','tail'},stop_token_ids=(99,))
    assert not installs


def test_owner_off_has_no_policy_or_grant_effect(world,tmp_path,monkeypatch):
    owner,route,members,installs=configured(world,tmp_path,monkeypatch)
    owner.enabled=False
    assert owner.prepare(command_id='start',request=world.request,control=world.control,
        manifest=world.manifest,participating_node_ids={'entry','tail'},stop_token_ids=(99,)) is None
    assert not installs


def test_startup_installs_every_participant_before_any_request_grant(world,tmp_path,monkeypatch):
    owner,route,members,installs=configured(world,tmp_path,monkeypatch)
    owner.install(stop_token_ids=(99,))
    assert [node for node,_ in installs]==['entry','tail']
    assert installs[0][1]==installs[1][1]
    assert owner.mailbox.opens==0 and not owner._owners
    owner.install(stop_token_ids=(99,))
    assert len(installs)==2  # no reset of native replay state
    attempt=owner.prepare(command_id='start',request=world.request,control=world.control,
        manifest=world.manifest,participating_node_ids={'entry','tail'},stop_token_ids=(99,))
    assert len(installs)==2 and owner.mailbox.opens==1
    attempt.close()


def test_partial_startup_install_cannot_retry_or_mint_a_grant(world,tmp_path,monkeypatch):
    owner,route,members,installs=configured(world,tmp_path,monkeypatch)
    def refuse(**kwargs):raise RuntimeError('native_install_refused')
    monkeypatch.setattr(route._sessions['tail'],'send',refuse)
    with pytest.raises(RuntimeError,match='native_install_refused'):
        owner.install(stop_token_ids=(99,))
    assert [node for node,_ in installs]==['entry']
    with pytest.raises(RuntimeError,match='token_path_grant_stale'):
        owner.install(stop_token_ids=(99,))
    assert len(installs)==1 and owner.mailbox.opens==0
