import json
import struct
import unittest

from mycelium_router.contracts import (
   FailureReport,
   HopHeader,
   ManifestDelta,
   ManifestLocked,
   PathBuildState,
   PathHop,
   PathManifest,
   ProgressivePrefillContext,
   ReservationCommitResult,
   ReservationRequest,
   ReservationResult,
   TokenEvent,
)
from mycelium_router.wire import (
   WireError,
   decode_frame,
   decode_progressive_prefill,
   encode_frame,
   encode_progressive_prefill,
)
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture


def rewrite_header(frame, mutate):
   header_size = struct.unpack(">I", frame[:4])[0]
   header = json.loads(frame[4 : 4 + header_size])
   mutate(header)
   encoded = json.dumps(
      header,
      sort_keys=True,
      separators=(",", ":"),
   ).encode("utf-8")
   return struct.pack(">I", len(encoded)) + encoded + frame[4 + header_size :]


class RouterWireTests(unittest.TestCase):
   def messages(self):
      hop = PathHop(
         "stage-000",
         "placement-a",
         "reservation-1",
         reservation_expires_at=30.0,
         reservation_epoch=7,
      )
      return (
         HopHeader(
            request_id="request",
            path_id="path",
            path_attempt=1,
            phase="DECODE",
            token_index=2,
            hop_index=1,
            source_placement_id="placement-a",
            destination_placement_id="placement-b",
            topology_version=3,
            idempotency_key="request:path:1:DECODE:2:1",
         ),
         ManifestDelta("request", "path", 1, 0, hop),
         ReservationRequest(
            request_id="request",
            path_id="path",
            path_attempt=1,
            placement_id="placement-a",
            kv_bytes=1024,
            deployment_epoch=7,
            lease_expires_at=30.0,
         ),
         ReservationResult(
            True,
            reservation_id="reservation-1",
            deployment_epoch=7,
            expires_at=30.0,
         ),
         ReservationCommitResult(True),
         TokenEvent("request", "path", 1, 2, 42, 3),
         FailureReport(
            request_id="request",
            path_id="path",
            path_attempt=1,
            token_index=2,
            scope="EDGE",
            reason="timeout",
            edge_id="edge-ab",
         ),
      )

   def test_all_router_messages_round_trip_with_binary_payload(self):
      for message in self.messages():
         with self.subTest(message=type(message).__name__):
            decoded = decode_frame(encode_frame(message, b"\x00activation\xff"))
            self.assertEqual(decoded.message, message)
            self.assertEqual(decoded.payload, b"\x00activation\xff")

   def test_progressive_prefill_round_trip_reconstructs_domain_context(self):
      graph = graph_fixture()
      request = request_fixture()
      first = graph.stages[0].placements[0]
      hop = PathHop(
         graph.stages[0].stage_id,
         first.placement_id,
         "reservation-prefill",
         reservation_expires_at=30.0,
         reservation_epoch=graph.deployment_epoch,
      )
      build = PathBuildState(
         request=request,
         graph=graph,
         path_id="path-prefill",
         path_attempt=1,
         ordered_hops=(hop,),
         excluded_placements=frozenset({"excluded-placement"}),
         excluded_edges=frozenset({"excluded-edge"}),
         excluded_devices=frozenset({"excluded-device"}),
      )
      header = HopHeader(
         request_id=request.request_id,
         path_id=build.path_id,
         path_attempt=build.path_attempt,
         phase="PREFILL",
         token_index=-1,
         hop_index=0,
         source_placement_id="",
         destination_placement_id=first.placement_id,
         topology_version=graph.topology_version,
         idempotency_key=f"{request.request_id}:{build.path_id}:1:PREFILL:-1:0",
      )
      context = ProgressivePrefillContext(
         graph=graph,
         request=request,
         build=build,
         payload=b"\x00prefill-activation\xff",
      )

      decoded_header, decoded_context = decode_progressive_prefill(
         encode_progressive_prefill(header, context)
      )

      self.assertEqual(decoded_header, header)
      self.assertEqual(decoded_context, context)

   def test_manifest_locked_round_trip_reconstructs_manifest_and_build(self):
      graph = graph_fixture()
      request = request_fixture()
      hops = tuple(
         PathHop(
            stage.stage_id,
            stage.placements[0].placement_id,
            f"reservation-{index}",
            reservation_expires_at=30.0,
            reservation_epoch=graph.deployment_epoch,
         )
         for index, stage in enumerate(graph.stages)
      )
      loopback = next(
         edge
         for edge in graph.loopback_edges
         if edge.from_placement_id == hops[-1].placement_id
         and edge.to_placement_id == hops[0].placement_id
      )
      manifest = PathManifest(
         path_id="path-locked",
         path_attempt=2,
         request_id=request.request_id,
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         topology_version=graph.topology_version,
         manifest_digest=graph.manifest_digest,
         ordered_hops=hops,
         loopback_edge_id=loopback.edge_id,
      )
      build = PathBuildState(
         request=request,
         graph=graph,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         ordered_hops=hops,
      )
      locked = ManifestLocked(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         manifest=manifest,
         build=build,
      )

      decoded = decode_frame(encode_frame(locked))

      self.assertEqual(decoded.message, locked)
      self.assertEqual(decoded.payload, b"")

   def test_unknown_protocol_version_fails_closed(self):
      frame = encode_frame(self.messages()[0])
      changed = rewrite_header(
         frame,
         lambda header: header.update(protocol="mycelium.router_wire.v2"),
      )

      with self.assertRaises(WireError) as caught:
         decode_frame(changed)

      self.assertEqual(caught.exception.code, "unknown_wire_protocol")

   def test_missing_path_attempt_fails_closed(self):
      frame = encode_frame(self.messages()[0])
      changed = rewrite_header(
         frame,
         lambda header: header["body"].pop("path_attempt"),
      )

      with self.assertRaises(WireError) as caught:
         decode_frame(changed)

      self.assertEqual(caught.exception.code, "missing_wire_field")

   def test_payload_length_mismatch_fails_closed(self):
      frame = encode_frame(self.messages()[0], b"abc")

      with self.assertRaises(WireError) as caught:
         decode_frame(frame + b"extra")

      self.assertEqual(caught.exception.code, "payload_length_mismatch")

   def test_payload_digest_mismatch_fails_closed(self):
      frame = bytearray(encode_frame(self.messages()[0], b"abc"))
      frame[-1] ^= 1

      with self.assertRaises(WireError) as caught:
         decode_frame(bytes(frame))

      self.assertEqual(caught.exception.code, "payload_digest_mismatch")

   def test_unknown_message_type_fails_closed(self):
      frame = encode_frame(self.messages()[0])
      changed = rewrite_header(
         frame,
         lambda header: header.update(message_type="UnknownMessage"),
      )

      with self.assertRaises(WireError) as caught:
         decode_frame(changed)

      self.assertEqual(caught.exception.code, "unknown_message_type")


if __name__ == "__main__":
   unittest.main()
