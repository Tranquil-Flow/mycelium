#!/usr/bin/env python3
import unittest
from pathlib import Path

import planner_simulator as sim


SCENARIO_PATH = Path(__file__).parent / "scenarios" / "hypothetical-six-node.json"


class PlannerSimulatorTests(unittest.TestCase):
   @classmethod
   def setUpClass(cls):
      cls.scenario = sim.load_scenario(SCENARIO_PATH)

   def test_scenario_uses_quarter_context_with_safety_multiplier(self):
      self.assertEqual(self.scenario.workload.prompt_tokens(), 1024)
      self.assertEqual(self.scenario.workload.planned_kv_tokens(), 1280)
      self.assertEqual(len(self.scenario.links), 30)

   def test_discrete_device_can_execute_overflow_layers_from_ram(self):
      node = sim.NodeSpec(
         node_id="small-vram",
         gpu_tflops=10,
         cpu_tflops=1,
         vram_available_gb=0.75,
         ram_available_gb=8,
         gpu_memory_bandwidth_gbps=300,
         ram_bandwidth_gbps=50,
         vram_ram_bandwidth_gbps=20,
         workspace_gb=0.25,
      )
      estimate = sim.estimate_stage(node, self.scenario.model, self.scenario.workload, 4)
      self.assertIsNotNone(estimate)
      self.assertGreater(estimate.ram_layer_count, 0)
      self.assertGreater(estimate.ram_used_gb, 0)
      self.assertIn(estimate.ram_execution, {"cpu", "gpu_stream"})

   def test_plan_is_infeasible_when_vram_and_ram_cannot_hold_stage(self):
      node = sim.NodeSpec(
         node_id="too-small",
         gpu_tflops=10,
         cpu_tflops=1,
         vram_available_gb=0.3,
         ram_available_gb=0.1,
         gpu_memory_bandwidth_gbps=300,
         ram_bandwidth_gbps=50,
         vram_ram_bandwidth_gbps=20,
         workspace_gb=0.25,
      )
      self.assertIsNone(sim.estimate_stage(node, self.scenario.model, self.scenario.workload, 4))

   def test_shortest_ring_contains_every_supplied_node(self):
      order = sim.shortest_ring(self.scenario, self.scenario.nodes)
      self.assertEqual(set(order), set(self.scenario.nodes))
      self.assertEqual(len(order), len(self.scenario.nodes))

   def test_plan_reports_contiguous_layers_prefill_decode_and_single_request(self):
      order = sim.shortest_ring(self.scenario, self.scenario.nodes)
      plan = sim.build_plan(self.scenario, order, strategy="test")
      self.assertIsNotNone(plan)
      self.assertGreater(plan["estimated_prefill_tokens_s"], 0)
      self.assertGreater(plan["estimated_decode_tokens_s"], 0)
      self.assertGreater(plan["estimated_single_request_tokens_s"], 0)
      expected_start = 0
      for stage in plan["route"]:
         self.assertEqual(stage["layers"][0], expected_start)
         expected_start = stage["layers"][1] + 1
      self.assertEqual(expected_start, self.scenario.model.num_layers)

   def test_single_request_topology_is_not_worse_than_network_shortest_topology(self):
      shortest_order = sim.shortest_ring(self.scenario, self.scenario.nodes)
      throughput_order = sim.single_request_throughput_ring(self.scenario, self.scenario.nodes)
      shortest_plan = sim.build_plan(self.scenario, shortest_order, strategy="shortest")
      throughput_plan = sim.build_plan(self.scenario, throughput_order, strategy="throughput")
      self.assertGreaterEqual(
         throughput_plan["estimated_single_request_tokens_s"],
         shortest_plan["estimated_single_request_tokens_s"],
      )

   def test_throughput_pruning_preserves_dropped_node_original_stage_signature(self):
      initial_order = sim.shortest_ring(self.scenario, self.scenario.nodes)
      initial = sim.build_plan(self.scenario, initial_order, strategy="initial")
      initial_by_node = {item["node_id"]: item for item in initial["route"]}
      pruned = sim.prune_throughput_nodes(self.scenario, initial, reoptimize_ring=False)
      self.assertGreater(pruned["estimated_combined_tokens_s"], initial["estimated_combined_tokens_s"])
      members = pruned["secondary_structure"]["members"]
      self.assertGreater(len(members), 0)
      for member in members:
         original = initial_by_node[member["node_id"]]
         self.assertEqual(member["original_layers"], original["layers"])
         self.assertEqual(member["stage_signature"], original["stage_signature"])
         self.assertFalse(member["runtime_capacity_reserved"])
         self.assertGreater(len(member["shared_primary_node_ids"]), 0)
      self.assertFalse(pruned["secondary_structure"]["executable_complete_ring"])
      self.assertEqual(pruned["secondary_structure"]["capacity_reservation_fraction"], 0.0)
      self.assertEqual(pruned["secondary_structure"]["router_policy"], "out_of_scope")
      self.assertEqual(
         len(pruned["secondary_structure"]["shared_primary_bottlenecks"]),
         len(pruned["node_order"]),
      )

   def test_large_ring_has_no_configured_device_count_cap(self):
      nodes = {
         f"n{index}": sim.NodeSpec(
            node_id=f"n{index}",
            gpu_tflops=5,
            cpu_tflops=1,
            vram_available_gb=2,
            ram_available_gb=4,
            gpu_memory_bandwidth_gbps=100,
            ram_bandwidth_gbps=40,
            vram_ram_bandwidth_gbps=10,
         )
         for index in range(13)
      }
      links = {}
      for src in nodes:
         for dst in nodes:
            if src != dst:
               links[(src, dst)] = sim.LinkSpec(src, dst, 2, 0.1, 500)
      scenario = sim.Scenario("large", self.scenario.model, self.scenario.workload, nodes, links)
      order = sim.shortest_ring(scenario, nodes)
      self.assertEqual(len(order), 13)
      self.assertEqual(set(order), set(nodes))

   def test_benchmark_compares_multiple_strategies(self):
      report = sim.benchmark_scenario(self.scenario, include_global_subset=True)
      self.assertTrue(report["ok"])
      self.assertEqual(len(report["ranking"]), 6)
      self.assertIn("throughput_pruned_local", report["strategies"])
      self.assertIn("single_request_throughput_all", report["strategies"])
      self.assertIn("global_best_shortest_subset", report["strategies"])


if __name__ == "__main__":
   unittest.main()
