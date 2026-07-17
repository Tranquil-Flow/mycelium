from __future__ import annotations

import multiprocessing as mp
import os
import queue
import random
import signal
import time
from typing import Any, Dict, List, Tuple

import pytest

pytest.importorskip("zenoh")

from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.zenoh_transport import ZenohTransport, ZenohTransportConfig
from tests.gossip.helpers import make_record


PEER_COUNT = 5


def _mesh_worker(index: int, multicast_address: str, commands: Any, events: Any) -> None:
    node_id = f"node-{index}"
    transport = ZenohTransport(
        "swarm-a",
        node_id,
        config=ZenohTransportConfig(
            listen_endpoints=("tcp/127.0.0.1:0",),
            multicast_enabled=True,
            multicast_address=multicast_address,
            multicast_interface="auto",
            lease_ms=2_000,
            keep_alive=4,
            query_timeout_seconds=2.0,
        ),
    )
    service = GossipService(
        swarm_id="swarm-a",
        node_id=node_id,
        incarnation=1,
        boot_id=f"boot-{node_id}-1",
        transport=transport,
        registry=VersionedRecordStore("swarm-a"),
        worker_poll_seconds=0.01,
        repair_interval_seconds=0.5,
    )
    try:
        service.start(background=True)
        for kind in (RecordKind.PROFILE, RecordKind.STATUS, RecordKind.OFFERING):
            service.publish_record(make_record(kind, node_id=node_id, ttl_ms=30_000))
        events.put(("ready", index, time.monotonic_ns()))
        while True:
            command = commands.get()
            if command[0] == "stop":
                break
            if command[0] == "snapshot":
                round_id = command[1]
                snapshot = service.registry.snapshot()
                peers = service.peer_states_snapshot()
                report = service.convergence_report(include_local=True)
                events.put(
                    (
                        "snapshot",
                        round_id,
                        index,
                        len(snapshot.records),
                        tuple((peer.node_id, peer.state.value) for peer in peers),
                        report.converged,
                        service.diagnostics.repair_runs,
                        service.diagnostics.repair_records,
                        transport.diagnostics.received_records,
                        time.monotonic_ns(),
                    )
                )
    except BaseException as exc:
        events.put(("error", index, type(exc).__name__, str(exc)))
        raise
    finally:
        service.stop()


def _collect_ready(events: Any, timeout: float) -> List[Tuple[Any, ...]]:
    deadline = time.monotonic() + timeout
    ready: Dict[int, Tuple[Any, ...]] = {}
    collected: List[Tuple[Any, ...]] = []
    while time.monotonic() < deadline and len(ready) < PEER_COUNT:
        try:
            event = events.get(timeout=min(0.2, deadline - time.monotonic()))
        except queue.Empty:
            continue
        collected.append(event)
        if event[0] == "error":
            raise AssertionError(event)
        if event[0] == "ready":
            ready[event[1]] = event
    assert len(ready) == PEER_COUNT, collected
    return collected


def _snapshot_round(
    commands: List[Any], events: Any, round_id: int, live_indexes: Tuple[int, ...], timeout: float = 5.0
) -> Dict[int, Tuple[Any, ...]]:
    for index in live_indexes:
        commands[index].put(("snapshot", round_id))
    deadline = time.monotonic() + timeout
    snapshots: Dict[int, Tuple[Any, ...]] = {}
    while time.monotonic() < deadline and len(snapshots) < len(live_indexes):
        try:
            event = events.get(timeout=min(0.2, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if event[0] == "error":
            raise AssertionError(event)
        if event[0] == "snapshot" and event[1] == round_id:
            snapshots[event[2]] = event
    assert len(snapshots) == len(live_indexes), snapshots
    return snapshots


def test_five_process_mesh_converges_repairs_and_detects_abrupt_loss() -> None:
    ctx = mp.get_context("spawn")
    events = ctx.Queue()
    commands = [ctx.Queue() for _ in range(PEER_COUNT)]
    multicast_address = f"224.0.0.225:{random.randint(18000, 24000)}"
    processes = [
        ctx.Process(target=_mesh_worker, args=(index, multicast_address, commands[index], events))
        for index in range(PEER_COUNT)
    ]
    started_ns = time.monotonic_ns()
    for process in processes:
        process.start()
    try:
        ready_events = _collect_ready(events, timeout=15.0)
        all_ready_ns = max(event[2] for event in ready_events if event[0] == "ready")

        converged: Dict[int, Tuple[Any, ...]] = {}
        for round_id in range(1, 31):
            converged = _snapshot_round(commands, events, round_id, tuple(range(PEER_COUNT)))
            if all(
                event[3] == PEER_COUNT * 3
                and len(event[4]) == PEER_COUNT
                and event[5] is True
                for event in converged.values()
            ):
                break
            time.sleep(0.2)
        else:
            raise AssertionError(converged)
        converged_ns = max(event[9] for event in converged.values())

        time.sleep(0.6)
        after_repair = _snapshot_round(commands, events, 100, tuple(range(PEER_COUNT)))
        assert all(event[6] > 0 for event in after_repair.values())
        assert all(event[3] == PEER_COUNT * 3 and event[5] is True for event in after_repair.values())

        killed_ns = time.monotonic_ns()
        os.kill(processes[-1].pid, signal.SIGKILL)
        processes[-1].join(5.0)
        assert processes[-1].exitcode == -signal.SIGKILL

        detected: Tuple[Any, ...] | None = None
        for round_id in range(101, 131):
            snapshot = _snapshot_round(commands, events, round_id, (0,))[0]
            node_four = dict(snapshot[4]).get("node-4")
            if node_four in {"suspect", "dead"}:
                detected = snapshot
                break
            time.sleep(0.1)
        assert detected is not None
        detected_ns = detected[9]

        print(
            {
                "peers": PEER_COUNT,
                "multicast_address": multicast_address,
                "startup_to_ready_ms": round((all_ready_ns - started_ns) / 1_000_000, 2),
                "ready_to_evidence_convergence_ms": round((converged_ns - all_ready_ns) / 1_000_000, 2),
                "kill_to_suspect_ms": round((detected_ns - killed_ns) / 1_000_000, 2),
                "repair_runs": [event[6] for event in after_repair.values()],
                "repair_records": [event[7] for event in after_repair.values()],
                "received_records": [event[8] for event in after_repair.values()],
            }
        )
    finally:
        for index, process in enumerate(processes):
            if process.is_alive():
                commands[index].put(("stop",))
        for process in processes:
            process.join(5.0)
            if process.is_alive():
                process.terminate()
                process.join(3.0)
