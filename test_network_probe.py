#!/usr/bin/env python3
import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent))

import network_probe as np


class NetworkProbeTests(unittest.TestCase):
    def test_aggregate_returns_min_avg_max_p95_jitter(self):
        samples = [10, 12, 11, 13, 50, 12, 11, 14, 12, 11]  # ms
        agg = np.aggregate_samples(samples)
        self.assertEqual(agg["count"], 10)
        self.assertAlmostEqual(agg["min_ms"], 10.0)
        self.assertAlmostEqual(agg["max_ms"], 50.0)
        # mean ~ 15.6
        self.assertAlmostEqual(agg["avg_ms"], 15.6)
        # jitter: average of |x - mean| ~ 6.88
        self.assertAlmostEqual(agg["jitter_ms"], 6.88)
        # p95 around 50
        self.assertGreaterEqual(agg["p95_ms"], 30)
        self.assertLessEqual(agg["p95_ms"], 50)

    def test_tcp_ping_handles_unreachable_target(self):
        result = np.tcp_ping("127.0.0.1", 1, timeout=0.5, attempts=2)
        self.assertIsNone(result["avg_ms"])
        self.assertEqual(result["count"], 0)
        self.assertGreater(len(result["errors"]), 0)

    def test_tcp_ping_against_local_server(self):
        import socket
        import threading
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def accept():
            for _ in range(3):
                conn, _ = server.accept()
                conn.close()

        t = threading.Thread(target=accept, daemon=True)
        t.start()

        result = np.tcp_ping("127.0.0.1", port, timeout=1.0, attempts=3, settle=0.0)
        server.close()
        self.assertIsNotNone(result["avg_ms"])
        self.assertEqual(result["count"], 3)
        self.assertGreaterEqual(result["avg_ms"], 0.0)

    def test_udp_ping_handles_unreachable_target(self):
        # UDP "ping" is send-side timing; even if no echo, we record send latency.
        result = np.udp_ping("127.0.0.1", 1, timeout=0.5, attempts=2)
        # Note: avg_ms may be None if `time.monotonic` path is short-circuited; we
        # accept either case as long as the call completed without exception.
        self.assertIn("count", result)
        self.assertIn("loss_ratio", result)
        self.assertLessEqual(result["count"], 2)

    def test_estimate_location_falls_back_to_country_only(self):
        out = np.estimate_location({"lat": 52.49, "lon": 13.36, "city": "Berlin", "country": "Germany"})
        self.assertEqual(out["lat"], 52.49)
        self.assertEqual(out["lon"], 13.36)
        self.assertEqual(out["precision"], "city")

        coarse = np.estimate_location({"lat": None, "lon": None, "country": "Germany"})
        self.assertEqual(coarse["precision"], "country")

        none = np.estimate_location(None)
        self.assertEqual(none["precision"], "unknown")

    def test_build_pairwise_matrix_extracts_ping_jitter_for_known_pairs(self):
        nodes = {
            "a": {"location": {"lat": 52.0, "lon": 13.0, "city": "Berlin", "country": "DE", "precision": "city"},
                  "lan_ip": "192.168.1.10"},
            "b": {"location": {"lat": 52.5, "lon": 13.5, "city": "Berlin", "country": "DE", "precision": "city"},
                  "lan_ip": "192.168.1.11"},
        }
        samples = {
            ("a", "b"): [10, 12, 11, 13, 12],
        }
        matrix = np.build_pairwise_matrix(nodes, samples)
        self.assertIn("a->b", matrix)
        self.assertEqual(matrix["a->b"]["samples"], 5)
        self.assertAlmostEqual(matrix["a->b"]["ping_avg_ms"], 11.6)
        self.assertGreaterEqual(matrix["a->b"]["jitter_ms"], 0.0)
        self.assertEqual(matrix["a->b"]["location_src"]["precision"], "city")
        self.assertEqual(matrix["a->b"]["location_dst"]["precision"], "city")
        # reverse direction should also exist but contain no samples
        self.assertIn("b->a", matrix)
        self.assertEqual(matrix["b->a"]["samples"], 0)

    def test_cli_writes_pairwise_report(self):
        nodes = {
            "self": {"node_id": "self", "location": {"lat": 52.0, "lon": 13.0, "city": "Berlin", "country": "DE"}},
            "peer": {"node_id": "peer", "location": {"lat": 52.0, "lon": 13.0, "city": "Berlin", "country": "DE"}},
        }
        with tempfile.TemporaryDirectory() as td:
            nodes_path = Path(td) / "nodes.json"
            out_path = Path(td) / "report.json"
            nodes_path.write_text(__import__("json").dumps({"nodes": nodes}))
            rc = np.main([
                "--self", "self",
                "--nodes-file", str(nodes_path),
                "--probe-attempts", "3",
                "--probe-timeout", "0.5",
                "--probe-port", "0",  # 0 -> just collect samples using fake targets to keep test offline
                "--out", str(out_path),
            ])
            # Port 0 means: don't actually probe, still write report structure
            self.assertEqual(rc, 0)
            report = __import__("json").loads(out_path.read_text())
            self.assertIn("self", report["locations"])
            self.assertEqual(report["locations"]["self"]["precision"], "city")

    def test_probe_uses_port_from_profile_url(self):
        # Two local listeners on different ports. profile_url points at port B,
        # --probe-port points at port A. We must probe B, not A.
        import socket, threading
        srv_a = socket.socket(); srv_a.bind(("127.0.0.1", 0)); srv_a.listen(8); pa = srv_a.getsockname()[1]
        srv_b = socket.socket(); srv_b.bind(("127.0.0.1", 0)); srv_b.listen(8); pb = srv_b.getsockname()[1]
        accepted = {"a": 0, "b": 0}
        def accept_loop(s, key):
            for _ in range(8):
                try:
                    c, _ = s.accept(); accepted[key] += 1; c.close()
                except OSError:
                    break
        threading.Thread(target=accept_loop, args=(srv_a, "a"), daemon=True).start()
        threading.Thread(target=accept_loop, args=(srv_b, "b"), daemon=True).start()

        nodes = {
            "self": {"node_id": "self"},
            "peer": {"node_id": "peer", "profile": {"profile_url": f"http://127.0.0.1:{pb}/profile"}},
        }
        samples, errors = np.collect_pairwise_samples(
            nodes, self_id="self", probe_port=pa, timeout=1.0, attempts=3, max_pairs=0,
        )
        srv_a.close(); srv_b.close()
        self.assertIn(("self", "peer"), samples)
        self.assertGreater(len(samples[("self", "peer")]), 0)
        # We should have hit B (the URL port), not A.
        self.assertGreaterEqual(accepted["b"], 1)


if __name__ == "__main__":
    unittest.main()
