"""Entry, Relay, and wire-level PathCancellation adversarial corpus."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import struct

import pytest

from mycelium_router.contracts import PathCancellation, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL, WireError, decode_frame, encode_frame
from test_router_inprocess_mesh import three_device_graph
from test_router_policy import request_fixture, state_table

from ._harness import build_mesh_case


BOOL_RED = "PRODUCTION RED PC-BOOL-1: bool passes as cancellation integer identity"


class _BoolIdentityProductionRed(AssertionError):
   """Expected only when the precisely shaped PC-BOOL-1 defect is present."""


def _mutate_wire_body(frame: bytes, field: str, value: object) -> bytes:
   header_length = struct.unpack(">I", frame[:4])[0]
   envelope = json.loads(frame[4 : 4 + header_length])
   envelope["body"][field] = value
   payload = frame[4 + header_length :]
   envelope["payload_length"] = len(payload)
   envelope["payload_sha256"] = hashlib.sha256(payload).hexdigest()
   header = json.dumps(
      envelope,
      sort_keys=True,
      separators=(",", ":"),
      allow_nan=False,
   ).encode("utf-8")
   return struct.pack(">I", len(header)) + header + payload


def test_path_cancellation_wire_is_scalar_control_only() -> None:
   cancellation = PathCancellation("request-1", "path-1", 0, 3)
   frame = encode_frame(cancellation)
   decoded = decode_frame(frame)
   header_length = struct.unpack(">I", frame[:4])[0]
   envelope = json.loads(frame[4 : 4 + header_length])

   assert decoded.message == cancellation
   assert decoded.payload == b""
   assert envelope == {
      "protocol": ROUTER_WIRE_PROTOCOL,
      "message_type": "PathCancellation",
      "body": {
         "request_id": "request-1",
         "path_id": "path-1",
         "path_attempt": 0,
         "topology_version": 3,
      },
      "payload_length": 0,
      "payload_sha256": hashlib.sha256(b"").hexdigest(),
   }
   assert "protected_edit" not in json.dumps(envelope)


@pytest.mark.parametrize("field", ["path_attempt", "topology_version"])
@pytest.mark.xfail(strict=True, raises=_BoolIdentityProductionRed, reason=BOOL_RED)
def test_wire_rejects_bool_as_integer_identity(field: str) -> None:
   frame = encode_frame(PathCancellation("request-1", "path-1", 0, 3))
   forged = _mutate_wire_body(frame, field, True)
   try:
      decoded = decode_frame(forged)
   except WireError as error:
      if error.code != "invalid_wire_field":
         raise RuntimeError(f"unexpected_wire_error:{error.code}") from error
      return
   observed = getattr(decoded.message, field)
   if observed is not True:
      raise RuntimeError(f"unexpected_bool_defect_shape:{field}={observed!r}")
   raise _BoolIdentityProductionRed(f"wire accepted bool identity:{field}")


@pytest.mark.parametrize(
   ("field", "replacement"),
   [
      ("request_id", "request-forged"),
      ("path_id", "path-forged"),
      ("path_attempt", 17),
      ("topology_version", 17),
   ],
)
def test_relay_rejects_wrong_cancellation_identity_without_release(
   field: str,
   replacement: object,
) -> None:
   case = build_mesh_case(request_id=f"request-wrong-{field}")
   target = case.routers["node-c"]
   before = list(case.runtimes["node-c"].cancel_calls)
   forged = replace(case.cancellation, **{field: replacement})

   assert not target.receive_path_cancellation(forged, source_node_id="node-a")
   assert case.record.manifest.path_id in target.relay._paths
   assert case.runtimes["node-c"].cancel_calls == before


def test_unknown_and_already_released_paths_fail_closed() -> None:
   case = build_mesh_case(request_id="request-unknown-released")
   target = case.routers["node-c"]
   unknown = replace(case.cancellation, path_id="path-never-registered")

   assert not target.receive_path_cancellation(unknown, source_node_id="node-a")
   assert target.receive_path_cancellation(
      case.cancellation,
      source_node_id="node-a",
   )
   assert not target.receive_path_cancellation(
      case.cancellation,
      source_node_id="node-a",
   )
   assert case.runtimes["node-c"].cancel_calls == [case.cancellation.path_id]


@pytest.mark.parametrize("source_node_id", ["node-c", "node-forged"])
def test_non_entry_or_unregistered_source_cannot_cancel_registered_path(
   source_node_id: str,
) -> None:
   case = build_mesh_case(request_id=f"request-invalid-source-{source_node_id}")
   target = case.routers["node-d"]

   assert not target.receive_path_cancellation(
      case.cancellation,
      source_node_id=source_node_id,
   )
   assert case.record.manifest.path_id in target.relay._paths
   assert case.runtimes["node-d"].cancel_calls == []


@pytest.mark.xfail(strict=True, raises=_BoolIdentityProductionRed, reason=BOOL_RED)
def test_relay_rejects_bool_path_attempt_without_release() -> None:
   case = build_mesh_case(request_id="request-bool-direct")
   target = case.routers["node-c"]
   forged = replace(case.cancellation, path_attempt=False)

   accepted = target.receive_path_cancellation(forged, source_node_id="node-a")
   if accepted:
      if (
         case.record.manifest.path_id in target.relay._paths
         or case.runtimes["node-c"].cancel_calls != [forged.path_id]
      ):
         raise RuntimeError("unexpected_bool_relay_defect_shape")
      raise _BoolIdentityProductionRed("relay accepted and released bool identity")
   assert not accepted
   assert case.record.manifest.path_id in target.relay._paths
   assert case.runtimes["node-c"].cancel_calls == []


def test_entry_cancels_before_manifest_lock_exactly_once() -> None:
   graph = three_device_graph()
   states = state_table(slow_b_bandwidth=True)
   states["node-d"] = replace(states["node-a"], node_id="node-d")
   capacity = FakeCapacityPort()
   transport = FakeTransportPort()
   runtime = FakeRuntimePort()
   router = Router(
      node_id="node-a",
      topology=FakeTopologyProvider(graph),
      device_states=FakeDeviceStateProvider(states),
      capacity=capacity,
      runtime=runtime,
      transport=transport,
      clock=ManualClock(),
      id_source=SequenceIdSource(),
      config=RouterConfig(prefill_chunk_size_tokens=0),
   )
   request = replace(request_fixture(), request_id="request-before-manifest-lock")
   router.start_distributed_prefill(
      request,
      InMemoryClientSink(),
      excluded_placements=frozenset({"node-b-stage-000"}),
   )

   assert router.request_status(request.request_id) == "PREFILL"
   assert router.cancel(request.request_id)
   assert not router.cancel(request.request_id)
   assert router.request_status(request.request_id) == "CANCELLED"
   assert len(transport.path_cancellations) == 1
   assert len(capacity.release_calls) == 1
   assert runtime.cancel_calls == [transport.path_cancellations[0].path_id]
   assert not hasattr(transport, "route_ready")
