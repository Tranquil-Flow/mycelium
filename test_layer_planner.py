#!/usr/bin/env python3
import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import layer_planner as lp


class LayerPlannerTests(unittest.TestCase):
    def model(self, layers=12, hidden=1024):
        return lp.ModelSpec(
            model_id="test-model",
            num_layers=layers,
            hidden_size=hidden,
            weight_bytes=2,
            activation_bytes=2,
            context_length=2048,
            batch_size=1,
            min_memory_reserve_gb=1.0,
            required_backends=["mlx", "torch_mps", "cuda", "llama_cpp"],
        )

    def node(self, node_id, *, ram=24, avail=20, bw=200, backend="mlx", device="desktop", gpu="Test GPU", ac=True, lat=52.49, lon=13.36):
        return {
            "node_id": node_id,
            "role": "peer",
            "profile": {
                "node_id": node_id,
                "protocol": "mycelium.node_profile.v1",
                "location": {"lat": lat, "lon": lon, "city": "Test"},
                "capabilities": {
                    "device_class": device,
                    "platform": "Darwin" if backend == "mlx" else "Linux",
                    "arch": "arm64",
                    "ram_total_gb": ram,
                    "ram_available_gb": avail,
                    "ram_bandwidth_gbps": bw,
                    "unified_memory": True,
                    "gpu_count": 1 if gpu else 0,
                    "primary_gpu_name": gpu,
                    "primary_gpu_backend": "metal" if backend == "mlx" else backend,
                    "vram_total_gb": None,
                    "vram_available_gb": None,
                    "vram_bandwidth_gbps": bw,
                    "storage_available_gb": 100,
                    "storage_type": "nvme",
                    "on_ac_power": ac,
                    "battery_pct": 100 if ac else 50,
                    "download_mbps": 100,
                    "upload_mbps": 100,
                    "lan_ip": None,
                    "backends": [backend] if backend else [],
                    "supported_precision": ["fp16", "fp32", "int8"],
                },
            },
        }

    def nodes_doc(self, records):
        return {"ok": True, "nodes": {r["node_id"]: r for r in records}}

    def test_single_eligible_node_receives_all_layers(self):
        plan = lp.plan_layer_allocation(self.nodes_doc([self.node("m4pro")]), self.model(layers=12))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["route"][0]["node_id"], "m4pro")
        self.assertEqual(plan["route"][0]["layers"], [0, 11])
        self.assertEqual(plan["route"][0]["layer_count"], 12)

    def test_phone_is_layer_eligible_by_default(self):
        phone = self.node("pixel", avail=8, bw=None, backend="llama_cpp", device="phone", gpu="Mali")
        plan = lp.plan_layer_allocation(self.nodes_doc([phone]), self.model(layers=4))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["route"][0]["node_id"], "pixel")

    def test_phone_layers_can_be_disabled_explicitly(self):
        phone = self.node("pixel", avail=8, bw=None, backend="llama_cpp", device="phone", gpu="Mali")
        plan = lp.plan_layer_allocation(
            self.nodes_doc([phone]),
            self.model(layers=4),
            allow_phone_layers=False,
        )
        self.assertFalse(plan["ok"])
        self.assertIn("phone_layers_disabled", plan["ineligible"]["pixel"])

    def test_two_equal_nodes_split_contiguous_layers(self):
        nodes = self.nodes_doc([self.node("a", bw=200), self.node("b", bw=200)])
        plan = lp.plan_layer_allocation(nodes, self.model(layers=12), max_nodes=2)
        self.assertTrue(plan["ok"])
        route = plan["route"]
        self.assertEqual(len(route), 2)
        self.assertEqual(route[0]["layers"], [0, 5])
        self.assertEqual(route[1]["layers"], [6, 11])

    def test_memory_cap_prevents_over_allocation_to_small_node(self):
        small = self.node("small", avail=1.05, bw=500)
        large = self.node("large", avail=40, bw=100)
        spec = self.model(layers=8, hidden=4096)
        # Force a large per-layer footprint so small node cannot fit any layer after reserve.
        spec.layer_weight_gb = 1.0
        plan = lp.plan_layer_allocation(self.nodes_doc([small, large]), spec, max_nodes=2)
        self.assertTrue(plan["ok"])
        self.assertEqual([r["node_id"] for r in plan["route"]], ["large"])
        self.assertIn("insufficient_memory", plan["ineligible"]["small"])

    def test_faster_node_gets_more_layers(self):
        fast = self.node("fast", bw=400)
        slow = self.node("slow", bw=100)
        plan = lp.plan_layer_allocation(self.nodes_doc([fast, slow]), self.model(layers=10), max_nodes=2)
        self.assertTrue(plan["ok"])
        counts = {r["node_id"]: r["layer_count"] for r in plan["route"]}
        self.assertGreater(counts["fast"], counts["slow"])

    def test_cli_writes_route_plan_from_nodes_file(self):
        nodes = self.nodes_doc([self.node("m4pro")])
        with tempfile.TemporaryDirectory() as td:
            nodes_path = Path(td) / "nodes.json"
            out_path = Path(td) / "route.json"
            nodes_path.write_text(json.dumps(nodes))
            with contextlib.redirect_stdout(io.StringIO()):
                rc = lp.main([
                    "--nodes-file", str(nodes_path),
                    "--model-id", "test-model",
                    "--num-layers", "6",
                    "--hidden-size", "512",
                    "--out", str(out_path),
                ])
            self.assertEqual(rc, 0)
            saved = json.loads(out_path.read_text())
            self.assertTrue(saved["ok"])
            self.assertEqual(saved["route"][0]["layers"], [0, 5])


if __name__ == "__main__":
    unittest.main()
