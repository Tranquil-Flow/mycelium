# SPDX-License-Identifier: AGPL-3.0-only
"""Native int8 (W8A32) kernel: digest pinning, numerics, runtime hook, stage benchmark.

Conformance on the authoring host only: the library is built here with the
host compiler.  Nothing in this file is Windows or acceleration evidence.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

import numpy_runtime
from mycelium_platform_neutral import int8_stage_benchmark as bench
from mycelium_platform_neutral.native_int8 import (
    NativeInt8Library,
    NativeInt8LinearProvider,
    NativeInt8Unavailable,
    file_digest,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native" / "a9_int8_kernel" / "mycelium_int8.c"
SUFFIX = ".dll" if sys.platform == "win32" else ".so"


@pytest.fixture(scope="module")
def library_path(tmp_path_factory) -> Path:
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if compiler is None:
        pytest.skip("no C compiler on this host")
    target = tmp_path_factory.mktemp("kernel") / f"mycelium_int8{SUFFIX}"
    subprocess.run(
        [compiler, "-O2", "-shared", "-fPIC", "-o", str(target), str(SOURCE)],
        check=True,
        capture_output=True,
    )
    return target


@pytest.fixture(autouse=True)
def _no_leaked_provider():
    numpy_runtime.install_int8_linear_provider(None)
    yield
    numpy_runtime.install_int8_linear_provider(None)


def _int8(rng, out_features, in_features):
    values = rng.integers(-127, 128, size=(out_features, in_features), dtype=np.int8)
    scales = (rng.random(out_features, dtype=np.float32) * 0.02 + 0.001).astype(np.float32)
    return values, scales


def test_library_is_digest_pinned_and_refuses_symlinks_and_relative_paths(library_path, tmp_path):
    digest = file_digest(library_path)
    with pytest.raises(NativeInt8Unavailable, match="provider_library_digest_mismatch"):
        NativeInt8Library(library_path, expected_digest="sha256:" + "0" * 64)
    link = tmp_path / f"link{SUFFIX}"
    os.symlink(library_path, link)
    with pytest.raises(NativeInt8Unavailable, match="provider_library_absent"):
        NativeInt8Library(link, expected_digest=digest)
    with pytest.raises(NativeInt8Unavailable, match="provider_library_path_not_absolute"):
        NativeInt8Library(Path(library_path.name), expected_digest=digest)
    with pytest.raises(NativeInt8Unavailable, match="provider_library_absent"):
        NativeInt8Library(tmp_path / "missing.so", expected_digest=digest)
    library = NativeInt8Library(library_path, expected_digest=digest)
    assert library.dispatch in {"scalar", "avx2_fma"}
    assert library.build_digest == digest


def test_kernel_matches_the_qualified_per_call_path(library_path):
    library = NativeInt8Library(library_path, expected_digest=file_digest(library_path))
    rng = np.random.default_rng(7)
    for rows, out_features, in_features in ((1, 5, 3), (3, 64, 896), (7, 130, 4864)):
        values, scales = _int8(rng, out_features, in_features)
        hidden = rng.normal(0, 1, size=(1, rows, in_features)).astype(np.float32)
        expected = np.matmul(hidden, values.astype(np.float32).T) * scales
        actual = library.linear(hidden, values, scales, threads=2)
        assert actual.shape == expected.shape
        np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-5)
    with pytest.raises(NativeInt8Unavailable, match="weight_representation_unsupported"):
        library.linear(hidden, values.astype(np.int16), scales, threads=1)
    with pytest.raises(NativeInt8Unavailable, match="activation_width_mismatch"):
        library.linear(hidden[..., :-1], values, scales, threads=1)


def test_runtime_hook_routes_every_int8_projection_and_keeps_default_path(library_path):
    rng = np.random.default_rng(11)
    values, scales = _int8(rng, 1500, 64)  # > _CANCELLABLE_OUTPUT_CHUNK exercises chunking
    weight = numpy_runtime.Int8RowwiseWeight(values, scales)
    hidden = rng.normal(0, 1, size=(1, 40, 64)).astype(np.float32)  # > sequence chunk
    reference_plain = numpy_runtime._qwen2_linear(hidden, weight)
    reference_chunked = numpy_runtime._qwen2_linear_checkpointed(hidden, weight, checkpoint=lambda: None)
    provider = NativeInt8LinearProvider(
        NativeInt8Library(library_path, expected_digest=file_digest(library_path)), threads=2
    )
    numpy_runtime.install_int8_linear_provider(provider)
    plain = numpy_runtime._qwen2_linear(hidden, weight)
    chunked = numpy_runtime._qwen2_linear_checkpointed(hidden, weight, checkpoint=lambda: None)
    np.testing.assert_allclose(plain, reference_plain, rtol=2e-5, atol=2e-5)
    np.testing.assert_allclose(chunked, reference_chunked, rtol=2e-5, atol=2e-5)
    assert provider.calls == 1 + 2 * 2  # two output chunks x two sequence chunks
    report = provider.report()
    assert report["full_weight_materializations"] == 0
    assert report["accelerated"] is False and report["qualified"] is False
    numpy_runtime.install_int8_linear_provider(None)
    assert np.array_equal(numpy_runtime._qwen2_linear(hidden, weight), reference_plain)
    with pytest.raises(numpy_runtime.NumpyRuntimeError):
        numpy_runtime.install_int8_linear_provider("not-callable")


def _tiny_model():
    config = {
        "n_layer": 2, "n_embd": 32, "n_head": 4, "n_kv_head": 2, "n_inner": 64,
        "vocab_size": 96, "n_positions": 256, "rms_norm_epsilon": 1e-6,
        "rope_theta": 1_000_000.0, "head_dim": 8, "activation_function": "silu",
        "tie_word_embeddings": True,
    }
    rng = np.random.default_rng(20261007)
    tensors = {
        "model.embed_tokens.weight": rng.normal(0, 0.05, (96, 32)).astype(np.float32),
        "model.norm.weight": rng.normal(1, 0.02, (32,)).astype(np.float32),
    }
    shapes = {
        "input_layernorm.weight": (32,), "self_attn.q_proj.weight": (32, 32),
        "self_attn.q_proj.bias": (32,), "self_attn.k_proj.weight": (16, 32),
        "self_attn.k_proj.bias": (16,), "self_attn.v_proj.weight": (16, 32),
        "self_attn.v_proj.bias": (16,), "self_attn.o_proj.weight": (32, 32),
        "post_attention_layernorm.weight": (32,), "mlp.gate_proj.weight": (64, 32),
        "mlp.up_proj.weight": (64, 32), "mlp.down_proj.weight": (32, 64),
    }
    for layer in range(2):
        for suffix, shape in shapes.items():
            centre = 1.0 if suffix.endswith("layernorm.weight") else 0.0
            tensors[f"model.layers.{layer}.{suffix}"] = rng.normal(centre, 0.05, shape).astype(np.float32)
    return numpy_runtime.quantize_qwen2_numpy_tensors(tensors), config


def test_stage_benchmark_pairs_modes_and_reports_components(library_path):
    tensors, config = _tiny_model()
    provider = NativeInt8LinearProvider(
        NativeInt8Library(library_path, expected_digest=file_digest(library_path)), threads=1
    )
    manifest = bench.frozen_manifest(
        vocab_size=96, paired_windows=3, prompt_lengths=(4, 9), output_lengths=(3, 5), repeats_per_cell=1
    )
    assert manifest == bench.frozen_manifest(
        vocab_size=96, paired_windows=3, prompt_lengths=(4, 9), output_lengths=(3, 5), repeats_per_cell=1
    )
    result = bench.run_paired_benchmark(tensors, config, manifest, candidate_provider=provider)
    assert result["requests_per_mode"] == {"reference": 12, "candidate": 12}
    assert result["token_parity_mismatches"] == []
    assert result["packed_state_unchanged"] is True
    assert result["cleanup"] == {"request_scoped_cleanup": True, "stage_local_kv": True}
    assert len(result["paired_window_ratios"]) == 3
    assert set(result["component_means_ms"]["candidate"]["decode"]) == set(bench.COMPONENTS)
    assert provider.calls > 0
    assert numpy_runtime.installed_int8_linear_provider() is None


def test_paired_lower_bound_is_conservative():
    assert bench.paired_lower_bound([2.0, 2.0, 2.0]) == pytest.approx(2.0)
    assert bench.paired_lower_bound([1.2, 3.0, 2.0]) < 1.5
    assert bench.paired_lower_bound([2.0]) == 0.0
    assert bench.paired_lower_bound([2.0, 0.0, 2.0]) == 0.0


def _import_in_child(env_extra):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", **env_extra}
    code = (
        "import numpy_runtime, json;"
        "p = numpy_runtime.installed_int8_linear_provider();"
        "print(json.dumps(None if p is None else p.report()['build_digest']))"
    )
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=ROOT)


def test_environment_opt_in_is_digest_pinned_and_fails_closed(library_path, tmp_path):
    digest = file_digest(library_path)
    assert _import_in_child({}).stdout.strip() == "null"
    report = tmp_path / "provider-report.json"
    ok = _import_in_child({
        "MYCELIUM_A9_INT8_PROVIDER_LIBRARY": str(library_path),
        "MYCELIUM_A9_INT8_PROVIDER_DIGEST": digest,
        "MYCELIUM_A9_INT8_PROVIDER_REPORT": str(report),
    })
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == f'"{digest}"'
    assert '"calls": 0' in report.read_text()
    for broken in (
        {"MYCELIUM_A9_INT8_PROVIDER_LIBRARY": str(library_path)},
        {"MYCELIUM_A9_INT8_PROVIDER_LIBRARY": str(library_path), "MYCELIUM_A9_INT8_PROVIDER_DIGEST": "sha256:" + "0" * 64},
    ):
        failed = _import_in_child(broken)
        assert failed.returncode != 0
        assert "provider_library_digest" in failed.stderr
