# SPDX-License-Identifier: AGPL-3.0-only
"""Actual native configure -> seed authorization -> builder, synthetic CPU only."""
import copy
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import traceback

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests/live')]
from phone_composition_fixture import fixture_build
from scripts import build_qwen_live_route as builder
from mycelium_iroh_sidecar import SidecarClient
from mycelium_live.phone_stage_session import PhoneStageSessionFactory
from physical_inference_qualification import PeerIdentity
from mycelium_seed import route_assignment as ra
from mycelium_seed.phone_preparation import issue_builder_authorization
from layer_assignment import assignment_id_for


def receive():
    assert select.select([sys.stdin],[],[],40)[0], 'native handshake deadline'
    return json.loads(sys.stdin.readline())


def rows(database):
    with ra._connect(database) as c:
        return {r[0]:[list(x) for x in c.execute('SELECT * FROM '+r[0]+' ORDER BY 1')]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'seed_%'")}


def main():
    transcript=[]; session=None; sidecar=None; error=None
    def terminated(*_): raise RuntimeError('owned fixture terminated')
    signal.signal(signal.SIGTERM,terminated)
    with tempfile.TemporaryDirectory(prefix='builder-native-') as temp, tempfile.TemporaryDirectory(prefix='bipc-',dir='/tmp') as ipc:
        tmp=Path(temp).resolve()
        try:
            native=receive(); transcript.append(dict(native_identity=native)); native['source']=os.environ['BUILDER_REVIEW_SOURCE']
            socket=Path(ipc)/'run/s.sock'; secret=b'c'*32
            sidecar=subprocess.Popen([os.environ['BUILDER_REVIEW_SIDECAR'],'--uds',str(socket),'--bootstrap-fd','0','--local-only'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            sidecar.stdin.write(secret);sidecar.stdin.close()
            assert select.select([sidecar.stdout],[],[],15)[0]
            ready=json.loads(sidecar.stdout.readline());assert ready['event']=='ready',ready
            native['controller']=ready['endpoint_id']
            def observe(setup):
                nonlocal session
                print(json.dumps(dict(setup,action='apply',controller=ready['endpoint_id'])),flush=True)
                loaded=receive()
                with SidecarClient(socket,secret,timeout=5) as c:
                    c.configure_peer(native['endpoint_id'],loaded['phone'],generation=1)
                factory=PhoneStageSessionFactory(socket_path=socket,bootstrap_secret=secret,controller_endpoint_id=ready['endpoint_id'])
                peer=PeerIdentity(node_id='node-1',host_id='node-1',boot_id=setup['member']['incarnation'],ssh_target=None,
                    staging_root='/tmp/mycelium/phone-stage',process_transport='external_iroh')
                session=factory(argv=(),node_id='node-1',run_id='synthetic-run',deployment_id=setup['deployment_id'],timeout_seconds=10,
                    peer=peer,endpoint_id=native['endpoint_id'],member_key_digest=native['key']['verification_key_digest'])
                def call(cmd,payload):
                    r=session.send(command_id='builder-'+cmd,command=cmd,payload=payload)
                    transcript.append(dict(command=cmd,response=r))
                    assert r['ok'],r
                    return r
                call('hello',{})
                configured=call('configure',setup['profile'])
                snapshot=call('snapshot',{})
                assert configured['result']['observation']['details']['assignment_id']==setup['record']['assignment_id']
                assert configured['result']['observation']['details']['runtime_build_digest']==loaded['runtime_build_digest']
                return configured,snapshot
            original=subprocess.run
            def no_network(cmd,**kw):
                if cmd[0]=='ssh': return subprocess.CompletedProcess(cmd,0,json.dumps(['fixture-remote','fixture-boot']),'')
                return original(cmd,**kw)
            subprocess.run=no_network
            fixture=fixture_build(tmp,native=native,observe=observe)
            assert fixture.document['configured']==next(x['response'] for x in transcript if x.get('command')=='configure')
            report=builder.build(fixture.args)
            assert report['route_ready'] is False
            transcript.append(dict(builder_report=report,configured_unmodified=True,physical=False))
            import shutil
            out=Path(os.environ['BUILDER_REVIEW_EVIDENCE'])
            shutil.copytree(fixture.args.output_root/'transfer-bundle',out/'emitted-bundle')
            for name in ('operator-plan.json','local-challenge-checkpoint.json'):
                shutil.copyfile(fixture.args.output_root/name,out/name)
            # Replace A through the real issuance/apply path: same pack/member/
            # generation/controller, new compiled identity B. Never rewrite A.
            assignment=copy.deepcopy(fixture.preparation['assignments'][1])
            assignment['artifact_cache_root']+='/replacement-B'
            assignment['assignment_id']=assignment_id_for(assignment)
            b=ra.issue_route_assignment(fixture.database,node_id='node-1',stage_pack=fixture.document['manifest'],
                peer_endpoint_id=fixture.nodes[2]['endpoint_id'],route_generation=1,controller_endpoint_ids=[native['controller']],
                seed_key_record=fixture.seed.signer.public_key_record(),preparation_assignment=assignment,
                artifact_sources=[dict(endpoint_id=fixture.nodes[0]['endpoint_id'],verification_key_digest=fixture.sessions['node-0'].signer.verification_key_digest)])
            member=fixture.seed.member('node-1');signer=fixture.sessions['node-1'].signer
            import time
            now=time.time()
            msg=dict(protocol=ra.ROUTE_ASSIGNMENT_REQUEST_PROTOCOL,message_id='apply-replacement',swarm_id=fixture.seed.swarm_id,
                sender_node_id='node-1',sender_endpoint_id=member['endpoint_id'],recipient_node_id='seed',incarnation=member['incarnation'],
                generation=member['generation'],issued_at=now,expires_at=now+60,applied_assignment_id=b['assignment_id'])
            ra.service_for(fixture.seed).answer(dict(protocol=ra.SIGNED_MESSAGE_PROTOCOL,message=msg,signature=signer.sign(msg),verification_key=signer.public_key_record()))
            before=rows(fixture.database)
            try: issue_builder_authorization(fixture.args.phone_seed_state_root,seed_key_digest=fixture.args.phone_seed_key_digest,
                approved_build_receipt_digest=fixture.approved,**fixture.request)
            except Exception as exc: assert 'phone_applied_assignment_id_mismatch' in str(exc)
            else: raise AssertionError('old A authorized under B')
            fixture.args.output_root=tmp/'refused-B'
            try: builder.build(fixture.args)
            except Exception as exc: assert 'phone_applied_assignment_id_mismatch' in str(exc)
            else: raise AssertionError('old A reprojected under B')
            assert not (fixture.args.output_root/'operator-plan.json').exists()
            assert rows(fixture.database)==before
            transcript.append(dict(replaced_B_rejected_by_issuer_and_final_projection=True,authority_rows_unchanged=True))
        except BaseException:
            error=traceback.format_exc();print(error,file=sys.stderr,flush=True)
        finally:
            if session is not None:
                try:
                    r=session.send(command_id='builder-stop',command='stop',payload={})
                    transcript.append(dict(command='stop',response=r));assert r['ok'],r
                except BaseException as stop_error:
                    error=error or traceback.format_exc()
                    transcript.append(dict(stop_error=repr(stop_error)))
                finally: session.close()
            if sidecar is not None:
                sidecar.terminate()
                try: sidecar.wait(timeout=5)
                except subprocess.TimeoutExpired: sidecar.kill();sidecar.wait(timeout=5)
                transcript.append(dict(sidecar_exit=sidecar.returncode,sidecar_stderr=sidecar.stderr.read().decode(errors='replace')))
            out=Path(os.environ['BUILDER_REVIEW_EVIDENCE'])
            (out/'native-builder.json').write_text(json.dumps(dict(error=error,transcript=transcript),indent=2)+'\n')
            print(json.dumps(dict(ok=error is None)),flush=True)
    return int(error is not None)

if __name__=='__main__': raise SystemExit(main())
