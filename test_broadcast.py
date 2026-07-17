#!/usr/bin/env python3
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import mycelium_broadcast as mb


class BroadcastTests(unittest.TestCase):
    def sample_profile(self, hostname="node-a"):
        return {
            "hostname": hostname,
            "platform": "Linux",
            "device_class": "desktop",
            "arch": "x86_64",
            "cpu": {"name": "Test CPU", "cores": 8},
            "ram": {"total_gb": 16, "available_gb": 10, "bandwidth_gbps": 80},
            "gpus": [],
            "storage": {"total_gb": 500, "available_gb": 300, "type": "nvme"},
            "power": {"on_ac_power": True, "battery_pct": None},
            "network": {"download_mbps": 100, "upload_mbps": 50},
            "location": {"lat": 52.49, "lon": 13.36, "city": "Berlin", "country": "Germany"},
            "backends": ["torch_cpu"],
            "supported_precision": ["fp32"],
            "timestamp": "2026-07-14T08:00:00+0000",
        }

    def test_normalize_profile_adds_router_fields(self):
        profile = mb.normalize_profile(
            self.sample_profile("node-a"),
            advertised_host="10.0.0.2",
            port=8788,
        )

        self.assertEqual(profile["protocol"], "mycelium.node_profile.v1")
        self.assertEqual(profile["node_id"], "node-a")
        self.assertEqual(profile["profile_url"], "http://10.0.0.2:8788/profile")
        self.assertIn("profile_hash", profile)
        self.assertEqual(profile["capabilities"]["ram_available_gb"], 10)
        self.assertEqual(profile["capabilities"]["upload_mbps"], 50)

    def test_registry_stores_profiles_and_expires_stale_nodes(self):
        registry = mb.PeerRegistry(ttl_seconds=0.05)
        profile = mb.normalize_profile(self.sample_profile("node-a"), "127.0.0.1", 8788)
        registry.upsert(profile, source="unit-test")

        peers = registry.snapshot()
        self.assertEqual(list(peers.keys()), ["node-a"])
        self.assertEqual(peers["node-a"]["source"], "unit-test")

        time.sleep(0.08)
        self.assertEqual(registry.snapshot(), {})

    def test_hello_roundtrip_contains_only_discovery_pointer(self):
        hello = mb.make_hello(node_id="node-a", profile_url="http://10.0.0.2:8788/profile", profile_hash="abc")
        parsed = mb.parse_hello(hello)

        self.assertEqual(parsed["protocol"], "mycelium.hello.v1")
        self.assertEqual(parsed["node_id"], "node-a")
        self.assertEqual(parsed["profile_url"], "http://10.0.0.2:8788/profile")
        self.assertEqual(parsed["profile_hash"], "abc")
        self.assertNotIn("ram", parsed)
        self.assertNotIn("gpus", parsed)

    def test_http_server_accepts_announce_and_serves_peers(self):
        own_profile = mb.normalize_profile(self.sample_profile("router"), "127.0.0.1", 0)
        registry = mb.PeerRegistry(ttl_seconds=60)
        server = mb.start_http_server("127.0.0.1", 0, own_profile, registry)
        try:
            host, port = server.server_address
            peer = mb.normalize_profile(self.sample_profile("peer-a"), "127.0.0.1", 9999)
            body = json.dumps({"profile": peer}).encode("utf-8")
            req = urllib.request.Request(
                f"http://{host}:{port}/announce",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read())["ok"], True)

            with urllib.request.urlopen(f"http://{host}:{port}/peers", timeout=5) as resp:
                peers = json.loads(resp.read())["peers"]

            self.assertIn("peer-a", peers)
            self.assertEqual(peers["peer-a"]["profile"]["capabilities"]["ram_total_gb"], 16)
        finally:
            server.shutdown()
            server.server_close()

    def test_seed_announce_posts_profile_to_seed(self):
        seed_profile = mb.normalize_profile(self.sample_profile("seed"), "127.0.0.1", 0)
        registry = mb.PeerRegistry(ttl_seconds=60)
        server = mb.start_http_server("127.0.0.1", 0, seed_profile, registry)
        try:
            host, port = server.server_address
            profile = mb.normalize_profile(self.sample_profile("node-b"), "127.0.0.1", 9998)
            result = mb.announce_to_seed(profile, f"http://{host}:{port}")
            self.assertTrue(result["ok"])
            self.assertIn("node-b", registry.snapshot())
        finally:
            server.shutdown()
            server.server_close()

    def test_nodes_endpoint_returns_self_plus_peers_for_router(self):
        own_profile = mb.normalize_profile(self.sample_profile("router"), "127.0.0.1", 0)
        registry = mb.PeerRegistry(ttl_seconds=60)
        server = mb.start_http_server("127.0.0.1", 0, own_profile, registry)
        try:
            host, port = server.server_address
            peer = mb.normalize_profile(self.sample_profile("peer-a"), "127.0.0.1", 9999)
            registry.upsert(peer, source="unit-test")

            with urllib.request.urlopen(f"http://{host}:{port}/nodes", timeout=5) as resp:
                nodes = json.loads(resp.read())["nodes"]

            self.assertEqual(set(nodes.keys()), {"router", "peer-a"})
            self.assertEqual(nodes["router"]["role"], "self")
            self.assertEqual(nodes["peer-a"]["role"], "peer")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
