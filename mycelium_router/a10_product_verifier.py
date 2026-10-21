# SPDX-License-Identifier: AGPL-3.0-only
"""One A10 target-verifier operation per product command.

The product dispatcher sends one bounded operation (one matrix operation or one
required negative, specification section 10 physical positive gate 4) to the
node that hosts the exact target; the node's own runtime opens the verifier and
produces the record (``execute_product_operation``).  The dispatcher accepts only
the record for exactly the operation it sent (``unwrap_product_result``).

Records carry ``provenance = live`` and ``execution_path = product_dispatcher``
because they are produced only inside the node's product command.  A negative has
no operation-record shape of its own; it travels in an envelope under the
operation-record protocol (the only protocol the route forwards) and is unwrapped
strictly back to the decider's closed negative shape.

The payload carries a matrix plan, binding digests and one selector -- never a
prompt, candidate token, decision or KV material.  Operation prefixes are derived
from the plan seed, exactly as in the direct matrix.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from mycelium_router.a10_qualification import (
   QUALIFYING_EXECUTION_PATH,
   QUALIFYING_PROVENANCE,
   VERIFIER_NEGATIVE_FIELDS,
   VERIFIER_OPERATION_PROTOCOL,
   validate_verifier_operation,
)
from mycelium_router.a10_runtime_batch import A10ContractError
from mycelium_router.a10_verifier_matrix import (
   VERIFIER_NEGATIVE_ORDER,
   MatrixPlan,
   run_matrix_operation,
   run_verifier_negative,
)


PRODUCT_COMMAND = "a10_target_verify"
PAYLOAD_PROTOCOL = "mycelium.a10_product_verification_operation.v1"
_BINDING_FIELDS = frozenset(
   {"runtime_digest", "model_digest", "representation_digest", "backend"}
)
_PLAN_FIELDS = frozenset({"widths", "prefix_buckets", "operations", "vocab_size", "seed"})
_NEGATIVE_ENVELOPE_FIELDS = frozenset({"protocol", "execution_path", "provenance", "negative"})


class A10ProductVerifierError(A10ContractError):
   """A product verification operation or its result was refused."""

   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _reject(code: str) -> NoReturn:
   raise A10ProductVerifierError(code)


def _integer(value: object) -> bool:
   return isinstance(value, int) and not isinstance(value, bool)


def _digest(value: object) -> bool:
   return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


def operation_payload(
   plan: MatrixPlan,
   *,
   index: int | None = None,
   case_id: str | None = None,
   runtime_digest: str,
   model_digest: str,
   representation_digest: str,
   backend: str,
) -> dict[str, Any]:
   """The private payload for one matrix operation or one required negative."""

   if (index is None) == (case_id is None):
      _reject("a10_product_payload_invalid")
   payload: dict[str, Any] = {
      "protocol": PAYLOAD_PROTOCOL,
      "kind": "operation" if index is not None else "negative",
      "plan": {
         "widths": dict(plan.widths),
         "prefix_buckets": {name: list(lengths) for name, lengths in plan.prefix_buckets.items()},
         "operations": plan.operations,
         "vocab_size": plan.vocab_size,
         "seed": plan.seed,
      },
      "binding": {
         "runtime_digest": runtime_digest,
         "model_digest": model_digest,
         "representation_digest": representation_digest,
         "backend": backend,
      },
   }
   if index is not None:
      payload["index"] = index
   else:
      payload["case_id"] = case_id
   _parse(payload)
   return payload


def _parse(payload: object) -> tuple[str, int | str, MatrixPlan, dict[str, str]]:
   if not isinstance(payload, Mapping) or payload.get("protocol") != PAYLOAD_PROTOCOL:
      _reject("a10_product_payload_invalid")
   kind = payload.get("kind")
   selector_field = {"operation": "index", "negative": "case_id"}.get(kind)  # type: ignore[arg-type]
   if selector_field is None or set(payload) != {"protocol", "kind", "plan", "binding", selector_field}:
      _reject("a10_product_payload_invalid")
   binding = payload["binding"]
   if (
      not isinstance(binding, Mapping)
      or set(binding) != _BINDING_FIELDS
      or not all(
         _digest(binding[field])
         for field in ("runtime_digest", "model_digest", "representation_digest")
      )
      or not isinstance(binding["backend"], str)
      or not binding["backend"]
   ):
      _reject("a10_product_payload_invalid")
   raw = payload["plan"]
   if not isinstance(raw, Mapping) or set(raw) != _PLAN_FIELDS:
      _reject("a10_product_payload_invalid")
   widths, buckets = raw["widths"], raw["prefix_buckets"]
   if (
      not isinstance(widths, Mapping)
      or not all(isinstance(key, str) and _integer(value) for key, value in widths.items())
      or not isinstance(buckets, Mapping)
      or not all(
         isinstance(key, str) and isinstance(value, list) and all(_integer(item) for item in value)
         for key, value in buckets.items()
      )
      or not all(_integer(raw[field]) for field in ("operations", "vocab_size", "seed"))
   ):
      _reject("a10_product_payload_invalid")
   plan = MatrixPlan(
      widths=dict(widths),
      prefix_buckets={key: tuple(value) for key, value in buckets.items()},
      operations=raw["operations"],
      vocab_size=raw["vocab_size"],
      seed=raw["seed"],
   )
   selector = payload[selector_field]
   if kind == "operation" and not _integer(selector):
      _reject("a10_product_payload_invalid")
   if kind == "negative" and selector not in VERIFIER_NEGATIVE_ORDER:
      _reject("a10_product_payload_invalid")
   return kind, selector, plan, dict(binding)


def validate_operation_payload(payload: object) -> dict[str, Any]:
   """Validate one product verification payload before it is dispatched."""

   _parse(payload)
   return dict(payload)  # type: ignore[arg-type]


def execute_product_operation(runtime: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
   """Node side: execute exactly one operation on this node's own target runtime."""

   kind, selector, plan, binding = _parse(payload)
   begin = getattr(runtime, "begin_target_verifier", None)
   cancel = getattr(runtime, "cancel", None)
   if not callable(begin) or not callable(cancel):
      _reject("a10_target_verifier_unavailable")

   def open_verifier(prefix: tuple[int, ...], ref: str) -> Any:
      # The node's own loaded stages only; a partial target is refused by the
      # runtime verifier's chain validation.
      return begin(prefix, verification_ref=ref)

   if kind == "operation":
      return run_matrix_operation(
         plan,
         int(selector),
         open_verifier=open_verifier,
         provenance=QUALIFYING_PROVENANCE,
         execution_path=QUALIFYING_EXECUTION_PATH,
         **binding,
      )
   negative = run_verifier_negative(
      str(selector),
      plan,
      open_verifier=open_verifier,
      cancel_verification=lambda verifier: cancel(verifier.path_id),
   )
   return {
      "protocol": VERIFIER_OPERATION_PROTOCOL,
      "execution_path": QUALIFYING_EXECUTION_PATH,
      "provenance": QUALIFYING_PROVENANCE,
      "negative": negative,
   }


def unwrap_product_result(
   payload: Mapping[str, Any], result: object
) -> tuple[str, dict[str, Any]]:
   """Dispatcher side: accept only the record for exactly the operation sent."""

   kind, selector, _plan, binding = _parse(payload)
   if not isinstance(result, Mapping):
      _reject("a10_product_result_invalid")
   if kind == "operation":
      try:
         record = validate_verifier_operation(result)
      except A10ContractError:
         _reject("a10_product_result_invalid")
      if (
         record["execution_path"] != QUALIFYING_EXECUTION_PATH
         or record["provenance"] != QUALIFYING_PROVENANCE
         or record["operation_ref"] != f"a10-matrix-{int(selector):04d}"
         or any(record[field] != value for field, value in binding.items())
      ):
         _reject("a10_product_result_invalid")
      return "operation", dict(record)
   negative = result.get("negative")
   if (
      set(result) != _NEGATIVE_ENVELOPE_FIELDS
      or result["protocol"] != VERIFIER_OPERATION_PROTOCOL
      or result["execution_path"] != QUALIFYING_EXECUTION_PATH
      or result["provenance"] != QUALIFYING_PROVENANCE
      or not isinstance(negative, Mapping)
      or set(negative) != VERIFIER_NEGATIVE_FIELDS
      or negative["case_id"] != selector
   ):
      _reject("a10_product_result_invalid")
   return "negative", dict(negative)


__all__ = [
   "A10ProductVerifierError",
   "PAYLOAD_PROTOCOL",
   "PRODUCT_COMMAND",
   "execute_product_operation",
   "operation_payload",
   "unwrap_product_result",
   "validate_operation_payload",
]
