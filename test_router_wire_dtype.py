# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV29 L2: wire dtype conversion is exact, bit-level and never silently lossy.

Covers the latent NumPy bf16 mapping bug (``_NUMPY_DTYPES['bfloat16']`` mapped to
float32 while the wire carries two bytes per element) and the fp16 overflow
guard.  Claim boundary: this is codec correctness; it does not enable
reduced-precision NumPy routes (``SUPPORTED_NUMPY_DTYPES`` stays float32).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from mycelium_platform_neutral.activation_transport import encode_bfloat16
from mycelium_router.numpy_runtime import NumpyRouterRuntimeError, NumpyRuntimePort
from mycelium_router.payloads import decode_activation, encode_activation
from mycelium_router.wire_dtype import (
    BF16_MAX,
    FP16_MAX,
    WireDtypeError,
    decode_wire_array,
    encode_wire_array,
)


def _code(raised: pytest.ExceptionInfo[BaseException]) -> str:
    return raised.value.code  # type: ignore[attr-defined]


def _bf16_representable(count: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    bits = rng.integers(0, 1 << 32, size=count, dtype=np.uint64).astype(np.uint32)
    bits &= np.uint32(0xFFFF0000)
    values = bits.view(np.float32)
    finite = np.isfinite(values)
    return np.where(finite, values, np.float32(1.5)).astype(np.float32)


# --- wire_dtype module ----------------------------------------------------------


def test_float32_is_identity_little_endian() -> None:
    values = np.array([[[0.0, -0.0, 1.0, -1.5, 3.4028235e38, 1e-45]]], dtype=np.float32)
    encoded = encode_wire_array(values, "float32")
    assert encoded == values.astype("<f4").tobytes()
    decoded = decode_wire_array(encoded, "float32", values.shape)
    assert decoded.dtype == np.float32 and decoded.tobytes() == values.tobytes()


def test_bfloat16_encode_matches_the_existing_rne_encoder_bit_for_bit() -> None:
    rng = np.random.default_rng(11)
    values = (rng.standard_normal(4096) * 10.0 ** rng.integers(-20, 20, 4096)).astype(np.float32)
    assert encode_wire_array(values, "bfloat16") == encode_bfloat16(values)


def test_bfloat16_rounds_ties_to_even() -> None:
    tie_down = np.array([0x3F808000], dtype=np.uint32).view(np.float32)  # tie, even stays 0x3F80
    tie_up = np.array([0x3F818000], dtype=np.uint32).view(np.float32)  # tie, rounds to 0x3F82
    assert encode_wire_array(tie_down, "bfloat16") == (0x3F80).to_bytes(2, "little")
    assert encode_wire_array(tie_up, "bfloat16") == (0x3F82).to_bytes(2, "little")


def test_bfloat16_round_trip_is_exact_for_representable_values() -> None:
    values = _bf16_representable(2048).reshape(1, 64, 32)
    payload = encode_wire_array(values, "bfloat16")
    assert len(payload) == values.size * 2
    decoded = decode_wire_array(payload, "bfloat16", values.shape)
    assert decoded.dtype == np.float32 and decoded.shape == values.shape
    assert decoded.tobytes() == values.tobytes()  # bit-exact, including signed zero


def test_float16_round_trip_is_exact_for_representable_values() -> None:
    rng = np.random.default_rng(3)
    values = rng.standard_normal((1, 8, 16)).astype(np.float16).astype(np.float32)
    decoded = decode_wire_array(encode_wire_array(values, "float16"), "float16", values.shape)
    assert decoded.tobytes() == values.tobytes()


def test_float16_overflow_is_refused_not_turned_into_inf() -> None:
    assert FP16_MAX == 65504.0
    ok = np.array([65504.0, -65504.0, 0.0], dtype=np.float32)
    assert decode_wire_array(encode_wire_array(ok, "float16"), "float16", (3,)).tolist() == ok.tolist()
    just_above = np.nextafter(np.float32(65504.0), np.float32(np.inf))
    for bad in (just_above, np.float32(65520.0), np.float32(1e30), np.float32(-1e30), -just_above):
        with pytest.raises(WireDtypeError) as raised:
            encode_wire_array(np.array([0.0, bad], dtype=np.float32), "float16")
        assert _code(raised) == "activation_float16_overflow"
    # float64 sources are range-checked before any narrowing cast.
    with pytest.raises(WireDtypeError) as raised:
        encode_wire_array(np.array([7.0e4], dtype=np.float64), "float16")
    assert _code(raised) == "activation_float16_overflow"


def test_bfloat16_and_float32_range_guards() -> None:
    assert BF16_MAX == float(np.array([0x7F7F0000], dtype=np.uint32).view(np.float32)[0])
    top = np.array([BF16_MAX, -BF16_MAX], dtype=np.float32)
    assert decode_wire_array(encode_wire_array(top, "bfloat16"), "bfloat16", (2,)).tolist() == top.tolist()
    with pytest.raises(WireDtypeError) as raised:
        encode_wire_array(np.array([3.4028235e38], dtype=np.float32), "bfloat16")
    assert _code(raised) == "activation_bfloat16_overflow"
    with pytest.raises(WireDtypeError) as raised:
        encode_wire_array(np.array([1e300], dtype=np.float64), "float32")
    assert _code(raised) == "activation_float32_overflow"


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("dtype", ["float16", "bfloat16", "float32"])
def test_non_finite_values_are_refused_on_encode(dtype: str, bad: float) -> None:
    with pytest.raises(WireDtypeError) as raised:
        encode_wire_array(np.array([1.0, bad], dtype=np.float32), dtype)
    assert _code(raised) == "nonfinite_activation"


def test_unsupported_dtype_array_and_length_are_refused() -> None:
    values = np.zeros((1, 1, 4), dtype=np.float32)
    for dtype in ("float64", "int8", "BF16", "", None):
        with pytest.raises(WireDtypeError) as raised:
            encode_wire_array(values, dtype)  # type: ignore[arg-type]
        assert _code(raised) == "unsupported_wire_dtype"
        with pytest.raises(WireDtypeError) as raised:
            decode_wire_array(b"\x00" * 16, dtype, (1, 1, 4))  # type: ignore[arg-type]
        assert _code(raised) == "unsupported_wire_dtype"
    with pytest.raises(WireDtypeError) as raised:
        encode_wire_array(np.zeros((1, 4), dtype=np.int32), "float32")
    assert _code(raised) == "unsupported_activation_array"
    with pytest.raises(WireDtypeError) as raised:
        decode_wire_array(b"\x00" * 15, "float32", (1, 1, 4))
    assert _code(raised) == "activation_data_length_mismatch"
    with pytest.raises(WireDtypeError) as raised:
        decode_wire_array(b"\x00" * 16, "bfloat16", (1, 1, 4))  # 8 bytes expected
    assert _code(raised) == "activation_data_length_mismatch"


# --- the NumPy stage: bf16 mapping bug ------------------------------------------


def _port() -> NumpyRuntimePort:
    port = object.__new__(NumpyRuntimePort)
    port.graph = SimpleNamespace(entry_stage_id="stage-0", final_stage_id="stage-9", hidden_size=4)
    port.decode_mode = "stage_local_kv"
    return port


def _decode(port: NumpyRuntimePort, payload: bytes, dtype: str, sequence: int) -> np.ndarray:
    item = SimpleNamespace(
        payload=payload,
        batch_key=SimpleNamespace(token_span=sequence),
        phase="PREFILL",
        position=0,
    )
    runtime = {"dtype": dtype, "model_config": {"n_positions": 64}}
    _tokens, hidden, count = port._decode_input(item, SimpleNamespace(stage_id="stage-1"), runtime)
    assert count == sequence
    return hidden


def test_numpy_stage_decodes_bfloat16_wire_bytes_exactly() -> None:
    bits = np.array([0x3F80, 0x4000, 0xBF80, 0x0000, 0x3F00, 0x4040, 0xC0A0, 0x3E80], dtype="<u2")
    payload = encode_activation(dtype="bfloat16", shape=(1, 2, 4), data=bits.tobytes())
    hidden = _decode(_port(), payload, "bfloat16", 2)
    assert hidden.dtype == np.float32
    assert hidden.reshape(-1).tolist() == [1.0, 2.0, -1.0, 0.0, 0.5, 3.0, -5.0, 0.25]


def test_bfloat16_round_trips_exactly_through_a_numpy_stage() -> None:
    port = _port()
    values = _bf16_representable(3 * 4, seed=21).reshape(1, 3, 4)
    runtime = {"dtype": "bfloat16", "model_config": {"vocab_size": 8}}
    result = port._runtime_result(SimpleNamespace(stage_id="stage-1"), runtime, values)
    assert result.success and result.payload is not None
    envelope = decode_activation(result.payload)
    assert envelope.dtype == "bfloat16" and envelope.shape == (1, 3, 4)
    assert len(envelope.data) == values.size * 2
    again = _decode(port, result.payload, "bfloat16", 3)
    assert again.tobytes() == values.tobytes()


def test_numpy_stage_refuses_float16_overflow_with_a_stable_code() -> None:
    port = _port()
    values = np.zeros((1, 1, 4), dtype=np.float32)
    values[0, 0, 2] = 70000.0
    runtime = {"dtype": "float16", "model_config": {"vocab_size": 8}}
    with pytest.raises(NumpyRouterRuntimeError) as raised:
        port._runtime_result(SimpleNamespace(stage_id="stage-1"), runtime, values)
    assert raised.value.code == "activation_float16_overflow"


def test_numpy_stage_float32_wire_bytes_are_unchanged() -> None:
    port = _port()
    values = np.arange(8, dtype=np.float32).reshape(1, 2, 4) / 3.0
    runtime = {"dtype": "float32", "model_config": {"vocab_size": 8}}
    result = port._runtime_result(SimpleNamespace(stage_id="stage-1"), runtime, values)
    assert result.payload == encode_activation(
        dtype="float32", shape=(1, 2, 4), data=values.astype("<f4").tobytes()
    )
    assert _decode(port, result.payload, "float32", 2).tobytes() == values.tobytes()
