# SPDX-License-Identifier: AGPL-3.0-or-later
"""Speed-aware placement: per-member step calibration, split choice, single-Mac fast path.

PLAN SELECTION ONLY.  Nothing here loads a model into a route, changes route
internals, or marks anything ready; the output is a split document in the same
shape as ``layer_split.memory_proportional_split`` (so the native builder,
staging and qualification still enforce it) plus a ``placement`` block that the
owner shows as one sentence.

1. Join-time calibration (member side).  Each member times, on its OWN MLX
   runtime and with the weights it already holds (no download), the per-layer
   decode step and prefill throughput of one real layer of the selected model.
   Without usable weights/MLX it reports a documented chip-identity estimate
   (published memory bandwidth / GPU FLOPs) labelled ``estimated``.  The record
   rides in the member's signed run-exchange report as the optional,
   closed ``step_calibration`` field (``run_exchange._OPTIONAL_REPORT_KEYS``).
2. Placement (owner side).  The contiguous split that minimizes the slowest
   stage's decode time (compute + its outbound link latency), prefill time as
   tie-break, subject to every member's memory budget (the same budget rule as
   ``layer_split``: policy fraction of RAM, the member's standing cap, and the
   3/4-RAM guard) and the per-stage overhead/resident rules of the memory split.
3. Fast path.  If the model fits one Mac's budget and its predicted single-node
   token latency beats the best split's (hop RTT included), the decision is
   ``single``.  ``SINGLE_HOST_ROUTE_SUPPORTED`` is False: the route builder
   needs >= 2 hosts, so the decision is reported (``applied: false``) while the
   best split is still what compiles; the owner can force ``always_split``.
4. ``placement.explanation`` is the one sentence for /api/status and the UI.

Model of time (all integer nanoseconds, deterministic):
  stage decode  = layers * decode_ns_per_layer (+ head/tied-embedding as layer
                  equivalents on the last stage) + one-way latency to the next
                  stage (last stage -> first: the sampled token returns);
  one-way       = measured median RTT / 2 per hop; a hop between two members is
                  rtt_a + rtt_b when each reports RTT to the owner (the owner
                  reports 0), unless a direct pair RTT is supplied;
  token latency = sum of stage computes + sum of one-way latencies (single
                  stream; a pipeline only overlaps stages across concurrent
                  requests, which this model deliberately does not credit).
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .layer_split import (DEFAULT_POLICY, MAX_HOSTS, MIN_HOSTS, PROTOCOL, LayerProfile,
                          SplitPolicy, SplitRefused, _digest, _validated_members,
                          memory_proportional_split)

CALIBRATION_PROTOCOL = 'mycelium.a13_step_calibration.v1'
PLACEMENT_PROTOCOL = 'mycelium.a13_placement.v1'
SETTING_PROTOCOL = 'mycelium.a13_placement_setting.v1'
SETTING_FILE = 'placement.json'
SETTING_ENVIRONMENT = 'MYCELIUM_A13_PLACEMENT'
MODES = ('auto', 'always_split')
#: The route builder needs >= 2 hosts (layer_split.MIN_HOSTS); a ``single``
#: decision is reported, not yet executed.  Flip when a one-host route exists.
SINGLE_HOST_ROUTE_SUPPORTED = False

METHOD_MEASURED = 'mlx_layer_matmul_proxy.v1'
METHOD_ESTIMATED = 'chip_bandwidth_flops_estimate.v1'
BASES = ('measured', 'estimated')
MAX_PROBE_LAYER_BYTES = 1 << 30
#: Same rule as model_resources.MEMBER_PEAK_MEMORY_FRACTION (not imported: that module imports this one's siblings).
MEMORY_GUARD_FRACTION = Fraction(3, 4)
#: A hop faster than this per-layer ratio is "about the same speed".
EVEN_RATIO = 1.15

_TEXT = re.compile(r'[ -~]{1,96}\Z')
_NS = 1_000_000

# -- chip identity -> published memory bandwidth (GB/s) and GPU FP16/FP32 TFLOPS.
# Rounded vendor figures; an ESTIMATE only (labelled so), never a measurement.
_CHIP_TABLE: tuple[tuple[str, float, float], ...] = (
    ('M5 Ultra', 1100.0, 40.0), ('M5 Max', 614.0, 20.0), ('M5 Pro', 307.0, 10.0), ('M5', 153.0, 4.6),
    ('M4 Ultra', 819.0, 32.0), ('M4 Max', 546.0, 18.4), ('M4 Pro', 273.0, 9.2), ('M4', 120.0, 4.3),
    ('M3 Ultra', 819.0, 28.4), ('M3 Max', 400.0, 16.4), ('M3 Pro', 150.0, 7.4), ('M3', 100.0, 4.1),
    ('M2 Ultra', 800.0, 27.2), ('M2 Max', 400.0, 13.6), ('M2 Pro', 200.0, 6.8), ('M2', 100.0, 3.6),
    ('M1 Ultra', 800.0, 21.0), ('M1 Max', 400.0, 10.4), ('M1 Pro', 200.0, 5.2), ('M1', 68.0, 2.6),
)
#: Fraction of the published peak a streaming matmul reaches.
_BANDWIDTH_EFFICIENCY = 0.6
_FLOPS_EFFICIENCY = 0.4


def chip_figures(chip: object) -> tuple[float, float] | None:
    """(bandwidth bytes/s, FLOPs/s) for an Apple chip string, else ``None``."""
    if not isinstance(chip, str):
        return None
    name = chip.strip()
    for key, bandwidth, tflops in _CHIP_TABLE:
        if re.search(r'\b' + re.escape(key) + r'\b', name):
            return bandwidth * 1e9, tflops * 1e12
    return None


# -- calibration records -------------------------------------------------------

def _finite(value: object, *, minimum: float = 0.0, maximum: float = 1e9) -> bool:
    return (type(value) in (int, float) and math.isfinite(value) and minimum < value <= maximum)


_CALIBRATION_KEYS = frozenset({
    'protocol', 'basis', 'method', 'chip', 'model_id', 'resolved_commit', 'layer_bytes',
    'decode_ms_per_layer', 'prefill_tokens_per_second_per_layer', 'prefill_tokens', 'samples',
    'wall_ms', 'measured_at'})


def validate_step_calibration(value: Any) -> dict[str, Any]:
    """Closed, bounded calibration record; raises ``ValueError('step_calibration_invalid')``."""
    def bad() -> ValueError:
        return ValueError('step_calibration_invalid')
    if not isinstance(value, dict) or set(value) != _CALIBRATION_KEYS:
        raise bad()
    try:
        raw = json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise bad() from None
    if len(raw) > 2048 or value['protocol'] != CALIBRATION_PROTOCOL or value['basis'] not in BASES:
        raise bad()
    if value['method'] != (METHOD_MEASURED if value['basis'] == 'measured' else METHOD_ESTIMATED):
        raise bad()
    for key in ('chip', 'model_id', 'resolved_commit'):
        if not isinstance(value[key], str) or not _TEXT.fullmatch(value[key]):
            raise bad()
    if (type(value['layer_bytes']) is not int or not 0 < value['layer_bytes'] <= 1 << 40
            or type(value['samples']) is not int or not 0 <= value['samples'] <= 100_000
            or type(value['prefill_tokens']) is not int or not 1 <= value['prefill_tokens'] <= 65_536):
        raise bad()
    if (not _finite(value['decode_ms_per_layer'], maximum=1e6)
            or not _finite(value['prefill_tokens_per_second_per_layer'], maximum=1e12)
            or not _finite(value['wall_ms'], minimum=-1.0, maximum=1e7)
            or not _finite(value['measured_at'], minimum=-1.0, maximum=1e12)):
        raise bad()
    return json.loads(raw)


def estimate_step_calibration(chip: object, profile: LayerProfile, *, model_id: str,
                              resolved_commit: str, now: float | None = None,
                              prefill_tokens: int = 128) -> dict[str, Any] | None:
    """Documented FLOPs/bandwidth estimate for a chip; ``None`` for an unknown chip.

    decode per layer  = layer bytes / (bandwidth * 0.6)       (memory bound)
    prefill per layer = layer bytes (= 2 FLOPs x bf16 params) per token / (FLOPs * 0.4)
    """
    figures = chip_figures(chip)
    if figures is None or not profile.layer_bytes:
        return None
    bandwidth, flops = figures
    layer_bytes = max(1, sum(profile.layer_bytes) // len(profile.layer_bytes))
    return validate_step_calibration({
        'protocol': CALIBRATION_PROTOCOL, 'basis': 'estimated', 'method': METHOD_ESTIMATED,
        'chip': str(chip).strip()[:96], 'model_id': model_id, 'resolved_commit': resolved_commit,
        'layer_bytes': layer_bytes,
        'decode_ms_per_layer': round(layer_bytes / (bandwidth * _BANDWIDTH_EFFICIENCY) * 1e3, 6),
        'prefill_tokens_per_second_per_layer': round(flops * _FLOPS_EFFICIENCY / layer_bytes, 3),
        'prefill_tokens': prefill_tokens, 'samples': 0, 'wall_ms': 0.0,
        'measured_at': float(time.time() if now is None else now)})


def measure_step_calibration(decode_step: Callable[[], None], prefill: Callable[[int], None], *,
                             chip: str, model_id: str, resolved_commit: str, layer_bytes: int,
                             prefill_tokens: int = 128, budget_seconds: float = 1.5,
                             max_decode_steps: int = 48, min_decode_steps: int = 4,
                             prefill_repeats: int = 3, clock: Callable[[], float] = time.perf_counter,
                             now: float | None = None) -> dict[str, Any]:
    """Bounded timing of one layer on the caller's own runtime.

    ``decode_step()`` runs one single-token layer pass to completion;
    ``prefill(n)`` runs an ``n``-token layer pass to completion.  One warm-up of
    each is discarded; the decode loop stops at ``max_decode_steps`` or when
    ``budget_seconds`` is spent (but never before ``min_decode_steps``).  The
    median is reported, so a stray stall does not skew the split.
    """
    if not (isinstance(budget_seconds, (int, float)) and 0 < budget_seconds <= 30
            and 1 <= min_decode_steps <= max_decode_steps <= 4096 and 1 <= prefill_repeats <= 16):
        raise ValueError('step_calibration_invalid')
    started = clock()
    decode_step()
    prefill(prefill_tokens)
    decode_times: list[float] = []
    while len(decode_times) < max_decode_steps:
        before = clock()
        decode_step()
        decode_times.append(clock() - before)
        if len(decode_times) >= min_decode_steps and clock() - started >= budget_seconds * 0.75:
            break
    prefill_times = []
    for _ in range(prefill_repeats):
        before = clock()
        prefill(prefill_tokens)
        prefill_times.append(clock() - before)
        if clock() - started >= budget_seconds:
            break
    decode = max(statistics.median(decode_times), 1e-9)
    prefill_time = max(statistics.median(prefill_times), 1e-9)
    return validate_step_calibration({
        'protocol': CALIBRATION_PROTOCOL, 'basis': 'measured', 'method': METHOD_MEASURED,
        'chip': str(chip)[:96] or 'unknown', 'model_id': model_id, 'resolved_commit': resolved_commit,
        'layer_bytes': int(layer_bytes),
        'decode_ms_per_layer': round(decode * 1e3, 6),
        'prefill_tokens_per_second_per_layer': round(prefill_tokens / prefill_time, 3),
        'prefill_tokens': prefill_tokens, 'samples': len(decode_times),
        'wall_ms': round((clock() - started) * 1e3, 3),
        'measured_at': float(time.time() if now is None else now)})


def mlx_layer_probe(source_root: str | Path) -> tuple[Callable[[], None], Callable[[int], None], int]:
    """(decode_step, prefill, layer_bytes) over one real layer already on this Mac.

    Reads only the safetensors header plus ONE layer's tensors from the weights
    this Mac already holds (never a download).  Each 2-D weight is multiplied by
    a synthetic activation: one token (decode, memory-bandwidth bound) or
    ``n`` tokens (prefill, compute bound).  Raises on missing weights or MLX.
    """
    from .layer_split import QWEN_DENSE_NAMING, _read_header, _weight_files
    import mlx.core as mx
    root = Path(source_root)
    pattern = re.compile(QWEN_DENSE_NAMING.layer_pattern)
    chosen: dict[Path, list[str]] = {}
    layer = None
    sizes = 0
    for path in _weight_files(root):
        if not path.is_file():
            continue
        for name, record in _read_header(path).items():
            match = pattern.match(name)
            if match is None:
                continue
            index = int(match.group(1))
            if layer is None or index < layer:
                layer, chosen, sizes = index, {}, 0
            if index == layer:
                chosen.setdefault(path, []).append(name)
                sizes += record['data_offsets'][1] - record['data_offsets'][0]
    if layer is None or not 0 < sizes <= MAX_PROBE_LAYER_BYTES:
        raise ValueError('step_calibration_no_weights')
    weights = []
    for path, names in chosen.items():
        tensors = mx.load(str(path))
        weights += [tensors[name] for name in names if len(tensors[name].shape) == 2]
    if not weights:
        raise ValueError('step_calibration_no_weights')
    mx.eval(*weights)

    def run(tokens: int) -> None:
        outputs = [mx.ones((tokens, w.shape[1]), dtype=w.dtype) @ w.T for w in weights]
        mx.eval(*outputs)
    return (lambda: run(1)), run, sizes


def calibrate_member(source_root: str | Path | None, profile: LayerProfile | None, *, chip: str,
                     model_id: str, resolved_commit: str,
                     probe: Callable[[], tuple] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    """Measured if this Mac holds weights and MLX works, else the chip estimate, else ``None``."""
    try:
        decode_step, prefill, layer_bytes = (probe or (lambda: mlx_layer_probe(source_root)))()
        return measure_step_calibration(decode_step, prefill, chip=chip, model_id=model_id,
                                        resolved_commit=resolved_commit, layer_bytes=layer_bytes, **kwargs)
    except Exception:
        if profile is None:
            return None
        return estimate_step_calibration(chip, profile, model_id=model_id, resolved_commit=resolved_commit)


def app_calibrator(app: Any) -> Callable[[Mapping[str, Any]], dict[str, Any] | None]:
    """``DemoTargetAgent`` calibrator: this Mac's own step calibration for the announced model.

    Uses the prepared snapshot this Mac already holds (whole-snapshot mode); with
    none yet (slice mode before the slices arrive) it falls back to the chip
    estimate, labelled ``estimated``.  Never downloads anything.
    """
    def run(announcement: Mapping[str, Any]) -> dict[str, Any] | None:
        import subprocess
        from .capability import _sysctl_text
        model = announcement.get('model') or {}
        chip = _sysctl_text(subprocess.run, 'machdep.cpu.brand_string') or 'unknown'
        root = None
        try:
            prepared = app.acquisition.prepared_source()
            if (prepared.model.model_id, prepared.model.resolved_commit) == (
                    model.get('model_id'), model.get('resolved_commit')):
                root = prepared.source_root
        except Exception:
            root = None
        profile = None
        try:
            from .model_resources import model_shape
            shape = model_shape(app.run_model_contract())
            profile = None if shape is None else shape.layer_profile()
        except Exception:
            profile = None
        return calibrate_member(root, profile, chip=chip, model_id=model['model_id'],
                                resolved_commit=model['resolved_commit'])
    return run


# -- owner setting ---------------------------------------------------------------

def load_setting(root: Path | str, *, environ: Mapping[str, str] | None = None) -> str:
    """``auto`` or ``always_split``; the environment wins, then the saved choice."""
    import os
    env = (os.environ if environ is None else environ).get(SETTING_ENVIRONMENT)
    if env in MODES:
        return env
    try:
        from .authority import _load_private_json
        value = _load_private_json(Path(root) / SETTING_FILE, max_bytes=4096)
    except Exception:
        return 'auto'
    if (isinstance(value, dict) and set(value) == {'protocol', 'mode'}
            and value['protocol'] == SETTING_PROTOCOL and value['mode'] in MODES):
        return value['mode']
    return 'auto'


def save_setting(root: Path | str, mode: object) -> str:
    if mode not in MODES:
        raise ValueError('placement_mode_invalid')
    from .authority import _atomic_private_json
    _atomic_private_json(Path(root) / SETTING_FILE, {'protocol': SETTING_PROTOCOL, 'mode': mode}, max_bytes=4096)
    return mode


# -- placement -----------------------------------------------------------------

def _member_budget(row: Mapping[str, Any], policy: SplitPolicy) -> int:
    memory = row['memory_bytes']
    budget = math.floor(memory * Fraction(policy.memory_fraction_numerator, policy.memory_fraction_denominator))
    cap = row.get('max_memory_bytes')
    if type(cap) is int and cap > 0:
        budget = min(budget, cap)
    return min(budget, math.floor(memory * MEMORY_GUARD_FRACTION))


def _calibration_ns(calibration: Mapping[str, Any]) -> tuple[int, int]:
    """(decode ns per layer, prefill ns per layer-token)."""
    return (max(1, round(calibration['decode_ms_per_layer'] * _NS)),
            max(1, round(1e9 / calibration['prefill_tokens_per_second_per_layer'])))


def _hop_one_way_ns(rows: Sequence[Mapping[str, Any]], pair_rtt_ms: Mapping[Any, float]) -> list[int]:
    """One-way latency of edge i -> i+1 (ring: last -> first), from measured RTTs."""
    count = len(rows)

    def rtt(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
        direct = pair_rtt_ms.get((a['node_id'], b['node_id']), pair_rtt_ms.get((b['node_id'], a['node_id'])))
        if direct is not None:
            return float(direct)
        return float(a.get('link_rtt_ms') or 0.0) + float(b.get('link_rtt_ms') or 0.0)
    if count == 2:  # one pair: the token goes there and comes back = one RTT in total
        return [round(rtt(rows[0], rows[1]) / 2 * _NS)] * 2
    return [round(rtt(rows[i], rows[(i + 1) % count]) / 2 * _NS) for i in range(count)]


def _best_partition(rows, profile, policy, decode_ns, prefill_ns, hop_ns, *, respect_memory):
    """Exact DP over contiguous partitions.

    Key per partition: (slowest stage decode ns incl. outbound hop, slowest stage
    prefill ns, total decode ns, boundaries).  Returns ``(key, boundaries)`` or ``None``.
    """
    count, layers = len(rows), profile.num_layers
    sizes = profile.layer_bytes
    prefix = [0]
    for size in sizes:
        prefix.append(prefix[-1] + size)
    mean = max(1, prefix[-1] // layers)
    tail_bytes = profile.output_bytes + (profile.embedding_bytes if profile.tie_word_embeddings else 0)
    resident = Fraction(policy.resident_factor_numerator, policy.resident_factor_denominator)
    budgets = [_member_budget(row, policy) for row in rows]

    def fits(index: int, start: int, end: int) -> bool:
        if not respect_memory:
            return True
        stage = prefix[end] - prefix[start] + (profile.embedding_bytes if index == 0 else 0) \
            + (tail_bytes if index == count - 1 else 0)
        return math.ceil(stage * resident) + policy.runtime_overhead_bytes <= budgets[index]

    def stage_times(index: int, start: int, end: int) -> tuple[int, int]:
        weight = prefix[end] - prefix[start] + (tail_bytes if index == count - 1 else 0)
        # layer-equivalents by bytes, so a heavier layer costs proportionally more
        return (decode_ns[index] * weight // mean + hop_ns[index],
                prefill_ns[index] * weight // mean)

    best: list[dict[int, tuple]] = [dict() for _ in range(count + 1)]
    best[0][0] = ((0, 0, 0), ())
    for index in range(count):
        for start, (key, bounds) in best[index].items():
            remaining = count - index - 1
            for end in range(start + 1, layers - remaining + 1):
                if not fits(index, start, end):
                    continue
                decode, prefill = stage_times(index, start, end)
                new = (max(key[0], decode), max(key[1], prefill), key[2] + decode)
                current = best[index + 1].get(end)
                candidate = (new, bounds + (end,))
                if current is None or (candidate[0], candidate[1]) < (current[0], current[1]):
                    best[index + 1][end] = candidate
    final = best[count].get(layers)
    return None if final is None else (final[0], (0,) + final[1])


def calibrated_rows(rows: Sequence[Mapping[str, Any]], profile: LayerProfile, *, model_id: str,
                    resolved_commit: str) -> list[dict[str, Any]] | None:
    """Rows with a trusted ``step_calibration`` each, or ``None`` (fall back to memory).

    A reported record counts only when it validates and is bound to THIS model;
    a member without one gets the chip estimate when its chip is known.
    """
    result = []
    for row in rows:
        row = dict(row)
        record = row.pop('step_calibration', None)
        try:
            record = None if record is None else validate_step_calibration(record)
        except ValueError:
            record = None
        if record is not None and (record['model_id'], record['resolved_commit']) != (model_id, resolved_commit):
            record = None
        if record is None:
            record = estimate_step_calibration(row.get('chip'), profile, model_id=model_id,
                                               resolved_commit=resolved_commit)
        if record is None:
            return None
        row['step_calibration'] = record
        result.append(row)
    return result


def _label(row: Mapping[str, Any]) -> str:
    text = row.get('label') or row.get('chip') or row['node_id'][:12]
    return str(text)


def _step_inputs(rows):
    """Per-member (decode ns/layer, prefill ns/layer-token, basis) or ``None`` if any is unknown."""
    decode, prefill, bases = [], [], []
    for row in rows:
        calibration = row.get('step_calibration')
        if not isinstance(calibration, Mapping):
            return None
        decode_ns, prefill_ns = _calibration_ns(calibration)
        decode.append(decode_ns)
        prefill.append(prefill_ns)
        bases.append(calibration['basis'])
    return decode, prefill, bases


def _tokens_per_second(token_ns: int) -> float:
    return round(1e9 / token_ns, 2)


def choose_placement(members: Sequence[Mapping[str, Any]], profile: LayerProfile,
                     policy: SplitPolicy = DEFAULT_POLICY, *, force_split: bool = False,
                     pair_rtt_ms: Mapping[Any, float] | None = None) -> dict[str, Any]:
    """Split document (``layer_split`` shape) + a ``placement`` decision block.

    Member rows: ``node_id``, ``memory_bytes``; optional ``step_calibration``
    (validated record), ``link_rtt_ms`` (median RTT to the owner; the owner 0),
    ``max_memory_bytes`` (standing cap), ``label``/``chip`` (display).  When any
    member has no calibration the plain memory-proportional split is returned
    unchanged (``placement.reason == 'no_calibration'``).
    """
    extras = {m['node_id']: m for m in members if isinstance(m, Mapping) and isinstance(m.get('node_id'), str)}
    rows = _validated_members(members)
    for row in rows:
        source = extras[row['node_id']]
        for key in ('step_calibration', 'link_rtt_ms', 'max_memory_bytes', 'label', 'chip'):
            if source.get(key) is not None:
                row[key] = source[key]
    inputs = _step_inputs(rows)
    plain = [{'node_id': r['node_id'], 'memory_bytes': r['memory_bytes']} for r in rows]
    if inputs is None:
        document = memory_proportional_split(plain, profile, policy)
        return _with_placement(document, {
            'protocol': PLACEMENT_PROTOCOL, 'mode': 'split', 'applied': True, 'reason': 'no_calibration',
            'force_split': bool(force_split), 'basis': 'memory',
            'explanation': 'Split by memory only: not every Mac has reported its speed yet.'})
    decode_ns, prefill_ns, bases = inputs
    count = len(rows)
    if count > profile.num_layers:
        raise SplitRefused('model_too_small_for_members')
    hop_ns = _hop_one_way_ns(rows, pair_rtt_ms or {})
    found = _best_partition(rows, profile, policy, decode_ns, prefill_ns, hop_ns, respect_memory=True)
    if found is None:
        memory_proportional_split(plain, profile, policy)  # raises the by-name refusal
        raise SplitRefused('member_cannot_hold_share')
    (bottleneck, prefill_bottleneck, _), boundaries = found
    free = _best_partition(rows, profile, policy, decode_ns, prefill_ns, hop_ns, respect_memory=False)
    memory_bound = free is not None and free[0][0] < bottleneck

    # stages (same fields as the memory split)
    layers = profile.layer_bytes
    tail = profile.output_bytes + (profile.embedding_bytes if profile.tie_word_embeddings else 0)
    resident = Fraction(policy.resident_factor_numerator, policy.resident_factor_denominator)
    mean = max(1, sum(layers) // profile.num_layers)
    stages, compute_total = [], 0
    for index, row in enumerate(rows):
        start, end = boundaries[index], boundaries[index + 1]
        stage = sum(layers[start:end]) + (profile.embedding_bytes if index == 0 else 0) \
            + (tail if index == count - 1 else 0)
        weight = sum(layers[start:end]) + (tail if index == count - 1 else 0)
        compute = decode_ns[index] * weight // mean
        compute_total += compute
        stages.append({'node_id': row['node_id'], 'start': start, 'end': end, 'stage_bytes': stage,
                       'cost_bytes': math.ceil(stage * resident) + policy.runtime_overhead_bytes,
                       'budget_bytes': _member_budget(row, policy)})
    split_token_ns = compute_total + sum(hop_ns)
    counts = [s['end'] - s['start'] for s in stages]

    # fast path: one Mac holds the whole model
    whole = sum(layers) + profile.embedding_bytes + profile.output_bytes
    whole_cost = math.ceil(whole * resident) + policy.runtime_overhead_bytes
    singles = []
    for index, row in enumerate(rows):
        if whole_cost <= _member_budget(row, policy):
            token_ns = decode_ns[index] * (sum(layers) + tail) // mean
            singles.append((token_ns, row['node_id'], index))
    best_single = min(singles) if singles else None
    prediction = {'split_token_ms': round(split_token_ns / _NS, 3), 'split_tokens_per_second': _tokens_per_second(split_token_ns),
                  'bottleneck_ms': round(bottleneck / _NS, 3), 'prefill_bottleneck_ms_per_token': round(prefill_bottleneck / _NS, 4),
                  'hop_one_way_ms': [round(h / _NS, 3) for h in hop_ns],
                  'single_token_ms': None if best_single is None else round(best_single[0] / _NS, 3),
                  'single_tokens_per_second': None if best_single is None else _tokens_per_second(best_single[0])}
    per_member = [{'node_id': r['node_id'], 'label': _label(r), 'layers': counts[i], 'basis': bases[i],
                   'decode_ms_per_layer': round(decode_ns[i] / _NS, 6), 'decode_ns_per_layer': decode_ns[i], 'link_rtt_ms': r.get('link_rtt_ms')}
                  for i, r in enumerate(rows)]
    estimated = 'estimated' in bases
    block = {'protocol': PLACEMENT_PROTOCOL, 'force_split': bool(force_split),
             'basis': 'estimated' if estimated else 'measured', 'members': per_member,
             'prediction': prediction, 'memory_bound': memory_bound}
    fast = best_single is not None and best_single[0] < split_token_ns
    if fast and not force_split:
        host = rows[best_single[2]]
        others = [r for r in rows if r is not host]
        block.update(mode='single', reason='fits_one_mac_faster', host=host['node_id'],
                     replicas=[r['node_id'] for r in others], applied=SINGLE_HOST_ROUTE_SUPPORTED)
        block['explanation'] = (
            'Fast path chosen: fits on %s (%s tok/s vs %s split); the other Mac%s stay%s available.'
            % (_label(host), _tokens_per_second(best_single[0]), _tokens_per_second(split_token_ns),
               '' if len(others) == 1 else 's', 's' if len(others) == 1 else ''))
        if not SINGLE_HOST_ROUTE_SUPPORTED:
            block['note'] = 'single_host_route_not_supported_yet_running_best_split'
    else:
        block.update(mode='split', applied=True,
                     reason='forced_always_split' if force_split and fast else
                     ('memory_bound' if memory_bound else 'model_does_not_fit_one_mac' if best_single is None
                      else 'split_predicted_faster'))
        block['explanation'] = _split_sentence(per_member, counts, memory_bound, force_split and fast)
    document = {
        'protocol': PROTOCOL, 'model_type': profile.model_type, 'num_layers': profile.num_layers,
        'profile_digest': _digest(profile.document()), 'policy': _policy_document(policy), 'members': plain,
        'weighting': 'estimated_step_time' if estimated else 'measured_step_time', 'stages': stages}
    return _with_placement(document, block)


def _policy_document(policy: SplitPolicy) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(policy)


def _with_placement(document: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in document.items() if k != 'split_digest'}
    body['placement'] = block
    body['split_digest'] = _digest(body)
    return body


def _split_sentence(members: Sequence[Mapping[str, Any]], counts: Sequence[int], memory_bound: bool,
                    forced_over_fast_path: bool) -> str:
    shape = '/'.join(str(c) for c in counts)
    slow = max(members, key=lambda m: m['decode_ns_per_layer'])
    fast = min(members, key=lambda m: m['decode_ns_per_layer'])
    ratio = slow['decode_ns_per_layer'] / max(1, fast['decode_ns_per_layer'])
    estimate = ' (estimated)' if any(m['basis'] == 'estimated' for m in members) else ''
    if forced_over_fast_path:
        head = 'Split %s because "always split" is on' % shape
        return head + (': %s is %.1fx slower per layer%s.' % (slow['label'], ratio, estimate)
                       if ratio >= EVEN_RATIO else '.')
    if memory_bound:
        return 'Split %s because memory limits what each Mac can hold%s.' % (shape, estimate)
    if ratio >= EVEN_RATIO:
        return 'Split %s because %s is %.1fx slower per layer%s.' % (shape, slow['label'], ratio, estimate)
    return 'Split %s: the Macs run at about the same speed per layer%s.' % (shape, estimate)


# -- per-token latency decomposition (hook) --------------------------------------

DECOMPOSITION_KINDS = ('compute', 'serialize', 'send', 'receive', 'other')


def decompose_token_latency(records: Sequence[Mapping[str, Any]], *, wall_ns: int | None = None) -> dict[str, Any]:
    """Aggregate per-hop timing records into stacked per-stage times.

    Each record: ``{'stage': int, 'kind': compute|serialize|send|receive|other,
    'start_ns': int, 'end_ns': int}`` from monotonic clocks.  Pure arithmetic: it
    is the sink a route can feed; ``mycelium_live`` does not emit these records
    yet (see docs/PLACEMENT.md), so no route internals are touched.  The result
    reports ``residual_ns`` against ``wall_ns`` so unexplained time is visible.
    """
    stages: dict[int, dict[str, int]] = {}
    for record in records:
        kind, stage = record.get('kind'), record.get('stage')
        start, end = record.get('start_ns'), record.get('end_ns')
        if (kind not in DECOMPOSITION_KINDS or type(stage) is not int or stage < 0
                or type(start) is not int or type(end) is not int or end < start):
            raise ValueError('latency_record_invalid')
        row = stages.setdefault(stage, {k: 0 for k in DECOMPOSITION_KINDS})
        row[kind] += end - start
    total = sum(sum(row.values()) for row in stages.values())
    result = {'stages': {str(k): stages[k] for k in sorted(stages)}, 'accounted_ns': total,
              'by_kind': {k: sum(row[k] for row in stages.values()) for k in DECOMPOSITION_KINDS}}
    if wall_ns is not None:
        result['wall_ns'] = wall_ns
        result['residual_ns'] = wall_ns - total
        result['residual_fraction'] = None if wall_ns <= 0 else round((wall_ns - total) / wall_ns, 4)
    return result


# -- owner wiring helpers --------------------------------------------------------

def public_decision(split: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The status-safe view of a split's decision (no node RAM/disk facts)."""
    block = (split or {}).get('placement')
    if not isinstance(block, Mapping):
        return None
    keep = ('protocol', 'mode', 'applied', 'reason', 'force_split', 'basis', 'explanation', 'note', 'host',
            'replicas', 'memory_bound')
    value = {k: block[k] for k in keep if k in block}
    value['members'] = [{k: m.get(k) for k in ('node_id', 'label', 'layers', 'basis', 'decode_ms_per_layer', 'link_rtt_ms')}
                        for m in block.get('members', [])]
    value['prediction'] = dict(block.get('prediction') or {})
    return json.loads(json.dumps(value))


__all__ = [
    'CALIBRATION_PROTOCOL', 'MODES', 'PLACEMENT_PROTOCOL', 'SETTING_ENVIRONMENT',
    'SINGLE_HOST_ROUTE_SUPPORTED', 'app_calibrator', 'calibrate_member', 'calibrated_rows', 'choose_placement', 'chip_figures',
    'decompose_token_latency', 'estimate_step_calibration', 'load_setting', 'measure_step_calibration',
    'mlx_layer_probe', 'public_decision', 'save_setting', 'validate_step_calibration',
]
