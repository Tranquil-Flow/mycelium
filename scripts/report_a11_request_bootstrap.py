#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""O3: informational paired-request bootstrap; never writes a gate decision.

Consumes the completed product runner's six windows and 72 privacy-reduced
request records. A self-digest detects corruption, not physical provenance.
The three-window-pair decision remains authoritative and is never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
from typing import Any

# Match the existing runner's supported direct-script invocation.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_a11_route_benchmark as runner  # noqa: E402
import mycelium_speculative_decode as sd  # noqa: E402

PROTOCOL = "mycelium.a11_request_bootstrap_report.v1"
MAX_INPUT_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReportRefused(ValueError):
    """Stable, non-sensitive refusal code."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ReportRefused(code)


def _positive(value: Any) -> float:
    _require(type(value) in (int, float), "request_wall_invalid")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ReportRefused("request_wall_invalid") from None
    _require(math.isfinite(number) and number > 0, "request_wall_invalid")
    return number


def report(document: Any) -> dict[str, Any]:
    """Validate pair completeness, then resample whole (A,B) request pairs."""
    _require(isinstance(document, dict), "run_invalid")
    _require(document.get("protocol") == runner.PROTOCOL, "run_protocol_invalid")
    _require(document.get("evidence_class") == "product_route", "run_evidence_class_invalid")
    _require(type(document.get("parity_failures")) is int and document["parity_failures"] == 0,
             "run_parity_invalid")
    try:
        benchmark = sd.validate_speculative_decode_benchmark(document.get("benchmark"))
    except (sd.SpeculativeDecodeContractError, TypeError, ValueError):
        raise ReportRefused("benchmark_invalid") from None
    _require(benchmark["executed"] is True and benchmark["valid"] is True, "benchmark_unusable")
    windows = document.get("windows")
    _require(windows == benchmark["windows"], "benchmark_windows_mismatch")
    _require(tuple(w["mode"] for w in windows) == runner.WINDOW_ORDER, "window_order_invalid")
    records = document.get("requests")
    shapes = runner.window_workload()
    width = len(shapes)
    _require(isinstance(records, list) and len(records) == len(windows) * width,
             "request_count_invalid")
    reference: dict[str, str] = {}
    window_walls: list[list[float]] = []
    for index, window in enumerate(windows):
        batch = records[index * width:(index + 1) * width]
        walls = []
        for shape, record in zip(shapes, batch, strict=True):
            _require(isinstance(record, dict), "request_invalid")
            _require(record.get("shape") == shape.key and record.get("mode") == window["mode"],
                     "request_pairing_invalid")
            _require(record.get("outcome") == "completed", "request_incomplete")
            _require(type(record.get("tokens")) is int and record["tokens"] == shape.max_new_tokens,
                     "request_tokens_invalid")
            digest = record.get("output_digest")
            _require(isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None,
                     "request_digest_invalid")
            _require(digest == reference.setdefault(shape.key, digest), "request_parity_invalid")
            walls.append(_positive(record.get("wall_ms")))
        # The runner rounds each request and the unrounded window sum to 0.001ms.
        # Only that worst-case rounding envelope is allowed, not a relative drift.
        total = math.fsum(walls)
        _require(math.isfinite(total) and abs(total - window["wall_clock_ms"]) <= (width + 1) * 0.0005 + 1e-8,
                 "request_aggregate_mismatch")
        _require(window["completed_requests"] == width
                 and window["target_committed_output_tokens"] == sum(s.max_new_tokens for s in shapes)
                 and window["error_timeout_cancellation_fallback_count"] == 0,
                 "request_window_incomplete")
        window_walls.append(walls)
    pairs: list[tuple[float, float]] = []
    for index in range(0, len(windows), 2):
        a, b = window_walls[index:index + 2]
        if windows[index]["mode"] != "target_only":
            a, b = b, a
        pairs.extend(zip(a, b, strict=True))
    observed = 1.0 - math.fsum(b for _, b in pairs) / math.fsum(a for a, _ in pairs)
    rng = random.Random(runner.BOOTSTRAP_SEED)
    estimates = []
    for _ in range(runner.BOOTSTRAP_RESAMPLES):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        estimates.append(1.0 - math.fsum(b for _, b in sample) / math.fsum(a for a, _ in sample))
    estimates.sort()
    lower = min(observed, estimates[int(0.05 * (len(estimates) - 1))])
    return {
        "protocol": PROTOCOL,
        "informational_only": True,
        "evidence_class": "derived_request_statistics",
        "source_evidence_class": document["evidence_class"],
        "benchmark_digest": benchmark["benchmark_digest"],
        "plan_digest": benchmark["plan_digest"],
        "request_pairs": len(pairs),
        "resamples": runner.BOOTSTRAP_RESAMPLES,
        "seed": runner.BOOTSTRAP_SEED,
        "lower_percentile": 0.05,
        "observed_reduction_fraction": observed,
        "lower_bound_fraction": lower,
        "claim_boundary": "O3 descriptive resampling only; correlated requests are not independent windows; not qualification or physical-provenance verification",
    }


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ReportRefused("nonfinite_json_number")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
        _require(len(raw) <= MAX_INPUT_BYTES, "input_too_large")
        document = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite)
        result = report(document)
        result["input_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        encoded = (json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        # Exclusive create refuses existing files, symlinks and the input itself.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        print(json.dumps({"protocol": PROTOCOL, "informational_only": True, "written": True}))
        return 0
    except ReportRefused as exc:
        print(json.dumps({"code": str(exc)}), file=sys.stderr)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        print(json.dumps({"code": "request_bootstrap_input_or_output_invalid"}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
