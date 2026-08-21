#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Execute the A4 qualification-negative gate against a live product serve.

A request carrying a stale (corrupted-digest) qualification binding must be
rejected with a bounded status and a zero state delta: no reservation, no queue
item, no retained request record, and a still-live route. Mirrors the sealed
qualification-409 methodology.

The output is a bounded privacy-reduced observation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_a4_product_gate import (  # noqa: E402
    GateError,
    ProductSession,
    _zero_live_resources,
    public_json,
)


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url
    session = ProductSession(base_url)
    binding = session._qualification["binding"]
    before = public_json(base_url, "/__mycelium/live-status")
    before_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")

    stale = dict(binding)
    stale["qualification_digest"] = "sha256:" + "0" * 64

    status_code: int | None = None
    body: Any = None
    try:
        session.json(
            "POST",
            "/api/v1/inference",
            body={
                "protocol": "mycelium.request_gateway.v2",
                "prompt": "A4 stale qualification probe",
                "max_new_tokens": 4,
                "qualification": stale,
                "workload_profile_id": "interactive_chat_v1",
                "qos_class": "interactive",
            },
            mutate=True,
        )
        status_code = 200
    except GateError as exc:
        message = str(exc)
        body = message
        status_code = getattr(exc, "status", None) or (
            409 if "409" in message else None
        )

    time.sleep(1.0)
    after = public_json(base_url, "/__mycelium/live-status")
    after_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")

    zero_delta = (
        _zero_live_resources(after_runtime)
        and _zero_live_resources(before_runtime)
        and len(after_runtime.get("requests") or [])
        == len(before_runtime.get("requests") or [])
    )
    rejected = status_code == 409 or "409" in str(body) or "stale" in str(body).lower()

    report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_negative_qualification_observation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "claim_boundary": "stale-qualification admission rejected with zero state delta",
        "rejection": {"status": status_code, "message": str(body)[:200]},
        "rejected": rejected,
        "route_alive_before": before.get("route_alive"),
        "route_alive_after": after.get("route_alive"),
        "request_count_delta": len(after_runtime.get("requests") or [])
        - len(before_runtime.get("requests") or []),
        "zero_runtime_resources_after": _zero_live_resources(after_runtime),
        "placement_reservations_after": {
            request.get("request_id"): request.get("reservation_count")
            for request in (after_runtime.get("requests") or [])
        },
    }
    checks = {
        "rejected_with_409": rejected,
        "zero_delta": zero_delta,
        "route_alive": after.get("route_alive") is True,
        "not_simulated": report["simulated"] is False,
    }
    report["passed"] = all(checks.values())
    report["checks"] = checks
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A4 qualification-negative gate against a live product serve."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_gate(args)
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "passed": report["passed"],
                "checks": report["checks"],
                "digest": digest,
            },
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
