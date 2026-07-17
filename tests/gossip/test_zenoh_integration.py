from __future__ import annotations

import multiprocessing as mp
import os
import signal
import socket
import time
from typing import Callable

import pytest

pytest.importorskip("zenoh")

from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.transport import LivenessKind
from mycelium_gossip.zenoh_transport import ZenohTransport, ZenohTransportConfig
from tests.gossip.helpers import make_record


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(predicate: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def _listener_config(port: int) -> ZenohTransportConfig:
    return ZenohTransportConfig(
        listen_endpoints=(f"tcp/127.0.0.1:{port}",),
        multicast_enabled=False,
        lease_ms=2_000,
        keep_alive=4,
        query_timeout_seconds=3.0,
    )


def _connector_config(port: int) -> ZenohTransportConfig:
    return ZenohTransportConfig(
        listen_endpoints=("tcp/127.0.0.1:0",),
        connect_endpoints=(f"tcp/127.0.0.1:{port}",),
        multicast_enabled=False,
        lease_ms=2_000,
        keep_alive=4,
        query_timeout_seconds=3.0,
    )


def test_real_zenoh_pub_query_liveness_and_clean_delete() -> None:
    port = _free_port()
    receiver = ZenohTransport("swarm-a", "node-a", config=_listener_config(port))
    sender = ZenohTransport("swarm-a", "node-b", config=_connector_config(port))
    records = []
    liveness = []
    receiver.start()
    unsubscribe_records = receiver.subscribe_records(records.append)
    unsubscribe_liveness = receiver.subscribe_liveness(liveness.append, history=True)
    sender.start()
    try:
        sender.declare_liveness("swarm-a", "node-b", 1, "boot-node-b-1")
        _wait_until(lambda: any(event.kind is LivenessKind.PUT for event in liveness))

        profile = make_record(RecordKind.PROFILE, node_id="node-b")
        sender.publish_record(profile)
        _wait_until(lambda: any(item.record.payload_hash == profile.payload_hash for item in records))

        replies = receiver.query_records("mycelium/swarm-a/**")
        assert [reply.record.payload_hash for reply in replies] == [profile.payload_hash]

        sender.stop()
        _wait_until(lambda: any(event.kind is LivenessKind.DELETE for event in liveness))
    finally:
        sender.stop()
        unsubscribe_liveness()
        unsubscribe_records()
        receiver.stop()


def test_three_real_services_converge_without_router_or_allocator() -> None:
    ports = [_free_port() for _ in range(3)]
    configs = [
        _listener_config(ports[0]),
        ZenohTransportConfig(
            listen_endpoints=(f"tcp/127.0.0.1:{ports[1]}",),
            connect_endpoints=(f"tcp/127.0.0.1:{ports[0]}",),
            multicast_enabled=False,
            lease_ms=2_000,
            keep_alive=4,
            query_timeout_seconds=3.0,
        ),
        ZenohTransportConfig(
            listen_endpoints=(f"tcp/127.0.0.1:{ports[2]}",),
            connect_endpoints=(
                f"tcp/127.0.0.1:{ports[0]}",
                f"tcp/127.0.0.1:{ports[1]}",
            ),
            multicast_enabled=False,
            lease_ms=2_000,
            keep_alive=4,
            query_timeout_seconds=3.0,
        ),
    ]
    transports = [
        ZenohTransport("swarm-a", f"node-{letter}", config=config)
        for letter, config in zip(("a", "b", "c"), configs)
    ]
    services = [
        GossipService(
            swarm_id="swarm-a",
            node_id=f"node-{letter}",
            incarnation=1,
            boot_id=f"boot-node-{letter}-1",
            transport=transport,
            registry=VersionedRecordStore("swarm-a"),
            worker_poll_seconds=0.01,
        )
        for letter, transport in zip(("a", "b", "c"), transports)
    ]
    for service in services:
        service.start(background=True)
    try:
        for service in services:
            node = service.node_id
            service.publish_record(make_record(RecordKind.PROFILE, node_id=node, ttl_ms=30_000))
            service.publish_record(make_record(RecordKind.STATUS, node_id=node, ttl_ms=30_000))
            service.publish_record(make_record(RecordKind.OFFERING, node_id=node, ttl_ms=30_000))

        def all_converged() -> bool:
            return all(
                len(service.registry.snapshot().records) == 9
                and len(service.peer_states_snapshot()) == 3
                for service in services
            )

        _wait_until(all_converged, timeout=10.0)
        for service in services:
            assert service.convergence_report(include_local=True).converged is True
    finally:
        for service in reversed(services):
            service.stop()


def _crashable_peer(port: int, ready: mp.synchronize.Event) -> None:
    transport = ZenohTransport("swarm-a", "node-b", config=_connector_config(port))
    transport.start()
    transport.declare_liveness("swarm-a", "node-b", 1, "boot-node-b-1")
    ready.set()
    while True:
        time.sleep(1.0)


def test_abrupt_process_death_emits_liveliness_delete() -> None:
    port = _free_port()
    receiver = ZenohTransport("swarm-a", "node-a", config=_listener_config(port))
    events = []
    receiver.start()
    unsubscribe = receiver.subscribe_liveness(events.append, history=True)
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_crashable_peer, args=(port, ready))
    process.start()
    try:
        assert ready.wait(8.0)
        _wait_until(lambda: any(event.kind is LivenessKind.PUT for event in events), timeout=8.0)
        os.kill(process.pid, signal.SIGKILL)
        process.join(5.0)
        assert process.exitcode == -signal.SIGKILL
        _wait_until(lambda: any(event.kind is LivenessKind.DELETE for event in events), timeout=8.0)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3.0)
        unsubscribe()
        receiver.stop()
