# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real local iroh adapter path: parent plus two native sidecars."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import pytest

from mycelium_router.contracts import (
    HopHeader,
    ManifestLocked,
    PathBuildState,
    PathHop,
    PathManifest,
    RouterConfig,
    TokenEvent,
)
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeRuntimePort,
    FakeTopologyProvider,
    InMemoryClientSink,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.transports.iroh import IrohTransport, PeerBinding
from mycelium_router.wire import encode_frame
from test_iroh_sidecar_cross_language import BINARY, CRATE, RunningSidecar
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


@pytest.fixture(scope="module", autouse=True)
def _offline_sidecar_binary() -> None:
    environment = dict(os.environ)
    environment["CARGO_NET_OFFLINE"] = "true"
    subprocess.run(
        ["cargo", "build", "--locked", "--bin", "mycelium-iroh-sidecar"],
        cwd=CRATE,
        env=environment,
        check=True,
    )
    assert BINARY.is_file()


@pytest.fixture
def local_sidecars():
    root = Path(tempfile.mkdtemp(prefix="mycelium-iroh-adapter-", dir="/tmp"))
    first = RunningSidecar(root / "first", bytes(range(32)))
    second = RunningSidecar(root / "second", bytes(range(32, 64)))
    try:
        yield first, second
    finally:
        first.stop()
        second.stop()
        shutil.rmtree(root, ignore_errors=True)


class _RouteProbe:
    def __init__(self) -> None:
        self.hops: list[tuple[HopHeader, bytes, str | None]] = []
        self.tokens: list[tuple[TokenEvent, str | None]] = []
        self.paths: list[tuple[PathManifest, str | None, str | None]] = []
        self.locks: list[tuple[ManifestLocked, str | None]] = []
        self.path_registered = threading.Event()
        self.hop_received = threading.Event()
        self.token_received = threading.Event()

    def register_path(
        self, request, manifest, graph, *, source_node_id=None, entry_node_id=None
    ):
        self.paths.append((manifest, source_node_id, entry_node_id))
        self.path_registered.set()
        return True

    def receive_manifest_locked(self, locked, *, source_node_id=None):
        self.locks.append((locked, source_node_id))
        return True

    def receive_hop(self, header, payload, *, source_node_id=None):
        self.hops.append((header, payload, source_node_id))
        self.hop_received.set()
        return True

    def receive_token_event(self, event, *, source_node_id=None):
        self.tokens.append((event, source_node_id))
        self.token_received.set()
        return True


def _binding(node_id: str, sidecar: RunningSidecar) -> PeerBinding:
    return PeerBinding(
        node_id=node_id,
        endpoint_id=sidecar.ready["endpoint_id"],
        endpoint_addr=sidecar.ready["endpoint_addr"],
        generation=1,
    )


def _adapter(
    node_id: str,
    sidecar: RunningSidecar,
    peer_node_id: str,
    peer: RunningSidecar,
) -> IrohTransport:
    return IrohTransport(
        node_id=node_id,
        socket_path=sidecar.socket_path,
        bootstrap_secret=sidecar.secret,
        peer=_binding(peer_node_id, peer),
        expected_endpoint_id=sidecar.ready["endpoint_id"],
        delivery_timeout_seconds=3.0,
        poll_interval_seconds=0.02,
    )


def _locked_route() -> ManifestLocked:
    graph = graph_fixture()
    request = replace(request_fixture(), request_id="request-iroh")
    hops = (
        PathHop(
            graph.stages[0].stage_id,
            graph.stages[0].placements[0].placement_id,
            "reservation-a",
            reservation_expires_at=30.0,
            reservation_epoch=graph.deployment_epoch,
        ),
        PathHop(
            graph.stages[1].stage_id,
            graph.stages[1].placements[0].placement_id,
            "reservation-b",
            reservation_expires_at=30.0,
            reservation_epoch=graph.deployment_epoch,
        ),
        PathHop(
            graph.stages[2].stage_id,
            graph.stages[2].placements[0].placement_id,
            "reservation-a-final",
            reservation_expires_at=30.0,
            reservation_epoch=graph.deployment_epoch,
        ),
    )
    manifest = PathManifest(
        path_id="path-iroh",
        path_attempt=0,
        request_id=request.request_id,
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        topology_version=graph.topology_version,
        manifest_digest=graph.manifest_digest,
        ordered_hops=hops,
        loopback_edge_id="loop-2a-0a",
    )
    build = PathBuildState(
        request=request,
        graph=graph,
        path_id=manifest.path_id,
        path_attempt=manifest.path_attempt,
        ordered_hops=hops,
    )
    return ManifestLocked(
        request_id=manifest.request_id,
        path_id=manifest.path_id,
        path_attempt=manifest.path_attempt,
        manifest=manifest,
        build=build,
    )


def test_three_process_prefill_decode_preserves_activation_and_token_digests(
    local_sidecars,
) -> None:
    first_sidecar, second_sidecar = local_sidecars
    first_probe = _RouteProbe()
    second_probe = _RouteProbe()
    first = _adapter("node-a", first_sidecar, "node-b", second_sidecar)
    second = _adapter("node-b", second_sidecar, "node-a", first_sidecar)
    first.bind_router(first_probe)
    second.bind_router(second_probe)
    first.start()
    second.start()

    locked = _locked_route()
    activation = bytes(range(64)) * 4
    header = HopHeader(
        request_id="request-iroh",
        path_id="path-iroh",
        path_attempt=0,
        phase="PREFILL",
        token_index=0,
        hop_index=1,
        source_placement_id="node-a-stage-000",
        destination_placement_id="node-b-stage-001",
        topology_version=locked.build.graph.topology_version,
        idempotency_key="request-iroh:prefill:0:1",
    )
    token = TokenEvent(
        request_id="request-iroh",
        path_id="path-iroh",
        path_attempt=0,
        token_index=0,
        token_id=31337,
        sampling_counter=1,
    )
    token_frame = encode_frame(token)

    try:
        first.send_manifest_locked(locked)
        assert second_probe.path_registered.wait(2)
        first.send_hop(header, activation)
        assert second_probe.hop_received.wait(2)
        second.send_token_event(token)
        assert first_probe.token_received.wait(2)

        received_header, received_activation, prefill_source = second_probe.hops[0]
        received_token, token_source = first_probe.tokens[0]
        assert received_header == header
        assert prefill_source == "node-a"
        assert token_source == "node-b"
        assert received_token == token
        assert hashlib.sha256(received_activation).hexdigest() == hashlib.sha256(
            activation
        ).hexdigest()
        assert hashlib.sha256(encode_frame(received_token)).hexdigest() == hashlib.sha256(
            token_frame
        ).hexdigest()

        process_ids = {os.getpid(), first_sidecar.process.pid, second_sidecar.process.pid}
        assert len(process_ids) == 3
        assert first.evidence().remote_frames_sent == 2
        assert second.evidence().remote_frames_sent == 1
        assert first.evidence().remote_frames_received == 1
        assert second.evidence().remote_frames_received == 2
        assert first.evidence().delivery_semantics == "remote_router_dispatch_ack"
        assert first.route_ready is False
        assert second.route_ready is False
    finally:
        first.close()
        second.close()


class _EvidenceRouter(Router):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.received_locks: list[tuple[ManifestLocked, str | None]] = []
        self.received_tokens: list[tuple[TokenEvent, str | None]] = []

    def receive_manifest_locked(self, locked, *, source_node_id=None):
        self.received_locks.append((locked, source_node_id))
        return super().receive_manifest_locked(
            locked, source_node_id=source_node_id
        )

    def receive_token_event(self, event, *, source_node_id=None):
        self.received_tokens.append((event, source_node_id))
        return super().receive_token_event(event, source_node_id=source_node_id)


def test_real_routers_run_prefill_and_decode_through_two_sidecars(local_sidecars) -> None:
    first_sidecar, second_sidecar = local_sidecars
    graph = graph_fixture()
    final_stage = graph.stages[-1]
    graph = replace(
        graph,
        stages=graph.stages[:-1]
        + (
            replace(
                final_stage,
                placements=tuple(
                    replace(placement, node_id="node-c")
                    for placement in final_stage.placements
                ),
            ),
        ),
    )
    first = _adapter("node-a", first_sidecar, "node-c", second_sidecar)
    second = _adapter("node-c", second_sidecar, "node-a", first_sidecar)
    capacity = FakeCapacityPort()
    clock = ManualClock()
    runtime_a = FakeRuntimePort()
    runtime_c = FakeRuntimePort()

    def make_router(node_id, runtime, transport):
        return _EvidenceRouter(
            node_id=node_id,
            topology=FakeTopologyProvider(graph),
            device_states=FakeDeviceStateProvider(
                state_table(slow_b_bandwidth=True)
            ),
            capacity=capacity,
            runtime=runtime,
            transport=transport,
            clock=clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(),
        )

    router_a = make_router("node-a", runtime_a, first)
    router_c = make_router("node-c", runtime_c, second)
    first.bind_router(router_a)
    second.bind_router(router_c)
    first.start()
    second.start()
    request = replace(request_fixture(), request_id="request-iroh-router")
    sink = InMemoryClientSink()

    try:
        request_id = router_a.start_distributed_prefill(
            request,
            sink,
            excluded_placements=frozenset({"node-b-stage-000"}),
        )
        assert request_id == request.request_id
        assert router_a.request_status(request_id) == "DECODING"
        record = router_a.get_request(request_id)
        placement_nodes = {
            placement.placement_id: placement.node_id
            for stage in graph.stages
            for placement in stage.placements
        }
        assert [
            placement_nodes[hop.placement_id]
            for hop in record.manifest.ordered_hops
        ] == ["node-a", "node-c", "node-c"]

        prefill_items = runtime_a.executed + runtime_c.executed
        assert len(prefill_items) == 3
        assert {item.phase for item in prefill_items} == {"PREFILL"}
        assert all(isinstance(item.payload, bytes) for item in prefill_items)
        activation_digests = {
            hashlib.sha256(item.payload).hexdigest()
            for item in prefill_items
            if isinstance(item.payload, bytes)
        }
        assert len(activation_digests) == 1
        assert first.evidence().remote_frames_sent >= 1
        assert second.evidence().remote_frames_sent >= 1

        assert router_a.decode_one_distributed(request_id) is True
        assert sink.token_indexes == [0]
        assert sink.token_ids == [101]
        decode_items = [
            item
            for item in runtime_a.executed + runtime_c.executed
            if item.phase == "DECODE"
        ]
        assert len(decode_items) == 3
        assert all(isinstance(item.payload, bytes) for item in decode_items)
        decode_digests = {
            hashlib.sha256(item.payload).hexdigest()
            for item in decode_items
            if isinstance(item.payload, bytes)
        }
        assert len(decode_digests) == 1

        assert len(router_a.received_tokens) == 1
        received_token, token_source = router_a.received_tokens[0]
        assert token_source == "node-c"
        expected_token = TokenEvent(
            request_id=request_id,
            path_id=record.manifest.path_id,
            path_attempt=record.manifest.path_attempt,
            token_index=0,
            token_id=101,
            sampling_counter=1,
        )
        assert hashlib.sha256(encode_frame(received_token)).digest() == hashlib.sha256(
            encode_frame(expected_token)
        ).digest()
        assert first.route_ready is False
        assert second.route_ready is False
    finally:
        first.close()
        second.close()
