# SPDX-License-Identifier: AGPL-3.0-only
"""Signed address recovery is not a new trust grant or membership renewal."""
import copy
import pytest
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_a13_macos.endpoint_recovery import endpoint_card, verify_endpoint_card


def material():
    s=generate_ed25519_signer(endpoint_id='owner-endpoint')
    member={'swarm_id':'swarm-one','seed_node_id':'seed-one','seed_url':'https://old.trycloudflare.com','seed_key_digest':s.verification_key_digest,'seed_key_records':[s.public_key_record()]}
    return s,member


def test_bound_same_seed_address_change():
    s,m=material();card=endpoint_card(signer=s,swarm_id=m['swarm_id'],seed_node_id=m['seed_node_id'],previous_origin=m['seed_url'],public_origin='https://new.trycloudflare.com',now=100)
    assert verify_endpoint_card(card,membership=m,now=101)=='https://new.trycloudflare.com'
    assert m['seed_url']=='https://old.trycloudflare.com'

@pytest.mark.parametrize('change',[{'public_origin':'https://evil.example'},{'swarm_id':'other'},{'previous_origin':'https://other.example'},{'expires_at':999999}])
def test_tampered_card_rejected(change):
    s,m=material();card=endpoint_card(signer=s,swarm_id=m['swarm_id'],seed_node_id=m['seed_node_id'],previous_origin=m['seed_url'],public_origin='https://new.trycloudflare.com',now=100)
    card['statement'].update(change)
    with pytest.raises(ValueError):verify_endpoint_card(card,membership=m,now=101)


def test_other_signer_and_stale_rejected():
    s,m=material();other=generate_ed25519_signer(endpoint_id='other')
    for signer,clock in [(other,101),(s,1000)]:
        card=endpoint_card(signer=signer,swarm_id=m['swarm_id'],seed_node_id=m['seed_node_id'],previous_origin=m['seed_url'],public_origin='https://new.trycloudflare.com',now=100)
        with pytest.raises(ValueError):verify_endpoint_card(card,membership=m,now=clock)


def test_card_cannot_jump_from_unrelated_old_origin():
    s,m=material();card=endpoint_card(signer=s,swarm_id=m['swarm_id'],seed_node_id=m['seed_node_id'],previous_origin='https://not-your-old.example',public_origin='https://new.trycloudflare.com',now=100)
    with pytest.raises(ValueError):verify_endpoint_card(card,membership=m,now=101)


def test_retained_seed_and_peer_relocate_without_repairing(tmp_path):
    import socket
    from mycelium_a13_macos.authority import DemoAuthority
    from mycelium_a13_macos.peer import RemoteMacMember
    from mycelium_a13_macos.tls import pinned_seed_client
    from mycelium_invite import verify_invite_bundle
    from mycelium_node.durable_membership import load_membership_state
    from mycelium_a13_macos.endpoint_recovery import relocate_authority,reconnect_peer
    from test_peer import CONSENT,QUOTA
    a=DemoAuthority(tmp_path/'owner');a.start()
    def port():
        with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
    a.configure_public_endpoint(public_origin='https://old.example',port=port(),acknowledged=True)
    a.create_swarm(CONSENT)
    def factory(*,bundle=None,membership=None,now,timeout):
        if bundle:
            v=verify_invite_bundle(bundle,now=now)
            membership={'seed_url':v['payload']['seed_url'],'swarm_id':v['payload']['swarm_id'],
                        'seed_key_digest':v['seed_key_digest'],'seed_key_records':v['seed_key_records']}
        return pinned_seed_client(seed_url=membership['seed_url'],swarm_id=membership['swarm_id'],
            seed_key_digest=membership['seed_key_digest'],seed_key_records=membership['seed_key_records'],
            ca_certificate=a.seed_server.material.ca_certificate,transport_url=a.seed_server.base_url,timeout=timeout)
    p=RemoteMacMember(tmp_path/'peer',_test_seed_client_factory=factory);p.start()
    try:
        handoff=a.approve_pairing(p.pair_request(CONSENT),ttl_seconds=120,quota=QUOTA);p.accept_handoff(handoff)
        before=load_membership_state(p.state_root);key=p._signer.verification_key_digest
        card=relocate_authority(a,public_origin='https://new.example',port=port(),acknowledged=True)
        from mycelium_a13_macos.endpoint_recovery import refresh_endpoint_card
        with pytest.raises(ValueError):refresh_endpoint_card(a,acknowledged=False)
        fresh=refresh_endpoint_card(a,acknowledged=True)
        assert fresh['statement']['previous_origin']==card['statement']['previous_origin']
        assert fresh['statement']['public_origin']==card['statement']['public_origin']
        assert fresh['statement']['issued_at']>=card['statement']['issued_at']
        target_card=refresh_endpoint_card(a,acknowledged=True,previous_origin=before['seed_url'])
        assert target_card['statement']['previous_origin']==before['seed_url']
        assert reconnect_peer(p,card=target_card,acknowledged=True)['membership_state']=='current'
        after=load_membership_state(p.state_root)
        assert all(before[k]==after[k] for k in ['node_id','swarm_id','seed_node_id','seed_key_digest','endpoint_id'])
        assert p._signer.verification_key_digest==key
        assert after['membership_generation']==before['membership_generation']+1
        assert after['seed_url']=='https://new.example'
    finally:p.stop();a.stop()


@pytest.mark.parametrize('failure_code', ['peer_transport_unreachable', 'seed_http_remote_error'])
def test_offline_target_keeps_authenticated_recovery_api(tmp_path,monkeypatch,failure_code):
    from mycelium_a13_macos.app import DemoApp,private_write
    from mycelium_a13_macos.peer import RemoteMacMember,RemoteMacMemberError
    from test_app_authority import CONSENT
    from test_app import login,request
    import json
    root=tmp_path/'app';root.mkdir(mode=0o700)
    from mycelium_a13_macos.authority import _atomic_private_json
    _atomic_private_json(root/'installation-role.json',{'protocol':'mycelium.a13_installation_role.v1','role':'target'})
    private_write(root/'resource-policy.json',json.dumps(CONSENT).encode())
    monkeypatch.setattr(RemoteMacMember,'start',lambda self:(_ for _ in ()).throw(RemoteMacMemberError(failure_code)))
    app=DemoApp(root,check_platform=False).start()
    try:
        h=login(app)
        assert app.running is False
        assert app.status()['recovery_error']==failure_code
        assert request(app,'/api/network/reconnect','POST',{'card':{},'relay_acknowledged':True})[0]==401
        assert request(app,'/api/network/reconnect','POST',{'card':{},'relay_acknowledged':False},h)[0]==400
    finally:app.close()
