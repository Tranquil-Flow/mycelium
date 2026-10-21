# SPDX-License-Identifier: AGPL-3.0-only
"""Fault-proxy tests for the promoted relay (``mycelium_testbed.faults``).

Every property here is one the research note claims for the stdlib relay and
against which it rejected the alternatives (research-fault-proxy.md §4, §6, §8):

* seeded replay (the reason ``toxiproxy`` was rejected: its jitter has no seed);
* half-close preserved (the case toxiproxy 2.12.0 fails at *baseline*);
* both partition shapes, asserted differently, because a silent blackhole and a
  refused connection are different failures for ``mycelium_live/route.py``;
* per-byte delay is pipelined, so budgets must come from the pipelined figure;
* the receipt hold that reproduces the round-50 wedge.

Deliberately **sync tests driving ``asyncio.run``**: the repository's suite runs on
the bare interpreter (``python3 -B -m pytest -p no:cacheprovider``) with no
``asyncio_mode`` configured, so an ``async def test_`` would be skipped rather than
run.  A skipped test is not evidence.
"""

from __future__ import annotations

import asyncio

import pytest

from mycelium_testbed.faults import (
    ROUND50_COMMAND_QUEUE_GRID_MS,
    ROUND50_DOH_RTT_MS,
    ROUND50_MEASURED,
    ROUND50_RECEIPT_GRID_MS,
    ROUND50_RELAY_RTT_MS,
    ROUND50_UNMEASURED,
    FaultConfig,
    FaultProxyError,
    round50_legs,
    round50_profile,
    start_proxy,
    Stats,
)

ECHO_PAYLOAD = b"REQ-0008"


# --------------------------------------------------------------------------- #
# a loopback echo server plus the two client shapes, all inside one loop
# --------------------------------------------------------------------------- #


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                try:
                    writer.write_eof()
                    await writer.drain()
                except (OSError, RuntimeError):
                    pass
                writer.close()
                return
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        return


async def _echo_port() -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def _round_trip(port: int, payload: bytes = ECHO_PAYLOAD) -> bytes:
    """One request/reply over the relay, read to EOF.

    Reading to EOF - not ``read(65536)`` - matters: with a per-byte delay the
    reply starts arriving after the first byte, so a single ``read`` would return
    a *partial* reply and the chunk counts would be meaningless.
    """

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        try:
            await writer.drain()
        except (OSError, RuntimeError):
            pass
        chunks: list[bytes] = []
        while True:
            data = await reader.read(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        writer.close()


async def _timed_round_trip(port: int, payload: bytes = ECHO_PAYLOAD) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _round_trip(port, payload)
    return (loop.time() - started) * 1000.0


async def _with_relay(echo_port: int, config: FaultConfig, body):
    """Start one relay, run ``body(handle)``, then tear both down."""

    handle = await start_proxy(config, "127.0.0.1", 0, "127.0.0.1", echo_port)
    try:
        return await body(handle)
    finally:
        handle.stop()
        if handle.server is not None:
            await handle.server.wait_closed()


# --------------------------------------------------------------------------- #
# determinism: the property the whole replay claim rests on
# --------------------------------------------------------------------------- #


def test_same_seed_replays_the_same_trace_digest() -> None:
    async def scenario() -> list[str]:
        server, echo_port = await _echo_port()
        try:
            digests = []
            for _ in range(2):
                config = FaultConfig(seed=7, latency_ms=5.0, chunk_bytes=8)

                async def body(handle):
                    assert await _round_trip(handle.port) == ECHO_PAYLOAD
                    return handle.trace_digest()

                digests.append(await _with_relay(echo_port, config, body))
            return digests
        finally:
            server.close()
            await server.wait_closed()

    digests = asyncio.run(scenario())
    assert digests[0] == digests[1]
    assert len(digests[0]) == 16


def test_a_different_seed_changes_the_pattern() -> None:
    async def scenario() -> list[str]:
        server, echo_port = await _echo_port()
        try:
            digests = []
            for seed in (7, 8):
                config = FaultConfig(
                    seed=seed, latency_ms=5.0, jitter_ms=2.0, chunk_bytes=8
                )

                async def body(handle):
                    await _round_trip(handle.port)
                    return handle.trace_digest()

                digests.append(await _with_relay(echo_port, config, body))
            return digests
        finally:
            server.close()
            await server.wait_closed()

    digests = asyncio.run(scenario())
    assert digests[0] != digests[1]


def test_trace_covers_direction_chunk_index_action_and_delay() -> None:
    async def scenario() -> tuple[list[dict], dict]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=3, latency_ms=5.0, chunk_bytes=8)

            async def body(handle):
                await _round_trip(handle.port)
                return list(handle.stats.trace), handle.stats.summary()

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    trace, summary = asyncio.run(scenario())
    assert trace
    assert {event["dir"] for event in trace} == {"up", "down"}
    assert all(event["action"] == "delay" for event in trace)
    assert all(event["delay_ms"] >= 0.0 for event in trace)
    up_indexes = [event["chunk_index"] for event in trace if event["dir"] == "up"]
    assert up_indexes == list(range(len(up_indexes)))
    # the digest is over the (dir, chunk_index, action, delay_ms) sequence
    assert set(trace[0]) == {"dir", "chunk_index", "bytes", "action", "delay_ms"}
    assert summary["trace_events"] == len(trace)


# --------------------------------------------------------------------------- #
# half-close: the case toxiproxy fails
# --------------------------------------------------------------------------- #


def test_half_close_still_delivers_the_reply() -> None:
    """A request that FINs before reading its reply must still see the reply.

    research-fault-proxy.md §4.5: the first relay draft (and toxiproxy 2.12.0 at
    baseline) returned ``reply_after_FIN=b''``.
    """

    async def scenario() -> bytes:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=11, latency_ms=1.0, chunk_bytes=8)

            async def body(handle):
                return await _round_trip(handle.port)

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == ECHO_PAYLOAD


# --------------------------------------------------------------------------- #
# latency shaping
# --------------------------------------------------------------------------- #


def test_latency_is_injected_in_both_directions() -> None:
    async def scenario() -> float:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=5, latency_ms=30.0, chunk_bytes=65536)

            async def body(handle):
                return await _timed_round_trip(handle.port)

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    elapsed_ms = asyncio.run(scenario())
    assert elapsed_ms >= 2 * 30.0


def test_per_byte_delay_pipelines_rather_than_multiplies() -> None:
    """8 B + 8 B at 10 ms/byte is ~100 ms, not 160 ms (research note §4.9)."""

    async def scenario() -> tuple[float, Stats]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=5, latency_ms=10.0, chunk_bytes=1)

            async def body(handle):
                elapsed_ms = await _timed_round_trip(handle.port)
                return elapsed_ms, handle.stats

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    elapsed_ms, stats = asyncio.run(scenario())
    assert stats.chunks_up == len(ECHO_PAYLOAD)
    assert stats.chunks_down == len(ECHO_PAYLOAD)
    assert 60.0 <= elapsed_ms < 160.0, elapsed_ms


def test_loss_mode_delay_models_tcp_retransmit() -> None:
    """Loss must show up as latency, never as a corrupted stream."""

    async def scenario() -> tuple[list[bytes], Stats]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(
                seed=3, loss=0.5, loss_mode="delay", loss_delay_ms=120.0, chunk_bytes=8
            )

            async def body(handle):
                replies = [await _round_trip(handle.port) for _ in range(10)]
                return replies, handle.stats

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    replies, stats = asyncio.run(scenario())
    assert replies == [ECHO_PAYLOAD] * 10
    assert stats.dropped == 0
    assert any(event["action"] == "loss-retransmit" for event in stats.trace)


def test_loss_mode_drop_is_opt_in_and_corrupts_the_stream() -> None:
    async def scenario() -> tuple[bytes, Stats]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=3, loss=1.0, loss_mode="drop", chunk_bytes=8)

            async def body(handle):
                reply = await asyncio.wait_for(_round_trip(handle.port), timeout=5.0)
                return reply, handle.stats

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    reply, stats = asyncio.run(scenario())
    assert reply == b""
    assert stats.dropped > 0


# --------------------------------------------------------------------------- #
# partitions: two shapes, asserted differently
# --------------------------------------------------------------------------- #


def test_blackhole_partition_silently_swallows_bytes() -> None:
    async def scenario() -> tuple[bytes, Stats]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=5, partition_at_chunk=0, partition_for_ms=0.0)

            async def body(handle):
                reply = await asyncio.wait_for(_round_trip(handle.port), timeout=2.0)
                return reply, handle.stats

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    reply, stats = asyncio.run(scenario())
    # the echo server never sees the payload, so nothing is echoed back
    assert reply != ECHO_PAYLOAD
    assert stats.blackholed >= 1


def test_refusal_partition_is_a_refused_connection() -> None:
    async def scenario() -> None:
        server, echo_port = await _echo_port()
        try:
            handle = await start_proxy(
                FaultConfig(seed=3, refuse_listener=True),
                "127.0.0.1",
                0,
                "127.0.0.1",
                echo_port,
            )
            try:
                assert handle.refusing is True
                assert handle.server is None
                with pytest.raises(ConnectionRefusedError):
                    await _round_trip(handle.port)
            finally:
                handle.stop()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_draining_listener_resets_instead_of_refusing() -> None:
    async def scenario() -> int:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=3, refuse_after_connections=0)

            async def body(handle):
                with pytest.raises((ConnectionResetError, BrokenPipeError)):
                    await _round_trip(handle.port)
                return handle.stats.refused

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == 1


# --------------------------------------------------------------------------- #
# the receipt hold (the round-50 wedge's knob)
# --------------------------------------------------------------------------- #


def test_receipt_hold_delays_only_the_down_direction() -> None:
    async def scenario() -> tuple[float, Stats]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(
                seed=5, hold_down_after_chunk=0, hold_down_ms=400.0, chunk_bytes=8
            )

            async def body(handle):
                elapsed_ms = await _timed_round_trip(handle.port)
                return elapsed_ms, handle.stats

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    elapsed_ms, stats = asyncio.run(scenario())
    assert elapsed_ms >= 400.0
    held = [event for event in stats.trace if event["action"] == "hold"]
    assert held
    assert all(event["dir"] == "down" for event in held)
    assert stats.held == len(held)


def test_set_hold_arms_and_clears_at_run_time() -> None:
    async def scenario() -> tuple[float, float, int, float, int]:
        server, echo_port = await _echo_port()
        try:
            config = FaultConfig(seed=5, chunk_bytes=8)

            async def body(handle):
                first_ms = await _timed_round_trip(handle.port)
                handle.reset_stats()
                handle.set_hold(300.0)
                held_ms = await _timed_round_trip(handle.port)
                held_events = handle.stats.held
                handle.set_hold(0.0)
                handle.reset_stats()
                cleared_ms = await _timed_round_trip(handle.port)
                cleared_events = handle.stats.held
                return first_ms, held_ms, held_events, cleared_ms, cleared_events

            return await _with_relay(echo_port, config, body)
        finally:
            server.close()
            await server.wait_closed()

    first_ms, held_ms, held_events, cleared_ms, cleared_events = asyncio.run(scenario())
    assert first_ms < 100.0
    assert held_ms >= 300.0
    assert held_events >= 1
    assert cleared_ms < 100.0
    assert cleared_events == 0


# --------------------------------------------------------------------------- #
# round-50 shaping: the numbers, and where each one comes from
# --------------------------------------------------------------------------- #


def test_round50_legs_carry_the_measured_numbers() -> None:
    legs = {leg.name: leg for leg in round50_legs(20260915)}
    assert set(legs) == {"origin", "worker-a", "worker-c"}

    worker = legs["worker-a"].config
    assert worker.up_latency_ms == pytest.approx(
        ROUND50_RELAY_RTT_MS + ROUND50_COMMAND_QUEUE_GRID_MS
    )
    assert worker.down_latency_ms == pytest.approx(ROUND50_RELAY_RTT_MS)
    assert worker.seed == 20260915

    origin = legs["origin"].config
    assert origin.up_latency_ms == pytest.approx(ROUND50_DOH_RTT_MS)
    assert origin.down_latency_ms == pytest.approx(ROUND50_DOH_RTT_MS)

    # every leg documents its own provenance, and the receipt grid is located on
    # the route side rather than dressed up as a link delay
    assert all(leg.basis for leg in legs.values())
    worker_basis = " ".join(legs["worker-a"].basis)
    assert "route.py:68" in worker_basis
    assert str(ROUND50_RECEIPT_GRID_MS) in worker_basis
    assert "route-side poll cadence" in worker_basis


def test_round50_measured_table_is_sourced_and_the_gap_is_named() -> None:
    symbols = {symbol for symbol, _value, _unit, _source in ROUND50_MEASURED}
    assert symbols == {
        "receipt_poll_mean_ms",
        "receipt_poll_p95_ms",
        "receipt_poll_max_ms",
        "relay_rtt_ms",
        "baseline_relay_rtt_ms",
    }
    assert all(
        source.startswith("research-")
        for _symbol, _value, _unit, source in ROUND50_MEASURED
    )
    assert ROUND50_UNMEASURED
    assert any("doh_rtt_ms" in item for item in ROUND50_UNMEASURED)


def test_the_negative_case_hold_is_armed_on_worker_c_only() -> None:
    legs = {leg.name: leg for leg in round50_legs(7, receipt_hold_ms=1700.0)}
    assert legs["worker-c"].config.hold_down_ms == 1700.0
    assert legs["worker-c"].config.hold_down_after_chunk == 0
    assert legs["worker-a"].config.hold_down_ms == 0.0
    assert legs["worker-a"].config.hold_down_after_chunk is None


def test_round50_profile_rejects_an_unknown_leg() -> None:
    with pytest.raises(FaultProxyError):
        round50_profile(1, leg="not-a-leg")


def test_config_rejects_unusable_values() -> None:
    with pytest.raises(FaultProxyError):
        FaultConfig(loss_mode="corrupt")
    with pytest.raises(FaultProxyError):
        FaultConfig(partition_mode="drop-everything")
    with pytest.raises(FaultProxyError):
        FaultConfig(loss=1.5)
    with pytest.raises(FaultProxyError):
        FaultConfig(chunk_bytes=0)
    with pytest.raises(FaultProxyError):
        FaultConfig(partition_sides=("sideways",))
