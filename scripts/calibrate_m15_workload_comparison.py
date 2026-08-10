#!/usr/bin/env python3
"""Bind privacy-reduced physical request evidence to an M15 plan comparison."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_layer_planner.workload_intelligence import attach_m15_observations  # noqa: E402


def _read(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("m15_calibration_input_unsafe")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("m15_calibration_input_unsafe")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("m15_calibration_input_invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("m15_calibration_input_invalid")
    return value


def _atomic_write(path: Path, document: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--planner-snapshot", type=Path, required=True)
    parser.add_argument("--calibration-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    calibration = _read(args.calibration_input)
    if set(calibration) != {"performance_budgets", "observations"}:
        raise ValueError("m15_calibration_input_invalid")
    result = attach_m15_observations(
        _read(args.comparison),
        _read(args.planner_snapshot),
        calibration["performance_budgets"],
        calibration["observations"],
    )
    _atomic_write(args.output, result)
    print(
        json.dumps(
            {
                "protocol": result["protocol"],
                "calibration_state": result["calibration_state"],
                "profiles": [item["profile_id"] for item in result["observations"]],
                "overall_states": [item["overall_state"] for item in result["observations"]],
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
