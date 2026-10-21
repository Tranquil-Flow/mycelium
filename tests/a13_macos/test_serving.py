# SPDX-License-Identifier: AGPL-3.0-only
"""A13 macOS production-serving seam tests (no model execution)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import tempfile
from dataclasses import replace

import pytest
import tests.physical_runner.conftest as native_plan_fixtures
from mycelium_physical_runner.config import load_operator_plan
from mycelium_a13_macos.authority import DemoAuthority, DemoAuthorityError


@pytest.fixture
def seed(tmp_path):
    authority=DemoAuthority(tmp_path/"existing-owner")
    authority.start()
    try:
        authority.create_swarm({"cooperative_peer_visibility_acknowledged":True,
            "resource_use_acknowledged":True,"contribution_enabled":False})
        request=authority.pair_request()
        handoff=authority.approve_pairing(request,ttl_seconds=120,quota={
            "storage_bytes":2_000_000_000,"network_bytes_per_day":2_000_000_000,"background_allowed":False})
        authority.accept_pairing(handoff)
        yield authority
    finally: authority.stop()


@pytest.fixture(autouse=True)
def private_fixture_identity(monkeypatch):
    # A non-credential marker file, never an SSH key or live connection.
    base=Path.home()/'.cache'/'mycelium-a13-test-identities'
    base.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as directory:
        monkeypatch.setattr(native_plan_fixtures,'_SECURE_IDENTITY_ROOT',Path(directory))
        yield

from mycelium_a13_macos.serving import (
    CANDIDATE_MODEL,
    EXECUTED_NATIVE_MODEL,
    ExecutionApproval,
    ModelContract,
    ServingPreparationError,
    prepare_local_source,
    prepare_registry_launch,
)


def _snapshot(tmp_path: Path, model: ModelContract = CANDIDATE_MODEL) -> Path:
    cache = tmp_path / ("models--" + model.model_id.replace("/", "--"))
    snapshot = cache / "snapshots" / model.resolved_commit
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["Qwen2ForCausalLM"],
                "model_type": "qwen2",
                "torch_dtype": "bfloat16",
            }
        ),
        encoding="utf-8",
    )
    (snapshot / "model.safetensors").write_bytes(b"safe-test-header")
    return snapshot


def _native_snapshot(tmp_path: Path) -> Path:
    snapshot = _snapshot(tmp_path)
    config = {
        "architectures": ["Qwen2ForCausalLM"],
        "model_type": "qwen2",
        "torch_dtype": "bfloat16",
        "num_hidden_layers": 1,
        "hidden_size": 4,
        "num_attention_heads": 1,
        "num_key_value_heads": 1,
        "head_dim": 4,
        "intermediate_size": 8,
        "vocab_size": 8,
        "max_position_embeddings": 16,
        "rms_norm_eps": 1e-6,
        "rope_theta": 10_000.0,
        "hidden_act": "silu",
        "tie_word_embeddings": True,
    }
    (snapshot / "config.json").write_text(json.dumps(config), encoding="utf-8")
    tensor_names = (
        "model.embed_tokens.weight",
        "model.layers.0.self_attn.q_proj.weight",
        "model.norm.weight",
    )
    header = {}
    payload = bytearray()
    for name in tensor_names:
        start = len(payload)
        payload.extend(struct.pack("<f", 1.0))
        header[name] = {
            "dtype": "F32",
            "shape": [1],
            "data_offsets": [start, len(payload)],
        }
    raw_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    (snapshot / "model.safetensors").write_bytes(
        struct.pack("<Q", len(raw_header)) + raw_header + payload
    )
    for name in (
        "tokenizer.json",
        "vocab.json",
        "merges.txt",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ):
        (snapshot / name).write_text("{}", encoding="utf-8")
    return snapshot


def _compiled_manifest(model: ModelContract) -> dict[str, object]:
    return {
        "model_id": model.model_id,
        "requested_revision": "main",
        "resolved_commit": model.resolved_commit,
        "format": "safetensors_monolithic",
        "files": [
            {
                "path": "model.safetensors",
                "size_bytes": 16,
                "content_digest": {"algorithm": "sha256", "value": "a" * 64},
            }
        ],
        "runtime_model": {"architecture": "qwen2", "model_config": {}},
        "manifest_digest": {"algorithm": "sha256", "value": "b" * 64},
    }


def test_owner_contract_matches_hosted_instruct_int8() -> None:
    # wp8-02 G1 (2026-09-16): the owner-directed useful-model composition.  The
    # pinned snapshot is the local HF cache entry a09a35458c... (14 files,
    # 15,242,807,270 B, 28 decoder layers) that the WP7/A3 records qualified.
    assert CANDIDATE_MODEL.model_id == "Qwen/Qwen2.5-7B-Instruct"
    assert CANDIDATE_MODEL.resolved_commit == "a09a35458c702b33eeacc393d103063234e8bc28"
    assert CANDIDATE_MODEL.source_representation == "bfloat16-safetensors"
    assert CANDIDATE_MODEL.runtime_backend == "mlx"
    assert CANDIDATE_MODEL.runtime_dtype == "float32"
    assert CANDIDATE_MODEL.runtime_quantization == "int8-weight-only"
    assert CANDIDATE_MODEL.decoding == "quantized_greedy_token_id"
    assert CANDIDATE_MODEL.sampling_seed == 0
    # The checked-in native end-to-end evidence is still the 0.5B wp8-01 round;
    # it deliberately no longer equals the selected candidate.
    assert EXECUTED_NATIVE_MODEL.model_id == "Qwen/Qwen2.5-0.5B-Instruct"
    assert EXECUTED_NATIVE_MODEL.runtime_quantization == "int8-weight-only"
    assert EXECUTED_NATIVE_MODEL != CANDIDATE_MODEL


def test_prepare_source_calls_real_compiler_seam_without_creating_runtime_state(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    runtime_root = tmp_path / "runtime-must-not-exist"
    seen = []

    def compiler(source):
        seen.append(source)
        return _compiled_manifest(CANDIDATE_MODEL)

    prepared = prepare_local_source(snapshot, compiler=compiler)

    assert prepared.model == CANDIDATE_MODEL
    assert prepared.manifest_digest == "sha256:" + "b" * 64
    assert prepared.source_bytes == len(b"safe-test-header")
    assert seen[0].root == snapshot
    assert seen[0].model_id == CANDIDATE_MODEL.model_id
    assert seen[0].resolved_commit == CANDIDATE_MODEL.resolved_commit
    assert not runtime_root.exists()
    assert prepared.resource_proposal.network_download_bytes == 11_535_382
    assert prepared.resource_proposal.execution_download_bytes == 0
    assert prepared.resource_proposal.run_count == 1


def test_prepare_source_rejects_unpinned_or_wrong_architecture_before_compiler(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    called = False

    def compiler(_source):
        nonlocal called
        called = True
        return _compiled_manifest(CANDIDATE_MODEL)

    wrong_revision = snapshot.parent / ("f" * 40)
    snapshot.rename(wrong_revision)
    with pytest.raises(ServingPreparationError, match="source_revision_mismatch"):
        prepare_local_source(wrong_revision, compiler=compiler)
    assert not called

    right_revision = wrong_revision.parent / CANDIDATE_MODEL.resolved_commit
    wrong_revision.rename(right_revision)
    config = json.loads((right_revision / "config.json").read_text(encoding="utf-8"))
    config["model_type"] = "gpt2"
    (right_revision / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ServingPreparationError, match="source_architecture_mismatch"):
        prepare_local_source(right_revision, compiler=compiler)
    assert not called


def _operator_plan(
    tmp_path: Path,
    prepared,
    *,
    seed=None,
    quantization: str = CANDIDATE_MODEL.runtime_quantization,
    dtype: str = "float32",
) -> Path:
    bundle = tmp_path / "bundle"
    control = bundle / "control"
    control.mkdir(parents=True)
    graph = {
        "deployment_id": "deployment-a13",
        "model_id": prepared.model.model_id,
        "resolved_commit": prepared.model.resolved_commit,
        "manifest_digest": prepared.manifest_digest,
        "stages": [
            {"placements": [{"node_id": "a13-local-0"}]},
            {"placements": [{"node_id": "a13-local-1"}]},
        ],
    }
    nodes = []
    for index in range(2):
        assignment = {
            "model_id": prepared.model.model_id,
            "resolved_commit": prepared.model.resolved_commit,
            "manifest_digest": prepared.manifest_digest,
            "node_id": f"a13-local-{index}",
            "runtime": {
                "backend": "mlx",
                "dtype": dtype,
                "quantization": quantization,
            },
        }
        assignment_name = f"node-{index}-assignment.json"
        (control / assignment_name).write_text(json.dumps(assignment), encoding="utf-8")
        nodes.append(
            {
                "node_id": f"a13-local-{index}",
                "configure": {
                    "assignment_file": f"control/{assignment_name}",
                    "graph": graph,
                },
            }
        )
    plan = tmp_path / "operator-plan.json"
    payload=native_plan_fixtures.operator_plan_payload(tmp_path.resolve())
    if seed is not None:
        payload['controller']['membership_snapshot'].update(
            swarm_id=seed.coordinator.swarm_id,seed_key_digest=seed.coordinator.signer.verification_key_digest)
    payload['controller']['source_root']=str(bundle.resolve())
    payload['controller']['run_plan']['nodes']=nodes
    payload['controller']['run_plan']['entry_node_id']='a13-local-0'
    payload['controller']['run_plan']['request']={'sampling_seed':0}
    for index,peer in enumerate(payload['controller']['peers']):
        peer['node_id']=f'a13-local-{index}'
    plan.write_text(json.dumps(payload),encoding='utf-8')
    load_operator_plan(plan)  # Native schema-valid fixture, not physical evidence.
    return plan


def _free_dynamic_port() -> int:
    """A loopback port that is free now and that ``RegistryLaunch.run`` accepts.

    ``run`` really binds the port it is given (``_assert_loopback_port_free``),
    so a constant such as 49152 collides under pytest-xdist when two workers
    reach that bind together (``OSError: [Errno 48] Address already in use``).
    The port is released before use, so another process could take it in
    between; ``run`` then fails closed with ``dynamic_loopback_port_unavailable``.
    That window is microseconds wide on a port the kernel just chose, unlike a
    constant every worker shares.  Ports below 49152 are refused by ``run``
    (``dynamic_loopback_port_required``), so an ephemeral port outside that range
    (Linux defaults start at 32768) is not returned.
    """
    import random
    import socket
    for candidate in (0, *random.sample(range(49_152, 65_536), 64)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            port = probe.getsockname()[1]
        if 49_152 <= port <= 65_535:
            return port
    raise RuntimeError("no free dynamic loopback port")


def _prepared(tmp_path: Path):
    return prepare_local_source(
        _snapshot(tmp_path), compiler=lambda _source: _compiled_manifest(CANDIDATE_MODEL)
    )


def _approval(launch=None) -> ExecutionApproval:
    proposal = CANDIDATE_MODEL.resource_proposal
    return ExecutionApproval(
        model_id=CANDIDATE_MODEL.model_id,
        resolved_commit=CANDIDATE_MODEL.resolved_commit,
        source_representation=CANDIDATE_MODEL.source_representation,
        runtime_dtype=CANDIDATE_MODEL.runtime_dtype,
        runtime_quantization=CANDIDATE_MODEL.runtime_quantization,
        max_execution_download_bytes=proposal.execution_download_bytes,
        max_additional_disk_bytes=proposal.additional_disk_bytes,
        max_combined_rss_bytes=proposal.max_combined_rss_bytes,
        max_wall_seconds=proposal.max_wall_seconds,
        run_count=proposal.run_count,
        operator_plan_digest=launch.operator_plan_digest if launch else '',
        physical_host_ids=launch.physical_host_ids if launch else (),
    )


def test_registry_launch_calls_native_qualification_registry_supervisor(
    tmp_path: Path, seed,
) -> None:
    prepared = _prepared(tmp_path)
    plan = _operator_plan(tmp_path, prepared, seed=seed)
    private_root = tmp_path / "private"
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return 17

    launch = prepare_registry_launch(
        prepared=prepared,
        operator_plan=plan,
        private_state_root=private_root,
        seed_state_root=seed._seed_root,
        runner=runner,
    )
    port = _free_dynamic_port()
    result = launch.run(approval=_approval(launch), port=port)

    assert result == 17
    assert len(calls) == 1
    assert calls[0] == {
        "operator_plans": (plan.resolve(),),
        "host": "127.0.0.1",
        "port": port,
        "registry_state": (private_root / "registry-state.json").resolve(),
        "seed_state_root": seed._seed_root.resolve(),
        "product_state_root": (private_root / "product").resolve(),
        "governance_root": None,
    }
    assert os.stat(private_root).st_mode & 0o077 == 0
    assert not (private_root / "seed").exists()
    assert launch.chain == (
        "PhysicalLiveRoute.from_operator_plan",
        "PhysicalLiveRoute.open",
        "_qualify_open_route",
        "PhysicalLiveRoute.set_deployment_qualification",
        "LiveDeploymentRegistry",
        "build_registry_stack",
        "run_registry_server",
    )


def test_registry_launch_rejects_changed_fidelity_and_missing_execution_approval(
    tmp_path: Path, seed,
) -> None:
    prepared = _prepared(tmp_path)
    quantized_plan = _operator_plan(tmp_path, prepared, quantization="none")
    with pytest.raises(ServingPreparationError, match="operator_plan_representation_mismatch"):
        prepare_registry_launch(
            prepared=prepared,
            operator_plan=quantized_plan,
            private_state_root=tmp_path / "private",
            runner=lambda **_kwargs: 0,
        )

    plan = _operator_plan(tmp_path / "second", prepared, seed=seed)
    launch = prepare_registry_launch(
        prepared=prepared,
        operator_plan=plan,
        private_state_root=tmp_path / "private-second",
        seed_state_root=seed._seed_root,
        runner=lambda **_kwargs: 0,
    )
    with pytest.raises(ServingPreparationError, match="model_execution_approval_required"):
        launch.run(approval=None, port=49153)


def test_registry_launch_is_loopback_only_and_rejects_reserved_ports(
    tmp_path: Path, seed,
) -> None:
    prepared = _prepared(tmp_path)
    launch = prepare_registry_launch(
        prepared=prepared,
        operator_plan=_operator_plan(tmp_path, prepared, seed=seed),
        seed_state_root=seed._seed_root,
        private_state_root=tmp_path / "private",
        runner=lambda **_kwargs: 0,
    )
    for port in (8791, 8876, 8877, 4350, 4351, 4352):
        with pytest.raises(ServingPreparationError, match="port_not_available_for_a13"):
            launch.run(approval=_approval(launch), port=port)
    with pytest.raises(ServingPreparationError, match="dynamic_loopback_port_required"):
        launch.run(approval=_approval(launch), port=80)


@pytest.mark.parametrize('mutation',['schema','same_host','local_processes'])
def test_preparation_rejects_invalid_two_host_contract_before_state(tmp_path,mutation):
    prepared=_prepared(tmp_path)
    plan=_operator_plan(tmp_path,prepared)
    value=json.loads(plan.read_text())
    if mutation=='schema':
        value['protocol']='not-the-canonical-operator-protocol'
    elif mutation=='same_host':
        value['controller']['peers'][1]['host_id']=value['controller']['peers'][0]['host_id']
    else:
        value['controller']['peers'][1]['process_transport']='local'
        value['controller']['peers'][1]['ssh_identity_file']=None
    plan.write_text(json.dumps(value))
    root=tmp_path/'must-not-exist'
    with pytest.raises(ServingPreparationError,match='operator_plan_(canonical_invalid|distinct_hosts_required)'):
        prepare_registry_launch(prepared=prepared,operator_plan=plan,private_state_root=root)
    assert not root.exists()


def test_execution_is_bound_to_exact_plan_and_two_hosts(tmp_path,seed):
    prepared=_prepared(tmp_path)
    plan=_operator_plan(tmp_path,prepared,seed=seed)
    calls=[]
    launch=prepare_registry_launch(prepared=prepared,operator_plan=plan,
        private_state_root=tmp_path/'private',seed_state_root=seed._seed_root,runner=lambda **kw:calls.append(kw) or 0)
    approval=_approval(launch);port=_free_dynamic_port()
    assert len(set(launch.physical_host_ids))==2
    with pytest.raises(ServingPreparationError,match='model_execution_topology_mismatch'):
        launch.run(approval=replace(approval,physical_host_ids=('unapproved-a','unapproved-b')),port=port)
    with pytest.raises(ServingPreparationError,match='model_execution_topology_mismatch'):
        launch.run(approval=replace(approval,operator_plan_digest='sha256:'+'0'*64),port=port)
    value=json.loads(plan.read_text())
    value['controller']['peers'][1]['ssh_target']='unapproved@other-host'
    plan.write_text(json.dumps(value))
    with pytest.raises(ServingPreparationError,match='operator_plan_changed'):
        launch.run(approval=approval,port=port)
    assert calls==[]


@pytest.mark.parametrize('field,value',[
    ('seed_key_digest','sha256:'+'0'*64),('swarm_id','different-swarm')])
def test_launch_requires_existing_matching_seed_before_creating_state(tmp_path,seed,field,value):
    prepared=_prepared(tmp_path)
    plan=_operator_plan(tmp_path,prepared,seed=seed)
    root=tmp_path/'must-not-create'
    with pytest.raises(ServingPreparationError,match='existing_seed_required'):
        prepare_registry_launch(prepared=prepared,operator_plan=plan,private_state_root=root)
    assert not root.exists()
    missing=tmp_path/'missing-seed'
    with pytest.raises(ServingPreparationError,match='operator_plan_seed_binding_invalid'):
        prepare_registry_launch(prepared=prepared,operator_plan=plan,private_state_root=root,seed_state_root=missing)
    assert not root.exists() and not missing.exists()
    payload=json.loads(plan.read_text())
    payload['controller']['membership_snapshot'][field]=value
    plan.write_text(json.dumps(payload))
    with pytest.raises(ServingPreparationError,match='operator_plan_seed_binding_invalid'):
        prepare_registry_launch(prepared=prepared,operator_plan=plan,private_state_root=root,seed_state_root=seed._seed_root)
    assert not root.exists()


def test_seed_binding_is_read_only_and_rechecked_at_execution(tmp_path,seed):
    import hashlib
    prepared=_prepared(tmp_path)
    plan=_operator_plan(tmp_path,prepared,seed=seed)
    def files():
        return {str(p.relative_to(seed._seed_root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in seed._seed_root.rglob('*') if p.is_file()}
    before=files()
    calls=[]
    launch=prepare_registry_launch(prepared=prepared,operator_plan=plan,
        private_state_root=tmp_path/'private',seed_state_root=seed._seed_root,
        runner=lambda **kw:calls.append(kw) or 0)
    assert files()==before
    assert launch.seed_state_root==seed._seed_root.resolve()
    # Remove a key in this disposable test installation, never a seed DB edit.
    (seed._seed_root/'identity'/'seed.key').unlink()
    with pytest.raises(ServingPreparationError,match='operator_plan_seed_binding_invalid'):
        launch.run(approval=_approval(launch),port=_free_dynamic_port())
    assert calls==[]


def test_owner_preparation_selects_own_seed_without_enabling_serving(tmp_path,seed):
    prepared=_prepared(tmp_path)
    plan=_operator_plan(tmp_path,prepared,seed=seed)
    factory=lambda **kwargs: None  # Admission seam; never launches a node here.
    launch=seed.prepare_serving(prepared=prepared,operator_plan=plan,session_factory=factory)
    assert launch.session_factory is factory
    assert launch.seed_state_root==seed._seed_root.resolve()
    assert launch.private_state_root==(seed.state_root/'serving').resolve()
    assert not (launch.private_state_root/'seed').exists()
    assert seed.status()['route_ready'] is False
    assert seed.status()['contribution_enabled'] is False
    seed.stop()
    with pytest.raises(DemoAuthorityError,match='authority_not_started'):
        seed.prepare_serving(prepared=prepared,operator_plan=plan)
    seed.start()
    member=seed.coordinator.members()[0]
    seed.revoke(member['node_id'],member['generation'])
    with pytest.raises(DemoAuthorityError,match='authority_member_not_current'):
        seed.prepare_serving(prepared=prepared,operator_plan=plan)
