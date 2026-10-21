# SPDX-License-Identifier: AGPL-3.0-only
"""Adjacent-host reduced-precision activation codec qualification publisher.

``emit`` runs on host A: it encodes a deterministic activation corpus with the
product :class:`ActivationFrameCodec` and writes the frames with A's identity.
``verify`` runs on host B: it regenerates the same corpus from the declared
seed, decodes A's frames under B's own codec instance, measures value parity and
byte reduction from the codec counters, runs the seven-class rejection battery,
and publishes whether two *distinct* Apple-silicon hosts share one codec edge.
A changed edge generation is recorded as a withdrawal, never silently decoded.
This is codec-edge evidence only; routing and committed-token parity remain the
product-route gate's job.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .activation_transport import REJECTION_CLASSES, ActivationFrameCodec, ActivationFrameRejected
from .seed_migration import observe_host_platform

PROTOCOL = "mycelium.a9_adjacent_codec_frames.v1"
SHAPE_TOKENS = (1, 8, 16)
MIN_BYTE_REDUCTION = 1.9
CLAIM_BOUNDARY = (
    "codec-edge value parity, byte reduction and rejection evidence between the two named hosts "
    "only; committed token parity, placement and the browser-visible route stay with the product "
    "route gate"
)


def corpus(seed: int, hidden_size: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    return [(rng.standard_normal((1, tokens, hidden_size)) * 3.0).astype(np.float32)
            for tokens in SHAPE_TOKENS]


def host_observation(host_id: str) -> dict[str, Any]:
    family, arch = observe_host_platform()
    return {"host_id": host_id, "platform": family, "architecture": arch}


def emit(*, host_id: str, generation: int, dtype: str, seed: int, hidden_size: int) -> dict[str, Any]:
    codec = ActivationFrameCodec(dtype=dtype, generation=generation)
    frames = []
    for index, values in enumerate(corpus(seed, hidden_size)):
        frame = codec.encode(values, frame_id=f"adjacent-{index}")
        frames.append({"frame_id": f"adjacent-{index}", "shape": list(values.shape),
                       "frame_hex": frame.hex(), "frame_sha256": "sha256:" + hashlib.sha256(frame).hexdigest()})
    return {"protocol": PROTOCOL, "producer": host_observation(host_id), "generation": generation,
            "dtype": dtype, "seed": seed, "hidden_size": hidden_size, "frames": frames}


def verify(document: dict[str, Any], *, host_id: str, generation: int, conformance_host: bool) -> dict[str, Any]:
    if document.get("protocol") != PROTOCOL:
        raise ValueError("adjacent_codec_frames_protocol_invalid")
    producer, verifier = document["producer"], host_observation(host_id)
    dtype, values = document["dtype"], corpus(document["seed"], document["hidden_size"])
    blockers: list[str] = []
    apple = lambda obs: (obs["platform"], obs["architecture"]) == ("macos", "arm64")  # noqa: E731
    adjacent = producer["host_id"] != verifier["host_id"] and apple(producer) and (
        apple(verifier) or conformance_host)
    if producer["host_id"] == verifier["host_id"]:
        blockers.append("hosts_not_adjacent")
    if not apple(producer) or not apple(verifier):
        blockers.append("host_not_apple_silicon")
        adjacent = adjacent and not blockers
    codec = ActivationFrameCodec(dtype=dtype, generation=generation)
    verified, exact, max_error, withdrawal = 0, True, 0.0, False
    bytes_before = bytes_after = 0
    for entry, source in zip(document["frames"], values, strict=True):
        frame = bytes.fromhex(entry["frame_hex"])
        try:
            if "sha256:" + hashlib.sha256(frame).hexdigest() != entry["frame_sha256"]:
                raise ActivationFrameRejected("corrupted_frame")
            decoded = codec.decode(frame, expect_shape=tuple(entry["shape"]), expect_dtype=dtype)
        except ActivationFrameRejected as rejected:
            blockers.append(f"frame_rejected:{rejected.code}")
            if generation != document["generation"] and "codec_generation_changed" not in blockers:
                blockers.append("codec_generation_changed")
                withdrawal = True
            continue
        reference = source.astype(np.float16).astype(np.float32) if dtype == "float16" else source
        verified += 1
        bytes_before += int(np.prod(entry["shape"])) * 4  # the float32 block the frame replaced
        bytes_after += len(frame.split(b"\n", 1)[1])  # payload bytes actually received
        exact = exact and bool(np.array_equal(decoded, reference))
        max_error = max(max_error, float(np.abs(decoded - source).max()))
    if verified < len(document["frames"]):
        exact = False
    ratio = bytes_before / bytes_after if bytes_after else 0.0
    if verified and ratio < MIN_BYTE_REDUCTION:
        blockers.append("byte_reduction_below_threshold")
    digests: dict[str, str] = {}
    try:
        digests = ActivationFrameCodec(dtype=dtype, generation=generation).negative_battery(values[1])
    except ActivationFrameRejected as rejected:
        blockers.append(f"battery_failed:{rejected.code}")
    if set(digests) != set(REJECTION_CLASSES):
        blockers.append("battery_incomplete")
    if not exact and verified == len(document["frames"]) and dtype == "float16":
        blockers.append("value_parity_failed")
    return {
        "adjacent_hosts": bool(adjacent), "producer": producer, "verifier": verifier,
        "generation": generation, "produced_generation": document["generation"], "dtype": dtype,
        "frames_total": len(document["frames"]), "frames_verified": verified,
        "parity": {"exact_float16_round_trip": exact and dtype == "float16", "max_abs_error": max_error},
        "byte_reduction": {"bytes_before": bytes_before, "bytes_after": bytes_after, "ratio": round(ratio, 6),
                           "source": "received_frame_payload_sizes"},
        "rejection_digests": digests, "withdrawal_observed": withdrawal,
        "blockers": blockers, "qualified": not blockers and bool(adjacent),
    }
