#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit prospective case-v2 shape reader. Never emits physical pass evidence.

A valid file cannot itself establish an executed product observation or
physical qualification, even when the gateway privately binds the notice to
an attempt. The ordinary local blocked fixture retains reservations.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mycelium_a6_cleanup_observation_v2 import CASE, validate_observation
from scripts.run_a6_recovery_case import _read_private_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(CASE,), required=True)
    parser.add_argument("--contract-version", choices=("v2",), required=True)
    parser.add_argument("--observation", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.observation.is_absolute():
            raise ValueError("absolute_observation_path_required")
        value = _read_private_json(args.observation)
        validate_observation(value)
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f"cleanup_v2_invalid:{exc}\n")
    print(json.dumps({"protocol": "mycelium.a6_cleanup_case_shape_check.v2",
                      "case_id": CASE, "shape_validated": True,
                      "product_proof": False, "physical_qualified": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
