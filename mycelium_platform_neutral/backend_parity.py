"""Cross-backend parity gate v0 (PV29 lane L1).

Decides whether a *route tuple* - an ordered list of layer-range stages, each on
its own backend/representation, joined by a float32 activation wire - is
numerically admissible against a reference.  It decomposes the error:

``H``   hop/split parity: the actual split route against a same-numerics
        monolith.  Isolates stage-boundary, RoPE offset, KV, chunking and hop
        bugs from quantization.
``Q0``/``Q1``  quantization class parity against the independent oracle
        (HF float32 offline logits).  The class is the worst stage representation.
``N``   hygiene: finite activations, exact wire dtype/shape, magnitude cap, no
        extra tensor leaving a non-final stage.
``G``   generation consistency and ``P`` join-time probe: *not evaluated* in v0
        (they need device runs); they return ``not_evaluated`` with a reason and
        can never turn a verdict into ``pass``.

Thresholds live in a versioned, digest-bound policy file (v2: per-stage
representation list -> class).  A policy is ``provisional`` until frozen; the
route record binds the policy digest, the corpus digest, the oracle identity
and every route component, and is Ed25519-signed, so any change invalidates it.

Claim boundary: a passing record claims parity of *that route tuple on that
corpus under that policy*.  It is not "Linux supported", not performance
eligibility, not route readiness, and not a physical-device claim.

This module imports NumPy only (plus the shared canonical-JSON/Ed25519 helpers);
it never imports MLX or torch, so it is importable on every platform.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes

POLICY_PROTOCOL = "mycelium.backend_parity_policy.v2"
CORPUS_PROTOCOL = "mycelium.backend_parity_corpus.v1"
RECORD_PROTOCOL = "mycelium.cross_backend_route_qualification.v1"
RECORD_ENVELOPE_PROTOCOL = "mycelium.cross_backend_route_qualification_envelope.v1"
BATTERY_PROTOCOL = "mycelium.backend_parity_defect_battery.v1"

CLAIM_BOUNDARY = (
    "parity of this exact route tuple on this corpus under this policy only; not platform "
    "support, not performance eligibility, not route readiness, not a physical-device claim"
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CASE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")

QUANTIZATION_CLASSES = ("Q0", "Q1")
GATE_STATUSES = ("pass", "fail", "not_evaluated")
VERDICTS = ("fail", "partial_pass", "pass")
THRESHOLD_OPS = (">=", "<=")
BACKEND_IDS = ("mlx", "numpy", "llama_stage", "mobile-rust-qwen2", "oracle")
DEVICE_CLASSES = ("cpu", "metal", "cuda", "vulkan")

#: Metrics each gate may threshold.  A policy naming any other metric is invalid.
H_METRICS = (
    "top1_agreement",
    "logits_cosine_min",
    "logits_max_abs_delta_top",
    "kl_mean",
    "kl_max",
    "hop_cosine_min",
    "hop_rel_l2_max",
)
Q_METRICS = ("top1_agreement", "topk_overlap_mean", "logits_cosine_min", "kl_mean", "kl_max")
N_METRICS = (
    "nonfinite_count",
    "wire_dtype_mismatch_count",
    "wire_shape_mismatch_count",
    "activation_abs_max",
    "extra_output_count",
)


class BackendParityError(ValueError):
    """A bounded, coded parity-gate failure (the message is the code)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _reject(code: str, detail: str = "") -> None:
    raise BackendParityError(code, detail)


# --------------------------------------------------------------------------- digests


def canonical(value: Any) -> bytes:
    try:
        return canonical_json_bytes(value)
    except Exception as exc:  # EvidenceValidationError and friends
        raise BackendParityError("parity_noncanonical_json", str(exc)) from exc


def digest_of(value: Any) -> str:
    return sha256_bytes(canonical(value))


def file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return "sha256:" + sha.hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


# --------------------------------------------------------------------------- metrics


def _as_matrix(value: Any, *, label: str = "input") -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim == 1:
        array = array[None, :]
    if array.ndim != 2:
        _reject("parity_shape_mismatch", f"{label} rank {array.ndim}")
    return array


def _paired(ref: Any, test: Any) -> tuple[np.ndarray, np.ndarray]:
    a = _as_matrix(ref, label="reference")
    b = _as_matrix(test, label="test")
    if a.shape != b.shape:
        _reject("parity_shape_mismatch", f"{a.shape} vs {b.shape}")
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        _reject("parity_nonfinite_input")
    return a, b


def cosine_rows(ref: Any, test: Any) -> np.ndarray:
    """Row-wise cosine similarity; an all-zero row yields 0.0, never NaN."""

    a, b = _paired(ref, test)
    numerator = np.sum(a * b, axis=1)
    denominator = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    return np.where(denominator > 0.0, numerator / np.where(denominator > 0.0, denominator, 1.0), 0.0)


def rel_l2_rows(ref: Any, test: Any) -> np.ndarray:
    """||test-ref|| / ||ref|| per row (0 when both are zero, inf when only ref is)."""

    a, b = _paired(ref, test)
    delta = np.linalg.norm(b - a, axis=1)
    scale = np.linalg.norm(a, axis=1)
    return np.where(scale > 0.0, delta / np.where(scale > 0.0, scale, 1.0), np.where(delta > 0.0, np.inf, 0.0))


def _log_softmax(x: np.ndarray) -> np.ndarray:
    shifted = x - np.max(x, axis=1, keepdims=True)
    return shifted - np.log(np.sum(np.exp(shifted), axis=1, keepdims=True))


def kl_rows(ref_logits: Any, test_logits: Any) -> np.ndarray:
    """KL(softmax(ref) || softmax(test)) per row in nats (float64, shift-invariant)."""

    a, b = _paired(ref_logits, test_logits)
    log_p = _log_softmax(a)
    log_q = _log_softmax(b)
    return np.sum(np.exp(log_p) * (log_p - log_q), axis=1)


def top1_agreement(
    ref_logits: Any, test_logits: Any, *, margin_floor: float = 0.0
) -> tuple[float, int, int]:
    """(agreement, scored rows, excluded rows).

    Rows whose reference top-1/top-2 margin is <= ``margin_floor`` are near-ties
    (legitimate float noise may flip them) and are excluded, and reported.
    """

    a, b = _paired(ref_logits, test_logits)
    if a.shape[1] < 2:
        _reject("parity_shape_mismatch", "need at least two classes")
    ordered = np.sort(a, axis=1)
    margin = ordered[:, -1] - ordered[:, -2]
    keep = margin > margin_floor if margin_floor > 0.0 else np.ones(a.shape[0], dtype=bool)
    scored = int(keep.sum())
    excluded = int(a.shape[0] - scored)
    if scored == 0:
        return 1.0, 0, excluded
    agree = np.argmax(a, axis=1) == np.argmax(b, axis=1)
    return float(agree[keep].mean()), scored, excluded


def topk_overlap(ref_logits: Any, test_logits: Any, k: int) -> float:
    """Mean |topk(ref) & topk(test)| / k over rows."""

    a, b = _paired(ref_logits, test_logits)
    if not 1 <= k <= a.shape[1]:
        _reject("parity_metric_param_invalid", f"k={k}")
    top_a = np.argpartition(-a, k - 1, axis=1)[:, :k]
    top_b = np.argpartition(-b, k - 1, axis=1)[:, :k]
    overlaps = [len(set(x.tolist()) & set(y.tolist())) / k for x, y in zip(top_a, top_b)]
    return float(np.mean(overlaps))


def max_abs_delta(ref: Any, test: Any, *, top_k: int | None = None) -> float:
    """max |test-ref|, over everything or over each row's reference top-``top_k`` entries."""

    a, b = _paired(ref, test)
    delta = np.abs(b - a)
    if top_k is None:
        return float(delta.max()) if delta.size else 0.0
    if not 1 <= top_k <= a.shape[1]:
        _reject("parity_metric_param_invalid", f"top_k={top_k}")
    index = np.argpartition(-a, top_k - 1, axis=1)[:, :top_k]
    return float(np.take_along_axis(delta, index, axis=1).max())


# --------------------------------------------------------------------------- thresholds


def validate_threshold_spec(spec: Any, *, allowed: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    if not isinstance(spec, Mapping) or not spec:
        _reject("parity_threshold_invalid", "empty or not an object")
    allowed_set = set(allowed) if allowed is not None else None
    result: dict[str, dict[str, Any]] = {}
    for metric, entry in spec.items():
        if (
            not isinstance(metric, str)
            or (allowed_set is not None and metric not in allowed_set)
            or not isinstance(entry, Mapping)
            or set(entry) != {"op", "value"}
            or entry["op"] not in THRESHOLD_OPS
            or not _finite_number(entry["value"])
        ):
            _reject("parity_threshold_invalid", str(metric))
        result[metric] = {"op": entry["op"], "value": float(entry["value"])}
    return result


def evaluate_thresholds(metrics: Mapping[str, Any], spec: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One check per thresholded metric; a missing or non-finite value fails."""

    checks = []
    for metric in sorted(spec):
        entry = spec[metric]
        raw = metrics.get(metric)
        value = float(raw) if _finite_number(raw) else None
        if value is None:
            passed = False
        elif entry["op"] == ">=":
            passed = value >= float(entry["value"])
        else:
            passed = value <= float(entry["value"])
        checks.append(
            {
                "metric": metric,
                "op": entry["op"],
                "threshold": float(entry["value"]),
                "value": value,
                "pass": bool(passed),
            }
        )
    return checks


# --------------------------------------------------------------------------- policy


def _thr(op: str, value: float) -> dict[str, Any]:
    return {"op": op, "value": float(value)}


def default_policy_v2() -> dict[str, Any]:
    """Plan §2.5 provisional priors - the starting point that calibration tightens."""

    policy: dict[str, Any] = {
        "protocol": POLICY_PROTOCOL,
        "policy_id": "qwen2.5-0.5b-instruct-f32wire-prior",
        "status": "provisional",
        "model": {
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "revision": "7ae557604adf67be50417f59c2c2f167def9a775",
        },
        "wire": {"dtype": "float32"},
        "metric_params": {
            "topk_overlap_k": 10,
            "max_abs_delta_top_k": 32,
            "top1_margin_floor": 1e-3,
        },
        "representations": {
            "none": {"class": "Q0"},
            "int8-weight-only": {"class": "Q0"},
            "gguf-q8_0": {"class": "Q0"},
            "gguf-q4_k_m": {"class": "Q1"},
        },
        "gates": {
            "H": {
                "thresholds": {
                    "top1_agreement": _thr(">=", 1.0),
                    "logits_cosine_min": _thr(">=", 0.99999),
                    "logits_max_abs_delta_top": _thr("<=", 5e-3),
                    "kl_mean": _thr("<=", 1e-5),
                    "hop_cosine_min": _thr(">=", 0.99999),
                    "hop_rel_l2_max": _thr("<=", 1e-3),
                }
            },
            "Q0": {
                "thresholds": {
                    "top1_agreement": _thr(">=", 0.99),
                    "topk_overlap_mean": _thr(">=", 0.9),
                    "logits_cosine_min": _thr(">=", 0.999),
                    "kl_mean": _thr("<=", 5e-3),
                }
            },
            "Q1": {
                "thresholds": {
                    "top1_agreement": _thr(">=", 0.95),
                    "topk_overlap_mean": _thr(">=", 0.8),
                    "logits_cosine_min": _thr(">=", 0.99),
                    "kl_mean": _thr("<=", 0.05),
                }
            },
            "N": {
                "thresholds": {
                    "nonfinite_count": _thr("<=", 0),
                    "wire_dtype_mismatch_count": _thr("<=", 0),
                    "wire_shape_mismatch_count": _thr("<=", 0),
                    "activation_abs_max": _thr("<=", 6.0e4),
                    "extra_output_count": _thr("<=", 0),
                }
            },
        },
        "not_evaluated_gates": {
            "G": "generation consistency (8 prompts x 64 greedy tokens, run twice) needs the actual route devices; evaluated in the SB2 slice, not v0",
            "P": "join-time probe compares a canned hidden-state input across the joining device; needs the device, not evaluated in v0",
        },
        "calibration": None,
        "frozen_at": None,
    }
    policy["policy_digest"] = policy_digest(policy)
    return policy


_POLICY_FIELDS = {
    "protocol",
    "policy_id",
    "status",
    "model",
    "wire",
    "metric_params",
    "representations",
    "gates",
    "not_evaluated_gates",
    "calibration",
    "frozen_at",
    "policy_digest",
}


def policy_digest(policy: Mapping[str, Any]) -> str:
    body = {key: value for key, value in policy.items() if key != "policy_digest"}
    return digest_of(body)


def validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_FIELDS:
        _reject("parity_policy_invalid", "fields")
    value = copy.deepcopy(dict(policy))
    model = value["model"]
    params = value["metric_params"]
    if (
        value["protocol"] != POLICY_PROTOCOL
        or not isinstance(value["policy_id"], str)
        or not _NAME.fullmatch(value["policy_id"])
        or value["status"] not in ("provisional", "frozen")
        or not isinstance(model, Mapping)
        or set(model) != {"model_id", "revision"}
        or not isinstance(model["model_id"], str)
        or not isinstance(model["revision"], str)
        or _REVISION.fullmatch(model["revision"]) is None
        or not isinstance(value["wire"], Mapping)
        or set(value["wire"]) != {"dtype"}
        or value["wire"]["dtype"] != "float32"
        or not isinstance(params, Mapping)
        or set(params) != {"topk_overlap_k", "max_abs_delta_top_k", "top1_margin_floor"}
        or type(params["topk_overlap_k"]) is not int
        or type(params["max_abs_delta_top_k"]) is not int
        or params["topk_overlap_k"] < 1
        or params["max_abs_delta_top_k"] < 1
        or not _finite_number(params["top1_margin_floor"])
        or params["top1_margin_floor"] < 0
    ):
        _reject("parity_policy_invalid", "header")
    representations = value["representations"]
    if not isinstance(representations, Mapping) or not representations:
        _reject("parity_policy_invalid", "representations")
    for name, entry in representations.items():
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not isinstance(entry, Mapping)
            or set(entry) != {"class"}
            or entry["class"] not in QUANTIZATION_CLASSES
        ):
            _reject("parity_policy_invalid", f"representation {name}")
    gates = value["gates"]
    if not isinstance(gates, Mapping) or set(gates) != {"H", "Q0", "Q1", "N"}:
        _reject("parity_policy_invalid", "gates")
    allowed = {"H": H_METRICS, "Q0": Q_METRICS, "Q1": Q_METRICS, "N": N_METRICS}
    for gate, body in gates.items():
        if not isinstance(body, Mapping) or set(body) != {"thresholds"}:
            _reject("parity_policy_invalid", f"gate {gate}")
        try:
            body_thresholds = validate_threshold_spec(body["thresholds"], allowed=allowed[gate])
        except BackendParityError as exc:
            raise BackendParityError("parity_policy_invalid", f"gate {gate}: {exc}") from exc
        value["gates"][gate]["thresholds"] = body_thresholds
    not_evaluated = value["not_evaluated_gates"]
    if (
        not isinstance(not_evaluated, Mapping)
        or set(not_evaluated) != {"G", "P"}
        or not all(isinstance(reason, str) and reason for reason in not_evaluated.values())
    ):
        _reject("parity_policy_invalid", "not_evaluated_gates")
    if value["status"] == "frozen":
        if type(value["frozen_at"]) is not int or value["frozen_at"] <= 0 or value["calibration"] is None:
            _reject("parity_policy_invalid", "frozen policy needs frozen_at and a calibration")
    elif value["frozen_at"] is not None:
        _reject("parity_policy_invalid", "provisional policy must not carry frozen_at")
    if value["calibration"] is not None and not isinstance(value["calibration"], Mapping):
        _reject("parity_policy_invalid", "calibration")
    if not _is_digest(value["policy_digest"]):
        _reject("parity_policy_invalid", "policy_digest")
    if value["policy_digest"] != policy_digest(policy):
        _reject("parity_policy_digest_mismatch")
    return value


def load_policy(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendParityError("parity_policy_unreadable", str(exc)) from exc
    return validate_policy(raw)


def write_policy(path: Path, policy: Mapping[str, Any]) -> None:
    checked = validate_policy(policy)
    if policy_digest(checked) != checked["policy_digest"]:
        _reject("parity_policy_digest_mismatch", "policy must be float-normalised before its digest is computed")
    Path(path).write_text(
        json.dumps(checked, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def route_quantization_class(policy: Mapping[str, Any], representations: Sequence[str]) -> str:
    """The worst class over the per-stage representation list; unknown -> refused."""

    if not representations:
        _reject("parity_representation_unknown", "empty list")
    worst = "Q0"
    for name in representations:
        entry = policy["representations"].get(name)
        if entry is None:
            _reject("parity_representation_unknown", str(name))
        if entry["class"] == "Q1":
            worst = "Q1"
    return worst


# --------------------------------------------------------------------------- corpus


def validate_corpus(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"protocol", "model_id", "revision", "cases"}:
        _reject("parity_corpus_invalid", "fields")
    if (
        value["protocol"] != CORPUS_PROTOCOL
        or not isinstance(value["model_id"], str)
        or not isinstance(value["revision"], str)
        or _REVISION.fullmatch(value["revision"]) is None
        or not isinstance(value["cases"], list)
        or not 1 <= len(value["cases"]) <= 32
    ):
        _reject("parity_corpus_invalid", "header")
    seen: set[str] = set()
    cases = []
    for case in value["cases"]:
        if not isinstance(case, Mapping) or set(case) != {"case_id", "kind", "text", "token_ids", "prefill_len"}:
            _reject("parity_corpus_invalid", "case fields")
        ids = case["token_ids"]
        if (
            not isinstance(case["case_id"], str)
            or _CASE_ID.fullmatch(case["case_id"]) is None
            or case["case_id"] in seen
            or not isinstance(case["kind"], str)
            or not isinstance(case["text"], str)
            or not isinstance(ids, list)
            or len(ids) < 3
            or len(ids) > 4096
            or not all(type(token) is int and 0 <= token < 1 << 20 for token in ids)
            or type(case["prefill_len"]) is not int
            or not 1 <= case["prefill_len"] < len(ids)
        ):
            _reject("parity_corpus_invalid", f"case {case.get('case_id')}")
        seen.add(case["case_id"])
        cases.append(
            {
                "case_id": case["case_id"],
                "kind": case["kind"],
                "text": case["text"],
                "token_ids": list(ids),
                "prefill_len": case["prefill_len"],
            }
        )
    return {
        "protocol": CORPUS_PROTOCOL,
        "model_id": value["model_id"],
        "revision": value["revision"],
        "cases": cases,
    }


def corpus_digest(corpus: Mapping[str, Any]) -> str:
    return digest_of(corpus)


def load_corpus(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendParityError("parity_corpus_unreadable", str(exc)) from exc
    return validate_corpus(raw)


def scored_positions(case: Mapping[str, Any]) -> list[int]:
    """Teacher-forced scoring: logits at position t predict token t+1.

    Prefill covers tokens [0, prefill_len); scoring starts at the last prefill
    position and each remaining token is then decoded one step at a time.
    """

    return list(range(case["prefill_len"] - 1, len(case["token_ids"]) - 1))


# --------------------------------------------------------------------------- captures


@dataclass
class CaseCapture:
    """What one route (or reference) produced for one corpus case.

    ``logits``  [n_scored, vocab] at the scored positions;
    ``hops``    {boundary layer b -> [positions, hidden]} as RECEIVED after the wire
                for every position 0..L-2 (prefill rows then one row per decode step);
    ``hop_wire`` {b -> {"dtype", "shape"}} of the first frame on that hop;
    ``extra_outputs`` tensors that left a non-final stage besides the hidden state.
    """

    case_id: str
    logits: np.ndarray
    hops: dict[int, np.ndarray] = field(default_factory=dict)
    hop_wire: dict[int, dict[str, Any]] = field(default_factory=dict)
    extra_outputs: int = 0


@dataclass
class RouteCapture:
    cases: dict[str, CaseCapture]
    meta: dict[str, Any] = field(default_factory=dict)


def _stack_logits(capture: RouteCapture, case_ids: Sequence[str]) -> np.ndarray:
    return np.concatenate([np.asarray(capture.cases[c].logits, dtype=np.float64) for c in case_ids], axis=0)


def _common_cases(a: RouteCapture, b: RouteCapture) -> list[str]:
    if set(a.cases) != set(b.cases) or not a.cases:
        _reject("parity_case_mismatch")
    return sorted(a.cases)


def _result(
    gate: str,
    status: str,
    *,
    reason: str = "",
    metrics: Mapping[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "gate": gate,
        "status": status,
        "reason": reason,
        "metrics": dict(metrics or {}),
        "checks": list(checks or []),
        "detail": dict(detail or {}),
    }


def logits_metrics(ref: np.ndarray, test: np.ndarray, params: Mapping[str, Any]) -> dict[str, Any]:
    agree, scored, excluded = top1_agreement(ref, test, margin_floor=float(params["top1_margin_floor"]))
    kl = kl_rows(ref, test)
    return {
        "positions": int(ref.shape[0]),
        "top1_agreement": agree,
        "top1_scored": scored,
        "top1_excluded_near_ties": excluded,
        "topk_overlap_mean": topk_overlap(ref, test, int(params["topk_overlap_k"])),
        "logits_cosine_min": float(cosine_rows(ref, test).min()),
        "logits_cosine_mean": float(cosine_rows(ref, test).mean()),
        "logits_max_abs_delta_top": max_abs_delta(ref, test, top_k=int(params["max_abs_delta_top_k"])),
        "kl_mean": float(kl.mean()),
        "kl_max": float(kl.max()),
    }


# --------------------------------------------------------------------------- gates


def gate_h(route: RouteCapture, monolith: RouteCapture, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Hop/split parity: the actual route against a same-numerics monolith."""

    cases = _common_cases(route, monolith)
    boundaries = {tuple(sorted(route.cases[c].hops)) for c in cases} | {
        tuple(sorted(monolith.cases[c].hops)) for c in cases
    }
    if len(boundaries) != 1:
        return _result("H", "fail", reason="hop_boundary_mismatch: route and monolith disagree on hop boundaries")
    metrics = logits_metrics(_stack_logits(monolith, cases), _stack_logits(route, cases), policy["metric_params"])
    cosines: list[float] = []
    rels: list[float] = []
    per_hop: dict[str, dict[str, float]] = {}
    for boundary in next(iter(boundaries)):
        ref_rows = np.concatenate([monolith.cases[c].hops[boundary] for c in cases], axis=0)
        test_rows = np.concatenate([route.cases[c].hops[boundary] for c in cases], axis=0)
        cos = float(cosine_rows(ref_rows, test_rows).min())
        rel = float(rel_l2_rows(ref_rows, test_rows).max())
        per_hop[str(boundary)] = {"cosine_min": cos, "rel_l2_max": rel, "rows": int(ref_rows.shape[0])}
        cosines.append(cos)
        rels.append(rel)
    metrics["hop_cosine_min"] = min(cosines) if cosines else 1.0
    metrics["hop_rel_l2_max"] = max(rels) if rels else 0.0
    checks = evaluate_thresholds(metrics, policy["gates"]["H"]["thresholds"])
    status = "pass" if all(check["pass"] for check in checks) else "fail"
    return _result("H", status, metrics=metrics, checks=checks, detail={"per_hop": per_hop})


def gate_q(
    route: RouteCapture, oracle: RouteCapture, policy: Mapping[str, Any], quantization_class: str
) -> dict[str, Any]:
    """Quantization-class parity of the route's final distributions against the oracle."""

    if quantization_class not in QUANTIZATION_CLASSES:
        _reject("parity_gate_class_invalid", str(quantization_class))
    cases = _common_cases(route, oracle)
    metrics = logits_metrics(_stack_logits(oracle, cases), _stack_logits(route, cases), policy["metric_params"])
    gate = f"Q{quantization_class[1:]}"
    checks = evaluate_thresholds(metrics, policy["gates"][gate]["thresholds"])
    status = "pass" if all(check["pass"] for check in checks) else "fail"
    return _result(gate, status, metrics=metrics, checks=checks)


def gate_n(route: RouteCapture, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Hygiene: finite, exact wire dtype/shape, magnitude cap, no extra outputs."""

    nonfinite = dtype_bad = shape_bad = extra = 0
    magnitude = 0.0
    for case in route.cases.values():
        extra += int(case.extra_outputs)
        nonfinite += int(np.count_nonzero(~np.isfinite(case.logits)))
        for boundary, hop in case.hops.items():
            array = np.asarray(hop)
            nonfinite += int(np.count_nonzero(~np.isfinite(array)))
            finite = array[np.isfinite(array)]
            if finite.size:
                magnitude = max(magnitude, float(np.abs(finite).max()))
            wire = case.hop_wire.get(boundary)
            if wire is None or wire.get("dtype") != policy["wire"]["dtype"]:
                dtype_bad += 1
            shape = tuple(wire.get("shape", ())) if wire else ()
            if len(shape) != 3 or shape[0] != 1 or shape[2] != array.shape[-1]:
                shape_bad += 1
    metrics = {
        "nonfinite_count": nonfinite,
        "wire_dtype_mismatch_count": dtype_bad,
        "wire_shape_mismatch_count": shape_bad,
        "activation_abs_max": magnitude,
        "extra_output_count": extra,
    }
    checks = evaluate_thresholds(metrics, policy["gates"]["N"]["thresholds"])
    status = "pass" if all(check["pass"] for check in checks) else "fail"
    return _result("N", status, metrics=metrics, checks=checks)


def gate_g(policy: Mapping[str, Any]) -> dict[str, Any]:
    return _result("G", "not_evaluated", reason=policy["not_evaluated_gates"]["G"])


def gate_p(policy: Mapping[str, Any]) -> dict[str, Any]:
    return _result("P", "not_evaluated", reason=policy["not_evaluated_gates"]["P"])


def route_verdict(gates: Mapping[str, Mapping[str, Any]]) -> str:
    """``fail`` if any gate failed; ``pass`` only when every gate was evaluated and passed."""

    statuses = [str(gate["status"]) for gate in gates.values()]
    if any(status not in GATE_STATUSES for status in statuses):
        _reject("parity_gate_status_invalid")
    if "fail" in statuses:
        return "fail"
    if any(status == "not_evaluated" for status in statuses):
        return "partial_pass"
    return "pass"


def evaluate_route(
    route: RouteCapture,
    monolith: RouteCapture,
    oracle: RouteCapture,
    policy: Mapping[str, Any],
    representations: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Run every gate for one route; the Q gate class is the worst stage representation."""

    quantization_class = route_quantization_class(policy, representations)
    q = gate_q(route, oracle, policy, quantization_class)
    return {
        "H": gate_h(route, monolith, policy),
        q["gate"]: q,
        "N": gate_n(route, policy),
        "G": gate_g(policy),
        "P": gate_p(policy),
    }


# --------------------------------------------------------------------------- route record

_STAGE_FIELDS = {
    "stage_index",
    "layer_start",
    "layer_end",
    "backend",
    "backend_version",
    "build_digest",
    "device_class",
    "quantization",
}
_ROUTE_FIELDS = {"model_id", "revision", "source_artifact_digest", "n_layer", "wire_dtype", "stages"}
_RECORD_FIELDS = {
    "protocol",
    "route",
    "route_digest",
    "corpus_digest",
    "policy_digest",
    "policy_status",
    "oracle",
    "quantization_class",
    "gates",
    "verdict",
    "unevaluated_gates",
    "created_at",
    "expires_at",
    "claim_boundary",
    "record_digest",
}
_ORACLE_FIELDS = {"implementation", "runtime_digest", "archive_digest", "dtype"}
_ENVELOPE_FIELDS = {"protocol", "record", "signature", "verification_key"}


def validate_route(route: Any) -> dict[str, Any]:
    if not isinstance(route, Mapping) or set(route) != _ROUTE_FIELDS:
        _reject("parity_route_invalid", "fields")
    stages = route["stages"]
    if (
        not isinstance(route["model_id"], str)
        or _REVISION.fullmatch(str(route["revision"])) is None
        or not _is_digest(route["source_artifact_digest"])
        or type(route["n_layer"]) is not int
        or route["n_layer"] < 1
        or route["wire_dtype"] not in ("float16", "bfloat16", "float32")
        or not isinstance(stages, list)
        or not 1 <= len(stages) <= 16
    ):
        _reject("parity_route_invalid", "header")
    expected_start = 0
    for index, stage in enumerate(stages):
        if (
            not isinstance(stage, Mapping)
            or set(stage) != _STAGE_FIELDS
            or stage["stage_index"] != index
            or type(stage["layer_start"]) is not int
            or type(stage["layer_end"]) is not int
            or stage["layer_start"] != expected_start
            or stage["layer_end"] <= stage["layer_start"]
            or stage["backend"] not in BACKEND_IDS
            or stage["device_class"] not in DEVICE_CLASSES
            or not isinstance(stage["backend_version"], str)
            or not _is_digest(stage["build_digest"])
            or not isinstance(stage["quantization"], str)
        ):
            _reject("parity_route_ranges_invalid", f"stage {index}")
        expected_start = stage["layer_end"]
    if expected_start != route["n_layer"]:
        _reject("parity_route_ranges_invalid", "stages do not cover the layer range")
    return copy.deepcopy(dict(route))


def route_digest(route: Mapping[str, Any]) -> str:
    return digest_of(validate_route(route))


def record_body_digest(record: Mapping[str, Any]) -> str:
    return digest_of({key: value for key, value in record.items() if key != "record_digest"})


def build_route_record(
    *,
    route: Mapping[str, Any],
    corpus: Mapping[str, Any],
    policy: Mapping[str, Any],
    oracle: Mapping[str, Any],
    gates: Mapping[str, Mapping[str, Any]],
    created_at: float,
    ttl_seconds: float = 30 * 24 * 3600,
) -> dict[str, Any]:
    checked_route = validate_route(route)
    checked_policy = validate_policy(policy)
    checked_corpus = validate_corpus(corpus)
    if set(oracle) != _ORACLE_FIELDS:
        _reject("parity_record_invalid", "oracle fields")
    quantization_class = route_quantization_class(
        checked_policy, [stage["quantization"] for stage in checked_route["stages"]]
    )
    if checked_route["wire_dtype"] != checked_policy["wire"]["dtype"]:
        # recorded honestly; the N gate (and the verifier) fail it.
        pass
    gate_map = {name: copy.deepcopy(dict(result)) for name, result in gates.items()}
    expected_names = {"H", f"Q{quantization_class[1:]}", "N", "G", "P"}
    if set(gate_map) != expected_names:
        _reject("parity_record_invalid", f"gates {sorted(gate_map)} != {sorted(expected_names)}")
    record: dict[str, Any] = {
        "protocol": RECORD_PROTOCOL,
        "route": checked_route,
        "route_digest": route_digest(checked_route),
        "corpus_digest": corpus_digest(checked_corpus),
        "policy_digest": checked_policy["policy_digest"],
        "policy_status": checked_policy["status"],
        "oracle": dict(oracle),
        "quantization_class": quantization_class,
        "gates": gate_map,
        "verdict": route_verdict(gate_map),
        "unevaluated_gates": sorted(n for n, g in gate_map.items() if g["status"] == "not_evaluated"),
        "created_at": float(created_at),
        "expires_at": float(created_at) + float(ttl_seconds),
        "claim_boundary": CLAIM_BOUNDARY,
    }
    record["record_digest"] = record_body_digest(record)
    return record


def sign_route_record(signer: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a record in an Ed25519 envelope (``signer`` is an ``Ed25519EvidenceSigner``)."""

    if record.get("record_digest") != record_body_digest(record):
        _reject("parity_record_digest_mismatch")
    return {
        "protocol": RECORD_ENVELOPE_PROTOCOL,
        "record": copy.deepcopy(dict(record)),
        "signature": signer.sign(record),
        "verification_key": signer.public_key_record(),
    }


def verify_route_record(
    envelope: Any,
    *,
    policy: Mapping[str, Any],
    now: float,
    expected_key_digest: str | None,
    corpus: Mapping[str, Any] | None = None,
    expected_route: Mapping[str, Any] | None = None,
    unanchored_ok: bool = False,
) -> dict[str, Any]:
    """Fail-closed verification; returns the record on success.

    Checks, in order: envelope shape, key anchor, signature, record digest, route
    digest + range coverage, policy digest, optional corpus/route equality, that
    every evaluated gate re-evaluates identically from its recorded metrics under
    the supplied policy, the verdict, the quantization class, the wire dtype and
    expiry.  Any mismatch raises ``BackendParityError``.
    """

    from mycelium_qualification.signing import build_ed25519_verifier

    if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS:
        _reject("parity_record_envelope_invalid")
    if envelope["protocol"] != RECORD_ENVELOPE_PROTOCOL:
        _reject("parity_record_envelope_invalid", "protocol")
    record = envelope["record"]
    signature = envelope["signature"]
    key = envelope["verification_key"]
    if not isinstance(record, Mapping) or not isinstance(signature, Mapping) or not isinstance(key, Mapping):
        _reject("parity_record_envelope_invalid")
    if expected_key_digest is None:
        if not unanchored_ok:
            _reject("parity_record_key_unanchored")
    elif signature.get("verification_key_digest") != expected_key_digest or key.get(
        "verification_key_digest"
    ) != expected_key_digest:
        _reject("parity_record_key_mismatch")
    try:
        verify = build_ed25519_verifier([dict(key)])
        signature_ok = bool(verify(canonical(record), dict(signature)))
    except Exception as exc:
        raise BackendParityError("parity_record_signature_invalid", str(exc)) from exc
    if not signature_ok:
        _reject("parity_record_signature_invalid")
    if set(record) != _RECORD_FIELDS or record["protocol"] != RECORD_PROTOCOL:
        _reject("parity_record_invalid", "fields")
    if record["record_digest"] != record_body_digest(record):
        _reject("parity_record_digest_mismatch")
    checked_route = validate_route(record["route"])
    if record["route_digest"] != digest_of(checked_route):
        _reject("parity_route_digest_mismatch")
    if expected_route is not None and digest_of(validate_route(expected_route)) != record["route_digest"]:
        _reject("parity_route_digest_mismatch", "route differs from the expected route tuple")
    checked_policy = validate_policy(policy)
    if record["policy_digest"] != checked_policy["policy_digest"]:
        _reject("parity_policy_digest_mismatch", "record was issued under another policy")
    if record["policy_status"] != checked_policy["status"]:
        _reject("parity_policy_digest_mismatch", "policy status differs")
    if corpus is not None and record["corpus_digest"] != corpus_digest(validate_corpus(corpus)):
        _reject("parity_corpus_digest_mismatch")
    if checked_route["wire_dtype"] != checked_policy["wire"]["dtype"]:
        _reject("parity_wire_dtype_mismatch")
    quantization_class = route_quantization_class(
        checked_policy, [stage["quantization"] for stage in checked_route["stages"]]
    )
    if record["quantization_class"] != quantization_class:
        _reject("parity_quantization_class_mismatch")
    gates = record["gates"]
    gate_names = {"H", f"Q{quantization_class[1:]}", "N", "G", "P"}
    if not isinstance(gates, Mapping) or set(gates) != gate_names:
        _reject("parity_record_invalid", "gate set")
    for name, result in gates.items():
        if not isinstance(result, Mapping) or result.get("gate") != name:
            _reject("parity_record_invalid", f"gate {name}")
        status = result.get("status")
        if status == "not_evaluated":
            if name not in ("G", "P") or result.get("checks") or not result.get("reason"):
                _reject("parity_gate_status_invalid", name)
            continue
        if name in ("G", "P"):
            # v0 has no evaluator for these; refuse a record that claims one.
            _reject("parity_gate_status_invalid", f"{name} cannot be evaluated by v0")
        recomputed = evaluate_thresholds(result["metrics"], checked_policy["gates"][name]["thresholds"])
        if recomputed != result["checks"]:
            _reject("parity_gate_recompute_mismatch", name)
        if (status == "pass") != all(check["pass"] for check in recomputed):
            _reject("parity_gate_recompute_mismatch", f"{name} status")
    if record["verdict"] != route_verdict(gates):
        _reject("parity_verdict_mismatch")
    if record["unevaluated_gates"] != sorted(n for n, g in gates.items() if g["status"] == "not_evaluated"):
        _reject("parity_record_invalid", "unevaluated_gates")
    if not (_finite_number(record["created_at"]) and _finite_number(record["expires_at"])):
        _reject("parity_record_invalid", "times")
    if record["created_at"] > now + 300.0:
        _reject("parity_record_from_the_future")
    if now > record["expires_at"]:
        _reject("parity_record_expired")
    return copy.deepcopy(dict(record))


# --------------------------------------------------------------------------- defect battery


def summarize_battery(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll a seeded-defect battery up: every defect must FAIL, every control must PASS.

    A row is ``{"name", "kind": "defect"|"control", "verdict", "failed_gates",
    "failed_metrics", ...}``.  ``verdict`` is a route verdict.  A defect row passes
    the battery iff its verdict is ``fail``; a control row iff it is not ``fail``.
    """

    problems = []
    for row in rows:
        kind = row.get("kind")
        if kind not in ("defect", "control"):
            _reject("parity_battery_invalid", str(row.get("name")))
        failed = row["verdict"] == "fail"
        if kind == "defect" and not failed:
            problems.append({"name": row["name"], "problem": "defect_not_detected"})
        if kind == "control" and failed:
            problems.append({"name": row["name"], "problem": "legitimate_noise_rejected"})
    defects = [r for r in rows if r["kind"] == "defect"]
    controls = [r for r in rows if r["kind"] == "control"]
    return {
        "protocol": BATTERY_PROTOCOL,
        "defects_total": len(defects),
        "defects_detected": sum(1 for r in defects if r["verdict"] == "fail"),
        "controls_total": len(controls),
        "controls_passed": sum(1 for r in controls if r["verdict"] != "fail"),
        "all_defects_fail": all(r["verdict"] == "fail" for r in defects),
        "all_controls_pass": all(r["verdict"] != "fail" for r in controls),
        "problems": problems,
        "rows": [dict(r) for r in rows],
    }
