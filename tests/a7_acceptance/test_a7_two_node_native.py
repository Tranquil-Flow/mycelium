# SPDX-License-Identifier: AGPL-3.0-only
"""A7 across two real node processes over native iroh (loopback).

node-a: entry + stage 0 (ACTIVE) + the stage-1 STANDBY on a dedicated MLX port.
node-b: the stage-1 source.  Both are ``physical_inference_node.py`` processes
with their own native sidecars, configured by the ordinary ``configure``/``start``
commands; A7 is enabled only by each node's staged ``a7/node.json``.

After two committed tokens the source process group is SIGKILLed.  node-a's
entry observes the failed hop, fences the old attempt, imports the
acknowledged checkpoint on its standby port and continues the same request
with no recovery prefill; the completed output equals the monolithic
reference.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import mlx.core as mx
import pytest

from mycelium_a7_standby_plan import (
   compile_standby_artifacts,
   extend_graph_with_standby,
   node_configs,
   write_standby_secret,
)
from mycelium_a7_standby_qualification import parity_challenge_result, port_placement_identity, qualify_standby
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import (
   build_execution_graph,
   build_physical_device_states,
   prepare_physical_deployment,
)
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.mlx_runtime import MLXRuntimePort
from mycelium_router.payloads import encode_token_ids
from runtime_loader import canonical_json, execute_loaded_stage, load_assignment_stage
from tests.physical_qualification.test_node_service import SIDECAR_BINARY, _NodeClient
from two_process_runtime_qualification import _LocalOnlyFetcher

STANDBY_ID = "placement-001-standby"
_PROMPT = [1, 2, 3]
_MAX_NEW = 5


def _request(request_id: str) -> dict[str, Any]:
   return {
      "request_id": request_id,
      "prompt_token_ids": list(_PROMPT),
      "max_new_tokens": _MAX_NEW,
      "expected_new_tokens": _MAX_NEW,
      "qos_class": "interactive",
      "admitted_at": 0.0,
      "target_ttft_ms": 1_000.0,
      "target_tpot_ms": 1_000.0,
      "target_tokens_per_second": 1.0,
      "sampling_seed": 17,
      "generation_config_digest": "sha256:" + "a" * 64,
   }


def _write(path: Path, document: Any) -> None:
   path.parent.mkdir(parents=True, exist_ok=True)
   path.write_bytes(canonical_json_bytes(document))


def _qualification(graph, loaded, standby_loaded) -> dict[str, Any]:
   """Exact identities plus an executed parity challenge on in-process MLX ports."""

   source_id = graph.stages[1].placements[0].placement_id
   entry_id = graph.stages[0].placements[0].placement_id
   entry_a = MLXRuntimePort("node-a", graph, {entry_id: loaded[0]}, decode_mode="stage_local_kv")
   entry_b = MLXRuntimePort("node-a", graph, {entry_id: loaded[0]}, decode_mode="stage_local_kv")
   source = MLXRuntimePort("node-b", graph, {source_id: loaded[1]}, decode_mode="stage_local_kv")
   standby = MLXRuntimePort("node-a", graph, {STANDBY_ID: standby_loaded}, decode_mode="stage_local_kv")

   from mycelium_router.contracts import HopWorkItem, RuntimeBatchKey

   def item(placement, stage_index, payload, *, phase, path_id, token_index, position, span, attempt=0):
      return HopWorkItem(
         request_id="qualification",
         path_id=path_id,
         path_attempt=attempt,
         phase=phase,
         token_index=token_index,
         hop_index=stage_index,
         placement_id=placement.placement_id,
         qos_class="interactive",
         deficit_ratio=0.0,
         enqueued_at=0.0,
         idempotency_key=f"{path_id}:{phase}:{token_index}:{stage_index}",
         payload=payload,
         batch_key=RuntimeBatchKey(
            deployment_id=graph.deployment_id,
            deployment_epoch=graph.deployment_epoch,
            model_commit=graph.resolved_commit,
            manifest_digest=graph.manifest_digest,
            placement_id=placement.placement_id,
            assignment_id=placement.assignment_id,
            stage_signature=placement.stage_signature,
            load_proof_digest=placement.load_proof_digest,
            runtime_backend=placement.runtime_backend,
            phase=phase,
            hidden_size=graph.hidden_size,
            activation_bytes=graph.activation_bytes,
            token_span=span,
         ),
         position=position,
         lease_expires_at=1e15,
      )

   stage0 = graph.stages[0].placements[0]
   stage1 = graph.stages[1].placements[0]
   standby_placement = graph.stages[1].placements[1]
   hidden = entry_a.execute(item(stage0, 0, encode_token_ids(tuple(_PROMPT)), phase="PREFILL", path_id="q:src", token_index=-1, position=0, span=3))
   first = source.execute(item(stage1, 1, hidden.payload, phase="PREFILL", path_id="q:src", token_index=-1, position=0, span=3))
   assert first.success, first.failure_reason
   standby.import_kv_checkpoint(source.export_kv_checkpoint("q:src"), request_id="qualification", path_id="q:sb", path_attempt=1, placement_id=STANDBY_ID, lease_expires_at=1e15, expected_next_position=len(_PROMPT), max_bytes=1 << 24)
   entry_b.import_kv_checkpoint(entry_a.export_kv_checkpoint("q:src"), request_id="qualification", path_id="q:sb", path_attempt=1, placement_id=entry_id, lease_expires_at=1e15, expected_next_position=len(_PROMPT), max_bytes=1 << 24)
   step = encode_token_ids((first.token_id,))
   hidden_a = entry_a.execute(item(stage0, 0, step, phase="DECODE", path_id="q:src", token_index=1, position=len(_PROMPT), span=1))
   hidden_b = entry_b.execute(item(stage0, 0, step, phase="DECODE", path_id="q:sb", token_index=1, position=len(_PROMPT), span=1, attempt=1))
   source_next = source.execute(item(stage1, 1, hidden_a.payload, phase="DECODE", path_id="q:src", token_index=1, position=len(_PROMPT), span=1))
   standby_next = standby.execute(item(standby_placement, 1, hidden_b.payload, phase="DECODE", path_id="q:sb", token_index=1, position=len(_PROMPT), span=1, attempt=1))
   assert source_next.success and standby_next.success, (source_next.failure_reason, standby_next.failure_reason)
   challenge = parity_challenge_result(
      challenge_id="a7-native",
      next_position=len(_PROMPT),
      source_output=str(source_next.token_id).encode(),
      standby_output=str(standby_next.token_id).encode(),
   )
   record = qualify_standby(
      source_identity=port_placement_identity(source, source_id),
      standby_identity=port_placement_identity(standby, STANDBY_ID),
      parity_challenge=challenge,
      reserved_kv_bytes=1 << 20,
      available_bytes=1 << 30,
      issued_at_unix_ms=int(time.time() * 1000) - 1_000,
      ttl_ms=3_600_000,
   )
   assert record["qualified"], record["reason"]
   return record


@pytest.mark.skipif(not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built")
def test_two_node_processes_cut_over_to_the_standby_after_source_kill(tmp_path: Path) -> None:
   deployment = prepare_physical_deployment(tmp_path / "deployment", runtime_backend="mlx")
   assignments = deployment.assignments
   standby = compile_standby_artifacts(
      manifest=deployment.manifest,
      route_plan=deployment.route_plan,
      stage_index=1,
      standby_node_id="node-a",
      deployment_id=assignments[0]["deployment_id"],
      deployment_epoch=assignments[0]["deployment_epoch"],
      cache_root=deployment.root,
      runtime={key: assignments[1]["runtime"][key] for key in ("backend", "dtype", "quantization")},
      control_plane_binding=assignments[0]["control_plane_binding"],
      fetch_file=_LocalOnlyFetcher(deployment.root),
   )
   loaded = [load_assignment_stage(a, r, load_generation=7) for a, r in zip(assignments, deployment.artifact_reports)]
   standby_loaded = load_assignment_stage(standby["assignment"], standby["loader_report"], load_generation=7)
   base = build_execution_graph(assignments, [stage.proof for stage in loaded], link_scheme="iroh", runtime_scheme="iroh")
   graph = extend_graph_with_standby(
      base,
      stage_index=1,
      placement_id=STANDBY_ID,
      node_id="node-a",
      assignment_id=standby["assignment"]["assignment_id"],
      load_proof_digest=layer_load_proof_digest(json.loads(canonical_json(standby_loaded.proof))),
   )
   assert graph.stages[1].placements[0].lifecycle_state == "ACTIVE"
   graph_document = json.loads(json.dumps(asdict(graph)))
   states = build_physical_device_states(graph)
   state_document = json.loads(json.dumps({node: asdict(state) for node, state in states.items()}))

   roots = {node: tmp_path / f"{node}-artifacts" for node in ("node-a", "node-b")}
   for index, node in enumerate(("node-a", "node-b")):
      _write(roots[node] / "model-manifest.json", deployment.manifest)
      _write(roots[node] / f"{node}-assignment.json", assignments[index])
      _write(roots[node] / f"{node}-stage-pack.json", deployment.stage_packs[index])
   _write(roots["node-a"] / "a7/standby-assignment.json", standby["assignment"])
   _write(roots["node-a"] / "a7/standby-stage-pack.json", standby["stage_pack"])
   secret = write_standby_secret(roots["node-a"] / "a7/standby.key")
   _write(roots["node-a"] / "a7/qualification.json", _qualification(graph, loaded, standby_loaded))
   incarnation = "sha256:" + "1" * 64
   configs = node_configs(
      deployment_id=graph.deployment_id,
      graph=graph,
      stage_index=1,
      standby_placement_id=STANDBY_ID,
      standby_secret=secret,
      standby_incarnation=incarnation,
      standby_files={
         "assignment_file": "a7/standby-assignment.json",
         "manifest_file": "model-manifest.json",
         "stage_pack_file": "a7/standby-stage-pack.json",
         "secret_file": "a7/standby.key",
      },
      standby_load_generation=7,
      qualification_file="a7/qualification.json",
      ack_timeout_seconds=5.0,
      receipt_timeout_seconds=5.0,
   )
   assert set(configs) == {"node-a", "node-b"}
   for node, config in configs.items():
      _write(roots[node] / "a7/node.json", config)

   run_id = str(uuid.uuid4())
   socket_base = Path(tempfile.mkdtemp(prefix="myc-a7-", dir="/tmp"))
   clients = {
      node: _NodeClient(node_id=node, run_id=run_id, deployment_id=graph.deployment_id, artifact_root=roots[node], socket_root=socket_base / node[-1])
      for node in ("node-a", "node-b")
   }
   first, second = clients["node-a"], clients["node-b"]
   killed = False
   try:
      configured = {}
      for node, client in clients.items():
         result = client.command(
            "configure",
            {
               "assignment_file": f"{node}-assignment.json",
               "manifest_file": "model-manifest.json",
               "stage_pack_file": f"{node}-stage-pack.json",
               "graph": graph_document,
               "device_states": state_document,
               "load_generation": 7,
            },
         )
         configured[node] = result["observation"]["details"]
      for client, peer, generation, local in ((first, "node-b", 2, 1), (second, "node-a", 1, 2)):
         details = configured[peer]
         client.command(
            "start",
            {
               "peer": {"node_id": peer, "endpoint_id": details["endpoint_addr"]["id"], "endpoint_addr": details["endpoint_addr"], "generation": generation},
               "local_generation": local,
            },
         )
         client._register_process_group_members()

      request_id = str(uuid.uuid4())
      started = first.command("infer_start", {"request": _request(request_id)})["observation"]["details"]
      assert started["status"] == "DECODING"
      decoded = first.command("infer_decode", {"request_id": request_id, "count": 1})["observation"]["details"]
      assert decoded["output"]["token_indexes"] == [0, 1]
      ack_deadline = time.monotonic() + 10
      while True:
         shadow = first.command("a7_snapshot", {"request_id": request_id})["observation"]["details"]
         if shadow["request"]["acknowledged"] >= 2 or time.monotonic() > ack_deadline:
            break
         time.sleep(0.05)
      if os.environ.get("A7_DEBUG_DUMP"):
         Path(os.environ["A7_DEBUG_DUMP"]).write_text(json.dumps({"a7a": shadow, "a7b": second.command("a7_snapshot", {})["observation"]["details"], "a": first.command("snapshot")["observation"]["details"], "b": second.command("snapshot")["observation"]["details"]}, default=str, indent=1))
      assert shadow["request"]["acknowledged"] == 2 == shadow["request"]["committed"], (shadow, second.command("a7_snapshot", {})["observation"]["details"], second.command("snapshot")["observation"]["details"].get("transport_outbound_trace"), first.command("snapshot")["observation"]["details"].get("transport_outbound_trace"))

      second._signal_owned_process_group(signal.SIGKILL)
      killed = True
      second._wait_for_owned_process_group_exit(timeout=10)
      killed_at = time.monotonic()
      # Without an A4 liveness feed the entry learns of the dead source when the
      # in-flight hop's iroh delivery deadline (the node command timeout) expires,
      # which can outlast one decode command's wait: poll for the cutover.
      raw = first.raw_command("infer_decode", {"request_id": request_id, "count": 1})
      after: dict[str, Any] = {}
      deadline = time.monotonic() + 90
      while time.monotonic() < deadline:
         after = first.command("a7_snapshot", {"request_id": request_id})["observation"]["details"]
         if after["request"]["result"] is not None:
            break
         time.sleep(0.25)
      detected_ms = (time.monotonic() - killed_at) * 1000
      if os.environ.get("A7_DEBUG_DUMP"):
         Path(os.environ["A7_DEBUG_DUMP"] + ".after").write_text(json.dumps({"raw": raw, "a7": after, "detected_ms": detected_ms, "a": first.command("snapshot")["observation"]["details"]}, default=str, indent=1))
      assert after["request"]["result"] is not None, (raw.get("error"), after)
      finished: dict[str, Any] = {}
      for _ in range(_MAX_NEW):
         step = first.raw_command("infer_decode", {"request_id": request_id, "count": 1})
         if step["ok"] is not True:
            if os.environ.get("A7_DEBUG_DUMP"):
               Path(os.environ["A7_DEBUG_DUMP"] + ".final").write_text(json.dumps({"step": step, "a": first.command("snapshot")["observation"]["details"]}, default=str, indent=1))
            pytest.fail(f"decode failed after cutover: {step.get('error')}")
         finished = step["result"]["observation"]["details"]
         if finished["status"] != "DECODING":
            break
      assert after["request"]["result"]["outcome"] == "fenced_kv_successor", after
      assert finished["status"] == "COMPLETED", (finished, after)
      assert finished["output"]["token_indexes"] == list(range(_MAX_NEW))

      reference = load_assignment_stage(deployment.reference_assignment, deployment.reference_report, load_generation=7)
      context = list(_PROMPT)
      expected: list[int] = []
      for _ in range(_MAX_NEW):
         logits = execute_loaded_stage(reference, token_ids=mx.array((tuple(context),), dtype=mx.uint32))
         mx.eval(logits)
         token = quantized_greedy_token_id(logits[0, -1, :].tolist())
         expected.append(token)
         context.append(token)
      assert finished["output"]["token_ids"] == expected
      final = first.command("a7_snapshot", {"request_id": request_id})["observation"]["details"]
      assert final["standby_runtime"]["prefill_operation_count"] == 0
      # The standby decoded every position after the committed watermark.
      assert final["standby_runtime"]["decode_operation_count"] == _MAX_NEW - 2
      assert final["standby_runtime"]["active_state_count"] == 0
      assert final["agent"]["shadow_entry_count"] == 0 and final["agent"]["entry_roles"] == 0
   finally:
      first.stop()
      if not killed:
         second.stop()
      else:
         try:
            second.stop()
         except Exception:
            pass
      shutil.rmtree(socket_base, ignore_errors=True)
