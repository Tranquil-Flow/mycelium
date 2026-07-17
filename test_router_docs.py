import json
import unittest
from pathlib import Path

from mycelium_router.serialization import execution_graph_from_dict
from mycelium_router.wire import decode_frame


ROOT = Path(__file__).parent


class RouterDocumentationTests(unittest.TestCase):
   def test_protocol_example_parses_through_production_contract(self):
      path = ROOT / "docs" / "router-protocol-example.json"
      payload = json.loads(path.read_text())
      graph = execution_graph_from_dict(payload)
      self.assertEqual(graph.protocol, "mycelium.execution_graph.v1")

   def test_request_usability_plan_covers_streaming_and_kv_lifecycle(self):
      text = (
         ROOT
         / "docs"
         / "plans"
         / "2026-07-16-request-streaming-session-lifecycle-mvp.md"
      ).read_text()
      for required in (
         "SessionLifecycleManager",
         "RuntimePort.release_kv",
         "text/event-stream",
         "slow_consumer",
         "complete chat",
         "separate fault-tolerance track",
      ):
         self.assertIn(required, text)

   def test_wire_example_parses_through_production_codec(self):
      path = ROOT / "docs" / "router-wire-example.hex"
      decoded = decode_frame(bytes.fromhex(path.read_text().strip()))
      self.assertEqual(decoded.message.path_id, "path-example")
      self.assertEqual(decoded.payload, b"activation-example")

   def test_handover_states_proof_and_integration_boundaries(self):
      text = (ROOT / "ROUTER_HANDOVER.md").read_text()
      for required in (
         "94 Router tests",
         "mycelium.execution_graph.v1",
         "half-open",
         "distributed progressive prefill",
         "opt-in chunked prefill",
         "prefill_chunk_size_tokens",
         "continuous batching",
         "MVP exclusions",
         "v1 optimization",
         "loopback TCP",
         "separate fault-tolerance track",
         "distributed dropout recovery",
         "simulator adapter",
         "real OS sockets",
         "not yet a process-isolated or multi-host production runtime",
      ):
         self.assertIn(required, text)


if __name__ == "__main__":
   unittest.main()
