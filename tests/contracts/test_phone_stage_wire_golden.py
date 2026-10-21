# SPDX-License-Identifier: AGPL-3.0-only
"""The phone stage-only node's wire golden vectors stay in step with the Router.

``scripts/generate_phone_stage_wire_golden.py`` runs the real Python Router as
the middle placement of a locked three-hop route and records what it admits,
refuses, forwards and reports. Regenerating must reproduce the committed
vectors byte for byte, so any Router change that alters what a middle node
does fails here (and the Rust replay in native/mobile_core/tests/
phone_stage_wire.rs pins the phone to the same bytes).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from mycelium_router.wire import decode_frame

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "contracts" / "phone-stage-wire-golden"


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_phone_stage_wire_golden", ROOT / "scripts" / "generate_phone_stage_wire_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regenerating_reproduces_the_committed_vectors() -> None:
    index = _generator().generate(check=True)
    assert len(index["frames"]) == 20
    assert {case["case"] for case in index["cases"]} == {
        "manifest_locked", "hop_prefill", "hop_decode", "stage_failure", "path_cancellation", "rejections",
    }


def test_forwarded_frames_advance_exactly_one_hop_with_a_recomputed_key() -> None:
    index = _generator().generate(check=True)
    for name in ("hop_prefill", "hop_decode"):
        case = next(c for c in index["cases"] if c["case"] == name)
        inbound = decode_frame((GOLDEN / case["input"]).read_bytes()).message
        forward = decode_frame((GOLDEN / case["forward"]).read_bytes()).message
        assert forward.hop_index == inbound.hop_index + 1 == 2
        assert forward.source_placement_id == "placement-phone"
        assert forward.destination_placement_id == "placement-final"
        assert forward.idempotency_key.endswith(f":{forward.hop_index}")
        assert forward.idempotency_key != inbound.idempotency_key
        assert (forward.phase, forward.token_index) == (inbound.phase, inbound.token_index)
