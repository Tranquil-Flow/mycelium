# SPDX-License-Identifier: AGPL-3.0-only
"""One A10 verifier operation per product command, executed by the node's runtime.

The payload names one matrix operation or one required negative; the node's own
runtime opens the verifier and produces the record; the dispatcher accepts only a
record for exactly the operation it sent.  The runtime here is the real in-process
MLX target of ``mlx_case`` (never physical evidence).
"""

from __future__ import annotations

import pytest

from mycelium_router.a10_product_verifier import (
   PRODUCT_COMMAND,
   A10ProductVerifierError,
   execute_product_operation,
   operation_payload,
   unwrap_product_result,
)
from mycelium_router.a10_qualification import (
   REQUIRED_VERIFIER_NEGATIVES,
   VERIFIER_OPERATION_PROTOCOL,
   validate_verifier_operation,
)
from mycelium_router.a10_verifier_matrix import (
   VERIFIER_NEGATIVE_ORDER,
   MatrixPlan,
   run_matrix_operation,
)
from tests.a10_deterministic.test_runtime_target_verifier import mlx_case  # noqa: F401

BINDING = {
   "runtime_digest": "sha256:" + "1" * 64,
   "model_digest": "sha256:" + "2" * 64,
   "representation_digest": "sha256:" + "3" * 64,
   "backend": "mlx",
}


class ColocatedRuntime:
   """The node runtime surface the handler uses, over the split in-process target."""

   def __init__(self, case) -> None:
      self.ports = case.ports
      self.stages = case.ports[0].a10_verifier_stages() + case.ports[1].a10_verifier_stages()

   def begin_target_verifier(self, prefix, *, verification_ref, stages=None):
      assert stages is None  # the node never supplies foreign stages
      return self.ports[0].begin_target_verifier(
         prefix, verification_ref=verification_ref, stages=self.stages)

   def cancel(self, path_id):
      for port in self.ports:
         port.cancel(path_id)


@pytest.fixture(scope="module")
def runtime(mlx_case):
   return ColocatedRuntime(mlx_case)


@pytest.fixture(scope="module")
def plan(runtime):
   return MatrixPlan(
      widths={"one": 1, "qualified_default": 3, "qualified_maximum": 6},
      prefix_buckets={"short": (2, 3), "long": (7, 8)},
      operations=12,
      vocab_size=runtime.stages[0].vocab_size,
   )


def test_the_command_name_is_the_one_the_route_forwards():
   assert PRODUCT_COMMAND == "a10_target_verify"


def test_each_product_operation_matches_the_matrix_operation_but_is_product_dispatched(runtime, plan):
   # Direct operations only: a cancelled verifier path stays cancelled in the
   # runtime, so the negatives are executed once, through the product command.
   direct = [
      run_matrix_operation(
         plan, index,
         open_verifier=lambda prefix, ref: runtime.begin_target_verifier(prefix, verification_ref=ref),
         provenance="fixture", execution_path="direct_runtime", **BINDING)
      for index in range(plan.operations)
   ]
   for index in range(plan.operations):
      payload = operation_payload(plan, index=index, **BINDING)
      result = execute_product_operation(runtime, payload)
      kind, record = unwrap_product_result(payload, result)
      assert kind == "operation"
      assert validate_verifier_operation(record) == record
      assert record["execution_path"] == "product_dispatcher"
      assert record["provenance"] == "live"
      expected = {**direct[index], "execution_path": "product_dispatcher", "provenance": "live"}
      assert record == expected
   for case_id in VERIFIER_NEGATIVE_ORDER:
      payload = operation_payload(plan, case_id=case_id, **BINDING)
      kind, negative = unwrap_product_result(payload, execute_product_operation(runtime, payload))
      assert kind == "negative"
      assert negative["case_id"] == case_id
      assert negative["refusal_code"] == REQUIRED_VERIFIER_NEGATIVES[case_id]
      assert negative["prefix_unchanged"] is True and negative["open_transactions"] == 0


@pytest.mark.parametrize("mutate, code", [
   (lambda p: p.update(protocol="mycelium.other.v1"), "a10_product_payload_invalid"),
   (lambda p: p.update(extra=1), "a10_product_payload_invalid"),
   (lambda p: p.update(kind="batch"), "a10_product_payload_invalid"),
   (lambda p: p.update(index=99), "invalid_matrix_operation_index"),
   (lambda p: p.update(index=True), "a10_product_payload_invalid"),
   (lambda p: p["binding"].update(runtime_digest="sha256:short"), "a10_product_payload_invalid"),
   (lambda p: p["binding"].update(backend=""), "a10_product_payload_invalid"),
   (lambda p: p["plan"].update(widths={"one": 1}), "invalid_matrix_widths"),
])
def test_the_node_refuses_a_malformed_operation_before_opening_a_verifier(runtime, plan, mutate, code):
   payload = operation_payload(plan, index=0, **BINDING)
   mutate(payload)
   with pytest.raises(Exception, match=code):
      execute_product_operation(runtime, payload)


def test_the_node_refuses_an_unknown_negative(runtime, plan):
   payload = operation_payload(plan, case_id="stale_prefix", **BINDING)
   payload["case_id"] = "invented"
   with pytest.raises(A10ProductVerifierError, match="a10_product_payload_invalid"):
      execute_product_operation(runtime, payload)


def test_a_runtime_without_a_verifier_is_refused(plan):
   payload = operation_payload(plan, index=0, **BINDING)
   with pytest.raises(A10ProductVerifierError, match="a10_target_verifier_unavailable"):
      execute_product_operation(object(), payload)


def test_the_dispatcher_accepts_only_the_record_for_the_operation_it_sent(runtime, plan):
   payload = operation_payload(plan, index=1, **BINDING)
   record = execute_product_operation(runtime, payload)
   other = execute_product_operation(runtime, operation_payload(plan, index=2, **BINDING))
   negative_payload = operation_payload(plan, case_id="position_bound", **BINDING)
   envelope = execute_product_operation(runtime, negative_payload)
   bad = [
      other,
      {**record, "execution_path": "direct_runtime"},
      {**record, "provenance": "fixture"},
      {**record, "runtime_digest": "sha256:" + "9" * 64},
      {key: value for key, value in record.items() if key != "parity"},
      envelope,
   ]
   for result in bad:
      with pytest.raises(A10ProductVerifierError, match="a10_product_result_invalid"):
         unwrap_product_result(payload, result)
   for result in [
      record,
      {**envelope, "extra": True},
      {**envelope, "negative": {**envelope["negative"], "case_id": "stale_prefix"}},
      {**envelope, "protocol": "mycelium.other.v1"},
   ]:
      with pytest.raises(A10ProductVerifierError, match="a10_product_result_invalid"):
         unwrap_product_result(negative_payload, result)
   assert envelope["protocol"] == VERIFIER_OPERATION_PROTOCOL
   assert envelope["execution_path"] == "product_dispatcher"
