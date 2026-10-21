# SPDX-License-Identifier: AGPL-3.0-only
"""Builder contract tests. Synthetic signatures/tensors are NOT device/APK evidence."""
import base64
import json
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import build_qwen_live_route as builder
from scripts import qwen_phone_builder as phone
from mycelium_mobile.qwen_stage_manifest import (
    build_stage_manifest_from_safetensors, golden_input_f32, sign_stage_manifest,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import generate_ed25519_signer
from test_qwen_route_builder import _template, _topology_node, _write_topology

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "native/mobile_core/target/debug/examples/qwen2_stage_exec"
if not BINARY.is_file():
    BINARY = ROOT.parent / "native-target/debug/examples/qwen2_stage_exec"
D = "sha256:" + "a" * 64


def topology():
    return [_topology_node(0, backend="numpy"), dict(node_id="node-1",
        process_transport="external_iroh", staging_root="/tmp/mycelium/phone-stage",
        endpoint_id="ab" * 32, membership_generation=2, runtime_backend=phone.BACKEND),
        _topology_node(2, backend="numpy")]


def test_phone_topology_omits_all_process_authority(tmp_path, monkeypatch):
    nodes = builder._topology_nodes(_template(), _write_topology(tmp_path/"topology.json", topology()))
    assert nodes[1] == topology()[1]
    calls = []
    monkeypatch.setattr(builder.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    builder.preflight_remote_peers(nodes)
    assert len(calls) == 1 and "operator@remote-2" in calls[0]


@pytest.mark.parametrize("field,value,code", [
    ("python_executable", "/python", "phone_process_authority_invalid"),
    ("endpoint_secret_file", "/key", "phone_process_authority_invalid"),
    ("ssh_target", "host", "phone_process_authority_invalid"),
    ("runtime_backend", "numpy", "phone_backend_transport_mismatch"),
])
def test_phone_topology_rejects_process_or_backend_substitution(tmp_path, field, value, code):
    nodes = topology()
    nodes[1][field] = value
    with pytest.raises(RuntimeError, match=code):
        builder._topology_nodes(_template(), _write_topology(tmp_path/"topology.json", nodes))


@pytest.mark.parametrize("index", [0, 2])
def test_phone_topology_rejects_entry_or_final(tmp_path, index):
    nodes = topology()
    nodes[1], nodes[index] = nodes[index], nodes[1]
    with pytest.raises(RuntimeError, match="external_node_stage_only"):
        builder._topology_nodes(_template(), _write_topology(tmp_path/"t.json", nodes,
                               placement_order_authority="m14_measured_cycle"))


@pytest.fixture
def synthetic_snapshot(tmp_path):
    fixture = json.loads((ROOT/"native/mobile_core/tests/fixtures/qwen2_synthetic.json").read_text())
    model = tmp_path/"synthetic-model"
    model.mkdir()
    (model/"config.json").write_text(json.dumps(dict(fixture["config"], model_type="qwen2")))
    (model/"model.safetensors").write_bytes(base64.b64decode(fixture["safetensors_b64"]))
    return model, fixture


def packaged_manifest(model):
    """The full-model manifest of the one-file synthetic snapshot (shape only)."""
    import hashlib
    import model_manifest as mm
    raw = (Path(model)/"model.safetensors").read_bytes()
    manifest = dict(protocol="mycelium.model_manifest.v1", model_id="synthetic-qwen", resolved_commit="a"*40,
        files=[dict(path="model.safetensors", size_bytes=len(raw),
                    content_digest=dict(algorithm="sha256", value=hashlib.sha256(raw).hexdigest()))])
    manifest["manifest_digest"] = mm._digest_document(manifest)
    return manifest


#: The phone's own stage-load count in these fixtures. Deliberately NOT the
#: seed route generation (17) or the membership generation (2).
LOAD = 3


@pytest.fixture
def evidence(synthetic_snapshot):
    model, _ = synthetic_snapshot
    import model_manifest as mm
    packaged = packaged_manifest(model)
    M = mm.manifest_digest_ref(packaged)
    source = packaged["files"][0]
    existing = json.loads((ROOT/"native/mobile_core/tests/fixtures/qwen2_stage_manifest_v2.json").read_text())
    manifest = build_stage_manifest_from_safetensors(model/"model.safetensors",
        json.loads((model/"config.json").read_text()), stage_id="stage-001", model_id="synthetic-qwen",
        resolved_commit="a"*40, start_layer=0, end_layer_exclusive=2,
        golden=existing["manifest"]["golden"])
    seed = generate_ed25519_signer(endpoint_id="seed-fixture")
    member = generate_ed25519_signer(endpoint_id="ab"*32)
    from layer_assignment import assignment_id_for
    from runtime_contracts import normalize_qwen2_model_config
    config = json.loads((model/'config.json').read_text())
    config.update(vocab_size=32,max_position_embeddings=1024,hidden_act='silu',tie_word_embeddings=False)
    assignment = dict(protocol='mycelium.layer_assignment.v2', deployment_id='11111111-1111-4111-8111-111111111111',
        deployment_epoch=1,node_id='node-1',manifest_digest=M,model_id='synthetic-qwen',resolved_commit='a'*40,
        range=dict(start_layer=0,end_layer_exclusive=2), components=['decoder'],component_tensor_keys={},component_aliases={},
        expected_tensor_prefixes=[],expected_tensor_keys=[t['name'] for t in manifest['tensors']],
        files=[dict(path=source['path'],size_bytes=source['size_bytes'],content_digest='sha256:'+source['content_digest']['value'])],artifact_cache_root='/tmp/fixture',
        runtime=dict(backend=phone.BACKEND,dtype='float32',quantization='int8-weight-only',architecture='qwen2',
                     model_config=normalize_qwen2_model_config(config,expected_layers=2)),route_ready=False)
    assignment['assignment_id']=assignment_id_for(assignment)
    profile = dict(assignment_id=assignment['assignment_id'], stage_id="stage-001",
        pack_digest=manifest["pack_digest"], manifest_digest=M, load_generation=LOAD)
    receipt = dict(protocol=phone.BUILD_PROTOCOL, source_commit="a"*40,
        mobile_core_version="0.1.0", runtime_build_digest=D, executor_digest=D, apk_digest=D)
    statement = dict(protocol=phone.BINDING_PROTOCOL, node_id="node-1", endpoint_id="ab"*32,
        incarnation="inc-fixture", membership_generation=2, member_verification_key=member.public_key_record(),
        run_id="synthetic-run", deployment_id="11111111-1111-4111-8111-111111111111", issued_at=time.time()-10,
        expires_at=time.time()+300, configure=profile, controller_endpoint_id="cd"*32,
        build_receipt_digest=phone.digest(canonical_json_bytes(receipt)),
        preparation_binding=dict(assignment=assignment,route_generation=17,
            provenance=phone.PREPARATION_PROVENANCE))
    def signed(event, details, monotonic):
        obs = dict(protocol="mycelium.physical_node_observation.v1", event=event,
            monotonic_ns=monotonic, run_id=statement["run_id"], deployment_id=statement["deployment_id"],
            node_id="node-1", host_id="node-1", process_id="inc-fixture", endpoint_id="ab"*32,
            peer_generation=0, state="CONFIGURED", route_ready=False, details=details)
        return dict(ok=True, result=dict(observation=obs, signature=member.sign(obs), verification_key=member.public_key_record()))
    details = {k:v for k,v in profile.items() if k != "pack_digest"}
    details.update(phone_stage_pack_digest=profile["pack_digest"], endpoint_addr={"id":"ab"*32},
        runtime_mode="complete_context_replay", runtime_backend=phone.BACKEND,
        runtime_build_digest=D, golden_output_digest=manifest["golden"]["output_digest"],
        load_identity_provenance=phone.LOAD_IDENTITY_PROVENANCE_V2)
    applied = dict(assignment_id=profile["assignment_id"], stage_id="stage-001",
                   phone_stage_pack_digest=profile["pack_digest"], load_generation=LOAD)
    doc = dict(authorization=dict(protocol="mycelium.membership.signed_message.v1", message=statement,
            signature=seed.sign(statement), verification_key=seed.public_key_record()),
        manifest=sign_stage_manifest(manifest, seed), build_receipt=receipt,
        configured=signed("configured", details, 1),
        snapshot=signed("snapshot", {"host_resources":{"supported_decode_modes":["complete_context_replay"]},
            "load_identity":applied, "configured_load_generation":LOAD}, 2))
    return doc, seed, member


def admit(evidence):
    doc, seed, _ = evidence
    return phone.PhoneEvidence(doc, seed_key_digest=seed.verification_key_digest, node=topology()[1],
                               run_id="synthetic-run", deployment_id="11111111-1111-4111-8111-111111111111")


def test_phone_proof_preserves_signatures_and_checks_exact_source(evidence, synthetic_snapshot):
    e = admit(evidence)
    import copy
    packaged = packaged_manifest(synthetic_snapshot[0])
    assignment = copy.deepcopy(e.statement['preparation_binding']['assignment'])
    proof = e.bind_assignment(assignment, stage_id="stage-001", snapshot_root=synthetic_snapshot[0],
                              model_manifest=packaged)
    assert proof["protocol"] == "mycelium.phone_authenticated_load_proof.v2"
    assert proof["load_identity_provenance"] == phone.LOAD_IDENTITY_PROVENANCE_V2
    assert proof["configured"] == evidence[0]["configured"] and proof["snapshot"] == evidence[0]["snapshot"]
    assert proof["source_binding"]["source_file"] == assignment["files"][0]
    # The seed's route generation stays its own recorded fact, unequal to the load.
    assert proof["preparation_binding"]["route_generation"] == 17 != e.native_load_generation == LOAD
    assert e.document["configured"] == evidence[0]["configured"]
    assert "loaded_tensor_digest" not in e.document  # no invented desktop loader output
    assignment["range"]["end_layer_exclusive"] = 1
    with pytest.raises(RuntimeError, match="phone_assignment_manifest_mismatch"):
        e.bind_assignment(assignment, stage_id="stage-001", snapshot_root=synthetic_snapshot[0],
                          model_manifest=packaged)


@pytest.mark.parametrize("attack,code", [
    ("other_manifest", "phone_packaged_manifest_mismatch"),
    ("undigested", "phone_packaged_manifest_mismatch"),
    ("other_source_hash", "phone_packaged_source_mapping_unsupported"),
    ("sharded", "phone_packaged_source_mapping_unsupported"),
    ("missing", "phone_packaged_manifest_mismatch"),
])
def test_claimed_manifest_must_be_the_packaged_one_holding_the_native_source(evidence, synthetic_snapshot, attack, code):
    """Manifest laundering: the applied stage is fixed; only the packaged relation changes."""
    import copy
    import model_manifest as mm
    e = admit(evidence)
    packaged = packaged_manifest(synthetic_snapshot[0])
    assignment = copy.deepcopy(e.statement['preparation_binding']['assignment'])
    if attack == "other_manifest":
        packaged["requested_revision"] = "another-model-with-the-same-name"
        packaged["manifest_digest"] = mm._digest_document({k: v for k, v in packaged.items() if k != "manifest_digest"})
    elif attack == "undigested":
        packaged["model_id"] = "synthetic-qwen-changed"
    elif attack == "missing":
        packaged = None
    else:
        if attack == "other_source_hash":
            packaged["files"][0]["content_digest"]["value"] = "0"*64
        else:
            packaged["files"].append(dict(packaged["files"][0], path="model-00002-of-00002.safetensors"))
            assignment["files"].append(dict(assignment["files"][0], path="model-00002-of-00002.safetensors"))
        # A coherent controller recomputes every digest it can reach.
        packaged["manifest_digest"] = mm._digest_document({k: v for k, v in packaged.items() if k != "manifest_digest"})
        for item in (assignment, e.statement['preparation_binding']['assignment']):
            item["manifest_digest"] = mm.manifest_digest_ref(packaged)
        e.profile["manifest_digest"] = mm.manifest_digest_ref(packaged)
    with pytest.raises(RuntimeError, match=code):
        e.bind_packaged(assignment, model_manifest=packaged)


@pytest.mark.parametrize("field", ["runtime_build_digest", "golden_output_digest", "runtime_backend"])
def test_old_native_observations_are_not_filled_from_host(evidence, field):
    doc, _, member = evidence
    obs = doc["configured"]["result"]["observation"]
    del obs["details"][field]
    doc["configured"]["result"]["signature"] = member.sign(obs)
    with pytest.raises(RuntimeError, match="phone_authenticated_load_provenance_required"):
        admit(evidence)


@pytest.mark.parametrize("where,field,value", [
    ("configured", "process_id", "inc-other"),
    ("configured", "run_id", "other-run"),
    ("snapshot", "deployment_id", "other-deployment"),
])
def test_signed_observation_substitution_is_refused(evidence, where, field, value):
    doc, _, member = evidence
    obs = doc[where]["result"]["observation"]
    obs[field] = value
    doc[where]["result"]["signature"] = member.sign(obs)
    with pytest.raises(Exception, match="node_observation_invalid"):
        admit(evidence)


def test_tamper_and_stale_authority_refused(evidence):
    doc, seed, _ = evidence
    doc["authorization"]["message"]["expires_at"] = time.time()-1
    with pytest.raises(RuntimeError, match="phone_authority_signature_invalid"):
        admit(evidence)
    doc["authorization"]["signature"] = seed.sign(doc["authorization"]["message"])
    with pytest.raises(RuntimeError, match="phone_authority_stale"):
        admit(evidence)


# The authorization is signed on the seed's clock and checked on the
# controller host (build_qwen_live_route): allow the controller to be up to
# MAX_CLOCK_SKEW_SECONDS behind the seed, never extend the expiry.
def _admit_at(evidence, now):
    doc, seed, _ = evidence
    doc["authorization"]["signature"] = seed.sign(doc["authorization"]["message"])
    return phone.PhoneEvidence(doc, seed_key_digest=seed.verification_key_digest, node=topology()[1],
                               run_id="synthetic-run", deployment_id="11111111-1111-4111-8111-111111111111",
                               now=now)


@pytest.mark.parametrize("behind", [0.2, 29.0])
def test_authority_on_a_controller_clock_up_to_30_s_behind_the_seed_is_accepted(evidence, behind):
    issued = evidence[0]["authorization"]["message"]["issued_at"]
    assert _admit_at(evidence, issued - behind).statement["issued_at"] == issued


def test_authority_on_a_controller_clock_31_s_behind_the_seed_is_refused(evidence):
    issued = evidence[0]["authorization"]["message"]["issued_at"]
    with pytest.raises(RuntimeError, match="phone_authority_stale"):
        _admit_at(evidence, issued - 31.0)


def test_authority_expiry_gets_no_clock_skew_allowance(evidence):
    expires = evidence[0]["authorization"]["message"]["expires_at"]
    with pytest.raises(RuntimeError, match="phone_authority_stale"):
        _admit_at(evidence, expires)


def test_wrong_seed_and_member_key_refused(evidence):
    doc, seed, _ = evidence
    with pytest.raises(RuntimeError, match="phone_authority_key_mismatch"):
        phone.PhoneEvidence(doc, seed_key_digest=D, node=topology()[1],
                            run_id="synthetic-run", deployment_id="11111111-1111-4111-8111-111111111111")
    impostor = generate_ed25519_signer(endpoint_id="ab"*32)
    reply = doc["configured"]["result"]
    reply.update(signature=impostor.sign(reply["observation"]), verification_key=impostor.public_key_record())
    with pytest.raises(Exception, match="node_observation_invalid"):
        admit(evidence)


def test_phone_identity_never_uses_ssh(evidence, monkeypatch):
    e = admit(evidence)
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: pytest.fail("phone launched SSH"))
    peer = dict(node_id="node-1", process_transport="external_iroh")
    result = builder.refresh_peer_identities([peer], "synthetic-run", phone_evidence={"node-1":e})
    assert result[0]["boot_id"] == "inc-fixture"


def test_native_persistent_executor_matches_committed_rust_golden(synthetic_snapshot, record_property):
    assert BINARY.is_file(), "Build the offline locked qwen2_stage_exec before this integration test"
    model, _ = synthetic_snapshot
    manifest = json.loads((ROOT/"native/mobile_core/tests/fixtures/qwen2_stage_manifest_v2.json").read_text())["manifest"]
    g = manifest["golden"]
    x = np.frombuffer(golden_input_f32(g["seed"], g["seq"], 64), dtype="<f4").reshape(1,g["seq"],64)
    with phone.RustStageExecutor(BINARY, model, 0, 2, hidden_size=64) as stage:
        pid = stage.process.pid
        for _ in range(3):
            result = stage.execute(x)
            assert phone.digest(result.tobytes()) == g["output_digest"]
            assert stage.process.pid == pid
        assert len(stage.records) == 3
        record_property("native_executor_pid", pid)
        record_property("native_steps", json.dumps(stage.records, sort_keys=True))
    assert stage.process.poll() is not None and not stage.root.exists()


def test_streaming_challenge_never_desktop_loads_phone(monkeypatch, synthetic_snapshot):
    """Real native middle; explicitly scripted outer stages test routing only."""
    model, _ = synthetic_snapshot
    assert BINARY.is_file()
    loaded = []
    monkeypatch.setattr(builder, "load_assignment_stage", lambda a,r,**kw: loaded.append(a["node_id"]) or a)
    def execute(a, token_ids=None, hidden_states=None):
        if token_ids is not None:
            return np.zeros((1, token_ids.shape[1], 64), dtype=np.float32)
        return hidden_states
    monkeypatch.setattr(builder, "execute_loaded_numpy_stage", execute)
    with phone.RustStageExecutor(BINARY, model, 0, 2, hidden_size=64) as stage:
        prompt, output = builder._streaming_challenge(SimpleNamespace(encode=lambda _: (1,2)),
            [dict(node_id=n) for n in ("entry","phone","final")], [{},{},{}],
            ["numpy",phone.BACKEND,"numpy"], phone_executors={"phone":stage})
        assert len(output) == 2 and prompt == (1,2)
        assert [r["seq"] for r in stage.records] == [2,3]
    assert loaded == ["entry","final","entry","final"]


def test_native_input_rejection_and_close(synthetic_snapshot):
    model, _ = synthetic_snapshot
    with phone.RustStageExecutor(BINARY, model, 0, 2, hidden_size=64) as stage:
        with pytest.raises(RuntimeError, match="phone_executor_input_invalid"):
            stage.execute(np.full((1,2,64), np.nan))
        with pytest.raises(RuntimeError, match="phone_executor_input_invalid"):
            stage.execute(np.zeros((2,2,64)))


def test_provenance_artifacts_cannot_be_substituted(tmp_path):
    # This is deliberately a receipt-verifier unit fixture, NOT an APK provenance receipt.
    receipt = dict(protocol=phone.BUILD_PROTOCOL, source_commit="a"*40, mobile_core_version="0.1.0",
                   runtime_build_digest=D, executor_digest=D, apk_digest=D)
    binary = tmp_path/"not-an-executor"
    binary.write_bytes(b"synthetic corrupt artifact")
    with pytest.raises(RuntimeError, match="phone_build_receipt_digest_mismatch"):
        phone.validate_build_receipt(receipt, expected_digest=D, binary=binary, apk=binary, source_root=tmp_path)
    with pytest.raises(RuntimeError, match="phone_executor_digest_mismatch"):
        phone.validate_build_receipt(receipt, expected_digest=phone.digest(canonical_json_bytes(receipt)),
                                     binary=binary, apk=binary, source_root=tmp_path)


def test_actual_build_requires_phone_authority_before_remote_or_materialization(tmp_path, monkeypatch):
    template = tmp_path/"template.json"
    template.write_text(json.dumps(_template()))
    top = _write_topology(tmp_path/"topology.json", topology())
    monkeypatch.setattr(builder, "preflight_remote_peers", lambda _: pytest.fail("must refuse before SSH"))
    out = tmp_path/"out"
    with pytest.raises(RuntimeError, match="phone_authorized_evidence_required"):
        builder.build(SimpleNamespace(template_plan=template, topology=top, output_root=out,
                                      model_root=tmp_path))
    assert not out.exists()


@pytest.mark.parametrize("dtype,quantization", [("float16","int8-weight-only"), ("float32","none")])
def test_phone_proof_rejects_unsupported_representations(evidence, synthetic_snapshot, dtype, quantization):
    with pytest.raises(RuntimeError, match="phone_runtime_representation_invalid"):
        admit(evidence).bind_assignment(dict(runtime=dict(backend=phone.BACKEND, dtype=dtype,
            quantization=quantization)), stage_id="stage-001", snapshot_root=synthetic_snapshot[0],
            model_manifest=packaged_manifest(synthetic_snapshot[0]))


def test_actual_build_refuses_old_native_load_provenance(evidence, tmp_path, monkeypatch):
    doc, seed, member = evidence
    deployment = doc["authorization"]["message"]["deployment_id"]
    statement = doc["authorization"]["message"]
    statement["deployment_id"] = deployment
    doc["authorization"]["signature"] = seed.sign(statement)
    for name in ("configured", "snapshot"):
        signed = doc[name]["result"]
        signed["observation"]["deployment_id"] = deployment
        if name == "configured":
            del signed["observation"]["details"]["runtime_build_digest"]
        signed["signature"] = member.sign(signed["observation"])
    path = tmp_path/"phone-evidence.json"
    path.write_text(json.dumps(dict(protocol="mycelium.phone_builder_evidence.v1", run_id="synthetic-run",
                                    nodes={"node-1":doc})))
    template = tmp_path/"template.json"
    template.write_text(json.dumps(_template()))
    top = _write_topology(tmp_path/"topology.json", topology())
    monkeypatch.setattr(builder, "preflight_remote_peers", lambda _: pytest.fail("must refuse before SSH"))
    out = tmp_path/"out"
    with pytest.raises(RuntimeError, match="phone_authenticated_load_provenance_required"):
        builder.build(SimpleNamespace(template_plan=template, topology=top, output_root=out,
            model_root=tmp_path, deployment_id=deployment, phone_evidence=path,
            phone_seed_key_digest=seed.verification_key_digest, phone_executor=BINARY,
            phone_apk=tmp_path/"absent.apk", phone_source_root=ROOT))
    assert not out.exists()


def test_membership_snapshot_does_not_accept_a_self_minted_seed():
    with pytest.raises(RuntimeError, match="phone_membership_authority_required"):
        phone.authorized_membership_snapshot(dict(protocol="mycelium.controller_membership_snapshot.v1",
            seed_key_digest="sha256:"+"b"*64), seed_key_digest=D, assignments=[], packs=[],
            graph_document={}, topology=[], phone_evidence={}, now=time.time())


def test_executor_deadline_reaps_unresponsive_native_process(tmp_path, monkeypatch):
    # Adversarial protocol fixture, not a successful native/provenance substitute.
    import sys
    script = tmp_path/"hung-executor"
    script.write_text("#!"+sys.executable+"\nimport time\ntime.sleep(10)\n")
    script.chmod(0o700)
    original = phone.subprocess.Popen
    children = []
    def spawn(*a, **kw):
        child = original(*a, **kw)
        children.append(child)
        return child
    monkeypatch.setattr(phone.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="phone_executor_deadline_exceeded"):
        phone.RustStageExecutor(script, tmp_path, 0, 1, hidden_size=64, timeout=0.05)
    assert time.monotonic() - started < 2
    assert len(children) == 1 and children[0].poll() is not None
