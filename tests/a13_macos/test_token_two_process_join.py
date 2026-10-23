# SPDX-License-Identifier: AGPL-3.0-only
"""Two native CPU processes + real local-only Iroh; fake compute and load.

Private test authority only. This is not hardware or model-performance evidence.
"""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.control_session import ManagedControlOwner
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_live.route import _ConcurrentNodeSession
from mycelium_live.token_path_owner import OwnerTokenPath
from mycelium_node.token_admission import EntryAdmissionPolicy
from mycelium_router.token_path_authority import ReceiptCursor
from mycelium_qualification.evidence import canonical_json_bytes,sha256_bytes
from tests.live.test_token_path_admission import world,real_prefill_node
from test_managed_control import _world,RUN_ID,DEPLOYMENT_ID as DEFAULT_DEPLOYMENT_ID
from test_worker import worker

BINARY=Path(os.environ.get('TOKENPATH_DESK_SIDECAR',str(Path(__file__).resolve().parents[2]/'native/iroh_transport/target/debug/mycelium-iroh-sidecar')))


def close_all(actions):
    """One failing finalizer must never prevent owned-child teardown."""
    failures=[]
    for action in actions:
        try:action()
        except BaseException as exc:failures.append(exc)
    if failures:raise BaseExceptionGroup('joined fixture teardown failed',failures)


def _safe(path):
    if not path.is_file():pytest.skip('explicit non-quarantined desk sidecar required')
    # V1's no-quarantine-write subprocess fence scans the xattr executable's
    # embedded setter symbols. Use the existing read-only libc inspector, not
    # a subprocess or an exemption from the safety fence.
    from scripts.tokenpath_ab_bench import quarantine_clear
    for target in {path.absolute(), path.resolve()}:
        for candidate in (target, *target.parents):
            quarantine_clear(candidate)


def _argv(fixture):
    root=Path(__file__).resolve().parents[2]
    helper=root/'tests/live/token_path_desk_child.py'
    _safe(Path(sys.executable));_safe(helper)
    boot="import sys,runpy;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name='__main__')"
    return (sys.executable,'-c',boot,str(root),str(helper),str(fixture))


@pytest.mark.parametrize('bootstrap,governed',[('host',False),('signed',False),('signed',True),('signed','chat')])
@pytest.mark.parametrize('enabled',[False,True])
def test_owner_native_two_process_route(tmp_path,world,monkeypatch,enabled,bootstrap,governed,failure=None,startup=False,bench=None,app_pair=False):
    _safe(BINARY)
    window = (180 if app_pair else 50) if bench is None else bench.window
    DEPLOYMENT_ID=DEFAULT_DEPLOYMENT_ID
    qualification=None
    if governed in ('chat','http','browser'):
        from tests.request_gateway.test_core import _synthetic_qualification
        qualification=_synthetic_qualification()
        DEPLOYMENT_ID=qualification.deployment_id
        monkeypatch.setattr('test_managed_control.DEPLOYMENT_ID',DEPLOYMENT_ID)
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH', 'on' if enabled else 'off')
    sockets=Path(tempfile.mkdtemp(prefix='tp-',dir=os.environ['TMPDIR']))
    try:
        from test_packaged_token_trust import app_control_world
        with (app_control_world if app_pair else _world)(tmp_path/'swarm') as a:
            base=real_prefill_node(world)
            entry_id=a['member']['node_id'];tail_id=a.get('tail_node_id',a['authority'].coordinator.seed_node_id)
            names={'entry':entry_id,'tail':tail_id}
            graph=replace(base.graph,deployment_id=DEPLOYMENT_ID,
                stages=tuple(replace(s,placements=tuple(replace(p,node_id=names[p.node_id],
                    stage_signature=sha256_bytes(p.stage_signature.encode()) if startup else p.stage_signature,
                    load_proof_digest=sha256_bytes(p.load_proof_digest.encode()) if startup else p.load_proof_digest)
                    for p in s.placements)) for s in base.graph.stages))
            if qualification is not None:
                graph=replace(graph,model_id=qualification.model_id,resolved_commit=qualification.resolved_commit,
                    deployment_epoch=qualification.deployment_epoch,
                    topology_version=qualification.topology_version,manifest_digest=qualification.manifest_digest)
            signers={entry_id:a['target_signer'],tail_id:a.get('tail_signer',a['owner_signer'])}
            if bench is not None:
                graph=bench.prepare_graph(tmp_path,entry_id,tail_id)
                DEPLOYMENT_ID=graph.deployment_id
                world.pins['model_digest']=graph.manifest_digest
            members={n:dict(node_id=n,endpoint_id=s._public_key_bytes.hex(),control_endpoint_id=s.endpoint_id,
                verification_key_digest=s.verification_key_digest,
                generation=a['member']['generation'] if n==entry_id else 1) for n,s in signers.items()}
            world.pins.update(run_id=RUN_ID,owner_endpoint_id=a['owner_signer'].endpoint_id,
                owner_key_digest=a['owner_signer'].verification_key_digest,entry_node_id=entry_id,
                entry_endpoint_id=members[entry_id]['endpoint_id'],entry_key_digest=a['target_signer'].verification_key_digest,
                participants=[{k:members[n][k] for k in ('node_id','endpoint_id','generation')} for n in (entry_id,tail_id)])
            world.manifest['deployment_id']=DEPLOYMENT_ID
            for hop in world.manifest['ordered_hops']:
                # Router leases cross hosts in the Unix-aligned protocol clock;
                # grant/control wait deadlines below remain process-monotonic.
                hop['reservation_expires_at']=time.time()+window
            world.control.update(deployment_id=DEPLOYMENT_ID,path_digest=sha256_bytes(canonical_json_bytes(world.manifest)),
                absolute_deadline_ms=int(time.monotonic()*1000)+int((window-10)*1000))
            fixtures={}
            for i,n in enumerate((entry_id,tail_id)):
                root=tmp_path/('native-'+str(i));root.mkdir(mode=0o700)
                key=root/'node.key'
                key.write_bytes(signers[n]._private_key.private_bytes(serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,serialization.NoEncryption()));key.chmod(0o600)
                host_pin=root/'token-host.private.json'
                local_member={k:members[n][k] for k in ('node_id','endpoint_id','generation','control_endpoint_id')}
                local_member['verification_key']=signers[n].public_key_record()
                if app_pair:
                    from test_packaged_token_trust import prepare_runtime
                    managed=prepare_runtime(a['apps'][n],a['app'],root,
                        deployment_id=DEPLOYMENT_ID,seconds=window+10,authorize_load=lambda _:True)
                    a['runtimes'][n]=managed
                    host_pin=managed.worker.token_policy_trust_file
                else:
                    host_pin.write_text(json.dumps(dict(protocol='mycelium.token_path_host_pin.v1',run_id=RUN_ID,
                    deployment_id=DEPLOYMENT_ID,node_id=n,owner_endpoint_id=a['owner_signer'].endpoint_id,
                    owner_key=a['owner_signer'].public_key_record(),member=local_member,
                    process_deadline_monotonic_s=time.monotonic()+window+10,lease_expires_at_unix_ms=int(time.time()*1000)+int((window+10)*1000),
                    clock_uncertainty_ms=100)))
                    host_pin.chmod(0o600)
                fixture=root/'fixture.private.json'
                fixture.write_text(json.dumps(dict(run_id=RUN_ID,node_id=n,tail_id=tail_id,
                    graph=asdict(graph),root=str(root),socket_root=str(sockets/str(i)),binary=str(BINARY),
                    key_file=str(key),members=members,owner_key=a['owner_signer'].public_key_record(),
                    owner_endpoint_id=a['owner_signer'].endpoint_id,enabled=enabled,bootstrap=bootstrap,failure=failure,startup=startup,
                    member_keys={node:s.public_key_record() for node,s in signers.items()},
                    real_compute=None if bench is None else bench.native_inputs[n],window=window,
                    trust_file=str(host_pin) if host_pin is not None else None)))
                fixture.chmod(0o600);fixtures[n]=fixture
            root=fixtures[entry_id].parent
            w=a['runtimes'][entry_id].worker if app_pair else worker(root,node_id=entry_id,run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
                limits=WorkerLimits(max_rss_bytes=2_000_000_000 if bench is None else 8_000_000_000,max_disk_bytes=10_000_000,
                                   max_wall_seconds=window+10,max_commands=100 if bench is None else 2000),authorize_load=lambda _:True,
                token_policy_trust_file=root/'token-host.private.json' if enabled and bootstrap=='signed' else None)
            automatic_cursor=getattr(w,'token_progress_cursor',None)
            if enabled and bootstrap=='signed':
                assert callable(automatic_cursor), 'worker must derive the cursor from installed signed policy'
            monkeypatch.setattr(w,'_native_argv',lambda:_argv(fixtures[entry_id]))
            tail=native=None
            policy=EntryAdmissionPolicy(scope_pins=world.pins,owner_key=a['owner_signer'].public_key_record(),
                max_new_tokens=6,stop_token_ids=(),process_deadline_monotonic_s=time.monotonic()+60,
                lease_deadline_monotonic_s=time.monotonic()+60,clock_uncertainty_ms=100)
            from mycelium_node.token_provisioning import sign_route_policy
            policy_members={node:{k:m[k] for k in ('node_id','endpoint_id','generation','control_endpoint_id')} |
                {'verification_key':signers[node].public_key_record()} for node,m in members.items()}
            route_policy=sign_route_policy(signer=a['owner_signer'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
                model_digest=graph.manifest_digest,requester_id='requester',members=policy_members,
                max_new_tokens=6,stop_token_ids=(),expires_at_unix_ms=int(time.time()*1000)+50000)
            def cursor(c):
                grant=policy.admit(c['payload']['token_grant'],request=c['payload']['request'],bound_control=world.control)
                return ReceiptCursor(grant=grant,entry_key=a['target_signer'].public_key_record())
            pump=ConcurrentTargetControlPump(tmp_path/'pump',client=a['target_client'],worker=w,
                current_member=lambda:a['member'],token_progress_cursor=(automatic_cursor if bootstrap=='signed' else cursor) if enabled else None)
            owner=ManagedControlOwner(tmp_path/'owner',client=a['owner_client'],current_member=lambda _:a['member'])
            entry=owner.session(node_id=entry_id,run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,timeout_seconds=20 if bench is None else 120)
            stop=threading.Event();errors=[];events=[];inference_commands=[];finalizers=[]
            def drive():
                backoff=.1
                try:
                    while not stop.is_set():
                        step=pump.step();events.append(step)
                        if bench is None:stop.wait(.003)
                        elif isinstance(step,dict) and step.get('state')=='outbound_retryable':
                            backoff=min(1.,max(.1,backoff*2));stop.wait(backoff)
                        else:
                            backoff=.1;pump.wait_for_wake(.05)  # ManagedNativeRuntime._serve cadence/wake
                except BaseException as exc:errors.append(str(exc))
            thread=threading.Thread(target=drive)
            def info(session,cid):
                return session.send(command_id=cid,command='desk_info',payload={})['result']
            try:
                if bench is not None:bench.cleanup_verified=False
                tail=_ConcurrentNodeSession(argv=_argv(fixtures[tail_id]),node_id=tail_id,run_id=RUN_ID,
                    deployment_id=DEPLOYMENT_ID,timeout_seconds=15 if bench is None else 120)
                w.start();w._load_admitted=True
                if enabled and bootstrap=='signed' and not startup:w._token_policy.configured(json.loads(json.dumps(asdict(graph))))  # fake configure/load
                native=w._session
                infos={entry_id:info(native,'entry-info'),tail_id:info(tail,'tail-info')}
                assert len({v['identity']['process_id'] for v in infos.values()})==2
                thread.start()
                for n,s in ((entry_id,entry),(tail_id,tail)):
                    if startup:continue  # The real owner startup sends hello/configure/start.
                    peer=tail_id if n==entry_id else entry_id
                    response=s.send(command_id='start-'+n,command='start',payload=dict(local_generation=members[n]['generation'],
                        peer=dict(node_id=peer,endpoint_id=members[peer]['endpoint_id'],generation=members[peer]['generation'],
                                  endpoint_addr=infos[peer]['endpoint_addr'])))
                    assert response['ok'] is True, response.get('error')
                if governed:
                    from token_governed_desk import run_governed
                    tokens,sealed=run_governed(tmp_path=tmp_path,monkeypatch=monkeypatch,world=world,a=a,
                        graph=graph,members=members,signers=signers,sessions={entry_id:entry,tail_id:tail},
                        infos=infos,enabled=enabled,inference_commands=inference_commands,
                        chat=governed if governed in ('chat','http','browser') else False,finalizers=finalizers,failure=failure,startup=startup,bench=bench)
                else:
                    if enabled and bootstrap=='signed':
                        for n,s in ((entry_id,entry),(tail_id,tail)):
                            response=s.send(command_id='install-'+n,command='install_token_path',payload={'policy':route_policy})
                            assert response['ok'] is True,response.get('error')
                            assert response['result']['observation']['event']=='token_path_installed'
                    sealed=[]
                    path=OwnerTokenPath(signer=a['owner_signer'],scope_pins=world.pins,
                        control_endpoint_id=a['member']['endpoint_id'],
                        entry_key=a['target_signer'].public_key_record(),mailbox=a['mailbox'],seal=sealed.append,
                        current=lambda:True,capable=lambda _:True,max_new_tokens=6,stop_token_ids=(),
                        process_deadline_monotonic_s=time.monotonic()+50,lease_deadline_monotonic_s=time.monotonic()+50,
                        clock_uncertainty_ms=100)
                    attempt=path.prepare(command_id='one-start',request=world.request,control=world.control,
                        manifest=world.manifest,participating_node_ids={entry_id,tail_id},stop_token_ids=())
                    binding=attempt.bind_payload() if attempt else dict(request_id='request',control=world.control)
                    for n,s in ((entry_id,entry),(tail_id,tail)):
                        assert s.send(command_id='bind-'+n,command='bind_request_control',payload=binding)['ok'] is True
                    tokens=[]
                    def send(p):
                        inference_commands.append('infer_start')
                        try:
                            response=entry.send(command_id='one-start',command='infer_start',payload=p)
                        except Exception:
                            assert not errors, errors
                            raise
                        assert response['ok'] is True, response.get('error')
                        return response
                    if attempt:
                        response=attempt.run(send=send,consume=lambda r:tokens.extend(r.token_ids),
                            interrupt=lambda:entry.interrupt_command('one-start',code='token_progress_interrupted'))
                    else:
                        response=send(dict(request=world.request,control=world.control,path_manifest=world.manifest))
                        tokens.extend(response['result']['observation']['details']['output']['token_ids'])
                        while len(tokens)<6:
                            inference_commands.append('infer_decode')
                            response=entry.send(command_id='decode-'+str(len(tokens)),command='infer_decode',
                                payload=dict(request_id='request',count=1,control=world.control))
                            tokens=list(response['result']['observation']['details']['output']['token_ids'])
                    assert response['ok'] is True
                if bench is None:
                    assert tokens==([100] if failure else list(range(100,105 if governed=='browser' else 106)))
                    assert inference_commands == ['infer_start'] + ([] if enabled else ['infer_decode']*(1 if failure else 5))
                final={entry_id:info(native,'final-entry'),tail_id:info(tail,'final-tail')}
                if bench is None:
                    assert all(len(v['executions'])==((2 if failure else 6)+(2 if startup else 0)) for v in final.values())
                else:bench.finish(final=final,events=events,database=a['database'])
                if governed:assert all(v['active']==0 for v in final.values())
                assert bool(sealed)==enabled
                assert not errors
            except BaseException:
                if bench is not None:
                    bench.capture_failure(sessions={entry_id:native,tail_id:tail},events=events,database=a['database'])
                raise
            finally:
                try:
                    close_all([*reversed(finalizers),stop.set,
                        lambda:thread.join(3) if thread.ident is not None else None,
                        owner.close,pump.close,w.close,lambda:tail.close() if tail is not None else None])
                finally:
                    if bench is not None:
                        bench.cleanup_verified=(not w.process_group_present() and not thread.is_alive()
                            and (tail is None or tail.returncode is not None))
            assert not thread.is_alive() and not w.process_group_present()
    finally:
        shutil.rmtree(sockets)


@pytest.mark.parametrize('enabled',[False,True])
def test_stage_loss_preserves_chat_terminal_and_signed_cleanup(tmp_path,world,monkeypatch,enabled):
    test_owner_native_two_process_route(tmp_path,world,monkeypatch,enabled,'signed','chat',failure='stage-loss')


@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.parametrize('surface',['http','browser'])
def test_http_and_page_preserve_served_by_over_native_receipts(tmp_path,world,monkeypatch,enabled,surface):
    test_owner_native_two_process_route(tmp_path,world,monkeypatch,enabled,'signed',surface)


@pytest.mark.parametrize('enabled',[False,True])
def test_owner_startup_qualifies_native_route_and_installs_both_participants(tmp_path,world,monkeypatch,enabled):
    test_owner_native_two_process_route(tmp_path,world,monkeypatch,enabled,'signed','chat',startup=True)
