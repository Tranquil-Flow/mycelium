# SPDX-License-Identifier: AGPL-3.0-only
"""Signed v2 stage manifests for a phone that runs real Qwen2 decoder layers.

A v1 mobile stage pack (``mycelium.pixel_stage_pack.v1``) carries its weights
inline, which caps it at the seed's 512 KiB inline limit: one real Qwen2.5-0.5B
layer is ~30 MB of bf16. A v2 manifest carries no weights. It pins everything
the phone needs to fetch, verify and run layers ``[start, end)``:

* the upstream model (``model_id``, ``resolved_commit``) and the source
  Safetensors file's SHA-256 and size;
* the architecture a decoder layer depends on (hidden, inner, heads, KV heads,
  head_dim, eps, rope theta, activation);
* the backend (``mobile-rust-qwen2``), the quantizer the phone runs on load
  (``numpy_rowwise_v1``, the bit-exact Rust port of
  ``numpy_runtime.quantize_qwen2_numpy_tensor``) and the activation dtype;
* every tensor of the range by name, source dtype, shape, byte size and the
  SHA-256 of its raw upstream little-endian bytes (the bytes the phone
  downloads and verifies before it quantizes them);
* optionally a golden: a deterministic input (``splitmix64_unit_f32_v1``) and
  the SHA-256 of the stage output the host build of the same Rust code
  produced, so the phone can prove on load that it computes bit-identically
  to the host (plan R1a).

``pack_digest`` is the SHA-256 of the canonical ASCII JSON of every other
field (same rule as v1). The seed signs the manifest with its member-channel
key as a ``mycelium.membership.signed_message.v1`` envelope, so a phone can
re-verify a manifest it persisted against the seed key its invitation pinned,
and the seed-signed route assignment binds the same ``pack_digest``.

This module is stdlib-only for verification (the seed imports it); building a
manifest from a Safetensors file reuses ``runtime_loader``'s fail-closed
header validation, imported lazily.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys
from typing import Any, BinaryIO, Mapping, NoReturn, Sequence

MANIFEST_PROTOCOL = "mycelium.mobile_stage_pack.v2"
GOLDEN_PROTOCOL = "mycelium.mobile_stage_golden.v1"
GOLDEN_GENERATOR = "splitmix64_unit_f32_v1"
SIGNED_MESSAGE_PROTOCOL = "mycelium.membership.signed_message.v1"
BACKEND = "mobile-rust-qwen2"
QUANTIZER = "numpy_rowwise_v1"
ACTIVATION_DTYPE = "float32"
SOURCE_FILE = "model.safetensors"
#: Same bound as the seed's inline stage-pack cap; a 24-layer manifest is ~60 KB.
MAX_MANIFEST_BYTES = 512 * 1024
MAX_LAYERS_PER_STAGE = 64
MAX_HIDDEN = 8192
MAX_INNER = 32768
MAX_TENSOR_BYTES = 1 << 30
MAX_GOLDEN_SEQ = 64
MAX_SAFE_INTEGER = (1 << 53) - 1
_HASH_CHUNK = 1 << 20

MATRIX_SUFFIXES = (
    "self_attn.q_proj.weight",
    "self_attn.k_proj.weight",
    "self_attn.v_proj.weight",
    "self_attn.o_proj.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
)
VECTOR_SUFFIXES = (
    "input_layernorm.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.bias",
    "post_attention_layernorm.weight",
)
DTYPE_BYTES = {"BF16": 2, "F32": 4}

_MANIFEST_FIELDS = frozenset(
    {
        "protocol",
        "stage_id",
        "model_id",
        "resolved_commit",
        "source",
        "architecture",
        "start_layer",
        "end_layer_exclusive",
        "backend",
        "quantizer",
        "activation_dtype",
        "tensors",
        "golden",
        "route_ready",
        "pack_digest",
    }
)
_SOURCE_FIELDS = frozenset({"file", "sha256", "size_bytes"})
_ARCH_FIELDS = frozenset(
    {
        "model_type",
        "hidden_act",
        "hidden_size",
        "intermediate_size",
        "num_attention_heads",
        "num_key_value_heads",
        "head_dim",
        "num_hidden_layers",
        "rms_norm_eps",
        "rope_theta",
    }
)
_TENSOR_FIELDS = frozenset({"name", "dtype", "shape", "size_bytes", "sha256"})
_GOLDEN_FIELDS = frozenset({"protocol", "generator", "seed", "seq", "output_digest"})
_ENVELOPE_FIELDS = frozenset({"protocol", "message", "signature", "verification_key"})
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class StageManifestError(ValueError):
    """Fail-closed manifest validation error with a stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject(code: str) -> NoReturn:
    raise StageManifestError(code)


def canonical_ascii(value: Any) -> bytes:
    """``json.dumps(sort_keys, compact, ensure_ascii=True, allow_nan=False)``."""

    try:
        return json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _reject("stage_manifest_noncanonical")


def manifest_digest(unsigned: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_ascii(dict(unsigned))).hexdigest()


def tensor_names(start_layer: int, end_layer_exclusive: int) -> list[str]:
    """Every upstream tensor a layer range needs, sorted (the manifest order)."""

    return sorted(
        f"model.layers.{layer}.{suffix}"
        for layer in range(start_layer, end_layer_exclusive)
        for suffix in MATRIX_SUFFIXES + VECTOR_SUFFIXES
    )


def expected_shapes(architecture: Mapping[str, Any], layer: int) -> dict[str, list[int]]:
    h = architecture["hidden_size"]
    i = architecture["intermediate_size"]
    kv = architecture["num_key_value_heads"] * architecture["head_dim"]
    n = f"model.layers.{layer}."
    return {
        n + "input_layernorm.weight": [h],
        n + "self_attn.q_proj.weight": [h, h],
        n + "self_attn.q_proj.bias": [h],
        n + "self_attn.k_proj.weight": [kv, h],
        n + "self_attn.k_proj.bias": [kv],
        n + "self_attn.v_proj.weight": [kv, h],
        n + "self_attn.v_proj.bias": [kv],
        n + "self_attn.o_proj.weight": [h, h],
        n + "post_attention_layernorm.weight": [h],
        n + "mlp.gate_proj.weight": [i, h],
        n + "mlp.up_proj.weight": [i, h],
        n + "mlp.down_proj.weight": [h, i],
    }


def _int(value: Any, code: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _reject(code)
    return value


def _positive_float(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value) or value <= 0:
        _reject(code)
    return value


def _string(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _reject(code)
    return value


def architecture_from_hf_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The decoder-layer fields of a Hugging Face Qwen2 ``config.json``."""

    if not isinstance(config, Mapping):
        _reject("stage_manifest_architecture_invalid")
    if config.get("model_type") != "qwen2":
        _reject("stage_manifest_model_type_unsupported")
    if config.get("hidden_act", "silu") != "silu":
        _reject("stage_manifest_activation_unsupported")
    hidden = _int(config.get("hidden_size"), "stage_manifest_architecture_invalid", minimum=1)
    heads = _int(config.get("num_attention_heads"), "stage_manifest_architecture_invalid", minimum=1)
    architecture = {
        "model_type": "qwen2",
        "hidden_act": "silu",
        "hidden_size": hidden,
        "intermediate_size": config.get("intermediate_size"),
        "num_attention_heads": heads,
        "num_key_value_heads": config.get("num_key_value_heads"),
        "head_dim": hidden // heads if hidden % heads == 0 else 0,
        "num_hidden_layers": config.get("num_hidden_layers"),
        "rms_norm_eps": float(config.get("rms_norm_eps", 0.0)),
        "rope_theta": float(config.get("rope_theta", 0.0)),
    }
    return _verify_architecture(architecture)


def _verify_architecture(value: Any) -> dict[str, Any]:
    code = "stage_manifest_architecture_invalid"
    if not isinstance(value, Mapping) or set(value) != _ARCH_FIELDS:
        _reject(code)
    if value["model_type"] != "qwen2" or value["hidden_act"] != "silu":
        _reject(code)
    hidden = _int(value["hidden_size"], code, minimum=1, maximum=MAX_HIDDEN)
    inner = _int(value["intermediate_size"], code, minimum=1, maximum=MAX_INNER)
    heads = _int(value["num_attention_heads"], code, minimum=1, maximum=hidden)
    kv_heads = _int(value["num_key_value_heads"], code, minimum=1, maximum=heads)
    head_dim = _int(value["head_dim"], code, minimum=2, maximum=hidden)
    layers = _int(value["num_hidden_layers"], code, minimum=1, maximum=4096)
    eps = _positive_float(value["rms_norm_eps"], code)
    theta = _positive_float(value["rope_theta"], code)
    if heads % kv_heads or head_dim % 2 or head_dim * heads != hidden or theta <= 1.0:
        _reject(code)
    return {
        "model_type": "qwen2",
        "hidden_act": "silu",
        "hidden_size": hidden,
        "intermediate_size": inner,
        "num_attention_heads": heads,
        "num_key_value_heads": kv_heads,
        "head_dim": head_dim,
        "num_hidden_layers": layers,
        "rms_norm_eps": eps,
        "rope_theta": theta,
    }


def _verify_golden(value: Any, architecture: Mapping[str, Any]) -> dict[str, Any] | None:
    if value is None:
        return None
    code = "stage_manifest_golden_invalid"
    if not isinstance(value, Mapping) or set(value) != _GOLDEN_FIELDS:
        _reject(code)
    if value["protocol"] != GOLDEN_PROTOCOL or value["generator"] != GOLDEN_GENERATOR:
        _reject(code)
    return {
        "protocol": GOLDEN_PROTOCOL,
        "generator": GOLDEN_GENERATOR,
        "seed": _int(value["seed"], code),
        "seq": _int(value["seq"], code, minimum=1, maximum=MAX_GOLDEN_SEQ),
        "output_digest": _string(value["output_digest"], _DIGEST_RE, code),
    }


def verify_stage_manifest(document: Any) -> dict[str, Any]:
    """Strictly validate one v2 manifest and recompute its ``pack_digest``."""

    if not isinstance(document, Mapping) or set(document) != _MANIFEST_FIELDS:
        _reject("stage_manifest_fields_invalid")
    if document["protocol"] != MANIFEST_PROTOCOL:
        _reject("stage_manifest_protocol_invalid")
    raw = canonical_ascii(dict(document))
    if len(raw) > MAX_MANIFEST_BYTES:
        _reject("stage_manifest_too_large")
    unsigned = {key: item for key, item in document.items() if key != "pack_digest"}
    if document["pack_digest"] != manifest_digest(unsigned):
        _reject("stage_manifest_digest_mismatch")
    _string(document["stage_id"], _ID_RE, "stage_manifest_identity_invalid")
    _string(document["model_id"], _ID_RE, "stage_manifest_identity_invalid")
    _string(document["resolved_commit"], _COMMIT_RE, "stage_manifest_identity_invalid")
    if document["backend"] != BACKEND:
        _reject("stage_manifest_backend_unsupported")
    if document["quantizer"] != QUANTIZER:
        _reject("stage_manifest_quantizer_unsupported")
    if document["activation_dtype"] != ACTIVATION_DTYPE:
        _reject("stage_manifest_activation_dtype_unsupported")
    if document["route_ready"] is not False:
        _reject("stage_manifest_route_ready_invalid")
    source = document["source"]
    if (
        not isinstance(source, Mapping)
        or set(source) != _SOURCE_FIELDS
        or source["file"] != SOURCE_FILE
    ):
        _reject("stage_manifest_source_invalid")
    _string(source["sha256"], _DIGEST_RE, "stage_manifest_source_invalid")
    _int(source["size_bytes"], "stage_manifest_source_invalid", minimum=9)
    architecture = _verify_architecture(document["architecture"])
    if architecture != document["architecture"]:
        _reject("stage_manifest_architecture_invalid")
    start = _int(document["start_layer"], "stage_manifest_layer_range_invalid")
    end = _int(document["end_layer_exclusive"], "stage_manifest_layer_range_invalid")
    if not start < end <= architecture["num_hidden_layers"] or end - start > MAX_LAYERS_PER_STAGE:
        _reject("stage_manifest_layer_range_invalid")
    tensors = document["tensors"]
    names = tensor_names(start, end)
    if not isinstance(tensors, list) or len(tensors) != len(names):
        _reject("stage_manifest_tensors_invalid")
    shapes: dict[str, list[int]] = {}
    for layer in range(start, end):
        shapes.update(expected_shapes(architecture, layer))
    for expected_name, tensor in zip(names, tensors):
        if not isinstance(tensor, Mapping) or set(tensor) != _TENSOR_FIELDS:
            _reject("stage_manifest_tensors_invalid")
        if tensor["name"] != expected_name:
            _reject("stage_manifest_tensors_invalid")
        if tensor["dtype"] not in DTYPE_BYTES:
            _reject("stage_manifest_tensor_dtype_unsupported")
        if tensor["shape"] != shapes[expected_name]:
            _reject("stage_manifest_tensor_shape_invalid")
        size = _int(tensor["size_bytes"], "stage_manifest_tensors_invalid", minimum=1, maximum=MAX_TENSOR_BYTES)
        if size != math.prod(tensor["shape"]) * DTYPE_BYTES[tensor["dtype"]]:
            _reject("stage_manifest_tensor_size_invalid")
        _string(tensor["sha256"], _DIGEST_RE, "stage_manifest_tensors_invalid")
    golden = _verify_golden(document["golden"], architecture)
    if golden != document["golden"]:
        _reject("stage_manifest_golden_invalid")
    return json.loads(raw)


def build_stage_manifest(
    *,
    stage_id: str,
    model_id: str,
    resolved_commit: str,
    source_sha256: str,
    source_size_bytes: int,
    architecture: Mapping[str, Any],
    start_layer: int,
    end_layer_exclusive: int,
    tensors: Sequence[Mapping[str, Any]],
    golden: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble, digest and verify one v2 manifest from already-known parts."""

    unsigned = {
        "protocol": MANIFEST_PROTOCOL,
        "stage_id": stage_id,
        "model_id": model_id,
        "resolved_commit": resolved_commit,
        "source": {"file": SOURCE_FILE, "sha256": source_sha256, "size_bytes": source_size_bytes},
        "architecture": dict(architecture),
        "start_layer": start_layer,
        "end_layer_exclusive": end_layer_exclusive,
        "backend": BACKEND,
        "quantizer": QUANTIZER,
        "activation_dtype": ACTIVATION_DTYPE,
        "tensors": sorted((dict(t) for t in tensors), key=lambda t: t["name"]),
        "golden": None if golden is None else dict(golden),
        "route_ready": False,
    }
    return verify_stage_manifest({**unsigned, "pack_digest": manifest_digest(unsigned)})


def _read_header(handle: BinaryIO, path: str) -> tuple[dict[str, dict[str, Any]], int]:
    # Same fail-closed validation the serving loader applies (duplicates,
    # dtype/shape/length, overlap, gap and trailing data).
    from runtime_loader import _validate_safetensors_header  # noqa: PLC0415 - numpy only when building

    return _validate_safetensors_header(handle, path)


def _sha256_range(handle: BinaryIO, start: int, length: int) -> str:
    handle.seek(start)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = handle.read(min(_HASH_CHUNK, remaining))
        if not chunk:
            _reject("stage_manifest_source_truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    return "sha256:" + digest.hexdigest()


def _sha256_file(handle: BinaryIO) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def build_stage_manifest_from_safetensors(
    safetensors_path: Path,
    hf_config: Mapping[str, Any],
    *,
    stage_id: str,
    model_id: str,
    resolved_commit: str,
    start_layer: int,
    end_layer_exclusive: int,
    golden: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Hash the raw upstream bytes of every tensor of ``[start, end)``."""

    architecture = architecture_from_hf_config(hf_config)
    path = Path(safetensors_path)
    with path.open("rb") as handle:
        header, data_start = _read_header(handle, str(path))
        size = handle.seek(0, 2)
        entries = []
        for name in tensor_names(start_layer, end_layer_exclusive):
            entry = header.get(name)
            if entry is None:
                _reject("stage_manifest_source_tensor_missing")
            begin, end = entry["data_offsets"]
            entries.append(
                {
                    "name": name,
                    "dtype": entry["dtype"],
                    "shape": list(entry["shape"]),
                    "size_bytes": end - begin,
                    "sha256": _sha256_range(handle, data_start + begin, end - begin),
                }
            )
        source_sha256 = _sha256_file(handle)
    return build_stage_manifest(
        stage_id=stage_id,
        model_id=model_id,
        resolved_commit=resolved_commit,
        source_sha256=source_sha256,
        source_size_bytes=size,
        architecture=architecture,
        start_layer=start_layer,
        end_layer_exclusive=end_layer_exclusive,
        tensors=entries,
        golden=golden,
    )


def export_tensor_objects(
    safetensors_path: Path, manifest: Mapping[str, Any], out_dir: Path
) -> list[Path]:
    """Write each tensor's raw bytes to ``out_dir/<sha256 hex>`` (content-addressed).

    These are the objects an artifact server hands out by digest and the phone
    stores under the same names. Every written object is re-hashed.
    """

    verified = verify_stage_manifest(manifest)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    path = Path(safetensors_path)
    with path.open("rb") as handle:
        header, data_start = _read_header(handle, str(path))
        for tensor in verified["tensors"]:
            entry = header.get(tensor["name"])
            if entry is None:
                _reject("stage_manifest_source_tensor_missing")
            begin, end = entry["data_offsets"]
            if end - begin != tensor["size_bytes"]:
                _reject("stage_manifest_source_mismatch")
            handle.seek(data_start + begin)
            data = handle.read(end - begin)
            if "sha256:" + hashlib.sha256(data).hexdigest() != tensor["sha256"]:
                _reject("stage_manifest_source_mismatch")
            target = out / tensor["sha256"].removeprefix("sha256:")
            target.write_bytes(data)
            written.append(target)
    return written


def sign_stage_manifest(manifest: Mapping[str, Any], signer: Any) -> dict[str, Any]:
    """Seed-sign a verified manifest as a member-channel signed message."""

    verified = verify_stage_manifest(manifest)
    return {
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": verified,
        "signature": signer.sign(verified),
        "verification_key": signer.public_key_record(),
    }


def verify_signed_stage_manifest(
    envelope: Any, pinned_key_record: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify a seed-signed manifest against the pinned seed key; return the manifest."""

    from mycelium_qualification.evidence import canonical_json_bytes  # noqa: PLC0415
    from mycelium_qualification.signing import build_ed25519_verifier  # noqa: PLC0415

    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS:
        _reject("stage_manifest_envelope_invalid")
    if envelope["protocol"] != SIGNED_MESSAGE_PROTOCOL:
        _reject("stage_manifest_envelope_invalid")
    if dict(envelope["verification_key"]) != dict(pinned_key_record):
        _reject("stage_manifest_signer_mismatch")
    manifest = verify_stage_manifest(envelope["message"])
    try:
        verify = build_ed25519_verifier([dict(pinned_key_record)])
        valid = verify(canonical_json_bytes(manifest), dict(envelope["signature"]))
    except Exception:  # noqa: BLE001 - any verifier failure is a refusal
        valid = False
    if not valid:
        _reject("stage_manifest_signature_invalid")
    return manifest


def is_signed_stage_manifest(document: Any) -> bool:
    return (
        isinstance(document, Mapping)
        and document.get("protocol") == SIGNED_MESSAGE_PROTOCOL
        and isinstance(document.get("message"), Mapping)
        and document["message"].get("protocol") == MANIFEST_PROTOCOL
    )


_MASK = (1 << 64) - 1


def golden_input_f32(seed: int, seq: int, hidden: int) -> bytes:
    """``splitmix64_unit_f32_v1``: ``seq*hidden`` little-endian f32 in [-1, 1).

    Each value is ``((z >> 40) - 2**23) / 2**23`` of one splitmix64 output, an
    exact f32 (24 significant bits), so Python and Rust agree bit for bit.
    """

    state = seed & _MASK
    out = bytearray()
    for _ in range(seq * hidden):
        state = (state + 0x9E3779B97F4A7C15) & _MASK
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        z ^= z >> 31
        out += struct.pack("<f", ((z >> 40) - (1 << 23)) / 8388608.0)
    return bytes(out)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_mobile.qwen_stage_manifest",
        description="Build or export a v2 mobile Qwen2 stage manifest.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--snapshot", required=True, type=Path, help="dir with model.safetensors + config.json")
    build.add_argument("--model-id", required=True)
    build.add_argument("--resolved-commit", required=True)
    build.add_argument("--stage-id", required=True)
    build.add_argument("--layers", required=True, help="START:END (end exclusive)")
    build.add_argument("--golden", type=Path, help="golden JSON from the host Rust executor")
    build.add_argument("--out", required=True, type=Path)
    export = sub.add_parser("export")
    export.add_argument("--snapshot", required=True, type=Path)
    export.add_argument("--manifest", required=True, type=Path)
    export.add_argument("--out-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            start, _, end = args.layers.partition(":")
            manifest = build_stage_manifest_from_safetensors(
                args.snapshot / SOURCE_FILE,
                json.loads((args.snapshot / "config.json").read_text()),
                stage_id=args.stage_id,
                model_id=args.model_id,
                resolved_commit=args.resolved_commit,
                start_layer=int(start),
                end_layer_exclusive=int(end),
                golden=json.loads(args.golden.read_text()) if args.golden else None,
            )
            args.out.write_bytes(canonical_ascii(manifest))
            print(json.dumps({"pack_digest": manifest["pack_digest"], "tensors": len(manifest["tensors"])}))
        else:
            manifest = json.loads(args.manifest.read_text())
            if is_signed_stage_manifest(manifest):
                manifest = manifest["message"]
            written = export_tensor_objects(args.snapshot / SOURCE_FILE, manifest, args.out_dir)
            print(json.dumps({"objects": len(written)}))
    except StageManifestError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
