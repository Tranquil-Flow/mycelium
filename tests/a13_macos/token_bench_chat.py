# SPDX-License-Identifier: AGPL-3.0-only
"""Private benchmark admission fixture, NOT physical qualification authority.

This projects the existing synthetic qualification fixture onto the measured
model graph. It must never be used in an installed app or exported as live
qualification evidence. Signature/grant/mailbox/native cleanup checks are real;
physical qualification and M16 resource observations remain test doubles.
"""
from dataclasses import replace
from types import SimpleNamespace
import time


def fixture_qualification(graph):
    from tests.request_gateway.test_core import _synthetic_qualification
    from mycelium_qualification.evidence import sha256_document
    from mycelium_router.serialization import execution_graph_to_dict
    q = _synthetic_qualification()
    # Only this private fixture is retargeted. No production parser, issuer,
    # admission validator, native signature or authority callback is patched.
    for field in ('deployment_id', 'deployment_epoch', 'topology_version',
                  'model_id', 'resolved_commit', 'manifest_digest'):
        object.__setattr__(q, field, getattr(graph, field))
    object.__setattr__(q, 'qualification_id', 'synthetic-test-fixture:tokenpath-real-compute')
    object.__setattr__(q, 'execution_graph_digest', sha256_document(execution_graph_to_dict(graph)))
    object.__setattr__(q, 'claim_boundary', 'PRIVATE BENCHMARK SYNTHETIC ADMISSION; NOT PHYSICAL QUALIFICATION')
    bindings=[]
    for stage, old in zip(graph.stages, q.stage_bindings, strict=True):
        p=stage.placements[0]
        bindings.append(replace(old,stage_id=stage.stage_id,placement_id=p.placement_id,
            node_id=p.node_id,assignment_id=p.assignment_id,stage_signature=p.stage_signature,
            load_proof_digest=p.load_proof_digest))
    object.__setattr__(q,'stage_bindings',tuple(bindings))
    return q


def gateway(*,route,graph,codec,tmp_path,cleanup,finalizers):
    from mycelium_m16_runtime import M16RuntimeCoordinator
    from mycelium_router.contracts import DeviceState, RouterConfig
    from mycelium_router.fakes import ManualClock, SequenceIdSource
    from mycelium_live.router_port import LiveRouterPort
    from mycelium_request_gateway.backend import RouterSessionBackend
    from mycelium_request_gateway.service import RequestGatewayService
    from tests.request_gateway.test_core import MutableQualificationSource
    q=fixture_qualification(graph)
    route.set_deployment_qualification(q,quantization='int8-weight-only')
    now=time.time()
    nodes={p.node_id for s in graph.stages for p in s.placements}
    states={n:DeviceState(n,1,now,'ALIVE',now+240,1.,2_000_000_000,0,{}, {}) for n in nodes}
    capacities={p.placement_id:dict(memory_capacity_bytes=2_000_000_000,
        kv_capacity_bytes=500_000_000,workspace_capacity_bytes=500_000_000,maximum_reservations=4)
        for s in graph.stages for p in s.placements}
    coordinator=M16RuntimeCoordinator(graph=graph,device_states=states,placement_capacities=capacities,
        workload_profiles={'interactive_chat_v1':'interactive'},clock=ManualClock(now),
        lease_clock=SimpleNamespace(now=time.time),id_source=SequenceIdSource(),config=RouterConfig())
    router=LiveRouterPort(route=route,execution_graph=graph,runtime_coordinator=coordinator)
    ids=iter(('warmup','measured'))
    service=RequestGatewayService(qualification_source=MutableQualificationSource(q),
        backend=RouterSessionBackend(router=router,codec=codec,clock=time.time,sampling_seed=0),
        request_id_source=lambda:next(ids),max_buffered_events=512,replay_checkpoint_root=tmp_path/'chat-replay')
    finalizers.extend([lambda:router.close(timeout_seconds=4),service.close])
    original=route._store_request_cleanup_receipt
    route._bench_cleanup_verified={}
    def store(**kwargs):
        receipt=original(**kwargs)
        route._bench_cleanup_verified[receipt['receipt_digest']]=route._cancellation_cleanup_complete(
            kwargs['participating_node_ids'],cleanup_subject=kwargs['cleanup_subject'])
        cleanup.append(receipt)
        return receipt
    route._store_request_cleanup_receipt=store
    return service,q,ids
