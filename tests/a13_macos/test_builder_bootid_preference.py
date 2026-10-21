# SPDX-License-Identifier: AGPL-3.0-only
"""The builder binds a managed peer's identity from the LIVE hello, and refuses placeholders.

WHY THIS FILE EXISTS
    Round p80r1 (2026-09-20) refused its only route with node_hello_identity_invalid /
    kind='boot_id_mismatch' (physical_inference_qualification.py:1720). topology.json carries NO boot_id
    at all, so the live hello observation was the builder's only measured source -- and the substitution
    seam must therefore PREFER that observation, falling back to the topology's own recorded identity
    only where no observation exists for that node.

    The fallback must never become an acceptance rule: ``_pending_peers`` in
    mycelium_a13_macos/compiler_inputs.py writes placeholder identities ("pending-host-a" /
    "pending-boot-a") precisely so that the builder replaces them, and its docstring requires that they
    are never mistaken for measured run-scoped identities. A node with neither a live observation nor a
    measured topology identity is still refused.

    Every REFUSAL is unchanged and pinned here: a node whose live hello presents a different node_id, a
    different host_id or a previous/moved boot_id is still refused by the identity check.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

import pytest

from scripts import build_qwen_live_route as builder
from test_managed_builder import DEPLOYMENT_ID, RUN_ID, _managed_node

_HOST_MATERIAL = hashlib.sha256(b"p80r1-host-material").hexdigest()[:32]
_BOOT_MATERIAL = hashlib.sha256(b"p80r1-boot-material").hexdigest()[:32]
_PLACEHOLDER_PEER = {"host_id": "pending-host-b", "boot_id": "pending-boot-b"}
_FIXED_NOW = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def fixed_builder_clock(monkeypatch):
    """The builder's admission clock is pinned to the run id's own instant (test_managed_builder's seam)."""
    monkeypatch.setattr(builder.time, "time", lambda: _FIXED_NOW)


def _measured(material: bytes) -> str:
    return hashlib.sha256(material).hexdigest()[:32]


def _topology_identity(material: bytes) -> dict:
    return {"host_id": "host-" + _measured(b"host:" + material),
            "boot_id": "boot-" + _measured(b"boot:" + material)}


def _observations_document(node_ids: tuple[str, ...]) -> dict:
    """A fresh, shape-valid v1 document with distinct identities per node."""
    return {
        "protocol": "mycelium.managed_hello_observations.v1",
        "deployment_id": DEPLOYMENT_ID,
        "run_id": RUN_ID,
        "captured_at_unix_ms": int(_FIXED_NOW * 1_000),
        "observations": [
            {"node_id": node_id,
             "host_id": "host-" + _measured(_HOST_MATERIAL.encode() + b":" + node_id.encode()),
             "boot_id": "boot-" + _measured(_BOOT_MATERIAL.encode() + b":" + node_id.encode())}
            for node_id in node_ids
        ],
    }


def _admit(node_ids: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    run_id, admitted = builder._managed_hello_observations(
        _observations_document(node_ids), node_ids, deployment_id=DEPLOYMENT_ID
    )
    assert run_id == RUN_ID
    return admitted


def test_the_live_hello_observation_is_preferred_over_the_topology_identity() -> None:
    """Preference order at the substitution site: the live hello wins over the recorded topology."""
    stale = _topology_identity(b"stale-topology-value")
    peers = [{**_managed_node(0), **stale}, _managed_node(1)]
    admitted = _admit(("node-0", "node-1"))

    bound = builder.refresh_peer_identities(peers, RUN_ID, managed_observations=admitted)

    assert bound[0]["boot_id"] == admitted["node-0"][1] != stale["boot_id"]
    assert bound[0]["host_id"] == admitted["node-0"][0] != stale["host_id"]
    assert bound[1]["boot_id"] == admitted["node-1"][1]


def test_a_node_without_an_observation_falls_back_to_its_topology_identity() -> None:
    """Where NO observation exists for a node, the topology's recorded identity is the only source left.

    RED while a partial observation map is refused outright: the round could then never build a plan for
    a peer the live hello did not cover, even though the topology recorded a measured identity for it.
    """
    recorded = _topology_identity(b"topology-recorded-node-1")
    peers = [_managed_node(0), {**_managed_node(1), **recorded}]
    admitted = _admit(("node-0",))

    bound = builder.refresh_peer_identities(peers, RUN_ID, managed_observations=admitted)

    assert bound[0]["host_id"] == admitted["node-0"][0]
    assert bound[0]["boot_id"] == admitted["node-0"][1]
    assert bound[1]["host_id"] == recorded["host_id"], "the fallback did not keep the topology value"
    assert bound[1]["boot_id"] == recorded["boot_id"], "the fallback did not keep the topology value"


def test_a_placeholder_identity_is_never_accepted_as_a_measured_identity() -> None:
    """The fallback must not become an acceptance rule for unmeasured values.

    ``_pending_peers`` writes "pending-host-a"/"pending-boot-a" as placeholders with no process
    authority; a peer carrying one must still be refused, exactly like a peer carrying no identity at
    all, and whether or not an observation document was supplied for the build.
    """
    admitted = _admit(("node-0",))
    placeholder = [_managed_node(0), {**_managed_node(1), **_PLACEHOLDER_PEER}]
    with pytest.raises(RuntimeError, match="managed_peer_observations_missing"):
        builder.refresh_peer_identities(placeholder, RUN_ID, managed_observations=admitted)

    absent = [_managed_node(0), _managed_node(1)]
    with pytest.raises(RuntimeError, match="managed_peer_observations_missing"):
        builder.refresh_peer_identities(absent, RUN_ID, managed_observations=admitted)
    with pytest.raises(RuntimeError, match="managed_peer_observations_missing"):
        builder.refresh_peer_identities(placeholder, RUN_ID, managed_observations=None)


def test_qualification_accepts_the_bound_hello_and_refuses_every_moved_identity() -> None:
    """The chain the round actually runs: admitted hello -> bound peer -> qualification verdict.

    ACCEPTED: a peer bound from the live observation, handed the SAME live hello report.
    REFUSED, unchanged: a previous/moved boot_id (the p80r1 shape), another node_id, another host_id.
    """
    from pathlib import Path

    from physical_inference_qualification import ControllerError, PeerIdentity, QualificationController

    admitted = _admit(("node-0", "node-1"))
    bound = builder.refresh_peer_identities(
        [_managed_node(0), _managed_node(1)], RUN_ID, managed_observations=admitted
    )
    verifier = QualificationController(
        mode="dry-run",
        peers=tuple(
            PeerIdentity(node_id=peer["node_id"], ssh_target=None, host_id=peer["host_id"],
                         boot_id=peer["boot_id"], staging_root=peer["staging_root"],
                         process_transport="managed_https")
            for peer in bound
        ),
        source_root=Path(__file__).resolve().parents[2],
        transfer_manifest={},
        membership_snapshot={},
        now=time.time(),
    )
    for peer in verifier.peers:
        live = {
            "protocol": "mycelium.physical_node_control.v1",
            "run_id": RUN_ID,
            "deployment_id": DEPLOYMENT_ID,
            "node_id": peer.node_id,
            "host_id": peer.host_id,
            "process_id": 42,
            "endpoint_id": None,
            "peer_generation": 0,
            "state": "NEW",
            "route_ready": False,
            "boot_id": peer.boot_id,
        }
        accepted = verifier._hello_identity({"ok": True, "result": live}, peer=peer, run_id=RUN_ID,
                                            deployment_id=DEPLOYMENT_ID)
        assert accepted["boot_id"] == peer.boot_id
        assert accepted["host_id"] == peer.host_id

        moved = dict(live, boot_id="boot-" + "0" * 32)  # the p80r1 shape: the node's identity moved
        with pytest.raises(ControllerError, match="node_hello_identity_invalid") as caught:
            verifier._hello_identity({"ok": True, "result": moved}, peer=peer, run_id=RUN_ID,
                                     deployment_id=DEPLOYMENT_ID)
        assert "boot_id_mismatch" in (caught.value.diagnostic or ""), caught.value.diagnostic

        for field, value in (("node_id", "node-elsewhere"), ("host_id", "host-" + "1" * 32)):
            with pytest.raises(ControllerError, match="node_hello_identity_invalid"):
                verifier._hello_identity({"ok": True, "result": {**live, field: value}}, peer=peer,
                                         run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
