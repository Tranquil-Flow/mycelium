# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate deterministic cross-language Router wire golden frames."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mycelium_router.contracts import (  # noqa: E402
   ExecutionGraph,
   FailureReport,
   HopHeader,
   LayerRange,
   ManifestDelta,
   ManifestLocked,
   PathBuildState,
   PathCancellation,
   PathHop,
   PathManifest,
   Placement,
   PlacementEdge,
   PrefillChunkCompleted,
   ProgressivePrefillMessage,
   RequestContext,
   ReservationCommitResult,
   ReservationRequest,
   ReservationResult,
   Stage,
   StageCost,
   TokenEvent,
)
from mycelium_router.payloads import (  # noqa: E402
   PayloadError,
   decode_activation,
   encode_activation,
)
from mycelium_router.wire import (  # noqa: E402
   ROUTER_WIRE_PROTOCOL,
   decode_frame,
   encode_frame,
)


OUTPUT_DIR = ROOT / "contracts" / "router-wire-golden"
INDEX_SCHEMA = "mycelium.router_wire.golden.v1"
ACTIVATION_OUTPUT_DIR = ROOT / "contracts" / "router-activation-golden"
ACTIVATION_SCHEMA = "mycelium.router_activation.golden.v1"


def _graph() -> ExecutionGraph:
   placements = (
      Placement(
         placement_id="placement-a",
         node_id="node-a",
         replica_group_id="replica-0",
         assignment_id="assignment-a",
         stage_signature="stage-signature-a",
         load_proof_digest="load-proof-a",
         runtime_backend="golden-runtime",
         runtime_endpoint="http://127.0.0.1:9001",
      ),
      Placement(
         placement_id="placement-b",
         node_id="node-b",
         replica_group_id="replica-1",
         assignment_id="assignment-b",
         stage_signature="stage-signature-b",
         load_proof_digest="load-proof-b",
         runtime_backend="golden-runtime",
         runtime_endpoint="http://127.0.0.1:9002",
      ),
   )
   stages = (
      Stage(
         stage_id="stage-000",
         layer_range=LayerRange(0, 1, 1),
         component_roles=("TOKEN_EMBEDDING", "DECODER_BLOCKS"),
         stage_cost=StageCost(2.0, 1.0, 16),
         placements=(placements[0],),
      ),
      Stage(
         stage_id="stage-001",
         layer_range=LayerRange(1, 2, 1),
         component_roles=("DECODER_BLOCKS", "FINAL_NORM", "LM_HEAD"),
         stage_cost=StageCost(3.0, 1.5, 16),
         placements=(placements[1],),
      ),
   )
   return ExecutionGraph(
      deployment_id="deployment-golden",
      deployment_epoch=7,
      topology_version=3,
      model_id="golden/model",
      resolved_commit="0123456789abcdef",
      manifest_digest="manifest-golden",
      entry_stage_id=stages[0].stage_id,
      final_stage_id=stages[-1].stage_id,
      hidden_size=4,
      activation_bytes=8,
      token_envelope_bytes=16,
      stages=stages,
      edges=(
         PlacementEdge(
            "edge-ab",
            placements[0].placement_id,
            placements[1].placement_id,
            "link-ab",
         ),
      ),
      loopback_edges=(
         PlacementEdge(
            "edge-ba",
            placements[1].placement_id,
            placements[0].placement_id,
            "link-ba",
         ),
      ),
   )


def _fixtures() -> tuple[tuple[str, object, bytes], ...]:
   graph = _graph()
   request = RequestContext(
      request_id="request-goldén-🌙",
      prompt_token_ids=(11, 22, 33),
      max_new_tokens=4,
      expected_new_tokens=3,
      qos_class="interactive",
      admitted_at=10.0,
      target_ttft_ms=250.0,
      target_tpot_ms=50.0,
      target_tokens_per_second=20.0,
      sampling_seed=1234,
      generation_config_digest="generation-golden",
   )
   hops = tuple(
      PathHop(
         stage_id=stage.stage_id,
         placement_id=stage.placements[0].placement_id,
         reservation_id=f"reservation-{index}",
         reservation_expires_at=30.0,
         reservation_epoch=graph.deployment_epoch,
      )
      for index, stage in enumerate(graph.stages)
   )
   build = PathBuildState(
      request=request,
      graph=graph,
      path_id="path-golden",
      path_attempt=1,
      ordered_hops=hops,
   )
   manifest = PathManifest(
      path_id=build.path_id,
      path_attempt=build.path_attempt,
      request_id=request.request_id,
      deployment_id=graph.deployment_id,
      deployment_epoch=graph.deployment_epoch,
      topology_version=graph.topology_version,
      manifest_digest=graph.manifest_digest,
      ordered_hops=hops,
      loopback_edge_id=graph.loopback_edges[0].edge_id,
   )
   prefill_header = HopHeader(
      request_id=request.request_id,
      path_id=build.path_id,
      path_attempt=build.path_attempt,
      phase="PREFILL",
      token_index=-1,
      hop_index=0,
      source_placement_id="",
      destination_placement_id=hops[0].placement_id,
      topology_version=graph.topology_version,
      idempotency_key="request-golden:path-golden:1:PREFILL:-1:0",
      prefill_chunk_token_count=3,
   )
   return (
      (
         "01-hop-header.bin",
         HopHeader(
            request_id=request.request_id,
            path_id=build.path_id,
            path_attempt=1,
            phase="DECODE",
            token_index=2,
            hop_index=1,
            source_placement_id=hops[0].placement_id,
            destination_placement_id=hops[1].placement_id,
            topology_version=graph.topology_version,
            idempotency_key="request-golden:path-golden:1:DECODE:2:1",
         ),
         b"\x00activation\xff",
      ),
      ("02-manifest-delta.bin", ManifestDelta(request.request_id, build.path_id, 1, 0, hops[0]), b""),
      (
         "03-manifest-locked.bin",
         ManifestLocked(request.request_id, build.path_id, 1, manifest, build),
         b"",
      ),
      (
         "04-progressive-prefill-message.bin",
         ProgressivePrefillMessage(
            header=prefill_header,
            graph=graph,
            request=request,
            ordered_hops=(hops[0],),
            excluded_placements=frozenset({"placement-excluded"}),
            excluded_edges=frozenset({"edge-excluded"}),
            excluded_devices=frozenset({"node-excluded"}),
         ),
         bytes(range(256)),
      ),
      (
         "05-prefill-chunk-completed.bin",
         PrefillChunkCompleted(request.request_id, build.path_id, 1, 1, 3),
         b"",
      ),
      (
         "06-reservation-request.bin",
         ReservationRequest(
            request.request_id,
            build.path_id,
            1,
            hops[0].placement_id,
            1024,
            graph.deployment_epoch,
            30.0,
         ),
         b"\x10\x00\x20\xff",
      ),
      (
         "07-reservation-result.bin",
         ReservationResult(
            True,
            "reservation-0",
            "",
            graph.deployment_epoch,
            1e-7,
         ),
         b"",
      ),
      ("08-reservation-commit-result.bin", ReservationCommitResult(True, ""), b""),
      (
         "09-token-event.bin",
         TokenEvent(
            request.request_id,
            build.path_id,
            1,
            2,
            123456789012345678901234567890,
            3,
         ),
         b"",
      ),
      (
         "10-failure-report.bin",
         FailureReport(
            request_id=request.request_id,
            path_id=build.path_id,
            path_attempt=1,
            token_index=2,
            scope="EDGE",
            reason="timeout",
            edge_id="edge-ab",
         ),
         b"",
      ),
      (
         "11-path-cancellation.bin",
         PathCancellation(
            request.request_id,
            build.path_id,
            build.path_attempt,
            graph.topology_version,
         ),
         b"",
      ),
   )


def generate() -> dict[str, Any]:
   OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
   fixtures = _fixtures()
   expected_files = {name for name, _, _ in fixtures}
   for old_frame in OUTPUT_DIR.glob("*.bin"):
      if old_frame.name not in expected_files:
         old_frame.unlink()

   entries: list[dict[str, object]] = []
   for filename, message, payload in fixtures:
      frame = encode_frame(message, payload)
      decoded = decode_frame(frame)
      if type(decoded.message) is not type(message) or decoded.payload != payload:
         raise RuntimeError(f"canonical round trip failed for {filename}")
      (OUTPUT_DIR / filename).write_bytes(frame)
      entries.append(
         {
            "byte_length": len(frame),
            "file": filename,
            "frame_sha256": hashlib.sha256(frame).hexdigest(),
            "message_type": type(message).__name__,
            "payload_length": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
         }
      )

   index = {
      "frames": entries,
      "protocol": ROUTER_WIRE_PROTOCOL,
      "schema": INDEX_SCHEMA,
   }
   (OUTPUT_DIR / "index.json").write_text(
      json.dumps(index, indent=2, sort_keys=True) + "\n",
      encoding="utf-8",
   )
   return index


_DTYPE_CODES = {"float16": 1, "bfloat16": 2, "float32": 3}
_ACTIVATION_HEADER = struct.Struct(">4sBBBBQ")


def _u16(*values: int) -> bytes:
   return b"".join(struct.pack("<H", value) for value in values)


def _f32(*values: float) -> bytes:
   return b"".join(struct.pack("<f", value) for value in values)


def _raw_frame(
   *,
   magic: bytes = b"MYAC",
   version: int = 1,
   dtype_code: int = 3,
   rank: int | None = None,
   byte_order: int = 1,
   data_length: int | None = None,
   dims: tuple[int, ...] = (1, 1, 1),
   data: bytes = b"\x00\x00\x00\x00",
) -> bytes:
   """A hand-built frame, so reject vectors can break one field at a time."""

   header = _ACTIVATION_HEADER.pack(
      magic,
      version,
      dtype_code,
      len(dims) if rank is None else rank,
      byte_order,
      len(data) if data_length is None else data_length,
   )
   return header + b"".join(struct.pack(">I", dim) for dim in dims) + data


def _activation_fixtures() -> tuple[tuple[str, dict[str, Any], bytes], ...]:
   """(file, metadata, frame): valid frames first, then reject frames.

   Element values are chosen at the format edges: signed zero, one, a negative
   value that needs a mantissa bit, the largest finite value, the smallest
   normal and the smallest subnormal.
   """

   f32 = _f32(0.0, -0.0, 1.0, -1.5, 3.4028234663852886e38, 1.1754943508222875e-38, 1e-45, 0.1)
   f16 = _u16(0x0000, 0x8000, 0x3C00, 0xBE00, 0x7BFF, 0x0400, 0x0001, 0x2E66)
   bf16 = _u16(0x0000, 0x8000, 0x3F80, 0xBFC0, 0x7F7F, 0x0080, 0x0001, 0x3DCD)
   valid = (
      ("01-f32-1x2x4.bin", "float32", (1, 2, 4), f32),
      ("02-f16-1x2x4.bin", "float16", (1, 2, 4), f16),
      ("03-bf16-1x2x4.bin", "bfloat16", (1, 2, 4), bf16),
      ("04-f32-rank1-3.bin", "float32", (3,), _f32(1.0, 2.0, 3.0)),
      ("05-f16-rank2-3x2.bin", "float16", (3, 2), _u16(0x3C00, 0x4000, 0x4200, 0x4400, 0x4500, 0x4600)),
      ("06-bf16-rank3-1x1x1.bin", "bfloat16", (1, 1, 1), _u16(0x3F80)),
      ("07-f32-rank8-1x1x1x1x1x1x1x2.bin", "float32", (1, 1, 1, 1, 1, 1, 1, 2), _f32(-2.5, 0.125)),
   )
   fixtures: list[tuple[str, dict[str, Any], bytes]] = []
   for name, dtype, shape, data in valid:
      frame = encode_activation(dtype=dtype, shape=shape, data=data)
      fixtures.append(
         (
            name,
            {
               "kind": "valid",
               "dtype": dtype,
               "dtype_code": _DTYPE_CODES[dtype],
               "shape": list(shape),
               "data_hex": data.hex(),
            },
            frame,
         )
      )
   good = _raw_frame()
   rejects = (
      ("r01-truncated-header.bin", good[:15]),
      ("r02-bad-magic.bin", _raw_frame(magic=b"MYAD")),
      ("r03-unknown-version.bin", _raw_frame(version=2)),
      ("r04-unknown-dtype-code.bin", _raw_frame(dtype_code=9)),
      ("r05-big-endian-flag.bin", _raw_frame(byte_order=0)),
      ("r06-rank-zero.bin", _raw_frame(rank=0, dims=())),
      ("r07-rank-nine.bin", _raw_frame(dims=(1,) * 9)),
      ("r08-truncated-shape.bin", _raw_frame(dims=(1, 1, 1))[: 16 + 8]),
      ("r09-oversize-claim.bin", _raw_frame(data_length=268_435_457, data=b"")),
      ("r10-data-shorter-than-header-claims.bin", _raw_frame(data_length=8, data=b"\x00\x00\x00\x00")),
      ("r11-trailing-byte.bin", _raw_frame(data_length=4, data=b"\x00\x00\x00\x00\x00")),
      ("r12-zero-dimension.bin", _raw_frame(dims=(1, 0, 1), data=b"")),
      ("r13-shape-data-mismatch.bin", _raw_frame(dims=(1, 2, 1), data=b"\x00\x00\x00\x00")),
   )
   for name, frame in rejects:
      try:
         decode_activation(frame)
      except PayloadError as error:
         code = error.code
      else:
         raise RuntimeError(f"reject vector {name} decoded")
      fixtures.append((name, {"kind": "reject", "expected_error": code}, frame))
   return tuple(sorted(fixtures, key=lambda item: item[0]))


def generate_activation() -> dict[str, Any]:
   """Write the C-consumable ``MYAC`` activation vectors (own directory)."""

   directory = ACTIVATION_OUTPUT_DIR
   directory.mkdir(parents=True, exist_ok=True)
   fixtures = _activation_fixtures()
   expected_files = {name for name, _, _ in fixtures}
   for old_frame in directory.glob("*.bin"):
      if old_frame.name not in expected_files:
         old_frame.unlink()

   entries: list[dict[str, Any]] = []
   rows = [f"# {ACTIVATION_SCHEMA}"]
   for name, meta, frame in fixtures:
      if meta["kind"] == "valid":
         decoded = decode_activation(frame)
         if (
            decoded.dtype != meta["dtype"]
            or list(decoded.shape) != meta["shape"]
            or decoded.data.hex() != meta["data_hex"]
         ):
            raise RuntimeError(f"canonical round trip failed for {name}")
         rows.append(
            "\t".join(
               (
                  "valid",
                  name,
                  str(meta["dtype_code"]),
                  str(len(meta["shape"])),
                  ",".join(str(dim) for dim in meta["shape"]),
                  meta["data_hex"],
               )
            )
         )
      else:
         rows.append("\t".join(("reject", name, meta["expected_error"])))
      (directory / name).write_bytes(frame)
      entries.append(
         {
            "byte_length": len(frame),
            "file": name,
            "frame_sha256": hashlib.sha256(frame).hexdigest(),
            **meta,
         }
      )

   index = {"protocol": "MYAC/1", "schema": ACTIVATION_SCHEMA, "vectors": entries}
   (directory / "index.json").write_text(
      json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
   )
   (directory / "vectors.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
   return index


if __name__ == "__main__":
   generated = generate()
   print(f"generated {len(generated['frames'])} Router wire golden frames")
   # stdout above is a pinned contract; the activation vectors report on stderr.
   activation = generate_activation()
   print(
      f"generated {len(activation['vectors'])} MYAC activation golden vectors",
      file=sys.stderr,
   )
