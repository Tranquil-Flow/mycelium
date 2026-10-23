# SPDX-License-Identifier: AGPL-3.0-only
"""Real ManagedWorker child -> concurrent pump -> pinned TLS -> native ACK.

One native CPU process, real native entry loop/signing, fake prefill/runtime and
load admission. This is not a PhysicalLiveRoute/chat or two-process hop proof.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import pytest
from cryptography.hazmat.primitives import serialization

from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_node.token_admission import EntryAdmissionPolicy
from mycelium_router.token_path_authority import ReceiptCursor
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from tests.live.test_token_path_admission import world, payload
from test_managed_control import _world, _enqueue, RUN_ID, DEPLOYMENT_ID
from test_worker import worker


CHILD = '''import json, sys, threading, time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mycelium_qualification.signing import Ed25519EvidenceSigner
from tests.live.test_token_path_admission import DeskWorld, node
from mycelium_node.token_admission import EntryAdmissionPolicy
from physical_inference_node import _ResponseEmitter, _response, NodeCommandError
from pathlib import Path
with open(sys.argv[1]) as f: d=json.load(f)
key=Ed25519PrivateKey.from_private_bytes(bytes.fromhex(d.pop('entry_secret')))
entry=Ed25519EvidenceSigner(endpoint_id=d['pins']['entry_endpoint_id'], _private_key=key,
    _public_key_bytes=key.public_key().public_bytes_raw())
owner_key=d.pop('owner_key')
failure=d.pop('failure',None)
w=DeskWorld(entry=entry, owner=None, **d, now=time.monotonic(), prefill=0, rounds=0,
           receipts=[], trace=[], cancelled=False)
n=node(w,enabled=False)
n.node_id=w.pins['entry_node_id']; n.run_id=w.pins['run_id']; n.deployment_id=w.control['deployment_id']
n._identity=lambda: {'run_id':n.run_id,'node_id':n.node_id}
n._token_path_admission=EntryAdmissionPolicy(scope_pins=w.pins, owner_key=owner_key,
    max_new_tokens=w.request['max_new_tokens'],stop_token_ids=tuple(w.grant['statement']['stop_token_ids']),process_deadline_monotonic_s=time.monotonic()+1800,
    lease_deadline_monotonic_s=time.monotonic()+25,clock_uncertainty_ms=100)
n._token_response_emitter=_ResponseEmitter()
def fail_decode(_):
    raise NodeCommandError('route_decode_stalled')
if failure=='after-prefix': n.router.decode_one_distributed=fail_decode
def execute(c):
    cid=c['command_id']
    try:
        if c['command']=='infer_start' and failure=='before-prefix':
            raise NodeCommandError('inference_start_failed')
        if c['command']=='hello': result={'process_id':__import__('os').getpid()}
        elif c['command']=='stop': result={'state':'STOPPED'}
        else: result=n.dispatch(c)
        if c['command']=='infer_start':
            deadline=time.monotonic()+10
            while not Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(.005)
        n._token_response_emitter.emit(_response(n,command_id=cid,ok=True,result=result))
    except NodeCommandError as e:
        n._token_response_emitter.emit(_response(n,command_id=cid,ok=False,error_code=e.code))
for raw in sys.stdin:
    c=json.loads(raw)
    if c['command']=='stop': execute(c); break
    threading.Thread(target=execute,args=(c,)).start()
'''


def prepare_world(w, a, root, *, failure=None):
    m = a['member']
    w.owner, w.entry = a['owner_signer'], a['target_signer']
    w.pins.update(run_id=RUN_ID, owner_endpoint_id=w.owner.endpoint_id,
        owner_key_digest=w.owner.verification_key_digest, entry_node_id=m['node_id'],
        entry_endpoint_id=w.entry.endpoint_id, entry_key_digest=w.entry.verification_key_digest,
        participants=[dict(node_id=m['node_id'], endpoint_id=m['endpoint_id'], generation=m['generation'])])
    w.control['deployment_id'] = DEPLOYMENT_ID
    w.grant['statement']['scope'] = {**deepcopy(w.pins), 'request_id': 'request',
        'control': deepcopy(w.control), 'request_digest': sha256_bytes(canonical_json_bytes(w.request))}
    w.grant['statement']['expires_at_unix_ms'] = int(time.time()*1000)+25000
    w.grant['signature'] = w.owner.sign(w.grant['statement'])
    p = EntryAdmissionPolicy(scope_pins=w.pins, owner_key=w.owner.public_key_record(),
        max_new_tokens=w.request['max_new_tokens'], stop_token_ids=tuple(w.grant['statement']['stop_token_ids']), process_deadline_monotonic_s=time.monotonic()+1800,
        lease_deadline_monotonic_s=time.monotonic()+25, clock_uncertainty_ms=100)
    grant = p.admit(w.grant, request=w.request, bound_control=w.control)
    def cursor():
        return ReceiptCursor(grant=grant, entry_key=w.entry.public_key_record())
    fixture = root/'fixture.private.json'
    fixture.write_text(json.dumps(dict(pins=w.pins, control=w.control, grant=w.grant, failure=failure,
        request=w.request, manifest=w.manifest, owner_key=w.owner.public_key_record(),
        entry_secret=w.entry._private_key.private_bytes(serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw, serialization.NoEncryption()).hex())))
    fixture.chmod(0o600)
    child = root/'child.py'; child.write_text(CHILD)
    release = root/'release'
    bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name='__main__')"
    argv=(sys.executable,'-c',bootstrap,str(Path(__file__).resolve().parents[2]),str(child),str(fixture),str(release))
    return cursor, argv, release


def test_durable_owner_ack_must_match_the_pending_receipt_before_native_send(tmp_path):
    from types import SimpleNamespace
    from mycelium_a13_macos.token_progress_pump import PumpTokenProgress
    from mycelium_a13_macos.control_pump import ControlPumpError
    calls=[]
    def send(**kwargs):
        calls.append(kwargs)
        return {'ok':True}
    pump=SimpleNamespace(root=tmp_path,worker=SimpleNamespace(send_before=send,close_progress=lambda _:None))
    bridge=PumpTokenProgress(pump,None)
    bridge.state['rows']['start']={'pending':{
        'statement':{'grant_digest':'grant-a','sequence':0,'status':'COMPLETED'},
        'signature':{'signed_statement_digest':'receipt-a'}},
        'owner_ack':{'statement':{'command_id':'other','grant_digest':'grant-b',
            'receipt_sequence':0,'receipt_digest':'receipt-b'}},'terminal':False,'error':None}
    with pytest.raises(ControlPumpError,match='token_progress_state_invalid'):
        bridge.next({})
    assert not calls and not bridge.ready_to_ack('start')


def test_reader_failure_is_sticky_and_cannot_turn_into_empty_success(tmp_path):
    from types import SimpleNamespace
    from mycelium_a13_macos.token_progress_pump import PumpTokenProgress
    from mycelium_a13_macos.control_pump import ControlPumpError
    import queue
    pump=SimpleNamespace(root=tmp_path)
    bridge=PumpTokenProgress(pump, None)
    q=queue.Queue(); q.put('token_progress_read_failed')
    bridge.state['rows']['start']={'pending':None,'owner_ack':None,'terminal':False,'error':None}
    bridge._readers['start']=(q,None)
    for _ in range(2):
        with pytest.raises(ControlPumpError,match='token_progress_read_failed'):
            bridge.next({})
    assert not bridge.ready_to_ack('start')
    resumed=PumpTokenProgress(pump,None)
    with pytest.raises(ControlPumpError,match='token_progress_read_failed'):
        resumed.next({})


def test_bridge_default_off_never_opens_a_worker_stream(tmp_path):
    from types import SimpleNamespace
    from mycelium_a13_macos.token_progress_pump import PumpTokenProgress
    from mycelium_a13_macos.control_pump import ControlPumpError
    def forbidden(**kwargs):
        raise AssertionError('default-off must not reach the native reader')
    pump=SimpleNamespace(root=tmp_path,worker=SimpleNamespace(open_progress=forbidden))
    with pytest.raises(ControlPumpError,match='token_progress_disabled'):
        PumpTokenProgress(pump,None).open({'command_id':'start'})


@pytest.mark.parametrize('outcome', ['pending', 'success', 'failed', 'uncertain', 'corrupt'])
def test_command_end_is_only_a_failed_ack_not_receipt_success(tmp_path, outcome):
    from types import SimpleNamespace
    from mycelium_a13_macos.token_progress_pump import PumpTokenProgress
    from mycelium_a13_macos.control_pump import ControlPumpError
    claim={'phase':'executed','native_response':{'ok':outcome=='success'}}
    if outcome=='pending':claim.update(phase='running',native_response=None)
    if outcome=='uncertain':claim.update(phase='reconciliation_required',native_response=None)
    pump=SimpleNamespace(root=tmp_path,_claims_state={'claims':{'start':claim}})
    bridge=PumpTokenProgress(pump,None)
    row={'pending':None,'owner_ack':None,'terminal':False,
         'error':'token_progress_read_failed' if outcome=='corrupt' else 'token_progress_command_ended'}
    bridge.state['rows']['start']=row;bridge.save()
    for current in (bridge,PumpTokenProgress(pump,None)):
        assert current.ready_to_ack('start')==(outcome=='failed')
        if outcome in ('success','corrupt'):
            with pytest.raises(ControlPumpError,match='token_progress_'):
                current.next({})
        else:assert current.next({}) is None
        assert current.state['rows']['start']['terminal'] is False


@pytest.mark.parametrize('fault', ['none', 'lost-http-ack', 'lost-native-ack'])
def test_real_worker_pump_receipts_arrive_before_start_response_and_drain_before_ack(tmp_path, world, monkeypatch, fault):
    with _world(tmp_path/'swarm') as a:
        root=tmp_path/'native'; root.mkdir()
        cursor, argv, release = prepare_world(world, a, root)
        sealed=[]
        stream=a['mailbox'].open_token_progress(command_id='grant-start', cursor=cursor(), seal=sealed.append)
        w=worker(root,node_id=a['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=10_000_000,
                               max_wall_seconds=40,max_commands=100),authorize_load=lambda _: True)
        monkeypatch.setattr(w,'_native_argv',lambda:argv)
        pump=ConcurrentTargetControlPump(tmp_path/'pump',client=a['target_client'],worker=w,
            current_member=lambda:a['member'], token_progress_cursor=lambda command:cursor())
        events=[]
        injected=[]
        original_progress=a['target_client'].token_progress
        original_send=w.send_before
        def lose_http(**kwargs):
            result=original_progress(**kwargs)
            if fault=='lost-http-ack' and not injected:
                injected.append(True)
                from mycelium_a13_macos.managed_control import ManagedControlError
                raise ManagedControlError('managed_control_response_timeout')
            return result
        def lose_native(**kwargs):
            result=original_send(**kwargs)
            if fault=='lost-native-ack' and not injected:
                injected.append(True)
                raise TimeoutError('command_deadline_exceeded')
            return result
        monkeypatch.setattr(a['target_client'],'token_progress',lose_http)
        monkeypatch.setattr(w,'send_before',lose_native)
        try:
            w.start()
            w._load_admitted=True  # Fake load admission; native never loads a model.
            _enqueue(a,'grant-start',operation='infer_start',payload=payload(world))
            assert pump.step()['state']=='dispatched'
            tokens=[]; terminal=False; end=time.monotonic()+8
            while time.monotonic()<end and not terminal:
                events.append(pump.step())
                try:
                    r=stream.read(timeout=0)
                except TimeoutError:
                    time.sleep(.005); continue
                tokens.extend(r.token_ids); terminal=r.status=='COMPLETED'
                if len(tokens)==1 and fault!='lost-http-ack':
                    # Receipt traffic must yield to the existing independent
                    # control slot rather than starve cancellation/health polls.
                    _enqueue(a,'independent-control',sequence=2)
                    event=pump.step(); events.append(event)
                    assert event['state']=='dispatched' and event['command_id']=='independent-control'
            assert tokens == [1,2,3,4,5,6] and terminal
            assert a['mailbox'].command_receipt('grant-start')['state']=='claimed'
            assert not release.exists()
            release.write_text('release')
            end=time.monotonic()+8
            while time.monotonic()<end:
                event=pump.step(); events.append(event)
                if event.get('command_id')=='grant-start' and event['state']=='completed': break
                time.sleep(.005)
            else: pytest.fail('start terminal not ACKed')
            assert a['mailbox'].command_receipt('grant-start')['state']=='completed'
            forwarded=[e for e in events if e['state']=='token_progress_forwarded']
            assert len(forwarded)==len(sealed) and len(sealed)>1
            assert pump._token_progress.pending_count == 0
        finally:
            release.write_text('release')
            pump.close()
        assert not w.process_group_present()


@pytest.mark.parametrize('failure', ['before-prefix', 'after-prefix'])
def test_native_refusal_drains_verified_prefix_and_keeps_control_alive(tmp_path, world, monkeypatch, failure):
    with _world(tmp_path/'swarm') as a:
        root=tmp_path/'native'; root.mkdir()
        cursor, argv, release=prepare_world(world,a,root,failure=failure)
        sealed=[]
        stream=a['mailbox'].open_token_progress(command_id='grant-start',cursor=cursor(),seal=sealed.append)
        w=worker(root,node_id=a['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=10_000_000,
                               max_wall_seconds=40,max_commands=100),authorize_load=lambda _:True)
        monkeypatch.setattr(w,'_native_argv',lambda:argv)
        pump=ConcurrentTargetControlPump(tmp_path/'pump',client=a['target_client'],worker=w,
            current_member=lambda:a['member'],token_progress_cursor=lambda _:cursor())
        try:
            w.start();w._load_admitted=True  # fake compute/load only
            _enqueue(a,'grant-start',operation='infer_start',payload=payload(world))
            events=[];deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                event=pump.step();events.append(event)
                if event.get('command_id')=='grant-start' and event['state']=='failed':break
                time.sleep(.005)
            else:pytest.fail('native rejection was not acknowledged')
            assert event['native_response']['error']['code']==(
                'inference_start_failed' if failure=='before-prefix' else 'route_decode_stalled')
            assert a['mailbox'].command_receipt('grant-start')['state']=='failed'
            assert sum(e['state']=='dispatched' and e.get('command_id')=='grant-start' for e in events)==1
            assert len(sealed)==(1 if failure=='after-prefix' else 0)
            if sealed:
                receipt=stream.read(timeout=0)
                assert receipt.token_ids==(1,) and receipt.status=='DECODING'
                assert any(e['state']=='token_progress_forwarded' for e in events)
            _enqueue(a,'independent-control',sequence=2)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                event=pump.step()
                if event.get('command_id')=='independent-control' and event['state']=='completed':break
                time.sleep(.005)
            else:pytest.fail('native refusal blocked independent control')
            assert pump._token_progress.pending_count==0
        finally:pump.close()
        assert not w.process_group_present()
