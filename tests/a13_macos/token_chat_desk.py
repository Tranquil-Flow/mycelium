# SPDX-License-Identifier: AGPL-3.0-only
"""CPU fixture qualification around the real M16/router/chat service stack."""
from dataclasses import replace
import time

from mycelium_m16_runtime import M16RuntimeCoordinator
from mycelium_router.contracts import DeviceState, RouterConfig
from mycelium_router.fakes import ManualClock, SequenceIdSource
from mycelium_live.router_port import LiveRouterPort
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from tests.request_gateway.test_core import _synthetic_qualification, MutableQualificationSource
from tests.live.test_a6_live_router_recovery import TokenCodec


def complete_chat(*, route, graph, tmp_path, cleanup, finalizers, failure=None, http=False, browser=False, qualification=None):
    qualification=qualification or _synthetic_qualification()
    assert qualification.deployment_id==graph.deployment_id and qualification.manifest_digest==graph.manifest_digest
    route.set_deployment_qualification(qualification,quantization="synthetic-desk")
    now=time.time()
    nodes={p.node_id for s in graph.stages for p in s.placements}
    states={n:DeviceState(n,1,now,'ALIVE',now+300,1.,1_000_000,0,{}, {}) for n in nodes}
    capacities={p.placement_id:dict(memory_capacity_bytes=1_000_000,kv_capacity_bytes=100_000,
        workspace_capacity_bytes=100_000,maximum_reservations=4) for s in graph.stages for p in s.placements}
    coordinator=M16RuntimeCoordinator(graph=graph,device_states=states,placement_capacities=capacities,
        workload_profiles={'interactive_chat_v1':'interactive'},clock=ManualClock(now),
        id_source=SequenceIdSource(),config=RouterConfig(reservation_lease_seconds=40))
    router=LiveRouterPort(route=route,execution_graph=graph,runtime_coordinator=coordinator)
    service=RequestGatewayService(qualification_source=MutableQualificationSource(qualification),
        backend=RouterSessionBackend(router=router,codec=TokenCodec(),clock=time.time),
        request_id_source=lambda:'native-chat',max_buffered_events=32,replay_checkpoint_root=tmp_path/'chat-replay')
    finalizers.extend([lambda:router.close(timeout_seconds=4),service.close])
    original=route._store_request_cleanup_receipt
    def store(**kwargs):
        receipt=original(**kwargs)
        cleanup.append(receipt)
        return receipt
    route._store_request_cleanup_receipt=store
    if http or browser:
        if browser:
            # The shipped UI offers production-sized budgets. Fake compute ends
            # with the sixth token; retain the real stop-token path, not a DOM edit.
            route.set_stop_token_ids(frozenset({105}))
        from token_http_chat_desk import complete_http_chat
        return complete_http_chat(service=service,graph=graph,failure=failure,browser=browser)
    request_id=service.submit(InferenceSubmission(prompt='live recovery prompt',max_new_tokens=6,
        qualification=qualification_binding(qualification)))
    stream=service.subscribe(request_id,last_event_id=None)
    events=[]
    try:
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            try:
                event=stream.next_event(timeout=2)
            except TimeoutError:
                continue
            if event is None:
                if events and events[-1].kind in ('completed','failed','cancelled'):break
                continue
            events.append(event);stream.ack(event.sequence)
            if event.kind in ('completed','failed','cancelled'):break
        terminals=[e for e in events if e.kind in ('completed','failed','cancelled')]
        assert [(e.kind,e.code) for e in terminals]==[
            ('failed','decode_completion_timeout') if failure else ('completed',None)], [(e.kind,e.code) for e in terminals]
        tokens=[e for e in events if e.kind=='token']
        assert [(e.token_index,e.text) for e in tokens]==[(i,f'<{100+i}>') for i in range(1 if failure else 6)]
        return [int(e.text[1:-1]) for e in tokens]
    finally:
        stream.close()
