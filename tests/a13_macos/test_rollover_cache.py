# SPDX-License-Identifier: AGPL-3.0-only
"""Rollover cache: real tiny compiler and fresh run bindings, no devices/network."""
import copy
import json
import struct
import time
from types import SimpleNamespace as NS

import pytest

from scripts import build_qwen_live_route as builder
from test_managed_builder_entrypoint import (_template, _managed_node, _topology_path,
    _write_observations, _observations_document, _build_args, _fixed_clock)
from _nhost_fixtures import tiny_dense_snapshot, COMMIT


def fixture(tmp_path, monkeypatch):
    snapshot = tiny_dense_snapshot(tmp_path / 'source', model_type='qwen3', num_layers=2)
    weights = snapshot / 'model.safetensors'
    from _nhost_fixtures import write_safetensors, _layer_tensors
    tensors = [('model.embed_tokens.weight', [32, 8]), ('model.norm.weight', [8])]
    for i in range(2):
        tensors.extend(_layer_tensors(i, 8, 16))
        tensors.extend((f'model.layers.{i}.self_attn.{n}_norm.weight', [8]) for n in ('q', 'k'))
    write_safetensors(weights, tensors)
    raw = weights.read_bytes()
    start = 8 + struct.unpack('<Q', raw[:8])[0]
    weights.write_bytes(raw[:start] + bytes(len(raw) - start))  # finite deterministic synthetic tensors
    (tmp_path / 'template.json').write_text(json.dumps(_template()))
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])
    observations = _write_observations(tmp_path / 'observations.json', _observations_document())
    args = _build_args(tmp_path, topology, observations, managed_transport=True,
                       model_root=snapshot, model_id='test-org/tiny-dense', resolved_commit=COMMIT,
                       managed_layer_ranges=[[0, 1], [1, 2]])
    _fixed_clock(monkeypatch)
    monkeypatch.setattr(builder.subprocess, 'run', lambda *a, **k: pytest.fail('no subprocess permitted'))
    # Only tokenizer / runtime source closure substituted; assignments, packs,
    # loader proofs, tiny numeric challenge and graph compilation are real.
    monkeypatch.setattr(builder, '_deployment_prompt_codec', lambda *a: NS(
        encode=lambda prompt: (1, 2), decode_token=lambda token: 'x'))
    monkeypatch.setattr(builder, '_copy_runtime_closure', lambda repo, bundle, template:
        (bundle / 'physical_inference_node.py').write_text('# inert fixture\n'))
    args.compilation_cache = {}
    args.compilation_cache_key = 'fixture-binding'
    return args


@pytest.mark.parametrize('slice_mode', [False, True])
def test_cached_compile_rebinds_every_run_without_loading_model_again(tmp_path, monkeypatch, slice_mode):
    args = fixture(tmp_path, monkeypatch)
    if slice_mode:
        args.stage_sharded = True
        args.emit_stage_shards = tmp_path / 'stage-shards'
    originals = {n: getattr(builder, n) for n in ('prepare_assignment_artifacts', 'load_assignment_stage', '_challenge')}
    began = time.monotonic()
    first = builder.build(args)
    cold = time.monotonic() - began
    assert args.compilation_cache, 'successful build must retain a bounded reusable compilation'
    old_root = args.output_root
    args.output_root = tmp_path / 'next'
    args.deployment_id = '5f6f2a46-0a1c-4b8e-9f11-2c2f5aa7b1c4'
    observations = json.loads(args.managed_hello_observations.read_text())
    observations.update(run_id='next-qwen-20260913T090000Z', deployment_id=args.deployment_id)
    args.managed_hello_observations.write_text(json.dumps(observations))
    def forbidden(*a, **k):
        pytest.fail('cache hit repeated local model preparation/load/challenge')
    monkeypatch.setattr(builder, 'prepare_assignment_artifacts', forbidden)
    monkeypatch.setattr(builder, 'load_assignment_stage', forbidden)
    monkeypatch.setattr(builder, '_challenge', forbidden)
    began = time.monotonic()
    second = builder.build(args)
    warm = time.monotonic() - began
    assert second['compilation_cache_hit'] is True
    assert second['route_ready'] is False  # qualification never reused
    assert second['challenge_output_token_ids'] == first['challenge_output_token_ids']
    plan = json.loads((args.output_root / 'operator-plan.json').read_text())
    assert plan['run_id'] == observations['run_id']
    assert plan['controller']['run_plan']['deployment_id'] == args.deployment_id
    for index in range(2):
        path = f'transfer-bundle/control/node-{index}-assignment.json'
        old = json.loads((old_root / path).read_text())
        new = json.loads((args.output_root / path).read_text())
        assert new['deployment_id'] == args.deployment_id
        assert new['assignment_id'] != old['assignment_id']
    print(f'fixture_compile_seconds cold={cold:.6f} cached={warm:.6f}')
    assert warm < 60
    warm_root = args.output_root
    for name, function in originals.items():
        monkeypatch.setattr(builder, name, function)
    args.output_root = tmp_path / 'independent-cold'
    args.emit_stage_shards = tmp_path / 'independent-shards' if slice_mode else None
    args.compilation_cache = {}
    builder.build(args)
    # Independently recomputed new-deployment graph/proof expectation must be
    # identical to the rebound cached one, not just self-consistent.
    for name in ('execution-graph.json', 'node-0-load-proof.json', 'node-1-load-proof.json'):
        hot = json.loads((warm_root / 'transfer-bundle/control' / name).read_text())
        fresh = json.loads((args.output_root / 'transfer-bundle/control' / name).read_text())
        assert hot == fresh


@pytest.mark.parametrize('change', ['model', 'manifest', 'members', 'generation', 'keys', 'split', 'runtime'])
def test_cache_key_changes_with_authority_or_model_inputs(tmp_path, monkeypatch, change):
    from test_nhost_chain_compile import _app
    from mycelium_a13_macos import chain_compile as cc
    app, captured, nodes, snapshot = _app(tmp_path, monkeypatch, (16, 16))
    from mycelium_a13_macos.compile_cache import cache_key
    prepared = app.acquisition.prepared_source()
    context = app._observation_context
    keys = {'gossip': [], 'load_proof': []}
    split = {'stages': [{'node_id': n, 'start': i, 'end': i + 1} for i, n in enumerate(nodes)]}
    before = cache_key(prepared, context.members, context.facts, split, keys, False)
    if change == 'model': prepared.model.model_id += '-changed'
    if change == 'manifest': prepared.manifest_digest = 'sha256:' + '9' * 64
    if change == 'members': context.members.pop()
    if change == 'generation': context.members[0]['generation'] += 1
    if change == 'keys': keys['load_proof'].append({'verification_key_digest': 'new'})
    if change == 'split': split['stages'][0]['end'] += 1
    if change == 'runtime': prepared.model.runtime_quantization = 'none'
    assert cache_key(prepared, context.members, context.facts, split, keys, False) != before


def test_owner_compile_real_slice_cache_survives_fresh_run_paths(tmp_path, monkeypatch):
    """Real owner compiler + builder + slice plans; workflow admission is inert."""
    import threading
    from _nhost_fixtures import file_rows
    from mycelium_a13_macos import chain_compile as cc, serving
    args = fixture(tmp_path, monkeypatch)
    prepared = NS(source_root=args.model_root, manifest={'files': file_rows(args.model_root)},
        manifest_digest='sha256:' + '1' * 64, source_bytes=1000,
        model=NS(model_id=args.model_id, resolved_commit=args.resolved_commit,
                 source_representation='bfloat16-safetensors', runtime_dtype='float32',
                 runtime_quantization='int8-weight-only'), resource_proposal=serving.RESOURCE_PROPOSAL)
    topology = json.loads(args.topology.read_text())['nodes']
    nodes = [n['node_id'] for n in topology]
    members = [dict(node_id=n['node_id'], generation=n['membership_generation'], incarnation='inc',
                    verification_key_digest='key', endpoint_id=n['endpoint_id'], lease_expires_at=time.time()+300)
               for n in topology]
    doc = json.loads(args.managed_hello_observations.read_text())
    context = NS(run_id=doc['run_id'], deployment_id=doc['deployment_id'], observations=doc,
        members=members, facts={n['node_id']:{**n,'activation_endpoint_id':n['endpoint_id'],
            'manifest_digest':prepared.manifest_digest,'source_root':'/fixture/slice/first'} for n in topology},
        factory=NS(owner=object()), retire_bootstrap=lambda: None)
    app = NS(root=tmp_path/'app', guard=threading.RLock(), closed=threading.Event(), running=True,
        installation_role='owner', _resource_check=lambda:None, _chain_slice_fetch=True,
        _observation_context=context, acquisition=NS(prepared_source=lambda:prepared), _chain_target_keys=[],
        authority=NS(coordinator=NS(member=lambda n:next(m for m in members if m['node_id']==n),
                                    signer=NS(public_key_record=lambda:{}))),
        _member_capability_reports={m['node_id']:{'node_id':m['node_id'],'generation':m['generation'],
            'expires_at':time.time()+300,'memory_bytes':16<<30} for m in members})
    monkeypatch.setattr(cc, 'fresh_operator_template', lambda *a:_template())
    # Tiny fixture forced through slice mode; production model/resource admission
    # remains covered separately and is not weakened by this fixture seam.
    monkeypatch.setattr(cc, 'slice_fetch_permitted', lambda *a, **k:True)
    plans = []
    def workflow(**kwargs):
        assert kwargs['observation_context'] is context
        plans.append(kwargs['slice_plan'])
        return NS(launch=NS(operator_plan_digest='fixture', physical_host_ids=tuple(nodes)),
                  bind_progress_identity=lambda **k:None)
    monkeypatch.setattr(cc, 'prepare_serving_workflow', workflow)
    for iteration in range(2):
        if iteration:
            app._chain_compile_state = None
            app._chain_epoch += 1
            context.run_id = 'next-qwen-20260913T090000Z'
            context.deployment_id = '5f6f2a46-0a1c-4b8e-9f11-2c2f5aa7b1c4'
            doc.update(run_id=context.run_id, deployment_id=context.deployment_id)
            for row in context.facts.values():
                row['staging_root'] += '/next'
                row['source_root'] = '/fixture/slice/next'
        cc.start_compile(app, acknowledged=True)
        app._chain_compile_thread.join(10)
        assert not app._chain_compile_thread.is_alive()
        assert app._chain_compile_state['state'] == 'compiled'
        assert app._chain_compile_state['compilation_cache_hit'] is bool(iteration)
    assert plans[0] == plans[1], 'identical slice bytes must retain the same digest across fresh runs'


def test_cache_corruption_cannot_reuse_old_proof(tmp_path, monkeypatch):
    args = fixture(tmp_path, monkeypatch)
    builder.build(args)
    assert args.compilation_cache
    weights = args.output_root / 'transfer-bundle/deployment/model.safetensors'
    raw = weights.read_bytes()
    weights.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    args.output_root = tmp_path / 'next'
    # Corruption refuses; it must not silently mint authority from old expected proofs.
    with pytest.raises(ValueError, match='compilation_cache_artifact_changed'):
        builder.build(args)
