# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only production start admission; fake compute and trusted provisioning.

This exercises the shipped infer_start dispatcher, not owner/chat negotiation or
remote authorization. No public activation exists until those boundaries land.
"""
from copy import deepcopy
import importlib
import threading
from types import SimpleNamespace

import pytest

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_node import PhysicalNodeService, _CaptureSink
from tests.live.test_entry_token_path import CONTROL


class DeskWorld(SimpleNamespace):
    def __repr__(self):
        return '<private desk authority>'


def api():
    return importlib.import_module('mycelium_node.token_admission')


@pytest.fixture
def world():
    owner = generate_ed25519_signer(endpoint_id='owner-endpoint')
    entry = generate_ed25519_signer(endpoint_id='entry-endpoint')
    request = dict(request_id='request', prompt_token_ids=[10, 11], max_new_tokens=6,
                   expected_new_tokens=6, qos_class='interactive', admitted_at=0.,
                   target_ttft_ms=1000., target_tpot_ms=100., target_tokens_per_second=10.,
                   sampling_seed=0, generation_config_digest='sha256:' + 'e' * 64)
    manifest = {'request_id': 'request', 'path_id': 'path'}
    control = {**CONTROL, 'path_attempt': 0,
               'path_digest': sha256_bytes(canonical_json_bytes(manifest))}
    pins = dict(run_id='desk', requester_id='requester',
                owner_endpoint_id=owner.endpoint_id, owner_key_digest=owner.verification_key_digest,
                entry_node_id='entry', entry_endpoint_id=entry.endpoint_id,
                entry_key_digest=entry.verification_key_digest,
                participants=[{'node_id': 'entry', 'endpoint_id': entry.endpoint_id, 'generation': 1},
                              {'node_id': 'tail', 'endpoint_id': 'tail-endpoint', 'generation': 2}],
                model_digest='sha256:' + 'c' * 64)
    scope = {**deepcopy(pins), 'request_id': 'request', 'control': deepcopy(control),
             'request_digest': sha256_bytes(canonical_json_bytes(request))}
    body = dict(protocol='mycelium.token_path_grant.v1', scope=scope,
                max_new_tokens=6, stop_token_ids=[99], expires_at_unix_ms=105000)
    grant = {'statement': body, 'signature': owner.sign(body)}
    return DeskWorld(owner=owner, entry=entry, request=request, manifest=manifest,
                           control=control, pins=pins, grant=grant, now=10., prefill=0,
                           rounds=0, receipts=[], trace=[], cancelled=False)


def policy(w):
    return api().EntryAdmissionPolicy(
        scope_pins=w.pins, owner_key=w.owner.public_key_record(),
        max_new_tokens=6, stop_token_ids=(99,),
        process_deadline_monotonic_s=1800., lease_deadline_monotonic_s=100.,
        clock_uncertainty_ms=100, clock=lambda: w.now, unix_ms=lambda: 100000)


def node(w, *, enabled=True):
    n = object.__new__(PhysicalNodeService)
    n.run_id, n.node_id = 'desk', 'entry'
    n.signer = w.entry
    n._identity = lambda: {'run_id': 'desk', 'node_id': 'entry'}
    n._control_lock = threading.RLock()
    n._request_controls = {'request': deepcopy(w.control)}
    n._sinks = {}
    n._request_cleanup_receipts = {}
    n._token_path_admission = policy(w) if enabled else None
    n._token_path_publish = lambda receipt, **kw: w.receipts.append(receipt)
    n._idle_gate = lambda: None
    n.state = 'RUNNING'
    n.transport = None
    n.command_timeout = 2
    n.router = SimpleNamespace(request_status=lambda _: 'DECODING',
                               decode_one_distributed=lambda _: decode())

    def prefill(payload):
        w.prefill += 1
        w.trace.extend([(0, 'entry'), (0, 'tail')])
        n._sinks['request'] = _CaptureSink()
        n._sinks['request'].emit(0, 1)
        return {'observation': {'details': {'status': 'DECODING'}}}

    def decode():
        w.rounds += 1
        w.trace.extend([(w.rounds, 'entry'), (w.rounds, 'tail')])
        n._sinks['request'].emit(w.rounds, w.rounds + 1)
        return True

    # Expensive compute/path provisioning remains fake, admission does not.
    n._infer_start_legacy = prefill
    return n


def payload(w):
    return dict(request=deepcopy(w.request), control=deepcopy(w.control),
                path_manifest=deepcopy(w.manifest), token_grant=deepcopy(w.grant))


def test_grant_bearing_native_start_verifies_before_prefill_and_drives_same_decode(world):
    n = node(world)
    result = n._infer_start(payload(world))
    assert world.prefill == 1 and world.rounds == 5
    assert result['observation']['details']['status'] == 'COMPLETED'
    authority = importlib.import_module('mycelium_router.token_path_authority')
    grant = policy(world).admit(world.grant, request=world.request, bound_control=world.control)
    cursor = authority.ReceiptCursor(grant=grant, entry_key=world.entry.public_key_record(),
                                     clock=lambda: world.now)
    assert tuple(t for r in world.receipts for t in cursor.accept(r)) == (1, 2, 3, 4, 5, 6)
    assert cursor.terminal == 'COMPLETED'


def test_native_grant_is_default_off_before_any_compute(world):
    n = node(world, enabled=False)
    with pytest.raises(RuntimeError, match='token_path_disabled'):
        n._infer_start(payload(world))
    assert world.prefill == world.rounds == 0 and not world.receipts


@pytest.mark.parametrize('mutation', ['request', 'control', 'manifest', 'signature',
                                      'budget', 'unknown', 'entry', 'unbound', 'channel'])
def test_native_refusals_never_prefill(world, mutation):
    n = node(world)
    p = payload(world)
    if mutation == 'request':
        p['request']['prompt_token_ids'].append(12)
    elif mutation == 'control':
        p['control']['publisher_generation'] += 1
    elif mutation == 'manifest':
        p['path_manifest']['path_id'] = 'foreign'
    elif mutation == 'signature':
        impostor = generate_ed25519_signer(endpoint_id=world.owner.endpoint_id)
        p['token_grant']['signature'] = impostor.sign(p['token_grant']['statement'])
    elif mutation == 'budget':
        p['token_grant']['statement']['max_new_tokens'] = 5
        p['token_grant']['signature'] = world.owner.sign(p['token_grant']['statement'])
    elif mutation == 'unknown':
        p['surprise'] = 1
    elif mutation == 'entry':
        n.node_id = 'tail'
        n._identity = lambda: {'run_id': 'desk', 'node_id': 'tail'}
    elif mutation == 'unbound':
        n._request_controls.clear()
    else:
        n._token_path_publish = None
    with pytest.raises((RuntimeError, ValueError)):
        n._infer_start(p)
    assert world.prefill == world.rounds == 0 and not world.receipts


def test_start_replay_cannot_repeat_prefill(world):
    n = node(world)
    n._infer_start(payload(world))
    with pytest.raises((RuntimeError, ValueError), match='token_path_grant_replay'):
        n._infer_start(payload(world))
    assert world.prefill == 1


def test_bound_control_cancel_before_start_is_not_grant_authority(world):
    n = node(world)
    n._request_controls['request']['cancellation_generation'] = 1
    with pytest.raises((RuntimeError, ValueError)):
        n._infer_start(payload(world))
    assert world.prefill == world.rounds == 0


def test_policy_snapshots_pins_and_clamps_expiry_once(world):
    p = policy(world)
    world.pins['participants'][1]['generation'] = 9
    g = p.admit(world.grant, request=world.request, bound_control=world.control)
    assert g.statement()['scope']['participants'][1]['generation'] == 2
    assert g.deadline_monotonic_s == pytest.approx(14.9)


def test_expired_policy_cannot_prefill(world):
    n = node(world)
    world.now = 1801.
    with pytest.raises((RuntimeError, ValueError), match='token_path_grant_expired'):
        n._infer_start(payload(world))
    assert world.prefill == 0


def real_prefill_node(w):
    """Shipped dispatch/manifest/control/prefill wait, fake two-stage router."""
    from dataclasses import replace
    from test_router_contracts import graph_fixture, placement, stage
    from mycelium_router.contracts import PathHop, PathManifest, PlacementEdge
    from mycelium_router.serialization import path_manifest_to_dict
    first, last = placement(0, 'entry'), placement(1, 'tail')
    graph = replace(graph_fixture(), deployment_id='deployment', topology_version=1,
        manifest_digest=w.pins['model_digest'], final_stage_id='stage-001',
        stages=(stage(0, 0, 4, (first,)), stage(1, 4, 8, (last,))),
        edges=(PlacementEdge('forward', first.placement_id, last.placement_id, 'entry-tail'),),
        loopback_edges=(PlacementEdge('return', last.placement_id, first.placement_id, 'tail-entry'),))
    manifest = PathManifest(path_id='path', path_attempt=0, request_id='request',
        deployment_id=graph.deployment_id, deployment_epoch=1, topology_version=1,
        manifest_digest=graph.manifest_digest,
        ordered_hops=(PathHop('stage-000', first.placement_id, 'reserve-entry', 1000., 1),
                      PathHop('stage-001', last.placement_id, 'reserve-tail', 1000., 1)),
        loopback_edge_id='return')
    w.manifest = path_manifest_to_dict(manifest)
    w.control['path_digest'] = sha256_bytes(canonical_json_bytes(w.manifest))
    w.grant['statement']['scope']['control'] = deepcopy(w.control)
    w.grant['signature'] = w.owner.sign(w.grant['statement'])
    n = node(w)
    del n._infer_start_legacy
    n.deployment_id = 'deployment'
    n.graph = graph
    n._clock = SimpleNamespace(now=lambda: w.now)
    n.runtime = SimpleNamespace(decode_mode='stage_local_kv')
    n._pending_cancellations = {}
    n._cancellation_controls = {}
    def prefill(request, sink, **kwargs):
        assert kwargs['manifest'] == manifest and kwargs['pinned_deployment'] is graph
        w.prefill += 1
        w.trace.extend([(0, 'entry'), (0, 'tail')])
        sink.emit(0, 1)
        return request.request_id
    n.router.start_locked_distributed_prefill = prefill
    n.router.get_request = lambda _: SimpleNamespace(manifest=manifest)
    n._request_controls.clear()
    n._bind_request_control({'request_id': 'request', 'control': deepcopy(w.control)})
    return n


def test_production_dispatch_validates_manifest_and_prefills_after_admission(world):
    n = real_prefill_node(world)
    response = n.dispatch(dict(protocol='mycelium.physical_node_control.v1',
        run_id='desk', deployment_id='deployment', command_id='start-one',
        command='infer_start', payload=payload(world)))
    assert response['observation']['details']['status'] == 'COMPLETED'
    assert world.prefill == 1 and world.rounds == 5
    assert len(world.trace) == 12


def test_real_child_dispatch_admits_grant_and_streams_before_start_response(world, tmp_path):
    import json
    import sys
    import time
    from pathlib import Path
    from cryptography.hazmat.primitives import serialization
    from mycelium_node.process import PhysicalNodeProcess
    from mycelium_router.token_path_authority import ReceiptCursor
    real_prefill_node(world)
    world.grant['statement']['expires_at_unix_ms'] = int(time.time() * 1000) + 20000
    world.grant['signature'] = world.owner.sign(world.grant['statement'])
    owner_policy = api().EntryAdmissionPolicy(scope_pins=world.pins,
        owner_key=world.owner.public_key_record(), max_new_tokens=6, stop_token_ids=(99,),
        process_deadline_monotonic_s=time.monotonic()+1800,
        lease_deadline_monotonic_s=time.monotonic()+20, clock_uncertainty_ms=100)
    grant = owner_policy.admit(world.grant, request=world.request, bound_control=world.control)
    cursor = ReceiptCursor(grant=grant, entry_key=world.entry.public_key_record())
    fixture = tmp_path / 'private-fixture.json'
    def secret(signer):
        return signer._private_key.private_bytes(serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw, serialization.NoEncryption()).hex()
    fixture.write_text(json.dumps(dict(request=world.request, manifest=world.manifest,
        control=world.control, pins=world.pins, grant=world.grant,
        owner_secret=secret(world.owner), entry_secret=secret(world.entry))))
    fixture.chmod(0o600)
    child = tmp_path / 'child.py'
    child.write_text('''import json, sys, threading, time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_node.token_admission import EntryAdmissionPolicy
from tests.live.test_token_path_admission import DeskWorld, real_prefill_node
from physical_inference_node import _ResponseEmitter, _response, NodeCommandError
with open(sys.argv[1]) as f: d=json.load(f)
def signer(name):
    key=Ed25519PrivateKey.from_private_bytes(bytes.fromhex(d.pop(name+'_secret')))
    return Ed25519EvidenceSigner(endpoint_id=name+'-endpoint', _private_key=key,
        _public_key_bytes=key.public_key().public_bytes_raw())
w=DeskWorld(owner=signer('owner'), entry=signer('entry'), **d,
    now=10., prefill=0, rounds=0, receipts=[], trace=[], cancelled=False)
n=real_prefill_node(w)
n._token_path_admission=EntryAdmissionPolicy(scope_pins=w.pins,
    owner_key=w.owner.public_key_record(), max_new_tokens=6, stop_token_ids=(99,),
    process_deadline_monotonic_s=time.monotonic()+1800,
    lease_deadline_monotonic_s=time.monotonic()+20, clock_uncertainty_ms=100)
emitter=_ResponseEmitter()
release=threading.Event()
def execute(command):
    cid=command['command_id']
    try:
        if command['command']=='infer_start':
            n._token_path_publish=lambda receipt, **kwargs: emitter.emit_progress(
                command_id=cid, node_id='entry', receipt=receipt, **kwargs)
            result=n.dispatch(command)
            release.wait(5)
        else:
            release.set()
            result={'test_barrier_released':True}
        emitter.emit(_response(n, command_id=cid, ok=True, result=result))
    except NodeCommandError as exc:
        emitter.emit(_response(n, command_id=cid, ok=False, error_code=exc.code))
for raw in sys.stdin:
    command=json.loads(raw)
    if command['command']=='stop':
        execute(command)
        break
    threading.Thread(target=execute,args=(command,)).start()
''')
    bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name='__main__')"
    root = str(Path(__file__).resolve().parents[2])
    process = PhysicalNodeProcess(command=(sys.executable, '-c', bootstrap, root, str(child), str(fixture)),
        node_id='entry', run_id='desk', deployment_id='deployment', response_timeout_seconds=7)
    results, errors = [], []
    thread = None
    try:
        inbox = process.open_progress(command_id='start-one', cursor=cursor,
                                      deadline_monotonic_s=time.monotonic()+5)
        def start():
            try:
                results.append(process.command('infer_start', payload(world), command_id='start-one'))
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=start)
        thread.start()
        assert inbox.read().token_ids == (1,)
        terminal = inbox.read()
        assert terminal.token_ids == (2, 3, 4, 5, 6) and terminal.status == 'COMPLETED'
        assert thread.is_alive() and results == []
        process.command('health', {}, command_id='release-test-barrier')
        thread.join(2)
        assert not thread.is_alive() and not errors
        assert results[0]['observation']['details']['status'] == 'COMPLETED'
        process.close_progress('start-one')
    finally:
        process.close()
        if thread is not None:
            thread.join(2)


def test_final_response_is_bounded_and_does_not_republish_tokens(world):
    result = node(world)._infer_start(payload(world))
    details = result['observation']['details']
    assert set(details) == {'request_id', 'status', 'token_grant_digest', 'token_count'}
    assert details['token_count'] == 6


def test_slow_wall_clock_sampling_cannot_extend_admission_deadline(world):
    p = policy(world)
    def delayed_wall():
        world.now += 10.
        return 100000
    p._unix_ms = delayed_wall
    n = node(world)
    n._token_path_admission = p
    with pytest.raises(RuntimeError, match='token_path_grant_expired'):
        n._infer_start(payload(world))
    assert world.prefill == 0


def test_legacy_start_does_not_invoke_grant_policy(world):
    n = node(world, enabled=False)
    p = payload(world)
    del p['token_grant']
    n._infer_start(p)
    assert world.prefill == 1 and not world.receipts
