"""KV transfer vs recompute cost model for recovery (backlog PV4).

When a covered placement is lost and BOTH recoveries are possible -- the A7
fenced KV continuation (the standby holds the acknowledged KV) and the A6
full-context replay (the successor re-prefills prompt + committed tokens) --
this module predicts what each would cost and names the cheaper one::

    kv_bytes     = layers x kv_heads x head_dim x 2 (K and V) x seq x dtype bytes
    transfer_s   = rtt + kv_bytes_to_move / measured link bandwidth (+ import)
    recompute_s  = replay_tokens / measured prefill rate (+ replay start-up)

``kv_bytes`` shrinks with grouped-query / multi-query attention (``kv_heads``),
quantised KV (``dtype`` plus optional group scales), sliding-window layers and
MLA latents; all of that is in :class:`KVShape`.

It is a *policy input only*.  It never makes a recovery possible: the caller
asks it after the closed A7 eligibility predicate and A6 availability already
hold.  Nothing here touches fencing, the committed watermark, exact output or
honest abort.  Unmeasured inputs keep today's behaviour (the A7 continuation)
and say so in the record; a result within the safety margin also keeps the
configured default.

The record it returns is closed and privacy-reduced (counts, bytes, seconds,
source labels -- never token values), so it can sit in the recovery receipt
beside the cutover outcome.

Measurement helpers are pure and clock-injected: :func:`link_from_samples`
(least squares ``t = rtt + bytes / bandwidth``), :func:`probe_link` (a bounded
probe over a caller-supplied send function) and :class:`PrefillRateTracker`
(median of recent prefill timings, falling back to the calibrated rate).

No import from the pinned A6/A7 modules: the entry controller takes the policy
as a plain callable.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

COST_DECISION_PROTOCOL = "mycelium.kv_recovery_cost_decision.v1"
COST_DECISION_FIELDS = frozenset(
    {
        "protocol",
        "choice",
        "basis",
        "missing_inputs",
        "summary",
        "margin",
        "tie_break",
        "kv_dtype",
        "kv_positions",
        "replay_tokens",
        "kv_bytes_total",
        "kv_bytes_to_move",
        "kv_bytes_shadowed",
        "transfer_s",
        "recompute_s",
        "link_bandwidth_bytes_per_s",
        "link_rtt_s",
        "link_source",
        "prefill_tokens_per_s",
        "prefill_source",
    }
)

CHOICES = ("kv", "replay")
BASES = ("clear_win", "within_margin_default", "inputs_unavailable")
DEFAULT_MARGIN = 0.25

# Element width in bits.  The A7 v1 wire carries float32 and float16; the rest
# are the quantised KV layouts a runtime may be configured with.
_DTYPE_BITS = {
    "float32": 32,
    "float16": 16,
    "bfloat16": 16,
    "fp8": 8,
    "int8": 8,
    "int4": 4,
}
_QUANTISED = frozenset({"fp8", "int8", "int4"})


class KVCostError(ValueError):
    """A malformed shape, measurement or decision input (fails closed)."""


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KVCostError(f"{name}_not_a_number")
    return float(value)


def _count(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise KVCostError(f"{name}_invalid")
    return value


def _usable(value: float) -> bool:
    return math.isfinite(value) and value > 0.0


# -- the attention shape ------------------------------------------------------------


@dataclass(frozen=True)
class KVShape:
    """The KV cache geometry of one stage (``layers`` = layers that stage holds)."""

    layers: int
    kv_heads: int
    head_dim: int
    dtype: str = "float16"
    #: Quantised dtypes only: one scale of ``quant_scale_bytes`` per group of elements.
    quant_group_size: int | None = None
    quant_scale_bytes: int = 2
    #: ``windowed_layers`` of the ``layers`` keep only the last ``sliding_window`` positions.
    sliding_window: int | None = None
    windowed_layers: int = 0
    #: MLA: one latent vector of this many elements per layer per token replaces K and V heads.
    mla_latent_dim: int | None = None

    def __post_init__(self) -> None:
        _count(self.layers, "layers", minimum=1)
        _count(self.kv_heads, "kv_heads", minimum=1)
        _count(self.head_dim, "head_dim", minimum=1)
        if self.dtype not in _DTYPE_BITS:
            raise KVCostError("kv_dtype_unknown")
        if self.quant_group_size is not None:
            _count(self.quant_group_size, "quant_group_size", minimum=1)
            if self.dtype not in _QUANTISED:
                raise KVCostError("quant_group_size_needs_quantised_dtype")
        _count(self.quant_scale_bytes, "quant_scale_bytes")
        _count(self.windowed_layers, "windowed_layers")
        if self.windowed_layers > self.layers:
            raise KVCostError("windowed_layers_exceed_layers")
        if self.windowed_layers and self.sliding_window is None:
            raise KVCostError("sliding_window_required")
        if self.sliding_window is not None:
            _count(self.sliding_window, "sliding_window", minimum=1)
        if self.mla_latent_dim is not None:
            _count(self.mla_latent_dim, "mla_latent_dim", minimum=1)

    @property
    def elements_per_layer_token(self) -> int:
        if self.mla_latent_dim is not None:
            return self.mla_latent_dim
        return 2 * self.kv_heads * self.head_dim

    def kv_bytes(self, seq: int) -> int:
        """Bytes of KV this stage holds for ``seq`` cached positions."""

        seq = _count(seq, "seq")
        positions = (self.layers - self.windowed_layers) * seq
        if self.windowed_layers:
            positions += self.windowed_layers * min(seq, self.sliding_window or seq)
        elements = self.elements_per_layer_token * positions
        total = (elements * _DTYPE_BITS[self.dtype] + 7) // 8
        if self.quant_group_size is not None:
            total += -(-elements // self.quant_group_size) * self.quant_scale_bytes
        return total


def shape_from_model_config(config: Mapping[str, Any], *, layers: int | None = None, dtype: str = "float16") -> KVShape:
    """A :class:`KVShape` from a Hugging Face style config (``layers`` = the stage's share)."""

    heads = _count(config.get("num_attention_heads"), "num_attention_heads", minimum=1)
    kv_heads = _count(config.get("num_key_value_heads", heads), "num_key_value_heads", minimum=1)
    head_dim = config.get("head_dim")
    if head_dim is None:
        hidden = _count(config.get("hidden_size"), "hidden_size", minimum=1)
        head_dim = hidden // heads
    total_layers = _count(config.get("num_hidden_layers"), "num_hidden_layers", minimum=1)
    kwargs: dict[str, Any] = {}
    if config.get("use_sliding_window") and config.get("sliding_window"):
        kwargs = {"sliding_window": int(config["sliding_window"]), "windowed_layers": layers or total_layers}
    return KVShape(layers=layers or total_layers, kv_heads=kv_heads, head_dim=int(head_dim), dtype=dtype, **kwargs)


# -- measurements -------------------------------------------------------------------


@dataclass(frozen=True)
class LinkMeasurement:
    """A measured holder -> successor link.  A zero or non-finite value is "not usable"."""

    bandwidth_bytes_per_s: float
    rtt_s: float
    source: str

    def __post_init__(self) -> None:
        if _number(self.bandwidth_bytes_per_s, "bandwidth") < 0 or _number(self.rtt_s, "rtt") < 0:
            raise KVCostError("link_measurement_negative")
        if not isinstance(self.source, str) or not self.source or len(self.source) > 64:
            raise KVCostError("link_source_invalid")

    @property
    def usable(self) -> bool:
        return _usable(self.bandwidth_bytes_per_s) and math.isfinite(self.rtt_s)


@dataclass(frozen=True)
class PrefillMeasurement:
    """The successor's measured prefill rate."""

    tokens_per_s: float
    source: str

    def __post_init__(self) -> None:
        if _number(self.tokens_per_s, "prefill_rate") < 0:
            raise KVCostError("prefill_measurement_negative")
        if not isinstance(self.source, str) or not self.source or len(self.source) > 64:
            raise KVCostError("prefill_source_invalid")

    @property
    def usable(self) -> bool:
        return _usable(self.tokens_per_s)


def link_from_samples(samples: Iterable[tuple[int, float]], *, source: str = "probe") -> LinkMeasurement | None:
    """Least-squares ``seconds = rtt + bytes / bandwidth`` over ``(bytes, seconds)`` samples.

    Needs two distinct payload sizes and a positive size dependence; otherwise
    there is nothing to separate latency from bandwidth and ``None`` comes back
    (a bandwidth is never invented).
    """

    points = [
        (float(size), float(seconds))
        for size, seconds in samples
        if isinstance(size, (int, float)) and isinstance(seconds, (int, float)) and size >= 0 and math.isfinite(seconds) and seconds >= 0
    ]
    if len({size for size, _seconds in points}) < 2:
        return None
    mean_x = sum(size for size, _ in points) / len(points)
    mean_y = sum(seconds for _, seconds in points) / len(points)
    sxx = sum((size - mean_x) ** 2 for size, _ in points)
    sxy = sum((size - mean_x) * (seconds - mean_y) for size, seconds in points)
    slope = sxy / sxx
    if not slope > 0.0:
        return None
    return LinkMeasurement(bandwidth_bytes_per_s=1.0 / slope, rtt_s=max(0.0, mean_y - slope * mean_x), source=source)


def probe_link(
    send: Callable[[int], Any],
    *,
    clock: Callable[[], float] = time.monotonic,
    sizes: Sequence[int] = (4096, 262_144, 1_048_576),
    budget_s: float = 2.0,
    source: str = "probe",
) -> LinkMeasurement | None:
    """A bounded probe: time ``send(nbytes)`` (returns when the peer acknowledged) per size.

    Sizes run smallest first.  A send is skipped when, even if it took as long
    per byte as the previous one, it would overrun ``budget_s`` -- the probe
    cannot stall a recovery.  A failing send ends the probe with whatever was
    measured; fewer than two samples give ``None``.
    """

    if not (isinstance(budget_s, (int, float)) and budget_s > 0):
        raise KVCostError("probe_budget_invalid")
    started = clock()
    samples: list[tuple[int, float]] = []
    for size in sorted(set(int(size) for size in sizes)):
        if size <= 0:
            continue
        if samples:
            last_size, last_seconds = samples[-1]
            if (clock() - started) + last_seconds * (size / last_size) > budget_s:
                break
        begun = clock()
        try:
            send(size)
        except Exception:  # noqa: BLE001 - a dead link is "no measurement", not a crash
            break
        samples.append((size, clock() - begun))
    return link_from_samples(samples, source=source)


class PrefillRateTracker:
    """Median of the last ``window`` prefill timings; the calibrated rate until one exists."""

    def __init__(self, calibration_tokens_per_s: float | None = None, *, window: int = 8) -> None:
        _count(window, "window", minimum=1)
        self._calibration = calibration_tokens_per_s
        self._rates: deque[float] = deque(maxlen=window)

    def observe(self, tokens: int, seconds: float) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or tokens <= 0:
            return
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not _usable(float(seconds)):
            return
        self._rates.append(float(tokens) / float(seconds))

    def measurement(self) -> PrefillMeasurement | None:
        if self._rates:
            ordered = sorted(self._rates)
            middle = len(ordered) // 2
            rate = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
            return PrefillMeasurement(tokens_per_s=rate, source="recent_timings")
        if self._calibration is not None and _usable(float(self._calibration)):
            return PrefillMeasurement(tokens_per_s=float(self._calibration), source="calibration")
        return None


# -- the decision -------------------------------------------------------------------


def _fmt_bytes(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MiB"
    if count >= 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count} B"


def _fmt_seconds(seconds: float) -> str:
    return f"~{seconds:.0f} s" if seconds >= 100.0 else f"~{seconds:.1f} s"


def _summary(record: Mapping[str, Any]) -> str:
    moved = _fmt_bytes(record["kv_bytes_to_move"])
    if record["kv_bytes_shadowed"]:
        moved = f"{moved} pending ({_fmt_bytes(record['kv_bytes_shadowed'])} already shadowed)"
    basis, choice = record["basis"], record["choice"]
    if basis == "inputs_unavailable":
        missing = " and ".join(record["missing_inputs"])
        return f"continued from KV (existing behaviour): {missing} not measured, no cost comparison"
    transfer, recompute = _fmt_seconds(record["transfer_s"]), _fmt_seconds(record["recompute_s"])
    if choice == "kv":
        if record["transfer_s"] <= record["recompute_s"]:
            return f"continued from KV: {moved} in {transfer} beats recompute {recompute}"
        return (
            f"continued from KV: {moved} in {transfer} vs recompute {recompute} "
            f"(replay is not cheaper by the {record['margin']:.0%} margin; KV is the default)"
        )
    if basis == "clear_win":
        return f"recomputed (replay): KV transfer of {moved} would take {transfer} vs recompute {recompute}"
    return (
        f"recomputed (replay): KV transfer of {moved} would take {transfer} vs recompute {recompute} "
        f"(KV is not cheaper by the {record['margin']:.0%} margin; replay is the default)"
    )


def decide_recovery(
    *,
    shape: KVShape,
    kv_positions: int,
    replay_tokens: int,
    link: LinkMeasurement | None,
    prefill: PrefillMeasurement | None,
    pending_positions: int | None = None,
    margin: float = DEFAULT_MARGIN,
    tie_break: str = "kv",
    import_overhead_s: float = 0.0,
    replay_overhead_s: float = 0.0,
) -> dict[str, Any]:
    """Predict both recoveries and choose; return the closed record.

    ``kv_positions`` is the KV length the successor must hold; ``replay_tokens``
    is what a replay re-prefills (prompt + committed tokens).
    ``pending_positions`` are the positions the successor does *not* hold yet
    (``None``: all of them -- a cold transfer; ``0``: the A7 shadow already
    delivered everything).  The KV continuation is chosen when
    ``transfer_s x (1 + margin) <= recompute_s``; replay when
    ``recompute_s x (1 + margin) <= transfer_s``; inside the band ``tie_break``
    decides.  Missing or unusable link / prefill measurements keep ``"kv"``
    (today's behaviour) with ``basis = inputs_unavailable``.
    """

    if not isinstance(shape, KVShape):
        raise KVCostError("shape_invalid")
    positions = _count(kv_positions, "kv_positions")
    replay = _count(replay_tokens, "replay_tokens")
    pending = positions if pending_positions is None else _count(pending_positions, "pending_positions")
    if pending > positions:
        raise KVCostError("pending_positions_exceed_kv_positions")
    margin = _number(margin, "margin")
    if not (math.isfinite(margin) and margin >= 0.0):
        raise KVCostError("margin_invalid")
    if tie_break not in CHOICES:
        raise KVCostError("tie_break_invalid")
    import_s = _number(import_overhead_s, "import_overhead_s")
    replay_s = _number(replay_overhead_s, "replay_overhead_s")
    if import_s < 0 or replay_s < 0:
        raise KVCostError("overhead_negative")

    total = shape.kv_bytes(positions)
    shadowed = shape.kv_bytes(positions - pending)
    to_move = total - shadowed

    missing: list[str] = []
    link_ok = isinstance(link, LinkMeasurement) and link.usable
    if not link_ok and to_move > 0:
        missing.append("link")
    prefill_ok = isinstance(prefill, PrefillMeasurement) and prefill.usable
    if not prefill_ok:
        missing.append("prefill")

    transfer_s: float | None = None
    if link_ok:
        transfer_s = link.rtt_s + to_move / link.bandwidth_bytes_per_s + import_s
    elif to_move == 0:
        transfer_s = import_s
    recompute_s = replay / prefill.tokens_per_s + replay_s if prefill_ok else None

    if missing:
        choice, basis = "kv", "inputs_unavailable"
    elif transfer_s * (1.0 + margin) <= recompute_s:
        choice, basis = "kv", "clear_win"
    elif recompute_s * (1.0 + margin) <= transfer_s:
        choice, basis = "replay", "clear_win"
    else:
        choice, basis = tie_break, "within_margin_default"

    record: dict[str, Any] = {
        "protocol": COST_DECISION_PROTOCOL,
        "choice": choice,
        "basis": basis,
        "missing_inputs": missing,
        "margin": margin,
        "tie_break": tie_break,
        "kv_dtype": shape.dtype,
        "kv_positions": positions,
        "replay_tokens": replay,
        "kv_bytes_total": total,
        "kv_bytes_to_move": to_move,
        "kv_bytes_shadowed": shadowed,
        "transfer_s": transfer_s,
        "recompute_s": recompute_s,
        "link_bandwidth_bytes_per_s": link.bandwidth_bytes_per_s if isinstance(link, LinkMeasurement) else None,
        "link_rtt_s": link.rtt_s if isinstance(link, LinkMeasurement) else None,
        "link_source": link.source if isinstance(link, LinkMeasurement) else None,
        "prefill_tokens_per_s": prefill.tokens_per_s if isinstance(prefill, PrefillMeasurement) else None,
        "prefill_source": prefill.source if isinstance(prefill, PrefillMeasurement) else None,
    }
    record["summary"] = _summary(record)
    return record


def make_recovery_cost_policy(
    *,
    shape_for: Callable[[str], KVShape | None],
    link_for: Callable[[Mapping[str, Any]], LinkMeasurement | None],
    prefill_for: Callable[[Mapping[str, Any]], PrefillMeasurement | None],
    margin: float = DEFAULT_MARGIN,
    tie_break: str = "kv",
    always_move_all: bool = False,
    import_overhead_s: float = 0.0,
) -> Callable[[Mapping[str, Any]], dict[str, Any] | None]:
    """The callable ``A7EntryController.recovery_cost_policy`` expects.

    The cutover context carries ``stage_id``, ``kv_positions``,
    ``replay_tokens`` and ``pending_positions`` (positions the standby has not
    acknowledged; ``0`` whenever the KV continuation is eligible).
    ``always_move_all`` prices a cold pull of the whole KV instead (a holder
    that does not shadow continuously).  A replay also needs one round trip to
    start the prefill on the successor, so a usable link's RTT is charged to
    ``recompute_s`` too.  ``None`` means "no opinion" (no shape known).
    """

    def policy(context: Mapping[str, Any]) -> dict[str, Any] | None:
        shape = shape_for(context["stage_id"])
        if shape is None:
            return None
        link = link_for(context)
        return decide_recovery(
            shape=shape,
            kv_positions=context["kv_positions"],
            replay_tokens=context["replay_tokens"],
            link=link,
            prefill=prefill_for(context),
            pending_positions=None if always_move_all else context.get("pending_positions"),
            margin=margin,
            tie_break=tie_break,
            import_overhead_s=import_overhead_s,
            replay_overhead_s=link.rtt_s if isinstance(link, LinkMeasurement) and link.usable else 0.0,
        )

    return policy


# -- keeping a shadow stream at all -----------------------------------------------------


def shadow_stream_overhead_s(shape: KVShape, *, committed_tokens: int, link: LinkMeasurement) -> float:
    """Time a request spends gated on standby acks: per token one RTT plus that token's KV."""

    count = _count(committed_tokens, "committed_tokens")
    if not isinstance(link, LinkMeasurement) or not link.usable:
        raise KVCostError("link_unusable")
    return count * (link.rtt_s + shape.kv_bytes(1) / link.bandwidth_bytes_per_s)


def shadow_break_even_loss_probability(*, overhead_s: float, recompute_s: float, resume_s: float) -> float:
    """The loss probability above which keeping the shadow beats not shadowing.

    A shadow saves ``recompute_s - resume_s`` when the source is lost and costs
    ``overhead_s`` on every request.  A value above 1 means it never pays,
    ``inf`` that resuming from KV is no cheaper than recomputing.
    """

    saved = _number(recompute_s, "recompute_s") - _number(resume_s, "resume_s")
    if saved <= 0.0:
        return math.inf
    return _number(overhead_s, "overhead_s") / saved
