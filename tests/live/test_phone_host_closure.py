# SPDX-License-Identifier: AGPL-3.0-only
"""No model loads: strict production adapters and fail-closed phone gates."""
import copy
import json
from types import SimpleNamespace

import pytest

from physical_inference_qualification import ControllerError


def test_production_factory_reads_private_local_binding(tmp_path):
    from mycelium_live.phone_stage_session import PhoneStageSessionFactory
    secret = tmp_path / 'bootstrap'
    secret.write_bytes(b'x' * 32)
    secret.chmod(0o600)
    descriptor = dict(socket_path=str(tmp_path / 'control.sock'), bootstrap_secret_file=str(secret),
                      controller_endpoint_id='cd' * 32)
    factory = PhoneStageSessionFactory.from_descriptor(descriptor)
    assert factory._controller_endpoint_id == 'cd' * 32
    secret.chmod(0o644)
    with pytest.raises(ControllerError, match='external_control_binding_invalid'):
        PhoneStageSessionFactory.from_descriptor(descriptor)
    secret.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(secret)
    with pytest.raises(ControllerError, match='external_control_binding_invalid'):
        PhoneStageSessionFactory.from_descriptor(dict(descriptor, bootstrap_secret_file=str(link)))


def test_new_qwen_phone_class_preserves_existing_android_membership():
    from mycelium_membership import (validate_peer_runtime_capability, peer_runtime_is_activation_eligible,
                                     ROUTER_ACTIVATION_PROTOCOL, MembershipContractError)
    capability = dict(runtime_backend='mobile-rust-qwen2',transport='iroh',activation_protocol=ROUTER_ACTIVATION_PROTOCOL)
    validate_peer_runtime_capability('android_native_qwen2_iroh', capability)
    assert peer_runtime_is_activation_eligible('android_native_qwen2_iroh', capability)
    with pytest.raises(MembershipContractError):
        validate_peer_runtime_capability('android_native_iroh', capability)


def test_phone_runtime_metadata_requires_qwen2_float32_int8():
    from runtime_contracts import normalize_qwen2_model_config, validate_normalized_runtime
    from layer_assignment import _normalize_runtime
    config=normalize_qwen2_model_config(dict(num_hidden_layers=2,hidden_size=8,num_attention_heads=2,
        num_key_value_heads=1,intermediate_size=16,vocab_size=32,max_position_embeddings=64,
        rms_norm_eps=1e-6,rope_theta=10000,hidden_act='silu',tie_word_embeddings=True),expected_layers=2)
    runtime=dict(backend='mobile-rust-qwen2',dtype='float32',quantization='int8-weight-only')
    model=dict(architecture='qwen2',model_config=config)
    assert _normalize_runtime(runtime,'phone',{'runtime_model':model}) == {**runtime,**model}
    for override in (dict(dtype='float16'),dict(quantization='none'),dict(architecture='qwen3')):
        with pytest.raises(ValueError): validate_normalized_runtime({**runtime,**model,**override})


def test_phone_runtime_registry_and_no_python_execution():
    from mycelium_layer_planner.gossip_adapter import _QUALIFIED_DECODE_MODES
    from mycelium_qualification.physical_deployment import _validate_runtime_backend, PhysicalDeploymentError
    from runtime_loader import select_stage_backend, RuntimeLoadError
    assert _QUALIFIED_DECODE_MODES['mobile-rust-qwen2'] == frozenset({'complete_context_replay'})
    assert _validate_runtime_backend('mobile-rust-qwen2', 'float32') == 'mobile-rust-qwen2'
    with pytest.raises(PhysicalDeploymentError):
        _validate_runtime_backend('mobile-rust-qwen2', 'float16')
    with pytest.raises(RuntimeLoadError, match='external_runtime_requires_phone_load_proof'):
        select_stage_backend(runtime={'backend': 'mobile-rust-qwen2'}, prefer='auto')


def test_a7_rejects_phone_before_bundle_or_model_access(tmp_path):
    from mycelium_a7_operator_plan import extend_operator_plan, A7PlanError
    for phone in ('source', 'standby'):
        plan = {'controller': {'peers': [dict(node_id=n, process_transport=('external_iroh' if n == phone else 'local'))
                                        for n in ('source', 'standby')]}}
        with pytest.raises(A7PlanError, match='a7_phone_kv_unsupported'):
            extend_operator_plan(plan, out_bundle=tmp_path/'never-created', variant='positive',
                                 source_node_id='source', standby_node_id='standby')
        assert not (tmp_path/'never-created').exists()


def test_phone_signed_identity_requires_out_of_band_seed_binding():
    from mycelium_qualification.live import _signed_observations, LiveRouteQualificationError
    from mycelium_qualification.signing import generate_ed25519_signer
    signer = generate_ed25519_signer(endpoint_id='ab'*32)
    obs = dict(protocol='mycelium.physical_node_observation.v1', event='configured', monotonic_ns=1,
               run_id='run', deployment_id='dep', node_id='phone', host_id='phone', process_id='inc-123',
               endpoint_id='ab'*32, peer_generation=1, state='RUNNING', route_ready=False, details={})
    host = dict(obs, node_id='host', host_id='host', process_id=15, endpoint_id='ef'*32)
    hs = generate_ed25519_signer(endpoint_id='ef'*32)
    signed = lambda s,o: dict(observation=o,signature=s.sign(o),verification_key=s.public_key_record())
    att = dict(run_id='run',execution_graph={'deployment_id':'dep'},signed_observations=[signed(signer,obs),signed(hs,host)])
    binding = dict(endpoint_id='ab'*32, process_id='inc-123', member_key_digest=signer.verification_key_digest)
    assert len(_signed_observations(att, phone_bindings={'phone':binding})) == 2
    for bad in ({}, {'phone':dict(binding,process_id='inc-other')}, {'phone':dict(binding,member_key_digest='sha256:'+'0'*64)}):
        with pytest.raises(LiveRouteQualificationError):
            _signed_observations(att, phone_bindings=bad)


def test_phone_load_proof_refuses_unproven_identity_and_pack():
    from mycelium_qualification.phone import phone_load_proof_digest
    obs = dict(node_id='phone', host_id='phone', process_id='inc-123', endpoint_id='ab'*32,
               details=dict(assignment_id='assignment', stage_id='middle', manifest_digest='sha256:'+'a'*64,
                            phone_stage_pack_digest='sha256:'+'b'*64, load_generation=1, runtime_mode='complete_context_replay'))
    with pytest.raises(ValueError, match="phone_load_provenance_unsupported"):
        phone_load_proof_digest(obs)
    obs['details']['phone_stage_pack_digest'] = 'sha256:'+'c'*64
    with pytest.raises(ValueError, match="phone_load_provenance_unsupported"):
        phone_load_proof_digest(obs)
    obs['details']['runtime_mode'] = 'stage_local_kv'
    with pytest.raises(ValueError):
        phone_load_proof_digest(obs)
