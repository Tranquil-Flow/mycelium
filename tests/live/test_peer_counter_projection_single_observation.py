# SPDX-License-Identifier: AGPL-3.0-only
"""The published peer counter triple must come from exactly ONE observation.

``scripts/run_m23_kv_gate.py:one_token_decode`` requires, per node::

    applied_operation_count == prefill_operation_count + decode_operation_count

and that invariant is *true by construction* in the runtime:
``mycelium_router/mlx_runtime.py`` (and its numpy twin) increments
``_applied_operation_count`` and exactly one of ``_prefill_operation_count`` /
``_decode_operation_count`` inside the same locked operation, so one observed
``kv_snapshot()`` can never violate it.

It could still break in the *published payload*, because the projection mixed
two provenances for the same triple: ``PhysicalLiveRoute._peer_counters()``
takes a per-field high-water ``max()`` across the full snapshot
(``_last_snapshots``) and the lean health probe (``_last_health`` -- which
carries only a lock-free ``applied`` counter, never the phase counters), and
the peer record publishes ``prefill``/``decode`` from the snapshot's runtime
block.  A health probe that sampled a later instant than the snapshot therefore
put one instant's ``applied`` beside another instant's ``prefill``/``decode``:
round-59 live evidence, ``applied 37`` sitting beside ``prefill 9 / decode 3``.

These tests pin the emitted record: ``applied`` / ``prefill`` / ``decode`` (and
``frames_sent`` / ``frames_received``) are drawn from a single observation, so
the gate's runtime-true identity holds in the payload exactly as it holds in
the runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_live import route as route_module
from mycelium_live.route import PhysicalLiveRoute
from tests.live.token_path_test_support import ObserverTokenOwner

NODE_ID = "node-a"
PLACEMENT_ID = "stage-000-primary"


def _graph() -> Any:
    return SimpleNamespace(
        deployment_id="deployment-counter-projection",
        model_id="org/model",
        topology_version=3,
        stages=(
            SimpleNamespace(
                stage_id="stage-000",
                layer_range=SimpleNamespace(start_layer=0, end_layer_exclusive=2),
                component_roles=("input_embedding",),
                placements=(
                    SimpleNamespace(
                        placement_id=PLACEMENT_ID,
                        node_id=NODE_ID,
                        runtime_backend="numpy",
                    ),
                ),
            ),
        ),
    )


def _route(monkeypatch: pytest.MonkeyPatch) -> PhysicalLiveRoute:
    """A real route with one peer; only the graph document is a double."""

    monkeypatch.setattr(
        route_module, "execution_graph_from_document", lambda _document: _graph()
    )
    return PhysicalLiveRoute(
        token_path_owner=ObserverTokenOwner(),
        controller=SimpleNamespace(peers=(SimpleNamespace(node_id=NODE_ID),)),
        endpoints={},
        run_plan={
            "deployment_id": "deployment-counter-projection",
            "nodes": [{"node_id": NODE_ID, "configure": {"graph": {}}}],
        },
    )


def _snapshot_details(
    *,
    applied: int,
    prefill: int,
    decode: int,
    frames_sent: int,
    frames_received: int,
) -> dict[str, Any]:
    """One node snapshot: the only observation carrying the full triple."""

    return {
        "runtime": {
            "mode": "stage_local_kv",
            "backend": "numpy",
            "architecture": "qwen2",
            "applied_operation_count": applied,
            "prefill_operation_count": prefill,
            "decode_operation_count": decode,
            "placement_counters": {
                PLACEMENT_ID: {
                    "prefill_operation_count": prefill,
                    "decode_operation_count": decode,
                    "active_state_count": 0,
                    "active_kv_bytes": 0,
                }
            },
        },
        "transport_counters": {
            "remote_frames_sent": frames_sent,
            "remote_frames_received": frames_received,
        },
    }


def _health_details(
    *,
    applied: int,
    frames_sent: int,
    frames_received: int,
) -> dict[str, Any]:
    """A lean health probe: lock-free ``applied`` plus transport, no phases."""

    return {
        "transport_counters": {
            "remote_frames_sent": frames_sent,
            "remote_frames_received": frames_received,
        },
        "runtime_counters": {"applied_operation_count": applied},
    }


def _peer(route: PhysicalLiveRoute) -> dict[str, Any]:
    status = route.public_status()
    peers = {peer["node_id"]: peer for peer in status["peers"]}
    return peers[NODE_ID]


def _counter_tuple(peer: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return (
        peer["frames_sent"],
        peer["frames_received"],
        peer["applied_operation_count"],
        peer["prefill_operation_count"],
        peer["decode_operation_count"],
    )


def test_published_peer_counter_triple_satisfies_the_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live shape: a fresher health ``applied`` beside the snapshot's phases."""

    route = _route(monkeypatch)
    route._last_snapshots = {
        NODE_ID: {
            "details": _snapshot_details(
                applied=12,
                prefill=9,
                decode=3,
                frames_sent=17,
                frames_received=19,
            )
        }
    }
    route._last_health = {
        NODE_ID: {
            "details": _health_details(
                applied=37,
                frames_sent=100,
                frames_received=200,
            )
        }
    }

    peer = _peer(route)
    applied = peer["applied_operation_count"]
    prefill = peer["prefill_operation_count"]
    decode = peer["decode_operation_count"]

    assert applied == prefill + decode, (
        "the emitted peer record must not blend one observation's applied with "
        f"another observation's phases: applied={applied} prefill={prefill} "
        f"decode={decode} (identity requires {prefill + decode})"
    )


def test_published_peer_counters_come_from_exactly_one_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No per-field high-water: the record equals one observation's tuple."""

    route = _route(monkeypatch)
    route._last_snapshots = {
        NODE_ID: {
            "details": _snapshot_details(
                applied=12,
                prefill=9,
                decode=3,
                frames_sent=17,
                frames_received=19,
            )
        }
    }
    route._last_health = {
        NODE_ID: {
            "details": _health_details(
                applied=37,
                frames_sent=100,
                frames_received=200,
            )
        }
    }

    assert _counter_tuple(_peer(route)) == (17, 19, 12, 9, 3)


def test_atomic_high_water_keeps_a_later_observation_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A greater-``applied`` observation is published whole, phases included.

    The lean probe does not carry the phase counters today.  If it ever does,
    the aggregate must stay atomic (that observation's own ``prefill`` and
    ``decode`` alongside its ``applied``) instead of max-ing fields
    independently -- which is what broke the identity in the first place.
    """

    route = _route(monkeypatch)
    route._last_snapshots = {
        NODE_ID: {
            "details": _snapshot_details(
                applied=12,
                prefill=9,
                decode=3,
                frames_sent=17,
                frames_received=19,
            )
        }
    }
    route._last_health = {
        NODE_ID: {
            "details": {
                "transport_counters": {
                    "remote_frames_sent": 100,
                    "remote_frames_received": 200,
                },
                "runtime": {
                    "applied_operation_count": 20,
                    "prefill_operation_count": 20,
                    "decode_operation_count": 0,
                },
            }
        }
    }

    # Independent per-field maxima would emit (100, 200, 20, 9, 0) -- 20 != 29.
    assert _counter_tuple(_peer(route)) == (100, 200, 20, 20, 0)


def test_observation_without_the_phase_counters_stays_self_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial observation cannot support the triple; it must not fake one."""

    route = _route(monkeypatch)
    route._last_snapshots = {
        NODE_ID: {
            "details": {
                "runtime": {"mode": "stage_local_kv", "backend": "numpy"},
                "transport_counters": {
                    "remote_frames_sent": 17,
                    "remote_frames_received": 19,
                },
            }
        }
    }
    route._last_health = {
        NODE_ID: {
            "details": _health_details(
                applied=37,
                frames_sent=100,
                frames_received=200,
            )
        }
    }

    peer = _peer(route)
    applied = peer["applied_operation_count"]
    prefill = peer["prefill_operation_count"]
    decode = peer["decode_operation_count"]

    assert applied == prefill + decode, (applied, prefill, decode)
    assert (applied, prefill, decode) == (0, 0, 0)
    # The frame counters still come from that same single observation.
    assert (peer["frames_sent"], peer["frames_received"]) == (17, 19)
