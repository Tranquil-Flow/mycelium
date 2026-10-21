# SPDX-License-Identifier: AGPL-3.0-only
"""Seeded, deterministic TCP fault proxy for the Mycelium core-pilot harness.

Promoted (PLAN 5.2 item 3, WP2) from the research spike at
``research/scratch/fault-proxy/faultproxy.py``, whose promotion was the explicit
recommendation of ``research/research-fault-proxy.md`` §8.  The relay mechanics,
the half-close rule, the string-keyed per-stream RNG and the trace-digest
replay property are the spike's, kept byte-for-byte in behaviour; everything
this module *adds* is marked with an ``EMULATION EXTENSION`` comment.

Why the spike and not ``toxiproxy`` (research note §8, §6b): the stdlib relay is
the only candidate that satisfies no-``sudo``, no external daemon **and** seeded
replay at once.  Measured against toxiproxy 2.12.0, the relay also preserves
half-close (``reply_after_FIN=b'REQ-0008EOF-ACK'``) where toxiproxy returns
``b''`` at baseline, and the released toxiproxy has no ``packet_loss`` toxic and
no bulk toxic delete.

Determinism rule (unchanged): each ``(connection, direction)`` stream is keyed by
``random.Random(f"{seed}:{conn_index}:{direction}")``.  Connection index is the
accept order, so a single-client run replays byte-for-byte and
:meth:`Stats.trace_digest` is identical across processes and across
``PYTHONHASHSEED`` values (the spike's first draft passed a tuple and raised
``TypeError``; string seeding is sha512-derived and stable).

Round-50 shaping
----------------

:data:`ROUND50_*` are the *measured* values the emulation shapes the path to, each
with its primary source.  They are deliberately not tunable knobs of this module:
:func:`round50_profile` composes them into a :class:`FaultConfig` (or a
:class:`Round50LegConfig` per leg) and cites the measurement, so a reader can
check the number rather than trust a default.

============================  =============  =========================================
symbol                        value          source
============================  =============  =========================================
``ROUND50_RECEIPT_POLL_MEAN_MS``        49.96  research-mailbox-longpoll.md §4.3 P3, n=200
``ROUND50_RECEIPT_POLL_P95_MS``         94.57  same, p95
``ROUND50_RECEIPT_POLL_MAX_MS``        100.22  same, max
``ROUND50_DEADLINE_GRID_MS``              50.0  ``physical_inference_node.py:74`` (0.05 s)
``ROUND50_RECEIPT_GRID_MS``              100.0  ``route.py:68`` (0.1 s)
``ROUND50_COMMAND_QUEUE_GRID_MS``         20.0  ``physical_inference_node.py:1741`` (0.02 s)
``ROUND50_RELAY_RTT_MS``                   1.355  research-iroh.md §[3] round1 rtt_ms
``ROUND50_BASELINE_RELAY_RTT_MS``          0.4  research-fault-proxy.md §6a A (mean 1.1 over 10, steady state 0.3-0.5)
============================  =============  =========================================

No round-50 **DoH** latency exists in the research corpus: the DoH loss was a
*quick-tunnel DNS/DoH* failure mode (PLAN D7: "rounds 46, 48 and 49 lost attempts
to quick-tunnel DNS/DoH"), not a measured RTT.  :data:`ROUND50_DOH_RTT_MS` is
therefore the loopback baseline of the origin leg and is flagged in
:data:`ROUND50_UNMEASURED` rather than dressed up as a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import random
import sys
import time
from typing import Any, Callable, Final, Sequence

__all__ = [
    "FaultConfig",
    "FaultProxyError",
    "FaultProxyHandle",
    "PartitionMode",
    "ROUND50_BASELINE_RELAY_RTT_MS",
    "ROUND50_COMMAND_QUEUE_GRID_MS",
    "ROUND50_DEADLINE_GRID_MS",
    "ROUND50_DOH_RTT_MS",
    "ROUND50_MEASURED",
    "ROUND50_RECEIPT_GRID_MS",
    "ROUND50_RECEIPT_POLL_MAX_MS",
    "ROUND50_RECEIPT_POLL_MEAN_MS",
    "ROUND50_RECEIPT_POLL_P95_MS",
    "ROUND50_RELAY_RTT_MS",
    "ROUND50_UNMEASURED",
    "Round50LegConfig",
    "Stats",
    "TRACE_ACTIONS",
    "start_proxy",
    "round50_profile",
]


class FaultProxyError(RuntimeError):
    """A fault-proxy configuration or liveness failure."""


# --------------------------------------------------------------------------- #
# round-50 measured shaping constants (see module docstring for sources)
# --------------------------------------------------------------------------- #

#: Measured added latency of the route's true interval poll on the cleanup-receipt
#: path (``route.py:2770``).  research-mailbox-longpoll.md §4.3 phase P3, n=200.
ROUND50_RECEIPT_POLL_MEAN_MS: Final[float] = 49.96
ROUND50_RECEIPT_POLL_P95_MS: Final[float] = 94.57
ROUND50_RECEIPT_POLL_MAX_MS: Final[float] = 100.22

#: The three quantisation grids the round-50 budget arithmetic is built from.
ROUND50_DEADLINE_GRID_MS: Final[float] = 50.0
ROUND50_RECEIPT_GRID_MS: Final[float] = 100.0
ROUND50_COMMAND_QUEUE_GRID_MS: Final[float] = 20.0

#: iroh loopback direct-path RTT measured in research-iroh.md §[3] round1/round2.
ROUND50_RELAY_RTT_MS: Final[float] = 1.355
#: Baseline RTT of the research relay itself, research-fault-proxy.md §6a A.
ROUND50_BASELINE_RELAY_RTT_MS: Final[float] = 0.4

#: The origin (``https://``) leg on loopback.  Flagged in
#: :data:`ROUND50_UNMEASURED`: the corpus records the DoH *failure mode*, never a
#: measured DoH RTT, so this leg is shaped from the loopback baseline and the gap
#: is reported rather than hidden.
ROUND50_DOH_RTT_MS: Final[float] = ROUND50_BASELINE_RELAY_RTT_MS

#: (symbol, value, unit, source) for every number above that was measured.
ROUND50_MEASURED: Final[tuple[tuple[str, float, str, str], ...]] = (
    (
        "receipt_poll_mean_ms",
        ROUND50_RECEIPT_POLL_MEAN_MS,
        "ms",
        "research-mailbox-longpoll.md 4.3 P3 (n=200)",
    ),
    (
        "receipt_poll_p95_ms",
        ROUND50_RECEIPT_POLL_P95_MS,
        "ms",
        "research-mailbox-longpoll.md 4.3 P3 (n=200)",
    ),
    (
        "receipt_poll_max_ms",
        ROUND50_RECEIPT_POLL_MAX_MS,
        "ms",
        "research-mailbox-longpoll.md 4.3 P3 (n=200)",
    ),
    (
        "relay_rtt_ms",
        ROUND50_RELAY_RTT_MS,
        "ms",
        "research-iroh.md 3 round1 selected ip path rtt_ms",
    ),
    (
        "baseline_relay_rtt_ms",
        ROUND50_BASELINE_RELAY_RTT_MS,
        "ms",
        "research-fault-proxy.md 6a scenario A",
    ),
)

#: Quantities the packet names ("DoH, relay, poll RTT") that the corpus does **not**
#: carry as a measurement.  Recorded so the gap is a finding, not a silent default.
ROUND50_UNMEASURED: Final[tuple[str, ...]] = (
    "doh_rtt_ms: the corpus records the quick-tunnel DNS/DoH *failure mode* "
    "(PLAN D7, rounds 46/48/49), never a measured DoH round trip; the origin leg "
    "is shaped from the loopback baseline instead",
)

#: Per-chunk actions a relay stream can take; also the trace-digest vocabulary.
TRACE_ACTIONS: Final[tuple[str, ...]] = (
    "delay",
    "loss-retransmit",
    "drop",
    "blackhole",
    "hold",
    "refused",
)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


#: How a partition is expressed on the wire.  ``blackhole`` keeps the socket and
#: silently swallows bytes (the peer times out); ``refuse`` stops accepting, so the
#: next ``connect`` fails with ``ConnectionRefusedError``.  The two are different
#: failures for ``mycelium_live/route.py`` and must never be asserted the same way
#: (research-fault-proxy.md §4.8).
PartitionMode = str  # "blackhole" | "refuse"
_PARTITION_MODES: Final[tuple[str, ...]] = ("blackhole", "refuse")


@dataclass
class FaultConfig:
    """One relay's deterministic shaping.  Every field is derived from ``seed``."""

    seed: int = 0
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss: float = 0.0
    loss_mode: str = "delay"  # "drop" | "delay"
    loss_delay_ms: float = 200.0
    #: Read granularity = delay/loss granularity.  ``1`` gives per-byte delay.
    chunk_bytes: int = 65536
    partition_at_chunk: int | None = None
    partition_for_ms: float = 0.0
    partition_sides: tuple[str, ...] = ("up", "down")
    partition_mode: PartitionMode = "blackhole"
    connect_delay_ms: float = 0.0
    linger_ms: float = 2000.0
    # -- EMULATION EXTENSION: round-50 knobs -------------------------------- #
    #: Per-direction latency override; ``None`` uses ``latency_ms``.
    up_latency_ms: float | None = None
    down_latency_ms: float | None = None
    #: Hold the ``down`` direction (the peer's receipt leg) by this many ms once
    #: its chunk index reaches ``hold_down_after_chunk``.  A *delay*, not a drop:
    #: the receipt still exists, it is simply observed after the route's window.
    #: This is the round-50 shape the negative case reproduces.
    hold_down_after_chunk: int | None = None
    hold_down_ms: float = 0.0
    #: Refusal partition, exact form: close the listener immediately after binding,
    #: so every subsequent ``connect`` fails with ``ConnectionRefusedError`` - the
    #: shape toxiproxy reaches with ``enabled=false`` (research-fault-proxy.md §6b
    #: T7: ``ConnectionRefusedError(61)``) and the one ``route.py`` must not confuse
    #: with a silent blackhole.
    refuse_listener: bool = False
    #: Refusal partition, degraded form: keep accepting but close the client at
    #: once for connection index ``>= refuse_after_connections``.  The peer sees
    #: ``ECONNRESET``, not ``ECONNREFUSED``; kept because it is what a listener
    #: that is *draining* looks like, and because the two must be asserted
    #: differently.
    refuse_after_connections: int | None = None
    #: Optional cap on accepted connections, for a bounded replay.
    max_connections: int | None = None

    def __post_init__(self) -> None:
        if self.loss_mode not in ("drop", "delay"):
            raise FaultProxyError(f"unknown_loss_mode:{self.loss_mode}")
        if self.partition_mode not in _PARTITION_MODES:
            raise FaultProxyError(f"unknown_partition_mode:{self.partition_mode}")
        if not 0.0 <= self.loss <= 1.0:
            raise FaultProxyError(f"loss_out_of_range:{self.loss}")
        if self.chunk_bytes < 1:
            raise FaultProxyError(f"chunk_bytes_must_be_positive:{self.chunk_bytes}")
        for direction in self.partition_sides:
            if direction not in ("up", "down"):
                raise FaultProxyError(f"unknown_partition_side:{direction}")

    def direction_latency_ms(self, direction: str) -> float:
        override = self.up_latency_ms if direction == "up" else self.down_latency_ms
        return self.latency_ms if override is None else float(override)

    def to_document(self) -> dict[str, Any]:
        return {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in vars(self).items()
        }


def round50_profile(
    seed: int,
    *,
    leg: str,
    jitter_ms: float = 0.0,
    chunk_bytes: int = 65536,
) -> FaultConfig:
    """A per-leg :class:`FaultConfig` shaped to the measured round-50 latencies.

    ``leg`` names which hop of the C4->C5 chain this relay fronts:

    ``"worker"``
        route -> worker command delivery and the receipt return.  The link delay is
        the iroh loopback direct-path RTT the route measures as
        ``details["transport"]["metrics"]["rtt_ms"]`` (``ROUND50_RELAY_RTT_MS``,
        ``route.py:96-103``), plus the node's own command-queue grid
        (``ROUND50_COMMAND_QUEUE_GRID_MS``, ``physical_inference_node.py:1741``
        ``poll_interval_seconds=0.02``) on the way in.

        The route's **receipt-observation grid** (``ROUND50_RECEIPT_GRID_MS``,
        ``route.py:68`` ``_CLEANUP_RECEIPT_POLL_SECONDS=0.1``) is deliberately *not*
        a link delay here: it is not on the wire.  It is a *poll cadence on the
        route side*, so the emulation reproduces it where it lives - in the driver's
        receipt wait - and measures the added latency there, against the research
        note's P3 sample (mean 49.96 ms / p95 94.57 ms / max 100.22 ms).  Modelling
        it as a per-chunk link delay would have put a route-side artefact on the
        worker leg and inflated every token hop.
    ``"origin"``
        the ``https://`` origin leg.  Shaped from the loopback baseline because no
        round-50 DoH RTT was ever measured (see :data:`ROUND50_UNMEASURED`).
    """

    if leg == "worker":
        up = ROUND50_RELAY_RTT_MS + ROUND50_COMMAND_QUEUE_GRID_MS
        down = ROUND50_RELAY_RTT_MS
    elif leg == "origin":
        up = ROUND50_DOH_RTT_MS
        down = ROUND50_DOH_RTT_MS
    else:
        raise FaultProxyError(f"unknown_leg:{leg}")
    return FaultConfig(
        seed=seed,
        latency_ms=0.0,
        jitter_ms=jitter_ms,
        up_latency_ms=up,
        down_latency_ms=down,
        chunk_bytes=chunk_bytes,
    )


@dataclass(frozen=True)
class Round50LegConfig:
    """A named leg of the emulated path, with its shaping and its provenance."""

    name: str
    leg: str
    config: FaultConfig
    basis: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "leg": self.leg,
            "basis": list(self.basis),
            "seed": self.config.seed,
            "up_latency_ms": self.config.up_latency_ms,
            "down_latency_ms": self.config.down_latency_ms,
            "jitter_ms": self.config.jitter_ms,
            "chunk_bytes": self.config.chunk_bytes,
            "hold_down_after_chunk": self.config.hold_down_after_chunk,
            "hold_down_ms": self.config.hold_down_ms,
            "loss": self.config.loss,
            "loss_mode": self.config.loss_mode,
            "partition_at_chunk": self.config.partition_at_chunk,
            "partition_for_ms": self.config.partition_for_ms,
            "partition_sides": list(self.config.partition_sides),
            "partition_mode": self.config.partition_mode,
            "refuse_after_connections": self.config.refuse_after_connections,
        }


def round50_legs(
    seed: int,
    *,
    receipt_hold_ms: float | None = None,
    jitter_ms: float = 0.0,
    chunk_bytes: int = 65536,
) -> tuple[Round50LegConfig, ...]:
    """The three relays of the emulated path, shaped to the round-50 numbers.

    ``receipt_hold_ms`` is the negative case's knob: when set, the
    ``worker-c`` leg holds its ``down`` (receipt) direction by that many ms, which
    is how the round-50 cleanup-receipt wedge is reproduced locally.
    """

    worker_basis = (
        f"up = relay_rtt {ROUND50_RELAY_RTT_MS} ms + command_queue_grid "
        f"{ROUND50_COMMAND_QUEUE_GRID_MS} ms",
        f"down = relay_rtt {ROUND50_RELAY_RTT_MS} ms",
        f"route receipt grid {ROUND50_RECEIPT_GRID_MS} ms is a route-side poll "
        "cadence, reproduced by the driver's receipt wait and measured there",
        "research-iroh.md 3 round1 selected ip path rtt_ms; "
        "physical_inference_node.py:1741 poll_interval_seconds=0.02; "
        "route.py:68 _CLEANUP_RECEIPT_POLL_SECONDS=0.1",
    )
    origin_basis = (
        f"loopback baseline rtt {ROUND50_DOH_RTT_MS} ms; no round-50 DoH RTT exists "
        "in the corpus (ROUND50_UNMEASURED)",
        "research-fault-proxy.md 6a scenario A",
    )

    def _leg(
        name: str, leg: str, basis: Sequence[str], hold: float | None
    ) -> Round50LegConfig:
        config = round50_profile(
            seed, leg=leg, jitter_ms=jitter_ms, chunk_bytes=chunk_bytes
        )
        if hold is not None:
            config.hold_down_after_chunk = 0
            config.hold_down_ms = float(hold)
        return Round50LegConfig(name=name, leg=leg, config=config, basis=tuple(basis))

    return (
        _leg("origin", "origin", origin_basis, None),
        _leg("worker-a", "worker", worker_basis, None),
        _leg("worker-c", "worker", worker_basis, receipt_hold_ms),
    )


# --------------------------------------------------------------------------- #
# statistics and the replay digest
# --------------------------------------------------------------------------- #


@dataclass
class Stats:
    """Per-relay counters plus the per-chunk trace the digest is taken over."""

    chunks_up: int = 0
    chunks_down: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    dropped: int = 0
    delayed: int = 0
    blackholed: int = 0
    held: int = 0
    refused: int = 0
    delay_ms_total: float = 0.0
    connections: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        direction: str,
        chunk_index: int,
        nbytes: int,
        action: str,
        delay_ms: float,
    ) -> None:
        self.trace.append(
            {
                "dir": direction,
                "chunk_index": chunk_index,
                "bytes": nbytes,
                "action": action,
                "delay_ms": round(delay_ms, 3),
            }
        )
        if action == "drop":
            self.dropped += 1
        elif action == "blackhole":
            self.blackholed += 1
        elif action == "hold":
            self.held += 1
            self.delay_ms_total += delay_ms
        elif action == "refused":
            self.refused += 1
        else:
            self.delayed += 1
            self.delay_ms_total += delay_ms
        if direction == "up":
            self.chunks_up += 1
            if action != "drop":
                self.bytes_up += nbytes
        else:
            self.chunks_down += 1
            if action != "drop":
                self.bytes_down += nbytes

    def trace_digest(self) -> str:
        """16-hex-char SHA-256 of the ``(dir, chunk_index, action, delay_ms)`` trace.

        The replay assertion: same seed -> same digest, across processes.
        """

        payload = json.dumps(self.trace, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict[str, Any]:
        document = {key: value for key, value in vars(self).items() if key != "trace"}
        document["trace_digest"] = self.trace_digest()
        document["trace_events"] = len(self.trace)
        return document


# --------------------------------------------------------------------------- #
# the relay
# --------------------------------------------------------------------------- #


def _rng(seed: int, conn_index: int, direction: str) -> random.Random:
    # NOTE: random.Random does NOT accept a tuple (TypeError at runtime), so the
    # per-stream RNG is keyed with a string. String seeding uses sha512 and is
    # stable across processes and PYTHONHASHSEED values, so replays match.
    return random.Random(f"{seed}:{conn_index}:{direction}")


def _partition_active(
    cfg: FaultConfig,
    direction: str,
    chunk_index: int,
    started_at: float | None,
    now: float,
) -> bool:
    if cfg.partition_at_chunk is None or direction not in cfg.partition_sides:
        return False
    if chunk_index < cfg.partition_at_chunk:
        return False
    if cfg.partition_for_ms <= 0:
        return True
    if started_at is None:
        return True
    return (now - started_at) * 1000.0 < cfg.partition_for_ms


def _hold_active(cfg: FaultConfig, direction: str, chunk_index: int) -> bool:
    """EMULATION EXTENSION: the receipt hold, on the ``down`` direction only."""

    return (
        direction == "down"
        and cfg.hold_down_after_chunk is not None
        and chunk_index >= cfg.hold_down_after_chunk
        and cfg.hold_down_ms > 0.0
    )


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: FaultConfig,
    rng: random.Random,
    direction: str,
    stats: Stats,
) -> None:
    chunk_index = 0
    partition_started: float | None = None
    try:
        while True:
            data = await reader.read(cfg.chunk_bytes)
            if not data:
                # Half-close: forward FIN without tearing down the other direction.
                try:
                    writer.write_eof()
                    await writer.drain()
                except (OSError, RuntimeError):
                    pass
                return
            now = time.monotonic()
            if _partition_active(cfg, direction, chunk_index, partition_started, now):
                if partition_started is None:
                    partition_started = now
                stats.record(direction, chunk_index, len(data), "blackhole", 0.0)
                chunk_index += 1
                continue
            delay_ms = 0.0
            base = cfg.direction_latency_ms(direction)
            if base or cfg.jitter_ms:
                jitter = (
                    rng.uniform(-cfg.jitter_ms, cfg.jitter_ms) if cfg.jitter_ms else 0.0
                )
                delay_ms += max(0.0, base + jitter)
            action = "delay"
            if cfg.loss > 0.0 and rng.random() < cfg.loss:
                if cfg.loss_mode == "drop":
                    stats.record(direction, chunk_index, len(data), "drop", 0.0)
                    chunk_index += 1
                    continue
                delay_ms += cfg.loss_delay_ms
                action = "loss-retransmit"
            # EMULATION EXTENSION: hold the receipt leg past the route's window.
            if _hold_active(cfg, direction, chunk_index):
                delay_ms += cfg.hold_down_ms
                action = "hold"
            if delay_ms > 0.0:
                await asyncio.sleep(delay_ms / 1000.0)
            writer.write(data)
            await writer.drain()
            stats.record(direction, chunk_index, len(data), action, delay_ms)
            chunk_index += 1
    except (ConnectionResetError, BrokenPipeError, OSError):
        return
    # NOTE: no writer.close() here. A relay that closes the peer writer on EOF
    # would destroy the opposite direction's pending reply (half-close). The
    # handler owns teardown, after both directions have drained (--linger-ms).


def make_handler(
    cfg: FaultConfig,
    upstream_host: str,
    upstream_port: int,
    stats: Stats,
    counter: list[int],
    on_listen: Callable[[], None] | None = None,
):
    async def handle(
        client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        conn_index = counter[0]
        counter[0] += 1
        stats.connections += 1
        # EMULATION EXTENSION: refusal partition.  Count the connection (so the
        # digest stays a faithful record of what was attempted) and drop the
        # client without ever opening the upstream.
        refuse = (
            cfg.refuse_after_connections is not None
            and conn_index >= cfg.refuse_after_connections
        )
        if refuse or (
            cfg.max_connections is not None and conn_index >= cfg.max_connections
        ):
            stats.record("up", conn_index, 0, "refused", 0.0)
            client_writer.close()
            return
        try:
            if cfg.connect_delay_ms > 0:
                await asyncio.sleep(cfg.connect_delay_ms / 1000.0)
            up_reader, up_writer = await asyncio.open_connection(
                upstream_host, upstream_port
            )
        except OSError:
            client_writer.close()
            return
        task_up = asyncio.create_task(
            _relay(
                client_reader,
                up_writer,
                cfg,
                _rng(cfg.seed, conn_index, "up"),
                "up",
                stats,
            )
        )
        task_down = asyncio.create_task(
            _relay(
                up_reader,
                client_writer,
                cfg,
                _rng(cfg.seed, conn_index, "down"),
                "down",
                stats,
            )
        )
        # Wait for both directions; when the first finishes (EOF propagated),
        # give the other one linger_ms to drain its pending reply, then tear down.
        _done, pending = await asyncio.wait(
            {task_up, task_down}, return_when=asyncio.FIRST_COMPLETED
        )
        if pending:
            _done_now, pending = await asyncio.wait(
                pending, timeout=cfg.linger_ms / 1000.0
            )
        for task in pending:
            task.cancel()
        for task in (task_up, task_down):
            if not task.done():
                task.cancel()
        for writer in (client_writer, up_writer):
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    return handle


@dataclass
class FaultProxyHandle:
    """A running relay: the server, its counters and how to stop it."""

    name: str
    leg: str
    config: FaultConfig
    server: Any
    stats: Stats
    counter: list[int]
    host: str
    port: int
    refusing: bool = False

    def trace_digest(self) -> str:
        return self.stats.trace_digest()

    # -- EMULATION EXTENSION ------------------------------------------------- #
    def set_hold(self, hold_ms: float, *, after_chunk: int = 0) -> None:
        """Arm (or clear) the ``down``-direction receipt hold at run time.

        The hold is read per chunk inside the relay, so a caller can arm it for
        exactly one bounded exchange - which is how the emulation injects the
        round-50 cleanup-receipt wedge without holding the token-carrying
        commands that came before it.  ``hold_ms <= 0`` clears it.
        """

        if hold_ms > 0:
            self.config.hold_down_after_chunk = int(after_chunk)
            self.config.hold_down_ms = float(hold_ms)
        else:
            self.config.hold_down_after_chunk = None
            self.config.hold_down_ms = 0.0

    def reset_stats(self) -> None:
        """Zero the counters and the trace, keeping the running relay.

        Used between scenarios so each scenario's :meth:`trace_digest` covers only
        its own traffic.
        """

        self.stats.trace.clear()
        for name in (
            "chunks_up",
            "chunks_down",
            "bytes_up",
            "bytes_down",
            "dropped",
            "delayed",
            "blackholed",
            "held",
            "refused",
            "connections",
        ):
            setattr(self.stats, name, 0)
        self.stats.delay_ms_total = 0.0

    def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "leg": self.leg,
            "listen": f"{self.host}:{self.port}",
            "refusing": self.refusing,
            "seed": self.config.seed,
            "config": self.config.to_document(),
            "stats": self.stats.summary(),
        }


async def start_proxy(
    cfg: FaultConfig,
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
    *,
    name: str = "proxy",
    leg: str = "generic",
    stats: Stats | None = None,
) -> FaultProxyHandle:
    """Start one relay in the current event loop.

    With ``cfg.refuse_listener`` the server binds (so a real ephemeral port is
    reported), then closes the listener at once: every subsequent ``connect`` on
    that port is refused by the kernel, which is the exact refusal partition.  The
    port is deliberately left unbound rather than re-bound by a stub listener,
    because a socket that is *open but never answered* is a blackhole, a different
    failure (research-fault-proxy.md §4.8).
    """

    resolved_stats = stats if stats is not None else Stats()
    counter = [0]
    server = await asyncio.start_server(
        make_handler(cfg, upstream_host, upstream_port, resolved_stats, counter),
        host=listen_host,
        port=listen_port,
    )
    if not server.sockets:
        raise FaultProxyError("relay_bound_no_socket")
    port = int(server.sockets[0].getsockname()[1])
    if cfg.refuse_listener:
        server.close()
        await server.wait_closed()
        return FaultProxyHandle(
            name=name,
            leg=leg,
            config=cfg,
            server=None,
            stats=resolved_stats,
            counter=counter,
            host=listen_host,
            port=port,
            refusing=True,
        )
    return FaultProxyHandle(
        name=name,
        leg=leg,
        config=cfg,
        server=server,
        stats=resolved_stats,
        counter=counter,
        host=listen_host,
        port=port,
    )


# --------------------------------------------------------------------------- #
# standalone CLI (the spike's entry point, kept for the scenario-K shape)
# --------------------------------------------------------------------------- #


async def _main_async(args: argparse.Namespace) -> int:
    cfg = FaultConfig(
        seed=args.seed,
        latency_ms=args.latency_ms,
        jitter_ms=args.jitter_ms,
        loss=args.loss,
        loss_mode=args.loss_mode,
        loss_delay_ms=args.loss_delay_ms,
        chunk_bytes=args.chunk_bytes,
        partition_at_chunk=args.partition_at_chunk,
        partition_for_ms=args.partition_for_ms,
        partition_sides=tuple(args.partition_sides.split(",")),
        partition_mode=args.partition_mode,
        connect_delay_ms=args.connect_delay_ms,
        linger_ms=args.linger_ms,
        hold_down_after_chunk=args.hold_down_after_chunk,
        hold_down_ms=args.hold_down_ms,
        refuse_after_connections=args.refuse_after_connections,
    )
    up_host, up_port = args.upstream.rsplit(":", 1)
    ls_host, ls_port = args.listen.rsplit(":", 1)
    handle = await start_proxy(
        cfg, ls_host, int(ls_port), up_host, int(up_port), name="cli", leg="cli"
    )
    print(
        json.dumps(
            {
                "event": "listening",
                "listen": args.listen,
                "bound_ports": [handle.port],
                "upstream": args.upstream,
                "seed": cfg.seed,
            }
        ),
        flush=True,
    )
    try:
        async with handle.server:
            await asyncio.sleep(args.duration)
    finally:
        summary = {"event": "summary", **handle.stats.summary()}
        if args.trace:
            with open(args.trace, "w", encoding="utf-8") as fh:
                for item in handle.stats.trace:
                    fh.write(json.dumps(item, sort_keys=True) + "\n")
        print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="seeded asyncio TCP fault proxy")
    parser.add_argument("--listen", default="127.0.0.1:19101")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--jitter-ms", type=float, default=0.0)
    parser.add_argument("--loss", type=float, default=0.0, help="0.0-1.0 per chunk")
    parser.add_argument("--loss-mode", choices=("drop", "delay"), default="delay")
    parser.add_argument("--loss-delay-ms", type=float, default=200.0)
    parser.add_argument("--chunk-bytes", type=int, default=65536)
    parser.add_argument("--partition-at-chunk", type=int, default=None)
    parser.add_argument("--partition-for-ms", type=float, default=0.0)
    parser.add_argument("--partition-sides", default="up,down")
    parser.add_argument(
        "--partition-mode", choices=_PARTITION_MODES, default="blackhole"
    )
    parser.add_argument("--hold-down-after-chunk", type=int, default=None)
    parser.add_argument("--hold-down-ms", type=float, default=0.0)
    parser.add_argument("--refuse-after-connections", type=int, default=None)
    parser.add_argument("--connect-delay-ms", type=float, default=0.0)
    parser.add_argument("--linger-ms", type=float, default=2000.0)
    parser.add_argument(
        "--trace", default=None, help="write per-chunk JSONL trace here"
    )
    parser.add_argument("--duration", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
