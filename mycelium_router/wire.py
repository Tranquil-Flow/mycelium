"""Versioned, fail-closed Router control framing with raw binary payloads."""

from dataclasses import asdict, dataclass
import hashlib
import json
import struct
from typing import Any

from mycelium_router.contracts import (
   FailureReport,
   HopHeader,
   ManifestDelta,
   ManifestLocked,
   PathCancellation,
   PathBuildState,
   PathHop,
   PathManifest,
   PrefillChunkCompleted,
   ProgressivePrefillContext,
   ProgressivePrefillMessage,
   RequestContext,
   ReservationCommitResult,
   ReservationRequest,
   ReservationResult,
   TokenEvent,
)
from mycelium_router.serialization import (
   execution_graph_from_dict,
   execution_graph_to_dict,
)
from mycelium_router.validation import ContractError, validate_manifest


ROUTER_WIRE_PROTOCOL = "mycelium.router_wire.v1"
_MAX_HEADER_BYTES = 1_048_576
_MAX_PAYLOAD_BYTES = 268_435_456


class WireError(ValueError):
   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class DecodedFrame:
   message: object
   payload: bytes


_MESSAGE_TYPES = {
   cls.__name__: cls
   for cls in (
      HopHeader,
      ManifestDelta,
      ManifestLocked,
      PathCancellation,
      ProgressivePrefillMessage,
      PrefillChunkCompleted,
      ReservationRequest,
      ReservationResult,
      ReservationCommitResult,
      TokenEvent,
      FailureReport,
   )
}


def encode_progressive_prefill(
   header: HopHeader,
   context: ProgressivePrefillContext,
) -> bytes:
   if not isinstance(context.payload, bytes):
      raise WireError("payload_must_be_bytes")
   message = ProgressivePrefillMessage(
      header=header,
      graph=context.graph,
      request=context.request,
      ordered_hops=context.build.ordered_hops,
      excluded_placements=context.build.excluded_placements,
      excluded_edges=context.build.excluded_edges,
      excluded_devices=context.build.excluded_devices,
   )
   _validate_message(message)
   return encode_frame(message, context.payload)


def decode_progressive_prefill(
   frame: bytes,
) -> tuple[HopHeader, ProgressivePrefillContext]:
   decoded = decode_frame(frame)
   if not isinstance(decoded.message, ProgressivePrefillMessage):
      raise WireError("unexpected_message_type", type(decoded.message).__name__)
   message = decoded.message
   build = PathBuildState(
      request=message.request,
      graph=message.graph,
      path_id=message.header.path_id,
      path_attempt=message.header.path_attempt,
      ordered_hops=message.ordered_hops,
      excluded_placements=message.excluded_placements,
      excluded_edges=message.excluded_edges,
      excluded_devices=message.excluded_devices,
   )
   return message.header, ProgressivePrefillContext(
      graph=message.graph,
      request=message.request,
      build=build,
      payload=decoded.payload,
   )


def _encode_message_body(message: object) -> dict[str, Any]:
   if isinstance(message, ProgressivePrefillMessage):
      return {
         "header": asdict(message.header),
         "graph": execution_graph_to_dict(message.graph),
         "request": asdict(message.request),
         "ordered_hops": [asdict(hop) for hop in message.ordered_hops],
         "excluded_placements": sorted(message.excluded_placements),
         "excluded_edges": sorted(message.excluded_edges),
         "excluded_devices": sorted(message.excluded_devices),
      }
   if isinstance(message, ManifestLocked):
      return {
         "request_id": message.request_id,
         "path_id": message.path_id,
         "path_attempt": message.path_attempt,
         "manifest": _manifest_to_dict(message.manifest),
         "build": _build_to_dict(message.build),
      }
   return asdict(message)


def encode_frame(message: object, payload: bytes = b"") -> bytes:
   message_type = type(message).__name__
   if message_type not in _MESSAGE_TYPES:
      raise WireError("unknown_message_type", message_type)
   if not isinstance(payload, bytes):
      raise WireError("payload_must_be_bytes")
   if len(payload) > _MAX_PAYLOAD_BYTES:
      raise WireError("payload_too_large")
   envelope = {
      "protocol": ROUTER_WIRE_PROTOCOL,
      "message_type": message_type,
      "body": _encode_message_body(message),
      "payload_length": len(payload),
      "payload_sha256": hashlib.sha256(payload).hexdigest(),
   }
   try:
      header = json.dumps(
         envelope,
         sort_keys=True,
         separators=(",", ":"),
         allow_nan=False,
      ).encode("utf-8")
   except (TypeError, ValueError) as error:
      raise WireError("invalid_wire_message", str(error)) from error
   if len(header) > _MAX_HEADER_BYTES:
      raise WireError("header_too_large")
   return struct.pack(">I", len(header)) + header + payload


def decode_frame(frame: bytes) -> DecodedFrame:
   if not isinstance(frame, bytes):
      raise WireError("frame_must_be_bytes")
   if len(frame) < 4:
      raise WireError("truncated_frame")
   header_length = struct.unpack(">I", frame[:4])[0]
   if header_length > _MAX_HEADER_BYTES:
      raise WireError("header_too_large")
   header_end = 4 + header_length
   if len(frame) < header_end:
      raise WireError("truncated_header")
   try:
      envelope = json.loads(frame[4:header_end].decode("utf-8"))
   except (UnicodeDecodeError, json.JSONDecodeError) as error:
      raise WireError("invalid_wire_json", str(error)) from error
   if not isinstance(envelope, dict):
      raise WireError("invalid_wire_envelope")
   protocol = _required(envelope, "protocol")
   if protocol != ROUTER_WIRE_PROTOCOL:
      raise WireError("unknown_wire_protocol", str(protocol))
   message_type = _required(envelope, "message_type")
   cls = _MESSAGE_TYPES.get(message_type)
   if cls is None:
      raise WireError("unknown_message_type", str(message_type))
   body = _required(envelope, "body")
   if not isinstance(body, dict):
      raise WireError("invalid_wire_body")
   payload_length = _required(envelope, "payload_length")
   if (
      not isinstance(payload_length, int)
      or isinstance(payload_length, bool)
      or payload_length < 0
   ):
      raise WireError("invalid_payload_length")
   if payload_length > _MAX_PAYLOAD_BYTES:
      raise WireError("payload_too_large")
   payload = frame[header_end:]
   if len(payload) != payload_length:
      raise WireError("payload_length_mismatch")
   expected_digest = _required(envelope, "payload_sha256")
   actual_digest = hashlib.sha256(payload).hexdigest()
   if expected_digest != actual_digest:
      raise WireError("payload_digest_mismatch")
   message = _decode_message(cls, body)
   _validate_message(message)
   return DecodedFrame(message=message, payload=payload)


def _required(mapping: dict[str, Any], field: str) -> Any:
   if field not in mapping:
      raise WireError("missing_wire_field", field)
   return mapping[field]


def _decode_message(cls, body: dict[str, Any]) -> object:
   values = dict(body)
   try:
      if cls is ProgressivePrefillMessage:
         return ProgressivePrefillMessage(
            header=_hop_header_from_dict(_required(values, "header")),
            graph=execution_graph_from_dict(_required(values, "graph")),
            request=_request_from_dict(_required(values, "request")),
            ordered_hops=tuple(
               _path_hop_from_dict(item)
               for item in _required(values, "ordered_hops")
            ),
            excluded_placements=frozenset(
               _required(values, "excluded_placements")
            ),
            excluded_edges=frozenset(_required(values, "excluded_edges")),
            excluded_devices=frozenset(_required(values, "excluded_devices")),
         )
      if cls is ManifestLocked:
         return ManifestLocked(
            request_id=_required(values, "request_id"),
            path_id=_required(values, "path_id"),
            path_attempt=_required(values, "path_attempt"),
            manifest=_manifest_from_dict(_required(values, "manifest")),
            build=_build_from_dict(_required(values, "build")),
         )
      if cls is ManifestDelta:
         values["hop"] = _path_hop_from_dict(_required(values, "hop"))
      return cls(**values)
   except WireError:
      raise
   except (ContractError, KeyError, TypeError, ValueError) as error:
      code = (
         "missing_wire_field"
         if isinstance(error, KeyError) or "missing" in str(error)
         else "invalid_wire_field"
      )
      raise WireError(code, str(error)) from error


def _path_hop_from_dict(body: dict[str, Any]) -> PathHop:
   if not isinstance(body, dict):
      raise WireError("invalid_wire_field", "hop")
   return PathHop(**body)


def _hop_header_from_dict(body: dict[str, Any]) -> HopHeader:
   if not isinstance(body, dict):
      raise WireError("invalid_wire_field", "header")
   return HopHeader(**body)


def _request_from_dict(body: dict[str, Any]) -> RequestContext:
   if not isinstance(body, dict):
      raise WireError("invalid_wire_field", "request")
   values = dict(body)
   values["prompt_token_ids"] = tuple(_required(values, "prompt_token_ids"))
   return RequestContext(**values)


def _manifest_to_dict(manifest: PathManifest) -> dict[str, Any]:
   body = asdict(manifest)
   body["ordered_hops"] = [asdict(hop) for hop in manifest.ordered_hops]
   return body


def _manifest_from_dict(body: dict[str, Any]) -> PathManifest:
   if not isinstance(body, dict):
      raise WireError("invalid_wire_field", "manifest")
   values = dict(body)
   values["ordered_hops"] = tuple(
      _path_hop_from_dict(item) for item in _required(values, "ordered_hops")
   )
   return PathManifest(**values)


def _build_to_dict(build: PathBuildState) -> dict[str, Any]:
   return {
      "request": asdict(build.request),
      "graph": execution_graph_to_dict(build.graph),
      "path_id": build.path_id,
      "path_attempt": build.path_attempt,
      "ordered_hops": [asdict(hop) for hop in build.ordered_hops],
      "excluded_placements": sorted(build.excluded_placements),
      "excluded_edges": sorted(build.excluded_edges),
      "excluded_devices": sorted(build.excluded_devices),
   }


def _build_from_dict(body: dict[str, Any]) -> PathBuildState:
   if not isinstance(body, dict):
      raise WireError("invalid_wire_field", "build")
   return PathBuildState(
      request=_request_from_dict(_required(body, "request")),
      graph=execution_graph_from_dict(_required(body, "graph")),
      path_id=_required(body, "path_id"),
      path_attempt=_required(body, "path_attempt"),
      ordered_hops=tuple(
         _path_hop_from_dict(item)
         for item in _required(body, "ordered_hops")
      ),
      excluded_placements=frozenset(
         _required(body, "excluded_placements")
      ),
      excluded_edges=frozenset(_required(body, "excluded_edges")),
      excluded_devices=frozenset(_required(body, "excluded_devices")),
   )


def _validate_message(message: object) -> None:
   if isinstance(message, ProgressivePrefillMessage):
      header = message.header
      _validate_message(header)
      if (
         header.phase != "PREFILL"
         or message.request.request_id != header.request_id
         or message.graph.topology_version != header.topology_version
         or len(message.ordered_hops) != header.hop_index + 1
         or message.ordered_hops[-1].placement_id
         != header.destination_placement_id
      ):
         raise WireError("invalid_progressive_prefill")
   if isinstance(message, ManifestLocked):
      if (
         not message.request_id
         or message.request_id != message.manifest.request_id
         or message.request_id != message.build.request.request_id
         or message.path_id != message.manifest.path_id
         or message.path_id != message.build.path_id
         or message.path_attempt != message.manifest.path_attempt
         or message.path_attempt != message.build.path_attempt
         or message.manifest.ordered_hops != message.build.ordered_hops
      ):
         raise WireError("invalid_manifest_locked")
      try:
         validate_manifest(message.manifest, message.build.graph)
      except ContractError as error:
         raise WireError("invalid_manifest_locked", error.code) from error
   if isinstance(
      message,
      (
         HopHeader,
         ManifestDelta,
         PathCancellation,
         ReservationRequest,
         TokenEvent,
         PrefillChunkCompleted,
         FailureReport,
      ),
   ):
      if not message.request_id or not message.path_id:
         raise WireError("invalid_wire_identity")
      if message.path_attempt < 0:
         raise WireError("invalid_path_attempt")
   if isinstance(message, HopHeader):
      if (
         message.hop_index < 0
         or message.topology_version < 0
         or not message.destination_placement_id
         or not message.idempotency_key
      ):
         raise WireError("invalid_hop_header")
   elif isinstance(message, ManifestDelta):
      if message.hop_index < 0 or not message.hop.reservation_id:
         raise WireError("invalid_manifest_delta")
   elif isinstance(message, PathCancellation):
      if message.topology_version < 0:
         raise WireError("invalid_path_cancellation")
   elif isinstance(message, ReservationRequest):
      if (
         not message.placement_id
         or message.kv_bytes < 0
         or message.deployment_epoch < 0
         or message.lease_expires_at <= 0
      ):
         raise WireError("invalid_reservation_request")
   elif isinstance(message, TokenEvent):
      if message.token_index < 0 or message.sampling_counter < 1:
         raise WireError("invalid_token_event")
   elif isinstance(message, PrefillChunkCompleted):
      if message.chunk_index < 1 or message.token_count < 1:
         raise WireError("invalid_prefill_chunk_completed")
   elif isinstance(message, FailureReport):
      if not message.scope or not message.reason:
         raise WireError("invalid_failure_report")
