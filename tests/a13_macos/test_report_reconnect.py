# SPDX-License-Identifier: AGPL-3.0-only
"""Owner restart/report composition; real signatures/inbox/compiler input check.

OS measurement, transport and native/model effects are injected. No devices.
"""
from types import SimpleNamespace
import time
import pytest
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.capability import MemberReports, signed_capability_link_report
from mycelium_a13_macos.chain_compile import _capability_rows
from mycelium_a13_macos.demo_chain import member_capability_records
from mycelium_a13_macos.demo_target import DemoTargetAgent
from mycelium_a13_macos.run_exchange import RunExchange
from mycelium_qualification.signing import generate_ed25519_signer
from test_capability_link import DEVICE, LINK


@pytest.fixture
def report_world(tmp_path):
    signer = generate_ed25519_signer(endpoint_id='member-key')
    member = SimpleNamespace(node_id='member', generation=1, incarnation='member-one')
    lookup = lambda n: dict(verification_key_digest=signer.verification_key_digest,
                            generation=member.generation, lifecycle_state='RUNNING')
    inbox = [MemberReports(lookup=lookup)]
    sends = []
    def deliver(report):
        result = inbox[0].submit(report)
        sends.append(report)
        return result
    app = DemoApp(tmp_path/'target', check_platform=False)
    app.installation_role='target'; app.running=True
    peer = SimpleNamespace(owner_origin=lambda:'https://owner.invalid',
        membership_binding=lambda: {'node_id':'member','swarm_id':'swarm','seed_key_digest':'pinned'})
    app.authority = SimpleNamespace(member=member, peer=peer,
        report_capability=lambda:{'state':'self_report_accepted'},
        status=lambda:{'membership_state':'current'}, pending_stage=lambda:None)
    app.status = lambda:{'running':app.running}
    app._join_client = lambda origin: SimpleNamespace(member_report=deliver)
    app._measure_and_sign = lambda client,purpose: signed_capability_link_report(
        signer=signer,node_id=member.node_id,purpose=purpose,generation=member.generation,
        device=DEVICE,link=LINK,now=time.time())
    owner = SimpleNamespace(member_capability_reports=lambda:inbox[0].latest(), local_contributor=None,
        authority=SimpleNamespace(coordinator=SimpleNamespace(member=lookup)))
    def compile_rows():
        return _capability_rows(member_capability_records(owner,['member']),['member'],
            members={'member': {'generation':member.generation}})
    def restart(): inbox[0] = MemberReports(lookup=lookup)
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('action',['/api/start','/api/capability/report'])
def test_reconnect_or_explicit_report_refills_restarted_owner_inbox(report_world, action):
    w=report_world
    assert w.app._auto_report('join')['state']=='delivered'
    assert w.compile_rows()[0]['node_id']=='member'
    w.restart()
    with pytest.raises(ValueError, match='member_capability_report_required'): w.compile_rows()
    w.app._action(action,{})
    assert w.compile_rows()[0]['node_id']=='member'
    assert len(w.sends)==2


def agent(w):
    a={'phase':'collect','run_id':'run-1','deployment_id':'dep-1','slice_mode':True,
       'owner_incarnation':'owner-one'}
    consent=SimpleNamespace(allows=lambda *args:None)
    target=DemoTargetAgent(w.app,consent=consent,source=lambda:dict(a),sink=lambda report:None)
    target._make_descriptor=lambda a:{'fixture':'no-native-work'}
    target._cleanup_if_requested=lambda actor:False
    return target,a,consent


def test_each_run_generation_and_owner_incarnation_refreshes_before_descriptor(report_world):
    w=report_world
    target,a,consent=agent(w)
    target.tick()
    assert w.compile_rows()[0]['node_id']=='member'
    assert w.sends[-1]['purpose']=='pre_run'
    target.tick(); assert len(w.sends)==1
    for change in ('run','generation','owner'):
        w.restart()
        if change=='run': a['run_id']='run-2'
        elif change=='generation': w.member.generation+=1
        else: a['owner_incarnation']='owner-two'
        target.tick()
        assert w.compile_rows()[0]['node_id']=='member'
    assert len(w.sends)==4


def test_announcement_exposes_authenticated_owner_incarnation():
    mailbox=SimpleNamespace(coordinator=SimpleNamespace(incarnation='owner-after-restart'))
    first=RunExchange(mailbox)
    incarnation=first.announcement()['owner_incarnation']
    assert incarnation==first.announcement()['owner_incarnation']
    # Seed incarnation is persistent metadata; an app restart must still differ.
    assert incarnation!=RunExchange(mailbox).announcement()['owner_incarnation']


def test_report_failure_or_withdrawn_consent_never_marks_report_delivered(report_world):
    w=report_world
    target,a,consent=agent(w)
    consent.allows=lambda *args:'standing_consent_withdrawn'
    target.tick()
    assert not w.sends
    assert target._descriptor is None
    consent.allows=lambda *args:None
    client=w.app._join_client
    w.app._join_client=lambda origin: (_ for _ in ()).throw(ValueError('transport_failed'))
    target.tick()
    assert target._descriptor is None
    w.app._join_client=client
    target.tick()
    assert w.compile_rows()[0]['node_id']=='member'


@pytest.mark.parametrize('phase',['idle','collect'])
def test_report_refusal_cannot_block_cleanup(report_world,phase):
    w=report_world
    target,a,consent=agent(w)
    a['phase']=phase
    w.app._join_client=lambda origin: (_ for _ in ()).throw(ValueError('transport_failed'))
    cleaned=[]
    target._cleanup_if_requested=lambda actor: cleaned.append(True) or True
    target.tick()
    assert cleaned
    assert target._descriptor is None
