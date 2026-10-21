# SPDX-License-Identifier: AGPL-3.0-only
"""A7 slice 2: STANDBY placements and their qualification.

A STANDBY placement is admitted by graph validation, never chosen by ordinary
routing, bound only by a dedicated standby runtime port that refuses ordinary
work, and becomes the successor's stage only through an imported checkpoint.
The qualifier alone decides readiness from exact identity, load proofs, an
executed parity challenge and the reservation.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest

from mycelium_a7_standby_qualification import (
   StandbyQualificationError,
   parity_challenge_result,
   port_placement_identity,
   qualify_standby,
   standby_qualification_is_current,
   validate_standby_qualification,
)
from mycelium_router.contracts import PlacementEdge
from mycelium_router.numpy_runtime import NumpyRuntimeError, NumpyRuntimePort
from mycelium_router.payloads import encode_token_ids
from mycelium_router.routing import RoutePolicy
from mycelium_router.validation import ContractError, validate_execution_graph
import test_router_numpy_qwen_kv as kv_fixture
from mycelium_router.layer_builder import layer_load_proof_digest
from test_router_numpy_qwen_kv import _case, _reference
from test_router_numpy_runtime import _plain
from test_router_numpy_runtime import _work_item

_LEASE = 1_000_000_000_000.0
_MAX = 1 << 20
STANDBY_ID = "placement-001-standby"


def standby_case(root: Path, architecture: str):
   """The M23 two-stage case plus an independently acquired stage-1 standby on node-a.

   The standby gets its own assignment (bound to node-a) and its own load proof,
   exactly as the Planner/loader would provision it (spec section 4).
   """

   captured: dict = {}
   original = kv_fixture.compile_layer_assignments

   def capture(**kwargs):
      captured.update(kwargs)
      return original(**kwargs)

   kv_fixture.compile_layer_assignments = capture
   try:
      case = _case(root, architecture)
   finally:
      kv_fixture.compile_layer_assignments = original
   route = dict(captured["route_plan"])
   route["route"] = [dict(route["route"][0], node_id="node-z"), dict(route["route"][1], node_id="node-a")]
   route["node_order"] = ["node-z", "node-a"]
   standby_assignment = original(
      **dict(
         captured,
         route_plan=route,
         cache_roots={"node-z": str(root), "node-a": str(root)},
         runtime_by_node={node: captured["runtime_by_node"]["node-a"] for node in ("node-z", "node-a")},
      )
   )[1]
   report = kv_fixture.provision_assignment(standby_assignment, fetch_file=kv_fixture._LocalFetcher(root, case.graph.model_id), local_files_only=True)
   case.standby_loaded = kv_fixture.load_assignment_stage(standby_assignment, report, load_generation=23)
   case.standby_assignment = standby_assignment
   return case


def standby_graph(case):
   """The case graph plus the STANDBY placement for stage 1 on node-a (owner topology R7)."""

   graph = case.graph
   source = graph.stages[1].placements[0]
   standby = replace(
      source,
      placement_id=STANDBY_ID,
      node_id="node-a",
      assignment_id=case.standby_assignment["assignment_id"],
      load_proof_digest=layer_load_proof_digest(_plain(case.standby_loaded.proof)),
      lifecycle_state="STANDBY",
      runtime_endpoint="memory://node-a/standby",
   )
   stages = (graph.stages[0], replace(graph.stages[1], placements=(source, standby)))
   entry = graph.stages[0].placements[0].placement_id
   edges = graph.edges + (PlacementEdge(edge_id=f"forward:{entry}->{STANDBY_ID}", from_placement_id=entry, to_placement_id=STANDBY_ID, link_id="test:node-a->node-a"),)
   loopbacks = graph.loopback_edges + (PlacementEdge(edge_id=f"loopback:{STANDBY_ID}->{entry}", from_placement_id=STANDBY_ID, to_placement_id=entry, link_id="test:node-a->node-a"),)
   return replace(graph, stages=stages, edges=edges, loopback_edges=loopbacks)


def _ports(case, graph):
   entry = NumpyRuntimePort("node-a", graph, {graph.stages[0].placements[0].placement_id: case.loaded[0]}, decode_mode="stage_local_kv")
   source = NumpyRuntimePort("node-b", graph, {graph.stages[1].placements[0].placement_id: case.loaded[1]}, decode_mode="stage_local_kv")
   standby = NumpyRuntimePort("node-a", graph, {STANDBY_ID: case.standby_loaded}, decode_mode="stage_local_kv")
   return entry, source, standby


def _item(case, graph, stage_index, payload, *, placement_id=None, phase, path_id, path_attempt, token_index, position, tokens=1):
   item = _work_item(case, stage_index, payload, phase=phase, token_span=tokens, request_id="request:standby", path_id=path_id, token_index=token_index, position=position, lease_expires_at=_LEASE)
   placement = next(p for p in graph.stages[stage_index].placements if p.placement_id == (placement_id or item.placement_id))
   key = replace(item.batch_key, placement_id=placement.placement_id, assignment_id=placement.assignment_id, load_proof_digest=placement.load_proof_digest)
   return replace(item, placement_id=placement.placement_id, batch_key=key, path_attempt=path_attempt)


def _run(case, graph, ports, tokens, *, placements=(None, None), **kwargs):
   payload = encode_token_ids(tokens)
   result = None
   for index, port in enumerate(ports):
      result = port.execute(_item(case, graph, index, payload, placement_id=placements[index], tokens=len(tokens) if kwargs["phase"] != "DECODE" else 1, **kwargs))
      assert result.success, result.failure_reason
      payload = result.payload if result.payload is not None else payload
   return result


def test_graph_validation_admits_standby_but_never_a_standby_only_stage(tmp_path: Path) -> None:
   case = standby_case(tmp_path, "qwen2")
   graph = standby_graph(case)
   assert validate_execution_graph(graph) is graph
   only_standby = replace(graph, stages=(graph.stages[0], replace(graph.stages[1], placements=(graph.stages[1].placements[1],))))
   with pytest.raises(ContractError) as excinfo:
      validate_execution_graph(only_standby)
   assert excinfo.value.code in {"stage_without_serving_placement", "unknown_edge_placement"}
   lone = replace(graph.stages[1], placements=(replace(graph.stages[1].placements[1]),))
   bad = replace(graph, stages=(graph.stages[0], lone), edges=graph.edges[1:], loopback_edges=graph.loopback_edges[1:])
   with pytest.raises(ContractError) as excinfo:
      validate_execution_graph(bad)
   assert excinfo.value.code == "stage_without_serving_placement"
   unknown = replace(graph, stages=(graph.stages[0], replace(graph.stages[1], placements=(graph.stages[1].placements[0], replace(graph.stages[1].placements[1], lifecycle_state="WARMING")))))
   with pytest.raises(ContractError) as excinfo:
      validate_execution_graph(unknown)
   assert excinfo.value.code == "invalid_placement_lifecycle"


def test_ordinary_routing_never_selects_the_standby(tmp_path: Path) -> None:
   case = standby_case(tmp_path, "qwen2")
   graph = standby_graph(case)
   policy = RoutePolicy(scorer=None)
   build = SimpleNamespace(graph=graph, excluded_placements=frozenset(), excluded_devices=frozenset(), excluded_edges=frozenset())
   paths = policy._complete_routes(build, ())
   assert paths and all(STANDBY_ID not in path for path in paths)
   # Even with the source excluded, the standby is never an ordinary candidate.
   build.excluded_placements = frozenset({graph.stages[1].placements[0].placement_id})
   assert not policy._complete_routes(build, ())


def test_standby_port_binds_only_standby_and_refuses_ordinary_work(tmp_path: Path) -> None:
   case = standby_case(tmp_path, "qwen2")
   graph = standby_graph(case)
   entry, _source, standby = _ports(case, graph)
   assert standby.standby_placement_ids == frozenset({STANDBY_ID})
   assert entry.standby_placement_ids == frozenset()
   with pytest.raises(NumpyRuntimeError) as excinfo:
      NumpyRuntimePort("node-a", graph, {graph.stages[0].placements[0].placement_id: case.loaded[0], STANDBY_ID: case.standby_loaded}, decode_mode="stage_local_kv")
   assert excinfo.value.code == "standby_port_mixed_lifecycle"
   with pytest.raises(NumpyRuntimeError) as excinfo:
      NumpyRuntimePort("node-a", graph, {STANDBY_ID: case.standby_loaded}, decode_mode="complete_context_replay")
   assert excinfo.value.code == "standby_requires_stage_local_kv"
   hidden = entry.execute(_item(case, graph, 0, encode_token_ids((1, 7, 11)), phase="PREFILL", path_id="p", path_attempt=0, token_index=-1, position=0, tokens=3))
   assert hidden.success
   for phase, token_index in (("PREFILL", -1), ("RECOVERY_PREFILL", 0)):
      refused = standby.execute(_item(case, graph, 1, hidden.payload, placement_id=STANDBY_ID, phase=phase, path_id=f"s-{phase}", path_attempt=0, token_index=token_index, position=0, tokens=3))
      assert not refused.success and refused.failure_reason == "standby_placement_not_serving"
   no_kv = standby.execute(_item(case, graph, 1, hidden.payload, placement_id=STANDBY_ID, phase="DECODE", path_id="never-imported", path_attempt=0, token_index=1, position=3))
   assert not no_kv.success and no_kv.failure_reason in {"kv_state_missing", "decode_requires_single_token"}
   assert standby.kv_snapshot()["prefill_operation_count"] == 0


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_standby_continues_the_source_exactly_and_qualifies(tmp_path: Path, architecture: str) -> None:
   case = standby_case(tmp_path, architecture)
   graph = standby_graph(case)
   entry, source, standby = _ports(case, graph)
   prompt = (1, 7, 11, 5)
   first = _run(case, graph, (entry, source), prompt, phase="PREFILL", path_id="path:src", path_attempt=0, token_index=-1, position=0).token_id
   second = _run(case, graph, (entry, source), (first,), phase="DECODE", path_id="path:src", path_attempt=0, token_index=1, position=len(prompt)).token_id
   payload = source.export_kv_checkpoint("path:src")
   standby.import_kv_checkpoint(payload, request_id="request:standby", path_id="path:succ", path_attempt=1, placement_id=STANDBY_ID, lease_expires_at=_LEASE, expected_next_position=len(prompt) + 1, max_bytes=_MAX)
   entry.rekey_kv_path("path:src", new_path_id="path:succ", new_path_attempt=1, lease_expires_at=_LEASE)
   resumed = _run(case, graph, (entry, standby), (second,), placements=(None, STANDBY_ID), phase="DECODE", path_id="path:succ", path_attempt=1, token_index=2, position=len(prompt) + 1)
   assert resumed.token_id == _reference(case, prompt + (first, second))
   assert standby.kv_snapshot()["prefill_operation_count"] == 0

   # Parity challenge: the source decodes the same next position from its own KV.
   hidden = entry.export_kv_checkpoint("path:succ")
   assert hidden  # entry KV moved to the successor; the source still holds path:src
   reference_hidden = NumpyRuntimePort("node-a", graph, {graph.stages[0].placements[0].placement_id: case.loaded[0]}, decode_mode="stage_local_kv")
   _run(case, graph, (reference_hidden,), prompt, phase="PREFILL", path_id="path:ref", path_attempt=0, token_index=-1, position=0)
   step = reference_hidden.execute(_item(case, graph, 0, encode_token_ids((first,)), phase="DECODE", path_id="path:ref", path_attempt=0, token_index=1, position=len(prompt)))
   step = reference_hidden.execute(_item(case, graph, 0, encode_token_ids((second,)), phase="DECODE", path_id="path:ref", path_attempt=0, token_index=2, position=len(prompt) + 1))
   source_out = source.execute(_item(case, graph, 1, step.payload, phase="DECODE", path_id="path:src", path_attempt=0, token_index=2, position=len(prompt) + 1))
   assert source_out.success
   challenge = parity_challenge_result(challenge_id="c1", next_position=len(prompt) + 1, source_output=source_out.payload or bytes([source_out.token_id]), standby_output=resumed.payload or bytes([resumed.token_id]))
   assert challenge["passed"] is True

   src_identity = port_placement_identity(source, graph.stages[1].placements[0].placement_id)
   sb_identity = port_placement_identity(standby, STANDBY_ID)
   record = qualify_standby(source_identity=src_identity, standby_identity=sb_identity, parity_challenge=challenge, reserved_kv_bytes=4096, available_bytes=1 << 30, issued_at_unix_ms=1_000, ttl_ms=60_000)
   assert record["qualified"] is True and record["reason"] == "qualified"
   assert standby_qualification_is_current(record, now_unix_ms=1_500)
   assert not standby_qualification_is_current(record, now_unix_ms=61_000)
   # Privacy: no raw placement/node names in the record.
   text = repr(record)
   assert STANDBY_ID not in text and "node-a" not in text


def test_qualification_rejects_every_unearned_rung(tmp_path: Path) -> None:
   case = standby_case(tmp_path, "qwen2")
   graph = standby_graph(case)
   _entry, source, standby = _ports(case, graph)
   src = port_placement_identity(source, graph.stages[1].placements[0].placement_id)
   sb = port_placement_identity(standby, STANDBY_ID)
   good = parity_challenge_result(challenge_id="c", next_position=3, source_output=b"x", standby_output=b"x")
   bad = parity_challenge_result(challenge_id="c", next_position=3, source_output=b"x", standby_output=b"y")
   common = dict(reserved_kv_bytes=4096, available_bytes=1 << 20, issued_at_unix_ms=10, ttl_ms=1000)
   dtype = dict(sb, **{"runtime.dtype": "float16"})
   result = qualify_standby(source_identity=src, standby_identity=dtype, parity_challenge=good, **common)
   assert (result["qualified"], result["reason"], result["mismatched_fields"]) == (False, "identity_incompatible", ["runtime.dtype"])
   unknown = dict(sb, **{"runtime.backend_version": None})
   assert qualify_standby(source_identity=src, standby_identity=unknown, parity_challenge=good, **common)["unknown_fields"] == ["runtime.backend_version"]
   assert qualify_standby(source_identity=src, standby_identity=sb, parity_challenge=None, **common)["reason"] == "parity_challenge_missing"
   assert qualify_standby(source_identity=src, standby_identity=sb, parity_challenge=bad, **common)["reason"] == "parity_challenge_failed"
   assert qualify_standby(source_identity=src, standby_identity=sb, parity_challenge=good, **dict(common, available_bytes=10))["reason"] == "resource_insufficient"
   assert qualify_standby(source_identity=src, standby_identity=sb, parity_challenge=good, **dict(common, available_bytes=None))["reason"] == "resource_insufficient"
   with pytest.raises(StandbyQualificationError):
      qualify_standby(source_identity=src, standby_identity=src, parity_challenge=good, **common)
   record = qualify_standby(source_identity=src, standby_identity=sb, parity_challenge=good, **common)
   assert record["qualified"]
   forged = dict(record, qualified=True, reason="qualified", resource_ok=False)
   with pytest.raises(StandbyQualificationError):
      validate_standby_qualification(forged)
   assert not standby_qualification_is_current(dict(record, reserved_kv_bytes=1), now_unix_ms=20)
