# SPDX-License-Identifier: AGPL-3.0-only
"""Reduced-precision activation codec — deterministic source-level acceptance tests.

Covers the frozen A9 decisions ``qualified_activation_precision`` (spec §3.4,
§9 negative battery) for the source tranche in
``mycelium_platform_neutral/activation_transport.py``.

The codec is a portable in-process frame codec: it proves shape/dtype/finite
value validation, byte reduction measured from real counters, the seven-class
rejection matrix, generation fencing, replay fencing, cancellation, and
request-scoped cleanup.  It does not prove a qualified physical edge, and the
acceptance cases ``reduced_precision_activation_qualification`` (physical
positive) and ``activation_codec_withdrawal`` (physical negative) stay unearned.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from mycelium_platform_neutral.activation_transport import (
    ACCEPTANCE_COVERAGE,
    FRAME_PROTOCOL,
    REJECTION_CLASSES,
    ActivationFrameCodec,
    ActivationFrameRejected,
    frame_projection_is_privacy_reduced,
)
from mycelium_platform_neutral.primitives import DIGEST_RE, canonical_bytes
from mycelium_platform_neutral.activation_codec import (
    REQUIRED_NEGATIVES,
    ActivationCodecQualification,
)

SEED = 20260926
GENERATION = 7


def _values(rows: int = 4, sequence: int = 8, hidden: int = 16):
    rng = np.random.default_rng(SEED)
    return (rng.standard_normal((rows, sequence, hidden)) * 4.0).astype(np.float32)


def _codec(dtype: str = "float16", generation: int = GENERATION, **kwargs) -> ActivationFrameCodec:
    return ActivationFrameCodec(dtype=dtype, generation=generation, **kwargs)


def test_float16_frame_round_trip_and_measured_byte_reduction() -> None:
    codec = _codec()
    values = _values()
    frame = codec.encode(values, frame_id="frame-0001")
    decoded = codec.decode(
        frame, expect_shape=values.shape, expect_dtype="float16"
    )
    assert decoded.dtype == np.float32
    assert decoded.shape == values.shape
    np.testing.assert_array_equal(decoded, values.astype(np.float16).astype(np.float32))
    np.testing.assert_allclose(decoded, values, rtol=1e-2, atol=1e-2)
    reduction = codec.byte_reduction()
    assert reduction["bytes_before"] == int(values.size * 4)
    assert reduction["bytes_after"] == int(values.size * 2)
    assert reduction["bytes_after"] < reduction["bytes_before"]
    assert reduction["samples"] == 1
    assert DIGEST_RE.fullmatch(reduction["counter_digest"])
    assert codec.byte_reduction_ratio() == 2.0


def test_bfloat16_frame_round_trip_within_half_precision_bound() -> None:
    codec = _codec(dtype="bfloat16")
    values = _values()
    frame = codec.encode(values, frame_id="frame-0002")
    decoded = codec.decode(frame, expect_shape=values.shape, expect_dtype="bfloat16")
    np.testing.assert_allclose(decoded, values, rtol=2 ** -8, atol=0)
    assert codec.byte_reduction()["bytes_after"] == int(values.size * 2)


def test_frames_are_canonical_and_self_describing() -> None:
    codec = _codec()
    values = _values()
    frame = codec.encode(values, frame_id="frame-0003")
    header, payload = frame.split(b"\n", 1)
    declared = json.loads(header.decode("ascii"))
    assert declared["protocol"] == FRAME_PROTOCOL
    assert declared["generation"] == GENERATION
    assert declared["dtype"] == "float16"
    assert declared["shape"] == {
        "batch": values.shape[0],
        "sequence": values.shape[1],
        "hidden_size": values.shape[2],
    }
    assert declared["payload_bytes"] == len(payload)
    assert set(declared) == {
        "protocol",
        "frame_id",
        "generation",
        "dtype",
        "shape",
        "payload_bytes",
        "payload_digest",
    }


def test_every_required_negative_is_rejected_with_its_own_code() -> None:
    assert tuple(REJECTION_CLASSES) == tuple(REQUIRED_NEGATIVES)
    values = _values()

    with pytest.raises(ActivationFrameRejected) as raised:
        _codec(dtype="float32")
    assert raised.value.code == "unsupported_dtype"

    relaxed = ActivationFrameCodec(
        dtype="float16", generation=GENERATION, sender_validates_finite=False
    )
    non_finite = values.copy()
    non_finite[0, 0, 0] = np.float32("inf")
    poisoned = relaxed.encode(non_finite, frame_id="frame-nonfinite")
    receiver = _codec()
    with pytest.raises(ActivationFrameRejected) as raised:
        receiver.decode(poisoned, expect_shape=values.shape, expect_dtype="float16")
    assert raised.value.code == "non_finite_value"

    good = _codec().encode(values, frame_id="frame-good")
    with pytest.raises(ActivationFrameRejected) as raised:
        _codec().decode(good, expect_shape=(1, 1, 1), expect_dtype="float16")
    assert raised.value.code == "wrong_shape"

    with pytest.raises(ActivationFrameRejected) as raised:
        _codec(dtype="bfloat16").decode(
            good, expect_shape=values.shape, expect_dtype="bfloat16"
        )
    assert raised.value.code == "wrong_dtype"

    stale = ActivationFrameCodec(dtype="float16", generation=GENERATION - 1)
    stale_frame = stale.encode(values, frame_id="frame-stale")
    with pytest.raises(ActivationFrameRejected) as raised:
        _codec().decode(stale_frame, expect_shape=values.shape, expect_dtype="float16")
    assert raised.value.code == "generation_mismatch"

    replayer = _codec()
    replayer.decode(good, expect_shape=values.shape, expect_dtype="float16")
    with pytest.raises(ActivationFrameRejected) as raised:
        replayer.decode(good, expect_shape=values.shape, expect_dtype="float16")
    assert raised.value.code == "replayed_frame"

    corrupted = bytearray(good)
    corrupted[-1] ^= 0x01
    with pytest.raises(ActivationFrameRejected) as raised:
        _codec().decode(bytes(corrupted), expect_shape=values.shape, expect_dtype="float16")
    assert raised.value.code == "corrupted_frame"


def test_negative_battery_produces_seven_distinct_evidence_digests() -> None:
    codec = _codec()
    battery = codec.negative_battery(_values())
    assert tuple(sorted(battery)) == tuple(sorted(REQUIRED_NEGATIVES))
    digests = list(battery.values())
    assert len(set(digests)) == len(REQUIRED_NEGATIVES)
    for value in digests:
        assert DIGEST_RE.fullmatch(value)
    assert codec.rejection_counts() == dict.fromkeys(REQUIRED_NEGATIVES, 1)


def test_replay_fence_is_bounded_and_request_scoped_cleanup_restores_it() -> None:
    codec = _codec(max_frames=2)
    values = _values()
    first = codec.encode(values, frame_id="frame-a")
    second = codec.encode(values, frame_id="frame-b")
    codec.decode(first, expect_shape=values.shape, expect_dtype="float16")
    codec.decode(second, expect_shape=values.shape, expect_dtype="float16")
    assert codec.outstanding_frames == 2
    codec.cleanup()
    assert codec.outstanding_frames == 0
    assert codec.byte_reduction()["samples"] == 0
    assert codec.pending_scratch_bytes() == 0
    decoded = codec.decode(first, expect_shape=values.shape, expect_dtype="float16")
    assert decoded.shape == values.shape


def test_cancellation_publishes_nothing_and_releases_the_decode_scratch() -> None:
    codec = _codec()
    values = _values()
    frame = codec.encode(values, frame_id="frame-cancel")
    codec.cancel()
    with pytest.raises(ActivationFrameRejected) as raised:
        codec.decode(frame, expect_shape=values.shape, expect_dtype="float16")
    assert raised.value.code == "cancelled"
    assert codec.pending_scratch_bytes() == 0
    assert codec.outstanding_frames == 0
    assert codec.published_frames == 1


def test_frame_size_and_replay_bounds_fail_closed() -> None:
    codec = _codec(max_frames=1, max_bytes=1024)
    values = _values()
    with pytest.raises(ActivationFrameRejected) as raised:
        codec.encode(values, frame_id="frame-oversized")
    assert raised.value.code == "workspace_exceeded"
    small = codec.encode(values[:1, :1, :8], frame_id="frame-small")
    codec.decode(small, expect_shape=(1, 1, 8), expect_dtype="float16")
    codec.cancel()
    with pytest.raises(ActivationFrameRejected) as raised:
        codec.encode(values[:1, :1, :8], frame_id="frame-fenced")
    assert raised.value.code == "cancelled"
    assert bytes(codec.scratch()) == b""


def test_projection_is_privacy_reduced_canonical_and_bounded() -> None:
    codec = _codec()
    values = _values()
    frame = codec.encode(values, frame_id="frame-projection")
    codec.decode(frame, expect_shape=values.shape, expect_dtype="float16")
    projection = codec.projection()
    assert projection["dtype"] == "float16"
    assert projection["generation"] == GENERATION
    assert projection["frames_decoded"] == 1
    assert projection["bytes_before"] == int(values.size * 4)
    assert projection["bytes_after"] == int(values.size * 2)
    assert frame_projection_is_privacy_reduced(projection) is True
    assert len(canonical_bytes(projection, "codec_projection_noncanonical")) < 4096
    for forbidden in ("values", "activation", "tensor", "payload", "token", "prompt"):
        assert forbidden not in projection
    assert json.dumps(projection) == json.dumps(projection)


def test_battery_output_satisfies_the_frozen_codec_qualification_shape() -> None:
    codec = _codec()
    values = _values()
    frame = codec.encode(values, frame_id="frame-qualification")
    codec.decode(frame, expect_shape=values.shape, expect_dtype="float16")
    battery = codec.negative_battery(values)
    reduction = codec.byte_reduction()
    edge = {
        "runtime_build_digest": "sha256:" + "1" * 64,
        "transport_family": "https",
        "device_class": "reference_host",
    }
    record = {
        "protocol": "mycelium.activation_codec_qualification.v1",
        "qualification_id": "codec-reference-host-local",
        "dtype": "float16",
        "sender": edge,
        "receiver": dict(edge),
        "generation_binding": GENERATION,
        "shapes": {"hidden_size": 16, "max_sequence": 8, "batch": 4},
        "parity": {
            "output_token_parity": True,
            "sample_count": 4,
            "parity_digest": "sha256:" + "2" * 64,
        },
        "validation": {
            "shape_checked": True,
            "dtype_checked": True,
            "finite_values_checked": True,
            "byte_reduction_measured": True,
        },
        "byte_reduction": {
            "bytes_before": reduction["bytes_before"],
            "bytes_after": reduction["bytes_after"],
            "samples": reduction["samples"],
            "counter_digest": reduction["counter_digest"],
        },
        "negatives": {
            name: {"rejected": True, "evidence_digest": digest}
            for name, digest in battery.items()
        },
        "withdrawal": {"state": "current", "reason": None},
        "fallback": {"dtype": None, "qualification_digest": None},
        "issued_at": 1000.0,
        "expires_at": 2000.0,
    }
    qualification = ActivationCodecQualification.from_mapping(record)
    assert qualification.negatives == battery
    assert qualification.is_current(now=1500.0, generation=GENERATION) is True


def test_acceptance_cases_stay_unearned_and_physical() -> None:
    inventory = json.loads(
        (Path(__file__).parent / "inventory.v1.json").read_text(encoding="utf-8")
    )
    cases = {case["case_id"]: case for case in inventory["acceptance_cases"]}
    for case_id, expected_kind in (
        ("reduced_precision_activation_qualification", "physical_positive"),
        ("activation_codec_withdrawal", "physical_negative"),
    ):
        assert cases[case_id]["gate_kind"] == expected_kind
        covered = ACCEPTANCE_COVERAGE[case_id]
        assert covered.startswith("not_earned:")
        assert "physical" in covered
