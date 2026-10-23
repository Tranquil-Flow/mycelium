# SPDX-License-Identifier: AGPL-3.0-only
"""Maximum-range durable pump state and pinned TLS replay; no model execution."""
from copy import deepcopy
import json

import pytest

from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.durable_requests import prepare, replay
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptJournal
from tests.live.test_token_path_admission import world, payload
from test_managed_control import _world, _enqueue, RUN_ID, DEPLOYMENT_ID
from test_worker import worker
from test_token_progress_pump import prepare_world


@pytest.mark.parametrize('storage',['pending','outbound'])
def test_token_path_maximum_range_survives_pump_reload_and_tls_replay(tmp_path,world,storage):
    budget=65536
    world.request.update(max_new_tokens=budget,expected_new_tokens=budget)
    world.grant['statement'].update(max_new_tokens=budget,stop_token_ids=[])
    with _world(tmp_path/'swarm') as a:
        root=tmp_path/'native';root.mkdir()
        cursor,_,_=prepare_world(world,a,root)
        sealed=[]
        stream=a['mailbox'].open_token_progress(command_id='grant-start',cursor=cursor(),seal=sealed.append)
        journal=ReceiptJournal(grant=cursor().grant,signer=world.entry)
        tokens=(2**31-1,)*budget
        receipt=journal.append(TokenBatch(0,0,tokens,'COMPLETED'))
        _enqueue(a,'grant-start',operation='infer_start',payload=payload(world))
        claim=a['target_client'].poll(target=a['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,sequence=1,nonce='poll-nonce',
            request_id='poll-request',active_claims={})
        packet=prepare(a['target_client'],'token_progress',target=a['member'],
            run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,command_id='grant-start',
            claim_token=claim['claim_token'],receipt=receipt,sequence=2,
            nonce='receipt-nonce',request_id='receipt-request')
        w=worker(root,node_id=a['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=10_000_000,
                               max_wall_seconds=40,max_commands=100),authorize_load=lambda _:True)
        args=dict(client=a['target_client'],worker=w,current_member=lambda:a['member'])
        pump=ConcurrentTargetControlPump(tmp_path/'pump',**args)
        try:
            if storage=='pending':
                # Three actual concurrent claim slots, each at the receipt ceiling.
                pump._token_progress.state['rows']={f'start-{i}':dict(pending=deepcopy(receipt),
                    owner_ack=None,terminal=False,error=None) for i in range(3)}
                expected=deepcopy(pump._token_progress.state)
                pump._token_progress.save()
            else:
                pump._state['outbound']=packet
                expected=deepcopy(pump._state)
                pump._save()
        finally:pump.close()
        resumed=ConcurrentTargetControlPump(tmp_path/'pump',**args)
        try:
            state=resumed._token_progress.state if storage=='pending' else resumed._state
            assert state==expected
            # Real pinned TLS, signature checks, sealer and mailbox cursor. No
            # fake receipt, no re-signing on replay and no native inference replay.
            ack=replay(a['target_client'],packet,target=a['member'])
            assert ack['statement']['receipt_digest']==receipt['signature']['signed_statement_digest']
            item=stream.read(timeout=0)
            assert item.token_ids==tokens and item.status=='COMPLETED'
            assert json.loads(item.envelope_bytes)['statement']['end_index']==budget
            assert replay(a['target_client'],packet,target=a['member'])==ack
            assert len(sealed)==1
        finally:resumed.close()
