"""Entry, Relay, and wire-level PathCancellation adversarial corpus."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import struct
from threading import Event, Thread
import time

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
def test_wire_rejects_bool_as_integer_identity(field: str) -> None:
   frame = encode_frame(PathCancellation("request-1", "path-1", 0, 3))
   forged = _mutate_wire_body(frame, field, True)
   with pytest.raises(WireError) as captured:
      decode_frame(forged)
   assert captured.value.code == "invalid_wire_field"


@pytest.mark.parametrize("field", ["path_attempt", "topology_version"])
def test_wire_encoder_rejects_bool_as_integer_identity(field: str) -> None:
   forged = replace(
      PathCancellation("request-1", "path-1", 0, 3),
      **{field: True},
   )
   with pytest.raises(WireError) as captured:
      encode_frame(forged)
   assert captured.value.code == "invalid_wire_field"


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


def test_cancelled_attempt_cannot_re_register_but_newer_attempt_can() -> None:
   case = build_mesh_case(request_id="request-cancel-tombstone")
   target = case.routers["node-c"]

   assert target.receive_path_cancellation(
      case.cancellation,
      source_node_id="node-a",
   )
   assert not target.relay.register_path(
      case.request,
      case.record.manifest,
      case.record.graph,
      entry_node_id="node-a",
   )
   assert target.relay.register_path(
      case.request,
      replace(case.record.manifest, path_attempt=1),
      case.record.graph,
      entry_node_id="node-a",
   )
   cancel_calls = len(case.runtimes["node-c"].cancel_calls)
   target.relay.release_path(case.cancellation.path_id, path_attempt=0)
   assert target.relay._paths[case.cancellation.path_id][1].path_attempt == 1
   assert len(case.runtimes["node-c"].cancel_calls) == cancel_calls


def test_runtime_cancel_finishes_before_new_attempt_registration() -> None:
   case = build_mesh_case(request_id="request-cancel-runtime-fence")
   target = case.routers["node-c"]
   runtime = case.runtimes["node-c"]
   entered = Event()
   release = Event()
   original_cancel = runtime.cancel

   def blocking_cancel(path_id: str) -> None:
      entered.set()
      assert release.wait(timeout=1.0)
      original_cancel(path_id)

   runtime.cancel = blocking_cancel
   cancellation_results: list[bool] = []
   cancellation_thread = Thread(
      target=lambda: cancellation_results.append(
         target.relay.receive_path_cancellation(
            case.cancellation,
            source_node_id="node-a",
         )
      )
   )
   cancellation_thread.start()
   assert entered.wait(timeout=1.0)

   registration_results: list[bool] = []
   registration_thread = Thread(
      target=lambda: registration_results.append(
         target.relay.register_path(
            case.request,
            replace(case.record.manifest, path_attempt=1),
            case.record.graph,
            entry_node_id="node-a",
         )
      )
   )
   registration_thread.start()
   time.sleep(0.02)
   assert registration_thread.is_alive()
   release.set()
   cancellation_thread.join(timeout=1.0)
   registration_thread.join(timeout=1.0)

   assert not cancellation_thread.is_alive()
   assert not registration_thread.is_alive()
   assert cancellation_results == [True]
   assert registration_results == [True]
   assert runtime.cancel_calls == [case.cancellation.path_id]
   assert target.relay._paths[case.cancellation.path_id][1].path_attempt == 1


def test_terminal_path_metadata_is_bounded() -> None:
   case = build_mesh_case(request_id="request-metadata-bound")
   relay = case.routers["node-c"].relay

   for index in range(4_200):
      relay.release_path(f"synthetic-terminal-path-{index}", path_attempt=0)

   assert len(relay._cancelled_path_attempts) <= 4_096
   assert len(relay._path_generations) <= 4_097
   replay = replace(
      case.record.manifest,
      path_id="synthetic-terminal-path-0",
      path_attempt=0,
   )
   assert not relay.register_path(
      case.request,
      replay,
      case.record.graph,
      entry_node_id="node-a",
   )


def test_unknown_unscoped_release_does_not_grow_generation_metadata() -> None:
   case = build_mesh_case(request_id="request-unscoped-release-bound")
   relay = case.routers["node-c"].relay
   generations_before = len(relay._path_generations)

   for index in range(4_200):
      relay.release_path(f"unknown-unscoped-path-{index}")

   assert len(relay._path_generations) == generations_before


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


def test_relay_rejects_bool_path_attempt_without_release() -> None:
   case = build_mesh_case(request_id="request-bool-direct")
   target = case.routers["node-c"]
   forged = replace(case.cancellation, path_attempt=False)

   assert not target.receive_path_cancellation(forged, source_node_id="node-a")
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
