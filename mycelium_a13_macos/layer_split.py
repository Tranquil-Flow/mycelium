# SPDX-License-Identifier: AGPL-3.0-only
"""Memory-proportional contiguous layer split for 2..8 A13 hosts.

Pure and deterministic: the same member reports and checkpoint headers always
give the same ranges and the same ``split_digest``. Only safetensors *headers*
are read (never tensor data); nothing here loads a model, qualifies a host or
marks a route ready. A split is an owner placement proposal that the native
builder, staging and qualification still independently enforce.

Weighting (all integer/Fraction arithmetic, no float rounding drift):

* budget_i  = floor(memory_bytes_i * memory_fraction)    (default 1/2 of RAM)
* weight_i  = (budget_i - runtime_overhead_bytes) * speed_i
* speed_i   = decode_tokens_per_second_i / lower-median, clamped to [1/2, 2],
              applied only when EVERY member reports a microbench; else 1.

Stage boundaries are placed where the cumulative stage load (first-stage
embedding + layers so far) is closest to the cumulative weight share of the
total load (embedding + layers + output head + a tied-embedding copy on the
last stage), ties to the smaller index, with at least one layer per stage.
Each member must then hold its own stage: ``stage_bytes * resident_factor +
runtime_overhead_bytes <= budget``; otherwise that member is refused by name.

``resident_factor`` defaults to 1 (bf16 source bytes). Measured on the 7B
route (serving.py RESOURCE_PROPOSAL: 7,371,915,264 B group RSS for an
11,285,830,448 B bf16 stage, i.e. ~0.65x incl. the app) this is conservative;
the 1 GiB runtime overhead covers the measured ~625 MB app footprint plus a
512 MiB workspace.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from typing import Any, Mapping, Sequence

PROTOCOL = "mycelium.a13_layer_split.v1"
MIN_HOSTS = 2
MAX_HOSTS = 8
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_HEADER_BYTES = 100 * 1024 * 1024
_MAX_LAYERS = 1024


class SplitRefused(ValueError):
    """A bounded, stable refusal; ``node_id`` names the member when relevant."""

    def __init__(self, code: str, node_id: str | None = None) -> None:
        self.code = code
        self.node_id = node_id
        super().__init__(code)


@dataclass(frozen=True)
class LayerNaming:
    """Tensor naming of a dense decoder checkpoint (the model-contract seam).

    Qwen2 and Qwen3 dense share it; a new family registers its own instance.
    """

    model_types: frozenset = frozenset({"qwen2", "qwen3"})
    layer_pattern: str = r"^model\.layers\.(\d+)\."
    embedding_prefixes: tuple = ("model.embed_tokens.",)
    output_prefixes: tuple = ("model.norm.", "lm_head.")


QWEN_DENSE_NAMING = LayerNaming()


@dataclass(frozen=True)
class SplitPolicy:
    memory_fraction_numerator: int = 1
    memory_fraction_denominator: int = 2
    resident_factor_numerator: int = 1
    resident_factor_denominator: int = 1
    runtime_overhead_bytes: int = 1 << 30
    speed_clamp_low_numerator: int = 1
    speed_clamp_low_denominator: int = 2
    speed_clamp_high: int = 2


DEFAULT_POLICY = SplitPolicy()
#: int8-weight-only runtime from a bf16 source: the 7B route's measured group
#: RSS (7,371,915,264 B incl. ~625 MB app) over its 11,285,830,448 B bf16 stage
#: is ~0.60x with the load transient; 5/8 keeps that measurement as a floor.
INT8_WEIGHT_ONLY_POLICY = SplitPolicy(resident_factor_numerator=5, resident_factor_denominator=8)


def policy_for_runtime(runtime_quantization: object) -> SplitPolicy:
    """The split policy for a model contract's runtime representation."""
    if runtime_quantization == "int8-weight-only":
        return INT8_WEIGHT_ONLY_POLICY
    return DEFAULT_POLICY


@dataclass(frozen=True)
class LayerProfile:
    model_type: str
    num_layers: int
    layer_bytes: tuple
    embedding_bytes: int
    output_bytes: int
    tie_word_embeddings: bool

    def document(self) -> dict[str, Any]:
        value = asdict(self)
        value["layer_bytes"] = list(self.layer_bytes)
        return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _read_header(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(8)
            if len(raw) != 8:
                raise ValueError
            length = struct.unpack("<Q", raw)[0]
            if not 2 <= length <= _MAX_HEADER_BYTES:
                raise ValueError
            body = handle.read(length)
            if len(body) != length:
                raise ValueError
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError
    except (OSError, ValueError, UnicodeDecodeError):
        raise SplitRefused("model_header_invalid") from None
    document.pop("__metadata__", None)
    return document


def _weight_files(root: Path) -> list[Path]:
    index = root / "model.safetensors.index.json"
    if index.is_file():
        try:
            weight_map = json.loads(index.read_text("utf-8"))["weight_map"]
            names = sorted(set(weight_map.values()))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise SplitRefused("model_index_invalid") from None
    else:
        names = ["model.safetensors"]
    paths = []
    for name in names:
        if not isinstance(name, str) or "/" in name or name.startswith("."):
            raise SplitRefused("model_index_invalid")
        paths.append(root / name)
    return paths


def model_layer_profile(source_root: str | Path, naming: LayerNaming = QWEN_DENSE_NAMING) -> LayerProfile:
    """Per-layer and static tensor bytes from config + safetensors headers."""
    root = Path(source_root)
    try:
        config = json.loads((root / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        raise SplitRefused("model_config_invalid") from None
    if not isinstance(config, dict) or config.get("model_type") not in naming.model_types:
        raise SplitRefused("model_architecture_unsupported")
    layers = config.get("num_hidden_layers")
    if type(layers) is not int or not 1 <= layers <= _MAX_LAYERS:
        raise SplitRefused("model_config_invalid")
    tied = config.get("tie_word_embeddings", False)
    if type(tied) is not bool:
        raise SplitRefused("model_config_invalid")
    pattern = re.compile(naming.layer_pattern)
    per_layer = [0] * layers
    embedding = output = 0
    seen: set[str] = set()
    for path in _weight_files(root):
        for name, record in _read_header(path).items():
            try:
                start, end = record["data_offsets"]
                if not (_int(start) and _int(end) and end >= start) or name in seen:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise SplitRefused("model_header_invalid") from None
            seen.add(name)
            size = end - start
            matched = pattern.match(name)
            if matched is not None:
                index = int(matched.group(1))
                if index >= layers:
                    raise SplitRefused("model_layer_out_of_range")
                per_layer[index] += size
            elif name.startswith(naming.embedding_prefixes):
                embedding += size
            elif name.startswith(naming.output_prefixes):
                output += size
            else:
                raise SplitRefused("model_tensor_unsupported")
    if any(size == 0 for size in per_layer) or embedding == 0:
        raise SplitRefused("model_header_invalid")
    return LayerProfile(model_type=config["model_type"], num_layers=layers,
                        layer_bytes=tuple(per_layer), embedding_bytes=embedding,
                        output_bytes=output, tie_word_embeddings=tied)


def _validated_members(members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(members, (list, tuple)) or not MIN_HOSTS <= len(members) <= MAX_HOSTS:
        raise SplitRefused("host_count_invalid")
    result, seen = [], set()
    for member in members:
        if not isinstance(member, Mapping):
            raise SplitRefused("member_capability_invalid")
        node = member.get("node_id")
        if not isinstance(node, str) or _NODE_RE.fullmatch(node) is None:
            raise SplitRefused("member_capability_invalid")
        if node in seen:
            raise SplitRefused("member_duplicate", node)
        seen.add(node)
        memory = member.get("memory_bytes")
        if type(memory) is not int or memory <= 0:
            raise SplitRefused("member_capability_invalid", node)
        row = {"node_id": node, "memory_bytes": memory}
        if "decode_tokens_per_second" in member:
            tps = member["decode_tokens_per_second"]
            if (type(tps) not in (int, float) or not math.isfinite(tps) or tps <= 0):
                raise SplitRefused("member_capability_invalid", node)
            row["decode_tokens_per_second"] = tps
        result.append(row)
    return result


def _speeds(members: list[dict[str, Any]], policy: SplitPolicy) -> tuple[list[Fraction], str]:
    if not all("decode_tokens_per_second" in m for m in members):
        return [Fraction(1)] * len(members), "memory"
    values = [Fraction(m["decode_tokens_per_second"]) for m in members]
    median = sorted(values)[(len(values) - 1) // 2]
    low = Fraction(policy.speed_clamp_low_numerator, policy.speed_clamp_low_denominator)
    high = Fraction(policy.speed_clamp_high)
    return [min(high, max(low, value / median)) for value in values], "memory_and_decode_microbench"


def memory_proportional_split(members: Sequence[Mapping[str, Any]], profile: LayerProfile,
                              policy: SplitPolicy = DEFAULT_POLICY) -> dict[str, Any]:
    """Contiguous ranges, in the given member order, proportional to memory."""
    rows = _validated_members(members)
    count = len(rows)
    if not isinstance(profile, LayerProfile) or profile.num_layers != len(profile.layer_bytes):
        raise SplitRefused("model_profile_invalid")
    if count > profile.num_layers:
        raise SplitRefused("model_too_small_for_members")
    fraction = Fraction(policy.memory_fraction_numerator, policy.memory_fraction_denominator)
    resident = Fraction(policy.resident_factor_numerator, policy.resident_factor_denominator)
    overhead = policy.runtime_overhead_bytes
    speeds, weighting = _speeds(rows, policy)
    budgets = [math.floor(row["memory_bytes"] * fraction) for row in rows]
    weights = []
    for row, budget, speed in zip(rows, budgets, speeds):
        if budget <= overhead:
            raise SplitRefused("member_cannot_hold_share", row["node_id"])
        weights.append((budget - overhead) * speed)
    layers = profile.layer_bytes
    tail = profile.output_bytes + (profile.embedding_bytes if profile.tie_word_embeddings else 0)
    total = profile.embedding_bytes + sum(layers) + tail
    cumulative = [profile.embedding_bytes]
    for size in layers:
        cumulative.append(cumulative[-1] + size)
    weight_total = sum(weights)
    boundaries, previous, running = [0], 0, Fraction(0)
    for index in range(count - 1):
        running += weights[index]
        target = total * running / weight_total
        low, high = previous + 1, profile.num_layers - (count - 1 - index)
        best = min(range(low, high + 1), key=lambda b: (abs(cumulative[b] - target), b))
        boundaries.append(best)
        previous = best
    boundaries.append(profile.num_layers)
    stages = []
    for index, row in enumerate(rows):
        start, end = boundaries[index], boundaries[index + 1]
        stage = sum(layers[start:end])
        if index == 0:
            stage += profile.embedding_bytes
        if index == count - 1:
            stage += tail
        cost = math.ceil(stage * resident) + overhead
        stages.append({"node_id": row["node_id"], "start": start, "end": end,
                       "stage_bytes": stage, "cost_bytes": cost, "budget_bytes": budgets[index]})
    for stage in stages:
        if stage["end"] - stage["start"] < 1 or stage["cost_bytes"] > stage["budget_bytes"]:
            raise SplitRefused("member_cannot_hold_share", stage["node_id"])
    document = {
        "protocol": PROTOCOL,
        "model_type": profile.model_type,
        "num_layers": profile.num_layers,
        "profile_digest": _digest(profile.document()),
        "policy": asdict(policy),
        "members": rows,
        "weighting": weighting,
        "stages": stages,
    }
    document["split_digest"] = _digest(document)
    return document


def split_ranges(split: Mapping[str, Any]) -> list[list[int]]:
    """``[[start, end), ...]`` in member order, re-checked against the digest."""
    body = {k: v for k, v in split.items() if k != "split_digest"}
    if split.get("protocol") != PROTOCOL or _digest(body) != split.get("split_digest"):
        raise SplitRefused("layer_split_digest_mismatch")
    return [[stage["start"], stage["end"]] for stage in split["stages"]]


__all__ = ["DEFAULT_POLICY", "INT8_WEIGHT_ONLY_POLICY", "policy_for_runtime", "LayerNaming", "LayerProfile", "MAX_HOSTS", "MIN_HOSTS",
           "PROTOCOL", "QWEN_DENSE_NAMING", "SplitPolicy", "SplitRefused",
           "memory_proportional_split", "model_layer_profile", "split_ranges"]
