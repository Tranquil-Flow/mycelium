# SPDX-License-Identifier: AGPL-3.0-only
"""Real CPU child/worker/TLS/pump with a held delivery, fake compute/load only."""
import json
import threading
import time

import pytest

from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.worker import WorkerLimits
from tests.live.test_token_path_admission import world, payload
from test_managed_control import _world, _enqueue, RUN_ID, DEPLOYMENT_ID
from test_worker import worker
import test_token_progress_pump as fixture


@pytest.mark.parametrize('budget', [32, 256])
def test_token_path_coalesces_while_authenticated_delivery_is_held(tmp_path, world, monkeypatch, budget):
    # A barrier, not load or a sleep determining the outcome. Token 2 cannot
    # start until the first real TLS receipt has arrived; its ACK is held.
    child = fixture.CHILD.replace('def execute(c):', '''
original_decode = n.router.decode_one_distributed
original_drive = n.drive_granted_entry_token_path
def drive(**kwargs):
    return original_drive(**dict(kwargs, batch_tokens=1))
n.drive_granted_entry_token_path = drive
def decode(request_id):
    if not w.rounds:
        until=time.monotonic()+8
        while not Path(sys.argv[2]+'.delivery').exists() and time.monotonic()<until:
            time.sleep(.005)
        assert Path(sys.argv[2]+'.delivery').exists()
    result=original_decode(request_id)
    if w.rounds == w.request['max_new_tokens']-1:
        Path(sys.argv[2]+'.generated').write_text(str(w.rounds+1))
    return result
n.router.decode_one_distributed=decode
def execute(c):''')
    monkeypatch.setattr(fixture, 'CHILD', child)
    world.request.update(max_new_tokens=budget, expected_new_tokens=budget)
    world.grant['statement'].update(max_new_tokens=budget, stop_token_ids=[])
    with _world(tmp_path/'swarm') as a:
        root=tmp_path/'native'; root.mkdir()
        cursor, argv, release=fixture.prepare_world(world, a, root)
        sealed=[]
        stream=a['mailbox'].open_token_progress(command_id='grant-start', cursor=cursor(), seal=sealed.append)
        w=worker(root,node_id=a['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=10_000_000,
                               max_wall_seconds=40,max_commands=1000),authorize_load=lambda _:True)
        monkeypatch.setattr(w,'_native_argv',lambda:argv)
        pump=ConcurrentTargetControlPump(tmp_path/'pump',client=a['target_client'],worker=w,
            current_member=lambda:a['member'],token_progress_cursor=lambda _:cursor())
        held=threading.Event(); resume=threading.Event(); stop=threading.Event()
        calls=[]; native_acks=[]; errors=[]; events=[]
        original=a['target_client'].token_progress
        send=w.send_before
        def deliver(**kwargs):
            result=original(**kwargs)
            calls.append(kwargs)
            if len(calls)==1:
                release.with_name(release.name+'.delivery').write_text('arrived')
                held.set()
                assert resume.wait(10)
            return result
        def ack(**kwargs):
            if kwargs['command']=='ack_token_progress': native_acks.append(kwargs)
            return send(**kwargs)
        monkeypatch.setattr(a['target_client'],'token_progress',deliver)
        monkeypatch.setattr(w,'send_before',ack)
        def run():
            try:
                while not stop.is_set():
                    event=pump.step(); events.append(event)
                    if event.get('state')=='completed' and event.get('command_id')=='grant-start':return
                    stop.wait(.003)
            except BaseException as exc:errors.append(repr(exc))
        thread=threading.Thread(target=run)
        generated=release.with_name(release.name+'.generated')
        try:
            w.start();w._load_admitted=True
            _enqueue(a,'grant-start',operation='infer_start',payload=payload(world))
            thread.start()
            assert held.wait(8), errors
            end=time.monotonic()+5
            while not generated.exists() and time.monotonic()<end: stop.wait(.005)
            assert generated.exists(), 'producer overflowed before budget while delivery was held'
            assert int(generated.read_text())==budget
            assert len(calls)==1 and not native_acks
            resume.set(); release.write_text('release')
            tokens=[]; terminal=False; end=time.monotonic()+10
            while not terminal and time.monotonic()<end:
                try:r=stream.read(timeout=.1)
                except TimeoutError:continue
                tokens.extend(r.token_ids);terminal=r.status=='COMPLETED'
            assert not errors, errors
            assert tokens==list(range(1,budget+1)) and terminal
            thread.join(8)
            assert not thread.is_alive() and not errors
            assert len(calls)==2, 'delivery count must follow ACK rounds, not token count'
            assert len(native_acks)==len(calls)==len(sealed)
            ranges=[json.loads(r)['statement'] for r in sealed]
            assert [(r['start_index'],r['end_index']) for r in ranges]==[(0,1),(1,budget)]
            assert a['mailbox'].command_receipt('grant-start')['state']=='completed'
        finally:
            resume.set();release.write_text('release');stop.set()
            if thread.ident is not None:thread.join(12)
            pump.close()
        assert not w.process_group_present()
