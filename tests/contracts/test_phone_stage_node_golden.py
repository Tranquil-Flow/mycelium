# SPDX-License-Identifier: AGPL-3.0-only
"""The phone stage-only node's behaviour scripts stay in step with the Router.

``scripts/generate_phone_stage_node_golden.py`` runs the real Python Router as
the middle placement through ordered frame sequences (registration, hops,
retries, refusals, cancellations, pending tombstones, failures) and records
its decisions, the transport acknowledgement and every frame it sends.
Regenerating must reproduce the committed scripts exactly, so a Router change
that alters any of it fails here; ``native/mobile_core/tests/
phone_stage_node.rs`` pins the phone to the same scripts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_phone_stage_node_golden", ROOT / "scripts" / "generate_phone_stage_node_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regenerating_reproduces_the_committed_scripts() -> None:
    index = _generator().generate(check=True)
    names = {scenario["name"] for scenario in index["scenarios"]}
    assert names == {
        "lifecycle", "pending_cancellation", "pending_cancellation_other_source",
        "manifest_sources", "next_attempt", "failures",
        "cross_owner_replacement", "foreign_tombstone", "owner_high_water", "separator_ids",
        "stale_manifest_after_horizon", "creator_slots", "pending_without_slot",
    }
    assert index["phone_bounds"] == {
        "quota_per_creator": 512, "max_creators": 16, "pending_per_source": 256, "pending_total": 4096, "max_paths": 256,
    }
    steps = [step for scenario in index["scenarios"] for step in scenario["steps"]]
    # A refused registration is the only frame the transport does not acknowledge.
    for step in steps:
        if not step["transport_ack"]:
            assert step.get("accepted") is False and "disposition" not in step
