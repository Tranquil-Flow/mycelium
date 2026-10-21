#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the A14b region-bearing product snapshot the browser contract test decodes.

Synthetic conformance input only, published through the ordinary product
projector: two reviewed coarse-region evidence records are built with the frozen
A14b conformance literals and passed to `ProductProjector.project` exactly as a
caller holding reviewed records would pass them. This creates no region
authority and claims no physical evidence; a snapshot published without
evidence keeps the A14a projection byte-identical.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.a14b_acceptance import conformance  # noqa: E402 - synthetic literals only

NOW = 1_000_000


def published_snapshot() -> dict:
    base = conformance.snapshot(NOW)
    member = conformance.members(base)[0]
    endpoint = conformance.node_of_kind(base, "endpoint")
    evidence = [
        conformance.region_evidence(member["id"], "member", "owner_declared", "europe"),
        conformance.region_evidence(
            endpoint["id"], "relay", "reviewed_relay_metadata", "asia"
        ),
    ]
    members, route, internet = conformance.a14a_inputs(now=NOW)
    projector = conformance.projector(pseudonym_salt=b"c" * 32)
    return projector.project(
        members=members,
        route_status=route,
        qualification=None,
        internet_native=internet,
        now_unix_ms=NOW,
        coarse_region_evidence=evidence,
    )


def main() -> int:
    fixture = published_snapshot()
    encoded = json.dumps(fixture, sort_keys=True, indent=2) + "\n"
    path = ROOT / "ui/web/src/test/a14bRegionConformance.json"
    if "--check" in sys.argv:
        if path.read_text() != encoded:
            raise SystemExit("A14b region conformance fixture drift")
    else:
        path.write_text(encoded)
    print("A14b region conformance fixture verified; physical_devices=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
