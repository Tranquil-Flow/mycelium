# SPDX-License-Identifier: AGPL-3.0-only
"""Mac MLX/INT8 measurement and adjacent-host codec qualification publisher.

Runs the product publisher on this host's real MLX over a tiny Qwen2 snapshot.
Nothing here is a Windows receipt, a two-host physical claim, or acceleration:
the frozen decision still blocks short, portable or throttled runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import run_a9_gate as gate_driver  # noqa: E402
from tests.a9_acceptance.test_a9_gate_driver import _tiny_snapshot  # noqa: E402

pytest.importorskip("mlx.core")


def _run(*argv):
    return gate_driver.main([str(item) for item in argv])


def _reference(hidden, values, scales):
    return np.matmul(hidden, values.astype(np.float32).T) * scales


def _weights(rows_out, cols, seed=1):
    rng = np.random.default_rng(seed)
    values = rng.integers(-127, 128, size=(rows_out, cols), dtype=np.int8)
    scales = (rng.random(rows_out, dtype=np.float32) * 0.01 + 1e-4).astype(np.float32)
    return values, scales


def test_provider_matches_numpy_reference_without_float_weight_copies():
    from mycelium_platform_neutral.mac_mlx_int8 import MacMlxInt8LinearProvider
    provider = MacMlxInt8LinearProvider()
    for rows, out, cols in ((1, 64, 64), (3, 96, 128), (8, 40, 96)):
        values, scales = _weights(out, cols, seed=out)
        hidden = np.random.default_rng(out).normal(0, 1, (1, rows, cols)).astype(np.float32)
        actual = provider(hidden, values, scales)
        expected = _reference(hidden, values, scales)
        assert actual.shape == expected.shape and actual.dtype == np.float32
        assert float(np.max(np.abs(actual - expected))) / float(np.max(np.abs(expected))) < 1e-3
    provider.mark_warm()
    assert provider.full_weight_materializations == 0
    report = provider.report()
    assert report["backend_id"] == "mac_mlx_int8_provider"
    assert report["accelerated"] is False and report["qualified"] is False
    assert report["packed_weight_conversions"] == 3


def test_provider_counts_a_repack_after_warm_as_a_full_weight_materialization():
    from mycelium_platform_neutral.mac_mlx_int8 import MacMlxInt8LinearProvider
    provider = MacMlxInt8LinearProvider()
    values, scales = _weights(32, 64)
    other, other_scales = _weights(32, 64, seed=9)
    hidden = np.ones((1, 1, 64), dtype=np.float32)
    provider(hidden, values, scales)
    provider.mark_warm()
    provider(hidden, values, scales)
    assert provider.full_weight_materializations == 0
    provider(hidden, other, other_scales)
    assert provider.full_weight_materializations == 1


def test_provider_refuses_unsupported_shape_and_dtype():
    from mycelium_platform_neutral.mac_mlx_int8 import MacMlxInt8LinearProvider, MacMlxUnavailable
    provider = MacMlxInt8LinearProvider()
    values, scales = _weights(8, 30)
    with pytest.raises(MacMlxUnavailable, match="mac_mlx_shape_unsupported"):
        provider(np.ones((1, 1, 30), dtype=np.float32), values, scales)
    values, scales = _weights(8, 64)
    with pytest.raises(MacMlxUnavailable, match="mac_mlx_operand_invalid"):
        provider(np.ones((1, 1, 64), dtype=np.float32), values.astype(np.int16), scales)


def test_mac_benchmark_refuses_a_non_apple_silicon_host_unless_conformance(tmp_path, monkeypatch):
    import mycelium_platform_neutral.mac_mlx_int8 as mod
    monkeypatch.setattr(mod, "observe_host_platform", lambda: ("linux", "x86_64"))
    snapshot = _tiny_snapshot(tmp_path / "model")
    common = ["mac-benchmark", "--gate-dir", tmp_path / "gate", "--snapshot", snapshot,
              "--profile-digest", "local-declaration", "--thermal-state", "nominal",
              "--prompt-lengths", "4", "9", "--output-lengths", "3", "5", "--repeats-per-cell", "2"]
    assert _run(*common) == 2
    assert not (tmp_path / "gate" / "13-mac-benchmark.json").exists()


def test_mac_benchmark_publishes_a_real_mlx_receipt_that_still_obeys_the_frozen_decision(tmp_path):
    snapshot = _tiny_snapshot(tmp_path / "model")
    gate = tmp_path / "gate"
    assert _run("mac-benchmark", "--gate-dir", gate, "--snapshot", snapshot,
                "--profile-digest", "local-declaration", "--thermal-state", "nominal",
                "--prompt-lengths", "4", "9", "--output-lengths", "3", "5", "--repeats-per-cell", "2") == 0
    receipt = json.loads((gate / "13-mac-benchmark.json").read_text())
    assert receipt["kind"] == "mac_stage_benchmark"
    assert receipt["mac_host"] is True and "windows_host" not in receipt
    assert receipt["profile_binding"] == "unsigned_local_declaration"
    assert receipt["provider"]["backend_id"] == "mac_mlx_int8_provider"
    assert receipt["result"]["token_parity_mismatches"] == []
    record = receipt["efficiency_qualification"]
    assert record["provider_binding"]["backend_id"] == "mac_mlx_int8_provider"
    assert record["provider_binding"]["device_class"] == "apple_silicon_mac"
    assert record["materialization_proof"]["warm_decode_full_weight_materializations"] == 0
    assert record["decision"]["state"] == "not_performance_ready"  # tiny run cannot clear the frozen thresholds
    assert "short_output_insufficient_tokens" in record["decision"]["blockers"]
    assert "windows" not in json.dumps(record["provider_binding"]).lower()


def _emit(gate, host="mac-a", generation=3, extra=()):
    return _run("codec-emit", "--gate-dir", gate, "--host-id", host, "--generation", generation,
                "--dtype", "float16", "--hidden-size", "64", *extra)


def _verify(gate, frames, host="mac-b", generation=3, extra=()):
    return _run("codec-verify", "--gate-dir", gate, "--frames", frames, "--host-id", host,
                "--generation", generation, *extra)


def test_codec_publisher_qualifies_only_distinct_adjacent_apple_hosts(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert _emit(a) == 0
    frames = a / "20-codec-frames.json"
    assert _verify(b, frames) == 0
    receipt = json.loads((b / "21-codec-adjacent.json").read_text())
    assert receipt["kind"] == "adjacent_codec_qualification"
    assert receipt["adjacent_hosts"] is True and receipt["qualified"] is True and receipt["blockers"] == []
    assert receipt["producer"]["host_id"] == "mac-a" and receipt["verifier"]["host_id"] == "mac-b"
    assert receipt["parity"]["exact_float16_round_trip"] is True
    assert receipt["byte_reduction"]["ratio"] >= 1.9
    assert len(receipt["rejection_digests"]) == 7
    assert receipt["generation"] == 3 and receipt["frames_verified"] == 3
    assert "token parity" in receipt["claim_boundary"]


def test_codec_publisher_refuses_same_host_as_adjacent_evidence(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert _emit(a, host="mac-a") == 0
    assert _verify(b, a / "20-codec-frames.json", host="mac-a") == 1
    receipt = json.loads((b / "21-codec-adjacent.json").read_text())
    assert receipt["adjacent_hosts"] is False and receipt["qualified"] is False
    assert "hosts_not_adjacent" in receipt["blockers"]


def test_codec_publisher_records_tamper_and_generation_withdrawal(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    assert _emit(a) == 0
    path = a / "20-codec-frames.json"
    doc = json.loads(path.read_text())
    frame = bytearray(bytes.fromhex(doc["frames"][1]["frame_hex"]))
    frame[-3] ^= 0xFF
    doc["frames"][1]["frame_hex"] = bytes(frame).hex()
    tampered = a / "tampered.json"
    tampered.write_text(json.dumps(doc))
    assert _verify(b, tampered) == 1
    receipt = json.loads((b / "21-codec-adjacent.json").read_text())
    assert receipt["qualified"] is False and receipt["frames_verified"] < 3
    assert any(item.startswith("frame_rejected:") for item in receipt["blockers"])
    assert _verify(c, path, generation=4) == 1  # the edge generation changed: withdrawal, not silent decode
    receipt = json.loads((c / "21-codec-adjacent.json").read_text())
    assert receipt["qualified"] is False and receipt["withdrawal_observed"] is True
    assert "codec_generation_changed" in receipt["blockers"]


def test_codec_publisher_refuses_non_apple_silicon_verifier_unless_conformance(tmp_path, monkeypatch):
    import mycelium_platform_neutral.adjacent_codec as mod
    a, b = tmp_path / "a", tmp_path / "b"
    assert _emit(a) == 0
    monkeypatch.setattr(mod, "observe_host_platform", lambda: ("windows", "x86_64"))
    assert _verify(b, a / "20-codec-frames.json") == 1
    receipt = json.loads((b / "21-codec-adjacent.json").read_text())
    assert receipt["qualified"] is False and "host_not_apple_silicon" in receipt["blockers"]
