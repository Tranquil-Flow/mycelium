import copy
import unittest

from mycelium_router.serialization import (
   execution_graph_from_dict,
   execution_graph_to_dict,
)
from mycelium_router.validation import ContractError
from test_router_contracts import graph_fixture


class ExecutionGraphSerializationTests(unittest.TestCase):
   def test_execution_graph_v1_round_trips_without_range_conversion(self):
      graph = graph_fixture()
      payload = execution_graph_to_dict(graph)
      self.assertIn("end_layer_exclusive", payload["stages"][0]["range"])
      self.assertNotIn("end_layer", payload["stages"][0]["range"])
      self.assertEqual(execution_graph_from_dict(payload), graph)

   def test_unknown_protocol_version_fails_closed(self):
      payload = execution_graph_to_dict(graph_fixture())
      payload["protocol"] = "mycelium.execution_graph.v2"
      with self.assertRaisesRegex(ContractError, "unsupported_graph_protocol"):
         execution_graph_from_dict(payload)

   def test_inclusive_range_shape_is_not_silently_reinterpreted(self):
      payload = execution_graph_to_dict(graph_fixture())
      changed = copy.deepcopy(payload)
      layer_range = changed["stages"][0]["range"]
      layer_range["end_layer"] = layer_range.pop("end_layer_exclusive") - 1
      with self.assertRaisesRegex(ContractError, "missing_contract_field"):
         execution_graph_from_dict(changed)


if __name__ == "__main__":
   unittest.main()
