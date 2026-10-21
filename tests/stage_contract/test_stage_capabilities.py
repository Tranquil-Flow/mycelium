# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV29 L0: the frozen stage contract (registry, capabilities, wire dtype negotiation).

Claim boundary: this is a contract freeze.  Registering ``llama_stage`` is a
declared, fenced identity - not an execution backend, not a qualification, and
not a Linux/Windows support claim.
"""

from __future__ import annotations

import copy
import json

import pytest

from mycelium_platform_neutral.primitives import (
    RUNTIME_BACKEND_STAGES,
    PlatformNeutralContractError,
)
from mycelium_platform_neutral.stage_capabilities import (
    FENCED_STAGE_BACKENDS,
    LOSSY_OPT_IN_FLAG,
    LOSSY_STAGE_QUANTIZATIONS,
    PROTOCOL,
    STAGE_BACKENDS,
    STAGE_QUANTIZATIONS,
    StageCapabilities,
    admit_stage_quantization,
    negotiate_route_wire_dtype,
    require_stage_backend,
    stage_backend_selectable,
)

_BUILD = "sha256:" + "ab" * 32


def _record(backend: str = "numpy", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "protocol": PROTOCOL,
        "backend": backend,
        "backend_version": "1.0.0",
        "build_digest": _BUILD,
        "device": "cpu",
        "device_name": "test cpu",
        "architectures": ["qwen2", "qwen3"],
        "weight_formats": ["int8-rowwise", "safetensors-bf16"],
        "wire_dtypes_in": ["float32"],
        "wire_dtypes_out": ["float32"],
        "decode_modes": ["complete_context_replay", "stage_local_kv"],
        "kv_checkpoint_format": None,
        "batching": False,
        "max_ctx": 4096,
        "memory_footprint_estimator": None,
        "deterministic": True,
    }
    base.update(overrides)
    return base


def _caps(backend: str = "numpy", **overrides: object) -> StageCapabilities:
    return StageCapabilities.from_mapping(_record(backend, **overrides))


def _code(excinfo: pytest.ExceptionInfo[PlatformNeutralContractError]) -> str:
    return excinfo.value.code


# --- registry -------------------------------------------------------------------


def test_registry_registers_llama_stage_fenced_and_keeps_llama_http_whole_model() -> None:
    assert RUNTIME_BACKEND_STAGES["llama_stage"] == "fenced"
    assert RUNTIME_BACKEND_STAGES["llama_http"] == "implemented"
    assert FENCED_STAGE_BACKENDS == frozenset({"llama_stage"})
    assert STAGE_BACKENDS == frozenset({"mlx", "numpy", "llama_stage", "mobile-rust-qwen2"})
    # llama_http is a whole-model replica path (plan section 1.2), never a stage.
    assert "llama_http" not in STAGE_BACKENDS


@pytest.mark.parametrize("backend", ["llama_http", "cuda", "", "MLX", None, 3, "unknown"])
def test_registry_rejects_unknown_or_non_stage_backend(backend: object) -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        require_stage_backend(backend)
    assert _code(raised) == "stage_backend_unregistered"


@pytest.mark.parametrize("backend", sorted({"mlx", "numpy", "llama_stage", "mobile-rust-qwen2"}))
def test_registry_accepts_each_registered_stage_backend(backend: str) -> None:
    assert require_stage_backend(backend) == backend


def test_quantization_vocabulary_is_frozen() -> None:
    assert STAGE_QUANTIZATIONS == (
        "none",
        "int8-weight-only",
        "gguf-f16",
        "gguf-q8_0",
        "gguf-q4_k_m",
    )
    assert LOSSY_STAGE_QUANTIZATIONS == frozenset({"gguf-q4_k_m"})
    assert LOSSY_OPT_IN_FLAG == "allow_lossy_stage_quantization"


@pytest.mark.parametrize("quantization", ["int4", "gguf-q2_k", "", "INT8", None, 8])
def test_registry_rejects_unknown_quantization(quantization: object) -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        admit_stage_quantization(quantization)
    assert _code(raised) == "stage_quantization_unregistered"


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only", "gguf-f16", "gguf-q8_0"])
def test_lossless_and_q8_stages_are_admitted_by_default(quantization: str) -> None:
    assert admit_stage_quantization(quantization) == quantization


def test_lossy_k_quant_is_refused_by_default_and_needs_the_explicit_flag() -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        admit_stage_quantization("gguf-q4_k_m")
    assert _code(raised) == "lossy_stage_quantization_refused"
    with pytest.raises(PlatformNeutralContractError):
        admit_stage_quantization("gguf-q4_k_m", **{LOSSY_OPT_IN_FLAG: False})
    # The flag is keyword-only and does not widen the vocabulary.
    assert admit_stage_quantization("gguf-q4_k_m", **{LOSSY_OPT_IN_FLAG: True}) == "gguf-q4_k_m"
    with pytest.raises(PlatformNeutralContractError):
        admit_stage_quantization("int4", **{LOSSY_OPT_IN_FLAG: True})
    with pytest.raises(PlatformNeutralContractError):
        admit_stage_quantization("gguf-q4_k_m", **{LOSSY_OPT_IN_FLAG: "yes"})


def test_llama_stage_is_fenced_until_its_runtime_is_present() -> None:
    assert stage_backend_selectable("llama_stage") is False  # no runtime module exists
    assert stage_backend_selectable("llama_stage", runtime_present=frozenset()) is False
    assert stage_backend_selectable("llama_stage", runtime_present=frozenset({"llama_stage"}))
    # Present runtimes cannot unfence an unregistered id.
    with pytest.raises(PlatformNeutralContractError):
        stage_backend_selectable("llama_http", runtime_present=frozenset({"llama_http"}))
    for backend in ("mlx", "numpy", "mobile-rust-qwen2"):
        assert stage_backend_selectable(backend) is True


# --- StageCapabilities: closed, canonical ---------------------------------------


def test_capabilities_round_trip_with_stable_canonical_bytes() -> None:
    record = _record()
    caps = StageCapabilities.from_mapping(record)
    assert caps.to_mapping() == record
    expected = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert caps.canonical_bytes() == expected.encode("utf-8")
    again = StageCapabilities.from_mapping(json.loads(caps.canonical_bytes()))
    assert again == caps
    assert again.canonical_bytes() == caps.canonical_bytes()
    assert again.digest() == caps.digest()
    assert caps.digest().startswith("sha256:") and len(caps.digest()) == 71


def test_canonical_bytes_are_pinned_for_a_fixed_record() -> None:
    # A pinned vector: any change to field set, order or encoding is a contract
    # change and must be made deliberately.
    caps = _caps()
    assert caps.canonical_bytes() == (
        b'{"architectures":["qwen2","qwen3"],"backend":"numpy","backend_version":"1.0.0",'
        b'"batching":false,"build_digest":"sha256:' + b"ab" * 32 + b'","decode_modes":'
        b'["complete_context_replay","stage_local_kv"],"deterministic":true,"device":"cpu",'
        b'"device_name":"test cpu","kv_checkpoint_format":null,"max_ctx":4096,'
        b'"memory_footprint_estimator":null,"protocol":"mycelium.stage_capabilities.v1",'
        b'"weight_formats":["int8-rowwise","safetensors-bf16"],"wire_dtypes_in":["float32"],'
        b'"wire_dtypes_out":["float32"]}'
    )


def test_unknown_and_missing_keys_are_refused() -> None:
    extra = _record()
    extra["gpu_turbo"] = True
    with pytest.raises(PlatformNeutralContractError) as raised:
        StageCapabilities.from_mapping(extra)
    assert _code(raised) == "stage_capabilities_invalid"
    for key in _record():
        missing = _record()
        del missing[key]
        with pytest.raises(PlatformNeutralContractError) as raised:
            StageCapabilities.from_mapping(missing)
        assert _code(raised) == "stage_capabilities_invalid"
    with pytest.raises(PlatformNeutralContractError):
        StageCapabilities.from_mapping([("backend", "numpy")])  # type: ignore[arg-type]


def test_protocol_must_match_exactly() -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        StageCapabilities.from_mapping(_record(protocol="mycelium.stage_capabilities.v2"))
    assert _code(raised) == "stage_capabilities_protocol_invalid"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("backend", "llama_http", "stage_backend_unregistered"),
        ("backend", "cuda", "stage_backend_unregistered"),
        ("build_digest", "sha256:xyz", "stage_capabilities_build_digest_invalid"),
        ("device", "gpu0", "stage_capabilities_device_invalid"),
        ("device", "cuda:", "stage_capabilities_device_invalid"),
        ("device", "CPU", "stage_capabilities_device_invalid"),
        ("architectures", ["llama"], "stage_capabilities_architectures_invalid"),
        ("architectures", [], "stage_capabilities_architectures_invalid"),
        ("weight_formats", ["gguf-q2_k"], "stage_capabilities_weight_formats_invalid"),
        ("weight_formats", [], "stage_capabilities_weight_formats_invalid"),
        ("wire_dtypes_in", ["float64"], "stage_capabilities_wire_dtypes_invalid"),
        ("wire_dtypes_in", [], "stage_capabilities_wire_dtypes_invalid"),
        ("wire_dtypes_out", ["float32", "float32"], "stage_capabilities_wire_dtypes_invalid"),
        ("decode_modes", ["speculative"], "stage_capabilities_decode_modes_invalid"),
        ("kv_checkpoint_format", "pickle-v1", "stage_capabilities_kv_format_invalid"),
        ("kv_checkpoint_format", "mlx-kv-v1", "stage_capabilities_kv_format_invalid"),  # not numpy's
        ("batching", 1, "stage_capabilities_batching_invalid"),
        ("deterministic", "yes", "stage_capabilities_deterministic_invalid"),
        ("max_ctx", 0, "stage_capabilities_max_ctx_invalid"),
        ("max_ctx", True, "stage_capabilities_max_ctx_invalid"),
        ("max_ctx", 1 << 31, "stage_capabilities_max_ctx_invalid"),
        ("device_name", " padded ", "stage_capabilities_device_name_invalid"),
        ("device_name", "", "stage_capabilities_device_name_invalid"),
    ],
)
def test_field_validation_is_closed(field: str, value: object, code: str) -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        StageCapabilities.from_mapping(_record(**{field: value}))
    assert _code(raised) == code


def test_lists_must_already_be_canonical_sorted_and_unique() -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        StageCapabilities.from_mapping(_record(architectures=["qwen3", "qwen2"]))
    assert _code(raised) == "stage_capabilities_architectures_invalid"
    with pytest.raises(PlatformNeutralContractError) as raised:
        StageCapabilities.from_mapping(
            _record(wire_dtypes_in=["float32", "bfloat16"], wire_dtypes_out=["float32"])
        )
    assert _code(raised) == "stage_capabilities_wire_dtypes_invalid"


def test_numpy_and_phone_backends_cannot_claim_reduced_precision_wire() -> None:
    for backend in ("numpy", "mobile-rust-qwen2"):
        with pytest.raises(PlatformNeutralContractError) as raised:
            _caps(backend, wire_dtypes_in=["bfloat16", "float32"])
        assert _code(raised) == "stage_capabilities_wire_dtypes_invalid"
    wide = ["bfloat16", "float16", "float32"]
    assert _caps("mlx", wire_dtypes_in=wide, wire_dtypes_out=wide, kv_checkpoint_format="mlx-kv-v1")


def test_kv_format_must_belong_to_the_declaring_backend() -> None:
    assert _caps("numpy", kv_checkpoint_format="numpy-kv-v1")
    assert _caps("llama_stage", kv_checkpoint_format="llama-seq-v1", device="cpu")
    with pytest.raises(PlatformNeutralContractError):
        _caps("llama_stage", kv_checkpoint_format="numpy-kv-v1")
    with pytest.raises(PlatformNeutralContractError):
        _caps("mobile-rust-qwen2", kv_checkpoint_format="numpy-kv-v1")


def test_devices_are_a_closed_grammar() -> None:
    for device in ("cpu", "metal", "cuda:0", "vulkan:2"):
        assert _caps("llama_stage", device=device).device == device


def test_mutating_a_returned_mapping_does_not_change_the_record() -> None:
    caps = _caps()
    exported = caps.to_mapping()
    exported["architectures"].append("qwen3_5")  # type: ignore[union-attr]
    assert caps.to_mapping() == _record()
    original = _record()
    snapshot = copy.deepcopy(original)
    StageCapabilities.from_mapping(original)
    assert original == snapshot


def test_admitted_quantizations_follow_the_lossy_policy() -> None:
    caps = _caps(
        "llama_stage",
        weight_formats=["gguf-f16", "gguf-q4_k_m", "gguf-q8_0"],
        wire_dtypes_in=["float32"],
    )
    assert caps.quantizations == ("gguf-f16", "gguf-q4_k_m", "gguf-q8_0")
    assert caps.admitted_quantizations() == ("gguf-f16", "gguf-q8_0")
    assert caps.admitted_quantizations(**{LOSSY_OPT_IN_FLAG: True}) == caps.quantizations


# --- wire dtype negotiation (plan R-W1/R-W2) ------------------------------------


_WIDE = ["bfloat16", "float16", "float32"]


def _wide(backend: str) -> StageCapabilities:
    kv = {"mlx": "mlx-kv-v1", "llama_stage": "llama-seq-v1"}.get(backend)
    return _caps(backend, wire_dtypes_in=_WIDE, wire_dtypes_out=_WIDE, kv_checkpoint_format=kv)


def test_mixed_backend_route_negotiates_float32() -> None:
    present = frozenset({"llama_stage"})
    route = (_wide("mlx"), _wide("llama_stage"))
    assert negotiate_route_wire_dtype(route, runtime_present=present) == "float32"
    # Reduced precision cannot be forced onto a mixed-backend route.
    for requested in ("float16", "bfloat16"):
        with pytest.raises(PlatformNeutralContractError) as raised:
            negotiate_route_wire_dtype(
                route,
                requested=requested,
                enabled=("float32", "float16", "bfloat16"),
                runtime_present=present,
            )
        assert _code(raised) == "mixed_backend_route_requires_float32"


def test_mac_plus_numpy_route_is_float32_even_if_the_mac_could_do_more() -> None:
    assert negotiate_route_wire_dtype((_wide("mlx"), _caps("numpy"))) == "float32"


def test_default_enablement_is_float32_only_even_for_homogeneous_wide_routes() -> None:
    assert negotiate_route_wire_dtype((_wide("mlx"), _wide("mlx"))) == "float32"
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype((_wide("mlx"), _wide("mlx")), requested="float16")
    assert _code(raised) == "wire_dtype_not_enabled"


def test_homogeneous_route_can_opt_into_an_enabled_reduced_dtype() -> None:
    enabled = ("float32", "bfloat16")
    route = (_wide("mlx"), _wide("mlx"))
    assert negotiate_route_wire_dtype(route, requested="bfloat16", enabled=enabled) == "bfloat16"
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype(route, requested="float16", enabled=enabled)
    assert _code(raised) == "wire_dtype_not_enabled"


def test_negotiation_refuses_instead_of_coercing() -> None:
    narrow = _caps("mlx", wire_dtypes_in=["float16"], wire_dtypes_out=["float16"],
                   kv_checkpoint_format="mlx-kv-v1")
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype((narrow, _wide("mlx")))
    assert _code(raised) == "wire_dtype_negotiation_failed"
    # in/out asymmetry: a stage that cannot emit float32 breaks the route.
    asym = _caps("mlx", wire_dtypes_in=["float32"], wire_dtypes_out=["float16"],
                 kv_checkpoint_format="mlx-kv-v1")
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype((asym,))
    assert _code(raised) == "wire_dtype_negotiation_failed"


def test_negotiation_needs_at_least_one_stage_and_valid_inputs() -> None:
    for bad in ((), [object()], None):
        with pytest.raises(PlatformNeutralContractError) as raised:
            negotiate_route_wire_dtype(bad)  # type: ignore[arg-type]
        assert _code(raised) == "route_stage_capabilities_invalid"
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype((_caps(),), enabled=("float64",))
    assert _code(raised) == "wire_dtype_not_enabled"


def test_fenced_backend_cannot_join_a_route_without_its_runtime() -> None:
    route = (_wide("mlx"), _wide("llama_stage"))
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype(route)
    assert _code(raised) == "fenced_stage_backend_unavailable"
    assert negotiate_route_wire_dtype(route, runtime_present=frozenset({"llama_stage"})) == "float32"
