# SPDX-License-Identifier: AGPL-3.0-only
"""PhysicalLiveRoute control-flow with scripted nodes, real receipt signatures.

Uses the governed completion fixture (scripted cleanup and qualification). This
is not the required two-process/native/chat proof and cannot qualify a device.
"""
import threading
import time

import pytest

from mycelium_live.token_path_owner import OwnerTokenPath
from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptJournal
from mycelium_qualification.signing import generate_ed25519_signer
from tests.live.test_governed_completion_snapshot_refresh import governed
from tests.live.test_token_path_owner import Mailbox
from types import SimpleNamespace


def test_governed_fixture_teardown_retires_route():
    """V1 has no diagnostics discovery; retain its route-lifetime invariant."""

    with pytest.MonkeyPatch.context() as patch:
        lifecycle = getattr(governed, '__wrapped__')(patch)
        state = next(lifecycle)
        try:
            assert state.route._open is True and state.route._closed is False
        finally:
            lifecycle.close()
        # Inspect the retained V1 route itself, without importing the V2-only
        # diagnostics feature or relying on garbage collection.
        assert state.route._open is False and state.route._closed is True


@pytest.mark.parametrize('enabled',[False,True])
def test_governed_route_uses_one_start_and_streams_without_duplicate_tokens(governed,monkeypatch,enabled):
    g=governed
    signer=generate_ed25519_signer(endpoint_id='owner')
    entry=generate_ed25519_signer(endpoint_id='entry')
    box=Mailbox()
    pins=dict(run_id='local-fixture',requester_id='requester',
        owner_endpoint_id=signer.endpoint_id,owner_key_digest=signer.verification_key_digest,
        entry_node_id='node-a',entry_endpoint_id=entry.endpoint_id,entry_key_digest=entry.verification_key_digest,
        participants=[dict(node_id=n,endpoint_id='entry' if n=='node-a' else 'tail',generation=1)
                      for n in ('node-a','node-b')],model_digest='sha256:'+'c'*64)
    path=OwnerTokenPath(enabled=enabled,signer=signer,scope_pins=pins,entry_key=entry.public_key_record(),
        mailbox=box,seal=lambda _:None,current=lambda:True,capable=lambda _:True,
        max_new_tokens=3,stop_token_ids=(),process_deadline_monotonic_s=time.monotonic()+60,
        lease_deadline_monotonic_s=time.monotonic()+60,clock_uncertainty_ms=100)
    g.route._token_path_owner=path
    attempts=[]; original_prepare=path.prepare
    def prepare(**kwargs):
        result=original_prepare(**kwargs)
        if result is not None: attempts.append(result)
        return result
    monkeypatch.setattr(path,'prepare',prepare)
    first=threading.Event()
    original_infer=g.route.infer
    def infer(*args,**kwargs):
        emit=kwargs['sink'].emit
        def wrapped_emit(i,t):
            emit(i,t);first.set()
        kwargs['sink'].emit=wrapped_emit
        return original_infer(*args,**kwargs)
    monkeypatch.setattr(g.route,'infer',infer)
    binds=[]
    for node in g.nodes.values():
        original=node.send
        def send(*,command_id,command,payload,node=node,original=original):
            if command=='bind_request_control':
                assert ('token_path' in payload)==enabled
                binds.append(payload)
            if command=='infer_start' and enabled:
                g.log.append((node.node_id,command))
                assert len(binds)==2
                assert payload==attempts[0].payload()
                journal=ReceiptJournal(grant=attempts[0].grant,signer=entry)
                box.stream.accept(journal.append(TokenBatch(0,0,(101,),'DECODING')))
                assert first.wait(1), 'chat sink must receive tokens before start returns'
                assert set(g.route._health_probe_state['progress_ms'])==set(g.nodes)
                box.stream.accept(journal.append(TokenBatch(1,1,(102,103),'COMPLETED')))
                return {'event':'inference_started','details':{'status':'COMPLETED','token_count':3,
                    'token_grant_digest':attempts[0].grant.digest}}
            return original(command_id=command_id,command=command,payload=payload)
        monkeypatch.setattr(node,'send',send)
    result,emitted=g.run()
    assert result.token_ids==(101,102,103) and emitted==[(0,101),(1,102),(2,103)]
    assert sum(command=='infer_start' for _,command in g.log)==1
    assert sum(command=='infer_decode' for _,command in g.log)==(0 if enabled else 2)
    assert bool(g.sealed)  # ordinary owner cleanup is still owed
    assert box.opens==int(enabled)
    if enabled:
        assert set(g.route._health_probe_state['progress_ms'])==set(g.nodes)  # verified compute progress, not health/cleanup authority

@pytest.mark.parametrize('challenge',[False,True])
def test_required_token_path_preserves_only_authenticated_startup_challenge(governed, challenge):
    g=governed
    g.route._token_path_owner=SimpleNamespace(required=True,prepare=lambda **_:pytest.fail('challenge must not mint a grant'))
    g.route._plan['request']={'prompt_token_ids':[7,8]}
    g.route._plan['expected_token_ids']=[101,102,103]
    kwargs=dict(max_new_tokens=3,request_id='startup-challenge',sink=SimpleNamespace(emit=lambda *_:None))
    if challenge:
        result=g.route.infer_startup_challenge(request_id=kwargs['request_id'],sink=kwargs['sink'])
        assert result.token_ids==(101,102,103)
    else:
        with pytest.raises(RuntimeError,match='token_path_activation_unsupported'):
            g.route.infer((7,8),**kwargs)
        assert not any(command=='infer_start' for _,command in g.log)
