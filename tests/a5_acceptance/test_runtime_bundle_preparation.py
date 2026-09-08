"""Offline packaging must use the real controller archive validator."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location('runtime_bundle', ROOT/'scripts/prepare_a5_runtime_bundle.py')
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_assembly_rejects_gitless_source(tmp_path):
    with pytest.raises(ValueError, match='source_git_root_invalid'):
        module().source_identity(tmp_path)


def test_manifest_serialization_and_archive_consumer(tmp_path):
    m = module()
    (tmp_path/'runtime.py').write_text('print(1)\n')
    manifest = m.transfer_manifest(tmp_path)
    import json, io, tarfile
    from physical_inference_qualification import build_transfer_archive, ControllerError
    serialized = json.loads(json.dumps(manifest))
    archive = build_transfer_archive(tmp_path, serialized)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert tar.getnames() == ['runtime.py']
    (tmp_path/'runtime.py').write_text('print(2)\n')
    with pytest.raises(ControllerError):
        build_transfer_archive(tmp_path, serialized)


def test_manifest_rejects_symlink(tmp_path):
    (tmp_path/'real.py').write_text('x=1')
    (tmp_path/'alias.py').symlink_to(tmp_path/'real.py')
    with pytest.raises(ValueError, match='unsafe_artifact'):
        module().transfer_manifest(tmp_path)


def test_node_subsets_roundtrip_through_real_controller(tmp_path):
    from physical_inference_qualification import PeerIdentity, QualificationController
    m = module()
    (tmp_path/'deployment').mkdir()
    for name in ['physical_inference_node.py', 'deployment/stage-left.safetensors', 'deployment/stage-right.safetensors']:
        (tmp_path/name).write_bytes(name.encode())
    manifest = m.transfer_manifest(tmp_path)
    subsets = m.node_transfer_manifests(manifest, {
        'left': ['physical_inference_node.py', 'deployment/stage-left.safetensors'],
        'right': ['physical_inference_node.py', 'deployment/stage-right.safetensors'],
    })
    peers = tuple(PeerIdentity(node_id=name, ssh_target='fixture@localhost', host_id=f'host-{name}',
        boot_id=f'boot-{name}', staging_root=f'/tmp/mycelium-test/{name}',
        process_transport='local') for name in ['left', 'right'])
    controller = QualificationController(mode='dry-run', peers=peers, source_root=tmp_path,
        transfer_manifest=manifest, node_transfer_manifests=subsets,
        membership_snapshot={}, now=1.0)
    assert list(controller._validate_transfers()) == manifest['files']
    assert {item['path'] for item in subsets['manifests']['left']['files']} == {'physical_inference_node.py', 'deployment/stage-left.safetensors'}
    from scripts.build_qwen_live_route import _node_transfer_manifests
    assert _node_transfer_manifests(manifest, [
        {'node_id': name, 'artifacts': [{'upstream_path': f'stage-{name}.safetensors'}]}
        for name in ['left', 'right']
    ]) == subsets


@pytest.mark.parametrize('selections', [
    {}, {'left': ['runtime.py']}, {'left': ['physical_inference_node.py']},
    {'left': ['physical_inference_node.py', 'physical_inference_node.py']},
    {'left': ['physical_inference_node.py', '../private']},
])
def test_node_subsets_reject_incomplete_or_unbound_selection(tmp_path, selections):
    m = module()
    for name in ['physical_inference_node.py', 'runtime.py']:
        (tmp_path/name).write_bytes(b'local fixture')
    with pytest.raises(ValueError, match='node_transfer'):
        m.node_transfer_manifests(m.transfer_manifest(tmp_path), selections)


@pytest.mark.parametrize('fault', ['missing-artifact', 'duplicate-node'])
def test_stage_pack_subset_producer_rejects_ambiguous_ownership(tmp_path, fault):
    from scripts.build_qwen_live_route import _node_transfer_manifests
    (tmp_path/'physical_inference_node.py').write_bytes(b'fixture')
    manifest = module().transfer_manifest(tmp_path)
    pack = {'node_id': 'left', 'artifacts': [{'upstream_path': 'missing.safetensors'}] if fault == 'missing-artifact' else []}
    with pytest.raises(ValueError, match='node_transfer'):
        _node_transfer_manifests(manifest, [pack, pack] if fault == 'duplicate-node' else [pack])


def test_explicit_null_node_selection_cannot_disable_binding(tmp_path, monkeypatch):
    import sys
    m = module()
    selection = tmp_path/'selection.json'
    selection.write_text('null')
    monkeypatch.setattr(sys, 'argv', ['prepare', '--repo', str(tmp_path), '--sidecar', 'unused',
        '--ui-root', 'unused', '--output', 'unused', '--transfer-path', 'unused',
        '--node-transfer-paths', str(selection)])
    def forbidden(*args):
        raise AssertionError('invalid selection reached packaging')
    monkeypatch.setattr(m, 'prepare', forbidden)
    with pytest.raises(ValueError, match='node_transfer_selection_invalid'):
        m.main()


def model_metadata_input() -> dict:
    from test_model_manifest import ModelManifestTests
    fixture = ModelManifestTests()
    return dict(model_id='org/model', requested_revision='main', resolved_commit='a' * 40,
        config=fixture.config(), checkpoint_index=fixture.index(), file_metadata=fixture.files())


def test_metadata_compiles_into_existing_model_manifest_consumer(tmp_path):
    import json
    from model_manifest import verify_manifest_digest
    metadata = tmp_path/'metadata.json'
    metadata.write_text(json.dumps(model_metadata_input()))
    manifest = module().load_model_metadata(metadata)
    assert manifest['protocol'] == 'mycelium.model_manifest.v1'
    assert manifest['resolved_commit'] == 'a' * 40
    assert verify_manifest_digest(json.loads(json.dumps(manifest))) is True
    manifest['resolved_commit'] = 'b' * 40
    assert verify_manifest_digest(manifest) is False


@pytest.mark.parametrize('fault', ['floating-revision', 'missing-file', 'extra-field', 'symlink'])
def test_metadata_rejects_unbound_inputs_before_packaging(tmp_path, fault):
    import json
    value = model_metadata_input()
    if fault == 'floating-revision':
        value['resolved_commit'] = 'main'
    elif fault == 'missing-file':
        value['file_metadata'].pop('shard-2.safetensors')
    elif fault == 'extra-field':
        value['admission'] = True
    metadata = tmp_path/'metadata.json'
    metadata.write_text(json.dumps(value))
    if fault == 'symlink':
        alias = tmp_path/'alias.json'; alias.symlink_to(metadata); metadata = alias
    with pytest.raises(ValueError):
        module().load_model_metadata(metadata)


def test_metadata_packaging_preserves_unverified_model_boundary(tmp_path, monkeypatch):
    import json
    from model_manifest import verify_manifest_digest
    m = module()
    metadata = tmp_path/'metadata.json'
    metadata.write_text(json.dumps(model_metadata_input()))
    pin = m.digest(metadata)
    native = tmp_path/'sidecar'; native.write_bytes(b'unit-test native placeholder')
    ui = tmp_path/'ui'; ui.mkdir(); (ui/'index.html').write_text('<title>unit fixture</title>')
    # Only identity probes are injected; packaging, serialization and the
    # controller archive consumer execute normally. Not a native-build proof.
    monkeypatch.setattr(m, 'source_identity', lambda repo: {'fixture': True})
    monkeypatch.setattr(m, 'native_identity', lambda sidecar: {'fixture': True})
    output = tmp_path/'bundle'
    result = m.prepare(ROOT, native, ui, output, ['physical_inference_node.py'],
        model_metadata=metadata, expected_model_metadata_digest=pin)
    assert verify_manifest_digest(json.loads((output/'model-manifest.json').read_text())) is True
    assert result['model_metadata']['input_sha256'] == pin
    assert result['model_metadata']['model_payload_verified'] is False
    assert result['model_metadata']['representation_identity'] is None
    assert result['model_identity'] is None
    assert result['admission'] is False
    assert result['qualification_claim'] is False
    assert result['promotion_authorized'] is False
    bad_output = tmp_path/'rejected'
    with pytest.raises(ValueError, match='model_metadata_digest_mismatch'):
        m.prepare(ROOT, native, ui, bad_output, ['physical_inference_node.py'],
            model_metadata=metadata, expected_model_metadata_digest='sha256:' + '0' * 64)
    assert not bad_output.exists()


@pytest.mark.parametrize('content', ['{"model_id":"one","model_id":"two"}', 'null'])
def test_metadata_rejects_ambiguous_json(tmp_path, content):
    p = tmp_path/'input.json'; p.write_text(content)
    with pytest.raises(ValueError):
        module().load_model_metadata(p)


def test_metadata_rejects_fifo_without_blocking(tmp_path):
    import os
    p = tmp_path/'input.json'; os.mkfifo(p)
    with pytest.raises(ValueError, match='unsafe_artifact'):
        module().load_model_metadata(p)


def test_native_architecture_is_checked(tmp_path):
    fake = tmp_path/'binary'
    fake.write_bytes(b'not executable'); fake.chmod(0o700)
    with pytest.raises(ValueError, match='native_architecture_invalid'):
        module().native_identity(fake)
