# SPDX-License-Identifier: AGPL-3.0-only
"""Private CPU proof child: shipped native service/Router/Iroh, fake runtime.

No public activation or model loading. All inputs live in an owner-only fixture.
"""
import json
from pathlib import Path
import sys
import threading
import time

from physical_inference_node import (PhysicalNodeService, _ResponseEmitter, _response,
    execution_graph_from_document)
from mycelium_node.identity import load_node_signer
from mycelium_node.token_participants import ParticipantAdmissionPolicy
from mycelium_router.contracts import DeviceState, RuntimeResult
from mycelium_router.fakes import FakeCapacityPort, FakeDeviceStateProvider, FakeTopologyProvider


class Capacity(FakeCapacityPort):
    def snapshot(self):
        return dict(active_reservation_count=len(set(self._reservation_requests)-self.released_ids),
            committed_reservation_count=len(self.committed_ids-self.released_ids))


class Runtime:
    decode_mode='stage_local_kv'
    def __init__(self, node_id, tail_id, failure=None):
        self.node_id,self.tail_id=node_id,tail_id
        self.failure=failure
        self.active=set()
        self.executions=[]
    def execute(self,item):
        self.active.add(item.path_id)
        self.executions.append((item.phase,item.token_index))
        if self.failure=='stage-loss' and self.node_id==self.tail_id and item.token_index==1:
            return RuntimeResult(success=False,failure_scope='placement',failure_reason='fixture_stage_lost')
        token=100+max(0,item.token_index) if self.node_id==self.tail_id else None
        return RuntimeResult(success=True,payload=item.payload,token_id=token)
    def execute_batch(self,batch):
        return tuple(self.execute(item) for item in batch.items)
    def cancel(self,path_id):
        self.active.discard(path_id)
    def kv_subject_clean(self,request_id,path_id,path_attempt):
        return path_id not in self.active
    def kv_snapshot(self):
        return dict(mode=self.decode_mode,states={p:{} for p in sorted(self.active)},
            active_state_count=len(self.active),active_kv_bytes=0,
            applied_operation_count=len(self.executions))
    def close(self,**_):
        self.active.clear()


def main():
    d=json.loads(Path(sys.argv[1]).read_text())
    window=d.get('window',50)

    n=PhysicalNodeService(run_id=d['run_id'],deployment_id=d['graph']['deployment_id'],
        node_id=d['node_id'],artifact_root=Path(d['root']),socket_root=Path(d['socket_root']),
        sidecar_binary=Path(d['binary']),sidecar_local_only=True,command_timeout=15 if not d.get('real_compute') else 120,
        endpoint_secret_file=Path(d['key_file']),requested_decode_mode='stage_local_kv',
        token_policy_trust_file=Path(d['root'])/'token-host.private.json' if d.get('bootstrap')=='signed' and d['enabled'] else None)
    if d.get('startup'):
        # Model/host metadata are synthetic qualification fixtures. The two real
        # processes run on ONE desk host; this must never become device evidence.
        n.host_id='synthetic-desk-host-'+n.node_id
    def configure_fixture():
        n.sidecar=n._new_sidecar_process()
        ready=n.sidecar.start()
        n.endpoint_id,n.endpoint_addr=ready['endpoint_id'],ready['endpoint_addr']
        n.signer=load_node_signer(Path(d['key_file']),endpoint_id=n.endpoint_id)
        n.graph=execution_graph_from_document(d['graph'])
        n.topology=FakeTopologyProvider(n.graph)
        now=time.time()
        n.device_states=FakeDeviceStateProvider({node:DeviceState(node,1,now,'ALIVE',now+window+10,
            1.,1_000_000 if not d.get('real_compute') else 2_000_000_000,0,{}, {}) for node in d['members']})
        n.capacity=Capacity()
        if d.get('startup'):
            from physical_sqlite_capacity import SQLiteQualificationCapacityPort
            n.capacity=SQLiteQualificationCapacityPort(Path(d['root'])/'capacity.sqlite3',n.topology,
                {node:1_000_000 for node in d['members']},clock=n._clock,id_source=n._ids,
                maximum_imported_lease_seconds=60)
        if d.get('real_compute'):
            from scripts.tokenpath_ab_bench import load_native_runtime
            n.runtime=load_native_runtime(n,d['real_compute'])
            n.runtime_identity=dict(backend='mlx',dtype='float32',quantization='int8-weight-only')
        else:
            n.runtime=Runtime(n.node_id,d['tail_id'],d.get('failure'))
            n.runtime_identity=dict(backend='numpy',dtype='float32',quantization='none')  # fixture compute metadata
        n._kv_lease_window_seconds=lambda:30.0
        n.state='CONFIGURED'
    if not d.get('startup'):configure_fixture()
    emitter=_ResponseEmitter()
    command_audit=[];progress_audit=[]
    if d.get('real_compute'):
        original_progress=emitter.emit_progress
        def measured_progress(**kwargs):
            receipt=kwargs['receipt']
            row=dict(started=time.monotonic(),sequence=receipt['statement']['sequence'],
                grant_digest=receipt['statement']['grant_digest'])
            progress_audit.append(row)
            try:return original_progress(**kwargs)
            finally:row['finished']=time.monotonic()
        emitter.emit_progress=measured_progress
    n._token_response_emitter=emitter
    def install():
        def peers():
            values={p.node_id:dict(node_id=p.node_id,endpoint_id=p.endpoint_id,generation=p.generation)
                    for p in n.transport._ordered_peer_bindings()}
            values[n.node_id]={k:d['members'][n.node_id][k] for k in ('node_id','endpoint_id','generation')}
            return values
        n.provision_token_path(ParticipantAdmissionPolicy(run_id=n.run_id,requester_id='requester',
            owner_endpoint_id=d['owner_endpoint_id'],owner_key=d['owner_key'],
            members=lambda:d['members'],peers=peers,max_new_tokens=6,stop_token_ids=(),
            process_deadline_monotonic_s=time.monotonic()+60,
            lease_deadline_monotonic_s=time.monotonic()+60,clock_uncertainty_ms=100))
    def execute(c):
        audit=None
        if d.get('real_compute'):
            audit=dict(command=c['command'],command_id=c['command_id'],started=time.monotonic(),
                grant='token_grant' in c['payload'])
            command_audit.append(audit)
        try:
            if c['command']=='desk_info':
                runtime_snapshot={} if n.runtime is None else n.runtime.kv_snapshot()
                result={'endpoint_addr':n.endpoint_addr,'identity':{**n._identity(),'boot_id':n.boot_id},
                    'executions':[] if n.runtime is None else n.runtime.executions,
                    'active':runtime_snapshot.get('active_state_count',0),
                    'runtime':runtime_snapshot,
                    'command_audit':command_audit,'progress_audit':progress_audit}
            elif c['command']=='configure' and d.get('startup'):
                assert n.state=='NEW' and c['payload']['graph']==d['graph']
                configure_fixture()
                placement=next(p for s in n.graph.stages for p in s.placements if p.node_id==n.node_id)
                result=n._signed_result('configured',dict(assignment_id=placement.assignment_id,
                    placement_id=placement.placement_id,manifest_digest=n.graph.manifest_digest,
                    endpoint_addr=n.endpoint_addr,runtime_mode=n.runtime.decode_mode,
                    stage_pack_digest='sha256:'+'a'*64,stage_pack_verification_digest='sha256:'+'b'*64))
            else:
                result=n.dispatch(c)
                if c['command']=='start' and d['enabled'] and d.get('bootstrap')!='signed':install()
            emitter.emit(_response(n,command_id=c['command_id'],ok=True,result=result))
            if audit is not None:
                audit['ok']=True
                if c['command']=='infer_cancel_wait':audit['signed_cleanup']=result
            n.run_after_response()
        except BaseException as e:
            # Keep private command/grant material out of diagnostics.
            code=getattr(e,'code','node_command_failed')
            print('DESK_CHILD_ERROR '+str(code),file=sys.stderr,flush=True)
            emitter.emit(_response(n,command_id=c['command_id'],ok=False,error_code=str(code)))
        finally:
            if audit is not None:audit['finished']=time.monotonic()
    try:
        for raw in sys.stdin:
            c=json.loads(raw)
            if c['command']=='stop':execute(c);break
            threading.Thread(target=execute,args=(c,),daemon=True).start()
    finally:
        if n.transport is not None:n.transport.close()
        if n.sidecar is not None:n.sidecar.close()


if __name__=='__main__':main()
