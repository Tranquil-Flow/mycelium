# SPDX-License-Identifier: AGPL-3.0-only
"""Real route over joined native sessions; fixture qualification/compute only.

No production signature or native cleanup verifier is replaced. Qualification
and the owner cleanup callback are desk authorities, not hardware qualification.
"""
from dataclasses import asdict
import time
import json
from types import SimpleNamespace

from physical_inference_qualification import QualificationController
from mycelium_live.route import PhysicalLiveRoute
from mycelium_a13_macos.token_owner import ManagedRouteTokenOwner


def run_governed(*, tmp_path, monkeypatch, world, a, graph, members, signers,
                 sessions, infos, enabled, inference_commands, chat=False, finalizers=None, failure=None, startup=False,bench=None):
    window=a.get('window',50) if bench is None else bench.window
    current={n:dict(node_id=n,endpoint_id=m['control_endpoint_id'],generation=m['generation'],
                   verification_key_digest=m['verification_key_digest'],lease_expires_at=time.time()+window)
             for n,m in members.items()}
    owner=ManagedRouteTokenOwner(signer=a['owner_signer'],current_member=(a['authority'].coordinator.member
        if 'apps' in a else current.__getitem__),
        mailbox=a['mailbox'],evidence_root=tmp_path/'governed-receipts',
        process_deadline_monotonic_s=time.monotonic()+window,
        clock_uncertainty_ms=None if 'apps' in a else 100) if enabled else None
    controller=object.__new__(QualificationController)
    controller.peers=tuple(SimpleNamespace(node_id=n,host_id=infos[n]['identity']['host_id'],
        boot_id=infos[n]['identity']['boot_id'],
        process_transport='managed_https' if n==a['member']['node_id'] else 'local') for n in members)
    endpoints={n:dict(endpoint_id=m['endpoint_id'],membership_generation=m['generation']) for n,m in members.items()}
    route=PhysicalLiveRoute(controller=controller,endpoints=endpoints,
        run_plan=dict(run_id=world.pins['run_id'],deployment_id=graph.deployment_id,
            entry_node_id=a['member']['node_id'],expected_token_ids=[100,101],
            request=dict(world.request),nodes=[dict(node_id=n,configure=dict(graph=json.loads(json.dumps(asdict(graph))))) for n in members]),
        token_path_owner=owner)
    route._sessions=sessions
    route._identities={n:infos[n]['identity'] for n in members}
    route._verification_keys={n:signers[n].public_key_record() for n in members}
    route._open=True
    route._deployment_qualification=SimpleNamespace(route_ready=True)
    if not chat and bench is None:
        monkeypatch.setattr('mycelium_request_gateway.contracts.qualification_digest',lambda _:world.control['qualification_digest'])
    for session in sessions.values():
        original=session.send
        def send(*,command_id,command,payload,original=original):
            if command in ('infer_start','infer_decode'):inference_commands.append(command)
            return original(command_id=command_id,command=command,payload=payload)
        monkeypatch.setattr(session,'send',send)
    control={**world.control,'request_id':world.request['request_id']}
    identity={k:control[k] for k in ('request_id','request_attempt','path_id','path_attempt',
        'deployment_id','deployment_epoch','topology_generation')}
    identity['path_manifest_digest']=control['path_digest']
    tokens=[];cleanup=[];qualification=None
    try:
        if startup:
            finalizers.insert(0,route.close)
            from token_startup_desk import qualify_startup
            qualification=qualify_startup(route=route,owner=owner,sessions=sessions,graph=graph,
                tmp_path=tmp_path,monkeypatch=monkeypatch,inference_commands=inference_commands,enabled=enabled)
        if bench is not None:
            tokens=bench.run(route=route,graph=graph,tmp_path=tmp_path,cleanup=cleanup,finalizers=finalizers,
                sessions=sessions,owner=owner,client=a['target_client'])
        elif chat:
            from token_chat_desk import complete_chat
            tokens=complete_chat(route=route,graph=graph,tmp_path=tmp_path,cleanup=cleanup,finalizers=finalizers,failure=failure,
                http=chat=='http',browser=chat=='browser',qualification=qualification)
        else:
            result=route.infer(world.request['prompt_token_ids'],max_new_tokens=6,request_id=world.request['request_id'],
                sink=SimpleNamespace(emit=lambda i,t:tokens.append(t)),route_identity=identity,
                locked_path_manifest=world.manifest,command_identity=control,
                cancel_requested=lambda:False,authorize_cleanup=lambda deadline:deadline,
                publish_cleanup_receipt=lambda receipt:cleanup.append(dict(receipt)))
            assert tuple(tokens)==result.token_ids
        assert cleanup and set(cleanup[0]['node_ids'])==set(members)
        route._snapshot_all()
        assert set(route._last_snapshots)==set(members)
        assert all(s['details']['runtime']['active_state_count']==0 for s in route._last_snapshots.values())
        if bench is None:
            assert all(s['details']['runtime']['applied_operation_count']==((2 if failure else 6)+(2 if startup else 0))
                       for s in route._last_snapshots.values())
        return tokens,list((tmp_path/'governed-receipts').glob('*.receipt'))
    finally:
        route._open=False
        route._cleanup_reconciliation_stop.set()
        route._cleanup_reconciliation_executor.shutdown(wait=True,cancel_futures=True)
