#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A7 operator-plan binder steps for the run-day window (network use only where stated).

Subcommands:

``topology``  (offline)
    The ``mycelium.qwen_live_topology.v1`` document for an A7-window deployment,
    derived from a freshly bound base plan (peers, endpoint ids and membership
    generations from its signed offers) with per-node overrides: staging root,
    python, sidecar and runtime backend.  The A7 window runs the laptop source
    stage on MLX; nothing here changes any other gate's plan.
``extend``  (offline; loads the standby stage locally)
    ``mycelium_a7_operator_plan.extend_operator_plan`` for one variant.
``bind``  (ssh to re-measure run-scoped peer identities)
    Re-sign the extended plan's offers with the seed (the run-day bind path).
``parity-config``  (offline)
    The cross-host parity configuration for an extended staging plan: local
    workers for the entry and standby, an ssh worker in the source host's
    staged candidate checkout.
``qualify-identities``  (runs the source worker over ssh)
    The incompatible case's qualification from the two measured identities.
"""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
   sys.path.insert(0, str(ROOT))

TOPOLOGY_PROTOCOL = "mycelium.qwen_live_topology.v1"


def _read(path: Path) -> Any:
   return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_private(path: Path, document: Any) -> None:
   path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
   descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
   with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      json.dump(document, handle, sort_keys=True, indent=2)
      handle.write("\n")


def _pairs(values: list[str] | None, flag: str) -> dict[str, str]:
   result: dict[str, str] = {}
   for value in values or ():
      node, separator, item = value.partition("=")
      if not separator or not node or not item or node in result:
         raise SystemExit(f"{flag} expects unique NODE=VALUE")
      result[node] = item
   return result


def topology(args: argparse.Namespace) -> dict[str, Any]:
   plan = _read(args.base_plan)
   controller = plan["controller"]
   run_nodes = {node["node_id"]: node for node in controller["run_plan"]["nodes"]}
   endpoints: dict[str, str] = {}
   generations: dict[str, int] = {}
   for envelope in controller["membership_snapshot"]["assignment_offers"]:
      message = envelope["message"]
      generations[message["recipient_node_id"]] = int(message["generation"])
      for record in message["peer_endpoint_records"]:
         endpoints.setdefault(record["node_id"], record["endpoint_id"])
   overrides = {
      "staging_root": _pairs(args.staging_root, "--staging-root"),
      "python_executable": _pairs(args.python, "--python"),
      "sidecar_binary": _pairs(args.sidecar, "--sidecar"),
      "runtime_backend": _pairs(args.backend, "--backend"),
   }
   nodes = []
   for peer in sorted(controller["peers"], key=lambda item: item["node_id"]):
      node_id = peer["node_id"]
      run_node = run_nodes[node_id]
      node = {
         "node_id": node_id,
         "process_transport": peer["process_transport"],
         "ssh_target": peer["ssh_target"],
         "ssh_identity_file": peer.get("ssh_identity_file"),
         "staging_root": overrides["staging_root"].get(node_id, peer["staging_root"]),
         "python_executable": overrides["python_executable"].get(node_id, run_node["python_executable"]),
         "sidecar_binary": overrides["sidecar_binary"].get(node_id, run_node["sidecar_binary"]),
         "endpoint_secret_file": run_node["endpoint_secret_file"],
         "endpoint_id": endpoints[node_id],
         "membership_generation": generations[node_id],
         "runtime_backend": overrides["runtime_backend"].get(node_id, "mlx"),
      }
      nodes.append(node)
   unknown = {node for values in overrides.values() for node in values} - {node["node_id"] for node in nodes}
   if unknown:
      raise SystemExit(f"unknown override node(s): {sorted(unknown)}")
   return {
      "protocol": TOPOLOGY_PROTOCOL,
      "_runday_notes": "A7 window deployment topology (derived from a freshly bound base plan; A7-window only).",
      "nodes": nodes,
   }


def extend(args: argparse.Namespace) -> dict[str, Any]:
   from mycelium_a7_operator_plan import extend_operator_plan

   plan, summary = extend_operator_plan(
      _read(args.plan),
      out_bundle=args.out_bundle,
      variant=args.variant,
      source_node_id=args.source_node,
      standby_node_id=args.standby_node,
      qualification=None if args.qualification is None else _read(args.qualification),
   )
   _write_private(args.out_plan, plan)
   if args.summary_out is not None:
      _write_private(args.summary_out, summary)
   return summary


def bind(args: argparse.Namespace) -> dict[str, Any]:
   from mycelium_a7_operator_plan import bind_extended_plan

   bound = bind_extended_plan(_read(args.plan), seed_root=args.seed_root, now=time.time(), refresh_identities=not args.no_identity_refresh)
   _write_private(args.out, bound)
   return {"stage": "bound", "output": str(args.out), "plan_id": bound["plan_id"], "run_id": bound["run_id"]}


def parity_config(args: argparse.Namespace) -> dict[str, Any]:
   from mycelium_a7_operator_plan import parity_worker_argv
   from mycelium_a7_parity_measurement import CONFIG_PROTOCOL, validate_config
   from physical_inference_node import execution_graph_from_document

   plan = _read(args.plan)
   summary = _read(args.summary)
   controller = plan["controller"]
   nodes = {node["node_id"]: node for node in controller["run_plan"]["nodes"]}
   peers = {peer["node_id"]: peer for peer in controller["peers"]}
   graph = execution_graph_from_document(next(iter(nodes.values()))["configure"]["graph"])
   entry_node = controller["run_plan"]["entry_node_id"]
   source_node, standby_node = summary["source_node_id"], summary["standby_node_id"]
   entry = graph.stages[0].placements[0]
   source = next(p for p in graph.stages[summary["stage_index"]].placements if p.lifecycle_state == "ACTIVE")

   def worker(node_id: str, placement_id: str, assignment: str, pack: str) -> list[str]:
      remote = peers[node_id].get("process_transport") == "ssh"
      return parity_worker_argv(
         plan,
         role_node=node_id,
         placement_id=placement_id,
         assignment_file=assignment,
         stage_pack_file=pack,
         python=args.remote_python if remote else args.local_python,
         code_root=args.remote_code_root if remote else args.local_code_root,
         ssh=None if not remote else {"target": peers[node_id]["ssh_target"], "identity_file": peers[node_id].get("ssh_identity_file")},
      )

   config = {
      "protocol": CONFIG_PROTOCOL,
      "source_node_id": source_node,
      "standby_node_id": standby_node,
      "prompt_token_ids": list(controller["run_plan"]["request"]["prompt_token_ids"]),
      "challenge_index": args.challenge_index,
      "workers": {
         "entry": worker(entry_node, entry.placement_id, nodes[entry_node]["configure"]["assignment_file"], nodes[entry_node]["configure"]["stage_pack_file"]),
         "source": worker(source_node, source.placement_id, nodes[source_node]["configure"]["assignment_file"], nodes[source_node]["configure"]["stage_pack_file"]),
         "standby": worker(standby_node, summary["standby_placement_id"], "a7/standby-assignment.json", "a7/standby-stage-pack.json"),
      },
      "max_checkpoint_bytes": args.max_checkpoint_bytes,
      "worker_timeout_s": args.worker_timeout_s,
      "qualification": {"out": str(args.qualification_out), "reserved_kv_bytes": args.reserved_kv_bytes, "ttl_ms": args.ttl_ms},
   }
   validate_config(config)
   _write_private(args.out, config)
   return {"stage": "parity_config", "output": str(args.out)}


def qualify_identities(args: argparse.Namespace) -> dict[str, Any]:
   from mycelium_a7_parity_measurement import measure_identity_qualification

   config = _read(args.config)
   record = measure_identity_qualification(config)
   _write_private(Path(config["qualification"]["out"]), record)
   return {"stage": "qualified_from_identities", "qualified": record["qualified"], "reason": record["reason"]}


def main(argv: list[str] | None = None) -> int:
   parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
   commands = parser.add_subparsers(dest="command", required=True)
   p = commands.add_parser("topology")
   p.add_argument("--base-plan", type=Path, required=True)
   p.add_argument("--out", type=Path, required=True)
   p.add_argument("--staging-root", action="append")
   p.add_argument("--python", action="append")
   p.add_argument("--sidecar", action="append")
   p.add_argument("--backend", action="append")
   p = commands.add_parser("extend")
   p.add_argument("--plan", type=Path, required=True)
   p.add_argument("--variant", required=True)
   p.add_argument("--source-node", required=True)
   p.add_argument("--standby-node", required=True)
   p.add_argument("--qualification", type=Path)
   p.add_argument("--out-bundle", type=Path, required=True)
   p.add_argument("--out-plan", type=Path, required=True)
   p.add_argument("--summary-out", type=Path)
   p = commands.add_parser("bind")
   p.add_argument("--plan", type=Path, required=True)
   p.add_argument("--seed-root", type=Path, required=True)
   p.add_argument("--out", type=Path, required=True)
   p.add_argument("--no-identity-refresh", action="store_true")
   p = commands.add_parser("parity-config")
   p.add_argument("--plan", type=Path, required=True)
   p.add_argument("--summary", type=Path, required=True)
   p.add_argument("--out", type=Path, required=True)
   p.add_argument("--qualification-out", type=Path, required=True)
   p.add_argument("--local-python", required=True)
   p.add_argument("--local-code-root", required=True)
   p.add_argument("--remote-python", required=True)
   p.add_argument("--remote-code-root", required=True)
   p.add_argument("--challenge-index", type=int, default=1)
   p.add_argument("--max-checkpoint-bytes", type=int, default=64 * 1024 * 1024)
   p.add_argument("--worker-timeout-s", type=float, default=600.0)
   p.add_argument("--reserved-kv-bytes", type=int, default=64 * 1024 * 1024)
   p.add_argument("--ttl-ms", type=int, default=12 * 60 * 60 * 1000)
   p = commands.add_parser("qualify-identities")
   p.add_argument("--config", type=Path, required=True)
   args = parser.parse_args(argv)
   if args.command == "topology":
      document = topology(args)
      _write_private(args.out, document)
      result: dict[str, Any] = {"stage": "topology", "output": str(args.out), "backends": {n["node_id"]: n["runtime_backend"] for n in document["nodes"]}}
   else:
      result = {"extend": extend, "bind": bind, "parity-config": parity_config, "qualify-identities": qualify_identities}[args.command](args)
   print(json.dumps(result, sort_keys=True))
   return 0


if __name__ == "__main__":
   raise SystemExit(main())
