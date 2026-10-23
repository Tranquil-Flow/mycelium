#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Private one-Mac token-path A/B; never a device/installed-app qualification.

Real assignment loader, MLX runtime, two native children, local Iroh, TLS mailbox,
worker, concurrent pump, governed PhysicalLiveRoute and request gateway. Qualification, capacity,
resource observer and initial configure admission remain explicit test fixtures.
No downloads. Importing this module never imports MLX or launches anything.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

MODEL_ID = 'Qwen/Qwen2.5-0.5B-Instruct'
REVISION = '7ae557604adf67be50417f59c2c2f167def9a775'
PROMPT = 'Explain how a community garden can conserve water, support biodiversity, and bring neighbours together. Give a detailed practical answer.'
BUDGET = 128
BUSY_ROOT = Path.home() / 'mycelium-demo-run'
ROOT = Path(__file__).resolve().parents[1]


class Busy(RuntimeError):
    pass


def check_busy(root=BUSY_ROOT):
    for name in ('CERT.busy', 'DEVICE.busy'):
        if (Path(root) / name).exists():
            raise Busy(name)


def measured_cleanup(receipts,identity,nodes):
    """Match the measured scope; cryptographic verification remains in the route."""
    for receipt in receipts:
        if any(receipt.get(k)!=v for k,v in identity.items() if k!='cancellation_generation'):continue
        generation=receipt.get('cancellation_generation')
        if type(generation) is not int or generation<identity.get('cancellation_generation',0):continue
        if set(receipt.get('node_ids',[]))!=set(nodes):continue
        body={k:v for k,v in receipt.items() if k!='receipt_digest'}
        digest='sha256:'+hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if receipt.get('receipt_digest')==digest:return receipt
    raise ValueError('measured_request_cleanup_unproven')


def quarantine_clear(path):
    """Inspect, never clear, macOS quarantine (including resolved symlinks)."""
    import ctypes
    import errno
    if sys.platform!='darwin':raise RuntimeError('macos_required')
    path=Path(path).resolve(strict=True)
    libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    get=libc.getxattr
    get.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
    get.restype=ctypes.c_ssize_t
    ctypes.set_errno(0)
    size=get(os.fsencode(path),b'com.apple.quarantine',None,0,0,0)
    if size>=0:raise RuntimeError('quarantined_path_refused')
    if ctypes.get_errno()!=errno.ENOATTR:raise OSError(ctypes.get_errno(),'quarantine_uninspectable',str(path))


def quarantine_native_packages():
    import importlib.util
    count=0
    for name in ('mlx','numpy','cryptography','tokenizers'):
        spec=importlib.util.find_spec(name)
        if spec is None:raise RuntimeError('required_package_missing:'+name)
        roots=list(spec.submodule_search_locations or [])
        if not roots:raise RuntimeError('package_root_missing:'+name)
        for root in roots:
            quarantine_clear(root)
            for path in Path(root).rglob('*'):
                if path.is_file() and path.suffix in ('.so','.dylib'):
                    quarantine_clear(path);count+=1
    if not count:raise RuntimeError('native_package_scan_empty')
    return count


def schedule():
    pairs = [('off', 'on'), ('off', 'on'), ('on', 'off'), ('off', 'on'), ('on', 'off'), ('off', 'on')]
    return [(i, 'cold' if i == 0 else 'warm', mode) for i, pair in enumerate(pairs) for mode in pair]


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * fraction
    low = math.floor(pos)
    return values[low] + (values[math.ceil(pos)] - values[low]) * (pos - low)


def metrics(start, arrivals, end):
    times = [t for t, _ in arrivals]
    if not times or start > times[0] or times[-1] > end or times != sorted(times) or end <= start:
        raise ValueError('invalid_measurement_times')
    gaps = [b - a for a, b in zip(times, times[1:])]
    return dict(tokens=len(times), ttft_s=times[0]-start,
        steady_tok_s=(len(times)-1)/(times[-1]-times[0]) if times[-1] > times[0] else None,
        end_to_end_tok_s=len(times)/(end-start), gap_p50_s=percentile(gaps, .5),
        gap_p95_s=percentile(gaps, .95), duration_s=end-start)


def command_proof(rows, mode, tokens):
    counts = Counter(r['command'] for r in rows)
    starts = [r for r in rows if r['command'] == 'infer_start']
    if len(starts) != 1 or bool(starts[0]['grant']) != (mode == 'on'):
        raise ValueError('invalid_start_count_or_grant')
    if counts['infer_decode'] != (0 if mode == 'on' else tokens-1):
        raise ValueError('invalid_decode_count')
    return dict(counts, infer_decode=counts['infer_decode'])


def validate_assignment(a, stage):
    start, end = ((0, 23), (23, 24))[stage]
    if (a.get('model_id') != MODEL_ID or a.get('resolved_commit') != REVISION
        or any(a.get('runtime', {}).get(k) != v for k, v in
               dict(backend='mlx', dtype='float32', quantization='int8-weight-only').items())
        or a.get('range') != dict(start_layer=start, end_layer_exclusive=end, layer_count=end-start)):
        raise ValueError('model_assignment_not_exact')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x') as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write('\n')
    path.chmod(0o600)


def load_native_runtime(node, config):
    """Called only inside the private native child; no compute replacement."""
    check_busy()
    from runtime_loader import load_assignment_stage
    from mycelium_router.mlx_runtime import MLXRuntimePort
    assignment = json.loads(Path(config['assignment']).read_text())
    report = json.loads(Path(config['report']).read_text())
    loaded = load_assignment_stage(assignment, report, load_generation=17)

    class MeasuredRuntime(MLXRuntimePort):
        executions: list[dict[str,Any]]
        def execute(self, item):
            check_busy()
            begin = time.monotonic()
            result = super().execute(item)
            self.executions.append(dict(request_id=item.request_id, phase=item.phase,
                token_index=item.token_index, started=begin, finished=time.monotonic(),
                token_id=result.token_id, success=result.success))
            return result

    placement = next(p for s in node.graph.stages for p in s.placements if p.node_id == node.node_id)
    runtime = MeasuredRuntime(node.node_id, node.graph, {placement.placement_id: loaded},
        clock=node._clock.now, decode_mode='stage_local_kv', kv_lease_renewal_seconds=30.,
        materialization='per_stage')
    runtime.executions = []
    return runtime


class Bench:
    window = 240

    def __init__(self, args, pair, warmth, mode):
        self.args, self.pair, self.warmth, self.mode = args, pair, warmth, mode
        self.cleanup_verified=True  # No children yet; cleared before native launch.
        self.start=self.end=0.
        self.native_inputs = {}
        self.commands, self.arrivals, self.receipts, self.http = [], [], [], []
        self.identities={}
        self.result: dict[str,Any] = dict(pair=pair, warmth=warmth, mode=mode, model_id=MODEL_ID, revision=REVISION,
            split=[[0, 23], [23, 24]], seed=0, max_new_tokens=BUDGET, stop_token_ids=[],
            prompt=PROMPT, prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
            claim_ceiling='one Mac/two native processes/loopback; synthetic qualification and load admission; NOT two-Mac or installed-app evidence',
            executed_by='local M4 Pro benchmark process',
            cold_definition='fresh native model-loaded processes; loader probes already ran; no OS cache flush',
            warm_definition='fresh native processes followed by an unmeasured 16-token same-prompt request',
            timing=dict(pump_wait_ceiling_s=.05, pump_wake='production wait_for_wake',
                mailbox='default long_poll_hold=1.5, long_poll_step=0.02', health='real route monitor, unchanged defaults'))

    def prepare_graph(self, tmp_path, entry, tail):
        check_busy()
        from layer_assignment import assignment_id_for
        from weight_provisioning import provision_assignment
        from runtime_loader import load_assignment_stage, canonical_json
        from mycelium_qualification.physical_deployment import build_execution_graph
        import gc
        import mlx.core as mx
        assignments, proofs = [], []
        deployment = str(uuid.uuid4())
        for i, node in enumerate((entry, tail)):
            check_busy()
            a = json.loads((self.args.template / f'node-{i}-assignment.json').read_text())
            validate_assignment(a, i)
            a.update(deployment_id=deployment, node_id=node, artifact_cache_root=str(self.args.model))
            a['control_plane_binding']['deployment_id'] = deployment
            a['assignment_id'] = assignment_id_for(a)
            def fetch(model_id, revision, filename, cache_root, *, local_files_only):
                if (model_id, revision, local_files_only) != (MODEL_ID, REVISION, True):
                    raise ValueError('offline_model_identity_mismatch')
                path = self.args.model / filename
                if not path.is_file(): raise FileNotFoundError(path)
                return path, True
            report = provision_assignment(a, fetch_file=fetch, local_files_only=True)
            if report.get('network_download_bytes') != 0 or not report.get('ready_for_load'):
                raise ValueError('offline_provisioning_failed')
            ap, rp = tmp_path/f'assignment-{i}.json', tmp_path/f'artifacts-{i}.json'
            write_json(ap, a); write_json(rp, report)
            loaded = load_assignment_stage(a, report, load_generation=17)
            proofs.append(json.loads(canonical_json(loaded.proof)))
            assignments.append(a)
            self.native_inputs[node] = dict(assignment=str(ap), report=str(rp))
            self.result.setdefault('source_files',report['verified_files'])
            del loaded
            gc.collect(); mx.clear_cache()
        self.entry, self.tail = entry, tail
        self.result['load_proofs'] = proofs
        graph = build_execution_graph(assignments, proofs, link_scheme='iroh', runtime_scheme='iroh')
        self.result['graph'] = asdict(graph)
        return graph

    def run(self, *, route, graph, tmp_path, cleanup, finalizers, sessions, owner, client):
        from types import SimpleNamespace
        from mycelium_live.codec import Qwen2PromptCodec
        from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
        from token_bench_chat import gateway
        codec = Qwen2PromptCodec.from_deployment(self.args.model)
        prompt = list(codec.encode(PROMPT))
        self.result['prompt_token_ids'] = prompt
        self.result['codec'] = codec.evidence_identity()
        self.result['generation_note'] = 'production quantized greedy sampler; seed 0; fixed budget, EOS stop disabled identically'
        self.result['surface'] = 'RequestGatewayService -> RouterSessionBackend -> M16/LiveRouterPort -> governed route'
        self.result.update(cleanup=cleanup,commands=self.commands,receipt_records=self.receipts,target_http=self.http)
        service,qualification,request_ids=gateway(route=route,graph=graph,codec=codec,
            tmp_path=tmp_path,cleanup=cleanup,finalizers=finalizers)
        if self.warmth=='cold':next(request_ids)
        router_tokens={}
        original_infer=route.infer
        def capture_infer(*args,**kwargs):
            sink=kwargs['sink'];request_id=kwargs['request_id']
            tokens=router_tokens.setdefault(request_id,[])
            self.identities[request_id]=dict(kwargs['command_identity'])
            if request_id=='measured':self.result['token_ids']=tokens
            def emit(index,token):
                if index!=len(tokens):raise RuntimeError('router_token_index_gap')
                tokens.append(token);sink.emit(index,token)
            kwargs['sink']=SimpleNamespace(emit=emit)
            return original_infer(*args,**kwargs)
        route.infer=capture_infer
        original_request=client._request
        def observed_http(path,envelope):
            row=dict(path=path,started=time.monotonic())
            self.http.append(row)
            try:
                result=original_request(path,envelope)
                row['ok']=True
                return result
            finally:row['finished']=time.monotonic()
        client._request=observed_http
        for node, session in sessions.items():
            send = session.send
            def observed(*, command_id, command, payload, send=send, node=node):
                row = dict(node=node, command_id=command_id, command=command,
                    request_id=payload.get('request_id', payload.get('request', {}).get('request_id')),
                    grant='token_grant' in payload, started=time.monotonic())
                self.commands.append(row)
                try:
                    response = send(command_id=command_id, command=command, payload=payload)
                    row['ok'] = response.get('ok')
                    return response
                finally: row['finished'] = time.monotonic()
            session.send = observed
        if owner is not None:
            seal = owner.seal
            def observed_seal(raw):
                seal(raw)
                self.receipts.append(dict(at=time.monotonic(), receipt=json.loads(raw)))
            owner.seal = observed_seal
        # The original fixture sets _open directly; explicitly run the shipped
        # monitor here rather than silently dropping production health traffic.
        monitor = threading.Thread(target=route._active_liveness_monitor_loop,
            name='mycelium-tokenpath-bench-liveness', daemon=True)
        monitor.start()
        def stop_monitor():
            route._liveness_monitor_stop.set(); monitor.join(5)
            if monitor.is_alive(): raise RuntimeError('monitor_cleanup_unproven')
        finalizers.append(stop_monitor)
        def infer(request_id, budget, measured):
            check_busy()
            arrivals = []
            start = time.monotonic()
            if measured:
                self.start=start;self.arrivals=arrivals;self.result['arrivals']=arrivals
            rid=service.submit(InferenceSubmission(prompt=PROMPT,max_new_tokens=budget,
                qualification=qualification_binding(qualification)))
            if rid!=request_id:raise RuntimeError('request_identity_mismatch')
            stream=service.subscribe(rid,last_event_id=None)
            terminal=None
            try:
                while time.monotonic()-start<120:
                    try:check_busy()
                    except Busy:
                        service.cancel(rid)
                        raise
                    try:event=stream.next_event(timeout=.1)
                    except TimeoutError:continue
                    if event is None:continue
                    at=time.monotonic()
                    stream.ack(event.sequence)
                    if event.kind=='token':
                        if event.token_index!=len(arrivals):raise RuntimeError('event_token_index_gap')
                        arrivals.append((at,router_tokens[rid][event.token_index]))
                    if event.kind in ('completed','failed','cancelled'):
                        terminal=(event.kind,event.code);break
            finally:
                stream.close()
                if measured:
                    self.end=time.monotonic()
                    if arrivals:self.result.update(metrics(start,arrivals,self.end))
            end = time.monotonic()
            if terminal is None:raise RuntimeError('chat_terminal_missing')
            if [t for _,t in arrivals] != router_tokens.get(rid,[]):
                raise RuntimeError('token_count_or_sink_parity')
            if terminal==('completed',None) and len(arrivals)!=budget:
                raise RuntimeError('completed_token_budget_mismatch')
            if measured:
                self.start, self.end, self.arrivals = start, end, arrivals
                if arrivals:self.result.update(metrics(start, arrivals, end))
                self.result['token_ids'] = list(router_tokens.get(rid,[]))
                self.result['terminal'],self.result['terminal_code'] = terminal
                self.result['successful'] = terminal==('completed',None)
            elif terminal!=('completed',None):
                raise RuntimeError('warmup_failed:'+str(terminal))
            return router_tokens.get(rid,[])
        if self.warmth == 'warm':
            infer('warmup', 16, False)
        self.result['load_before'] = list(os.getloadavg())
        tokens = infer('measured', BUDGET, True)
        self.result['load_after'] = list(os.getloadavg())
        self.result['cleanup'] = cleanup
        receipt=measured_cleanup(cleanup,self.identities['measured'],{self.entry,self.tail})
        self.result['measured_cleanup']=receipt
        self.result['request_cleanup_verified']=route._bench_cleanup_verified.get(receipt['receipt_digest']) is True
        if not self.result['request_cleanup_verified']:raise RuntimeError('measured_signed_cleanup_unproven')
        return list(tokens)

    def mailbox_rows(self,database):
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            rows=[]
            for row in db.execute('SELECT * FROM managed_commands'):
                row=dict(row);statement=json.loads(row['envelope_json'])['statement'];native=statement['native_command']
                rows.append(dict(command=native['command'],command_id=native['command_id'],
                    grant='token_grant' in native.get('payload',{}),issued_at=statement['issued_at'],
                    claimed_at=row.get('claimed_at'),updated_at=row.get('updated_at'),state=row.get('state')))
            return rows

    def capture_failure(self,*,sessions,events,database):
        """Bounded metadata only, before teardown deletes private credentials."""
        self.result['pump_events']=events
        final={}
        for node,session in sessions.items():
            if session is None:continue
            try:
                final[node]=session.send_before(command_id='bench-failure-'+uuid.uuid4().hex,
                    command='desk_info',payload={},deadline_monotonic_s=time.monotonic()+2)['result']
            except BaseException as exc:final[node]={'diagnostic_error_type':type(exc).__name__}
        self.result['failure_native']=final
        try:self.result['mailbox_rows']=self.mailbox_rows(database)
        except BaseException as exc:self.result['mailbox_diagnostic_error_type']=type(exc).__name__

    def finish(self, *, final, events, database):
        self.result.update(final_native=final,pump_events=events,mailbox_rows=self.mailbox_rows(database))
        # Both arms use the serving mode. OFF changes only token-path control,
        # not compute granularity; evidence comes from the actual native ports.
        for node in (self.entry, self.tail):
            runtime = final[node].get('runtime', {})
            if (runtime.get('materialization'), runtime.get('work_unit')) != (
                    'per_stage', 'transformer_stage'):
                raise ValueError('serving_materialization_evidence_mismatch')
        trace = final[self.tail]['executions']
        produced = [x for x in trace if x['request_id']=='measured' and x['token_id'] is not None]
        if [x['token_id'] for x in produced][:len(self.arrivals)] != self.result['token_ids']:
            raise RuntimeError('native_tail_sink_parity')
        self.result['native_token_ids']=[x['token_id'] for x in produced]
        if len(produced)>1:
            self.result['native_steady_tok_s']=(len(produced)-1)/(produced[-1]['finished']-produced[0]['finished'])
        lags = [arrival[0]-x['finished'] for arrival,x in zip(self.arrivals,produced)]
        self.result['delivery_lag_p50_s'] = percentile(lags,.5)
        self.result['delivery_lag_p95_s'] = percentile(lags,.95)
        self.result['commands'] = self.commands
        measured = [r for r in self.commands if self.start <= r['started'] <= self.end]
        self.result['command_counts'] = (command_proof(measured,self.mode,BUDGET)
            if self.mode=='on' or self.result['successful'] else dict(Counter(r['command'] for r in measured)))
        self.result['sealed_receipts'] = len([r for r in self.receipts if self.start <= r['at'] <= self.end])
        native_commands=[r for v in final.values() for r in v['command_audit']
            if self.start<=r['started']<=self.end]
        self.result['native_command_counts']=dict(Counter(r['command'] for r in native_commands))
        if self.mode=='on' or self.result['successful']:command_proof(native_commands,self.mode,BUDGET)
        emits={(r['grant_digest'],r['sequence']):r for r in final[self.entry]['progress_audit']}
        receipt_lags=[]
        for row in self.receipts:
            statement=row['receipt']['statement']
            emit=emits[(statement['grant_digest'],statement['sequence'])]
            if self.start<=emit['started']<=self.end:
                receipt_lags.append(row['at']-emit['started'])
        self.result['receipt_lag_p50_s']=percentile(receipt_lags,.5)
        self.result['receipt_lag_p95_s']=percentile(receipt_lags,.95)
        self.result['arrivals'] = self.arrivals
        self.result['receipt_records'] = self.receipts
        self.result['target_http'] = self.http
        self.result['target_http_counts'] = dict(Counter(r['path'] for r in self.http if self.start<=r['started']<=self.end))
        if not all(v['active']==0 for v in final.values()): raise RuntimeError('native_cleanup_unproven')
        if not self.result.get('request_cleanup_verified'):raise RuntimeError('measured_signed_cleanup_unproven')
        self.result['cleanup_proven'] = True


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-real',action='store_true',help='required opt-in; fresh private scratch only')
    parser.add_argument('--model',type=Path,required=True,help='existing exact HF snapshot; never downloaded')
    parser.add_argument('--template',type=Path,required=True,help='read-only cached [0,23)+[23,24) assignment templates')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--limit',type=int,default=12,choices=range(1,13),help='diagnostic prefix, not a full campaign')
    args=parser.parse_args(argv)
    if not args.execute_real: parser.error('--execute-real is required; nothing launched')
    check_busy()
    if args.model.name != REVISION: raise ValueError('snapshot_revision_not_exact')
    args.model=args.model.resolve();args.template=args.template.resolve();args.out=args.out.resolve()
    # Normal package execution, direct scripts, and file-spec imports remain safe.
    for p in (ROOT,ROOT/'tests/a13_macos'):
        if str(p) not in sys.path: sys.path.insert(0,str(p))
    # Inspect extensions before importing any dependency that could load them.
    quarantine_clear(sys.executable)
    quarantine_clear(__file__)
    quarantine_native_packages()
    from test_token_two_process_join import test_owner_native_two_process_route, _safe, BINARY
    from tests.live.test_token_path_admission import world
    import pytest
    for p in (Path(sys.executable),Path(__file__),ROOT/'tests/live/token_path_desk_child.py',BINARY): _safe(p)
    args.out.mkdir(parents=True,exist_ok=False,mode=0o700)
    import importlib.metadata
    import subprocess
    paths=['scripts/tokenpath_ab_bench.py','tests/a13_macos/test_token_two_process_join.py',
           'tests/a13_macos/token_governed_desk.py','tests/a13_macos/token_bench_chat.py',
           'tests/live/token_path_desk_child.py','mycelium_a13_macos/managed_runtime.py',
           'mycelium_a13_macos/concurrent_pump.py','mycelium_live/route.py',
           'mycelium_router/mlx_runtime.py','runtime_loader.py']
    write_json(args.out/'identity.json',dict(source_commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
        files={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
        sidecar_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
        python=sys.version,mlx=importlib.metadata.version('mlx'),
        machine=subprocess.check_output(['/usr/sbin/sysctl','-n','machdep.cpu.brand_string','hw.memsize'],text=True).strip(),
        timing='production; no product defaults patched',network='local-only Iroh, loopback TLS'))
    results=[]
    for pair,warmth,mode in schedule()[:args.limit]:
        check_busy()
        bench=Bench(args,pair,warmth,mode)
        tag=f'{pair}-{warmth}-{mode}'
        scratch=Path(tempfile.mkdtemp(prefix='tokpab-',dir=os.environ['TMPDIR']))
        scratch.chmod(0o700)
        print('START',tag,'load',os.getloadavg(),flush=True)
        try:
            with pytest.MonkeyPatch.context() as mp:
                test_owner_native_two_process_route(scratch,world.__wrapped__(),mp,mode=='on','signed',True,bench=bench)
            if not bench.cleanup_verified:raise RuntimeError('process_cleanup_unconfirmed')
            bench.result['process_cleanup_proven']=True
            write_json(args.out/(tag+'.json'),bench.result)
            results.append(bench.result)
            print('RESULT',tag,json.dumps({k:bench.result.get(k) for k in ('terminal','terminal_code','tokens','ttft_s','steady_tok_s','end_to_end_tok_s','command_counts')}),flush=True)
        except BaseException as exc:
            bench.result['error']=str(exc)
            bench.result['error_type']=type(exc).__name__
            write_json(args.out/(tag+'-FAILED.json'),bench.result)
            raise
        finally:
            # No private signing keys retained. Exact signed receipts are in results.
            if bench.cleanup_verified:shutil.rmtree(scratch)
            else:print('UNCONFIRMED_CLEANUP_PRIVATE_STATE',str(scratch),flush=True)
        reference=next((r['token_ids'] for r in results if r.get('successful')),None)
        if reference is not None and any(r['token_ids']!=reference[:len(r['token_ids'])]
                                        or r['native_token_ids']!=reference[:len(r['native_token_ids'])] for r in results):
            raise RuntimeError('on_off_token_prefix_parity_failed')
    write_json(args.out/'campaign.json',dict(runs=len(results),prefix_parity=True,
        full_parity=all(r.get('successful') for r in results),complete=len(results)==12,
        successful_runs=sum(r.get('successful',False) for r in results),
        source_commit=__import__('subprocess').check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
