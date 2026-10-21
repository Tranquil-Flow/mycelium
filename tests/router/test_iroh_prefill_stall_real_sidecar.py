# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D against the real native sidecar, without a network.

Two real ``mycelium-iroh-sidecar`` processes on loopback (``--local-only``),
two real ``IrohTransport`` adapters.  After chunk 0 crosses, the *receiving*
sidecar is frozen with SIGSTOP: the sender's QUIC stream then gets no
acknowledgement at all, which is the observable shape of a relay that stops
carrying bytes.  The receiver is resumed only after the sender has detected
the stall and resent.

This proves the recovery against the real sidecar contract: the exact-message
``cancel`` of the stuck outbound item, a fresh local data session, and a
resend under the same message id that the real stack applies exactly once.

What it does not prove: a public n0 relay, or that a fresh stream on a
degraded relay connection gets through sooner than the stuck one.  That is
the lead's hardware verification.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import tempfile
import threading
import time
from typing import Any

import pytest

import mycelium_router.transports.iroh as iroh_module
from mycelium_router.contracts import HopHeader
from mycelium_router.transports.iroh import IrohTransport, PeerBinding
from mycelium_router.wire import encode_frame
from test_iroh_sidecar_cross_language import BINARY, RunningSidecar


pytestmark = pytest.mark.skipif(
    not BINARY.is_file(),
    reason="native sidecar binary not built (native/iroh_transport/target/debug)",
)


def _policy(**overrides: Any) -> Any:
    values = dict(
        stall_floor_seconds=0.8,
        stall_ceiling_seconds=1.0,
        stall_rtt_multiplier=8.0,
        stall_transfer_multiplier=4.0,
        total_floor_seconds=1.0,
        total_command_fraction=0.8,
        total_transfer_multiplier=3.0,
        total_rtt_per_chunk_multiplier=4.0,
        unmeasured_goodput_bytes_per_second=64.0 * 1024 * 1024,
        unmeasured_rtt_seconds=0.001,
        max_resends=6,
        ordering_margin_seconds=0.2,
        watchdog_rtt_multiplier=4.0,
    )
    values.update(overrides)
    policy_type = getattr(iroh_module, "PrefillStallPolicy", None)
    if policy_type is None:
        from types import SimpleNamespace

        return SimpleNamespace(**values)
    return policy_type(**values)


class _Probe:
    def __init__(self) -> None:
        self.hops: list[HopHeader] = []
        self.lock = threading.Lock()
        self.received = threading.Condition(self.lock)

    def receive_hop(self, header, _payload, *, source_node_id=None):
        del source_node_id
        with self.received:
            self.hops.append(header)
            self.received.notify_all()
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
    *,
    delivery_timeout_seconds: float,
) -> IrohTransport:
    return IrohTransport(
        node_id=node_id,
        socket_path=sidecar.socket_path,
        bootstrap_secret=sidecar.secret,
        peer=_binding(peer_node_id, peer),
        expected_endpoint_id=sidecar.ready["endpoint_id"],
        delivery_timeout_seconds=delivery_timeout_seconds,
        poll_interval_seconds=0.02,
    )


def _chunk(index: int) -> bytes:
    header = HopHeader(
        request_id="request-relay-stall",
        path_id="path-relay-stall",
        path_attempt=0,
        phase="PREFILL_CHUNK",
        token_index=index,
        hop_index=1,
        source_placement_id="placement-0",
        destination_placement_id="placement-1",
        topology_version=1,
        idempotency_key=f"request-relay-stall:chunk:{index}",
        prefill_chunk_token_count=8,
    )
    # One startup-prefill chunk of Qwen2.5-0.5B activations: 8 x 896 x 4 B.
    return encode_frame(header, bytes(range(256)) * 112)


@pytest.fixture
def sidecars():
    root = Path(tempfile.mkdtemp(prefix="mycelium-relay-stall-", dir="/tmp"))
    first = RunningSidecar(root / "a", bytes(range(32)))
    second = RunningSidecar(root / "b", bytes(range(32, 64)))
    try:
        yield first, second
    finally:
        for sidecar in (first, second):
            try:
                os.kill(sidecar.process.pid, signal.SIGCONT)
            except OSError:
                pass
            sidecar.stop()
        shutil.rmtree(root, ignore_errors=True)


def test_frozen_receiver_mid_prefill_recovers_by_resend_and_applies_each_chunk_once(
    sidecars, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy()
    monkeypatch.setattr(
        iroh_module, "DEFAULT_PREFILL_STALL_POLICY", policy, raising=False
    )
    sender_sidecar, receiver_sidecar = sidecars
    probe = _Probe()
    sender = _adapter(
        "node-a", sender_sidecar, "node-b", receiver_sidecar,
        delivery_timeout_seconds=30.0,
    )
    receiver = _adapter(
        "node-b", receiver_sidecar, "node-a", sender_sidecar,
        delivery_timeout_seconds=60.0,
    )
    sender.bind_router(probe)
    receiver.bind_router(probe)
    sender.start()
    receiver.start()
    resumed = threading.Event()
    try:
        sender.send_router_frame(_chunk(0), destination_node_id="node-b")
        with probe.received:
            assert probe.received.wait_for(lambda: len(probe.hops) == 1, 10)

        os.kill(receiver_sidecar.process.pid, signal.SIGSTOP)
        snapshot = getattr(sender, "stall_recovery_snapshot", None)

        def resume_after_two_resends() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if not callable(snapshot):
                    # Base tree: nothing to observe; resume after a while.
                    time.sleep(3.0)
                    break
                if snapshot()["stall_resends"] >= 2:
                    break
                time.sleep(0.02)
            os.kill(receiver_sidecar.process.pid, signal.SIGCONT)
            resumed.set()

        resumer = threading.Thread(target=resume_after_two_resends, daemon=True)
        resumer.start()
        started = time.monotonic()
        receipt = sender.send_router_frame(_chunk(1), destination_node_id="node-b")
        elapsed = time.monotonic() - started
        resumer.join(5)

        assert resumed.is_set()
        assert receipt.message_id
        assert callable(snapshot)
        recovery = snapshot()
        assert recovery["stall_resends"] >= 2
        assert recovery["stall_failures"] == 0
        # Resumed after two stall windows; well inside the 30 s frame budget.
        assert 2 * policy.stall_floor_seconds <= elapsed < 15.0
        with probe.received:
            assert probe.received.wait_for(lambda: len(probe.hops) >= 2, 10)
        time.sleep(0.5)
        assert [header.token_index for header in probe.hops] == [0, 1]
    finally:
        if not resumed.is_set():
            os.kill(receiver_sidecar.process.pid, signal.SIGCONT)
        sender.close()
        receiver.close()
