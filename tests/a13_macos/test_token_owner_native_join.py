# SPDX-License-Identifier: AGPL-3.0-only
"""Owner grant -> durable session -> real native CPU child -> TLS receipts.

Fake compute/load; one native process. This joins owner issuance and consumption
with the real worker/pump, but is not the two-process/chat acceptance proof.
"""
import threading
import time

from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.control_session import ManagedControlOwner
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_live.token_path_owner import OwnerTokenPath
from mycelium_node.token_admission import EntryAdmissionPolicy
from mycelium_router.token_path_authority import ReceiptCursor
from tests.live.test_token_path_admission import world
from test_managed_control import _world, RUN_ID, DEPLOYMENT_ID
from test_worker import worker
from test_token_progress_pump import prepare_world


def test_owner_issued_single_start_streams_real_native_receipts_before_completion(tmp_path,world,monkeypatch):
    with _world(tmp_path/'swarm') as a:
        root=tmp_path/'native';root.mkdir()
        world.control['absolute_deadline_ms']=int(time.monotonic()*1000)+25000
        _,argv,release=prepare_world(world,a,root)
        sealed=[]
        path=OwnerTokenPath(enabled=True,signer=a['owner_signer'],scope_pins=world.pins,
            entry_key=a['target_signer'].public_key_record(),mailbox=a['mailbox'],seal=sealed.append,
            current=lambda:True,capable=lambda _:True,max_new_tokens=6,stop_token_ids=(99,),
            process_deadline_monotonic_s=time.monotonic()+40,
            lease_deadline_monotonic_s=time.monotonic()+25,clock_uncertainty_ms=100)
        attempt=path.prepare(command_id='owner-grant-start',request=world.request,
            control=world.control,manifest=world.manifest,
            participating_node_ids={a['member']['node_id']},stop_token_ids=(99,))
        policy=EntryAdmissionPolicy(scope_pins=world.pins,owner_key=a['owner_signer'].public_key_record(),
            max_new_tokens=6,stop_token_ids=(99,),process_deadline_monotonic_s=time.monotonic()+40,
            lease_deadline_monotonic_s=time.monotonic()+25,clock_uncertainty_ms=100)
        def cursor(command):
            grant=policy.admit(command['payload']['token_grant'],request=command['payload']['request'],
                bound_control=world.control)
            return ReceiptCursor(grant=grant,entry_key=a['target_signer'].public_key_record())
        w=worker(root,node_id=a['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=10_000_000,
                               max_wall_seconds=40,max_commands=100),authorize_load=lambda _:True)
        monkeypatch.setattr(w,'_native_argv',lambda:argv)
        pump=ConcurrentTargetControlPump(tmp_path/'pump',client=a['target_client'],worker=w,
            current_member=lambda:a['member'],token_progress_cursor=cursor)
        owner=ManagedControlOwner(tmp_path/'owner',client=a['owner_client'],current_member=lambda _:a['member'])
        session=owner.session(node_id=a['member']['node_id'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,timeout_seconds=15)
        stop=threading.Event(); errors=[]; events=[]
        def drive():
            try:
                while not stop.is_set():
                    events.append(pump.step())
                    stop.wait(.003)
            except BaseException as exc:
                errors.append(exc)
        thread=threading.Thread(target=drive)
        tokens=[];sends=[]
        def send(payload):
            sends.append('infer_start')
            return session.send(command_id=attempt.command_id,command='infer_start',payload=payload)
        def consume(receipt):
            assert not release.exists()
            assert a['mailbox'].command_receipt(attempt.command_id)['state']=='claimed'
            tokens.extend(receipt.token_ids)
            if receipt.status=='COMPLETED':release.write_text('release')
        try:
            w.start();w._load_admitted=True
            thread.start()
            response=attempt.run(send=send,consume=consume,
                interrupt=lambda:session.interrupt_command(attempt.command_id,code='token_progress_interrupted'))
            assert response['ok'] is True
            assert response['result']['observation']['details']['status']=='COMPLETED'
            assert tokens==[1,2,3,4,5,6] and sends==['infer_start']
            assert sealed and not errors
            assert a['mailbox'].command_receipt(attempt.command_id)['state']=='completed'
            assert pump._token_progress.pending_count==0
        finally:
            release.write_text('release');stop.set()
            if thread.ident is not None:thread.join(3)
            owner.close();pump.close()
        assert not thread.is_alive() and not w.process_group_present()
