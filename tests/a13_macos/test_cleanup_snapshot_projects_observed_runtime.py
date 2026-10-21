# SPDX-License-Identifier: AGPL-3.0-only
"""The lean cleanup snapshot must project the **runtime** observation, not only transport.

Round 59, run `a13-qwen-20260915T234808Z` (preview.59).  The only post-served-request
observation of either node is the sealed-receipt fast path in
`physical_inference_node._snapshot`, whose signed response is exactly

    {"request_cleanup": {...}, "transport_counters": {...}}

with **no** `runtime` block -- verified verbatim from the managed-control mailbox at
sequence 85750 (owner) / 85751 (laptop).

The route reads per-placement work from those *observed* counters
(`mycelium_live/route.py:_peer_counters` builds them from `_last_snapshots[node]` and
`_last_health[node]` and maps a missing runtime block to `applied_operation_count = 0`).
A peer whose health probe channel is not observed -- `peers[1].data_plane_health_observed`
was `False` for the whole run -- therefore reports **zero** placement work after every
served request, whether or not it worked: the frozen pre-request value wins and the delta
is 0.  That is why C5's "per-placement work on both Macs for the served request" cannot be
evidenced from the current readout, and it must be fixed before any further run, because a
correct two-placement served request would read 0 as well.

The counters needed are *already observed* when the receipt is sealed (the sealing snapshot
contains them), so projecting them costs no reacquisition of live state -- which the
immutable 2.0 s cancellation budget depends on.  These tests pin both halves of that
contract: the projection must be present, and taking the lean path must not touch the
runtime.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, cast

from physical_inference_node import PhysicalNodeService

REQUEST_ID = "request-cleanup-runtime-projection"


def _cleanup_subject() -> dict[str, Any]:
    return {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "request_id": REQUEST_ID,
        "request_attempt": 1,
        "path_id": "path-cleanup-runtime-projection",
        "path_attempt": 0,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "command_id": "command-cleanup-runtime-projection",
        "cancellation_generation": 1,
        "publisher_generation": 1,
        "absolute_deadline_ms": 999_999_999,
    }


def _sealed_service(*, runtime_projection: Any, reacquire_raises: bool) -> Any:
    """A node whose cleanup receipt is already sealed, with one observed runtime block."""

    def _no_reacquire() -> dict[str, Any]:
        raise AssertionError("lean cleanup snapshot reacquired live runtime state")

    def _observed_runtime() -> dict[str, Any]:
        return {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "states": {},
            "active_state_count": 0,
            "active_kv_bytes": 0,
        }

    service = object.__new__(PhysicalNodeService)
    untyped = cast(Any, service)
    untyped.state = "RUNNING"
    untyped.runtime = SimpleNamespace(
        kv_snapshot=_no_reacquire if reacquire_raises else _observed_runtime
    )
    untyped._last_cancellation = None
    untyped._control_lock = threading.RLock()
    untyped._request_cleanup_receipts = {
        REQUEST_ID: {
            **_cleanup_subject(),
            "runtime_clean": True,
            "transport_clean": True,
            "cancellation_worker_complete": True,
            "complete": True,
        }
    }
    # The co-committed observation projection, as the sealer writes it.
    untyped._request_cleanup_receipt_counters = {
        REQUEST_ID: {
            "remote_frames_sent": 25,
            "remote_frames_received": 24,
            "router_frames_dispatched": 24,
            "duplicate_frames": 0,
        }
    }
    if runtime_projection is not None:
        untyped._request_cleanup_receipt_runtime = {REQUEST_ID: runtime_projection}
    untyped.transport = SimpleNamespace(
        counter_snapshot=_no_reacquire,
        evidence=lambda: (_ for _ in ()).throw(
            AssertionError("lean receipt queried full transport evidence")
        ),
    )
    untyped._signed_result = lambda event, details: {"event": event, "details": details}
    return service


def test_lean_cleanup_snapshot_projects_the_observed_runtime_counters() -> None:
    """Per-placement work must survive into the post-request observation."""

    observed = {
        "backend": "numpy",
        "active_state_count": 0,
        "applied_operation_count": 22,
        "prefill_operation_count": 9,
        "decode_operation_count": 13,
        "observed_work_unit_count": 154,
        "placement_counters": {
            "placement-001": {
                "active_state_count": 0,
                "prefill_operation_count": 9,
                "decode_operation_count": 13,
            }
        },
    }
    service = _sealed_service(runtime_projection=observed, reacquire_raises=True)

    snapshot = service._snapshot(
        {"cleanup_subject": _cleanup_subject(), "receipt_only": True}
    )
    details = snapshot["details"]

    # The transport half of the contract is unchanged (it is what round 59 did observe).
    assert details["request_cleanup"]["complete"] is True
    assert details["transport_counters"] == {
        "remote_frames_sent": 25,
        "remote_frames_received": 24,
        "router_frames_dispatched": 24,
        "duplicate_frames": 0,
    }

    # ...and the runtime half must be there, or no per-placement work can ever be read.
    runtime = details.get("runtime")
    assert runtime is not None, (
        "the lean cleanup snapshot dropped the runtime observation: the route then reads "
        "applied_operation_count = 0 for this peer and C5 cannot be evidenced"
    )
    assert runtime["applied_operation_count"] == 22
    assert (
        runtime["placement_counters"]["placement-001"]["decode_operation_count"] == 13
    )


def test_lean_cleanup_snapshot_without_a_runtime_projection_stays_lean() -> None:
    """A receipt sealed without runtime observation must not fabricate one."""

    service = _sealed_service(runtime_projection=None, reacquire_raises=True)

    details = service._snapshot(
        {"cleanup_subject": _cleanup_subject(), "receipt_only": True}
    )["details"]

    assert details["request_cleanup"]["complete"] is True
    assert "runtime" not in details
