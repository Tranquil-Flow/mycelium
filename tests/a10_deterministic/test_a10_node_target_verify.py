# SPDX-License-Identifier: AGPL-3.0-only
"""The node executes ``a10_target_verify`` on its own target runtime, fail-closed.

The command runs only on a RUNNING node, through the node's own loaded runtime,
and answers with that runtime's verification record.  Any refusal is a named
node error; nothing is synthesized.  The positive case drives the real in-process
MLX target of ``mlx_case`` through ``PhysicalNodeService.dispatch`` (never
physical evidence).
"""

from __future__ import annotations

import pytest

import physical_inference_node
from mycelium_router.a10_product_verifier import operation_payload, unwrap_product_result
from mycelium_router.a10_verifier_matrix import MatrixPlan
from physical_inference_node import NODE_CONTROL_PROTOCOL, NodeCommandError, PhysicalNodeService
from tests.a10_deterministic.test_a10_product_verifier import BINDING, ColocatedRuntime
from tests.a10_deterministic.test_runtime_target_verifier import mlx_case  # noqa: F401


def _service(runtime, *, state="RUNNING") -> PhysicalNodeService:
   service = object.__new__(PhysicalNodeService)
   service.state = state
   service.runtime = runtime
   service.run_id = "run-a10"
   service.deployment_id = "deployment-a10"
   return service


def _command(payload) -> dict:
   return {"protocol": NODE_CONTROL_PROTOCOL, "command_id": "cmd-a10", "run_id": "run-a10",
           "deployment_id": "deployment-a10", "command": "a10_target_verify",
           "payload": payload}


def _plan(vocab_size: int = 32) -> MatrixPlan:
   return MatrixPlan(widths={"one": 1, "qualified_default": 3, "qualified_maximum": 6},
                     prefix_buckets={"short": (2, 3), "long": (7, 8)},
                     operations=6, vocab_size=vocab_size)


def test_a_running_node_answers_with_its_own_runtime_record(mlx_case):
   runtime = ColocatedRuntime(mlx_case)
   plan = _plan(runtime.stages[0].vocab_size)
   payload = operation_payload(plan, index=4, **BINDING)
   result = _service(runtime).dispatch(_command(payload))
   kind, record = unwrap_product_result(payload, result)
   assert kind == "operation"
   assert record["execution_path"] == "product_dispatcher"
   assert record["operation_ref"] == "a10-matrix-0004"


@pytest.mark.parametrize("state", ["CONFIGURED", "STOPPING", "STOPPED"])
def test_a_node_that_is_not_running_refuses(state):
   payload = operation_payload(_plan(), index=0, **BINDING)
   with pytest.raises(NodeCommandError, match="invalid_state_for_a10_target_verify"):
      _service(object(), state=state).dispatch(_command(payload))


def test_a_node_without_a_loaded_target_verifier_refuses():
   payload = operation_payload(_plan(), index=0, **BINDING)
   with pytest.raises(NodeCommandError, match="a10_target_verifier_unavailable"):
      _service(object()).dispatch(_command(payload))


def test_a_malformed_operation_is_a_named_node_error():
   with pytest.raises(NodeCommandError, match="a10_product_payload_invalid"):
      _service(object()).dispatch(_command({"protocol": "mycelium.other.v1"}))


def test_a_runtime_failure_is_named_without_its_exception_text(monkeypatch):
   class Boom(Exception):
      code = "path_cancelled"

   def execute(_runtime, _payload):
      raise Boom("private detail")

   monkeypatch.setattr(physical_inference_node, "_a10_execute_product_operation", execute)
   payload = operation_payload(_plan(), index=0, **BINDING)
   with pytest.raises(NodeCommandError) as caught:
      _service(object()).dispatch(_command(payload))
   assert str(caught.value) == "a10_target_verify_failed:path_cancelled"

   def crash(_runtime, _payload):
      raise RuntimeError("private detail")

   monkeypatch.setattr(physical_inference_node, "_a10_execute_product_operation", crash)
   with pytest.raises(NodeCommandError) as caught:
      _service(object()).dispatch(_command(payload))
   assert str(caught.value) == "a10_target_verify_failed"
