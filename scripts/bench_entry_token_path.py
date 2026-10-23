#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Desk/fake only: no network, native sidecar, model load, or devices.

Example: PYTHONPATH=. python scripts/bench_entry_token_path.py --tokens 128
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from mycelium_router.token_path import DecodeSnapshot, drive_entry_decode


def trial(*, local: bool, tokens: int, hop_ms: float, control_ms: float,
          batch_tokens: int) -> dict:
    output: list[int] = []
    arrivals: list[float] = []
    commands = 0
    hop_count = 0
    receipts = 0
    started = time.perf_counter()

    def command():
        nonlocal commands
        commands += 1
        time.sleep(control_ms / 1000)

    def compute() -> DecodeSnapshot:
        nonlocal hop_count
        # Fake stage-0 -> stage-1 hidden state and sampled-token return hops.
        time.sleep(hop_ms / 1000)
        token = len(output) + 1  # fake compute, no model/MLX/GPU
        time.sleep(hop_ms / 1000)
        hop_count += 2
        output.append(token)
        return DecodeSnapshot(tuple(output), 'DECODING')

    def publish(batch):
        nonlocal receipts
        receipts += 1
        arrivals.extend([time.perf_counter()] * len(batch.token_ids))

    command()  # one request admission / infer_start
    initial = compute()  # first token from fake prefill
    if local:
        drive_entry_decode(
            initial=lambda: initial, decode=compute, max_new_tokens=tokens,
            stop_token_ids=(), deadline_monotonic_s=time.monotonic() + 30,
            run_deadline_monotonic_s=time.monotonic() + 30,
            publish=publish, cancelled=lambda: False, enabled=True,
            batch_tokens=batch_tokens,
        )
    else:
        arrivals.append(time.perf_counter())
        receipts += 1
        for _ in range(tokens - 1):
            command()
            compute()
            arrivals.append(time.perf_counter())
            receipts += 1
    elapsed = time.perf_counter() - started
    assert len(output) == len(arrivals) == tokens
    assert commands == (1 if local else tokens)
    assert output == list(range(1, tokens + 1))
    return {
        'path': 'entry_local' if local else 'per_token_control',
        'tokens': tokens, 'inference_commands': commands,
        'fake_hops': hop_count, 'batches': receipts,
        'elapsed_seconds': elapsed, 'end_to_end_tokens_per_second': tokens / elapsed,
        'steady_tokens_per_second': (tokens - 1) / (arrivals[-1] - arrivals[0]),
        'ttft_ms': (arrivals[0] - started) * 1000,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tokens', type=int, default=128)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--hop-ms', type=float, default=1.0)
    parser.add_argument('--control-ms', type=float, default=10.0)
    parser.add_argument('--batch-tokens', type=int, default=8)
    args = parser.parse_args()
    if not 2 <= args.tokens <= 4096 or not 1 <= args.repeats <= 20:
        parser.error('tokens 2..4096, repeats 1..20 required')
    if not 0 <= args.hop_ms <= 100 or not 0 <= args.control_ms <= 100:
        parser.error('delays must be finite and within 0..100 ms')
    if not 1 <= args.batch_tokens < args.tokens:
        parser.error('batch_tokens must be positive and less than tokens')
    runs = []
    for repeat in range(args.repeats):
        # Interleave order to reduce warm-up/scheduling bias.
        for local in ((False, True) if repeat % 2 == 0 else (True, False)):
            runs.append(trial(local=local, tokens=args.tokens, hop_ms=args.hop_ms,
                              control_ms=args.control_ms, batch_tokens=args.batch_tokens))
    metrics = ('end_to_end_tokens_per_second', 'steady_tokens_per_second', 'ttft_ms')
    medians = {path: {metric: statistics.median(r[metric] for r in runs if r['path'] == path)
                      for metric in metrics}
               for path in ('per_token_control', 'entry_local')}
    print(json.dumps({'label': 'DESK/FAKE: not iroh, MLX, GPU or device performance',
                      'parameters': vars(args), 'medians': medians, 'runs': runs}, indent=2))


if __name__ == '__main__':
    main()
