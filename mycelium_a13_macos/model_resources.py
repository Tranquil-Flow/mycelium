# SPDX-License-Identifier: AGPL-3.0-only
"""Per-model, per-split resource proposals for the A13 app.

Replaces the single 7B-derived ``serving.RESOURCE_PROPOSAL`` as the source of
every resource bound the app enforces or shows:

* the owner/target storage consent ceiling (``app.consent_valid``),
* the pairing quota cap (owner approves a joining Mac's quota),
* each member's worker disk limit and memory/load-peak guard limit,
* the slice-fetch (D-1 relaxed) download and disk budget per member,
* the numbers shown in the consent text and in the run's participant list.

Inputs are the selected model contract (its registered ``ModelShape``: tensor
shapes of the pinned revision, hence exact bf16 source bytes per layer,
embedding and head) and the compiled memory-proportional split
(``layer_split``: each member's stage source bytes and layer range).  All
arithmetic is integer/Fraction; every factor is a named constant below with
its derivation.

The Qwen2.5-7B candidate keeps its measured, hand-derived numbers EXACTLY
(``MEASURED_ANCHORS``): ``model_resource_proposal(CANDIDATE_MODEL)`` equals the
2026-09-16/19 ``RESOURCE_PROPOSAL`` field for field, and so do its per-member
limits (the runner's proposal / 2).  Every other model is modelled, not
measured; see ``REPORT.md`` of lane a13-resources for what the first real
Qwen3-14B run must re-measure.

Nothing here loads tensors, reads the network or qualifies a host.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Any, Iterable, Mapping, Sequence

from .layer_split import (LayerProfile, SplitRefused, memory_proportional_split,
                          policy_for_runtime)

# ---------------------------------------------------------------------------
# Named constants (derivations in the comments; change only with evidence).
# ---------------------------------------------------------------------------

#: The runner derives each member's worker limits as proposal / 2 (two-host
#: legacy).  Kept so an aggregate proposal is still 2 x the largest member
#: limit and the 7B constants stay byte-identical.
RUNNER_LIMIT_DIVISOR = 2
#: Disk headroom over a member's computed disk need (7B: 10%, 2026-09-16).
DISK_HEADROOM = Fraction(110, 100)
#: Headroom over a member's group load peak for the guard (7B: 15% over the
#: guard's own measured self-report, 2026-09-16/19).
GUARD_HEADROOM = Fraction(115, 100)
#: Group RSS load peak per resident weight byte.  Measured on 7B p76r1: the
#: guard saw 7,371,915,264 B (app + worker group) for a stage whose resident
#: int8 weights are 3,811,704,832 B, i.e. 1.934x INCLUDING the app.  2 is the
#: modelled factor; ``GROUP_FIXED_BYTES`` and the KV term are added on top, so
#: the 7B stage models to 8,932,032,512 B (21% above that measurement).
LOAD_PEAK_FACTOR = 2
#: App process + MLX workspace not proportional to the stage: the measured
#: ~625 MB app footprint (09-16) + 512 MiB workspace, rounded to the 1 GiB
#: ``layer_split`` already reserves per member.
GROUP_FIXED_BYTES = 1 << 30
#: Resident bytes per bf16 SOURCE byte at the runtime quantization.
#: int8-weight-only: int8 matrices + float32 row scales + float32 norms;
#: measured 7B upper stage 3,811,704,832 resident / 7,615,620,096 bf16 =
#: 0.50051 -> 501/1000.  ``none`` (float32 weights): 4 B per 2 B source.
RESIDENT_FACTORS: Mapping[str, Fraction] = {
    "int8-weight-only": Fraction(501, 1000),
    "none": Fraction(2),
}
#: KV element size at the runtime activation dtype (KV is float32 today).
KV_BYTES_PER_ELEMENT: Mapping[str, int] = {"float32": 4, "float16": 2}
#: Demo context cap per request: chat prompt <= 2,000 chars (<= 2,000 tokens),
#: template < 100 tokens, <= 256 new tokens (chat.MAX_NEW_TOKENS_CAP) ->
#: 2,356; rounded up to 4,096.  Stage-local KV grows with the sequence, so
#: this bounds it for one in-flight request (the chat runs one at a time).
DEMO_CONTEXT_TOKENS = 4096
#: Inline safetensors headers a member's slice may carry
#: (slice_acquisition.MAX_INLINE_BYTES_PER_NODE).
SLICE_HEADER_ALLOWANCE_BYTES = 4 * 1024 * 1024
#: The signed control archive staged next to the weights (tokenizer, configs,
#: plans): at most managed_offers.MAX_ARCHIVE.  The worker's disk guard walks
#: the whole staged artifact root, so it counts.
CONTROL_ARCHIVE_ALLOWANCE_BYTES = 64 * 1024 * 1024
#: A member's slice is written once by the ranged fetch and copied once more
#: by TargetStaging when it accepts the prepositioned files.
STAGED_COPY_FACTOR = 2
#: Largest guard limit a member may run under, as a share of its RAM: MLX
#: usable memory on Apple silicon is ~70-75% of RAM (qwen3-dense report), the
#: rest is macOS and other apps.  Also the ceiling of the memory a joining
#: Mac may consent to per run (8 GiB Air -> 6 GiB).
MEMBER_PEAK_MEMORY_FRACTION = Fraction(3, 4)
#: Memory-limit choices offered to a joining Mac, as shares of its RAM.
MEMORY_CAP_CHOICES = (Fraction(1, 2), Fraction(5, 8), Fraction(3, 4))
_MEMORY_CAP_GRANULARITY = 256 * 1024 * 1024
MAX_WALL_SECONDS = 1_800
RUN_COUNT = 1


class ResourceRefused(SplitRefused):
    """A bounded refusal with a closed ``code``, the ``node_id`` and a human message."""

    def __init__(self, code: str, node_id: str | None = None, message: str | None = None) -> None:
        super().__init__(code, node_id)
        self.message = message or refusal_message(code)


# ---------------------------------------------------------------------------
# Model shapes (the per-model contract inputs).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelShape:
    """Tensor shapes of one pinned dense Qwen revision (config.json facts).

    ``stores_tied_lm_head``: the checkpoint stores ``lm_head.weight`` although
    embeddings are tied (Qwen3-0.6B does); the split then counts it like the
    safetensors headers do.
    """

    label: str
    num_layers: int
    hidden_size: int
    intermediate_size: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int
    vocab_size: int
    tie_word_embeddings: bool
    attention_bias: bool
    qk_norm: bool
    snapshot_bytes: int
    non_weight_bytes: int
    stores_tied_lm_head: bool = False
    source_bytes_per_parameter: int = 2  # bf16 source

    @property
    def layer_parameters(self) -> int:
        h, q, kv = self.hidden_size, self.num_attention_heads * self.head_dim, self.num_key_value_heads * self.head_dim
        params = h * q + 2 * h * kv + q * h + 3 * h * self.intermediate_size + 2 * h
        if self.attention_bias:
            params += q + 2 * kv
        if self.qk_norm:
            params += 2 * self.head_dim
        return params

    @property
    def layer_source_bytes(self) -> int:
        return self.layer_parameters * self.source_bytes_per_parameter

    @property
    def embedding_source_bytes(self) -> int:
        return self.vocab_size * self.hidden_size * self.source_bytes_per_parameter

    @property
    def output_source_bytes(self) -> int:
        """Final norm, plus lm_head when untied (or stored although tied)."""
        params = self.hidden_size
        if not self.tie_word_embeddings or self.stores_tied_lm_head:
            params += self.vocab_size * self.hidden_size
        return params * self.source_bytes_per_parameter

    @property
    def weight_source_bytes(self) -> int:
        return (self.num_layers * self.layer_source_bytes + self.embedding_source_bytes
                + self.output_source_bytes)

    @property
    def model_type(self) -> str:
        return "qwen3" if self.qk_norm else "qwen2"

    def kv_bytes_per_token_per_layer(self, runtime_dtype: str) -> int:
        size = KV_BYTES_PER_ELEMENT.get(runtime_dtype)
        if size is None:
            raise ResourceRefused("model_resources_dtype_unsupported")
        return 2 * self.num_key_value_heads * self.head_dim * size

    def layer_profile(self) -> LayerProfile:
        """The same profile ``layer_split.model_layer_profile`` reads from headers."""
        return LayerProfile(model_type=self.model_type, num_layers=self.num_layers,
                            layer_bytes=(self.layer_source_bytes,) * self.num_layers,
                            embedding_bytes=self.embedding_source_bytes,
                            output_bytes=self.output_source_bytes,
                            tie_word_embeddings=self.tie_word_embeddings)


@dataclass(frozen=True)
class MeasuredAnchor:
    """Measured numbers that override the model for one contract (7B only)."""

    group_peak_bytes: int          # guard self-report, heavier stage, app incl.
    member_disk_bytes: int         # heavier member's worker disk need
    owner_storage_bytes: int       # largest per-host storage need (consent)
    network_download_bytes: int    # non-weight assets the app may fetch
    source: str


def _key(model: Any) -> tuple[str, str]:
    return (getattr(model, "model_id", None), getattr(model, "resolved_commit", None))


#: Registered shapes, keyed by (model_id, resolved_commit).  Snapshot totals:
#: HF tree of the pinned revision (Qwen3, run_model.CHOICES), the 7B G2
#: derivation, and the A9 0.5B import manifest; non-weight bytes = snapshot
#: minus safetensors files.  Every shape reproduces the safetensors-header
#: profile of the pinned checkpoint (test_model_resources cross-checks the
#: local snapshots of 0.6B and 14B byte for byte).
MODEL_SHAPES: dict[tuple[str, str], ModelShape] = {
    ("Qwen/Qwen2.5-7B-Instruct", "a09a35458c702b33eeacc393d103063234e8bc28"): ModelShape(
        "Qwen2.5-7B", 28, 3584, 18944, 28, 4, 128, 152064, False, True, False,
        snapshot_bytes=15_242_807_270, non_weight_bytes=11_535_382),
    ("Qwen/Qwen2.5-0.5B-Instruct", "7ae557604adf67be50417f59c2c2f167def9a775"): ModelShape(
        "Qwen2.5-0.5B", 24, 896, 4864, 14, 2, 64, 151936, True, True, False,
        snapshot_bytes=999_604_126, non_weight_bytes=11_506_302),
    ("Qwen/Qwen3-0.6B", "c1899de289a04d12100db370d81485cdf75e47ca"): ModelShape(
        "Qwen3-0.6B", 28, 1024, 3072, 16, 8, 128, 151936, True, False, True,
        snapshot_bytes=1_519_209_243, non_weight_bytes=15_908_915, stores_tied_lm_head=True),
    ("Qwen/Qwen3-8B", "b968826d9c46dd6066d109eabc6255188de91218"): ModelShape(
        "Qwen3-8B", 36, 4096, 12288, 32, 8, 128, 151936, False, False, True,
        snapshot_bytes=16_397_461_266, non_weight_bytes=15_944_490),
    ("Qwen/Qwen3-14B", "40c069824f4251a91eefaf281ebe4c544efd3e18"): ModelShape(
        "Qwen3-14B", 40, 5120, 17408, 40, 8, 128, 151936, False, False, True,
        snapshot_bytes=29_552_613_776, non_weight_bytes=15_948_136),
}

#: The 7B candidate's measured derivation (serving.RESOURCE_PROPOSAL comments):
#:   guard: 2 x ceil(1.15 x 7,371,915,264) = 16,955,405,108 (p76r1, owner node);
#:   disk:  2 x ceil(1.10 x 11,297,336,366) = 24,854,140,006 (m4pro worker
#:          stage copy 11,285,830,448 + ~11,505,918 assets);
#:   consent: laptop seeded snapshot 15,242,807,270 + stage copy
#:          7,810,167,792 = 23,052,975,062.
MEASURED_ANCHORS: dict[tuple[str, str], MeasuredAnchor] = {
    ("Qwen/Qwen2.5-7B-Instruct", "a09a35458c702b33eeacc393d103063234e8bc28"): MeasuredAnchor(
        group_peak_bytes=7_371_915_264, member_disk_bytes=11_297_336_366,
        owner_storage_bytes=23_052_975_062, network_download_bytes=11_535_382,
        source="7B balanced split: guard self-report p76r1 (2026-09-19), "
               "covering stage copy runs/wp8-02-g1/g2-derivation.json"),
}


def model_shape(model: Any) -> ModelShape | None:
    return MODEL_SHAPES.get(_key(model))


def require_shape(model: Any) -> ModelShape:
    shape = model_shape(model)
    if shape is None:
        raise ResourceRefused("model_resources_unregistered")
    return shape


def measured_anchor(model: Any) -> MeasuredAnchor | None:
    return MEASURED_ANCHORS.get(_key(model))


def _resident_factor(model: Any) -> Fraction:
    factor = RESIDENT_FACTORS.get(getattr(model, "runtime_quantization", None))
    if factor is None:
        raise ResourceRefused("model_resources_quantization_unsupported")
    return factor


def _ceil(value: Fraction | int) -> int:
    return math.ceil(value)


# ---------------------------------------------------------------------------
# Per-member derivation from the compiled split.
# ---------------------------------------------------------------------------

def _stage_rows(split: Mapping[str, Any], shape: ModelShape) -> list[dict[str, Any]]:
    stages = split.get("stages") if isinstance(split, Mapping) else None
    if not isinstance(stages, list) or not stages:
        raise ResourceRefused("model_resources_split_invalid")
    rows, previous = [], 0
    for stage in stages:
        try:
            node, start, end, size = stage["node_id"], stage["start"], stage["end"], stage["stage_bytes"]
        except (KeyError, TypeError):
            raise ResourceRefused("model_resources_split_invalid") from None
        if (not isinstance(node, str) or any(type(v) is not int for v in (start, end, size))
                or start != previous or end <= start or end > shape.num_layers or size <= 0):
            raise ResourceRefused("model_resources_split_invalid")
        previous = end
        rows.append({"node_id": node, "start": start, "end": end, "stage_source_bytes": size,
                     "memory_bytes": next((m.get("memory_bytes") for m in split.get("members", [])
                                           if isinstance(m, Mapping) and m.get("node_id") == node), None)})
    if previous != shape.num_layers:
        raise ResourceRefused("model_resources_split_invalid")
    return rows


def member_resources(model: Any, split: Mapping[str, Any], *, owner_node_id: str | None = None,
                     slice_mode: bool = True) -> list[dict[str, Any]]:
    """One row per member: what its share needs and the limits it runs under."""
    shape = require_shape(model)
    anchor = measured_anchor(model)
    factor = _resident_factor(model)
    kv_per_layer = shape.kv_bytes_per_token_per_layer(getattr(model, "runtime_dtype", None))
    result = []
    for row in _stage_rows(split, shape):
        stage = row["stage_source_bytes"]
        layers = row["end"] - row["start"]
        resident = _ceil(stage * factor)
        kv = layers * kv_per_layer * DEMO_CONTEXT_TOKENS
        if anchor is not None:
            peak, basis = anchor.group_peak_bytes, "measured"
            disk_need = anchor.member_disk_bytes
        else:
            peak, basis = LOAD_PEAK_FACTOR * resident + GROUP_FIXED_BYTES + kv, "modelled"
            disk_need = (stage + SLICE_HEADER_ALLOWANCE_BYTES if slice_mode else shape.snapshot_bytes + stage) \
                + CONTROL_ARCHIVE_ALLOWANCE_BYTES
        max_rss = _ceil(GUARD_HEADROOM * peak)
        max_disk = _ceil(DISK_HEADROOM * disk_need)
        footprint = STAGED_COPY_FACTOR * disk_need
        if row["node_id"] == owner_node_id and slice_mode and anchor is None:
            footprint += shape.weight_source_bytes  # owner compile emits every member's stage shards
        memory = row["memory_bytes"]
        result.append({
            "node_id": row["node_id"], "start": row["start"], "end": row["end"], "layers": layers,
            "stage_source_bytes": stage, "resident_bytes": resident, "kv_bytes": kv,
            "load_peak_bytes": peak, "max_rss_bytes": max_rss, "disk_need_bytes": disk_need,
            "max_disk_bytes": max_disk, "disk_footprint_bytes": footprint, "basis": basis,
            "memory_bytes": memory,
            "memory_ceiling_bytes": None if type(memory) is not int else member_memory_ceiling(memory),
        })
    return result


def aggregate_proposal(model: Any, members: Sequence[Mapping[str, Any]]):
    """The run's ``ResourceProposal``: 2 x the largest member limit (runner halves it)."""
    from .serving import ResourceProposal
    shape = require_shape(model)
    anchor = measured_anchor(model)
    network = anchor.network_download_bytes if anchor is not None else shape.snapshot_bytes
    return ResourceProposal(
        network_download_bytes=network,
        execution_download_bytes=0,
        additional_disk_bytes=RUNNER_LIMIT_DIVISOR * max(int(m["max_disk_bytes"]) for m in members),
        max_combined_rss_bytes=RUNNER_LIMIT_DIVISOR * max(int(m["max_rss_bytes"]) for m in members),
        max_wall_seconds=MAX_WALL_SECONDS,
        run_count=RUN_COUNT,
    )


def split_resource_proposal(model: Any, split: Mapping[str, Any], **options: Any):
    return aggregate_proposal(model, member_resources(model, split, **options))


def _whole_model_split(shape: ModelShape) -> dict[str, Any]:
    profile = shape.layer_profile()
    tail = profile.output_bytes + (profile.embedding_bytes if profile.tie_word_embeddings else 0)
    return {"stages": [{"node_id": "whole-model", "start": 0, "end": shape.num_layers,
                        "stage_bytes": profile.embedding_bytes + sum(profile.layer_bytes) + tail}],
            "members": []}


def model_resource_proposal(model: Any):
    """Model-level (pre-split) proposal: an upper bound for every member of any split.

    Anchored contracts (7B) return their measured proposal; others the limits
    of one hypothetical member holding the whole model.
    """
    shape = require_shape(model)
    return split_resource_proposal(model, _whole_model_split(shape))


def whole_model_member(model: Any) -> dict[str, Any]:
    return member_resources(model, _whole_model_split(require_shape(model)))[0]


# ---------------------------------------------------------------------------
# Consent ceilings and requirements.
# ---------------------------------------------------------------------------

def owner_storage_requirement(model: Any) -> int:
    """Storage the owner must consent to for this model.

    The owner's single largest enforced item is the pinned snapshot download
    (acquisition storage limit); its own slice fetch and staging are each
    smaller than the snapshot.  7B: the measured seeded-path figure.
    """
    shape = require_shape(model)
    anchor = measured_anchor(model)
    if anchor is not None:
        return anchor.owner_storage_bytes
    return _ceil(DISK_HEADROOM * shape.snapshot_bytes)


def model_storage_ceiling(model: Any) -> int:
    """Largest storage consent meaningful for this model on any host."""
    return max(owner_storage_requirement(model), model_resource_proposal(model).additional_disk_bytes
               if measured_anchor(model) is not None else whole_model_member(model)["max_disk_bytes"])


def _registered() -> Iterable[Any]:
    from .serving import REGISTERED_MODELS
    return [m for m in REGISTERED_MODELS if model_shape(m) is not None]


def storage_consent_ceiling() -> int:
    """``app.consent_valid``'s bound: the largest per-model storage ceiling."""
    return max(model_storage_ceiling(m) for m in _registered())


def pairing_quota_ceiling() -> int:
    """Largest storage/network quota an owner may approve for a joining Mac.

    A member stores only its slice; the largest slice any registered model can
    give one member is the whole model (7B: its measured proposal).
    """
    values = []
    for model in _registered():
        if measured_anchor(model) is not None:
            values.append(model_resource_proposal(model).additional_disk_bytes)
        else:
            values.append(whole_model_member(model)["max_disk_bytes"])
    return max(values)


def member_memory_ceiling(memory_bytes: int) -> int:
    if type(memory_bytes) is not int or memory_bytes <= 0:
        raise ResourceRefused("member_capability_invalid")
    return math.floor(memory_bytes * MEMBER_PEAK_MEMORY_FRACTION)


def memory_cap_options(memory_bytes: int) -> list[int]:
    """Realistic per-run memory limits a Mac with this RAM may consent to."""
    ceiling = member_memory_ceiling(memory_bytes)
    values = set()
    for share in MEMORY_CAP_CHOICES:
        value = math.floor(memory_bytes * share) // _MEMORY_CAP_GRANULARITY * _MEMORY_CAP_GRANULARITY
        if 0 < value <= ceiling:
            values.add(value)
    return sorted(values)


# ---------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------

def _gb(value: int | None) -> str:
    return "?" if value is None else f"{value / 1e9:.1f} GB"


_MESSAGES = {
    "member_memory_insufficient_for_share": (
        "{who} has {memory} of memory. Its share of {model} (layers {first}-{last}) needs "
        "about {need} at its load peak, more than the {ceiling} this app lets a Mac with "
        "that memory use. Add a Mac with more memory, or choose a smaller model."),
    "member_disk_insufficient_for_share": (
        "{who} has {free} of free disk. Its share of {model} (layers {first}-{last}) needs "
        "about {need} on disk for this run. Free some disk space on that Mac, or choose a "
        "smaller model."),
    "member_memory_cap_below_share": (
        "{who} agreed to at most {cap} of memory per run, but its share of {model} (layers "
        "{first}-{last}) needs {need}. Raise that Mac's memory limit (rejoin with a larger "
        "limit), add a Mac, or choose a smaller model."),
    "member_storage_cap_below_share": (
        "{who} agreed to at most {cap} of storage, but its share of {model} (layers "
        "{first}-{last}) needs {need}. Raise that Mac's storage limit, add a Mac, or choose "
        "a smaller model."),
    "owner_storage_consent_below_model": (
        "This Mac's storage limit is {cap}, but {model} needs {need} here (its pinned "
        "download plus headroom). Choose a larger storage limit or a smaller model."),
    "member_cannot_hold_share": (
        "{who} cannot hold even the smallest share of {model} within half of its memory. "
        "Add a Mac with more memory, or choose a smaller model."),
    "model_resources_unregistered": (
        "This model has no registered resource derivation, so the app cannot bound it."),
}
#: Owner-side wording when the member's own caps are not known to the owner.
_MESSAGES_NO_CAP = {
    "member_memory_cap_below_share": (
        "{who} refused its share of {model} (layers {first}-{last}): it needs {need} of memory "
        "per run, more than that Mac agreed to. Raise that Mac's memory limit (rejoin with a "
        "larger limit), add a Mac, or choose a smaller model."),
    "member_storage_cap_below_share": (
        "{who} refused its share of {model} (layers {first}-{last}): it needs {need} of "
        "storage, more than that Mac agreed to. Raise that Mac's storage limit, add a Mac, or "
        "choose a smaller model."),
}
_GENERIC = "A Mac cannot hold its share of this model."


def refusal_message(code: str, row: Mapping[str, Any] | None = None, *, model: Any = None,
                    who: str | None = None, cap: int | None = None, free: int | None = None,
                    need: int | None = None) -> str:
    template = _MESSAGES.get(code)
    if cap is None and code in _MESSAGES_NO_CAP:
        template = _MESSAGES_NO_CAP[code]
    if template is None:
        return _GENERIC
    row = row or {}
    shape = model_shape(model) if model is not None else None
    node = row.get("node_id")
    values = {
        "who": who or ("The Mac " + node[:12] if isinstance(node, str) else "A Mac"),
        "model": shape.label if shape is not None else "the selected model",
        "first": row.get("start", 0) + 1 if row else "?", "last": row.get("end", "?"),
        "memory": _gb(row.get("memory_bytes")), "ceiling": _gb(row.get("memory_ceiling_bytes")),
        "cap": _gb(cap), "free": _gb(free),
        "need": _gb(need if need is not None else row.get("max_rss_bytes")),
    }
    return template.format(**values)


def check_members(model: Any, members: Sequence[Mapping[str, Any]],
                  capabilities: Mapping[str, Mapping[str, Any]] | None = None) -> None:
    """Refuse (by name) the first member whose RAM or free disk cannot hold its share."""
    capabilities = capabilities or {}
    for row in members:
        ceiling = row.get("memory_ceiling_bytes")
        if ceiling is not None and row["max_rss_bytes"] > ceiling:
            code = "member_memory_insufficient_for_share"
            raise ResourceRefused(code, row["node_id"], refusal_message(code, row, model=model))
        free = (capabilities.get(row["node_id"]) or {}).get("available_storage_bytes")
        # Measured (7B) rows keep their pre-staged disk accounting unchecked here.
        if row.get("basis") == "modelled" and type(free) is int and row["disk_footprint_bytes"] > free:
            code = "member_disk_insufficient_for_share"
            raise ResourceRefused(code, row["node_id"], refusal_message(
                code, row, model=model, free=free, need=row["disk_footprint_bytes"]))


#: Fields of a member row the owner announces to that member (no RAM/disk facts
#: of other Macs).
PUBLIC_RESOURCE_FIELDS = ("node_id", "start", "end", "stage_source_bytes", "resident_bytes",
                          "kv_bytes", "load_peak_bytes", "max_rss_bytes", "max_disk_bytes",
                          "disk_footprint_bytes", "basis")


def public_member_resources(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in PUBLIC_RESOURCE_FIELDS}


def check_member_caps(row: Mapping[str, Any], caps: Mapping[str, Any]) -> str | None:
    """A joined Mac's standing caps against its share: ``None`` or a closed code."""
    if type(caps.get("max_memory_bytes")) is int and caps["max_memory_bytes"] < row["max_rss_bytes"]:
        return "member_memory_cap_below_share"
    if type(caps.get("storage_bytes")) is int and caps["storage_bytes"] < row["max_disk_bytes"]:
        return "member_storage_cap_below_share"
    return None


def cap_refusal_message(code: str, row: Mapping[str, Any], caps: Mapping[str, Any], *,
                        model: Any = None) -> str:
    if code == "member_storage_cap_below_share":
        return refusal_message(code, row, model=model, cap=caps.get("storage_bytes"),
                               need=row["max_disk_bytes"])
    return refusal_message(code, row, model=model, cap=caps.get("max_memory_bytes"),
                           need=row["max_rss_bytes"])


def work_limits_for(row: Mapping[str, Any], *, storage_bytes: int | None = None,
                    max_wall_seconds: int = MAX_WALL_SECONDS, max_commands: int = 5000) -> dict[str, int]:
    """Worker limits for one member: its guard and disk limit (capped by its consent)."""
    disk = int(row["max_disk_bytes"])
    if type(storage_bytes) is int and storage_bytes > 0:
        disk = min(disk, storage_bytes)
    return {"max_rss_bytes": int(row["max_rss_bytes"]), "max_disk_bytes": disk,
            "max_wall_seconds": min(int(max_wall_seconds), 3600), "max_commands": int(max_commands)}


# ---------------------------------------------------------------------------
# Planning helpers (UI, report, tests).
# ---------------------------------------------------------------------------

def plan_for_members(model: Any, members: Sequence[Mapping[str, Any]], *,
                     owner_node_id: str | None = None, slice_mode: bool = True) -> dict[str, Any]:
    """Split ``members`` (node_id, memory_bytes) for ``model`` and derive every bound."""
    shape = require_shape(model)
    split = memory_proportional_split(list(members), shape.layer_profile(),
                                      policy_for_runtime(getattr(model, "runtime_quantization", None)))
    rows = member_resources(model, split, owner_node_id=owner_node_id, slice_mode=slice_mode)
    return {"split": split, "members": rows, "proposal": aggregate_proposal(model, rows)}


def consent_document(model: Any = None, *, device_memory_bytes: int | None = None) -> dict[str, Any]:
    """Numbers the consent UI shows (the same functions enforce them)."""
    from .serving import REGISTERED_MODELS
    models = []
    for candidate in REGISTERED_MODELS:
        shape = model_shape(candidate)
        if shape is None:
            continue
        whole = whole_model_member(candidate)
        models.append({"model_id": candidate.model_id, "resolved_commit": candidate.resolved_commit,
                       "label": shape.label, "snapshot_bytes": shape.snapshot_bytes,
                       "weight_source_bytes": shape.weight_source_bytes,
                       "owner_storage_bytes": owner_storage_requirement(candidate),
                       "whole_model_member_disk_bytes": whole["max_disk_bytes"],
                       "whole_model_member_rss_bytes": whole["max_rss_bytes"]})
    selected = None
    if model is not None and model_shape(model) is not None:
        selected = next(m for m in models if (m["model_id"], m["resolved_commit"]) == _key(model))
    document = {"storage_consent_ceiling_bytes": storage_consent_ceiling(),
                "pairing_quota_ceiling_bytes": pairing_quota_ceiling(),
                "selected": selected, "models": models,
                "device_memory_bytes": device_memory_bytes,
                "memory_cap_ceiling_bytes": None, "memory_cap_options": []}
    if type(device_memory_bytes) is int and device_memory_bytes > 0:
        document["memory_cap_ceiling_bytes"] = member_memory_ceiling(device_memory_bytes)
        document["memory_cap_options"] = memory_cap_options(device_memory_bytes)
    return document


__all__ = [
    "CONTROL_ARCHIVE_ALLOWANCE_BYTES", "DEMO_CONTEXT_TOKENS", "DISK_HEADROOM", "GROUP_FIXED_BYTES", "GUARD_HEADROOM",
    "LOAD_PEAK_FACTOR", "MEASURED_ANCHORS", "MEMBER_PEAK_MEMORY_FRACTION", "MODEL_SHAPES",
    "MeasuredAnchor", "ModelShape", "RESIDENT_FACTORS", "RUNNER_LIMIT_DIVISOR",
    "ResourceRefused", "STAGED_COPY_FACTOR", "aggregate_proposal", "cap_refusal_message",
    "check_member_caps", "check_members", "consent_document", "member_memory_ceiling",
    "member_resources", "memory_cap_options", "model_resource_proposal", "model_shape",
    "model_storage_ceiling", "owner_storage_requirement", "pairing_quota_ceiling",
    "PUBLIC_RESOURCE_FIELDS", "plan_for_members", "public_member_resources", "refusal_message", "require_shape", "split_resource_proposal",
    "storage_consent_ceiling", "whole_model_member", "work_limits_for",
]
