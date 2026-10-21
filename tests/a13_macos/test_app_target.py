# SPDX-License-Identifier: AGPL-3.0-only
"""Two local applications through real TLS; NOT physical two-Mac evidence."""
import json

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite
from test_app import login, request
from test_app_authority import CONSENT


def test_target_application_pair_resume_capability_and_owner_revoke(tmp_path):
    owner=DemoApp(tmp_path/'owner',check_platform=False).start()
    target=None
    try:
        oh=login(owner)
        assert request(owner,'/api/create','POST',{'consent':CONSENT},oh)[0]==200
        ca=owner.authority.seed_server.material.ca_certificate
        def client(*,bundle=None,membership=None,now,timeout):
            if bundle is not None:
                return pinned_seed_client_from_invite(bundle,now=now,ca_certificate=ca,timeout=timeout)
            return pinned_seed_client(seed_url=membership['seed_url'],swarm_id=membership['swarm_id'],
                seed_key_digest=membership['seed_key_digest'],seed_key_records=list(membership['seed_key_records']),
                ca_certificate=ca,timeout=timeout)
        def factory(root): return RemoteMacMember(root,_test_seed_client_factory=client)
        root=tmp_path/'target'
        target=DemoApp(root,check_platform=False,_test_peer_factory=factory).start()
        th=login(target)
        assert request(target,'/api/join/request','POST',{},th)[0]==400
        assert not (root/'target').exists()
        result=request(target,'/api/join/request','POST',{'consent':CONSENT},th)
        assert result[0]==200
        pairing=json.loads(result[2])
        target.close()
        target=DemoApp(root,check_platform=False,_test_peer_factory=factory).start()
        th=login(target)
        assert json.loads(request(target,'/api/join/request','POST',{'consent':CONSENT},th)[2])==pairing
        assert request(target,'/api/create','POST',{'consent':CONSENT},th)[0]==400
        assert request(owner,'/api/join/request','POST',{'consent':CONSENT},oh)[0]==400
        body={'request':pairing,'ttl_seconds':120,'quota':{'storage_bytes':2_000_000_000,
            'network_bytes_per_day':2_000_000_000,'background_allowed':False}}
        preview=request(owner,'/api/pair/preview','POST',{'request':pairing},oh)
        assert preview[0]==200
        assert json.loads(preview[2])['node_id']==pairing['node_id']
        response=request(owner,'/api/pair/approve','POST',body,oh)
        assert response[0]==200
        handoff=json.loads(response[2])
        assert request(target,'/api/pair/accept','POST',{'handoff':handoff},th)[0]==200
        identity=target.authority.member.signer.verification_key_digest
        generation=target.authority.member.generation
        assert request(target,'/api/capability/report','POST',{},th)[0]==200
        status=json.loads(request(target,'/api/status',headers=th)[2])
        assert status['installation_role']=='target'
        assert status['authority']['membership_state']=='current'
        assert status['authority']['capability_state']=='self_report_accepted'
        assert not status['serving']['route_ready']
        assert not list(root.rglob('*.sqlite3')) and not list(root.rglob('seed.key'))
        assert request(target,'/api/stop','POST',{},th)[0]==200
        assert request(target,'/api/start','POST',{},th)[0]==200
        assert target.authority.member.signer.verification_key_digest==identity
        assert target.authority.member.generation>generation
        assert target.authority.status()['capability_state']=='not_reported'
        roster=json.loads(request(owner,'/api/status',headers=oh)[2])['authority']['members']
        record=next(m for m in roster if m['node_id']==pairing['node_id'])
        assert request(owner,'/api/revoke','POST',{'node_id':record['node_id'],'generation':record['generation']},oh)[0]==200
        assert request(target,'/api/capability/report','POST',{},th)[0]==409
        assert request(target,'/api/status')[0]==401
    finally:
        if target is not None: target.close()
        owner.close()
