#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""RED tests for mycelium_router.instrument — hop-level timing capture.

Tests are pure-Python, no fixtures beyond stdlib. They define the contract the
recorder must hold: monotonic ordering, complete fields per hop, payload sizes,
and zero-cost when disabled.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# The repository under test, never a foreign checkout.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_router import instrument  # noqa: E402


class HopRecorderContract(unittest.TestCase):

    def test_disabled_recorder_is_a_no_op(self):
        r = instrument.HopRecorder(enabled=False)
        with r.trace_request("req-1") as req:
            with req.hop("stage-0", "node-0") as h:
                h.queue_in(); h.compute_start()
                h.compute_end(); h.send(bytes_out=128)
                h.recv(bytes_in=64)
        # No file should be opened
        self.assertEqual(r.sink_path, None)
        self.assertEqual(r.records, [])

    def test_records_are_monotonic_per_hop(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            r = instrument.HopRecorder(enabled=True, sink_path=path)
            with r.trace_request("req-1") as req:
                with req.hop("prefill", "node-0") as h:
                    h.queue_in()
                    time.sleep(0.001)
                    h.compute_start()
                    time.sleep(0.002)
                    h.compute_end()
                    time.sleep(0.001)
                    h.send(bytes_out=4096)
                    time.sleep(0.001)
                    h.recv(bytes_in=4096)
                with req.hop("decode", "node-1") as h:
                    h.queue_in()
                    time.sleep(0.001)
                    h.compute_start()
                    time.sleep(0.001)
                    h.compute_end()
                    h.send(bytes_out=256)
                    h.recv(bytes_in=128)
            r.flush()
            data = [json.loads(l) for l in open(path) if l.strip()]
            self.assertEqual(len(data), 1)
            trace = data[0]
            self.assertEqual(trace["request_id"], "req-1")
            self.assertEqual(len(trace["hops"]), 2)
            for hop in trace["hops"]:
                ts = [hop["queue_in_at"], hop["compute_start_at"],
                      hop["compute_end_at"], hop["send_start_at"],
                      hop["send_end_at"], hop["recv_end_at"]]
                self.assertEqual(ts, sorted(ts), f"non-monotonic hop: {hop}")
                self.assertGreater(hop["compute_end_at"], hop["compute_start_at"])
        finally:
            os.unlink(path)

    def test_every_hop_records_required_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            r = instrument.HopRecorder(enabled=True, sink_path=path)
            with r.trace_request("req-2") as req:
                with req.hop("s0", "n0") as h:
                    h.queue_in()
                    h.compute_start()
                    time.sleep(0.001)  # ensure compute_ms > 0
                    h.compute_end()
                    h.send(bytes_out=1024)
                    time.sleep(0.001)
                    h.recv(bytes_in=512)
            r.flush()
            hop = json.loads(open(path).readline())["hops"][0]
            for key in ("stage_id", "node_id", "queue_in_at", "compute_start_at",
                        "compute_end_at", "send_start_at", "send_end_at",
                        "recv_start_at", "recv_end_at", "bytes_sent",
                        "bytes_recv", "wall_ms", "compute_ms", "transport_ms",
                        "queue_ms"):
                self.assertIn(key, hop, f"missing field: {key}")
            self.assertEqual(hop["bytes_sent"], 1024)
            self.assertEqual(hop["bytes_recv"], 512)
            self.assertGreater(hop["wall_ms"], 0)
            self.assertGreater(hop["compute_ms"], 0)
            self.assertGreaterEqual(hop["transport_ms"], 0)
        finally:
            os.unlink(path)

    def test_request_records_total_ttft(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            r = instrument.HopRecorder(enabled=True, sink_path=path)
            with r.trace_request("req-3") as req:
                time.sleep(0.01)
                with req.hop("s0", "n0") as h:
                    h.queue_in(); h.compute_start(); h.compute_end()
                    h.send(bytes_out=64); h.recv(bytes_in=32)
                time.sleep(0.005)
            r.flush()
            trace = json.loads(open(path).readline())
            self.assertIn("received_at", trace)
            self.assertIn("finished_at", trace)
            self.assertIn("ttft_ms", trace)
            self.assertGreater(trace["ttft_ms"], 10)
        finally:
            os.unlink(path)

    def test_zero_cost_when_disabled(self):
        # When disabled, the with-block context managers must not allocate
        # anything significant — measured by wall time of 10k no-op traces.
        r = instrument.HopRecorder(enabled=False)
        start = time.perf_counter()
        for _ in range(10000):
            with r.trace_request("x") as req:
                with req.hop("s", "n") as h:
                    h.queue_in(); h.compute_start(); h.compute_end()
                    h.send(0); h.recv(0)
        elapsed = time.perf_counter() - start
        # 10k traces must finish in well under 200 ms (~20 us each).
        self.assertLess(elapsed, 0.2, f"disabled recorder too slow: {elapsed:.3f}s")


class AnalyzerContract(unittest.TestCase):

    def test_summary_keys_present(self):
        from mycelium_router import analyze
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            # Two minimal synthetic traces.
            with open(path, "w") as fh:
                fh.write(json.dumps({
                    "request_id": "r1",
                    "received_at": 1000.0, "finished_at": 1001.5, "ttft_ms": 1500,
                    "hops": [
                        {"stage_id": "prefill", "node_id": "node-0",
                         "queue_in_at": 1000.0, "compute_start_at": 1000.1,
                         "compute_end_at": 1000.6, "send_start_at": 1000.61,
                         "send_end_at": 1000.62, "recv_start_at": 1001.10,
                         "recv_end_at": 1001.20, "bytes_sent": 4096,
                         "bytes_recv": 1024, "wall_ms": 1200, "compute_ms": 500,
                         "transport_ms": 100, "queue_ms": 100},
                        {"stage_id": "decode", "node_id": "node-1",
                         "queue_in_at": 1001.21, "compute_start_at": 1001.22,
                         "compute_end_at": 1001.45, "send_start_at": 1001.46,
                         "send_end_at": 1001.47, "recv_start_at": 1001.48,
                         "recv_end_at": 1001.49, "bytes_sent": 256,
                         "bytes_recv": 128, "wall_ms": 290, "compute_ms": 230,
                         "transport_ms": 30, "queue_ms": 10},
                    ],
                }) + "\n")
                fh.write(json.dumps({
                    "request_id": "r2",
                    "received_at": 2000.0, "finished_at": 2001.6, "ttft_ms": 1600,
                    "hops": [
                        {"stage_id": "prefill", "node_id": "node-0",
                         "queue_in_at": 2000.0, "compute_start_at": 2000.1,
                         "compute_end_at": 2000.65, "send_start_at": 2000.66,
                         "send_end_at": 2000.67, "recv_start_at": 2001.15,
                         "recv_end_at": 2001.25, "bytes_sent": 4096,
                         "bytes_recv": 1024, "wall_ms": 1250, "compute_ms": 550,
                         "transport_ms": 100, "queue_ms": 100},
                    ],
                }) + "\n")
            summary = analyze.summarize(path)
            for key in ("n_requests", "ttft_ms", "per_hop_stage_node",
                        "per_hop_breakdown_pct", "theoretical_overlap_floor_ms",
                        "sequential_floor_ms", "bottleneck_ranking"):
                self.assertIn(key, summary, f"missing key: {key}")
            self.assertEqual(summary["n_requests"], 2)
            # Two requests, prefill happens twice, decode once.
            self.assertEqual(summary["per_hop_stage_node"]["prefill@node-0"]["wall_ms"]["n"], 2)
            self.assertEqual(summary["per_hop_stage_node"]["decode@node-1"]["wall_ms"]["n"], 1)
            # Sequential floor = sum of per-hop wall_ms, theoretical = max per hop stage.
            self.assertGreater(summary["sequential_floor_ms"], 0)
            self.assertLessEqual(summary["theoretical_overlap_floor_ms"],
                                 summary["sequential_floor_ms"])
        finally:
            os.unlink(path)

    def test_report_emits_markdown_with_histogram(self):
        from mycelium_router import analyze
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            with open(path, "w") as fh:
                for i in range(20):
                    fh.write(json.dumps({
                        "request_id": f"r{i}",
                        "received_at": 1000.0 + i, "finished_at": 1001.0 + i,
                        "ttft_ms": 1000 + i * 5,
                        "hops": [{
                            "stage_id": "prefill", "node_id": "n0",
                            "queue_in_at": 1000.0, "compute_start_at": 1000.0,
                            "compute_end_at": 1000.5, "send_start_at": 1000.5,
                            "send_end_at": 1000.6, "recv_start_at": 1000.9,
                            "recv_end_at": 1001.0, "bytes_sent": 4096,
                            "bytes_recv": 1024, "wall_ms": 1000, "compute_ms": 500,
                            "transport_ms": 100, "queue_ms": 400,
                        }],
                    }) + "\n")
            md = analyze.render_markdown(analyze.summarize(path))
            for section in ("# Pipeline Analysis", "## Per-hop wall-time",
                           "## Bottleneck ranking", "## Sequential vs theoretical",
                           "## Recommendation"):
                self.assertIn(section, md, f"missing section: {section}")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
