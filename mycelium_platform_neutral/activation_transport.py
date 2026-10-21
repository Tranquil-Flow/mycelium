# SPDX-License-Identifier: AGPL-3.0-only
"""Reduced-precision activation transport — fail-closed frame codec source.

Spec §3.4, §4.8 and frozen decision ``qualified_activation_precision``.

What this module is: the *source* half of the reduced-precision activation path.
It implements a self-describing, canonical, generation-fenced activation frame
codec with real validation and real counters:

* shape / dtype / finite-value validation on both the sending and receiving side;
* float16 and bfloat16 payloads, both 16-bit, with round-to-nearest-even
  bfloat16 encoding computed here (numpy has no bfloat16 dtype);
* measured byte reduction from actual payload counters (never asserted);
* the seven-class rejection battery the frozen contract
  (``mycelium.activation_codec_qualification.v1``) requires, each producing its
  own evidence digest;
* replay fencing with a bounded fence, cancellation that publishes nothing, and
  request-scoped cleanup that zeroes the decode scratch buffer.

What this module is NOT: a qualified edge.  Frame parity here is *decoded value*
parity on a portable reference host, not committed-token parity on a physically
qualified adjacent runtime pair.  The acceptance cases
``reduced_precision_activation_qualification`` (physical positive) and
``activation_codec_withdrawal`` (physical negative) stay unearned, and
:data:`ACCEPTANCE_COVERAGE` records that as machine-readable state.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from .primitives import (
    DIGEST_RE,
    PlatformNeutralContractError,
    bounded_int,
    canonical_bytes,
    record_digest,
    require,
    segment,
)

#: Protocol of one activation frame (header + payload).
FRAME_PROTOCOL = "mycelium.activation_frame.v1"
#: Protocol of the privacy-reduced runtime projection of this codec.
PROJECTION_PROTOCOL = "mycelium.activation_transport_projection.v1"

#: Dtypes this codec can transport.  ``float32`` is deliberately excluded.
SUPPORTED_DTYPES = ("float16", "bfloat16")
#: The seven negative classes the frozen codec contract requires, in contract order.
REJECTION_CLASSES = (
    "unsupported_dtype",
    "corrupted_frame",
    "non_finite_value",
    "wrong_shape",
    "wrong_dtype",
    "replayed_frame",
    "generation_mismatch",
)
_HEADER_FIELDS = frozenset(
    {
        "protocol",
        "frame_id",
        "generation",
        "dtype",
        "shape",
        "payload_bytes",
        "payload_digest",
    }
)
_SHAPE_FIELDS = frozenset({"batch", "sequence", "hidden_size"})
_PAYLOAD_DTYPES = {"float16": "<f2", "bfloat16": "<u2"}
_FLOAT32_BYTES = 4
_PROJECTION_KEYS = frozenset(
    {
        "protocol",
        "dtype",
        "generation",
        "frames_encoded",
        "frames_decoded",
        "frames_rejected",
        "rejections",
        "bytes_before",
        "bytes_after",
        "byte_reduction_ratio",
        "encode_ms_total",
        "decode_ms_total",
        "outstanding_frames",
    }
)
#: Key names a privacy-reduced projection may never contain.
FORBIDDEN_PROJECTION_KEYS = frozenset(
    {
        "values",
        "activation",
        "activation_values",
        "tensor",
        "payload",
        "token",
        "token_id",
        "prompt",
        "output",
        "kv",
        "logits",
    }
)

#: Closed set of codes this codec raises.  The seven rejection classes are also
#: the contract's negative vocabulary; the rest are codec boundary refusals.
BOUNDARY_CODES = frozenset(
    set(REJECTION_CLASSES)
    | {
        "cancelled",
        "workspace_exceeded",
        "frame_malformed",
        "header_invalid",
        "unsupported_protocol",
        "replay_fence_exhausted",
        "battery_incomplete",
        "byte_reduction_not_measured",
    }
)

#: Acceptance-inventory coverage.  Source evidence is not physical evidence.
ACCEPTANCE_COVERAGE = {
    "reduced_precision_activation_qualification": (
        "not_earned: physical_positive acceptance case requires the parity, byte "
        "reduction, replay, cancellation and corruption gates on a physically "
        "qualified edge between the exact adjacent runtime builds; this tranche "
        "produces the gates and their evidence digests on a reference host only"
    ),
    "activation_codec_withdrawal": (
        "not_earned: physical_negative acceptance case requires an admission and "
        "reconnect through a changed edge generation; the generation fence is "
        "implemented and unit-proven here, the physical edge is not"
    ),
}


class ActivationFrameRejected(RuntimeError):
    """A bounded activation-frame refusal carrying one stable code."""

    def __init__(self, code: str) -> None:
        if code not in BOUNDARY_CODES:
            raise ValueError(f"unregistered_activation_frame_code:{code}")
        self.code = code
        super().__init__(code)


def encode_bfloat16(block: np.ndarray) -> bytes:
    """Round-to-nearest-even float32 -> bfloat16 (top 16 bits) little-endian."""

    bits = np.ascontiguousarray(block, dtype=np.float32).view(np.uint32)
    rounded = (bits + np.uint32(0x7FFF) + ((bits >> np.uint32(16)) & np.uint32(1))) >> np.uint32(16)
    return rounded.astype("<u2").tobytes()


def decode_bfloat16(payload: bytes) -> np.ndarray:
    """bfloat16 little-endian -> float32 (exact widening)."""

    raw = np.frombuffer(payload, dtype="<u2").astype(np.uint32)
    return (raw << np.uint32(16)).view(np.float32).copy()


def frame_projection_is_privacy_reduced(projection: Any) -> bool:
    """True only for a closed, count-only projection with no activation key."""

    if not isinstance(projection, Mapping):
        return False
    keys = set(projection)
    if not keys <= _PROJECTION_KEYS:
        return False
    if keys & FORBIDDEN_PROJECTION_KEYS:
        return False
    for value in projection.values():
        if isinstance(value, Mapping):
            if not all(isinstance(item, str) for item in value.values()):
                return False
        elif isinstance(value, (str, int, float, bool)) or value is None:
            continue
        else:
            return False
    return True


class ActivationFrameCodec:
    """One request-scoped reduced-precision activation frame codec.

    ``dtype`` is the negotiated dtype of this edge; a frame of any other dtype is
    refused rather than coerced.  ``generation`` is the negotiation generation:
    both sides must be on the same generation or the frame is refused.
    ``sender_validates_finite`` models a peer's sender-side policy: a qualified
    sender validates before encoding, and the receiver validates again so an
    unqualified peer cannot push a non-finite activation through.
    """

    def __init__(
        self,
        *,
        dtype: str,
        generation: int,
        max_frames: int = 1024,
        max_bytes: int = 8 << 20,
        sender_validates_finite: bool = True,
    ) -> None:
        if dtype not in SUPPORTED_DTYPES:
            raise ActivationFrameRejected("unsupported_dtype")
        self._dtype = dtype
        self._generation = bounded_int(
            generation, "codec_generation_invalid", minimum=1, maximum=1 << 40
        )
        self._max_frames = bounded_int(
            max_frames, "codec_replay_bound_invalid", minimum=1, maximum=1 << 20
        )
        self._max_bytes = bounded_int(
            max_bytes, "codec_frame_budget_invalid", minimum=64, maximum=1 << 40
        )
        self._sender_validates_finite = bool(sender_validates_finite)
        self._seen: list[str] = []
        self._cancelled = False
        self._scratch: bytearray | None = None
        self._bytes_before = 0
        self._bytes_after = 0
        self._samples = 0
        self._frames_encoded = 0
        self._frames_decoded = 0
        self._rejections: dict[str, int] = {}
        self._encode_seconds = 0.0
        self._decode_seconds = 0.0
        self._last_reduction_digest = "sha256:" + "0" * 64

    # -- request scope ---------------------------------------------------
    @property
    def dtype(self) -> str:
        return self._dtype

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def outstanding_frames(self) -> int:
        return len(self._seen)

    @property
    def published_frames(self) -> int:
        return self._frames_encoded

    def scratch(self) -> bytes:
        """Expose the decode scratch buffer for assertion; never a data path."""

        return bytes(self._scratch) if self._scratch is not None else b""

    def pending_scratch_bytes(self) -> int:
        return len(self._scratch) if self._scratch is not None else 0

    def cancel(self) -> None:
        """Cancel this request scope; nothing further is published or admitted."""

        self._cancelled = True
        self._release_scratch()

    def cleanup(self) -> None:
        """End the request scope: zero the scratch buffer and the replay fence."""

        self._release_scratch()
        self._seen.clear()
        self._cancelled = False
        self._bytes_before = 0
        self._bytes_after = 0
        self._samples = 0
        self._frames_encoded = 0
        self._frames_decoded = 0
        self._encode_seconds = 0.0
        self._decode_seconds = 0.0
        self._rejections.clear()
        self._last_reduction_digest = "sha256:" + "0" * 64

    def _release_scratch(self) -> None:
        if self._scratch is not None:
            self._scratch[:] = b"\x00" * len(self._scratch)
            self._scratch = None

    # -- encoding --------------------------------------------------------
    def encode(self, values: Any, *, frame_id: str) -> bytes:
        if self._cancelled:
            raise ActivationFrameRejected("cancelled")
        started = time.perf_counter()
        try:
            segment(frame_id, "codec_frame_id_invalid")
        except PlatformNeutralContractError as exc:
            raise ActivationFrameRejected("header_invalid") from exc
        block = np.asarray(values)
        if block.dtype.kind != "f":
            raise ActivationFrameRejected("unsupported_dtype")
        if block.ndim != 3 or 0 in block.shape:
            raise ActivationFrameRejected("wrong_shape")
        block = np.ascontiguousarray(block, dtype=np.float32)
        if self._sender_validates_finite and not bool(np.isfinite(block).all()):
            raise ActivationFrameRejected("non_finite_value")
        payload = self._encode_payload(block)
        header = {
            "protocol": FRAME_PROTOCOL,
            "frame_id": frame_id,
            "generation": self._generation,
            "dtype": self._dtype,
            "shape": {
                "batch": int(block.shape[0]),
                "sequence": int(block.shape[1]),
                "hidden_size": int(block.shape[2]),
            },
            "payload_bytes": len(payload),
            "payload_digest": "sha256:" + _sha256_hex(payload),
        }
        try:
            header_bytes = canonical_bytes(header, "codec_header_noncanonical")
        except PlatformNeutralContractError as exc:
            raise ActivationFrameRejected("header_invalid") from exc
        if len(header_bytes) + 1 + len(payload) > self._max_bytes:
            raise ActivationFrameRejected("workspace_exceeded")
        self._bytes_before += int(block.size * _FLOAT32_BYTES)
        self._bytes_after += len(payload)
        self._samples += 1
        self._frames_encoded += 1
        self._last_reduction_digest = self._reduction_digest()
        self._encode_seconds += time.perf_counter() - started
        return header_bytes + b"\n" + payload

    def _encode_payload(self, block: np.ndarray) -> bytes:
        if self._dtype == "float16":
            return np.ascontiguousarray(block, dtype=np.float32).astype("<f2").tobytes()
        return encode_bfloat16(block)

    # -- decoding --------------------------------------------------------
    def decode(
        self, frame: Any, *, expect_shape: Any, expect_dtype: str
    ) -> np.ndarray:
        if self._cancelled:
            raise ActivationFrameRejected("cancelled")
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise ActivationFrameRejected("frame_malformed")
        started = time.perf_counter()
        try:
            if b"\n" not in bytes(frame):
                raise ActivationFrameRejected("frame_malformed")
            header_bytes, payload = bytes(frame).split(b"\n", 1)
            try:
                header = json.loads(header_bytes.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ActivationFrameRejected("header_invalid") from exc
            if not isinstance(header, dict) or set(header) != _HEADER_FIELDS:
                raise ActivationFrameRejected("header_invalid")
            if header["protocol"] != FRAME_PROTOCOL:
                raise ActivationFrameRejected("unsupported_protocol")
            frame_generation = header["generation"]
            if not isinstance(frame_generation, int) or isinstance(frame_generation, bool):
                raise ActivationFrameRejected("header_invalid")
            if frame_generation != self._generation:
                raise ActivationFrameRejected("generation_mismatch")
            frame_id = header["frame_id"]
            if not isinstance(frame_id, str):
                raise ActivationFrameRejected("header_invalid")
            if frame_id in self._seen:
                raise ActivationFrameRejected("replayed_frame")
            if len(self._seen) >= self._max_frames:
                raise ActivationFrameRejected("replay_fence_exhausted")
            frame_dtype = header["dtype"]
            if frame_dtype not in SUPPORTED_DTYPES:
                raise ActivationFrameRejected("unsupported_dtype")
            if frame_dtype != expect_dtype or frame_dtype != self._dtype:
                raise ActivationFrameRejected("wrong_dtype")
            shape = header["shape"]
            if not isinstance(shape, dict) or set(shape) != _SHAPE_FIELDS:
                raise ActivationFrameRejected("header_invalid")
            declared_shape = (
                shape["batch"],
                shape["sequence"],
                shape["hidden_size"],
            )
            if expect_shape is not None:
                expected = tuple(
                    int(item) for item in np.asarray(expect_shape, dtype=np.int64).ravel()
                )
                if expected != declared_shape:
                    raise ActivationFrameRejected("wrong_shape")
            declared_bytes = header["payload_bytes"]
            declared_digest = header["payload_digest"]
            if (
                not isinstance(declared_bytes, int)
                or isinstance(declared_bytes, bool)
                or declared_bytes != len(payload)
                or not isinstance(declared_digest, str)
                or DIGEST_RE.fullmatch(declared_digest) is None
                or declared_digest != "sha256:" + _sha256_hex(payload)
            ):
                raise ActivationFrameRejected("corrupted_frame")
            self._scratch = bytearray(payload)
            decoded = np.frombuffer(bytes(self._scratch), dtype=_PAYLOAD_DTYPES[frame_dtype])
            widened = (
                decoded.astype(np.float32)
                if frame_dtype == "float16"
                else decode_bfloat16(payload)
            )
            values = np.ascontiguousarray(
                widened.reshape(declared_shape), dtype=np.float32
            )
            self._release_scratch()
            if not bool(np.isfinite(values).all()):
                raise ActivationFrameRejected("non_finite_value")
        except ActivationFrameRejected as rejection:
            self._release_scratch()
            self._rejections[rejection.code] = self._rejections.get(rejection.code, 0) + 1
            raise
        finally:
            self._release_scratch()
        self._seen.append(frame_id)
        self._frames_decoded += 1
        self._decode_seconds += time.perf_counter() - started
        return values

    # -- counters --------------------------------------------------------
    def byte_reduction(self) -> dict[str, Any]:
        return {
            "bytes_before": self._bytes_before,
            "bytes_after": self._bytes_after,
            "samples": self._samples,
            "counter_digest": self._last_reduction_digest,
        }

    def byte_reduction_ratio(self) -> float:
        if self._samples == 0 or self._bytes_after == 0:
            raise ActivationFrameRejected("byte_reduction_not_measured")
        return self._bytes_before / self._bytes_after

    def rejection_counts(self) -> dict[str, int]:
        return dict(self._rejections)

    def projection(self) -> dict[str, Any]:
        """Privacy-reduced runtime projection: dtype, generations, bytes, counts."""

        return {
            "protocol": PROJECTION_PROTOCOL,
            "dtype": self._dtype,
            "generation": self._generation,
            "frames_encoded": self._frames_encoded,
            "frames_decoded": self._frames_decoded,
            "frames_rejected": int(sum(self._rejections.values())),
            "rejections": dict(self._rejections),
            "bytes_before": self._bytes_before,
            "bytes_after": self._bytes_after,
            "byte_reduction_ratio": round(self._bytes_before / self._bytes_after, 6)
            if self._bytes_after
            else 0.0,
            "encode_ms_total": round(self._encode_seconds * 1000.0, 6),
            "decode_ms_total": round(self._decode_seconds * 1000.0, 6),
            "outstanding_frames": len(self._seen),
        }

    # -- negative battery ------------------------------------------------
    def negative_battery(self, values: Any) -> dict[str, str]:
        """Exercise all seven negative classes and return their evidence digests.

        A class whose rejection does not happen raises ``battery_incomplete``: the
        battery reports measured rejections, never assumed ones.
        """

        baseline = np.ascontiguousarray(np.asarray(values, dtype=np.float32))
        if baseline.ndim != 3 or 0 in baseline.shape:
            raise ActivationFrameRejected("wrong_shape")
        shape = tuple(int(item) for item in baseline.shape)
        digests: dict[str, str] = {}

        digests["unsupported_dtype"] = self._expect_rejection(
            "unsupported_dtype",
            lambda: ActivationFrameCodec(dtype="float32", generation=self._generation),
        )

        def _non_finite_frame() -> object:
            poisoned = self._new_codec(sender_validates_finite=False).encode(
                _poisoned(baseline), frame_id="neg-non-finite"
            )
            return self._new_codec().decode(
                poisoned, expect_shape=shape, expect_dtype=self._dtype
            )

        digests["non_finite_value"] = self._expect_rejection(
            "non_finite_value", _non_finite_frame
        )

        def _wrong_shape() -> np.ndarray:
            frame = self._new_codec().encode(baseline, frame_id="neg-wrong-shape")
            return self._new_codec().decode(
                frame, expect_shape=(1, 1, 1), expect_dtype=self._dtype
            )

        digests["wrong_shape"] = self._expect_rejection("wrong_shape", _wrong_shape)

        def _wrong_dtype() -> np.ndarray:
            frame = self._new_codec().encode(baseline, frame_id="neg-wrong-dtype")
            other = "bfloat16" if self._dtype == "float16" else "float16"
            return self._new_codec(dtype=other).decode(
                frame, expect_shape=shape, expect_dtype=other
            )

        digests["wrong_dtype"] = self._expect_rejection("wrong_dtype", _wrong_dtype)

        def _stale_generation() -> np.ndarray:
            other_generation = (
                self._generation - 1 if self._generation > 1 else self._generation + 1
            )
            stale = ActivationFrameCodec(
                dtype=self._dtype, generation=other_generation
            )
            frame = stale.encode(baseline, frame_id="neg-stale-generation")
            return self._new_codec().decode(
                frame, expect_shape=shape, expect_dtype=self._dtype
            )

        digests["generation_mismatch"] = self._expect_rejection(
            "generation_mismatch", _stale_generation
        )

        def _replayed() -> np.ndarray:
            receiver = self._new_codec()
            frame = receiver.encode(baseline, frame_id="neg-replayed")
            receiver.decode(frame, expect_shape=shape, expect_dtype=self._dtype)
            return receiver.decode(frame, expect_shape=shape, expect_dtype=self._dtype)

        digests["replayed_frame"] = self._expect_rejection("replayed_frame", _replayed)

        def _corrupted() -> np.ndarray:
            receiver = self._new_codec()
            frame = bytearray(receiver.encode(baseline, frame_id="neg-corrupted"))
            frame[-1] ^= 0x01
            return receiver.decode(
                bytes(frame), expect_shape=shape, expect_dtype=self._dtype
            )

        digests["corrupted_frame"] = self._expect_rejection(
            "corrupted_frame", _corrupted
        )

        if set(digests) != set(REJECTION_CLASSES):
            raise ActivationFrameRejected("battery_incomplete")
        return digests

    def _new_codec(self, dtype: str | None = None, **kwargs: Any) -> "ActivationFrameCodec":
        return ActivationFrameCodec(
            dtype=dtype or self._dtype,
            generation=self._generation,
            max_frames=self._max_frames,
            max_bytes=self._max_bytes,
            **kwargs,
        )

    def _expect_rejection(self, name: str, thunk: Callable[[], Any]) -> str:
        try:
            thunk()
        except ActivationFrameRejected as rejection:
            if rejection.code != name:
                raise ActivationFrameRejected("battery_incomplete") from rejection
            self._rejections[name] = self._rejections.get(name, 0) + 1
            return record_digest(
                {
                    "negative_class": name,
                    "rejection_code": rejection.code,
                    "frame_protocol": FRAME_PROTOCOL,
                    "generation": self._generation,
                    "receiver_dtype": self._dtype,
                    "rejected": True,
                },
                "codec_negative_digest_noncanonical",
            )
        raise ActivationFrameRejected("battery_incomplete")

    def _reduction_digest(self) -> str:
        return record_digest(
            {
                "bytes_before": self._bytes_before,
                "bytes_after": self._bytes_after,
                "samples": self._samples,
                "frames_encoded": self._frames_encoded,
            },
            "codec_reduction_digest_noncanonical",
        )


def _poisoned(block: np.ndarray) -> np.ndarray:
    poisoned = np.array(block, dtype=np.float32, copy=True)
    poisoned.reshape(-1)[0] = np.float32("inf")
    return poisoned


def _sha256_hex(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def require_bytes(value: Any, code: str = "codec_frame_malformed") -> bytes:
    """Small helper so a caller can assert a returned frame is real bytes."""

    require(isinstance(value, bytes) and bool(value), code)
    return value


__all__ = [
    "ACCEPTANCE_COVERAGE",
    "ActivationFrameCodec",
    "ActivationFrameRejected",
    "BOUNDARY_CODES",
    "FORBIDDEN_PROJECTION_KEYS",
    "FRAME_PROTOCOL",
    "PROJECTION_PROTOCOL",
    "REJECTION_CLASSES",
    "SUPPORTED_DTYPES",
    "decode_bfloat16",
    "encode_bfloat16",
    "frame_projection_is_privacy_reduced",
    "require_bytes",
]
