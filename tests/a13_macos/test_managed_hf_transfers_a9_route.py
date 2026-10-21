# SPDX-License-Identifier: AGPL-3.0-only
"""Two-Mac A13 attempt09: whole-snapshot binding for the registered A9 route model.

The packaged two-Mac run (Qwen/Qwen2.5-0.5B-Instruct @ 7ae55760..., model choice
``a9-route``) compiled a real two-stage chain and then failed
``managed_hf_transfer_binding_invalid`` in ``bind_hf_prepositions``.  The only failing
check was the model-identity guard: it accepted ``CANDIDATE_MODEL`` or a model with a
demo-picker ``Choice`` (Qwen3 sizes) and so refused ``A9_ROUTE_MODEL``, which
``serving.REGISTERED_MODELS`` (the registry every sibling gate uses) does register.

Real input shape reproduced here: two members (owner + a remote Mac with its own
snapshot root), string ``'sha256:<hex>'`` digests in the controller transfer manifest
(``{'algorithm','value'}`` dicts in the model manifest), a ``deployment/model.safetensors``
row plus a ``deployment/model.safetensors.index.json`` row that must stay inline.
Pure plan transformation over a tiny real snapshot; no model load, no network.
"""
import copy
import dataclasses
import hashlib
import shutil

import pytest
from test_serving import _native_snapshot

from mycelium_a13_macos.serving import (A9_ROUTE_MODEL, CANDIDATE_MODEL, REGISTERED_MODELS,
                                        prepare_local_source)

OWNER, TARGET = 'remote-mac-owner0000', 'remote-mac-target000'


def _a9_snapshot(root):
    """The tiny native snapshot re-homed as the pinned Qwen2.5-0.5B (A9 route) revision."""
    tiny = _native_snapshot(root/'tiny')
    snapshot = (root/('models--'+A9_ROUTE_MODEL.model_id.replace('/', '--'))
                /'snapshots'/A9_ROUTE_MODEL.resolved_commit)
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(tiny, snapshot)
    return snapshot


def _document(snapshot):
    raw = (snapshot/'model.safetensors').read_bytes()
    rows = sorted([
        {'path': 'control/model-manifest.json', 'size_bytes': 17,
         'content_digest': 'sha256:'+hashlib.sha256(b'control').hexdigest()},
        {'path': 'deployment/model.safetensors', 'size_bytes': len(raw),
         'content_digest': 'sha256:'+hashlib.sha256(raw).hexdigest()},
        {'path': 'deployment/model.safetensors.index.json', 'size_bytes': 9,
         'content_digest': 'sha256:'+hashlib.sha256(b'index').hexdigest()},
        {'path': 'physical_inference_node.py', 'size_bytes': 5,
         'content_digest': 'sha256:'+hashlib.sha256(b'node').hexdigest()},
    ], key=lambda row: row['path'])
    peers = [{'node_id': node, 'process_transport': 'managed_https'} for node in (OWNER, TARGET)]
    return {'peers': peers,
            'transfer_manifest': {'protocol': 'mycelium.controller_transfer_manifest.v1', 'files': rows},
            'node_transfer_manifests': {
                'protocol': 'mycelium.controller_node_transfer_manifests.v1',
                'manifests': {node: {'protocol': 'mycelium.controller_transfer_manifest.v1',
                                     'files': copy.deepcopy(rows)} for node in (OWNER, TARGET)}}}


@pytest.fixture
def a9(tmp_path):
    snapshot = _a9_snapshot(tmp_path)
    prepared = prepare_local_source(snapshot, model=A9_ROUTE_MODEL)
    # The remote Mac's own pinned snapshot path (same HF-cache suffix, different prefix).
    target_root = str(snapshot).replace(str(tmp_path), '/Users/remote/Library/Caches/run/state/target/serving/model-source-7ae557604adf')
    return prepared, _document(snapshot), {OWNER: str(snapshot), TARGET: target_root}


def test_a9_route_model_is_a_registered_model():
    assert A9_ROUTE_MODEL in REGISTERED_MODELS and A9_ROUTE_MODEL != CANDIDATE_MODEL


def test_a9_route_two_member_whole_snapshot_binds(a9):
    from mycelium_a13_macos.managed_transfers import bind_hf_prepositions
    prepared, document, roots = a9
    before = copy.deepcopy(document)
    result = bind_hf_prepositions(document, prepared=prepared, target_source_roots=roots)
    assert document == before                       # input untouched
    members = result['prepositioned_artifacts']['members']
    assert set(members) == {OWNER, TARGET}
    for node, root in roots.items():
        [local] = members[node]
        assert local['destination_path'] == 'deployment/model.safetensors'
        assert local['source_path'] == root+'/model.safetensors'
        assert local['content_digest'].startswith('sha256:')
        kept = [row['path'] for row in result['node_transfer_manifests']['manifests'][node]['files']]
        assert 'deployment/model.safetensors' not in kept
        assert 'deployment/model.safetensors.index.json' in kept     # inline, not a weight
    # the controller-wide manifest still lists every row (it pins identity)
    assert [r['path'] for r in result['transfer_manifest']['files']] == [r['path'] for r in document['transfer_manifest']['files']]


def _refuses(a9, *, prepared=None, document=None, roots=None):
    from mycelium_a13_macos.managed_transfers import bind_hf_prepositions
    p, d, r = a9
    prepared, document, roots = prepared or p, document or d, roots or r
    before = copy.deepcopy(document)
    with pytest.raises(ValueError, match='^managed_hf_transfer_binding_invalid$'):
        bind_hf_prepositions(document, prepared=prepared, target_source_roots=roots)
    assert document == before


def test_unregistered_contract_for_the_same_snapshot_still_refuses(a9):
    # Manifest id/commit still match the snapshot; ONLY the contract is not in the registry.
    prepared = a9[0]
    rogue = dataclasses.replace(prepared, model=dataclasses.replace(prepared.model, runtime_quantization='int4-rogue'))
    assert rogue.model not in REGISTERED_MODELS
    _refuses(a9, prepared=rogue)


def test_unregistered_model_identity_still_refuses(a9):
    prepared = a9[0]
    _refuses(a9, prepared=dataclasses.replace(prepared, model=dataclasses.replace(prepared.model, model_id='Evil/Model')))


def test_weight_digest_mismatch_still_refuses_for_a9(a9):
    document = copy.deepcopy(a9[1])
    for row in document['transfer_manifest']['files']:
        if row['path'] == 'deployment/model.safetensors':
            row['content_digest'] = 'sha256:'+'0'*64
    _refuses(a9, document=document)


@pytest.mark.parametrize('wrong', ['other_model', 'other_revision', 'relative', 'traversal'])
def test_a9_root_for_another_model_or_revision_refuses(a9, wrong):
    _, _, roots = a9
    roots = dict(roots)
    bad = {'other_model': roots[TARGET].replace('Qwen2.5-0.5B-Instruct', 'Qwen2.5-7B-Instruct'),
           'other_revision': roots[TARGET][:-40]+'0'*40,
           'relative': 'models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/'+A9_ROUTE_MODEL.resolved_commit,
           'traversal': roots[TARGET]+'/../'+A9_ROUTE_MODEL.resolved_commit}[wrong]
    roots[TARGET] = bad
    _refuses(a9, roots=roots)


def test_a9_member_without_a_root_refuses(a9):
    _refuses(a9, roots={OWNER: a9[2][OWNER]})
