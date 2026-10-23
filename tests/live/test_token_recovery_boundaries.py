# SPDX-License-Identifier: AGPL-3.0-only
"""Characterize unresolved recovery boundaries; never bypass a grant fence.

These passing refusal tests are NOT A6/A7 continuation qualification. They pin
why original grants cannot be recycled to claim that qualification.
"""
from copy import deepcopy
from dataclasses import replace

import pytest

from mycelium_qualification.evidence import canonical_json_bytes,sha256_bytes
from mycelium_router.serialization import path_manifest_to_dict
from tests.live.test_token_path_admission import world,policy
from tests.live.test_token_path_hops import setup,header,api


def fresh_attempt(w,s):
    manifest=replace(s.manifest,path_id='path-next',path_attempt=1)
    control={**w.control,'request_attempt':w.control['request_attempt']+1,
        'path_id':manifest.path_id,'path_attempt':manifest.path_attempt,
        'path_digest':sha256_bytes(canonical_json_bytes(path_manifest_to_dict(manifest)))}
    body=deepcopy(w.grant['statement']);body['scope']['control']=control
    grant=policy(w).admit({'statement':body,'signature':w.owner.sign(body)},
        request=w.request,bound_control=control)
    return manifest,grant


def test_a6_fresh_signed_attempt_cannot_reset_a_previously_used_process_gate(world):
    s=setup(world)
    manifest,grant=fresh_attempt(world,s)
    with pytest.raises(ValueError,match='token_path_grant_replay'):
        s.gate.install(grant=grant,request=world.request,manifest=manifest,graph=s.graph,current=lambda:True)
    assert not s.runtime.executed
    # A never-used participant can admit the new signed attempt. This does not
    # establish successor qualification or authorize a shared process reset.
    successor=api().GrantedHopGate(node_id='tail',clock=lambda:world.now,peers=lambda:s.peers)
    successor.install(grant=grant,request=world.request,manifest=manifest,graph=s.graph,current=lambda:True)
    assert successor.ingress(header(world,path_id='path-next',path_attempt=1),'entry') is None


@pytest.mark.parametrize('queued',[False,True])
def test_a7_old_grant_refuses_a_successor_path_before_any_relay_compute(world,queued):
    s=setup(world)
    assert s.router.receive_hop(header(world),b'prefix',source_node_id='entry').disposition=='COMPLETED'
    before=len(s.runtime.executed)
    method=s.router.enqueue_hop if queued else s.router.receive_hop
    result=method(header(world,'DECODE',1,path_id='path-successor',path_attempt=1),
        b'successor',source_node_id='entry')
    assert (result.disposition,result.reason)==('REJECTED','token_path_hop_mismatch')
    assert len(s.runtime.executed)==before and s.router.pending_batch_hops()==0
