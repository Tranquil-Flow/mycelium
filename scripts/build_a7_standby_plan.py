#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Extend a route run plan with an A7 stage standby and stage its A7 node files.

Network-free.  Inputs are the existing route run plan (whose ``nodes[*].configure
.graph`` the route sends to every node), the standby's own compiled assignment
(``mycelium_a7_standby_plan.compile_standby_artifacts``) and the standby's load
proof digest measured by loading that assignment on the standby host.  Outputs:

* ``--out-plan``: the run plan with the STANDBY placement in every node's graph;
* ``--stage-root``: ``<node_id>/a7/node.json`` per participating node and the
  standby's owner-only ``<standby_node>/a7/standby.key``.

The entry configuration names ``a7/qualification.json``; it must be produced by
the qualifier from measured identities and an executed parity challenge before
the node starts, or the entry refuses A7 coverage at configure time.
"""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
   sys.path.insert(0, str(ROOT))

from mycelium_a7_standby_plan import (
   extend_graph_with_standby,
   node_configs,
   plan_summary,
   require_distinct_failure_domains,
   update_plan_graphs,
   write_standby_secret,
)
from dataclasses import asdict

from physical_inference_node import execution_graph_from_document


def _write_private(path: Path, document: object) -> None:
   path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
   descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
   with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      json.dump(document, handle, sort_keys=True, indent=2)
      handle.write("\n")


#: LEGACY default (owner ruling R7: the standby is a second MLX placement on the
#: R7 fleet's entry Mac).  New runs name the standby through ``--role-binding``.
LEGACY_STANDBY_NODE = "node-0"


def resolve_standby_node(args: argparse.Namespace) -> str:
   """The standby member: the A7 binding's ``standby`` role, an explicit flag, or legacy."""

   if args.role_binding is None:
      return args.standby_node if args.standby_node is not None else LEGACY_STANDBY_NODE
   from mycelium_gate_requirements.binding import load_binding, role_node_ids

   nodes = role_node_ids(load_binding(args.role_binding, gate_id="A7"), "standby")
   if len(nodes) != 1:
      raise ValueError("standby_role_binding_incomplete")
   if args.standby_node is not None and args.standby_node != nodes[0]:
      raise ValueError("standby_node_conflicts_with_role_binding")
   return nodes[0]


def main(argv: list[str] | None = None) -> int:
   parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
   parser.add_argument("--plan", type=Path, required=True)
   parser.add_argument("--stage-index", type=int, default=1)
   parser.add_argument("--standby-node", default=None,
                       help=f"standby member node id (default: the --role-binding standby, else legacy {LEGACY_STANDBY_NODE})")
   parser.add_argument("--role-binding", type=Path, default=None,
                       help="verified role binding of the pinned A7 requirements spec")
   parser.add_argument("--standby-placement-id", required=True)
   parser.add_argument("--standby-assignment", type=Path, required=True)
   parser.add_argument("--standby-load-proof-digest", required=True)
   parser.add_argument("--standby-incarnation", required=True)
   parser.add_argument("--manifest-file", default="model-manifest.json")
   parser.add_argument("--load-generation", type=int, required=True)
   parser.add_argument("--out-plan", type=Path, required=True)
   parser.add_argument("--stage-root", type=Path, required=True)
   args = parser.parse_args(argv)
   args.standby_node = resolve_standby_node(args)

   plan = json.loads(args.plan.read_text(encoding="utf-8"))
   assignment = json.loads(args.standby_assignment.read_text(encoding="utf-8"))
   graph = execution_graph_from_document(plan["nodes"][0]["configure"]["graph"])
   extended = extend_graph_with_standby(
      graph,
      stage_index=args.stage_index,
      placement_id=args.standby_placement_id,
      node_id=args.standby_node,
      assignment_id=assignment["assignment_id"],
      load_proof_digest=args.standby_load_proof_digest,
   )
   require_distinct_failure_domains(extended, stage_index=args.stage_index, standby_placement_id=args.standby_placement_id)
   secret = write_standby_secret(args.stage_root / args.standby_node / "a7" / "standby.key")
   configs = node_configs(
      deployment_id=extended.deployment_id,
      graph=extended,
      stage_index=args.stage_index,
      standby_placement_id=args.standby_placement_id,
      standby_secret=secret,
      standby_incarnation=args.standby_incarnation,
      standby_files={
         "assignment_file": "a7/standby-assignment.json",
         "manifest_file": args.manifest_file,
         "stage_pack_file": "a7/standby-stage-pack.json",
         "secret_file": "a7/standby.key",
      },
      standby_load_generation=args.load_generation,
      qualification_file="a7/qualification.json",
   )
   for node_id, config in configs.items():
      _write_private(args.stage_root / node_id / "a7" / "node.json", config)
   _write_private(args.out_plan, update_plan_graphs(plan, json.loads(json.dumps(asdict(extended)))))
   print(json.dumps(plan_summary(extended, stage_index=args.stage_index, standby_placement_id=args.standby_placement_id), sort_keys=True))
   return 0


if __name__ == "__main__":
   raise SystemExit(main())
