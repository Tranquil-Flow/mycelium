# SPDX-License-Identifier: AGPL-3.0-only
"""Real signatures and authority; deterministic delivery/clock seams, no compute."""
from copy import deepcopy
import json

import pytest

from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptCursor, ReceiptJournal
from tests.live.test_token_path_authority import setup, issue, admit


def large_stream(setup, budget=256):
    grant=admit(setup, issue(setup,max_new_tokens=budget,stop_token_ids=[]),
                max_new_tokens=budget,stop_token_ids=())
    journal=ReceiptJournal(grant=grant,signer=setup[1],clock=lambda:11.)
    cursor=ReceiptCursor(grant=grant,entry_key=setup[1].public_key_record(),clock=lambda:11.)
    return journal,cursor


def test_token_path_receipt_chain_binds_exact_predecessor(setup):
    journal,cursor=large_stream(setup)
    first=journal.append(TokenBatch(0,0,(1,),'DECODING'))
    assert first['statement'].get('previous_receipt_digest')==journal.grant.digest
    assert cursor.accept(first)==(1,)
    journal.acknowledge(0,first['signature']['signed_statement_digest'])
    last=journal.append(TokenBatch(1,1,tuple(range(2,257)),'COMPLETED'))
    assert last['statement']['previous_receipt_digest']==first['signature']['signed_statement_digest']
    wrong=deepcopy(last)
    wrong['statement']['previous_receipt_digest']='sha256:'+'0'*64
    wrong['signature']=setup[1].sign(wrong['statement'])
    with pytest.raises(ValueError,match='token_path_receipt_chain'):cursor.accept(wrong)
    assert cursor.accept(last)==tuple(range(2,257))


def test_token_path_slow_chat_sink_retains_full_admitted_budget(setup):
    from mycelium_a13_macos.token_receipt_mailbox import MailboxReceiptStream
    journal,cursor=large_stream(setup)
    sealed=[]
    # Default product capacity, not a deliberately reduced fault-injection cap.
    from inspect import signature
    from mycelium_a13_macos.token_receipt_mailbox import TokenProgressMailboxMixin
    capacity=signature(TokenProgressMailboxMixin.open_token_progress).parameters['capacity'].default
    stream=MailboxReceiptStream(cursor,sealed.append,capacity)
    for i in range(256):
        receipt=journal.append(TokenBatch(i,i,(i,), 'COMPLETED' if i==255 else 'DECODING'))
        stream.accept(receipt)
        journal.acknowledge(i,receipt['signature']['signed_statement_digest'])
    assert [stream.read(timeout=0).token_ids[0] for _ in range(256)]==list(range(256))
    assert len(sealed)==256


@pytest.mark.parametrize('mutation',['gap','overlap','end','boolean','budget','token','grant'])
def test_token_path_range_refusals_preserve_verified_prefix(setup,mutation):
    journal,cursor=large_stream(setup)
    first=journal.append(TokenBatch(0,0,(1,),'DECODING'));cursor.accept(first)
    last=journal.append(TokenBatch(1,1,tuple(range(2,257)),'COMPLETED'))
    bad=deepcopy(last);body=bad['statement']
    if mutation=='gap':body['start_index']=2
    if mutation=='overlap':body['start_index']=0
    if mutation=='end':body['end_index']=255
    if mutation=='boolean':body['start_index']=True
    if mutation=='budget':body['token_ids'].append(257);body['end_index']=257
    if mutation=='token':body['token_ids'][0]=True
    if mutation=='grant':body['grant_digest']='sha256:'+'0'*64
    bad['signature']=setup[1].sign(body)
    with pytest.raises(ValueError):cursor.accept(bad)
    assert cursor.accept(last)==tuple(range(2,257))
    assert cursor.terminal=='COMPLETED'


def test_token_path_maximum_budget_fits_signed_frame_and_owner_sealer(setup,tmp_path):
    from mycelium_node.token_progress import progress_frame,MAX_PROGRESS_BYTES
    journal,cursor=large_stream(setup,65536)
    receipt=journal.append(TokenBatch(0,0,(2**31-1,)*65536,'COMPLETED'))
    frame=progress_frame(command_id='start',node_id='entry',receipt=receipt)
    assert len(json.dumps(frame,separators=(',',':')).encode())<=MAX_PROGRESS_BYTES
    assert cursor.accept(receipt)==(2**31-1,)*65536
    from mycelium_a13_macos.token_owner import ManagedRouteTokenOwner
    from mycelium_qualification.evidence import canonical_json_bytes
    owner=ManagedRouteTokenOwner(signer=setup[0],current_member=lambda _:None,
        mailbox=None,evidence_root=tmp_path/'sealed',process_deadline_monotonic_s=1800.)
    raw=canonical_json_bytes(receipt)
    owner.seal(raw)
    assert [p.read_bytes() for p in owner.root.glob('*.receipt')]==[raw]


@pytest.mark.parametrize('cause',['expiry','cancel'])
def test_token_path_held_ack_drain_keeps_original_fence_and_verified_prefix(setup,cause):
    from mycelium_router.token_receipt_delivery import CoalescedReceiptDelivery
    journal,cursor=large_stream(setup)
    now=[11.];current=[True];published=[]
    journal._clock=lambda:now[0]
    journal._current=lambda:current[0]
    # A caller's request-control fence is not the journal's membership fence.
    delivery=CoalescedReceiptDelivery(journal,published.append,lambda:None)
    delivery.submit(TokenBatch(0,0,(1,),'DECODING'))
    assert cursor.accept(published[0])==(1,)
    delivery.submit(TokenBatch(1,1,(2,3),'COMPLETED'))
    waits=[]
    def revoke(timeout):
        waits.append(timeout)
        assert len(waits)==1, 'held ACK drain did not recheck journal authority'
        if cause=='expiry':now[0]=journal.grant.deadline_monotonic_s
        else:current[0]=False
    journal.wait_for_delivery_slot=revoke
    with pytest.raises(ValueError,match='token_path_grant_'+('expired' if cause=='expiry' else 'stale')):
        delivery.finish()
    assert waits==[.05] and len(published)==1
    assert delivery.index==1 and delivery.tokens==[2,3]
    assert cursor.terminal is None and journal.terminal is None


@pytest.mark.parametrize('start',[0,2])
def test_token_path_coalescing_cannot_hide_source_overlap_or_gap(setup,start):
    from mycelium_router.token_receipt_delivery import CoalescedReceiptDelivery
    journal,cursor=large_stream(setup)
    published=[]
    delivery=CoalescedReceiptDelivery(journal,published.append,journal._fence)
    delivery.submit(TokenBatch(0,0,(1,),'DECODING'))
    with pytest.raises(ValueError,match='token_path_receipt_order'):
        delivery.submit(TokenBatch(1,start,(2,),'DECODING'))
    assert delivery.tokens==[] and len(published)==1
    first=published[0]
    assert cursor.accept(first)==(1,)
    journal.acknowledge(0,first['signature']['signed_statement_digest'])
    delivery.submit(TokenBatch(1,1,(2,3),'COMPLETED'))
    delivery.finish()
    assert cursor.accept(published[1])==(2,3) and cursor.terminal=='COMPLETED'
