# SPDX-License-Identifier: AGPL-3.0-only
"""Signed v2 mobile stage manifests (real Qwen2 layers delivered by digest)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from mycelium_mobile import qwen_stage_manifest as qm
from mycelium_qualification.signing import Ed25519EvidenceSigner

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "native/mobile_core/tests/fixtures"
SYNTHETIC = json.loads((FIXTURES / "qwen2_synthetic.json").read_text())
COMMIT = "7ae557604adf67be50417f59c2c2f167def9a775"


def _signer(byte: int, endpoint: str = "seed-endpoint") -> Ed25519EvidenceSigner:
    key = Ed25519PrivateKey.from_private_bytes(bytes([byte]) * 32)
    return Ed25519EvidenceSigner(
        endpoint_id=endpoint, _private_key=key, _public_key_bytes=key.public_key().public_bytes_raw()
    )


def _synthetic_file(tmp_path: Path) -> Path:
    path = tmp_path / "model.safetensors"
    path.write_bytes(base64.b64decode(SYNTHETIC["safetensors_b64"]))
    return path


def _config() -> dict:
    return {"model_type": "qwen2", "hidden_act": "silu", **SYNTHETIC["config"]}


def _manifest(tmp_path: Path, **overrides) -> dict:
    args = dict(stage_id="stage-synthetic-0-2", model_id="synthetic/qwen2-tiny", resolved_commit=COMMIT,
                start_layer=0, end_layer_exclusive=2)
    args.update(overrides)
    return qm.build_stage_manifest_from_safetensors(_synthetic_file(tmp_path), _config(), **args)


def _redigest(document: dict) -> dict:
    unsigned = {k: v for k, v in document.items() if k != "pack_digest"}
    return {**unsigned, "pack_digest": qm.manifest_digest(unsigned)}


def test_manifest_pins_every_tensor_by_raw_upstream_digest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    raw = (tmp_path / "model.safetensors").read_bytes()
    assert manifest["protocol"] == qm.MANIFEST_PROTOCOL
    assert manifest["backend"] == "mobile-rust-qwen2"
    assert manifest["quantizer"] == "numpy_rowwise_v1"
    assert manifest["activation_dtype"] == "float32"
    assert manifest["source"] == {
        "file": "model.safetensors",
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    assert manifest["source"]["sha256"] == SYNTHETIC["safetensors_sha256"]
    assert manifest["architecture"] == {
        "model_type": "qwen2", "hidden_act": "silu", "hidden_size": 64, "intermediate_size": 128,
        "num_attention_heads": 4, "num_key_value_heads": 2, "head_dim": 16, "num_hidden_layers": 2,
        "rms_norm_eps": 1e-06, "rope_theta": 1000000.0,
    }
    assert [t["name"] for t in manifest["tensors"]] == qm.tensor_names(0, 2)
    assert len(manifest["tensors"]) == 24
    header_len = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8 : 8 + header_len])
    for tensor in manifest["tensors"]:
        begin, end = header[tensor["name"]]["data_offsets"]
        body = raw[8 + header_len + begin : 8 + header_len + end]
        assert tensor["sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
        assert tensor["size_bytes"] == len(body)
    assert qm.verify_stage_manifest(manifest) == manifest
    # Deterministic: the same inputs give the same digest.
    assert _manifest(tmp_path)["pack_digest"] == manifest["pack_digest"]
    assert len(qm.canonical_ascii(manifest)) < 16 * 1024


def test_manifest_tamper_and_shape_errors_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    def code(document) -> str:
        with pytest.raises(qm.StageManifestError) as exc:
            qm.verify_stage_manifest(document)
        return exc.value.code

    tampered = copy.deepcopy(manifest)
    tampered["tensors"][0]["sha256"] = "sha256:" + "0" * 64
    assert code(tampered) == "stage_manifest_digest_mismatch"
    for field, value, expected in [
        ("backend", "mobile-rust-reference", "stage_manifest_backend_unsupported"),
        ("quantizer", "mlx_int8", "stage_manifest_quantizer_unsupported"),
        ("activation_dtype", "float16", "stage_manifest_activation_dtype_unsupported"),
        ("route_ready", True, "stage_manifest_route_ready_invalid"),
        ("end_layer_exclusive", 3, "stage_manifest_layer_range_invalid"),
        ("start_layer", 2, "stage_manifest_layer_range_invalid"),
        ("resolved_commit", "main", "stage_manifest_identity_invalid"),
    ]:
        edited = copy.deepcopy(manifest)
        edited[field] = value
        assert code(_redigest(edited)) == expected, field
    edited = copy.deepcopy(manifest)
    edited["tensors"][0]["shape"] = [1, 64]
    assert code(_redigest(edited)) == "stage_manifest_tensor_shape_invalid"
    edited = copy.deepcopy(manifest)
    edited["tensors"][0]["dtype"] = "F16"
    assert code(_redigest(edited)) == "stage_manifest_tensor_dtype_unsupported"
    edited = copy.deepcopy(manifest)
    edited["tensors"][0]["size_bytes"] += 2
    assert code(_redigest(edited)) == "stage_manifest_tensor_size_invalid"
    edited = copy.deepcopy(manifest)
    edited["tensors"].pop()
    assert code(_redigest(edited)) == "stage_manifest_tensors_invalid"
    edited = copy.deepcopy(manifest)
    edited["tensors"].reverse()
    assert code(_redigest(edited)) == "stage_manifest_tensors_invalid"
    edited = copy.deepcopy(manifest)
    edited["architecture"]["head_dim"] = 8
    assert code(_redigest(edited)) == "stage_manifest_architecture_invalid"
    edited = copy.deepcopy(manifest)
    edited["extra"] = 1
    assert code(edited) == "stage_manifest_fields_invalid"
    with pytest.raises(qm.StageManifestError) as exc:
        _manifest(tmp_path, end_layer_exclusive=3)
    assert exc.value.code == "stage_manifest_source_tensor_missing"


def test_golden_is_validated_and_bound_into_the_digest(tmp_path: Path) -> None:
    golden = {"protocol": qm.GOLDEN_PROTOCOL, "generator": qm.GOLDEN_GENERATOR, "seed": 42, "seq": 3,
              "output_digest": "sha256:" + "a" * 64}
    with_golden = _manifest(tmp_path, golden=golden)
    assert with_golden["golden"] == golden
    assert with_golden["pack_digest"] != _manifest(tmp_path)["pack_digest"]
    for bad in [{**golden, "seq": 0}, {**golden, "seq": 65}, {**golden, "generator": "x"},
                {**golden, "output_digest": "md5:1"}, {**golden, "seed": -1}]:
        with pytest.raises(qm.StageManifestError) as exc:
            _manifest(tmp_path, golden=bad)
        assert exc.value.code == "stage_manifest_golden_invalid"


def test_seed_signed_manifest_verifies_only_under_the_pinned_seed_key(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    seed = _signer(7)
    envelope = qm.sign_stage_manifest(manifest, seed)
    assert qm.is_signed_stage_manifest(envelope)
    assert qm.verify_signed_stage_manifest(envelope, seed.public_key_record()) == manifest
    with pytest.raises(qm.StageManifestError) as exc:
        qm.verify_signed_stage_manifest(envelope, _signer(8).public_key_record())
    assert exc.value.code == "stage_manifest_signer_mismatch"
    impostor = qm.sign_stage_manifest(manifest, _signer(8))
    impostor["verification_key"] = seed.public_key_record()
    with pytest.raises(qm.StageManifestError) as exc:
        qm.verify_signed_stage_manifest(impostor, seed.public_key_record())
    assert exc.value.code == "stage_manifest_signature_invalid"
    other = _manifest(tmp_path, stage_id="stage-other")
    swapped = {**envelope, "message": other}
    with pytest.raises(qm.StageManifestError) as exc:
        qm.verify_signed_stage_manifest(swapped, seed.public_key_record())
    assert exc.value.code == "stage_manifest_signature_invalid"


def test_exported_objects_are_content_addressed_raw_bytes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    written = qm.export_tensor_objects(tmp_path / "model.safetensors", manifest, tmp_path / "objects")
    assert len(written) == 24
    for tensor in manifest["tensors"]:
        data = (tmp_path / "objects" / tensor["sha256"].removeprefix("sha256:")).read_bytes()
        assert "sha256:" + hashlib.sha256(data).hexdigest() == tensor["sha256"]
        assert len(data) == tensor["size_bytes"]


def test_golden_input_generator_is_exact_and_stable() -> None:
    data = qm.golden_input_f32(42, 2, 8)
    values = struct.unpack("<16f", data)
    assert all(-1.0 <= v < 1.0 for v in values)
    # Every value is an exact multiple of 2**-23 (24-bit lattice), so it is
    # representable in f32 and identical in Rust.
    assert all((v * 8388608.0).is_integer() for v in values)
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(qm.golden_input_f32(42, 2, 8)).hexdigest()
    assert qm.golden_input_f32(43, 2, 8) != data


def test_cli_build_and_export(tmp_path: Path) -> None:
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    _synthetic_file(snapshot)
    (snapshot / "config.json").write_text(json.dumps(_config()))
    out = tmp_path / "manifest.json"
    assert qm.main(["build", "--snapshot", str(snapshot), "--model-id", "synthetic/qwen2-tiny",
                    "--resolved-commit", COMMIT, "--stage-id", "s", "--layers", "1:2", "--out", str(out)]) == 0
    manifest = json.loads(out.read_text())
    assert qm.verify_stage_manifest(manifest)["start_layer"] == 1
    assert len(manifest["tensors"]) == 12
    assert qm.main(["export", "--snapshot", str(snapshot), "--manifest", str(out),
                    "--out-dir", str(tmp_path / "o")]) == 0
    assert len(list((tmp_path / "o").iterdir())) == 12


def _snapshot() -> Path | None:
    value = os.environ.get("MYCELIUM_QWEN05_SNAPSHOT")
    return Path(value) if value and (Path(value) / "model.safetensors").is_file() else None


@pytest.mark.skipif(_snapshot() is None, reason="MYCELIUM_QWEN05_SNAPSHOT not set")
def test_real_qwen05_two_layer_manifest() -> None:
    snapshot = _snapshot()
    config = json.loads((snapshot / "config.json").read_text())
    manifest = qm.build_stage_manifest_from_safetensors(
        snapshot / "model.safetensors", config, stage_id="qwen05-12-14",
        model_id="Qwen/Qwen2.5-0.5B-Instruct", resolved_commit=COMMIT,
        start_layer=12, end_layer_exclusive=14,
    )
    assert manifest["architecture"]["hidden_size"] == 896
    assert manifest["architecture"]["head_dim"] == 64
    assert len(manifest["tensors"]) == 24
    assert {t["dtype"] for t in manifest["tensors"]} == {"BF16"}
    # The digest decisions.log records for this snapshot.
    assert manifest["source"]["sha256"].startswith("sha256:fdf756fa")
    total = sum(t["size_bytes"] for t in manifest["tensors"])
    assert 55_000_000 < total < 65_000_000  # two bf16 layers, ~29.9 MB each
