# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate deterministic cross-language Router wire golden frames."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
from mycelium_router.wire import (  # noqa: E402
   ROUTER_WIRE_PROTOCOL,
   decode_frame,
   encode_frame,
)


OUTPUT_DIR = ROOT / "contracts" / "router-wire-golden"
INDEX_SCHEMA = "mycelium.router_wire.golden.v1"


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


if __name__ == "__main__":
   generated = generate()
   print(f"generated {len(generated['frames'])} Router wire golden frames")
