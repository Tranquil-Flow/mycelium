# SPDX-License-Identifier: AGPL-3.0-only
"""Pure benchmark contract checks: no model, subprocess, device or network."""
import importlib.util
from pathlib import Path
import sys

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / 'scripts/tokenpath_ab_bench.py'
    spec = importlib.util.spec_from_file_location('tokenpath_ab_bench_contract', path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def test_exact_order_and_separate_cold_pair():
    m = module()
    assert m.schedule() == [(0, 'cold', 'off'), (0, 'cold', 'on'),
        (1, 'warm', 'off'), (1, 'warm', 'on'), (2, 'warm', 'on'), (2, 'warm', 'off'),
        (3, 'warm', 'off'), (3, 'warm', 'on'), (4, 'warm', 'on'), (4, 'warm', 'off'),
        (5, 'warm', 'off'), (5, 'warm', 'on')]


def test_metrics_use_delivery_times_and_exclude_prefill():
    m = module()
    r = m.metrics(10., [(11., 5), (11.5, 6), (12., 7)], 13.)
    assert r['ttft_s'] == 1.
    assert r['steady_tok_s'] == 2.
    assert r['end_to_end_tok_s'] == 1.
    assert r['gap_p50_s'] == .5
    assert r['gap_p95_s'] == .5
    assert r['tokens'] == 3
    with pytest.raises(ValueError): m.metrics(10., [], 13.)
    with pytest.raises(ValueError): m.metrics(10., [(11., 1), (10.9, 2)], 13.)


@pytest.mark.parametrize('flag', ['CERT.busy', 'DEVICE.busy'])
def test_busy_refuses_before_any_effect(tmp_path, flag):
    m = module()
    (tmp_path / flag).write_text('busy')
    with pytest.raises(m.Busy): m.check_busy(tmp_path)
    (tmp_path / flag).unlink()
    m.check_busy(tmp_path)


def test_revision_and_ranges_fail_closed():
    m = module()
    a = {'model_id': m.MODEL_ID, 'resolved_commit': m.REVISION,
         'runtime': {'backend': 'mlx', 'dtype': 'float32', 'quantization': 'int8-weight-only'},
         'range': {'start_layer': 0, 'end_layer_exclusive': 23, 'layer_count': 23}}
    m.validate_assignment(a, 0)
    a['resolved_commit'] = 'wrong'
    with pytest.raises(ValueError): m.validate_assignment(a, 0)


def test_command_proof_rejects_silent_fallback():
    m = module()
    rows = [{'command': 'infer_start', 'grant': True}]
    assert m.command_proof(rows, 'on', 128)['infer_decode'] == 0
    with pytest.raises(ValueError): m.command_proof(rows + [{'command': 'infer_decode'}], 'on', 128)
    rows = [{'command': 'infer_start', 'grant': False}] + [{'command': 'infer_decode'}] * 127
    assert m.command_proof(rows, 'off', 128)['infer_decode'] == 127
    with pytest.raises(ValueError): m.command_proof(rows[:-1], 'off', 128)


def test_cli_refuses_without_execution_opt_in_before_effects(tmp_path,monkeypatch):
    m=module()
    def forbidden(*a,**k):raise AssertionError('effect_before_opt_in')
    monkeypatch.setattr(m,'check_busy',forbidden)
    with pytest.raises(SystemExit) as exc:
        m.main(['--model',str(tmp_path/'missing'),'--template',str(tmp_path/'missing'),
                '--out',str(tmp_path/'must-not-exist')])
    assert exc.value.code==2
    assert not (tmp_path/'must-not-exist').exists()


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS xattr ABI')
def test_quarantine_refusal_on_plain_data_without_execution(tmp_path, monkeypatch):
    import ctypes
    from types import SimpleNamespace
    m=module();path=tmp_path/'plain-data';path.write_text('never executable')
    # Exercise the actual macOS read ABI on clean data. V1 forbids writing real
    # quarantine even to data; inject only the quarantined syscall response.
    m.quarantine_clear(path)
    class Present:
        def __call__(self, name, attribute, buffer, size, position, options):
            assert name == str(path.resolve()).encode()
            assert attribute == b'com.apple.quarantine'
            assert buffer is None and (size, position, options) == (0, 0, 0)
            return 1
    with monkeypatch.context() as patch:
        patch.setattr(ctypes, 'CDLL', lambda *a, **k: SimpleNamespace(getxattr=Present()))
        with pytest.raises(RuntimeError,match='quarantined_path_refused'):m.quarantine_clear(path)
    with pytest.raises(FileNotFoundError):m.quarantine_clear(tmp_path/'absent')
    m.quarantine_clear(path)  # No real attribute was written or cleared.


def test_native_fixture_inspection_refuses_without_launch_or_quarantine_write(tmp_path, monkeypatch):
    from scripts import tokenpath_ab_bench as bench
    from test_token_two_process_join import _safe
    import subprocess
    path=tmp_path/'plain-data';path.write_text('never executable')
    def forbidden(*a, **k):raise AssertionError('inspector must not spawn')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    _safe(path)
    seen=[]
    def reject(candidate):
        seen.append(candidate)
        raise RuntimeError('quarantined_path_refused')
    monkeypatch.setattr(bench, 'quarantine_clear', reject)
    with pytest.raises(RuntimeError, match='quarantined_path_refused'):_safe(path)
    assert seen == [path]


def test_chat_admission_fixture_is_explicit_and_graph_bound():
    from tests.request_gateway.test_core import _synthetic_qualification
    from tests.a13_macos.token_bench_chat import fixture_qualification
    from tests.live.test_token_path_admission import world,real_prefill_node
    from mycelium_request_gateway.contracts import qualification_binding
    from mycelium_request_gateway.backend import RouterSessionBackend
    from dataclasses import replace
    graph=real_prefill_node(getattr(world,'__wrapped__')()).graph
    assert graph is not None
    graph=replace(graph,stages=tuple(replace(s,placements=tuple(replace(p,
        stage_signature='sha256:'+'a'*64,load_proof_digest='sha256:'+str(i+1)*64)
        for p in s.placements)) for i,s in enumerate(graph.stages)))
    q=fixture_qualification(graph)
    assert q.qualification_id.startswith('synthetic-test-fixture:')
    assert 'NOT PHYSICAL' in q.claim_boundary
    assert qualification_binding(q).model_id==graph.model_id
    assert RouterSessionBackend._qualified_placement_projection(q,graph)
    assert _synthetic_qualification().qualification_id!=q.qualification_id


@pytest.mark.parametrize('failed',[False,True])
def test_finish_retains_native_receipt_ack_and_failed_prefix(tmp_path,monkeypatch,failed):
    import sqlite3
    m=module();monkeypatch.setattr(m,'check_busy',lambda:None)
    b=object.__new__(m.Bench)
    b.start,b.end,b.mode,b.entry,b.tail=10.,13.,'on','entry','tail'
    b.arrivals=[(11.,7),(12.,8)][:1 if failed else 2]
    b.result={'token_ids':[t for _,t in b.arrivals],'successful':not failed,'request_cleanup_verified':True,
              'terminal':'failed' if failed else 'completed'}
    b.commands=[dict(command='infer_start',grant=True,started=10.1)]
    b.receipts=[dict(at=11.,receipt={'statement':{'grant_digest':'g','sequence':0}})]
    b.http=[dict(path='/token-progress/v1',started=10.7)]
    evidence=dict(materialization='per_stage',work_unit='transformer_stage')
    final={'entry':dict(active=0,runtime=dict(evidence),command_audit=b.commands+[
        dict(command='ack_token_progress',started=11.1)],
        progress_audit=[dict(grant_digest='g',sequence=0,started=10.5)]),
        'tail':dict(active=0,runtime=dict(evidence),command_audit=[],executions=[
            dict(request_id='measured',token_id=t,finished=at-.1) for at,t in [(11.,7),(12.,8)]])}
    db=tmp_path/'mailbox.sqlite3'
    with sqlite3.connect(db) as c:c.execute('CREATE TABLE managed_commands (envelope_json TEXT)')
    b.finish(final=final,events=[],database=db)
    assert b.result['native_token_ids']==[7,8]
    assert b.result['native_command_counts']['ack_token_progress']==1
    assert b.result['command_counts']['infer_decode']==0
    assert b.result['receipt_lag_p50_s']==.5
    assert b.result['cleanup_proven'] is True
    assert b.result['successful'] is (not failed)
    assert all(v['runtime']==evidence for v in b.result['final_native'].values())


def test_all_cleanup_actions_run_despite_a_finalizer_error():
    from test_token_two_process_join import close_all
    calls=[]
    def broken():calls.append('broken');raise RuntimeError('close failed')
    with pytest.raises(BaseExceptionGroup):
        close_all([broken,lambda:calls.append('worker'),lambda:calls.append('tail')])
    assert calls==['broken','worker','tail']


def test_gateway_uses_advancing_unix_lease_clock(tmp_path,monkeypatch):
    from tests.a13_macos import token_bench_chat as chat
    from tests.live.test_token_path_admission import world,real_prefill_node
    from types import SimpleNamespace
    import mycelium_m16_runtime
    graph=real_prefill_node(getattr(world,'__wrapped__')()).graph
    assert graph is not None
    monkeypatch.setattr(chat,'fixture_qualification',lambda _:object())
    now=[1000.]
    monkeypatch.setattr(chat.time,'time',lambda:now[0])
    def capture(**kwargs):
        clock=kwargs['lease_clock']
        assert clock.now()==1000.
        now[0]=1030.
        assert clock.now()==1030.
        raise RuntimeError('captured_before_effects')
    monkeypatch.setattr(mycelium_m16_runtime,'M16RuntimeCoordinator',capture)
    with pytest.raises(RuntimeError,match='captured_before_effects'):
        chat.gateway(route=SimpleNamespace(set_deployment_qualification=lambda *a,**k:None),
            graph=graph,codec=None,tmp_path=tmp_path,cleanup=[],finalizers=[])


def test_native_serving_constructor_explicitly_selects_per_stage(tmp_path, monkeypatch):
    """Exercise the child factory with inert loader/runtime dependencies; no MLX."""
    from types import ModuleType, SimpleNamespace
    m = module()
    monkeypatch.setattr(m, 'check_busy', lambda: None)
    calls = []
    loaded = object()
    loader = ModuleType('runtime_loader')
    def load(assignment, report, *, load_generation):
        calls.append((assignment, report, load_generation))
        return loaded
    setattr(loader, 'load_assignment_stage', load)
    runtime_module = ModuleType('mycelium_router.mlx_runtime')
    class CaptureRuntime:
        def __init__(self, *args, **kwargs):
            self.args, self.options = args, kwargs
    setattr(runtime_module, 'MLXRuntimePort', CaptureRuntime)
    monkeypatch.setitem(sys.modules, 'runtime_loader', loader)
    monkeypatch.setitem(sys.modules, 'mycelium_router.mlx_runtime', runtime_module)
    assignment, report = tmp_path/'assignment.json', tmp_path/'report.json'
    assignment.write_text('{"fixture": "assignment"}')
    report.write_text('{"fixture": "report"}')
    placement = SimpleNamespace(node_id='entry', placement_id='entry-placement')
    node = SimpleNamespace(node_id='entry', _clock=SimpleNamespace(now=lambda: 100.),
        graph=SimpleNamespace(stages=[SimpleNamespace(placements=[placement])]))
    runtime = m.load_native_runtime(node, {'assignment': str(assignment), 'report': str(report)})
    assert calls == [({'fixture': 'assignment'}, {'fixture': 'report'}, 17)]
    assert runtime.args == ('entry', node.graph, {'entry-placement': loaded})
    assert runtime.options['decode_mode'] == 'stage_local_kv'
    assert runtime.options.get('materialization') == 'per_stage'


@pytest.mark.parametrize('evidence', [
    {},
    {'materialization': 'per_stage'},
    {'materialization': 'per_stage', 'work_unit': 'transformer_layer'},
    {'materialization': 'per_layer', 'work_unit': 'transformer_layer'},
])
def test_benchmark_refuses_missing_or_contradictory_serving_evidence(tmp_path, monkeypatch, evidence):
    import sqlite3
    m = module()
    b = object.__new__(m.Bench)
    b.start, b.end, b.mode, b.entry, b.tail = 10., 13., 'on', 'entry', 'tail'
    b.arrivals = [(11., 7), (12., 8)]
    b.result = {'token_ids': [7, 8], 'successful': True, 'request_cleanup_verified': True}
    b.commands = [dict(command='infer_start', grant=True, started=10.1)]
    b.receipts, b.http = [], []
    final = {n: dict(active=0, command_audit=b.commands if n=='entry' else [],
                    progress_audit=[], runtime=dict(evidence), executions=[
                        dict(request_id='measured', token_id=t, finished=at-.1)
                        for at,t in b.arrivals]) for n in ('entry', 'tail')}
    db = tmp_path/'mailbox.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE managed_commands (envelope_json TEXT)')
    with pytest.raises(ValueError, match='serving_materialization_evidence_mismatch'):
        b.finish(final=final, events=[], database=db)
    assert not b.result.get('cleanup_proven')


def test_measured_cleanup_cannot_borrow_warmup_or_zero_kv():
    import hashlib,json
    m=module()
    expected=dict(request_id='measured',path_id='p',cancellation_generation=0)
    def receipt(request_id):
        body=dict(request_id=request_id,path_id='p',cancellation_generation=1,
                  node_ids=['e','t'])
        body['receipt_digest']='sha256:'+hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return body
    warm=receipt('warmup');good=receipt('measured')
    with pytest.raises(ValueError):m.measured_cleanup([warm],expected,{'e','t'})
    assert m.measured_cleanup([warm,good],expected,{'e','t'})==good
    with pytest.raises(ValueError):m.measured_cleanup([good],expected,{'e','other'})
    with pytest.raises(ValueError):m.measured_cleanup([{**good,'path_id':'changed'}],expected,{'e','t'})
