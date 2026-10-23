# SPDX-License-Identifier: AGPL-3.0-only
"""Default-off entry-local decode orchestration; not a wire authority boundary.

Call only after authenticated request admission. Callbacks retain ownership of
transport, cancellation fences, runtime evidence and cleanup. This driver does
not verify signatures, interrupt blocking calls, or mint cleanup receipts.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable


@dataclass(frozen=True)
class DecodeSnapshot:
    token_ids: tuple[int, ...]
    status: str


@dataclass(frozen=True)
class TokenBatch:
    sequence: int
    start_index: int
    token_ids: tuple[int, ...]
    status: str


def drive_entry_decode(
    *,
    initial: Callable[[], DecodeSnapshot],
    decode: Callable[[], DecodeSnapshot],
    max_new_tokens: int,
    stop_token_ids: tuple[int, ...],
    deadline_monotonic_s: float,
    run_deadline_monotonic_s: float,
    publish: Callable[[TokenBatch], None],
    cancelled: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    enabled: bool = False,
    batch_tokens: int = 8,
    batch_seconds: float = 0.05,
) -> TokenBatch:
    """Drive existing decode locally and publish bounded, contiguous deltas.

    Deadlines are already translated to this process's monotonic clock. The
    run deadline must originate at process start (ceiling/lease), not at request
    admission. The caller must bound blocking decode/publication by this same
    deadline; post-call checks cannot preempt an uninterruptible dependency.
    Exceptions are deliberately not converted into success-shaped receipts.
    """
    if enabled is not True:
        raise RuntimeError('token_path_disabled')
    if (type(max_new_tokens) is not int or max_new_tokens <= 0
            or type(batch_tokens) is not int or not 1 <= batch_tokens <= 128
            or not isinstance(stop_token_ids, tuple)
            or len(stop_token_ids) > 128
            or any(type(t) is not int or t < 0 for t in stop_token_ids)):
        raise ValueError('token_path_invalid_limits')
    for value in (deadline_monotonic_s, run_deadline_monotonic_s, batch_seconds):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('token_path_invalid_deadline')
    if not 0 < batch_seconds <= 0.05:
        raise ValueError('token_path_invalid_batch_interval')
    deadline = min(deadline_monotonic_s, run_deadline_monotonic_s)
    emitted = 0
    sequence = 0
    last_publish = clock()
    previous: tuple[int, ...] = ()

    def check_deadline() -> None:
        if clock() >= deadline:
            raise TimeoutError('command_deadline_exceeded')

    def send(tokens: tuple[int, ...], status: str) -> TokenBatch:
        nonlocal emitted, sequence, last_publish
        check_deadline()
        batch = TokenBatch(sequence, emitted, tokens, status)
        publish(batch)
        # A late return cannot become successful completion. Publication itself
        # needs the caller's deadline fence; already delivered data is not recalled.
        check_deadline()
        # A failed publication must never trigger another decode.
        emitted += len(tokens)
        sequence += 1
        last_publish = clock()
        return batch

    check_deadline()
    if cancelled():
        return send((), 'CANCELLED')
    state = initial()
    first_snapshot = True
    while True:
        check_deadline()
        if cancelled() or state.status == 'CANCELLED':
            return send((), 'CANCELLED')
        tokens = state.token_ids
        if (not isinstance(tokens, tuple)
                or any(type(t) is not int or t < 0 for t in tokens)
                or tokens[:len(previous)] != previous
                or len(tokens) < len(previous)
                or len(tokens) > max_new_tokens):
            raise RuntimeError('token_path_output_invalid')
        if state.status not in {'DECODING', 'COMPLETED'}:
            raise RuntimeError('token_path_inference_failed')
        visible = tokens
        stopped = False
        for index, token in enumerate(tokens):
            if token in stop_token_ids:
                visible, stopped = tokens[:index], True
                break
        terminal = stopped or len(tokens) == max_new_tokens or state.status == 'COMPLETED'
        # Chunk even a large prefill/speculative output; no batch exceeds bound.
        while len(visible) - emitted >= batch_tokens:
            if cancelled():
                return send((), 'CANCELLED')
            final = terminal and emitted + batch_tokens == len(visible)
            result = send(visible[emitted:emitted + batch_tokens],
                          'COMPLETED' if final else 'DECODING')
            if final:
                return result
        if terminal:
            if cancelled():
                return send((), 'CANCELLED')
            return send(visible[emitted:], 'COMPLETED')
        # Preserve TTFT: prefill's already computed token must not wait for
        # the first decode batch. Subsequent snapshots use the batch policy.
        if len(visible) > emitted and (first_snapshot or clock() - last_publish >= batch_seconds):
            if cancelled():
                return send((), 'CANCELLED')
            send(visible[emitted:], 'DECODING')
        first_snapshot = False
        check_deadline()
        if cancelled():
            return send((), 'CANCELLED')
        previous = tokens
        state = decode()
        check_deadline()
        if (not cancelled() and state.status == 'DECODING'
                and len(state.token_ids) <= len(previous)):
            raise RuntimeError('route_decode_stalled')
