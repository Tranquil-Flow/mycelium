# SPDX-License-Identifier: AGPL-3.0-or-later
"""A governed completion refreshes live-status peers[] after sealed cleanup.

Governed request cleanup is receipt-only: it proves request-scoped teardown
without replacing the full per-node snapshot that ``public_status`` projects.
Before this refresh, ``peers[]`` therefore kept its startup counters for the
life of the route (live: applied 12 / prefill 9 / decode 3 after real
inference), so every capture delta was zero.

These tests drive ``PhysicalLiveRoute.infer`` on the governed path with a
scripted two-node local session (never physical/browser qualification).  The
real completion cleanup runs (``cancel_request`` fanout, receipt-only
``_wait_for_cancellation_cleanup``, receipt sealing); only signature
verification, qualification digest and the manifest parser are doubles.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

import mycelium_live.route as route_module

DIGEST = "sha256:" + "a" * 64
REQUEST_ID = "request-refresh"
NODES = ("node-a", "node-b")
PLACEMENTS = {"node-a": "stage-000-primary", "node-b": "stage-001-primary"}
PROMPT = (11, 12, 13)
MAX_NEW_TOKENS = 3


class ScriptedNode:
    """One node's command boundary with a runtime-true counter triple.

    Every executed operation increments ``applied`` and exactly one of
    ``prefill`` / ``decode``, like the MLX and numpy runtimes.
    """

    returncode = None

    def __init__(self, node_id: str, log: list[tuple[str, str]], *, entry: bool):
        self.node_id = node_id
        self.log = log
        self.entry = entry
        self.applied = 12
        self.prefill = 9
        self.decode = 3
        self.frames_sent = 17
        self.frames_received = 19
        self.output: list[int] = []
        self.start_status = "DECODING"
        self.snapshot_error: BaseException | None = None

    def _operate(self, *, phase: str) -> None:
        self.applied += 1
        if phase == "prefill":
            self.prefill += 1
        else:
            self.decode += 1
        self.frames_sent += 1
        self.frames_received += 1

    def _details(self) -> dict[str, Any]:
        return {
            "runtime": {
                "mode": "stage_local_kv",
                "backend": "numpy",
                "architecture": "qwen2",
                "applied_operation_count": self.applied,
                "prefill_operation_count": self.prefill,
                "decode_operation_count": self.decode,
                "placement_counters": {
                    PLACEMENTS[self.node_id]: {
                        "prefill_operation_count": self.prefill,
                        "decode_operation_count": self.decode,
                        "active_state_count": 0,
                        "active_kv_bytes": 0,
                    }
                },
            },
            "transport_counters": {
                "remote_frames_sent": self.frames_sent,
                "remote_frames_received": self.frames_received,
            },
        }

    def send(self, *, command_id, command, payload):
        self.log.append((self.node_id, command))
        if command == "bind_request_control":
            return {"event": "request_control_bound", "details": {}}
        if command == "infer_start":
            assert self.entry
            for node in self.peers:
                node._operate(phase="prefill")
            if self.start_status == "DECODING":
                self.output = [101]
            return {
                "event": "inference_started",
                "details": {
                    "status": self.start_status,
                    "output": {"token_ids": list(self.output)},
                },
            }
        if command == "infer_decode":
            assert self.entry
            for node in self.peers:
                node._operate(phase="decode")
            self.output.append(101 + len(self.output))
            status = "COMPLETED" if len(self.output) >= MAX_NEW_TOKENS else "DECODING"
            return {
                "event": "inference_decoded",
                "details": {
                    "status": status,
                    "output": {"token_ids": list(self.output)},
                },
            }
        if command == "snapshot":
            assert payload == {}, "the refresh is a full snapshot, not a receipt"
            if self.snapshot_error is not None:
                raise self.snapshot_error
            return {"event": "snapshot", "details": self._details()}
        raise AssertionError(f"unexpected command {command}")

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        self.log.append((self.node_id, command))
        assert command == "infer_cancel_wait"
        assert deadline_monotonic_s > time.monotonic()
        return {
            "event": "inference_cancelled",
            "details": {
                "request_cleanup": {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key != "deadline_budget_ms"
                    },
                    "runtime_clean": True,
                    "transport_clean": True,
                    "complete": True,
                },
            },
        }

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        return False


def _graph() -> Any:
    return SimpleNamespace(
        deployment_id="deployment-refresh",
        deployment_epoch=4,
        topology_version=8,
        model_id="org/model",
        stages=tuple(
            SimpleNamespace(
                stage_id=f"stage-00{index}",
                layer_range=SimpleNamespace(
                    start_layer=index, end_layer_exclusive=index + 1
                ),
                component_roles=(),
                placements=(
                    SimpleNamespace(
                        placement_id=PLACEMENTS[node_id],
                        node_id=node_id,
                        runtime_backend="numpy",
                    ),
                ),
            )
            for index, node_id in enumerate(NODES)
        ),
    )


def _startup_snapshot(node: ScriptedNode) -> dict[str, Any]:
    return {"event": "snapshot", "details": node._details()}


@pytest.fixture
def governed(monkeypatch: pytest.MonkeyPatch):
    import mycelium_request_gateway.contracts as gateway_contracts
    import mycelium_router.serialization as serialization
    import mycelium_router.validation as validation

    graph = _graph()
    monkeypatch.setattr(route_module, "execution_graph_from_document", lambda _: graph)
    monkeypatch.setattr(gateway_contracts, "qualification_digest", lambda _: DIGEST)
    route = route_module.PhysicalLiveRoute(
        controller=SimpleNamespace(
            peers=tuple(SimpleNamespace(node_id=node_id) for node_id in NODES)
        ),
        endpoints={},
        run_plan={
            "run_id": "local-fixture",
            "deployment_id": graph.deployment_id,
            "request": {},
            "nodes": [
                {"node_id": node_id, "configure": {"graph": {}}} for node_id in NODES
            ],
        },
    )
    route._open = True
    route._graph = graph
    route._deployment_qualification = SimpleNamespace(route_ready=True)
    log: list[tuple[str, str]] = []
    nodes = {
        node_id: ScriptedNode(node_id, log, entry=node_id == "node-a")
        for node_id in NODES
    }
    for node in nodes.values():
        node.peers = tuple(nodes.values())
    route._sessions = dict(nodes)
    # Startup live_attestation left one full snapshot per node.
    route._last_snapshots = {
        node_id: _startup_snapshot(node) for node_id, node in nodes.items()
    }
    manifest = {"fixture": "local-only", "request_id": REQUEST_ID}
    manifest_digest = "sha256:" + hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    parsed = SimpleNamespace(
        ordered_hops=tuple(
            SimpleNamespace(placement_id=PLACEMENTS[node_id]) for node_id in NODES
        ),
        request_id=REQUEST_ID,
        path_id="path-refresh",
        path_attempt=1,
    )
    monkeypatch.setattr(serialization, "path_manifest_from_dict", lambda _: parsed)
    monkeypatch.setattr(validation, "validate_manifest", lambda *_: None)
    monkeypatch.setattr(route, "_reject_if_affected_peer_quarantined", lambda _: None)
    monkeypatch.setattr(
        route, "_verify_observation", lambda _node_id, response, **_kwargs: response
    )
    sealed: list[dict[str, Any]] = []
    real_seal = route._seal_and_publish_request_cleanup_receipt

    def seal(**kwargs):
        # The refresh must not run inside cleanup: at seal time peers[] still
        # shows the startup projection and no full snapshot was issued.
        assert ("node-a", "snapshot") not in log
        assert ("node-b", "snapshot") not in log
        receipt = real_seal(**kwargs)
        # The cancellation fanout may seal the same receipt from its own
        # thread; the request thread's inline seal is the ordering boundary.
        if threading.current_thread() is threading.main_thread():
            log.append(("route", "cleanup_receipt_sealed"))
        sealed.append(dict(receipt) if receipt is not None else {})
        return receipt

    monkeypatch.setattr(route, "_seal_and_publish_request_cleanup_receipt", seal)

    def run(**overrides: Any):
        route_identity = {
            "request_id": REQUEST_ID,
            "request_attempt": 1,
            "path_id": "path-refresh",
            "path_attempt": 1,
            "path_manifest_digest": manifest_digest,
            "deployment_id": graph.deployment_id,
            "deployment_epoch": graph.deployment_epoch,
            "topology_generation": graph.topology_version,
        }
        command_identity = {
            **{
                key: value
                for key, value in route_identity.items()
                if key != "path_manifest_digest"
            },
            "path_digest": manifest_digest,
            "qualification_digest": DIGEST,
            "command_id": "owner-command-refresh",
            "publisher_generation": 1,
            "absolute_deadline_ms": int(time.monotonic() * 1_000) + 30_000,
            "cancellation_generation": 0,
        }
        emitted: list[tuple[int, int]] = []
        result = route.infer(
            PROMPT,
            max_new_tokens=MAX_NEW_TOKENS,
            request_id=REQUEST_ID,
            sink=SimpleNamespace(emit=lambda index, token: emitted.append((index, token))),
            route_identity=route_identity,
            locked_path_manifest=manifest,
            command_identity=command_identity,
            authorize_cleanup=lambda deadline: deadline,
            publish_cleanup_receipt=lambda _receipt: None,
            **overrides,
        )
        return result, emitted

    state = SimpleNamespace(route=route, nodes=nodes, log=log, sealed=sealed, run=run)
    try:
        yield state
    finally:
        route._cleanup_reconciliation_stop.set()
        route._cleanup_reconciliation_executor.shutdown(wait=True, cancel_futures=True)


def _peers(route) -> dict[str, dict[str, Any]]:
    return {peer["node_id"]: peer for peer in route.public_status()["peers"]}


def _triple(peer: dict[str, Any]) -> tuple[int, int, int]:
    return (
        peer["applied_operation_count"],
        peer["prefill_operation_count"],
        peer["decode_operation_count"],
    )


def test_governed_completion_refreshes_peers_after_sealed_cleanup(governed) -> None:
    before = {node_id: _triple(peer) for node_id, peer in _peers(governed.route).items()}
    assert before == {"node-a": (12, 9, 3), "node-b": (12, 9, 3)}

    result, emitted = governed.run()

    assert result.token_ids == (101, 102, 103)
    assert [token for _index, token in emitted] == [101, 102, 103]
    assert governed.sealed and all(
        receipt == governed.sealed[0] for receipt in governed.sealed
    )
    # Sealing is idempotent (cleanup may seal before the request's own seal
    # call); the last seal on the request thread is the ordering boundary.
    seal_at = max(
        position
        for position, entry in enumerate(governed.log)
        if entry == ("route", "cleanup_receipt_sealed")
    )
    # Exactly one full snapshot per participant, strictly after the sealed
    # receipt (the receipt-only cleanup issued none of its own).
    snapshots = [
        (position, entry)
        for position, entry in enumerate(governed.log)
        if entry[1] == "snapshot"
    ]
    assert [entry for _position, entry in snapshots] == [
        ("node-a", "snapshot"),
        ("node-b", "snapshot"),
    ]
    assert all(position > seal_at for position, _entry in snapshots)

    after = _peers(governed.route)
    for node_id in NODES:
        applied, prefill, decode = _triple(after[node_id])
        # One prefill and two decode operations reached every participant.
        assert (applied, prefill, decode) == (15, 10, 5)
        assert applied == prefill + decode
        assert decode - before[node_id][2] > 0
        assert (after[node_id]["frames_sent"], after[node_id]["frames_received"]) == (
            20,
            22,
        )
    recent = governed.route._recent_inferences[-1]
    assert {
        delta["node_id"]: delta["applied_operation_count"]
        for delta in recent["peer_counter_deltas"]
    } == {"node-a": 3, "node-b": 3}
    assert not [
        incident
        for incident in governed.route._incidents
        if incident["request_id"] == REQUEST_ID
    ]


def test_failed_refresh_keeps_the_request_completed_and_is_recorded(
    governed,
) -> None:
    before = governed.route.public_status()["peers"]
    # node-b answers the refresh with an error after node-a answered fully:
    # neither node's projection may move (no half-updated snapshot).
    governed.nodes["node-b"].snapshot_error = RuntimeError("node_response_timeout")

    result, _emitted = governed.run()

    assert result.token_ids == (101, 102, 103)
    assert governed.route.public_status()["peers"] == before
    assert ("route", "cleanup_receipt_sealed") in governed.log
    incidents = [
        incident
        for incident in governed.route._incidents
        if incident["request_id"] == REQUEST_ID
    ]
    assert [(item["state"], item["reason"]) for item in incidents] == [
        ("post_request_snapshot_refresh_failed", "node_response_timeout")
    ]
    assert REQUEST_ID in governed.route._request_outputs
