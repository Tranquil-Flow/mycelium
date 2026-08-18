"""Read-only local model catalog and pre-provisioning feasibility for M17."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import struct
from typing import Any, Iterable, Mapping, Sequence

from model_adapters import adapter_for_config
from mycelium_layer_planner.contracts import (
    ModelIdentity,
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
from weight_quantization import rowwise_int8_streaming_transient_bytes


MODEL_CATALOG_PROTOCOL = "mycelium.model_catalog.v1"
MODEL_FEASIBILITY_PROTOCOL = "mycelium.model_feasibility.v1"
MODEL_OPERATION_PROTOCOL = "mycelium.model_operation.v1"
MODEL_LIFECYCLE_PROTOCOL = "mycelium.model_lifecycle.v1"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ENTRIES = 256
_MAX_REASONS = 32
_MAX_REJECTED_CANDIDATES = 128
_MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    payload = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _bounded_reason(reason: str) -> str:
    return reason[:512]


def _require_digest(name: str, value: str) -> None:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be sha256:<64 hex>")


def _model_id_from_cache_name(name: str) -> str:
    if not name.startswith("models--"):
        raise ValueError("Hugging Face model cache directory must start with models--")
    parts = name.removeprefix("models--").split("--")
    if not parts or any(not part for part in parts):
        raise ValueError("invalid Hugging Face model cache directory")
    return "/".join(parts)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _blob_digest(path: Path) -> str | None:
    try:
        resolved_name = path.resolve(strict=True).name
    except OSError:
        return None
    if _SHA256_RE.fullmatch(resolved_name):
        return f"sha256:{resolved_name}"
    return None


def _dtype_bytes(config: dict[str, Any]) -> int | None:
    dtype = config.get("torch_dtype") or config.get("dtype")
    return {
        "float32": 4,
        "fp32": 4,
        "float16": 2,
        "fp16": 2,
        "bfloat16": 2,
        "bf16": 2,
        "int8": 1,
        "uint8": 1,
    }.get(dtype)


def _quantization(config: dict[str, Any]) -> str:
    quantization = config.get("quantization_config")
    if isinstance(quantization, dict):
        method = quantization.get("quant_method") or quantization.get("method")
        bits = quantization.get("bits")
        if isinstance(method, str) and method:
            return f"{method}-{bits}bit" if isinstance(bits, int) else method
    dtype = config.get("torch_dtype") or config.get("dtype")
    return dtype if isinstance(dtype, str) and dtype else "unknown"


def _head_dim(config: dict[str, Any]) -> int | None:
    explicit = config.get("head_dim")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    hidden = config.get("hidden_size")
    attention_heads = config.get("num_attention_heads")
    if (
        isinstance(hidden, int)
        and not isinstance(hidden, bool)
        and hidden > 0
        and isinstance(attention_heads, int)
        and not isinstance(attention_heads, bool)
        and attention_heads > 0
        and hidden % attention_heads == 0
    ):
        return hidden // attention_heads
    return None


def _safetensors_header(path: Path) -> dict[str, dict[str, Any]]:
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise ValueError("truncated safetensors prefix")
        header_length = struct.unpack("<Q", prefix)[0]
        if (
            header_length <= 0
            or header_length > _MAX_SAFETENSORS_HEADER_BYTES
            or header_length > file_size - 8
        ):
            raise ValueError("invalid safetensors header length")
        raw = handle.read(header_length)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid safetensors header JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("safetensors header must be an object")
    value.pop("__metadata__", None)
    header: dict[str, dict[str, Any]] = {}
    for name, record in value.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise ValueError("invalid safetensors tensor record")
        offsets = record.get("data_offsets")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in offsets
            )
            or offsets[0] < 0
            or offsets[1] <= offsets[0]
        ):
            raise ValueError(f"invalid safetensors offsets for {name}")
        header[name] = record
    if not header:
        raise ValueError("safetensors file has no tensors")
    return header


def _tensor_accounting(
    *,
    snapshot: Path,
    required_weight_files: Sequence[str],
    weight_map: dict[str, str],
    config: dict[str, Any],
) -> tuple[tuple[int, ...], int, int, int, tuple[str, ...]]:
    try:
        adapter = adapter_for_config(config)
        adapter.validate_architectures(config)
        num_layers = adapter.layer_count(config)
    except ValueError:
        return (), 0, 0, 0, ()
    headers: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for filename in required_weight_files:
        path = snapshot / filename
        if not path.is_file():
            continue
        try:
            headers[filename] = _safetensors_header(path)
        except (OSError, ValueError) as exc:
            errors.append(f"invalid_safetensors_header:{filename}:{exc}")
    if not weight_map and len(headers) == 1:
        filename, header = next(iter(headers.items()))
        weight_map = {name: filename for name in header}
    if not weight_map or len(headers) != len(required_weight_files):
        return (), 0, 0, 0, tuple(errors)
    header_names = {name for header in headers.values() for name in header}
    if set(weight_map) != header_names:
        errors.append("checkpoint_index_header_mismatch")
        return (), 0, 0, 0, tuple(errors)

    layer_bytes = [0] * num_layers
    component_bytes = {name: 0 for name in adapter.components if name != "decoder"}
    other_static_bytes = 0
    for tensor_name, filename in weight_map.items():
        record = headers[filename][tensor_name]
        start, end = record["data_offsets"]
        size = end - start
        matched = False
        for layer in range(num_layers):
            prefixes = tuple(
                template.format(layer=layer)
                for template in (
                    adapter.block_prefix_template,
                    *adapter.alternate_block_prefix_templates,
                )
            )
            if tensor_name.startswith(prefixes):
                layer_bytes[layer] += size
                matched = True
                break
        if matched:
            continue
        for component, prefixes in adapter.components.items():
            if component == "decoder":
                continue
            if any(
                "{layer}" not in prefix and tensor_name.startswith(prefix)
                for prefix in prefixes
            ):
                component_bytes[component] += size
                matched = True
                break
        if not matched:
            other_static_bytes += size
    if any(size <= 0 for size in layer_bytes):
        errors.append("incomplete_layer_tensor_accounting")
        return (), 0, 0, 0, tuple(errors)
    entry_static = component_bytes.get("input_embedding", 0)
    final_static = (
        sum(size for name, size in component_bytes.items() if name != "input_embedding")
        + other_static_bytes
    )
    if config.get("tie_word_embeddings") is True:
        final_static += entry_static
    return (
        tuple(layer_bytes),
        entry_static,
        final_static,
        other_static_bytes,
        tuple(errors),
    )


def _int8_runtime_accounting(
    *,
    snapshot: Path,
    required_weight_files: Sequence[str],
    weight_map: dict[str, str],
    config: dict[str, Any],
) -> tuple[
    tuple[int, ...],
    int,
    int,
    tuple[int, ...],
    int,
    int,
    tuple[int, ...],
    int,
    int,
]:
    """Return exact resident and transient-load bytes for rowwise int8 serving."""

    try:
        adapter = adapter_for_config(config)
        adapter.validate_architectures(config)
        num_layers = adapter.layer_count(config)
    except ValueError:
        return (), 0, 0, (), 0, 0, (), 0, 0
    headers = {
        filename: _safetensors_header(snapshot / filename)
        for filename in required_weight_files
        if (snapshot / filename).is_file()
    }
    if not weight_map and len(headers) == 1:
        filename, header = next(iter(headers.items()))
        weight_map = {name: filename for name in header}
    if not weight_map or len(headers) != len(required_weight_files):
        return (), 0, 0, (), 0, 0, (), 0, 0
    resident_layers = [0] * num_layers
    load_layers = [0] * num_layers
    streaming_transient_layers = [0] * num_layers
    resident_components = {name: 0 for name in adapter.components if name != "decoder"}
    load_components = dict(resident_components)
    streaming_transient_components = dict(resident_components)
    resident_other = 0
    load_other = 0
    streaming_transient_other = 0
    for tensor_name, filename in weight_map.items():
        record = headers[filename][tensor_name]
        shape = record.get("shape")
        if (
            not isinstance(shape, list)
            or not shape
            or not all(type(item) is int and item > 0 for item in shape)
        ):
            return (), 0, 0, (), 0, 0, (), 0, 0
        elements = 1
        for extent in shape:
            elements *= extent
        unquantized_bytes = elements * 4
        resident_bytes = (
            elements + shape[0] * 4
            if len(shape) == 2 and tensor_name.endswith(".weight")
            else unquantized_bytes
        )
        load_bytes = unquantized_bytes + resident_bytes
        streaming_transient_bytes = rowwise_int8_streaming_transient_bytes(
            tuple(shape),
            quantized_matrix=(len(shape) == 2 and tensor_name.endswith(".weight")),
        )
        matched = False
        for layer in range(num_layers):
            prefixes = tuple(
                template.format(layer=layer)
                for template in (
                    adapter.block_prefix_template,
                    *adapter.alternate_block_prefix_templates,
                )
            )
            if tensor_name.startswith(prefixes):
                resident_layers[layer] += resident_bytes
                load_layers[layer] += load_bytes
                streaming_transient_layers[layer] = max(
                    streaming_transient_layers[layer],
                    streaming_transient_bytes,
                )
                matched = True
                break
        if matched:
            continue
        for component, prefixes in adapter.components.items():
            if component == "decoder":
                continue
            if any(
                "{layer}" not in prefix and tensor_name.startswith(prefix)
                for prefix in prefixes
            ):
                resident_components[component] += resident_bytes
                load_components[component] += load_bytes
                streaming_transient_components[component] = max(
                    streaming_transient_components[component],
                    streaming_transient_bytes,
                )
                matched = True
                break
        if not matched:
            resident_other += resident_bytes
            load_other += load_bytes
            streaming_transient_other = max(
                streaming_transient_other,
                streaming_transient_bytes,
            )
    if any(size <= 0 for size in resident_layers) or any(
        size <= 0 for size in load_layers
    ):
        return (), 0, 0, (), 0, 0, (), 0, 0
    resident_entry = resident_components.get("input_embedding", 0)
    load_entry = load_components.get("input_embedding", 0)
    resident_final = (
        sum(
            size
            for name, size in resident_components.items()
            if name != "input_embedding"
        )
        + resident_other
    )
    load_final = (
        sum(size for name, size in load_components.items() if name != "input_embedding")
        + load_other
    )
    streaming_transient_entry = streaming_transient_components.get("input_embedding", 0)
    streaming_transient_final = max(
        (
            size
            for name, size in streaming_transient_components.items()
            if name != "input_embedding"
        ),
        default=0,
    )
    streaming_transient_final = max(
        streaming_transient_final,
        streaming_transient_other,
    )
    if config.get("tie_word_embeddings") is True:
        resident_final += resident_entry
        load_final += load_entry
        streaming_transient_final = max(
            streaming_transient_final,
            streaming_transient_entry,
        )
    return (
        tuple(resident_layers),
        resident_entry,
        resident_final,
        tuple(load_layers),
        load_entry,
        load_final,
        tuple(streaming_transient_layers),
        streaming_transient_entry,
        streaming_transient_final,
    )


@dataclass(frozen=True)
class LocalModelEntry:
    """Internal catalog entry; ``snapshot_path`` never enters projections."""

    model_id: str
    revision: str
    snapshot_path: Path
    state: str
    model_type: str
    architecture: str
    adapter_id: str | None
    checkpoint_format: str
    quantization: str
    num_layers: int | None
    hidden_size: int | None
    kv_heads: int | None
    head_dim: int | None
    dtype_bytes: int | None
    max_context_tokens: int | None
    weight_bytes: int
    layer_weight_bytes: tuple[int, ...]
    entry_static_bytes: int
    final_static_bytes: int
    other_static_bytes: int
    int8_layer_weight_bytes: tuple[int, ...]
    int8_entry_static_bytes: int
    int8_final_static_bytes: int
    int8_load_layer_bytes: tuple[int, ...]
    int8_load_entry_static_bytes: int
    int8_load_final_static_bytes: int
    int8_streaming_transient_layer_bytes: tuple[int, ...]
    int8_streaming_transient_entry_static_bytes: int
    int8_streaming_transient_final_static_bytes: int
    required_files: tuple[str, ...]
    present_files: tuple[str, ...]
    file_records: tuple[dict[str, object], ...]
    reasons: tuple[str, ...]
    artifact_digest: str

    @property
    def complete(self) -> bool:
        return self.state != "incomplete"

    @property
    def compatible(self) -> bool:
        return self.state == "compatible"

    def projection(self) -> dict[str, object]:
        representation = {
            "quantization": "int8-weight-only",
            "quantizer": "mycelium.rowwise_symmetric_int8.v1",
            "runtime_dtype": "float32",
            "resident_weight_bytes": (
                sum(self.int8_layer_weight_bytes)
                + self.int8_entry_static_bytes
                + self.int8_final_static_bytes
            ),
            "load_peak_weight_bytes": (
                sum(self.int8_load_layer_bytes)
                + self.int8_load_entry_static_bytes
                + self.int8_load_final_static_bytes
            ),
            "preparation_required": True,
        }
        representation["representation_digest"] = _digest(
            {"artifact_digest": self.artifact_digest, **representation}
        )
        return {
            "model_id": self.model_id,
            "revision": self.revision,
            "state": self.state,
            "model_type": self.model_type,
            "architecture": self.architecture,
            "adapter_id": self.adapter_id,
            "checkpoint_format": self.checkpoint_format,
            "quantization": self.quantization,
            "num_layers": self.num_layers,
            "hidden_size": self.hidden_size,
            "kv_heads": self.kv_heads,
            "head_dim": self.head_dim,
            "dtype_bytes": self.dtype_bytes,
            "max_context_tokens": self.max_context_tokens,
            "weight_bytes": self.weight_bytes,
            "layer_weight_bytes": list(self.layer_weight_bytes),
            "entry_static_bytes": self.entry_static_bytes,
            "final_static_bytes": self.final_static_bytes,
            "other_static_bytes": self.other_static_bytes,
            "serving_representations": [representation]
            if self.int8_layer_weight_bytes
            else [],
            "exact_tensor_accounting": bool(self.layer_weight_bytes),
            "required_file_count": len(self.required_files),
            "present_file_count": len(self.present_files),
            "files": [dict(record) for record in self.file_records],
            "reasons": list(self.reasons),
            "artifact_digest": self.artifact_digest,
            "route_ready": False,
            "qualification_evaluated": False,
        }


@dataclass(frozen=True)
class NodeFeasibilityEvidence:
    """Fresh, signed, privacy-reduced resources for one candidate stage host."""

    node_id: str
    observation_digest: str
    signature_digest: str
    observed_at_unix_ms: int
    valid_until_unix_ms: int
    backend: str
    supported_architectures: tuple[str, ...]
    supported_dtypes: tuple[str, ...]
    supported_quantizations: tuple[str, ...]
    supported_decode_modes: tuple[str, ...]
    runtime_build_digest: str
    available_memory_bytes: int
    rss_bytes: int
    swap_used_bytes: int
    disk_free_bytes: int
    cached_content_digests: tuple[str, ...] = ()
    thermal_state: str | None = None
    power_state: str | None = None
    decode_modes_by_architecture: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.node_id or self.node_id != self.node_id.strip():
            raise ValueError("node feasibility evidence requires a node_id")
        for name in ("observation_digest", "signature_digest", "runtime_build_digest"):
            _require_digest(name, getattr(self, name))
        if (
            type(self.observed_at_unix_ms) is not int
            or type(self.valid_until_unix_ms) is not int
            or self.observed_at_unix_ms <= 0
            or self.valid_until_unix_ms <= self.observed_at_unix_ms
        ):
            raise ValueError("node feasibility evidence freshness is invalid")
        if not self.backend or self.backend != self.backend.strip():
            raise ValueError("node feasibility evidence backend is invalid")
        for name in (
            "supported_architectures",
            "supported_dtypes",
            "supported_quantizations",
            "supported_decode_modes",
        ):
            values = getattr(self, name)
            if (
                not values
                or len(values) != len(set(values))
                or any(not value or value != value.strip() for value in values)
            ):
                raise ValueError(f"node feasibility evidence {name} is invalid")
        for name in (
            "available_memory_bytes",
            "rss_bytes",
            "swap_used_bytes",
            "disk_free_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"node feasibility evidence {name} is invalid")
        if len(self.cached_content_digests) != len(set(self.cached_content_digests)):
            raise ValueError("duplicate cached content digest")
        for digest in self.cached_content_digests:
            _require_digest("cached_content_digest", digest)
        for name in ("thermal_state", "power_state"):
            value = getattr(self, name)
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"node feasibility evidence {name} is invalid")
        if not isinstance(self.decode_modes_by_architecture, Mapping):
            raise ValueError("node feasibility architecture decode modes are invalid")
        for architecture, modes in self.decode_modes_by_architecture.items():
            if (
                not isinstance(architecture, str)
                or not architecture
                or architecture != architecture.strip()
                or not isinstance(modes, tuple)
                or not modes
                or len(modes) != len(set(modes))
                or any(not mode or mode != mode.strip() for mode in modes)
                or any(mode not in self.supported_decode_modes for mode in modes)
            ):
                raise ValueError(
                    "node feasibility architecture decode modes are invalid"
                )

    def projection(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "observation_digest": self.observation_digest,
            "signature_digest": self.signature_digest,
            "observed_at_unix_ms": self.observed_at_unix_ms,
            "valid_until_unix_ms": self.valid_until_unix_ms,
            "backend": self.backend,
            "supported_architectures": list(self.supported_architectures),
            "supported_dtypes": list(self.supported_dtypes),
            "supported_quantizations": list(self.supported_quantizations),
            "supported_decode_modes": list(self.supported_decode_modes),
            "runtime_build_digest": self.runtime_build_digest,
            "available_memory_bytes": self.available_memory_bytes,
            "rss_bytes": self.rss_bytes,
            "swap_used_bytes": self.swap_used_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "cached_content_digests": list(self.cached_content_digests),
            "thermal_state": self.thermal_state,
            "power_state": self.power_state,
            "decode_modes_by_architecture": {
                architecture: list(modes)
                for architecture, modes in sorted(
                    self.decode_modes_by_architecture.items()
                )
            },
        }


@dataclass(frozen=True)
class DirectedEdgeFeasibilityEvidence:
    """One measured directed edge; reverse reachability is never inferred."""

    src: str
    dst: str
    observation_digest: str
    observed_at_unix_ms: int
    valid_until_unix_ms: int
    goodput_Bps: float
    rtt_ms: float
    jitter_ms: float
    loss_ratio: float

    def __post_init__(self) -> None:
        if not self.src or not self.dst or self.src == self.dst:
            raise ValueError("directed feasibility edge endpoints are invalid")
        _require_digest("edge observation_digest", self.observation_digest)
        if (
            type(self.observed_at_unix_ms) is not int
            or type(self.valid_until_unix_ms) is not int
            or self.observed_at_unix_ms <= 0
            or self.valid_until_unix_ms <= self.observed_at_unix_ms
        ):
            raise ValueError("directed feasibility edge freshness is invalid")
        for name in ("goodput_Bps", "rtt_ms", "jitter_ms", "loss_ratio"):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(f"directed feasibility edge {name} is invalid")
        if self.goodput_Bps <= 0 or self.loss_ratio > 1:
            raise ValueError("directed feasibility edge capacity is invalid")

    def projection(self) -> dict[str, object]:
        return {
            "src": self.src,
            "dst": self.dst,
            "observation_digest": self.observation_digest,
            "observed_at_unix_ms": self.observed_at_unix_ms,
            "valid_until_unix_ms": self.valid_until_unix_ms,
            "goodput_Bps": self.goodput_Bps,
            "rtt_ms": self.rtt_ms,
            "jitter_ms": self.jitter_ms,
            "loss_ratio": self.loss_ratio,
        }


@dataclass(frozen=True)
class SwarmFeasibilityEvidence:
    """One signed capability/link generation used atomically by the M17 planner."""

    generation: int
    evidence_digest: str
    signature_set_digest: str
    verification_key_set_digest: str
    placement_snapshot_generation: int
    placement_digest: str
    topology_digest: str
    observed_at_unix_ms: int
    valid_until_unix_ms: int
    nodes: tuple[NodeFeasibilityEvidence, ...]
    directed_edges: tuple[DirectedEdgeFeasibilityEvidence, ...]

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError(
                "swarm evidence generation must be a positive exact integer"
            )
        for name in (
            "evidence_digest",
            "signature_set_digest",
            "verification_key_set_digest",
            "placement_digest",
            "topology_digest",
        ):
            _require_digest(name, getattr(self, name))
        if (
            type(self.placement_snapshot_generation) is not int
            or self.placement_snapshot_generation <= 0
        ):
            raise ValueError(
                "placement snapshot generation must be a positive exact integer"
            )
        if (
            type(self.observed_at_unix_ms) is not int
            or type(self.valid_until_unix_ms) is not int
            or self.observed_at_unix_ms <= 0
            or self.valid_until_unix_ms <= self.observed_at_unix_ms
        ):
            raise ValueError("swarm evidence freshness is invalid")
        node_ids = [node.node_id for node in self.nodes]
        if not node_ids or len(node_ids) != len(set(node_ids)):
            raise ValueError("swarm evidence node identities are invalid")
        edge_ids = [(edge.src, edge.dst) for edge in self.directed_edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate directed feasibility edge")
        if any(
            edge.src not in node_ids or edge.dst not in node_ids
            for edge in self.directed_edges
        ):
            raise ValueError("directed feasibility edge references an unknown node")

    def projection(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "evidence_digest": self.evidence_digest,
            "signature_set_digest": self.signature_set_digest,
            "verification_key_set_digest": self.verification_key_set_digest,
            "placement_snapshot_generation": self.placement_snapshot_generation,
            "placement_digest": self.placement_digest,
            "topology_digest": self.topology_digest,
            "observed_at_unix_ms": self.observed_at_unix_ms,
            "valid_until_unix_ms": self.valid_until_unix_ms,
            "nodes": [node.projection() for node in self.nodes],
            "directed_edges": [edge.projection() for edge in self.directed_edges],
        }


def swarm_feasibility_evidence_from_document(
    document: Mapping[str, object],
) -> SwarmFeasibilityEvidence:
    """Decode the closed, independently signed M17 swarm evidence projection."""

    expected = {
        "protocol",
        "generation",
        "evidence_digest",
        "signature_set_digest",
        "verification_key_set_digest",
        "placement_snapshot_generation",
        "observed_at_unix_ms",
        "valid_until_unix_ms",
        "nodes",
        "directed_edges",
        "placement_digest",
        "topology_digest",
        "route_ready",
    }
    if (
        set(document) != expected
        or document.get("protocol") != ("mycelium.swarm_feasibility_evidence.v1")
        or document.get("route_ready") is not False
    ):
        raise ValueError("swarm feasibility evidence document is invalid")
    nodes_value = document.get("nodes")
    edges_value = document.get("directed_edges")
    if not isinstance(nodes_value, list) or not isinstance(edges_value, list):
        raise ValueError("swarm feasibility evidence collections are invalid")
    nodes: list[NodeFeasibilityEvidence] = []
    node_fields = set(NodeFeasibilityEvidence.__dataclass_fields__)
    for value in nodes_value:
        if not isinstance(value, dict) or set(value) != node_fields:
            raise ValueError("swarm feasibility node evidence is invalid")
        nodes.append(
            NodeFeasibilityEvidence(
                **{
                    **value,
                    "supported_architectures": tuple(value["supported_architectures"]),
                    "supported_dtypes": tuple(value["supported_dtypes"]),
                    "supported_quantizations": tuple(value["supported_quantizations"]),
                    "supported_decode_modes": tuple(value["supported_decode_modes"]),
                    "cached_content_digests": tuple(value["cached_content_digests"]),
                    "decode_modes_by_architecture": {
                        architecture: tuple(modes)
                        for architecture, modes in value[
                            "decode_modes_by_architecture"
                        ].items()
                    },
                }
            )
        )
    edges: list[DirectedEdgeFeasibilityEvidence] = []
    edge_fields = set(DirectedEdgeFeasibilityEvidence.__dataclass_fields__)
    for value in edges_value:
        if not isinstance(value, dict) or set(value) != edge_fields:
            raise ValueError("swarm feasibility directed edge evidence is invalid")
        edges.append(DirectedEdgeFeasibilityEvidence(**value))
    detached = dict(document)
    evidence_digest = detached.pop("evidence_digest")
    if evidence_digest != _digest(detached):
        raise ValueError("swarm feasibility evidence digest mismatch")
    for name in ("placement_digest", "topology_digest"):
        value = document.get(name)
        if not isinstance(value, str):
            raise ValueError(f"swarm feasibility {name} is invalid")
        _require_digest(name, value)
    return SwarmFeasibilityEvidence(
        generation=int(document["generation"]),
        evidence_digest=str(evidence_digest),
        signature_set_digest=str(document["signature_set_digest"]),
        verification_key_set_digest=str(document["verification_key_set_digest"]),
        placement_snapshot_generation=int(document["placement_snapshot_generation"]),
        placement_digest=str(document["placement_digest"]),
        topology_digest=str(document["topology_digest"]),
        observed_at_unix_ms=int(document["observed_at_unix_ms"]),
        valid_until_unix_ms=int(document["valid_until_unix_ms"]),
        nodes=tuple(nodes),
        directed_edges=tuple(edges),
    )


def _snapshot_entry(model_root: Path, revision: str) -> LocalModelEntry:
    model_id = _model_id_from_cache_name(model_root.name)
    snapshot = model_root / "snapshots" / revision
    reasons: list[str] = []
    if not _COMMIT_RE.fullmatch(revision):
        reasons.append("mutable_or_invalid_revision")
    config_path = snapshot / "config.json"
    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            config = _read_json(config_path)
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.append("invalid_config")
    else:
        reasons.append("missing_required_file:config.json")

    tokenizer_present = (snapshot / "tokenizer.json").is_file() or (
        (snapshot / "vocab.json").is_file() and (snapshot / "merges.txt").is_file()
    )
    if not tokenizer_present:
        reasons.append("missing_tokenizer")

    index_path = snapshot / "model.safetensors.index.json"
    single_path = snapshot / "model.safetensors"
    checkpoint_format = "unknown"
    required_weight_files: list[str] = []
    weight_map: dict[str, str] = {}
    if index_path.is_file():
        checkpoint_format = "safetensors_sharded"
        try:
            index = _read_json(index_path)
            raw_weight_map = index.get("weight_map")
            if (
                not isinstance(raw_weight_map, dict)
                or not raw_weight_map
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in raw_weight_map.items()
                )
            ):
                reasons.append("invalid_or_empty_weight_map")
            else:
                weight_map = dict(raw_weight_map)
                required_weight_files = sorted(set(weight_map.values()))
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.append("invalid_checkpoint_index")
    elif single_path.is_file():
        checkpoint_format = "safetensors_single"
        required_weight_files = [single_path.name]
    else:
        reasons.append("missing_weight_artifact")

    required_files = ["config.json", *required_weight_files]
    present_files: list[str] = []
    file_records: list[dict[str, object]] = []
    weight_bytes = 0
    for name in sorted(set(required_files)):
        candidate = snapshot / name
        if not candidate.is_file():
            reasons.append(f"missing_required_file:{name}")
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            reasons.append(f"unreadable_required_file:{name}")
            continue
        present_files.append(name)
        record: dict[str, object] = {"name": name, "size_bytes": size}
        content_digest = _blob_digest(candidate)
        if content_digest is not None:
            record["content_digest"] = content_digest
        file_records.append(record)
        if name.endswith((".safetensors", ".bin")):
            weight_bytes += size

    model_type = (
        config.get("model_type")
        if isinstance(config.get("model_type"), str)
        else "unknown"
    )
    architectures = config.get("architectures")
    architecture = (
        architectures[0]
        if isinstance(architectures, list)
        and architectures
        and isinstance(architectures[0], str)
        else "unknown"
    )
    adapter_id: str | None = None
    num_layers: int | None = None
    adapter = None
    try:
        adapter = adapter_for_config(config)
        adapter.validate_architectures(config)
        adapter_id = adapter.architecture
        num_layers = adapter.layer_count(config)
    except ValueError as exc:
        reasons.append(f"architecture_adapter:{exc}")

    if adapter is None or not adapter.runtime_supported:
        reasons.append(f"runtime_adapter_unavailable:{model_type}")

    hidden_size = config.get("hidden_size")
    if (
        not isinstance(hidden_size, int)
        or isinstance(hidden_size, bool)
        or hidden_size <= 0
    ):
        hidden_size = None
    kv_heads = config.get("num_key_value_heads") or config.get("num_attention_heads")
    if not isinstance(kv_heads, int) or isinstance(kv_heads, bool) or kv_heads <= 0:
        kv_heads = None
    head_dim = _head_dim(config)
    dtype_bytes = _dtype_bytes(config)
    if dtype_bytes is None:
        reasons.append("unsupported_or_unknown_dtype")
    max_context_tokens = config.get("max_position_embeddings")
    if (
        not isinstance(max_context_tokens, int)
        or isinstance(max_context_tokens, bool)
        or max_context_tokens <= 0
    ):
        max_context_tokens = None

    (
        layer_weight_bytes,
        entry_static_bytes,
        final_static_bytes,
        other_static_bytes,
        accounting_errors,
    ) = _tensor_accounting(
        snapshot=snapshot,
        required_weight_files=required_weight_files,
        weight_map=weight_map,
        config=config,
    )
    (
        int8_layer_weight_bytes,
        int8_entry_static_bytes,
        int8_final_static_bytes,
        int8_load_layer_bytes,
        int8_load_entry_static_bytes,
        int8_load_final_static_bytes,
        int8_streaming_transient_layer_bytes,
        int8_streaming_transient_entry_static_bytes,
        int8_streaming_transient_final_static_bytes,
    ) = _int8_runtime_accounting(
        snapshot=snapshot,
        required_weight_files=required_weight_files,
        weight_map=weight_map,
        config=config,
    )
    reasons.extend(accounting_errors)
    if not layer_weight_bytes and required_weight_files and adapter_id is not None:
        reasons.append("exact_tensor_accounting_unavailable")
    if layer_weight_bytes:
        weight_bytes = sum(layer_weight_bytes) + entry_static_bytes + final_static_bytes
        if config.get("tie_word_embeddings") is True:
            weight_bytes -= entry_static_bytes

    reasons = sorted({_bounded_reason(reason) for reason in reasons})[:_MAX_REASONS]
    incomplete = any(
        reason.startswith(
            (
                "missing_",
                "invalid_",
                "unreadable_",
                "mutable_",
            )
        )
        for reason in reasons
    )
    state = "incomplete" if incomplete else "compatible"
    if not incomplete and any(
        reason.startswith(
            (
                "architecture_adapter:",
                "runtime_adapter_unavailable:",
                "unsupported_",
                "exact_tensor_accounting_",
            )
        )
        for reason in reasons
    ):
        state = "discovered"

    descriptor = {
        "model_id": model_id,
        "revision": revision,
        "checkpoint_format": checkpoint_format,
        "model_type": model_type,
        "quantization": _quantization(config),
        "files": file_records,
    }
    return LocalModelEntry(
        model_id=model_id,
        revision=revision,
        snapshot_path=snapshot,
        state=state,
        model_type=model_type,
        architecture=architecture,
        adapter_id=adapter_id,
        checkpoint_format=checkpoint_format,
        quantization=_quantization(config),
        num_layers=num_layers,
        hidden_size=hidden_size,
        kv_heads=kv_heads,
        head_dim=head_dim,
        dtype_bytes=dtype_bytes,
        max_context_tokens=max_context_tokens,
        weight_bytes=weight_bytes,
        layer_weight_bytes=layer_weight_bytes,
        entry_static_bytes=entry_static_bytes,
        final_static_bytes=final_static_bytes,
        other_static_bytes=other_static_bytes,
        int8_layer_weight_bytes=int8_layer_weight_bytes,
        int8_entry_static_bytes=int8_entry_static_bytes,
        int8_final_static_bytes=int8_final_static_bytes,
        int8_load_layer_bytes=int8_load_layer_bytes,
        int8_load_entry_static_bytes=int8_load_entry_static_bytes,
        int8_load_final_static_bytes=int8_load_final_static_bytes,
        int8_streaming_transient_layer_bytes=(int8_streaming_transient_layer_bytes),
        int8_streaming_transient_entry_static_bytes=(
            int8_streaming_transient_entry_static_bytes
        ),
        int8_streaming_transient_final_static_bytes=(
            int8_streaming_transient_final_static_bytes
        ),
        required_files=tuple(sorted(set(required_files))),
        present_files=tuple(sorted(present_files)),
        file_records=tuple(file_records),
        reasons=tuple(reasons),
        artifact_digest=_digest(descriptor),
    )


def scan_huggingface_cache(cache_root: Path) -> tuple[LocalModelEntry, ...]:
    """Discover local snapshots without writing or performing network access."""

    if not cache_root.is_dir():
        return ()
    entries: list[LocalModelEntry] = []
    for model_root in sorted(cache_root.glob("models--*"), key=lambda path: path.name):
        snapshots = model_root / "snapshots"
        if not snapshots.is_dir():
            continue
        for snapshot in sorted(snapshots.iterdir(), key=lambda path: path.name):
            if snapshot.is_dir():
                entries.append(_snapshot_entry(model_root, snapshot.name))
                if len(entries) > _MAX_ENTRIES:
                    raise ValueError("local model catalog exceeds maximum entry count")
    return tuple(entries)


def catalog_document(
    entries: Iterable[LocalModelEntry],
    *,
    generation: int,
    discovered_entries: Iterable[Mapping[str, object]] = (),
    discovery: Mapping[str, object] | None = None,
    entry_discovery: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    if type(generation) is not int or generation <= 0:
        raise ValueError("catalog generation must be a positive exact integer")
    discovery_by_identity = entry_discovery or {}
    projections = []
    for entry in entries:
        projection = entry.projection()
        identity = (entry.model_id, entry.revision)
        metadata = discovery_by_identity.get(identity)
        if metadata is not None:
            projection.update(json.loads(json.dumps(dict(metadata), allow_nan=False)))
        projections.append(projection)
    projections.extend(
        json.loads(json.dumps(dict(entry), allow_nan=False))
        for entry in discovered_entries
    )
    projections = sorted(
        projections,
        key=lambda entry: (str(entry["model_id"]), str(entry["revision"])),
    )
    if len(projections) > _MAX_ENTRIES:
        raise ValueError("local model catalog exceeds maximum entry count")
    document: dict[str, object] = {
        "protocol": MODEL_CATALOG_PROTOCOL,
        "generation": generation,
        "source": "local_read_only_inventory",
        "download_policy": "operator_approval_required",
        "entries": projections,
        "discovery": json.loads(json.dumps(dict(discovery), allow_nan=False))
        if discovery is not None
        else {
            "scope": "coordinator_only",
            "accepted_member_count": 0,
            "rejected_member_count": 0,
            "blockers": [],
        },
        "route_ready": False,
        "qualification_evaluated": False,
    }
    document["catalog_digest"] = _digest(document)
    return document


def model_identity(entry: LocalModelEntry) -> ModelIdentity:
    if not entry.compatible:
        raise ValueError("model entry is not runtime-compatible")
    required = {
        "num_layers": entry.num_layers,
        "hidden_size": entry.hidden_size,
        "dtype_bytes": entry.dtype_bytes,
        "kv_heads": entry.kv_heads,
        "head_dim": entry.head_dim,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if (
        missing
        or entry.weight_bytes <= 0
        or len(entry.layer_weight_bytes) != entry.num_layers
    ):
        raise ValueError(f"model entry lacks planning metadata: {', '.join(missing)}")
    return ModelIdentity(
        model_id=entry.model_id,
        revision=entry.revision,
        weight_digest=entry.artifact_digest,
        architecture=entry.model_type,
        num_layers=int(entry.num_layers),
        hidden_size=int(entry.hidden_size),
        dtype_bytes=int(entry.dtype_bytes),
        kv_heads=int(entry.kv_heads),
        head_dim=int(entry.head_dim),
        weight_bytes=entry.weight_bytes,
    )


def _exact_allocation(
    entry: LocalModelEntry,
    nodes: Sequence[NodeCapability],
    workload: WorkloadScenario,
    policy: PlanningPolicy,
    evidence: SwarmFeasibilityEvidence,
    required_decode_mode: str,
    serving_quantization: str | None,
    representation_authorization: Mapping[str, object] | None,
) -> tuple[
    bool,
    list[dict[str, object]],
    float | None,
    tuple[str, ...],
    list[dict[str, object]],
]:
    model = model_identity(entry)
    if not nodes:
        return False, [], None, ("no_nodes",), []
    if len(nodes) > model.num_layers:
        return False, [], None, ("stage_count_exceeds_layer_count",), []
    resource_by_node = {record.node_id: record for record in evidence.nodes}
    edge_by_pair = {(edge.src, edge.dst): edge for edge in evidence.directed_edges}
    prefix = [0]
    for size in entry.layer_weight_bytes:
        prefix.append(prefix[-1] + size)
    rejected: list[dict[str, object]] = []
    authorized_stages: tuple[Mapping[str, object], ...] | None = None
    authorized_representation_digest: str | None = None
    if representation_authorization is not None:
        authorization_protocol = representation_authorization.get("protocol")
        if (
            authorization_protocol
            not in {
                "mycelium.model_preparation_authorization.v1",
                "mycelium.model_preparation_authorization.v2",
            }
            or representation_authorization.get("model_id") != entry.model_id
            or representation_authorization.get("revision") != entry.revision
            or representation_authorization.get("source_quantization")
            != entry.quantization
            or representation_authorization.get("serving_quantization")
            != serving_quantization
            or not isinstance(representation_authorization.get("serving_dtype"), str)
            or not representation_authorization.get("serving_dtype")
            or not isinstance(
                representation_authorization.get("representation_digest"), str
            )
            or _DIGEST_PATTERN.fullmatch(
                str(representation_authorization["representation_digest"])
            )
            is None
            or (
                entry.quantization != serving_quantization
                and representation_authorization.get("conversion_authorized")
                is not True
            )
        ):
            raise ValueError("approved_serving_representation_invalid")
        if authorization_protocol == "mycelium.model_preparation_authorization.v2":
            serving_representations = entry.projection().get("serving_representations")
            matching_representations = (
                [
                    value
                    for value in serving_representations
                    if isinstance(value, Mapping)
                    and value.get("quantization") == serving_quantization
                    and value.get("runtime_dtype")
                    == representation_authorization.get("serving_dtype")
                    and value.get("representation_digest")
                    == representation_authorization.get("representation_digest")
                    and value.get("quantizer")
                    == representation_authorization.get("quantizer")
                ]
                if isinstance(serving_representations, list)
                else []
            )
            if (
                representation_authorization.get("source_artifact_digest")
                != entry.artifact_digest
                or not isinstance(representation_authorization.get("quantizer"), str)
                or not representation_authorization.get("quantizer")
                or representation_authorization.get("download_authorized") is not False
                or len(matching_representations) != 1
            ):
                raise ValueError("approved_serving_representation_invalid")
        raw_stages = representation_authorization.get("stages")
        if not isinstance(raw_stages, list) or len(raw_stages) != len(nodes):
            raise ValueError("approved_serving_representation_invalid")
        cursor = 0
        validated_stages: list[Mapping[str, object]] = []
        for index, (raw_stage, node) in enumerate(zip(raw_stages, nodes, strict=True)):
            if not isinstance(raw_stage, Mapping):
                raise ValueError("approved_serving_representation_invalid")
            start = raw_stage.get("start_layer")
            end = raw_stage.get("end_layer_exclusive")
            files = raw_stage.get("assignment_files")
            artifact_bytes = raw_stage.get("assignment_artifact_bytes")
            if (
                raw_stage.get("stage_index") != index
                or raw_stage.get("node_id") != node.node_id
                or type(start) is not int
                or type(end) is not int
                or start != cursor
                or end <= start
                or not isinstance(raw_stage.get("backend"), str)
                or raw_stage.get("decode_mode") != required_decode_mode
                or not isinstance(files, list)
                or not files
                or not all(
                    isinstance(item, str) and item and item == Path(item).name
                    for item in files
                )
                or len(files) != len(set(files))
                or type(artifact_bytes) is not int
                or artifact_bytes <= 0
            ):
                raise ValueError("approved_serving_representation_invalid")
            validated_stages.append(raw_stage)
            cursor = end
        if cursor != model.num_layers:
            raise ValueError("approved_serving_representation_invalid")
        authorized_stages = tuple(validated_stages)
        authorized_representation_digest = str(
            representation_authorization["representation_digest"]
        )

    def assignment_files(
        start: int,
        end: int,
        stage_index: int,
    ) -> tuple[dict[str, object], ...]:
        records = {str(record["name"]): record for record in entry.file_records}
        weight_names = {
            name for name in records if name.endswith((".safetensors", ".bin"))
        }
        selected = {"config.json"} & set(records)
        index_path = entry.snapshot_path / "model.safetensors.index.json"
        if entry.checkpoint_format == "safetensors_sharded" and index_path.is_file():
            index = _read_json(index_path)
            weight_map = index.get("weight_map")
            if not isinstance(weight_map, dict) or not weight_map:
                raise ValueError("assignment_file_coverage_unavailable")
            config = _read_json(entry.snapshot_path / "config.json")
            adapter = adapter_for_config(config)
            layer_prefixes = tuple(
                template.format(layer=layer)
                for layer in range(start, end)
                for template in (
                    adapter.block_prefix_template,
                    *adapter.alternate_block_prefix_templates,
                )
            )
            static_prefixes: tuple[str, ...] = ()
            if stage_index == 0:
                static_prefixes += adapter.components.get("input_embedding", ())
            if stage_index == len(nodes) - 1:
                static_prefixes += tuple(
                    prefix
                    for role, prefixes in adapter.components.items()
                    if role not in {"decoder", "input_embedding"}
                    for prefix in prefixes
                    if "{layer}" not in prefix
                )
            for tensor_name, filename in weight_map.items():
                if not isinstance(tensor_name, str) or not isinstance(filename, str):
                    raise ValueError("assignment_file_coverage_unavailable")
                if tensor_name.startswith(layer_prefixes) or tensor_name.startswith(
                    static_prefixes
                ):
                    selected.add(filename)
        else:
            selected.update(weight_names)
        if not selected or not selected.issubset(records):
            raise ValueError("assignment_file_coverage_unavailable")
        return tuple(records[name] for name in sorted(selected))

    required_edges: list[DirectedEdgeFeasibilityEvidence] = []
    for index in range(len(nodes) - 1):
        edge = edge_by_pair.get((nodes[index].node_id, nodes[index + 1].node_id))
        if edge is not None:
            required_edges.append(edge)
    if len(nodes) > 1:
        edge = edge_by_pair.get((nodes[-1].node_id, nodes[0].node_id))
        if edge is not None:
            required_edges.append(edge)
    conservative_transfer_Bps = min(
        (edge.goodput_Bps for edge in required_edges),
        default=1_000_000_000.0,
    )

    def reject(stage_index: int, start: int, end: int, reason: str) -> None:
        if len(rejected) < _MAX_REJECTED_CANDIDATES:
            rejected.append(
                {
                    "stage_index": stage_index,
                    "node_id": nodes[stage_index].node_id,
                    "start_layer": start,
                    "end_layer_exclusive": end,
                    "reason": _bounded_reason(reason),
                }
            )

    def candidate_cost(
        stage_index: int,
        start: int,
        end: int,
    ) -> tuple[float, dict[str, object]] | None:
        node = nodes[stage_index]
        if not node.eligible:
            reject(
                stage_index,
                start,
                end,
                node.exclusion_reason or "node_ineligible",
            )
            return None
        resource = resource_by_node[node.node_id]
        layer_count = end - start
        static_bytes = 0
        if stage_index == 0:
            static_bytes += entry.entry_static_bytes
        if stage_index == len(nodes) - 1:
            static_bytes += entry.final_static_bytes
        layer_bytes = prefix[end] - prefix[start]
        resident_layer_bytes = layer_bytes
        resident_static_bytes = static_bytes
        load_peak_weight_bytes = layer_bytes + static_bytes
        representation_digest: str | None = None
        if serving_quantization == "int8-weight-only":
            if (
                len(entry.int8_layer_weight_bytes) != entry.num_layers
                or len(entry.int8_load_layer_bytes) != entry.num_layers
                or len(entry.int8_streaming_transient_layer_bytes) != entry.num_layers
            ):
                reject(stage_index, start, end, "serving_representation_unavailable")
                return None
            resident_layer_bytes = sum(entry.int8_layer_weight_bytes[start:end])
            resident_static_bytes = 0
            streaming_transient_candidates = list(
                entry.int8_streaming_transient_layer_bytes[start:end]
            )
            if stage_index == 0:
                resident_static_bytes += entry.int8_entry_static_bytes
                streaming_transient_candidates.append(
                    entry.int8_streaming_transient_entry_static_bytes
                )
            if stage_index == len(nodes) - 1:
                resident_static_bytes += entry.int8_final_static_bytes
                streaming_transient_candidates.append(
                    entry.int8_streaming_transient_final_static_bytes
                )
            load_peak_weight_bytes = (
                resident_layer_bytes
                + resident_static_bytes
                + max(streaming_transient_candidates, default=0)
            )
            representation = entry.projection()["serving_representations"][0]
            representation_digest = str(representation["representation_digest"])
        authorized_stage = (
            None if authorized_stages is None else authorized_stages[stage_index]
        )
        if authorized_stage is not None:
            if (
                authorized_stage["start_layer"] != start
                or authorized_stage["end_layer_exclusive"] != end
                or authorized_stage["backend"] != resource.backend
            ):
                reject(
                    stage_index, start, end, "approved_representation_stage_mismatch"
                )
                return None
            representation_digest = authorized_representation_digest
        kv_bytes = (
            model.kv_bytes_per_layer_token
            * layer_count
            * workload.total_context_tokens
            * workload.concurrency
            * workload.batch_size
        )
        activation_bytes = model.activation_bytes(
            max(workload.effective_prompt_tokens, 1),
            workload.batch_size * workload.concurrency,
        )
        available_memory = min(node.total_memory_bytes, resource.available_memory_bytes)
        runtime_reserve = int(available_memory * policy.memory_reserve_fraction)
        memory_limit = available_memory - runtime_reserve
        resident_required = (
            resident_layer_bytes
            + resident_static_bytes
            + kv_bytes
            + activation_bytes
            + node.workspace_bytes
        )
        load_peak_required = load_peak_weight_bytes + node.workspace_bytes
        required = max(resident_required, load_peak_required)
        if required > memory_limit:
            reject(
                stage_index,
                start,
                end,
                "insufficient_load_memory"
                if load_peak_required > resident_required
                else "insufficient_memory",
            )
            return None
        if authorized_stage is None:
            try:
                files = assignment_files(start, end, stage_index)
            except (OSError, ValueError, json.JSONDecodeError):
                reject(stage_index, start, end, "assignment_file_coverage_unavailable")
                return None
            cached_digests = set(resource.cached_content_digests)
            cached_artifact_bytes = sum(
                int(record["size_bytes"])
                for record in files
                if record.get("content_digest") in cached_digests
            )
            assignment_artifact_bytes = sum(
                int(record["size_bytes"]) for record in files
            )
            assignment_file_names = [str(record["name"]) for record in files]
        else:
            # The immutable authorization owns exact assignment-local byte bounds.
            # Chunk manifests later prove cache reuse; until joined, conservatively
            # count the entire approved stage as missing.
            cached_artifact_bytes = 0
            assignment_artifact_bytes = int(
                authorized_stage["assignment_artifact_bytes"]
            )
            assignment_file_names = list(authorized_stage["assignment_files"])
        missing_artifact_bytes = assignment_artifact_bytes - cached_artifact_bytes
        staging_overhead_bytes = missing_artifact_bytes
        required_disk_bytes = missing_artifact_bytes + staging_overhead_bytes
        if required_disk_bytes > resource.disk_free_bytes:
            reject(stage_index, start, end, "insufficient_disk")
            return None
        fast_limit = min(node.fast_memory_bytes, memory_limit)
        spill_bytes = max(0.0, required - fast_limit)
        if spill_bytes and node.spill_bandwidth_Bps <= 0:
            reject(stage_index, start, end, "spill_unavailable")
            return None
        spill_penalty = (
            spill_bytes / node.spill_bandwidth_Bps * 1_000.0 if spill_bytes else 0.0
        )
        prefill = (
            node.prefill_ms_per_layer_token
            * layer_count
            * workload.effective_prompt_tokens
        )
        decode = node.decode_ms_per_layer_token * layer_count
        service_work = (
            prefill
            + workload.output_tokens * decode
            + (workload.output_tokens + 1) * spill_penalty
        )
        if policy.objective == "prefill_ttft":
            objective = prefill + spill_penalty
        elif policy.objective == "decode_tpot":
            objective = decode + spill_penalty
        else:
            objective = service_work
        fixed_without_kv = resident_required - kv_bytes
        kv_per_context = (
            model.kv_bytes_per_layer_token
            * layer_count
            * workload.concurrency
            * workload.batch_size
        )
        maximum_context = (
            max(0, (memory_limit - fixed_without_kv) // kv_per_context)
            if kv_per_context
            else 0
        )
        per_concurrency = (
            model.kv_bytes_per_layer_token
            * layer_count
            * workload.total_context_tokens
            * workload.batch_size
            + model.activation_bytes(
                max(workload.effective_prompt_tokens, 1),
                workload.batch_size,
            )
        )
        fixed_without_dynamic = resident_required - kv_bytes - activation_bytes
        maximum_concurrency = (
            max(0, (memory_limit - fixed_without_dynamic) // per_concurrency)
            if per_concurrency
            else 0
        )
        return objective, {
            "stage_index": stage_index,
            "node_id": node.node_id,
            "start_layer": start,
            "end_layer_exclusive": end,
            "layer_weight_bytes": layer_bytes,
            "static_weight_bytes": static_bytes,
            "resident_layer_weight_bytes": resident_layer_bytes,
            "resident_static_weight_bytes": resident_static_bytes,
            "load_peak_weight_bytes": load_peak_weight_bytes,
            "resident_required_memory_bytes": int(resident_required),
            "load_peak_required_memory_bytes": int(load_peak_required),
            "representation_digest": representation_digest,
            "activation_bytes": activation_bytes,
            "kv_bytes": kv_bytes,
            "workspace_bytes": node.workspace_bytes,
            "runtime_reserve_bytes": runtime_reserve,
            "required_memory_bytes": int(required),
            "available_memory_bytes": int(available_memory),
            "headroom_bytes": int(memory_limit - required),
            "spill_bytes": int(spill_bytes),
            "rss_bytes": resource.rss_bytes,
            "swap_used_bytes": resource.swap_used_bytes,
            "disk_free_bytes": resource.disk_free_bytes,
            "required_disk_bytes": required_disk_bytes,
            "staging_overhead_bytes": staging_overhead_bytes,
            "assignment_artifact_bytes": assignment_artifact_bytes,
            "cached_artifact_bytes": cached_artifact_bytes,
            "missing_artifact_bytes": missing_artifact_bytes,
            "assignment_files": assignment_file_names,
            "backend": resource.backend,
            "runtime_build_digest": resource.runtime_build_digest,
            "dtype": "float32"
            if serving_quantization == "int8-weight-only"
            else entry.quantization,
            "source_quantization": entry.quantization,
            "quantization": serving_quantization or entry.quantization,
            "decode_mode": required_decode_mode,
            "thermal_state": resource.thermal_state,
            "power_state": resource.power_state,
            "maximum_context_tokens": int(maximum_context),
            "maximum_concurrency": int(maximum_concurrency),
            "modeled_transfer_ms": (
                missing_artifact_bytes / conservative_transfer_Bps * 1_000.0
            ),
            "modeled_service_work_ms": service_work,
        }

    if authorized_stages is not None:
        stages: list[dict[str, object]] = []
        objective = 0.0
        for stage_index, stage in enumerate(authorized_stages):
            cost = candidate_cost(
                stage_index,
                int(stage["start_layer"]),
                int(stage["end_layer_exclusive"]),
            )
            if cost is None:
                diagnostics = {"approved_representation_currently_infeasible"}
                diagnostics.update(
                    f"{item['reason']}:{item['node_id']}" for item in rejected
                )
                return False, [], None, tuple(sorted(diagnostics)), rejected
            objective = max(objective, cost[0])
            stages.append(cost[1])
        return True, stages, objective, (), rejected

    dp: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {(0, 0): (0.0, ())}
    for stage_count in range(1, len(nodes) + 1):
        remaining = len(nodes) - stage_count
        for assigned in range(stage_count, model.num_layers - remaining + 1):
            best: tuple[float, tuple[int, ...]] | None = None
            for previous in range(stage_count - 1, assigned):
                prior = dp.get((stage_count - 1, previous))
                if prior is None:
                    continue
                cost = candidate_cost(stage_count - 1, previous, assigned)
                if cost is None:
                    continue
                candidate = (max(prior[0], cost[0]), prior[1] + (assigned - previous,))
                if best is None or candidate < best:
                    best = candidate
            if best is not None:
                dp[(stage_count, assigned)] = best
    selected = dp.get((len(nodes), model.num_layers))
    if selected is None:
        diagnostics = {"no_feasible_contiguous_exact_weight_allocation"}
        diagnostics.update(f"{item['reason']}:{item['node_id']}" for item in rejected)
        return (
            False,
            [],
            None,
            tuple(sorted(diagnostics)),
            rejected,
        )
    stages: list[dict[str, object]] = []
    cursor = 0
    for stage_index, count in enumerate(selected[1]):
        cost = candidate_cost(stage_index, cursor, cursor + count)
        if cost is None:
            raise AssertionError("selected exact allocation became infeasible")
        stages.append(cost[1])
        cursor += count
    return True, stages, selected[0], (), rejected


def _preflight_feasibility(
    entry: LocalModelEntry,
    nodes: Sequence[NodeCapability],
    workload: WorkloadScenario,
    evidence: SwarmFeasibilityEvidence,
    *,
    evaluated_at_unix_ms: int,
    required_decode_mode: str,
    serving_quantization: str | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if evaluated_at_unix_ms > evidence.valid_until_unix_ms:
        reasons.append("stale_swarm_evidence")
    resource_by_node = {record.node_id: record for record in evidence.nodes}
    node_ids = [node.node_id for node in nodes]
    for node in nodes:
        resource = resource_by_node.get(node.node_id)
        if resource is None:
            reasons.append(f"missing_node_evidence:{node.node_id}")
            continue
        if evaluated_at_unix_ms > resource.valid_until_unix_ms:
            reasons.append(f"stale_node_evidence:{node.node_id}")
        try:
            adapter = adapter_for_config(
                _read_json(entry.snapshot_path / "config.json")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.append("architecture_adapter_unavailable")
            continue
        if resource.backend not in adapter.runtime_backends:
            reasons.append(f"backend_unsupported:{node.node_id}:{resource.backend}")
        if entry.adapter_id not in resource.supported_architectures:
            reasons.append(
                f"architecture_unsupported:{node.node_id}:{entry.adapter_id}"
            )
        runtime_dtype = (
            "float32"
            if serving_quantization == "int8-weight-only"
            else entry.quantization
        )
        runtime_quantization = serving_quantization or entry.quantization
        if runtime_dtype not in resource.supported_dtypes:
            reasons.append(f"dtype_unsupported:{node.node_id}:{runtime_dtype}")
        if runtime_quantization not in resource.supported_quantizations:
            reasons.append(
                f"quantization_unsupported:{node.node_id}:{runtime_quantization}"
            )
        architecture_modes = resource.decode_modes_by_architecture.get(
            entry.adapter_id,
            resource.supported_decode_modes
            if not resource.decode_modes_by_architecture
            else (),
        )
        if required_decode_mode not in architecture_modes:
            reasons.append(
                f"decode_mode_unsupported:{node.node_id}:{required_decode_mode}"
            )
        if resource.available_memory_bytes <= 0:
            reasons.append(f"memory_unavailable:{node.node_id}")
        if resource.disk_free_bytes <= 0:
            reasons.append(f"disk_unavailable:{node.node_id}")
        if resource.thermal_state in {"serious", "critical", "emergency"}:
            reasons.append(f"thermal_pressure:{node.node_id}:{resource.thermal_state}")
        if resource.power_state in {"critical_battery", "shutdown_imminent"}:
            reasons.append(f"power_pressure:{node.node_id}:{resource.power_state}")
    edge_by_pair = {(edge.src, edge.dst): edge for edge in evidence.directed_edges}
    required_pairs = [
        (node_ids[index], node_ids[index + 1]) for index in range(len(node_ids) - 1)
    ]
    if len(node_ids) > 1:
        required_pairs.append((node_ids[-1], node_ids[0]))
    for src, dst in required_pairs:
        edge = edge_by_pair.get((src, dst))
        if edge is None:
            reasons.append(f"missing_directed_edge:{src}->{dst}")
        elif evaluated_at_unix_ms > edge.valid_until_unix_ms:
            reasons.append(f"stale_directed_edge:{src}->{dst}")
    if (
        entry.max_context_tokens is not None
        and workload.total_context_tokens > entry.max_context_tokens
    ):
        reasons.append(
            f"model_context_limit_exceeded:{workload.total_context_tokens}>"
            f"{entry.max_context_tokens}"
        )
    return tuple(sorted({_bounded_reason(reason) for reason in reasons}))


def evaluate_model_feasibility(
    entry: LocalModelEntry,
    *,
    ordered_nodes: Sequence[NodeCapability],
    workload: WorkloadScenario,
    policy: PlanningPolicy,
    evidence: SwarmFeasibilityEvidence,
    evaluated_at_unix_ms: int,
    required_decode_mode: str,
    serving_quantization: str | None = None,
    representation_authorization: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Evaluate one immutable model against one fresh signed swarm generation."""

    if type(evaluated_at_unix_ms) is not int or evaluated_at_unix_ms <= 0:
        raise ValueError("feasibility evaluation time must be a positive exact integer")
    if not required_decode_mode or required_decode_mode != required_decode_mode.strip():
        raise ValueError("required decode mode is invalid")
    if serving_quantization not in {None, "int8-weight-only"}:
        raise ValueError("serving quantization is invalid")
    reasons: list[str] = []
    stages: list[dict[str, object]] = []
    rejected_candidates: list[dict[str, object]] = []
    if not entry.compatible:
        reasons.extend(entry.reasons or ("model_not_runtime_compatible",))
        feasible = False
        bottleneck: float | None = None
    else:
        reasons.extend(
            _preflight_feasibility(
                entry,
                ordered_nodes,
                workload,
                evidence,
                evaluated_at_unix_ms=evaluated_at_unix_ms,
                required_decode_mode=required_decode_mode,
                serving_quantization=serving_quantization,
            )
        )
        if reasons:
            feasible = False
            bottleneck = None
        else:
            (
                feasible,
                stages,
                bottleneck,
                diagnostics,
                rejected_candidates,
            ) = _exact_allocation(
                entry,
                ordered_nodes,
                workload,
                policy,
                evidence,
                required_decode_mode,
                serving_quantization,
                representation_authorization,
            )
            reasons.extend(diagnostics)
    cached_artifact_bytes = sum(int(stage["cached_artifact_bytes"]) for stage in stages)
    missing_artifact_bytes = sum(
        int(stage["missing_artifact_bytes"]) for stage in stages
    )
    modeled_transfer_ms = sum(float(stage["modeled_transfer_ms"]) for stage in stages)
    maximum_context = min(
        (int(stage["maximum_context_tokens"]) for stage in stages),
        default=0,
    )
    if entry.max_context_tokens is not None and maximum_context:
        maximum_context = min(maximum_context, entry.max_context_tokens)
    maximum_concurrency = min(
        (int(stage["maximum_concurrency"]) for stage in stages),
        default=0,
    )
    if stages:
        limiting_stage = min(stages, key=lambda stage: int(stage["headroom_bytes"]))
        resource_bottleneck: dict[str, object] = {
            "kind": "memory",
            "node_id": limiting_stage["node_id"],
            "headroom_bytes": limiting_stage["headroom_bytes"],
        }
    else:
        resource_bottleneck = {
            "kind": "rejection",
            "reason": sorted(set(reasons))[0] if reasons else "unknown",
        }
    node_ids = [node.node_id for node in ordered_nodes]
    edge_by_pair = {(edge.src, edge.dst): edge for edge in evidence.directed_edges}
    required_pairs = [
        (node_ids[index], node_ids[index + 1]) for index in range(len(node_ids) - 1)
    ]
    if len(node_ids) > 1:
        required_pairs.append((node_ids[-1], node_ids[0]))
    result: dict[str, object] = {
        "protocol": MODEL_FEASIBILITY_PROTOCOL,
        "model_id": entry.model_id,
        "revision": entry.revision,
        "artifact_digest": entry.artifact_digest,
        "source_quantization": entry.quantization,
        "serving_quantization": serving_quantization or entry.quantization,
        "serving_dtype": (
            representation_authorization["serving_dtype"]
            if representation_authorization is not None
            else (
                "float32"
                if serving_quantization == "int8-weight-only"
                else entry.quantization
            )
        ),
        "representation_digest": (
            representation_authorization["representation_digest"]
            if representation_authorization is not None
            else (
                entry.projection()["serving_representations"][0][
                    "representation_digest"
                ]
                if serving_quantization == "int8-weight-only"
                and entry.int8_layer_weight_bytes
                else entry.artifact_digest
            )
        ),
        "representation_authority": (
            {
                "kind": "approved_existing_immutable_representation",
                "owner_decision_digest": representation_authorization.get(
                    "owner_decision_digest"
                ),
                "prior_feasibility_digest": representation_authorization.get(
                    "feasibility_digest"
                ),
                "source_artifact_digest": representation_authorization.get(
                    "source_artifact_digest"
                ),
                "quantizer": representation_authorization.get("quantizer"),
            }
            if representation_authorization is not None
            else {"kind": "locally_derived_candidate"}
        ),
        "evidence_digest": evidence.evidence_digest,
        "evidence_generation": evidence.generation,
        "evidence_signature_set_digest": evidence.signature_set_digest,
        "evidence_verification_key_set_digest": evidence.verification_key_set_digest,
        "evidence_valid_until_unix_ms": evidence.valid_until_unix_ms,
        "evaluated_at_unix_ms": evaluated_at_unix_ms,
        "workload": {
            "name": workload.name,
            "context_tokens": workload.total_context_tokens,
            "concurrency": workload.concurrency,
            "batch_size": workload.batch_size,
            "qos_class": workload.qos_class,
            "required_decode_mode": required_decode_mode,
        },
        "planner": "capability_aware_contiguous_exact_weight_dp",
        "evaluation_mode": (
            "approved_assignment_current_capability_validation"
            if representation_authorization is not None
            else "fresh_contiguous_allocation"
        ),
        "state": "feasible" if feasible else "infeasible",
        "stages": stages,
        "bottleneck_service_work_ms": bottleneck,
        "resource_bottleneck": resource_bottleneck,
        "maximum_qualified_context_tokens": maximum_context,
        "maximum_qualified_concurrency": maximum_concurrency,
        "cached_artifact_bytes": cached_artifact_bytes,
        "missing_artifact_bytes": missing_artifact_bytes,
        "modeled_transfer_ms": modeled_transfer_ms,
        "modeled_execution_ms": bottleneck,
        "required_directed_edges": [
            edge_by_pair[pair].projection()
            for pair in required_pairs
            if pair in edge_by_pair
        ],
        "rejected_candidates": rejected_candidates,
        "reasons": sorted({_bounded_reason(reason) for reason in reasons})[
            :_MAX_REASONS
        ],
        "provisioning_authorized": feasible,
        "route_ready": False,
        "qualification_evaluated": False,
    }
    result["feasibility_digest"] = _digest(result)
    return result


def validate_feasibility_currency(
    report: Mapping[str, object],
    current_evidence: SwarmFeasibilityEvidence,
    *,
    evaluated_at_unix_ms: int,
) -> None:
    """Fail closed if a provisioning decision no longer matches current evidence."""

    if report.get("protocol") != MODEL_FEASIBILITY_PROTOCOL:
        raise ValueError("invalid_feasibility_report")
    if (
        report.get("evidence_generation") != current_evidence.generation
        or report.get("evidence_digest") != current_evidence.evidence_digest
        or report.get("evidence_signature_set_digest")
        != current_evidence.signature_set_digest
        or report.get("evidence_verification_key_set_digest")
        != current_evidence.verification_key_set_digest
    ):
        raise ValueError("capability_evidence_drift")
    if evaluated_at_unix_ms > current_evidence.valid_until_unix_ms:
        raise ValueError("stale_swarm_evidence")
    if (
        report.get("state") != "feasible"
        or report.get("provisioning_authorized") is not True
    ):
        raise ValueError("feasibility_does_not_authorize_provisioning")


def model_operation_document(
    catalog: dict[str, object],
    feasibility_reports: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Bind one read-only catalog generation to bounded feasibility decisions."""

    if catalog.get("protocol") != MODEL_CATALOG_PROTOCOL:
        raise ValueError("model operation requires a model catalog")
    catalog_digest = catalog.get("catalog_digest")
    if not isinstance(catalog_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", catalog_digest
    ):
        raise ValueError("model catalog digest is invalid")
    if len(feasibility_reports) > _MAX_ENTRIES:
        raise ValueError("model operation feasibility report limit exceeded")
    reports: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for report in feasibility_reports:
        if report.get("protocol") != MODEL_FEASIBILITY_PROTOCOL:
            raise ValueError("model operation feasibility protocol mismatch")
        identity = (str(report.get("model_id")), str(report.get("revision")))
        if identity in identities:
            raise ValueError("duplicate model operation feasibility identity")
        identities.add(identity)
        reports.append(json.loads(json.dumps(report, allow_nan=False)))
    document: dict[str, object] = {
        "protocol": MODEL_OPERATION_PROTOCOL,
        "catalog_generation": catalog.get("generation"),
        "catalog_digest": catalog_digest,
        "catalog": json.loads(json.dumps(catalog, allow_nan=False)),
        "feasibility_reports": sorted(
            reports,
            key=lambda report: (str(report["model_id"]), str(report["revision"])),
        ),
        "selection_authority": "qualified_deployment_registry",
        "download_policy": "operator_approval_required",
        "route_ready": False,
    }
    document["lifecycle"] = model_lifecycle_document(
        catalog,
        reports,
    )
    document["operation_digest"] = _digest(document)
    return document


_LIFECYCLE_AUTHORITIES = {
    "incomplete": "local_model_catalog",
    "discovered": "local_model_catalog",
    "compatible": "architecture_runtime_adapter",
    "feasible": "capability_aware_planner",
    "provisioning": "assignment_artifact_acquirer",
    "provisioned": "artifact_integrity_verifier",
    "loaded": "stage_runtime_loader",
    "qualified": "route_qualifier",
    "active": "qualified_deployment_registry",
    "unavailable": "route_qualifier",
    "retired": "operator_retirement_authority",
}
_LIFECYCLE_ORDER = {
    "incomplete": 0,
    "discovered": 1,
    "compatible": 2,
    "feasible": 3,
    "provisioning": 4,
    "provisioned": 5,
    "loaded": 6,
    "unavailable": 7,
    "qualified": 8,
    "active": 9,
    "retired": 10,
}


def _projected_catalog_entries(
    catalog: Mapping[str, object],
) -> list[dict[str, object]]:
    entries = catalog.get("entries")
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES:
        raise ValueError("model lifecycle catalog entries are invalid")
    projected: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("model lifecycle catalog entry is invalid")
        model_id = entry.get("model_id")
        revision = entry.get("revision")
        artifact_digest = entry.get("artifact_digest")
        if (
            not isinstance(model_id, str)
            or not model_id
            or not isinstance(revision, str)
            or _COMMIT_RE.fullmatch(revision) is None
            or not isinstance(artifact_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest) is None
        ):
            raise ValueError("model lifecycle catalog identity is invalid")
        projected.append(entry)
    return projected


def model_lifecycle_document(
    catalog: Mapping[str, object],
    feasibility_reports: Sequence[Mapping[str, object]],
    *,
    deployment_registry: Mapping[str, object] | None = None,
    acquisition_reports: Sequence[Mapping[str, object]] = (),
    artifact_reports: Sequence[Mapping[str, object]] = (),
    load_proofs: Sequence[Mapping[str, object]] = (),
    retired: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    """Resolve each immutable model identity to one authority-owned lifecycle state."""

    if catalog.get("protocol") != MODEL_CATALOG_PROTOCOL:
        raise ValueError("model lifecycle requires a model catalog")
    catalog_digest = catalog.get("catalog_digest")
    if (
        not isinstance(catalog_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", catalog_digest) is None
    ):
        raise ValueError("model lifecycle catalog digest is invalid")
    entries = _projected_catalog_entries(catalog)
    identities = {(str(item["model_id"]), str(item["revision"])) for item in entries}

    evidence: dict[tuple[str, str], list[tuple[str, str, str | None]]] = {
        identity: [] for identity in identities
    }
    for report in feasibility_reports:
        identity = (str(report.get("model_id")), str(report.get("revision")))
        if identity not in evidence:
            raise ValueError("model lifecycle feasibility identity is not catalogued")
        if report.get("state") == "feasible":
            evidence[identity].append(
                (
                    "feasible",
                    "capability_aware_planner",
                    str(report.get("feasibility_digest")),
                )
            )

    for report in acquisition_reports:
        identity = (str(report.get("model_id")), str(report.get("resolved_commit")))
        if identity in evidence and report.get("state") == "acquired":
            evidence[identity].append(
                (
                    "provisioning",
                    "assignment_artifact_acquirer",
                    str(report.get("acquisition_digest")),
                )
            )
    for report in artifact_reports:
        identity = (str(report.get("model_id")), str(report.get("resolved_commit")))
        if identity in evidence and report.get("ready_for_load") is True:
            digest = report.get("artifact_report_digest")
            evidence[identity].append(
                (
                    "provisioned",
                    "artifact_integrity_verifier",
                    digest if isinstance(digest, str) else None,
                )
            )
    for proof in load_proofs:
        identity = (str(proof.get("model_id")), str(proof.get("resolved_commit")))
        if identity in evidence and proof.get("route_ready") is False:
            digest = proof.get("load_proof_digest") or proof.get("probe_digest")
            evidence[identity].append(
                (
                    "loaded",
                    "stage_runtime_loader",
                    digest if isinstance(digest, str) else None,
                )
            )

    deployment_ids: dict[tuple[str, str], list[str]] = {
        identity: [] for identity in identities
    }
    active_deployment: dict[tuple[str, str], str] = {}
    if deployment_registry is not None:
        if (
            deployment_registry.get("protocol")
            != "mycelium.live_deployment_registry.v1"
        ):
            raise ValueError("model lifecycle deployment registry is invalid")
        deployments = deployment_registry.get("deployments")
        selected = deployment_registry.get("selected_deployment_id")
        if not isinstance(deployments, list) or len(deployments) > _MAX_ENTRIES:
            raise ValueError("model lifecycle deployments are invalid")
        for deployment in deployments:
            if not isinstance(deployment, Mapping):
                raise ValueError("model lifecycle deployment is invalid")
            identity = (
                str(deployment.get("model_id")),
                str(deployment.get("model_revision")),
            )
            if identity not in evidence:
                continue
            deployment_id = deployment.get("deployment_id")
            if not isinstance(deployment_id, str) or not deployment_id:
                raise ValueError("model lifecycle deployment identity is invalid")
            deployment_ids[identity].append(deployment_id)
            if deployment.get("health") == "qualified":
                evidence[identity].append(
                    (
                        "qualified",
                        "route_qualifier",
                        str(deployment.get("qualification_id")),
                    )
                )
                if deployment_id == selected:
                    evidence[identity].append(
                        (
                            "active",
                            "qualified_deployment_registry",
                            deployment_id,
                        )
                    )
                    active_deployment[identity] = deployment_id
            else:
                evidence[identity].append(
                    (
                        "unavailable",
                        "route_qualifier",
                        str(deployment.get("health")),
                    )
                )

    retired_set = set(retired)
    models: list[dict[str, object]] = []
    for entry in entries:
        identity = (str(entry["model_id"]), str(entry["revision"]))
        catalog_state = str(entry.get("state"))
        if catalog_state not in {"incomplete", "discovered", "compatible"}:
            raise ValueError("model lifecycle catalog state is invalid")
        reasons = entry.get("reasons")
        reason = (
            str(reasons[0])
            if isinstance(reasons, list) and reasons
            else "catalog_compatible"
        )
        candidates = [
            (
                catalog_state,
                _LIFECYCLE_AUTHORITIES[catalog_state],
                str(entry["artifact_digest"]),
            ),
            *evidence[identity],
        ]
        if identity in retired_set:
            candidates.append(("retired", "operator_retirement_authority", None))
        state, authority, evidence_ref = max(
            candidates,
            key=lambda candidate: _LIFECYCLE_ORDER[candidate[0]],
        )
        if state not in {"incomplete", "discovered", "compatible"}:
            reason = {
                "feasible": "swarm_capacity_feasible",
                "provisioning": "assignment_artifacts_acquired",
                "provisioned": "assignment_artifacts_verified",
                "loaded": "assignment_stage_loaded",
                "qualified": "route_qualification_current",
                "active": "selected_for_future_requests",
                "unavailable": "deployment_not_qualified",
                "retired": "operator_retired",
            }[state]
        models.append(
            {
                "model_id": identity[0],
                "revision": identity[1],
                "artifact_digest": entry["artifact_digest"],
                "state": state,
                "authority": authority,
                "reason": _bounded_reason(reason),
                "evidence_ref": evidence_ref,
                "deployment_ids": sorted(deployment_ids[identity]),
                "active_deployment_id": active_deployment.get(identity),
                "selectable": state in {"qualified", "active"},
            }
        )
    document: dict[str, object] = {
        "protocol": MODEL_LIFECYCLE_PROTOCOL,
        "catalog_digest": catalog_digest,
        "state_definitions": [
            {
                "state": state,
                "authority": authority,
                "selectable": state in {"qualified", "active"},
            }
            for state, authority in _LIFECYCLE_AUTHORITIES.items()
        ],
        "models": sorted(
            models,
            key=lambda item: (str(item["model_id"]), str(item["revision"])),
        ),
        "route_ready": False,
    }
    document["lifecycle_digest"] = _digest(document)
    return document


def enrich_model_operation_lifecycle(
    operation: Mapping[str, object],
    deployment_registry: Mapping[str, object],
) -> dict[str, object]:
    """Attach the current registry-owned qualified/active states to an operation."""

    if operation.get("protocol") != MODEL_OPERATION_PROTOCOL:
        raise ValueError("model operation protocol mismatch")
    catalog = operation.get("catalog")
    reports = operation.get("feasibility_reports")
    if not isinstance(catalog, Mapping) or not isinstance(reports, list):
        raise ValueError("model operation lifecycle inputs are invalid")
    document = json.loads(json.dumps(operation, allow_nan=False))
    document["lifecycle"] = model_lifecycle_document(
        catalog,
        reports,
        deployment_registry=deployment_registry,
    )
    document.pop("operation_digest", None)
    document["operation_digest"] = _digest(document)
    return document
