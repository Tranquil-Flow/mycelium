# SPDX-License-Identifier: AGPL-3.0-only
"""Numbers-only bounded aggregates; clocks are simulated, not slept."""
from types import SimpleNamespace


def test_rolling_quantiles_counts_and_expiry():
    from mycelium_a13_macos.swarm_stats import PerformanceWindow
    clock = [100.0]
    window = PerformanceWindow(clock=lambda: clock[0])
    for i in range(60):
        window.record(ttft_ms=1000+i, decode_millitokens_s=2000+i, request_ms=3000+i)
    snapshot = window.snapshot()
    assert snapshot['ttft_ms'] == {'p50': 1034, 'p90': 1054, 'n': 50}
    assert snapshot['requests_24h'] == 60
    clock[0] += 3600
    assert window.snapshot()['ttft_ms'] == {'p50': None, 'p90': None, 'n': 0}
    assert window.snapshot()['requests_24h'] == 60
    clock[0] += 86400
    assert window.snapshot()['requests_24h'] == 0


def test_new_run_drops_old_percentiles_but_preserves_24h_count():
    from mycelium_a13_macos.swarm_stats import PerformanceWindow
    clock = [0.0]
    window = PerformanceWindow(clock=lambda:clock[0])
    window.select_context('run-one')
    window.record(ttft_ms=1, decode_millitokens_s=2, request_ms=3, context='run-one')
    clock[0] = 100
    window.select_context('run-two')
    assert window.snapshot()['ttft_ms']['n'] == 0
    assert window.snapshot()['requests_24h'] == 1
    window.record(ttft_ms=1, decode_millitokens_s=2, request_ms=3, context='run-one')
    assert window.snapshot()['ttft_ms']['n'] == 0  # late completion is not the current run
    assert window.snapshot()['requests_24h'] == 2


def test_chat_records_successful_token_timing_without_content(tmp_path):
    from mycelium_a13_macos.chat import ChatService
    clock = [1.0]
    class Client:
        def stream(self, prompt, count, token):
            clock[0] = 3.0; token('private first')
            clock[0] = 3.5; token('private second')
            clock[0] = 4.0; token('private third')
            clock[0] = 5.0
            return {'terminal': 'completed'}
    chat = ChatService(SimpleNamespace(), client_factory=lambda _: Client(), clock=lambda: clock[0])
    record = {'id':'test', 'tokens':[], 'started':1.0, 'cancel_reason':None, 'max_new_tokens':3,
              'terminal':None, 'error':None}
    chat._work(record, 'unused', 'private prompt')
    snapshot = chat.performance.snapshot()
    assert snapshot['ttft_ms'] == {'p50':2000, 'p90':2000, 'n':1}
    assert snapshot['decode_millitokens_s']['p50'] == 2000
    assert snapshot['request_ms']['p50'] == 4000
    assert 'private' not in repr(snapshot)
    record = dict(record, id='cancel', started=5.0, cancel_reason='cancelled', tokens=[])
    class Failed:
        def stream(self, *args): return {'terminal':'failed'}
    chat._client_factory = lambda _: Failed()
    chat._work(record, 'unused', 'secret')
    assert chat.performance.snapshot()['requests_24h'] == 1


def test_mode_comes_from_constructed_route_not_environment(monkeypatch):
    from tests.live.test_peer_counter_projection_single_observation import _route
    route = _route(monkeypatch)
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH', '1')
    route._token_path_owner = None
    assert route.public_status()['token_path_enabled'] is False
    route._token_path_owner = SimpleNamespace(enabled=True)
    assert route.public_status()['token_path_enabled'] is True


def test_stage_durations_are_decode_only_deduplicated_and_not_hops():
    from mycelium_a13_macos.swarm_stats import stage_projection
    run = {'participants':[{'layers':[0,23]}, {'layers':[23,24]}]}
    interval = {'stage_index':0, 'hop_kind':'compute', 'phase':'decode', 'runtime_batch_ref':'',
                'started_at':10.0, 'finished_at':10.025, 'interval_ref':'one'}
    peer = {'placements':[{'start_layer':0,'end_layer_exclusive':23}], 'a10_publication':{
        'clock':{'wall_anchor':1800000000, 'monotonic_anchor':10.0},
        'intervals':[interval, interval.copy(), dict(interval, phase='prefill', interval_ref='two')]}}
    projected = stage_projection(run, {'peers':[peer]}, now=1800000001)
    assert projected[0]['compute_ms'] == {'p50':25, 'p90':25, 'n':1}
    assert projected[1]['compute_ms'] == {'p50':None, 'p90':None, 'n':0}
    assert stage_projection(run, {'peers':[peer]}, now=1800003601)[0]['compute_ms']['n'] == 0


def test_app_snapshot_actual_mode_uptime_and_reader_failure(tmp_path):
    from mycelium_a13_macos.swarm_stats import AppStats
    from mycelium_swarm_directory.stats_docs import check_values
    clock = [1800000000]
    run = {'run_id':'one','participants':[{'layers':[0,24]}], 'registry_origin':'http://127.0.0.1:1234'}
    app = SimpleNamespace(owner_run=SimpleNamespace(status=lambda:run))
    runtime = {'simulated':False,'route_alive':True,'token_path_enabled':True,'peers':[]}
    stats = AppStats(clock=lambda:clock[0], reader=lambda *_:runtime)
    one = stats.snapshot(app, 'serving')
    check_values(one)
    assert one['mode'] == 'fast' and one['uptime_seconds'] == 0
    clock[0] += 30
    assert stats.snapshot(app, 'serving')['uptime_seconds'] == 30
    assert stats.snapshot(app, 'offline')['serving_since'] is None
    run['run_id'] = 'two'
    runtime['token_path_enabled'] = False
    assert stats.snapshot(app, 'serving')['mode'] == 'normal'
    def fail(*_): raise OSError('unreachable')
    stats.reader = fail
    assert stats.snapshot(app, 'serving')['mode'] == 'unknown'


def test_adjacent_activation_rtt_precedes_control_and_excludes_other_nodes():
    from mycelium_live.swarm_metrics import project_hops
    from tests.topology.test_m14_topology_evidence import observation
    graph = SimpleNamespace(stages=[SimpleNamespace(placements=[SimpleNamespace(node_id=n)]) for n in ('node-a','node-b')])
    raw = observation('node-a','node-b',12.5)
    snapshots = {'node-a':{'details':{'transport':{'transport_path_observations':[raw]}}}}
    assert project_hops(graph,snapshots,None,now_unix_ms=2000) == [
        {'from_stage':0,'to_stage':1,'kind':'activation_rtt','rtt_ms':12,'samples':4,'age_seconds':1}]
    assert project_hops(graph,snapshots,None,now_unix_ms=11001) == []
    raw['remote_node_id'] = 'node-c'
    assert project_hops(graph,snapshots,None,now_unix_ms=2000) == []


def test_verified_idle_probe_produces_nonempty_hops_without_new_traffic(monkeypatch):
    from tests.live.test_peer_counter_projection_single_observation import _route
    from mycelium_a13_macos.swarm_stats import AppStats
    from mycelium_swarm_directory.stats_docs import check_values
    from mycelium_live import route as module
    route = _route(monkeypatch)
    stage = route._graph.stages[0]
    second = SimpleNamespace(stage_id='stage-001', layer_range=SimpleNamespace(start_layer=2,end_layer_exclusive=3),
        component_roles=(), placements=(SimpleNamespace(placement_id='p-b',node_id='node-b',runtime_backend='numpy'),))
    route._graph.stages = (stage,second)
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda:clock[0])
    sent = []
    def send(*args, **kwargs):
        sent.append(kwargs['command']); clock[0] += .125; return {}
    route._send_before = send
    route._command_id = lambda *_:'probe'
    route._verify_observation = lambda *args, **kwargs: {'details':{}}
    assert route._idle_probe_peer('node-b') is True
    runtime = route.public_status()
    assert runtime['swarm_hops'] == [{'from_stage':None,'to_stage':1,'kind':'owner_control_rtt_probe',
                                     'rtt_ms':125,'samples':1,'age_seconds':0}]
    run = {'run_id':'one','registry_origin':'http://127.0.0.1:1234','participants':[
        {'node_id':'node-a','layers':[0,2],'label':'This Mac (owner)'},
        {'node_id':'node-b','layers':[2,3],'label':'Joined Mac'}]}
    app = SimpleNamespace(owner_run=SimpleNamespace(status=lambda:run))
    stats = AppStats(clock=lambda:1800000000, reader=lambda *_:runtime).snapshot(app,'serving')
    assert stats['hops'] == runtime['swarm_hops']
    check_values(stats)
    assert sent == ['health']  # publication itself never sends a probe
    def refuse(*args, **kwargs): raise ValueError('bad signature')
    route._verify_observation = refuse
    assert route._idle_probe_peer('node-b') is False
    assert route.public_status()['swarm_hops'][0]['samples'] == 1
    clock[0] += 91
    assert route.public_status()['swarm_hops'] == []


def test_new_publisher_preserves_heartbeat_when_stats_refused(tmp_path):
    from mycelium_a13_macos.swarm_publish import SwarmPublisher, SwarmPublishError
    from mycelium_swarm_directory import docs, stats_docs
    from mycelium_swarm_directory.crypto import verify
    calls = []
    class Client:
        def __init__(self, origin): pass
        def post(self, path, document):
            calls.append((path, document))
            if path == '/stats':
                stats_docs.check(document, verify)
                raise SwarmPublishError('swarm_directory_route_unknown')
            if path == '/heartbeat': docs.check(document, docs.HEARTBEAT, verify)
            return {}
    app = SimpleNamespace(root=tmp_path, owner_run=SimpleNamespace(status=lambda: {
        'state':'serving','route_ready':True,'registry_origin':None,'participants':[], 'run_id':'one'}),
        run_model_contract=lambda: SimpleNamespace(model_id='Qwen/Qwen3-0.6B'))
    pub = SwarmPublisher(app, origin=lambda:'https://entry.example.org', client_factory=Client,
                         chat=lambda:None, clock=lambda:1800000000)
    try:
        result = pub.publish({'directory':'https://example.org','name':'Test','access_mode':'request'})
        assert result['enabled'] and result['error_code'] is None
        assert [p for p,d in calls] == ['/records','/heartbeat','/stats']
        pub.heartbeat_now()
        assert pub.status()['error_code'] is None
    finally: pub.close()
