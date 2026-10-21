# SPDX-License-Identifier: AGPL-3.0-only
"""A7 operator-plan extension: one physical plan that stages a KV-successor standby.

Input is an ordinary operator plan produced by ``scripts/build_qwen_live_route.py``
(its ``controller.source_root`` transfer bundle, run plan, transfer manifests
and coordinator offers).  :func:`extend_operator_plan` returns a new, *unbound*
plan over a cloned bundle that:

* compiles the standby's own assignment for the covered stage on the standby
  node, provisions and loads it locally exactly as the builder proves every
  other placement (local load proof rewritten to the target assignment), and
  compiles its stage pack against the bundle's own tensors;
* extends the execution graph with that STANDBY placement (written to
  ``control/execution-graph.json`` and every node's ``configure.graph``), and
  sets the run plan's decode mode to ``stage_local_kv``;
* writes each node's own ``a7/<node_id>/node.json`` for the case variant, the
  standby's assignment, stage pack and owner-only key, and the measured
  qualification record when one is supplied;
* recomputes the transfer manifest and per-node selections, so the standby key,
  the standby assets and the qualification are shipped to their owner only;
* updates every coordinator offer's ``graph_digest`` to the new graph.

The offers must then be re-signed (:func:`bind_extended_plan`, the same
``bind_operator_plan_document`` the run-day bind uses).  Nothing here measures
or invents a qualification: the parity challenge produces it.
"""

from __future__ import annotations

import copy
import gc
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CASE_VARIANTS = ("parity", "parity_incompatible", "positive", "incompatible", "lagging", "abort")
#: Staging-only layouts (no node configs): the standby assets for a measurement.
STAGING_VARIANTS = frozenset({"parity", "parity_incompatible"})
#: The gate case each served variant measures.
CASE_FOR_VARIANT = {
   "positive": "ordinary_browser_source_kill_kv_successor",
   "incompatible": "incompatible_standby_falls_back_to_a6",
   "lagging": "lagging_standby_falls_back_to_a6",
   "abort": "no_a6_successor_aborts",
}
_A7_PRIVATE_PREFIX = "a7/"


class A7PlanError(ValueError):
   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _require(condition: bool, code: str) -> None:
   if not condition:
      raise A7PlanError(code)


def _clone_tree(source: Path, destination: Path) -> None:
   _require(source.is_dir() and not destination.exists(), "a7_plan_bundle_clone_invalid")
   if sys.platform == "darwin":
      # APFS clones: the 0.5B tensors cost no extra disk per case.
      completed = subprocess.run(["cp", "-Rc", str(source), str(destination)], capture_output=True, check=False)
      if completed.returncode == 0:
         return
   shutil.copytree(source, destination)


def _variant_knobs(variant: str) -> dict[str, Any]:
   """Per-case A7 configuration (the case's controlled condition, nothing measured)."""

   # The lagging standby: its store cannot hold even one checkpoint, so it
   # never acknowledges; the entry's short sync-ack window records the lag.
   lagging = {"standby_max_checkpoint_bytes": 1, "ack_timeout_seconds": 0.05}
   if variant == "abort":
      # No KV successor (the standby lags) and no A6 authority: explicit abort.
      return {**lagging, "a6_available": False}
   if variant == "lagging":
      return lagging
   return {}


def extend_operator_plan(
   plan: Mapping[str, Any],
   *,
   out_bundle: Path,
   variant: str,
   source_node_id: str,
   standby_node_id: str,
   qualification: Mapping[str, Any] | None = None,
   standby_incarnation: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
   """The unbound A7 plan for one case variant, over a clone of the plan's bundle."""

   from mycelium_a7_node_wiring import a7_node_config_path_for
   from mycelium_a7_standby_plan import (
      extend_graph_with_standby,
      node_configs,
      plan_summary,
      require_distinct_failure_domains,
      write_standby_secret,
   )
   from mycelium_a7_standby_qualification import validate_standby_qualification
   from mycelium_qualification.physical_deployment import _route_with_nodes
   from mycelium_router.layer_builder import layer_load_proof_digest
   from physical_inference_node import execution_graph_from_document
   from physical_inference_qualification import node_transfer_manifests
   from runtime_loader import canonical_json, load_assignment_stage
   from scripts.build_qwen_live_route import (
      _bytes,
      _digest,
      _physical_graph_document,
      _target_proof,
      _target_report,
      _transfer_manifest,
      _write_document,
   )
   from layer_assignment import compile_layer_assignments
   from stage_pack import artifact_report_for_loader, compile_stage_pack, verify_stage_pack
   from weight_provisioning import artifact_report_errors, provision_assignment

   peers = plan.get("controller", {}).get("peers", ())
   _require(not any(peer.get("node_id") in {source_node_id, standby_node_id} and (
      peer.get("process_transport") == "external_iroh"
      or str(peer.get("peer_class", "")).startswith("android_native")
      or str(peer.get("runtime_backend", "")).startswith("mobile-rust-")
   ) for peer in peers), "a7_phone_kv_unsupported")
   _require(variant in CASE_VARIANTS, "a7_plan_variant_invalid")
   _require(source_node_id != standby_node_id, "a7_plan_same_failure_domain")
   _require((qualification is None) == (variant in STAGING_VARIANTS), "a7_plan_qualification_required")
   document = json.loads(json.dumps(dict(plan)))
   controller = document["controller"]
   run_plan = controller["run_plan"]
   nodes = run_plan["nodes"]
   graphs = [node["configure"]["graph"] for node in nodes]
   _require(all(graph == graphs[0] for graph in graphs), "a7_plan_graphs_differ")
   graph = execution_graph_from_document(graphs[0])
   _require(
      not any(p.lifecycle_state == "STANDBY" for s in graph.stages for p in s.placements),
      "a7_plan_already_extended",
   )
   covered = [
      index
      for index, stage in enumerate(graph.stages)
      if [p.node_id for p in stage.placements if p.lifecycle_state == "ACTIVE"] == [source_node_id]
   ]
   _require(len(covered) == 1, "a7_plan_source_stage_invalid")
   stage_index = covered[0]
   source_placement = graph.stages[stage_index].placements[0]
   _require(not any(p.node_id in {source_node_id, standby_node_id} and
      p.runtime_backend.startswith("mobile-rust-") for s in graph.stages for p in s.placements),
      "a7_phone_kv_unsupported")
   peers = {peer["node_id"]: peer for peer in controller["peers"]}
   _require(standby_node_id in peers and source_node_id in peers, "a7_plan_peer_missing")
   by_node = {node["node_id"]: node for node in nodes}
   source_bundle = Path(controller["source_root"])
   _clone_tree(source_bundle, out_bundle)
   bundle = out_bundle

   def read(relative: str) -> Any:
      return json.loads((bundle / relative).read_text(encoding="utf-8"))

   manifest = read(by_node[source_node_id]["configure"]["manifest_file"])
   source_assignment = read(by_node[source_node_id]["configure"]["assignment_file"])
   load_generation = by_node[standby_node_id]["configure"]["load_generation"]
   runtime = {key: source_assignment["runtime"][key] for key in ("backend", "dtype", "quantization")}
   numpy_standby = variant in {"incompatible", "parity_incompatible"}
   if numpy_standby:
      # Gate TOPOLOGY fallback_incompatibility "numpy_runtime": the standby is a
      # numpy placement, so its KV identity differs from the MLX source.
      runtime["backend"] = "numpy" if runtime.get("backend") != "numpy" else "mlx"

   route_nodes = tuple(
      standby_node_id if index == stage_index else f"a7-unused-{index}" for index in range(len(graph.stages))
   )
   ranges = [range(s.layer_range.start_layer, s.layer_range.end_layer_exclusive) for s in graph.stages]
   route = _route_with_nodes(manifest, route_nodes, ranges)

   def compile_for(cache_root: str) -> dict[str, Any]:
      assignments = compile_layer_assignments(
         route_plan=route,
         manifest=manifest,
         deployment_id=graph.deployment_id,
         deployment_epoch=graph.deployment_epoch,
         cache_roots={node: cache_root for node in route_nodes},
         runtime_by_node={node: dict(runtime) for node in route_nodes},
         control_plane_binding=copy.deepcopy(source_assignment["control_plane_binding"]),
      )
      return assignments[stage_index]

   deployment_root = bundle / "deployment"
   local_assignment = compile_for(str(deployment_root))

   def local_fetch(model_id: str, revision: str, filename: str, cache_root: Path, *, local_files_only: bool):
      del model_id, revision, cache_root, local_files_only
      return deployment_root / filename, True

   base_report = provision_assignment(local_assignment, fetch_file=local_fetch, local_files_only=True)
   _require(not artifact_report_errors(local_assignment, base_report), "a7_plan_standby_artifacts_invalid")
   local_pack = compile_stage_pack(local_assignment, manifest, base_report)
   local_verification = verify_stage_pack(local_pack, assignment=local_assignment, manifest=manifest)
   loader_report = artifact_report_for_loader(local_pack, local_verification, assignment=local_assignment, manifest=manifest)
   loaded = load_assignment_stage(local_assignment, loader_report, load_generation=load_generation)
   proof_document = json.loads(canonical_json(loaded.proof))
   del loaded
   gc.collect()

   target_assignment = compile_for(f"{peers[standby_node_id]['staging_root']}/deployment")
   target_report = _target_report(base_report, target_assignment)
   target_pack = compile_stage_pack(target_assignment, manifest, target_report, source_artifact_root=deployment_root)
   target_verification = verify_stage_pack(target_pack, assignment=target_assignment, manifest=manifest, source_artifact_root=deployment_root)
   target_proof = _target_proof(proof_document, target_assignment, target_pack, target_verification)
   standby_placement_id = f"{source_placement.placement_id}-a7-standby"
   extended = extend_graph_with_standby(
      graph,
      stage_index=stage_index,
      placement_id=standby_placement_id,
      node_id=standby_node_id,
      assignment_id=target_assignment["assignment_id"],
      load_proof_digest=layer_load_proof_digest(target_proof),
   )
   if numpy_standby:
      stages = list(extended.stages)
      from dataclasses import replace

      from mycelium_router.stage_signatures import stage_signature_for_backend

      numpy_signature = stage_signature_for_backend(extended, stages[stage_index], runtime["backend"])
      placements = tuple(
         replace(p, runtime_backend=runtime["backend"], stage_signature=numpy_signature) if p.placement_id == standby_placement_id else p
         for p in stages[stage_index].placements
      )
      stages[stage_index] = replace(stages[stage_index], placements=placements)
      from mycelium_router.validation import validate_execution_graph

      extended = validate_execution_graph(replace(extended, stages=tuple(stages)))
   require_distinct_failure_domains(extended, stage_index=stage_index, standby_placement_id=standby_placement_id)
   graph_document = _physical_graph_document(extended)
   _write_document(bundle / "control" / "execution-graph.json", graph_document)
   for node in nodes:
      node["configure"]["graph"] = copy.deepcopy(graph_document)
   run_plan["decode_mode"] = "stage_local_kv"

   standby_files = {
      "assignment_file": "a7/standby-assignment.json",
      "manifest_file": by_node[standby_node_id]["configure"]["manifest_file"],
      "stage_pack_file": "a7/standby-stage-pack.json",
      "secret_file": "a7/standby.key",
   }
   _write_document(bundle / standby_files["assignment_file"], target_assignment)
   _write_document(bundle / standby_files["stage_pack_file"], target_pack)
   owner_files: dict[str, set[str]] = {node_id: set() for node_id in by_node}
   owner_files[standby_node_id] |= {standby_files["assignment_file"], standby_files["stage_pack_file"]}
   configs: dict[str, dict[str, Any]] = {}
   entry_node_id = run_plan["entry_node_id"]
   if variant not in STAGING_VARIANTS:
      assert qualification is not None
      validate_standby_qualification(qualification)
      _write_document(bundle / "a7" / "qualification.json", qualification)
      owner_files[entry_node_id].add("a7/qualification.json")
      secret = write_standby_secret(bundle / standby_files["secret_file"])
      owner_files[standby_node_id].add(standby_files["secret_file"])
      knobs = _variant_knobs(variant)
      configs = node_configs(
         deployment_id=extended.deployment_id,
         graph=extended,
         stage_index=stage_index,
         standby_placement_id=standby_placement_id,
         standby_secret=secret,
         standby_incarnation=standby_incarnation or _digest(_bytes({"standby": standby_placement_id, "variant": variant})),
         standby_files=standby_files,
         standby_load_generation=load_generation,
         qualification_file="a7/qualification.json",
         **{key: value for key, value in knobs.items() if key in {"a6_available", "ack_timeout_seconds"}},
      )
      if "standby_max_checkpoint_bytes" in knobs:
         configs[standby_node_id]["standby"]["max_checkpoint_bytes"] = knobs["standby_max_checkpoint_bytes"]
      for node_id, config in configs.items():
         relative = a7_node_config_path_for(node_id)
         _write_document(bundle / relative, config)
         owner_files[node_id].add(relative)

   transfer = _transfer_manifest(bundle)
   private = {path for files in owner_files.values() for path in files}
   _require(all(path.startswith(_A7_PRIVATE_PREFIX) for path in private), "a7_plan_private_path_invalid")
   unexpected = sorted(
      record["path"] for record in transfer["files"] if record["path"].startswith(_A7_PRIVATE_PREFIX) and record["path"] not in private
   )
   _require(not unexpected, "a7_plan_bundle_has_foreign_a7_files")
   old_selections = {
      node_id: [record["path"] for record in manifest_document["files"]]
      for node_id, manifest_document in controller["node_transfer_manifests"]["manifests"].items()
   }
   standby_tensors = {f"deployment/{artifact['upstream_path']}" for artifact in target_pack["artifacts"]}
   selections: dict[str, list[str]] = {}
   for node_id, previous in old_selections.items():
      chosen = {path for path in previous if not path.startswith(_A7_PRIVATE_PREFIX)}
      chosen |= owner_files.get(node_id, set())
      if node_id == standby_node_id:
         chosen |= standby_tensors
      selections[node_id] = sorted(chosen)
   controller["source_root"] = str(bundle)
   controller["transfer_manifest"] = transfer
   controller["node_transfer_manifests"] = node_transfer_manifests(transfer, selections)
   graph_digest = _digest(_bytes(graph_document))
   for envelope in controller["membership_snapshot"]["assignment_offers"]:
      envelope["message"]["graph_digest"] = graph_digest
   document["plan_id"] = f"{document['plan_id']}-a7-{variant}"
   summary = {
      "protocol": "mycelium.a7_operator_plan_extension.v1",
      "variant": variant,
      "stage_index": stage_index,
      "source_node_id": source_node_id,
      "standby_node_id": standby_node_id,
      "standby_placement_id": standby_placement_id,
      "standby_runtime_backend": runtime["backend"],
      "graph_digest": graph_digest,
      "configured_nodes": sorted(configs),
      "plan": plan_summary(extended, stage_index=stage_index, standby_placement_id=standby_placement_id),
   }
   return document, summary


def bind_extended_plan(plan: Mapping[str, Any], *, seed_root: Path, now: float, refresh_identities: bool = True) -> dict[str, Any]:
   """Re-measure run-scoped peer identities and re-sign the offers (as the run-day bind does)."""

   from mycelium_seed.plan_binding import bind_operator_plan_document
   from scripts.bind_operator_plan_to_seed import _load_seed
   from scripts.build_qwen_live_route import refresh_peer_identities

   document = json.loads(json.dumps(dict(plan)))
   controller = document["controller"]
   if refresh_identities:
      controller["peers"] = refresh_peer_identities(
         [dict(peer) for peer in controller["peers"]],
         document["run_id"],
         python_executables_by_node={node["node_id"]: node["python_executable"] for node in controller["run_plan"]["nodes"]},
      )
   signer, swarm_id, seed_node_id, members = _load_seed(seed_root)
   bound = bind_operator_plan_document(document, signer=signer, swarm_id=swarm_id, seed_node_id=seed_node_id, members=members, now=now)
   bound["now_unix_ms"] = int(now * 1000)
   return bound


def parity_worker_argv(plan: Mapping[str, Any], *, role_node: str, placement_id: str, assignment_file: str, stage_pack_file: str, python: str, code_root: str | None, ssh: Mapping[str, Any] | None) -> list[str]:
   """The worker command for one placement, run from that node's staging root."""

   import shlex

   controller = plan["controller"]
   peer = next(item for item in controller["peers"] if item["node_id"] == role_node)
   node = next(item for item in controller["run_plan"]["nodes"] if item["node_id"] == role_node)
   argv = [
      python,
      "-B",
      "-m",
      "mycelium_a7_parity_worker",
      "--root",
      peer["staging_root"],
      "--graph-file",
      "control/execution-graph.json",
      "--node-id",
      role_node,
      "--placement-id",
      placement_id,
      "--assignment-file",
      assignment_file,
      "--stage-pack-file",
      stage_pack_file,
      "--manifest-file",
      node["configure"]["manifest_file"],
      "--load-generation",
      str(node["configure"]["load_generation"]),
   ]
   if ssh is None:
      return argv if code_root is None else ["/usr/bin/env", f"PYTHONPATH={code_root}", *argv]
   remote = " ".join(shlex.quote(part) for part in argv)
   if code_root is not None:
      remote = f"cd {shlex.quote(code_root)} && PYTHONPATH={shlex.quote(code_root)} {remote}"
   command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=6"]
   if ssh.get("identity_file"):
      command += ["-i", str(ssh["identity_file"]), "-o", "IdentitiesOnly=yes"]
   return [*command, str(ssh["target"]), remote]


__all__ = ("A7PlanError", "CASE_FOR_VARIANT", "CASE_VARIANTS", "STAGING_VARIANTS", "bind_extended_plan", "extend_operator_plan", "parity_worker_argv")
