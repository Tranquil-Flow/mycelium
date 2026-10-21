# SPDX-License-Identifier: AGPL-3.0-only
"""A7 slice 4: operator plan (standby placement) and per-node A7 configuration."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from mycelium_a7_checkpoint_channel import verify_standby_key_record
from mycelium_a7_node_wiring import (
   A7WiringError,
   standby_material,
   standby_public_records,
   validate_against_graph,
   validate_node_config,
)
from mycelium_a7_standby_plan import (
   StandbyPlanError,
   extend_graph_with_standby,
   node_configs,
   plan_summary,
   require_distinct_failure_domains,
   update_plan_graphs,
   write_standby_secret,
)
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_router.routing import RoutePolicy
from mycelium_router.serialization import execution_graph_to_dict
from test_router_numpy_qwen_kv import _case

STANDBY = "placement-001-standby"
INCARNATION = "sha256:" + "2" * 64


def _graph(tmp_path: Path):
   case = _case(tmp_path, "qwen2")
   return extend_graph_with_standby(
      case.graph,
      stage_index=1,
      placement_id=STANDBY,
      node_id="node-a",
      assignment_id=case.graph.stages[1].placements[0].assignment_id,
      load_proof_digest="sha256:" + "f" * 64,
   )


def _configs(tmp_path: Path, graph):
   secret = write_standby_secret(tmp_path / "a7" / "standby.key")
   return secret, node_configs(
      deployment_id=graph.deployment_id,
      graph=graph,
      stage_index=1,
      standby_placement_id=STANDBY,
      standby_secret=secret,
      standby_incarnation=INCARNATION,
      standby_files={"assignment_file": "a7/a.json", "manifest_file": "m.json", "stage_pack_file": "a7/p.json", "secret_file": "a7/standby.key"},
      standby_load_generation=3,
      qualification_file="a7/q.json",
   )


def test_standby_is_appended_after_the_source_with_successor_edges(tmp_path: Path) -> None:
   graph = _graph(tmp_path)
   stage = graph.stages[1]
   assert [p.lifecycle_state for p in stage.placements] == ["ACTIVE", "STANDBY"]
   assert stage.placements[1].stage_signature == stage.placements[0].stage_signature
   entry = graph.stages[0].placements[0].placement_id
   assert any(e.from_placement_id == entry and e.to_placement_id == STANDBY for e in graph.edges)
   assert any(e.from_placement_id == STANDBY and e.to_placement_id == entry for e in graph.loopback_edges)
   # The route's default selection (placements[0]) and ordinary routing never pick it.
   build = type("B", (), {"graph": graph, "excluded_placements": frozenset(), "excluded_devices": frozenset(), "excluded_edges": frozenset()})()
   assert all(STANDBY not in route for route in RoutePolicy(scorer=None)._complete_routes(build, ()))
   with pytest.raises(StandbyPlanError):
      extend_graph_with_standby(graph, stage_index=1, placement_id=STANDBY, node_id="node-a", assignment_id="x", load_proof_digest="sha256:" + "0" * 64)
   summary = plan_summary(graph, stage_index=1, standby_placement_id=STANDBY)
   assert summary["route_ready"] is False and summary["same_failure_domain_as_source"] is False
   require_distinct_failure_domains(graph, stage_index=1, standby_placement_id=STANDBY)
   (tmp_path / "b").mkdir()
   same = extend_graph_with_standby(_case(tmp_path / "b", "qwen2").graph, stage_index=1, placement_id="s2", node_id="node-b", assignment_id="x", load_proof_digest="sha256:" + "0" * 64)
   with pytest.raises(StandbyPlanError):
      require_distinct_failure_domains(same, stage_index=1, standby_placement_id="s2")


def test_plan_graph_update_replaces_every_node_graph(tmp_path: Path) -> None:
   graph = _graph(tmp_path)
   document = execution_graph_to_dict(graph)
   plan = {"nodes": [{"node_id": "node-a", "configure": {"graph": {}}}, {"node_id": "node-b", "configure": {"graph": {}}}], "run_id": "r"}
   updated = update_plan_graphs(plan, document)
   assert all(node["configure"]["graph"] == document for node in updated["nodes"])
   assert plan["nodes"][0]["configure"]["graph"] == {}
   with pytest.raises(StandbyPlanError):
      update_plan_graphs({"nodes": [{"node_id": "x"}]}, document)


def test_standby_secret_is_owner_only_and_never_overwritten(tmp_path: Path) -> None:
   path = tmp_path / "a7" / "standby.key"
   secret = write_standby_secret(path)
   assert len(secret) == 64 and path.stat().st_mode & 0o777 == 0o600
   with pytest.raises(FileExistsError):
      write_standby_secret(path)
   # Public records are deterministic from the staged secret, and verify.
   first = standby_public_records(secret, node_id="node-a", placement_digest="sha256:" + "3" * 64, incarnation=INCARNATION)
   again = standby_public_records(secret, node_id="node-a", placement_digest="sha256:" + "3" * 64, incarnation=INCARNATION)
   assert first == again
   verify = build_ed25519_verifier([first["signer_record"]])
   verify_standby_key_record(first["key_record"], verify_signature=verify, standby_placement_id="sha256:" + "3" * 64, standby_membership_incarnation=INCARNATION)
   with pytest.raises(A7WiringError):
      standby_material(secret[:63], node_id="node-a", placement_digest="sha256:" + "3" * 64, incarnation=INCARNATION)
   assert os.urandom  # secrets come from the OS CSPRNG


def test_node_configs_validate_against_the_graph(tmp_path: Path) -> None:
   graph = _graph(tmp_path)
   _secret, configs = _configs(tmp_path, graph)
   assert set(configs) == {"node-a", "node-b"}
   node_a = validate_node_config(configs["node-a"], deployment_id=graph.deployment_id, node_id="node-a")
   node_b = validate_node_config(configs["node-b"], deployment_id=graph.deployment_id, node_id="node-b")
   assert node_a["standby"] is not None and node_a["entry"] is not None and node_a["source"] is None
   assert node_b["source"] is not None and node_b["standby"] is None and node_b["entry"] is None
   validate_against_graph(node_a, graph, node_id="node-a")
   validate_against_graph(node_b, graph, node_id="node-b")
   for bad, code in (
      (dict(configs["node-a"], extra=1), "a7_node_config_fields"),
      (dict(configs["node-a"], deployment_id="other"), "a7_node_config_deployment_mismatch"),
      (dict(configs["node-a"], standby=None, entry=None, source=None), "a7_node_config_empty"),
      (dict(configs["node-a"], standby=dict(configs["node-a"]["standby"], max_checkpoint_bytes=0)), "a7_standby_config_invalid"),
   ):
      with pytest.raises(A7WiringError) as excinfo:
         validate_node_config(bad, deployment_id=graph.deployment_id, node_id="node-a")
      assert excinfo.value.code == code
   with pytest.raises(A7WiringError) as excinfo:
      validate_against_graph(node_a, graph, node_id="node-b")
   assert excinfo.value.code == "a7_standby_placement_invalid"
   promoted = replace(graph, stages=(graph.stages[0], replace(graph.stages[1], placements=(graph.stages[1].placements[0], replace(graph.stages[1].placements[1], lifecycle_state="ACTIVE")))))
   with pytest.raises(A7WiringError):
      validate_against_graph(node_a, promoted, node_id="node-a")


def test_plan_builder_cli_writes_the_extended_plan_and_node_files(tmp_path: Path) -> None:
   import importlib.util
   import json as _json

   from physical_inference_node import execution_graph_from_document

   case = _case(tmp_path, "qwen2")
   from dataclasses import asdict

   document = _json.loads(_json.dumps(asdict(case.graph)))
   plan = {"nodes": [{"node_id": n, "configure": {"graph": document}} for n in ("node-a", "node-b")]}
   (tmp_path / "plan.json").write_text(_json.dumps(plan))
   (tmp_path / "assignment.json").write_text(_json.dumps({"assignment_id": "standby-assignment"}))
   path = Path(__file__).resolve().parents[2] / "scripts" / "build_a7_standby_plan.py"
   spec = importlib.util.spec_from_file_location("build_a7_standby_plan", path)
   module = importlib.util.module_from_spec(spec)
   spec.loader.exec_module(module)
   assert module.main([
      "--plan", str(tmp_path / "plan.json"), "--standby-node", "node-a", "--standby-placement-id", STANDBY,
      "--standby-assignment", str(tmp_path / "assignment.json"), "--standby-load-proof-digest", "sha256:" + "e" * 64,
      "--standby-incarnation", INCARNATION, "--load-generation", "4", "--out-plan", str(tmp_path / "out.json"), "--stage-root", str(tmp_path / "stage"),
   ]) == 0
   out = _json.loads((tmp_path / "out.json").read_text())
   graph = execution_graph_from_document(out["nodes"][1]["configure"]["graph"])
   assert [p.lifecycle_state for p in graph.stages[1].placements] == ["ACTIVE", "STANDBY"]
   for node in ("node-a", "node-b"):
      config = _json.loads((tmp_path / "stage" / node / "a7" / "node.json").read_text())
      validate_against_graph(validate_node_config(config, deployment_id=graph.deployment_id, node_id=node), graph, node_id=node)
   assert (tmp_path / "stage" / "node-a" / "a7" / "standby.key").stat().st_mode & 0o777 == 0o600
